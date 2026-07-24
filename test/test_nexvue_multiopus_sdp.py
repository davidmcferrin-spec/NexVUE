#!/usr/bin/env python3
"""
Unit tests for WHEP multiopus SDP offer munge (nexvue-vu.js).

Must stay aligned with:
  - nexvue-vu.js MULTICHANNEL_OPUS_FMTP / mungeWhepOfferSdp
  - MediaMTX internal/protocols/webrtc/from_stream.go multichannelOpusSDP
  - MediaMTX internal/servers/webrtc/reader.js #enableMultichannelOpus

Run: python3 test/test_nexvue_multiopus_sdp.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

# Exact MediaMTX from_stream.go table — pion matches these fmtp lines.
MULTICHANNEL_OPUS_FMTP = {
    3: "channel_mapping=0,2,1;num_streams=2;coupled_streams=1",
    4: "channel_mapping=0,1,2,3;num_streams=2;coupled_streams=2",
    5: "channel_mapping=0,4,1,2,3;num_streams=3;coupled_streams=2",
    6: "channel_mapping=0,4,1,2,3,5;num_streams=4;coupled_streams=2",
    7: "channel_mapping=0,4,1,2,3,5,6;num_streams=4;coupled_streams=4",
    8: "channel_mapping=0,6,1,4,5,2,3,7;num_streams=5;coupled_streams=4",
}

SAMPLE_OFFER = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=mid:0\r\n"
    "a=recvonly\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=mid:1\r\n"
    "a=recvonly\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
)


def reserve_payload_type(used: set[str]) -> str:
    for i in range(30, 128):
        if (i <= 63 or i >= 96) and str(i) not in used:
            used.add(str(i))
            return str(i)
    raise RuntimeError("unable to find a free RTP payload type")


def collect_payload_types(sdp: str) -> set[str]:
    used: set[str] = set()
    for section in sdp.split("m=")[1:]:
        header = section.split("\r\n")[0]
        for tok in header.split(" ")[3:]:
            if tok:
                used.add(tok)
    return used


def munge_whep_offer_sdp(sdp: str) -> str:
    """Python mirror of nexvue-vu.js mungeWhepOfferSdp."""
    if not sdp:
        return sdp
    if re.search(r"multiopus/48000/", sdp, re.I):
        return sdp
    sections = sdp.split("m=")
    if len(sections) < 2:
        return sdp
    used = collect_payload_types(sdp)
    edited = False
    for i in range(1, len(sections)):
        if not sections[i].startswith("audio"):
            continue
        lines = sections[i].split("\r\n")
        insert_at = len(lines)
        while insert_at > 0 and lines[insert_at - 1] == "":
            insert_at -= 1
        for ch in range(3, 9):
            pt = reserve_payload_type(used)
            lines[0] += f" {pt}"
            lines.insert(insert_at, f"a=rtpmap:{pt} multiopus/48000/{ch}")
            insert_at += 1
            lines.insert(insert_at, f"a=fmtp:{pt} {MULTICHANNEL_OPUS_FMTP[ch]}")
            insert_at += 1
            lines.insert(insert_at, f"a=rtcp-fb:{pt} transport-cc")
            insert_at += 1
        sections[i] = "\r\n".join(lines)
        edited = True
        break
    return "m=".join(sections) if edited else sdp


class TestMultiopusFmtp(unittest.TestCase):
    def test_fmtp_matches_js_source(self):
        js = (Path(__file__).resolve().parent.parent / "nexvue-vu.js").read_text(
            encoding="utf-8"
        )
        for ch, fmtp in MULTICHANNEL_OPUS_FMTP.items():
            self.assertIn(f"{ch}: \"{fmtp}\"", js, f"JS missing MediaMTX fmtp for {ch}ch")

    def test_munge_adds_all_channel_counts(self):
        out = munge_whep_offer_sdp(SAMPLE_OFFER)
        self.assertIn("opus/48000/2", out)  # stereo kept
        for ch in range(3, 9):
            self.assertIn(f"multiopus/48000/{ch}", out)
            self.assertIn(MULTICHANNEL_OPUS_FMTP[ch], out)
        # Audio m-line must list the new payload types.
        audio_m = [ln for ln in out.split("\r\n") if ln.startswith("m=audio")][0]
        self.assertGreaterEqual(len(audio_m.split()), 9)  # SAVPF + opus + 6 multiopus

    def test_munge_idempotent(self):
        once = munge_whep_offer_sdp(SAMPLE_OFFER)
        twice = munge_whep_offer_sdp(once)
        self.assertEqual(once, twice)

    def test_empty_passthrough(self):
        self.assertEqual(munge_whep_offer_sdp(""), "")
        self.assertEqual(munge_whep_offer_sdp("v=0\r\n"), "v=0\r\n")


if __name__ == "__main__":
    unittest.main()
