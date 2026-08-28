"""Private, run-bound Provider attempt ledger for isolated contact Processing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .wechat_contact_synthesis import (
    ContactSynthesisStore,
    require_contact_provider_authority_ref,
)
from .wechat_digest import WechatContactBinding, WechatDigestError

_SCHEMA = "wechat-contact-unified-provider-usage/2.0"
_PRE_ATTEMPT_PROOF_SCHEMA = "wechat-contact-pre-attempt-semantic-proof/1.0"
_CATEGORIES = {"semantic", "governance", "contact_synthesis"}


def _fingerprint(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _read(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or (path.stat().st_mode & 0o777) != 0o600:
        raise WechatDigestError("联系人统一 Provider 用量记录不可验证。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WechatDigestError("联系人统一 Provider 用量记录不可验证。") from exc
    if not isinstance(payload, dict):
        raise WechatDigestError("联系人统一 Provider 用量记录不可验证。")
    return payload


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
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
        if _read(path) != value:
            raise WechatDigestError("联系人统一 Provider ledger 写入后无法读回。")
    finally:
        temporary.unlink(missing_ok=True)


class ContactProviderAttempt:
    """One idempotent durable reservation; callers advance it around Provider I/O."""

    def __init__(self, budget: ContactProviderBudget, ordinal: int) -> None:
        self._budget, self.ordinal = budget, ordinal

    def mark_started(self) -> None:
        self._budget._transition(self.ordinal, "reserved", "started")

    def complete(self) -> None:
        self._budget._transition(self.ordinal, "started", "result")

    def __call__(self) -> None:
        self.complete()


class ContactProviderBudget:
    """The only absolute Provider-cap authority for one frozen contact run."""

    def __init__(self, root: Path, *, binding: WechatContactBinding, authority_ref: str, absolute_cap: int) -> None:
        self.root = Path(root)
        self.binding = binding
        self.contact_identity = {"conversation_key": binding.conversation_key, "provider_conversation_id": binding.provider_conversation_id, "is_group": binding.is_group}
        self.authority_ref = require_contact_provider_authority_ref(authority_ref)
        if isinstance(absolute_cap, bool) or not isinstance(absolute_cap, int) or absolute_cap < 1:
            raise WechatDigestError("联系人模型调用 absolute cap 必须为正数。")
        self.absolute_cap = absolute_cap
        self.path = self.root / "unified-provider-usage.json"
        self.pre_attempt_proofs_root = self.root / "pre-attempt-semantic-proofs"
        self.lock_path = self.root / ".unified-provider-usage.lock"
        ContactSynthesisStore(self.root).ensure_provider_authority(binding=binding, authority_ref=self.authority_ref, absolute_cap=absolute_cap)

    def _scope(self) -> dict[str, object]:
        contact_root = self.root.parent
        try:
            active = json.loads((contact_root / "active.json").read_text(encoding="utf-8"))
            active_run_id = active["active_run_id"]
            if active_run_id is not None and (
                not isinstance(active_run_id, str) or not active_run_id
            ):
                raise TypeError
            existing_scope: dict[str, object] | None = None
            if self.path.exists():
                existing = _read(self.path)
                candidate = existing.get("scope")
                if not isinstance(candidate, dict):
                    raise TypeError
                existing_scope = candidate
            if existing_scope is None:
                if active_run_id is None:
                    raise TypeError
                run_id = active_run_id
            else:
                run_id = existing_scope.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise TypeError
                if active_run_id not in {None, run_id}:
                    raise ValueError
            run_root = contact_root / "runs" / run_id
            plan = json.loads((run_root / "plan.json").read_text(encoding="utf-8"))
            receipt = json.loads((run_root / "run-plan-receipt.json").read_text(encoding="utf-8"))
            if not isinstance(plan, dict) or not isinstance(receipt, dict):
                raise TypeError
            capture, upper = plan.get("capture_fingerprint"), plan.get("upper_bound")
            conversations = plan.get("conversations")
            if (
                not isinstance(capture, str)
                or upper is None
                or not isinstance(conversations, list)
                or len(conversations) != 1
                or not isinstance(conversations[0], dict)
                or conversations[0].get("conversation_key")
                != self.contact_identity["conversation_key"]
            ):
                raise ValueError
            binding = plan.get("contact_binding")
            if binding is not None and binding != self.contact_identity:
                raise ValueError
            if active_run_id is None and existing_scope is not None:
                status = json.loads((run_root / "status.json").read_text(encoding="utf-8"))
                if not isinstance(status, dict) or status.get("state") != "completed":
                    raise ValueError
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WechatDigestError("联系人 Provider scope 无法绑定当前 frozen run。") from exc
        return {"contact_identity": self.contact_identity, "workspace_fingerprint": _fingerprint(str(contact_root.resolve())), "run_id": run_id, "plan_fingerprint": _fingerprint(plan), "plan_receipt_fingerprint": _fingerprint(receipt), "capture_fingerprint": capture, "frozen_upper_bound": upper, "authority_ref": self.authority_ref, "absolute_cap": self.absolute_cap}

    def ensure_pre_attempt_semantic_proof(
        self,
        *,
        representation_id: str,
        privacy_binding: dict[str, object],
        semantic_inventory: dict[str, object],
    ) -> dict[str, object]:
        """Persist and read back the exact contact proof before a legacy-free resume.

        This is deliberately narrower than a Provider reservation: it proves that
        a frozen contact run is still at an unambiguous Provider-0 boundary.
        """

        if (
            not isinstance(representation_id, str)
            or not representation_id
            or set(privacy_binding)
            != {"policy", "policy_version", "route", "receipt_fingerprint"}
            or any(not isinstance(value, str) or not value for value in privacy_binding.values())
            or set(semantic_inventory)
            != {
                "inventory_kind",
                "semantic_run_id",
                "run_receipt_fingerprint",
                "attempt_count",
                "reserved_count",
                "started_count",
                "result_count",
            }
            or semantic_inventory.get("inventory_kind")
            not in {"absent", "run_receipt_only"}
            or any(
                semantic_inventory.get(key) != 0
                for key in ("attempt_count", "reserved_count", "started_count", "result_count")
            )
        ):
            raise WechatDigestError("联系人 pre-attempt Semantic proof 输入无效。")
        scope = self._scope()
        contact_root = self.root.parent
        run_id = scope["run_id"]
        assert isinstance(run_id, str)
        try:
            status = json.loads(
                (contact_root / "runs" / run_id / "status.json").read_text(
                    encoding="utf-8"
                )
            )
            if (
                not isinstance(status, dict)
                or status.get("state") != "failed"
                or status.get("failure_category") != "SemanticHandoffError"
                or status.get("checkpoint_published") is not False
                or not isinstance(status.get("items"), dict)
            ):
                raise ValueError
            matching_items = [
                item
                for item in status["items"].values()
                if isinstance(item, dict)
                and item.get("state") == "represented"
                and item.get("representation_id") == representation_id
            ]
            if len(matching_items) != 1:
                raise ValueError
            authority = ContactSynthesisStore(self.root).read_provider_authority(
                self.binding
            )
            if (
                authority is None
                or authority.get("authority_ref") != self.authority_ref
                or authority.get("absolute_cap") != self.absolute_cap
            ):
                raise ValueError
            if self.path.exists() or self.path.is_symlink():
                usage = _read(self.path)
                attempts = self._validated(usage)
                if attempts:
                    raise ValueError
                ledger = {
                    "state": "durable_zero",
                    "usage_fingerprint": usage.get("usage_fingerprint"),
                }
            else:
                ledger = {"state": "absent_zero", "usage_fingerprint": None}
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WechatDigestError(
                "联系人 pre-attempt Semantic proof 无法绑定当前 frozen run。"
            ) from exc
        payload = {
            "schema_version": _PRE_ATTEMPT_PROOF_SCHEMA,
            "contact_identity": self.contact_identity,
            "workspace_fingerprint": scope["workspace_fingerprint"],
            "run_id": run_id,
            "plan_fingerprint": scope["plan_fingerprint"],
            "plan_receipt_fingerprint": scope["plan_receipt_fingerprint"],
            "capture_fingerprint": scope["capture_fingerprint"],
            "frozen_upper_bound": scope["frozen_upper_bound"],
            "authority_ref": self.authority_ref,
            "absolute_cap": self.absolute_cap,
            "authority_fingerprint": authority["authority_fingerprint"],
            "status_fingerprint": _fingerprint(status),
            "unified_ledger": ledger,
            "representation_id": representation_id,
            "privacy_binding": privacy_binding,
            "semantic_inventory": semantic_inventory,
        }
        proof = {**payload, "proof_fingerprint": _fingerprint(payload)}
        proof_path = self.pre_attempt_proofs_root / run_id / f"{representation_id}.json"
        if proof_path.exists() or proof_path.is_symlink():
            if _read(proof_path) != proof:
                raise WechatDigestError("联系人 pre-attempt Semantic proof 漂移。")
        else:
            _write(proof_path, proof)
        if _read(proof_path) != proof:
            raise WechatDigestError("联系人 pre-attempt Semantic proof 读回失败。")
        return proof

    def _validated(self, current: dict[str, object]) -> list[dict[str, object]]:
        attempts = current.get("attempts")
        payload = {key: value for key, value in current.items() if key != "usage_fingerprint"}
        if (current.get("schema_version") != _SCHEMA or current.get("scope") != self._scope() or current.get("authority_ref") != self.authority_ref or current.get("absolute_cap") != self.absolute_cap or not isinstance(attempts, list) or current.get("usage_fingerprint") != _fingerprint(payload)):
            raise WechatDigestError("联系人统一 Provider 用量记录漂移。")
        for ordinal, attempt in enumerate(attempts, 1):
            raw = {key: value for key, value in attempt.items() if key != "attempt_fingerprint"} if isinstance(attempt, dict) else {}
            state = attempt.get("state") if isinstance(attempt, dict) else None
            timeout_binding = (
                attempt.get("timeout_binding")
                if isinstance(attempt, dict)
                else None
            )
            if (
                not isinstance(attempt, dict)
                or attempt.get("ordinal") != ordinal
                or attempt.get("category") not in _CATEGORIES
                or state not in {"reserved", "started", "result", "unknown", "timeout_no_result", "startup_no_result"}
                or not isinstance(attempt.get("request_binding"), dict)
                or (state == "timeout_no_result") != isinstance(timeout_binding, dict)
                or (
                    state == "startup_no_result"
                    and not isinstance(attempt.get("startup_binding"), dict)
                )
                or attempt.get("attempt_fingerprint") != _fingerprint(raw)
            ):
                raise WechatDigestError("联系人统一 Provider attempt 不可验证。")
        return attempts

    def reserve(self, category: str, request_binding: dict[str, object] | None = None) -> ContactProviderAttempt:
        if category not in _CATEGORIES:
            raise WechatDigestError("联系人 Provider 调用类别无效。")
        binding = dict(request_binding or {"request": "callback-local"})
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                attempts = self._validated(_read(self.path)) if self.path.exists() else []
                if any(item["state"] in {"started", "unknown"} for item in attempts):
                    raise WechatDigestError("联系人模型调用结果未知；禁止自动重试，需新的明确决定。")
                reserved = [item for item in attempts if item["state"] == "reserved"]
                if reserved:
                    candidate = reserved[-1]
                    if candidate["category"] != category or candidate["request_binding"] != binding:
                        raise WechatDigestError("联系人 Provider reservation 与当前请求不一致。")
                    return ContactProviderAttempt(self, int(candidate["ordinal"]))
                if len(attempts) >= self.absolute_cap:
                    raise WechatDigestError("联系人模型调用已达到授权上限；既有结果保持不变。")
                raw = {"ordinal": len(attempts) + 1, "category": category, "state": "reserved", "request_binding": binding}
                attempt = {**raw, "attempt_fingerprint": _fingerprint(raw)}
                payload = {"schema_version": _SCHEMA, "authority_ref": self.authority_ref, "absolute_cap": self.absolute_cap, "scope": self._scope(), "attempts": [*attempts, attempt]}
                _write(self.path, {**payload, "usage_fingerprint": _fingerprint(payload)})
                return ContactProviderAttempt(self, len(attempts) + 1)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def before_call(self, category: str) -> ContactProviderAttempt:
        return self.reserve(category)

    def reconcile_result(self, category: str, request_binding: dict[str, object]) -> None:
        """Close only an exact started attempt after its native result was read back."""
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                current = _read(self.path)
                attempts = self._validated(current)
                matches = [
                    item for item in attempts
                    if item["category"] == category
                    and item["request_binding"] == request_binding
                ]
                if len(matches) != 1:
                    raise WechatDigestError("联系人统一 Provider result binding 不可验证。")
                attempt = matches[0]
                if attempt["state"] == "result":
                    return
                if attempt["state"] != "started":
                    raise WechatDigestError("联系人统一 Provider result 缺少 started attempt。")
                raw = {
                    key: value
                    for key, value in attempt.items()
                    if key != "attempt_fingerprint"
                }
                raw["state"] = "result"
                ordinal = int(attempt["ordinal"])
                attempts[ordinal - 1] = {
                    **raw,
                    "attempt_fingerprint": _fingerprint(raw),
                }
                payload = {
                    key: value
                    for key, value in current.items()
                    if key != "usage_fingerprint"
                }
                payload["attempts"] = attempts
                _write(self.path, {**payload, "usage_fingerprint": _fingerprint(payload)})
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def seal_governance_timeout(
        self, *, timeout_binding: dict[str, object]
    ) -> dict[str, int]:
        """Close one exact contact Governance timeout without a Provider retry."""

        required = {
            "run_id",
            "item_id",
            "atomic_information_fingerprint",
            "governance_receipt_fingerprint",
            "governance_metrics_fingerprint",
        }
        if (
            set(timeout_binding) != required
            or any(
                not isinstance(value, str) or not value
                for value in timeout_binding.values()
            )
        ):
            raise WechatDigestError("联系人 Governance timeout binding 不可验证。")
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                current = _read(self.path)
                attempts = self._validated(current)
                matches = [
                    item
                    for item in attempts
                    if item["category"] == "governance"
                    and all(
                        item["request_binding"].get(key)
                        == timeout_binding[key]
                        for key in (
                            "run_id",
                            "item_id",
                            "atomic_information_fingerprint",
                        )
                    )
                ]
                if len(matches) != 1:
                    raise WechatDigestError(
                        "联系人 Governance timeout 未能唯一绑定 started attempt。"
                    )
                attempt = matches[0]
                if attempt["state"] == "timeout_no_result":
                    if attempt.get("timeout_binding") != timeout_binding:
                        raise WechatDigestError(
                            "联系人 Governance timeout binding 漂移。"
                        )
                elif attempt["state"] == "started":
                    ordinal = int(attempt["ordinal"])
                    raw = {
                        key: value
                        for key, value in attempt.items()
                        if key != "attempt_fingerprint"
                    }
                    raw["state"] = "timeout_no_result"
                    raw["timeout_binding"] = timeout_binding
                    attempts[ordinal - 1] = {
                        **raw,
                        "attempt_fingerprint": _fingerprint(raw),
                    }
                    payload = {
                        key: value
                        for key, value in current.items()
                        if key != "usage_fingerprint"
                    }
                    payload["attempts"] = attempts
                    _write(
                        self.path,
                        {**payload, "usage_fingerprint": _fingerprint(payload)},
                    )
                else:
                    raise WechatDigestError(
                        "联系人 Governance timeout 缺少 started attempt。"
                    )
                total = len(attempts)
                return {
                    "contact_attempt_total": total,
                    "absolute_cap": self.absolute_cap,
                    "remaining": self.absolute_cap - total,
                    "next_contact_ordinal": total + 1,
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def seal_governance_startup_transport_failure(
        self, *, startup_binding: dict[str, object]
    ) -> dict[str, int]:
        """Close the exact contact startup transport attempt without a retry."""

        required = {
            "run_id", "item_id", "atomic_information_fingerprint",
            "governance_receipt_fingerprint", "governance_metrics_fingerprint",
        }
        if (
            set(startup_binding) != required
            or any(not isinstance(value, str) or not value for value in startup_binding.values())
        ):
            raise WechatDigestError("联系人 Governance 启动 transport binding 不可验证。")
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                current = _read(self.path)
                attempts = self._validated(current)
                matches = [
                    item for item in attempts
                    if item["category"] == "governance"
                    and all(item["request_binding"].get(key) == startup_binding[key]
                            for key in ("run_id", "item_id", "atomic_information_fingerprint"))
                ]
                if len(matches) != 1:
                    raise WechatDigestError("联系人 Governance 启动未能唯一绑定 started attempt。")
                attempt = matches[0]
                if attempt["state"] == "startup_no_result":
                    if attempt.get("startup_binding") != startup_binding:
                        raise WechatDigestError("联系人 Governance 启动 binding 漂移。")
                elif attempt["state"] == "started":
                    ordinal = int(attempt["ordinal"])
                    raw = {key: value for key, value in attempt.items() if key != "attempt_fingerprint"}
                    raw["state"] = "startup_no_result"
                    raw["startup_binding"] = startup_binding
                    attempts[ordinal - 1] = {**raw, "attempt_fingerprint": _fingerprint(raw)}
                    payload = {key: value for key, value in current.items() if key != "usage_fingerprint"}
                    payload["attempts"] = attempts
                    _write(self.path, {**payload, "usage_fingerprint": _fingerprint(payload)})
                else:
                    raise WechatDigestError("联系人 Governance 启动缺少 started attempt。")
                total = len(attempts)
                return {"contact_attempt_total": total, "absolute_cap": self.absolute_cap,
                        "remaining": self.absolute_cap - total,
                        "next_contact_ordinal": total + 1}
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _transition(self, ordinal: int, expected: str, target: str) -> None:
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                current = _read(self.path)
                attempts = self._validated(current)
                if ordinal < 1 or ordinal > len(attempts):
                    raise WechatDigestError("联系人统一 Provider attempt 不可验证。")
                attempt = attempts[ordinal - 1]
                if attempt["state"] == target:
                    return
                if attempt["state"] != expected:
                    raise WechatDigestError("联系人统一 Provider attempt 状态不可验证。")
                raw = {key: value for key, value in attempt.items() if key != "attempt_fingerprint"}
                raw["state"] = target
                attempts[ordinal - 1] = {**raw, "attempt_fingerprint": _fingerprint(raw)}
                payload = {key: value for key, value in current.items() if key != "usage_fingerprint"}
                payload["attempts"] = attempts
                _write(self.path, {**payload, "usage_fingerprint": _fingerprint(payload)})
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
