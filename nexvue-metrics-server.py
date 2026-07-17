#!/usr/bin/env python3
"""
nexvue-metrics-server.py — usage/analytics metrics COLLECTOR for NexVUE.

This process does NOT listen on any network port or serve any HTTP at all —
it is a pure background poller that writes time-series samples into SQLite.
Reading the data back out is Apache + PHP's job (see nexvue-metrics.php),
querying the same SQLite file directly. That split means this collector has
zero firewall/port exposure of any kind: nothing to open, nothing to proxy,
no WebSocket, nothing for a security team to review on this side at all.

This is USAGE ANALYTICS, not health/uptime monitoring (that is CheckMK's job
per the project roadmap) — it answers "how much bandwidth did we serve last
week," "was this input locked all night," and "which IP was watching
channel 2 at 3am," not "is the service up right now."

Polls existing NexVUE services (and local /proc) on an interval and stores
time-series samples in SQLite (stdlib `sqlite3` — no pip, no new dependency):
  - MediaMTX Control API   -> per-path bandwidth (bytesSent delta), viewer
                              counts (readers), active-stream (ready) state
  - MediaMTX Control API   -> per-VIEWER session detail: remote IP, channel,
                              user (once Phase 2 auth exists), byte counts
                              (/v3/webrtcsessions/list — a separate, richer
                              endpoint from the paths list above)
  - nexvue-status daemon   -> per-input signal lock, detected format,
                              reference lock/format
  - /proc/stat, meminfo, loadavg -> host CPU %, memory used/total, load1
  - intel_gpu_top -J (optional) -> iGPU Video/Render/VideoEnhance busy %
                              and frequency (capacity correlation for
                              Metrics — not a CheckMK substitute)

Old samples are pruned hourly (NEXVUE_METRICS_RETENTION_DAYS, default 30) so
the SQLite file does not grow unbounded.

Runs as systemd service nexvue-metrics.service. No LISTEN_ADDR, no port —
there is genuinely nothing here to open a firewall rule for.
"""

import json
import logging
import os
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[nexvue-metrics] %(message)s")
log = logging.getLogger(__name__)

# ---- Configuration (env-overridable via systemd Environment= lines) -------------
DB_PATH = os.environ.get("NEXVUE_METRICS_DB", "/var/lib/nexvue/metrics.db")
POLL_INTERVAL_S = float(os.environ.get("NEXVUE_METRICS_POLL_INTERVAL_S", "15"))
RETENTION_DAYS = float(os.environ.get("NEXVUE_METRICS_RETENTION_DAYS", "30"))
PRUNE_EVERY_S = 3600  # retention sweep runs hourly, not every poll

MEDIAMTX_API_URL = os.environ.get("NEXVUE_MEDIAMTX_API_URL", "https://127.0.0.1:9997")
# When unset, try HTTP then HTTPS (status TLS is optional; matches nexvue-status.php).
# When set, use that single base URL only.
_STATUS_URL_ENV = os.environ.get("NEXVUE_STATUS_URL", "").strip()
FETCH_TIMEOUT_S = 5.0

_db_lock = threading.Lock()
# In-memory cumulative-counter baseline per channel, so MediaMTX's running
# byte totals can be turned into instantaneous bandwidth between samples.
# Touched only from the single poll_loop thread — no lock needed.
_prev_bytes: dict = {}
# Previous /proc/stat counters for CPU % between polls.
_prev_cpu: tuple | None = None
# Rate-limit intel_gpu_top failure logs (seconds between warnings).
_gpu_warn_last = 0.0
_GPU_WARN_EVERY_S = 300.0
INTEL_GPU_TOP = os.environ.get("NEXVUE_INTEL_GPU_TOP", "intel_gpu_top")
INTEL_GPU_TOP_TIMEOUT_S = float(os.environ.get("NEXVUE_INTEL_GPU_TOP_TIMEOUT_S", "2.5"))


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


def _status_base_urls() -> list[str]:
    """Base URLs for the status daemon (no trailing path). Env override wins."""
    if _STATUS_URL_ENV:
        return [_STATUS_URL_ENV.rstrip("/")]
    return ["http://127.0.0.1:9998", "https://127.0.0.1:9998"]


def _fetch_json_first(urls: list[str]) -> dict:
    """Try each URL in order; return the first successful JSON body.

    Raises the last exception if every candidate fails — callers log once.
    """
    last_exc: BaseException | None = None
    for url in urls:
        try:
            return _fetch_json(url)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise urllib.error.URLError("no status URL candidates")


def _compute_bandwidth_bps(prev_ts: int, prev_bytes: int, now_ts: int, now_bytes: int) -> float:
    """Bits/sec between two cumulative byte-counter samples. Returns 0 for a
    non-positive interval or a counter reset (e.g. the stream restarted and
    MediaMTX's counter started over) rather than a nonsensical negative rate."""
    dt = now_ts - prev_ts
    if dt <= 0 or now_bytes < prev_bytes:
        return 0.0
    return (now_bytes - prev_bytes) * 8 / dt


def _parse_proc_stat_cpu(text: str) -> tuple[int, int]:
    """Return (idle_ticks, total_ticks) from a /proc/stat blob. First line
    must be the aggregate `cpu ` row. Raises ValueError if malformed."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            # cpu user nice system idle iowait irq softirq steal guest guest_nice
            nums = [int(x) for x in parts[1:]]
            if len(nums) < 4:
                raise ValueError("cpu line too short")
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
            total = sum(nums)
            return idle, total
    raise ValueError("no cpu line in /proc/stat")


def _compute_cpu_pct(prev_idle: int, prev_total: int, idle: int, total: int) -> float:
    """Percent non-idle CPU between two /proc/stat samples (0–100)."""
    d_total = total - prev_total
    d_idle = idle - prev_idle
    if d_total <= 0:
        return 0.0
    busy = d_total - d_idle
    if busy < 0:
        return 0.0
    return 100.0 * busy / d_total


def _parse_meminfo(text: str) -> tuple[int, int]:
    """Return (mem_used_bytes, mem_total_bytes) from /proc/meminfo text.
    Prefers MemAvailable for used = total − available."""
    vals: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.split()
        if not parts:
            continue
        # Values are kB
        vals[key] = int(parts[0]) * 1024
    total = vals.get("MemTotal", 0)
    available = vals.get("MemAvailable", vals.get("MemFree", 0))
    used = max(0, total - available)
    return used, total


def _parse_loadavg(text: str) -> float:
    """1-minute load average from /proc/loadavg."""
    return float(text.split()[0])


def sample_host() -> tuple[float | None, int, int, float]:
    """Read host CPU/memory/load. cpu_pct is None on the first sample (no
    prior /proc/stat baseline yet)."""
    global _prev_cpu
    cpu_pct: float | None = None
    try:
        stat_text = Path("/proc/stat").read_text(encoding="utf-8")
        idle, total = _parse_proc_stat_cpu(stat_text)
        if _prev_cpu is not None:
            cpu_pct = _compute_cpu_pct(_prev_cpu[0], _prev_cpu[1], idle, total)
        _prev_cpu = (idle, total)
    except (OSError, ValueError) as exc:
        log.warning("host cpu sample failed: %s", exc)

    mem_used, mem_total = 0, 0
    try:
        mem_used, mem_total = _parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("host mem sample failed: %s", exc)

    load1 = 0.0
    try:
        load1 = _parse_loadavg(Path("/proc/loadavg").read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError) as exc:
        log.warning("host loadavg sample failed: %s", exc)

    return cpu_pct, mem_used, mem_total, load1


def _classify_gpu_engine(name: str) -> str | None:
    """Map an intel_gpu_top engine name to video | enhance | render."""
    n = name.lower().replace(" ", "")
    if "videoenhance" in n or "vebox" in n or n.startswith("vecs"):
        return "enhance"
    if n.startswith("video") or n.startswith("vcs") or "/vcs" in n:
        return "video"
    if "render" in n or n.startswith("rcs") or "3d" in n:
        return "render"
    return None


def _parse_intel_gpu_top_json(text: str) -> dict:
    """
    Parse one sample from intel_gpu_top -J output.

    Returns dict with keys gpu_video_pct, gpu_render_pct, gpu_video_enhance_pct,
    gpu_freq_mhz (values may be None if absent). Raises ValueError if unusable.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("empty intel_gpu_top output")
    # Man page: wrap NDJSON in [] — also accept a single object or first object.
    if raw.startswith("["):
        arr = json.loads(raw)
        if not arr:
            raise ValueError("empty JSON array")
        obj = arr[0]
    else:
        obj, _ = json.JSONDecoder().raw_decode(raw)

    if not isinstance(obj, dict):
        raise ValueError("sample is not an object")

    engines = obj.get("engines")
    video_vals: list[float] = []
    render_vals: list[float] = []
    enhance_vals: list[float] = []

    if isinstance(engines, dict):
        items = engines.items()
    elif isinstance(engines, list):
        items = []
        for e in engines:
            if isinstance(e, dict):
                items.append((e.get("name") or e.get("engine") or "", e))
    else:
        items = []

    for name, info in items:
        if not isinstance(info, dict):
            continue
        busy = info.get("busy")
        if busy is None:
            continue
        try:
            pct = float(busy)
        except (TypeError, ValueError):
            continue
        kind = _classify_gpu_engine(str(name))
        if kind == "video":
            video_vals.append(pct)
        elif kind == "enhance":
            enhance_vals.append(pct)
        elif kind == "render":
            render_vals.append(pct)

    freq_mhz = None
    freq = obj.get("frequency")
    if isinstance(freq, dict):
        for key in ("actual", "requested", "cur", "current"):
            if freq.get(key) is not None:
                try:
                    freq_mhz = float(freq[key])
                    break
                except (TypeError, ValueError):
                    pass
    elif isinstance(obj.get("freq"), (int, float)):
        freq_mhz = float(obj["freq"])

    return {
        "gpu_video_pct": max(video_vals) if video_vals else None,
        "gpu_render_pct": max(render_vals) if render_vals else None,
        "gpu_video_enhance_pct": max(enhance_vals) if enhance_vals else None,
        "gpu_freq_mhz": freq_mhz,
    }


def sample_gpu() -> dict:
    """
    One-shot intel_gpu_top JSON sample. Returns empty-ish dict with None fields
    on failure; rate-limits warning logs.

    intel_gpu_top -J streams NDJSON forever and never exits on its own, so we
    always kill it via timeout after enough wall time for at least one -s
    period. TimeoutExpired is therefore the *normal* success path — parse
    whatever landed on stdout before the kill.
    """
    global _gpu_warn_last
    empty = {
        "gpu_video_pct": None,
        "gpu_render_pct": None,
        "gpu_video_enhance_pct": None,
        "gpu_freq_mhz": None,
    }
    out = ""
    try:
        # -s 100 ≈ one 100ms sample period; -o - writes JSON to stdout.
        proc = subprocess.run(
            [INTEL_GPU_TOP, "-J", "-s", "100", "-o", "-"],
            capture_output=True,
            text=True,
            timeout=INTEL_GPU_TOP_TIMEOUT_S,
            check=False,
        )
        out = (proc.stdout or "").strip()
        if not out:
            err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            raise RuntimeError(err)
    except subprocess.TimeoutExpired as exc:
        # Expected: tool is a continuous stream. Prefer captured stdout.
        raw = exc.stdout
        if isinstance(raw, bytes):
            out = raw.decode(errors="replace").strip()
        else:
            out = (raw or "").strip()
        if not out:
            now = time.time()
            if now - _gpu_warn_last >= _GPU_WARN_EVERY_S:
                err = exc.stderr
                if isinstance(err, bytes):
                    err = err.decode(errors="replace")
                log.warning(
                    "intel GPU sample timed out with empty stdout (%s)",
                    (err or "").strip() or f"timeout {INTEL_GPU_TOP_TIMEOUT_S}s",
                )
                _gpu_warn_last = now
            return empty
    except FileNotFoundError:
        now = time.time()
        if now - _gpu_warn_last >= _GPU_WARN_EVERY_S:
            log.warning("intel_gpu_top not found — install intel-gpu-tools for iGPU metrics")
            _gpu_warn_last = now
        return empty
    except (RuntimeError, OSError) as exc:
        now = time.time()
        if now - _gpu_warn_last >= _GPU_WARN_EVERY_S:
            log.warning("intel GPU sample failed: %s", exc)
            _gpu_warn_last = now
        return empty

    try:
        return _parse_intel_gpu_top_json(out)
    except (ValueError, json.JSONDecodeError) as exc:
        now = time.time()
        if now - _gpu_warn_last >= _GPU_WARN_EVERY_S:
            log.warning("intel GPU sample failed: %s", exc)
            _gpu_warn_last = now
        return empty


# ---- Database --------------------------------------------------------------------

def _ensure_db_readable() -> None:
    """
    Make the SQLite file (and its WAL-mode sidecar files) world-readable so
    PHP running as the web server's own user (commonly www-data, not this
    service's `nexvue` user) can open it directly — no group-membership
    fiddling needed. Safe: this data (bandwidth, viewer IPs/channels) is
    explicitly meant to be served publicly via Apache anyway, so read access
    isn't a meaningful exposure. Called after init and after every poll
    cycle, since WAL mode creates/recreates the -wal/-shm sidecars over time.
    """
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{DB_PATH}{suffix}")
        if p.exists():
            try:
                p.chmod(0o644)
            except OSError as exc:
                log.warning("could not chmod %s: %s", p, exc)


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # WAL mode lets this collector (writer) and PHP (reader, via Apache)
        # coexist without "database is locked" contention. PHP must open its
        # connection read-only (see nexvue-metrics.php) rather than needing
        # any special handling on this side.
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
            -- one channel — IP, channel, when they joined, how long, how
            -- much data, and (once Phase 2 auth exists) which user.
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

            -- Host capacity samples (CPU/memory/load + optional iGPU engines)
            -- for Metrics correlation. Not a health monitor — CheckMK remains
            -- Phase 4 for that. GPU columns filled when intel_gpu_top works.
            CREATE TABLE IF NOT EXISTS host_samples (
                ts INTEGER NOT NULL PRIMARY KEY,
                cpu_pct REAL,
                mem_used_bytes INTEGER,
                mem_total_bytes INTEGER,
                load1 REAL,
                gpu_video_pct REAL,
                gpu_render_pct REAL,
                gpu_video_enhance_pct REAL,
                gpu_freq_mhz REAL
            );
        """)
        # Safe migrations for databases created before later columns existed —
        # ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" in SQLite.
        for ddl in (
            "ALTER TABLE viewer_sessions ADD COLUMN user TEXT",
            "ALTER TABLE host_samples ADD COLUMN gpu_video_pct REAL",
            "ALTER TABLE host_samples ADD COLUMN gpu_render_pct REAL",
            "ALTER TABLE host_samples ADD COLUMN gpu_video_enhance_pct REAL",
            "ALTER TABLE host_samples ADD COLUMN gpu_freq_mhz REAL",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    log.info("database ready at %s", DB_PATH)
    _ensure_db_readable()


def prune_old_samples() -> None:
    cutoff = int(time.time() - RETENTION_DAYS * 86400)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        for table in ("samples", "totals", "input_status", "host_samples"):
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        # viewer_sessions has no `ts` column (it's a lifecycle row, not a
        # timestamped sample) — prune on last_seen instead.
        conn.execute("DELETE FROM viewer_sessions WHERE last_seen < ?", (cutoff,))
        conn.commit()
    log.info("pruned samples older than %s (%.0f day retention)", cutoff, RETENTION_DAYS)


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
                item.get("user", ""),  # blank until Phase 2 auth exists
            ))
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT INTO viewer_sessions "
                "(session_id, remote_addr, channel, user_agent, first_seen, last_seen, bytes_sent, user) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "last_seen=excluded.last_seen, bytes_sent=excluded.bytes_sent, user=excluded.user",
                rows,
            )
            conn.commit()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
        log.warning("MediaMTX webrtcsessions poll failed: %s", exc)

    # --- Status daemon: per-input lock/format history ---
    try:
        status_urls = [f"{base}/status" for base in _status_base_urls()]
        data = _fetch_json_first(status_urls)
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

    # --- Host CPU / memory / load + optional iGPU engines ---
    cpu_pct, mem_used, mem_total, load1 = sample_host()
    gpu = sample_gpu()
    if cpu_pct is not None:
        try:
            with _db_lock, sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO host_samples "
                    "(ts, cpu_pct, mem_used_bytes, mem_total_bytes, load1, "
                    " gpu_video_pct, gpu_render_pct, gpu_video_enhance_pct, gpu_freq_mhz) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        now, cpu_pct, mem_used, mem_total, load1,
                        gpu.get("gpu_video_pct"), gpu.get("gpu_render_pct"),
                        gpu.get("gpu_video_enhance_pct"), gpu.get("gpu_freq_mhz"),
                    ),
                )
                conn.commit()
        except OSError as exc:
            log.warning("host_samples write failed: %s", exc)


def poll_loop() -> None:
    last_prune = 0.0
    while True:
        poll_once()
        _ensure_db_readable()
        if time.time() - last_prune > PRUNE_EVERY_S:
            try:
                prune_old_samples()
            except OSError as exc:
                log.warning("prune failed: %s", exc)
            last_prune = time.time()
        time.sleep(POLL_INTERVAL_S)


def main() -> None:
    init_db()
    log.info("collector running, polling every %.1fs, db=%s (no HTTP server — "
              "reads happen via PHP querying this SQLite file directly)",
              POLL_INTERVAL_S, DB_PATH)
    poll_loop()  # runs forever on the main thread — nothing else to do


if __name__ == "__main__":
    main()
