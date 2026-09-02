#!/usr/bin/env python3
"""
Contract + optional Node unit tests for Safe, WFM/vectorscope, and Player RTA.

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
SPECTRUM = ROOT / "web-node" / "nexvue-spectrum.js"
VU = ROOT / "web-node" / "nexvue-vu.js"
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
        self.assertIn('PREF_POP = "nexvue-scopes-pop"', js)
        self.assertIn('PREF_POS = "nexvue-scopes-pos"', js)
        self.assertIn("layoutFor", js)
        self.assertIn("sampleW: plotW", js)
        self.assertIn("nexvue-scopes-pop", js)
        self.assertIn("z-index: 55", js)
        self.assertIn("Click to enlarge", js)
        self.assertIn("Drag to move", js)
        self.assertIn("clampPos", js)
        self.assertIn("parsePos", js)
        self.assertIn("rgbToYcbcr", js)
        self.assertIn("yToIre", js)
        self.assertIn("0.2126", js)
        self.assertIn("BAR75", js)
        self.assertIn("requestVideoFrameCallback", js)
        self.assertIn("NexVueScopes", js)
        self.assertIn("i += 4", js)
        self.assertNotIn("i += 16", js)

    def test_spectrum_exports_rta_scale(self) -> None:
        js = SPECTRUM.read_text(encoding="utf-8")
        self.assertIn('PREF_ON = "nexvue-spectrum-on"', js)
        self.assertIn('PREF_POP = "nexvue-spectrum-pop"', js)
        self.assertIn('PREF_POS = "nexvue-spectrum-pos"', js)
        self.assertIn("BANDS = 64", js)
        self.assertIn("F_MIN = 10", js)
        self.assertIn("F_MAX = 22000", js)
        self.assertIn("DB_MIN = -60", js)
        self.assertIn("DB_MAX = 0", js)
        self.assertIn("0, -6, -12, -20, -30, -40, -60", js)
        self.assertIn('return db === 0 ? "FS"', js)
        self.assertIn("ALIGN_DB = -20", js)
        self.assertIn("rgba(86,196,245", js)
        self.assertIn("z-index: 55", js)
        self.assertIn("Click to enlarge", js)
        self.assertIn("bandEdges", js)
        self.assertIn("fillBands", js)
        self.assertIn("getSpectrumPair", js)
        self.assertIn("NexVueSpectrum", js)

    def test_vu_exposes_spectrum_pair(self) -> None:
        js = VU.read_text(encoding="utf-8")
        self.assertIn("SPEC_FFT = 8192", js)
        self.assertIn("function wireSpectrum", js)
        self.assertIn("getSpectrumPair", js)
        self.assertIn("MediaStreamSource", js)
        self.assertIn('source.channelInterpretation = "discrete"', js)
        self.assertIn('splitter.channelInterpretation = "discrete"', js)
        self.assertIn('return db === 0 ? "FS"', js)
        self.assertIn(".nexvue-vu-track::after", js)
        self.assertIn("#56c4f5", js)
        self.assertIn("text-shadow: 0 0 3px #000", js)
        self.assertIn("ALIGN_DB", js)
        self.assertIn("SCALE_MARKS_DB", js)
        self.assertIn('PREF_POP = "nexvue-vu-pop"', js)
        self.assertIn("nexvue-vu-floatable", js)
        self.assertIn("nexvue-vu-pop", js)
        self.assertIn("background: var(--panel", js)
        self.assertIn("width: 16px", js)
        self.assertIn("z-index: 55", js)
        self.assertNotIn("nexvue-vu-rail", js)
        self.assertNotIn("opts.rail", js)

    def test_player_has_safe_and_scope_toggles(self) -> None:
        html = PLAYER.read_text(encoding="utf-8")
        self.assertIn('id="safe"', html)
        self.assertIn('id="scope"', html)
        self.assertIn('id="rta"', html)
        self.assertIn("nexvue-safe.js", html)
        self.assertIn("nexvue-scopes.js", html)
        self.assertIn("nexvue-spectrum.js", html)
        self.assertIn("NexVueSafe.attach", html)
        self.assertIn("NexVueScopes.attach", html)
        self.assertIn("NexVueSpectrum.attach", html)
        self.assertIn("floatable: true", html)
        self.assertIn("stage-row", html)
        self.assertIn("aspect-ratio: 16 / 9", html)
        self.assertNotIn("rail: true", html)
        self.assertNotIn("nexvue-vu-rail", html)

    def test_multiview_has_safe_and_scope_toggles(self) -> None:
        html = MULTI.read_text(encoding="utf-8")
        self.assertIn('id="safe"', html)
        self.assertIn('id="scope"', html)
        self.assertNotIn('id="rta"', html)
        self.assertIn("nexvue-safe.js", html)
        self.assertIn("nexvue-scopes.js", html)
        self.assertNotIn("nexvue-spectrum.js", html)
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
const dock = S.layoutFor(false);
const pop = S.layoutFor(true);
if (dock.sampleW !== dock.plotW) throw new Error("dock sampleW");
if (pop.sampleW !== pop.plotW) throw new Error("pop sampleW");
if (dock.wfmW !== 220 || dock.vecSize !== 140) throw new Error("dock size");
if (pop.wfmW !== 440 || pop.vecSize !== 280) throw new Error("pop size");
if (pop.plotW <= dock.plotW) throw new Error("pop not wider");
if (S.parsePos(null) !== null) throw new Error("parse null");
if (S.parsePos("nope") !== null) throw new Error("parse junk");
if (S.parsePos('{{"left":12}}') !== null) throw new Error("parse incomplete");
const p = S.parsePos('{{"left":40.5,"top":80}}');
if (!p || p.left !== 40.5 || p.top !== 80) throw new Error("parse pos");
const obj = S.parsePos({{ left: 10, top: 20 }});
if (!obj || obj.left !== 10 || obj.top !== 20) throw new Error("parse object");
const c = S.clampPos(-40, -10, 200, 100, 1000, 800, 8);
if (c.left !== 8 || c.top !== 8) throw new Error("clamp low " + JSON.stringify(c));
const d = S.clampPos(900, 750, 200, 100, 1000, 800, 8);
if (d.left !== 792 || d.top !== 692) throw new Error("clamp high " + JSON.stringify(d));
const tiny = S.clampPos(0, 0, 900, 700, 400, 300, 8);
if (tiny.left !== 8 || tiny.top !== 8) throw new Error("clamp overflow " + JSON.stringify(tiny));
if (S.DRAG_THRESHOLD !== 6) throw new Error("threshold");
"""
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_spectrum_log_bands_and_dbfs(self) -> None:
        js_path = SPECTRUM.as_posix()
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
const S = ctx.NexVueSpectrum;
if (S.BANDS !== 64) throw new Error("bands");
if (S.F_MIN !== 10 || S.F_MAX !== 22000) throw new Error("freq");
if (S.DB_MIN !== -60 || S.DB_MAX !== 0) throw new Error("db");
if (S.heightFromDb(-60) !== 0) throw new Error("floor");
if (S.heightFromDb(0) !== 100) throw new Error("fs");
if (Math.abs(S.heightFromDb(-30) - 50) > 0.01) throw new Error("mid");
if (S.heightFromDb(-90) !== 0) throw new Error("below floor");
if (S.heightFromDb(6) !== 100) throw new Error("above fs");
const edges = S.bandEdges(64, 10, 22000);
if (edges.length !== 65) throw new Error("edge count");
if (Math.abs(edges[0] - 10) > 1e-6) throw new Error("fmin");
if (Math.abs(edges[64] - 22000) > 1e-6) throw new Error("fmax");
const mid = edges[32];
if (!(mid > 400 && mid < 600)) throw new Error("log mid " + mid);
const fft = 8192;
const sr = 48000;
const bins = new Float32Array(fft / 2);
bins.fill(-120);
const hzPerBin = sr / fft;
const kHzBin = Math.round(1000 / hzPerBin);
bins[kHzBin] = 0;
const out = new Float32Array(64);
S.fillBands(bins, sr, fft, out, edges);
let hit = -1;
for (let i = 0; i < 64; i++) {{
  if (out[i] > -1) hit = i;
}}
if (hit < 0) throw new Error("1kHz not mapped");
if (!(edges[hit] <= 1000 && edges[hit + 1] >= 1000)) throw new Error("1kHz band " + hit);
if (S.freqLabel(10) !== "10") throw new Error("lab 10");
if (S.freqLabel(1000) !== "1k") throw new Error("lab 1k");
if (S.freqLabel(22000) !== "22k") throw new Error("lab 22k");
if (S.dbScaleLabel(0) !== "FS") throw new Error("fs label");
if (S.dbScaleLabel(-6) !== "-6") throw new Error("m6 label");
if (S.ALIGN_DB !== -20) throw new Error("align");
if (S.DB_MARKS.join(",") !== "0,-6,-12,-20,-30,-40,-60") throw new Error("marks");
const dock = S.layoutFor(false);
const pop = S.layoutFor(true);
if (dock.w !== 520 || pop.w !== 880) throw new Error("rta size");
if (pop.plotH <= dock.plotH || pop.w <= dock.w) throw new Error("rta pop not larger");
if (S.parsePos(null) !== null) throw new Error("parse null");
const rp = S.parsePos('{{"left":40,"top":80}}');
if (!rp || rp.left !== 40 || rp.top !== 80) throw new Error("parse pos");
const rc = S.clampPos(-10, -10, 200, 100, 1000, 800, 8);
if (rc.left !== 8 || rc.top !== 8) throw new Error("clamp");
if (S.DRAG_THRESHOLD !== 6) throw new Error("threshold");
"""
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
