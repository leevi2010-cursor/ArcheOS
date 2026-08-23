from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from unittest import mock

from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    JsonlAtomicInformationStore,
)
from archeos.cli import main
from archeos.digestion import JsonlChangeJournal, JsonlChangeProposalStore
from archeos.digestion.models import (
    ChangeJournalRecord,
    ChangeProposal,
    HumanReviewContent,
    WorldModelOperation,
)
from archeos.timeline import (
    CodexTimelineProvider,
    LegacyRecoveryAuthorization,
    Selection,
    TimelineError,
    _fingerprint,
    _timeline_schema,
    build_timelines,
    load_selection,
    recover_legacy_timeline_artifact,
    render_markdown,
    validate_package,
)
from archeos.workspace import initialize_workspace
from archeos.world_model import SQLiteWorldModelRepository


@contextmanager
def _chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _revision(
    name: str,
    object_ids: tuple[str, ...],
    *,
    source_id: str | None = None,
) -> AtomicInformationRevision:
    source = source_id or f"source-{name}"
    candidate_id = f"candidate-{name}"
    atomic_id = (
        "atomic_info_"
        + hashlib.sha256(f"{source}\0{candidate_id}".encode()).hexdigest()[:32]
    )
    return AtomicInformationRevision(
        atomic_information_id=atomic_id,
        revision_number=1,
        revision_id=f"{atomic_id}-r0001",
        origin_source_id=source,
        origin_candidate_id=candidate_id,
        origin_fingerprint=hashlib.sha256(name.encode()).hexdigest(),
        statement=f"Synthetic statement for {name}",
        semantic_type="observation",
        raw_concerns=(f"Synthetic concern for {name}",),
        related_object_ids=object_ids,
        source_evidence=(
            EvidenceRecord(
                source_id=source,
                artifact="synthetic-source.md",
                segment=1,
                speaker="Speaker_1",
                start="00:00:01.000",
                end="00:00:02.000",
                excerpt=f"Synthetic evidence for {name}",
            ),
        ),
        context="Synthetic test context",
        confidence=0.9,
        created_at="2026-08-23T00:00:00Z",
        revision_reason="initial_ingestion",
    )


def _context(object_id: str, atomic_ids: tuple[str, ...]) -> dict[str, object]:
    evidence_by_atomic = {
        atomic_id: [f"evidence-view:{atomic_id}:0001"] for atomic_id in atomic_ids
    }
    evidence_refs = {
        evidence_id: {
            "atomic_information_id": atomic_id,
            "evidence": {
                "source_id": "synthetic-source-record",
                "artifact": "synthetic-artifact.md",
                "segment": 1,
                "speaker": "Synthetic Speaker",
                "start": "00:00:01.000",
                "end": "00:00:02.000",
                "excerpt": "Synthetic evidence excerpt",
                "locator": "line:12",
            },
        }
        for atomic_id, evidence_ids in evidence_by_atomic.items()
        for evidence_id in evidence_ids
    }
    return {
        "context": {"root": {"object_id": object_id}},
        "supplemental_atomic_information": [],
        "atomic_information_ids": list(atomic_ids),
        "allowed_object_ids": [object_id],
        "evidence_view_refs": evidence_refs,
        "evidence_by_atomic": evidence_by_atomic,
        "required_incomplete_reasons": [],
    }


def _package_from_context(context: dict[str, object]) -> dict[str, object]:
    object_id = str(context["selected_object_id"])
    atomic_ids = list(context["atomic_information_ids"])
    evidence_by_atomic = context["evidence_by_atomic"]
    first_atomic = atomic_ids[0]
    first_evidence = evidence_by_atomic[first_atomic][0]
    return {
        "object_id": object_id,
        "what_it_is": {
            "summary": "This is a synthetic long-lived business Object.",
            "roles": ["project"],
            "lifecycle": "ongoing",
            "object_ids": [object_id],
            "atomic_information_ids": [first_atomic],
            "evidence_ids": [first_evidence],
        },
        "timeline_entries": [
            {
                "event": "A synthetic event occurred.",
                "time": "2026-08-23T09:00:00+08:00",
                "time_end": None,
                "time_basis": "event_time",
                "time_basis_detail": "The supplied Evidence states the event time.",
                "participants": [
                    {"name": "Synthetic participant", "object_id": object_id}
                ],
                "location": "Synthetic location",
                "state_change": "The synthetic state became active.",
                "uncertainty": None,
                "object_ids": [object_id],
                "atomic_information_ids": [first_atomic],
                "evidence_ids": [first_evidence],
            },
            {
                "event": "A second event has no demonstrated occurrence time.",
                "time": None,
                "time_end": None,
                "time_basis": "source_time",
                "time_basis_detail": "Only the Source timestamp is available.",
                "participants": [],
                "location": None,
                "state_change": None,
                "uncertainty": "The event occurrence time is unknown.",
                "object_ids": [object_id],
                "atomic_information_ids": [first_atomic],
                "evidence_ids": [first_evidence],
            },
        ],
        "current_state": {
            "state": "The synthetic Object is active.",
            "as_of": "2026-08-23",
            "uncertainty": None,
            "object_ids": [object_id],
            "atomic_information_ids": [first_atomic],
            "evidence_ids": [first_evidence],
        },
        "conflicts": [],
        "unknowns": [
            {
                "question": "When did the second event occur?",
                "kind": "time",
                "object_ids": [object_id],
                "atomic_information_ids": [first_atomic],
                "evidence_ids": [first_evidence],
            }
        ],
        "information_accounting": [
            {
                "atomic_information_id": atomic_id,
                "category": "event" if index == 0 else "supporting_context",
            }
            for index, atomic_id in enumerate(atomic_ids)
        ],
        "coverage": {
            "complete": True,
            "covered_atomic_information_ids": atomic_ids,
            "incomplete_reasons": [],
        },
    }


class _SyntheticProvider:
    contract_version = "synthetic-timeline.v1"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, context: dict[str, object]) -> dict[str, object]:
        self.calls.append(str(context["selected_object_id"]))
        return _package_from_context(context)


def _validation_options(
    context: dict[str, object], object_id: str
) -> dict[str, object]:
    return {
        "evidence_ids": set(context["evidence_view_refs"]),
        "expected_object_id": object_id,
        "allowed_object_ids": set(context["allowed_object_ids"]),
        "evidence_by_atomic": {
            key: set(value) for key, value in context["evidence_by_atomic"].items()
        },
    }


class TimelineContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.object_id = "object-synthetic"
        self.atomic_id = "atomic-synthetic"
        self.context = _context(self.object_id, (self.atomic_id,))
        self.provider_context = {
            **self.context,
            "selected_object_id": self.object_id,
            "selection_label": "Synthetic",
            "supplemental_atomic_information_ids": [],
        }
        self.package = _package_from_context(self.provider_context)

    def validate(self, package: dict[str, object]) -> dict[str, object]:
        return validate_package(
            package,
            {self.atomic_id},
            **_validation_options(self.context, self.object_id),
        )

    def test_provider_schema_is_strict_and_complete_at_every_object_level(self) -> None:
        schema = _timeline_schema()

        def inspect(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    inspect(value)
            elif isinstance(node, list):
                for value in node:
                    inspect(value)

        inspect(schema)
        self.assertEqual(
            set(schema["properties"]),
            {
                "object_id",
                "what_it_is",
                "timeline_entries",
                "current_state",
                "conflicts",
                "unknowns",
                "information_accounting",
                "coverage",
            },
        )

    def test_provider_schema_requires_provenance_only_where_validator_does(
        self,
    ) -> None:
        schema = _timeline_schema()
        properties = schema["properties"]
        required_reference_owners = (
            properties["what_it_is"],
            properties["timeline_entries"]["items"],
            properties["current_state"],
            properties["conflicts"]["items"],
        )
        for owner in required_reference_owners:
            with self.subTest(owner=owner):
                references = owner["properties"]
                self.assertNotIn("minItems", references["object_ids"])
                self.assertEqual(references["atomic_information_ids"]["minItems"], 1)
                self.assertEqual(references["evidence_ids"]["minItems"], 1)

        unknown_references = properties["unknowns"]["items"]["properties"]
        for field in ("object_ids", "atomic_information_ids", "evidence_ids"):
            with self.subTest(unknown_field=field):
                self.assertNotIn("minItems", unknown_references[field])

        self.assertNotIn("uniqueItems", json.dumps(schema))
        self.validate(deepcopy(self.package))

    def test_validator_rejects_empty_and_duplicate_required_provenance(self) -> None:
        required_reference_owners = (
            "what_it_is",
            "timeline_entries",
            "current_state",
            "conflicts",
        )
        for owner in required_reference_owners:
            for field in ("atomic_information_ids", "evidence_ids"):
                with self.subTest(owner=owner, field=field, violation="empty"):
                    candidate = deepcopy(self.package)
                    if owner == "timeline_entries":
                        target = candidate[owner][0]
                    elif owner == "conflicts":
                        candidate[owner] = [
                            {
                                "summary": "Synthetic conflict",
                                "unresolved": False,
                                "object_ids": [],
                                "atomic_information_ids": [self.atomic_id],
                                "evidence_ids": [
                                    self.context["evidence_by_atomic"][self.atomic_id][
                                        0
                                    ]
                                ],
                            }
                        ]
                        target = candidate[owner][0]
                    else:
                        target = candidate[owner]
                    target[field] = []
                    with self.assertRaisesRegex(TimelineError, "must not be empty"):
                        self.validate(candidate)

                with self.subTest(owner=owner, field=field, violation="duplicate"):
                    candidate = deepcopy(self.package)
                    if owner == "timeline_entries":
                        target = candidate[owner][0]
                    elif owner == "conflicts":
                        candidate[owner] = [
                            {
                                "summary": "Synthetic conflict",
                                "unresolved": False,
                                "object_ids": [],
                                "atomic_information_ids": [self.atomic_id],
                                "evidence_ids": [
                                    self.context["evidence_by_atomic"][self.atomic_id][
                                        0
                                    ]
                                ],
                            }
                        ]
                        target = candidate[owner][0]
                    else:
                        target = candidate[owner]
                    target[field] = [*target[field], *target[field]]
                    with self.assertRaisesRegex(
                        TimelineError, "must not contain duplicates"
                    ):
                        self.validate(candidate)

        unknown = deepcopy(self.package)
        unknown["unknowns"][0]["object_ids"] = []
        unknown["unknowns"][0]["atomic_information_ids"] = []
        unknown["unknowns"][0]["evidence_ids"] = []
        self.validate(unknown)

        duplicate_unknown = deepcopy(self.package)
        for field in ("object_ids", "atomic_information_ids", "evidence_ids"):
            with self.subTest(owner="unknowns", field=field, violation="duplicate"):
                candidate = deepcopy(duplicate_unknown)
                target = candidate["unknowns"][0]
                target[field] = [*target[field], *target[field]]
                with self.assertRaisesRegex(
                    TimelineError, "must not contain duplicates"
                ):
                    self.validate(candidate)

    def test_selection_is_private_minimal_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "selection.json"
            payload = {
                "objects": [
                    {"object_id": f"object-{index}", "label": f"Object {index}"}
                    for index in range(3)
                ]
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(len(load_selection(path)), 3)

            path.chmod(0o644)
            with self.assertRaisesRegex(TimelineError, "0600"):
                load_selection(path)
            path.chmod(0o600)
            payload["objects"][0]["provider_result"] = {}
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(TimelineError, "unsupported"):
                load_selection(path)

    def test_stable_fingerprint_excludes_only_context_generated_at(self) -> None:
        context = deepcopy(self.context)
        context["context"] = {
            "root": {
                "object_id": self.object_id,
                "roles": ["project"],
                "lifecycle": {"kind": "bounded", "status": "active"},
            },
            "relationships": [{"relation": "related_to", "neighbor": "other"}],
            "atomic_information": [
                {
                    "revision_id": "revision-1",
                    "source_evidence": [{"excerpt": "evidence-1"}],
                }
            ],
            "recent_changes": [{"change_id": "change-1"}],
            "pending_judgments": [{"proposal_id": "proposal-1"}],
            "metadata": {
                "generated_at": "2026-08-23T00:00:00Z",
                "complete": True,
            },
        }
        selection = Selection(self.object_id, "Synthetic")
        baseline = _fingerprint(selection, context, "provider.v1")
        rebuilt = deepcopy(context)
        rebuilt["context"]["metadata"]["generated_at"] = "2026-08-24T00:00:00Z"
        self.assertEqual(baseline, _fingerprint(selection, rebuilt, "provider.v1"))

        mutations = {
            "Object": lambda value: value["context"]["root"].update(
                object_id="object-drift"
            ),
            "Role": lambda value: value["context"]["root"]["roles"].append(
                "business_line"
            ),
            "Lifecycle": lambda value: value["context"]["root"]["lifecycle"].update(
                status="paused"
            ),
            "Relationship": lambda value: value["context"]["relationships"][0].update(
                relation="depends_on"
            ),
            "Atomic revision": lambda value: value["context"]["atomic_information"][
                0
            ].update(revision_id="revision-2"),
            "Evidence": lambda value: value["context"]["atomic_information"][0][
                "source_evidence"
            ][0].update(excerpt="evidence-2"),
            "Change": lambda value: value["context"]["recent_changes"][0].update(
                change_id="change-2"
            ),
            "pending judgment": lambda value: value["context"]["pending_judgments"][
                0
            ].update(proposal_id="proposal-2"),
            "metadata semantic field": lambda value: value["context"][
                "metadata"
            ].update(complete=False),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                drifted = deepcopy(context)
                mutate(drifted)
                self.assertNotEqual(
                    baseline,
                    _fingerprint(selection, drifted, "provider.v1"),
                )
        self.assertNotEqual(
            baseline,
            _fingerprint(Selection(self.object_id, "Changed"), context, "provider.v1"),
        )
        self.assertNotEqual(
            baseline,
            _fingerprint(
                Selection(self.object_id, "Synthetic", ("atomic-supplemental",)),
                context,
                "provider.v1",
            ),
        )
        self.assertNotEqual(
            baseline,
            _fingerprint(selection, context, "provider.v2"),
        )

    def test_validator_rejects_identity_reference_time_and_structure_errors(
        self,
    ) -> None:
        mutations = {
            "wrong Object": lambda value: value.update(object_id="wrong"),
            "unknown Object ref": lambda value: value["timeline_entries"][0][
                "object_ids"
            ].append("unknown"),
            "unknown participant": lambda value: value["timeline_entries"][0][
                "participants"
            ][0].update(object_id="unknown"),
            "unknown Atomic ref": lambda value: value["current_state"][
                "atomic_information_ids"
            ].append("unknown"),
            "unknown Evidence ref": lambda value: value["current_state"][
                "evidence_ids"
            ].append("unknown"),
            "missing as-of": lambda value: value["current_state"].pop("as_of"),
            "non-event time": lambda value: value["timeline_entries"][1].update(
                time="2026-08-23"
            ),
            "missing state change": lambda value: value["timeline_entries"][0].pop(
                "state_change"
            ),
            "bad unknown kind": lambda value: value["unknowns"][0].update(kind="guess"),
            "duplicate accounting": lambda value: value[
                "information_accounting"
            ].append(deepcopy(value["information_accounting"][0])),
            "incomplete without reason": lambda value: value["coverage"].update(
                complete=False
            ),
            "Provider Evidence index injection": lambda value: value.update(
                evidence_index=[]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = deepcopy(self.package)
                mutate(candidate)
                with self.assertRaises(TimelineError):
                    self.validate(candidate)

    def test_validator_rejects_cross_atomic_evidence_and_silent_conflict(self) -> None:
        second_atomic = "atomic-second"
        context = _context(self.object_id, (self.atomic_id, second_atomic))
        provider_context = {
            **context,
            "selected_object_id": self.object_id,
            "selection_label": "Synthetic",
            "supplemental_atomic_information_ids": [],
        }
        package = _package_from_context(provider_context)
        package["current_state"]["evidence_ids"] = [
            context["evidence_by_atomic"][second_atomic][0]
        ]
        with self.assertRaisesRegex(TimelineError, "outside its Atomic"):
            validate_package(
                package,
                {self.atomic_id, second_atomic},
                **_validation_options(context, self.object_id),
            )

        package = _package_from_context(provider_context)
        package["conflicts"] = [
            {
                "summary": "Two supplied Claims disagree.",
                "unresolved": True,
                "object_ids": [self.object_id],
                "atomic_information_ids": [self.atomic_id],
                "evidence_ids": [context["evidence_by_atomic"][self.atomic_id][0]],
            }
        ]
        with self.assertRaisesRegex(TimelineError, "current_state uncertainty"):
            validate_package(
                package,
                {self.atomic_id, second_atomic},
                **_validation_options(context, self.object_id),
            )

    def test_context_incompleteness_cannot_be_reported_complete(self) -> None:
        with self.assertRaisesRegex(TimelineError, "incomplete bounded Context"):
            validate_package(
                self.package,
                {self.atomic_id},
                **_validation_options(self.context, self.object_id),
                required_incomplete_reasons={"evidence_limit"},
            )

    def test_markdown_has_fixed_business_sections_and_separates_unknown_time(
        self,
    ) -> None:
        package = deepcopy(self.package)
        earlier = deepcopy(package["timeline_entries"][0])
        earlier.update(event="An earlier event.", time="2026-08-22")
        package["timeline_entries"].append(earlier)
        rendered = render_markdown(package)
        sections = [line for line in rendered.splitlines() if line.startswith("## ")]
        self.assertEqual(
            sections,
            [
                "## 对象是什么",
                "## 关键事件时间线",
                "## 当前状态",
                "## 依据",
                "## 冲突",
                "## 未知与待确认",
                "## 覆盖范围",
            ],
        )
        self.assertLess(
            rendered.index("2026-08-22"),
            rendered.index("2026-08-23T09:00:00+08:00"),
        )
        self.assertLess(
            rendered.index("2026-08-23T09:00:00+08:00"),
            rendered.index("事件时间尚不明确"),
        )
        self.assertIn("截至：2026-08-23", rendered)
        self.assertIn("地点：未确认", rendered)
        self.assertIn("业务角色：项目", rendered)
        self.assertIn("生命周期：持续经营", rendered)
        self.assertNotIn("业务角色：project", rendered)
        self.assertNotIn("生命周期：ongoing", rendered)
        self.assertNotIn("{'", rendered)

        package["what_it_is"]["roles"] = [
            "person",
            "company",
            "brand",
            "project",
            "business_line",
            "event",
            "goal",
            "decision",
            "unexpected_role",
        ]
        package["what_it_is"]["lifecycle"] = "bounded"
        rendered = render_markdown(package)
        self.assertIn(
            "业务角色：人物、公司、品牌、项目、业务线、事件、目标、决策、尚未确认",
            rendered,
        )
        self.assertIn("生命周期：有明确完成边界", rendered)
        self.assertNotIn("business_line", rendered)
        self.assertNotIn("unexpected_role", rendered)


class TimelineRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selections = tuple(
            Selection(f"object-{index}", f"Object {index}") for index in range(3)
        )
        self.contexts = {
            selection.object_id: _context(
                selection.object_id,
                (f"atomic-{index}",),
            )
            for index, selection in enumerate(self.selections)
        }

    def test_normal_failure_preserves_prefix_and_resume_never_repeats_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            successful = _SyntheticProvider()

            def interrupt(context: dict[str, object]) -> dict[str, object]:
                if context["selected_object_id"] == "object-1":
                    raise RuntimeError("synthetic interruption")
                return successful(context)

            interrupt.contract_version = successful.contract_version
            with self.assertRaisesRegex(
                TimelineError,
                "timeline provider failed for Object object-1",
            ):
                build_timelines(
                    self.selections,
                    self.contexts,
                    interrupt,
                    output,
                )
            self.assertTrue((output / "object-0.json").is_file())
            self.assertTrue((output / "object-0.md").is_file())
            self.assertFalse((output / "object-1.json").exists())

            resumed = _SyntheticProvider()
            result = build_timelines(
                self.selections,
                self.contexts,
                resumed,
                output,
                resume=True,
            )
            self.assertEqual(result["provider_calls"], 2)
            self.assertEqual(resumed.calls, ["object-1", "object-2"])
            first_index = json.loads(
                (output / "object-0.json").read_text(encoding="utf-8")
            )["evidence_index"]
            second_resume = _SyntheticProvider()
            result = build_timelines(
                self.selections,
                self.contexts,
                second_resume,
                output,
                resume=True,
            )
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(second_resume.calls, [])

            saved = json.loads((output / "object-0.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["evidence_index"], first_index)
            self.assertEqual(len(saved["evidence_index"]), 1)
            presentation = saved["evidence_index"][0]
            self.assertEqual(presentation["excerpt"], "Synthetic evidence excerpt")
            self.assertEqual(presentation["source_id"], "synthetic-source-record")
            self.assertEqual(presentation["speaker"], "Synthetic Speaker")
            self.assertEqual(presentation["locator"], "line:12")
            markdown = (output / "object-0.md").read_text(encoding="utf-8")
            self.assertIn("Synthetic evidence excerpt", markdown)
            self.assertIn("synthetic-artifact.md", markdown)
            self.assertNotIn("synthetic-source-record", markdown)
            self.assertIn("Synthetic Speaker", markdown)
            self.assertIn("第 12 行", markdown)
            self.assertNotIn("line:12", markdown)
            self.assertIn("业务角色：项目", markdown)
            self.assertIn("生命周期：持续经营", markdown)
            self.assertNotIn("project", markdown)
            self.assertNotIn("ongoing", markdown)
            self.assertNotIn("object-0", markdown)
            self.assertNotIn("evidence-view:", markdown)
            self.assertNotRegex(markdown, r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")

    def test_conflict_uses_business_evidence_number_without_duplicate_expansion(
        self,
    ) -> None:
        class ConflictProvider(_SyntheticProvider):
            def __call__(self, context: dict[str, object]) -> dict[str, object]:
                package = super().__call__(context)
                atomic_id = context["atomic_information_ids"][0]
                evidence_ref = context["evidence_by_atomic"][atomic_id][0]
                package["current_state"]["uncertainty"] = (
                    "The supplied accounts remain unresolved."
                )
                package["conflicts"] = [
                    {
                        "summary": "Two supplied accounts disagree.",
                        "unresolved": True,
                        "object_ids": [context["selected_object_id"]],
                        "atomic_information_ids": [atomic_id],
                        "evidence_ids": [evidence_ref],
                    }
                ]
                return package

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            build_timelines(
                self.selections,
                self.contexts,
                ConflictProvider(),
                output,
            )
            markdown = (output / "object-0.md").read_text(encoding="utf-8")
            conflict = markdown.split("## 冲突", 1)[1].split("## 未知", 1)[0]
            self.assertIn("Two supplied accounts disagree.", conflict)
            self.assertIn("依据：1", conflict)
            self.assertNotIn("Synthetic evidence excerpt", conflict)
            self.assertEqual(markdown.count("Synthetic evidence excerpt"), 1)
            self.assertEqual(markdown.count("- **依据 1**"), 1)

    def test_authorized_legacy_recovery_is_zero_call_for_complete_and_incomplete(
        self,
    ) -> None:
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                output = root / "output"
                selection = self.selections[0]
                context = self.contexts[selection.object_id]
                provider = _SyntheticProvider()
                build_timelines(
                    self.selections,
                    self.contexts,
                    provider,
                    output,
                )
                artifact = output / f"{selection.object_id}.json"
                package = json.loads(artifact.read_text(encoding="utf-8"))
                if not complete:
                    package["coverage"].update(
                        complete=False,
                        incomplete_reasons=["synthetic gap"],
                    )
                legacy_fingerprint = "1" * 64
                package["input_fingerprint"] = legacy_fingerprint
                legacy_bytes = (
                    json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode()
                artifact.write_bytes(legacy_bytes)
                receipt_path = root / "receipts" / f"{selection.object_id}.json"
                stable_fingerprint = _fingerprint(
                    selection,
                    context,
                    provider.contract_version,
                )
                authorization = LegacyRecoveryAuthorization(
                    authority_ref="Issue #160 synthetic authorization",
                    object_id=selection.object_id,
                    legacy_artifact_sha256=hashlib.sha256(legacy_bytes).hexdigest(),
                    legacy_input_fingerprint=legacy_fingerprint,
                    stable_input_fingerprint=stable_fingerprint,
                )

                receipt = recover_legacy_timeline_artifact(
                    artifact,
                    receipt_path,
                    selection,
                    context,
                    provider.contract_version,
                    authorization,
                )
                self.assertEqual(receipt["authority_ref"], authorization.authority_ref)
                self.assertEqual(receipt["object_id"], selection.object_id)
                self.assertEqual(
                    receipt["legacy_artifact_sha256"],
                    authorization.legacy_artifact_sha256,
                )
                self.assertEqual(
                    receipt["recovered_artifact_sha256"],
                    hashlib.sha256(artifact.read_bytes()).hexdigest(),
                )
                self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
                resumed = _SyntheticProvider()
                result = build_timelines(
                    self.selections,
                    self.contexts,
                    resumed,
                    output,
                    resume=True,
                )
                self.assertEqual(resumed.calls, [])
                self.assertEqual(result["provider_calls"], 0)
                self.assertEqual(result["complete"], complete)

    def test_legacy_recovery_rejects_hash_context_and_evidence_drift_before_write(
        self,
    ) -> None:
        for case in (
            "artifact hash",
            "semantic Context",
            "Evidence",
            "authority Object",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                output = root / "output"
                build_timelines(
                    self.selections,
                    self.contexts,
                    _SyntheticProvider(),
                    output,
                )
                selection = self.selections[0]
                context = deepcopy(self.contexts[selection.object_id])
                artifact = output / f"{selection.object_id}.json"
                package = json.loads(artifact.read_text(encoding="utf-8"))
                legacy_fingerprint = "2" * 64
                package["input_fingerprint"] = legacy_fingerprint
                legacy_bytes = (
                    json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode()
                artifact.write_bytes(legacy_bytes)
                stable_fingerprint = _fingerprint(
                    selection,
                    context,
                    _SyntheticProvider.contract_version,
                )
                authority_hash = hashlib.sha256(legacy_bytes).hexdigest()
                if case == "artifact hash":
                    artifact.write_bytes(legacy_bytes + b" ")
                elif case == "semantic Context":
                    context["context"]["root"]["semantic_drift"] = True
                else:
                    evidence_ref = next(iter(context["evidence_view_refs"]))
                    context["evidence_view_refs"][evidence_ref]["evidence"][
                        "excerpt"
                    ] = "drifted Evidence"
                    stable_fingerprint = _fingerprint(
                        selection,
                        context,
                        _SyntheticProvider.contract_version,
                    )
                before = artifact.read_bytes()
                authorization = LegacyRecoveryAuthorization(
                    authority_ref="Issue #160 synthetic authorization",
                    object_id=(
                        "object-not-authorized"
                        if case == "authority Object"
                        else selection.object_id
                    ),
                    legacy_artifact_sha256=authority_hash,
                    legacy_input_fingerprint=legacy_fingerprint,
                    stable_input_fingerprint=stable_fingerprint,
                )
                receipt = root / "receipt.json"
                with self.assertRaises(TimelineError):
                    recover_legacy_timeline_artifact(
                        artifact,
                        receipt,
                        selection,
                        context,
                        _SyntheticProvider.contract_version,
                        authorization,
                    )
                self.assertEqual(artifact.read_bytes(), before)
                self.assertFalse(receipt.exists())

    def test_markdown_deduplicates_evidence_and_separates_source_record_time(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            context = _context("object-0", ("atomic-0", "atomic-1"))
            refs = list(context["evidence_view_refs"])
            locators = (
                {
                    "source_id": "source-private-a",
                    "message_key": "message-private-a",
                    "cursor": {
                        "timestamp": 1_700_000_100,
                        "conversation_key": "conversation-private-a",
                        "message_key": "message-private-a",
                    },
                },
                {
                    "source_id": "source-private-b",
                    "message_key": "message-private-b",
                    "cursor": {
                        "timestamp": 1_700_000_000,
                        "conversation_key": "conversation-private-b",
                        "message_key": "message-private-b",
                    },
                },
            )
            for index, evidence_ref in enumerate(refs):
                evidence = context["evidence_view_refs"][evidence_ref]["evidence"]
                evidence["excerpt"] = f"Unique evidence {index + 1}"
                evidence["locator"] = json.dumps(locators[index], sort_keys=True)

            def package_provider(payload: dict[str, object]) -> dict[str, object]:
                package = _package_from_context(payload)
                if payload["selected_object_id"] != "object-0":
                    return package
                package["timeline_entries"][1]["event"] = "Later recorded unknown event"
                package["timeline_entries"].append(
                    {
                        **deepcopy(package["timeline_entries"][1]),
                        "event": "Earlier recorded unknown event",
                        "atomic_information_ids": ["atomic-1"],
                        "evidence_ids": [refs[1]],
                    }
                )
                return package

            selections = tuple(
                Selection(f"object-{index}", f"Object {index}") for index in range(3)
            )
            contexts = {
                "object-0": context,
                "object-1": _context("object-1", ("atomic-object-1",)),
                "object-2": _context("object-2", ("atomic-object-2",)),
            }
            package_provider.contract_version = "synthetic-timeline.v1"
            build_timelines(
                selections,
                contexts,
                package_provider,
                Path(temp) / "output",
            )
            markdown = (Path(temp) / "output" / "object-0.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(markdown.count("Unique evidence 1"), 1)
            self.assertEqual(markdown.count("Unique evidence 2"), 1)
            self.assertEqual(markdown.count("- **依据 1**"), 1)
            self.assertEqual(markdown.count("- **依据 2**"), 1)
            self.assertLess(
                markdown.index("Earlier recorded unknown event"),
                markdown.index("Later recorded unknown event"),
            )
            self.assertEqual(markdown.count("事件时间：未知"), 2)
            self.assertIn("2023-11-15T06:13:20+08:00", markdown)
            self.assertIn("不等于事件发生时间", markdown)
            self.assertIn("聊天记录中的第 1 条", markdown)
            for hidden in (
                "message-private-a",
                "message-private-b",
                "conversation-private-a",
                "conversation-private-b",
                '"cursor"',
                "evidence-view:",
                "source-private-a",
                "source-private-b",
            ):
                self.assertNotIn(hidden, markdown)

    def test_only_corrupt_or_malformed_saved_results_are_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            build_timelines(
                self.selections,
                self.contexts,
                _SyntheticProvider(),
                output,
            )
            (output / "object-0.json").write_text("broken", encoding="utf-8")
            incomplete_path = output / "object-1.json"
            incomplete = json.loads(incomplete_path.read_text(encoding="utf-8"))
            incomplete["coverage"].update(
                complete=False,
                incomplete_reasons=["provider reported incomplete synthesis"],
            )
            incomplete_path.write_text(json.dumps(incomplete), encoding="utf-8")

            malformed_path = output / "object-2.json"
            malformed = json.loads(malformed_path.read_text(encoding="utf-8"))
            malformed["evidence_index"][0]["excerpt"] = "Provider-injected content"
            malformed_path.write_text(json.dumps(malformed), encoding="utf-8")

            provider = _SyntheticProvider()
            result = build_timelines(
                self.selections,
                self.contexts,
                provider,
                output,
                resume=True,
            )
            self.assertEqual(result["provider_calls"], 2)
            self.assertEqual(provider.calls, ["object-0", "object-2"])
            self.assertFalse(result["complete"])
            incomplete_markdown = (output / "object-1.md").read_text(encoding="utf-8")
            self.assertIn("部分资料尚未完整覆盖", incomplete_markdown)
            self.assertIn("本次验收不通过", incomplete_markdown)
            self.assertNotIn("provider", incomplete_markdown.lower())

            second_provider = _SyntheticProvider()
            second_result = build_timelines(
                self.selections,
                self.contexts,
                second_provider,
                output,
                resume=True,
            )
            self.assertEqual(second_result["provider_calls"], 0)
            self.assertEqual(second_provider.calls, [])
            self.assertFalse(second_result["complete"])

    def test_each_json_and_markdown_is_fsynced_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            with mock.patch("archeos.timeline.os.fsync", wraps=os.fsync) as fsync:
                result = build_timelines(
                    self.selections,
                    self.contexts,
                    _SyntheticProvider(),
                    output,
                )
            self.assertGreaterEqual(fsync.call_count, 12)
            self.assertEqual(len(result["packages"]), 3)
            for selection in self.selections:
                self.assertIsInstance(
                    json.loads(
                        (output / f"{selection.object_id}.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    dict,
                )
                self.assertTrue((output / f"{selection.object_id}.md").is_file())

    def test_output_inside_any_git_worktree_is_rejected_outside_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            (repository / ".git").mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            provider = _SyntheticProvider()
            with _chdir(outside), self.assertRaisesRegex(TimelineError, "Git"):
                build_timelines(
                    self.selections,
                    self.contexts,
                    provider,
                    repository / "private-output",
                )
            self.assertEqual(provider.calls, [])


class TimelineCliSyntheticWorkspaceTest(unittest.TestCase):
    def test_cli_main_uses_default_workspace_from_another_cwd_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "synthetic-workspace"
            config = root / "default-archeos.toml"
            initialize_workspace(workspace, config_path=config)
            elsewhere = root / "unrelated-cwd"
            elsewhere.mkdir()
            output = root / "private-output"

            with SQLiteWorldModelRepository(
                workspace / "04_core" / "archeos.sqlite3"
            ) as repository:
                objects = tuple(
                    repository.create_object(
                        f"Synthetic Object {index}", roles=("project",)
                    )
                    for index in range(3)
                )

            revisions = tuple(
                _revision(f"atomic-{index}", (record.object_id,))
                for index, record in enumerate(objects)
            )
            supplemental = _revision("atomic-supplemental", ())
            atomic_path = workspace / "03_information" / "atomic_information.jsonl"
            JsonlAtomicInformationStore(atomic_path).ingest_batch(
                (*revisions, supplemental)
            )

            journal_path = workspace / "03_information" / "change_journal.jsonl"
            JsonlChangeJournal(journal_path).append(
                ChangeJournalRecord(
                    "synthetic-change",
                    revisions[0].atomic_information_id,
                    revisions[0].revision_id,
                    "add_role",
                    (objects[0].object_id,),
                    "synthetic-interpretation",
                    "automatic",
                    None,
                    "applied",
                    "2026-08-23T00:01:00Z",
                    "2026-08-23T00:01:01Z",
                    None,
                )
            )
            proposal_path = workspace / "03_information" / "change_proposals.jsonl"
            JsonlChangeProposalStore(proposal_path).add_pending(
                ChangeProposal(
                    "synthetic-proposal",
                    revisions[0].atomic_information_id,
                    revisions[0].revision_id,
                    (
                        WorldModelOperation(
                            "no_structural_change",
                            target_object_id=objects[0].object_id,
                        ),
                    ),
                    (objects[0].object_id,),
                    "Synthetic pending proposal",
                    ("synthetic-evidence",),
                    "before",
                    "synthetic-proposal-interpretation",
                    HumanReviewContent(
                        "Synthetic finding",
                        "low",
                        "Review",
                        "Synthetic evidence",
                        "Synthetic consequence",
                    ),
                    "pending",
                    "2026-08-23T00:02:00Z",
                    None,
                )
            )

            source_record = workspace / "01_inbox" / "synthetic-source.bin"
            source_record.write_bytes(b"synthetic source bytes\n")
            representation_record = (
                workspace
                / "02_processing"
                / "representations"
                / "synthetic-representation.json"
            )
            representation_record.parent.mkdir(parents=True, exist_ok=True)
            representation_record.write_text(
                '{"kind":"synthetic representation"}\n', encoding="utf-8"
            )

            selection = root / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "objects": [
                            {
                                "object_id": objects[0].object_id,
                                "label": "First Object",
                                "supplemental_atomic_information_ids": [
                                    supplemental.atomic_information_id
                                ],
                            },
                            {
                                "object_id": objects[1].object_id,
                                "label": "Second Object",
                            },
                            {
                                "object_id": objects[2].object_id,
                                "label": "Third Object",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            selection.chmod(0o600)

            protected_paths = {
                "world_model": workspace / "04_core" / "archeos.sqlite3",
                "atomic_information": atomic_path,
                "proposal": proposal_path,
                "journal": journal_path,
                "source": source_record,
                "representation": representation_record,
            }
            before_bytes = {
                name: path.read_bytes() for name, path in protected_paths.items()
            }
            before_records = {
                "atomic": tuple(
                    asdict(item)
                    for item in JsonlAtomicInformationStore(
                        atomic_path
                    ).list_atomic_information()
                ),
                "proposal": tuple(
                    asdict(item)
                    for item in JsonlChangeProposalStore(proposal_path).list_history()
                ),
                "journal": tuple(
                    asdict(item)
                    for item in JsonlChangeJournal(journal_path).list_changes()
                ),
            }

            sdk_calls: list[str] = []

            class Result:
                def __init__(self, package: dict[str, object]) -> None:
                    self.final_response = json.dumps(package)

            class Thread:
                def run(self, prompt: str, **kwargs: object) -> Result:
                    self.assert_strict_schema(kwargs["output_schema"])
                    if (
                        "Write every human-facing text field in concise Chinese business language"
                        not in prompt
                    ):
                        raise AssertionError(
                            "fake SDK did not receive the language rule"
                        )
                    if (
                        "The Object explanation, every event, the current state, and "
                        "every conflict must each cite at least one supplied Atomic "
                        "Information and its corresponding Evidence." not in prompt
                    ):
                        raise AssertionError(
                            "fake SDK did not receive the provenance rule"
                        )
                    context = json.loads(prompt.split("BOUNDED_CONTEXT_JSON:\n", 1)[1])
                    sdk_calls.append(context["selected_object_id"])
                    return Result(_package_from_context(context))

                @staticmethod
                def assert_strict_schema(schema: object) -> None:
                    if (
                        not isinstance(schema, dict)
                        or schema.get("additionalProperties") is not False
                    ):
                        raise AssertionError("fake SDK received a non-strict schema")

            class Codex:
                def __enter__(self):
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def thread_start(self, **_kwargs: object) -> Thread:
                    return Thread()

            stdout = StringIO()
            with (
                _chdir(elsewhere),
                mock.patch(
                    "archeos.workspace.default_config_path",
                    return_value=config,
                ),
                mock.patch(
                    "archeos.cli._load_sdk",
                    return_value=(Codex, "deny-all", "read-only"),
                ),
                mock.patch(
                    "archeos.timeline._fingerprint",
                    return_value="0" * 64,
                ),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "stage1-review",
                        "build",
                        "--selection-file",
                        str(selection),
                        "--output-root",
                        str(output),
                    ]
                )

            self.assertEqual(exit_code, 0, stdout.getvalue())
            self.assertEqual(len(sdk_calls), 3)
            self.assertEqual(sdk_calls, [record.object_id for record in objects])
            self.assertEqual(len(list(output.glob("*.json"))), 3)
            self.assertEqual(len(list(output.glob("*.md"))), 3)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["provider_calls"], 3)
            self.assertTrue(result["complete"])

            for index, record in enumerate(objects):
                markdown = (output / f"{record.object_id}.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn(f"Synthetic evidence for atomic-{index}", markdown)
                self.assertIn("synthetic-source.md", markdown)
                self.assertIn("Speaker_1", markdown)
                self.assertIn("第 1 段", markdown)
                self.assertNotIn("segment:1", markdown)
                self.assertIn("业务角色：项目", markdown)
                self.assertIn("生命周期：持续经营", markdown)
                self.assertNotIn(revisions[index].origin_source_id, markdown)
                self.assertNotIn(record.object_id, markdown)
                self.assertNotIn(
                    revisions[index].atomic_information_id,
                    markdown,
                )
                self.assertNotIn("evidence-view:", markdown)
                self.assertNotIn("Provider", markdown)
                self.assertNotIn("bounded Context", markdown)
                self.assertNotRegex(markdown, r"\b(?:project|ongoing)\b")
                self.assertNotRegex(markdown, r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")

                saved_package = json.loads(
                    (output / f"{record.object_id}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    saved_package["evidence_index"][0]["source_id"],
                    revisions[index].origin_source_id,
                )

            incomplete_path = output / f"{objects[1].object_id}.json"
            incomplete = json.loads(incomplete_path.read_text(encoding="utf-8"))
            incomplete["coverage"].update(
                complete=False,
                incomplete_reasons=["synthetic bounded Context gap"],
            )
            incomplete_path.write_text(json.dumps(incomplete), encoding="utf-8")
            resume_stdout = StringIO()
            with (
                _chdir(elsewhere),
                mock.patch(
                    "archeos.workspace.default_config_path",
                    return_value=config,
                ),
                mock.patch(
                    "archeos.cli._load_sdk",
                    return_value=(Codex, "deny-all", "read-only"),
                ),
                mock.patch(
                    "archeos.timeline._fingerprint",
                    return_value="0" * 64,
                ),
                redirect_stdout(resume_stdout),
            ):
                resume_exit_code = main(
                    [
                        "stage1-review",
                        "build",
                        "--selection-file",
                        str(selection),
                        "--output-root",
                        str(output),
                        "--resume",
                    ]
                )
            resume_result = json.loads(resume_stdout.getvalue())
            self.assertEqual(resume_exit_code, 1)
            self.assertEqual(resume_result["provider_calls"], 0)
            self.assertFalse(resume_result["complete"])
            self.assertEqual(len(sdk_calls), 3)
            incomplete_markdown = (output / f"{objects[1].object_id}.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("部分资料尚未完整覆盖", incomplete_markdown)
            self.assertIn("本次验收不通过", incomplete_markdown)
            self.assertNotIn("provider", incomplete_markdown.lower())
            self.assertNotIn("Provider", incomplete_markdown)
            self.assertNotIn("bounded Context", incomplete_markdown)
            self.assertNotIn("synthetic bounded Context gap", incomplete_markdown)

            after_bytes = {
                name: path.read_bytes() for name, path in protected_paths.items()
            }
            after_records = {
                "atomic": tuple(
                    asdict(item)
                    for item in JsonlAtomicInformationStore(
                        atomic_path
                    ).list_atomic_information()
                ),
                "proposal": tuple(
                    asdict(item)
                    for item in JsonlChangeProposalStore(proposal_path).list_history()
                ),
                "journal": tuple(
                    asdict(item)
                    for item in JsonlChangeJournal(journal_path).list_changes()
                ),
            }
            self.assertEqual(before_bytes, after_bytes)
            self.assertEqual(before_records, after_records)

    def test_codex_provider_maps_normal_sdk_failure_to_timeline_error(self) -> None:
        class Codex:
            def __enter__(self):
                raise RuntimeError("synthetic SDK failure")

            def __exit__(self, *_args: object) -> None:
                return None

        provider = CodexTimelineProvider(
            sdk_loader=lambda: (Codex, "deny-all", "read-only")
        )
        with self.assertRaisesRegex(
            TimelineError,
            "Codex timeline provider failed: synthetic SDK failure",
        ):
            provider({"selected_object_id": "synthetic"})


if __name__ == "__main__":
    unittest.main()
