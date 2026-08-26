"""Contact-scoped WeChat Processing helpers.

Contact selection, checkpoints and acceptance packs are Processing authority and
Views.  They do not introduce Contact, Conversation or Message Core concepts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path

from .atomic_information import JsonlAtomicInformationStore
from .wechat_digest import (
    TERMINAL_ITEM_STATES,
    WechatCapture,
    WechatCaptureProvider,
    WechatContactBinding,
    WechatDigestError,
    WechatDigestRunStore,
)
from .world_model import SQLiteWorldModelRepository

CONTACT_SELECTION_SCHEMA_VERSION = "wechat-contact-selection/1.0"
CONTACT_ACCEPTANCE_SCHEMA_VERSION = "wechat-contact-acceptance-pack/1.0"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _private_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise WechatDigestError("联系人处理记录不可验证；未读取消息。")
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise WechatDigestError("联系人处理记录不是私有 0600 文件。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WechatDigestError("联系人处理记录不可验证；未读取消息。") from exc
    if not isinstance(value, dict):
        raise WechatDigestError("联系人处理记录不可验证；未读取消息。")
    return value


class WechatContactSelectionStore:
    """Persist and read back the exact metadata-only contact choice."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def bind(self, binding: WechatContactBinding) -> Path:
        if re.fullmatch(
            r"wechat_conversation_[0-9a-f]{32}", binding.conversation_key
        ) is None:
            raise WechatDigestError("联系人技术身份无效；未读取消息。")
        path = self.root / f"{binding.conversation_key}.json"
        authority = binding.to_dict()
        payload = {
            "schema_version": CONTACT_SELECTION_SCHEMA_VERSION,
            "selection": authority,
            "selection_fingerprint": _fingerprint(authority),
            "person_object_id": None,
        }
        if self.root.exists():
            for existing_path in self.root.glob("wechat_conversation_*.json"):
                if existing_path == path:
                    continue
                existing = _read_private_json(existing_path)
                selection = existing.get("selection")
                if not isinstance(selection, dict):
                    raise WechatDigestError("联系人选择记录不可验证；未读取消息。")
                if (
                    selection.get("display_name") == binding.display_name
                    or selection.get("provider_conversation_id")
                    == binding.provider_conversation_id
                ):
                    raise WechatDigestError(
                        "联系人名称或技术身份与已保存选择不一致；请重新确认联系人。"
                    )
        if path.exists() or path.is_symlink():
            if _read_private_json(path) != payload:
                raise WechatDigestError(
                    "联系人名称或技术身份与已保存选择不一致；请重新确认联系人。"
                )
        else:
            _private_write(path, payload)
        if _read_private_json(path) != payload:
            raise WechatDigestError("联系人选择记录写入后无法一致读回。")
        return path


class OverlapFilteringWechatCaptureProvider:
    """Remove already committed legacy message provenance before new effects."""

    def __init__(
        self,
        delegate: WechatCaptureProvider,
        seen_message_keys: Callable[[], frozenset[str]],
    ) -> None:
        self.delegate = delegate
        self.provider_version = delegate.provider_version
        self._seen_message_keys = seen_message_keys

    @property
    def last_capture_metrics(self) -> Mapping[str, int]:
        value = getattr(self.delegate, "last_capture_metrics", {})
        return value if isinstance(value, Mapping) else {}

    def capture(self, after_cursor, **kwargs) -> WechatCapture:
        capture = self.delegate.capture(after_cursor, **kwargs)
        if kwargs.get("observe_only"):
            return capture
        seen = self._seen_message_keys()
        return WechatCapture(
            capture.provider_version,
            capture.after_cursor,
            capture.upper_bound,
            tuple(message for message in capture.messages if message.message_key not in seen),
        )


def committed_legacy_message_keys(workspace: Path) -> frozenset[str]:
    """Read plan/status metadata only; never open captured message bodies."""

    runs_root = Path(workspace) / "02_processing" / "wechat_digest" / "runs"
    if not runs_root.exists():
        return frozenset()
    seen: set[str] = set()
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        try:
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatDigestError(
                "旧微信运行的消息来源记录不可验证；未产生重复长期写入。"
            ) from exc
        if not isinstance(plan, dict) or not isinstance(status, dict):
            raise WechatDigestError("旧微信运行的消息来源记录不可验证。")
        items = status.get("items")
        conversations = plan.get("conversations")
        if not isinstance(items, dict) or not isinstance(conversations, list):
            raise WechatDigestError("旧微信运行的消息来源记录不可验证。")
        for conversation in conversations:
            if not isinstance(conversation, dict):
                raise WechatDigestError("旧微信运行的消息来源记录不可验证。")
            key = conversation.get("conversation_key")
            message_keys = conversation.get("message_keys")
            item = items.get(f"conversation:{key}")
            if (
                not isinstance(key, str)
                or not isinstance(message_keys, list)
                or any(not isinstance(value, str) for value in message_keys)
                or not isinstance(item, dict)
            ):
                raise WechatDigestError("旧微信运行的消息来源记录不可验证。")
            if item.get("state") in TERMINAL_ITEM_STATES:
                seen.update(message_keys)
    return frozenset(seen)


def _event_group(revision) -> tuple[str, str, str]:
    evidence_time = next(
        (item.start for item in revision.source_evidence if item.start), None
    )
    when = revision.claim.claimed_at if revision.claim and revision.claim.claimed_at else evidence_time
    when = when or revision.created_at
    day = when[:10] if len(when) >= 10 else "时间未知"
    concern = (
        min(revision.raw_concerns, key=str.casefold)
        if revision.raw_concerns
        else "联系人会话"
    )
    category = (
        "业务变化"
        if revision.semantic_type in {"action", "commitment", "decision"}
        else "待确认事项"
        if revision.semantic_type == "question"
        else "业务信息"
    )
    return day, concern, category


def build_contact_acceptance_pack(
    *,
    workspace: Path,
    run_store: WechatDigestRunStore,
    binding: WechatContactBinding,
    output_root: Path,
) -> tuple[Path, Path]:
    """Build a private contact-level View from durable Information and evidence."""

    ordered_ids: list[str] = []
    attachment_counts: Counter[str] = Counter()
    for run_dir in sorted(run_store.runs_root.glob("run_*")):
        run_id = run_dir.name
        plan = run_store.plan(run_id)
        status = run_store.status(run_id)
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("联系人运行状态不可验证。")
        for item in items.values():
            if not isinstance(item, dict):
                raise WechatDigestError("联系人运行状态不可验证。")
            values = item.get("atomic_information_ids", [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise WechatDigestError("联系人长期信息引用不可验证。")
            for value in values:
                if value not in ordered_ids:
                    ordered_ids.append(value)
        attachments = plan.get("attachments")
        if not isinstance(attachments, list):
            raise WechatDigestError("联系人附件统计不可验证。")
        for attachment in attachments:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("status"), str):
                raise WechatDigestError("联系人附件统计不可验证。")
            status_value = str(attachment["status"])
            attachment_key = attachment.get("attachment_key")
            item = items.get(f"attachment:{attachment_key}")
            item_state = item.get("state") if isinstance(item, dict) else None
            coverage_state = (
                "missing"
                if status_value == "missing"
                else "unsupported"
                if status_value == "ambiguous"
                else "parsed"
                if item_state == "processed"
                else "privacy_blocked"
                if item_state == "local_only"
                else "failed"
                if item_state == "failed_closed"
                else "unsupported"
                if item_state == "unsupported"
                else "available"
            )
            attachment_counts[coverage_state] += 1

    information = JsonlAtomicInformationStore(
        Path(workspace) / "03_information" / "atomic_information.jsonl"
    )
    by_id = {item.atomic_information_id: item for item in information.list_atomic_information()}
    missing_ids = [value for value in ordered_ids if value not in by_id]
    if missing_ids:
        raise WechatDigestError("联系人业务验收所需的长期信息无法完整读回。")
    revisions = tuple(by_id[value] for value in ordered_ids if value in by_id)
    grouped: dict[tuple[str, str, str], list[object]] = defaultdict(list)
    for revision in revisions:
        grouped[_event_group(revision)].append(revision)

    events = []
    for index, ((day, concern, category), values) in enumerate(sorted(grouped.items()), start=1):
        evidence = [
            {
                "atomic_information_id": revision.atomic_information_id,
                "source_id": item.source_id,
                "locator": item.locator,
                "excerpt": item.excerpt,
            }
            for revision in values
            for item in revision.source_evidence
        ]
        participants = sorted(
            {
                label
                for revision in values
                for label in (
                    *((revision.claim.claimant_label,) if revision.claim else ()),
                    *revision.raw_concerns,
                )
                if label
            },
            key=str.casefold,
        )
        events.append(
            {
                "event_id": f"event-{index}",
                "time": None if day == "时间未知" else day,
                "participants": participants,
                "location_or_channel": "微信会话",
                "event": "；".join(dict.fromkeys(revision.statement for revision in values)),
                "business_subject": concern,
                "category": category,
                "status_change": [
                    revision.statement
                    for revision in values
                    if revision.semantic_type in {"action", "commitment", "decision"}
                ],
                "evidence": evidence,
            }
        )

    object_candidates: list[dict[str, object]] = []
    related_object_ids = {
        object_id for revision in revisions for object_id in revision.related_object_ids
    }
    database = Path(workspace) / "04_core" / "archeos.sqlite3"
    if database.exists():
        repository = SQLiteWorldModelRepository(database)
        repository.initialize()
        try:
            for value in repository.list_objects():
                if value.object_id not in related_object_ids:
                    continue
                roles = [item.role for item in repository.list_roles(value.object_id, active_only=True)]
                names = repository.list_names(value.object_id, active_only=True)
                name = next(
                    (item.name for item in names if item.is_primary),
                    names[0].name if names else value.object_id,
                )
                object_candidates.append(
                    {"name": name, "roles": roles, "object_id": value.object_id}
                )
        finally:
            repository.close()
    known_names = {str(item["name"]) for item in object_candidates}
    for name in sorted({name for revision in revisions for name in revision.raw_concerns}, key=str.casefold):
        if name not in known_names:
            object_candidates.append({"name": name, "roles": [], "object_id": None})

    conflicts_by_concern: dict[str, set[str]] = defaultdict(set)
    for revision in revisions:
        if revision.claim:
            for concern in revision.raw_concerns:
                conflicts_by_concern[concern].add(revision.claim.stance)
    conflicts = [
        {"subject": concern, "reason": "同一事项存在相反声明立场"}
        for concern, stances in sorted(conflicts_by_concern.items())
        if "deny" in stances and "assert" in stances
    ]
    unknowns = [
        revision.statement
        for revision in revisions
        if revision.semantic_type == "question" or revision.confidence < 0.6
    ]
    if any(
        attachment_counts[field]
        for field in ("missing", "unsupported", "failed", "privacy_blocked")
    ):
        unknowns.append("部分附件尚未形成可验证内容。")

    pack = {
        "schema_version": CONTACT_ACCEPTANCE_SCHEMA_VERSION,
        "contact": {
            "display_name": binding.display_name,
            "conversation_key": binding.conversation_key,
            "person_object_id": None,
        },
        "object_candidates": object_candidates,
        "events": events,
        "current_state": {
            "completed": [
                item.statement for item in revisions if item.semantic_type in {"action", "decision"}
            ],
            "in_progress": [
                item.statement for item in revisions if item.semantic_type == "commitment"
            ],
            "todos": [
                item.statement for item in revisions if item.semantic_type == "question"
            ],
            "commitments": [
                item.statement for item in revisions if item.semantic_type == "commitment"
            ],
            "blockers": [item.statement for item in revisions if "阻" in item.statement],
        },
        "conflicts": conflicts,
        "unknowns": list(dict.fromkeys(unknowns)),
        "attachment_coverage": {
            "total": sum(attachment_counts.values()),
            "available": attachment_counts["available"],
            "parsed": attachment_counts["parsed"],
            "privacy_blocked": attachment_counts["privacy_blocked"],
            "missing": attachment_counts["missing"],
            "unsupported": attachment_counts["unsupported"],
            "failed": attachment_counts["failed"],
        },
        "information_count": len(revisions),
    }
    pack["pack_fingerprint"] = _fingerprint(pack)
    json_path = Path(output_root) / "contact-acceptance.json"
    markdown_path = Path(output_root) / "contact-acceptance.md"
    _private_write(json_path, pack)
    lines = [
        f"# {binding.display_name} 微信业务验收",
        "",
        f"- 长期信息：{len(revisions)} 条",
        f"- 业务事件：{len(events)} 项",
        f"- 对象候选：{len(object_candidates)} 个",
        "",
        "## 发生过什么",
        "",
        *(f"- {item['time'] or '时间未知'}｜{item['event']}" for item in events),
        "",
        "## 冲突与未知",
        "",
        *(f"- {item}" for item in (list(dict.fromkeys(unknowns)) or ["暂未发现"])),
    ]
    markdown_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=markdown_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write("\n".join(lines) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, markdown_path)
        directory = os.open(markdown_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    if _read_private_json(json_path) != pack:
        raise WechatDigestError("联系人业务验收包写入后无法一致读回。")
    if (
        markdown_path.is_symlink()
        or stat.S_IMODE(markdown_path.stat(follow_symlinks=False).st_mode) != 0o600
        or markdown_path.read_text(encoding="utf-8") != "\n".join(lines) + "\n"
    ):
        raise WechatDigestError("联系人业务验收摘要写入后无法一致读回。")
    return json_path, markdown_path
