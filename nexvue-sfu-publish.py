#!/usr/bin/env python3
"""
WHIP each live path to Cloudflare Stream once.

Reads /var/lib/nexvue/auth/sfu-publish.json (written by Settings). When
mode is hybrid or sfu, pulls local MediaMTX RTSP (H.264 + Opus, no
transcode) and publishes via WHIP so share and portal viewers can play
from Stream.

Idle when mode is off or the file is missing. Stdlib + apt PyGObject.
GI is optional at import so the config helpers stay unit-testable.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOG_PREFIX = "[nexvue-sfu-publish]"
logging.basicConfig(
    level=getattr(logging, os.environ.get("NEXVUE_SFU_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format=f"{LOG_PREFIX} %(message)s",
)
log = logging.getLogger("nexvue-sfu-publish")

DEFAULT_PUBLISH_FILE = "/var/lib/nexvue/auth/sfu-publish.json"
IDLE_S = 10
RECONNECT_S = 5
STUN = "stun://stun.cloudflare.com:3478"
ICE_WAIT_S = 12

# GLib.GError is a RuntimeError. KeyError is a promise reply with no offer.
_PUBLISH_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    ImportError,
    AttributeError,
    TypeError,
    KeyError,
)


def publish_file_path() -> Path:
    return Path(os.environ.get("NEXVUE_SFU_PUBLISH_FILE", DEFAULT_PUBLISH_FILE))


def load_publish_config(path: Path | None = None) -> dict[str, Any]:
    p = path or publish_file_path()
    if not p.is_file():
        return {"mode": "off", "paths": {}, "rtsp_jwt": ""}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"mode": "off", "paths": {}, "rtsp_jwt": ""}
    if not isinstance(data, dict):
        return {"mode": "off", "paths": {}, "rtsp_jwt": ""}
    mode = str(data.get("mode") or "off")
    if mode not in ("off", "hybrid", "sfu"):
        mode = "off"
    raw_paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    paths: dict[str, dict[str, str]] = {}
    for key, val in raw_paths.items():
        name = str(key).lower()
        if not isinstance(val, dict):
            continue
        pub = str(val.get("publish_url") or "").strip()
        rtsp = str(val.get("rtsp_url") or "").strip()
        if pub and rtsp:
            paths[name] = {"publish_url": pub, "rtsp_url": rtsp}
    return {
        "mode": mode,
        "paths": paths,
        "rtsp_jwt": str(data.get("rtsp_jwt") or "").strip(),
        "mtime": p.stat().st_mtime,
    }


def rtsp_url_with_jwt(rtsp_url: str, jwt: str) -> str:
    if not jwt or "jwt=" in rtsp_url:
        return rtsp_url
    sep = "&" if "?" in rtsp_url else "?"
    return rtsp_url + sep + "jwt=" + urllib.parse.quote(jwt, safe="")


def whip_pipeline(rtsp_url: str) -> str:
    loc = rtsp_url.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"webrtcbin name=send stun-server={STUN} bundle-policy=max-bundle "
        f'rtspsrc name=src location="{loc}" protocols=tcp latency=200 '
        "src. ! application/x-rtp,media=video ! queue max-size-buffers=8 leaky=downstream ! send. "
        "src. ! application/x-rtp,media=audio ! queue max-size-buffers=8 leaky=downstream ! send."
    )


def whip_post(url: str, sdp: str, timeout: int = 12) -> str:
    req = urllib.request.Request(
        url,
        data=sdp.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/sdp"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"WHIP HTTP {exc.code}: {err}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"WHIP failed: {exc.reason}") from exc
    if not body.strip():
        raise RuntimeError("WHIP returned an empty SDP answer")
    return body


def _gi_webrtc():
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import GLib, Gst, GstSdp, GstWebRTC

    return GLib, Gst, GstSdp, GstWebRTC


class _WhipAttempt:
    """One GStreamer main loop that WHIPs a single path, then returns."""

    def __init__(self, session: WhipSession) -> None:
        self._session = session
        self._glib, self._gst, self._sdp, self._webrtc = _gi_webrtc()
        self._loop: Any = None
        self._pipe: Any = None
        self._webrtcbin: Any = None
        self._bus: Any = None
        self._errors: list[str] = []
        self._posted = False

    def run(self) -> None:
        gst = self._gst
        gst.init(None)
        self._pipe = gst.parse_launch(whip_pipeline(self._session.rtsp_url))
        self._webrtcbin = self._pipe.get_by_name("send")
        if self._webrtcbin is None:
            raise RuntimeError("webrtcbin missing")
        self._loop = self._glib.MainLoop()
        self._webrtcbin.connect("on-negotiation-needed", self._on_negotiation_needed)
        self._bus = self._pipe.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message", self._on_bus)
        if self._pipe.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
            raise RuntimeError("pipeline failed to start")
        log.info(
            "%s WHIP publishing %s",
            self._session.path,
            self._session.rtsp_url.split("?", 1)[0],
        )
        self._glib.timeout_add(250, self._on_stop_tick)
        self._glib.timeout_add(ICE_WAIT_S * 1000, self._on_ice_timeout)
        try:
            self._loop.run()
            if self._errors:
                raise RuntimeError(self._errors[0])
        finally:
            self._bus.remove_signal_watch()
            self._pipe.set_state(gst.State.NULL)

    def _fail(self, message: str) -> None:
        self._errors.append(message)
        if self._loop is not None:
            self._loop.quit()

    def _send_offer(self) -> bool:
        if self._session._stop.is_set():
            self._loop.quit()
            return False
        try:
            return self._post_gathered_offer()
        except _PUBLISH_ERRORS as exc:
            self._fail(str(exc))
        return False

    def _post_gathered_offer(self) -> bool:
        webrtc = self._webrtcbin
        state = webrtc.get_property("ice-gathering-state")
        if state != self._webrtc.WebRTCICEGatheringState.COMPLETE:
            return True
        local = webrtc.get_property("local-description")
        if local is None:
            return True
        if self._posted:
            return False
        self._posted = True
        answer_text = whip_post(self._session.publish_url, local.sdp.as_text())
        res, sdpmsg = self._sdp.SDPMessage.new_from_text(answer_text)
        if res != self._sdp.SDPResult.OK:
            raise RuntimeError("could not parse WHIP SDP answer")
        answer = self._webrtc.WebRTCSessionDescription.new(
            self._webrtc.WebRTCSDPType.ANSWER, sdpmsg
        )
        webrtc.emit("set-remote-description", answer, self._gst.Promise.new())
        log.info("%s WHIP published", self._session.path)
        return False

    def _on_offer_created(self, promise: Any, _user: Any) -> None:
        try:
            reply = promise.get_reply()
            offer = reply.get_value("offer")
            self._webrtcbin.emit(
                "set-local-description", offer, self._gst.Promise.new()
            )
            self._glib.timeout_add(200, self._send_offer)
        except _PUBLISH_ERRORS as exc:
            self._fail(str(exc))

    def _on_negotiation_needed(self, _element: Any) -> None:
        promise = self._gst.Promise.new_with_change_func(self._on_offer_created, None)
        self._webrtcbin.emit("create-offer", None, promise)

    def _on_bus(self, _bus: Any, msg: Any) -> bool:
        if msg.type == self._gst.MessageType.ERROR:
            gerr, debug = msg.parse_error()
            self._fail(f"{gerr.message} ({debug})")
        elif msg.type == self._gst.MessageType.EOS:
            self._fail("EOS")
        return True

    def _on_stop_tick(self) -> bool:
        if self._session._stop.is_set():
            self._loop.quit()
            return False
        return True

    def _on_ice_timeout(self) -> bool:
        if not self._posted and not self._errors:
            self._fail("ICE gathering timed out")
        return False


class WhipSession:
    def __init__(self, path: str, rtsp_url: str, publish_url: str) -> None:
        self.path = path
        self.rtsp_url = rtsp_url
        self.publish_url = publish_url
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"sfu-{self.path}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except _PUBLISH_ERRORS as exc:
                log.warning("%s WHIP session ended: %s", self.path, exc)
            if self._stop.is_set():
                break
            self._stop.wait(RECONNECT_S)

    def _run_once(self) -> None:
        _WhipAttempt(self).run()


def run(stop: threading.Event) -> int:
    sessions: dict[str, WhipSession] = {}
    last_sig = ""
    while not stop.is_set():
        cfg = load_publish_config()
        mode = str(cfg.get("mode") or "off")
        paths: dict[str, dict[str, str]] = cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}
        jwt = str(cfg.get("rtsp_jwt") or "")
        want: dict[str, dict[str, str]] = {}
        if mode in ("hybrid", "sfu"):
            for path, spec in paths.items():
                want[path] = {
                    "publish_url": spec["publish_url"],
                    "rtsp_url": rtsp_url_with_jwt(spec["rtsp_url"], jwt),
                }
        sig = json.dumps({"mode": mode, "paths": want}, sort_keys=True)
        if sig != last_sig:
            for sess in sessions.values():
                sess.stop()
            sessions = {}
            last_sig = sig
            if not want:
                log.info("SFU publish idle (mode=%s)", mode)
            else:
                try:
                    _gi_webrtc()
                except _PUBLISH_ERRORS as exc:
                    log.error("GStreamer WebRTC bindings missing: %s", exc)
                    last_sig = ""
                    stop.wait(IDLE_S)
                    continue
            for path, spec in want.items():
                sess = WhipSession(path, spec["rtsp_url"], spec["publish_url"])
                sess.start()
                sessions[path] = sess
                log.info("started WHIP for %s", path)
        stop.wait(IDLE_S)
    for sess in sessions.values():
        sess.stop()
    return 0


def _stop_on_signal(stop: threading.Event, _signum: int, _frame: Any) -> None:
    stop.set()


def main() -> int:
    stop = threading.Event()
    handler = functools.partial(_stop_on_signal, stop)
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    return run(stop)


if __name__ == "__main__":
    if os.environ.get("NEXVUE_SFU_PUBLISH_INCLUDE_ONLY") == "1":
        sys.exit(0)
    sys.exit(main())
