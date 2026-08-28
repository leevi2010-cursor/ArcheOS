"""One private, contact-scoped Provider allowance shared by Processing routes."""

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

_SCHEMA = "wechat-contact-unified-provider-usage/1.0"


def _fingerprint(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


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
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, object]:
    if (
        path.is_symlink()
        or not path.is_file()
        or (path.stat().st_mode & 0o777) != 0o600
    ):
        raise WechatDigestError("联系人统一 Provider 用量记录不可验证。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WechatDigestError("联系人统一 Provider 用量记录不可验证。") from exc
    if not isinstance(value, dict):
        raise WechatDigestError("联系人统一 Provider 用量记录不可验证。")
    return value


class ContactProviderBudget:
    """Reserve one shared call before Semantic or Governance invokes a Provider."""

    def __init__(
        self,
        root: Path,
        *,
        binding: WechatContactBinding,
        authority_ref: str,
        absolute_cap: int,
    ) -> None:
        self.root = Path(root)
        self.authority_ref = require_contact_provider_authority_ref(authority_ref)
        if (
            isinstance(absolute_cap, bool)
            or not isinstance(absolute_cap, int)
            or absolute_cap < 1
        ):
            raise WechatDigestError("联系人模型调用 absolute cap 必须为正数。")
        self.absolute_cap = absolute_cap
        self.path = self.root / "unified-provider-usage.json"
        self.lock_path = self.root / ".unified-provider-usage.lock"
        ContactSynthesisStore(self.root).ensure_provider_authority(
            binding=binding,
            authority_ref=self.authority_ref,
            absolute_cap=absolute_cap,
        )

    def before_call(self, category: str) -> None:
        if category not in {"semantic", "governance"}:
            raise WechatDigestError("联系人 Provider 调用类别无效。")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists() or self.path.is_symlink():
                    current = _read(self.path)
                    entries = current.get("entries")
                    payload = {
                        key: value
                        for key, value in current.items()
                        if key != "usage_fingerprint"
                    }
                    if (
                        current.get("schema_version") != _SCHEMA
                        or current.get("authority_ref") != self.authority_ref
                        or current.get("absolute_cap") != self.absolute_cap
                        or not isinstance(entries, list)
                        or any(
                            item not in {"semantic", "governance"}
                            for item in entries
                        )
                        or current.get("usage_fingerprint")
                        != _fingerprint(payload)
                    ):
                        raise WechatDigestError("联系人统一 Provider 用量记录漂移。")
                else:
                    entries = []
                if len(entries) >= self.absolute_cap:
                    raise WechatDigestError(
                        "联系人模型调用已达到授权上限；既有结果保持不变。"
                    )
                payload = {
                    "schema_version": _SCHEMA,
                    "authority_ref": self.authority_ref,
                    "absolute_cap": self.absolute_cap,
                    "entries": [*entries, category],
                }
                _write(
                    self.path,
                    {**payload, "usage_fingerprint": _fingerprint(payload)},
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
