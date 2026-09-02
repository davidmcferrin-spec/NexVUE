#!/usr/bin/env python3
"""
Contract + optional Node unit tests for fill-window (theater) and Player PiP.

Run: python3 test/test_nexvue_ui_theater.py
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
UI = ROOT / "web-node" / "nexvue-ui.js"
PLAYER = ROOT / "web-node" / "index.html"
MULTI = ROOT / "web-node" / "multiview.html"
OTHER_PAGES = (
    "metrics.html",
    "channels.html",
    "services.html",
    "users.html",
    "login.html",
)


class TestTheaterContracts(unittest.TestCase):
    def test_ui_exports_theater_and_pip(self) -> None:
        js = UI.read_text(encoding="utf-8")
        self.assertIn('THEATER_KEY = "nexvue-theater"', js)
        self.assertIn("data-theater-page", js)
        for name in (
            "getTheaterPref",
            "setTheater",
            "toggleTheater",
            "applyTheater",
            "wireTheaterControls",
            "pipSupported",
            "isPipActive",
            "requestPip",
            "exitPip",
            "togglePip",
            "requestPictureInPicture",
            "nexvue-theater-changed",
        ):
            self.assertIn(name, js, name)
        self.assertIn("classList.add(\"theater\")", js)

    def test_player_has_fill_window_and_pip(self) -> None:
        html = PLAYER.read_text(encoding="utf-8")
        self.assertIn('data-theater-page', html)
        self.assertIn('id="theater"', html)
        self.assertIn('id="theater-exit"', html)
        self.assertIn("html.theater .videowrap", html)
        self.assertIn("html.theater .stage-row", html)
        self.assertIn("html.theater.player-idle .bar:not(.controls)", html)
        self.assertIn('id="pip"', html)
        self.assertIn("togglePip", html)
        self.assertIn("enterpictureinpicture", html)
        self.assertIn("restorePipIfNeeded", html)
        self.assertIn("preferPip", html)
        self.assertIn("pipLeavingForTeardown", html)
        self.assertIn("dblclick", html)

    def test_multiview_has_fill_window_not_pip(self) -> None:
        html = MULTI.read_text(encoding="utf-8")
        self.assertIn('data-theater-page', html)
        self.assertIn('id="theater"', html)
        self.assertIn('id="theater-exit"', html)
        self.assertIn("html.theater .pane-bar", html)
        self.assertIn("html.theater", html)
        self.assertNotIn('id="pip"', html)
        self.assertNotIn("togglePip", html)
        self.assertNotIn("enterpictureinpicture", html)

    def test_ops_pages_do_not_opt_into_theater(self) -> None:
        for name in OTHER_PAGES:
            html = (ROOT / "web-node" / name).read_text(encoding="utf-8")
            self.assertNotIn("data-theater-page", html, name)
            self.assertNotIn('id="theater"', html, name)


class TestToolbarClusters(unittest.TestCase):
    def test_player_clusters_and_orient_popover(self) -> None:
        html = PLAYER.read_text(encoding="utf-8")
        self.assertIn('class="bar controls"', html)
        for cluster in (
            'class="bar-cluster watch"',
            'class="bar-cluster listen"',
            'class="bar-cluster overlays"',
            'id="orient-wrap"',
            'class="bar-cluster window"',
        ):
            self.assertIn(cluster, html, cluster)
        self.assertIn('id="orient"', html)
        self.assertIn('id="orient-pop"', html)
        self.assertIn('id="orient-reset"', html)
        self.assertIn("setOrientOpen", html)
        self.assertIn("paintOrientBtn", html)
        for keep in ('id="mirror"', 'id="flip"', 'id="rot-cw"', 'id="rot-ccw"'):
            self.assertIn(keep, html, keep)
        self.assertIn('aria-label="Fill window"', html)
        self.assertIn('aria-label="Fullscreen"', html)
        self.assertIn('aria-label="Picture-in-Picture"', html)
        self.assertNotIn(">▢ Fill window<", html)
        self.assertNotIn(">⛶ Fullscreen<", html)

    def test_multiview_clusters_without_orient(self) -> None:
        html = MULTI.read_text(encoding="utf-8")
        self.assertIn('class="bar controls"', html)
        self.assertIn('class="bar-cluster layout"', html)
        self.assertIn('class="bar-cluster watch"', html)
        self.assertIn('class="bar-cluster overlays"', html)
        self.assertIn('class="bar-cluster listen"', html)
        self.assertIn('class="bar-cluster window"', html)
        self.assertNotIn('id="orient"', html)
        self.assertNotIn('id="orient-pop"', html)
        self.assertNotIn('id="mirror"', html)
        self.assertIn('aria-label="Fill window"', html)
        self.assertIn('aria-label="Fullscreen"', html)
        self.assertNotIn(">▢ Fill window<", html)
        self.assertNotIn(">⛶ Fullscreen<", html)


class TestStatsDrawerInFlow(unittest.TestCase):
    """Session metrics must stay in document flow — not position:fixed.

    iOS Chrome/Safari pin fixed bottom:0 to the layout viewport, so the
    bar detaches above the visible window when the toolbar is showing.
    """

    def test_player_drawer_is_in_flow(self) -> None:
        html = PLAYER.read_text(encoding="utf-8")
        self.assertIn("viewport-fit=cover", html)
        self.assertIn("100dvh", html)
        self.assertIn("safe-area-inset-bottom", html)
        self.assertIn("flex-shrink: 0", html)
        self.assertNotIn("position: fixed; left: 0; right: 0; bottom: 0", html)
        self.assertNotIn("padding-bottom: 56px", html)
        self.assertIn('id="stats-drawer"', html)

    def test_multiview_drawer_is_in_flow(self) -> None:
        html = MULTI.read_text(encoding="utf-8")
        self.assertIn("viewport-fit=cover", html)
        self.assertIn("100dvh", html)
        self.assertIn("safe-area-inset-bottom", html)
        self.assertNotIn("position: fixed; left: 0; right: 0; bottom: 0", html)
        self.assertIn('id="stats-drawer"', html)


@unittest.skipUnless(NODE and UI.is_file(), "node CLI missing")
class TestTheaterJs(unittest.TestCase):
    def test_pref_and_class_and_pip_guards(self) -> None:
        harness = r"""
const fs = require("fs");
const store = {};
const classList = new Set();
const attrs = { "data-theater-page": "" };
const theaterBtn = {
  classList: { toggle() {} },
  setAttribute() {},
  addEventListener() {},
};
const document = {
  documentElement: {
    classList: {
      add: (c) => classList.add(c),
      remove: (c) => classList.delete(c),
      contains: (c) => classList.has(c),
      toggle: (c, on) => { if (on) classList.add(c); else classList.delete(c); },
    },
    hasAttribute: (n) => Object.prototype.hasOwnProperty.call(attrs, n),
    getAttribute: () => null,
    setAttribute() {},
  },
  getElementById: (id) => (id === "theater" ? theaterBtn : null),
  addEventListener() {},
  pictureInPictureEnabled: true,
  pictureInPictureElement: null,
  exitPictureInPicture: () => Promise.resolve(),
  readyState: "complete",
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return { id: "", textContent: "", style: {}, setAttribute() {} }; },
  head: { appendChild() {} },
};
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const window = {
  localStorage,
  document,
  dispatchEvent() {},
  addEventListener() {},
};
global.window = window;
global.document = document;
global.localStorage = localStorage;
global.CustomEvent = class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
eval(fs.readFileSync(process.argv[2], "utf8"));
const UI = window.NexVueUI;
if (!UI) throw new Error("NexVueUI missing");
if (UI.THEATER_KEY !== "nexvue-theater") throw new Error("THEATER_KEY");
if (UI.getTheaterPref() !== false) throw new Error("pref default");
if (UI.isTheater() !== false) throw new Error("class default");
UI.setTheater(true);
if (UI.getTheaterPref() !== true) throw new Error("pref on");
if (!classList.has("theater")) throw new Error("class on");
if (store["nexvue-theater"] !== "1") throw new Error("storage 1");
UI.setTheater(false);
if (UI.getTheaterPref() !== false) throw new Error("pref off");
if (classList.has("theater")) throw new Error("class off");
if (Object.prototype.hasOwnProperty.call(store, "nexvue-theater")) throw new Error("storage cleared");
if (UI.pipSupported() !== true) throw new Error("pipSupported");
const dead = { srcObject: null };
UI.requestPip(dead).then((on) => {
  if (on !== false) throw new Error("pip no srcObject");
  console.log("ok");
});
"""
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(harness, encoding="utf-8")
            r = subprocess.run(
                [NODE, str(script), str(UI)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
            self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
