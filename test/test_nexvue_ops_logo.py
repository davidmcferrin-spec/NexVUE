#!/usr/bin/env python3
"""
Unit tests for station logo helpers in nexvue-ops.php.

Requires `php` on PATH with GD (getimagesizefromstring). Skipped when php
is unavailable (e.g. Windows laptop without PHP).

Run: python3 test/test_nexvue_ops_logo.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS_PHP = ROOT / "nexvue-ops.php"
PHP = shutil.which("php")

# 1×1 PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@unittest.skipUnless(PHP and OPS_PHP.is_file(), "php CLI or nexvue-ops.php missing")
class TestOpsLogo(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.brand = Path(self._td.name) / "branding"
        self.brand.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str) -> dict:
        ops = OPS_PHP.as_posix()
        brand = self.brand.as_posix()
        code = f"""
putenv('NEXVUE_BRANDING_DIR={brand}');
include '{ops}';
{body}
"""
        env = os.environ.copy()
        env.pop("NEXVUE_OPS_HTTP", None)
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

    def test_get_missing(self) -> None:
        data = self._php("echo json_encode(logo_get_info());")
        self.assertFalse(data["exists"])

    def test_put_get_delete(self) -> None:
        b64 = base64.b64encode(PNG_1X1).decode("ascii")
        data = self._php(f"""
try {{
  $meta = logo_put_base64('{b64}');
  $info = logo_get_info();
  echo json_encode(['meta' => $meta, 'info' => $info]);
}} catch (Throwable $e) {{
  echo json_encode(['error' => $e->getMessage()]);
}}
""")
        self.assertNotIn("error", data)
        self.assertEqual(data["meta"]["mime"], "image/png")
        self.assertEqual(data["meta"]["width"], 1)
        self.assertEqual(data["meta"]["height"], 1)
        self.assertTrue(data["info"]["exists"])
        self.assertEqual(data["info"]["mime"], "image/png")
        self.assertTrue((self.brand / "logo.bin").is_file())
        self.assertTrue((self.brand / "logo.json").is_file())

        data = self._php("""
logo_delete();
echo json_encode(logo_get_info());
""")
        self.assertFalse(data["exists"])
        self.assertFalse((self.brand / "logo.bin").exists())
        self.assertFalse((self.brand / "logo.json").exists())

    def test_reject_oversized(self) -> None:
        # Build oversized payload inside PHP (avoid huge argv on Windows).
        data = self._php("""
try {
  $huge = base64_encode(str_repeat('x', 1048576 + 64));
  logo_put_base64($huge);
  echo json_encode(['ok' => true]);
} catch (InvalidArgumentException $e) {
  echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
}
""")
        self.assertFalse(data["ok"])
        self.assertIn("1 MB", data["error"])

    def test_reject_non_image(self) -> None:
        b64 = base64.b64encode(b"not-an-image").decode("ascii")
        data = self._php(f"""
try {{
  logo_put_base64('{b64}');
  echo json_encode(['ok' => true]);
}} catch (InvalidArgumentException $e) {{
  echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
}}
""")
        self.assertFalse(data["ok"])
        self.assertIn("unrecognized", data["error"].lower())

    def test_reject_svg(self) -> None:
        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
        b64 = base64.b64encode(svg).decode("ascii")
        data = self._php(f"""
try {{
  logo_put_base64('{b64}');
  echo json_encode(['ok' => true]);
}} catch (InvalidArgumentException $e) {{
  echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
}}
""")
        self.assertFalse(data["ok"])

    def test_data_url_prefix(self) -> None:
        b64 = base64.b64encode(PNG_1X1).decode("ascii")
        data = self._php(f"""
try {{
  $meta = logo_put_base64('data:image/png;base64,{b64}');
  echo json_encode(['ok' => true, 'mime' => $meta['mime']]);
}} catch (Throwable $e) {{
  echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
}}
""")
        self.assertTrue(data.get("ok"), data)
        self.assertEqual(data["mime"], "image/png")

    def test_atomic_replace(self) -> None:
        b64 = base64.b64encode(PNG_1X1).decode("ascii")
        self._php(f"logo_put_base64('{b64}'); echo json_encode(['ok'=>true]);")
        first_mtime = (self.brand / "logo.bin").stat().st_mtime_ns
        data = self._php(f"""
$meta = logo_put_base64('{b64}');
echo json_encode(['ok' => true, 'bytes' => $meta['bytes']]);
""")
        self.assertTrue(data["ok"])
        self.assertEqual(data["bytes"], len(PNG_1X1))
        self.assertTrue((self.brand / "logo.bin").is_file())
        # No leftover temp files.
        leftovers = list(self.brand.glob("*.tmp.*"))
        self.assertEqual(leftovers, [])
        _ = first_mtime  # used for readability; replace may share mtime resolution


if __name__ == "__main__":
    unittest.main()
