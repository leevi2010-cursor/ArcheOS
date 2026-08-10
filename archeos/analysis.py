from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .transcription import Transcript


SEMANTIC_TYPES = {
    "observation",
    "preference",
    "requirement",
    "judgment",
    "decision",
    "commitment",
    "action",
    "question",
    "other",
}


@dataclass(frozen=True)
class MeetingSummary:
    topic: str
    participants: tuple[str, ...]
    discussion_goal: str
    main_discussion: tuple[str, ...]
    key_viewpoints: tuple[str, ...]
    agreements: tuple[str, ...]
    disagreements: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    next_actions: tuple[str, ...]


@dataclass(frozen=True)
class AtomicNoteCandidate:
    statement: str
    semantic_type: str
    concerns: tuple[str, ...]
    evidence_segments: tuple[int, ...]
    context: str
    confidence: float


@dataclass(frozen=True)
class ResidueItem:
    evidence_segments: tuple[int, ...]
    reason_not_absorbed: str
    future_value_or_uncertainty: str


@dataclass(frozen=True)
class AnalysisResult:
    meeting_summary: MeetingSummary
    atomic_notes: tuple[AtomicNoteCandidate, ...]
    residue: tuple[ResidueItem, ...]


class AnalysisProvider(Protocol):
    name: str

    def analyze(self, transcript: Transcript) -> AnalysisResult: ...


def analysis_schema() -> dict[str, object]:
    string_array = {"type": "array", "items": {"type": "string"}}
    evidence_segments = {
        "type": "array",
        "items": {"type": "integer", "minimum": 1},
        "minItems": 1,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["meeting_summary", "atomic_notes", "residue"],
        "properties": {
            "meeting_summary": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "topic",
                    "participants",
                    "discussion_goal",
                    "main_discussion",
                    "key_viewpoints",
                    "agreements",
                    "disagreements",
                    "unresolved_questions",
                    "next_actions",
                ],
                "properties": {
                    "topic": {"type": "string"},
                    "participants": string_array,
                    "discussion_goal": {"type": "string"},
                    "main_discussion": string_array,
                    "key_viewpoints": string_array,
                    "agreements": string_array,
                    "disagreements": string_array,
                    "unresolved_questions": string_array,
                    "next_actions": string_array,
                },
            },
            "atomic_notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "statement",
                        "semantic_type",
                        "concerns",
                        "evidence_segments",
                        "context",
                        "confidence",
                    ],
                    "properties": {
                        "statement": {"type": "string"},
                        "semantic_type": {
                            "type": "string",
                            "enum": sorted(SEMANTIC_TYPES),
                        },
                        "concerns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "evidence_segments": evidence_segments,
                        "context": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
            "residue": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "evidence_segments",
                        "reason_not_absorbed",
                        "future_value_or_uncertainty",
                    ],
                    "properties": {
                        "evidence_segments": evidence_segments,
                        "reason_not_absorbed": {"type": "string"},
                        "future_value_or_uncertainty": {"type": "string"},
                    },
                },
            },
        },
    }


def _non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"analysis field {field} must be a non-empty string")
    return value.strip()


def _require_keys(value: dict[str, object], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise ValueError(f"analysis field {field} does not match the required schema")


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"analysis field {field} must be an array")
    return tuple(_non_empty(item, field) for item in value)


def _segment_refs(value: object, field: str, segment_count: int) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"analysis field {field} must contain segment references")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        raise ValueError(f"analysis field {field} contains a non-integer segment reference")
    refs = tuple(value)
    if len(set(refs)) != len(refs) or any(ref < 1 or ref > segment_count for ref in refs):
        raise ValueError(f"analysis field {field} contains an invalid segment reference")
    return refs


def parse_analysis(payload: object, *, segment_count: int) -> AnalysisResult:
    if not isinstance(payload, dict):
        raise ValueError("analysis result must be an object")
    _require_keys(payload, {"meeting_summary", "atomic_notes", "residue"}, "root")
    summary_data = payload.get("meeting_summary")
    if not isinstance(summary_data, dict):
        raise ValueError("analysis result must contain meeting_summary")
    _require_keys(
        summary_data,
        {
            "topic",
            "participants",
            "discussion_goal",
            "main_discussion",
            "key_viewpoints",
            "agreements",
            "disagreements",
            "unresolved_questions",
            "next_actions",
        },
        "meeting_summary",
    )
    summary = MeetingSummary(
        topic=_non_empty(summary_data.get("topic"), "meeting_summary.topic"),
        participants=_strings(
            summary_data.get("participants"), "meeting_summary.participants"
        ),
        discussion_goal=_non_empty(
            summary_data.get("discussion_goal"), "meeting_summary.discussion_goal"
        ),
        main_discussion=_strings(
            summary_data.get("main_discussion"), "meeting_summary.main_discussion"
        ),
        key_viewpoints=_strings(
            summary_data.get("key_viewpoints"), "meeting_summary.key_viewpoints"
        ),
        agreements=_strings(
            summary_data.get("agreements"), "meeting_summary.agreements"
        ),
        disagreements=_strings(
            summary_data.get("disagreements"), "meeting_summary.disagreements"
        ),
        unresolved_questions=_strings(
            summary_data.get("unresolved_questions"),
            "meeting_summary.unresolved_questions",
        ),
        next_actions=_strings(
            summary_data.get("next_actions"), "meeting_summary.next_actions"
        ),
    )

    raw_notes = payload.get("atomic_notes")
    if not isinstance(raw_notes, list):
        raise ValueError("analysis result atomic_notes must be an array")
    notes: list[AtomicNoteCandidate] = []
    for index, item in enumerate(raw_notes, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"atomic_notes[{index}] must be an object")
        _require_keys(
            item,
            {
                "statement",
                "semantic_type",
                "concerns",
                "evidence_segments",
                "context",
                "confidence",
            },
            f"atomic_notes[{index}]",
        )
        semantic_type = _non_empty(
            item.get("semantic_type"), f"atomic_notes[{index}].semantic_type"
        )
        if semantic_type not in SEMANTIC_TYPES:
            raise ValueError(f"atomic_notes[{index}] has an invalid semantic_type")
        confidence = item.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise ValueError(f"atomic_notes[{index}].confidence must be between 0 and 1")
        concerns = _strings(item.get("concerns"), f"atomic_notes[{index}].concerns")
        if not concerns:
            raise ValueError(f"atomic_notes[{index}].concerns must not be empty")
        notes.append(
            AtomicNoteCandidate(
                statement=_non_empty(
                    item.get("statement"), f"atomic_notes[{index}].statement"
                ),
                semantic_type=semantic_type,
                concerns=concerns,
                evidence_segments=_segment_refs(
                    item.get("evidence_segments"),
                    f"atomic_notes[{index}].evidence_segments",
                    segment_count,
                ),
                context=_non_empty(
                    item.get("context"), f"atomic_notes[{index}].context"
                ),
                confidence=float(confidence),
            )
        )

    raw_residue = payload.get("residue")
    if not isinstance(raw_residue, list):
        raise ValueError("analysis result residue must be an array")
    residue: list[ResidueItem] = []
    for index, item in enumerate(raw_residue, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"residue[{index}] must be an object")
        _require_keys(
            item,
            {
                "evidence_segments",
                "reason_not_absorbed",
                "future_value_or_uncertainty",
            },
            f"residue[{index}]",
        )
        residue.append(
            ResidueItem(
                evidence_segments=_segment_refs(
                    item.get("evidence_segments"),
                    f"residue[{index}].evidence_segments",
                    segment_count,
                ),
                reason_not_absorbed=_non_empty(
                    item.get("reason_not_absorbed"),
                    f"residue[{index}].reason_not_absorbed",
                ),
                future_value_or_uncertainty=_non_empty(
                    item.get("future_value_or_uncertainty"),
                    f"residue[{index}].future_value_or_uncertainty",
                ),
            )
        )
    return AnalysisResult(summary, tuple(notes), tuple(residue))


class FileAnalysisProvider:
    name = "analysis-file"

    def __init__(self, analysis_file: Path) -> None:
        self.analysis_file = analysis_file

    def analyze(self, transcript: Transcript) -> AnalysisResult:
        if not self.analysis_file.is_file():
            raise RuntimeError(f"analysis file not found: {self.analysis_file}")
        try:
            payload = json.loads(self.analysis_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("analysis file is not valid JSON") from exc
        return parse_analysis(payload, segment_count=len(transcript.segments))
