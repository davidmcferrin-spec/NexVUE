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
        self.assertIn("opusenc bitrate=384000", blob)

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


class TestAudioRelayCadence(unittest.TestCase):
    """Regression coverage for a real quality bug: the publish appsrc relay
    was pacing/timestamping audio pushes at AUDIO_FRAME_MS (an opusenc-only
    setting — how big Opus's own encode frames are, nothing to do with how
    capture delivers raw PCM) instead of the cadence audio actually arrives
    at (per video-frame callback, no rechunking element exists in the
    capture audio chain). That mismatch mislabeled every pushed buffer's
    PTS/duration versus its real sample count — heard as steady
    stuttering/warped audio with zero GStreamer errors logged, since
    nothing crashes, the audio clock just drifts."""

    def test_audio_relay_duration_matches_video_frame_period_not_frame_ms(self) -> None:
        cfg = mod.load_config(env(AUDIO_FRAME_MS="10"))
        rt = mod.EncodeRuntime(cfg)
        self.assertEqual(rt._a_dur, rt._v_dur)
        self.assertNotEqual(rt._a_dur, cfg.audio_frame_ms * 1_000_000)

    def test_audio_relay_duration_tracks_output_fps_not_frame_ms_either_way(self) -> None:
        # deint top halves the output rate — the relay cadence must follow
        # that, regardless of what AUDIO_FRAME_MS (opusenc-only) is set to.
        cfg = mod.load_config(env(DEINT_FIELDS="top", AUDIO_FRAME_MS="60"))
        rt = mod.EncodeRuntime(cfg)
        self.assertEqual(rt._a_dur, mod.fps_duration_ns("30000/1001"))
        self.assertNotEqual(rt._a_dur, 60 * 1_000_000)

    def test_silence_buffer_sized_for_actual_relay_duration(self) -> None:
        cfg = mod.load_config(env())
        rt = mod.EncodeRuntime(cfg)
        expected_samples = round(48000 * rt._a_dur / 1_000_000_000)
        self.assertEqual(len(rt._silence), expected_samples * 8 * 2)


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
