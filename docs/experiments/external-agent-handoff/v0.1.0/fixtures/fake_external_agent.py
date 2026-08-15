#!/usr/bin/env python3
"""Public synthetic child used to exercise the Issue #66 failure matrix."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _valid_result(request: dict[str, object]) -> dict[str, object]:
    package = request["analysis_package"]
    assert isinstance(package, dict)
    units = package["units"]
    assert isinstance(units, list)
    candidates = []
    for index, unit in enumerate(units, start=1):
        assert isinstance(unit, dict)
        candidates.append(
            {
                "statement": f"公开合成结论 {index}。",
                "semantic_type": "observation",
                "concerns": ["公开合成对象"],
                "evidence_unit_ids": [unit["unit_id"]],
                "context": "Issue #66 synthetic gate",
                "confidence": 1.0,
            }
        )
    return {
        "protocol_version": request["protocol_version"],
        "input_fingerprint": request["input_fingerprint"],
        "candidates": candidates,
        "residue": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--leaked-value")
    args = parser.parse_args()

    request = json.load(sys.stdin)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.25)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if args.mode == "timeout":
        time.sleep(10)
        return 0
    child.wait()
    if args.mode == "runtime_failure":
        return 7
    if args.mode == "no_result":
        return 0

    result_path = Path(args.result_file)
    if args.mode == "empty_result":
        result_path.write_text("   ", encoding="utf-8")
        return 0
    if args.mode == "invalid_json":
        result_path.write_text("{", encoding="utf-8")
        return 0

    result = _valid_result(request)
    candidates = result["candidates"]
    assert isinstance(candidates, list)
    if args.mode == "unknown_ref":
        candidates[0]["evidence_unit_ids"] = ["unit_unknown"]
    elif args.mode == "duplicate_ref":
        candidates[1]["evidence_unit_ids"] = candidates[0]["evidence_unit_ids"]
    elif args.mode == "incomplete_coverage":
        candidates.pop()
    elif args.mode == "wrong_fingerprint":
        result["input_fingerprint"] = "sha256:" + "0" * 64
    elif args.mode not in {"valid", "argv_leak", "env_leak"}:
        raise ValueError("unsupported synthetic mode")

    result_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    os.chmod(result_path, 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
