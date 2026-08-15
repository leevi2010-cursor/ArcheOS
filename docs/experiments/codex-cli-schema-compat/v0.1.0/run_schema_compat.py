"""Issue #80 public-synthetic Codex CLI schema-compatibility experiment.

This is an experiment-only execution harness.  It validates that the existing
#31/#48 result contract can be serialized with Codex-compatible ``type`` plus
``const`` nodes.  It never opens Sources, Representations, #76 artifacts, or
long-term stores.
"""
from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from archeos.representation_information import (
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationAnalysisUnit,
    RepresentationInformationError,
    RepresentationInformationService,
    _candidate_draft,
    _provider_unit,
    _residue_draft,
    representation_analysis_schema,
)

PROTOCOL_VERSION = "semantic-quality-wechat/1.0"
MAX_MODEL_CALLS = 3
TIMEOUT_SECONDS = 120
VERSION_TIMEOUT_SECONDS = 15
SAFE_TAIL_LIMIT = 1600
SIMPLE_SCHEMA_PATH = ROOT / "schemas/simple-type-const.schema.json"
SEMANTIC_SCHEMA_PATH = ROOT / "schemas/semantic-quality-result.schema.json"


class SchemaCompatibilityError(RuntimeError):
    """The tracked schema is not safe to submit to the current Codex route."""


class ResultError(RuntimeError):
    """The provider response failed the preserved #31/#48 contract."""


class ProcessCleanupError(RuntimeError):
    """A timed-out Codex process group could not be proven absent."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def input_fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact(text: str, limit: int = SAFE_TAIL_LIMIT) -> str:
    """Keep synthetic diagnostics actionable without persisting credentials."""
    patterns = (
        r"(?i)bearer\s+[-a-z0-9._~+/]+",
        r"(?i)(?:sk|rk|pk|token|secret|api[_-]?key|password)\s*[:=]\s*['\"]?[^\s'\"]+",
        r"(?i)\b(?:sk|rk|pk)-[a-z0-9._-]+",
        r"(?i)authorization\s*[:=]\s*[^\s]+",
    )
    value = text
    for pattern in patterns:
        value = re.sub(pattern, "[REDACTED_CREDENTIAL]", value)
    return value[-limit:]


def safe_environment() -> dict[str, str]:
    """Pass only ordinary CLI/auth location variables, never diagnostic env."""
    allowed = (
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_HOME",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["NO_COLOR"] = "1"
    return environment


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise SchemaCompatibilityError("const value is not JSON-compatible")


def preflight_const_types(schema: object, path: str = "$") -> None:
    """Check only the known Codex compatibility rule; never rewrite a schema."""
    if isinstance(schema, list):
        for index, child in enumerate(schema):
            preflight_const_types(child, f"{path}[{index}]")
        return
    if not isinstance(schema, dict):
        return
    if "const" in schema:
        expected = _json_type(schema["const"])
        observed = schema.get("type")
        if observed != expected:
            raise SchemaCompatibilityError(
                f"{path}: const requires explicit matching type {expected!r}"
            )
    for key, child in schema.items():
        if key != "const":
            preflight_const_types(child, f"{path}.{key}")


def draft202012_check(schema: object) -> None:
    """Use the declared ``mcp`` dependency's JSON Schema validator, or stop."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise SchemaCompatibilityError(
            "Draft 2020-12 schema validation is unavailable; install declared core dependencies"
        ) from exc
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise SchemaCompatibilityError("tracked schema is not valid Draft 2020-12 JSON Schema") from exc


def semantic_schema() -> dict[str, object]:
    """Rebuild the #76 strict schema, changing only protocol serialization."""
    base = representation_analysis_schema()
    properties = copy.deepcopy(base["properties"])
    properties.update({
        "protocol_version": {"type": "string", "const": PROTOCOL_VERSION},
        "input_fingerprint": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    })
    return {
        "$schema": base["$schema"],
        "type": "object",
        "additionalProperties": False,
        "required": ["protocol_version", "input_fingerprint", "candidates", "residue"],
        "properties": properties,
    }


def load_schema(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaCompatibilityError("tracked schema is unavailable or invalid JSON") from exc
    if not isinstance(value, dict) or value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise SchemaCompatibilityError("tracked schema is not a Draft 2020-12 JSON Schema object")
    if value.get("type") != "object" or not isinstance(value.get("properties"), dict):
        raise SchemaCompatibilityError("tracked schema root is not a strict object schema")
    draft202012_check(value)
    preflight_const_types(value)
    return value


def preflight() -> dict[str, object]:
    """Deterministic preflight before *any* model call."""
    simple = load_schema(SIMPLE_SCHEMA_PATH)
    semantic = load_schema(SEMANTIC_SCHEMA_PATH)
    expected = semantic_schema()
    if semantic != expected:
        raise SchemaCompatibilityError("tracked semantic schema changed the #31/#48 strict contract")
    if simple.get("properties") != {"answer": {"type": "string", "const": "SYNTHETIC_OK"}}:
        raise SchemaCompatibilityError("minimal smoke schema is not the approved type+const shape")
    return {
        "simple_schema_preflight": "passed",
        "semantic_schema_preflight": "passed",
        "protocol_binding_preserved": True,
        "additional_properties_false_preserved": True,
        "candidate_residue_schema_preserved": True,
    }


def _synthetic_unit(identifier: str, sequence: int, content: str, *, context_ids: tuple[str, ...] = ()) -> RepresentationAnalysisUnit:
    return RepresentationAnalysisUnit(
        unit_id=identifier,
        representation_id="repr_synthetic_issue80_19_anchor",
        source_id="src_synthetic_issue80_19_anchor",
        source_content_hash="sha256:" + "8" * 64,
        representation_kind="wechat_conversation",
        kind="message",
        content=content,
        structured_value=None,
        locator={"synthetic_sequence": sequence},
        context="Public synthetic conversation context.",
        artifact_id="conversation_structure",
        artifact_locator="artifacts/conversation.json",
        analysis_eligible=True,
        context_support_unit_ids=context_ids,
    )


def synthetic_batch(anchor_count: int) -> RepresentationAnalysisBatch:
    if anchor_count not in {2, 19}:
        raise ValueError("synthetic contract supports only 2 or 19 anchors")
    support_id = "unit_synthetic_context_00"
    support = _synthetic_unit(
        support_id,
        0,
        "Public synthetic prior message: delivery scope is under discussion.",
    )
    anchors = tuple(
        _synthetic_unit(
            f"unit_synthetic_anchor_{index:02d}",
            index,
            f"Public synthetic business message {index}: retain this anchor exactly once.",
            context_ids=(support_id,),
        )
        for index in range(1, anchor_count + 1)
    )
    return RepresentationAnalysisBatch(anchor_units=anchors, context_support_units=(support,))


def provider_request(batch: RepresentationAnalysisBatch) -> tuple[dict[str, object], str]:
    """Preserve the #76 request/binding shape while using synthetic units only."""
    payload: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "rules": [
            "Return only the strict schema result.",
            "Account for every anchor with Candidate or Residue.",
            "Candidate must cite an anchor; context is Evidence only when explicitly cited and evidence-capable.",
            "Use Residue for unresolved or insufficiently supported anchors; never invent identity or facts.",
        ],
        "anchor_units": [_provider_unit(unit, role="anchor") for unit in batch.anchor_units],
        "context_support_units": [_provider_unit(unit, role="context_support") for unit in batch.context_support_units],
    }
    fingerprint = input_fingerprint(payload)
    return ({**payload, "input_fingerprint": fingerprint}, fingerprint)


def parse_and_validate(raw: str | None, batch: RepresentationAnalysisBatch, expected_fingerprint: str) -> RepresentationAnalysisResult:
    if not raw or not raw.strip():
        raise ResultError("provider produced no structured result")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResultError("provider result is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"protocol_version", "input_fingerprint", "candidates", "residue"}:
        raise ResultError("result root does not match the strict schema")
    if payload["protocol_version"] != PROTOCOL_VERSION or payload["input_fingerprint"] != expected_fingerprint:
        raise ResultError("protocol or input fingerprint binding failed")
    try:
        result = RepresentationAnalysisResult(
            candidates=tuple(_candidate_draft(item) for item in payload["candidates"]),
            residue=tuple(_residue_draft(item) for item in payload["residue"]),
        )
        # #31 remains the single authority for coverage and context Evidence.
        RepresentationInformationService._validate_batch_result(batch, result)
    except (KeyError, TypeError, ValueError, RepresentationInformationError) as exc:
        raise ResultError("#31 coverage/context validator failed") from exc
    return result


def _write_private_json(path: Path, value: object) -> None:
    path.write_text(canonical_json(value), encoding="utf-8")
    os.chmod(path, 0o600)


def _failure_category(stderr: str, *, exit_code: int | None, result_present: bool, validation: str) -> str | None:
    material = stderr.lower()
    if exit_code not in (0, None):
        if any(token in material for token in ("invalid_json_schema", "json schema", "response_format")):
            return "structured_output_schema_failure"
        if any(token in material for token in ("login", "auth", "credential", "unauthorized", "401", "403")):
            return "runtime_or_auth_failure"
        if any(token in material for token in ("sandbox", "permission denied", "output-last-message", "no such file")):
            return "filesystem_or_sandbox_output_path_failure"
        return "other_reproducible_runtime_failure"
    if not result_present:
        return "filesystem_or_sandbox_output_path_failure"
    if validation != "passed":
        return "structured_output_schema_failure"
    return None


def _process_group_absent(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return True
        raise ProcessCleanupError("process-group absence cannot be verified") from exc
    return False


@dataclass(frozen=True)
class CleanupOutcome:
    status: str
    stdout: str
    stderr: str
    drain_timed_out: bool
    process_group_absent: bool


def _bounded_drain(process: Any) -> tuple[str, str, bool]:
    try:
        stdout, stderr = process.communicate(timeout=5)
        return stdout, stderr, False
    except subprocess.TimeoutExpired as exc:
        return str(exc.output or ""), str(exc.stderr or ""), True


def _signal_process_group(process: Any, sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise ProcessCleanupError("process-group signal failed") from exc


def terminate_process_group(process: Any) -> CleanupOutcome:
    """TERM, reap, verify; then KILL, reap, and finally verify if required."""
    if not isinstance(getattr(process, "pid", None), int):
        raise ProcessCleanupError("timed-out process has no valid process-group id")
    if _process_group_absent(process.pid):
        stdout, stderr, drain_timed_out = _bounded_drain(process)
        return CleanupOutcome("already_absent", stdout, stderr, drain_timed_out, True)
    _signal_process_group(process, signal.SIGTERM)
    stdout, stderr, drain_timed_out = _bounded_drain(process)
    if _process_group_absent(process.pid):
        return CleanupOutcome("terminated", stdout, stderr, drain_timed_out, True)
    _signal_process_group(process, signal.SIGKILL)
    kill_stdout, kill_stderr, kill_drain_timed_out = _bounded_drain(process)
    stdout += kill_stdout
    stderr += kill_stderr
    drain_timed_out = drain_timed_out or kill_drain_timed_out
    if _process_group_absent(process.pid):
        return CleanupOutcome("killed", stdout, stderr, drain_timed_out, True)
    raise ProcessCleanupError("timed-out process group remains after SIGKILL")


@dataclass(frozen=True)
class ProcessOutcome:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    startup_error: str | None
    cleanup_status: str
    cleanup_error: str | None
    drain_timed_out: bool
    process_group_id: int | None
    process_group_absent: bool | None


def run_bounded_process(
    command: list[str],
    *,
    input_text: str | None,
    timeout: int,
    runner: Callable[..., Any] = subprocess.Popen,
) -> ProcessOutcome:
    """Run once and return bounded diagnostics even when startup or timeout fails."""
    try:
        process = runner(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=safe_environment(),
        )
    except OSError as exc:
        return ProcessOutcome(
            "",
            "",
            None,
            False,
            f"{type(exc).__name__}: {exc}",
            "not_started",
            None,
            False,
            None,
            None,
        )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        return ProcessOutcome(
            stdout,
            stderr,
            process.returncode,
            False,
            None,
            "not_required",
            None,
            False,
            process.pid,
            None,
        )
    except subprocess.TimeoutExpired:
        cleanup_status = "failed"
        cleanup_error: str | None = None
        stdout = stderr = ""
        drain_timed_out = True
        process_group_absent = False
        try:
            cleanup = terminate_process_group(process)
            cleanup_status = cleanup.status
            stdout, stderr = cleanup.stdout, cleanup.stderr
            drain_timed_out = cleanup.drain_timed_out
            process_group_absent = cleanup.process_group_absent
        except ProcessCleanupError as cleanup_exc:
            cleanup_error = str(cleanup_exc)
        return ProcessOutcome(
            stdout,
            stderr,
            process.returncode,
            True,
            None,
            cleanup_status,
            cleanup_error,
            drain_timed_out,
            process.pid,
            process_group_absent,
        )


def run_codex_version(
    codex_bin: str, *, timeout: int = VERSION_TIMEOUT_SECONDS, runner: Callable[..., Any] = subprocess.Popen
) -> dict[str, object]:
    outcome = run_bounded_process([codex_bin, "--version"], input_text=None, timeout=timeout, runner=runner)
    if outcome.startup_error is not None:
        category = "runtime_or_auth_failure"
    elif outcome.timed_out:
        category = "other_reproducible_runtime_failure" if outcome.cleanup_error else "transient_or_unreproduced_failure"
    elif outcome.exit_code != 0:
        category = "runtime_or_auth_failure"
    else:
        category = None
    return {
        "status": "passed" if category is None else "failed",
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
        "startup_error": redact(outcome.startup_error or "") or None,
        "stdout_tail": redact(outcome.stdout),
        "stderr_tail": redact(outcome.stderr),
        "cleanup_status": outcome.cleanup_status,
        "cleanup_error": redact(outcome.cleanup_error or "") or None,
        "drain_timed_out": outcome.drain_timed_out,
        "process_group_absent": outcome.process_group_absent,
        "failure_category": category,
        "codex_version": redact((outcome.stdout or outcome.stderr).strip()) or None,
    }


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    label: str
    schema: Path
    prompt: str
    batch: RepresentationAnalysisBatch | None
    expected_fingerprint: str | None


def runs() -> list[RunSpec]:
    small = synthetic_batch(2)
    large = synthetic_batch(19)
    small_request, small_fingerprint = provider_request(small)
    large_request, large_fingerprint = provider_request(large)
    prefix = "You are executing a public synthetic contract test. Do not call tools. Return only JSON matching the supplied schema.\n"
    return [
        RunSpec("run_1", "minimal type+const smoke", SIMPLE_SCHEMA_PATH,
                prefix + "Return SYNTHETIC_OK.", None, None),
        RunSpec("run_2", "corrected #76 small contract", SEMANTIC_SCHEMA_PATH,
                prefix + canonical_json(small_request), small, small_fingerprint),
        RunSpec("run_3", "19 anchors plus context support", SEMANTIC_SCHEMA_PATH,
                prefix + canonical_json(large_request), large, large_fingerprint),
    ]


def run_one(spec: RunSpec, *, codex_bin: str, timeout: int, popen: Callable[..., Any] = subprocess.Popen) -> dict[str, object]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="archeos-issue80-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        schema_path = directory / "schema.json"
        result_path = directory / "result.json"
        schema = load_schema(spec.schema)
        _write_private_json(schema_path, schema)
        command = [
            codex_bin, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
            "--output-schema", str(schema_path), "--output-last-message", str(result_path), "--cd", str(directory), "-",
        ]
        outcome = run_bounded_process(command, input_text=spec.prompt, timeout=timeout, runner=popen)
        result_present = result_path.is_file()
        raw = result_path.read_text(encoding="utf-8", errors="replace") if result_present else ""
        json_status = "not_applicable" if spec.batch is None else "not_present"
        validation = "not_run"
        validation_error = ""
        if not outcome.timed_out and outcome.exit_code == 0:
            try:
                if spec.batch is None:
                    if json.loads(raw) != {"answer": "SYNTHETIC_OK"}:
                        raise ResultError("minimal type+const output mismatch")
                else:
                    json.loads(raw)
                    json_status = "valid"
                    parse_and_validate(raw, spec.batch, str(spec.expected_fingerprint))
                validation = "passed"
            except (json.JSONDecodeError, ResultError) as exc:
                if spec.batch is not None:
                    json_status = "invalid" if isinstance(exc, json.JSONDecodeError) else "valid"
                validation = "failed"
                validation_error = str(exc)
        if outcome.timed_out:
            category = "other_reproducible_runtime_failure" if outcome.cleanup_error else "transient_or_unreproduced_failure"
        elif outcome.startup_error:
            category = "runtime_or_auth_failure"
        else:
            category = _failure_category(
                outcome.stderr,
                exit_code=outcome.exit_code,
                result_present=result_present,
                validation=validation,
            )
        anchor_total = len(spec.batch.anchor_units) if spec.batch else 0
        return {
            "run_id": spec.run_id,
            "label": spec.label,
            "called": True,
            "provider_completed": outcome.exit_code == 0 and not outcome.timed_out,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "stdout_tail": redact(outcome.stdout),
            "stderr_tail": redact(outcome.stderr),
            "startup_error": redact(outcome.startup_error or "") or None,
            "cleanup_status": outcome.cleanup_status,
            "cleanup_error": redact(outcome.cleanup_error or "") or None,
            "drain_timed_out": outcome.drain_timed_out,
            "process_group_absent": outcome.process_group_absent,
            "result_file_present": result_present,
            "json_status": json_status,
            "strict_schema_status": validation,
            "protocol_binding_status": "passed" if validation == "passed" and spec.batch else "not_applicable",
            "input_fingerprint_binding_status": "passed" if validation == "passed" and spec.batch else "not_applicable",
            "anchor_units": anchor_total,
            "accounted_anchor_units": anchor_total if validation == "passed" and spec.batch else 0,
            "unaccounted_anchor_units": 0 if validation == "passed" and spec.batch else anchor_total,
            "context_evidence_validator_status": "passed" if validation == "passed" and spec.batch else "not_applicable",
            "validation_error": redact(validation_error) or None,
            "failure_category": category,
            "latency_seconds": round(time.monotonic() - started, 3),
        }


def run_experiment(*, codex_bin: str, timeout: int, popen: Callable[..., Any] = subprocess.Popen) -> dict[str, object]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    preflight_result = preflight()
    version = run_codex_version(codex_bin, runner=popen)
    report: dict[str, object] = {
        "issue": 80,
        "synthetic_only": True,
        "maximum_model_calls": MAX_MODEL_CALLS,
        "model_calls": 0,
        "codex_version": version["codex_version"],
        "version_exit_code": version["exit_code"],
        "version": version,
        "preflight": preflight_result,
        "runs": [],
        "strict_execution_baseline": "not_recovered",
        "failure_category": "unresolved",
        "production_writes": 0,
    }
    if version["failure_category"] is not None:
        report["failure_category"] = version["failure_category"]
        return report
    for spec in runs():
        if int(report["model_calls"]) >= MAX_MODEL_CALLS:
            raise RuntimeError("model-call budget exhausted")
        result = run_one(spec, codex_bin=codex_bin, timeout=timeout, popen=popen)
        report["runs"].append(result)
        report["model_calls"] = int(report["model_calls"]) + 1
        if result["failure_category"] is not None:
            report["failure_category"] = result["failure_category"]
            break
    completed = {item["run_id"]: item for item in report["runs"]}
    if all(key in completed and completed[key]["failure_category"] is None for key in ("run_1", "run_2", "run_3")):
        report["strict_execution_baseline"] = "recovered"
        report["failure_category"] = None
    return report


def write_report(path: Path, report: Mapping[str, object]) -> None:
    if path.resolve().is_relative_to(REPO_ROOT.resolve()):
        raise ValueError("diagnostic report must stay outside the repository")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private_json(path, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run_experiment(codex_bin=args.codex_bin, timeout=args.timeout)
        write_report(args.report, report)
    except SchemaCompatibilityError as exc:
        print(json.dumps({"preflight": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({key: report[key] for key in ("model_calls", "strict_execution_baseline", "failure_category")}, ensure_ascii=False))
    return 0 if report["strict_execution_baseline"] == "recovered" else 2


if __name__ == "__main__":
    raise SystemExit(main())
