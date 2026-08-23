#!/usr/bin/env python3
"""
nexvue-sfu-publish.py — WHIP each live path to Cloudflare Stream once.

Reads /var/lib/nexvue/auth/sfu-publish.json (written by Settings). When
mode is hybrid|sfu, pulls local MediaMTX RTSP (H.264 + Opus, no transcode)
and publishes via WHIP so share/portal viewers can WHEP from Stream.

Idle when mode is off or the file is missing. Stdlib + apt PyGObject.
GI is optional at import so config helpers stay unit-testable.
"""
from __future__ import annotations

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
            except Exception as exc:
                log.warning("%s WHIP session ended: %s", self.path, exc)
            if self._stop.is_set():
                break
            self._stop.wait(RECONNECT_S)

    def _run_once(self) -> None:
        GLib, Gst, GstSdp, GstWebRTC = _gi_webrtc()
        Gst.init(None)
        loc = self.rtsp_url.replace("\\", "\\\\").replace('"', '\\"')
        desc = (
            f"webrtcbin name=send stun-server={STUN} bundle-policy=max-bundle "
            f'rtspsrc name=src location="{loc}" protocols=tcp latency=200 '
            "src. ! application/x-rtp,media=video ! queue max-size-buffers=8 leaky=downstream ! send. "
            "src. ! application/x-rtp,media=audio ! queue max-size-buffers=8 leaky=downstream ! send."
        )
        pipe = Gst.parse_launch(desc)
        webrtc = pipe.get_by_name("send")
        if webrtc is None:
            raise RuntimeError("webrtcbin missing")
        loop = GLib.MainLoop()
        err: list[str] = []
        posted = {"done": False}

        def fail(msg: str) -> None:
            err.append(msg)
            loop.quit()

        def send_offer() -> bool:
            if self._stop.is_set():
                loop.quit()
                return False
            try:
                state = webrtc.get_property("ice-gathering-state")
                if state != GstWebRTC.WebRTCICEGatheringState.COMPLETE:
                    return True
                local = webrtc.get_property("local-description")
                if local is None:
                    return True
                if posted["done"]:
                    return False
                posted["done"] = True
                sdp_text = local.sdp.as_text()
                answer_text = whip_post(self.publish_url, sdp_text)
                res, sdpmsg = GstSdp.SDPMessage.new_from_text(answer_text)
                if res != GstSdp.SDPResult.OK:
                    raise RuntimeError("could not parse WHIP SDP answer")
                answer = GstWebRTC.WebRTCSessionDescription.new(
                    GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
                )
                webrtc.emit("set-remote-description", answer, Gst.Promise.new())
                log.info("%s WHIP published", self.path)
            except Exception as exc:
                fail(str(exc))
            return False

        def on_offer_created(promise: Any, _user: Any) -> None:
            try:
                reply = promise.get_reply()
                offer = reply.get_value("offer")
                webrtc.emit("set-local-description", offer, Gst.Promise.new())
                GLib.timeout_add(200, send_offer)
            except Exception as exc:
                fail(str(exc))

        def on_negotiation_needed(_el: Any) -> None:
            promise = Gst.Promise.new_with_change_func(on_offer_created, None)
            webrtc.emit("create-offer", None, promise)

        def on_bus(_bus: Any, msg: Any) -> bool:
            if msg.type == Gst.MessageType.ERROR:
                gerr, debug = msg.parse_error()
                fail(f"{gerr.message} ({debug})")
            elif msg.type == Gst.MessageType.EOS:
                fail("EOS")
            return True

        def on_stop_tick() -> bool:
            if self._stop.is_set():
                loop.quit()
                return False
            return True

        webrtc.connect("on-negotiation-needed", on_negotiation_needed)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_bus)
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("pipeline failed to start")
        log.info("%s WHIP publishing %s", self.path, self.rtsp_url.split("?", 1)[0])
        def ice_timeout() -> bool:
            if not posted["done"] and not err:
                fail("ICE gathering timed out")
            return False

        GLib.timeout_add(250, on_stop_tick)
        GLib.timeout_add(ICE_WAIT_S * 1000, ice_timeout)
        try:
            loop.run()
            if err:
                raise RuntimeError(err[0])
        finally:
            bus.remove_signal_watch()
            pipe.set_state(Gst.State.NULL)


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
                except Exception as exc:
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


def main() -> int:
    stop = threading.Event()

    def _handle(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return run(stop)


if __name__ == "__main__":
    if os.environ.get("NEXVUE_SFU_PUBLISH_INCLUDE_ONLY") == "1":
        sys.exit(0)
    sys.exit(main())
