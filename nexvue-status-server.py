#!/usr/bin/env python3
"""
nexvue-status-server.py — serve DeckLink input/reference status as JSON over HTTP.

Polls the decklink-status helper on an interval and caches the result, so any
number of web clients can poll without hammering the DeckLink API. Stdlib
only — no pip dependencies.

Endpoint:
    GET /status  ->  {"devices":[...], "ts": <unix>, "stale": bool}

Runs as a systemd service (nexvue-status.service), port 9998.
Phase 3 note: like the MediaMTX API, this is LAN-trust-level. In the DMZ it
should bind to loopback and be relayed by the portal heartbeat, not exposed.
"""

import json
import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HELPER = "/usr/local/bin/decklink-status"
POLL_INTERVAL_S = 2.0
HELPER_TIMEOUT_S = 5.0
STALE_AFTER_S = 10.0
LISTEN_ADDR = ("0.0.0.0", 9998)

logging.basicConfig(level=logging.INFO, format="[nexvue-status] %(message)s")
log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache = {"devices": [], "ts": 0.0, "error": "no data yet"}


def poll_loop() -> None:
    """Refresh the status cache forever. Helper failures are recorded in the
    payload rather than crashing the server — a wedged driver should degrade
    the display, not kill status for the whole box."""
    global _cache
    while True:
        try:
            out = subprocess.run(
                [HELPER], capture_output=True, timeout=HELPER_TIMEOUT_S, text=True
            )
            data = json.loads(out.stdout)
            data["ts"] = time.time()
            data.pop("error", None) if out.returncode == 0 else None
            with _lock:
                _cache = data
        except FileNotFoundError:
            with _lock:
                _cache = {"devices": [], "ts": time.time(),
                          "error": f"{HELPER} not installed"}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            log.warning("helper poll failed: %s", exc)
            with _lock:
                _cache = {"devices": _cache.get("devices", []),
                          "ts": _cache.get("ts", 0.0),
                          "error": str(exc)}
        time.sleep(POLL_INTERVAL_S)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.split("?")[0] != "/status":
            self.send_error(404)
            return
        with _lock:
            payload = dict(_cache)
        payload["stale"] = (time.time() - payload.get("ts", 0)) > STALE_AFTER_S
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass  # journald noise control; errors still surface via logging


def main() -> None:
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(LISTEN_ADDR, Handler)
    log.info("serving on %s:%d, polling %s every %.1fs",
             *LISTEN_ADDR, HELPER, POLL_INTERVAL_S)
    server.serve_forever()


if __name__ == "__main__":
    main()
