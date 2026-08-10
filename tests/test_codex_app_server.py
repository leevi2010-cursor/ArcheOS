from __future__ import annotations

import io
import json
import unittest

from archeos.codex_app_server import (
    AppServerTransport,
    CodexAppServerAnalysisProvider,
    app_server_command,
)
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
        "atomic_notes": [
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


def protocol_lines(*, completed_status: str = "completed", output: str | None = None) -> str:
    messages = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "thread-1"}}},
        {"id": 3, "result": {"turn": {"id": "turn-1"}}},
    ]
    if output is not None:
        messages.append(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "text": output},
                },
            }
        )
    messages.append(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": completed_status},
            },
        }
    )
    return "".join(json.dumps(message) + "\n" for message in messages)


class CodexAppServerProviderTest(unittest.TestCase):
    def test_uses_app_server_not_codex_exec(self) -> None:
        self.assertEqual(
            app_server_command("codex"),
            ["codex", "app-server", "--stdio"],
        )

    def test_requests_runtime_structured_output(self) -> None:
        writer = io.StringIO()
        transport = AppServerTransport(
            io.StringIO(protocol_lines(output=json.dumps(analysis_payload()))),
            writer,
        )
        result = CodexAppServerAnalysisProvider(
            transport_factory=lambda: transport
        ).analyze(transcript())

        self.assertEqual(result.atomic_notes[0].evidence_segments, (1,))
        self.assertEqual(result.residue[0].evidence_segments, (2,))
        sent = [json.loads(line) for line in writer.getvalue().splitlines()]
        self.assertEqual(
            [message["method"] for message in sent],
            ["initialize", "initialized", "thread/start", "turn/start"],
        )
        thread_params = sent[2]["params"]
        self.assertNotIn("model", thread_params)
        self.assertTrue(thread_params["ephemeral"])
        self.assertEqual(thread_params["sandbox"], "read-only")
        self.assertIn("cwd", thread_params)
        turn_params = sent[3]["params"]
        self.assertIn("outputSchema", turn_params)
        self.assertNotIn("model", turn_params)

    def test_surfaces_runtime_failure_without_retry(self) -> None:
        writer = io.StringIO()
        transport = AppServerTransport(
            io.StringIO(protocol_lines(completed_status="failed")),
            writer,
        )
        provider = CodexAppServerAnalysisProvider(transport_factory=lambda: transport)
        with self.assertRaisesRegex(RuntimeError, "turn failed"):
            provider.analyze(transcript())
        self.assertEqual(len(writer.getvalue().splitlines()), 4)

    def test_does_not_repair_invalid_structured_output(self) -> None:
        transport = AppServerTransport(
            io.StringIO(protocol_lines(output="not json")),
            io.StringIO(),
        )
        provider = CodexAppServerAnalysisProvider(transport_factory=lambda: transport)
        with self.assertRaisesRegex(RuntimeError, "invalid structured output"):
            provider.analyze(transcript())


if __name__ == "__main__":
    unittest.main()
