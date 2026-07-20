#!/usr/bin/env python3
"""
nexvue-supervisor.py — Phase 1.5 persistent RTSP publisher with live-source <->
NO SIGNAL slate switching (replaces the bare gst-launch ExecStart in
nexvue-encode@.service).

Goal (README "Phase 1.5 supervisor — specification"): eliminate the
no-signal-at-boot restart loop. A channel with no live lock at start (or
that loses lock later) serves a generated slate instead of failing/restart-
looping — viewers stay connected (same RTSP/WHEP session, same output caps);
only the picture changes.

Architecture:
    nexvue-encode@N.service (ExecStart) -> this process, one per channel
        gst pipeline: persistent slate (videotestsrc+textoverlay,
        silent audiotestsrc) + input-selector, with a DYNAMIC live source
        bin (DeckLink SDI or SRT decode) added/removed as it comes
        up/errors. Downstream of the selectors is unchanged from
        nexvue-encode.sh: normalize -> HI encode (+ optional LO tee) ->
        rtspclientsink(s); audio -> shared opusenc.
        SRT always decode+re-encode so slate / normalize / LO stay valid.
        MediaMTX (H.264 + Opus, no transcoding) is untouched.
        Station-wide MAX_LO_RENDITIONS (default 6) clamps which channels
        that request LO_ENABLE actually build the LO tee (deterministic
        by ascending channel id among requesters).

Stdlib only, no pip (project policy). GStreamer access is via PyGObject
(python3-gi + gir1.2-gstreamer-1.0 etc., from apt — see setup.sh) and is
strictly optional at IMPORT time: this module must load (and its pure
helpers — load_config, StateMachine — must be unit-testable) on a machine
with no GI installed at all. Only main()/Supervisor.run() require GI, and
main() exits 69 with a clear message if it is missing.
"""
from __future__ import annotations

import enum
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

LOG_PREFIX = "[nexvue-supervisor]"
LOG_LEVEL = os.environ.get("NEXVUE_SUPERVISOR_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format=f"{LOG_PREFIX} %(message)s")
log = logging.getLogger("nexvue-supervisor")

# Station channel slots are 0..MAX_CHANNELS-1 (independent of DeckLink
# MAX_DEVICES). SRT-only channels can use ids above the card's connector count.
DEFAULT_MAX_CHANNELS = 10
DEFAULT_MAX_LO_RENDITIONS = 6
DEFAULT_CHANNELS_DIR = Path("/etc/nexvue/channels")
# SRT: treat "no decoded video buffer for this long" as signal=false so the
# existing LIVE→SLATE loss debounce can run (srtsrc has no DeckLink "signal").
SRT_SIGNAL_STALE_S = 1.0

# ---------------------------------------------------------------------------
# GStreamer / PyGObject — optional at import time (see module docstring).
# ---------------------------------------------------------------------------
try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib, Gst  # noqa: E402  (import after require_version, by design)

    try:
        gi.require_version("GstVideo", "1.0")
        from gi.repository import GstVideo  # noqa: E402
    except (ImportError, ValueError):
        GstVideo = None  # force-keyframe falls back to a hand-built event

    GST_AVAILABLE = True
except (ImportError, ValueError):
    Gst = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]
    GstVideo = None
    GST_AVAILABLE = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    """Raised by load_config for a missing/invalid environment value.

    exit_code mirrors nexvue-encode.sh's convention: 1 for a required value
    that is simply absent (bash's ``${VAR:?msg}``), 64 (EX_USAGE) for a
    present-but-invalid value.
    """

    def __init__(self, message: str, exit_code: int = 64) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# LO framerates accepted by Settings / ops (must be a GStreamer fraction).
LO_FPS_ALLOWED = frozenset({"60000/1001", "30000/1001", "15000/1001"})
# Hand-edited / legacy typos that used to produce framerate=(int)N and break
# videoscale→vah264enc linking (restart storm). Map to the curated fraction.
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


def normalize_lo_fps(value: str) -> str:
    """Return a curated LO_FPS fraction, or the stripped input for allowlist check."""
    v = (value or "").strip()
    if not v:
        return "30000/1001"
    return LO_FPS_ALIASES.get(v, LO_FPS_ALIASES.get(v.lower(), v))

# LO_PRESET ladder -> (width, height, default bitrate kbps). Matches
# nexvue-encode.sh exactly — the "p" number is the HEIGHT (480p = 854x480).
# Bitrate defaults sized for CBR at LO target-usage=7 (same speed tier as HI).
# Lower LO_TARGET_USAGE values look "sharper" but send QoS upstream and the
# LO videorate/videoscale path then over-drops — can land near ~1 fps.
LO_PRESETS: Dict[str, Tuple[int, int, int]] = {
    "720p": (1280, 720, 2500),
    "540p": (960, 540, 1500),
    "480p": (854, 480, 1200),
    "360p": (640, 360, 800),
    "240p": (426, 240, 500),
    "180p": (320, 180, 350),
}


@dataclass(frozen=True)
class SupervisorConfig:
    """Immutable, fully-validated view of the channel environment. Built
    once by load_config() — every field here is already sane, so pipeline
    code never re-validates."""

    channel_id: int
    channel_path: str
    input_type: str = "decklink"  # decklink | srt
    device_number: int = 0
    max_devices: int = 8
    max_channels: int = DEFAULT_MAX_CHANNELS
    srt_uri: str = ""
    srt_latency_ms: int = 120
    deint_fields: str = "all"
    bitrate_kbps: int = 5000
    gop_frames: int = 60
    enable_audio: bool = True
    audio_bitrate_bps: int = 128000
    audio_channels: int = 2
    audio_frame_ms: int = 10
    audio_queue_buffers: int = 100
    audio_resample_quality: int = 9
    decklink_buffer_frames: int = 2
    # 0 = disabled (default). The Phase 1 gst-launch pipeline used 3000 ms, but
    # that fights SIGNAL_LOSS_DEBOUNCE_S (15s): a brief unlock would ERROR the
    # DeckLink bin via watchdog long before the state machine could ride it
    # out as black frames. Signal property + debounce replace the watchdog.
    watchdog_ms: int = 0
    output_width: int = 1920
    output_height: int = 1080
    rtsp_url: str = ""
    video_encoder: str = "vah264enc"
    extra_enc_args: str = ""
    lo_requested: bool = False
    lo_enable: bool = False
    max_lo_renditions: int = DEFAULT_MAX_LO_RENDITIONS
    lo_preset: str = "720p"
    lo_fps: str = "30000/1001"
    lo_rtsp_url: str = ""
    lo_width: int = 1280
    lo_height: int = 720
    lo_bitrate_kbps: int = 2500
    # LO quality / buffering (default usage=7 matches HI speed; lower = sharper but
    # risks QoS-driven frame starvation on the LO scale/rate branch).
    lo_target_usage: int = 7
    lo_queue_buffers: int = 16
    lo_gop_frames: int = 60
    captions_enable: bool = True
    captions_dir: str = "/run/nexvue/captions"
    captions_decode_bin: str = "/usr/local/bin/nexvue-captions-decode.py"
    channel_alias: str = ""
    # Phase 1.5 knobs (README "Phase 1.5 supervisor" state machine section).
    signal_loss_debounce_s: float = 15.0
    signal_acquire_debounce_s: float = 1.0
    # Retry delay after live-source branch ERROR/EOS (env: DECKLINK_RETRY_S /
    # SOURCE_RETRY_S — same knob for DeckLink and SRT).
    live_retry_s: float = 3.0
    # Derived, not read directly from the environment.
    output_fps: str = "60000/1001"


def _parse_env_lo_enable(text: str) -> bool:
    """Last active LO_ENABLE assignment in a channel .env body."""
    last: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != "LO_ENABLE":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[0] == value[-1]:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        last = value.lower()
    return last == "true"


def list_lo_requester_ids(channels_dir: Path, max_channels: int = DEFAULT_MAX_CHANNELS) -> List[int]:
    """Channel ids with LO_ENABLE=true, sorted ascending (deterministic pool order)."""
    ids: List[int] = []
    if not channels_dir.is_dir():
        return ids
    for path in channels_dir.glob("*.env"):
        stem = path.stem
        if not stem.isdigit():
            continue
        cid = int(stem)
        if not (0 <= cid < max_channels):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _parse_env_lo_enable(text):
            ids.append(cid)
    ids.sort()
    return ids


def resolve_lo_enable(
    channel_id: int,
    requested: bool,
    *,
    channels_dir: Path,
    max_lo: int,
    max_channels: int = DEFAULT_MAX_CHANNELS,
) -> bool:
    """Floating LO pool: among requesters (ascending id), only the first max_lo win."""
    if not requested:
        return False
    if max_lo <= 0:
        return False
    requesters = list_lo_requester_ids(channels_dir, max_channels=max_channels)
    # Count this process even if the on-disk scan missed our file (unit tests,
    # or a write that has not hit the directory the supervisor was pointed at).
    if channel_id not in requesters:
        requesters = sorted(set(requesters) | {channel_id})
    winners = requesters[:max_lo]
    return channel_id in winners


def load_config(
    env: Mapping[str, str],
    *,
    channels_dir: Optional[Path] = None,
) -> SupervisorConfig:
    """Validate os.environ (or any string mapping, for tests) into a
    SupervisorConfig. Raises ConfigError on the first problem found —
    mirrors nexvue-encode.sh's fail-fast validation block.

    channels_dir is used only for the floating LO pool clamp (default
    /etc/nexvue/channels, overridable via NEXVUE_CHANNELS_DIR).
    """

    def raw(name: str) -> Optional[str]:
        v = env.get(name)
        if v is None:
            return None
        v = v.strip()
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
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {v!r}") from None

    def opt_float(name: str, default: float) -> float:
        v = raw(name)
        if v is None:
            return default
        try:
            return float(v)
        except ValueError:
            raise ConfigError(f"{name} must be a number, got {v!r}") from None

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

    input_type = opt("INPUT_TYPE", "decklink").lower()
    if input_type not in ("decklink", "srt"):
        raise ConfigError(f"INPUT_TYPE must be 'decklink' or 'srt', got {input_type!r}")

    channel_path = required("CHANNEL_PATH")
    if not channel_path or not all(c.isalnum() or c in "-_" for c in channel_path):
        raise ConfigError(f"CHANNEL_PATH must be alphanumeric (with -/_ ), got {channel_path!r}")

    max_channels = opt_int("MAX_CHANNELS", DEFAULT_MAX_CHANNELS)
    if not (1 <= max_channels <= 16):
        raise ConfigError(f"MAX_CHANNELS must be an integer 1-16, got {max_channels}")

    max_devices = opt_int("MAX_DEVICES", 8)
    if not (1 <= max_devices <= 8):
        raise ConfigError(f"MAX_DEVICES must be an integer 1-8, got {max_devices}")

    srt_uri = ""
    srt_latency_ms = opt_int("SRT_LATENCY_MS", 120)
    if srt_latency_ms < 0:
        raise ConfigError(f"SRT_LATENCY_MS must be >= 0, got {srt_latency_ms}")

    if input_type == "decklink":
        device_number_raw = required("DEVICE_NUMBER")
        try:
            device_number = int(device_number_raw)
        except ValueError:
            raise ConfigError(f"DEVICE_NUMBER must be an integer, got {device_number_raw!r}") from None
        if not (0 <= device_number < max_devices):
            raise ConfigError(
                f"DEVICE_NUMBER must be 0-{max_devices - 1} for this card "
                f"(MAX_DEVICES={max_devices}), got {device_number}"
            )
        channel_id = opt_int("CHANNEL_ID", device_number)
    else:
        srt_uri = required("SRT_URI")
        if not srt_uri.lower().startswith("srt://"):
            raise ConfigError(f"SRT_URI must start with srt://, got {srt_uri!r}")
        # Optional; unused for capture. Kept so mixed stations can still store a slot hint.
        device_number = opt_int("DEVICE_NUMBER", -1)
        channel_id = opt_int("CHANNEL_ID", -1)
        if channel_id < 0:
            # Derive from CHANNEL_PATH like ch8 → 8 when CHANNEL_ID was not exported.
            suffix = channel_path[2:] if channel_path.startswith("ch") else ""
            if suffix.isdigit():
                channel_id = int(suffix)
            else:
                raise ConfigError(
                    "CHANNEL_ID is required for INPUT_TYPE=srt when CHANNEL_PATH is not chN",
                    exit_code=1,
                )

    if not (0 <= channel_id < max_channels):
        raise ConfigError(
            f"CHANNEL_ID must be 0-{max_channels - 1} (MAX_CHANNELS={max_channels}), got {channel_id}"
        )

    deint_fields = opt("DEINT_FIELDS", "all")
    if deint_fields not in ("all", "top"):
        raise ConfigError(f"DEINT_FIELDS must be 'all' or 'top', got {deint_fields!r}")
    output_fps = "60000/1001" if deint_fields == "all" else "30000/1001"

    bitrate_kbps = opt_int("BITRATE_KBPS", 5000)
    if bitrate_kbps <= 0:
        raise ConfigError(f"BITRATE_KBPS must be positive, got {bitrate_kbps}")

    gop_frames = opt_int("GOP_FRAMES", 60)
    if gop_frames <= 0:
        raise ConfigError(f"GOP_FRAMES must be positive, got {gop_frames}")

    enable_audio = opt_bool("ENABLE_AUDIO", True)
    audio_bitrate_bps = opt_int("AUDIO_BITRATE_BPS", 128000)
    if audio_bitrate_bps <= 0:
        raise ConfigError(f"AUDIO_BITRATE_BPS must be positive, got {audio_bitrate_bps}")

    audio_channels = opt_int("AUDIO_CHANNELS", 2)
    if audio_channels not in (2, 8, 16):
        raise ConfigError(f"AUDIO_CHANNELS must be 2, 8, or 16 (SDI embedded pairs), got {audio_channels}")

    audio_frame_ms = opt_int("AUDIO_FRAME_MS", 10)
    if audio_frame_ms not in (2, 5, 10, 20, 40, 60):
        raise ConfigError(f"AUDIO_FRAME_MS must be one of 2,5,10,20,40,60, got {audio_frame_ms}")

    audio_queue_buffers = opt_int("AUDIO_QUEUE_BUFFERS", 100)
    if audio_queue_buffers < 1:
        raise ConfigError(f"AUDIO_QUEUE_BUFFERS must be a positive integer, got {audio_queue_buffers}")

    audio_resample_quality = opt_int("AUDIO_RESAMPLE_QUALITY", 9)
    if not (0 <= audio_resample_quality <= 10):
        raise ConfigError(f"AUDIO_RESAMPLE_QUALITY must be an integer 0-10, got {audio_resample_quality}")

    decklink_buffer_frames = opt_int("DECKLINK_BUFFER_FRAMES", 2)
    if decklink_buffer_frames < 1:
        raise ConfigError(f"DECKLINK_BUFFER_FRAMES must be a positive integer, got {decklink_buffer_frames}")

    watchdog_ms = opt_int("WATCHDOG_MS", 0)
    if watchdog_ms < 0:
        raise ConfigError(f"WATCHDOG_MS must be >= 0, got {watchdog_ms}")

    output_width = opt_int("OUTPUT_WIDTH", 1920)
    output_height = opt_int("OUTPUT_HEIGHT", 1080)
    if output_width <= 0 or output_width % 2 or output_height <= 0 or output_height % 2:
        raise ConfigError(
            f"OUTPUT_WIDTH/OUTPUT_HEIGHT must be positive even integers, got {output_width}x{output_height}"
        )

    rtsp_url = opt("RTSP_URL", f"rtsp://127.0.0.1:8554/{channel_path}")

    video_encoder = opt("VIDEO_ENCODER", "vah264enc")
    if video_encoder not in ("vah264enc", "x264enc"):
        raise ConfigError(f"VIDEO_ENCODER must be 'vah264enc' or 'x264enc', got {video_encoder!r}")

    extra_enc_args = opt("EXTRA_ENC_ARGS", "")

    lo_requested = opt_bool("LO_ENABLE", False)
    max_lo_renditions = opt_int("MAX_LO_RENDITIONS", DEFAULT_MAX_LO_RENDITIONS)
    if max_lo_renditions < 0:
        raise ConfigError(f"MAX_LO_RENDITIONS must be >= 0, got {max_lo_renditions}")

    pool_dir = channels_dir
    if pool_dir is None:
        override = raw("NEXVUE_CHANNELS_DIR")
        pool_dir = Path(override) if override else DEFAULT_CHANNELS_DIR
    lo_enable = resolve_lo_enable(
        channel_id,
        lo_requested,
        channels_dir=pool_dir,
        max_lo=max_lo_renditions,
        max_channels=max_channels,
    )
    if lo_requested and not lo_enable:
        requesters = list_lo_requester_ids(pool_dir, max_channels=max_channels)
        log.warning(
            "LO_ENABLE requested on channel %d but floating pool is full "
            "(MAX_LO_RENDITIONS=%d; requesters=%s; winners=%s) — running HI-only",
            channel_id,
            max_lo_renditions,
            requesters,
            requesters[:max_lo_renditions],
        )

    lo_preset = opt("LO_PRESET", "720p")
    if lo_preset not in LO_PRESETS:
        raise ConfigError(f"LO_PRESET must be one of {','.join(LO_PRESETS)}, got {lo_preset!r}")
    lo_w_def, lo_h_def, lo_br_def = LO_PRESETS[lo_preset]
    lo_width = opt_int("LO_WIDTH", lo_w_def)
    lo_height = opt_int("LO_HEIGHT", lo_h_def)
    lo_bitrate_kbps = opt_int("LO_BITRATE_KBPS", lo_br_def)
    if lo_enable or lo_requested:
        if lo_width <= 0 or lo_width % 2 or lo_height <= 0 or lo_height % 2:
            raise ConfigError(f"LO_WIDTH/LO_HEIGHT must be positive even integers, got {lo_width}x{lo_height}")
        if lo_bitrate_kbps <= 0:
            raise ConfigError(f"LO_BITRATE_KBPS must be positive, got {lo_bitrate_kbps}")
    lo_fps = normalize_lo_fps(opt("LO_FPS", "30000/1001"))
    if lo_fps not in LO_FPS_ALLOWED:
        raise ConfigError(
            f"LO_FPS must be one of {', '.join(sorted(LO_FPS_ALLOWED))} "
            f"(or alias 60/30/15 / 59.94/29.97), got {opt('LO_FPS', '')!r}"
        )
    lo_rtsp_url = opt("LO_RTSP_URL", f"rtsp://127.0.0.1:8554/{channel_path}lo")

    # vah264enc target-usage: 1 = slow/best, 7 = fastest. LO defaults to 7
    # (same as HI / pre-supervisor). Lower values over-trigger QoS drops on LO.
    lo_target_usage = opt_int("LO_TARGET_USAGE", 7)
    if not (1 <= lo_target_usage <= 7):
        raise ConfigError(f"LO_TARGET_USAGE must be an integer 1-7, got {lo_target_usage}")
    lo_queue_buffers = opt_int("LO_QUEUE_BUFFERS", 16)
    if lo_queue_buffers < 1:
        raise ConfigError(f"LO_QUEUE_BUFFERS must be a positive integer, got {lo_queue_buffers}")
    # Blank / unset → inherit HI GOP_FRAMES.
    lo_gop_frames = opt_int("LO_GOP_FRAMES", gop_frames)
    if lo_gop_frames <= 0:
        raise ConfigError(f"LO_GOP_FRAMES must be positive, got {lo_gop_frames}")

    # CEA-608 extraction is DeckLink output-cc only; SRT has no equivalent path yet.
    captions_enable = opt_bool("CAPTIONS_ENABLE", True)
    if input_type == "srt":
        captions_enable = False
    captions_dir = opt("CAPTIONS_DIR", "/run/nexvue/captions")
    captions_decode_bin = opt("CAPTIONS_DECODE_BIN", "/usr/local/bin/nexvue-captions-decode.py")

    channel_alias = opt("CHANNEL_ALIAS", "")

    signal_loss_debounce_s = opt_float("SIGNAL_LOSS_DEBOUNCE_S", 15.0)
    if signal_loss_debounce_s < 0:
        raise ConfigError(f"SIGNAL_LOSS_DEBOUNCE_S must be >= 0, got {signal_loss_debounce_s}")
    signal_acquire_debounce_s = opt_float("SIGNAL_ACQUIRE_DEBOUNCE_S", 1.0)
    if signal_acquire_debounce_s < 0:
        raise ConfigError(f"SIGNAL_ACQUIRE_DEBOUNCE_S must be >= 0, got {signal_acquire_debounce_s}")
    # SOURCE_RETRY_S aliases DECKLINK_RETRY_S for SRT wording; either may be set.
    if raw("SOURCE_RETRY_S") is not None:
        live_retry_s = opt_float("SOURCE_RETRY_S", 3.0)
    else:
        live_retry_s = opt_float("DECKLINK_RETRY_S", 3.0)
    if live_retry_s <= 0:
        raise ConfigError(f"live source retry (DECKLINK_RETRY_S/SOURCE_RETRY_S) must be > 0, got {live_retry_s}")

    # A short watchdog undercuts loss debounce (tears down DeckLink before the
    # state machine can ride out a hiccup). Bump it if the operator set both.
    if watchdog_ms > 0:
        min_watchdog_ms = int((signal_loss_debounce_s + 5.0) * 1000)
        if watchdog_ms < min_watchdog_ms:
            log.warning(
                "WATCHDOG_MS=%d is shorter than SIGNAL_LOSS_DEBOUNCE_S+5s (%d); "
                "raising to %d so hiccups do not bypass slate debounce",
                watchdog_ms,
                min_watchdog_ms,
                min_watchdog_ms,
            )
            watchdog_ms = min_watchdog_ms

    return SupervisorConfig(
        channel_id=channel_id,
        channel_path=channel_path,
        input_type=input_type,
        device_number=device_number,
        max_devices=max_devices,
        max_channels=max_channels,
        srt_uri=srt_uri,
        srt_latency_ms=srt_latency_ms,
        deint_fields=deint_fields,
        bitrate_kbps=bitrate_kbps,
        gop_frames=gop_frames,
        enable_audio=enable_audio,
        audio_bitrate_bps=audio_bitrate_bps,
        audio_channels=audio_channels,
        audio_frame_ms=audio_frame_ms,
        audio_queue_buffers=audio_queue_buffers,
        audio_resample_quality=audio_resample_quality,
        decklink_buffer_frames=decklink_buffer_frames,
        watchdog_ms=watchdog_ms,
        output_width=output_width,
        output_height=output_height,
        rtsp_url=rtsp_url,
        video_encoder=video_encoder,
        extra_enc_args=extra_enc_args,
        lo_requested=lo_requested,
        lo_enable=lo_enable,
        max_lo_renditions=max_lo_renditions,
        lo_preset=lo_preset,
        lo_fps=lo_fps,
        lo_rtsp_url=lo_rtsp_url,
        lo_width=lo_width,
        lo_height=lo_height,
        lo_bitrate_kbps=lo_bitrate_kbps,
        lo_target_usage=lo_target_usage,
        lo_queue_buffers=lo_queue_buffers,
        lo_gop_frames=lo_gop_frames,
        captions_enable=captions_enable,
        captions_dir=captions_dir,
        captions_decode_bin=captions_decode_bin,
        channel_alias=channel_alias,
        signal_loss_debounce_s=signal_loss_debounce_s,
        signal_acquire_debounce_s=signal_acquire_debounce_s,
        live_retry_s=live_retry_s,
        output_fps=output_fps,
    )


# Keep the old name as an alias so external callers / docs that still say
# decklink_retry_s keep working when reading configs built above.
# (SupervisorConfig uses live_retry_s; tests that referenced .decklink_retry_s
# are updated to .live_retry_s.)


# ---------------------------------------------------------------------------
# State machine (pure Python — no GI). Injectable clock so tests run at
# simulated time instead of sleeping for real debounce windows.
# ---------------------------------------------------------------------------
class State(enum.Enum):
    LIVE = "LIVE"
    SLATE = "SLATE"
    RECOVERING = "RECOVERING"


class StateMachine:
    """Live-source <-> slate state machine (README "Phase 1.5 supervisor —
    specification", State machine section).

    | State       | Input           | RTSP        | Captions JSON          |
    |-------------|-----------------|-------------|-------------------------|
    | LIVE        | DeckLink / SRT  | publishing  | extract CC1 (DeckLink)  |
    | SLATE       | generated slate | publishing  | clear cue (once)        |
    | RECOVERING  | probing live    | unchanged   | unchanged until decided |

    Design notes:
    - LIVE -> SLATE needs ``loss_debounce_s`` of continuous no-signal. Brief
      unlocks already ride through as black frames; there is deliberately
      no reason to punch through to a visible slate for anything short-lived.
    - SLATE -> LIVE needs signal AND a real non-GAP buffer held for
      ``acquire_debounce_s`` — a parameter lock alone is not proof frames flow.
    - ``on_live_error`` (DeckLink/SRT branch ERROR/EOS) forces immediate SLATE
      and tears down that branch for retry; common-path errors exit the process.
    """

    def __init__(
        self,
        *,
        loss_debounce_s: float = 15.0,
        acquire_debounce_s: float = 1.0,
        clock: Optional[Callable[[], float]] = None,
        on_enter_live: Optional[Callable[[], None]] = None,
        on_enter_slate: Optional[Callable[[], None]] = None,
        on_enter_recovering: Optional[Callable[[], None]] = None,
        on_caption_clear: Optional[Callable[[], None]] = None,
    ) -> None:
        self.loss_debounce_s = loss_debounce_s
        self.acquire_debounce_s = acquire_debounce_s
        self._clock = clock or time.monotonic
        self._on_enter_live = on_enter_live
        self._on_enter_slate = on_enter_slate
        self._on_enter_recovering = on_enter_recovering
        self._on_caption_clear = on_caption_clear
        self.state = State.SLATE
        self._loss_since: Optional[float] = None
        self._acquire_since: Optional[float] = None
        self._have_valid_buffer = False
        self._signal_present = False

    def on_signal(self, present: bool) -> None:
        """Feed the live-source lock equivalent (DeckLink ``signal`` property,
        or SRT recent-decoded-buffer health). Only acts on an actual change
        so callers may poll or wire this to a GObject notify handler."""
        present = bool(present)
        if present == self._signal_present:
            return
        self._signal_present = present
        now = self._clock()
        if self.state is State.LIVE:
            # Hiccups heal on their own: a returning signal simply cancels
            # the loss timer, no transition, no black-frame visible gap.
            self._loss_since = None if present else now
            return
        if present:
            self._acquire_since = None
            self._have_valid_buffer = False
            if self.state is State.SLATE:
                self._enter(State.RECOVERING)
        else:
            self._acquire_since = None
            self._have_valid_buffer = False
            if self.state is State.RECOVERING:
                self._enter(State.SLATE)

    def on_valid_buffer(self) -> None:
        """A non-GAP video buffer arrived from the live branch. Starts the
        acquire-debounce clock the first time this fires while RECOVERING
        with signal already true."""
        self._have_valid_buffer = True
        if self.state is State.RECOVERING and self._signal_present and self._acquire_since is None:
            self._acquire_since = self._clock()

    def on_live_error(self) -> None:
        """Hard failure on the live branch — immediate slate (bypass loss debounce)."""
        self._signal_present = False
        self._have_valid_buffer = False
        self._loss_since = None
        self._acquire_since = None
        if self.state is not State.SLATE:
            self._enter(State.SLATE)

    # Back-compat name used by older call sites / docs.
    def on_decklink_error(self) -> None:
        self.on_live_error()

    def tick(self) -> None:
        now = self._clock()
        if self.state is State.LIVE:
            if self._loss_since is not None and (now - self._loss_since) >= self.loss_debounce_s:
                self._enter(State.SLATE)
        elif self.state is State.RECOVERING:
            if (
                self._signal_present
                and self._have_valid_buffer
                and self._acquire_since is not None
                and (now - self._acquire_since) >= self.acquire_debounce_s
            ):
                self._enter(State.LIVE)

    def _enter(self, new_state: State) -> None:
        if new_state is self.state:
            return
        self.state = new_state
        if new_state is State.LIVE:
            self._loss_since = None
            if self._on_enter_live:
                self._on_enter_live()
        elif new_state is State.SLATE:
            self._acquire_since = None
            self._have_valid_buffer = False
            if self._on_enter_slate:
                self._on_enter_slate()
            if self._on_caption_clear:
                self._on_caption_clear()
        elif new_state is State.RECOVERING:
            if self._on_enter_recovering:
                self._on_enter_recovering()


# ---------------------------------------------------------------------------
# Captions side channel — spawns nexvue-captions-decode.py with a data FIFO
# (fed by the GStreamer filesink, unbuffered — see nexvue-encode.sh/CLAUDE.md
# for why buffer-mode=unbuffered is mandatory) and a control FIFO used to
# push an immediate CLEAR on entering SLATE, rather than waiting out the
# decoder's own idle-erase timeout. Pure stdlib — no GI needed here.
# ---------------------------------------------------------------------------
class CaptionsSupervisor:
    def __init__(self, config: SupervisorConfig, logger: logging.Logger) -> None:
        self.enabled = False
        self.data_fifo: Optional[Path] = None
        self.control_fifo: Optional[Path] = None
        self._config = config
        self._log = logger
        self._proc: Optional[subprocess.Popen] = None
        self._control_fd: Optional[int] = None
        self._control_warned = False
        self._respawn_after_mono = 0.0
        self._respawn_failures = 0

        if not config.captions_enable:
            return
        decode_bin = Path(config.captions_decode_bin)
        if not decode_bin.is_file():
            logger.warning(
                "CAPTIONS_ENABLE=true but decode helper missing (%s) — captions off", decode_bin
            )
            return
        state_dir = Path(config.captions_dir)
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("could not create captions dir %s (%s) — captions off", state_dir, exc)
            return

        data_fifo = state_dir / f"{config.channel_path}.ccraw"
        control_fifo = state_dir / f"{config.channel_path}.ccctl"
        for fifo in (data_fifo, control_fifo):
            try:
                fifo.unlink()
            except FileNotFoundError:
                pass
            try:
                os.mkfifo(fifo)
                os.chmod(fifo, 0o644)
            except OSError as exc:
                logger.warning("could not create captions FIFO %s (%s) — captions off", fifo, exc)
                return

        self.data_fifo = data_fifo
        self.control_fifo = control_fifo
        self._spawn()
        self.enabled = self._proc is not None

    def _spawn(self) -> None:
        cfg = self._config
        try:
            self._proc = subprocess.Popen(
                [
                    sys.executable,
                    cfg.captions_decode_bin,
                    "--channel", cfg.channel_path,
                    "--fifo", str(self.data_fifo),
                    "--control-fifo", str(self.control_fifo),
                    "--state-dir", cfg.captions_dir,
                ]
            )
        except OSError as exc:
            self._log.warning("failed to start captions decoder (%s) — captions off", exc)
            self._proc = None

    def poll_respawn(self) -> None:
        """Call periodically. A dead decoder is respawned so a transient
        crash self-heals without restarting the whole encode pipeline —
        captions are a side channel, never worth an encoder restart."""
        if not self.enabled or self._proc is None:
            return
        rc = self._proc.poll()
        if rc is None:
            self._respawn_failures = 0
            return
        now = time.monotonic()
        if now < self._respawn_after_mono:
            return
        self._log.warning("captions decoder exited (rc=%s) — respawning", rc)
        self._close_control_fd()
        self._spawn()
        # Back off before the next respawn if this one dies immediately
        # (e.g. EROFS while sibling encode units restart /run/nexvue).
        self._respawn_failures += 1
        delay = min(30.0, 0.5 * (2 ** min(self._respawn_failures - 1, 5)))
        self._respawn_after_mono = time.monotonic() + delay

    def _ensure_control_fd(self) -> Optional[int]:
        if self._control_fd is not None:
            return self._control_fd
        if self.control_fifo is None:
            return None
        try:
            fd = os.open(str(self.control_fifo), os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            # No reader yet (decoder still starting, or dead) — best effort,
            # never block the caller waiting for one to show up.
            if not self._control_warned:
                self._log.debug("captions control FIFO has no reader yet")
                self._control_warned = True
            return None
        self._control_warned = False
        self._control_fd = fd
        return fd

    def _close_control_fd(self) -> None:
        if self._control_fd is not None:
            try:
                os.close(self._control_fd)
            except OSError:
                pass
            self._control_fd = None

    def write_clear(self) -> None:
        """Best-effort — a missing/dead decoder must never affect encode."""
        if not self.enabled:
            return
        fd = self._ensure_control_fd()
        if fd is None:
            return
        try:
            os.write(fd, b"CLEAR\n")
        except OSError:
            self._close_control_fd()

    def close(self) -> None:
        self._close_control_fd()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        for fifo in (self.data_fifo, self.control_fifo):
            if fifo is not None:
                try:
                    fifo.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Pure helpers shared by pipeline assembly (no GI needed — testable).
# ---------------------------------------------------------------------------
def norm_caps_string(width: int, height: int, fps: str) -> str:
    """The normalization capsfilter shared by slate and DeckLink branches —
    identical caps on both selector inputs means switching never
    renegotiates the encoder or drops the WHEP session."""
    return f"video/x-raw,format=NV12,width={width},height={height},framerate={fps},pixel-aspect-ratio=1/1"


def build_encoder_desc(
    config: SupervisorConfig,
    bitrate_kbps: int,
    *,
    for_lo: bool = False,
) -> str:
    """Same encoder property set as nexvue-encode.sh's build_enc().

    HI and LO both default to target-usage=7 / veryfast. LO may use a lower
    LO_TARGET_USAGE when deliberately trading speed for quality.
    """
    extra = f" {config.extra_enc_args}" if config.extra_enc_args else ""
    gop = config.lo_gop_frames if for_lo else config.gop_frames
    if config.video_encoder == "vah264enc":
        usage = config.lo_target_usage if for_lo else 7
        return (
            f"vah264enc rate-control=cbr bitrate={bitrate_kbps} "
            f"key-int-max={gop} b-frames=0 target-usage={usage}{extra}"
        )
    return (
        f"x264enc tune=zerolatency speed-preset=veryfast bitrate={bitrate_kbps} "
        f"key-int-max={gop} bframes=0{extra}"
    )


def _leaky_queue(max_buffers: int, *, name: str = "") -> str:
    """Leaky queue that is buffer-count limited only (disable default 1s/10MB caps)."""
    name_part = f"name={name} " if name else ""
    return (
        f"queue {name_part}max-size-buffers={max_buffers} "
        f"max-size-time=0 max-size-bytes=0 leaky=downstream"
    )


# Named elements inside the DeckLink / SRT live bins. Used to classify bus
# ERROR messages that arrive AFTER teardown (parent bin already gone) so a
# late dlq0 not-negotiated does not kill the slate/RTSP session.
LIVE_BRANCH_NAME_PREFIXES = (
    "dlvideo",
    "dlq",
    "dlaudio",
    "dlaq",
    "dlcc",
    "srtsrc",
    "srtq",
    "srtdecode",
    "srtvq",
    "srtaq",
)


def live_branch_element_name(name: str) -> bool:
    n = name or ""
    return any(n == p or n.startswith(p) for p in LIVE_BRANCH_NAME_PREFIXES)


def slate_overlay_text(config: SupervisorConfig) -> str:
    """NO SIGNAL burn-in text, optionally with the channel alias. Sanitized
    for safe embedding in a quoted gst-launch property value."""
    alias = config.channel_alias.replace('"', "'").replace("\\", "") if config.channel_alias else ""
    return f"NO SIGNAL - {alias}" if alias else "NO SIGNAL"


def _try_set(element, prop_name: str, value) -> bool:
    """Best-effort property set. input-selector's cache-buffers/drop-
    backwards/sync-mode are only present on some GStreamer versions — never
    let a missing property abort pipeline assembly on an older box."""
    if element is None:
        return False
    attr = prop_name.replace("-", "_")
    if not hasattr(element.props, attr):
        return False
    try:
        element.set_property(prop_name, value)
        return True
    except Exception as exc:  # noqa: BLE001 - defensive: never let this abort startup
        log.debug("could not set %s=%s on %s (%s)", prop_name, value, element, exc)
        return False


def _try_set_pad(pad, prop_name: str, value) -> bool:
    """Best-effort pad property (e.g. input-selector sink always-ok)."""
    if pad is None:
        return False
    try:
        pad.set_property(prop_name, value)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("could not set pad %s=%s (%s)", prop_name, value, exc)
        return False


# ---------------------------------------------------------------------------
# GStreamer pipeline / supervisor process. All Gst/GLib access lives inside
# method bodies (never at class- or module-body scope), and annotations are
# lazy strings (`from __future__ import annotations`), so this class is safe
# to define — but never to instantiate/run — without GI installed.
# ---------------------------------------------------------------------------
class Supervisor:
    TICK_MS = 200

    def __init__(self, config: SupervisorConfig) -> None:
        self._config = config
        self._log = logging.getLogger("nexvue-supervisor")
        self._captions = CaptionsSupervisor(config, self._log)
        self._state_machine = StateMachine(
            loss_debounce_s=config.signal_loss_debounce_s,
            acquire_debounce_s=config.signal_acquire_debounce_s,
            on_enter_live=self._on_enter_live,
            on_enter_slate=self._on_enter_slate,
            on_enter_recovering=self._on_enter_recovering,
            on_caption_clear=self._on_caption_clear,
        )

        self._pipeline = None
        self._video_selector = None
        self._audio_selector = None
        self._slate_video_pad = None
        self._slate_audio_pad = None
        # Live branch (DeckLink or SRT) — same selector sink_1 pads either way.
        self._live_video_bin = None
        self._live_audio_bin = None
        self._live_video_pad = None
        self._live_audio_pad = None
        self._live_bins_active = False
        self._live_error_grace_until = 0.0
        self._live_retry_failures = 0
        self._slate_pause_timeout_id = 0
        self._srt_elements: list = []
        self._srt_video_linked = False
        self._srt_audio_linked = False
        self._srt_last_buffer_mono = 0.0
        self._cc_valve = None
        self._cc_queue = None
        self._main_loop = None
        self._exit_code = 0

    # -- pipeline assembly --------------------------------------------------
    def _build_static_desc(self) -> str:
        cfg = self._config
        caps = norm_caps_string(cfg.output_width, cfg.output_height, cfg.output_fps)
        text = slate_overlay_text(cfg).replace('"', "'")
        parts = [
            # sync-streams=false: with true, the selector holds buffers to the
            # clock like a sink and the LO tee branch starves (~1–2 fps) while
            # HI still looks mostly ok. Brief glitch on LIVE↔SLATE is fine.
            "input-selector name=vsel sync-streams=false",
            (
                "videotestsrc name=slatevideo is-live=true pattern=black "
                f'! textoverlay name=slatetext text="{text}" valignment=center '
                'halignment=center font-desc="Sans Bold 48" '
                f"! videorate ! videoscale ! videoconvert ! {caps} "
                f"! {_leaky_queue(4)} ! vsel.sink_0"
            ),
        ]

        vout = f"vsel. ! {_leaky_queue(4, name='vout')}"
        if cfg.lo_enable:
            hi_enc = build_encoder_desc(cfg, cfg.bitrate_kbps)
            lo_enc = build_encoder_desc(cfg, cfg.lo_bitrate_kbps, for_lo=True)
            lo_caps = (
                f"video/x-raw,format=NV12,width={cfg.lo_width},height={cfg.lo_height},"
                f"framerate={cfg.lo_fps},pixel-aspect-ratio=1/1,interlace-mode=progressive"
            )
            lo_q = cfg.lo_queue_buffers
            parts.append(f"{vout} ! tee name=vt")
            parts.append(
                f"vt. ! {_leaky_queue(4)} ! {hi_enc} "
                "! h264parse name=hiparse config-interval=-1 ! sink."
            )
            # Rate first (cheap), then bilinear scale — nearest-neighbour made
            # 480p graphics look "combed"/jagged. Force progressive caps so any
            # leftover field flags cannot weave into the LO encode.
            parts.append(
                f"vt. ! {_leaky_queue(lo_q)} "
                f"! videorate qos=false skip-to-first=true "
                f"! videoscale qos=false method=bilinear add-borders=false "
                f"! videoconvert qos=false ! {lo_caps} "
                f"! {lo_enc} ! h264parse name=loparse config-interval=-1 ! sinklo."
            )
        else:
            hi_enc = build_encoder_desc(cfg, cfg.bitrate_kbps)
            parts.append(f"{vout} ! {hi_enc} ! h264parse name=hiparse config-interval=-1 ! sink.")

        # Note: do not set sync= on rtspclientsink — this element's GstProperty
        # set does not expose BaseSink sync on the Ubuntu/gst-rtsp-server build
        # we ship (parse_launch fails with "no property sync").
        parts.append(f"rtspclientsink name=sink location={cfg.rtsp_url} protocols=tcp")
        if cfg.lo_enable:
            parts.append(f"rtspclientsink name=sinklo location={cfg.lo_rtsp_url} protocols=tcp")

        if cfg.enable_audio:
            parts.append("input-selector name=asel sync-streams=false")
            parts.append(
                "audiotestsrc name=slateaudio is-live=true wave=silence "
                f"! audioconvert ! audioresample quality={cfg.audio_resample_quality} "
                "! audio/x-raw,format=S16LE,rate=48000,channels=2 "
                f"! {_leaky_queue(cfg.audio_queue_buffers)} ! asel.sink_0"
            )
            audio_chain = (
                f"asel. ! {_leaky_queue(cfg.audio_queue_buffers)} "
                "! audiorate ! audioconvert "
                f"! audioresample quality={cfg.audio_resample_quality} "
                "! audio/x-raw,rate=48000,channels=2 "
                f"! opusenc bitrate={cfg.audio_bitrate_bps} frame-size={cfg.audio_frame_ms}"
            )
            if cfg.lo_enable:
                parts.append(f"{audio_chain} ! tee name=at")
                parts.append(
                    f"at. ! {_leaky_queue(cfg.audio_queue_buffers)} ! sink."
                )
                parts.append(
                    f"at. ! {_leaky_queue(cfg.audio_queue_buffers)} ! sinklo."
                )
            else:
                parts.append(f"{audio_chain} ! sink.")

        if self._captions.enabled:
            parts.append(
                "queue name=ccq max-size-buffers=8 max-size-time=0 max-size-bytes=0 "
                "leaky=downstream "
                "! valve name=ccvalve drop=true "
                "! ccconverter ! closedcaption/x-cea-608,format=raw "
                f"! filesink name=ccsink location={self._captions.data_fifo} "
                "buffer-mode=unbuffered sync=false append=false"
            )

        return " ".join(parts)

    def _create_decklink_video_bin(self):
        cfg = self._config
        caps = norm_caps_string(cfg.output_width, cfg.output_height, cfg.output_fps)
        head = (
            f"decklinkvideosrc name=dlvideo device-number={cfg.device_number} mode=auto "
            f"buffer-size={cfg.decklink_buffer_frames} drop-no-signal-frames=false"
        )
        if self._captions.enabled:
            head += " output-cc=true"
        chain = [head, "queue name=dlq0 max-size-buffers=4 leaky=downstream"]
        if self._captions.enabled:
            chain.append("ccextractor name=cc")
        if cfg.watchdog_ms > 0:
            chain.append(f"watchdog timeout={cfg.watchdog_ms}")
        chain += [
            f"deinterlace fields={cfg.deint_fields} method=greedyh",
            "videorate",
            "videoscale",
            "videoconvert",
            caps,
            "queue name=dlqout max-size-buffers=4 leaky=downstream",
        ]
        desc = " ! ".join(chain)
        if self._captions.enabled:
            desc += " cc.caption ! queue name=dlccout max-size-buffers=8 leaky=downstream"

        bin_ = Gst.parse_bin_from_description(desc, False)
        out_pad = bin_.get_by_name("dlqout").get_static_pad("src")
        video_ghost = Gst.GhostPad.new("src", out_pad)
        video_ghost.set_active(True)
        bin_.add_pad(video_ghost)

        caption_ghost = None
        if self._captions.enabled:
            cc_out = bin_.get_by_name("dlccout").get_static_pad("src")
            caption_ghost = Gst.GhostPad.new("caption_src", cc_out)
            caption_ghost.set_active(True)
            bin_.add_pad(caption_ghost)

        return bin_, video_ghost, caption_ghost

    def _create_decklink_audio_bin(self):
        cfg = self._config
        desc = (
            f"decklinkaudiosrc name=dlaudio device-number={cfg.device_number} "
            f"channels={cfg.audio_channels} "
            f"! queue name=dlaq0 max-size-buffers={cfg.audio_queue_buffers} leaky=downstream "
            f"! audioconvert ! audioresample quality={cfg.audio_resample_quality} "
            "! audio/x-raw,format=S16LE,rate=48000,channels=2 "
            "! queue name=dlaqout max-size-buffers=4 leaky=downstream"
        )
        bin_ = Gst.parse_bin_from_description(desc, False)
        out_pad = bin_.get_by_name("dlaqout").get_static_pad("src")
        ghost = Gst.GhostPad.new("src", out_pad)
        ghost.set_active(True)
        bin_.add_pad(ghost)
        return bin_, ghost

    # -- lifecycle -----------------------------------------------------------
    def run(self) -> int:
        if not GST_AVAILABLE:
            raise RuntimeError("Supervisor.run() requires PyGObject/GStreamer")

        Gst.init(None)
        try:
            self._pipeline = Gst.parse_launch(self._build_static_desc())
        except GLib.Error as exc:
            self._log.error("failed to build static pipeline (%s) — check GStreamer plugin install", exc)
            return 69

        self._video_selector = self._pipeline.get_by_name("vsel")
        self._slate_video_pad = self._video_selector.get_static_pad("sink_0")
        # Live sources on an inactive selector pad must see FLOW_OK or they
        # stop with basesrc "reason error" / not-linked. Default is usually
        # true; set explicitly after we turned sync-streams off.
        _try_set_pad(self._slate_video_pad, "always-ok", True)
        self._video_selector.set_property("active-pad", self._slate_video_pad)
        # sync-streams is off in the launch string; do not re-enable cache/sync
        # knobs that only matter (and can hurt pacing) when sync-streams=true.
        for sel_name in ("vsel", "asel"):
            sel = self._pipeline.get_by_name(sel_name)
            _try_set(sel, "drop-backwards", True)

        if self._config.enable_audio:
            self._audio_selector = self._pipeline.get_by_name("asel")
            self._slate_audio_pad = self._audio_selector.get_static_pad("sink_0")
            _try_set_pad(self._slate_audio_pad, "always-ok", True)
            self._audio_selector.set_property("active-pad", self._slate_audio_pad)

        if self._captions.enabled:
            self._cc_valve = self._pipeline.get_by_name("ccvalve")
            self._cc_queue = self._pipeline.get_by_name("ccq")

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._pipeline.set_state(Gst.State.PLAYING)
        # Start on slate; keep slate running until we enter LIVE (then pause it).
        self._set_slate_running(True)
        lo_note = (
            f"true({self._config.lo_bitrate_kbps}kbps)"
            if self._config.lo_enable
            else ("requested-denied" if self._config.lo_requested else "false")
        )
        self._log.info(
            "starting: id=%d type=%s device=%s path=%s deint=%s hi=%dkbps lo=%s audio=%s captions=%s enc=%s",
            self._config.channel_id,
            self._config.input_type,
            self._config.device_number if self._config.input_type == "decklink" else "-",
            self._config.channel_path,
            self._config.deint_fields,
            self._config.bitrate_kbps,
            lo_note,
            self._config.enable_audio,
            self._captions.enabled,
            self._config.video_encoder,
        )
        self._attach_live()

        self._main_loop = GLib.MainLoop()
        GLib.timeout_add(self.TICK_MS, self._tick)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_terminate)

        try:
            self._main_loop.run()
        finally:
            self._shutdown()
        return self._exit_code

    def _shutdown(self) -> None:
        """Tear down quickly so systemctl stop/restart does not sit on the
        default 90s TimeoutStopSec. rtspclientsink TCP close is the usual hang."""
        pipe = self._pipeline
        self._pipeline = None
        if pipe is None:
            self._captions.close()
            return
        wait_ns = 2 * Gst.SECOND
        try:
            # Null publish sinks first — they block NULL the longest.
            for name in ("sink", "sinklo"):
                el = pipe.get_by_name(name)
                if el is not None:
                    try:
                        el.set_state(Gst.State.NULL)
                    except Exception:  # noqa: BLE001
                        pass
            # Release DeckLink exclusive-open promptly so a restart can reopen.
            for bin_ in (self._live_video_bin, self._live_audio_bin):
                if bin_ is not None:
                    try:
                        bin_.set_state(Gst.State.NULL)
                    except Exception:  # noqa: BLE001
                        pass
            for name in ("slatevideo", "slateaudio", "slatetext"):
                el = pipe.get_by_name(name)
                if el is not None:
                    try:
                        el.set_state(Gst.State.NULL)
                    except Exception:  # noqa: BLE001
                        pass
            pipe.set_state(Gst.State.NULL)
            # Bound wait (do not use CLOCK_TIME_NONE — that can hang for minutes).
            pipe.get_state(wait_ns)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("pipeline shutdown incomplete (%s)", exc)
        finally:
            self._captions.close()

    def _on_terminate(self) -> bool:
        self._log.info("received termination signal — shutting down")
        self._exit_code = 0
        if self._main_loop is not None:
            self._main_loop.quit()
        return False

    def _fatal(self, code: int) -> None:
        self._exit_code = code
        if self._main_loop is not None and self._main_loop.is_running():
            self._main_loop.quit()

    def _tick(self) -> bool:
        if self._config.input_type == "srt" and self._live_bins_active:
            # srtsrc has no DeckLink-style signal property — recent decoded
            # video buffers are the lock indicator.
            fresh = (time.monotonic() - self._srt_last_buffer_mono) <= SRT_SIGNAL_STALE_S
            self._state_machine.on_signal(fresh and self._srt_video_linked)
        self._state_machine.tick()
        self._captions.poll_respawn()
        return True

    def _attach_live(self) -> bool:
        if self._config.input_type == "srt":
            return self._attach_srt()
        return self._attach_decklink()

    @staticmethod
    def _request_pad(element, name: str = "sink_%u"):
        """Gst.Element.request_pad_simple when available; else get_request_pad."""
        if hasattr(element, "request_pad_simple"):
            pad = element.request_pad_simple(name)
            if pad is not None:
                return pad
        return element.get_request_pad(name)

    # -- DeckLink branch attach/detach --------------------------------------
    def _attach_decklink(self) -> bool:
        cfg = self._config
        try:
            video_bin, video_src_pad, caption_pad = self._create_decklink_video_bin()
        except GLib.Error as exc:
            self._log.error(
                "failed to build DeckLink video bin (%s) — retrying in %.1fs", exc, cfg.live_retry_s
            )
            GLib.timeout_add(int(cfg.live_retry_s * 1000), self._attach_live)
            return False

        self._pipeline.add(video_bin)
        sink_pad = self._request_pad(self._video_selector, "sink_%u")
        _try_set_pad(sink_pad, "always-ok", True)
        video_src_pad.link(sink_pad)
        self._live_video_pad = sink_pad
        if caption_pad is not None and self._cc_queue is not None:
            caption_pad.link(self._cc_queue.get_static_pad("sink"))
        video_bin.sync_state_with_parent()

        dlvideo = video_bin.get_by_name("dlvideo")
        dlvideo.connect("notify::signal", self._on_signal_notify)
        dlvideo.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_video_probe)
        self._live_video_bin = video_bin

        if cfg.enable_audio:
            audio_bin, audio_src_pad = self._create_decklink_audio_bin()
            self._pipeline.add(audio_bin)
            asink_pad = self._request_pad(self._audio_selector, "sink_%u")
            _try_set_pad(asink_pad, "always-ok", True)
            audio_src_pad.link(asink_pad)
            self._live_audio_pad = asink_pad
            audio_bin.sync_state_with_parent()
            self._live_audio_bin = audio_bin

        self._live_bins_active = True
        self._live_error_grace_until = 0.0
        self._live_retry_failures = 0
        # notify::signal only fires on CHANGE — read the current value once
        # right after linking in case the card already had lock the whole
        # time we were waiting out DECKLINK_RETRY_S.
        self._state_machine.on_signal(bool(dlvideo.get_property("signal")))
        self._log.info("DeckLink branch (device %d) attached", cfg.device_number)
        return False

    def _attach_srt(self) -> bool:
        """SRT → decode → normalize into the same selector pads as DeckLink.

        Always decode+re-encode (never remux) so slate / constant caps / LO
        stay valid when the Haivision encoder changes format.
        """
        cfg = self._config
        self._srt_elements = []
        self._srt_video_linked = False
        self._srt_audio_linked = False
        self._srt_last_buffer_mono = 0.0

        src = Gst.ElementFactory.make("srtsrc", "srtsrc")
        if src is None:
            self._log.error("srtsrc element missing — install gstreamer1.0-plugins-bad")
            GLib.timeout_add(int(cfg.live_retry_s * 1000), self._attach_live)
            return False
        src.set_property("uri", cfg.srt_uri)
        _try_set(src, "latency", cfg.srt_latency_ms)
        _try_set(src, "wait-for-connection", True)

        q = Gst.ElementFactory.make("queue", "srtq0")
        q.set_property("max-size-buffers", 0)
        q.set_property("max-size-time", 0)
        q.set_property("max-size-bytes", 0)
        _try_set(q, "leaky", 2)  # downstream

        decode = Gst.ElementFactory.make("decodebin", "srtdecode")
        if decode is None:
            self._log.error("decodebin missing — cannot decode SRT")
            GLib.timeout_add(int(cfg.live_retry_s * 1000), self._attach_live)
            return False

        for el in (src, q, decode):
            self._pipeline.add(el)
            self._srt_elements.append(el)
        src.link(q)
        q.link(decode)
        decode.connect("pad-added", self._on_srt_pad_added)

        for el in (src, q, decode):
            el.sync_state_with_parent()

        self._live_bins_active = True
        # Treat the decodebin graph as the "live video bin" for bus routing.
        self._live_video_bin = decode
        self._log.info("SRT branch attached (%s)", cfg.srt_uri)
        return False

    def _on_srt_pad_added(self, _decodebin, pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.is_empty():
            return
        structure = caps.get_structure(0)
        if structure is None:
            return
        media = structure.get_name() or ""
        cfg = self._config

        if media.startswith("video/") and not self._srt_video_linked:
            norm = norm_caps_string(cfg.output_width, cfg.output_height, cfg.output_fps)
            try:
                chain = Gst.parse_bin_from_description(
                    "queue name=srtvq0 max-size-buffers=4 leaky=downstream "
                    f"! deinterlace fields={cfg.deint_fields} method=greedyh "
                    "! videorate ! videoscale ! videoconvert "
                    f"! {norm} "
                    "! queue name=srtvqout max-size-buffers=4 leaky=downstream",
                    False,
                )
            except GLib.Error as exc:
                self._log.error("failed to build SRT video normalize chain (%s)", exc)
                return
            self._pipeline.add(chain)
            self._srt_elements.append(chain)
            sink = chain.get_static_pad("sink")
            if sink is None:
                # parse_bin without ghost pads — get first sink pad
                sink = chain.get_by_name("srtvq0").get_static_pad("sink")
            if pad.link(sink) != Gst.PadLinkReturn.OK:
                self._log.error("failed to link decodebin video pad to SRT normalize chain")
                return
            out = chain.get_by_name("srtvqout").get_static_pad("src")
            sel_sink = self._request_pad(self._video_selector, "sink_%u")
            _try_set_pad(sel_sink, "always-ok", True)
            out.link(sel_sink)
            self._live_video_pad = sel_sink
            out.add_probe(Gst.PadProbeType.BUFFER, self._on_srt_video_probe)
            chain.sync_state_with_parent()
            self._srt_video_linked = True
            self._log.info("SRT video pad linked")
            return

        if media.startswith("audio/") and cfg.enable_audio and not self._srt_audio_linked:
            try:
                chain = Gst.parse_bin_from_description(
                    f"queue name=srtaq0 max-size-buffers={cfg.audio_queue_buffers} leaky=downstream "
                    f"! audioconvert ! audioresample quality={cfg.audio_resample_quality} "
                    "! audio/x-raw,format=S16LE,rate=48000,channels=2 "
                    "! queue name=srtaqout max-size-buffers=4 leaky=downstream",
                    False,
                )
            except GLib.Error as exc:
                self._log.error("failed to build SRT audio normalize chain (%s)", exc)
                return
            self._pipeline.add(chain)
            self._srt_elements.append(chain)
            sink = chain.get_by_name("srtaq0").get_static_pad("sink")
            if pad.link(sink) != Gst.PadLinkReturn.OK:
                self._log.error("failed to link decodebin audio pad to SRT audio chain")
                return
            out = chain.get_by_name("srtaqout").get_static_pad("src")
            asink = self._request_pad(self._audio_selector, "sink_%u")
            _try_set_pad(asink, "always-ok", True)
            out.link(asink)
            self._live_audio_pad = asink
            chain.sync_state_with_parent()
            self._srt_audio_linked = True
            self._log.info("SRT audio pad linked")

    def _teardown_live(self) -> None:
        # Prefer slate pads before tearing the live branch down. Slate may have
        # been PAUSED while LIVE — wake it before selecting its pad.
        self._set_slate_running(True)
        if self._video_selector is not None and self._slate_video_pad is not None:
            self._video_selector.set_property("active-pad", self._slate_video_pad)
        if self._audio_selector is not None and self._slate_audio_pad is not None:
            self._audio_selector.set_property("active-pad", self._slate_audio_pad)

        if self._config.input_type == "srt":
            for el in reversed(self._srt_elements):
                try:
                    el.set_state(Gst.State.NULL)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._pipeline.remove(el)
                except Exception:  # noqa: BLE001
                    pass
            if self._live_video_pad is not None and self._video_selector is not None:
                try:
                    self._video_selector.release_request_pad(self._live_video_pad)
                except Exception:  # noqa: BLE001
                    pass
            if self._live_audio_pad is not None and self._audio_selector is not None:
                try:
                    self._audio_selector.release_request_pad(self._live_audio_pad)
                except Exception:  # noqa: BLE001
                    pass
            self._srt_elements = []
            self._srt_video_linked = False
            self._srt_audio_linked = False
            self._live_video_bin = None
            self._live_audio_bin = None
            self._live_video_pad = None
            self._live_audio_pad = None
            return

        for pad, bin_, selector in (
            (self._live_video_pad, self._live_video_bin, self._video_selector),
            (self._live_audio_pad, self._live_audio_bin, self._audio_selector),
        ):
            if bin_ is None:
                continue
            bin_.set_state(Gst.State.NULL)
            if pad is not None and selector is not None:
                peer = pad.get_peer()
                if peer is not None:
                    peer.unlink(pad)
                selector.release_request_pad(pad)
            self._pipeline.remove(bin_)

        self._live_video_bin = None
        self._live_audio_bin = None
        self._live_video_pad = None
        self._live_audio_pad = None

    def _handle_live_failure(self) -> None:
        if not self._live_bins_active:
            return
        self._live_bins_active = False
        # Child queues (dlq0) often post ERROR after the src; keep those from
        # being classified as fatal while teardown drains.
        self._live_error_grace_until = time.monotonic() + 5.0
        self._cancel_slate_pause()
        self._state_machine.on_live_error()
        self._teardown_live()
        self._live_retry_failures += 1
        # Back off reopen storms — hammering DeckLink every 3s yields error (-5).
        delay = min(
            30.0,
            max(self._config.live_retry_s, self._config.live_retry_s * (2 ** min(self._live_retry_failures - 1, 4))),
        )
        label = "SRT" if self._config.input_type == "srt" else "DeckLink"
        self._log.warning("%s branch down — retrying in %.1fs", label, delay)
        GLib.timeout_add(int(delay * 1000), self._attach_live)

    def _teardown_live_audio_only(self) -> None:
        """Audio not-negotiated must not bounce the video/DeckLink reopen loop."""
        if self._audio_selector is not None and self._slate_audio_pad is not None:
            self._audio_selector.set_property("active-pad", self._slate_audio_pad)
        pad = self._live_audio_pad
        bin_ = self._live_audio_bin
        self._live_audio_pad = None
        self._live_audio_bin = None
        if bin_ is None:
            return
        try:
            bin_.set_state(Gst.State.NULL)
        except Exception:  # noqa: BLE001
            pass
        if pad is not None and self._audio_selector is not None:
            try:
                peer = pad.get_peer()
                if peer is not None:
                    peer.unlink(pad)
                self._audio_selector.release_request_pad(pad)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._pipeline.remove(bin_)
        except Exception:  # noqa: BLE001
            pass
        self._log.warning("DeckLink/SRT audio detached (video continues on slate silence)")

    def _is_live_audio_element(self, element) -> bool:
        if element is None:
            return False
        try:
            name = element.get_name() or ""
        except Exception:  # noqa: BLE001
            name = ""
        if name.startswith("dlaudio") or name.startswith("dlaq") or name.startswith("srtaq"):
            return True
        node = element
        while node is not None:
            if self._live_audio_bin is not None and node is self._live_audio_bin:
                return True
            node = node.get_parent()
        return False

    # -- bus / probes ---------------------------------------------------------
    def _is_live_source(self, element) -> bool:
        if element is not None:
            try:
                name = element.get_name() or ""
            except Exception:  # noqa: BLE001
                name = ""
            if live_branch_element_name(name):
                return True
        if self._config.input_type == "srt":
            for el in self._srt_elements:
                node = element
                while node is not None:
                    if node is el:
                        return True
                    node = node.get_parent()
            return False
        for bin_ in (self._live_video_bin, self._live_audio_bin):
            if bin_ is None:
                continue
            node = element
            while node is not None:
                if node is bin_:
                    return True
                node = node.get_parent()
        return False

    @staticmethod
    def _is_caption_element(element) -> bool:
        """Caption side-channel elements live in the static pipeline (not the
        live bin). A dead FIFO reader EPIPs filesink — that must never
        take down HI/LO encode / RTSP (same rule as the old gst-launch path).
        """
        prefixes = ("ccsink", "ccvalve", "ccq", "ccconverter")
        node = element
        while node is not None:
            try:
                name = node.get_name() or ""
            except Exception:  # noqa: BLE001
                name = ""
            for p in prefixes:
                if name == p or name.startswith(p):
                    return True
            node = node.get_parent()
        return False

    def _on_bus_message(self, _bus, message) -> bool:
        mtype = message.type
        label = "SRT" if self._config.input_type == "srt" else "DeckLink"
        if mtype == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            if self._is_live_audio_element(message.src) and self._live_video_bin is not None:
                self._log.warning(
                    "%s audio error (keeping video): %s (%s)", label, err.message, debug
                )
                self._teardown_live_audio_only()
            elif self._is_live_source(message.src):
                self._log.warning("%s branch error: %s (%s)", label, err.message, debug)
                self._handle_live_failure()
            elif self._is_caption_element(message.src):
                self._log.warning(
                    "caption side-channel error (non-fatal): %s (%s) — "
                    "decoder will respawn; encode continues",
                    err.message,
                    debug,
                )
            elif time.monotonic() < self._live_error_grace_until:
                # Late ERROR from a torn-down live child (e.g. dlq0 after
                # dlvideo not-negotiated) — stay on slate; do not exit 1.
                self._log.warning(
                    "ignoring post-teardown live error: %s (%s)", err.message, debug
                )
            else:
                self._log.error("fatal pipeline error: %s (%s)", err.message, debug)
                self._fatal(1)
        elif mtype == Gst.MessageType.EOS:
            if self._is_live_audio_element(message.src) and self._live_video_bin is not None:
                self._log.warning("%s audio EOS (keeping video)", label)
                self._teardown_live_audio_only()
            elif self._is_live_source(message.src):
                self._log.warning("%s branch EOS", label)
                self._handle_live_failure()
            elif self._is_caption_element(message.src):
                self._log.warning("caption side-channel EOS (non-fatal)")
            elif time.monotonic() < self._live_error_grace_until:
                self._log.warning("ignoring post-teardown live EOS")
            else:
                self._log.error("unexpected EOS on common (non-live) path")
                self._fatal(1)
        elif mtype == Gst.MessageType.WARNING and self._is_live_source(message.src):
            # decklinkvideosrc posts a WARNING (not ERROR) on "No signal" —
            # that is routine operation already handled by the signal
            # property + state machine debounce; keep it at debug so a real
            # outage does not spam the journal once per lost frame.
            err, _debug = message.parse_warning()
            self._log.debug("%s branch warning: %s", label, err.message)
        return True

    def _on_signal_notify(self, element, _pspec) -> None:
        present = bool(element.get_property("signal"))
        self._log.debug("DeckLink signal -> %s", present)
        self._state_machine.on_signal(present)

    def _on_video_probe(self, _pad, info) -> "Gst.PadProbeReturn":
        buf = info.get_buffer()
        if buf is not None and not (buf.get_flags() & Gst.BufferFlags.GAP):
            self._state_machine.on_valid_buffer()
        return Gst.PadProbeReturn.OK

    def _on_srt_video_probe(self, _pad, info) -> "Gst.PadProbeReturn":
        buf = info.get_buffer()
        if buf is not None and not (buf.get_flags() & Gst.BufferFlags.GAP):
            self._srt_last_buffer_mono = time.monotonic()
            self._state_machine.on_valid_buffer()
        return Gst.PadProbeReturn.OK

    # -- state machine callbacks ----------------------------------------------
    def _set_slate_running(self, running: bool) -> None:
        """Pause slate sources while LIVE so they do not burn CPU/GPU at 1080p
        behind the inactive selector pad (was ~+10% iGPU with LO still starved)."""
        if self._pipeline is None:
            return
        state = Gst.State.PLAYING if running else Gst.State.PAUSED
        for name in ("slatevideo", "slateaudio", "slatetext"):
            el = self._pipeline.get_by_name(name)
            if el is None:
                continue
            try:
                el.set_state(state)
            except Exception as exc:  # noqa: BLE001
                self._log.debug("slate %s -> %s failed (%s)", name, state, exc)

    def _cancel_slate_pause(self) -> None:
        if self._slate_pause_timeout_id:
            try:
                GLib.source_remove(self._slate_pause_timeout_id)
            except Exception:  # noqa: BLE001
                pass
            self._slate_pause_timeout_id = 0

    def _deferred_pause_slate(self) -> bool:
        self._slate_pause_timeout_id = 0
        # Only pause if we are still LIVE — a flap back to slate must keep
        # videotestsrc running.
        if self._state_machine.state == State.LIVE:
            self._set_slate_running(False)
        return False

    def _on_enter_live(self) -> None:
        self._log.info("state -> LIVE")
        if self._live_video_pad is not None:
            self._video_selector.set_property("active-pad", self._live_video_pad)
        if self._config.enable_audio and self._live_audio_pad is not None:
            self._audio_selector.set_property("active-pad", self._live_audio_pad)
        # Defer pausing slate: pausing in the same turn as the pad switch can
        # flush the selector and bounce DeckLink with basesrc error (-5).
        self._cancel_slate_pause()
        self._slate_pause_timeout_id = GLib.timeout_add(2000, self._deferred_pause_slate)
        # Reopen caption valve only after the live source is the active pad.
        if self._cc_valve is not None:
            self._cc_valve.set_property("drop", False)
        self._force_keyframe()

    def _on_enter_slate(self) -> None:
        self._log.info("state -> SLATE")
        self._cancel_slate_pause()
        # Close the caption valve BEFORE flipping to slate so late 608 pairs
        # from the still-running DeckLink branch cannot land in the decoder
        # after CLEAR (plan: valve closes before entering SLATE).
        if self._cc_valve is not None:
            self._cc_valve.set_property("drop", True)
        # Resume slate before selecting its pad so the first buffers are ready.
        self._set_slate_running(True)
        if self._video_selector is not None:
            self._video_selector.set_property("active-pad", self._slate_video_pad)
        if self._config.enable_audio and self._audio_selector is not None:
            self._audio_selector.set_property("active-pad", self._slate_audio_pad)
        self._force_keyframe()

    def _on_enter_recovering(self) -> None:
        src = "SRT" if self._config.input_type == "srt" else "DeckLink"
        self._log.info("state -> RECOVERING (probing %s)", src)

    def _on_caption_clear(self) -> None:
        self._captions.write_clear()

    def _make_force_key_unit_event(self):
        if GstVideo is not None:
            return GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
        structure = Gst.Structure.new_empty("GstForceKeyUnit")
        structure.set_value("all-headers", True)
        return Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, structure)

    def _force_keyframe(self) -> None:
        if self._pipeline is None:
            return
        for name in ("hiparse", "loparse"):
            el = self._pipeline.get_by_name(name)
            if el is None:
                continue
            try:
                el.send_event(self._make_force_key_unit_event())
            except Exception as exc:  # noqa: BLE001 - a missed keyframe request is not fatal
                self._log.debug("force-keyframe request on %s failed (%s)", name, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:  # noqa: ARG001 - argv unused, kept for CLI symmetry
    if not GST_AVAILABLE:
        log.error(
            "PyGObject/GStreamer bindings not available — install python3-gi, "
            "gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0, gir1.2-gst-plugins-bad-1.0 "
            "(apt only — this project never uses pip; see setup.sh)"
        )
        return 69

    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        log.error(str(exc))
        return exc.exit_code

    supervisor = Supervisor(config)
    try:
        return supervisor.run()
    except Exception:  # noqa: BLE001 - last-resort: full traceback to journal, exit non-zero for systemd Restart=
        log.error("unhandled exception in supervisor:\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
