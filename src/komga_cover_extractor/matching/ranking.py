"""Pure release ranking and upgrade decisions.

Ranking rules are compiled when their immutable policy values are created. The
matching layer consumes release observations only; it never opens the observed
path or decides that two releases are byte-identical without an explicit hash.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import regex
from regex import Pattern

from komga_cover_extractor.domain.releases import ReleaseSnapshot

RankFileType = Literal["both", "chapter", "volume"]
RankStatus = Literal["ranked", "invalid_input"]
UpgradeStatus = Literal["upgrade", "not_upgrade", "review_required", "invalid_input"]


@dataclass(frozen=True, slots=True)
class RankRule:
    """One ordered, case-insensitive regex score rule from configuration."""

    name: str
    score: float
    file_type: RankFileType = "both"
    _compiled: Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("rank rule name must be a non-empty regex")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("rank rule score must be numeric")
        if not math.isfinite(float(self.score)):
            raise ValueError("rank rule score must be finite")
        if self.file_type not in {"both", "chapter", "volume"}:
            raise ValueError("rank rule file_type must be both, chapter, or volume")
        try:
            compiled = regex.compile(self.name, flags=regex.IGNORECASE)
        except regex.error as error:
            raise ValueError(f"invalid rank rule regex: {error}") from None
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "_compiled", compiled)


@dataclass(frozen=True, slots=True)
class RankMatch:
    """A rule and the first substring it matched in a release name."""

    rule: RankRule
    text: str

    @property
    def score(self) -> float:
        return self.rule.score


@dataclass(frozen=True, slots=True)
class RankingPolicy:
    """The small immutable set of rules used to compare release quality."""

    rules: tuple[RankRule, ...] = ()

    def __post_init__(self) -> None:
        rules = tuple(self.rules)
        if any(not isinstance(rule, RankRule) for rule in rules):
            raise TypeError("ranking policy rules must be RankRule values")
        object.__setattr__(self, "rules", rules)


@dataclass(frozen=True, slots=True)
class RankedRelease:
    status: RankStatus
    reason: str
    release: ReleaseSnapshot
    candidates: tuple[RankMatch, ...]
    scores: tuple[float, ...]
    total_score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "scores", tuple(float(score) for score in self.scores))
        if len(self.candidates) != len(self.scores):
            raise ValueError("rank candidates and scores must have equal lengths")

    @property
    def score(self) -> float:
        """Convenient singular spelling for consumers displaying one score."""
        return self.total_score


@dataclass(frozen=True, slots=True)
class UpgradeDecision:
    status: UpgradeStatus
    reason: str
    incoming: RankedRelease
    existing: RankedRelease
    candidates: tuple[ReleaseSnapshot, ...]
    scores: tuple[float, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "scores", tuple(float(score) for score in self.scores))
        if len(self.scores) != 2:
            raise ValueError("upgrade decisions require incoming and existing scores")

    @property
    def is_upgrade(self) -> bool | None:
        if self.status == "upgrade":
            return True
        if self.status == "not_upgrade":
            return False
        return None


def rank_release(
    release: ReleaseSnapshot, rules: tuple[RankRule, ...]
) -> RankedRelease:
    """Sum each applicable rule that matches once, retaining input order.

    Rule ``name`` values are regexes, matching the legacy ``Keyword.name``
    contract. Chapter/volume-specific rules do not apply to the other kind.
    """
    if not isinstance(release, ReleaseSnapshot):
        raise TypeError("release must be a ReleaseSnapshot")
    ordered_rules = tuple(rules)
    if any(not isinstance(rule, RankRule) for rule in ordered_rules):
        raise TypeError("rules must contain RankRule values")
    if release.kind not in {"chapter", "volume"}:
        return RankedRelease(
            "invalid_input", "unknown_release_kind", release, (), (), 0.0
        )

    matches: list[RankMatch] = []
    for rule in ordered_rules:
        if rule.file_type not in {"both", release.kind}:
            continue
        match = rule._compiled.search(release.name)
        if match is not None:
            matches.append(RankMatch(rule, match.group(0)))

    scores = tuple(match.score for match in matches)
    return RankedRelease(
        "ranked",
        "matched_rules" if matches else "no_matching_rules",
        release,
        tuple(matches),
        scores,
        sum(scores),
    )


def _content_digest(release: ReleaseSnapshot) -> tuple[str | None, bool]:
    """Return the SHA-256 carried by the caller's source fingerprint."""
    fingerprint = release.observation.fingerprint
    if fingerprint is None:
        return None, False
    if fingerprint.path != release.path:
        return None, True
    value = fingerprint.sha256
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        return None, True
    return value, False


def decide_upgrade(
    incoming: ReleaseSnapshot,
    existing: ReleaseSnapshot,
    policy: RankingPolicy,
) -> UpgradeDecision:
    """Decide upgrade status from strict rule scores and explicit byte evidence.

    A strictly higher score is an upgrade. Content hashes come only from each
    observation's ``FileFingerprint``; equal-ranked different or unknown bytes
    require review. This function never opens files or treats missing hashes as
    equality.
    """
    if not isinstance(incoming, ReleaseSnapshot) or not isinstance(existing, ReleaseSnapshot):
        raise TypeError("incoming and existing must be ReleaseSnapshot values")
    if not isinstance(policy, RankingPolicy):
        raise TypeError("policy must be a RankingPolicy")

    incoming_rank = rank_release(incoming, policy.rules)
    existing_rank = rank_release(existing, policy.rules)
    scores = (incoming_rank.total_score, existing_rank.total_score)
    candidates = (incoming, existing)
    if incoming_rank.status != "ranked" or existing_rank.status != "ranked":
        return UpgradeDecision(
            "invalid_input", "release_kind_unavailable", incoming_rank, existing_rank,
            candidates, scores,
        )

    incoming_digest, incoming_malformed = _content_digest(incoming)
    existing_digest, existing_malformed = _content_digest(existing)
    if incoming_malformed or existing_malformed:
        return UpgradeDecision(
            "invalid_input", "invalid_content_hash", incoming_rank, existing_rank,
            candidates, scores,
        )
    if incoming_rank.total_score > existing_rank.total_score:
        return UpgradeDecision(
            "upgrade", "strictly_higher_rank", incoming_rank, existing_rank,
            candidates, scores,
        )
    if incoming_rank.total_score < existing_rank.total_score:
        return UpgradeDecision(
            "not_upgrade", "lower_rank", incoming_rank, existing_rank,
            candidates, scores,
        )
    if incoming_digest is not None and incoming_digest == existing_digest:
        return UpgradeDecision(
            "not_upgrade", "identical_content", incoming_rank, existing_rank,
            candidates, scores,
        )
    if incoming_digest is None or existing_digest is None:
        return UpgradeDecision(
            "review_required", "equal_rank_content_identity_unknown",
            incoming_rank, existing_rank, candidates, scores,
        )
    return UpgradeDecision(
        "review_required", "equal_rank_different_content", incoming_rank,
        existing_rank, candidates, scores,
    )
