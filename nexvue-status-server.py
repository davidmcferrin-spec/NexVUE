#!/usr/bin/env python3
"""
nexvue-status-server.py — serve DeckLink input/reference status as JSON over HTTP.

Polls the decklink-status helper on an interval and caches the result, so any
number of web clients can poll without hammering the DeckLink API. Stdlib
only — no pip dependencies.

Endpoint:
    GET /status  ->  {"devices":[...], "ts": <unix>, "stale": bool,
                      "poll_stats": {...}}   (poll_stats is self-diagnostic —
                      see PollStats below)

Runs as a systemd service (nexvue-status.service), port 9998.
Phase 3 note: like the MediaMTX API, this is LAN-trust-level. In the DMZ it
should bind to loopback and be relayed by the portal heartbeat, not exposed.

Robustness notes (see README "nexvue-status hardening" if that section
exists, or CLAUDE.md known-issues):
  - poll_loop has a broad except-Exception safety net IN ADDITION TO the
    specific exception handling below it, so an unanticipated failure mode
    can never silently kill the polling thread — systemd's Restart=always
    can only recover a dead PROCESS, not a dead thread inside a live one.
  - A systemd watchdog ping (sd_notify WATCHDOG=1) fires once per completed
    poll LOOP ITERATION (regardless of whether the poll itself succeeded),
    so if the loop ever truly hangs despite the safety net above, systemd's
    own WatchdogSec= will detect the missed heartbeat and restart the unit.
  - The HTTP server sets a per-connection socket timeout and daemon_threads,
    so a stalled/slow client (flaky mobile networks, NAT weirdness — this
    project has seen plenty) can't block a handler thread indefinitely or
    prevent a clean systemd restart.
"""

import json
import logging
import os
import socket
import ssl
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HELPER = "/usr/local/bin/decklink-status"
# The helper now ACTIVELY probes idle inputs (~0.7s each) to detect signal on
# unconnected sub-devices, so a full run can take a few seconds on a 4-8 input
# card with idle connectors. Poll less frequently than the old passive helper
# and give it generous headroom. Inputs held by a running encoder are read via
# the fast status-flag fallback, so in production (encoders running) the helper
# is quick — the slow path only hits genuinely idle inputs.
POLL_INTERVAL_S = 5.0
HELPER_TIMEOUT_S = 15.0
# Must stay above HELPER_TIMEOUT_S: while a slow decklink-status probe is
# in flight, `ts` is not updated. If STALE_AFTER_S is shorter than the
# helper's worst case, /status flips stale=true mid-poll and the player
# blanks its signal dots even though the daemon is healthy.
STALE_AFTER_S = HELPER_TIMEOUT_S + POLL_INTERVAL_S
LISTEN_ADDR = ("0.0.0.0", 9998)

# Log level: DEBUG surfaces every poll cycle and every request; INFO (default)
# keeps a periodic summary plus warnings/errors, without per-poll spam over a
# multi-day run. Bump to DEBUG (systemd Environment=NEXVUE_STATUS_LOG_LEVEL=DEBUG)
# when actively troubleshooting.
LOG_LEVEL = os.environ.get("NEXVUE_STATUS_LOG_LEVEL", "INFO").upper()
SUMMARY_EVERY_S = 300  # periodic poll-health heartbeat, independent of DEBUG

# TLS (optional). If the page serving the player is HTTPS, browsers block it
# from fetching plain HTTP (mixed content) — so once Apache is on TLS, this
# daemon must be too. Same cert/key Apache uses works fine; set via env vars
# (systemd Environment= lines) rather than editing this file.
TLS_CERT = os.environ.get("NEXVUE_STATUS_TLS_CERT", "")
TLS_KEY = os.environ.get("NEXVUE_STATUS_TLS_KEY", "")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                     format="[nexvue-status] %(message)s")
log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache = {"devices": [], "ts": 0.0, "error": "no data yet"}


class PollStats:
    """Self-diagnostic counters, exposed in the /status payload itself so a
    plain `curl /status` reveals daemon health — no separate metrics call
    needed to answer "is polling actually working." Thread-safe via _lock
    (the same lock guarding _cache; contention is negligible at this rate)."""
    def __init__(self):
        self.total_polls = 0
        self.failed_polls = 0
        self.consecutive_failures = 0
        self.last_success_ts = 0.0
        self.last_poll_duration_s = 0.0
        self.started_ts = time.time()

    def record(self, ok: bool, duration_s: float) -> None:
        self.total_polls += 1
        self.last_poll_duration_s = duration_s
        if ok:
            self.consecutive_failures = 0
            self.last_success_ts = time.time()
        else:
            self.failed_polls += 1
            self.consecutive_failures += 1

    def as_dict(self) -> dict:
        return {
            "total_polls": self.total_polls,
            "failed_polls": self.failed_polls,
            "consecutive_failures": self.consecutive_failures,
            "last_success_ts": self.last_success_ts,
            "last_poll_duration_s": round(self.last_poll_duration_s, 3),
            "uptime_s": round(time.time() - self.started_ts, 1),
        }


_stats = PollStats()
# Set once in main() once we know whether TLS actually came up — lets the
# self-check below hit the right scheme without duplicating that logic.
_serving_scheme = "http"


def _sd_notify(state: str) -> None:
    """Minimal systemd sd_notify client (no python-systemd dependency, which
    would violate this project's no-pip rule) — sends a datagram to the
    socket path systemd provides in $NOTIFY_SOCKET. Silently does nothing if
    not running under systemd (e.g. run by hand for debugging), or if the
    notify call fails for any reason — this is a best-effort convenience,
    never something that should be allowed to disrupt the daemon itself."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass


def poll_once() -> None:
    """One poll cycle. Raises nothing — every failure mode is caught and
    recorded, both the specific ones we know about and (via the broad
    except at the bottom) anything we don't. That broad catch is deliberate
    defense in depth: poll_loop's while-True must never exit, because
    systemd's Restart=always can revive a dead PROCESS but has no way to
    notice or fix a thread that silently stopped iterating inside a process
    that's still technically alive."""
    global _cache
    start = time.monotonic()
    ok = False
    try:
        out = subprocess.run(
            [HELPER], capture_output=True, timeout=HELPER_TIMEOUT_S, text=True
        )
        data = json.loads(out.stdout)
        data["ts"] = time.time()
        if out.returncode == 0:
            data.pop("error", None)
        with _lock:
            _cache = data
        ok = out.returncode == 0
        log.debug("poll ok (rc=%s, %d devices)", out.returncode, len(data.get("devices", [])))
    except FileNotFoundError:
        log.error("helper not installed at %s", HELPER)
        with _lock:
            _cache = {"devices": [], "ts": time.time(), "error": f"{HELPER} not installed"}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log.warning("helper poll failed: %s", exc)
        with _lock:
            _cache = {"devices": _cache.get("devices", []),
                      "ts": _cache.get("ts", 0.0),
                      "error": str(exc)}
    except Exception:  # noqa: BLE001 — intentional last-resort safety net; see docstring
        log.error("unexpected error in poll cycle — cache left at last-known-good "
                  "state, loop continues:\n%s", traceback.format_exc())
        with _lock:
            _cache = {"devices": _cache.get("devices", []),
                      "ts": _cache.get("ts", 0.0),
                      "error": "unexpected poll error (see log)"}
    duration = time.monotonic() - start
    _stats.record(ok, duration)
    if duration > HELPER_TIMEOUT_S * 0.8:
        log.warning("poll cycle took %.1fs, approaching the %.0fs timeout", duration, HELPER_TIMEOUT_S)


def _self_check_ok() -> bool:
    """
    Prove the server can actually complete a real request end-to-end, not
    just that the poll_loop thread is iterating — those turned out to be
    independent failure modes in practice: this exact daemon once kept
    logging healthy poll summaries for 10+ minutes while its HTTP listener
    had stopped completing connections to real clients. Gating the systemd
    watchdog ping on this check closes that gap — if the HTTP path wedges,
    pings stop, and systemd's WatchdogSec restarts the unit automatically
    instead of needing a manual restart to notice.
    """
    url = f"{_serving_scheme}://127.0.0.1:{LISTEN_ADDR[1]}/status"
    ctx = None
    if _serving_scheme == "https":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed cert on this same box
    try:
        with urllib.request.urlopen(url, timeout=3, context=ctx) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def poll_loop() -> None:
    """Refresh the status cache forever. Helper failures are recorded in the
    payload rather than crashing the server — a wedged driver should degrade
    the display, not kill status for the whole box."""
    last_summary = time.monotonic()
    while True:
        poll_once()
        # Only ping the watchdog if the HTTP path is verifiably still
        # serving real requests — see _self_check_ok's docstring for why
        # "the poll loop is iterating" alone isn't sufficient evidence.
        if _self_check_ok():
            _sd_notify("WATCHDOG=1")
        else:
            log.warning("self-check failed — HTTP path may be wedged; "
                        "withholding watchdog ping so systemd can recover it")

        if time.monotonic() - last_summary > SUMMARY_EVERY_S:
            s = _stats.as_dict()
            log.info("poll summary: %d total, %d failed, %d consecutive fail, "
                      "last ok %.0fs ago, last duration %.2fs, uptime %.0fs",
                      s["total_polls"], s["failed_polls"], s["consecutive_failures"],
                      time.time() - s["last_success_ts"] if s["last_success_ts"] else -1,
                      s["last_poll_duration_s"], s["uptime_s"])
            last_summary = time.monotonic()

        time.sleep(POLL_INTERVAL_S)


class Handler(BaseHTTPRequestHandler):
    # Bounds how long ANY single connection can occupy a handler thread —
    # without this, a client that connects and stalls (flaky mobile network,
    # NAT weirdness, a half-open TCP connection) can block that thread
    # forever, since ThreadingHTTPServer has no built-in request timeout and
    # spawns one thread per connection with no upper bound. Enough stalled
    # clients accumulate into real memory/thread pressure over hours-to-days
    # of uptime — a very plausible explanation for an intermittent crash.
    timeout = 10

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        req_start = time.monotonic()
        status_code = 200
        try:
            if self.path.split("?")[0] != "/status":
                status_code = 404
                self.send_error(404)
                return
            with _lock:
                payload = dict(_cache)
            payload["stale"] = (time.time() - payload.get("ts", 0)) > STALE_AFTER_S
            payload["poll_stats"] = _stats.as_dict()
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            # Routine, expected on flaky/mobile networks — the client hung up
            # or stalled mid-response. Log it plainly at DEBUG (visible if
            # troubleshooting connection churn) rather than let a full
            # traceback hit the journal for what is normal network behavior.
            status_code = -1  # sentinel: "client disconnected", not a real HTTP code
            log.debug("client %s disconnected mid-response", self.client_address[0])
        finally:
            duration_ms = (time.monotonic() - req_start) * 1000
            if status_code == -1:
                log.debug("GET %s from %s -> disconnected (%.0fms)",
                          self.path, self.client_address[0], duration_ms)
            else:
                log.debug("GET %s from %s -> %d (%.0fms)",
                          self.path, self.client_address[0], status_code, duration_ms)
            if duration_ms > 2000:
                log.warning("slow request: GET %s from %s took %.0fms",
                            self.path, self.client_address[0], duration_ms)

    def log_message(self, *_args) -> None:
        pass  # replaced by the structured logging in do_GET above


class StatusHTTPServer(ThreadingHTTPServer):
    # Without this, a handler thread stuck on a stalled client (bounded to
    # `Handler.timeout` seconds above, but still) is a non-daemon thread by
    # ThreadingMixIn's default, which can delay clean process shutdown long
    # enough for systemd to SIGKILL rather than a clean stop/restart — the
    # kind of thing that shows up in the journal looking like an unexplained
    # crash rather than the timeout-driven cleanup it actually was.
    daemon_threads = True

    # stdlib default is 5 — genuinely small. This box has been hit with
    # bursts of near-simultaneous connections repeatedly during bring-up
    # (curl, openssl s_client, multiple browsers, all testing TLS at once);
    # a burst exceeding the backlog can look like a server hang from the
    # client side even though the process itself is fine. 64 gives real
    # headroom without meaningfully increasing resource use at idle.
    request_queue_size = 64


def main() -> None:
    global _serving_scheme
    server = StatusHTTPServer(LISTEN_ADDR, Handler)

    scheme = "http"
    if TLS_CERT and TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(certfile=TLS_CERT, keyfile=TLS_KEY)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
        except (ssl.SSLError, FileNotFoundError, PermissionError, OSError) as exc:
            # wrap_socket() can itself raise (bad cert/key pairing, protocol
            # mismatch) — it MUST be inside this try, not just load_cert_chain,
            # or a bad cert crashes the whole daemon at startup instead of
            # degrading to plain HTTP as intended.
            log.error("TLS setup failed (%s) — falling back to plain HTTP", exc)
        else:
            scheme = "https"
    elif TLS_CERT or TLS_KEY:
        log.warning("only one of NEXVUE_STATUS_TLS_CERT/_KEY set — need both; serving plain HTTP")

    # Must be set BEFORE the poll thread starts — poll_loop's self-check
    # reads this to know which scheme to test. Starting the thread earlier
    # (as an older version of this file did) is a race: the self-check could
    # run against the wrong scheme before main() finishes resolving TLS.
    _serving_scheme = scheme
    threading.Thread(target=poll_loop, daemon=True).start()

    log.info("serving %s on %s:%d, polling %s every %.1fs (log level %s)",
             scheme, *LISTEN_ADDR, HELPER, POLL_INTERVAL_S, LOG_LEVEL)
    _sd_notify("READY=1")
    server.serve_forever()


if __name__ == "__main__":
    main()
