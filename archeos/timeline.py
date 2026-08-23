"""Read-only Stage 1 Object Timeline projection.

This module deliberately owns no persistence: inputs are supplied by a caller and
the result is a derived artifact.  The provider is called once per selected object.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
import tempfile


class TimelineError(ValueError):
    pass


class TimelineProvider(Protocol):
    contract_version: str
    def __call__(self, context: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _timeline_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["object_id", "what_it_is", "timeline_entries", "current_state", "conflicts", "unknowns", "information_accounting", "coverage"], "properties": {"object_id": {"type": "string"}, "what_it_is": {"type": "string"}, "timeline_entries": {"type": "array"}, "current_state": {"type": "object"}, "conflicts": {"type": "array"}, "unknowns": {"type": "array"}, "information_accounting": {"type": "object"}, "coverage": {"type": "object"}}}


class CodexTimelineProvider:
    contract_version = "timeline.v1"
    def __init__(self, *, sdk_loader: Callable[[], tuple[type[object], object, object]]):
        self.sdk_loader = sdk_loader
    def __call__(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        codex_type, deny_all, read_only = self.sdk_loader()
        prompt = "Return one complete Object Timeline package as JSON. Use only supplied bounded context; never guess IDs or dates. Unknown event time must be null. Do not call tools or mutate data.\n" + json.dumps(context, ensure_ascii=False, default=str)
        with tempfile.TemporaryDirectory(prefix="archeos-timeline-") as directory:
            with codex_type() as codex:  # type: ignore[attr-defined]
                thread = codex.thread_start(approval_mode=deny_all, cwd=directory, developer_instructions="Read-only; return structured JSON only.", ephemeral=True, sandbox=read_only)
                result = thread.run(prompt, output_schema=_timeline_schema(), sandbox=read_only)
        response = getattr(result, "final_response", None)
        if not isinstance(response, str): raise TimelineError("Codex provider returned no structured result")
        try: return json.loads(response)
        except json.JSONDecodeError as exc: raise TimelineError("Codex provider returned invalid JSON") from exc


@dataclass(frozen=True)
class Selection:
    object_id: str
    label: str
    atomic_information_ids: tuple[str, ...] = ()


def load_selection(path: Path) -> tuple[Selection, ...]:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise TimelineError("selection file must have private mode 0600")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "contexts" in raw:
        raise TimelineError("selection file may not contain contexts or provider results")
    entries = raw.get("objects") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not 3 <= len(entries) <= 5:
        raise TimelineError("selection must contain exactly 3–5 objects")
    result = []
    seen: set[str] = set()
    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            raise TimelineError(f"objects[{i}] must be an object")
        oid, label = item.get("object_id"), item.get("label")
        if not isinstance(oid, str) or not oid.strip() or oid in seen:
            raise TimelineError(f"objects[{i}].object_id must be unique and non-empty")
        if not isinstance(label, str) or not label.strip():
            raise TimelineError(f"objects[{i}].label must be non-empty")
        ids = item.get("atomic_information_ids", [])
        if not isinstance(ids, list) or any(not isinstance(x, str) or not x.strip() for x in ids):
            raise TimelineError(f"objects[{i}].atomic_information_ids must be strings")
        seen.add(oid)
        result.append(Selection(oid, label, tuple(ids)))
    return tuple(result)


def _fingerprint(selection: Selection, context: Mapping[str, Any], contract_version: str = "timeline.v1") -> str:
    payload = json.dumps({"selection": selection.__dict__, "context": context, "provider_contract": contract_version}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_package(package: Mapping[str, Any], supplied_ids: set[str], evidence_ids: set[str] | None = None, expected_object_id: str | None = None) -> dict[str, Any]:
    """Validate provider output and enforce complete, non-duplicated accounting."""
    required = {"object_id", "what_it_is", "timeline_entries", "current_state", "conflicts", "unknowns", "information_accounting", "coverage"}
    if not required <= set(package):
        raise TimelineError("provider result is missing required timeline fields")
    if not isinstance(package.get("object_id"), str) or not package["object_id"].strip():
        raise TimelineError("provider result must identify the selected Object")
    if expected_object_id is not None and package["object_id"] != expected_object_id:
        raise TimelineError("provider result Object ID does not match selection")
    if not isinstance(package["what_it_is"], str) or not package["what_it_is"].strip():
        raise TimelineError("what_it_is must be non-empty text")
    state = package["current_state"]
    if not isinstance(state, dict) or not isinstance(state.get("state"), str) or not state["state"].strip() or not isinstance(state.get("evidence_ids"), list) or not state["evidence_ids"] or "uncertainty" not in state:
        raise TimelineError("current_state requires state, evidence_ids and uncertainty")
    if package.get("conflicts"):
        if not state.get("uncertainty") or not any("conflict" in str(x).lower() or "冲突" in str(x) for x in (state.get("uncertainty"), state.get("state"))):
            raise TimelineError("unresolved conflicts require explicit current-state conflict uncertainty")
    accounting = package["information_accounting"]
    if not isinstance(accounting, dict) or set(accounting) != supplied_ids:
        raise TimelineError("information_accounting must cover every input exactly once")
    allowed = {"event", "current_state", "supporting_context", "conflict", "unknown", "not_relevant"}
    if any(value not in allowed for value in accounting.values()):
        raise TimelineError("invalid information_accounting category")
    refs = set()
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_ids" and isinstance(item, list): refs.update(item)
                else: walk(item)
        elif isinstance(value, list):
            for item in value: walk(item)
    walk(package)
    if evidence_ids is not None and not refs <= evidence_ids:
        raise TimelineError("provider result references unavailable Evidence")
    for key in ("atomic_information_ids", "object_ids"):
        values = set()
        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    if k == key and isinstance(v, list): values.update(v)
                    else: collect(v)
            elif isinstance(value, list):
                for v in value: collect(v)
        collect(package)
        if key == "atomic_information_ids" and not values <= supplied_ids:
            raise TimelineError("provider result references unavailable Atomic Information")
    if not package["current_state"].get("evidence_ids"):
        raise TimelineError("current_state must cite Evidence")
    for entry in package.get("timeline_entries", []):
        if not isinstance(entry, dict) or not entry.get("event"):
            raise TimelineError("timeline entry requires event")
        if entry.get("time_basis", "unknown") not in {"event_time", "claim_time", "source_time", "processing_time", "unknown"}:
            raise TimelineError("invalid time_basis")
        if entry.get("time_basis") != "event_time" and entry.get("time") not in (None, "unknown"):
            raise TimelineError("non-event time cannot be presented as event time")
        if "uncertainty" not in entry:
            raise TimelineError("timeline entry requires uncertainty")
        for field in ("participants", "location", "state_change", "evidence_ids"):
            if field not in entry:
                raise TimelineError(f"timeline entry requires {field}")
        if not isinstance(entry["evidence_ids"], list) or not entry["evidence_ids"]:
            raise TimelineError("timeline entry requires Evidence")
    coverage = package.get("coverage")
    if isinstance(coverage, dict) and coverage.get("complete") is False and not package.get("incomplete_reasons"):
        raise TimelineError("incomplete package requires incomplete_reasons")
    return dict(package)


def build_timelines(selections: tuple[Selection, ...], contexts: Mapping[str, Mapping[str, Any]], provider: Callable[[Mapping[str, Any]], Mapping[str, Any]], output_root: Path, resume: bool = False) -> dict[str, Any]:
    repo_root = Path.cwd().resolve()
    resolved_output = output_root.resolve()
    if resolved_output == repo_root or repo_root in resolved_output.parents:
        raise TimelineError("output-root must be outside the repository (local-only)")
    output_root.mkdir(parents=True, exist_ok=True)
    packages = []
    calls = 0
    for selection in selections:
        context = contexts.get(selection.object_id)
        if context is None: raise TimelineError(f"unknown object: {selection.object_id}")
        contract_version = getattr(provider, "contract_version", "timeline.v1")
        fingerprint = _fingerprint(selection, context, contract_version)
        target = output_root / f"{selection.object_id}.json"
        if resume and target.is_file():
            try:
                saved = json.loads(target.read_text(encoding="utf-8"))
                if saved.get("input_fingerprint") == fingerprint:
                    supplied_saved = set(context.get("atomic_information_ids", ())) | set(selection.atomic_information_ids)
                    validate_package(saved, supplied_saved, set(context.get("evidence_ids", ())), selection.object_id)
                    packages.append(saved); continue
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        supplied = set(context.get("atomic_information_ids", ())) | set(selection.atomic_information_ids)
        result = validate_package(provider({**context, "supplemental_atomic_information_ids": list(selection.atomic_information_ids)}), supplied, set(context.get("evidence_ids", ())), selection.object_id)
        calls += 1
        result.update({"object_id": selection.object_id, "selection_label": selection.label, "input_fingerprint": fingerprint})
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        validate_package(json.loads(target.read_text(encoding="utf-8")), supplied, set(context.get("evidence_ids", ())), selection.object_id)
        packages.append(result)
    return {"schema_version": "stage1-object-timeline.v1", "packages": packages, "provider_calls": calls}


def render_markdown(package: Mapping[str, Any]) -> str:
    entries = sorted(package.get("timeline_entries", []), key=lambda e: (e.get("time") is None, e.get("time") or ""))
    lines = [f"# {package.get('selection_label', package['object_id'])}", "", "## 对象是什么", str(package["what_it_is"]), "", "## 关键事件时间线"]
    for entry in entries:
        lines.append(f"- **{entry.get('time') or '未知时间'}**（{entry.get('time_basis','unknown')}）：{entry.get('event')}；参与者：{entry.get('participants')}；地点：{entry.get('location')}；状态变化：{entry.get('state_change')}；依据：{entry.get('evidence_ids')}；不确定性：{entry.get('uncertainty')}")
    lines += ["", "## 当前状态", str(package["current_state"].get("state", package["current_state"])), "", "## 依据", str(package["current_state"].get("evidence_ids", [])), "", "## 冲突"]
    lines += [f"- {x}" for x in package.get("conflicts", [])] or ["- 无"]
    lines += ["", "## 未知与待确认"]
    lines += [f"- {x}" for x in package.get("unknowns", [])] or ["- 无"]
    lines += ["", "## 覆盖范围", json.dumps(package["coverage"], ensure_ascii=False)]
    return "\n".join(lines) + "\n"
