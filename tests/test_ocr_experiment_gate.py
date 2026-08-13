from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path("docs/experiments/ocr-capabilities/v0.1.0/run_synthetic_ocr_gate.sh")


class OcrExperimentGateTest(unittest.TestCase):
    def test_exit_zero_without_tsv_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            for name, content in {
                "swift": "#!/bin/sh\nprevious=\nlast=\nfor item in \"$@\"; do previous=$last; last=$item; done\ntouch \"$previous\"\n",
                "sips": "#!/bin/sh\nfor item in \"$@\"; do output=$item; done\ntouch \"$output\"\n",
                "tesseract": "#!/bin/sh\nexit 0\n",
            }.items():
                tool = tools / name
                tool.write_text(content, encoding="utf-8")
                tool.chmod(0o755)
            tessdata = root / "tessdata"
            tessdata.mkdir()
            environment = os.environ | {
                "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                "TESSERACT_BIN": str(tools / "tesseract"),
                "TESSDATA_DIR": str(tessdata),
            }
            result = subprocess.run(
                ["sh", str(SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "case=mixed_print exit=0 case_status=failed "
            "tsv_bbox_confidence=unavailable word_rows=0 "
            "reason=missing_or_invalid_tsv",
            result.stdout,
        )
