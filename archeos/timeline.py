"""Read-only Stage 1 Object Timeline projection and presentation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


class TimelineError(ValueError):
    """A normal, business-safe Timeline build failure."""


class TimelineProvider(Protocol):
    contract_version: str

    def __call__(self, context: Mapping[str, Any]) -> Mapping[str, Any]: ...


_TIME_BASES = (
    "event_time",
    "claim_time",
    "source_time",
    "processing_time",
    "unknown",
)
_ACCOUNTING_CATEGORIES = (
    "event",
    "current_state",
    "supporting_context",
    "conflict",
    "unknown",
    "not_relevant",
)
_UNKNOWN_KINDS = (
    "time",
    "identity",
    "location",
    "causality",
    "current_state",
    "other",
)
_PROVIDER_FIELDS = {
    "object_id",
    "what_it_is",
    "timeline_entries",
    "current_state",
    "conflicts",
    "unknowns",
    "information_accounting",
    "coverage",
}
_ARTIFACT_FIELDS = _PROVIDER_FIELDS | {
    "selection_label",
    "input_fingerprint",
    "evidence_index",
}
_EVIDENCE_PRESENTATION_FIELDS = {
    "evidence_ref",
    "excerpt",
    "source_id",
    "artifact",
    "speaker",
    "segment",
    "start",
    "end",
    "locator",
}
_ROLE_LABELS = {
    "person": "人物",
    "company": "公司",
    "brand": "品牌",
    "project": "项目",
    "business_line": "业务线",
    "event": "事件",
    "goal": "目标",
    "decision": "决策",
}
_LIFECYCLE_LABELS = {
    "ongoing": "持续经营",
    "bounded": "有明确完成边界",
    "planned": "计划中",
    "active": "进行中",
    "paused": "已暂停",
    "completed": "已完成",
    "cancelled": "已取消",
    "ended": "已结束",
}
_COVERAGE_REASON_LABELS = {
    "relationships_limit": "部分关联对象未纳入本次检查",
    "atomic_information_limit": "部分信息未纳入本次检查",
    "recent_changes_limit": "部分近期变化未纳入本次检查",
    "pending_judgments_limit": "部分待确认事项未纳入本次检查",
    "evidence_limit": "部分信息依据未完整纳入本次检查",
}


def _strict_object(
    properties: Mapping[str, Any], required: Sequence[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


def _nullable_text() -> dict[str, Any]:
    return {"type": ["string", "null"]}


def _reference_properties(*, require_provenance: bool = False) -> dict[str, Any]:
    references = {"type": "array", "items": {"type": "string"}}
    provenance_references = {
        **references,
        **({"minItems": 1} if require_provenance else {}),
    }
    return {
        "object_ids": references,
        "atomic_information_ids": provenance_references,
        "evidence_ids": provenance_references,
    }


def _timeline_schema() -> dict[str, Any]:
    """Return the complete strict schema supplied to the Codex SDK."""
    required_references = _reference_properties(require_provenance=True)
    what_it_is = _strict_object(
        {
            "summary": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
            "lifecycle": _nullable_text(),
            **required_references,
        },
        ("summary", "roles", "lifecycle", *required_references),
    )
    participant = _strict_object(
        {"name": {"type": "string"}, "object_id": _nullable_text()},
        ("name", "object_id"),
    )
    timeline_entry = _strict_object(
        {
            "event": {"type": "string"},
            "time": _nullable_text(),
            "time_end": _nullable_text(),
            "time_basis": {"type": "string", "enum": list(_TIME_BASES)},
            "time_basis_detail": {"type": "string"},
            "participants": {"type": "array", "items": participant},
            "location": _nullable_text(),
            "state_change": _nullable_text(),
            "uncertainty": _nullable_text(),
            **required_references,
        },
        (
            "event",
            "time",
            "time_end",
            "time_basis",
            "time_basis_detail",
            "participants",
            "location",
            "state_change",
            "uncertainty",
            *required_references,
        ),
    )
    current_state = _strict_object(
        {
            "state": {"type": "string"},
            "as_of": _nullable_text(),
            "uncertainty": _nullable_text(),
            **required_references,
        },
        ("state", "as_of", "uncertainty", *required_references),
    )
    conflict = _strict_object(
        {
            "summary": {"type": "string"},
            "unresolved": {"type": "boolean"},
            **required_references,
        },
        ("summary", "unresolved", *required_references),
    )
    optional_references = _reference_properties()
    unknown = _strict_object(
        {
            "question": {"type": "string"},
            "kind": {"type": "string", "enum": list(_UNKNOWN_KINDS)},
            **optional_references,
        },
        ("question", "kind", *optional_references),
    )
    accounting = _strict_object(
        {
            "atomic_information_id": {"type": "string"},
            "category": {
                "type": "string",
                "enum": list(_ACCOUNTING_CATEGORIES),
            },
        },
        ("atomic_information_id", "category"),
    )
    coverage = _strict_object(
        {
            "complete": {"type": "boolean"},
            "covered_atomic_information_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "incomplete_reasons": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        ("complete", "covered_atomic_information_ids", "incomplete_reasons"),
    )
    return _strict_object(
        {
            "object_id": {"type": "string"},
            "what_it_is": what_it_is,
            "timeline_entries": {"type": "array", "items": timeline_entry},
            "current_state": current_state,
            "conflicts": {"type": "array", "items": conflict},
            "unknowns": {"type": "array", "items": unknown},
            "information_accounting": {"type": "array", "items": accounting},
            "coverage": coverage,
        },
        tuple(_PROVIDER_FIELDS),
    )


class CodexTimelineProvider:
    """One-shot read-only Codex SDK adapter for one bounded Object context."""

    contract_version = "stage1-object-timeline-provider.v2"

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], tuple[type[object], object, object]],
    ) -> None:
        self.sdk_loader = sdk_loader

    def __call__(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        codex_type, deny_all, read_only = self.sdk_loader()
        prompt = (
            "Build exactly one complete Object Timeline JSON package. Follow the "
            "output schema exactly. Use only the supplied bounded Object, Atomic "
            "Information, and Evidence view references. Never invent IDs, dates, "
            "participants, locations, state changes, or certainty. Use event_time "
            "only for a demonstrated event occurrence time; for claim/source/processing "
            "time set time and time_end to null and retain the applicable time_basis. "
            "When no actual event time value is available, never use event_time; use "
            "unknown with both time and time_end set to null. "
            "The Object explanation, every event, the current state, and every "
            "conflict must each cite at least one supplied Atomic Information and "
            "its corresponding Evidence. Atomic Information states what the source "
            "expressed; an Event states what happened in the business, so never map "
            "them one-to-one by default. First consolidate questions, answers, "
            "confirmations, details, and state changes that concern the same "
            "participants, business matter or transaction, and compatible time "
            "context into one Event citing multiple Atomic Information and Evidence "
            "items where appropriate. Keep genuinely different matters, participant "
            "groups, incompatible time windows, state changes, or conflicts separate. "
            "When at least 8 Atomic Information items are supplied, return no more "
            "than 7 Events, prefer 2-6 genuinely independent Events without padding, "
            "return fewer Events than supplied items, and make at least one Event "
            "cite multiple supplied Atomic Information items. "
            "Account for every supplied Atomic Information ID exactly once. An "
            "unresolved conflict must remain explicit in current_state. If bounded "
            "input coverage is incomplete, set coverage.complete=false and give "
            "incomplete_reasons. Write every human-facing text field in concise "
            "Chinese business language. Do not expose implementation terms, schema "
            "details, or internal IDs in human-facing text. Do not call tools or "
            "mutate data.\n"
            "BOUNDED_CONTEXT_JSON:\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
        )
        try:
            with tempfile.TemporaryDirectory(prefix="archeos-timeline-") as directory:
                with codex_type() as codex:  # type: ignore[attr-defined]
                    thread = codex.thread_start(
                        approval_mode=deny_all,
                        cwd=directory,
                        developer_instructions=(
                            "Read-only. Return only schema-compliant structured JSON. "
                            "All human-facing text must use concise Chinese business "
                            "language without implementation terminology."
                        ),
                        ephemeral=True,
                        sandbox=read_only,
                    )
                    result = thread.run(
                        prompt,
                        output_schema=_timeline_schema(),
                        sandbox=read_only,
                    )
        except Exception as exc:
            raise TimelineError(f"Codex timeline provider failed: {exc}") from exc
        response = getattr(result, "final_response", None)
        if not isinstance(response, str):
            raise TimelineError("Codex timeline provider returned no structured result")
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise TimelineError(
                "Codex timeline provider returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise TimelineError("Codex timeline provider result must be a JSON object")
        return payload


@dataclass(frozen=True)
class Selection:
    object_id: str
    label: str
    supplemental_atomic_information_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacyRecoveryAuthorization:
    """Exact local authority binding for one legacy Timeline artifact."""

    authority_ref: str
    object_id: str
    legacy_artifact_sha256: str
    legacy_input_fingerprint: str
    stable_input_fingerprint: str


def load_selection(path: Path) -> tuple[Selection, ...]:
    """Load the private, minimal 3-5 Object selection contract."""
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise TimelineError("selection file must have private mode 0600")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TimelineError("selection file must contain valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"objects"}:
        raise TimelineError("selection file must contain only an objects array")
    entries = raw["objects"]
    if not isinstance(entries, list) or not 3 <= len(entries) <= 5:
        raise TimelineError("selection must contain exactly 3-5 objects")
    result: list[Selection] = []
    seen: set[str] = set()
    allowed_fields = {
        "object_id",
        "label",
        "supplemental_atomic_information_ids",
    }
    for index, item in enumerate(entries):
        if not isinstance(item, dict) or not {"object_id", "label"} <= set(item):
            raise TimelineError(f"objects[{index}] must identify an Object and label")
        if not set(item) <= allowed_fields:
            raise TimelineError(f"objects[{index}] contains unsupported fields")
        object_id = _non_empty_text(item["object_id"], f"objects[{index}].object_id")
        label = _non_empty_text(item["label"], f"objects[{index}].label")
        if object_id in seen:
            raise TimelineError(f"objects[{index}].object_id must be unique")
        supplemental = item.get("supplemental_atomic_information_ids", [])
        if not isinstance(supplemental, list):
            raise TimelineError(
                f"objects[{index}].supplemental_atomic_information_ids must be an array"
            )
        supplemental_ids = tuple(
            _non_empty_text(value, "supplemental Atomic Information ID")
            for value in supplemental
        )
        if len(set(supplemental_ids)) != len(supplemental_ids):
            raise TimelineError(
                f"objects[{index}] contains duplicate supplemental Atomic Information IDs"
            )
        seen.add(object_id)
        result.append(Selection(object_id, label, supplemental_ids))
    return tuple(result)


def _fingerprint(
    selection: Selection,
    context: Mapping[str, Any],
    contract_version: str,
) -> str:
    semantic_context = dict(context)
    raw_bundle = semantic_context.get("context")
    if isinstance(raw_bundle, Mapping):
        bundle = dict(raw_bundle)
        raw_metadata = bundle.get("metadata")
        if isinstance(raw_metadata, Mapping):
            metadata = dict(raw_metadata)
            metadata.pop("generated_at", None)
            bundle["metadata"] = metadata
        semantic_context["context"] = bundle
    payload = json.dumps(
        {
            "selection": {
                "object_id": selection.object_id,
                "label": selection.label,
                "supplemental_atomic_information_ids": list(
                    selection.supplemental_atomic_information_ids
                ),
            },
            "context": semantic_context,
            "provider_contract": contract_version,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def evidence_view_refs(
    atomic_information_id: str,
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Create stable Derived Artifact references, never new Core Evidence IDs."""
    return {
        f"evidence-view:{atomic_information_id}:{index:04d}": {
            "atomic_information_id": atomic_information_id,
            "evidence": dict(item),
        }
        for index, item in enumerate(evidence, start=1)
    }


def _non_empty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TimelineError(f"{field} must be non-empty text")
    return value


def _nullable_text_value(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _non_empty_text(value, field)


def _strict_fields(
    value: object,
    field: str,
    expected: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise TimelineError(f"{field} does not match the Timeline contract")
    return value


def _reference_list(
    value: object,
    field: str,
    allowed: set[str],
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TimelineError(f"{field} must be an array")
    refs = tuple(_non_empty_text(item, field) for item in value)
    if required and not refs:
        raise TimelineError(f"{field} must not be empty")
    if len(set(refs)) != len(refs):
        raise TimelineError(f"{field} must not contain duplicates")
    unknown = set(refs) - allowed
    if unknown:
        raise TimelineError(f"{field} references unavailable IDs: {sorted(unknown)}")
    return refs


def _validate_references(
    value: Mapping[str, Any],
    field: str,
    *,
    allowed_object_ids: set[str],
    allowed_atomic_ids: set[str],
    allowed_evidence_ids: set[str],
    evidence_by_atomic: Mapping[str, set[str]],
    require_atomic: bool = False,
    require_evidence: bool = False,
) -> None:
    _reference_list(value["object_ids"], f"{field}.object_ids", allowed_object_ids)
    atomic_ids = _reference_list(
        value["atomic_information_ids"],
        f"{field}.atomic_information_ids",
        allowed_atomic_ids,
        required=require_atomic,
    )
    evidence_ids = _reference_list(
        value["evidence_ids"],
        f"{field}.evidence_ids",
        allowed_evidence_ids,
        required=require_evidence,
    )
    if atomic_ids and evidence_ids:
        corresponding = set().union(
            *(evidence_by_atomic.get(atomic_id, set()) for atomic_id in atomic_ids)
        )
        if not set(evidence_ids) <= corresponding:
            raise TimelineError(
                f"{field} cites Evidence outside its Atomic Information"
            )


def _parse_event_time(value: str, field: str) -> datetime:
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TimelineError(f"{field} must be an ISO-8601 event time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cited_evidence_ids(package: Mapping[str, Any]) -> tuple[str, ...]:
    cited: set[str] = set()
    referenced_items = [
        package["what_it_is"],
        *package["timeline_entries"],
        package["current_state"],
        *package["conflicts"],
        *package["unknowns"],
    ]
    for item in referenced_items:
        cited.update(item["evidence_ids"])
    return tuple(sorted(cited))


def _evidence_presentation(
    evidence_ref: str,
    evidence_view_refs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    raw_view = evidence_view_refs.get(evidence_ref)
    view = _strict_fields(
        raw_view,
        f"evidence_view_refs[{evidence_ref}]",
        {"atomic_information_id", "evidence"},
    )
    _non_empty_text(
        view["atomic_information_id"],
        f"evidence_view_refs[{evidence_ref}].atomic_information_id",
    )
    evidence = view["evidence"]
    if not isinstance(evidence, dict):
        raise TimelineError("bounded Context Evidence record is invalid")
    source_id = _non_empty_text(evidence.get("source_id"), "Evidence.source_id")
    artifact = _non_empty_text(evidence.get("artifact"), "Evidence.artifact")
    excerpt = _non_empty_text(evidence.get("excerpt"), "Evidence.excerpt")
    speaker = _nullable_text_value(evidence.get("speaker"), "Evidence.speaker")
    start = _nullable_text_value(evidence.get("start"), "Evidence.start")
    end = _nullable_text_value(evidence.get("end"), "Evidence.end")
    segment = evidence.get("segment")
    if isinstance(segment, bool) or not isinstance(segment, int) or segment < 0:
        raise TimelineError("Evidence.segment must be a non-negative integer")
    raw_locator = evidence.get("locator")
    locator = (
        _non_empty_text(raw_locator, "Evidence.locator")
        if raw_locator is not None
        else f"segment:{segment}"
    )
    return {
        "evidence_ref": evidence_ref,
        "excerpt": excerpt,
        "source_id": source_id,
        "artifact": artifact,
        "speaker": speaker,
        "segment": segment,
        "start": start,
        "end": end,
        "locator": locator,
    }


def _build_evidence_index(
    package: Mapping[str, Any],
    evidence_view_refs: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _evidence_presentation(evidence_ref, evidence_view_refs)
        for evidence_ref in _cited_evidence_ids(package)
    ]


def _normalize_provider_result(package: Mapping[str, Any]) -> dict[str, Any]:
    """Conservatively normalize one contradictory missing-event-time label."""
    normalized = deepcopy(dict(package))
    entries = normalized.get("timeline_entries")
    if not isinstance(entries, list):
        return normalized
    for entry in entries:
        if (
            isinstance(entry, dict)
            and entry.get("time_basis") == "event_time"
            and "time" in entry
            and entry["time"] is None
            and "time_end" in entry
            and entry["time_end"] is None
        ):
            entry["time_basis"] = "unknown"
    return normalized


def validate_package(
    package: Mapping[str, Any],
    supplied_ids: set[str],
    evidence_ids: set[str] | None = None,
    expected_object_id: str | None = None,
    *,
    allowed_object_ids: set[str] | None = None,
    evidence_by_atomic: Mapping[str, set[str]] | None = None,
    required_incomplete_reasons: set[str] | None = None,
    evidence_view_refs: Mapping[str, Mapping[str, Any]] | None = None,
    artifact: bool | None = None,
) -> dict[str, Any]:
    """Deterministically validate provider output before any result is persisted."""
    fields = set(package)
    expected_fields = (
        _ARTIFACT_FIELDS
        if artifact is True
        else _PROVIDER_FIELDS
        if artifact is False
        else None
    )
    if expected_fields is not None and fields != expected_fields:
        raise TimelineError(
            "provider result does not match the Timeline package fields"
        )
    if expected_fields is None and fields not in (_PROVIDER_FIELDS, _ARTIFACT_FIELDS):
        raise TimelineError(
            "provider result does not match the Timeline package fields"
        )
    object_id = _non_empty_text(package["object_id"], "object_id")
    if expected_object_id is not None and object_id != expected_object_id:
        raise TimelineError("provider result Object ID does not match selection")

    allowed_objects = set(allowed_object_ids or {object_id})
    allowed_evidence = set(evidence_ids or set())
    by_atomic = evidence_by_atomic or {}
    reference_fields = {
        "object_ids",
        "atomic_information_ids",
        "evidence_ids",
    }

    what_it_is = _strict_fields(
        package["what_it_is"],
        "what_it_is",
        {"summary", "roles", "lifecycle", *reference_fields},
    )
    _non_empty_text(what_it_is["summary"], "what_it_is.summary")
    if not isinstance(what_it_is["roles"], list) or any(
        not isinstance(role, str) or not role.strip() for role in what_it_is["roles"]
    ):
        raise TimelineError("what_it_is.roles must be an array of non-empty text")
    _nullable_text_value(what_it_is["lifecycle"], "what_it_is.lifecycle")
    _validate_references(
        what_it_is,
        "what_it_is",
        allowed_object_ids=allowed_objects,
        allowed_atomic_ids=supplied_ids,
        allowed_evidence_ids=allowed_evidence,
        evidence_by_atomic=by_atomic,
        require_atomic=True,
        require_evidence=True,
    )

    raw_entries = package["timeline_entries"]
    if not isinstance(raw_entries, list):
        raise TimelineError("timeline_entries must be an array")
    entry_fields = {
        "event",
        "time",
        "time_end",
        "time_basis",
        "time_basis_detail",
        "participants",
        "location",
        "state_change",
        "uncertainty",
        *reference_fields,
    }
    for index, raw_entry in enumerate(raw_entries):
        field = f"timeline_entries[{index}]"
        entry = _strict_fields(raw_entry, field, entry_fields)
        _non_empty_text(entry["event"], f"{field}.event")
        basis = entry["time_basis"]
        if basis not in _TIME_BASES:
            raise TimelineError(f"{field}.time_basis is invalid")
        _non_empty_text(entry["time_basis_detail"], f"{field}.time_basis_detail")
        event_time = _nullable_text_value(entry["time"], f"{field}.time")
        event_time_end = _nullable_text_value(entry["time_end"], f"{field}.time_end")
        if basis == "event_time":
            if event_time is None:
                raise TimelineError(f"{field} with event_time requires time")
            start = _parse_event_time(event_time, f"{field}.time")
            if event_time_end is not None:
                end = _parse_event_time(event_time_end, f"{field}.time_end")
                if end < start:
                    raise TimelineError(f"{field}.time_end must not precede time")
        elif event_time is not None or event_time_end is not None:
            raise TimelineError(
                f"{field} may not present claim/source/processing time as event time"
            )
        participants = entry["participants"]
        if not isinstance(participants, list):
            raise TimelineError(f"{field}.participants must be an array")
        for participant_index, raw_participant in enumerate(participants):
            participant_field = f"{field}.participants[{participant_index}]"
            participant = _strict_fields(
                raw_participant,
                participant_field,
                {"name", "object_id"},
            )
            _non_empty_text(participant["name"], f"{participant_field}.name")
            participant_id = _nullable_text_value(
                participant["object_id"], f"{participant_field}.object_id"
            )
            if participant_id is not None and participant_id not in allowed_objects:
                raise TimelineError(
                    f"{participant_field}.object_id references an unavailable Object"
                )
        _nullable_text_value(entry["location"], f"{field}.location")
        _nullable_text_value(entry["state_change"], f"{field}.state_change")
        _nullable_text_value(entry["uncertainty"], f"{field}.uncertainty")
        _validate_references(
            entry,
            field,
            allowed_object_ids=allowed_objects,
            allowed_atomic_ids=supplied_ids,
            allowed_evidence_ids=allowed_evidence,
            evidence_by_atomic=by_atomic,
            require_atomic=True,
            require_evidence=True,
        )

    if len(supplied_ids) >= 8:
        if len(raw_entries) > 7:
            raise TimelineError(
                "Timeline consolidation requires at most 7 Events for 8+ inputs"
            )
        if len(raw_entries) >= len(supplied_ids):
            raise TimelineError(
                "Timeline consolidation requires fewer Events than supplied inputs"
            )
        if not any(
            len(set(entry["atomic_information_ids"])) >= 2 for entry in raw_entries
        ):
            raise TimelineError(
                "Timeline consolidation requires at least one multi-Information Event"
            )

    current_state = _strict_fields(
        package["current_state"],
        "current_state",
        {"state", "as_of", "uncertainty", *reference_fields},
    )
    _non_empty_text(current_state["state"], "current_state.state")
    _nullable_text_value(current_state["as_of"], "current_state.as_of")
    uncertainty = _nullable_text_value(
        current_state["uncertainty"], "current_state.uncertainty"
    )
    _validate_references(
        current_state,
        "current_state",
        allowed_object_ids=allowed_objects,
        allowed_atomic_ids=supplied_ids,
        allowed_evidence_ids=allowed_evidence,
        evidence_by_atomic=by_atomic,
        require_atomic=True,
        require_evidence=True,
    )

    raw_conflicts = package["conflicts"]
    if not isinstance(raw_conflicts, list):
        raise TimelineError("conflicts must be an array")
    conflict_fields = {"summary", "unresolved", *reference_fields}
    unresolved = False
    for index, raw_conflict in enumerate(raw_conflicts):
        field = f"conflicts[{index}]"
        conflict = _strict_fields(raw_conflict, field, conflict_fields)
        _non_empty_text(conflict["summary"], f"{field}.summary")
        if not isinstance(conflict["unresolved"], bool):
            raise TimelineError(f"{field}.unresolved must be boolean")
        unresolved = unresolved or conflict["unresolved"]
        _validate_references(
            conflict,
            field,
            allowed_object_ids=allowed_objects,
            allowed_atomic_ids=supplied_ids,
            allowed_evidence_ids=allowed_evidence,
            evidence_by_atomic=by_atomic,
            require_atomic=True,
            require_evidence=True,
        )
    if unresolved and uncertainty is None:
        raise TimelineError(
            "unresolved conflicts require explicit current_state uncertainty"
        )

    raw_unknowns = package["unknowns"]
    if not isinstance(raw_unknowns, list):
        raise TimelineError("unknowns must be an array")
    unknown_fields = {"question", "kind", *reference_fields}
    for index, raw_unknown in enumerate(raw_unknowns):
        field = f"unknowns[{index}]"
        unknown = _strict_fields(raw_unknown, field, unknown_fields)
        _non_empty_text(unknown["question"], f"{field}.question")
        if unknown["kind"] not in _UNKNOWN_KINDS:
            raise TimelineError(f"{field}.kind is invalid")
        _validate_references(
            unknown,
            field,
            allowed_object_ids=allowed_objects,
            allowed_atomic_ids=supplied_ids,
            allowed_evidence_ids=allowed_evidence,
            evidence_by_atomic=by_atomic,
        )

    raw_accounting = package["information_accounting"]
    if not isinstance(raw_accounting, list):
        raise TimelineError("information_accounting must be an array")
    accounted: list[str] = []
    for index, raw_item in enumerate(raw_accounting):
        item = _strict_fields(
            raw_item,
            f"information_accounting[{index}]",
            {"atomic_information_id", "category"},
        )
        atomic_id = _non_empty_text(
            item["atomic_information_id"],
            f"information_accounting[{index}].atomic_information_id",
        )
        if atomic_id not in supplied_ids:
            raise TimelineError("information_accounting references unavailable input")
        if item["category"] not in _ACCOUNTING_CATEGORIES:
            raise TimelineError("information_accounting contains an invalid category")
        accounted.append(atomic_id)
    if len(accounted) != len(set(accounted)) or set(accounted) != supplied_ids:
        raise TimelineError(
            "information_accounting must cover every input exactly once"
        )

    coverage = _strict_fields(
        package["coverage"],
        "coverage",
        {"complete", "covered_atomic_information_ids", "incomplete_reasons"},
    )
    if not isinstance(coverage["complete"], bool):
        raise TimelineError("coverage.complete must be boolean")
    covered = _reference_list(
        coverage["covered_atomic_information_ids"],
        "coverage.covered_atomic_information_ids",
        supplied_ids,
    )
    if set(covered) != supplied_ids:
        raise TimelineError("coverage must include every supplied Atomic Information")
    reasons = coverage["incomplete_reasons"]
    if not isinstance(reasons, list):
        raise TimelineError("coverage.incomplete_reasons must be an array")
    reason_set = {
        _non_empty_text(reason, "coverage.incomplete_reasons") for reason in reasons
    }
    if len(reason_set) != len(reasons):
        raise TimelineError("coverage.incomplete_reasons must not contain duplicates")
    required_reasons = set(required_incomplete_reasons or set())
    if coverage["complete"] and (reason_set or required_reasons):
        raise TimelineError(
            "an incomplete bounded Context cannot produce complete coverage"
        )
    if not coverage["complete"] and not reason_set:
        raise TimelineError("incomplete coverage requires incomplete_reasons")
    if not required_reasons <= reason_set:
        raise TimelineError("coverage omits bounded Context incomplete reasons")

    if fields == _ARTIFACT_FIELDS:
        _non_empty_text(package["selection_label"], "selection_label")
        fingerprint = _non_empty_text(package["input_fingerprint"], "input_fingerprint")
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise TimelineError("input_fingerprint must be a SHA-256 digest")
        if evidence_view_refs is None:
            raise TimelineError("artifact Evidence index cannot be verified")
        expected_index = _build_evidence_index(package, evidence_view_refs)
        raw_index = package["evidence_index"]
        if not isinstance(raw_index, list) or any(
            not isinstance(item, dict) or set(item) != _EVIDENCE_PRESENTATION_FIELDS
            for item in raw_index
        ):
            raise TimelineError("artifact Evidence index is malformed")
        if raw_index != expected_index:
            raise TimelineError(
                "artifact Evidence index does not match bounded Context"
            )
    return dict(package)


def _git_ancestor(path: Path) -> Path | None:
    current = path.resolve()
    if not current.exists():
        current = next(
            (parent for parent in current.parents if parent.exists()), current
        )
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_digest(value: object, field: str) -> str:
    digest = _non_empty_text(value, field)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise TimelineError(f"{field} must be a SHA-256 digest")
    return digest


def _serialized_package(package: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def recover_legacy_timeline_artifact(
    artifact_path: Path,
    receipt_path: Path,
    selection: Selection,
    context: Mapping[str, Any],
    contract_version: str,
    authorization: LegacyRecoveryAuthorization,
) -> dict[str, Any]:
    """Rebind one exact validated legacy artifact without calling a Provider."""
    resolved_artifact = artifact_path.expanduser().resolve()
    resolved_receipt = receipt_path.expanduser().resolve()
    if _git_ancestor(resolved_artifact) is not None:
        raise TimelineError("legacy artifact must remain outside Git")
    if _git_ancestor(resolved_receipt) is not None:
        raise TimelineError("recovery receipt must remain outside Git")
    if not resolved_artifact.is_file():
        raise TimelineError("legacy Timeline artifact does not exist")
    if resolved_receipt.exists():
        raise TimelineError("recovery receipt already exists")

    authority_ref = _non_empty_text(
        authorization.authority_ref, "recovery authority_ref"
    )
    authority_object_id = _non_empty_text(
        authorization.object_id, "recovery Object binding"
    )
    if authority_object_id != selection.object_id:
        raise TimelineError(
            "recovery authority Object binding does not match selection"
        )
    expected_legacy_hash = _sha256_digest(
        authorization.legacy_artifact_sha256,
        "recovery legacy artifact hash",
    )
    expected_legacy_fingerprint = _sha256_digest(
        authorization.legacy_input_fingerprint,
        "recovery legacy input fingerprint",
    )
    expected_stable_fingerprint = _sha256_digest(
        authorization.stable_input_fingerprint,
        "recovery stable input fingerprint",
    )

    legacy_bytes = resolved_artifact.read_bytes()
    if _sha256_bytes(legacy_bytes) != expected_legacy_hash:
        raise TimelineError("legacy Timeline artifact hash does not match authority")
    stable_fingerprint = _fingerprint(selection, context, contract_version)
    if stable_fingerprint != expected_stable_fingerprint:
        raise TimelineError(
            "current semantic Context does not match recovery authority"
        )
    try:
        legacy_package = json.loads(legacy_bytes)
    except json.JSONDecodeError as exc:
        raise TimelineError("legacy Timeline artifact is not valid JSON") from exc

    supplied = set(context.get("atomic_information_ids", ()))
    supplied.update(selection.supplemental_atomic_information_ids)
    allowed_objects, allowed_evidence, by_atomic, required_reasons = _validation_scope(
        context
    )
    evidence_views = context.get("evidence_view_refs")
    if not isinstance(evidence_views, dict):
        raise TimelineError("current Evidence references are invalid")
    validation_options = {
        "evidence_ids": allowed_evidence,
        "expected_object_id": selection.object_id,
        "allowed_object_ids": allowed_objects,
        "evidence_by_atomic": by_atomic,
        "required_incomplete_reasons": required_reasons,
        "evidence_view_refs": evidence_views,
    }
    validated_legacy = validate_package(
        legacy_package,
        supplied,
        artifact=True,
        **validation_options,
    )
    if validated_legacy["input_fingerprint"] != expected_legacy_fingerprint:
        raise TimelineError(
            "legacy input fingerprint does not match recovery authority"
        )
    if expected_legacy_fingerprint == stable_fingerprint:
        raise TimelineError("Timeline artifact already uses the stable fingerprint")

    recovered_package = dict(validated_legacy)
    recovered_package["input_fingerprint"] = stable_fingerprint
    recovered_bytes = _serialized_package(recovered_package)
    recovered_hash = _sha256_bytes(recovered_bytes)
    receipt = {
        "schema_version": "stage1-timeline-recovery-receipt.v1",
        "authority_ref": authority_ref,
        "object_id": selection.object_id,
        "legacy_artifact_sha256": expected_legacy_hash,
        "recovered_artifact_sha256": recovered_hash,
        "legacy_input_fingerprint": expected_legacy_fingerprint,
        "stable_input_fingerprint": stable_fingerprint,
    }

    resolved_receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(resolved_receipt.parent, 0o700)
    _atomic_write(resolved_artifact, recovered_bytes.decode("utf-8"))
    os.chmod(resolved_artifact, 0o600)
    readback_bytes = resolved_artifact.read_bytes()
    if _sha256_bytes(readback_bytes) != recovered_hash:
        raise TimelineError("recovered Timeline artifact hash readback failed")
    recovered_readback = validate_package(
        json.loads(readback_bytes),
        supplied,
        artifact=True,
        **validation_options,
    )
    if recovered_readback != recovered_package:
        raise TimelineError("recovered Timeline artifact validation readback failed")
    _atomic_write(
        resolved_receipt,
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    os.chmod(resolved_receipt, 0o600)
    if stat.S_IMODE(resolved_receipt.stat().st_mode) != 0o600:
        raise TimelineError("recovery receipt must have private mode 0600")
    if json.loads(resolved_receipt.read_text(encoding="utf-8")) != receipt:
        raise TimelineError("recovery receipt readback failed")
    return receipt


def _validation_scope(
    context: Mapping[str, Any],
) -> tuple[set[str], set[str], dict[str, set[str]], set[str]]:
    allowed_objects = set(context.get("allowed_object_ids", ()))
    evidence_refs = context.get("evidence_view_refs", {})
    if not isinstance(evidence_refs, dict):
        raise TimelineError("bounded Context Evidence references are invalid")
    allowed_evidence = set(evidence_refs)
    raw_by_atomic = context.get("evidence_by_atomic", {})
    if not isinstance(raw_by_atomic, dict):
        raise TimelineError("bounded Context Evidence accounting is invalid")
    by_atomic = {
        str(atomic_id): set(refs)
        for atomic_id, refs in raw_by_atomic.items()
        if isinstance(refs, list)
    }
    required_reasons = set(context.get("required_incomplete_reasons", ()))
    return allowed_objects, allowed_evidence, by_atomic, required_reasons


def build_timelines(
    selections: tuple[Selection, ...],
    contexts: Mapping[str, Mapping[str, Any]],
    provider: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    output_root: Path,
    resume: bool = False,
) -> dict[str, Any]:
    """Build, durably save, read back, and present one package at a time."""
    if not 3 <= len(selections) <= 5:
        raise TimelineError("selection must contain exactly 3-5 objects")
    resolved_output = output_root.expanduser().resolve()
    repository = _git_ancestor(resolved_output)
    if repository is not None:
        raise TimelineError(
            f"output-root must be outside every Git repository or worktree: {repository}"
        )
    resolved_output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(resolved_output, 0o700)

    packages: list[dict[str, Any]] = []
    calls = 0
    contract_version = getattr(provider, "contract_version", "timeline.v1")
    for selection in selections:
        context = contexts.get(selection.object_id)
        if context is None:
            raise TimelineError(f"unknown Object: {selection.object_id}")
        fingerprint = _fingerprint(selection, context, contract_version)
        target = resolved_output / f"{selection.object_id}.json"
        markdown_target = resolved_output / f"{selection.object_id}.md"
        supplied = set(context.get("atomic_information_ids", ()))
        supplied.update(selection.supplemental_atomic_information_ids)
        allowed_objects, allowed_evidence, by_atomic, required_reasons = (
            _validation_scope(context)
        )
        evidence_views = context["evidence_view_refs"]
        validation_options = {
            "evidence_ids": allowed_evidence,
            "expected_object_id": selection.object_id,
            "allowed_object_ids": allowed_objects,
            "evidence_by_atomic": by_atomic,
            "required_incomplete_reasons": required_reasons,
            "evidence_view_refs": evidence_views,
        }

        if resume and target.is_file():
            try:
                saved = json.loads(target.read_text(encoding="utf-8"))
                validated_saved = validate_package(
                    saved,
                    supplied,
                    artifact=True,
                    **validation_options,
                )
                if validated_saved["input_fingerprint"] == fingerprint:
                    _atomic_write(markdown_target, render_markdown(validated_saved))
                    packages.append(validated_saved)
                    continue
            except (
                OSError,
                TimelineError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pass

        provider_payload = {
            **context,
            "selected_object_id": selection.object_id,
            "selection_label": selection.label,
            "supplemental_atomic_information_ids": list(
                selection.supplemental_atomic_information_ids
            ),
        }
        calls += 1
        try:
            provider_result = provider(provider_payload)
        except TimelineError as exc:
            raise TimelineError(
                f"timeline provider failed for Object {selection.object_id}: {exc}"
            ) from exc
        except Exception as exc:
            raise TimelineError(
                f"timeline provider failed for Object {selection.object_id}: {exc}"
            ) from exc
        result = validate_package(
            _normalize_provider_result(provider_result),
            supplied,
            artifact=False,
            **validation_options,
        )
        result.update(
            {
                "selection_label": selection.label,
                "input_fingerprint": fingerprint,
                "evidence_index": _build_evidence_index(result, evidence_views),
            }
        )
        serialized = _serialized_package(result).decode("utf-8")
        _atomic_write(target, serialized)
        readback = json.loads(target.read_text(encoding="utf-8"))
        validated_readback = validate_package(
            readback,
            supplied,
            artifact=True,
            **validation_options,
        )
        if validated_readback != result:
            raise TimelineError(
                "durable Timeline readback does not match validated result"
            )
        _atomic_write(markdown_target, render_markdown(validated_readback))
        packages.append(validated_readback)

    return {
        "schema_version": "stage1-object-timeline.v1",
        "packages": packages,
        "provider_calls": calls,
        "complete": all(package["coverage"]["complete"] for package in packages),
    }


def _display(
    value: object,
    empty: str = "未提供",
    *,
    hidden_ids: set[str] | None = None,
) -> str:
    if value is None:
        return empty
    if isinstance(value, str):
        result = value
        for internal_id in sorted(hidden_ids or set(), key=len, reverse=True):
            result = result.replace(internal_id, "（内部标识已隐藏）")
        result = result.replace("bounded Context", "当前资料范围")
        result = result.replace("Provider", "处理过程")
        result = result.replace("provider", "处理过程")
        result = result.replace("schema", "数据规则")
        result = result.replace("Schema", "数据规则")
        return result
    if isinstance(value, list):
        return (
            "、".join(_display(item, hidden_ids=hidden_ids) for item in value)
            if value
            else empty
        )
    return str(value)


def _internal_reference_ids(package: Mapping[str, Any]) -> set[str]:
    internal_ids = {package["object_id"]}
    referenced_items = [
        package["what_it_is"],
        *package["timeline_entries"],
        package["current_state"],
        *package["conflicts"],
        *package["unknowns"],
    ]
    for item in referenced_items:
        internal_ids.update(item["object_ids"])
        internal_ids.update(item["atomic_information_ids"])
        internal_ids.update(item["evidence_ids"])
        for participant in item.get("participants", []):
            if participant["object_id"] is not None:
                internal_ids.add(participant["object_id"])
    internal_ids.update(
        item["atomic_information_id"] for item in package["information_accounting"]
    )
    internal_ids.update(
        item["source_id"]
        for item in package.get("evidence_index", [])
        if isinstance(item, dict) and isinstance(item.get("source_id"), str)
    )
    return internal_ids


def _business_label(value: object, labels: Mapping[str, str]) -> str:
    if not isinstance(value, str) or not value.strip():
        return "尚未确认"
    clean = value.strip()
    if clean in labels:
        return labels[clean]
    if "_" in clean or clean.isascii() and clean.replace("-", "").isalnum():
        return "尚未确认"
    return clean


def _coverage_reason(value: object, hidden_ids: set[str]) -> str:
    if isinstance(value, str) and value in _COVERAGE_REASON_LABELS:
        return _COVERAGE_REASON_LABELS[value]
    if isinstance(value, str) and (
        "_" in value
        or "provider" in value.lower()
        or "bounded context" in value.lower()
        or "schema" in value.lower()
    ):
        return "部分资料尚未完整覆盖"
    return _display(value, hidden_ids=hidden_ids)


def _evidence_lookup(package: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_index = package.get("evidence_index", [])
    if not isinstance(raw_index, list):
        return {}
    return {
        item["evidence_ref"]: item
        for item in raw_index
        if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
    }


def _evidence_numbers(
    item: Mapping[str, Any],
    numbers: Mapping[str, int],
) -> str:
    cited = [
        numbers[evidence_ref]
        for evidence_ref in item.get("evidence_ids", [])
        if evidence_ref in numbers
    ]
    return "、".join(str(number) for number in cited) if cited else "暂无可展示依据"


def _source_recorded_at(value: object) -> tuple[datetime, str] | None:
    parsed: datetime
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            return None
        if seconds >= 100_000_000_000:
            seconds /= 1000
        try:
            parsed = datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        parsed = parsed.astimezone(timezone.utc)
    else:
        return None
    business_time = parsed.astimezone(timezone(timedelta(hours=8)))
    return parsed, business_time.isoformat(timespec="seconds")


def _locator_presentation(entry: Mapping[str, Any]) -> tuple[str, datetime | None, str]:
    segment = entry.get("segment")
    fallback = (
        f"第 {segment} 条资料" if isinstance(segment, int) else "资料中的对应位置"
    )
    raw_locator = entry.get("locator")
    locator: object = raw_locator
    if isinstance(raw_locator, str):
        try:
            locator = json.loads(raw_locator)
        except json.JSONDecodeError:
            prefix, separator, suffix = raw_locator.partition(":")
            if separator and suffix.isdigit():
                labels = {"line": "行", "segment": "段", "page": "页", "row": "行"}
                label = labels.get(prefix.lower())
                if label is not None:
                    return f"第 {int(suffix)} {label}", None, "未提供"
            return fallback, None, "未提供"
    if not isinstance(locator, Mapping):
        return fallback, None, "未提供"

    recorded = None
    cursor = locator.get("cursor")
    if isinstance(cursor, Mapping):
        recorded = _source_recorded_at(cursor.get("timestamp"))
    if recorded is None:
        for field in ("sent_at", "recorded_at", "timestamp"):
            recorded = _source_recorded_at(locator.get(field))
            if recorded is not None:
                break

    location = fallback
    for field, label in (
        ("sequence", "条记录"),
        ("page", "页"),
        ("line", "行"),
        ("row", "行"),
        ("slide", "页幻灯片"),
    ):
        position = locator.get(field)
        if (
            isinstance(position, int)
            and not isinstance(position, bool)
            and position >= 0
        ):
            location = f"第 {position} {label}"
            break
    if isinstance(cursor, Mapping) or "message_key" in locator:
        location = (
            f"聊天记录中的第 {segment} 条" if isinstance(segment, int) else "聊天记录"
        )
    if recorded is None:
        return location, None, "未提供"
    return location, recorded[0], recorded[1]


def _business_artifact(value: object, hidden_ids: set[str]) -> str:
    displayed = _display(value, hidden_ids=hidden_ids)
    return displayed.replace("\\", "/").rsplit("/", 1)[-1]


def _evidence_section_lines(
    lookup: Mapping[str, Mapping[str, Any]],
    numbers: Mapping[str, int],
    hidden_ids: set[str],
) -> list[str]:
    lines: list[str] = []
    for evidence_ref, number in sorted(numbers.items(), key=lambda item: item[1]):
        entry = lookup[evidence_ref]
        location, _recorded_sort, recorded_display = _locator_presentation(entry)
        lines.extend(
            [
                f"- **依据 {number}**",
                f"  - 摘录：{_display(entry['excerpt'], hidden_ids=hidden_ids)}",
                "  - 来源文件/记录："
                f"{_business_artifact(entry['artifact'], hidden_ids)}",
                f"  - 说话人：{_display(entry['speaker'], hidden_ids=hidden_ids)}",
                f"  - 业务位置：{location}",
                f"  - 资料记录时间：{recorded_display}（不等于事件发生时间）",
            ]
        )
    return lines or ["- 暂无可展示依据"]


def _unknown_entry_sort_key(
    entry: Mapping[str, Any],
    lookup: Mapping[str, Mapping[str, Any]],
) -> tuple[int, datetime]:
    timestamps = []
    for evidence_ref in entry.get("evidence_ids", []):
        evidence = lookup.get(evidence_ref)
        if evidence is None:
            continue
        _location, recorded, _displayed = _locator_presentation(evidence)
        if recorded is not None:
            timestamps.append(recorded)
    return (
        (0, min(timestamps))
        if timestamps
        else (1, datetime.max.replace(tzinfo=timezone.utc))
    )


def render_markdown(package: Mapping[str, Any]) -> str:
    """Render the fixed seven-section business review presentation."""
    known_entries = [
        entry for entry in package["timeline_entries"] if entry["time"] is not None
    ]
    known_entries.sort(key=lambda entry: _parse_event_time(entry["time"], "time"))
    unknown_entries = [
        entry for entry in package["timeline_entries"] if entry["time"] is None
    ]
    what_it_is = package["what_it_is"]
    current_state = package["current_state"]
    evidence_lookup = _evidence_lookup(package)
    evidence_numbers = {
        evidence_ref: index
        for index, evidence_ref in enumerate(evidence_lookup, start=1)
    }
    unknown_entries.sort(
        key=lambda entry: _unknown_entry_sort_key(entry, evidence_lookup)
    )
    hidden_ids = _internal_reference_ids(package)
    lines = [
        f"# {package.get('selection_label', '对象时间线')}",
        "",
        "## 对象是什么",
        _display(what_it_is["summary"], hidden_ids=hidden_ids),
        "- 业务角色："
        + (
            "、".join(
                _business_label(role, _ROLE_LABELS) for role in what_it_is["roles"]
            )
            or "尚未确认"
        ),
        f"- 生命周期：{_business_label(what_it_is['lifecycle'], _LIFECYCLE_LABELS)}",
        f"- 依据：{_evidence_numbers(what_it_is, evidence_numbers)}",
        "",
        "## 关键事件时间线",
        "### 已知事件时间",
    ]
    if not known_entries:
        lines.append("- 暂无可确认事件时间")
    for entry in known_entries:
        participants = _display(
            [participant["name"] for participant in entry["participants"]],
            "未确认",
            hidden_ids=hidden_ids,
        )
        lines.extend(
            [
                f"- **{_display(entry['time'], hidden_ids=hidden_ids)}**："
                f"{_display(entry['event'], hidden_ids=hidden_ids)}",
                f"  - 时间依据："
                f"{_display(entry['time_basis_detail'], hidden_ids=hidden_ids)}",
                f"  - 参与者：{participants}",
                f"  - 地点：{_display(entry['location'], '未确认', hidden_ids=hidden_ids)}",
                f"  - 状态变化：{_display(entry['state_change'], '未确认', hidden_ids=hidden_ids)}",
                f"  - 不确定性："
                f"{_display(entry['uncertainty'], '无明显不确定性', hidden_ids=hidden_ids)}",
                f"  - 依据：{_evidence_numbers(entry, evidence_numbers)}",
            ]
        )
    lines.append("### 事件时间尚不明确")
    if not unknown_entries:
        lines.append("- 无")
    for entry in unknown_entries:
        participants = _display(
            [participant["name"] for participant in entry["participants"]],
            "未确认",
            hidden_ids=hidden_ids,
        )
        lines.extend(
            [
                f"- {_display(entry['event'], hidden_ids=hidden_ids)}",
                f"  - 时间依据："
                f"{_display(entry['time_basis_detail'], hidden_ids=hidden_ids)}",
                f"  - 参与者：{participants}",
                f"  - 地点：{_display(entry['location'], '未确认', hidden_ids=hidden_ids)}",
                f"  - 状态变化：{_display(entry['state_change'], '未确认', hidden_ids=hidden_ids)}",
                f"  - 不确定性："
                f"{_display(entry['uncertainty'], '时间尚未确认', hidden_ids=hidden_ids)}",
                "  - 事件时间：未知",
                f"  - 依据：{_evidence_numbers(entry, evidence_numbers)}",
            ]
        )
    lines.extend(
        [
            "",
            "## 当前状态",
            _display(current_state["state"], hidden_ids=hidden_ids),
            f"- 截至：{_display(current_state['as_of'], '尚未确认', hidden_ids=hidden_ids)}",
            f"- 不确定性："
            f"{_display(current_state['uncertainty'], '无明显不确定性', hidden_ids=hidden_ids)}",
            f"- 依据：{_evidence_numbers(current_state, evidence_numbers)}",
            "",
            "## 依据",
        ]
    )
    lines.extend(_evidence_section_lines(evidence_lookup, evidence_numbers, hidden_ids))
    lines.extend(
        [
            "",
            "## 冲突",
        ]
    )
    if package["conflicts"]:
        for conflict in package["conflicts"]:
            status = "尚未解决" if conflict["unresolved"] else "已能并列解释"
            lines.append(
                f"- {_display(conflict['summary'], hidden_ids=hidden_ids)}（{status}）"
            )
            lines.append(f"  - 依据：{_evidence_numbers(conflict, evidence_numbers)}")
    else:
        lines.append("- 暂未发现")
    lines.extend(["", "## 未知与待确认"])
    if package["unknowns"]:
        for unknown in package["unknowns"]:
            lines.append(f"- {_display(unknown['question'], hidden_ids=hidden_ids)}")
            lines.append(f"  - 依据：{_evidence_numbers(unknown, evidence_numbers)}")
    else:
        lines.append("- 暂无")
    coverage = package["coverage"]
    lines.extend(
        [
            "",
            "## 覆盖范围",
            f"- 已检查信息数：{len(coverage['covered_atomic_information_ids'])}",
            f"- 是否完整：{'是' if coverage['complete'] else '否'}",
        ]
    )
    if coverage["incomplete_reasons"]:
        lines.append(
            "- 不完整原因："
            + "；".join(
                _coverage_reason(reason, hidden_ids)
                for reason in coverage["incomplete_reasons"]
            )
        )
    if not coverage["complete"]:
        lines.append("- 验收结论：当前资料覆盖不完整，本次验收不通过")
    return "\n".join(lines) + "\n"
