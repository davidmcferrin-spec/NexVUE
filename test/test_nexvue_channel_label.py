#!/usr/bin/env python3
"""
CHANNEL_ALIAS display helpers: path stays identity; UIs show the alias.

Run: python3 test/test_nexvue_channel_label.py
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
USERS = ROOT / "web-node" / "users.html"
SHARE = ROOT / "web-node" / "nexvue-share-ui.js"
METRICS = ROOT / "web-node" / "metrics.html"
SERVICES = ROOT / "web-node" / "services.html"
CATALOG = ROOT / "web-portal" / "catalog.html"
WATCH = ROOT / "web-portal" / "watch.html"
CHANNELS = ROOT / "web-node" / "channels.html"


class TestAliasDisplayContracts(unittest.TestCase):
    def test_ui_exports_label_helpers(self) -> None:
        js = UI.read_text(encoding="utf-8")
        for name in (
            "channelBase",
            "channelLabel",
            "channelTitle",
            "channelLabels",
            "unitLabel",
            "loadChannelAliases",
            "setChannelAliases",
            "getChannelAliases",
            "/api/ops?action=aliases",
        ):
            self.assertIn(name, js, name)

    def test_users_and_shares_use_helpers(self) -> None:
        users = USERS.read_text(encoding="utf-8")
        self.assertIn("loadChannelAliases", users)
        self.assertIn("channelLabel", users)
        self.assertIn("channelLabels", users)
        self.assertIn('value="${path}"', users)
        self.assertNotIn("> ch${i}", users)

        share = SHARE.read_text(encoding="utf-8")
        self.assertIn("paintShareChecks", share)
        self.assertIn("channelLabel", share)
        self.assertIn("channelLabels", share)

    def test_metrics_and_services_use_helpers(self) -> None:
        metrics = METRICS.read_text(encoding="utf-8")
        self.assertIn("loadChannelAliases", metrics)
        self.assertIn("chLabel(", metrics)
        self.assertIn("chTitle(", metrics)

        services = SERVICES.read_text(encoding="utf-8")
        self.assertIn("loadChannelAliases", services)
        self.assertIn("unitLabel", services)
        self.assertIn("dataset.unit", services)

    def test_portal_watch_receives_alias(self) -> None:
        catalog = CATALOG.read_text(encoding="utf-8")
        self.assertIn("&alias=", catalog)
        watch = WATCH.read_text(encoding="utf-8")
        self.assertIn('params.get("alias")', watch)
        self.assertIn("alias || channel", watch)

    def test_settings_keeps_path_identity(self) -> None:
        html = CHANNELS.read_text(encoding="utf-8")
        self.assertIn("CHANNEL_PATH", html)
        self.assertIn("CHANNEL_ALIAS", html)
        self.assertIn("Path", html)


@unittest.skipUnless(NODE and UI.is_file(), "node CLI missing")
class TestChannelLabelJs(unittest.TestCase):
    def test_base_label_title_unit(self) -> None:
        harness = r"""
const fs = require("fs");
const document = {
  documentElement: {
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    hasAttribute() { return false; },
    getAttribute() { return null; },
    setAttribute() {},
  },
  getElementById() { return null; },
  addEventListener() {},
  readyState: "complete",
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return { id: "", textContent: "", style: {}, setAttribute() {} }; },
  head: { appendChild() {} },
};
const localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const window = { localStorage, document, dispatchEvent() {}, addEventListener() {} };
global.window = window;
global.document = document;
global.localStorage = localStorage;
global.CustomEvent = class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
eval(fs.readFileSync(process.argv[2], "utf8"));
const UI = window.NexVueUI;
if (!UI) throw new Error("NexVueUI missing");
if (UI.channelBase("ch0lo") !== "ch0") throw new Error("base lo");
if (UI.channelBase("3") !== "ch3") throw new Error("base digit");
if (UI.channelBase("ch2") !== "ch2") throw new Error("base path");
const aliases = { ch0: "TVU 35", "0": "TVU 35", ch1: "ch1" };
if (UI.channelLabel("ch0", aliases) !== "TVU 35") throw new Error("label alias");
if (UI.channelLabel("ch0lo", aliases) !== "TVU 35 LO") throw new Error("label lo");
if (UI.channelLabel("ch1", aliases) !== "ch1") throw new Error("label empty alias");
if (UI.channelLabel("ch2", aliases) !== "ch2") throw new Error("label missing");
if (UI.channelTitle("ch0", aliases) !== "TVU 35 (ch0)") throw new Error("title");
if (UI.channelTitle("ch1", aliases) !== "ch1") throw new Error("title path");
if (UI.channelLabels(null, aliases) !== "all") throw new Error("labels all");
if (UI.channelLabels([], aliases) !== "—") throw new Error("labels empty");
if (UI.channelLabels(["ch0", "ch1"], aliases) !== "TVU 35, ch1") throw new Error("labels join");
if (UI.unitLabel("nexvue-encode@0", aliases) !== "nexvue-encode@0 (TVU 35)") throw new Error("unit alias");
if (UI.unitLabel("nexvue-encode@1", aliases) !== "nexvue-encode@1") throw new Error("unit path");
if (UI.unitLabel("mediamtx", aliases) !== "mediamtx") throw new Error("unit other");
UI.setChannelAliases(aliases);
if (UI.channelLabel("ch0") !== "TVU 35") throw new Error("cache");
console.log("ok");
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
