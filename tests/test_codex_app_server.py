from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Self

import archeos.codex_app_server as adapter
from archeos.codex_app_server import CodexAnalysisProvider
from archeos.transcription import Transcript, TranscriptSegment


def transcript() -> Transcript:
    return Transcript(
        text="决定验证流程。旧方案版本不明确。",
        segments=(
            TranscriptSegment("决定验证流程。", 0, 2, "Speaker_1"),
            TranscriptSegment("旧方案版本不明确。", 2, 4, "Speaker_2"),
        ),
        engine="test",
    )


def analysis_payload() -> dict[str, object]:
    return {
        "meeting_summary": {
            "topic": "流程验证",
            "participants": ["Speaker_1", "Speaker_2"],
            "discussion_goal": "验证处理流程。",
            "main_discussion": ["讨论了流程和旧方案。"],
            "key_viewpoints": [],
            "agreements": ["先验证流程。"],
            "disagreements": [],
            "unresolved_questions": ["旧方案指哪个版本？"],
            "next_actions": [],
        },
        "atomic_information_candidates": [
            {
                "statement": "Speaker_1 决定验证流程。",
                "semantic_type": "decision",
                "concerns": ["Speaker_1", "流程"],
                "evidence_segments": [1],
                "context": "讨论流程验证。",
                "confidence": 0.9,
            }
        ],
        "residue": [
            {
                "evidence_segments": [2],
                "reason_not_absorbed": "旧方案的版本指代不明确。",
                "future_value_or_uncertainty": "确认版本后可能形成原子信息。",
            }
        ],
    }


class FakeThread:
    def __init__(self, response: str | None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.run_kwargs: dict[str, object] | None = None

    def run(self, prompt: str, **kwargs: object) -> object:
        if self.error:
            raise self.error
        self.run_kwargs = {"prompt": prompt, **kwargs}
        return SimpleNamespace(final_response=self.response)


class FakeCodex:
    instance: FakeCodex
    thread = FakeThread(json.dumps(analysis_payload()))

    def __init__(self) -> None:
        self.thread_kwargs: dict[str, object] | None = None
        FakeCodex.instance = self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def thread_start(self, **kwargs: object) -> FakeThread:
        self.thread_kwargs = kwargs
        return self.thread


def sdk_loader() -> tuple[type[object], object, object]:
    return FakeCodex, "deny-all", "read-only"


class CodexAnalysisProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeCodex.thread = FakeThread(json.dumps(analysis_payload()))

    def test_uses_official_sdk_surface_with_required_boundaries(self) -> None:
        result = CodexAnalysisProvider(sdk_loader=sdk_loader).analyze(transcript())

        self.assertEqual(
            result.atomic_information_candidates[0].evidence_segments, (1,)
        )
        self.assertEqual(result.residue[0].evidence_segments, (2,))
        thread_kwargs = FakeCodex.instance.thread_kwargs
        self.assertIsNotNone(thread_kwargs)
        assert thread_kwargs is not None
        self.assertEqual(thread_kwargs["approval_mode"], "deny-all")
        self.assertEqual(thread_kwargs["sandbox"], "read-only")
        self.assertTrue(thread_kwargs["ephemeral"])
        self.assertIn("cwd", thread_kwargs)
        self.assertIn("developer_instructions", thread_kwargs)
        self.assertNotIn("model", thread_kwargs)

        run_kwargs = FakeCodex.thread.run_kwargs
        self.assertIsNotNone(run_kwargs)
        assert run_kwargs is not None
        self.assertEqual(run_kwargs["sandbox"], "read-only")
        self.assertIn("output_schema", run_kwargs)
        self.assertNotIn("model", run_kwargs)
        prompt = run_kwargs["prompt"]
        self.assertIsInstance(prompt, str)
        self.assertIn("Every non-empty transcript segment", prompt)
        self.assertIn("filler, context, or noise", prompt)

    def test_surfaces_sdk_failure_without_retry(self) -> None:
        FakeCodex.thread = FakeThread(None, RuntimeError("runtime unavailable"))
        with self.assertRaisesRegex(RuntimeError, "Codex app-server analysis failed"):
            CodexAnalysisProvider(sdk_loader=sdk_loader).analyze(transcript())

    def test_does_not_repair_invalid_structured_output(self) -> None:
        FakeCodex.thread = FakeThread("not json")
        with self.assertRaisesRegex(RuntimeError, "invalid structured output"):
            CodexAnalysisProvider(sdk_loader=sdk_loader).analyze(transcript())

    def test_no_custom_json_rpc_transport_remains(self) -> None:
        self.assertFalse(hasattr(adapter, "AppServerTransport"))
        self.assertFalse(hasattr(adapter, "app_server_command"))


if __name__ == "__main__":
    unittest.main()
