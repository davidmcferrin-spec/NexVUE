#!/usr/bin/env python3
"""
nexvue-supervisor.py — Phase 1.5 persistent RTSP publisher with DeckLink <->
NO SIGNAL slate switching (replaces the bare gst-launch ExecStart in
nexvue-encode@.service).

Goal (README "Phase 1.5 supervisor — specification"): eliminate the
no-signal-at-boot restart loop. A channel with no DeckLink lock at start (or
that loses lock later) serves a generated slate instead of failing/restart-
looping — viewers stay connected (same RTSP/WHEP session, same output caps);
only the picture changes.

Architecture:
    nexvue-encode@N.service (ExecStart) -> this process, one per channel
        gst pipeline: persistent slate (videotestsrc+textoverlay,
        silent audiotestsrc) + input-selector, with a DYNAMIC DeckLink
        capture bin added/removed as it comes up/errors. Downstream of the
        selectors is unchanged from nexvue-encode.sh: normalize -> HI encode
        (+ optional LO tee) -> rtspclientsink(s); audio -> shared opusenc.
        MediaMTX (H.264 + Opus, no transcoding) is untouched.

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
from typing import Callable, Dict, Mapping, Optional, Tuple

LOG_PREFIX = "[nexvue-supervisor]"
LOG_LEVEL = os.environ.get("NEXVUE_SUPERVISOR_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format=f"{LOG_PREFIX} %(message)s")
log = logging.getLogger("nexvue-supervisor")

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


# LO_PRESET ladder -> (width, height, default bitrate kbps). Matches
# nexvue-encode.sh exactly — the "p" number is the HEIGHT (480p = 854x480).
LO_PRESETS: Dict[str, Tuple[int, int, int]] = {
    "720p": (1280, 720, 1200),
    "540p": (960, 540, 800),
    "480p": (854, 480, 700),
    "360p": (640, 360, 500),
    "240p": (426, 240, 300),
    "180p": (320, 180, 200),
}


@dataclass(frozen=True)
class SupervisorConfig:
    """Immutable, fully-validated view of the channel environment. Built
    once by load_config() — every field here is already sane, so pipeline
    code never re-validates."""

    device_number: int
    channel_path: str
    max_devices: int = 8
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
    lo_enable: bool = False
    lo_preset: str = "720p"
    lo_fps: str = "30000/1001"
    lo_rtsp_url: str = ""
    lo_width: int = 1280
    lo_height: int = 720
    lo_bitrate_kbps: int = 1200
    captions_enable: bool = True
    captions_dir: str = "/run/nexvue/captions"
    captions_decode_bin: str = "/usr/local/bin/nexvue-captions-decode.py"
    channel_alias: str = ""
    # Phase 1.5 knobs (README "Phase 1.5 supervisor" state machine section).
    signal_loss_debounce_s: float = 15.0
    signal_acquire_debounce_s: float = 1.0
    decklink_retry_s: float = 3.0
    # Derived, not read directly from the environment.
    output_fps: str = "60000/1001"


def load_config(env: Mapping[str, str]) -> SupervisorConfig:
    """Validate os.environ (or any string mapping, for tests) into a
    SupervisorConfig. Raises ConfigError on the first problem found —
    mirrors nexvue-encode.sh's fail-fast validation block."""

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

    device_number_raw = required("DEVICE_NUMBER")
    channel_path = required("CHANNEL_PATH")

    max_devices = opt_int("MAX_DEVICES", 8)
    if not (1 <= max_devices <= 8):
        raise ConfigError(f"MAX_DEVICES must be an integer 1-8, got {max_devices}")

    try:
        device_number = int(device_number_raw)
    except ValueError:
        raise ConfigError(f"DEVICE_NUMBER must be an integer, got {device_number_raw!r}") from None
    if not (0 <= device_number < max_devices):
        raise ConfigError(
            f"DEVICE_NUMBER must be 0-{max_devices - 1} for this card "
            f"(MAX_DEVICES={max_devices}), got {device_number}"
        )

    if not channel_path or not all(c.isalnum() or c in "-_" for c in channel_path):
        raise ConfigError(f"CHANNEL_PATH must be alphanumeric (with -/_ ), got {channel_path!r}")

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

    lo_enable = opt_bool("LO_ENABLE", False)
    lo_preset = opt("LO_PRESET", "720p")
    if lo_preset not in LO_PRESETS:
        raise ConfigError(f"LO_PRESET must be one of {','.join(LO_PRESETS)}, got {lo_preset!r}")
    lo_w_def, lo_h_def, lo_br_def = LO_PRESETS[lo_preset]
    lo_width = opt_int("LO_WIDTH", lo_w_def)
    lo_height = opt_int("LO_HEIGHT", lo_h_def)
    lo_bitrate_kbps = opt_int("LO_BITRATE_KBPS", lo_br_def)
    if lo_enable:
        if lo_width <= 0 or lo_width % 2 or lo_height <= 0 or lo_height % 2:
            raise ConfigError(f"LO_WIDTH/LO_HEIGHT must be positive even integers, got {lo_width}x{lo_height}")
        if lo_bitrate_kbps <= 0:
            raise ConfigError(f"LO_BITRATE_KBPS must be positive, got {lo_bitrate_kbps}")
    lo_fps = opt("LO_FPS", "30000/1001")
    lo_rtsp_url = opt("LO_RTSP_URL", f"rtsp://127.0.0.1:8554/{channel_path}lo")

    captions_enable = opt_bool("CAPTIONS_ENABLE", True)
    captions_dir = opt("CAPTIONS_DIR", "/run/nexvue/captions")
    captions_decode_bin = opt("CAPTIONS_DECODE_BIN", "/usr/local/bin/nexvue-captions-decode.py")

    channel_alias = opt("CHANNEL_ALIAS", "")

    signal_loss_debounce_s = opt_float("SIGNAL_LOSS_DEBOUNCE_S", 15.0)
    if signal_loss_debounce_s < 0:
        raise ConfigError(f"SIGNAL_LOSS_DEBOUNCE_S must be >= 0, got {signal_loss_debounce_s}")
    signal_acquire_debounce_s = opt_float("SIGNAL_ACQUIRE_DEBOUNCE_S", 1.0)
    if signal_acquire_debounce_s < 0:
        raise ConfigError(f"SIGNAL_ACQUIRE_DEBOUNCE_S must be >= 0, got {signal_acquire_debounce_s}")
    decklink_retry_s = opt_float("DECKLINK_RETRY_S", 3.0)
    if decklink_retry_s <= 0:
        raise ConfigError(f"DECKLINK_RETRY_S must be > 0, got {decklink_retry_s}")

    return SupervisorConfig(
        device_number=device_number,
        channel_path=channel_path,
        max_devices=max_devices,
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
        lo_enable=lo_enable,
        lo_preset=lo_preset,
        lo_fps=lo_fps,
        lo_rtsp_url=lo_rtsp_url,
        lo_width=lo_width,
        lo_height=lo_height,
        lo_bitrate_kbps=lo_bitrate_kbps,
        captions_enable=captions_enable,
        captions_dir=captions_dir,
        captions_decode_bin=captions_decode_bin,
        channel_alias=channel_alias,
        signal_loss_debounce_s=signal_loss_debounce_s,
        signal_acquire_debounce_s=signal_acquire_debounce_s,
        decklink_retry_s=decklink_retry_s,
        output_fps=output_fps,
    )


# ---------------------------------------------------------------------------
# State machine (pure Python — no GI). Injectable clock so tests run at
# simulated time instead of sleeping for real debounce windows.
# ---------------------------------------------------------------------------
class State(enum.Enum):
    LIVE = "LIVE"
    SLATE = "SLATE"
    RECOVERING = "RECOVERING"


class StateMachine:
    """DeckLink <-> slate state machine (README "Phase 1.5 supervisor —
    specification", State machine section).

    | State       | Input           | RTSP        | Captions JSON          |
    |-------------|-----------------|-------------|-------------------------|
    | LIVE        | DeckLink        | publishing  | extract CC1 as today    |
    | SLATE       | generated slate | publishing  | clear cue (once)        |
    | RECOVERING  | probing DeckLink| unchanged   | unchanged until decided |

    Boot starts SLATE (never LIVE) so a channel with no lock at process
    start serves a picture instead of the old restart loop.

    Loss debounce is deliberately generous (SIGNAL_LOSS_DEBOUNCE_S,
    default 15s): a brief real-world hiccup already rides through as black
    frames (drop-no-signal-frames=false at the capture element), so there is
    no reason to punch through to a visible slate for anything short-lived.

    Acquire requires BOTH a true signal property AND at least one real
    (non-GAP) buffer, held continuously for SIGNAL_ACQUIRE_DEBOUNCE_S — a
    parameter lock alone is not sufficient evidence that frames are actually
    flowing.
    """

    def __init__(
        self,
        loss_debounce_s: float,
        acquire_debounce_s: float,
        clock: Callable[[], float] = time.monotonic,
        on_enter_live: Optional[Callable[[], None]] = None,
        on_enter_slate: Optional[Callable[[], None]] = None,
        on_enter_recovering: Optional[Callable[[], None]] = None,
        on_caption_clear: Optional[Callable[[], None]] = None,
    ) -> None:
        self._loss_debounce_s = loss_debounce_s
        self._acquire_debounce_s = acquire_debounce_s
        self._clock = clock
        self._on_enter_live = on_enter_live
        self._on_enter_slate = on_enter_slate
        self._on_enter_recovering = on_enter_recovering
        self._on_caption_clear = on_caption_clear

        self._state = State.SLATE
        self._signal = False
        self._valid_buffer = False
        self._loss_since: Optional[float] = None
        self._acquire_since: Optional[float] = None

    @property
    def state(self) -> State:
        return self._state

    def on_signal(self, present: bool) -> None:
        """Feed the decklinkvideosrc "signal" property (or an equivalent
        lock indicator). Only acts on an actual change so callers may poll
        or wire this to a GObject notify::signal handler indifferently."""
        present = bool(present)
        if present == self._signal:
            return
        self._signal = present
        now = self._clock()
        if self._state is State.LIVE:
            # Hiccups heal on their own: a returning signal simply cancels
            # the loss timer, no transition, no black-frame visible gap.
            self._loss_since = None if present else now
            return
        if present:
            # Fresh signal window — needs a valid buffer before it counts
            # toward the acquire debounce (see on_valid_buffer).
            self._acquire_since = None
            self._valid_buffer = False
            if self._state is State.SLATE:
                self._enter(State.RECOVERING)
        else:
            self._acquire_since = None
            self._valid_buffer = False
            if self._state is State.RECOVERING:
                # Never got promoted to LIVE — demote immediately, there is
                # no "hiccup" to debounce through since we were not on air.
                self._enter(State.SLATE)

    def on_valid_buffer(self) -> None:
        """Feed a non-GAP buffer arrival from the DeckLink capture element.
        Starts the acquire-debounce clock the first time this fires while
        RECOVERING with signal already true; later calls just keep
        _valid_buffer true (tick() re-checks continuously)."""
        self._valid_buffer = True
        if self._state is State.RECOVERING and self._signal and self._acquire_since is None:
            self._acquire_since = self._clock()

    def on_decklink_error(self) -> None:
        """The DeckLink bin is being torn down (GStreamer ERROR/EOS) — there
        is no live signal source left to debounce against, so demote
        immediately regardless of the loss timer. The pipeline layer
        retries after DECKLINK_RETRY_S; this machine simply starts over
        from SLATE when that succeeds."""
        self._signal = False
        self._valid_buffer = False
        self._loss_since = None
        self._acquire_since = None
        if self._state is not State.SLATE:
            self._enter(State.SLATE)

    def tick(self) -> None:
        """Call periodically (e.g. every 100-250ms) to evaluate the two
        debounce windows using the injected clock."""
        now = self._clock()
        if self._state is State.LIVE:
            if self._loss_since is not None and (now - self._loss_since) >= self._loss_debounce_s:
                self._enter(State.SLATE)
        elif self._state is State.RECOVERING:
            if (
                self._signal
                and self._valid_buffer
                and self._acquire_since is not None
                and (now - self._acquire_since) >= self._acquire_debounce_s
            ):
                self._enter(State.LIVE)

    def _enter(self, new_state: State) -> None:
        if new_state is self._state:
            return
        self._state = new_state
        if new_state is State.LIVE:
            self._loss_since = None
            if self._on_enter_live:
                self._on_enter_live()
        elif new_state is State.SLATE:
            self._acquire_since = None
            self._valid_buffer = False
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
        if rc is not None:
            self._log.warning("captions decoder exited (rc=%s) — respawning", rc)
            self._close_control_fd()
            self._spawn()

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
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
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


def build_encoder_desc(config: SupervisorConfig, bitrate_kbps: int) -> str:
    """Same encoder property set as nexvue-encode.sh's build_enc()."""
    extra = f" {config.extra_enc_args}" if config.extra_enc_args else ""
    if config.video_encoder == "vah264enc":
        return (
            f"vah264enc rate-control=cbr bitrate={bitrate_kbps} "
            f"key-int-max={config.gop_frames} b-frames=0 target-usage=7{extra}"
        )
    return (
        f"x264enc tune=zerolatency speed-preset=veryfast bitrate={bitrate_kbps} "
        f"key-int-max={config.gop_frames} bframes=0{extra}"
    )


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
        self._decklink_video_bin = None
        self._decklink_audio_bin = None
        self._decklink_video_pad = None
        self._decklink_audio_pad = None
        self._decklink_bins_active = False
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
            "input-selector name=vsel sync-streams=true",
            (
                "videotestsrc name=slatevideo is-live=true pattern=black "
                f'! textoverlay name=slatetext text="{text}" valignment=center '
                'halignment=center font-desc="Sans Bold 48" '
                f"! videorate ! videoscale ! videoconvert ! {caps} "
                "! queue max-size-buffers=4 leaky=downstream ! vsel.sink_0"
            ),
        ]

        vout = "vsel. ! queue name=vout max-size-buffers=4 leaky=downstream"
        if cfg.lo_enable:
            hi_enc = build_encoder_desc(cfg, cfg.bitrate_kbps)
            lo_enc = build_encoder_desc(cfg, cfg.lo_bitrate_kbps)
            lo_caps = norm_caps_string(cfg.lo_width, cfg.lo_height, cfg.lo_fps)
            parts.append(f"{vout} ! tee name=vt")
            parts.append(
                f"vt. ! queue max-size-buffers=4 leaky=downstream ! {hi_enc} "
                "! h264parse name=hiparse config-interval=-1 ! sink."
            )
            parts.append(
                "vt. ! queue max-size-buffers=4 leaky=downstream ! videorate ! videoscale "
                f"! {lo_caps} ! {lo_enc} ! h264parse name=loparse config-interval=-1 ! sinklo."
            )
        else:
            hi_enc = build_encoder_desc(cfg, cfg.bitrate_kbps)
            parts.append(f"{vout} ! {hi_enc} ! h264parse name=hiparse config-interval=-1 ! sink.")

        parts.append(f"rtspclientsink name=sink location={cfg.rtsp_url} protocols=tcp")
        if cfg.lo_enable:
            parts.append(f"rtspclientsink name=sinklo location={cfg.lo_rtsp_url} protocols=tcp")

        if cfg.enable_audio:
            parts.append("input-selector name=asel sync-streams=true")
            parts.append(
                "audiotestsrc name=slateaudio is-live=true wave=silence "
                f"! audioconvert ! audioresample quality={cfg.audio_resample_quality} "
                "! audio/x-raw,format=S16LE,rate=48000,channels=2 "
                f"! queue max-size-buffers={cfg.audio_queue_buffers} leaky=downstream ! asel.sink_0"
            )
            audio_chain = (
                f"asel. ! queue max-size-buffers={cfg.audio_queue_buffers} leaky=downstream "
                "! audiorate ! audioconvert "
                f"! audioresample quality={cfg.audio_resample_quality} "
                "! audio/x-raw,rate=48000,channels=2 "
                f"! opusenc bitrate={cfg.audio_bitrate_bps} frame-size={cfg.audio_frame_ms}"
            )
            if cfg.lo_enable:
                parts.append(f"{audio_chain} ! tee name=at")
                parts.append(
                    f"at. ! queue max-size-buffers={cfg.audio_queue_buffers} leaky=downstream ! sink."
                )
                parts.append(
                    f"at. ! queue max-size-buffers={cfg.audio_queue_buffers} leaky=downstream ! sinklo."
                )
            else:
                parts.append(f"{audio_chain} ! sink.")

        if self._captions.enabled:
            parts.append(
                "queue name=ccq max-size-buffers=8 leaky=downstream "
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
        self._video_selector.set_property("active-pad", self._slate_video_pad)
        for sel_name in ("vsel", "asel"):
            sel = self._pipeline.get_by_name(sel_name)
            _try_set(sel, "cache-buffers", True)
            _try_set(sel, "drop-backwards", True)
            _try_set(sel, "sync-mode", 1)  # 1 == "clock", if the enum exists

        if self._config.enable_audio:
            self._audio_selector = self._pipeline.get_by_name("asel")
            self._slate_audio_pad = self._audio_selector.get_static_pad("sink_0")
            self._audio_selector.set_property("active-pad", self._slate_audio_pad)

        if self._captions.enabled:
            self._cc_valve = self._pipeline.get_by_name("ccvalve")
            self._cc_queue = self._pipeline.get_by_name("ccq")

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._pipeline.set_state(Gst.State.PLAYING)
        self._log.info(
            "starting: device=%d path=%s deint=%s hi=%dkbps lo=%s audio=%s captions=%s enc=%s",
            self._config.device_number,
            self._config.channel_path,
            self._config.deint_fields,
            self._config.bitrate_kbps,
            f"true({self._config.lo_bitrate_kbps}kbps)" if self._config.lo_enable else "false",
            self._config.enable_audio,
            self._captions.enabled,
            self._config.video_encoder,
        )
        self._attach_decklink()

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
        try:
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
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
        self._state_machine.tick()
        self._captions.poll_respawn()
        return True

    # -- DeckLink branch attach/detach --------------------------------------
    def _attach_decklink(self) -> bool:
        cfg = self._config
        try:
            video_bin, video_src_pad, caption_pad = self._create_decklink_video_bin()
        except GLib.Error as exc:
            self._log.error(
                "failed to build DeckLink video bin (%s) — retrying in %.1fs", exc, cfg.decklink_retry_s
            )
            GLib.timeout_add(int(cfg.decklink_retry_s * 1000), self._attach_decklink)
            return False

        self._pipeline.add(video_bin)
        sink_pad = self._video_selector.get_request_pad("sink_1")
        video_src_pad.link(sink_pad)
        self._decklink_video_pad = sink_pad
        if caption_pad is not None and self._cc_queue is not None:
            caption_pad.link(self._cc_queue.get_static_pad("sink"))
        video_bin.sync_state_with_parent()

        dlvideo = video_bin.get_by_name("dlvideo")
        dlvideo.connect("notify::signal", self._on_signal_notify)
        dlvideo.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_video_probe)
        self._decklink_video_bin = video_bin

        if cfg.enable_audio:
            audio_bin, audio_src_pad = self._create_decklink_audio_bin()
            self._pipeline.add(audio_bin)
            asink_pad = self._audio_selector.get_request_pad("sink_1")
            audio_src_pad.link(asink_pad)
            self._decklink_audio_pad = asink_pad
            audio_bin.sync_state_with_parent()
            self._decklink_audio_bin = audio_bin

        self._decklink_bins_active = True
        # notify::signal only fires on CHANGE — read the current value once
        # right after linking in case the card already had lock the whole
        # time we were waiting out DECKLINK_RETRY_S.
        self._state_machine.on_signal(bool(dlvideo.get_property("signal")))
        self._log.info("DeckLink branch (device %d) attached", cfg.device_number)
        return False

    def _teardown_decklink(self) -> None:
        for pad, bin_, selector in (
            (self._decklink_video_pad, self._decklink_video_bin, self._video_selector),
            (self._decklink_audio_pad, self._decklink_audio_bin, self._audio_selector),
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

        self._decklink_video_bin = None
        self._decklink_audio_bin = None
        self._decklink_video_pad = None
        self._decklink_audio_pad = None

    def _handle_decklink_failure(self) -> None:
        if not self._decklink_bins_active:
            return
        self._decklink_bins_active = False
        self._state_machine.on_decklink_error()
        self._teardown_decklink()
        self._log.warning("DeckLink branch down — retrying in %.1fs", self._config.decklink_retry_s)
        GLib.timeout_add(int(self._config.decklink_retry_s * 1000), self._attach_decklink)

    # -- bus / probes ---------------------------------------------------------
    def _is_decklink_source(self, element) -> bool:
        for bin_ in (self._decklink_video_bin, self._decklink_audio_bin):
            if bin_ is None:
                continue
            node = element
            while node is not None:
                if node is bin_:
                    return True
                node = node.get_parent()
        return False

    def _on_bus_message(self, _bus, message) -> bool:
        mtype = message.type
        if mtype == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            if self._is_decklink_source(message.src):
                self._log.warning("DeckLink branch error: %s (%s)", err.message, debug)
                self._handle_decklink_failure()
            else:
                self._log.error("fatal pipeline error: %s (%s)", err.message, debug)
                self._fatal(1)
        elif mtype == Gst.MessageType.EOS:
            if self._is_decklink_source(message.src):
                self._log.warning("DeckLink branch EOS")
                self._handle_decklink_failure()
            else:
                self._log.error("unexpected EOS on common (non-DeckLink) path")
                self._fatal(1)
        elif mtype == Gst.MessageType.WARNING and self._is_decklink_source(message.src):
            # decklinkvideosrc posts a WARNING (not ERROR) on "No signal" —
            # that is routine operation already handled by the signal
            # property + state machine debounce; keep it at debug so a real
            # outage does not spam the journal once per lost frame.
            err, _debug = message.parse_warning()
            self._log.debug("DeckLink branch warning: %s", err.message)
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

    # -- state machine callbacks ----------------------------------------------
    def _on_enter_live(self) -> None:
        self._log.info("state -> LIVE")
        if self._decklink_video_pad is not None:
            self._video_selector.set_property("active-pad", self._decklink_video_pad)
        if self._config.enable_audio and self._decklink_audio_pad is not None:
            self._audio_selector.set_property("active-pad", self._decklink_audio_pad)
        if self._cc_valve is not None:
            self._cc_valve.set_property("drop", False)
        self._force_keyframe()

    def _on_enter_slate(self) -> None:
        self._log.info("state -> SLATE")
        if self._video_selector is not None:
            self._video_selector.set_property("active-pad", self._slate_video_pad)
        if self._config.enable_audio and self._audio_selector is not None:
            self._audio_selector.set_property("active-pad", self._slate_audio_pad)
        if self._cc_valve is not None:
            self._cc_valve.set_property("drop", True)
        self._force_keyframe()

    def _on_enter_recovering(self) -> None:
        self._log.info("state -> RECOVERING (probing DeckLink)")

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
