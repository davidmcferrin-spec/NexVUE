#!/usr/bin/env python3
"""
nexvue_opus_ms.py — 8ch Opus with Chrome / MediaMTX multiopus mapping.

Stock GStreamer opusenc uses libopus family-1 surround (5 streams, 3
coupled, mapping 0,4,1,2,3,5,6,7). MediaMTX's WHEP SDP (and the player
offer munge) advertises Chrome's table instead:

  channel_mapping=0,6,1,4,5,2,3,7;num_streams=5;coupled_streams=4

MediaMTX remuxes packets as-is and only overwrites the SDP fmtp. Chrome
then decodes with the SDP map. Coupled streams are mid-side internally;
that mismatch showed up as L hot / R a quieter copy of the same spectrum.

This encoder calls libopus opus_multistream_encoder_create with Chrome's
table so the bitstream matches WHEP. Stdlib + libopus0 (apt); no pip.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from typing import Callable, List, Optional, Tuple

# Must stay byte-identical to nexvue-vu.js MULTICHANNEL_OPUS_FMTP[8]
# and MediaMTX internal/protocols/webrtc/from_stream.go.
CHROME_8CH_CHANNELS = 8
CHROME_8CH_STREAMS = 5
CHROME_8CH_COUPLED = 4
CHROME_8CH_MAPPING = (0, 6, 1, 4, 5, 2, 3, 7)
CHROME_8CH_FMTP = (
    "channel_mapping=0,6,1,4,5,2,3,7;num_streams=5;coupled_streams=4"
)
CHROME_8CH_GST_CAPS = (
    "audio/x-opus,rate=48000,channels=8,channel-mapping-family=1,"
    "stream-count=5,coupled-count=4,"
    "channel-mapping=(int)<0, 6, 1, 4, 5, 2, 3, 7>"
)

OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_COMPLEXITY_REQUEST = 4010
OPUS_OK = 0
_PCM_WIDTH = 2
_MAX_PACKET = 4000


def frame_bytes(rate: int, frame_ms: int, channels: int = CHROME_8CH_CHANNELS) -> int:
    return (rate * frame_ms // 1000) * channels * _PCM_WIDTH


def frame_duration_ns(frame_ms: int) -> int:
    return frame_ms * 1_000_000


class OpusChromeFramer:
    """Accumulate interleaved S16LE PCM and emit one Opus packet per frame."""

    def __init__(
        self,
        encode_fn: Callable[[bytes], bytes],
        frame_ms: int = 10,
        rate: int = 48000,
        channels: int = CHROME_8CH_CHANNELS,
    ) -> None:
        self.encode_fn = encode_fn
        self.frame_ms = frame_ms
        self.rate = rate
        self.channels = channels
        self.frame_bytes = frame_bytes(rate, frame_ms, channels)
        self.frame_dur_ns = frame_duration_ns(frame_ms)
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    @property
    def leftover(self) -> int:
        return len(self._buf)

    def feed(self, pcm: bytes) -> List[Tuple[bytes, int]]:
        if not pcm:
            return []
        self._buf.extend(pcm)
        out: List[Tuple[bytes, int]] = []
        fb = self.frame_bytes
        while len(self._buf) >= fb:
            chunk = bytes(self._buf[:fb])
            del self._buf[:fb]
            pkt = self.encode_fn(chunk)
            if pkt:
                out.append((pkt, self.frame_dur_ns))
        return out


def _load_libopus() -> ctypes.CDLL:
    candidates = []
    found = ctypes.util.find_library("opus")
    if found:
        candidates.append(found)
    candidates.extend(("libopus.so.0", "libopus.so", "opus"))
    seen = set()
    last: Optional[OSError] = None
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        try:
            return ctypes.CDLL(name)
        except OSError as exc:
            last = exc
    raise OSError(
        "libopus not found (apt: libopus0). Last error: %s" % (last,)
    )


class ChromeMsEncoder:
    """8ch WAVE PCM (FL FR FC LFE BL BR SL SR) → Chrome-mapping Opus packets."""

    def __init__(
        self,
        bitrate_bps: int = 384000,
        frame_ms: int = 10,
        rate: int = 48000,
    ) -> None:
        if frame_ms not in (2, 5, 10, 20, 40, 60):
            raise ValueError("frame_ms must be 2, 5, 10, 20, 40, or 60")
        self.frame_ms = frame_ms
        self.rate = rate
        self.frame_samples = rate * frame_ms // 1000
        self.frame_bytes = frame_bytes(rate, frame_ms)
        lib = _load_libopus()
        self._lib = lib

        lib.opus_multistream_encoder_create.restype = ctypes.c_void_p
        lib.opus_multistream_encoder_create.argtypes = [
            ctypes.c_int32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.opus_multistream_encode.restype = ctypes.c_int
        lib.opus_multistream_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
        ]
        lib.opus_multistream_encoder_ctl.restype = ctypes.c_int
        lib.opus_multistream_encoder_destroy.restype = None
        lib.opus_multistream_encoder_destroy.argtypes = [ctypes.c_void_p]

        mapping = (ctypes.c_ubyte * CHROME_8CH_CHANNELS)(*CHROME_8CH_MAPPING)
        err = ctypes.c_int(0)
        enc = lib.opus_multistream_encoder_create(
            ctypes.c_int32(rate),
            CHROME_8CH_CHANNELS,
            CHROME_8CH_STREAMS,
            CHROME_8CH_COUPLED,
            mapping,
            OPUS_APPLICATION_AUDIO,
            ctypes.byref(err),
        )
        if not enc or err.value != OPUS_OK:
            raise OSError("opus_multistream_encoder_create failed (%s)" % err.value)
        self._enc = enc
        rc = lib.opus_multistream_encoder_ctl(
            enc, OPUS_SET_BITRATE_REQUEST, ctypes.c_int(int(bitrate_bps))
        )
        if rc != OPUS_OK:
            self.close()
            raise OSError("OPUS_SET_BITRATE failed (%s)" % rc)
        lib.opus_multistream_encoder_ctl(enc, OPUS_SET_COMPLEXITY_REQUEST, ctypes.c_int(5))
        self._out = (ctypes.c_ubyte * _MAX_PACKET)()

    def encode(self, pcm: bytes) -> bytes:
        if len(pcm) != self.frame_bytes:
            raise ValueError(
                "expected %s bytes of S16LE 8ch, got %s" % (self.frame_bytes, len(pcm))
            )
        samples = (ctypes.c_int16 * (self.frame_samples * CHROME_8CH_CHANNELS)).from_buffer_copy(
            pcm
        )
        n = self._lib.opus_multistream_encode(
            self._enc,
            samples,
            self.frame_samples,
            self._out,
            ctypes.c_int32(_MAX_PACKET),
        )
        if n < 0:
            raise OSError("opus_multistream_encode failed (%s)" % n)
        return bytes(self._out[:n])

    def close(self) -> None:
        enc = getattr(self, "_enc", None)
        if enc:
            self._lib.opus_multistream_encoder_destroy(enc)
            self._enc = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
