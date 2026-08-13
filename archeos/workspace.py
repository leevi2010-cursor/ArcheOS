"""Local Workspace configuration and safe Codex MCP integration helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKSPACE_DIRECTORIES = ("01_inbox", "02_processing", "03_information", "04_core")
MANAGED_CONFIG_COMMENT = "# ArcheOS managed MCP integration; remove with `archeos integration codex remove`."
MANAGED_TABLE = "[mcp_servers.archeos]"


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "archeos" / "config.toml"


@dataclass(frozen=True)
class WorkspaceConfig:
    workspace: Path
    config_path: Path

    def to_dict(self) -> dict[str, str]:
        return {"workspace": str(self.workspace), "config_path": str(self.config_path)}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid configuration file: {path}") from exc


def load_workspace_config(config_path: Path | None = None) -> WorkspaceConfig:
    path = (config_path or default_config_path()).expanduser()
    payload = _load_toml(path)
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict) or not isinstance(workspace.get("path"), str):
        raise ValueError("configuration does not contain workspace.path")
    return WorkspaceConfig(Path(workspace["path"]).expanduser().resolve(), path)


def initialize_workspace(
    workspace: Path | str,
    *,
    config_path: Path | None = None,
) -> tuple[WorkspaceConfig, bool]:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    created = False
    for name in WORKSPACE_DIRECTORIES:
        target = root / name
        if not target.exists():
            target.mkdir()
            created = True
        elif not target.is_dir():
            raise ValueError(f"workspace path is not a directory: {target}")

    ignore_path = root / ".gitignore"
    if not ignore_path.exists():
        ignore_path.write_text(
            "# Local ArcheOS information is private by default.\n"
            "01_inbox/**\n02_processing/**\n03_information/**\n04_core/**\n",
            encoding="utf-8",
        )
        created = True
    path = (config_path or default_config_path()).expanduser()
    desired = "[workspace]\npath = " + _toml_string(str(root)) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == desired:
        return WorkspaceConfig(root, path), created
    if path.exists():
        current = load_workspace_config(path)
        if current.workspace != root:
            raise ValueError(
                f"configuration already points at another workspace: {current.workspace}"
            )
        return current, created
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desired, encoding="utf-8")
    os.chmod(path, 0o600)
    return WorkspaceConfig(root, path), True


def doctor(config_path: Path | None = None) -> dict[str, object]:
    result: dict[str, object] = {"archeos_version": _version(), "python": os.sys.version.split()[0]}
    try:
        config = load_workspace_config(config_path)
    except ValueError as exc:
        result.update({"healthy": False, "configuration": "unavailable", "error": str(exc)})
        return result

    root = config.workspace
    result["workspace"] = str(root)
    result["workspace_structure"] = all((root / item).is_dir() for item in WORKSPACE_DIRECTORIES)
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".archeos-doctor-", delete=True):
            pass
        result["workspace_read_write"] = True
    except OSError:
        result["workspace_read_write"] = False
    ignore_path = root / ".gitignore"
    result["privacy_boundary"] = (
        "configured" if ignore_path.is_file() and "01_inbox/**" in ignore_path.read_text(encoding="utf-8") else "needs_attention"
    )
    result["context_read_path"] = "available" if (root / "04_core" / "archeos.sqlite3").is_file() else "empty_workspace"
    codex = shutil.which("codex")
    result["codex"] = "unavailable"
    if codex:
        try:
            check = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=5, check=False)
            result["codex"] = "available" if check.returncode == 0 else "not_startable"
        except (OSError, subprocess.TimeoutExpired):
            result["codex"] = "not_startable"
    integration = codex_integration_status(config, None)
    result["codex_mcp"] = integration["state"]
    result["optional_audio_runtime"] = _optional_audio_status()
    result["healthy"] = bool(result["workspace_structure"] and result["workspace_read_write"])
    return result


def _version() -> str:
    from . import __version__

    return __version__


def _optional_audio_status() -> str:
    try:
        import pyannote.audio  # type: ignore[import-not-found] # noqa: F401
    except ImportError:
        return "unavailable"
    return "available"


def default_codex_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"


def _managed_block(config: WorkspaceConfig) -> str:
    executable = shutil.which("archeos") or "archeos"
    return (
        f"{MANAGED_CONFIG_COMMENT}\n{MANAGED_TABLE}\n"
        f"command = {_toml_string(executable)}\n"
        f"args = [\"mcp\", \"serve\", \"--workspace\", {_toml_string(str(config.workspace))}]\n"
        "default_tools_approval_mode = \"writes\"\n"
    )


def _managed_bounds(text: str) -> tuple[int, int] | None:
    start = text.find(MANAGED_CONFIG_COMMENT)
    if start < 0:
        return None
    table = text.find(MANAGED_TABLE, start)
    if table < 0:
        raise ValueError("ArcheOS managed marker has no MCP table")
    next_table = text.find("\n[", table + len(MANAGED_TABLE))
    end = len(text) if next_table < 0 else next_table + 1
    return start, end


def _read_codex_config(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Codex config is invalid; refusing to modify it: {path}") from exc
    return text


def _write_private_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".archeos.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def install_codex_integration(config: WorkspaceConfig, codex_config: Path | None = None) -> str:
    path = (codex_config or default_codex_config_path()).expanduser()
    text = _read_codex_config(path)
    bounds = _managed_bounds(text)
    if bounds is None and MANAGED_TABLE in text:
        raise ValueError("Codex already has an unmanaged 'archeos' MCP server; refusing to replace it")
    if bounds is not None:
        text = text[: bounds[0]] + text[bounds[1] :]
    if text and not text.endswith("\n\n"):
        text += "\n"
    _write_private_config(path, text + _managed_block(config))
    return str(path)


def remove_codex_integration(codex_config: Path | None = None) -> str:
    path = (codex_config or default_codex_config_path()).expanduser()
    text = _read_codex_config(path)
    bounds = _managed_bounds(text)
    if bounds is None:
        return "not_installed"
    _write_private_config(path, (text[: bounds[0]] + text[bounds[1] :]).strip() + "\n")
    return "removed"


def codex_integration_status(config: WorkspaceConfig, codex_config: Path | None) -> dict[str, object]:
    path = (codex_config or default_codex_config_path()).expanduser()
    try:
        text = _read_codex_config(path)
        bounds = _managed_bounds(text)
    except ValueError as exc:
        return {"state": "invalid_config", "config_path": str(path), "error": str(exc)}
    if bounds is None:
        return {"state": "not_installed", "config_path": str(path)}
    block = text[bounds[0] : bounds[1]]
    expected_workspace = f'"{str(config.workspace)}"'
    if MANAGED_TABLE not in block or expected_workspace not in block:
        return {"state": "needs_attention", "config_path": str(path)}
    return {
        "state": "configured",
        "config_path": str(path),
        "server": "archeos",
        "transport": "stdio",
        "restart_required": True,
    }
