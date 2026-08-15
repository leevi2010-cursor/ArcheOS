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
    _units_from_representation,
    representation_analysis_schema,
)
from archeos.representation import LocalRepresentationRepository  # noqa: E402
from archeos.source import LocalManagedSourceRepository  # noqa: E402

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


def provider_request(batch: RepresentationAnalysisBatch) -> tuple[dict[str, object], str]:
    """Canonical request; its bound input is exactly the JSON sent via stdin."""
    payload = {"protocol_version": PROTOCOL_VERSION,
             "rules": ["Return only the strict schema result.", "Account for every anchor exactly once with Candidate or Residue.",
                       "Candidate must cite an anchor; context is Evidence only when explicitly cited and evidence-capable.",
                       "Use Residue for unresolved or insufficiently supported anchors; never invent identity or facts."],
             **provider_input(batch)}
    payload["rules"][1] = "Account for every anchor with Candidate or Residue."
    fingerprint = input_fingerprint(payload)
    return ({**payload, "input_fingerprint": fingerprint}, fingerprint)


def require_one_batch(units: tuple[Any, ...]) -> RepresentationAnalysisBatch:
    batches = _analysis_batches(units, 19)
    if len(batches) != 1 or len(batches[0].anchor_units) != 19:
        raise GateError("expected exactly one batch with 19 eligible anchors")
    return batches[0]


def build_real_preflight(representation_id: str, representations: Any, sources: Any) -> RepresentationAnalysisBatch:
    """Read-only #76 gate from an explicit representation ID; no provider or writes."""
    representation = representations.get(representation_id)
    if representation.kind != "wechat_conversation":
        raise GateError("representation kind is not wechat_conversation")
    verification = representations.verify(representation_id)
    if not verification.verified:
        raise GateError("representation verification failed")
    source = sources.get(representation.source_id)
    if source.availability != "available" or source.content_hash != representation.source_content_hash:
        raise GateError("managed source is unavailable or hash-mismatched")
    source_verification = sources.verify(representation.source_id)
    if not source_verification.verified or source_verification.observed_content_hash != representation.source_content_hash:
        raise GateError("managed source verification failed")
    units = _units_from_representation(representation, representations)
    if len(units) != 50:
        raise GateError("expected exactly 50 canonical message units")
    batch = require_one_batch(units)
    # Re-verify immediately before an authorized process can be started.
    if not representations.verify(representation_id).verified or not sources.verify(representation.source_id).verified:
        raise GateError("input changed before provider invocation")
    return batch


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


def _unsafe_symlink_ancestor(path: Path) -> bool:
    for ancestor in (path, *path.parents):
        if ancestor in {Path("/"), Path("/var"), Path("/tmp")}:
            break
        if ancestor.is_symlink():
            return True
    return False


def _write_private_json(path: Path, value: object) -> None:
    if _unsafe_symlink_ancestor(path):
        raise GateError("private artifact path must not traverse a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, raw_temporary = tempfile.mkstemp(prefix=".issue76-", dir=path.parent)
    temporary = Path(raw_temporary)
    os.chmod(temporary, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def consume_marker(marker_path: Path, fingerprint: str, provider_version: str = "codex-cli-unverified") -> None:
    if _unsafe_symlink_ancestor(marker_path):
        raise GateError("marker path must not traverse a symlink")
    marker_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(marker_path.parent, 0o700)
    value = {"issue": 76, "started_at": datetime.now(UTC).isoformat(), "provider_route": "codex-cli",
             "provider_version": provider_version, "input_fingerprint": fingerprint, "status": "started"}
    try:
        fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise GateError("real call has already been consumed") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def update_marker(marker_path: Path, status: str) -> None:
    if status not in {"completed", "failed"}:
        raise GateError("invalid marker status")
    if _unsafe_symlink_ancestor(marker_path):
        raise GateError("marker path must not traverse a symlink")
    value = json.loads(marker_path.read_text(encoding="utf-8"))
    value["status"] = status
    _write_private_json(marker_path, value)


def codex_invocation(schema_path: Path, result_path: Path, cwd: Path) -> list[str]:
    return ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
            "--output-schema", str(schema_path), "--output-last-message", str(result_path), "--cd", str(cwd), "-"]


def read_codex_version() -> str:
    try:
        result = subprocess.run(["codex", "--version"], check=True, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError("codex version preflight failed") from exc
    version = (result.stdout or result.stderr).strip()
    if not version:
        raise GateError("codex version preflight returned no version")
    return version


def _cleanup_process_group(process: Any) -> None:
    def absent() -> bool:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        return False

    if absent():
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, OSError):
            if absent():
                return
            continue
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if absent():
            return
    if not absent():
        raise GateError("provider process group still exists after cleanup")


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
            _cleanup_process_group(process)
            raise GateError("provider timeout") from exc
        except Exception as exc:
            _cleanup_process_group(process)
            raise GateError("provider process failed") from exc
        if process.returncode != 0:
            _cleanup_process_group(process)
            raise GateError("provider exited non-zero")
        try:
            os.chmod(result_path, 0o600)
            raw = result_path.read_text(encoding="utf-8")
        except OSError as exc:
            _cleanup_process_group(process)
            raise GateError("provider produced no result file") from exc
        _cleanup_process_group(process)
        return raw


def run_real_call(batch: RepresentationAnalysisBatch, *, marker_path: Path = MARKER_PATH,
                  review_root: Path | None = None, runner: Callable[..., Any] = subprocess.Popen,
                  provider_version: str = "codex-cli-unverified") -> RepresentationAnalysisResult:
    """The only real-call path.  It consumes the marker before process start."""
    request, fingerprint = provider_request(batch)
    consume_marker(marker_path, fingerprint, provider_version)
    try:
        with tempfile.TemporaryDirectory(prefix="archeos-issue76-result-") as directory:
            private_directory = Path(directory)
            os.chmod(private_directory, 0o700)
            result_path = private_directory / "result.json"
            schema_path = private_directory / "schema.json"
            _write_private_json(schema_path, strict_schema())
            raw = run_codex_cli(canonical_bytes(request).decode("utf-8"), schema_path, result_path, runner=runner)
            result = parse_and_validate(raw, batch, fingerprint)
            if review_root is not None:
                packet = write_local_review_packet(batch, result, review_root)
                if not review_packet_readback(packet) or packet.stat().st_mode & 0o777 != 0o600:
                    raise GateError("local review packet readback failed")
        update_marker(marker_path, "completed")
        return result
    except Exception:
        update_marker(marker_path, "failed")
        raise


def run_authorized_representation(representation_id: str, representations: Any, sources: Any, **kwargs: Any) -> RepresentationAnalysisResult:
    """The explicit real CLI route; callers must already have reviewer authorization."""
    return run_real_call(build_real_preflight(representation_id, representations, sources), **kwargs)


def write_local_review_packet(
    batch: RepresentationAnalysisBatch, result: RepresentationAnalysisResult, directory: Path
) -> Path:
    """Write raw local review material only after strict validation succeeds."""
    if directory.resolve().is_relative_to(REPO_ROOT.resolve()):
        raise GateError("review packet root must be local-only outside the repository")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    accounted: dict[str, list[str]] = {unit.unit_id: [] for unit in batch.anchor_units}
    for label, entries in (("candidate", result.candidates), ("residue", result.residue)):
        for index, entry in enumerate(entries, start=1):
            for unit_id in entry.evidence_unit_ids:
                if unit_id in accounted:
                    accounted[unit_id].append(f"{label}_{index}")
    supplied = {unit.unit_id: unit for unit in (*batch.anchor_units, *batch.context_support_units)}
    packet = {
        "anchor_view": [
            {"unit_id": unit.unit_id, "locator": unit.locator, "content": unit.content,
             "context_support_unit_ids": list(unit.context_support_unit_ids), "accounting": accounted[unit.unit_id]}
            for unit in batch.anchor_units
        ],
        "candidate_view": [
            {"statement": candidate.statement, "semantic_type": candidate.semantic_type,
             "concerns": list(candidate.concerns), "evidence_unit_ids": list(candidate.evidence_unit_ids),
             "context": candidate.context,
             "canonical_evidence": [{"unit_id": unit_id, "content": supplied[unit_id].content,
                                      "locator": supplied[unit_id].locator,
                                      "role": "anchor" if unit_id in accounted else "context_support"}
                                    for unit_id in candidate.evidence_unit_ids]}
            for candidate in result.candidates
        ],
        "context_support_view": [
            {"unit_id": unit.unit_id, "content": unit.content, "locator": unit.locator,
             "evidence_capable": unit.analysis_eligible}
            for unit in batch.context_support_units
        ],
    }
    path = directory / "review-packet.json"
    _write_private_json(path, packet)
    return path


def review_packet_readback(path: Path) -> bool:
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(packet, dict) and all(isinstance(packet.get(key), list) for key in (
        "anchor_view", "candidate_view", "context_support_view"))


def cleanup_local_review_packet(directory: Path) -> None:
    path = directory / "review-packet.json"
    if path.is_symlink() or not path.exists():
        raise GateError("local review packet is unavailable for cleanup")
    path.unlink()
    if path.exists():
        raise GateError("local review packet cleanup could not be verified")


def synthetic_status() -> dict[str, object]:
    return {"issue": 76, "mode": "synthetic", "provider_calls": 0, "provider_completed": False,
            "semantic_quality": "not_assessed_without_valid_output", "real_marker_written": False,
            "recommendation": "fail"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--representation-id")
    parser.add_argument("--representation-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--review-root", type=Path)
    parser.add_argument("--marker-path", type=Path, default=MARKER_PATH)
    args = parser.parse_args()
    if args.real:
        if os.environ.get("REAL_CALL_APPROVED") != "1":
            raise SystemExit("REAL_CALL_APPROVED is required before any real call")
        if not all((args.representation_id, args.representation_root, args.source_root, args.review_root)):
            raise SystemExit("--representation-id, --representation-root, --source-root and --review-root are required")
        run_authorized_representation(args.representation_id, LocalRepresentationRepository(args.representation_root),
                                      LocalManagedSourceRepository(args.source_root), marker_path=args.marker_path,
                                      review_root=args.review_root, provider_version=read_codex_version())
        return 0
    print(json.dumps(synthetic_status(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
