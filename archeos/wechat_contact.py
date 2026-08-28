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
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
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
from .wechat_contact_synthesis import (
    ContactSynthesisProvider,
    ContactSynthesisStore,
)

CONTACT_SELECTION_SCHEMA_VERSION = "wechat-contact-selection/1.0"
CONTACT_ACCEPTANCE_SCHEMA_VERSION = "wechat-contact-acceptance-pack/3.0"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        if (
            re.fullmatch(r"wechat_conversation_[0-9a-f]{32}", binding.conversation_key)
            is None
        ):
            raise WechatDigestError("联系人技术身份无效；未读取消息。")
        path = self.root / f"{binding.conversation_key}.json"
        stable_identity = {
            "conversation_key": binding.conversation_key,
            "provider_conversation_id": binding.provider_conversation_id,
            "is_group": binding.is_group,
        }
        if self.root.exists():
            for existing_path in self.root.glob("wechat_conversation_*.json"):
                if existing_path == path:
                    continue
                existing = _read_private_json(existing_path)
                selection = existing.get("selection")
                if not isinstance(selection, dict):
                    raise WechatDigestError("联系人选择记录不可验证；未读取消息。")
                existing_identity = selection.get("stable_identity")
                if not isinstance(existing_identity, dict):
                    raise WechatDigestError("联系人选择记录不可验证；未读取消息。")
                if (
                    existing_identity.get("provider_conversation_id")
                    == binding.provider_conversation_id
                ):
                    raise WechatDigestError(
                        "联系人技术身份与已保存选择不一致；请重新确认联系人。"
                    )
        if path.exists() or path.is_symlink():
            existing = _read_private_json(path)
            selection = existing.get("selection")
            if (
                not isinstance(selection, dict)
                or selection.get("stable_identity") != stable_identity
                or existing.get("schema_version") != CONTACT_SELECTION_SCHEMA_VERSION
                or existing.get("person_object_id") is not None
            ):
                raise WechatDigestError(
                    "联系人技术身份与已保存选择不一致；请重新确认联系人。"
                )
            history = selection.get("display_name_history")
            if (
                not isinstance(history, list)
                or not history
                or any(not isinstance(item, str) or not item for item in history)
            ):
                raise WechatDigestError("联系人展示名称历史不可验证；未读取消息。")
            if binding.display_name not in history:
                history = [*history, binding.display_name]
        else:
            history = [binding.display_name]
        selection = {
            "stable_identity": stable_identity,
            "current_display_name": binding.display_name,
            "display_name_history": history,
        }
        payload = {
            "schema_version": CONTACT_SELECTION_SCHEMA_VERSION,
            "selection": selection,
            "selection_fingerprint": _fingerprint(selection),
            "person_object_id": None,
        }
        if not path.exists() or _read_private_json(path) != payload:
            _private_write(path, payload)
        if _read_private_json(path) != payload:
            raise WechatDigestError("联系人选择记录写入后无法一致读回。")
        return path


@dataclass(frozen=True)
class LegacyMessageOverlap:
    """Legacy provenance that is either safe to filter or unsafe to replay."""

    committed_message_keys: frozenset[str]
    nonterminal_message_keys: frozenset[str]


class OverlapFilteringWechatCaptureProvider:
    """Remove already committed legacy message provenance before new effects."""

    def __init__(
        self,
        delegate: WechatCaptureProvider,
        overlap_authority: Callable[[], LegacyMessageOverlap],
    ) -> None:
        self.delegate = delegate
        self.provider_version = delegate.provider_version
        self._overlap_authority = overlap_authority

    @property
    def last_capture_metrics(self) -> Mapping[str, int]:
        value = getattr(self.delegate, "last_capture_metrics", {})
        return value if isinstance(value, Mapping) else {}

    def capture(self, after_cursor, **kwargs) -> WechatCapture:
        capture = self.delegate.capture(after_cursor, **kwargs)
        if kwargs.get("observe_only"):
            return capture
        authority = self._overlap_authority()
        captured_keys = {message.message_key for message in capture.messages}
        if captured_keys & authority.nonterminal_message_keys:
            raise WechatDigestError(
                "所选联系人范围与未完成的旧微信任务重叠；"
                "无法证明不会重复长期效果，已在写入前停止。"
            )
        return WechatCapture(
            capture.provider_version,
            capture.after_cursor,
            capture.upper_bound,
            tuple(
                message
                for message in capture.messages
                if message.message_key not in authority.committed_message_keys
            ),
        )


def legacy_message_overlap(workspace: Path) -> LegacyMessageOverlap:
    """Classify legacy message provenance without opening captured bodies."""

    runs_root = Path(workspace) / "02_processing" / "wechat_digest" / "runs"
    if not runs_root.exists():
        return LegacyMessageOverlap(frozenset(), frozenset())
    committed: set[str] = set()
    nonterminal: set[str] = set()
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
                committed.update(message_keys)
            else:
                nonterminal.update(message_keys)
    overlap = committed & nonterminal
    if overlap:
        nonterminal.update(overlap)
        committed.difference_update(overlap)
    return LegacyMessageOverlap(frozenset(committed), frozenset(nonterminal))


def committed_legacy_message_keys(workspace: Path) -> frozenset[str]:
    """Compatibility projection of provably terminal legacy provenance."""

    return legacy_message_overlap(workspace).committed_message_keys


def _contact_semantic_provider_calls(
    workspace: Path,
    receipts: tuple[dict[str, object], ...],
) -> int:
    """Read only item-bound Semantic receipts; never inventory the Workspace."""

    audit_root = Path(workspace) / "02_processing" / "semantic_handoff_runs"
    seen_processing_runs: set[str] = set()
    for receipt in receipts:
        processing_run_id = receipt.get("processing_run_id")
        relative_path = receipt.get("audit_relative_path")
        if (
            set(receipt)
            != {
                "source_id",
                "representation_id",
                "processing_run_id",
                "audit_relative_path",
                "audit_fingerprint",
                "input_fingerprint",
                "result_fingerprint",
                "package_fingerprint",
            }
            or not isinstance(processing_run_id, str)
            or not isinstance(relative_path, str)
            or processing_run_id in seen_processing_runs
        ):
            raise WechatDigestError(
                "联系人 Semantic receipt identity 无效或重复。"
            )
        expected_relative = str(
            Path("02_processing")
            / "semantic_handoff_runs"
            / processing_run_id
            / "processing-run-audit.json"
        )
        if relative_path != expected_relative:
            raise WechatDigestError("联系人 Semantic receipt 路径绑定漂移。")
        audit_path = Path(workspace) / relative_path
        audit = _read_private_json(audit_path)
        raw_fingerprint = "sha256:" + hashlib.sha256(
            audit_path.read_bytes()
        ).hexdigest()
        if (
            audit_path.parent.parent != audit_root
            or audit.get("schema_version") != "processing-run-audit/1.0"
            or audit.get("provider_route") != "codex-cli"
            or audit.get("processing_run_id") != processing_run_id
            or audit.get("input_fingerprint") != receipt.get("input_fingerprint")
            or audit.get("result_fingerprint")
            != receipt.get("result_fingerprint")
            or audit.get("package_fingerprint")
            != receipt.get("package_fingerprint")
            or raw_fingerprint != receipt.get("audit_fingerprint")
        ):
            raise WechatDigestError(
                "联系人 Semantic Processing Run receipt 绑定漂移。"
            )
        seen_processing_runs.add(processing_run_id)
    return len(seen_processing_runs)


def build_contact_acceptance_pack(
    *,
    workspace: Path,
    run_store: WechatDigestRunStore,
    binding: WechatContactBinding,
    output_root: Path,
    synthesis_provider_factory: Callable[[], ContactSynthesisProvider],
    authority_ref: str,
    absolute_cap: int,
    synthesis_segment_size: int = 100,
    resume_provider_calls: int = 0,
    before_provider_call: Callable[[], None] | None = None,
) -> tuple[Path, Path]:
    """Build one contact View while excluding a concurrent contact digest."""

    lock = getattr(run_store, "lock", None)
    with lock() if callable(lock) else nullcontext():
        return _build_contact_acceptance_pack_unlocked(
            workspace=workspace,
            run_store=run_store,
            binding=binding,
            output_root=output_root,
            synthesis_provider_factory=synthesis_provider_factory,
            synthesis_segment_size=synthesis_segment_size,
            authority_ref=authority_ref,
            absolute_cap=absolute_cap,
            resume_provider_calls=resume_provider_calls,
            before_provider_call=before_provider_call,
        )


def _build_contact_acceptance_pack_unlocked(
    *,
    workspace: Path,
    run_store: WechatDigestRunStore,
    binding: WechatContactBinding,
    output_root: Path,
    synthesis_provider_factory: Callable[[], ContactSynthesisProvider],
    synthesis_segment_size: int,
    authority_ref: str,
    absolute_cap: int,
    resume_provider_calls: int,
    before_provider_call: Callable[[], None] | None,
) -> tuple[Path, Path]:
    """Project one durable contact-level synthesis into a private business View."""

    ordered_ids: list[str] = []
    attachment_counts: Counter[str] = Counter()
    governance_provider_calls = 0
    semantic_receipts: list[dict[str, object]] = []
    ordered_runs: list[tuple[tuple[int, str, str], str]] = []
    for run_dir in run_store.runs_root.glob("run_*"):
        plan = run_store.plan(run_dir.name)
        after = plan.get("after_cursor")
        if not isinstance(after, dict):
            raise WechatDigestError("联系人运行顺序不可验证。")
        try:
            key = (
                int(after["timestamp"]),
                str(after["conversation_key"]),
                str(after["message_key"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WechatDigestError("联系人运行顺序不可验证。") from exc
        ordered_runs.append((key, run_dir.name))
    for _key, run_id in sorted(ordered_runs):
        plan = run_store.plan(run_id)
        status = run_store.status(run_id)
        items = status.get("items")
        run_completed = status.get("state") == "completed"
        if status.get("state") not in {"processing", "completed"} or not isinstance(
            items, dict
        ):
            raise WechatDigestError("联系人运行状态不可验证。")
        attachments = plan.get("attachments")
        conversations = plan.get("conversations")
        if not isinstance(attachments, list) or not isinstance(conversations, list):
            raise WechatDigestError("联系人运行计划项目顺序不可验证。")
        ordered_item_ids: list[str] = []
        planned_sources: dict[str, str] = {}
        for attachment in attachments:
            if not isinstance(attachment, dict) or not isinstance(
                attachment.get("attachment_key"), str
            ) or not isinstance(attachment.get("source_id"), str):
                raise WechatDigestError("联系人附件顺序不可验证。")
            item_id = f"attachment:{attachment['attachment_key']}"
            ordered_item_ids.append(item_id)
            planned_sources[item_id] = str(attachment["source_id"])
        for conversation in conversations:
            if not isinstance(conversation, dict) or not isinstance(
                conversation.get("conversation_key"), str
            ) or not isinstance(conversation.get("source_id"), str):
                raise WechatDigestError("联系人会话顺序不可验证。")
            item_id = f"conversation:{conversation['conversation_key']}"
            ordered_item_ids.append(item_id)
            planned_sources[item_id] = str(conversation["source_id"])
        if set(ordered_item_ids) != set(items):
            raise WechatDigestError("联系人运行计划与状态项目不一致。")
        if run_completed:
            for item_id in ordered_item_ids:
                item = items[item_id]
                if not isinstance(item, dict):
                    raise WechatDigestError("联系人运行状态不可验证。")
                source_id = item.get("source_id")
                representation_id = item.get("representation_id")
                item_receipts = item.get("semantic_provider_receipts", [])
                values = item.get("atomic_information_ids", [])
                if not isinstance(values, list) or any(
                    not isinstance(value, str) for value in values
                ) or source_id != planned_sources[item_id] or not isinstance(
                    item_receipts, list
                ) or any(not isinstance(receipt, dict) for receipt in item_receipts):
                    raise WechatDigestError("联系人长期信息引用不可验证。")
                if representation_id is not None:
                    if not isinstance(representation_id, str):
                        raise WechatDigestError(
                            "联系人 Representation 引用不可验证。"
                        )
                    if values and not item_receipts:
                        raise WechatDigestError(
                            "联系人长期信息缺少 item-bound Semantic receipt。"
                        )
                    for receipt in item_receipts:
                        if (
                            receipt.get("source_id") != source_id
                            or receipt.get("representation_id")
                            != representation_id
                        ):
                            raise WechatDigestError(
                                "联系人 Semantic receipt 与 Source/Representation 不一致。"
                            )
                        semantic_receipts.append(dict(receipt))
                elif values:
                    raise WechatDigestError(
                        "联系人长期信息缺少 Representation 引用。"
                    )
                for value in values:
                    if value not in ordered_ids:
                        ordered_ids.append(value)
                governance_metrics = item.get("governance_metrics")
                if governance_metrics is not None:
                    if (
                        not isinstance(governance_metrics, dict)
                        or isinstance(governance_metrics.get("turn_count"), bool)
                        or not isinstance(governance_metrics.get("turn_count"), int)
                        or int(governance_metrics["turn_count"]) < 0
                    ):
                        raise WechatDigestError(
                            "联系人 Governance 调用计数不可验证。"
                        )
                    governance_provider_calls += int(
                        governance_metrics["turn_count"]
                    )
        for attachment in attachments:
            if not isinstance(attachment, dict) or not isinstance(
                attachment.get("status"), str
            ):
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
    by_id = {
        item.atomic_information_id: item
        for item in information.list_atomic_information()
    }
    missing_ids = [value for value in ordered_ids if value not in by_id]
    if missing_ids:
        raise WechatDigestError("联系人业务验收所需的长期信息无法完整读回。")
    revisions = tuple(by_id[value] for value in ordered_ids)
    semantic_provider_calls = _contact_semantic_provider_calls(
        workspace, tuple(semantic_receipts)
    )
    synthesis_outcome = ContactSynthesisStore(
        run_store.root / "synthesis", segment_size=synthesis_segment_size
    ).synthesize(
        revisions,
        binding=binding,
        provider=synthesis_provider_factory(),
        authority_ref=authority_ref,
        absolute_cap=absolute_cap,
        semantic_provider_calls=semantic_provider_calls,
        governance_provider_calls=governance_provider_calls,
        resume_provider_calls=resume_provider_calls,
        before_provider_call=before_provider_call,
    )
    synthesis = synthesis_outcome.result

    def evidence_projection(atomic_ids: object) -> list[dict[str, object]]:
        if not isinstance(atomic_ids, list):
            raise WechatDigestError("联系人业务验收 Evidence 引用无效。")
        return [
            {
                "atomic_information_id": atomic_id,
                "source_id": evidence.source_id,
                "locator": evidence.locator,
                "speaker": evidence.speaker,
                "start": evidence.start,
                "end": evidence.end,
                "excerpt": evidence.excerpt,
            }
            for atomic_id in atomic_ids
            for evidence in by_id[str(atomic_id)].source_evidence
        ]

    events = [
        {
            **item,
            "evidence": evidence_projection(
                item.get("evidence_atomic_information_ids")
            ),
        }
        for item in synthesis["events"]
        if isinstance(item, dict)
    ]
    object_candidates = [
        {
            **item,
            "object_id": (related_ids[0] if len(related_ids) == 1 else None),
            "evidence": evidence_projection(
                item.get("evidence_atomic_information_ids")
            ),
        }
        for item in synthesis["object_candidates"]
        if isinstance(item, dict)
        for related_ids in (
            sorted(
                {
                    object_id
                    for atomic_id in item.get("evidence_atomic_information_ids", [])
                    for object_id in by_id[str(atomic_id)].related_object_ids
                }
            ),
        )
    ]
    conflicts = [
        {
            **item,
            "evidence": evidence_projection(
                item.get("evidence_atomic_information_ids")
            ),
        }
        for item in synthesis["conflicts"]
        if isinstance(item, dict)
    ]
    unknowns = [
        {
            **item,
            "evidence": evidence_projection(
                item.get("evidence_atomic_information_ids")
            ),
        }
        for item in synthesis["unknowns"]
        if isinstance(item, dict)
    ]
    if any(
        attachment_counts[field]
        for field in ("missing", "unsupported", "failed", "privacy_blocked")
    ):
        unknowns.append(
            {
                "subject": "附件覆盖",
                "details": "部分附件尚未形成可验证内容。",
                "evidence_atomic_information_ids": [],
                "evidence": [],
            }
        )

    pack = {
        "schema_version": CONTACT_ACCEPTANCE_SCHEMA_VERSION,
        "contact": {
            "display_name": binding.display_name,
            "conversation_key": binding.conversation_key,
            "person_object_id": None,
        },
        "object_candidates": object_candidates,
        "events": events,
        "current_state": synthesis["current_state"],
        "conflicts": conflicts,
        "unknowns": unknowns,
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
        "synthesis_fingerprint": _fingerprint(synthesis),
        "provider_metrics": synthesis_outcome.provider_metrics,
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
        *(
            f"- {item['time_start'] or '时间未知'}｜{item['what_happened']}"
            for item in events
        ),
        "",
        "## 当前状态",
        "",
        *(
            f"- {label}：{value}"
            for field, label in (
                ("completed", "已完成"),
                ("in_progress", "进行中"),
                ("todos", "待办"),
                ("commitments", "承诺"),
                ("blockers", "阻塞"),
            )
            for value in synthesis["current_state"][field]
        ),
        "",
        "## 冲突",
        "",
        *(
            f"- {item['subject']}：{item['details']}"
            for item in (conflicts or [{"subject": "状态", "details": "暂未发现"}])
        ),
        "",
        "## 未知",
        "",
        *(
            f"- {item['subject']}：{item['details']}"
            for item in (unknowns or [{"subject": "状态", "details": "暂未发现"}])
        ),
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
