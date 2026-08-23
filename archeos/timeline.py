"""Read-only Stage 1 Object Timeline projection.

This module deliberately owns no persistence: inputs are supplied by a caller and
the result is a derived artifact.  The provider is called once per selected object.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


class TimelineError(ValueError):
    pass


@dataclass(frozen=True)
class Selection:
    object_id: str
    label: str
    atomic_information_ids: tuple[str, ...] = ()


def load_selection(path: Path) -> tuple[Selection, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
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


def _fingerprint(selection: Selection, context: Mapping[str, Any]) -> str:
    payload = json.dumps({"selection": selection.__dict__, "context": context}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_package(package: Mapping[str, Any], supplied_ids: set[str], evidence_ids: set[str] | None = None) -> dict[str, Any]:
    """Validate provider output and enforce complete, non-duplicated accounting."""
    required = {"object_id", "what_it_is", "timeline_entries", "current_state", "conflicts", "unknowns", "information_accounting", "coverage"}
    if not required <= set(package):
        raise TimelineError("provider result is missing required timeline fields")
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
    if not package["current_state"].get("evidence_ids"):
        raise TimelineError("current_state must cite Evidence")
    return dict(package)


def build_timelines(selections: tuple[Selection, ...], contexts: Mapping[str, Mapping[str, Any]], provider: Callable[[Mapping[str, Any]], Mapping[str, Any]], output_root: Path, resume: bool = False) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    packages = []
    calls = 0
    for selection in selections:
        context = contexts.get(selection.object_id)
        if context is None: raise TimelineError(f"unknown object: {selection.object_id}")
        fingerprint = _fingerprint(selection, context)
        target = output_root / f"{selection.object_id}.json"
        if resume and target.is_file():
            saved = json.loads(target.read_text(encoding="utf-8"))
            if saved.get("input_fingerprint") == fingerprint:
                packages.append(saved); continue
        supplied = set(context.get("atomic_information_ids", ())) | set(selection.atomic_information_ids)
        result = validate_package(provider({**context, "supplemental_atomic_information_ids": list(selection.atomic_information_ids)}), supplied, set(context.get("evidence_ids", ())))
        calls += 1
        result.update({"object_id": selection.object_id, "selection_label": selection.label, "input_fingerprint": fingerprint})
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        packages.append(result)
    return {"schema_version": "stage1-object-timeline.v1", "packages": packages, "provider_calls": calls}


def render_markdown(package: Mapping[str, Any]) -> str:
    lines = [f"# {package.get('selection_label', package['object_id'])}", "", "## 对象是什么", str(package["what_it_is"]), "", "## 关键事件时间线"]
    for entry in package.get("timeline_entries", []):
        lines.append(f"- **{entry.get('time', '未知时间')}**：{entry.get('event', entry.get('description', '未知事件'))}")
    lines += ["", "## 当前状态", str(package["current_state"].get("state", package["current_state"])), "", "## 冲突"]
    lines += [f"- {x}" for x in package.get("conflicts", [])] or ["- 无"]
    lines += ["", "## 未知与待确认"]
    lines += [f"- {x}" for x in package.get("unknowns", [])] or ["- 无"]
    lines += ["", "## 覆盖范围", json.dumps(package["coverage"], ensure_ascii=False)]
    return "\n".join(lines) + "\n"
