from __future__ import annotations

import copy
import unittest

from archeos.analysis import parse_analysis


def valid_payload() -> dict[str, object]:
    return {
        "meeting_summary": {
            "topic": "流程验证",
            "participants": ["Speaker_1"],
            "discussion_goal": "验证通用处理能力。",
            "main_discussion": ["讨论了处理边界。"],
            "key_viewpoints": [],
            "agreements": [],
            "disagreements": [],
            "unresolved_questions": ["证据是否充分？"],
            "next_actions": [],
        },
        "atomic_notes": [
            {
                "statement": "需要验证通用处理能力。",
                "semantic_type": "requirement",
                "concerns": ["通用处理能力"],
                "evidence_segments": [1, 2],
                "context": "讨论处理边界。",
                "confidence": 0.8,
            }
        ],
        "residue": [
            {
                "evidence_segments": [3],
                "reason_not_absorbed": "指代不明确。",
                "future_value_or_uncertainty": "需要补充上下文。",
            }
        ],
    }


class AnalysisSchemaTest(unittest.TestCase):
    def test_accepts_cross_segment_evidence(self) -> None:
        result = parse_analysis(valid_payload(), segment_count=3)
        self.assertEqual(result.atomic_notes[0].evidence_segments, (1, 2))

    def test_rejects_out_of_range_evidence(self) -> None:
        payload = copy.deepcopy(valid_payload())
        payload["atomic_notes"][0]["evidence_segments"] = [4]  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "invalid segment reference"):
            parse_analysis(payload, segment_count=3)

    def test_rejects_duplicate_evidence_references(self) -> None:
        payload = copy.deepcopy(valid_payload())
        payload["atomic_notes"][0]["evidence_segments"] = [1, 1]  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "invalid segment reference"):
            parse_analysis(payload, segment_count=3)

    def test_rejects_empty_concerns(self) -> None:
        payload = copy.deepcopy(valid_payload())
        payload["atomic_notes"][0]["concerns"] = []  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "concerns must not be empty"):
            parse_analysis(payload, segment_count=3)

    def test_rejects_extra_fields(self) -> None:
        payload = valid_payload()
        payload["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "required schema"):
            parse_analysis(payload, segment_count=3)


if __name__ == "__main__":
    unittest.main()
