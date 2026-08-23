#!/usr/bin/env python3
"""
Contract + optional Node unit tests for Safe overlay and WFM/vectorscope.

Run: python3 test/test_nexvue_video_overlays.py
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
SAFE = ROOT / "web-node" / "nexvue-safe.js"
SCOPES = ROOT / "web-node" / "nexvue-scopes.js"
PLAYER = ROOT / "web-node" / "index.html"
MULTI = ROOT / "web-node" / "multiview.html"


class TestOverlayContracts(unittest.TestCase):
    def test_safe_exports_and_hd_insets(self) -> None:
        js = SAFE.read_text(encoding="utf-8")
        self.assertIn('PREF_ON = "nexvue-safe-on"', js)
        self.assertIn('PREF_TARGET = "nexvue-safe-target"', js)
        self.assertIn('PREF_CUT43 = "nexvue-safe-43"', js)
        self.assertIn("ACTION_INSET = 3.5", js)
        self.assertIn("TITLE_INSET = 5", js)
        self.assertIn("CUT43_WIDTH = 75", js)
        for name in (
            "getOnPref",
            "setOnPref",
            "videoContentRect",
            "attach",
            "NexVueSafe",
        ):
            self.assertIn(name, js, name)

    def test_scopes_exports_rec709_and_ire(self) -> None:
        js = SCOPES.read_text(encoding="utf-8")
        self.assertIn('PREF_ON = "nexvue-scopes-on"', js)
        self.assertIn("rgbToYcbcr", js)
        self.assertIn("yToIre", js)
        self.assertIn("0.2126", js)
        self.assertIn("BAR75", js)
        self.assertIn("requestVideoFrameCallback", js)
        self.assertIn("NexVueScopes", js)
        self.assertIn("i += 4", js)
        self.assertNotIn("i += 16", js)
        self.assertIn("const SAMPLE_W = PLOT_W", js)

    def test_player_has_safe_and_scope_toggles(self) -> None:
        html = PLAYER.read_text(encoding="utf-8")
        self.assertIn('id="safe"', html)
        self.assertIn('id="scope"', html)
        self.assertIn("nexvue-safe.js", html)
        self.assertIn("nexvue-scopes.js", html)
        self.assertIn("NexVueSafe.attach", html)
        self.assertIn("NexVueScopes.attach", html)

    def test_multiview_has_safe_and_scope_toggles(self) -> None:
        html = MULTI.read_text(encoding="utf-8")
        self.assertIn('id="safe"', html)
        self.assertIn('id="scope"', html)
        self.assertIn("nexvue-safe.js", html)
        self.assertIn("nexvue-scopes.js", html)
        self.assertIn("NexVueSafe.attach", html)
        self.assertIn("applyScopeVisible", html)


@unittest.skipUnless(NODE, "node not available")
class TestOverlayMath(unittest.TestCase):
    def test_black_is_0_ire_white_is_100(self) -> None:
        js_path = SCOPES.as_posix()
        script = f"""
const fs = require("fs");
const vm = require("vm");
const ctx = {{ window: {{}}, globalThis: {{}}, document: {{
  getElementById: () => null,
  createElement: () => ({{ style: {{}}, appendChild() {{}}, querySelector() {{ return null; }}, setAttribute() {{}} }}),
  head: {{ appendChild() {{}} }},
}}}};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync({js_path!r}, "utf8"), ctx);
const S = ctx.NexVueScopes;
const blk = S.rgbToYcbcr(0, 0, 0);
const wht = S.rgbToYcbcr(1, 1, 1);
if (Math.abs(S.yToIre(blk.y)) > 0.01) throw new Error("black IRE " + S.yToIre(blk.y));
if (Math.abs(S.yToIre(wht.y) - 100) > 0.2) throw new Error("white IRE " + S.yToIre(wht.y));
const t = S.barTargets();
if (t.length !== 6) throw new Error("targets " + t.length);
const names = t.map((x) => x.name).join(",");
if (names !== "R,Mg,B,Cy,G,Yl") throw new Error(names);
"""
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
