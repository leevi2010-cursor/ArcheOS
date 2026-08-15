from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "docs" / "experiments" / "semantic-provider-real" / "v0.1.0" / "run_real_handoff.py"


class RealSemanticProviderExperimentAuditTest(unittest.TestCase):
    def test_retired_harness_cannot_claim_unobserved_privacy_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HARNESS)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        status = json.loads(result.stdout)
        self.assertFalse(status["execution_enabled"])
        self.assertEqual(status["privacy_boundary_passed"], "not_verified")
        self.assertIsNot(status["privacy_boundary_passed"], True)
        self.assertEqual(status["runtime_failure"], "historical_real_harness_disabled")

    def test_retired_harness_has_no_real_data_arguments(self) -> None:
        source = HARNESS.read_text(encoding="utf-8")

        self.assertNotIn("--source-id", source)
        self.assertNotIn("--representation-id", source)
        self.assertNotIn("--representation-root", source)
        self.assertNotIn("subprocess.Popen", source)


if __name__ == "__main__":
    unittest.main()
