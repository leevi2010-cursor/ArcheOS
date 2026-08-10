from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class InboxPrivacyTest(unittest.TestCase):
    def test_nested_inbox_content_is_ignored_but_governance_is_tracked(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        private = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "01_inbox/nested/private/meeting.pdf",
            ],
            cwd=repository,
            check=False,
        )
        governance = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "01_inbox/AGENTS.md",
            ],
            cwd=repository,
            check=False,
        )

        self.assertEqual(private.returncode, 0)
        self.assertEqual(governance.returncode, 1)


if __name__ == "__main__":
    unittest.main()
