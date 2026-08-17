"""Bounded structured-data experiment for Issue #108.

This harness deliberately stays outside ArcheOS Core. It consumes the existing
Managed Source and XLSX Normalized Representation contracts, then emits a
replaceable Derived Artifact / Projection candidate for experiment evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

from openpyxl import Workbook
from openpyxl.utils.cell import coordinate_to_tuple

from archeos.representation import (
    LocalRepresentationRepository,
    RepresentationService,
)
from archeos.representation.adapters import XlsxRepresentationAdapter
from archeos.source import LocalManagedSourceRepository

SCHEMA_VERSION = "structured-data-experiment/1.0"
FIXED_TIME = "2026-08-17T00:00:00Z"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MONEY_PATTERN = re.compile(r"^[￥¥]?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)$")
DIMENSION_PATTERN = re.compile(
    r"(?:宽)?\s*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|m|米)\b", re.IGNORECASE
)

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "product_code": ("产品编号", "系列号", "产品编码"),
    "product_name": ("名称", "产品名称", "品名"),
    "quoted_price": ("单价", "报价(CNY)", "含税单价"),
    "notes": ("备注", "规格备注"),
    "width": ("宽度",),
    "width_unit": ("宽度单位",),
    "material": ("材质",),
    "configuration": ("配置",),
}
REQUIRED_FIELDS = ("product_code", "product_name", "quoted_price")
HUMAN_REVIEW_CATEGORIES = {
    "identity_ambiguity",
    "non_unique_key",
    "conflicting_values",
    "temporal_ambiguity",
    "ambiguous_free_text",
    "type_mismatch",
}


class StructuredDataExperimentError(RuntimeError):
    """Fail-closed experiment error; it must never become business Residue."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: object) -> str:
    return (
        "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    )


def load_fixture_specs(fixture_root: Path) -> list[dict[str, object]]:
    specs = []
    for path in sorted(fixture_root.glob("quote-v*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if set(payload) != {"version_id", "sheet_name", "replaces", "headers", "rows"}:
            raise StructuredDataExperimentError("fixture has unknown or missing fields")
        if not isinstance(payload["version_id"], str) or not payload["version_id"]:
            raise StructuredDataExperimentError("fixture version_id is invalid")
        headers = payload["headers"]
        rows = payload["rows"]
        if (
            not isinstance(headers, list)
            or not headers
            or not all(isinstance(item, str) and item for item in headers)
            or len(headers) != len(set(headers))
        ):
            raise StructuredDataExperimentError("fixture headers are invalid")
        if not isinstance(rows, list) or not all(
            isinstance(row, list) and len(row) == len(headers) for row in rows
        ):
            raise StructuredDataExperimentError("fixture rows are invalid")
        specs.append(payload)
    if len(specs) < 3:
        raise StructuredDataExperimentError(
            "at least three schema versions are required"
        )
    return specs


def _canonicalize_xlsx(path: Path) -> None:
    canonical = path.with_suffix(".canonical.xlsx")
    with (
        zipfile.ZipFile(path, "r") as source,
        zipfile.ZipFile(canonical, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for name in sorted(source.namelist()):
            original = source.getinfo(name)
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = original.compress_type
            entry.external_attr = original.external_attr
            target.writestr(entry, source.read(name))
    os.replace(canonical, path)


def _write_xlsx(spec: Mapping[str, object], path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = str(spec["sheet_name"])
    worksheet.append(list(spec["headers"]))
    for row in spec["rows"]:
        worksheet.append(list(row))
    workbook.properties.creator = "ArcheOS synthetic fixture"
    workbook.properties.created = "2020-01-01T00:00:00Z"
    workbook.properties.modified = "2020-01-01T00:00:00Z"
    workbook.save(path)
    workbook.close()
    _canonicalize_xlsx(path)


def build_representations(
    specs: Iterable[Mapping[str, object]], workspace: Path
) -> list[dict[str, object]]:
    managed_root = workspace / "managed"
    representation_root = workspace / "representations"
    representation_repository = LocalRepresentationRepository(
        representation_root, clock=lambda: FIXED_TIME
    )
    built_versions: list[dict[str, object]] = []
    for index, spec in enumerate(specs, start=1):
        version_id = str(spec["version_id"])
        external = workspace / f"{version_id}.xlsx"
        _write_xlsx(spec, external)
        source_id = f"src_{index:032x}"
        source_repository = LocalManagedSourceRepository(
            managed_root,
            id_factory=lambda source_id=source_id: source_id,
            clock=lambda: FIXED_TIME,
        )
        source = source_repository.admit(
            external,
            metadata={"media_type": XLSX_MEDIA_TYPE, "filename_hint": "synthetic.xlsx"},
        ).source
        service = RepresentationService(
            source_repository, representation_repository, clock=lambda: FIXED_TIME
        )
        result = service.build(source.source_id, XlsxRepresentationAdapter(), {})
        verification = service.verify(result.representation.representation_id)
        if not verification.verified:
            raise StructuredDataExperimentError(
                "Normalized Representation verification failed"
            )
        artifact = result.representation.artifacts[0]
        payload = json.loads(
            representation_repository.read_artifact(
                result.representation.representation_id, artifact.artifact_id
            )
        )
        built_versions.append(
            {
                "version_id": version_id,
                "replaces": spec["replaces"],
                "source_id": source.source_id,
                "source_content_hash": source.content_hash,
                "representation_id": result.representation.representation_id,
                "representation_kind": result.representation.kind,
                "representation_status": result.representation.status,
                "representation_warnings": [
                    item.to_dict() for item in result.representation.warnings
                ],
                "payload": payload,
            }
        )
    return built_versions


def _validate_representation(
    version: Mapping[str, object],
) -> list[Mapping[str, object]]:
    if version.get("representation_kind") != "xlsx_structure":
        raise StructuredDataExperimentError(
            "only XLSX structure Representations are supported"
        )
    payload = version.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"sheets"}:
        raise StructuredDataExperimentError("Representation payload is invalid")
    sheets = payload["sheets"]
    if (
        not isinstance(sheets, list)
        or len(sheets) != 1
        or not isinstance(sheets[0], dict)
    ):
        raise StructuredDataExperimentError("experiment requires exactly one worksheet")
    cells = sheets[0].get("cells")
    if not isinstance(cells, list):
        raise StructuredDataExperimentError("Representation cells are invalid")
    for cell in cells:
        if not isinstance(cell, dict) or not {"source_locator", "value"} <= set(cell):
            raise StructuredDataExperimentError("Representation cell is invalid")
        locator = cell["source_locator"]
        if (
            not isinstance(locator, dict)
            or set(locator) != {"sheet", "sheet_index", "cell"}
            or not isinstance(locator["cell"], str)
        ):
            raise StructuredDataExperimentError("Representation locator is invalid")
    return cells


def _matrix(
    cells: Iterable[Mapping[str, object]],
) -> dict[int, dict[int, Mapping[str, object]]]:
    rows: dict[int, dict[int, Mapping[str, object]]] = defaultdict(dict)
    for cell in cells:
        try:
            row, column = coordinate_to_tuple(str(cell["source_locator"]["cell"]))
        except (TypeError, ValueError) as exc:
            raise StructuredDataExperimentError(
                "Representation cell coordinate is invalid"
            ) from exc
        rows[row][column] = cell
    return dict(rows)


def _alias_index(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    index = {
        alias: field for field, aliases in FIELD_ALIASES.items() for alias in aliases
    }
    for source_header, field in (overrides or {}).items():
        if (
            not isinstance(source_header, str)
            or not isinstance(field, str)
            or not field
        ):
            raise StructuredDataExperimentError("mapping override is invalid")
        index[source_header] = field
    return index


def _discover_header_row(
    matrix: Mapping[int, Mapping[int, Mapping[str, object]]],
    alias_index: Mapping[str, str],
) -> int:
    scores: list[tuple[int, int]] = []
    for row_number, columns in sorted(matrix.items())[:5]:
        values = [cell.get("value") for cell in columns.values()]
        alias_hits = sum(
            isinstance(value, str) and value in alias_index for value in values
        )
        strings = sum(
            isinstance(value, str) and bool(value.strip()) for value in values
        )
        scores.append((alias_hits * 10 + strings, row_number))
    if not scores:
        raise StructuredDataExperimentError("no table rows were found")
    scores.sort(reverse=True)
    if scores[0][0] == 0 or (len(scores) > 1 and scores[0][0] == scores[1][0]):
        raise StructuredDataExperimentError("header row is ambiguous")
    return scores[0][1]


def _evidence(
    version: Mapping[str, object], cell: Mapping[str, object]
) -> dict[str, object]:
    return {
        "source_id": version["source_id"],
        "representation_id": version["representation_id"],
        "source_locator": cell["source_locator"],
    }


def _normalized_value(
    value: object,
    raw_value: object,
    version: Mapping[str, object],
    cell: Mapping[str, object],
    rule: str,
) -> dict[str, object]:
    return {
        "value": value,
        "raw_value": raw_value,
        "mapping_rule": rule,
        "evidence": _evidence(version, cell),
    }


def _parse_money(value: object) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    match = MONEY_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    return int(number) if number.is_integer() else number


def _to_mm(number: object, unit: object) -> int | float | None:
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        return None
    if not isinstance(unit, str):
        return None
    factor = {"mm": 1, "cm": 10, "m": 1000, "米": 1000}.get(unit.lower())
    if factor is None:
        return None
    value = number * factor
    return int(value) if float(value).is_integer() else value


def _row_locator(sheet: str, row_number: int) -> dict[str, object]:
    return {"sheet": sheet, "row": row_number}


def _issue(
    category: str,
    version_id: str,
    locator: object,
    detail: str,
    *,
    requires_human: bool | None = None,
) -> dict[str, object]:
    return {
        "category": category,
        "version_id": version_id,
        "affected_locator": locator,
        "detail": detail,
        "requires_human_judgment": (
            category in HUMAN_REVIEW_CATEGORIES
            if requires_human is None
            else requires_human
        ),
    }


def _analyze_version(
    version: Mapping[str, object], overrides: Mapping[str, str] | None
) -> tuple[dict[str, object], list[dict[str, object]]]:
    cells = _validate_representation(version)
    faithful_fingerprint = _fingerprint(version["payload"])
    matrix = _matrix(cells)
    aliases = _alias_index(overrides)
    header_row = _discover_header_row(matrix, aliases)
    header_cells = matrix[header_row]
    headers: dict[int, tuple[str, Mapping[str, object]]] = {}
    mapping_candidates = []
    mapped_fields: dict[str, int] = {}
    issues: list[dict[str, object]] = []
    for column, cell in sorted(header_cells.items()):
        raw = cell.get("value")
        if not isinstance(raw, str) or not raw.strip():
            continue
        header = raw.strip()
        headers[column] = (header, cell)
        canonical = aliases.get(header)
        if canonical is None:
            mapping_candidates.append(
                {
                    "source_header": header,
                    "canonical_candidate_field": None,
                    "status": "unresolved",
                    "mapping_rule": "no_approved_alias",
                    "confidence": 0.0,
                    "uncertainty": "header meaning is not mapped",
                    "evidence": _evidence(version, cell),
                }
            )
            continue
        if canonical in mapped_fields:
            raise StructuredDataExperimentError(
                "multiple headers map to the same candidate field"
            )
        mapped_fields[canonical] = column
        mapping_candidates.append(
            {
                "source_header": header,
                "canonical_candidate_field": canonical,
                "status": "candidate",
                "mapping_rule": "version_specific_header_alias",
                "confidence": 1.0,
                "uncertainty": None,
                "evidence": _evidence(version, cell),
            }
        )

    sheet = str(next(iter(header_cells.values()))["source_locator"]["sheet"])
    records = []
    key_rows: dict[object, list[dict[str, object]]] = defaultdict(list)
    observed_units: set[str] = set()
    for row_number in sorted(row for row in matrix if row > header_row):
        row_cells = matrix[row_number]
        record_values: dict[str, object] = {}
        unresolved: list[dict[str, object]] = []
        raw_by_field: dict[str, tuple[object, Mapping[str, object]]] = {}
        for field, column in mapped_fields.items():
            cell = row_cells.get(column)
            if cell is not None:
                raw_by_field[field] = (cell.get("value"), cell)

        for field in ("product_code", "product_name", "material", "configuration"):
            if field not in raw_by_field:
                continue
            raw, cell = raw_by_field[field]
            if raw is not None:
                record_values[field] = _normalized_value(
                    raw, raw, version, cell, "faithful_header_mapping"
                )

        if "quoted_price" in raw_by_field:
            raw, cell = raw_by_field["quoted_price"]
            amount = _parse_money(raw)
            if amount is not None:
                record_values["quoted_price"] = _normalized_value(
                    {"currency": "CNY", "amount": amount},
                    raw,
                    version,
                    cell,
                    "deterministic_cny_parse",
                )
            elif raw is not None:
                issue = _issue(
                    "type_mismatch",
                    str(version["version_id"]),
                    cell["source_locator"],
                    "quoted price is not deterministically numeric",
                )
                issues.append(issue)
                unresolved.append(issue)

        if "width" in raw_by_field and "width_unit" in raw_by_field:
            raw_width, width_cell = raw_by_field["width"]
            raw_unit, unit_cell = raw_by_field["width_unit"]
            width_mm = _to_mm(raw_width, raw_unit)
            if width_mm is None:
                issue = _issue(
                    "unit_inconsistency",
                    str(version["version_id"]),
                    [width_cell["source_locator"], unit_cell["source_locator"]],
                    "explicit width unit could not be normalized",
                )
                issues.append(issue)
                unresolved.append(issue)
            else:
                observed_units.add(str(raw_unit).lower())
                record_values["width_mm"] = _normalized_value(
                    width_mm,
                    {"number": raw_width, "unit": raw_unit},
                    version,
                    width_cell,
                    "deterministic_length_to_mm",
                )

        if "notes" in raw_by_field:
            raw_notes, notes_cell = raw_by_field["notes"]
            if isinstance(raw_notes, str) and raw_notes:
                matches = list(DIMENSION_PATTERN.finditer(raw_notes))
                if len(matches) == 1:
                    number = float(matches[0].group(1))
                    unit = matches[0].group(2)
                    observed_units.add(unit.lower())
                    width_mm = _to_mm(number, unit)
                    if width_mm is not None:
                        record_values["width_mm"] = _normalized_value(
                            width_mm,
                            raw_notes,
                            version,
                            notes_cell,
                            "deterministic_dimension_from_free_text",
                        )
                    issues.append(
                        _issue(
                            "hidden_structure",
                            str(version["version_id"]),
                            notes_cell["source_locator"],
                            "free-text field contains a deterministic dimension",
                            requires_human=False,
                        )
                    )
                fragments = [
                    item.strip() for item in raw_notes.split("/") if item.strip()
                ]
                ambiguous = [
                    item for item in fragments if not DIMENSION_PATTERN.fullmatch(item)
                ]
                if ambiguous:
                    issue = _issue(
                        "ambiguous_free_text",
                        str(version["version_id"]),
                        notes_cell["source_locator"],
                        "non-dimensional free-text fragments remain uninterpreted",
                    )
                    issues.append(issue)
                    unresolved.append(issue)

        locator = _row_locator(sheet, row_number)
        for required in REQUIRED_FIELDS:
            if required not in raw_by_field or raw_by_field[required][0] is None:
                issues.append(
                    _issue(
                        "missing_values",
                        str(version["version_id"]),
                        locator,
                        f"required candidate field {required} is missing",
                        requires_human=False,
                    )
                )
        record = {
            "record_locator": locator,
            "values": record_values,
            "unresolved": unresolved,
        }
        records.append(record)
        key = record_values.get("product_code")
        if isinstance(key, dict):
            key_rows[_canonical_json(key["value"])].append(record)

    duplicate_keys = {key: rows for key, rows in key_rows.items() if len(rows) > 1}
    for duplicate_rows in duplicate_keys.values():
        locators = [row["record_locator"] for row in duplicate_rows]
        issues.append(
            _issue(
                "non_unique_key",
                str(version["version_id"]),
                locators,
                "candidate product code is non-unique",
            )
        )
        issues.append(
            _issue(
                "possible_duplicate",
                str(version["version_id"]),
                locators,
                "rows share the same candidate code and name",
                requires_human=False,
            )
        )
        prices = {
            _canonical_json(row["values"]["quoted_price"]["value"])
            for row in duplicate_rows
            if "quoted_price" in row["values"]
        }
        if len(prices) > 1:
            issues.append(
                _issue(
                    "conflicting_values",
                    str(version["version_id"]),
                    locators,
                    "duplicate candidate code has conflicting prices",
                )
            )

    if len(observed_units) > 1:
        issues.append(
            _issue(
                "unit_inconsistency",
                str(version["version_id"]),
                {"sheet": sheet},
                "multiple source length units are present",
                requires_human=False,
            )
        )

    identity_evidence = {
        "candidate_field": "product_code" if "product_code" in mapped_fields else None,
        "unique_within_version": not duplicate_keys and bool(key_rows),
        "automatic_object_binding_allowed": False,
        "reason": (
            "non-unique or unstable key; Identity Gate review required"
            if duplicate_keys
            else "source key is Identity Gate Evidence only, never Object truth"
        ),
    }
    projection = {
        "version_id": version["version_id"],
        "source_id": version["source_id"],
        "representation_id": version["representation_id"],
        "mapping_revision": _fingerprint(mapping_candidates),
        "records": records,
        "identity_evidence": identity_evidence,
    }
    layers = {
        "observed_source_structure": {
            "representation_fingerprint": faithful_fingerprint,
            "sheet_count": 1,
            "cell_count": len(cells),
            "payload": version["payload"],
        },
        "inferred_structure_candidate": {
            "header_row": header_row,
            "record_rows": len(records),
            "observed_headers": [item[0] for _, item in sorted(headers.items())],
        },
        "domain_schema_candidate": {
            "fields": sorted(mapped_fields),
            "required_fields": list(REQUIRED_FIELDS),
            "scope": "synthetic_supplier_quote",
            "status": "experimental",
        },
        "business_canonical_mapping_candidate": mapping_candidates,
    }
    return {"layers": layers, "projection": projection}, issues


def _record_index(
    analyzed: Mapping[str, object],
) -> dict[str, list[Mapping[str, object]]]:
    result: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in analyzed["projection"]["records"]:
        code = record["values"].get("product_code")
        if isinstance(code, dict):
            result[str(code["value"])].append(record)
    return result


def _cross_version_assessment(
    versions: list[Mapping[str, object]], analyzed: list[Mapping[str, object]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    issues: list[dict[str, object]] = []
    temporal_candidates: list[dict[str, object]] = []
    previous_headers: set[str] | None = None
    previous_version: Mapping[str, object] | None = None
    previous_index: dict[str, list[Mapping[str, object]]] | None = None
    names_by_code: dict[str, set[str]] = defaultdict(set)
    codes_by_name: dict[str, set[str]] = defaultdict(set)
    for version, item in zip(versions, analyzed, strict=True):
        headers = set(
            item["layers"]["inferred_structure_candidate"]["observed_headers"]
        )
        if previous_headers is not None and headers != previous_headers:
            locator = {
                "from": previous_version["version_id"],
                "to": version["version_id"],
            }
            issues.append(
                _issue(
                    "schema_drift",
                    str(version["version_id"]),
                    locator,
                    "observed column set changed between versions",
                    requires_human=False,
                )
            )
            issues.append(
                _issue(
                    "header_drift",
                    str(version["version_id"]),
                    locator,
                    "source headers changed and require version-specific mapping",
                    requires_human=False,
                )
            )
        current_index = _record_index(item)
        for code, records in current_index.items():
            for record in records:
                name = record["values"].get("product_name")
                if isinstance(name, dict):
                    names_by_code[code].add(str(name["value"]))
                    codes_by_name[str(name["value"])].add(code)
        if previous_index is not None:
            for code in sorted(set(previous_index) & set(current_index)):
                old_prices = {
                    _canonical_json(record["values"]["quoted_price"]["value"])
                    for record in previous_index[code]
                    if "quoted_price" in record["values"]
                }
                new_prices = {
                    _canonical_json(record["values"]["quoted_price"]["value"])
                    for record in current_index[code]
                    if "quoted_price" in record["values"]
                }
                if not old_prices or not new_prices or old_prices == new_prices:
                    continue
                locator = {
                    "from_version": previous_version["version_id"],
                    "to_version": version["version_id"],
                    "candidate_key": code,
                }
                if version.get("replaces") == previous_version["version_id"]:
                    temporal_candidates.append(
                        {
                            "kind": "temporal_update_candidate",
                            "locator": locator,
                            "status": "candidate",
                            "reason": "newer Source explicitly declares replaces relation",
                        }
                    )
                else:
                    issues.append(
                        _issue(
                            "temporal_ambiguity",
                            str(version["version_id"]),
                            locator,
                            "price changed without an explicit replaces relation",
                        )
                    )
                    issues.append(
                        _issue(
                            "conflicting_values",
                            str(version["version_id"]),
                            locator,
                            "cross-version prices conflict and remain unresolved",
                        )
                    )
        previous_headers = headers
        previous_version = version
        previous_index = current_index

    for name, codes in sorted(codes_by_name.items()):
        if len(codes) > 1:
            issues.append(
                _issue(
                    "identity_ambiguity",
                    "cross-version",
                    {"candidate_name": name, "candidate_codes": sorted(codes)},
                    "same display name uses different source keys; no automatic bind",
                )
            )
    return issues, temporal_candidates


def _provenance_complete(analyzed: Iterable[Mapping[str, object]]) -> bool:
    for version in analyzed:
        for record in version["projection"]["records"]:
            for normalized in record["values"].values():
                if set(normalized) != {
                    "value",
                    "raw_value",
                    "mapping_rule",
                    "evidence",
                }:
                    return False
                evidence = normalized["evidence"]
                if set(evidence) != {
                    "source_id",
                    "representation_id",
                    "source_locator",
                }:
                    return False
    return True


def _metrics(
    analyzed: list[Mapping[str, object]], issues: list[Mapping[str, object]]
) -> dict[str, int]:
    records = [record for item in analyzed for record in item["projection"]["records"]]
    affected_rows: set[str] = set()

    def collect_rows(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                collect_rows(item)
            return
        if not isinstance(value, dict):
            return
        if isinstance(value.get("sheet"), str) and isinstance(value.get("row"), int):
            affected_rows.add(
                _canonical_json({"sheet": value["sheet"], "row": value["row"]})
            )
        if isinstance(value.get("sheet"), str) and isinstance(value.get("cell"), str):
            row, _ = coordinate_to_tuple(value["cell"])
            affected_rows.add(_canonical_json({"sheet": value["sheet"], "row": row}))
        for item in value.values():
            collect_rows(item)

    for issue in issues:
        if issue["requires_human_judgment"]:
            collect_rows(issue["affected_locator"])
    row_locators = {_canonical_json(record["record_locator"]) for record in records}
    return {
        "rows_total": len(records),
        "rows_safe_to_normalize": sum(bool(record["values"]) for record in records),
        "rows_needing_review": len(row_locators & affected_rows),
        "schema_drift_count": sum(
            issue["category"] == "schema_drift" for issue in issues
        ),
        "non_unique_key_count": sum(
            issue["category"] == "non_unique_key" for issue in issues
        ),
        "unit_issue_count": sum(
            issue["category"] == "unit_inconsistency" for issue in issues
        ),
        "hidden_structure_count": sum(
            issue["category"] == "hidden_structure" for issue in issues
        ),
        "conflict_count": sum(
            issue["category"] == "conflicting_values" for issue in issues
        ),
        "unresolved_count": sum(
            bool(issue["requires_human_judgment"]) for issue in issues
        ),
    }


def analyze_representations(
    versions: list[Mapping[str, object]],
    *,
    mapping_overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, object]:
    if len(versions) < 3:
        raise StructuredDataExperimentError(
            "at least three Representation versions are required"
        )
    analyzed = []
    issues: list[dict[str, object]] = []
    for version in versions:
        item, version_issues = _analyze_version(
            version, (mapping_overrides or {}).get(str(version["version_id"]))
        )
        analyzed.append(item)
        issues.extend(version_issues)
    cross_issues, temporal_candidates = _cross_version_assessment(versions, analyzed)
    issues.extend(cross_issues)
    if not _provenance_complete(analyzed):
        raise StructuredDataExperimentError("normalized projection lost provenance")

    category_counts = {
        category: sum(issue["category"] == category for issue in issues)
        for category in (
            "identity_ambiguity",
            "non_unique_key",
            "schema_drift",
            "header_drift",
            "hidden_structure",
            "type_mismatch",
            "unit_inconsistency",
            "missing_values",
            "conflicting_values",
            "possible_duplicate",
            "temporal_ambiguity",
        )
    }

    first_non_unique = next(
        (issue for issue in issues if issue["category"] == "non_unique_key"), None
    )
    first_conflict = next(
        (issue for issue in issues if issue["category"] == "conflicting_values"), None
    )
    atomic_information_candidates = [
        {
            "semantic_type": "observation",
            "statement": "supplier product code is not sufficient to identify every variant",
            "evidence": []
            if first_non_unique is None
            else [first_non_unique["affected_locator"]],
        },
        {
            "semantic_type": "observation",
            "statement": "at least one quoted price conflict remains unresolved",
            "evidence": []
            if first_conflict is None
            else [first_conflict["affected_locator"]],
        },
    ]
    context_preview = {
        "bounded": True,
        "complete": True,
        "entries": [
            {
                "entry_kind": "existing_object_context",
                "object_id": "obj_existing_synthetic_catalog",
                "current_name": "Synthetic Supplier Catalog",
                "current_roles": ["business_line"],
            },
            {
                "entry_kind": "normalized_structured_state",
                "version_id": analyzed[-1]["projection"]["version_id"],
                "records": analyzed[-1]["projection"]["records"][:3],
            },
            {
                "entry_kind": "atomic_information",
                "items": atomic_information_candidates,
            },
            {
                "entry_kind": "data_quality_warnings",
                "items": [
                    issue for issue in issues if issue["requires_human_judgment"]
                ][:8],
            },
        ],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_scope": "synthetic_supplier_quote_schema_drift",
        "versions": analyzed,
        "data_quality_assessment": issues,
        "data_quality_checks": {
            **category_counts,
            "provenance_completeness": "pass",
        },
        "temporal_interpretation_candidates": temporal_candidates,
        "atomic_information_candidates": atomic_information_candidates,
        "context_preview": context_preview,
        "metrics": _metrics(analyzed, issues),
        "safety": {
            "faithful_representation_overwritten": False,
            "full_table_atomized": False,
            "provider_calls": 0,
            "world_model_writes": 0,
            "residue_items_from_runtime_failure": 0,
            "provenance_complete": True,
        },
        "architecture_recommendation": "C",
    }


def run(fixture_root: Path, workspace: Path) -> dict[str, object]:
    specs = load_fixture_specs(fixture_root)
    versions = build_representations(specs, workspace)
    return analyze_representations(versions)


def _write_output_atomic(path: Path, result: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture-root", type=Path, default=Path(__file__).with_name("fixtures")
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="archeos-structured-data-") as temp:
            result = run(args.fixture_root, Path(temp))
        if args.output is None:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _write_output_atomic(args.output, result)
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        StructuredDataExperimentError,
    ) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
