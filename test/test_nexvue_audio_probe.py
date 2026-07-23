#!/usr/bin/env python3
"""Unit tests for audio_probe_suggest() in nexvue-ops.php.

Run: python3 test/test_nexvue_audio_probe.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS_PHP = ROOT / "nexvue-ops.php"
PROBE_SH = ROOT / "nexvue-ops-audio-probe.sh"
PHP = shutil.which("php")
BASH = shutil.which("bash") or (
    Path(r"C:\Program Files\Git\bin\bash.exe")
    if Path(r"C:\Program Files\Git\bin\bash.exe").is_file()
    else None
)


def php_suggest(mask: list[int]) -> list[dict]:
    ops = OPS_PHP.resolve().as_posix()
    mask_json = json.dumps(mask)
    code = (
        f"include '{ops}';"
        f"$mask = json_decode('{mask_json}', true);"
        "echo json_encode(audio_probe_suggest($mask));"
    )
    env = os.environ.copy()
    env.pop("NEXVUE_OPS_HTTP", None)
    out = subprocess.check_output([PHP, "-r", code], env=env, text=True)
    return json.loads(out)


@unittest.skipUnless(PHP and OPS_PHP.is_file(), "php CLI or nexvue-ops.php missing")
class TestAudioProbeSuggest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(php_suggest([]), [])

    def test_stereo_exact(self) -> None:
        s = php_suggest([1, 2])
        self.assertTrue(s)
        self.assertEqual(s[0]["layout"], "stereo")
        self.assertTrue(s[0]["exact"])

    def test_51_exact(self) -> None:
        s = php_suggest([1, 2, 3, 4, 5, 6])
        self.assertEqual(s[0]["layout"], "51")
        self.assertTrue(s[0]["exact"])

    def test_stereo_sap_exact(self) -> None:
        s = php_suggest([1, 2, 7, 8])
        self.assertEqual(s[0]["layout"], "stereo_sap")
        self.assertTrue(s[0]["exact"])

    def test_51_sap_exact(self) -> None:
        s = php_suggest([1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(s[0]["layout"], "51_sap")
        self.assertTrue(s[0]["exact"])

    def test_51_preferred_over_stereo_when_surround_hot(self) -> None:
        s = php_suggest([1, 2, 3, 4, 5, 6])
        layouts = [x["layout"] for x in s]
        self.assertEqual(layouts[0], "51")
        self.assertIn("stereo", layouts)


@unittest.skipUnless(BASH and PROBE_SH.is_file(), "bash or probe wrapper missing")
class TestAudioProbeWrapper(unittest.TestCase):
    def test_rejects_bad_device(self) -> None:
        r = subprocess.run(
            [str(BASH), str(PROBE_SH), "99"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(r.returncode, 0)

    def test_rejects_non_numeric(self) -> None:
        r = subprocess.run(
            [str(BASH), str(PROBE_SH), "abc"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
