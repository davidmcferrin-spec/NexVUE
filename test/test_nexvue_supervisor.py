#!/usr/bin/env python3
"""
Unit tests for nexvue-supervisor.py — config validation and the pure-Python
DeckLink <-> slate state machine. Deliberately GI-free: these exercise
load_config()/StateMachine() exactly the way the module is designed to be
testable without PyGObject/GStreamer installed (see module docstring).

Run: python3 test/test_nexvue_supervisor.py
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent.parent / "nexvue-supervisor.py"
spec = importlib.util.spec_from_file_location("nexvue_supervisor", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["nexvue_supervisor"] = mod
spec.loader.exec_module(mod)


class FakeClock:
    """Injectable monotonic clock for deterministic debounce testing —
    StateMachine never has to actually sleep."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestLoadConfig(unittest.TestCase):
    def test_module_loads_without_gi(self) -> None:
        # The module must be importable on a box with no PyGObject at all
        # (unit test / CI machines, or a bare `python3 -c "import ..."`).
        self.assertFalse(mod.GST_AVAILABLE)

    def test_minimal_env_applies_encode_sh_defaults(self) -> None:
        cfg = mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0"})
        self.assertEqual(cfg.device_number, 0)
        self.assertEqual(cfg.channel_path, "ch0")
        self.assertEqual(cfg.max_devices, 8)
        self.assertEqual(cfg.deint_fields, "all")
        self.assertEqual(cfg.output_fps, "60000/1001")
        self.assertEqual(cfg.bitrate_kbps, 5000)
        self.assertEqual(cfg.gop_frames, 60)
        self.assertTrue(cfg.enable_audio)
        self.assertEqual(cfg.audio_bitrate_bps, 128000)
        self.assertEqual(cfg.audio_channels, 2)
        self.assertEqual(cfg.audio_frame_ms, 10)
        self.assertEqual(cfg.audio_queue_buffers, 100)
        self.assertEqual(cfg.audio_resample_quality, 9)
        self.assertEqual(cfg.rtsp_url, "rtsp://127.0.0.1:8554/ch0")
        self.assertEqual(cfg.video_encoder, "vah264enc")
        self.assertFalse(cfg.lo_enable)
        self.assertTrue(cfg.captions_enable)
        # Phase 1.5 knobs — generous loss debounce, quick acquire, 3s retry.
        self.assertEqual(cfg.signal_loss_debounce_s, 15.0)
        self.assertEqual(cfg.signal_acquire_debounce_s, 1.0)
        self.assertEqual(cfg.live_retry_s, 3.0)
        # Watchdog off by default so a brief unlock cannot ERROR the DeckLink
        # bin before SIGNAL_LOSS_DEBOUNCE_S can ride it out as black frames.
        self.assertEqual(cfg.watchdog_ms, 0)

    def test_missing_required_raises_exit_1(self) -> None:
        with self.assertRaises(mod.ConfigError) as ctx:
            mod.load_config({"CHANNEL_PATH": "ch0"})
        self.assertEqual(ctx.exception.exit_code, 1)

        with self.assertRaises(mod.ConfigError) as ctx:
            mod.load_config({"DEVICE_NUMBER": "0"})
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_device_number_bounds_use_max_devices(self) -> None:
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "4", "CHANNEL_PATH": "ch4", "MAX_DEVICES": "4"})
        cfg = mod.load_config({"DEVICE_NUMBER": "3", "CHANNEL_PATH": "ch3", "MAX_DEVICES": "4"})
        self.assertEqual(cfg.device_number, 3)
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "8", "CHANNEL_PATH": "ch8"})  # default MAX_DEVICES=8

    def test_max_devices_out_of_range_rejected(self) -> None:
        for bad in ("0", "9", "-1", "bogus"):
            with self.assertRaises(mod.ConfigError):
                mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "MAX_DEVICES": bad})

    def test_channel_path_must_be_safe(self) -> None:
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "../etc"})
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": ""})

    def test_deint_fields_top_halves_output_fps(self) -> None:
        cfg = mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "DEINT_FIELDS": "top"})
        self.assertEqual(cfg.output_fps, "30000/1001")
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "DEINT_FIELDS": "bogus"})

    def test_lo_preset_ladder_resolves_raster_and_bitrate(self) -> None:
        cfg = mod.load_config(
            {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "LO_ENABLE": "true", "LO_PRESET": "360p"}
        )
        self.assertEqual((cfg.lo_width, cfg.lo_height, cfg.lo_bitrate_kbps), (640, 360, 800))
        self.assertEqual(cfg.lo_target_usage, 7)
        self.assertEqual(cfg.lo_queue_buffers, 16)
        with self.assertRaises(mod.ConfigError):
            mod.load_config(
                {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "LO_ENABLE": "true", "LO_PRESET": "1080p"}
            )

    def test_lo_smooth_knobs_validated(self) -> None:
        cfg = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "LO_ENABLE": "true",
                "LO_TARGET_USAGE": "3",
                "LO_QUEUE_BUFFERS": "24",
                "LO_GOP_FRAMES": "30",
            }
        )
        self.assertEqual(cfg.lo_target_usage, 3)
        self.assertEqual(cfg.lo_queue_buffers, 24)
        self.assertEqual(cfg.lo_gop_frames, 30)
        self.assertEqual(cfg.lo_bitrate_kbps, 2500)  # 720p default
        with self.assertRaises(mod.ConfigError):
            mod.load_config(
                {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "LO_TARGET_USAGE": "8"}
            )
        with self.assertRaises(mod.ConfigError):
            mod.load_config(
                {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "LO_QUEUE_BUFFERS": "0"}
            )
        with self.assertRaises(mod.ConfigError):
            mod.load_config(
                {
                    "DEVICE_NUMBER": "0",
                    "CHANNEL_PATH": "ch0",
                    "LO_ENABLE": "true",
                    "LO_FPS": "24",
                }
            )
        # Legacy bare rates normalize instead of restart-storming.
        cfg_alias = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "LO_ENABLE": "true",
                "LO_FPS": "60",
            }
        )
        self.assertEqual(cfg_alias.lo_fps, "60000/1001")
        cfg_fps = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "LO_ENABLE": "true",
                "LO_FPS": "15000/1001",
            }
        )
        self.assertEqual(cfg_fps.lo_fps, "15000/1001")

    def test_lo_explicit_overrides_beat_preset(self) -> None:
        cfg = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "LO_ENABLE": "true",
                "LO_PRESET": "480p",
                "LO_WIDTH": "512",
                "LO_HEIGHT": "288",
                "LO_BITRATE_KBPS": "400",
            }
        )
        self.assertEqual((cfg.lo_width, cfg.lo_height, cfg.lo_bitrate_kbps), (512, 288, 400))

    def test_bool_fields_reject_bogus_values(self) -> None:
        for key in ("ENABLE_AUDIO", "LO_ENABLE", "CAPTIONS_ENABLE"):
            with self.assertRaises(mod.ConfigError):
                mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", key: "bogus"})

    def test_audio_frame_ms_and_resample_quality_validated(self) -> None:
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "AUDIO_FRAME_MS": "7"})
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "AUDIO_RESAMPLE_QUALITY": "11"})
        cfg = mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "AUDIO_RESAMPLE_QUALITY": "3"})
        self.assertEqual(cfg.audio_resample_quality, 3)

    def test_video_encoder_must_be_known(self) -> None:
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "VIDEO_ENCODER": "nvenc"})
        cfg = mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "VIDEO_ENCODER": "x264enc"})
        self.assertEqual(cfg.video_encoder, "x264enc")

    def test_new_phase15_knobs_validated(self) -> None:
        cfg = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "SIGNAL_LOSS_DEBOUNCE_S": "20",
                "SIGNAL_ACQUIRE_DEBOUNCE_S": "2.5",
                "DECKLINK_RETRY_S": "5",
            }
        )
        self.assertEqual(cfg.signal_loss_debounce_s, 20.0)
        self.assertEqual(cfg.signal_acquire_debounce_s, 2.5)
        self.assertEqual(cfg.live_retry_s, 5.0)
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "SIGNAL_LOSS_DEBOUNCE_S": "-1"})
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "DECKLINK_RETRY_S": "0"})

    def test_watchdog_ms_clamped_above_loss_debounce(self) -> None:
        # Short WATCHDOG_MS undercuts 15s loss debounce → raise to debounce+5s.
        cfg = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "WATCHDOG_MS": "3000",
                "SIGNAL_LOSS_DEBOUNCE_S": "15",
            }
        )
        self.assertEqual(cfg.watchdog_ms, 20000)
        cfg2 = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "WATCHDOG_MS": "25000",
                "SIGNAL_LOSS_DEBOUNCE_S": "15",
            }
        )
        self.assertEqual(cfg2.watchdog_ms, 25000)
        with self.assertRaises(mod.ConfigError):
            mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "WATCHDOG_MS": "-1"})

    def test_is_caption_element_matches_side_channel_names(self) -> None:
        class FakeEl:
            def __init__(self, name, parent=None):
                self._name = name
                self._parent = parent

            def get_name(self):
                return self._name

            def get_parent(self):
                return self._parent

        root = FakeEl("pipeline0")
        self.assertTrue(mod.Supervisor._is_caption_element(FakeEl("ccsink", root)))
        self.assertTrue(mod.Supervisor._is_caption_element(FakeEl("ccvalve0", root)))
        self.assertTrue(mod.Supervisor._is_caption_element(FakeEl("ccconverter", root)))
        self.assertFalse(mod.Supervisor._is_caption_element(FakeEl("vah264enc0", root)))
        self.assertFalse(mod.Supervisor._is_caption_element(FakeEl("decklinkvideosrc0", root)))

    def test_inline_whitespace_is_trimmed(self) -> None:
        # Values arrive already sourced by bash (see nexvue-encode@.service),
        # so inline "# comments" are already gone — but defend against
        # stray leading/trailing whitespace from a hand-edited env file.
        cfg = mod.load_config({"DEVICE_NUMBER": "  0  ", "CHANNEL_PATH": "  ch0  "})
        self.assertEqual(cfg.device_number, 0)
        self.assertEqual(cfg.channel_path, "ch0")


class TestStateMachine(unittest.TestCase):
    def _machine(self, **kwargs):
        self.events = []
        clock = kwargs.pop("clock", None) or FakeClock()
        self.clock = clock
        sm = mod.StateMachine(
            loss_debounce_s=kwargs.pop("loss_debounce_s", 15.0),
            acquire_debounce_s=kwargs.pop("acquire_debounce_s", 1.0),
            clock=clock,
            on_enter_live=lambda: self.events.append("LIVE"),
            on_enter_slate=lambda: self.events.append("SLATE"),
            on_enter_recovering=lambda: self.events.append("RECOVERING"),
            on_caption_clear=lambda: self.events.append("CLEAR"),
            **kwargs,
        )
        return sm

    def test_boots_in_slate_without_calling_any_callback(self) -> None:
        sm = self._machine()
        self.assertEqual(sm.state, mod.State.SLATE)
        self.assertEqual(self.events, [])

    def test_boot_with_lock_promotes_to_live_after_acquire_debounce(self) -> None:
        sm = self._machine(acquire_debounce_s=1.0)
        sm.on_signal(True)
        self.assertEqual(sm.state, mod.State.RECOVERING)
        self.assertEqual(self.events, ["RECOVERING"])

        sm.on_valid_buffer()
        self.clock.advance(0.5)
        sm.tick()
        self.assertEqual(sm.state, mod.State.RECOVERING)  # not yet — under debounce

        self.clock.advance(0.6)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)
        self.assertEqual(self.events, ["RECOVERING", "LIVE"])

    def test_acquire_never_promotes_without_a_valid_buffer(self) -> None:
        # Parameter lock alone (signal=true, no buffers) must never be
        # mistaken for "captured frames are actually flowing."
        sm = self._machine(acquire_debounce_s=1.0)
        sm.on_signal(True)
        self.clock.advance(5.0)
        sm.tick()
        self.assertEqual(sm.state, mod.State.RECOVERING)

    def test_signal_loss_while_recovering_demotes_immediately(self) -> None:
        sm = self._machine(acquire_debounce_s=1.0)
        sm.on_signal(True)
        sm.on_valid_buffer()
        self.assertEqual(sm.state, mod.State.RECOVERING)
        sm.on_signal(False)
        self.assertEqual(sm.state, mod.State.SLATE)
        self.assertEqual(self.events, ["RECOVERING", "SLATE", "CLEAR"])

    def test_short_hiccup_while_live_never_reaches_slate(self) -> None:
        sm = self._machine(loss_debounce_s=15.0, acquire_debounce_s=1.0)
        sm.on_signal(True)
        sm.on_valid_buffer()
        self.clock.advance(1.5)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)

        # Brief loss (well under the generous 15s debounce) heals itself —
        # black frames ride through, never a visible slate flip.
        sm.on_signal(False)
        self.clock.advance(2.0)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)
        sm.on_signal(True)
        self.clock.advance(1.0)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)
        self.assertNotIn("SLATE", self.events)

    def test_sustained_loss_while_live_demotes_after_debounce_and_clears_once(self) -> None:
        sm = self._machine(loss_debounce_s=15.0, acquire_debounce_s=1.0)
        sm.on_signal(True)
        sm.on_valid_buffer()
        self.clock.advance(1.5)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)

        sm.on_signal(False)
        self.clock.advance(14.9)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)  # still under debounce

        self.clock.advance(0.2)
        sm.tick()
        self.assertEqual(sm.state, mod.State.SLATE)
        # Caption clear fires exactly once on entering SLATE.
        self.assertEqual(self.events.count("CLEAR"), 1)

        # Repeated ticks while already SLATE must not re-fire the clear.
        self.clock.advance(5.0)
        sm.tick()
        sm.tick()
        self.assertEqual(self.events.count("CLEAR"), 1)

    def test_decklink_error_forces_immediate_slate_bypassing_loss_debounce(self) -> None:
        sm = self._machine(loss_debounce_s=15.0, acquire_debounce_s=1.0)
        sm.on_signal(True)
        sm.on_valid_buffer()
        self.clock.advance(1.5)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)

        sm.on_decklink_error()
        self.assertEqual(sm.state, mod.State.SLATE)
        self.assertEqual(self.events.count("CLEAR"), 1)

    def test_decklink_error_while_already_slate_is_a_no_op(self) -> None:
        sm = self._machine()
        self.assertEqual(sm.state, mod.State.SLATE)
        sm.on_decklink_error()
        self.assertEqual(sm.state, mod.State.SLATE)
        self.assertEqual(self.events, [])  # no spurious extra CLEAR

    def test_live_to_slate_to_recovering_to_live_full_cycle(self) -> None:
        sm = self._machine(loss_debounce_s=1.0, acquire_debounce_s=1.0)
        sm.on_signal(True)
        sm.on_valid_buffer()
        self.clock.advance(1.1)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)

        sm.on_signal(False)
        self.clock.advance(1.1)
        sm.tick()
        self.assertEqual(sm.state, mod.State.SLATE)

        sm.on_signal(True)
        self.assertEqual(sm.state, mod.State.RECOVERING)
        sm.on_valid_buffer()
        self.clock.advance(1.1)
        sm.tick()
        self.assertEqual(sm.state, mod.State.LIVE)
        self.assertEqual(self.events, ["RECOVERING", "LIVE", "SLATE", "CLEAR", "RECOVERING", "LIVE"])


class TestPureHelpers(unittest.TestCase):
    def test_norm_caps_string(self) -> None:
        caps = mod.norm_caps_string(1920, 1080, "60000/1001")
        self.assertEqual(
            caps,
            "video/x-raw,format=NV12,width=1920,height=1080,framerate=60000/1001,pixel-aspect-ratio=1/1",
        )

    def test_build_encoder_desc_vah264enc(self) -> None:
        cfg = mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0"})
        desc = mod.build_encoder_desc(cfg, 5000)
        self.assertIn("vah264enc", desc)
        self.assertIn("bitrate=5000", desc)
        self.assertIn("key-int-max=60", desc)
        self.assertIn("b-frames=0", desc)
        self.assertIn("target-usage=7", desc)

    def test_build_encoder_desc_lo_uses_lo_target_usage(self) -> None:
        cfg = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "LO_ENABLE": "true",
                "LO_TARGET_USAGE": "4",
                "LO_GOP_FRAMES": "30",
            }
        )
        hi = mod.build_encoder_desc(cfg, cfg.bitrate_kbps)
        lo = mod.build_encoder_desc(cfg, cfg.lo_bitrate_kbps, for_lo=True)
        self.assertIn("target-usage=7", hi)
        self.assertIn("target-usage=4", lo)
        self.assertIn("key-int-max=30", lo)

    def test_leaky_queue_disables_time_and_bytes_caps(self) -> None:
        q = mod._leaky_queue(16, name="loq")
        self.assertIn("name=loq", q)
        self.assertIn("max-size-buffers=16", q)
        self.assertIn("max-size-time=0", q)
        self.assertIn("max-size-bytes=0", q)
        self.assertIn("leaky=downstream", q)

    def test_lo_static_desc_disables_filter_qos(self) -> None:
        cfg = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "LO_ENABLE": "true",
                "LO_PRESET": "360p",
            }
        )
        desc = mod.Supervisor(cfg)._build_static_desc()
        self.assertIn("videorate qos=false", desc)
        self.assertIn("videoscale qos=false", desc)
        self.assertIn("videoconvert qos=false", desc)
        self.assertIn("max-size-time=0 max-size-bytes=0", desc)
        self.assertIn("target-usage=7", desc)

    def test_build_encoder_desc_x264_fallback(self) -> None:
        cfg = mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "VIDEO_ENCODER": "x264enc"})
        desc = mod.build_encoder_desc(cfg, 2500)
        self.assertIn("x264enc tune=zerolatency", desc)
        self.assertIn("speed-preset=veryfast", desc)
        self.assertIn("bitrate=2500", desc)
        lo = mod.build_encoder_desc(cfg, 800, for_lo=True)
        self.assertIn("speed-preset=veryfast", lo)

    def test_slate_overlay_text_with_and_without_alias(self) -> None:
        cfg = mod.load_config({"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0"})
        self.assertEqual(mod.slate_overlay_text(cfg), "NO SIGNAL")
        cfg2 = mod.load_config(
            {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "CHANNEL_ALIAS": "Prompter A"}
        )
        self.assertEqual(mod.slate_overlay_text(cfg2), "NO SIGNAL - Prompter A")

    def test_slate_overlay_text_sanitizes_quotes(self) -> None:
        cfg = mod.load_config(
            {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "CHANNEL_ALIAS": 'Cam "1"'}
        )
        self.assertNotIn('"', mod.slate_overlay_text(cfg))


class TestSrtAndLoPool(unittest.TestCase):
    def test_srt_config_requires_uri_and_disables_captions(self) -> None:
        cfg = mod.load_config(
            {
                "INPUT_TYPE": "srt",
                "CHANNEL_PATH": "ch8",
                "CHANNEL_ID": "8",
                "SRT_URI": "srt://10.0.0.5:9000?mode=caller",
            }
        )
        self.assertEqual(cfg.input_type, "srt")
        self.assertEqual(cfg.channel_id, 8)
        self.assertEqual(cfg.srt_uri, "srt://10.0.0.5:9000?mode=caller")
        self.assertFalse(cfg.captions_enable)
        with self.assertRaises(mod.ConfigError):
            mod.load_config(
                {"INPUT_TYPE": "srt", "CHANNEL_PATH": "ch8", "CHANNEL_ID": "8"}
            )

    def test_srt_rejects_non_srt_uri(self) -> None:
        with self.assertRaises(mod.ConfigError):
            mod.load_config(
                {
                    "INPUT_TYPE": "srt",
                    "CHANNEL_PATH": "ch8",
                    "CHANNEL_ID": "8",
                    "SRT_URI": "rtsp://127.0.0.1/x",
                }
            )

    def test_lo_pool_grants_first_max_by_ascending_id(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(8):
                (root / f"{i}.env").write_text(f"LO_ENABLE=true\nCHANNEL_PATH=ch{i}\n", encoding="utf-8")
            # Channel 6 is the 7th requester (0..6) → denied when max=6
            denied = mod.load_config(
                {
                    "DEVICE_NUMBER": "0",
                    "CHANNEL_PATH": "ch6",
                    "CHANNEL_ID": "6",
                    "LO_ENABLE": "true",
                    "MAX_LO_RENDITIONS": "6",
                },
                channels_dir=root,
            )
            self.assertTrue(denied.lo_requested)
            self.assertFalse(denied.lo_enable)
            # Channel 5 is within the first 6 (0..5)
            granted = mod.load_config(
                {
                    "DEVICE_NUMBER": "0",
                    "CHANNEL_PATH": "ch5",
                    "CHANNEL_ID": "5",
                    "LO_ENABLE": "true",
                    "MAX_LO_RENDITIONS": "6",
                },
                channels_dir=root,
            )
            self.assertTrue(granted.lo_enable)

    def test_lo_pool_allows_sparse_assignment(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in (1, 2, 4, 5, 8, 9):
                (root / f"{i}.env").write_text("LO_ENABLE=true\n", encoding="utf-8")
            for i in (1, 2, 4, 5, 8, 9):
                cfg = mod.load_config(
                    {
                        "DEVICE_NUMBER": "0" if i < 8 else "0",
                        "CHANNEL_PATH": f"ch{i}",
                        "CHANNEL_ID": str(i),
                        "LO_ENABLE": "true",
                        "MAX_LO_RENDITIONS": "6",
                        "INPUT_TYPE": "decklink" if i < 8 else "srt",
                        **(
                            {"SRT_URI": "srt://127.0.0.1:9000?mode=caller"}
                            if i >= 8
                            else {}
                        ),
                    },
                    channels_dir=root,
                )
                self.assertTrue(cfg.lo_enable, f"channel {i} should get LO")


class TestCaptionsSupervisorWithoutHelper(unittest.TestCase):
    def test_disabled_when_captions_enable_false(self) -> None:
        cfg = mod.load_config(
            {"DEVICE_NUMBER": "0", "CHANNEL_PATH": "ch0", "CAPTIONS_ENABLE": "false"}
        )
        cs = mod.CaptionsSupervisor(cfg, mod.log)
        self.assertFalse(cs.enabled)
        cs.write_clear()  # must be a safe no-op
        cs.poll_respawn()  # must be a safe no-op
        cs.close()

    def test_disabled_when_decode_helper_missing(self) -> None:
        cfg = mod.load_config(
            {
                "DEVICE_NUMBER": "0",
                "CHANNEL_PATH": "ch0",
                "CAPTIONS_DECODE_BIN": "/nonexistent/nexvue-captions-decode.py",
            }
        )
        cs = mod.CaptionsSupervisor(cfg, mod.log)
        self.assertFalse(cs.enabled)


if __name__ == "__main__":
    unittest.main()
