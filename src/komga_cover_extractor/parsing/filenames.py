from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from typing import cast

import regex
from unidecode import unidecode

from komga_cover_extractor.domain.releases import (
    FileObservation,
    ReleaseNumber,
    ReleaseSnapshot,
)

from .normalization import (
    clean_str,
    contains_brackets,
    remove_brackets,
    remove_dual_space,
    replace_underscores,
    similar,
)
from .numbers import (
    _FILE_EXTENSIONS,
    NumberPolicy,
    _contains_chapter_keyword,
    _contains_volume_keyword,
    _find_number_match,
    _get_extension,
    _part_after_number,
    parse_release_number,
)

titlecase = cast(Callable[[str], str], import_module("titlecase").titlecase)

_DEFAULT_EXCEPTION_KEYWORDS = (
    r"Extra",
    r"Ones?(-|)shot",
    r"Omake",
    r"Special",
    r"Bonus",
    r"Side(-|)story",
)
_DEFAULT_IMAGE_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".tbn", ".webp"))
_DEFAULT_NOVEL_EXTENSIONS = frozenset((".epub",))
_DEFAULT_MANGA_EXTENSIONS = frozenset((".zip", ".cbz"))
_MINIMUM_METADATA_YEAR = 1950


@dataclass(frozen=True, slots=True)
class _ParsingPatterns:
    exception: regex.Pattern[str]
    release_group_end: regex.Pattern[str]
    release_groups: regex.Pattern[str]
    publishers: regex.Pattern[str]


def _joined(patterns: tuple[str, ...]) -> str:
    return "|".join(patterns) if patterns else r"(?!)"


def _compile(pattern: str, *, name: str) -> regex.Pattern[str]:
    try:
        return regex.compile(pattern, flags=regex.IGNORECASE)
    except regex.error as error:
        raise ValueError(f"invalid {name} regex policy: {error}") from error


def _compile_parsing_patterns(
    exception_keywords: tuple[str, ...],
    release_groups: tuple[str, ...],
    publishers: tuple[str, ...],
) -> _ParsingPatterns:
    extensions_expression = r"|".join(regex.escape(extension) for extension in _FILE_EXTENSIONS)
    release_group_expression = _joined(release_groups)
    publisher_expression = _joined(publishers)
    return _ParsingPatterns(
        exception=_compile(_joined(exception_keywords), name="exception keyword"),
        release_group_end=_compile(
            rf"-(?! )([^\(\)\[\]\{{\}}]+)(?:{extensions_expression})$",
            name="release group",
        ),
        release_groups=_compile(
            rf"(?<=[\(\[\{{])({release_group_expression})(?=[\)\]\}}])",
            name="release-group dictionary",
        ),
        publishers=_compile(
            rf"(?<=[\(\[\{{])({publisher_expression})(?=[\)\]\}}])",
            name="publisher dictionary",
        ),
    )


@dataclass(frozen=True, slots=True)
class ParsingPolicy:
    """Small immutable parsing snapshot; no configuration or I/O ownership."""

    number_policy: NumberPolicy = field(default_factory=NumberPolicy)
    exception_keywords: tuple[str, ...] = _DEFAULT_EXCEPTION_KEYWORDS
    release_groups: tuple[str, ...] = ()
    publishers: tuple[str, ...] = ()
    average_chapter_image_count: int = 85
    chapter_support_toggle: bool = False
    _compiled: _ParsingPatterns = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.number_policy, NumberPolicy):
            raise TypeError("number_policy must be a NumberPolicy")
        for name in ("exception_keywords", "release_groups", "publishers"):
            values = getattr(self, name)
            if isinstance(values, str):
                raise TypeError(f"{name} must be a tuple of regex strings")
            if not isinstance(values, tuple):
                values = tuple(values)
                object.__setattr__(self, name, values)
            if any(not isinstance(value, str) for value in values):
                raise TypeError(f"{name} must contain strings")
            for value in values:
                if not value:
                    raise ValueError(f"{name} cannot contain empty patterns")
                _compile(value, name=name)
        if (
            isinstance(self.average_chapter_image_count, bool)
            or not isinstance(self.average_chapter_image_count, int)
            or self.average_chapter_image_count < 1
        ):
            raise ValueError("average_chapter_image_count must be a positive integer")
        if type(self.chapter_support_toggle) is not bool:
            raise TypeError("chapter_support_toggle must be bool")
        object.__setattr__(
            self,
            "_compiled",
            _compile_parsing_patterns(
                self.exception_keywords, self.release_groups, self.publishers
            ),
        )


@dataclass(frozen=True, slots=True)
class RenamePolicy:
    """Per-invocation filename rendering settings derived from configuration."""

    preferred_volume_renaming_format: str = "v"
    preferred_chapter_renaming_format: str = "c"
    zfill_volume_int_value: int = 2
    zfill_volume_float_value: int = 4
    zfill_chapter_int_value: int = 3
    zfill_chapter_float_value: int = 5
    add_volume_one_number_to_one_shots: bool = False
    add_issue_number_to_manga_file_name: bool = False
    restructure_when_renaming: bool = False
    search_and_add_premium_to_file_name: bool = False
    add_publisher_name_to_file_name_when_renaming: bool = False
    publishers: tuple[str, ...] = ()
    move_release_group_to_end_of_file_name: bool = False
    replace_unicode_when_restructuring: bool = False
    rename_chapters_with_preferred_chapter_keyword: bool = False
    publisher_similarity_score: float = 0.9
    release_group_similarity_score: float = 0.8
    required_similarity_score: float = 0.979
    number_policy: NumberPolicy = field(default_factory=NumberPolicy)
    _publishers: regex.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "preferred_volume_renaming_format",
            "preferred_chapter_renaming_format",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in (
            "zfill_volume_int_value",
            "zfill_volume_float_value",
            "zfill_chapter_int_value",
            "zfill_chapter_float_value",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "add_volume_one_number_to_one_shots",
            "add_issue_number_to_manga_file_name",
            "restructure_when_renaming",
            "search_and_add_premium_to_file_name",
            "add_publisher_name_to_file_name_when_renaming",
            "move_release_group_to_end_of_file_name",
            "replace_unicode_when_restructuring",
            "rename_chapters_with_preferred_chapter_keyword",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if not isinstance(self.number_policy, NumberPolicy):
            raise TypeError("number_policy must be a NumberPolicy")
        if isinstance(self.publishers, str):
            raise TypeError("publishers must be a tuple of literal names")
        if not isinstance(self.publishers, tuple):
            object.__setattr__(self, "publishers", tuple(self.publishers))
        for publisher in self.publishers:
            if not isinstance(publisher, str) or not publisher:
                raise ValueError("publishers must contain non-empty literal names")
        publisher_expression = _joined(tuple(regex.escape(publisher) for publisher in self.publishers))
        object.__setattr__(
            self,
            "_publishers",
            _compile(
                rf"(?<=[\(\[\{{])({publisher_expression})(?=[\)\]\}}])",
                name="publisher dictionary",
            ),
        )
        for name in (
            "publisher_similarity_score",
            "release_group_similarity_score",
            "required_similarity_score",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")


_VOLUME_YEAR_PATTERN = regex.compile(r"(\(|\[|\{)(\d{4})(\)|\]|\})")
_YEAR_OR_DIGITAL_PATTERN = regex.compile(
    r"([\[\{\(]((\d{4})|(Digital))[\]\}\)])", flags=regex.IGNORECASE
)
_SUBTITLE_SEPARATOR_PATTERN = regex.compile(r"((\s+(-)|:)\s+)")


def _is_year_marker(value: str) -> bool:
    return len(value) == 4 and value.isdigit()


def _release_year(name: str, metadata: dict[str, str]) -> int | None:
    match = _VOLUME_YEAR_PATTERN.search(name)
    if match:
        return int(match.group()[1:-1])

    value: str | None = None
    if "Summary" in metadata and "Year" in metadata:
        value = metadata["Year"]
    elif "dc:description" in metadata and "dc:date" in metadata:
        value = metadata["dc:date"].strip().split("-")[0]
    if value is None or not (len(value) == 4 and value.isdigit()):
        return None
    year = int(value)
    return year if year >= _MINIMUM_METADATA_YEAR else None


def _publisher_from_metadata(metadata: dict[str, str]) -> str | None:
    key = "Publisher" if "Publisher" in metadata else "dc:publisher"
    if key not in metadata:
        return None
    publisher = remove_dual_space(titlecase(metadata[key])).strip()
    if "llc" in publisher.lower():
        publisher = regex.sub(r", LLC.*", "", publisher, flags=regex.IGNORECASE).strip()
    if key == "dc:publisher":
        publisher = publisher.replace("LLC", "").strip()
        publisher = publisher.replace(":", " - ").strip()
    return remove_dual_space(publisher)


def _publisher_from_name(name: str, policy: ParsingPolicy) -> str | None:
    if not policy.publishers:
        return None
    match = policy._compiled.publishers.search(name)
    return match.group() if match else None


def _release_group(name: str, series_name: str, policy: ParsingPolicy) -> str | None:
    series_free_name = (
        regex.sub(regex.escape(series_name), "", name, flags=regex.IGNORECASE).strip()
        if series_name
        else ""
    )
    search = (
        policy._compiled.release_group_end.search(series_free_name or name)
        if "-" in name
        else None
    )
    if search:
        return search.group(1)
    if policy.release_groups and contains_brackets(name):
        matches = tuple(policy._compiled.release_groups.finditer(name))
        if matches:
            return matches[-1].group(1)
    return None


def _remove_matching(text: str, pattern: str) -> str:
    return regex.sub(rf"\b{regex.escape(pattern)}\b", "", text, flags=regex.IGNORECASE).strip()


def _get_extras(
    file_name: str,
    *,
    kind: str,
    series_name: str,
    subtitle: str,
    extension: str,
) -> tuple[str, ...]:
    if series_name:
        file_name = _remove_matching(file_name, series_name)
    if subtitle:
        file_name = _remove_matching(file_name, subtitle)
    results = regex.findall(r"((?:\{|\(|\[).*?(?:\]|\)|\}))", file_name, flags=regex.IGNORECASE)
    excluded: tuple[str, ...] = (
        r"((\{|\(|\[)(Premium|J-Novel Club Premium)(\]|\)|\}))",
        r"(\((\d{4})\))",
    )
    if kind == "chapter":
        excluded += (r"((\{|\(|\[)Part([-_. ]|)([0-9]+)(\]|\)|\}))",)
    combined = "|".join(excluded)
    results = [item for item in results if not regex.search(combined, item, flags=regex.IGNORECASE)]
    results = list(dict.fromkeys(results))

    if extension in _DEFAULT_NOVEL_EXTENSIONS:
        modifier = "[%s]"
    elif extension in _DEFAULT_MANGA_EXTENSIONS:
        modifier = "(%s)"
    else:
        modifier = "%s"

    part_match = (
        regex.search(r"(\s|\b)Part([-_. ]|)([0-9]+)", file_name, flags=regex.IGNORECASE)
        if "part" in file_name.lower()
        else None
    )
    if part_match:
        token = modifier % part_match.group().strip()
        if token not in results:
            results.append(token)
    if "premium" in file_name.lower():
        token = modifier % "Premium"
        if token not in results:
            results.append(token)
    premium = [item for item in results if "premium" in item.lower()]
    ordinary = [item for item in results if "premium" not in item.lower()]
    return tuple(premium + ordinary)



def _subtitle(
    name: str,
    extension: str,
    series_name: str,
    publishers: tuple[str, ...],
    number: ReleaseNumber,
    parent_name: str,
    number_policy: NumberPolicy,
) -> str | None:
    without_series = regex.sub(
        rf"^{regex.escape(series_name)}", "", name, flags=regex.IGNORECASE
    ).strip() if series_name else name
    dash_or_colon = _SUBTITLE_SEPARATOR_PATTERN.search(without_series) if ("-" in without_series or ":" in without_series) else None
    marker = _YEAR_OR_DIGITAL_PATTERN.search(without_series)
    publisher_marker = None
    if not marker:
        for publisher in publishers:
            publisher_marker = regex.search(
                rf"([\[\{{\(])({regex.escape(publisher)})([\]\}}\)])",
                without_series,
                flags=regex.IGNORECASE,
            )
            if publisher_marker:
                break
    if not (dash_or_colon and (marker or publisher_marker)):
        return None

    subtitle = regex.sub(r"(.*)((\s+(-)|:)\s+)", "", without_series)
    if extension and subtitle.endswith(extension):
        subtitle = subtitle[: -len(extension)]
    if not publisher_marker:
        subtitle = regex.sub(
            r"([\[\{\(]((\d{4})|(Digital))[\]\}\)])(.*)",
            "",
            subtitle,
            flags=regex.IGNORECASE,
        )
    else:
        subtitle = regex.sub(
            rf"([\[\{{\(])({regex.escape(publisher_marker.group(2))})([\]\}}\)])(.*)",
            "",
            subtitle,
            flags=regex.IGNORECASE,
        )
    subtitle = remove_dual_space(subtitle).strip()
    if subtitle and regex.search(regex.escape(subtitle), parent_name, flags=regex.IGNORECASE):
        return None
    if subtitle and number.values:
        number_text = "-".join(number.raw_tokens)
        keyword_expression = number_policy._compiled.volume_keywords_expression
        if keyword_expression != r"(?!)" and regex.fullmatch(
            rf"(?:{keyword_expression})(\s+)?(0+)?{regex.escape(number_text)}",
            subtitle.strip(),
            flags=regex.IGNORECASE,
        ):
            return None
    return subtitle or None


def _file_is_exception(name: str, policy: ParsingPolicy) -> bool:
    return bool(policy.exception_keywords and policy._compiled.exception.search(name))


def _sibling_file_names(observation: FileObservation, policy: ParsingPolicy) -> tuple[str, ...]:
    candidates = tuple(dict.fromkeys((observation.path, *observation.siblings)))
    names: list[str] = []
    for sibling in candidates:
        name = sibling.name
        if name.startswith(".") or _get_extension(name) not in _FILE_EXTENSIONS:
            continue
        if not policy.chapter_support_toggle:
            is_volume = _contains_volume_keyword(name, policy.number_policy)
            if not is_volume and _contains_chapter_keyword(name, policy.number_policy):
                continue
        names.append(name)
    return tuple(names)


def _is_one_shot(
    name: str,
    observation: FileObservation,
    policy: ParsingPolicy,
    *,
    has_volume_keyword: bool,
    has_chapter_keyword: bool,
) -> bool:
    if has_volume_keyword or has_chapter_keyword or _file_is_exception(name, policy):
        return False
    if observation.at_configured_root and observation.root_role in {"download", "both"}:
        return True
    return len(_sibling_file_names(observation, policy)) == 1


def _page_heuristic(observation: FileObservation, policy: ParsingPolicy) -> bool:
    pages = tuple(
        page
        for page in observation.archive_pages
        if not page.is_directory
        and not page.name.rsplit("/", 1)[-1].startswith((".", "__"))
        and _get_extension(page.name).lower() in _DEFAULT_IMAGE_EXTENSIONS
    )
    if not pages:
        return False
    first = min(observation.archive_pages, key=lambda page: page.name)
    grayscale_first = (
        not first.is_directory
        and _get_extension(first.name).lower() in _DEFAULT_IMAGE_EXTENSIONS
        and first.is_grayscale is True
    )
    return grayscale_first or len(pages) <= policy.average_chapter_image_count


def _series_name_from_contents(folder_name: str, file_names: Sequence[str]) -> str:
    if not file_names:
        return ""
    minimum = len(file_names)
    series_name = ""
    for index, character in enumerate(folder_name):
        matches = sum(
            1
            for name in file_names
            if index < len(name) and name[index].lower() == character.lower()
        )
        if matches < minimum:
            break
        series_name += character
    return series_name.strip() if len(series_name) >= 3 else ""




def _volume_series_name(
    file_name: str,
    observation: FileObservation,
    number: ReleaseNumber,
    policy: ParsingPolicy,
    one_shot: bool,
    *,
    second: bool = False,
) -> str:
    name = file_name
    if name.startswith(("(", "[", "{")) and regex.search(
        r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+(\s+[A-Za-z]{2})", name
    ):
        name = regex.sub(r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+\s+", "", name).strip()
    name = remove_dual_space(name.replace("_extra", ".5")).strip()
    if "one" in name.lower() and "shot" in name.lower():
        name = regex.sub(r"(-\s*)Ones?(-|)shot\s*", "", name, flags=regex.IGNORECASE).strip()
    if "_" in name:
        name = replace_underscores(name)

    volume_keywords = policy.number_policy._compiled.volume_keywords_expression
    if not one_shot:
        one_shot = _is_one_shot(
            name,
            observation,
            policy,
            has_volume_keyword=_contains_volume_keyword(name, policy.number_policy),
            has_chapter_keyword=_contains_chapter_keyword(name, policy.number_policy),
        )
    if one_shot:
        extension_words = r"|".join(regex.escape(item[1:]) for item in _FILE_EXTENSIONS)
        name = regex.sub(
            rf"([-_ ]+|)(((\[|\(|\{{).*(\]|\)|\}}))|LN)([-_. ]+|)(?:{extension_words}|).*",
            "",
            name,
            flags=regex.IGNORECASE,
        ).strip()
    else:
        volume_pattern = (
            rf"(\b|\s)(?<![A-Za-z])((\s|)-(\s|)|)(Part|)(\[|\(|\{{)?"
            rf"(?:{volume_keywords})(\.|)([-_. ]|)([0-9]+)(\b|\s).*"
        )
        match = regex.search(volume_pattern, name, flags=regex.IGNORECASE)
        if match:
            name = name[:match.start()].strip()
        else:
            extension_expression = r"|".join(regex.escape(item) for item in _FILE_EXTENSIONS)
            name = regex.sub(
                rf"(\d+)?([-_. ]+)?((\[|\(|\{{)(.*)(\]|\)|\}}))?([-_. ]+)?(?:{extension_expression})$",
                "",
                name,
                flags=regex.IGNORECASE,
            ).strip()

    if name.endswith(","):
        name = name[:-1].strip()
    if _get_extension(name) in _FILE_EXTENSIONS:
        name = regex.sub(
            rf"(?:{'|'.join(regex.escape(item) for item in _FILE_EXTENSIONS)})$",
            "",
            name,
            flags=regex.IGNORECASE,
        ).strip()
    if name.lower().endswith("complete"):
        name = regex.sub(r"(-|:)\s*Complete$", "", name, flags=regex.IGNORECASE).strip()

    if not name and not second:
        parent_name = observation.path.parent.name
        if not observation.at_configured_root and not policy.number_policy._compiled.volume_marker.search(parent_name):
            name = _volume_series_name(
                parent_name, observation, number, policy, False, second=True
            )
            if contains_brackets(name):
                name = remove_brackets(name)
    return name


def _chapter_file_name_cleaning(
    file_name: str,
    chapter_number: ReleaseNumber,
    number_policy: NumberPolicy,
    *,
    regex_matched: bool,
) -> str:
    if contains_brackets(file_name):
        file_name = remove_brackets(file_name)
    if file_name.endswith(("(", "[", "{")):
        file_name = file_name[:-1].strip()
    if file_name and file_name[0].isdigit():
        file_name = regex.sub(
            r"(^([0-9]+)(([-_.])([0-9]+)|)+(\s+)?([-_]+)(\s+))", "", file_name
        ).strip()
    if regex_matched and file_name.endswith("-"):
        file_name = regex.sub(
            r"(#)?([0-9]+)([-_.][0-9]+)*((x|#)([0-9]+)([-_.][0-9]+)*)*\s*-$",
            "",
            file_name,
        ).strip()
    if file_name.endswith("-"):
        file_name = regex.sub(r"(?<![A-Za-z])(-\s*)$", "", file_name).strip()
    if file_name.replace("#", "").isdigit() or file_name.replace("#", "").replace(".", "", 1).isdigit():
        return ""

    number_text = "-".join(chapter_number.raw_tokens)
    if not regex_matched and number_text and file_name and file_name[-1].isdigit():
        number_escaped = regex.escape(number_text)
        chapter_keywords = number_policy._compiled.chapter_keywords_expression
        file_name = regex.sub(
            rf"-?(\s+)?((?<!({chapter_keywords})(\s+)?)(\s+)?\b#?((0+)?({number_escaped}))#?$)",
            "",
            file_name,
            flags=regex.IGNORECASE,
        ).strip()
    if (" s" in file_name.lower() or "sea" in file_name.lower()) and file_name and file_name[-1].isdigit():
        file_name = regex.sub(r"(Season|Sea| S)(\s+)?([0-9]+)$", "", file_name, flags=regex.IGNORECASE)
    if ("-" in file_name or ":" in file_name) and file_name and file_name[0].isdigit():
        file_name = regex.sub(r"((\s+(-)|:)\s+).*$", "", file_name, flags=regex.IGNORECASE).strip()
    return file_name


def _chapter_series_name(
    file_name: str,
    observation: FileObservation,
    number: ReleaseNumber,
    policy: ParsingPolicy,
    *,
    second: bool = False,
) -> str:
    name = file_name
    if name.startswith(("(", "[", "{")) and regex.search(
        r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+(\s+[A-Za-z]{2})", name
    ):
        name = regex.sub(r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+\s+", "", name).strip()
    name = name.replace("_extra", ".5")
    if "one" in name.lower() and "shot" in name.lower():
        name = regex.sub(r"(-\s*)Ones?(-|)shot\s*", "", name, flags=regex.IGNORECASE).strip()
    name = remove_dual_space(name).strip()
    extension = _get_extension(name)
    if extension:
        name = name[: -len(extension)]
    if "_" in name:
        name = replace_underscores(name)

    matched = False
    for pattern in policy.number_policy._compiled.chapter_searches:
        search = pattern.search(name)
        if search:
            matched = True
            name = name.split(search.group())[0].strip()
            break
    result = _chapter_file_name_cleaning(
        name, number, policy.number_policy, regex_matched=matched
    )
    if result.endswith(","):
        result = result[:-1].strip()

    if not result and not second:
        parent_name = observation.path.parent.name
        if not observation.at_configured_root and not policy.number_policy._compiled.chapter_marker.search(parent_name):
            parent_number = parse_release_number(parent_name, "volume", policy.number_policy)
            result = _chapter_series_name(parent_name, observation, parent_number, policy, second=True)
            if contains_brackets(result):
                result = remove_brackets(result)
    return result




def _premium_from_observation(
    name: str, extension: str, metadata: tuple[tuple[str, str], ...]
) -> bool:
    if extension not in _DEFAULT_NOVEL_EXTENSIONS:
        return False
    lowered_name = name.lower()
    if "(premium)" in lowered_name or "[premium]" in lowered_name:
        return True

    for key, value in metadata:
        key_lower = key.lower()
        value_lower = value.lower()
        if key_lower in {"premium", "premium_content", "premium-content"}:
            if value_lower.strip() in {"1", "true", "yes"}:
                return True
        if key_lower.endswith("/toc.xhtml") or key_lower == "toc.xhtml":
            if "j-novel" in value_lower and regex.search(
                r"(Bonus\s+((Color\s+)?Illustrations?|(Short\s+)?Stories))",
                value,
                flags=regex.IGNORECASE,
            ):
                return True
        if key_lower.endswith("/copyright.xhtml") or key_lower == "copyright.xhtml":
            if "premium" in value_lower and regex.search(
                r"(Premium(\s)+(E?-?Book|Epub))", value, flags=regex.IGNORECASE
            ):
                return True
    return False


def _identifiers(metadata: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    identifier_keys = {"identifier", "identifiers", "dc:identifier", "isbn", "issn", "uuid"}
    return tuple(
        (name, value)
        for name, value in metadata
        if name.lower() in identifier_keys or name.lower().startswith("identifier:")
    )


def parse_release(observation: FileObservation, policy: ParsingPolicy) -> ReleaseSnapshot:
    """Build a release snapshot entirely from caller-supplied observations."""

    if not isinstance(observation, FileObservation):
        raise TypeError("observation must be a FileObservation")
    if not isinstance(policy, ParsingPolicy):
        raise TypeError("policy must be a ParsingPolicy")

    name = observation.path.name
    extension = _get_extension(name)
    number_policy = policy.number_policy
    has_volume_keyword = _contains_volume_keyword(name, number_policy)
    has_chapter_keyword = not has_volume_keyword and _contains_chapter_keyword(name, number_policy)
    kind = "chapter" if has_chapter_keyword else "volume"
    part_kind = kind
    number = parse_release_number(name, kind, number_policy)
    one_shot = _is_one_shot(
        name,
        observation,
        policy,
        has_volume_keyword=has_volume_keyword,
        has_chapter_keyword=has_chapter_keyword,
    )
    exception = _file_is_exception(name, policy)
    number_missing_or_zero = number.kind == "missing" or (
        number.kind == "scalar" and number.values[0] == 0
    )

    if (
        extension in _DEFAULT_MANGA_EXTENSIONS
        and kind != "chapter"
        and (number_missing_or_zero or one_shot)
        and (exception or one_shot)
        and _page_heuristic(observation, policy)
    ):
        kind = "chapter"
        one_shot = True

    if one_shot:
        number = ReleaseNumber.scalar("1", part=number.part)

    metadata = dict(observation.metadata)
    year = _release_year(name, metadata)
    publisher_from_metadata = _publisher_from_metadata(metadata)
    publisher_from_name = _publisher_from_name(name, policy)
    publisher = publisher_from_metadata or publisher_from_name
    if kind == "chapter":
        display_series = _chapter_series_name(name, observation, number, policy)
    else:
        display_series = _volume_series_name(name, observation, number, policy, one_shot)
    if not display_series:
        display_series = _series_name_from_contents(observation.path.parent.name, (name,))

    release_group = _release_group(name, display_series, policy)
    subtitle = _subtitle(
        name,
        extension,
        display_series,
        tuple(value for value in (publisher_from_metadata, publisher_from_name) if value),
        number,
        observation.path.parent.name,
        number_policy,
    )
    part_context = name
    for value in (display_series, subtitle):
        if value:
            part_context = regex.sub(regex.escape(value), "", part_context, flags=regex.IGNORECASE)
    part = _part_after_number(part_context, part_kind, number_policy)
    if part != number.part:
        number = ReleaseNumber(number.kind, number.values, number.raw_tokens, part)
    extras = _get_extras(
        name,
        kind=kind,
        series_name=display_series,
        subtitle=subtitle or "",
        extension=extension,
    )
    provenance = ["filename"]
    if observation.metadata:
        provenance.append("metadata")
    if "_extra" in name:
        provenance.append("filename:extra_marker")
    if one_shot:
        provenance.append("classification:one_shot")
    if exception:
        provenance.append("classification:exception_keyword")
    if observation.archive_pages:
        provenance.append("archive_pages")
    return ReleaseSnapshot(
        observation=observation,
        number=number,
        kind=kind,
        display_series=display_series,
        canonical_series_key=clean_str(display_series),
        provenance=tuple(provenance),
        year=year,
        release_group=release_group,
        publisher=publisher,
        is_premium=_premium_from_observation(name, extension, observation.metadata),
        extras=extras,
        subtitle=subtitle,
        identifiers=_identifiers(observation.metadata),
    )


def _is_one_shot_release(release: ReleaseSnapshot) -> bool:
    return "classification:one_shot" in release.provenance


def _format_number_token(
    token: str, *, kind: str, policy: RenamePolicy, extra_marker: bool
) -> str:
    if extra_marker and token.endswith(".5"):
        token = token[:-2]
    value = Decimal(token)
    if "." not in token:
        token = str(int(value))
    width = (
        policy.zfill_chapter_float_value
        if kind == "chapter" and "." in token
        else policy.zfill_chapter_int_value
        if kind == "chapter"
        else policy.zfill_volume_float_value
        if "." in token
        else policy.zfill_volume_int_value
    )
    threshold = Decimal(100 if kind == "chapter" else 10)
    return token.zfill(width) if value < threshold else token


def _render_number(
    release: ReleaseSnapshot, policy: RenamePolicy, source_span: tuple[int, int] | None
) -> str:
    number = release.number
    if number.kind == "missing":
        return ""
    extra_marker = bool(regex.search(r"\d_extra\b", release.name))
    tokens = tuple(
        _format_number_token(
            token,
            kind=release.kind,
            policy=policy,
            extra_marker=extra_marker and index == len(number.raw_tokens) - 1,
        )
        for index, token in enumerate(number.raw_tokens)
    )
    token = "-".join(tokens)
    if release.kind == "chapter" and number.part is not None:
        indicator = "x"
        part_match = regex.search(
            r"\d+(?:[._-]\d+)*(?P<marker>[x#])\d+",
            release.name[source_span[0]:source_span[1]] if source_span else "",
            flags=regex.IGNORECASE,
        )
        if part_match:
            indicator = part_match.group("marker")
        token += f"{indicator}{number.part}"
    return token


def _render_keyword(release: ReleaseSnapshot, policy: RenamePolicy) -> str:
    return (
        policy.preferred_chapter_renaming_format
        if release.kind == "chapter"
        else policy.preferred_volume_renaming_format
    )


def _render_source_match(
    name: str, release: ReleaseSnapshot, policy: RenamePolicy
) -> tuple[int, int] | None:
    if release.number.kind == "missing":
        return None
    patterns = policy.number_policy._compiled
    source_tokens = list(release.number.raw_tokens)
    if regex.search(r"\d_extra\b", name) and source_tokens[-1].endswith(".5"):
        source_tokens[-1] = source_tokens[-1][:-2]
    sequence = "-".join(regex.escape(token).replace(r"\.", r"[._]") for token in source_tokens)
    series_pattern = regex.escape(release.display_series).replace(r"\ ", r"[_ ]+")
    series = regex.search(series_pattern, name, flags=regex.IGNORECASE) if release.display_series else None
    start = series.end() if series else 0
    keyword = (
        patterns.volume_keywords_expression
        if release.kind == "volume"
        else patterns.chapter_keywords_expression
    )
    if keyword != r"(?!)":
        part_expression = r"(?P<part>[x#]\d+(?:[-_.]\d+)*)?" if release.kind == "chapter" else ""
        issue_expression = r"(?:\s+#\d+(?:[-_.]\d+)*)?" if release.kind == "volume" else ""
        match = regex.search(
            rf"(?<![A-Za-z\[\(\{{])(?P<keyword>(?:{keyword}))[-_.\s]*"
            rf"(?P<number>{sequence}){part_expression}"
            rf"(?P<extra>_extra)?{issue_expression}",
            name[start:],
            flags=regex.IGNORECASE,
        )
        if match:
            if release.subtitle and patterns.subtitle_exclusion.search(
                name, 0, start + match.start("keyword")
            ):
                return None
            return start + match.start("keyword"), start + match.end()
    if release.kind != "chapter" or _find_number_match(name, "chapter", policy.number_policy) is None:
        return None

    part_expression = r"(?:[x#]\d+)?" if release.number.part is not None else ""
    match = regex.search(
        rf"(?<![\d.])#?{sequence}{part_expression}(?:_extra)?(?!\d)",
        name[start:],
        flags=regex.IGNORECASE,
    )
    if match is None:
        return None
    span = start + match.start(), start + match.end()
    return span


def _modifier(extension: str) -> str:
    if extension in _DEFAULT_NOVEL_EXTENSIONS:
        return "[%s]"
    if extension in _DEFAULT_MANGA_EXTENSIONS:
        return "(%s)"
    return "%s"


def _is_similar(left: str, right: str, threshold: float) -> bool:
    return similar(left, right) >= threshold


def _restructure(release: ReleaseSnapshot, policy: RenamePolicy) -> str:
    extension = release.path.suffix
    if not release.number.values and not _is_one_shot_release(release):
        return release.name
    if release.kind == "chapter" and not policy.rename_chapters_with_preferred_chapter_keyword:
        return release.name
    if _is_one_shot_release(release) and not policy.add_volume_one_number_to_one_shots:
        return release.name

    source_span = _render_source_match(release.name, release, policy)
    number = _render_number(release, policy, source_span)
    if not number:
        return release.name
    keyword = _render_keyword(release, policy)
    base = release.display_series.strip()
    rename = f"{base} {keyword}{number}"
    if "filename:extra_marker" in release.provenance:
        rename += "_extra"
    if (
        policy.add_issue_number_to_manga_file_name
        and release.kind == "volume"
        and extension in _DEFAULT_MANGA_EXTENSIONS
    ):
        rename += f" #{number}"
    if release.subtitle:
        rename += f" - {release.subtitle}"
    modifier = _modifier(extension)
    if release.year:
        rename += f" {modifier % release.year}"
    extras = list(release.extras)
    if release.year:
        extras = [
            extra
            for extra in extras
            if not (
                str(release.year) in extra
                or _is_year_marker(extra[1:-1])
                or _is_similar(
                    extra, str(release.year), policy.required_similarity_score
                )
            )
        ]
    publisher_match = policy._publishers.search(release.name)
    publisher_from_name = publisher_match.group() if publisher_match else None
    if policy.add_publisher_name_to_file_name_when_renaming and release.publisher:
        publisher_names = (*policy.publishers, release.publisher)
        if publisher_from_name:
            publisher_names += (publisher_from_name,)
        extras = [
            extra
            for extra in extras
            if not any(
                _is_similar(extra[1:-1], publisher, policy.publisher_similarity_score)
                for publisher in publisher_names
            )
        ]
        rename += f" {modifier % release.publisher}"
    if release.is_premium and policy.search_and_add_premium_to_file_name:
        extras = [extra for extra in extras if "premium" not in extra.lower()]
        rename += f" {modifier % 'Premium'}"
    if (
        policy.move_release_group_to_end_of_file_name
        and policy.add_publisher_name_to_file_name_when_renaming
        and release.release_group
        and release.release_group not in (release.publisher, publisher_from_name)
    ):
        extras = [
            extra
            for extra in extras
            if not _is_similar(extra[1:-1], release.release_group, policy.release_group_similarity_score)
        ]
    extras_to_add = [
        extra
        for extra in extras
        if not regex.search(regex.escape(extra), rename, flags=regex.IGNORECASE)
    ]
    if extras_to_add:
        rename += " " + " ".join(extras_to_add)
    if policy.move_release_group_to_end_of_file_name and release.release_group:
        if not regex.search(rf"\b{regex.escape(release.release_group)}\b", rename, flags=regex.IGNORECASE):
            rename += f" {modifier % release.release_group}"
    rename = rename.replace("*", "").strip()
    rename += extension
    if policy.replace_unicode_when_restructuring:
        rename = remove_dual_space(unidecode(rename))
    return rename.replace('"', "'").replace("/", "-").strip()


def _render_release_name(release: ReleaseSnapshot, policy: RenamePolicy) -> str:
    if release.kind not in {"volume", "chapter"}:
        return release.name
    if release.kind == "chapter" and not policy.rename_chapters_with_preferred_chapter_keyword:
        return release.name
    if _is_one_shot_release(release) and not policy.add_volume_one_number_to_one_shots:
        return release.name
    if policy.restructure_when_renaming:
        return _restructure(release, policy)

    span = _render_source_match(release.name, release, policy)
    number = _render_number(release, policy, span)
    if not number:
        return release.name
    replacement = f"{_render_keyword(release, policy)}{number}"
    if span is None:
        if not _is_one_shot_release(release):
            return release.name
        if "filename:extra_marker" in release.provenance:
            replacement += "_extra"
        if (
            policy.add_issue_number_to_manga_file_name
            and release.kind == "volume"
            and release.path.suffix in _DEFAULT_MANGA_EXTENSIONS
        ):
            replacement += f" #{number}"
        result = f"{release.display_series} {replacement}"
        if release.subtitle:
            result += f" - {release.subtitle}"
        if release.year:
            result += f" ({release.year})"
        if release.extras:
            result += " " + " ".join(release.extras)
        result += release.path.suffix
    else:
        start, end = span
        if "_extra" in release.name[start:end]:
            replacement += "_extra"
        if (
            policy.add_issue_number_to_manga_file_name
            and release.kind == "volume"
            and release.path.suffix in _DEFAULT_MANGA_EXTENSIONS
        ):
            replacement += f" #{number}"
        result = release.name[:start] + replacement + release.name[end:]

    result = regex.sub(r"_(?!extra\b)", " ", result.replace("?", ""))
    result = remove_dual_space(result).strip()
    result = regex.sub(r"([A-Za-z])([:：])", r"\1 -", result)
    return remove_dual_space(result)


def render_release(release: ReleaseSnapshot, policy: RenamePolicy) -> str:
    """Render one relative filename without reading or mutating the filesystem."""
    if not isinstance(release, ReleaseSnapshot):
        raise TypeError("release must be a ReleaseSnapshot")
    if not isinstance(policy, RenamePolicy):
        raise TypeError("policy must be a RenamePolicy")
    result = _render_release_name(release, policy)
    if (
        not result.strip()
        or result in {".", ".."}
        or any(character in result for character in ("/", "\\", "\0"))
        or regex.match(r"^[A-Za-z]:", result)
    ):
        raise ValueError("rendered filename must be a relative basename")
    return result


__all__ = ["ParsingPolicy", "RenamePolicy", "parse_release", "render_release"]
