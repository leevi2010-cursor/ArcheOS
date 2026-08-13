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

    def test_nested_core_data_is_ignored_but_governance_is_tracked(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        private_paths = (
            "04_core/archeos.sqlite3",
            "04_core/nested/private.sqlite3",
        )

        for private_path in private_paths:
            result = subprocess.run(
                ["git", "check-ignore", "--quiet", "--no-index", private_path],
                cwd=repository,
                check=False,
            )
            self.assertEqual(result.returncode, 0, private_path)

        governance = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "04_core/AGENTS.md",
            ],
            cwd=repository,
            check=False,
        )
        self.assertEqual(governance.returncode, 1)

    def test_atomic_information_is_ignored_but_governance_is_tracked(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        private_paths = (
            "03_information/atomic_information.jsonl",
            "03_information/change_proposals.jsonl",
            "03_information/change_journal.jsonl",
            "03_information/nested/private-information.jsonl",
        )

        for private_path in private_paths:
            result = subprocess.run(
                ["git", "check-ignore", "--quiet", "--no-index", private_path],
                cwd=repository,
                check=False,
            )
            self.assertEqual(result.returncode, 0, private_path)

        governance = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "03_information/AGENTS.md",
            ],
            cwd=repository,
            check=False,
        )
        self.assertEqual(governance.returncode, 1)

        obsolete = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "03_notes/notes.jsonl",
            ],
            cwd=repository,
            check=False,
        )
        self.assertEqual(obsolete.returncode, 1)

    def test_normalized_representations_are_ignored(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        representation = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "02_processing/representations/src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
                "repr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
                "artifacts/private.json",
            ],
            cwd=repository,
            check=False,
        )
        self.assertEqual(representation.returncode, 0)


if __name__ == "__main__":
    unittest.main()
