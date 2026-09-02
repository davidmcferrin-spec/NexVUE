#!/usr/bin/env python3
"""
Unit tests for nexvue-encode.py — config, pipeline assembly, capture retry
policy, and open-gate / hold-frame helpers. GI-free: the module must load
and these tests must pass without PyGObject.

Run: python3 test/test_nexvue_encode.py
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent.parent / "nexvue-encode.py"
spec = importlib.util.spec_from_file_location("nexvue_encode", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["nexvue_encode"] = mod
spec.loader.exec_module(mod)


def env(**extra):
    base = {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0"}
    base.update(extra)
    return base


class TestLoadConfig(unittest.TestCase):
    def test_module_imports(self) -> None:
        self.assertTrue(hasattr(mod, "load_config"))
        self.assertTrue(hasattr(mod, "decide_capture_failure"))
        self.assertTrue(hasattr(mod, "CHROME_8CH_GST_CAPS"))
        self.assertIn("coupled-count=4", mod.CHROME_8CH_GST_CAPS)

    def test_setup_installs_opus_ms_sibling(self) -> None:
        setup = (Path(__file__).resolve().parent.parent / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("nexvue_opus_ms.py", setup)
        self.assertIn("libopus0", setup)

    def test_defaults_match_production_encode(self) -> None:
        cfg = mod.load_config(env())
        self.assertEqual(cfg.device_number, 0)
        self.assertEqual(cfg.channel_path, "ch0")
        self.assertEqual(cfg.deint_fields, "all")
        self.assertEqual(cfg.deint_method, "yadif")
        self.assertEqual(cfg.output_fps, "60000/1001")
        self.assertEqual(cfg.bitrate_kbps, 5000)
        self.assertTrue(cfg.enable_audio)
        self.assertEqual(cfg.audio_bitrate_bps, 384000)
        self.assertEqual(cfg.audio_layout, "51_sap")
        self.assertEqual(cfg.audio_resample_quality, 9)
        self.assertEqual(cfg.audio_queue_buffers, 100)
        self.assertEqual(cfg.rtsp_url, "rtsp://127.0.0.1:8554/ch0")
        self.assertTrue(cfg.lo_enable)
        self.assertEqual((cfg.lo_width, cfg.lo_height, cfg.lo_bitrate_kbps), (640, 360, 500))
        self.assertTrue(cfg.captions_enable)
        self.assertEqual(cfg.watchdog_ms, 0)
        self.assertEqual(cfg.hold_last_s, 15.0)
        self.assertEqual(cfg.open_gate_s, 10)

    def test_missing_required_exit_1(self) -> None:
        with self.assertRaises(mod.ConfigError) as ctx:
            mod.load_config({"CHANNEL_PATH": "ch0"})
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_device_bounds(self) -> None:
        with self.assertRaises(mod.ConfigError):
            mod.load_config(env(DEVICE_NUMBER="4", MAX_DEVICES="4", CHANNEL_PATH="ch4"))
        cfg = mod.load_config(env(DEVICE_NUMBER="3", MAX_DEVICES="4", CHANNEL_PATH="ch3"))
        self.assertEqual(cfg.device_number, 3)
        with self.assertRaises(mod.ConfigError):
            mod.load_config(env(DEVICE_NUMBER="8"))

    def test_deint_and_method(self) -> None:
        cfg = mod.load_config(env(DEINT_FIELDS="top"))
        self.assertEqual(cfg.output_fps, "30000/1001")
        with self.assertRaises(mod.ConfigError):
            mod.load_config(env(DEINT_FIELDS="bogus"))
        with self.assertRaises(mod.ConfigError):
            mod.load_config(env(DEINT_METHOD="weave"))
        self.assertEqual(mod.load_config(env(DEINT_METHOD="greedyh")).deint_method, "greedyh")

    def test_jwt_appended(self) -> None:
        cfg = mod.load_config(env(NEXVUE_PUBLISH_JWT="tok"))
        self.assertTrue(cfg.rtsp_url.endswith("?jwt=tok"))
        self.assertTrue(cfg.lo_rtsp_url.endswith("?jwt=tok"))

    def test_lo_fps_aliases(self) -> None:
        cfg = mod.load_config(env(LO_FPS="60"))
        self.assertEqual(cfg.lo_fps, "60000/1001")
        with self.assertRaises(mod.ConfigError):
            mod.load_config(env(LO_FPS="24"))

    def test_audio_layout_validated_but_encode_stays_8ch(self) -> None:
        cfg = mod.load_config(env(AUDIO_LAYOUT="stereo"))
        self.assertEqual(cfg.audio_layout, "stereo")
        with self.assertRaises(mod.ConfigError):
            mod.load_config(env(AUDIO_LAYOUT="bogus"))

    def test_stagger_defaults_to_channel_id(self) -> None:
        cfg = mod.load_config(env(CHANNEL_ID="5", DEVICE_NUMBER="5", CHANNEL_PATH="ch5"))
        self.assertEqual(cfg.start_stagger_s, 5)
        cfg2 = mod.load_config(env(DECKLINK_START_STAGGER_S="0"))
        self.assertEqual(cfg2.start_stagger_s, 0)

    def test_inline_comment_stripped(self) -> None:
        cfg = mod.load_config(env(BITRATE_KBPS="2000  # note"))
        self.assertEqual(cfg.bitrate_kbps, 2000)


class TestPipelineAssembly(unittest.TestCase):
    def test_print_contains_publish_and_capture(self) -> None:
        blob = mod.print_pipelines(mod.load_config(env(LO_ENABLE="false")))
        self.assertIn("decklinkvideosrc device-number=0", blob)
        self.assertIn("appsink name=vasink", blob)
        self.assertIn("async=false", blob)
        self.assertIn("appsrc name=vsrc", blob)
        self.assertIn("rtsp://127.0.0.1:8554/ch0", blob)
        self.assertNotIn("input-selector", blob)
        self.assertNotIn("videotestsrc", blob)
        self.assertNotIn("watchdog", blob)

    def test_watchdog_only_when_set(self) -> None:
        blob = mod.print_pipelines(mod.load_config(env(WATCHDOG_MS="3000")))
        self.assertIn("watchdog timeout=3000", blob)

    def test_lo_tee_and_two_encoders(self) -> None:
        blob = mod.print_pipelines(mod.load_config(env(LO_ENABLE="true", LO_PRESET="720p")))
        self.assertIn("tee name=vt", blob)
        self.assertIn("name=sinklo location=rtsp://127.0.0.1:8554/ch0lo", blob)
        self.assertEqual(blob.count("vah264enc"), 2)
        self.assertIn("width=1280,height=720", blob)

    def test_no_audio_omits_decklinkaudiosrc(self) -> None:
        blob = mod.print_pipelines(mod.load_config(env(ENABLE_AUDIO="false")))
        self.assertNotIn("opusenc", blob)
        self.assertNotIn("audio/x-opus", blob)
        self.assertNotIn("decklinkaudiosrc", blob)

    def test_captions_unbuffered_filesink(self) -> None:
        blob = mod.print_pipelines(
            mod.load_config(env(CAPTIONS_ENABLE="true", CAPTIONS_PIPELINE_ONLY="true"))
        )
        self.assertIn("output-cc=true", blob)
        self.assertIn("ccextractor name=cc", blob)
        self.assertIn("cc.caption", blob)
        self.assertIn("filesink location=/dev/null buffer-mode=unbuffered", blob)
        self.assertNotIn("cc708overlay", blob)

    def test_eight_positioned_audio_branches(self) -> None:
        blob = mod.print_pipelines(mod.load_config(env()))
        self.assertEqual(blob.count("channels=1,channel-mask=(bitmask)0x"), 8)
        self.assertIn("channel-mask=(bitmask)0x400", blob)
        self.assertIn("channel-mask=(bitmask)0x800", blob)
        self.assertIn("decklinkaudiosrc device-number=0 channels=8", blob)
        self.assertIn("audio/x-opus,rate=48000,channels=8,channel-mapping-family=1", blob)
        self.assertIn("stream-count=5,coupled-count=4", blob)
        self.assertIn("channel-mapping=(int)<0, 6, 1, 4, 5, 2, 3, 7>", blob)
        self.assertIn("opusparse", blob)
        self.assertNotIn("opusenc", blob)

    def test_opusenc_fallback_pipeline(self) -> None:
        blob = mod.publish_pipeline_desc(mod.load_config(env()), chrome_opus=False)
        self.assertIn("opusenc bitrate=384000", blob)
        self.assertIn("frame-size=10", blob)
        self.assertNotIn("opusparse", blob)
        self.assertIn("channel-mask=(bitmask)0xc3f", blob)
        custom = mod.publish_pipeline_desc(
            mod.load_config(env(AUDIO_BITRATE_BPS="256000", AUDIO_FRAME_MS="20")),
            chrome_opus=False,
        )
        self.assertIn("opusenc bitrate=256000", custom)
        self.assertIn("frame-size=20", custom)

    def test_ensure_opus_falls_back_on_create_fail(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))

        def boom(*_a, **_k):
            raise OSError("opus_multistream_encoder_create failed (-1)")

        orig = mod.ChromeMsEncoder
        mod.ChromeMsEncoder = boom
        try:
            rt._ensure_opus()
        finally:
            mod.ChromeMsEncoder = orig
        self.assertTrue(rt._opus_fallback)
        self.assertIsNone(rt._opus_framer)
        blob = mod.publish_pipeline_desc(rt.cfg, chrome_opus=rt._opus_framer is not None)
        self.assertIn("opusenc", blob)

    def test_x264_fallback(self) -> None:
        blob = mod.print_pipelines(mod.load_config(env(VIDEO_ENCODER="x264enc")))
        self.assertIn("x264enc tune=zerolatency", blob)


class TestNullPollAction(unittest.TestCase):
    """Regression coverage for a real production incident: a channel wedged
    forever after a not-negotiated race because a FAILED state-change
    attempt was treated as "safely reached NULL", releasing every reference
    to a pipeline that still held the DeckLink exclusive-open handle."""

    def test_null_state_is_done_even_if_change_reported_failure(self) -> None:
        # GStreamer can report the state as NULL with a stale/failed last
        # return code — the actual state wins.
        d = mod.null_poll_action(is_null=True, change_failed=True, now=0.0, deadline=5.0)
        self.assertEqual(d, "done")

    def test_failed_change_before_null_retries_not_done(self) -> None:
        d = mod.null_poll_action(is_null=False, change_failed=True, now=0.0, deadline=5.0)
        self.assertEqual(d, "retry_null")

    def test_ordinary_async_wait_keeps_waiting(self) -> None:
        d = mod.null_poll_action(is_null=False, change_failed=False, now=0.0, deadline=5.0)
        self.assertEqual(d, "wait")

    def test_deadline_reached_exits_even_with_failure(self) -> None:
        d = mod.null_poll_action(is_null=False, change_failed=True, now=5.0, deadline=5.0)
        self.assertEqual(d, "hang_exit")

    def test_deadline_reached_without_failure_still_exits(self) -> None:
        d = mod.null_poll_action(is_null=False, change_failed=False, now=6.0, deadline=5.0)
        self.assertEqual(d, "hang_exit")

    def test_null_wins_over_deadline(self) -> None:
        # Reaching NULL right as the deadline lands must never hard-exit.
        d = mod.null_poll_action(is_null=True, change_failed=False, now=5.0, deadline=5.0)
        self.assertEqual(d, "done")


class TestCapturePolicy(unittest.TestCase):
    def test_been_live_always_retries(self) -> None:
        d = mod.decide_capture_failure(
            been_live=True, probe="unlocked", failures=3, base_backoff_s=2.0, cap_s=15.0
        )
        self.assertEqual(d.action, "retry")
        self.assertGreater(d.backoff_s, 0)

    def test_never_live_unlocked_exits_for_park(self) -> None:
        d = mod.decide_capture_failure(
            been_live=False, probe="unlocked", failures=1, base_backoff_s=2.0, cap_s=15.0
        )
        self.assertEqual(d.action, "exit_unlocked")

    def test_never_live_locked_retries_reopen_race(self) -> None:
        d = mod.decide_capture_failure(
            been_live=False, probe="locked", failures=1, base_backoff_s=2.0, cap_s=15.0
        )
        self.assertEqual(d.action, "retry")
        self.assertEqual(d.backoff_s, 2.0)

    def test_never_live_busy_retries_not_park(self) -> None:
        # Premature probe while we still hold exclusive-open reports busy
        # and must not be treated as unlocked (that skipped auto-park).
        d = mod.decide_capture_failure(
            been_live=False, probe="busy", failures=1, base_backoff_s=2.0, cap_s=15.0
        )
        self.assertEqual(d.action, "retry")

    def test_backoff_caps(self) -> None:
        d = mod.decide_capture_failure(
            been_live=True, probe="locked", failures=8, base_backoff_s=2.0, cap_s=15.0
        )
        self.assertEqual(d.backoff_s, 15.0)

    def test_open_gate_playing_ok(self) -> None:
        self.assertEqual(
            mod.capture_open_status(
                state="PLAYING", saw_error=False, got_frame=True, elapsed_s=1.0, gate_s=10.0
            ),
            "ok",
        )

    def test_open_gate_preroll_paused_is_ok(self) -> None:
        self.assertEqual(
            mod.capture_open_status(
                state="PAUSED", saw_error=False, got_frame=True, elapsed_s=12.0, gate_s=10.0
            ),
            "ok",
        )

    def test_open_gate_error_fails(self) -> None:
        self.assertEqual(
            mod.capture_open_status(
                state="PAUSED", saw_error=True, got_frame=False, elapsed_s=0.2, gate_s=10.0
            ),
            "fail",
        )

    def test_open_gate_no_frame_fails_after_gate(self) -> None:
        self.assertEqual(
            mod.capture_open_status(
                state="PAUSED", saw_error=False, got_frame=False, elapsed_s=5.0, gate_s=10.0
            ),
            "wait",
        )
        self.assertEqual(
            mod.capture_open_status(
                state="PAUSED", saw_error=False, got_frame=False, elapsed_s=10.0, gate_s=10.0
            ),
            "fail",
        )

    def test_hold_then_black(self) -> None:
        self.assertEqual(
            mod.video_hold_kind(has_frame=False, last_mono=0.0, now=10.0, hold_last_s=15.0),
            "black",
        )
        self.assertEqual(
            mod.video_hold_kind(has_frame=True, last_mono=10.0, now=10.1, hold_last_s=15.0),
            "live",
        )
        self.assertEqual(
            mod.video_hold_kind(has_frame=True, last_mono=10.0, now=12.0, hold_last_s=15.0),
            "hold",
        )
        self.assertEqual(
            mod.video_hold_kind(has_frame=True, last_mono=10.0, now=30.0, hold_last_s=15.0),
            "black",
        )


def _pcm(tag: int, samples: int = 1602) -> bytes:
    """Distinct 8ch S16LE chunk (tag in the first byte)."""
    buf = bytearray(samples * 8 * 2)
    buf[0] = tag & 0xFF
    return bytes(buf)


class TestPcmDuration(unittest.TestCase):
    def test_empty_is_zero(self) -> None:
        self.assertEqual(mod.pcm_duration_ns(0), 0)

    def test_29_97_drop_frame_pair(self) -> None:
        # 48 kHz / 29.97 is not an integer sample count — 1601 vs 1602.
        self.assertEqual(mod.pcm_duration_ns(1602 * 8 * 2), 33_375_000)
        self.assertEqual(mod.pcm_duration_ns(1601 * 8 * 2), 33_354_167)


class TestAudioRelayDecision(unittest.TestCase):
    def test_drain_wins_over_stale(self) -> None:
        self.assertEqual(
            mod.audio_relay_decision(
                queue_len=1, last_audio_mono=0.0, now=10.0, waiting_since=0.0,
                have_capture=False,
            ),
            "drain",
        )

    def test_silence_when_no_capture_or_never_live(self) -> None:
        self.assertEqual(
            mod.audio_relay_decision(
                queue_len=0, last_audio_mono=9.9, now=10.0, waiting_since=0.0,
                have_capture=False,
            ),
            "silence",
        )
        self.assertEqual(
            mod.audio_relay_decision(
                queue_len=0, last_audio_mono=0.0, now=10.0, waiting_since=0.0,
                have_capture=True,
            ),
            "silence",
        )

    def test_silence_when_stale(self) -> None:
        self.assertEqual(
            mod.audio_relay_decision(
                queue_len=0, last_audio_mono=10.0, now=10.26, waiting_since=0.0,
                have_capture=True,
            ),
            "silence",
        )

    def test_wait_when_live_but_late(self) -> None:
        self.assertEqual(
            mod.audio_relay_decision(
                queue_len=0, last_audio_mono=10.0, now=10.005, waiting_since=0.0,
                have_capture=True,
            ),
            "wait",
        )

    def test_silence_after_underrun_slack(self) -> None:
        self.assertEqual(
            mod.audio_relay_decision(
                queue_len=0, last_audio_mono=10.0, now=10.012,
                waiting_since=10.0, have_capture=True,
            ),
            "silence",
        )


class TestAudioRelayCadence(unittest.TestCase):
    """Unique-chunk drain: each captured PCM buffer is pushed once with
    duration from its sample count. Repeating `_last_audio` at a fixed
    cadence (and earlier, pacing that cadence at AUDIO_FRAME_MS) was the
    post-split stutter vs gst-launch. AUDIO_FRAME_MS sizes the Chrome-mapping
    Opus framer only — it does not pace this relay.
    """

    def test_silence_chunk_matches_video_period_not_frame_ms(self) -> None:
        cfg = mod.load_config(env(AUDIO_FRAME_MS="10"))
        rt = mod.EncodeRuntime(cfg)
        self.assertEqual(rt._a_dur, rt._v_dur)
        self.assertNotEqual(rt._a_dur, cfg.audio_frame_ms * 1_000_000)
        expected_samples = round(48000 * rt._a_dur / 1_000_000_000)
        self.assertEqual(len(rt._silence), expected_samples * 8 * 2)

    def test_silence_tracks_output_fps_not_frame_ms(self) -> None:
        cfg = mod.load_config(env(DEINT_FIELDS="top", AUDIO_FRAME_MS="60"))
        rt = mod.EncodeRuntime(cfg)
        self.assertEqual(rt._a_dur, mod.fps_duration_ns("30000/1001"))
        self.assertNotEqual(rt._a_dur, 60 * 1_000_000)

    def test_pts_is_running_sum_of_sample_durations(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))
        a, b = _pcm(1, 1602), _pcm(2, 1601)
        rt._enqueue_audio(a, now=1.0)
        rt._enqueue_audio(b, now=1.01)
        p1, d1 = rt._audio_relay_step(1.02)
        pts1 = rt._commit_audio_push(d1)
        p2, d2 = rt._audio_relay_step(1.03)
        pts2 = rt._commit_audio_push(d2)
        self.assertEqual(p1, a)
        self.assertEqual(p2, b)
        self.assertEqual(d1, mod.pcm_duration_ns(len(a)))
        self.assertEqual(d2, mod.pcm_duration_ns(len(b)))
        self.assertEqual(pts1, 0)
        self.assertEqual(pts2, d1)
        self.assertEqual(rt._a_pts, d1 + d2)
        self.assertNotEqual(rt._a_pts, 2 * rt._v_dur)

    def test_ingest_does_not_overwrite_prior_chunk(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))
        rt._enqueue_audio(_pcm(1), now=1.0)
        rt._enqueue_audio(_pcm(2), now=1.01)
        self.assertEqual(len(rt._audio_q), 2)

    def test_queue_cap_drops_oldest(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))
        for i in range(mod.AUDIO_Q_MAX + 3):
            rt._enqueue_audio(_pcm(i + 1), now=1.0 + i * 0.001)
        self.assertEqual(len(rt._audio_q), mod.AUDIO_Q_MAX)
        first, _dur = rt._audio_relay_step(2.0)
        self.assertEqual(first[0], 4)  # tags 1..3 dropped

    def test_empty_fresh_queue_does_not_replay(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))
        rt._cap = object()  # capture up — late tick must wait, not replay
        chunk = _pcm(9)
        rt._enqueue_audio(chunk, now=1.0)
        p1, _d1 = rt._audio_relay_step(1.0)
        p2, d2 = rt._audio_relay_step(1.002)
        self.assertEqual(p1, chunk)
        self.assertIsNone(p2)
        self.assertEqual(d2, 0)

    def test_underrun_slack_inserts_silence_not_replay(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))
        rt._cap = object()
        chunk = _pcm(3)
        rt._enqueue_audio(chunk, now=1.0)
        p1, _d1 = rt._audio_relay_step(1.0)
        p2, _d2 = rt._audio_relay_step(1.002)
        p3, d3 = rt._audio_relay_step(1.012)
        self.assertEqual(p1, chunk)
        self.assertIsNone(p2)
        self.assertEqual(p3, rt._silence)
        self.assertEqual(d3, mod.pcm_duration_ns(len(rt._silence)))

    def test_stale_or_no_capture_emits_silence_once_sized(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))
        payload, dur = rt._audio_relay_step(now=10.0)
        self.assertEqual(payload, rt._silence)
        self.assertEqual(dur, mod.pcm_duration_ns(len(rt._silence)))
        self.assertEqual(len(payload), len(rt._silence))

    def test_teardown_clears_queue(self) -> None:
        rt = mod.EncodeRuntime(mod.load_config(env()))
        rt._enqueue_audio(_pcm(1), now=1.0)
        rt._clear_audio_relay()
        self.assertEqual(len(rt._audio_q), 0)
        payload, _dur = rt._audio_relay_step(now=1.01)
        self.assertEqual(payload, rt._silence)

    def test_frame_ms_does_not_change_drain_math(self) -> None:
        chunk = _pcm(5, 1602)
        for frame_ms in ("10", "60"):
            rt = mod.EncodeRuntime(mod.load_config(env(AUDIO_FRAME_MS=frame_ms)))
            rt._enqueue_audio(chunk, now=1.0)
            _payload, dur = rt._audio_relay_step(1.0)
            self.assertEqual(dur, mod.pcm_duration_ns(len(chunk)))


class TestSilenceNs(unittest.TestCase):
    def test_matches_ms_helper_on_whole_milliseconds(self) -> None:
        # 10ms at 48kHz is a whole-sample case both helpers must agree on.
        self.assertEqual(
            mod.make_silence_s16_ns(8, 48000, 10_000_000),
            mod.make_silence_s16(8, 48000, 10),
        )

    def test_precise_for_non_whole_millisecond_duration(self) -> None:
        # 60000/1001 fps period (~16.683ms) truncating to whole ms would
        # under-count samples by a fraction every frame — the exact bug
        # class this helper exists to avoid.
        dur_ns = mod.fps_duration_ns("60000/1001")
        out = mod.make_silence_s16_ns(8, 48000, dur_ns)
        expected_samples = round(48000 * dur_ns / 1_000_000_000)
        self.assertEqual(len(out), expected_samples * 8 * 2)
        # A naive ms-truncating computation would give a different (wrong)
        # sample count for this same period.
        naive_samples = 48000 * (dur_ns // 1_000_000) // 1000
        self.assertNotEqual(expected_samples, naive_samples)


class TestProbeParse(unittest.TestCase):
    def test_locked_busy_unlocked(self) -> None:
        payload = '{"devices":[{"index":5,"busy":false,"input_locked":true}]}'
        self.assertEqual(mod.parse_decklink_probe(payload, 5), "locked")
        payload = '{"devices":[{"index":5,"busy":true,"input_locked":true}]}'
        self.assertEqual(mod.parse_decklink_probe(payload, 5), "busy")
        payload = '{"devices":[{"index":5,"busy":false,"input_locked":false}]}'
        self.assertEqual(mod.parse_decklink_probe(payload, 5), "unlocked")
        self.assertEqual(mod.parse_decklink_probe(payload, 0), "error")
        self.assertEqual(mod.parse_decklink_probe("not-json", 0), "error")


if __name__ == "__main__":
    unittest.main()
