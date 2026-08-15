#!/usr/bin/env python3
"""
nexvue-encode.py — persistent MediaMTX publish + disposable DeckLink capture.

Production encoder for nexvue-encode@N (invoked via nexvue-encode.sh).
Two GStreamer pipelines in one process, no input-selector / slate:

  Capture (disposable):  DeckLink A/V → normalize → appsink
  Publish (persistent):  appsrc → HI/LO encode → rtspclientsink

SDI hiccups, exclusive-open races, and not-negotiated errors tear down
capture only. Publish keeps pushing last-frame / black + silence so WHEP
sessions stay up. RTSP sink death rebuilds publish only.

Stdlib + apt PyGObject (python3-gi). GI is optional at import time so
load_config / pipeline assembly / retry policy stay unit-testable without
GStreamer. main() exits 69 if GI is missing at runtime.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

LOG_PREFIX = "[nexvue-encode]"
logging.basicConfig(
    level=getattr(logging, os.environ.get("NEXVUE_ENCODE_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format=f"{LOG_PREFIX} %(message)s",
)
log = logging.getLogger("nexvue-encode")

try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib, Gst  # noqa: E402

    GST_AVAILABLE = True
except (ImportError, ValueError):
    Gst = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]
    GST_AVAILABLE = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    def __init__(self, message: str, exit_code: int = 64) -> None:
        super().__init__(message)
        self.exit_code = exit_code


LO_PRESETS = {
    "720p": (1280, 720, 1200),
    "540p": (960, 540, 800),
    "480p": (854, 480, 700),
    "360p": (640, 360, 500),
    "240p": (426, 240, 300),
    "180p": (320, 180, 200),
}
LO_FPS_ALLOWED = frozenset({"60000/1001", "30000/1001", "15000/1001"})
LO_FPS_ALIASES = {
    "60": "60000/1001",
    "59.94": "60000/1001",
    "59.940": "60000/1001",
    "30": "30000/1001",
    "29.97": "30000/1001",
    "29.970": "30000/1001",
    "15": "15000/1001",
    "14.99": "15000/1001",
    "14.985": "15000/1001",
}
AUDIO_LAYOUTS = {
    "stereo": "stereo",
    "2.0": "stereo",
    "51": "51",
    "5.1": "51",
    "surround": "51",
    "stereo_sap": "stereo_sap",
    "sap": "stereo_sap",
    "51_sap": "51_sap",
    "5.1_sap": "51_sap",
    "surround_sap": "51_sap",
}
# embed-index → interleave sink → mono channel-mask (FL FR FC LFE RL RR SL SR)
AUDIO_POSITION_MASKS = (
    (0, 0, "0x1"),
    (1, 1, "0x2"),
    (2, 2, "0x4"),
    (3, 3, "0x8"),
    (4, 4, "0x10"),
    (5, 5, "0x20"),
    (6, 6, "0x400"),
    (7, 7, "0x800"),
)
DEINT_METHODS = frozenset(
    {"yadif", "greedyh", "tomsmocomp", "greedyl", "vfir", "linear"}
)


def normalize_lo_fps(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return "30000/1001"
    return LO_FPS_ALIASES.get(v, LO_FPS_ALIASES.get(v.lower(), v))


def append_jwt(url: str, token: str) -> str:
    if not token:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}jwt={token}"


def fps_duration_ns(fps: str) -> int:
    num_s, den_s = fps.split("/", 1)
    return int(int(den_s) * 1_000_000_000 / int(num_s))


def nv12_size(width: int, height: int) -> int:
    return width * height * 3 // 2


def make_black_nv12(width: int, height: int) -> bytes:
    y = width * height
    return bytes(y) + bytes([128] * (y // 2))


def make_silence_s16(channels: int, rate: int, frame_ms: int) -> bytes:
    samples = rate * frame_ms // 1000
    return bytes(samples * channels * 2)


def capture_retry_backoff_s(failures: int, base_s: float, cap_s: float) -> float:
    if failures < 1:
        return max(0.0, base_s)
    return min(cap_s, max(base_s, base_s * (2 ** min(failures - 1, 4))))


@dataclass(frozen=True)
class CaptureDecision:
    action: str  # retry | exit_unlocked | fatal
    reason: str
    backoff_s: float


def decide_capture_failure(
    *,
    been_live: bool,
    probe: str,
    failures: int,
    base_backoff_s: float,
    cap_s: float,
) -> CaptureDecision:
    """In-process capture death: retry while locked / after live; park-cycle if never live + unlocked."""
    backoff = capture_retry_backoff_s(failures, base_backoff_s, cap_s)
    if been_live:
        return CaptureDecision("retry", "been-live — hold publish, reopen capture", backoff)
    if probe == "unlocked":
        return CaptureDecision(
            "exit_unlocked",
            "never-live + unlocked — systemd auto-park cycle",
            0.0,
        )
    return CaptureDecision("retry", f"probe={probe} — reopen race", backoff)


def capture_open_status(
    *,
    state: str,
    saw_error: bool,
    elapsed_s: float,
    gate_s: float,
) -> str:
    """wait | ok | fail — fail on ERROR or silent PAUSED past the open gate."""
    if saw_error:
        return "fail"
    if state == "PLAYING":
        return "ok"
    if elapsed_s >= gate_s:
        return "fail"
    return "wait"


def video_hold_kind(
    *,
    has_frame: bool,
    last_mono: float,
    now: float,
    hold_last_s: float,
) -> str:
    """live | hold | black — what the publish pump should push."""
    if not has_frame:
        return "black"
    age = now - last_mono
    if age <= 0.25:
        return "live"
    if age <= hold_last_s:
        return "hold"
    return "black"


@dataclass(frozen=True)
class EncodeConfig:
    channel_id: int
    channel_path: str
    device_number: int
    max_devices: int
    deint_fields: str
    deint_method: str
    bitrate_kbps: int
    gop_frames: int
    enable_audio: bool
    audio_layout: str
    audio_embeds: str
    audio_bitrate_bps: int
    audio_frame_ms: int
    audio_queue_buffers: int
    audio_resample_quality: int
    decklink_buffer_frames: int
    watchdog_ms: int
    output_width: int
    output_height: int
    output_fps: str
    rtsp_url: str
    video_encoder: str
    extra_enc_args: str
    lo_enable: bool
    lo_preset: str
    lo_fps: str
    lo_rtsp_url: str
    lo_width: int
    lo_height: int
    lo_bitrate_kbps: int
    lo_target_usage: int
    lo_queue_buffers: int
    captions_enable: bool
    captions_pipeline_only: bool
    captions_dir: str
    captions_decode_bin: str
    captions_fifo: str
    open_delay_s: int
    open_backoff_s: int
    open_backoff_cap_s: int
    hang_kill_s: int
    open_gate_s: int
    start_stagger_s: int
    hold_last_s: float
    auto_park_bin: str


def load_config(env: Mapping[str, str]) -> EncodeConfig:
    def raw(name: str) -> Optional[str]:
        v = env.get(name)
        if v is None:
            return None
        v = v.split("#", 1)[0].strip()
        return v or None

    def required(name: str) -> str:
        v = raw(name)
        if v is None:
            raise ConfigError(f"{name} is required", exit_code=1)
        return v

    def opt(name: str, default: str) -> str:
        v = raw(name)
        return v if v is not None else default

    def opt_int(name: str, default: int) -> int:
        v = raw(name)
        if v is None:
            return default
        try:
            return int(v)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {v!r}") from exc

    def opt_float(name: str, default: float) -> float:
        v = raw(name)
        if v is None:
            return default
        try:
            return float(v)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {v!r}") from exc

    def opt_bool(name: str, default: bool) -> bool:
        v = raw(name)
        if v is None:
            return default
        low = v.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        raise ConfigError(f"{name} must be 'true' or 'false', got {v!r}")

    device_raw = required("DEVICE_NUMBER")
    try:
        device_number = int(device_raw)
    except ValueError as exc:
        raise ConfigError(f"DEVICE_NUMBER must be an integer, got {device_raw!r}") from exc

    channel_path = required("CHANNEL_PATH")
    if not all(c.isalnum() or c in "-_" for c in channel_path):
        raise ConfigError(f"CHANNEL_PATH must be alphanumeric (with -/_), got {channel_path!r}")

    max_devices = opt_int("MAX_DEVICES", 8)
    if not (1 <= max_devices <= 8):
        raise ConfigError(f"MAX_DEVICES must be an integer 1-8, got {max_devices}")
    if not (0 <= device_number < max_devices):
        raise ConfigError(
            f"DEVICE_NUMBER must be 0-{max_devices - 1} for this card "
            f"(MAX_DEVICES={max_devices}), got {device_number}"
        )

    channel_id = opt_int("CHANNEL_ID", device_number)
    deint_fields = opt("DEINT_FIELDS", "all")
    if deint_fields not in ("all", "top"):
        raise ConfigError(f"DEINT_FIELDS must be 'all' or 'top', got {deint_fields!r}")
    output_fps = "60000/1001" if deint_fields == "all" else "30000/1001"

    deint_method = opt("DEINT_METHOD", "yadif").lower()
    if deint_method not in DEINT_METHODS:
        raise ConfigError(
            "DEINT_METHOD must be yadif|greedyh|tomsmocomp|greedyl|vfir|linear, "
            f"got {deint_method!r}"
        )

    bitrate_kbps = opt_int("BITRATE_KBPS", 5000)
    gop_frames = opt_int("GOP_FRAMES", 60)
    enable_audio = opt_bool("ENABLE_AUDIO", True)
    audio_bitrate_bps = opt_int("AUDIO_BITRATE_BPS", 384000)
    audio_frame_ms = opt_int("AUDIO_FRAME_MS", 10)
    if audio_frame_ms not in (2, 5, 10, 20, 40, 60):
        raise ConfigError(f"AUDIO_FRAME_MS must be one of 2,5,10,20,40,60, got {audio_frame_ms}")
    audio_queue_buffers = opt_int("AUDIO_QUEUE_BUFFERS", 100)
    if audio_queue_buffers < 1:
        raise ConfigError(f"AUDIO_QUEUE_BUFFERS must be a positive integer, got {audio_queue_buffers}")
    audio_resample_quality = opt_int("AUDIO_RESAMPLE_QUALITY", 9)
    if not (0 <= audio_resample_quality <= 10):
        raise ConfigError(f"AUDIO_RESAMPLE_QUALITY must be an integer 0-10, got {audio_resample_quality}")

    audio_channels_legacy = opt("AUDIO_CHANNELS", "")
    audio_layout_raw = opt("AUDIO_LAYOUT", "")
    if not audio_layout_raw:
        if audio_channels_legacy in ("", "8"):
            audio_layout_raw = "51_sap"
        elif audio_channels_legacy == "2":
            audio_layout_raw = "stereo"
        elif audio_channels_legacy == "4":
            audio_layout_raw = "stereo_sap"
        elif audio_channels_legacy in ("3", "5", "6"):
            audio_layout_raw = "51"
        else:
            raise ConfigError(
                f"AUDIO_CHANNELS '{audio_channels_legacy}' needs "
                "AUDIO_LAYOUT=stereo|51|stereo_sap|51_sap"
            )
    audio_layout = AUDIO_LAYOUTS.get(audio_layout_raw)
    if audio_layout is None:
        raise ConfigError(
            f"AUDIO_LAYOUT must be stereo|51|stereo_sap|51_sap, got {audio_layout_raw!r}"
        )
    audio_embeds = opt("AUDIO_EMBEDS", "")

    decklink_buffer_frames = opt_int("DECKLINK_BUFFER_FRAMES", 2)
    watchdog_ms = opt_int("WATCHDOG_MS", 0)
    if watchdog_ms < 0:
        raise ConfigError(f"WATCHDOG_MS must be >= 0, got {watchdog_ms}")

    output_width = opt_int("OUTPUT_WIDTH", 1920)
    output_height = opt_int("OUTPUT_HEIGHT", 1080)
    rtsp_url = opt("RTSP_URL", f"rtsp://127.0.0.1:8554/{channel_path}")
    video_encoder = opt("VIDEO_ENCODER", "vah264enc")
    if video_encoder not in ("vah264enc", "x264enc"):
        raise ConfigError(f"VIDEO_ENCODER must be 'vah264enc' or 'x264enc', got {video_encoder!r}")
    extra_enc_args = opt("EXTRA_ENC_ARGS", "")

    lo_enable = opt_bool("LO_ENABLE", True)
    lo_preset = opt("LO_PRESET", "360p")
    if lo_preset not in LO_PRESETS:
        raise ConfigError(
            f"LO_PRESET must be one of {','.join(LO_PRESETS)}, got {lo_preset!r}"
        )
    lo_w_def, lo_h_def, lo_br_def = LO_PRESETS[lo_preset]
    lo_width = opt_int("LO_WIDTH", lo_w_def)
    lo_height = opt_int("LO_HEIGHT", lo_h_def)
    lo_bitrate_kbps = opt_int("LO_BITRATE_KBPS", lo_br_def)
    lo_fps = normalize_lo_fps(opt("LO_FPS", "30000/1001"))
    if lo_fps not in LO_FPS_ALLOWED:
        raise ConfigError(
            f"LO_FPS must be one of {', '.join(sorted(LO_FPS_ALLOWED))} "
            f"(or alias 60/30/15 / 59.94/29.97), got {opt('LO_FPS', '')!r}"
        )
    lo_rtsp_url = opt("LO_RTSP_URL", f"rtsp://127.0.0.1:8554/{channel_path}lo")
    lo_target_usage = opt_int("LO_TARGET_USAGE", 7)
    if not (1 <= lo_target_usage <= 7):
        raise ConfigError(f"LO_TARGET_USAGE must be an integer 1-7, got {lo_target_usage}")
    lo_queue_buffers = opt_int("LO_QUEUE_BUFFERS", 16)
    if lo_queue_buffers < 1:
        raise ConfigError(f"LO_QUEUE_BUFFERS must be a positive integer, got {lo_queue_buffers}")

    jwt = opt("NEXVUE_PUBLISH_JWT", "")
    rtsp_url = append_jwt(rtsp_url, jwt)
    lo_rtsp_url = append_jwt(lo_rtsp_url, jwt)

    captions_enable = opt_bool("CAPTIONS_ENABLE", True)
    captions_pipeline_only = opt_bool("CAPTIONS_PIPELINE_ONLY", False)
    captions_dir = opt("CAPTIONS_DIR", "/run/nexvue/captions")
    captions_decode_bin = opt(
        "CAPTIONS_DECODE_BIN", "/usr/local/bin/nexvue-captions-decode.py"
    )
    if captions_pipeline_only:
        captions_fifo = "/dev/null"
    else:
        captions_fifo = (Path(captions_dir) / f"{channel_path}.ccraw").as_posix()

    open_delay_s = opt_int("DECKLINK_OPEN_DELAY_S", 2)
    open_backoff_s = opt_int("DECKLINK_OPEN_BACKOFF_S", 2)
    open_backoff_cap_s = opt_int("DECKLINK_OPEN_BACKOFF_CAP_S", 15)
    hang_kill_s = opt_int("DECKLINK_HANG_KILL_S", 5)
    open_gate_s = opt_int("DECKLINK_OPEN_GATE_S", 10)
    for key, val in (
        ("DECKLINK_OPEN_DELAY_S", open_delay_s),
        ("DECKLINK_OPEN_BACKOFF_S", open_backoff_s),
        ("DECKLINK_OPEN_BACKOFF_CAP_S", open_backoff_cap_s),
        ("DECKLINK_HANG_KILL_S", hang_kill_s),
        ("DECKLINK_OPEN_GATE_S", open_gate_s),
    ):
        if val < 0:
            raise ConfigError(f"{key} must be an integer seconds, got {val}")

    stagger_raw = raw("DECKLINK_START_STAGGER_S")
    if stagger_raw is None:
        start_stagger_s = channel_id if channel_id >= 0 else 0
    else:
        start_stagger_s = opt_int("DECKLINK_START_STAGGER_S", 0)
        if start_stagger_s < 0:
            raise ConfigError(
                f"DECKLINK_START_STAGGER_S must be an integer seconds, got {start_stagger_s}"
            )

    hold_last_s = opt_float("SIGNAL_LOSS_HOLD_S", 15.0)
    if hold_last_s < 0:
        raise ConfigError(f"SIGNAL_LOSS_HOLD_S must be >= 0, got {hold_last_s}")

    auto_park_bin = opt("AUTO_PARK_BIN", "/usr/local/bin/nexvue-encode-auto-park.sh")

    return EncodeConfig(
        channel_id=channel_id,
        channel_path=channel_path,
        device_number=device_number,
        max_devices=max_devices,
        deint_fields=deint_fields,
        deint_method=deint_method,
        bitrate_kbps=bitrate_kbps,
        gop_frames=gop_frames,
        enable_audio=enable_audio,
        audio_layout=audio_layout,
        audio_embeds=audio_embeds,
        audio_bitrate_bps=audio_bitrate_bps,
        audio_frame_ms=audio_frame_ms,
        audio_queue_buffers=audio_queue_buffers,
        audio_resample_quality=audio_resample_quality,
        decklink_buffer_frames=decklink_buffer_frames,
        watchdog_ms=watchdog_ms,
        output_width=output_width,
        output_height=output_height,
        output_fps=output_fps,
        rtsp_url=rtsp_url,
        video_encoder=video_encoder,
        extra_enc_args=extra_enc_args,
        lo_enable=lo_enable,
        lo_preset=lo_preset,
        lo_fps=lo_fps,
        lo_rtsp_url=lo_rtsp_url,
        lo_width=lo_width,
        lo_height=lo_height,
        lo_bitrate_kbps=lo_bitrate_kbps,
        lo_target_usage=lo_target_usage,
        lo_queue_buffers=lo_queue_buffers,
        captions_enable=captions_enable,
        captions_pipeline_only=captions_pipeline_only,
        captions_dir=captions_dir,
        captions_decode_bin=captions_decode_bin,
        captions_fifo=captions_fifo,
        open_delay_s=open_delay_s,
        open_backoff_s=open_backoff_s,
        open_backoff_cap_s=open_backoff_cap_s,
        hang_kill_s=hang_kill_s,
        open_gate_s=open_gate_s,
        start_stagger_s=start_stagger_s,
        hold_last_s=hold_last_s,
        auto_park_bin=auto_park_bin,
    )


# ---------------------------------------------------------------------------
# Pipeline assembly (pure strings — no GI)
# ---------------------------------------------------------------------------
def build_encoder_desc(cfg: EncodeConfig, bitrate_kbps: int, *, for_lo: bool = False) -> str:
    extra = f" {cfg.extra_enc_args}" if cfg.extra_enc_args else ""
    if cfg.video_encoder == "vah264enc":
        usage = cfg.lo_target_usage if for_lo else 7
        return (
            f"vah264enc rate-control=cbr bitrate={bitrate_kbps} "
            f"key-int-max={cfg.gop_frames} b-frames=0 target-usage={usage}{extra}"
        )
    return (
        f"x264enc tune=zerolatency speed-preset=veryfast bitrate={bitrate_kbps} "
        f"key-int-max={cfg.gop_frames} bframes=0{extra}"
    )


def _norm_caps(cfg: EncodeConfig) -> str:
    return (
        f"video/x-raw,format=NV12,width={cfg.output_width},height={cfg.output_height},"
        f"framerate={cfg.output_fps},pixel-aspect-ratio=1/1"
    )


def _audio_remix_desc() -> str:
    parts = ["deinterleave name=adl", "interleave name=ali"]
    for src, sink, mask in AUDIO_POSITION_MASKS:
        parts.append(
            f"adl.src_{src} ! queue max-size-buffers=8 leaky=downstream "
            f"! audioconvert ! audio/x-raw,channels=1,channel-mask=(bitmask){mask} "
            f"! ali.sink_{sink}"
        )
    return " ".join(parts)


def capture_pipeline_desc(cfg: EncodeConfig) -> str:
    parts = [
        f"decklinkvideosrc device-number={cfg.device_number} mode=auto "
        f"buffer-size={cfg.decklink_buffer_frames} drop-no-signal-frames=false"
    ]
    if cfg.captions_enable:
        parts[0] += " output-cc=true"
    parts.append("! queue max-size-buffers=4 leaky=downstream")
    if cfg.captions_enable:
        parts.append("! ccextractor name=cc")
        parts.append("cc. ! queue max-size-buffers=4 leaky=downstream")
    if cfg.watchdog_ms > 0:
        parts.append(f"! watchdog timeout={cfg.watchdog_ms}")
    parts.append(f"! deinterlace fields={cfg.deint_fields} method={cfg.deint_method}")
    parts.append("! videorate ! videoscale ! videoconvert")
    parts.append(f"! {_norm_caps(cfg)}")
    parts.append("! appsink name=vasink emit-signals=true max-buffers=2 drop=true sync=false")

    if cfg.captions_enable:
        parts.append(
            "cc.caption ! queue max-size-buffers=8 leaky=downstream "
            "! ccconverter ! closedcaption/x-cea-608,format=raw "
            f"! filesink location={cfg.captions_fifo} buffer-mode=unbuffered "
            "sync=false append=false"
        )

    if cfg.enable_audio:
        q = cfg.audio_queue_buffers
        parts.append(
            f"decklinkaudiosrc device-number={cfg.device_number} channels=8 "
            f"! queue max-size-buffers={q} leaky=downstream "
            f"! audiorate ! audioconvert ! audioresample quality={cfg.audio_resample_quality} "
            "! audio/x-raw,rate=48000,channels=8 "
            f"! {_audio_remix_desc()} "
            "ali. ! audioconvert "
            "! audio/x-raw,format=S16LE,rate=48000,channels=8,channel-mask=(bitmask)0xc3f "
            "! appsink name=aasink emit-signals=true max-buffers=8 drop=true sync=false"
        )
    return " ".join(parts)


def publish_pipeline_desc(cfg: EncodeConfig) -> str:
    parts = [
        f"rtspclientsink name=sink location={cfg.rtsp_url} protocols=tcp",
    ]
    if cfg.lo_enable:
        parts.append(f"rtspclientsink name=sinklo location={cfg.lo_rtsp_url} protocols=tcp")

    parts.append(
        "appsrc name=vsrc is-live=true format=time do-timestamp=false block=false "
        f"! {_norm_caps(cfg)} "
        "! queue max-size-buffers=4 leaky=downstream"
    )
    hi = build_encoder_desc(cfg, cfg.bitrate_kbps)
    if cfg.lo_enable:
        lo = build_encoder_desc(cfg, cfg.lo_bitrate_kbps, for_lo=True)
        lo_caps = (
            f"video/x-raw,format=NV12,width={cfg.lo_width},height={cfg.lo_height},"
            f"framerate={cfg.lo_fps},pixel-aspect-ratio=1/1"
        )
        parts.append("! tee name=vt")
        parts.append(f"vt. ! queue max-size-buffers=4 leaky=downstream ! {hi} ! h264parse config-interval=-1 ! sink.")
        parts.append(
            f"vt. ! queue max-size-buffers={cfg.lo_queue_buffers} leaky=downstream "
            f"! videorate qos=false ! videoscale qos=false "
            f"! {lo_caps} ! {lo} ! h264parse config-interval=-1 ! sinklo."
        )
    else:
        parts.append(f"! {hi} ! h264parse config-interval=-1 ! sink.")

    if cfg.enable_audio:
        q = cfg.audio_queue_buffers
        parts.append(
            "appsrc name=asrc is-live=true format=time do-timestamp=false block=false "
            "! audio/x-raw,format=S16LE,rate=48000,channels=8,channel-mask=(bitmask)0xc3f "
            f"! opusenc bitrate={cfg.audio_bitrate_bps} frame-size={cfg.audio_frame_ms}"
        )
        if cfg.lo_enable:
            parts.append("! tee name=at")
            parts.append(f"at. ! queue max-size-buffers={q} leaky=downstream ! sink.")
            parts.append(f"at. ! queue max-size-buffers={q} leaky=downstream ! sinklo.")
        else:
            parts.append("! sink.")
    return " ".join(parts)


def print_pipelines(cfg: EncodeConfig) -> str:
    return capture_pipeline_desc(cfg) + " " + publish_pipeline_desc(cfg)


def parse_decklink_probe(json_text: str, device_number: int) -> str:
    try:
        data = json.loads(json_text)
    except (TypeError, ValueError):
        return "error"
    for dev in data.get("devices") or []:
        try:
            if int(dev.get("index", -1)) != device_number:
                continue
        except (TypeError, ValueError):
            continue
        if dev.get("busy") is True:
            return "busy"
        if dev.get("input_locked") is True:
            return "locked"
        return "unlocked"
    return "error"


def probe_decklink(device_number: int, status_bin: str = "") -> str:
    bin_path = status_bin
    if not bin_path:
        for candidate in ("/usr/local/bin/decklink-status", "decklink-status"):
            if candidate.startswith("/") and os.access(candidate, os.X_OK):
                bin_path = candidate
                break
            found = _which(candidate)
            if found:
                bin_path = found
                break
    if not bin_path:
        return "skip"
    try:
        out = subprocess.check_output([bin_path], stderr=subprocess.DEVNULL, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return "error"
    return parse_decklink_probe(out.decode("utf-8", "replace"), device_number)


def _which(name: str) -> str:
    from shutil import which

    return which(name) or ""


# ---------------------------------------------------------------------------
# Runtime (GI)
# ---------------------------------------------------------------------------
class EncodeRuntime:
    def __init__(self, cfg: EncodeConfig) -> None:
        self.cfg = cfg
        self._loop = None
        self._pub = None
        self._cap = None
        self._vsrc = None
        self._asrc = None
        self._exit_code = 0
        self._stopping = False
        self._been_live = False
        self._cap_failures = 0
        self._cap_saw_error = False
        self._cap_open_mono = 0.0
        self._cap_retry_id = 0
        self._pub_rebuild_id = 0
        self._lock = threading.Lock()
        self._last_video: Optional[bytes] = None
        self._last_audio: Optional[bytes] = None
        self._last_video_mono = 0.0
        self._last_audio_mono = 0.0
        self._black = make_black_nv12(cfg.output_width, cfg.output_height)
        self._silence = make_silence_s16(8, 48000, cfg.audio_frame_ms)
        self._v_n = 0
        self._a_n = 0
        self._v_dur = fps_duration_ns(cfg.output_fps)
        self._a_dur = cfg.audio_frame_ms * 1_000_000
        self._v_start = 0.0
        self._a_start = 0.0
        self._captions_proc: Optional[subprocess.Popen] = None
        self._pump_started = False
        self._use_captions = cfg.captions_enable

    def _capture_cfg(self) -> EncodeConfig:
        if self._use_captions == self.cfg.captions_enable:
            return self.cfg
        return replace(self.cfg, captions_enable=self._use_captions)

    def run(self) -> int:
        Gst.init(None)
        if self.cfg.video_encoder == "vah264enc" and Gst.ElementFactory.find("vah264enc") is None:
            log.error(
                "vah264enc unavailable — check intel-media-va-driver-non-free and "
                "/dev/dri perms, or set VIDEO_ENCODER=x264enc"
            )
            return 69
        if self._use_captions and (
            Gst.ElementFactory.find("ccextractor") is None
            or Gst.ElementFactory.find("ccconverter") is None
        ):
            log.warning("CAPTIONS_ENABLE=true but ccextractor/ccconverter unavailable — captions off")
            self._use_captions = False
        self._loop = GLib.MainLoop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)

        log.info(
            "starting: device=%s path=%s deint=%s/%s hi=%skbps lo=%s(%skbps) "
            "audio=%s layout=%s(8ch@%sbps embeds=%s) captions=%s enc=%s",
            self.cfg.device_number,
            self.cfg.channel_path,
            self.cfg.deint_fields,
            self.cfg.deint_method,
            self.cfg.bitrate_kbps,
            self.cfg.lo_enable,
            self.cfg.lo_bitrate_kbps,
            self.cfg.enable_audio,
            self.cfg.audio_layout,
            self.cfg.audio_bitrate_bps,
            self.cfg.audio_embeds or "1-8",
            self._use_captions,
            self.cfg.video_encoder,
        )
        log.info(
            "publishing HI to %s%s",
            self.cfg.rtsp_url,
            f", LO to {self.cfg.lo_rtsp_url}" if self.cfg.lo_enable else "",
        )

        if not self._start_publish():
            return 1
        self._start_pumps()
        delay_ms = int((self.cfg.start_stagger_s + self.cfg.open_delay_s) * 1000)
        if self.cfg.start_stagger_s:
            log.info("start stagger %ss (slot %s)", self.cfg.start_stagger_s, self.cfg.channel_id)
        if self.cfg.open_delay_s:
            log.info("DeckLink open settle %ss", self.cfg.open_delay_s)
        GLib.timeout_add(max(delay_ms, 1), self._start_capture_first)
        try:
            self._loop.run()
        finally:
            self._shutdown()
        return self._exit_code

    def _on_signal(self, signum, _frame) -> None:
        log.info("signal %s — stopping", signum)
        self._stopping = True
        self._exit_code = 0
        if self._loop is not None:
            self._loop.quit()

    def _start_publish(self) -> bool:
        desc = publish_pipeline_desc(self.cfg)
        try:
            self._pub = Gst.parse_launch(desc)
        except Exception as exc:  # noqa: BLE001
            log.error("publish parse failed: %s", exc)
            return False
        self._vsrc = self._pub.get_by_name("vsrc")
        self._asrc = self._pub.get_by_name("asrc")
        bus = self._pub.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_pub_bus)
        ret = self._pub.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error("publish failed to start")
            return False
        log.info("publish pipeline PLAYING (black/silence until capture)")
        return True

    def _rebuild_publish(self) -> bool:
        log.warning("rebuilding publish pipeline (RTSP/encoder recover)")
        old = self._pub
        self._pub = None
        self._vsrc = None
        self._asrc = None
        if old is not None:
            self._null_pipeline(old, "publish")
        self._v_n = 0
        self._a_n = 0
        self._v_start = time.monotonic()
        self._a_start = self._v_start
        return self._start_publish()

    def _start_pumps(self) -> None:
        if self._pump_started:
            return
        self._pump_started = True
        now = time.monotonic()
        self._v_start = now
        self._a_start = now
        GLib.timeout_add(1, self._video_tick)
        if self.cfg.enable_audio:
            GLib.timeout_add(1, self._audio_tick)

    def _video_tick(self) -> bool:
        if self._stopping or self._vsrc is None:
            return False
        now = time.monotonic()
        with self._lock:
            kind = video_hold_kind(
                has_frame=self._last_video is not None,
                last_mono=self._last_video_mono,
                now=now,
                hold_last_s=self.cfg.hold_last_s,
            )
            if kind == "black":
                payload = self._black
            else:
                payload = self._last_video or self._black
        self._push_appsrc(self._vsrc, payload, self._v_n * self._v_dur, self._v_dur)
        self._v_n += 1
        target = self._v_start + (self._v_n * self._v_dur / 1_000_000_000)
        delay_ms = max(1, int((target - time.monotonic()) * 1000))
        GLib.timeout_add(delay_ms, self._video_tick)
        return False

    def _audio_tick(self) -> bool:
        if self._stopping or self._asrc is None:
            return False
        now = time.monotonic()
        with self._lock:
            age = now - self._last_audio_mono
            if self._last_audio is not None and age <= 0.25:
                payload = self._last_audio
            else:
                payload = self._silence
        self._push_appsrc(self._asrc, payload, self._a_n * self._a_dur, self._a_dur)
        self._a_n += 1
        target = self._a_start + (self._a_n * self._a_dur / 1_000_000_000)
        delay_ms = max(1, int((target - time.monotonic()) * 1000))
        GLib.timeout_add(delay_ms, self._audio_tick)
        return False

    def _push_appsrc(self, appsrc, payload: bytes, pts: int, duration: int) -> None:
        buf = Gst.Buffer.new_allocate(None, len(payload), None)
        buf.fill(0, payload)
        buf.pts = pts
        buf.dts = pts
        buf.duration = duration
        try:
            appsrc.emit("push-buffer", buf)
        except Exception as exc:  # noqa: BLE001
            log.debug("appsrc push failed: %s", exc)

    def _start_capture_first(self) -> bool:
        self._start_capture()
        return False

    def _start_capture(self) -> None:
        if self._stopping or self._cap is not None:
            return
        self._cap_saw_error = False
        self._cap_open_mono = time.monotonic()
        self._ensure_captions()
        desc = capture_pipeline_desc(self._capture_cfg())
        try:
            self._cap = Gst.parse_launch(desc)
        except Exception as exc:  # noqa: BLE001
            log.error("capture parse failed: %s", exc)
            self._on_capture_failed("parse")
            return
        vasink = self._cap.get_by_name("vasink")
        if vasink is not None:
            vasink.connect("new-sample", self._on_video_sample)
        aasink = self._cap.get_by_name("aasink")
        if aasink is not None:
            aasink.connect("new-sample", self._on_audio_sample)
        bus = self._cap.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_cap_bus)
        log.info("gst open attempt (capture) device=%s", self.cfg.device_number)
        ret = self._cap.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.warning("capture set_state PLAYING failed immediately")
            self._teardown_capture("set-state-fail")
            self._on_capture_failed("set-state")
            return
        GLib.timeout_add(200, self._poll_capture_open)

    def _poll_capture_open(self) -> bool:
        if self._stopping or self._cap is None:
            return False
        _ret, state, _pend = self._cap.get_state(0)
        if state == Gst.State.PLAYING:
            state_name = "PLAYING"
        elif state == Gst.State.PAUSED:
            state_name = "PAUSED"
        else:
            state_name = state.value_nick.upper() if hasattr(state, "value_nick") else str(state)
        status = capture_open_status(
            state=state_name,
            saw_error=self._cap_saw_error,
            elapsed_s=time.monotonic() - self._cap_open_mono,
            gate_s=float(self.cfg.open_gate_s),
        )
        if status == "wait":
            return True
        if status == "ok":
            log.info("capture PLAYING")
            self._cap_failures = 0
            return False
        log.warning(
            "capture open gate failed (state=%s error=%s after %.1fs) — teardown",
            state,
            self._cap_saw_error,
            time.monotonic() - self._cap_open_mono,
        )
        self._teardown_capture("open-gate")
        self._on_capture_failed("open-gate")
        return False

    def _on_video_sample(self, sink) -> int:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        data = _buffer_bytes(sample.get_buffer())
        if data:
            with self._lock:
                self._last_video = data
                self._last_video_mono = time.monotonic()
                self._been_live = True
        return Gst.FlowReturn.OK

    def _on_audio_sample(self, sink) -> int:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        data = _buffer_bytes(sample.get_buffer())
        if data:
            with self._lock:
                self._last_audio = data
                self._last_audio_mono = time.monotonic()
        return Gst.FlowReturn.OK

    def _on_cap_bus(self, _bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self._cap_saw_error = True
            log.warning("capture error: %s (%s)", err.message, debug)
            GLib.idle_add(self._handle_cap_error)
        elif message.type == Gst.MessageType.EOS:
            log.warning("capture EOS")
            self._cap_saw_error = True
            GLib.idle_add(self._handle_cap_error)

    def _handle_cap_error(self) -> bool:
        if self._stopping or self._cap is None:
            return False
        self._teardown_capture("error")
        self._on_capture_failed("error")
        return False

    def _on_pub_bus(self, _bus, message) -> None:
        if message.type != Gst.MessageType.ERROR:
            return
        err, debug = message.parse_error()
        src = message.src
        name = ""
        try:
            name = src.get_name() if src is not None else ""
        except Exception:  # noqa: BLE001
            name = ""
        log.warning("publish error from %s: %s (%s)", name or "?", err.message, debug)
        if name in ("sink", "sinklo") or (name and name.startswith("sink")):
            if self._pub_rebuild_id:
                return
            self._pub_rebuild_id = GLib.timeout_add(500, self._do_rebuild_publish)
            return
        log.error("fatal publish/encoder error — exiting for systemd restart")
        self._exit_code = 1
        self._stopping = True
        if self._loop is not None:
            self._loop.quit()

    def _do_rebuild_publish(self) -> bool:
        self._pub_rebuild_id = 0
        if self._stopping:
            return False
        if not self._rebuild_publish():
            self._exit_code = 1
            self._stopping = True
            if self._loop is not None:
                self._loop.quit()
        return False

    def _on_capture_failed(self, why: str) -> None:
        if self._stopping:
            return
        self._cap_failures += 1
        probe = probe_decklink(self.cfg.device_number)
        decision = decide_capture_failure(
            been_live=self._been_live,
            probe=probe,
            failures=self._cap_failures,
            base_backoff_s=float(self.cfg.open_backoff_s),
            cap_s=float(self.cfg.open_backoff_cap_s),
        )
        log.info(
            "device %s: probe=%s been_live=%s failures=%s (%s) — %s",
            self.cfg.device_number,
            probe,
            self._been_live,
            self._cap_failures,
            why,
            decision.reason,
        )
        if decision.action == "exit_unlocked":
            log.info("auto-park: unlocked start (cycle) — exiting for systemd restart")
            self._exit_code = 1
            self._stopping = True
            if self._loop is not None:
                self._loop.quit()
            return
        delay_ms = int(decision.backoff_s * 1000)
        log.info("retrying capture in %ss", decision.backoff_s)
        if self._cap_retry_id:
            GLib.source_remove(self._cap_retry_id)
        self._cap_retry_id = GLib.timeout_add(max(delay_ms, 1), self._retry_capture)

    def _retry_capture(self) -> bool:
        self._cap_retry_id = 0
        self._start_capture()
        return False

    def _teardown_capture(self, why: str) -> None:
        cap = self._cap
        self._cap = None
        if cap is None:
            return
        log.info("tearing down capture (%s)", why)
        self._null_pipeline(cap, "capture")
        self._stop_captions_proc()

    def _null_pipeline(self, pipeline, label: str) -> None:
        done = threading.Event()

        def _null() -> None:
            try:
                bus = pipeline.get_bus()
                try:
                    bus.remove_signal_watch()
                except Exception:  # noqa: BLE001
                    pass
                pipeline.set_state(Gst.State.NULL)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s NULL failed: %s", label, exc)
            finally:
                done.set()

        t = threading.Thread(target=_null, name=f"null-{label}", daemon=True)
        t.start()
        if not done.wait(self.cfg.hang_kill_s):
            log.error("%s set_state(NULL) hung %ss — exiting for systemd restart", label, self.cfg.hang_kill_s)
            os._exit(1)

    def _ensure_captions(self) -> None:
        if not self._use_captions or self.cfg.captions_pipeline_only:
            return
        if self._captions_proc is not None and self._captions_proc.poll() is None:
            return
        fifo = Path(self.cfg.captions_fifo)
        if fifo.as_posix() == "/dev/null":
            return
        decode = Path(self.cfg.captions_decode_bin)
        if not decode.is_file():
            log.warning("captions decode helper missing (%s) — captions off this cycle", decode)
            return
        fifo.parent.mkdir(parents=True, exist_ok=True)
        if fifo.exists():
            try:
                fifo.unlink()
            except OSError:
                pass
        try:
            os.mkfifo(fifo, 0o644)
        except OSError as exc:
            log.warning("could not create captions FIFO %s: %s", fifo, exc)
            return
        try:
            self._captions_proc = subprocess.Popen(
                [
                    sys.executable,
                    str(decode),
                    "--channel",
                    self.cfg.channel_path,
                    "--fifo",
                    str(fifo),
                    "--state-dir",
                    self.cfg.captions_dir,
                ],
            )
        except OSError as exc:
            log.warning("could not start captions decoder: %s", exc)
            self._captions_proc = None

    def _stop_captions_proc(self) -> None:
        proc = self._captions_proc
        self._captions_proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        fifo = Path(self.cfg.captions_fifo)
        if fifo.as_posix() != "/dev/null":
            try:
                fifo.unlink()
            except OSError:
                pass

    def _shutdown(self) -> None:
        self._stopping = True
        self._teardown_capture("shutdown")
        if self._pub is not None:
            self._null_pipeline(self._pub, "publish")
            self._pub = None
        self._stop_captions_proc()


def _buffer_bytes(buf) -> bytes:
    if buf is None:
        return b""
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return b""
    try:
        return bytes(info.data)
    finally:
        buf.unmap(info)


def run_auto_park_check(cfg: EncodeConfig) -> int:
    """Return 0 to continue, 1 to cycle, 75 to park. Missing helper → 0."""
    if not cfg.auto_park_bin or not os.access(cfg.auto_park_bin, os.X_OK):
        return 0
    if cfg.channel_id < 0:
        return 0
    try:
        proc = subprocess.run(
            [cfg.auto_park_bin, "check", str(cfg.channel_id), str(cfg.device_number)],
            check=False,
        )
    except OSError as exc:
        log.warning("auto-park check failed: %s — continuing encode", exc)
        return 0
    return proc.returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="NexVUE resilient encoder")
    parser.add_argument(
        "--print-pipeline",
        action="store_true",
        help="Print capture+publish gst descriptions and exit (assembly tests)",
    )
    args = parser.parse_args(argv)
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        log.error("%s", exc)
        return exc.exit_code

    if args.print_pipeline or os.environ.get("NEXVUE_ENCODE_PRINT_PIPELINE") in ("1", "true"):
        print(print_pipelines(cfg))
        return 0

    park_rc = run_auto_park_check(cfg)
    if park_rc == 1:
        log.info("auto-park: unlocked start (cycle) — exiting for systemd restart")
        return 1
    if park_rc == 75:
        log.info("auto-park: unlock threshold reached — exiting 75 for disable")
        return 75
    if park_rc not in (0,):
        log.warning("auto-park check rc=%s — continuing encode", park_rc)

    if not GST_AVAILABLE:
        log.error("PyGObject/GStreamer GI missing — install python3-gi gir1.2-gstreamer-1.0")
        return 69

    return EncodeRuntime(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
