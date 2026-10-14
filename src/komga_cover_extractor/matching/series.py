"""Pure series matching over explicit release and observation snapshots.

All candidate tiers are evaluated over the complete library snapshot. The
Matching and evidence caches are bounded, policy-scoped, and fingerprinted;
they never prune the current candidate set. Image similarity is supplied as
an observation callback; this module never opens archives, decodes images,
scans paths, or performs requests.
"""
from __future__ import annotations

import math
import re
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.operations import FileFingerprint
from komga_cover_extractor.domain.releases import ReleaseNumber, ReleaseSnapshot
from komga_cover_extractor.matching.classification import (
    ClassificationPolicy,
    check_root_suitability,
)
from komga_cover_extractor.parsing.normalization import clean_str, parse_words, similar

MATCH_CACHE_MAX_ENTRIES = 4096
MATCH_CACHE_TTL_SECONDS = 300.0
MATCH_RULE_VERSION = "series-v3"

MatchStatus = Literal[
    "matched", "no_match", "ambiguous", "missing_dependency", "invalid_input"
]
CoverSimilarity = Callable[[ReleaseSnapshot, ReleaseSnapshot], float | None]
EvidenceKind = Literal["cover_similarity", "unique_identifier"]

_TITLE_DELIMITER_PATTERN = re.compile(r"((\s+(-)|:)\s+)")
_SHORTENED_TITLE_PATTERN = re.compile(r"((\s+(-)|:)\s+.*)")
_SUBTITLE_SUFFIX_PATTERN = re.compile(r"(.*)((\s+(-)|:)\s+)")



@dataclass(frozen=True, slots=True)
class MatchingPolicy:
    required_similarity_score: float = 0.979

    required_matching_percentage: float = 90.0
    match_through_image_similarity: bool = True
    required_image_similarity_score: float = 0.9
    match_through_identifiers: bool = True
    classification: ClassificationPolicy = field(default_factory=ClassificationPolicy)
    _fingerprint: tuple[object, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("required_similarity_score", "required_image_similarity_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between zero and one")
            object.__setattr__(self, name, float(value))

        value = self.required_matching_percentage
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("required_matching_percentage must be numeric")
        if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 100.0:
            raise ValueError("required_matching_percentage must be between zero and one hundred")
        object.__setattr__(self, "required_matching_percentage", float(value))
        if type(self.match_through_image_similarity) is not bool or type(
            self.match_through_identifiers
        ) is not bool:
            raise TypeError("matching toggles must be booleans")
        if not isinstance(self.classification, ClassificationPolicy):
            raise TypeError("classification must be a ClassificationPolicy")
        object.__setattr__(
            self,
            "_fingerprint",
            (
                MATCH_RULE_VERSION,
                self.required_similarity_score,
                self.required_matching_percentage,
                self.match_through_image_similarity,
                self.required_image_similarity_score,
                self.match_through_identifiers,
                self.classification.fingerprint,
            ),
        )

    @property
    def fingerprint(self) -> tuple[object, ...]:
        """Canonical behavior-setting key, not the identity of this policy object."""
        return self._fingerprint


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    series_path: Path
    root: LibraryRoot | None
    releases: tuple[ReleaseSnapshot, ...]
    score: float | None
    evidence: str
    identifiers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "series_path", Path(self.series_path))
        object.__setattr__(self, "releases", tuple(self.releases))
        object.__setattr__(self, "identifiers", tuple(tuple(item) for item in self.identifiers))

    @property
    def path(self) -> Path:
        return self.series_path


@dataclass(frozen=True, slots=True)
class MatchDecision:
    status: MatchStatus
    reason: str
    selected: MatchCandidate | None
    candidates: tuple[MatchCandidate, ...]
    scores: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "scores", tuple(float(score) for score in self.scores))
        if self.status == "matched" and self.selected is None:
            raise ValueError("matched decisions require a selected candidate")
        if self.status != "matched" and self.selected is not None:
            raise ValueError("only matched decisions may select a candidate")


@dataclass(frozen=True, slots=True)
class _NameScoreKey:
    incoming: FileFingerprint
    candidate: FileFingerprint
    candidate_name: str
    incoming_name: str


@dataclass(frozen=True, slots=True)
class _EvidenceKey:
    incoming: FileFingerprint
    candidate: FileFingerprint
    evidence: EvidenceKind


def _cache_sources_current(
    key: _NameScoreKey | _EvidenceKey,
    incoming: ReleaseSnapshot,
    observed: Mapping[Path, FileFingerprint | None],
) -> bool:
    return key.candidate == observed.get(key.candidate.path) and (
        key.incoming.path != incoming.path
        or key.incoming == _source_fingerprint(incoming)
    )


class MatchScoreCache:
    """A bounded, policy-bound cache for fingerprinted, caller-observed names."""

    __slots__ = ("_policy_fingerprint", "_entries")

    def __init__(self, policy: MatchingPolicy) -> None:
        if not isinstance(policy, MatchingPolicy):
            raise TypeError("policy must be a MatchingPolicy")
        self._policy_fingerprint = policy.fingerprint
        self._entries: OrderedDict[_NameScoreKey, tuple[float, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def _get(
        self, key: _NameScoreKey, policy: MatchingPolicy, now: float
    ) -> float | None:
        if policy.fingerprint != self._policy_fingerprint:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, score = entry
        if now < stored_at or now - stored_at >= MATCH_CACHE_TTL_SECONDS:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return score

    def _put(
        self,
        key: _NameScoreKey,
        score: float,
        policy: MatchingPolicy,
        now: float,
    ) -> None:
        if policy.fingerprint != self._policy_fingerprint:
            return
        self._entries[key] = (now, score)
        self._entries.move_to_end(key)
        while len(self._entries) > MATCH_CACHE_MAX_ENTRIES:
            self._entries.popitem(last=False)

    def invalidate(self, path: Path) -> None:
        """Discard observations of a changed or removed file or directory."""
        path = Path(path)
        stale = [
            key
            for key in self._entries
            if key.incoming.path.is_relative_to(path)
            or key.candidate.path.is_relative_to(path)
        ]
        for key in stale:
            del self._entries[key]

    def _refresh(
        self,
        incoming: ReleaseSnapshot,
        observed: Mapping[Path, FileFingerprint | None],
        policy: MatchingPolicy,
        now: float,
    ) -> None:
        if policy.fingerprint != self._policy_fingerprint:
            self._entries.clear()
            self._policy_fingerprint = policy.fingerprint
        stale = [
            key
            for key, (stored_at, _) in self._entries.items()
            if now < stored_at
            or now - stored_at >= MATCH_CACHE_TTL_SECONDS
            or not _cache_sources_current(key, incoming, observed)
        ]
        for key in stale:
            del self._entries[key]


@dataclass(frozen=True, slots=True)
class _CachedMatchEvidence:
    observed_at: float
    score: float | None = None
    identifiers: tuple[tuple[str, str], ...] = ()


class MatchEvidenceCache:
    """Bounded, policy-scoped image scores and shared identifier observations.

    Entries require both source fingerprints and an explicit monotonic time.
    Matching never treats a previous selection or a path string as evidence.
    """

    __slots__ = ("_policy_fingerprint", "_entries")

    def __init__(self, policy: MatchingPolicy) -> None:
        if not isinstance(policy, MatchingPolicy):
            raise TypeError("policy must be a MatchingPolicy")
        self._policy_fingerprint = policy.fingerprint
        self._entries: OrderedDict[_EvidenceKey, _CachedMatchEvidence] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def _get(
        self,
        evidence: EvidenceKind,
        incoming: ReleaseSnapshot,
        candidate_release: ReleaseSnapshot,
        policy: MatchingPolicy,
        now: float,
    ) -> _CachedMatchEvidence | None:
        if policy.fingerprint != self._policy_fingerprint:
            return None
        key = _evidence_key(evidence, incoming, candidate_release)
        if key is None:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        if (
            now < entry.observed_at
            or now - entry.observed_at >= MATCH_CACHE_TTL_SECONDS
        ):
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry

    def _put(
        self,
        evidence: EvidenceKind,
        incoming: ReleaseSnapshot,
        candidate_release: ReleaseSnapshot,
        policy: MatchingPolicy,
        now: float,
        *,
        score: float | None = None,
        identifiers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if policy.fingerprint != self._policy_fingerprint:
            return
        key = _evidence_key(evidence, incoming, candidate_release)
        if key is None:
            return
        entry = _CachedMatchEvidence(now, score, identifiers)
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > MATCH_CACHE_MAX_ENTRIES:
            self._entries.popitem(last=False)

    def invalidate(self, path: Path) -> None:
        """Discard observations of a changed or removed file or directory."""
        path = Path(path)
        stale = [
            key
            for key in self._entries
            if key.incoming.path.is_relative_to(path)
            or key.candidate.path.is_relative_to(path)
        ]
        for key in stale:
            del self._entries[key]

    def _refresh(
        self,
        incoming: ReleaseSnapshot,
        observed: Mapping[Path, FileFingerprint | None],
        policy: MatchingPolicy,
        now: float,
    ) -> None:
        if policy.fingerprint != self._policy_fingerprint:
            self._entries.clear()
            self._policy_fingerprint = policy.fingerprint
        stale = [
            key
            for key, entry in self._entries.items()
            if now < entry.observed_at
            or now - entry.observed_at >= MATCH_CACHE_TTL_SECONDS
            or not _cache_sources_current(key, incoming, observed)
        ]
        for key in stale:
            del self._entries[key]


def _source_fingerprint(release: ReleaseSnapshot) -> FileFingerprint | None:
    fingerprint = release.observation.fingerprint
    if fingerprint is None or fingerprint.path != release.path:
        return None
    return fingerprint


def _score_key(
    candidate: ReleaseSnapshot,
    incoming: ReleaseSnapshot,
    candidate_name: str,
    incoming_name: str,
) -> _NameScoreKey | None:
    candidate_fingerprint = _source_fingerprint(candidate)
    incoming_fingerprint = _source_fingerprint(incoming)
    if candidate_fingerprint is None or incoming_fingerprint is None:
        return None
    return _NameScoreKey(
        incoming_fingerprint, candidate_fingerprint, candidate_name, incoming_name
    )


def _name_similarity(
    candidate: ReleaseSnapshot,
    incoming: ReleaseSnapshot,
    candidate_name: str,
    incoming_name: str,
    policy: MatchingPolicy,
    cache: MatchScoreCache | None,
    now: float | None,
) -> float:
    key = _score_key(candidate, incoming, candidate_name, incoming_name)
    if cache is not None and key is not None and now is not None:
        score = cache._get(key, policy, now)
        if score is not None:
            return score
    # Preserve the original evidence order: existing folder name, then incoming.
    score = similar(candidate_name, incoming_name)
    if cache is not None and key is not None and now is not None:
        cache._put(key, score, policy, now)
    return score


def _release_identifiers(release: ReleaseSnapshot) -> frozenset[tuple[str, str]]:
    return frozenset(
        (name.casefold().strip(), value.casefold().strip())
        for name, value in release.identifiers
        if name.strip() and value.strip()
    )


def _evidence_key(
    evidence: EvidenceKind,
    incoming: ReleaseSnapshot,
    candidate_release: ReleaseSnapshot,
) -> _EvidenceKey | None:
    incoming_fingerprint = _source_fingerprint(incoming)
    candidate_fingerprint = _source_fingerprint(candidate_release)
    if incoming_fingerprint is None or candidate_fingerprint is None:
        return None
    return _EvidenceKey(incoming_fingerprint, candidate_fingerprint, evidence)


def _matching_identifier_observations(
    incoming: ReleaseSnapshot, candidate: MatchCandidate
) -> tuple[tuple[ReleaseSnapshot, tuple[tuple[str, str], ...]], ...]:
    if incoming.kind != "volume":
        return ()
    incoming_ids = _release_identifiers(incoming)
    if not incoming_ids:
        return ()
    extension = incoming.path.suffix.casefold()
    matches: list[tuple[ReleaseSnapshot, tuple[tuple[str, str], ...]]] = []
    for release in candidate.releases:
        if release.path.suffix.casefold() != extension:
            continue
        shared = tuple(sorted(incoming_ids.intersection(_release_identifiers(release))))
        if shared:
            matches.append((release, shared))
    return tuple(matches)


def _same_index_number(left: ReleaseNumber, right: ReleaseNumber) -> bool:
    """Match legacy index values without expanding a range or folding parts."""
    if left.kind == "missing" or right.kind == "missing" or left.part != right.part:
        return False
    left_is_collection = left.kind in {"range", "set"}
    right_is_collection = right.kind in {"range", "set"}
    if left_is_collection and right_is_collection:
        return left.values == right.values
    if left_is_collection:
        return right.values[0] in left.values
    if right_is_collection:
        return left.values[0] in right.values
    return left.values[0] == right.values[0]


def _matching_cover_releases(
    incoming: ReleaseSnapshot, candidate: MatchCandidate
) -> tuple[ReleaseSnapshot, ...]:
    """Use matching indexes first, then unrelated indexes only when <=10 remain."""
    matching: list[ReleaseSnapshot] = []
    unrelated: list[ReleaseSnapshot] = []
    for release in candidate.releases:
        if _same_index_number(release.number, incoming.number):
            matching.append(release)
        else:
            unrelated.append(release)
    if len(unrelated) <= 10:
        matching.extend(unrelated)
    return tuple(matching)



def _folder_type_suitable(
    incoming: ReleaseSnapshot,
    folder_releases: tuple[ReleaseSnapshot, ...],
    policy: MatchingPolicy,
) -> bool:
    """Preserve check_upgrade's medium and chapter/volume percentage gates."""
    if not folder_releases or incoming.kind not in {"chapter", "volume"}:
        return False
    threshold = policy.required_matching_percentage
    incoming_extension = incoming.path.suffix.casefold()
    folder_extensions = tuple(
        release.path.suffix.casefold() for release in folder_releases
    )
    medium_match = any(
        (100.0 if incoming_extension in extensions else 0.0) >= threshold
        and (
            sum(extension in extensions for extension in folder_extensions)
            / len(folder_releases)
            * 100.0
        )
        >= threshold
        for extensions in (
            frozenset(policy.classification.manga_extensions),
            frozenset(policy.classification.novel_extensions),
        )
    )
    kind_percentage = (
        sum(release.kind == incoming.kind for release in folder_releases)
        / len(folder_releases)
        * 100.0
    )
    return medium_match and kind_percentage >= threshold


def _shortened_title(title: str) -> str:
    if ("-" in title or ":" in title) and _TITLE_DELIMITER_PATTERN.search(title):
        return _SHORTENED_TITLE_PATTERN.sub("", title).strip()
    return ""


def _subtitle_from_dash(title: str) -> str:
    match = _TITLE_DELIMITER_PATTERN.search(title) if "-" in title or ":" in title else None
    return _SUBTITLE_SUFFIX_PATTERN.sub("", title) if match is not None else ""


def _contains_consecutive_items(
    first: tuple[str, ...], second: tuple[str, ...], count: int = 3
) -> bool:
    if len(first) < count or len(second) < count:
        return False
    for first_index in range(len(first) - count + 1):
        for second_index in range(len(second) - count + 1):
            if first[first_index : first_index + count] == second[
                second_index : second_index + count
            ]:
                return True
    return False


def _sibling_directory_word_counts(
    parent: Path,
    library: LibrarySnapshot,
    directory_siblings: Mapping[Path, tuple[str, ...]] | None,
) -> Counter[str]:
    if directory_siblings is not None and parent in directory_siblings:
        directory_names = directory_siblings[parent]
    else:
        directory_names = tuple(
            {
                release.parent.relative_to(parent).parts[0]
                for release in library.releases
                if release.parent != parent and release.parent.is_relative_to(parent)
            }
        )
    return Counter(
        word for directory_name in directory_names for word in parse_words(directory_name)
    )


def _alternative_match_allowed(
    folder_name: str,
    incoming: ReleaseSnapshot,
    counted_words: Mapping[str, int],
    policy: MatchingPolicy,
) -> bool:
    """Port the original alternative-cover gate over supplied name evidence."""
    folder_subtitle = _subtitle_from_dash(folder_name)
    folder_subtitle_clean = clean_str(folder_subtitle) if folder_subtitle else ""
    file_subtitle = _subtitle_from_dash(incoming.display_series)
    file_subtitle_clean = clean_str(file_subtitle) if file_subtitle else ""

    short_folder_name = clean_str(_shortened_title(folder_name) or folder_name)
    short_series_name = clean_str(
        _shortened_title(incoming.display_series) or incoming.display_series
    )
    if not short_folder_name or not short_series_name:
        return False

    long_folder_words = parse_words(folder_name)
    long_file_words = parse_words(incoming.display_series)
    short_folder_words = parse_words(short_folder_name)
    short_file_words = parse_words(short_series_name)
    if not short_folder_words or not short_file_words:
        return False

    folder_name_match = short_folder_name.lower().strip() == short_series_name.lower().strip()
    similar_score_match = (
        similar(short_folder_name, short_series_name)
        >= policy.required_similarity_score
    )
    consecutive_items_match = _contains_consecutive_items(
        tuple(short_folder_words), tuple(short_file_words)
    ) or _contains_consecutive_items(
        tuple(long_folder_words), tuple(long_file_words)
    )
    unique_words_match = any(
        word in long_file_words
        and word in counted_words
        and counted_words[word] <= 3
        for word in long_folder_words
    )
    subtitle_match = bool(folder_subtitle_clean and file_subtitle_clean) and (
        folder_subtitle_clean == file_subtitle_clean
        or similar(folder_subtitle_clean, file_subtitle_clean)
        >= policy.required_similarity_score
    )
    return (
        folder_name_match
        or similar_score_match
        or consecutive_items_match
        or unique_words_match
        or subtitle_match
    )


def _image_candidates(
    incoming: ReleaseSnapshot,
    candidates: tuple[MatchCandidate, ...],
    library: LibrarySnapshot,
    policy: MatchingPolicy,
    directory_siblings: Mapping[Path, tuple[str, ...]] | None,
) -> tuple[MatchCandidate, ...]:
    word_counts_by_parent: dict[Path, Counter[str]] = {}
    eligible: list[MatchCandidate] = []
    for candidate in candidates:
        parent = candidate.series_path.parent
        if parent not in word_counts_by_parent:
            word_counts_by_parent[parent] = _sibling_directory_word_counts(
                parent, library, directory_siblings
            )
        if _alternative_match_allowed(
            candidate.series_path.name,
            incoming,
            word_counts_by_parent[parent],
            policy,
        ):
            eligible.append(candidate)
    return tuple(eligible)

def _base_candidates(
    incoming: ReleaseSnapshot,
    library: LibrarySnapshot,
    policy: MatchingPolicy,
) -> tuple[MatchCandidate, ...]:
    grouped: dict[Path, list[ReleaseSnapshot]] = {}
    for release in library.releases:
        grouped.setdefault(release.parent, []).append(release)

    result: list[MatchCandidate] = []
    roots = library.roots
    for series_path in sorted(grouped, key=lambda path: str(path)):
        releases = tuple(grouped[series_path])

        if not _folder_type_suitable(incoming, releases, policy):
            continue
        if not roots:
            result.append(MatchCandidate(series_path, None, releases, None, ""))
            continue

        containing_roots = tuple(root for root in roots if root.contains(series_path))
        if not containing_roots:
            continue
        for root in containing_roots:
            suitability = check_root_suitability(
                (incoming,), root, policy.classification
            )
            if suitability.status == "suitable":
                result.append(MatchCandidate(series_path, root, releases, None, ""))
    return tuple(result)


def _decision_for_tier(
    tier_candidates: tuple[MatchCandidate, ...], reason: str
) -> MatchDecision:
    scores = tuple(
        candidate.score for candidate in tier_candidates if candidate.score is not None
    )
    if len(tier_candidates) > 1:
        return MatchDecision("ambiguous", reason, None, tier_candidates, scores)
    if len(tier_candidates) == 1:
        return MatchDecision("matched", reason, tier_candidates[0], tier_candidates, scores)
    return MatchDecision("no_match", "no_candidate_in_tier", None, (), ())


def _cover_tier(
    incoming: ReleaseSnapshot,
    candidates: tuple[MatchCandidate, ...],
    policy: MatchingPolicy,
    cover_similarity: CoverSimilarity | None,
    evidence_cache: MatchEvidenceCache | None,
    now: float | None,
    *,
    reason: str,
) -> MatchDecision:
    matches: list[MatchCandidate] = []
    unresolved = False
    for candidate in candidates:
        missing_score = False
        for release in _matching_cover_releases(incoming, candidate):
            observation = (
                evidence_cache._get("cover_similarity", incoming, release, policy, now)
                if evidence_cache is not None and now is not None
                else None
            )
            image_score = (
                observation.score
                if observation is not None
                else cover_similarity(incoming, release)
                if cover_similarity is not None
                else None
            )
            if image_score is None:
                missing_score = True
                continue
            if (
                isinstance(image_score, bool)
                or not isinstance(image_score, (int, float))
                or not math.isfinite(float(image_score))
                or not -1.0 <= float(image_score) <= 1.0
            ):
                return MatchDecision(
                    "invalid_input", "invalid_cover_similarity_observation", None, (), ()
                )
            score = float(image_score)
            if observation is None and evidence_cache is not None and now is not None:
                evidence_cache._put(
                    "cover_similarity", incoming, release, policy, now, score=score
                )
            if score >= policy.required_image_similarity_score:
                matches.append(
                    MatchCandidate(
                        candidate.series_path,
                        candidate.root,
                        candidate.releases,
                        score,
                        "cached_cover_similarity"
                        if observation is not None
                        else "cover_similarity",
                    )
                )
                break
        else:
            unresolved |= missing_score

    if len(matches) > 1:
        return _decision_for_tier(tuple(matches), reason)
    if unresolved:
        return MatchDecision(
            "missing_dependency",
            "cover_similarity_observation_unavailable"
            if cover_similarity is None and not matches
            else "cover_similarity_observation_incomplete",
            None,
            tuple(matches),
            tuple(candidate.score for candidate in matches if candidate.score is not None),
        )
    return _decision_for_tier(tuple(matches), reason)


def _identifier_tier(
    incoming: ReleaseSnapshot,
    candidates: tuple[MatchCandidate, ...],
    policy: MatchingPolicy,
    evidence_cache: MatchEvidenceCache | None,
    now: float | None,
    *,
    require_cached: bool,
) -> MatchDecision:
    matches: list[MatchCandidate] = []
    observations: list[tuple[ReleaseSnapshot, tuple[tuple[str, str], ...]]] = []
    cached_match_found = False
    for candidate in candidates:
        identifiers: set[tuple[str, str]] = set()
        candidate_cached = False
        for release, shared in _matching_identifier_observations(incoming, candidate):
            identifiers.update(shared)
            observation = (
                evidence_cache._get("unique_identifier", incoming, release, policy, now)
                if evidence_cache is not None and now is not None
                else None
            )
            if observation is not None and any(
                identifier in observation.identifiers for identifier in shared
            ):
                candidate_cached = True
                cached_match_found = True
            else:
                observations.append((release, shared))
        if identifiers:
            matches.append(
                MatchCandidate(
                    candidate.series_path,
                    candidate.root,
                    candidate.releases,
                    None,
                    "cached_unique_identifier" if candidate_cached else "unique_identifier",
                    tuple(sorted(identifiers)),
                )
            )
    if require_cached and not cached_match_found:
        return _decision_for_tier((), "cached_identifier_match")
    if evidence_cache is not None and now is not None:
        for release, shared_identifiers in observations:
            evidence_cache._put(
                "unique_identifier", incoming, release, policy, now, identifiers=shared_identifiers
            )
    return _decision_for_tier(
        tuple(matches),
        "cached_identifier_match" if require_cached else "unique_identifier_match",
    )


def match_series(
    incoming: ReleaseSnapshot,
    library: LibrarySnapshot,
    policy: MatchingPolicy,
    *,
    cover_similarity: CoverSimilarity | None = None,
    cache: MatchScoreCache | None = None,
    evidence_cache: MatchEvidenceCache | None = None,
    now: float | None = None,
    directory_siblings: Mapping[Path, tuple[str, ...]] | None = None,
) -> MatchDecision:
    """Match in evidence tiers, requiring uniqueness within the winning tier.

    Fresh cached image and identifier observations precede name matching.
    Prioritized and full names then precede new cover comparisons and the
    unique-identifier fallback. Cache entries cannot replace current releases
    or survive a source fingerprint, policy, or TTL change.
    ``now`` is a caller-supplied monotonic observation time. Supply complete
    ``directory_siblings`` observations to include empty and grouping folders
    in the alternative-cover word counts.
    """
    if not isinstance(incoming, ReleaseSnapshot):
        raise TypeError("incoming must be a ReleaseSnapshot")
    if not isinstance(library, LibrarySnapshot):
        raise TypeError("library must be a LibrarySnapshot")
    if not isinstance(policy, MatchingPolicy):
        raise TypeError("policy must be a MatchingPolicy")
    if not library.available:
        return MatchDecision(
            "missing_dependency",
            library.reason or "library_unavailable",
            None,
            (),
            (),
        )

    if any(not isinstance(release, ReleaseSnapshot) for release in library.releases):
        return MatchDecision(
            "invalid_input", "invalid_library_release_observation", None, (), ()
        )
    if cache is not None and not isinstance(cache, MatchScoreCache):
        raise TypeError("cache must be a MatchScoreCache")
    if evidence_cache is not None and not isinstance(evidence_cache, MatchEvidenceCache):
        raise TypeError("evidence_cache must be a MatchEvidenceCache")
    if directory_siblings is not None:
        if not isinstance(directory_siblings, Mapping):
            raise TypeError("directory_siblings must be a mapping")
        if any(
            not isinstance(parent, Path)
            or not isinstance(names, tuple)
            or any(
                not isinstance(name, str)
                or not name
                or name in {".", ".."}
                or "\x00" in name
                or Path(name).name != name
                for name in names
            )
            for parent, names in directory_siblings.items()
        ):
            return MatchDecision(
                "invalid_input", "invalid_directory_sibling_observation", None, (), ()
            )

    cache_time: float | None = None
    if (cache is not None or evidence_cache is not None) and now is not None:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            return MatchDecision("invalid_input", "invalid_cache_time", None, (), ())
        cache_time = float(now)
        observed = {
            release.path: _source_fingerprint(release) for release in library.releases
        }
        if cache is not None:
            cache._refresh(incoming, observed, policy, cache_time)
        if evidence_cache is not None:
            evidence_cache._refresh(incoming, observed, policy, cache_time)

    all_candidates = _base_candidates(incoming, library, policy)
    if not all_candidates:
        return MatchDecision("no_match", "no_suitable_series_folder_or_root", None, (), ())

    image_candidates = (
        _image_candidates(incoming, all_candidates, library, policy, directory_siblings)
        if policy.match_through_image_similarity
        else ()
    )

    if (
        evidence_cache is not None
        and cache_time is not None
        and any(
            (
                observation := evidence_cache._get(
                    "cover_similarity", incoming, release, policy, cache_time
                )
            )
            is not None
            and observation.score is not None
            and observation.score >= policy.required_image_similarity_score
            for candidate in image_candidates
            for release in _matching_cover_releases(incoming, candidate)
        )
    ):
        return _cover_tier(
            incoming,
            image_candidates,
            policy,
            cover_similarity,
            evidence_cache,
            cache_time,
            reason="cached_cover_similarity_match",
        )

    if (
        evidence_cache is not None
        and cache_time is not None
        and policy.match_through_identifiers
    ):
        cached_identifiers = _identifier_tier(
            incoming,
            all_candidates,
            policy,
            evidence_cache,
            cache_time,
            require_cached=True,
        )
        if cached_identifiers.status != "no_match":
            return cached_identifiers

    incoming_name = clean_str(incoming.display_series)
    incoming_key = (
        incoming.canonical_series_key.casefold().strip()
        or incoming_name.casefold()
    )
    prioritized = tuple(
        MatchCandidate(
            item.series_path, item.root, item.releases, 1.0, "prioritized_name"
        )
        for item in all_candidates
        if incoming_key and incoming_key == clean_str(item.series_path.name).casefold().strip()
    )
    if prioritized:
        return _decision_for_tier(prioritized, "prioritized_name_match")

    full_matches: list[MatchCandidate] = []
    if incoming_name:
        for candidate in all_candidates:
            score = _name_similarity(
                candidate.releases[0],
                incoming,
                clean_str(candidate.series_path.name),
                incoming_name,
                policy,
                cache,
                cache_time,
            )
            if score >= policy.required_similarity_score:
                full_matches.append(
                    MatchCandidate(
                        candidate.series_path,
                        candidate.root,
                        candidate.releases,
                        score,
                        "full_name",
                    )
                )
    if full_matches:
        return _decision_for_tier(tuple(full_matches), "full_name_match")

    if image_candidates:
        images = _cover_tier(
            incoming,
            image_candidates,
            policy,
            cover_similarity,
            evidence_cache,
            cache_time,
            reason="cover_similarity_match",
        )
        if images.status != "no_match":
            return images

    if policy.match_through_identifiers:
        identifiers = _identifier_tier(
            incoming,
            all_candidates,
            policy,
            evidence_cache,
            cache_time,
            require_cached=False,
        )
        if identifiers.status != "no_match":
            return identifiers

    return MatchDecision("no_match", "no_matching_evidence", None, (), ())
