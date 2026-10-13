"""Pure library classification and destination-root suitability decisions.

This module uses only caller-supplied file/release observations. It never walks
or opens a root, and it never assumes an empty collection satisfies restrictions.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import regex
from regex import Pattern

from komga_cover_extractor.domain.libraries import LibraryRoot
from komga_cover_extractor.domain.releases import ReleaseSnapshot
from komga_cover_extractor.parsing.normalization import (
    clean_str,
    remove_brackets,
    similar,
)


@dataclass(frozen=True, slots=True)
class LibraryType:
    """Pure classification rule copied from configuration at bootstrap."""

    name: str
    extensions: tuple[str, ...]
    must_contain: tuple[str, ...]
    must_not_contain: tuple[str, ...]
    match_percentage: int = 90

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("library type name must be non-empty")
        for field_name in ("extensions", "must_contain", "must_not_contain"):
            raw_values = getattr(self, field_name)
            if isinstance(raw_values, str) or not isinstance(raw_values, (tuple, list)):
                raise TypeError(f"{field_name} must be a tuple or list of strings")
            values = tuple(raw_values)
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{field_name} must contain non-empty strings")
            object.__setattr__(self, field_name, values)
        if (
            isinstance(self.match_percentage, bool)
            or not isinstance(self.match_percentage, int)
            or not 0 <= self.match_percentage <= 100
        ):
            raise ValueError("match_percentage must be an integer from 0 through 100")


ClassificationStatus = Literal["matched", "no_match", "invalid_input"]
RootSuitabilityStatus = Literal["suitable", "no_match", "invalid_input"]
RootRoutingStatus = Literal["routed", "no_match", "ambiguous", "invalid_input"]

@dataclass(frozen=True, slots=True)
class ClassificationPolicy:
    """An immutable classification snapshot with the characterized defaults."""

    library_types: tuple[LibraryType, ...] = ()
    manga_extensions: tuple[str, ...] = (".zip", ".cbz")
    novel_extensions: tuple[str, ...] = (".epub",)
    supported_extensions: tuple[str, ...] = (".epub", ".zip", ".cbz")
    common_extension_threshold: float = 0.30
    chapter_threshold: float = 0.90
    volume_threshold: float = 0.90
    sample_fraction: float = 0.75
    sample_minimum_files: int = 100
    folder_name_agreement_threshold: float = 0.80
    rename_similarity_floor: float = 0.25
    required_similarity_score: float = 0.979
    _compiled_library_types: tuple[
        tuple[tuple[Pattern[str], ...], tuple[Pattern[str], ...]], ...
    ] = field(init=False, repr=False, compare=False)

    _library_extensions: tuple[frozenset[str], ...] = field(
        init=False, repr=False, compare=False
    )
    _fingerprint: tuple[object, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        library_types = tuple(self.library_types)
        if any(not isinstance(item, LibraryType) for item in library_types):
            raise TypeError("library_types must contain LibraryType values")
        object.__setattr__(self, "library_types", library_types)
        for name in (
            "manga_extensions", "novel_extensions", "supported_extensions"
        ):
            raw_values = getattr(self, name)
            if isinstance(raw_values, str) or not isinstance(raw_values, (tuple, list)):
                raise TypeError(f"{name} must be a tuple or list of extensions")
            values = tuple(raw_values)
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} must contain non-empty extensions")
            object.__setattr__(self, name, tuple(value.casefold() for value in values))
        for name in (
            "common_extension_threshold", "chapter_threshold", "volume_threshold",
            "sample_fraction", "folder_name_agreement_threshold",
            "rename_similarity_floor", "required_similarity_score",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between zero and one")
            object.__setattr__(self, name, float(value))
        if (
            isinstance(self.sample_minimum_files, bool)
            or not isinstance(self.sample_minimum_files, int)
            or self.sample_minimum_files < 1
        ):
            raise ValueError("sample_minimum_files must be a positive integer")

        compiled_rules: list[tuple[tuple[Pattern[str], ...], tuple[Pattern[str], ...]]] = []
        compiled_extensions = tuple(
            frozenset(extension.casefold() for extension in rule.extensions)
            for rule in library_types
        )
        for rule in library_types:
            try:
                must_contain = tuple(
                    regex.compile(pattern, regex.IGNORECASE)
                    for pattern in rule.must_contain
                )
                must_not_contain = tuple(
                    regex.compile(pattern, regex.IGNORECASE)
                    for pattern in rule.must_not_contain
                )
            except regex.error as error:
                raise ValueError(f"invalid LibraryType regex: {error}") from None
            compiled_rules.append((must_contain, must_not_contain))
        object.__setattr__(self, "_compiled_library_types", tuple(compiled_rules))
        object.__setattr__(self, "_library_extensions", compiled_extensions)
        object.__setattr__(
            self,
            "_fingerprint",
            (
                tuple(
                    (
                        rule.name,
                        tuple(rule.extensions),
                        tuple(rule.must_contain),
                        tuple(rule.must_not_contain),
                        rule.match_percentage,
                    )
                    for rule in library_types
                ),
                self.manga_extensions,
                self.novel_extensions,
                self.supported_extensions,
                self.common_extension_threshold,
                self.chapter_threshold,
                self.volume_threshold,
                self.sample_fraction,
                self.sample_minimum_files,
                self.folder_name_agreement_threshold,
                self.rename_similarity_floor,
                self.required_similarity_score,
            ),
        )

    @property
    def fingerprint(self) -> tuple[object, ...]:
        """Stable value key for this behavior policy, independent of object identity."""
        return self._fingerprint


@dataclass(frozen=True, slots=True)
class LibraryTypeScore:
    library_type: LibraryType
    matched_files: int
    total_files: int
    percentage: float
    required_percentage: float

    @property
    def matched(self) -> bool:
        return self.percentage >= self.required_percentage


@dataclass(frozen=True, slots=True)
class LibraryTypeDecision:
    status: ClassificationStatus
    reason: str
    library_type: LibraryType | None
    candidates: tuple[LibraryType, ...]
    scores: tuple[LibraryTypeScore, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "scores", tuple(self.scores))


@dataclass(frozen=True, slots=True)
class RootClassificationDecision:
    status: ClassificationStatus
    reason: str
    formats: tuple[str, ...]
    extensions: tuple[str, ...]
    total_files: int
    sample_size: int
    chapter_percentage: float
    volume_percentage: float
    extension_percentages: tuple[tuple[str, float], ...]
    library_type: LibraryTypeDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "formats", tuple(self.formats))
        object.__setattr__(self, "extensions", tuple(self.extensions))
        object.__setattr__(self, "extension_percentages", tuple(self.extension_percentages))


@dataclass(frozen=True, slots=True)
class FolderSeriesDecision:
    status: ClassificationStatus
    reason: str
    agreement: float | None
    candidates: tuple[ReleaseSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True, slots=True)
class RootSuitabilityDecision:
    status: RootSuitabilityStatus
    reason: str
    root: LibraryRoot
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True, slots=True)
class RootRoutingDecision:
    status: RootRoutingStatus
    reason: str
    selected: LibraryRoot | None
    candidates: tuple[LibraryRoot, ...]
    suitability: tuple[RootSuitabilityDecision, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "suitability", tuple(self.suitability))


def _release_name(value: ReleaseSnapshot | Path | str) -> str:
    if isinstance(value, ReleaseSnapshot):
        return value.name
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        return Path(value).name
    raise TypeError("classification inputs must be release snapshots or paths")


def _release_extension(value: ReleaseSnapshot | Path | str) -> str:
    return Path(_release_name(value)).suffix.casefold()




def _matches_library_type(
    name: str,
    extension: str,
    allowed_extensions: frozenset[str],
    compiled: tuple[tuple[Pattern[str], ...], tuple[Pattern[str], ...]],
) -> bool:
    must_contain, must_not_contain = compiled
    return (
        extension in allowed_extensions
        and all(pattern.search(name) is not None for pattern in must_contain)
        and all(pattern.search(name) is None for pattern in must_not_contain)
    )


def classify_library_type(
    files: Sequence[ReleaseSnapshot | Path | str],
    policy: ClassificationPolicy,
    *,
    required_match_percentage: int | float | None = None,
) -> LibraryTypeDecision:
    """Classify with ordered ``LibraryType`` rules, stopping at the first match."""
    if not isinstance(policy, ClassificationPolicy):
        raise TypeError("policy must be a ClassificationPolicy")
    values = tuple(files)
    if not values:
        return LibraryTypeDecision("no_match", "empty_input", None, (), ())
    try:
        names = tuple(_release_name(value) for value in values)
        extensions = tuple(_release_extension(value) for value in values)
    except TypeError:
        return LibraryTypeDecision("invalid_input", "invalid_file_observation", None, (), ())

    if required_match_percentage is not None:
        if (
            isinstance(required_match_percentage, bool)
            or not isinstance(required_match_percentage, (int, float))
            or not math.isfinite(float(required_match_percentage))
            or not 0.0 <= float(required_match_percentage) <= 100.0
        ):
            return LibraryTypeDecision(
                "invalid_input", "invalid_required_match_percentage", None, (), ()
            )

    scores: list[LibraryTypeScore] = []
    matching: list[LibraryType] = []
    for rule, compiled, allowed_extensions in zip(
        policy.library_types,
        policy._compiled_library_types,
        policy._library_extensions,
    ):
        matched_count = sum(
            _matches_library_type(name, extension, allowed_extensions, compiled)
            for name, extension in zip(names, extensions)
        )
        percentage = matched_count / len(values) * 100.0
        threshold = (
            float(required_match_percentage)
            if required_match_percentage is not None
            else float(rule.match_percentage)
        )
        score = LibraryTypeScore(
            rule, matched_count, len(values), percentage, threshold
        )
        scores.append(score)
        if score.matched:
            matching.append(rule)
            return LibraryTypeDecision(
                "matched", "first_ordered_rule_matched", rule, tuple(matching), tuple(scores)
            )
    return LibraryTypeDecision(
        "no_match", "no_ordered_rule_matched", None, tuple(matching), tuple(scores)
    )


def classify_root(
    releases: Sequence[ReleaseSnapshot], policy: ClassificationPolicy
) -> RootClassificationDecision:
    """Infer root format/extension policy using the legacy bounded sample rules."""
    if not isinstance(policy, ClassificationPolicy):
        raise TypeError("policy must be a ClassificationPolicy")
    observed = tuple(releases)
    empty_type = LibraryTypeDecision("no_match", "empty_input", None, (), ())
    if not observed:
        return RootClassificationDecision(
            "no_match", "empty_input", (), (), 0, 0, 0.0, 0.0, (), empty_type
        )
    if any(not isinstance(release, ReleaseSnapshot) for release in observed):
        return RootClassificationDecision(
            "invalid_input", "invalid_release_observation", (), (), len(observed), 0,
            0.0, 0.0, (), empty_type,
        )

    sample_size = len(observed)
    if sample_size >= policy.sample_minimum_files:
        sample_size = int(sample_size * policy.sample_fraction)
    sample = observed[:sample_size]
    if not sample:
        return RootClassificationDecision(
            "no_match", "empty_classification_sample", (), (), len(observed), 0,
            0.0, 0.0, (), empty_type,
        )

    extension_counts: dict[str, int] = {}
    kinds = {"chapter": 0, "volume": 0}
    for release in sample:
        extension = release.path.suffix.casefold()
        extension_counts[extension] = extension_counts.get(extension, 0) + 1
        if release.kind in kinds:
            kinds[release.kind] += 1

    extension_percentages = tuple(
        (extension, count / len(sample) * 100.0)
        for extension, count in extension_counts.items()
    )
    common_extensions = [
        extension
        for extension, count in extension_counts.items()
        if count / len(sample) >= policy.common_extension_threshold
    ]
    if not common_extensions:
        selected_extensions = list(policy.supported_extensions)
    else:
        selected_extensions = list(common_extensions)
        for family in (policy.manga_extensions, policy.novel_extensions):
            if any(extension in family for extension in common_extensions):
                selected_extensions.extend(
                    extension for extension in family if extension not in selected_extensions
                )

    chapter_percentage = kinds["chapter"] / len(sample) * 100.0
    volume_percentage = kinds["volume"] / len(sample) * 100.0
    formats: tuple[str, ...]
    if chapter_percentage / 100.0 >= policy.chapter_threshold:
        formats = ("chapter",)
    elif volume_percentage / 100.0 >= policy.volume_threshold:
        formats = ("volume",)
    else:
        formats = ("chapter", "volume")

    library_type = classify_library_type(sample, policy)
    if not any(kinds.values()):
        return RootClassificationDecision(
            "no_match", "no_classifiable_release_kinds", formats,
            tuple(selected_extensions), len(observed), len(sample),
            chapter_percentage, volume_percentage, extension_percentages, library_type,
        )
    return RootClassificationDecision(
        "matched", "root_policy_inferred", formats, tuple(selected_extensions),
        len(observed), len(sample), chapter_percentage, volume_percentage,
        extension_percentages, library_type,
    )


def folder_series_agreement(
    releases: Sequence[ReleaseSnapshot], policy: ClassificationPolicy
) -> FolderSeriesDecision:
    """Measure how many releases agree with the first release's series name.

    This is the characterized 80% folder-name consensus gate. No first release
    is selected from an empty input, and a missing name counts as disagreement.
    """
    if not isinstance(policy, ClassificationPolicy):
        raise TypeError("policy must be a ClassificationPolicy")
    observed = tuple(releases)
    if not observed:
        return FolderSeriesDecision("no_match", "empty_input", None, ())
    if any(not isinstance(release, ReleaseSnapshot) for release in observed):
        return FolderSeriesDecision("invalid_input", "invalid_release_observation", None, ())
    anchor = clean_str(observed[0].display_series)
    if not anchor:
        return FolderSeriesDecision("no_match", "missing_anchor_series_name", 0.0, observed)
    matching = 1
    for release in observed[1:]:
        name = clean_str(release.display_series)
        if name and similar(name, anchor) >= policy.required_similarity_score:
            matching += 1
    agreement = matching / len(observed)
    matched = agreement >= policy.folder_name_agreement_threshold
    return FolderSeriesDecision(
        "matched" if matched else "no_match",
        "folder_series_agreement_met" if matched else "folder_series_agreement_below_threshold",
        agreement,
        observed,
    )


def folder_rename_similarity(
    series_name: str, folder_name: str, policy: ClassificationPolicy
) -> float:
    """Return characterized similarity for the 0.25 folder-rename floor."""
    if not isinstance(policy, ClassificationPolicy):
        raise TypeError("policy must be a ClassificationPolicy")
    if not isinstance(series_name, str) or not isinstance(folder_name, str):
        raise TypeError("series_name and folder_name must be strings")
    without_brackets = max(
        similar(
            remove_brackets(series_name),
            remove_brackets(folder_name),
        ),
        similar(series_name, folder_name),
    )
    return without_brackets


def folder_name_is_rename_candidate(
    series_name: str, folder_name: str, policy: ClassificationPolicy
) -> bool:
    return folder_rename_similarity(series_name, folder_name, policy) >= policy.rename_similarity_floor


def check_root_suitability(
    releases: Sequence[ReleaseSnapshot],
    root: LibraryRoot,
    policy: ClassificationPolicy,
    *,
    classification_files: Sequence[ReleaseSnapshot] | None = None,
) -> RootSuitabilityDecision:
    """Check every incoming observation against one root's explicit restrictions."""
    if not isinstance(root, LibraryRoot) or not isinstance(policy, ClassificationPolicy):
        raise TypeError("root and policy must be LibraryRoot and ClassificationPolicy values")
    incoming = tuple(releases)
    if not incoming:
        return RootSuitabilityDecision("no_match", "empty_input", root)
    if any(not isinstance(release, ReleaseSnapshot) for release in incoming):
        return RootSuitabilityDecision("invalid_input", "invalid_release_observation", root)

    disallowed_extensions = tuple(
        release.name
        for release in incoming
        if root.extensions and release.path.suffix.casefold()
        not in {extension.casefold() for extension in root.extensions}
    )
    if disallowed_extensions:
        return RootSuitabilityDecision(
            "no_match", "extension_restriction", root, disallowed_extensions
        )
    disallowed_formats = tuple(
        release.name
        for release in incoming
        if root.formats and release.kind not in root.formats
    )
    if disallowed_formats:
        return RootSuitabilityDecision(
            "no_match", "format_restriction", root, disallowed_formats
        )

    if root.library_types:
        classified_files = tuple(classification_files or incoming)
        if not classified_files:
            return RootSuitabilityDecision("no_match", "library_type_unobserved", root)
        classification = classify_library_type(classified_files, policy)
        if classification.status != "matched" or classification.library_type is None:
            return RootSuitabilityDecision(
                "no_match", "library_type_unclassified", root
            )
        if classification.library_type.name not in root.library_types:
            return RootSuitabilityDecision(
                "no_match", "library_type_restriction", root,
                (classification.library_type.name,),
            )
    return RootSuitabilityDecision("suitable", "all_restrictions_satisfied", root)


def route_releases_to_root(
    releases: Sequence[ReleaseSnapshot],
    roots: Sequence[LibraryRoot],
    policy: ClassificationPolicy,
) -> RootRoutingDecision:
    """Select only a unique root that accepts every supplied release."""
    incoming = tuple(releases)
    destinations = tuple(roots)
    if not isinstance(policy, ClassificationPolicy):
        return RootRoutingDecision("invalid_input", "invalid_classification_policy", None, (), ())
    if any(not isinstance(release, ReleaseSnapshot) for release in incoming):
        return RootRoutingDecision("invalid_input", "invalid_release_observation", None, (), ())
    if not incoming:
        return RootRoutingDecision("no_match", "empty_input", None, (), ())
    if not destinations:
        return RootRoutingDecision("no_match", "no_destination_roots", None, (), ())
    if any(not isinstance(root, LibraryRoot) for root in destinations):
        return RootRoutingDecision("invalid_input", "invalid_destination_root", None, (), ())
    suitability = tuple(
        check_root_suitability(incoming, root, policy) for root in destinations
    )
    eligible = tuple(item.root for item in suitability if item.status == "suitable")
    if not eligible:
        return RootRoutingDecision(
            "no_match", "no_suitable_destination", None, (), suitability
        )
    if len(eligible) > 1:
        return RootRoutingDecision("ambiguous", "multiple_suitable_destinations", None, eligible, suitability)
    return RootRoutingDecision("routed", "unique_suitable_destination", eligible[0], eligible, suitability)
