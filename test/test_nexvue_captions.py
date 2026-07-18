#!/usr/bin/env python3
"""
Unit tests for caption side channel: CEA-608 decoder, PHP helpers, and
player DOM / Cast payload contracts.

Run: python3 test/test_nexvue_captions.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHP = shutil.which("php")
CAPTIONS_PHP = ROOT / "nexvue-captions.php"
DECODE_PY = ROOT / "nexvue-captions-decode.py"
CAPTIONS_JS = ROOT / "nexvue-captions.js"


def _load_decode():
    spec = importlib.util.spec_from_file_location("nexvue_captions_decode", DECODE_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _text_pairs(s: str) -> list[tuple[int, int]]:
    """Pack ASCII text into CEA-608 character pairs (odd length padded with 0)."""
    codes = [ord(c) for c in s]
    if len(codes) % 2:
        codes.append(0x00)
    return [(codes[i], codes[i + 1]) for i in range(0, len(codes), 2)]


class TestCea608Decoder(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_decode()

    def _feed(self, dec, pairs) -> list[str]:
        texts = []
        for a, b in pairs:
            t = dec.feed_pair(a, b)
            if t is not None:
                texts.append(t)
        return texts

    def test_rollup_text_and_edm(self) -> None:
        dec = self.mod.Cea608Cc1()
        # PAC row 15 (approx via 0x14/0x40), then "HI", then EDM.
        pairs = [
            (0x14, 0x70),  # PAC toward bottom rows
            (0x48, 0x49),  # H I
            (0x14, 0x2C),  # EDM
        ]
        texts = []
        for a, b in pairs:
            t = dec.feed_pair(a, b)
            if t is not None:
                texts.append(t)
        self.assertTrue(any("HI" in t for t in texts), texts)
        self.assertEqual(texts[-1], "")

    def test_rollup_caps_at_two_lines(self) -> None:
        # RU3 window fills with three lines; overlay contract is last-2 only,
        # newest at the bottom.
        dec = self.mod.Cea608Cc1()
        pairs = [(0x14, 0x26)]          # RU3
        pairs += _text_pairs("ONE")
        pairs += [(0x14, 0x2D)]         # CR
        pairs += _text_pairs("TWO")
        pairs += [(0x14, 0x2D)]         # CR
        pairs += _text_pairs("SIX")
        texts = self._feed(dec, pairs)
        self.assertEqual(dec.visible_text(), "TWO\nSIX")
        self.assertTrue(all(t.count("\n") <= 1 for t in texts), texts)

    def test_entering_rollup_erases_popon_leftovers(self) -> None:
        # Pop-on segment (e.g. an ad) leaves positioned text; switching back
        # to roll-up must not leave it behind as a frozen line.
        dec = self.mod.Cea608Cc1()
        pairs = [(0x14, 0x20)]          # RCL (pop-on)
        pairs += [(0x14, 0x70)]         # PAC near the bottom
        pairs += _text_pairs("SPONSOR")
        pairs += [(0x14, 0x2F)]         # EOC — swap to display
        texts = self._feed(dec, pairs)
        self.assertIn("SPONSOR", texts[-1])
        texts = self._feed(dec, [(0x14, 0x25)])  # RU2
        self.assertEqual(dec.visible_text(), "")
        self.assertEqual(texts[-1], "")

    def test_rollup_pac_base_move_relocates_and_erases(self) -> None:
        # A roll-up PAC naming a new base row moves the whole window there;
        # the rows left behind are erased (this was the "stuck line" bug).
        dec = self.mod.Cea608Cc1()      # RU2 default, base row 14
        pairs = _text_pairs("AA")
        pairs += [(0x14, 0x2D)]         # CR
        pairs += _text_pairs("BB")
        self._feed(dec, pairs)
        self.assertEqual(dec.visible_text(), "AA\nBB")
        self._feed(dec, [(0x14, 0x40)])  # PAC → base row 12
        self.assertEqual(dec.visible_text(), "AA\nBB")
        disp = dec._displayed
        self.assertEqual("".join(disp[13]) + "".join(disp[14]), "")
        self.assertEqual("".join(disp[12]).rstrip(), "BB")

    def test_idle_clear_erases_visible_text_once(self) -> None:
        # CEA-608 receiver convention: display erased after ~16s of caption
        # silence. idle_clear reports the transition exactly once.
        dec = self.mod.Cea608Cc1()
        self._feed(dec, _text_pairs("HELLO"))
        self.assertEqual(dec.visible_text(), "HELLO")
        self.assertEqual(dec.idle_clear(), "")
        self.assertEqual(dec.visible_text(), "")
        self.assertIsNone(dec.idle_clear())  # already clear — no re-emit

    def test_feed_pair_survives_arbitrary_bytes(self) -> None:
        # The decoder must never raise: a crash would drop the FIFO reader
        # and take the whole encode pipeline down with it.
        dec = self.mod.Cea608Cc1()
        for b1 in range(0, 256, 7):
            for b2 in range(0, 256, 11):
                dec.feed_pair(b1, b2)

    def test_atomic_write_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ch0.json"
            self.mod.atomic_write_json(path, {"channel": "ch0", "text": "A", "seq": 1})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["text"], "A")
            self.assertEqual(data["seq"], 1)


@unittest.skipUnless(PHP and CAPTIONS_PHP.is_file(), "php CLI or nexvue-captions.php missing")
class TestCaptionsPhp(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str) -> dict:
        php_path = CAPTIONS_PHP.as_posix()
        cap_dir = self.dir.as_posix()
        code = f"""
putenv('NEXVUE_CAPTIONS_DIR={cap_dir}');
include '{php_path}';
{body}
"""
        env = os.environ.copy()
        env.pop("NEXVUE_CAPTIONS_HTTP", None)
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        if r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        out = (r.stdout or "").strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {out!r}\nstderr={r.stderr!r}")

    def test_normalize_channel(self) -> None:
        data = self._php("""
echo json_encode([
  'ok' => captions_normalize_channel('ch0'),
  'bad' => captions_normalize_channel('../etc'),
  'empty' => captions_normalize_channel(''),
]);
""")
        self.assertEqual(data["ok"], "ch0")
        self.assertIsNone(data["bad"])
        self.assertIsNone(data["empty"])

    def test_read_state_and_strip_controls(self) -> None:
        (self.dir / "ch1.json").write_text(
            json.dumps({"channel": "ch1", "text": "OK\x00X", "clear": False, "seq": 3, "ts": 1.5}),
            encoding="utf-8",
        )
        data = self._php("""
echo json_encode(captions_read_state('ch1'));
""")
        self.assertEqual(data["seq"], 3)
        self.assertNotIn("\x00", data["text"])
        self.assertIn("OK", data["text"])

    def test_stale_state_served_cleared(self) -> None:
        # Non-empty state whose file mtime is far older than the decoder's
        # idle-erase window means a dead writer — must not freeze on screen.
        path = self.dir / "ch2.json"
        path.write_text(
            json.dumps({"channel": "ch2", "text": "FROZEN", "clear": False, "seq": 9, "ts": 1.0}),
            encoding="utf-8",
        )
        old = 1_000_000_000  # year 2001
        os.utime(path, (old, old))
        data = self._php("echo json_encode(captions_read_state('ch2'));")
        self.assertEqual(data["text"], "")
        self.assertTrue(data["clear"])
        # Fresh file with the same content is served as-is.
        os.utime(path)
        data = self._php("echo json_encode(captions_read_state('ch2'));")
        self.assertEqual(data["text"], "FROZEN")
        self.assertFalse(data["clear"])

    def test_sse_encode(self) -> None:
        data = self._php("""
$s = captions_empty_state('ch0');
$s['text'] = 'HI';
$s['clear'] = false;
$s['seq'] = 2;
$line = captions_sse_encode($s);
echo json_encode(['line' => $line]);
""")
        self.assertTrue(data["line"].startswith("data: "))
        self.assertIn('"text":"HI"', data["line"])


class TestCaptionsDomContract(unittest.TestCase):
    def test_shared_js_exports(self) -> None:
        js = CAPTIONS_JS.read_text(encoding="utf-8")
        self.assertIn("NexVueCaptions", js)
        self.assertIn("nexvue-captions-on", js)
        self.assertIn("EventSource", js)

    def test_player_files_wire_cc(self) -> None:
        for name in ("index.html", "multiview.html", "cast-receiver.html"):
            html = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("nexvue-captions.js", html, name)
            self.assertIn("cc-overlay" if name != "multiview.html" else "pane-cc", html, name)
            if name == "index.html":
                self.assertIn('id="cc"', html)
                self.assertIn("captions:", html)
            if name == "cast-receiver.html":
                self.assertIn("captions", html)
            if name == "multiview.html":
                self.assertIn('id="cc"', html)

    def test_setup_lists_caption_files(self) -> None:
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        for needle in (
            "nexvue-captions.php",
            "nexvue-captions.js",
            "nexvue-captions-decode.py",
            "ccextractor",
        ):
            self.assertIn(needle, setup)


if __name__ == "__main__":
    unittest.main()
