from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from ..atomic_information import AtomicInformationRevision
from ..atomic_information.models import validate_atomic_information_revision

DEFAULT_TOP_K = 8
MAX_TOP_K = 50
MAX_CANDIDATE_POOL = 512
_BASIS_WINDOW = 5
_BASIS_ORDER = (
    "exact_normalized",
    "same_source",
    "raw_concern_overlap",
    "number_date_key_token",
    "bounded_lexical",
)
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "asked",
        "been",
        "before",
        "could",
        "current",
        "from",
        "have",
        "information",
        "more",
        "only",
        "participant",
        "participants",
        "reported",
        "requested",
        "said",
        "same",
        "should",
        "stated",
        "still",
        "than",
        "that",
        "their",
        "they",
        "this",
        "used",
        "using",
        "were",
        "which",
        "while",
        "will",
        "with",
        "would",
    }
)
_TOKEN_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?(?:[-/]\d+){0,2}\b|\b[a-z]{1,5}\d+[a-z0-9]*\b",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\d{4}年\d{1,2}月\d{1,2}日")


@dataclass(frozen=True)
class InformationRetrievalCandidate:
    target_revision: AtomicInformationRevision
    atomic_information_id: str
    revision_id: str
    retrieval_basis: tuple[str, ...]
    rank: int
    source_relationship: str
    representation_available: bool
    time_available: bool
    claimant_available: bool
    lexical_score: float


@dataclass(frozen=True)
class RetrievalCompleteness:
    candidate_pool_size: int
    eligible_candidate_count: int
    matched_candidate_count: int
    returned_candidate_count: int
    top_k: int
    pool_complete: bool
    truncated: bool

    @property
    def complete(self) -> bool:
        return self.pool_complete and not self.truncated


@dataclass(frozen=True)
class InformationRetrievalResult:
    query_atomic_information_id: str
    query_revision_id: str
    candidates: tuple[InformationRetrievalCandidate, ...]
    metadata: RetrievalCompleteness


@dataclass(frozen=True)
class _Features:
    normalized_statement: str
    lexical: frozenset[str]
    concerns: frozenset[str]
    key_tokens: frozenset[str]


def _normalize_statement(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _lexical_features(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    english = {
        word
        for word in re.findall(r"[a-z][a-z0-9_-]{3,}", normalized)
        if word not in _STOP_WORDS
    }
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", normalized))
    bigrams = {chinese[index : index + 2] for index in range(len(chinese) - 1)}
    return frozenset(english | bigrams)


def _key_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return frozenset(match.group(0) for match in _TOKEN_PATTERN.finditer(normalized))


def _features(revision: AtomicInformationRevision) -> _Features:
    concerns: set[str] = set()
    for concern in revision.raw_concerns:
        concerns.update(_lexical_features(concern))
    return _Features(
        normalized_statement=_normalize_statement(revision.statement),
        lexical=_lexical_features(revision.statement),
        concerns=frozenset(concerns),
        key_tokens=_key_tokens(revision.statement),
    )


def _overlap_score(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _stable_key(revision: AtomicInformationRevision) -> tuple[str, str]:
    return (revision.atomic_information_id, revision.revision_id)


def _time_available(revision: AtomicInformationRevision) -> bool:
    return bool(
        (revision.claim is not None and revision.claim.claimed_at)
        or _DATE_PATTERN.search(revision.statement)
        or any(
            evidence.start is not None or evidence.end is not None
            for evidence in revision.source_evidence
        )
    )


class BoundedInformationCandidateRetriever:
    def retrieve(
        self,
        query_revision: AtomicInformationRevision,
        candidate_pool: Sequence[AtomicInformationRevision],
        *,
        top_k: int = DEFAULT_TOP_K,
        pool_complete: bool = True,
    ) -> InformationRetrievalResult:
        validate_atomic_information_revision(query_revision, "query_revision")
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= MAX_TOP_K
        ):
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        if not isinstance(pool_complete, bool):
            raise TypeError("pool_complete must be a boolean")
        if not isinstance(candidate_pool, Sequence):
            raise TypeError("candidate_pool must be a bounded Sequence")
        if len(candidate_pool) > MAX_CANDIDATE_POOL:
            raise ValueError(
                f"candidate_pool exceeds the bounded maximum of {MAX_CANDIDATE_POOL}"
            )

        pool = tuple(candidate_pool)
        seen: set[tuple[str, str]] = set()
        eligible: list[AtomicInformationRevision] = []
        for index, revision in enumerate(pool, start=1):
            validate_atomic_information_revision(revision, f"candidate_pool[{index}]")
            key = _stable_key(revision)
            if key in seen:
                raise ValueError("candidate_pool contains a duplicate revision")
            seen.add(key)
            if revision.atomic_information_id != query_revision.atomic_information_id:
                eligible.append(revision)

        query_features = _features(query_revision)
        candidate_features = {
            _stable_key(revision): _features(revision) for revision in eligible
        }

        def basis(revision: AtomicInformationRevision) -> tuple[str, ...]:
            features = candidate_features[_stable_key(revision)]
            values: list[str] = []
            if (
                query_features.normalized_statement
                and query_features.normalized_statement == features.normalized_statement
            ):
                values.append("exact_normalized")
            if query_revision.origin_source_id == revision.origin_source_id:
                values.append("same_source")
            if query_features.concerns & features.concerns:
                values.append("raw_concern_overlap")
            if query_features.key_tokens & features.key_tokens:
                values.append("number_date_key_token")
            if _overlap_score(query_features.lexical, features.lexical) > 0:
                values.append("bounded_lexical")
            return tuple(values)

        all_basis = {_stable_key(item): basis(item) for item in eligible}

        def lexical_score(revision: AtomicInformationRevision) -> float:
            return _overlap_score(
                query_features.lexical,
                candidate_features[_stable_key(revision)].lexical,
            )

        def lexical_overlap_count(revision: AtomicInformationRevision) -> int:
            return len(
                query_features.lexical
                & candidate_features[_stable_key(revision)].lexical
            )

        def ranked_for_basis(name: str) -> list[AtomicInformationRevision]:
            matches = [
                item for item in eligible if name in all_basis[_stable_key(item)]
            ]
            key = lambda item: (
                -lexical_score(item),
                -lexical_overlap_count(item),
                _stable_key(item),
            )
            return sorted(matches, key=key)

        ordered: list[AtomicInformationRevision] = []
        for name in _BASIS_ORDER:
            window = top_k if name == "bounded_lexical" else min(_BASIS_WINDOW, top_k)
            for revision in ranked_for_basis(name)[:window]:
                if revision not in ordered:
                    ordered.append(revision)
        selected = ordered[:top_k]
        candidates = tuple(
            InformationRetrievalCandidate(
                target_revision=revision,
                atomic_information_id=revision.atomic_information_id,
                revision_id=revision.revision_id,
                retrieval_basis=all_basis[_stable_key(revision)],
                rank=rank,
                source_relationship=(
                    "same"
                    if revision.origin_source_id == query_revision.origin_source_id
                    else "different"
                ),
                representation_available=any(
                    evidence.representation_id is not None
                    for evidence in revision.source_evidence
                ),
                time_available=_time_available(revision),
                claimant_available=revision.claim is not None,
                lexical_score=lexical_score(revision),
            )
            for rank, revision in enumerate(selected, start=1)
        )
        matched_count = sum(bool(value) for value in all_basis.values())
        metadata = RetrievalCompleteness(
            candidate_pool_size=len(pool),
            eligible_candidate_count=len(eligible),
            matched_candidate_count=matched_count,
            returned_candidate_count=len(candidates),
            top_k=top_k,
            pool_complete=pool_complete,
            truncated=matched_count > len(candidates),
        )
        return InformationRetrievalResult(
            query_atomic_information_id=query_revision.atomic_information_id,
            query_revision_id=query_revision.revision_id,
            candidates=candidates,
            metadata=metadata,
        )
