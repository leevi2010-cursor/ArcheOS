#!/usr/bin/env python3
"""Bounded public-synthetic diagnosis for Issue #78.

The six cases deliberately contrast the #66 same-directory invocation with
the #76 split-directory invocation.  This is experiment code only: it never
opens a Source or Representation and never imports or writes production stores.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[3]
ISSUE66_ROOT = REPO_ROOT / "docs/experiments/external-agent-handoff/v0.1.0"
ISSUE66_FIXTURE = ISSUE66_ROOT / "fixtures/synthetic-handoff-package.json"
ISSUE66_SCHEMA = ISSUE66_ROOT / "schemas/external-agent-result.schema.json"
ISSUE76_SCHEMA = REPO_ROOT / "docs/experiments/semantic-quality-wechat/v0.1.0/schemas/result.schema.json"
SIMPLE_SCHEMA = ROOT / "schemas/simple-result.schema.json"
MAX_MODEL_CALLS = 6
PROTOCOL = "semantic-quality-wechat/1.0"
SAFE_TAIL_LIMIT = 1200


class DiagnosisError(RuntimeError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact(text: str, limit: int = SAFE_TAIL_LIMIT) -> str:
    """Keep actionable synthetic output while never persisting likely credentials."""
    patterns = (
        r"(?i)bearer\s+[-a-z0-9._~+/]+",
        r"(?i)(?:sk|rk|pk|token|secret|api[_-]?key|password)\s*[:=]\s*['\"]?[^\s'\"]+",
        r"(?i)\b(?:sk|rk|pk)-[a-z0-9._-]+",
        r"(?i)authorization\s*[:=]\s*[^\s]+",
    )
    safe = text
    for pattern in patterns:
        safe = re.sub(pattern, "[REDACTED_CREDENTIAL]", safe)
    return safe[-limit:]


def command_shape(case_id: str) -> str:
    return {
        "A": "stdin prompt, same private cwd, no output schema",
        "B": "stdin prompt, same private cwd, simple schema and result",
        "C": "#66 public fixture, cwd/schema/result in one directory",
        "D": "#76 contrast: cwd separate from schema/result",
        "E": "#76 strict schema, corrected one-directory small contract",
        "F": "#76 strict schema, corrected one-directory 19-anchor contract",
    }[case_id]


def safe_environment() -> dict[str, str]:
    """Preserve only ordinary CLI/auth location variables, never diagnostics."""
    allowed = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_HOME")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["NO_COLOR"] = "1"
    return environment


def classify(
    *,
    exit_code: int | None,
    timed_out: bool,
    startup_error: bool,
    stderr: str,
    result_present: bool,
    json_status: str,
    strict_status: str,
) -> str | None:
    material = stderr.lower()
    if timed_out:
        return "transient_or_unreproduced_failure"
    if startup_error:
        return "runtime_or_auth_failure"
    if exit_code not in (0, None):
        # A non-fatal MCP/auth sidecar warning can co-occur with a definitive
        # response-format rejection. Prefer the deterministic request error.
        if any(token in material for token in ("invalid_json_schema", "json schema", "response_format")):
            return "structured_output_schema_failure"
        if any(token in material for token in ("login", "auth", "credential", "unauthorized", "401", "403")):
            return "runtime_or_auth_failure"
        if any(token in material for token in ("unknown option", "unexpected argument", "invalid value", "unrecognized")):
            return "cli_flag_or_version_incompatibility"
        if any(token in material for token in ("schema", "structured output")):
            return "structured_output_schema_failure"
        if any(token in material for token in ("sandbox", "permission denied", "output-last-message", "no such file")):
            return "filesystem_or_sandbox_output_path_failure"
        return "other_reproducible_runtime_failure"
    if strict_status == "passed":
        return None
    if not result_present:
        return "filesystem_or_sandbox_output_path_failure"
    if json_status != "valid" or strict_status != "passed":
        return "structured_output_schema_failure"
    return None


def strict_simple(payload: object) -> None:
    if payload != {"answer": "SYNTHETIC_OK"}:
        raise DiagnosisError("simple strict schema mismatch")


def strict_issue66(payload: object, expected_fingerprint: str, ids: set[str]) -> None:
    if not isinstance(payload, dict) or set(payload) != {"protocol_version", "input_fingerprint", "candidates", "residue"}:
        raise DiagnosisError("#66 result root mismatch")
    if payload["protocol_version"] != "external-agent-handoff/1.0" or payload["input_fingerprint"] != expected_fingerprint:
        raise DiagnosisError("#66 result binding mismatch")
    seen: set[str] = set()
    for entries in (payload["candidates"], payload["residue"]):
        if not isinstance(entries, list):
            raise DiagnosisError("#66 result collection mismatch")
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("evidence_unit_ids"), list):
                raise DiagnosisError("#66 evidence shape mismatch")
            for unit_id in item["evidence_unit_ids"]:
                if unit_id not in ids or unit_id in seen:
                    raise DiagnosisError("#66 coverage mismatch")
                seen.add(unit_id)
    if seen != ids:
        raise DiagnosisError("#66 coverage incomplete")


def strict_issue76(payload: object, expected_fingerprint: str, anchors: set[str]) -> None:
    if not isinstance(payload, dict) or set(payload) != {"protocol_version", "input_fingerprint", "candidates", "residue"}:
        raise DiagnosisError("#76 result root mismatch")
    if payload["protocol_version"] != PROTOCOL or payload["input_fingerprint"] != expected_fingerprint:
        raise DiagnosisError("#76 result binding mismatch")
    seen: set[str] = set()
    for label, entries in (("candidate", payload["candidates"]), ("residue", payload["residue"])):
        if not isinstance(entries, list):
            raise DiagnosisError("#76 result collection mismatch")
        for item in entries:
            required = ({"statement", "semantic_type", "concerns", "evidence_unit_ids", "context", "confidence"}
                        if label == "candidate" else {"evidence_unit_ids", "reason_not_absorbed", "future_value_or_uncertainty"})
            if not isinstance(item, dict) or set(item) != required:
                raise DiagnosisError("#76 entry schema mismatch")
            evidence = item["evidence_unit_ids"]
            if not isinstance(evidence, list) or not evidence:
                raise DiagnosisError("#76 missing evidence")
            anchor_refs = [value for value in evidence if value in anchors]
            if label == "candidate" and not anchor_refs:
                raise DiagnosisError("#76 candidate lacks anchor evidence")
            for unit_id in anchor_refs:
                if unit_id in seen:
                    raise DiagnosisError("#76 duplicate anchor coverage")
                seen.add(unit_id)
    if seen != anchors:
        raise DiagnosisError("#76 anchor coverage incomplete")


def issue76_request(anchor_count: int) -> tuple[str, str, Callable[[object], None]]:
    anchors = [
        {"unit_id": f"synthetic-anchor-{index:02d}", "role": "anchor", "content": f"Synthetic business message {index}.",
         "locator": {"synthetic_sequence": index}, "context": "Synthetic conversation context."}
        for index in range(1, anchor_count + 1)
    ]
    support = [{"unit_id": "synthetic-context-01", "role": "context_support", "content": "Synthetic prior turn.",
                "locator": {"synthetic_sequence": 0}, "context": "Synthetic conversation context."}]
    request: dict[str, Any] = {
        "protocol_version": PROTOCOL,
        "rules": [
            "Return only the strict schema result.",
            "Account for every anchor with Candidate or Residue.",
            "Candidate must cite an anchor; use Residue for insufficient support.",
            "Do not invent identity, Relationship, Atomic Information, or World Model state.",
        ],
        "anchor_units": anchors,
        "context_support_units": support,
    }
    bound = fingerprint(request)
    request["input_fingerprint"] = bound
    prompt = """You are an external semantic-analysis executor using public synthetic data. Do not call tools.\nReturn only JSON matching the supplied schema.\n""" + canonical_json(request)
    anchor_ids = {item["unit_id"] for item in anchors}
    return prompt, bound, lambda payload: strict_issue76(payload, bound, anchor_ids)


def issue66_request() -> tuple[str, str, Callable[[object], None]]:
    package = json.loads(ISSUE66_FIXTURE.read_text(encoding="utf-8"))
    expected = fingerprint(package)
    unit_ids = {str(item["unit_id"]) for item in package["units"]}
    request = {"protocol_version": "external-agent-handoff/1.0", "input_fingerprint": expected,
               "analysis_package": package}
    prompt = """You are an External Agent in a public synthetic diagnostic. Do not call tools or read files. Return only JSON matching the supplied schema. Preserve protocol_version and input_fingerprint exactly. Account for every unit exactly once across candidates and residue.\n\nSynthetic request follows:\n""" + canonical_json(package)
    prompt = prompt.rsplit("\n", 1)[0] + "\n" + canonical_json(request)
    return prompt, expected, lambda payload: strict_issue66(payload, expected, unit_ids)


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    schema: Path | None
    prompt: str
    validator: Callable[[object], None] | None
    layout: str


def cases() -> list[CaseSpec]:
    issue66_prompt, _, issue66_validator = issue66_request()
    small_prompt, _, small_validator = issue76_request(2)
    large_prompt, _, large_validator = issue76_request(19)
    return [
        CaseSpec("A", None, "Public synthetic smoke. Reply with exactly SYNTHETIC_OK. Do not call tools.", None, "same"),
        CaseSpec("B", SIMPLE_SCHEMA, "Public synthetic structured-output smoke. Return only the required JSON. Do not call tools.", strict_simple, "same"),
        CaseSpec("C", ISSUE66_SCHEMA, issue66_prompt, issue66_validator, "same"),
        CaseSpec("D", ISSUE66_SCHEMA, issue66_prompt, issue66_validator, "split"),
        CaseSpec("E", ISSUE76_SCHEMA, small_prompt, small_validator, "same"),
        CaseSpec("F", ISSUE76_SCHEMA, large_prompt, large_validator, "same"),
    ]


def run_case(spec: CaseSpec, *, codex_bin: str, timeout: int,
             popen: Callable[..., Any] = subprocess.Popen) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="archeos-issue78-") as outer_raw:
        outer = Path(outer_raw)
        os.chmod(outer, 0o700)
        cwd = outer if spec.layout == "same" else Path(tempfile.mkdtemp(prefix="archeos-issue78-cwd-"))
        try:
            os.chmod(cwd, 0o700)
            result_path = outer / "result.json"
            schema_path: Path | None = None
            if spec.schema is not None:
                schema_path = outer / "schema.json"
                shutil.copyfile(spec.schema, schema_path)
                os.chmod(schema_path, 0o600)
            command = [codex_bin, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(cwd)]
            if schema_path is not None:
                command.extend(["--output-schema", str(schema_path), "--output-last-message", str(result_path)])
            command.append("-")
            timed_out = False
            startup_error: str | None = None
            stdout = stderr = ""
            exit_code: int | None = None
            try:
                process = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, env=safe_environment())
                try:
                    stdout, stderr = process.communicate(input=spec.prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    stdout, stderr = process.communicate()
                exit_code = process.returncode
            except OSError as exc:
                startup_error = f"{type(exc).__name__}: {exc}"
            raw = ""
            result_present = result_path.is_file()
            if spec.schema is None:
                raw = stdout
                result_present = bool(raw.strip())
            elif result_present:
                raw = result_path.read_text(encoding="utf-8", errors="replace")
            json_status = "not_applicable" if spec.validator is None else "not_present"
            strict_status = "not_applicable" if spec.validator is None else "not_run"
            validation_error: str | None = None
            if spec.validator is not None and raw.strip():
                try:
                    payload = json.loads(raw)
                    json_status = "valid"
                    spec.validator(payload)
                    strict_status = "passed"
                except (json.JSONDecodeError, DiagnosisError, TypeError, ValueError) as exc:
                    json_status = "invalid" if isinstance(exc, json.JSONDecodeError) else "valid"
                    strict_status = "failed"
                    validation_error = str(exc)
            elif spec.validator is None and exit_code == 0 and not timed_out and "SYNTHETIC_OK" in raw:
                strict_status = "passed"
            elif spec.validator is None:
                strict_status = "failed"
            category = classify(
                exit_code=exit_code,
                timed_out=timed_out,
                startup_error=startup_error is not None,
                stderr=stderr + "\n" + (startup_error or ""),
                result_present=result_present,
                json_status=json_status,
                strict_status=strict_status,
            )
            return {
                "case_id": spec.case_id,
                "called": True,
                "command_shape": command_shape(spec.case_id),
                "exit_code": exit_code,
                "timed_out": timed_out,
                "startup_error": redact(startup_error or "") or None,
                "stdout_tail": redact(stdout),
                "stderr_tail": redact(stderr),
                "result_file_present": result_present,
                "result_parse_status": json_status,
                "strict_validation_status": strict_status,
                "validation_error": redact(validation_error or "") or None,
                "latency_seconds": round(time.monotonic() - started, 3),
                "failure_category": category,
            }
        finally:
            if cwd != outer:
                shutil.rmtree(cwd, ignore_errors=True)


def run_matrix(*, codex_bin: str, timeout: int, popen: Callable[..., Any] = subprocess.Popen) -> dict[str, Any]:
    version = subprocess.run([codex_bin, "--version"], check=False, capture_output=True, text=True, timeout=15)
    report: dict[str, Any] = {
        "issue": 78,
        "synthetic_only": True,
        "codex_version": redact((version.stdout or version.stderr).strip()) or None,
        "version_exit_code": version.returncode,
        "maximum_model_calls": MAX_MODEL_CALLS,
        "model_calls": 0,
        "cases": [],
        "execution_baseline": "not_recovered",
        "root_cause_classification": "unresolved",
    }
    for spec in cases():
        result = run_case(spec, codex_bin=codex_bin, timeout=timeout, popen=popen)
        report["cases"].append(result)
        report["model_calls"] += 1
        if result["failure_category"] is not None:
            report["root_cause_classification"] = result["failure_category"]
            # A is the only universal prerequisite. A #66 failure makes later
            # comparisons uninformative; an E failure makes the larger F shape
            # uninformative. D failure must continue into E: that is the direct
            # split-directory correction being tested.
            if spec.case_id in {"A", "C", "E"}:
                break
    completed = {item["case_id"]: item for item in report["cases"]}
    corrected = [completed[key] for key in ("E", "F") if key in completed]
    if (completed.get("A", {}).get("failure_category") is None and completed.get("B", {}).get("failure_category") is None
            and any(item["strict_validation_status"] == "passed" for item in corrected)):
        report["execution_baseline"] = "recovered"
        if completed.get("D", {}).get("failure_category") is not None:
            report["root_cause_classification"] = "filesystem_or_sandbox_output_path_failure"
        else:
            report["root_cause_classification"] = "transient_or_unreproduced_failure"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    report = run_matrix(codex_bin=args.codex_bin, timeout=args.timeout)
    args.report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(args.report, 0o600)
    print(json.dumps({key: report[key] for key in ("model_calls", "execution_baseline", "root_cause_classification")}, ensure_ascii=False))
    return 0 if report["execution_baseline"] == "recovered" else 2


if __name__ == "__main__":
    raise SystemExit(main())
