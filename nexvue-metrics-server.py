#!/usr/bin/env python3
"""
nexvue-metrics-server.py — usage/analytics metrics collector, API, and
dashboard server for NexVUE.

This is USAGE ANALYTICS, not health/uptime monitoring (that is CheckMK's job
per the project roadmap) — it answers "how much bandwidth did we serve last
week" and "was this input locked all night," not "is the service up."

Polls two existing NexVUE services on an interval and stores time-series
samples in SQLite (stdlib only — no pip, no new dependency):
  - MediaMTX Control API   -> per-path bandwidth (bytesSent delta), viewer
                              counts (readers), active-stream (ready) state
  - nexvue-status daemon   -> per-input signal lock, detected format,
                              reference lock/format

Serves:
  GET /              -> the dashboard (nexvue-metrics-dashboard.html, must
                        sit next to this script)
  GET /api/history    -> combined JSON time series for the requested window
                        (?hours=N, default 24)

Old samples are pruned hourly (NEXVUE_METRICS_RETENTION_DAYS, default 30) so
the SQLite file does not grow unbounded.

Runs as systemd service nexvue-metrics.service, port 9999.
"""

import json
import logging
import os
import socket
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

logging.basicConfig(level=logging.INFO, format="[nexvue-metrics] %(message)s")
log = logging.getLogger(__name__)

# ---- Configuration (env-overridable via systemd Environment= lines) -------------
# Loopback-only by default: metrics is meant to be reached via an Apache
# reverse proxy on the already-open 443, not by exposing this port directly
# through the firewall. Set NEXVUE_METRICS_BIND=0.0.0.0 to allow direct
# external access instead (then you DO need a firewall rule for this port —
# see the README firewall section for the tradeoff).
LISTEN_ADDR = (os.environ.get("NEXVUE_METRICS_BIND", "127.0.0.1"), 9999)
DB_PATH = os.environ.get("NEXVUE_METRICS_DB", "/var/lib/nexvue/metrics.db")
POLL_INTERVAL_S = float(os.environ.get("NEXVUE_METRICS_POLL_INTERVAL_S", "15"))
RETENTION_DAYS = float(os.environ.get("NEXVUE_METRICS_RETENTION_DAYS", "30"))
PRUNE_EVERY_S = 3600  # retention sweep runs hourly, not every poll

MEDIAMTX_API_URL = os.environ.get("NEXVUE_MEDIAMTX_API_URL", "https://127.0.0.1:9997")
STATUS_DAEMON_URL = os.environ.get("NEXVUE_STATUS_URL", "https://127.0.0.1:9998")
FETCH_TIMEOUT_S = 5.0

# TLS for THIS service's own :9999 API/dashboard (optional; matches the
# pattern in nexvue-status-server.py). Independent of the loopback calls
# above, which have their own unverified-cert handling below.
TLS_CERT = os.environ.get("NEXVUE_METRICS_TLS_CERT", "")
TLS_KEY = os.environ.get("NEXVUE_METRICS_TLS_KEY", "")

DASHBOARD_HTML_PATH = Path(__file__).with_name("nexvue-metrics-dashboard.html")

_db_lock = threading.Lock()
# In-memory cumulative-counter baseline per channel, so MediaMTX's running
# byte totals can be turned into instantaneous bandwidth between samples.
# Touched only from the single poll_loop thread — no lock needed.
_prev_bytes: dict = {}


def _unverified_ssl_context() -> "ssl.SSLContext | None":
    """
    Context for calling our OWN loopback services (MediaMTX API, status
    daemon), which may be using a self-signed cert. Skipping verification is
    acceptable here specifically because this traffic never leaves 127.0.0.1
    — it is not fetching anything from the outside network. Do not reuse
    this pattern for any request that isn't strictly loopback-to-self.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_json(url: str) -> dict:
    ctx = _unverified_ssl_context() if url.startswith("https://") else None
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S, context=ctx) as resp:
        return json.loads(resp.read().decode())


def _compute_bandwidth_bps(prev_ts: int, prev_bytes: int, now_ts: int, now_bytes: int) -> float:
    """Bits/sec between two cumulative byte-counter samples. Returns 0 for a
    non-positive interval or a counter reset (e.g. the stream restarted and
    MediaMTX's counter started over) rather than a nonsensical negative rate."""
    dt = now_ts - prev_ts
    if dt <= 0 or now_bytes < prev_bytes:
        return 0.0
    return (now_bytes - prev_bytes) * 8 / dt


# ---- Database --------------------------------------------------------------------

def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # WAL mode lets the background writer and concurrent HTTP-read
        # connections coexist without "database is locked" contention.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER NOT NULL,
                channel TEXT NOT NULL,
                bandwidth_bps REAL,
                readers INTEGER,
                ready INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_samples_channel_ts ON samples(channel, ts);
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

            CREATE TABLE IF NOT EXISTS totals (
                ts INTEGER NOT NULL PRIMARY KEY,
                active_streams INTEGER,
                total_readers INTEGER,
                total_bandwidth_bps REAL
            );

            CREATE TABLE IF NOT EXISTS input_status (
                ts INTEGER NOT NULL,
                device_index INTEGER NOT NULL,
                card_name TEXT,
                input_locked INTEGER,
                input_mode TEXT,
                reference_locked INTEGER,
                reference_mode TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_input_status_dev_ts ON input_status(device_index, ts);

            -- One row per WebRTC viewer session (state="read" only — this
            -- excludes our own encoders' "publish" sessions). Upserted by
            -- session_id each poll: first_seen is set once, last_seen and
            -- bytes_sent update every cycle the session is still active, so
            -- a single row gives the full lifecycle of one viewer watching
            -- one channel — exactly what "drill down on IP + channel" needs,
            -- without one row per poll cycle per viewer bloating the table.
            CREATE TABLE IF NOT EXISTS viewer_sessions (
                session_id TEXT PRIMARY KEY,
                remote_addr TEXT,
                channel TEXT,
                user_agent TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                bytes_sent INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_viewer_sessions_channel ON viewer_sessions(channel);
            CREATE INDEX IF NOT EXISTS idx_viewer_sessions_remote ON viewer_sessions(remote_addr);
            CREATE INDEX IF NOT EXISTS idx_viewer_sessions_last_seen ON viewer_sessions(last_seen);
        """)
    log.info("database ready at %s", DB_PATH)


def prune_old_samples() -> None:
    cutoff = int(time.time() - RETENTION_DAYS * 86400)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        for table in ("samples", "totals", "input_status"):
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        # viewer_sessions has no `ts` column (it's a lifecycle row, not a
        # timestamped sample) — prune on last_seen instead.
        conn.execute("DELETE FROM viewer_sessions WHERE last_seen < ?", (cutoff,))
        conn.commit()
    log.info("pruned samples older than %s (%.0f day retention)", cutoff, RETENTION_DAYS)


def query_history(since_ts: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        samples = [dict(r) for r in conn.execute(
            "SELECT ts, channel, bandwidth_bps, readers, ready FROM samples "
            "WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
        )]
        totals = [dict(r) for r in conn.execute(
            "SELECT ts, active_streams, total_readers, total_bandwidth_bps FROM totals "
            "WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
        )]
        input_status = [dict(r) for r in conn.execute(
            "SELECT ts, device_index, card_name, input_locked, input_mode, "
            "reference_locked, reference_mode FROM input_status "
            "WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
        )]
    return {"samples": samples, "totals": totals, "input_status": input_status}


def query_viewer_sessions(since_ts: int) -> list:
    """Viewer drill-down: sessions still active OR that ended within the
    window, newest first. duration_s is computed here rather than stored,
    since last_seen keeps moving for an active session."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT session_id, remote_addr, channel, user_agent, first_seen, "
            "last_seen, bytes_sent FROM viewer_sessions "
            "WHERE last_seen >= ? ORDER BY last_seen DESC", (since_ts,)
        )]
    now = time.time()
    for r in rows:
        r["duration_s"] = round(r["last_seen"] - r["first_seen"], 1)
        r["active"] = (now - r["last_seen"]) < (POLL_INTERVAL_S * 3)
    return rows


# ---- Collection loop ---------------------------------------------------------------

def poll_once() -> None:
    now = int(time.time())

    # --- MediaMTX: bandwidth, viewers, active-stream state, per path ---
    try:
        data = _fetch_json(f"{MEDIAMTX_API_URL}/v3/paths/list")
        rows = []
        active_streams = 0
        total_readers = 0
        total_bw = 0.0
        for item in data.get("items", []):
            name = item.get("name", "")
            ready = bool(item.get("ready"))
            readers = len(item.get("readers", []))
            bytes_sent = int(item.get("bytesSent", 0))

            bw = 0.0
            prev = _prev_bytes.get(name)
            if prev:
                bw = _compute_bandwidth_bps(prev[0], prev[1], now, bytes_sent)
            _prev_bytes[name] = (now, bytes_sent)

            rows.append((now, name, bw, readers, int(ready)))
            if ready:
                active_streams += 1
            total_readers += readers
            total_bw += bw

        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT INTO samples (ts, channel, bandwidth_bps, readers, ready) VALUES (?,?,?,?,?)",
                rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO totals (ts, active_streams, total_readers, total_bandwidth_bps) "
                "VALUES (?,?,?,?)",
                (now, active_streams, total_readers, total_bw),
            )
            conn.commit()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
        log.warning("MediaMTX API poll failed: %s", exc)

    # --- MediaMTX: per-viewer session drill-down (IP + channel + duration) ---
    # /v3/paths/list only gives a reader COUNT per path; the actual remote
    # address and per-session detail live on this separate endpoint.
    try:
        data = _fetch_json(f"{MEDIAMTX_API_URL}/v3/webrtcsessions/list")
        rows = []
        for item in data.get("items", []):
            # state is "read" (viewer) or "publish" (our own encoders) —
            # only viewers are relevant for this drill-down.
            if item.get("state") != "read":
                continue
            rows.append((
                item.get("id", ""),
                item.get("remoteAddr", ""),
                item.get("path", ""),
                item.get("userAgent", ""),
                now,  # first_seen — only used on first INSERT, see ON CONFLICT below
                now,  # last_seen — updated every poll while the session persists
                int(item.get("outboundBytes", 0)),
            ))
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT INTO viewer_sessions "
                "(session_id, remote_addr, channel, user_agent, first_seen, last_seen, bytes_sent) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "last_seen=excluded.last_seen, bytes_sent=excluded.bytes_sent",
                rows,
            )
            conn.commit()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
        log.warning("MediaMTX webrtcsessions poll failed: %s", exc)

    # --- Status daemon: per-input lock/format history ---
    try:
        data = _fetch_json(f"{STATUS_DAEMON_URL}/status")
        if not data.get("stale"):
            rows = [
                (
                    now,
                    d.get("index"),
                    d.get("name", ""),
                    int(bool(d.get("input_locked"))),
                    d.get("input_mode", "unknown"),
                    int(bool(d.get("reference_locked"))),
                    d.get("reference_mode", "unknown"),
                )
                for d in data.get("devices", [])
            ]
            with _db_lock, sqlite3.connect(DB_PATH) as conn:
                conn.executemany(
                    "INSERT INTO input_status (ts, device_index, card_name, input_locked, "
                    "input_mode, reference_locked, reference_mode) VALUES (?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
        log.warning("status daemon poll failed: %s", exc)


def poll_loop() -> None:
    last_prune = 0.0
    while True:
        poll_once()
        if time.time() - last_prune > PRUNE_EVERY_S:
            try:
                prune_old_samples()
            except OSError as exc:
                log.warning("prune failed: %s", exc)
            last_prune = time.time()
        time.sleep(POLL_INTERVAL_S)


# ---- HTTP: API + dashboard -----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/api/history":
            qs = parse_qs(urlsplit(self.path).query)
            try:
                hours = float(qs.get("hours", ["24"])[0])
            except ValueError:
                hours = 24.0
            since_ts = int(time.time() - hours * 3600)
            try:
                self._send_json(query_history(since_ts))
            except sqlite3.Error as exc:
                self._send_json({"error": str(exc)}, status=500)
            return

        if path == "/api/viewers":
            qs = parse_qs(urlsplit(self.path).query)
            try:
                hours = float(qs.get("hours", ["24"])[0])
            except ValueError:
                hours = 24.0
            since_ts = int(time.time() - hours * 3600)
            try:
                self._send_json({"sessions": query_viewer_sessions(since_ts)})
            except sqlite3.Error as exc:
                self._send_json({"error": str(exc)}, status=500)
            return

        if path == "/":
            try:
                body = DASHBOARD_HTML_PATH.read_bytes()
            except OSError:
                self.send_error(404, "dashboard html not found next to this script")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404)

    def log_message(self, *_args) -> None:
        pass  # journald noise control; failures still surface via logging


def main() -> None:
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()

    server = ThreadingHTTPServer(LISTEN_ADDR, Handler)
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(certfile=TLS_CERT, keyfile=TLS_KEY)
        except (ssl.SSLError, FileNotFoundError, PermissionError) as exc:
            log.error("TLS cert/key load failed (%s) — falling back to plain HTTP", exc)
        else:
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            scheme = "https"
    elif TLS_CERT or TLS_KEY:
        log.warning("only one of NEXVUE_METRICS_TLS_CERT/_KEY set — need both; serving plain HTTP")

    log.info("serving %s on %s:%d (poll every %.0fs, retain %.0fd, db=%s)",
             scheme, *LISTEN_ADDR, POLL_INTERVAL_S, RETENTION_DAYS, DB_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
