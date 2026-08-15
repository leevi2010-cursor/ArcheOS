"""Strict local WeChat Conversation Representation v1."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from ..source.identity import require_managed_source_id
from ..source.models import ManagedSource
from .identity import require_content_hash
from .models import AdapterArtifact, AdapterBuildResult


WECHAT_CONVERSATION_KIND = "wechat_conversation"
WECHAT_CONVERSATION_SCHEMA_VERSION = "1.0"
DEFAULT_CONTEXT_MESSAGES = 1

_EXPORT_FIELDS = {
    "chat",
    "username",
    "is_group",
    "count",
    "offset",
    "limit",
    "start_time",
    "end_time",
    "type",
    "messages",
    "failures",
}
_MESSAGE_PATTERN = re.compile(
    r"^\[(?P<sent_at>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] "
    r"(?P<sender_label>[^:\n]+): (?P<content>[\s\S]*)$"
)
_IMAGE_PATTERN = re.compile(r"\[图片\](?: local_id=\d+)?")
_PLACEHOLDER_TYPES = {
    "[语音]": "voice_placeholder",
    "[视频]": "video_placeholder",
    "[表情]": "sticker_placeholder",
    "[通话]": "call_placeholder",
    "[系统消息]": "system",
    "[撤回了一条消息]": "revoke",
}
_ATTACHMENT_PREFIX_TYPES = {
    "[文件]": "file_placeholder",
    "[小程序]": "mini_program_placeholder",
    "[链接]": "link_placeholder",
    "[链接/文件]": "link_or_file_placeholder",
    "[合并聊天记录]": "merged_chat_placeholder",
}
_UNRESOLVED_REFERENCE_PATTERN = re.compile(
    r"他们|她们|它们|那个项目|这个项目|那个报价|这个报价|这件事|那件事"
)


class WechatConversationError(ValueError):
    """The Managed Source cannot form the approved strict v1 Representation."""


class WechatConversationRepresentationAdapter:
    name = "wechat-conversation"
    version = "1.0.2"
    kind = WECHAT_CONVERSATION_KIND
    supported_media_types = ("application/json",)

    def build(
        self,
        source: ManagedSource,
        materialized_path: Path,
        staging_dir: Path,
        configuration: Mapping[str, object],
    ) -> AdapterBuildResult:
        if configuration:
            raise WechatConversationError(
                "WeChat Conversation v1 does not accept runtime configuration"
            )
        try:
            raw = json.loads(materialized_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatConversationError(
                "WeChat Managed Source must be valid UTF-8 JSON"
            ) from exc
        conversation = build_wechat_conversation_artifact(
            raw,
            source_id=source.source_id,
            source_content_hash=source.content_hash,
        )
        validate_wechat_conversation_artifact(conversation)
        locator = "artifacts/conversation.json"
        target = staging_dir / locator
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                conversation,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        return AdapterBuildResult(
            self.kind,
            (AdapterArtifact("conversation", locator, "application/json"),),
            1.0,
        )


def build_wechat_conversation_artifact(
    value: object,
    *,
    source_id: str,
    source_content_hash: str,
) -> dict[str, object]:
    payload = _strict_export(value)
    parsed_messages = tuple(
        _parse_message(item, sequence, source_id=source_id)
        for sequence, item in enumerate(payload["messages"], start=1)
    )
    participant_labels = tuple(
        dict.fromkeys(message["sender_label"] for message in parsed_messages)
    )
    sent_at_values = tuple(message["sent_at"] for message in parsed_messages)
    external_conversation_id = payload["username"] or "unavailable"
    artifact: dict[str, object] = {
        "schema_version": WECHAT_CONVERSATION_SCHEMA_VERSION,
        "provider": "wechat",
        "source": {
            "source_id": source_id,
            "content_hash": source_content_hash,
        },
        "conversation": {
            "external_conversation_id": external_conversation_id,
            "conversation_type": "group" if payload["is_group"] else "direct",
            "participants": [
                {
                    "sender_label": label,
                    "object_identity": "unavailable",
                }
                for label in participant_labels
            ],
            "time_range": {
                "start": min(sent_at_values),
                "end": max(sent_at_values),
                "timezone": "unavailable",
            },
            "messages": list(parsed_messages),
            "provider_metadata": {
                "chat_label": payload["chat"],
                "username": payload["username"],
                "is_group": payload["is_group"],
                "count": payload["count"],
                "offset": payload["offset"],
                "limit": payload["limit"],
                "start_time": payload["start_time"],
                "end_time": payload["end_time"],
                "type": payload["type"],
                "failures": payload["failures"],
            },
        },
    }
    return artifact


def validate_wechat_conversation_artifact(value: object) -> dict[str, object]:
    root = _exact_object(
        value,
        {"schema_version", "provider", "source", "conversation"},
        "Conversation artifact",
    )
    if root["schema_version"] != WECHAT_CONVERSATION_SCHEMA_VERSION:
        raise WechatConversationError("Conversation schema_version is unsupported")
    if root["provider"] != "wechat":
        raise WechatConversationError("Conversation provider must be wechat")
    source = _exact_object(
        root["source"], {"source_id", "content_hash"}, "Conversation source"
    )
    try:
        require_managed_source_id(source["source_id"])
        require_content_hash(source["content_hash"], field="Conversation content_hash")
    except ValueError as exc:
        raise WechatConversationError(
            "Conversation source identity is invalid"
        ) from exc
    conversation = _exact_object(
        root["conversation"],
        {
            "external_conversation_id",
            "conversation_type",
            "participants",
            "time_range",
            "messages",
            "provider_metadata",
        },
        "Conversation",
    )
    if conversation["conversation_type"] not in {"direct", "group", "unknown"}:
        raise WechatConversationError("conversation_type is invalid")
    _non_empty(conversation["external_conversation_id"], "external_conversation_id")
    participants = _array(conversation["participants"], "participants")
    labels: set[str] = set()
    for participant in participants:
        item = _exact_object(
            participant,
            {"sender_label", "object_identity"},
            "participant",
        )
        label = _non_empty(item["sender_label"], "participant sender_label")
        if label in labels or item["object_identity"] != "unavailable":
            raise WechatConversationError(
                "participants must be unique labels without Object binding"
            )
        labels.add(label)
    messages = _array(conversation["messages"], "messages")
    if not messages:
        raise WechatConversationError("Conversation must contain messages")
    message_by_sequence: dict[int, dict[str, object]] = {}
    for expected_sequence, message in enumerate(messages, start=1):
        item = _validate_message(message, source["source_id"], expected_sequence)
        message_by_sequence[expected_sequence] = item
        if item["sender_label"] not in labels:
            raise WechatConversationError("message sender_label is not a participant")
    time_range = _exact_object(
        conversation["time_range"], {"start", "end", "timezone"}, "time_range"
    )
    sent_at_values = [message["sent_at"] for message in message_by_sequence.values()]
    if (
        time_range["start"] != min(sent_at_values)
        or time_range["end"] != max(sent_at_values)
        or time_range["timezone"] != "unavailable"
    ):
        raise WechatConversationError("time_range does not match messages")
    provider_metadata = _validate_provider_metadata(conversation["provider_metadata"])
    expected_external_id = provider_metadata["username"] or "unavailable"
    if conversation["external_conversation_id"] != expected_external_id:
        raise WechatConversationError(
            "external_conversation_id does not match provider metadata"
        )
    expected_conversation_type = "group" if provider_metadata["is_group"] else "direct"
    if conversation["conversation_type"] != expected_conversation_type:
        raise WechatConversationError(
            "conversation_type does not match provider metadata"
        )
    if provider_metadata["count"] != len(messages):
        raise WechatConversationError("provider count does not match messages")
    return root


def wechat_conversation_analysis_rows(
    value: object,
) -> Iterable[
    tuple[
        str,
        str | None,
        object | None,
        object,
        str,
        bool,
        str | None,
        tuple[object, ...],
    ]
]:
    root = validate_wechat_conversation_artifact(value)
    conversation = root["conversation"]
    assert isinstance(conversation, dict)
    messages = conversation["messages"]
    assert isinstance(messages, list)
    message_by_sequence = {
        message["sequence"]: message
        for message in messages
        if isinstance(message, dict)
    }
    for sequence in sorted(message_by_sequence):
        anchor = message_by_sequence[sequence]
        eligible = anchor["message_type"] == "text" and bool(
            str(anchor["visible_content"]).strip()
        )
        classification, reason = _classification(str(anchor["message_type"]), eligible)
        context_sequences = (
            tuple(
                nearby
                for nearby in range(
                    max(1, sequence - DEFAULT_CONTEXT_MESSAGES),
                    min(len(messages), sequence + DEFAULT_CONTEXT_MESSAGES) + 1,
                )
                if nearby != sequence
            )
            if eligible
            else ()
        )
        structured = {
            "sender_label": anchor["sender_label"],
            "sent_at": anchor["sent_at"],
            "message_type": anchor["message_type"],
            "processing_classification": classification,
            "external_message_id": anchor["external_message_id"],
            "reply_to": anchor["reply_to"],
            "attachment_refs": anchor["attachment_refs"],
            "unresolved_references": anchor["unresolved_references"],
        }
        yield (
            "wechat_message",
            anchor["visible_content"],
            structured,
            anchor["source_locator"],
            "WeChat message; bounded context support is supplied separately.",
            eligible,
            reason,
            tuple(
                message_by_sequence[nearby]["source_locator"]
                for nearby in context_sequences
            ),
        )


def wechat_conversation_metrics(value: object) -> dict[str, int]:
    root = validate_wechat_conversation_artifact(value)
    conversation = root["conversation"]
    assert isinstance(conversation, dict)
    messages = conversation["messages"]
    participants = conversation["participants"]
    assert isinstance(messages, list)
    assert isinstance(participants, list)
    locators = [message["source_locator"] for message in messages]
    classifications = [
        _classification(
            str(message["message_type"]),
            message["message_type"] == "text"
            and bool(str(message["visible_content"]).strip()),
        )
        for message in messages
    ]
    eligible_sequences = [
        index
        for index, (classification, _reason) in enumerate(classifications, start=1)
        if classification == "business_semantic"
    ]
    return {
        "message_total": len(messages),
        "replayable_messages": sum(
            locator["sequence"] == sequence
            for sequence, locator in enumerate(locators, start=1)
        ),
        "stable_locator_failures": len(messages)
        - len({(locator["source_id"], locator["sequence"]) for locator in locators}),
        "participant_object_bindings": sum(
            participant["object_identity"] != "unavailable"
            for participant in participants
        ),
        "analysis_eligible": len(eligible_sequences),
        "context_support_references": sum(
            min(len(messages), sequence + DEFAULT_CONTEXT_MESSAGES)
            - max(1, sequence - DEFAULT_CONTEXT_MESSAGES)
            for sequence in eligible_sequences
        ),
        "excluded_or_unsupported": len(messages) - len(eligible_sequences),
        "unresolved_reference_count": sum(
            len(message["unresolved_references"]) for message in messages
        ),
        "missing_external_message_id_count": sum(
            message["external_message_id"] == "unavailable" for message in messages
        ),
        "missing_reply_metadata_count": sum(
            message["reply_to"] == "unavailable" for message in messages
        ),
        "missing_attachment_metadata_count": sum(
            message["metadata_availability"]["attachments"] == "unavailable"
            for message in messages
        ),
    }


def _strict_export(value: object) -> dict[str, object]:
    payload = _exact_object(value, _EXPORT_FIELDS, "WeChat export")
    _non_empty(payload["chat"], "chat")
    if not isinstance(payload["username"], str):
        raise WechatConversationError("username must be a string")
    if not isinstance(payload["is_group"], bool):
        raise WechatConversationError("is_group must be a boolean")
    messages = _array(payload["messages"], "messages")
    if not messages or any(not isinstance(item, str) for item in messages):
        raise WechatConversationError("messages must be a non-empty array of strings")
    count = _integer(payload["count"], "count", minimum=0)
    _integer(payload["offset"], "offset", minimum=0)
    _integer(payload["limit"], "limit", minimum=1)
    if count != len(messages):
        raise WechatConversationError("count must equal the number of messages")
    for field in ("start_time", "end_time", "type"):
        if payload[field] is not None and not isinstance(payload[field], str):
            raise WechatConversationError(f"{field} must be a string or null")
    if payload["failures"] is not None:
        raise WechatConversationError("exports with failures are not accepted")
    return payload


def _parse_message(
    value: object, sequence: int, *, source_id: str
) -> dict[str, object]:
    if not isinstance(value, str):
        raise WechatConversationError("message must be a string")
    match = _MESSAGE_PATTERN.fullmatch(value)
    if match is None:
        raise WechatConversationError(
            f"message {sequence} does not match the strict export format"
        )
    sent_at = match.group("sent_at")
    try:
        datetime.strptime(sent_at, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise WechatConversationError(
            f"message {sequence} has an invalid timestamp"
        ) from exc
    sender_label = match.group("sender_label")
    content = match.group("content")
    message_type = _message_type(content)
    references = [
        {"surface": found.group(0), "resolved_object_id": "unavailable"}
        for found in _UNRESOLVED_REFERENCE_PATTERN.finditer(content)
    ]
    return {
        "sequence": sequence,
        "external_message_id": "unavailable",
        "sender_label": sender_label,
        "sent_at": sent_at,
        "message_type": message_type,
        "visible_content": content,
        "reply_to": "unavailable",
        "attachment_refs": [],
        "source_locator": {"source_id": source_id, "sequence": sequence},
        "unresolved_references": references,
        "metadata_availability": {
            "external_message_id": "unavailable",
            "reply": "unavailable",
            "attachments": "unavailable",
            "participant_identity": "unavailable",
        },
    }


def _message_type(content: str) -> str:
    if not content.strip():
        return "empty"
    if _IMAGE_PATTERN.fullmatch(content):
        return "image_placeholder"
    if content in _PLACEHOLDER_TYPES:
        return _PLACEHOLDER_TYPES[content]
    for prefix, message_type in _ATTACHMENT_PREFIX_TYPES.items():
        if content == prefix or content.startswith(prefix + " "):
            return message_type
    if content.startswith("[系统]"):
        return "system"
    if content.startswith("[撤回]"):
        return "revoke"
    return "text"


def _classification(message_type: str, eligible: bool) -> tuple[str, str | None]:
    if eligible:
        return "business_semantic", None
    if message_type in {"system", "revoke"}:
        return "conversation_context", "SYSTEM_OR_REVOKE_CONTEXT_ONLY"
    if message_type == "empty":
        return "noise", "EMPTY_MESSAGE"
    if message_type.endswith("_placeholder"):
        return "attachment_reference", "MESSAGE_METADATA_UNAVAILABLE"
    return "unsupported", "MESSAGE_TYPE_UNSUPPORTED"


def _validate_message(
    value: object, source_id: object, expected_sequence: int
) -> dict[str, object]:
    item = _exact_object(
        value,
        {
            "sequence",
            "external_message_id",
            "sender_label",
            "sent_at",
            "message_type",
            "visible_content",
            "reply_to",
            "attachment_refs",
            "source_locator",
            "unresolved_references",
            "metadata_availability",
        },
        "message",
    )
    if item["sequence"] != expected_sequence:
        raise WechatConversationError("message sequence is not contiguous")
    if item["external_message_id"] != "unavailable":
        raise WechatConversationError("external_message_id must be unavailable in v1")
    _non_empty(item["sender_label"], "message sender_label")
    sent_at = _non_empty(item["sent_at"], "message sent_at")
    try:
        datetime.strptime(sent_at, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise WechatConversationError("message sent_at is invalid") from exc
    message_type = _non_empty(item["message_type"], "message_type")
    if not isinstance(item["visible_content"], str):
        raise WechatConversationError("visible_content must be a string")
    if message_type != _message_type(item["visible_content"]):
        raise WechatConversationError("message_type does not match visible_content")
    if item["reply_to"] != "unavailable":
        raise WechatConversationError("reply_to must be unavailable in v1")
    if item["attachment_refs"] != []:
        raise WechatConversationError(
            "attachment_refs require separate Managed Sources"
        )
    locator = _exact_object(
        item["source_locator"], {"source_id", "sequence"}, "source_locator"
    )
    if locator != {"source_id": source_id, "sequence": expected_sequence}:
        raise WechatConversationError("source_locator is not source-local and stable")
    references = _array(item["unresolved_references"], "unresolved_references")
    expected_surfaces = [
        match.group(0)
        for match in _UNRESOLVED_REFERENCE_PATTERN.finditer(item["visible_content"])
    ]
    observed_surfaces: list[str] = []
    for reference in references:
        unresolved = _exact_object(
            reference,
            {"surface", "resolved_object_id"},
            "unresolved_reference",
        )
        observed_surfaces.append(
            _non_empty(unresolved["surface"], "unresolved reference surface")
        )
        if unresolved["resolved_object_id"] != "unavailable":
            raise WechatConversationError("unresolved references cannot bind Objects")
    if observed_surfaces != expected_surfaces:
        raise WechatConversationError("unresolved references are not deterministic")
    availability = _exact_object(
        item["metadata_availability"],
        {"external_message_id", "reply", "attachments", "participant_identity"},
        "metadata_availability",
    )
    if set(availability.values()) != {"unavailable"}:
        raise WechatConversationError("missing metadata must remain unavailable")
    return item


def _validate_provider_metadata(value: object) -> dict[str, object]:
    metadata = _exact_object(
        value,
        {
            "chat_label",
            "username",
            "is_group",
            "count",
            "offset",
            "limit",
            "start_time",
            "end_time",
            "type",
            "failures",
        },
        "provider_metadata",
    )
    _non_empty(metadata["chat_label"], "provider chat_label")
    if not isinstance(metadata["username"], str):
        raise WechatConversationError("provider username must be a string")
    if not isinstance(metadata["is_group"], bool):
        raise WechatConversationError("provider is_group must be a boolean")
    _integer(metadata["count"], "provider count", minimum=0)
    _integer(metadata["offset"], "provider offset", minimum=0)
    _integer(metadata["limit"], "provider limit", minimum=1)
    for field in ("start_time", "end_time", "type"):
        if metadata[field] is not None and not isinstance(metadata[field], str):
            raise WechatConversationError(f"provider {field} is invalid")
    if metadata["failures"] is not None:
        raise WechatConversationError("provider failures must be null")
    return metadata


def _exact_object(value: object, keys: set[str], field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise WechatConversationError(f"{field} does not match the strict contract")
    return value


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise WechatConversationError(f"{field} must be an array")
    return value


def _non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WechatConversationError(f"{field} must be a non-empty string")
    return value


def _integer(value: object, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WechatConversationError(f"{field} must be an integer >= {minimum}")
    return value
