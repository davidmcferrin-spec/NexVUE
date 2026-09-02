#!/usr/bin/env python3
"""
Unit tests for nexvue_opus_ms.py — Chrome/MediaMTX 8ch Opus mapping + framer.

GI-free. Encode/decode round-trip is skipped when libopus is not installed
(Windows desktop). Run: python3 test/test_nexvue_opus_ms.py
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "nexvue_opus_ms.py"
spec = importlib.util.spec_from_file_location("nexvue_opus_ms", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["nexvue_opus_ms"] = mod
spec.loader.exec_module(mod)


def _pcm_dual_mono(frame_ms: int = 10, amp: int = 8000) -> bytes:
    samples = 48000 * frame_ms // 1000
    out = bytearray()
    for i in range(samples):
        s = amp if (i % 8) < 4 else -amp
        le = s.to_bytes(2, "little", signed=True)
        # WAVE 8ch: L=R=tone, C…SR silence
        out += le + le + (b"\x00\x00" * 6)
    return bytes(out)


class TestChromeMappingConstants(unittest.TestCase):
    def test_fmtp_matches_mediamtx_and_player(self) -> None:
        self.assertEqual(
            mod.CHROME_8CH_FMTP,
            "channel_mapping=0,6,1,4,5,2,3,7;num_streams=5;coupled_streams=4",
        )
        js = (ROOT / "web-node" / "nexvue-vu.js").read_text(encoding="utf-8")
        self.assertIn(f'8: "{mod.CHROME_8CH_FMTP}"', js)
        portal = (ROOT / "web-portal" / "nexvue-portal-whep.js").read_text(encoding="utf-8")
        self.assertIn(f'8: "{mod.CHROME_8CH_FMTP}"', portal)

    def test_gst_caps_carry_chrome_table(self) -> None:
        caps = mod.CHROME_8CH_GST_CAPS
        self.assertIn("channels=8", caps)
        self.assertIn("stream-count=5", caps)
        self.assertIn("coupled-count=4", caps)
        self.assertIn("0, 6, 1, 4, 5, 2, 3, 7", caps)

    def test_mapping_tuple(self) -> None:
        self.assertEqual(mod.CHROME_8CH_MAPPING, (0, 6, 1, 4, 5, 2, 3, 7))
        self.assertEqual(mod.CHROME_8CH_STREAMS, 5)
        self.assertEqual(mod.CHROME_8CH_COUPLED, 4)


class TestOpusChromeFramer(unittest.TestCase):
    def test_emits_one_packet_per_frame(self) -> None:
        seen = []

        def enc(pcm: bytes) -> bytes:
            seen.append(len(pcm))
            return b"OPUS" + bytes([len(seen)])

        fr = mod.OpusChromeFramer(enc, frame_ms=10)
        self.assertEqual(fr.frame_bytes, 48000 * 10 // 1000 * 8 * 2)
        half = bytes(fr.frame_bytes // 2)
        self.assertEqual(fr.feed(half), [])
        self.assertEqual(fr.leftover, fr.frame_bytes // 2)
        out = fr.feed(half)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], b"OPUS\x01")
        self.assertEqual(out[0][1], 10_000_000)
        self.assertEqual(seen, [fr.frame_bytes])
        self.assertEqual(fr.leftover, 0)

    def test_two_frames_in_one_chunk(self) -> None:
        fr = mod.OpusChromeFramer(lambda pcm: b"x", frame_ms=10)
        out = fr.feed(bytes(fr.frame_bytes * 2))
        self.assertEqual(len(out), 2)
        self.assertEqual(fr.leftover, 0)

    def test_reset_drops_leftover(self) -> None:
        fr = mod.OpusChromeFramer(lambda pcm: b"x", frame_ms=10)
        fr.feed(bytes(100))
        fr.reset()
        self.assertEqual(fr.leftover, 0)

    def test_empty_feed(self) -> None:
        fr = mod.OpusChromeFramer(lambda pcm: b"x", frame_ms=10)
        self.assertEqual(fr.feed(b""), [])


class TestChromeMsEncoder(unittest.TestCase):
    def test_roundtrip_l_equals_r_when_libopus_present(self) -> None:
        try:
            enc = mod.ChromeMsEncoder(bitrate_bps=64000, frame_ms=10)
        except OSError:
            self.skipTest("libopus not installed")
        pcm = _pcm_dual_mono()
        pkt = enc.encode(pcm)
        enc.close()
        self.assertGreater(len(pkt), 8)
        # Same mapping on decode — L and R must stay equal (the WHEP bug
        # was encode family-1 surround / decode Chrome table).
        try:
            dec = _decode_chrome(pkt, frame_samples=480)
        except OSError:
            self.skipTest("libopus decoder unavailable")
        left = dec[0::8]
        right = dec[1::8]
        self.assertEqual(len(left), len(right))
        # Allow a few LSBs of Opus error; not a 6–18 dB hole.
        max_err = max(abs(a - b) for a, b in zip(left, right))
        self.assertLess(max_err, 400, f"L/R decoded peak delta {max_err}")


def _decode_chrome(pkt: bytes, frame_samples: int) -> list[int]:
    import ctypes

    lib = mod._load_libopus()
    lib.opus_multistream_decoder_create.restype = ctypes.c_void_p
    lib.opus_multistream_decoder_create.argtypes = [
        ctypes.c_int32,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.opus_multistream_decode.restype = ctypes.c_int
    lib.opus_multistream_decode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.opus_multistream_decoder_destroy.restype = None
    lib.opus_multistream_decoder_destroy.argtypes = [ctypes.c_void_p]
    mapping = (ctypes.c_ubyte * 8)(*mod.CHROME_8CH_MAPPING)
    err = ctypes.c_int(0)
    dec = lib.opus_multistream_decoder_create(
        ctypes.c_int32(48000),
        8,
        mod.CHROME_8CH_STREAMS,
        mod.CHROME_8CH_COUPLED,
        mapping,
        ctypes.byref(err),
    )
    if not dec or err.value != 0:
        raise OSError("decoder create failed")
    out = (ctypes.c_int16 * (frame_samples * 8))()
    pkt_buf = (ctypes.c_ubyte * len(pkt)).from_buffer_copy(pkt)
    n = lib.opus_multistream_decode(
        dec, pkt_buf, ctypes.c_int32(len(pkt)), out, frame_samples, 0
    )
    lib.opus_multistream_decoder_destroy(dec)
    if n < 0:
        raise OSError("decode failed")
    return list(out[: n * 8])


if __name__ == "__main__":
    unittest.main()
