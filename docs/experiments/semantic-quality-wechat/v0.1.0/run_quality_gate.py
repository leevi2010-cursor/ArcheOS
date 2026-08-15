#!/usr/bin/env python3
"""Issue #76 analysis-only semantic-quality gate.

This experiment deliberately has no production entry point.  Real execution is
blocked unless a Reviewer has supplied ``REAL_CALL_APPROVED=1``; tests exercise
its lifecycle with a fake subprocess only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from archeos.representation_information import (  # noqa: E402
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationCandidateDraft,
    RepresentationInformationError,
    RepresentationInformationService,
    RepresentationResidueDraft,
    _analysis_batches,
    _candidate_draft,
    _provider_unit,
    _residue_draft,
    representation_analysis_schema,
)

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schemas" / "result.schema.json"
PROTOCOL_VERSION = "semantic-quality-wechat/1.0"
TIMEOUT_SECONDS = 120
MARKER_PATH = Path.home() / ".archeos/experiments/issue76/real-call-consumed.json"


class GateError(RuntimeError):
    pass


class ResultBindingError(GateError):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def input_fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def strict_schema() -> dict[str, object]:
    base = representation_analysis_schema()
    properties = dict(base["properties"])
    properties.update({
        "protocol_version": {"const": PROTOCOL_VERSION},
        "input_fingerprint": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    })
    return {"$schema": base["$schema"], "type": "object", "additionalProperties": False,
            "required": ["protocol_version", "input_fingerprint", "candidates", "residue"],
            "properties": properties}


def provider_input(batch: RepresentationAnalysisBatch) -> dict[str, object]:
    return {
        "anchor_units": [_provider_unit(unit, role="anchor") for unit in batch.anchor_units],
        "context_support_units": [_provider_unit(unit, role="context_support") for unit in batch.context_support_units],
    }


def require_one_batch(units: tuple[Any, ...]) -> RepresentationAnalysisBatch:
    batches = _analysis_batches(units, 19)
    if len(batches) != 1 or len(batches[0].anchor_units) != 19:
        raise GateError("expected exactly one batch with 19 eligible anchors")
    return batches[0]


def parse_and_validate(raw: str | None, batch: RepresentationAnalysisBatch, expected_fingerprint: str) -> RepresentationAnalysisResult:
    if not raw or not raw.strip():
        raise GateError("provider produced no result")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateError("provider output is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"protocol_version", "input_fingerprint", "candidates", "residue"}:
        raise GateError("result root schema mismatch")
    if payload["protocol_version"] != PROTOCOL_VERSION or payload["input_fingerprint"] != expected_fingerprint:
        raise ResultBindingError("result protocol/fingerprint binding mismatch")
    try:
        result = RepresentationAnalysisResult(
            candidates=tuple(_candidate_draft(item) for item in payload["candidates"]),
            residue=tuple(_residue_draft(item) for item in payload["residue"]),
        )
    except (KeyError, TypeError, ValueError, RepresentationInformationError) as exc:
        raise GateError("result item schema mismatch") from exc
    # #31 remains the sole authority for references, coverage and context eligibility.
    RepresentationInformationService._validate_batch_result(batch, result)
    return result


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def consume_marker(marker_path: Path, fingerprint: str) -> None:
    marker_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(marker_path.parent, 0o700)
    value = {"issue": 76, "started_at": datetime.now(UTC).isoformat(),
             "provider_route": "codex-cli", "input_fingerprint": fingerprint, "status": "started"}
    try:
        fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise GateError("real call has already been consumed") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def update_marker(marker_path: Path, status: str) -> None:
    value = json.loads(marker_path.read_text(encoding="utf-8"))
    value["status"] = status
    _write_private_json(marker_path, value)


def codex_invocation(schema_path: Path, result_path: Path, cwd: Path) -> list[str]:
    return ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
            "--output-schema", str(schema_path), "--output-last-message", str(result_path), "--cd", str(cwd), "-"]


def run_codex_cli(prompt: str, schema_path: Path, result_path: Path, *, runner: Callable[..., Any] = subprocess.Popen) -> str:
    with tempfile.TemporaryDirectory(prefix="archeos-issue76-") as directory:
        cwd = Path(directory)
        os.chmod(cwd, 0o700)
        os.chmod(schema_path, 0o600)
        result_path.touch(mode=0o600, exist_ok=True)
        os.chmod(result_path, 0o600)
        process = runner(codex_invocation(schema_path, result_path, cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _stdout, _stderr = process.communicate(input=prompt, timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise GateError("provider timeout") from exc
        if process.returncode != 0:
            raise GateError("provider exited non-zero")
        try:
            os.chmod(result_path, 0o600)
            return result_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GateError("provider produced no result file") from exc


def run_real_call(batch: RepresentationAnalysisBatch, *, marker_path: Path = MARKER_PATH,
                  runner: Callable[..., Any] = subprocess.Popen) -> RepresentationAnalysisResult:
    """The only real-call path.  It consumes the marker before process start."""
    payload = provider_input(batch)
    fingerprint = input_fingerprint(payload)
    consume_marker(marker_path, fingerprint)
    try:
        with tempfile.TemporaryDirectory(prefix="archeos-issue76-result-") as directory:
            private_directory = Path(directory)
            os.chmod(private_directory, 0o700)
            result_path = private_directory / "result.json"
            schema_path = private_directory / "schema.json"
            _write_private_json(schema_path, strict_schema())
            raw = run_codex_cli(json.dumps(payload, ensure_ascii=False), schema_path, result_path, runner=runner)
            result = parse_and_validate(raw, batch, fingerprint)
        update_marker(marker_path, "completed")
        return result
    except Exception:
        update_marker(marker_path, "failed")
        raise


def write_local_review_packet(
    batch: RepresentationAnalysisBatch, result: RepresentationAnalysisResult, directory: Path
) -> Path:
    """Write raw local review material only after strict validation succeeds."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    accounted: dict[str, list[str]] = {unit.unit_id: [] for unit in batch.anchor_units}
    for label, entries in (("candidate", result.candidates), ("residue", result.residue)):
        for index, entry in enumerate(entries, start=1):
            for unit_id in entry.evidence_unit_ids:
                if unit_id in accounted:
                    accounted[unit_id].append(f"{label}_{index}")
    packet = {
        "anchor_view": [
            {"unit_id": unit.unit_id, "locator": unit.locator, "content": unit.content,
             "context_support_unit_ids": list(unit.context_support_unit_ids), "accounting": accounted[unit.unit_id]}
            for unit in batch.anchor_units
        ],
        "candidate_view": [
            {"statement": candidate.statement, "semantic_type": candidate.semantic_type,
             "concerns": list(candidate.concerns), "evidence_unit_ids": list(candidate.evidence_unit_ids),
             "context": candidate.context}
            for candidate in result.candidates
        ],
    }
    path = directory / "review-packet.json"
    _write_private_json(path, packet)
    return path


def synthetic_status() -> dict[str, object]:
    return {"issue": 76, "mode": "synthetic", "provider_calls": 0, "provider_completed": False,
            "semantic_quality": "not_assessed_without_valid_output", "real_marker_written": False,
            "recommendation": "fail"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    if args.real:
        if os.environ.get("REAL_CALL_APPROVED") != "1":
            raise SystemExit("REAL_CALL_APPROVED is required before any real call")
        raise SystemExit("real mode requires the authorized local representation adapter; not run by preflight")
    print(json.dumps(synthetic_status(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
