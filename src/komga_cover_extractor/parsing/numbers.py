from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

import regex

from komga_cover_extractor.domain.releases import ReleaseNumber

from .normalization import remove_brackets, remove_dual_space, replace_underscores

_DEFAULT_VOLUME_KEYWORDS = (
    "LN",
    "Light Novels?",
    "Novels?",
    "Books?",
    "Volumes?",
    "Vols?",
    "Discs?",
    "Tomo",
    "Tome",
    "Von",
    "V",
    "第",
    "T",
)
_DEFAULT_CHAPTER_KEYWORDS = ("Chapters?", "Chaps?", "Chs?", "Cs?", "D")
_DEFAULT_EXCLUSION_KEYWORDS = (
    r"(\s)Part(\s)",
    r"(\s)Episode(\s)",
    r"(\s)Season(\s)",
    r"(\s)Arc(\s)",
    r"(\s)Prologue(\s)",
    r"(\s)Epilogue(\s)",
    r"(\s)Omake(\s)",
    r"(\s)Extra(\s)",
    r"(\s)- Special(\s)",
    r"(\s)Side Story(\s)",
    r"(\s)Act(\s)",
    r"(\s)Special Episode(\s)",
    r"(\s)Ep(\s)",
    r"(\s)- Version(\s)",
    r"(\s)Ver(\s)",
    r"(\s)PT\.",
    r"(\s)PT(\s)",
    r",",
    r"(\s)×",
    r"\d\s*-\s*",
    r"\bNo.",
    r"\bNo.(\s)",
    r"\bBonus(\s)",
    r"(\]|\}|\)) -",
    r"\bZom(\s)",
    "Tail -",
    "꞉",
    ":",
    r"\d\.",
)
_DEFAULT_SUBTITLE_EXCLUSIONS = (r"-(\s)", r"-", r"-\s[A-Za-z]+\s")

# Formats accepted by the original File/Volume classification path. RAR/7z are
# conversion inputs, not file-class formats, and are deliberately not included.
_FILE_EXTENSIONS = (".epub", ".zip", ".cbz")

# The original single-volume year guard is intentionally retained. Multi-volume
# ranges were not subject to this cutoff.
_SINGLE_VOLUME_YEAR_CUTOFF = Decimal("2000")


@dataclass(frozen=True, slots=True)
class _NumberPatterns:
    volume_number: regex.Pattern[str]
    volume_keyword: regex.Pattern[str]
    chapter_number_at_end: regex.Pattern[str]
    chapter_searches: tuple[regex.Pattern[str], ...]
    chapter_number_fallback: regex.Pattern[str]
    volume_marker: regex.Pattern[str]
    chapter_marker: regex.Pattern[str]
    volume_keywords_expression: str
    chapter_keywords_expression: str
    subtitle_exclusion: regex.Pattern[str]



def _compile(pattern: str, *, name: str) -> regex.Pattern[str]:
    try:
        return regex.compile(pattern, flags=regex.IGNORECASE)
    except regex.error as error:
        raise ValueError(f"invalid {name} regex policy: {error}") from error


def _compile_patterns(policy: NumberPolicy) -> _NumberPatterns:
    volume_keywords = policy.volume_keywords
    chapter_keywords = policy.chapter_keywords
    exclusion_keywords = policy.exclusion_keywords

    volume_expression = (
        "(?<![A-Za-z])" + "|(?<![A-Za-z])".join(volume_keywords)
        if volume_keywords
        else r"(?!)"
    )
    chapter_expression = (
        r"(?<![A-Za-z])" + r"|(?<![A-Za-z])".join(chapter_keywords)
        if chapter_keywords
        else r"(?!)"
    )
    exclusion_expression = "|".join(exclusion_keywords)
    subtitle_expression = "|".join(policy.subtitle_exclusion_keywords)
    subtitle_exclusion = _compile(
        rf"(?:{subtitle_expression})$" if subtitle_expression else r"(?!)",
        name="subtitle exclusion",
    )
    exclusion_prefix = (
        rf"(?<!{exclusion_expression})" if exclusion_expression else ""
    )
    chapter_exclusion_prefix = (
        rf"(?<![A-Za-z]|{exclusion_expression})"
        if exclusion_expression
        else r"(?<![A-Za-z])"
    )
    manga_extensions_expression = r"|".join(
        regex.escape(extension) for extension in (".zip", ".cbz")
    )
    extensions_expression = r"|".join(
        regex.escape(extension) for extension in _FILE_EXTENSIONS
    )

    chapter_searches = (
        r"\b\s-\s*(#)?(\d+)([-_.]\d+)*(x\d+)?\s*-\s",
        rf"\b(?<![\[\(\{{])({chapter_expression})(\.)?\s*(\d+)([-_.]\d+)*(x\d+)?\b(?<!\s(\d+)([-_.]\d+)*(x\d+)?\s.*)",
        rf"{chapter_exclusion_prefix}(?<![\[\(\{{])((({chapter_expression})([-_. ]+)?(\d+)([-_.]\d+)*(x\d+)?)|\s+(\d+)(\.\d+)?(x\d+((\.\d+)+)?)?(\s+|#\d+|{manga_extensions_expression}))",
        rf"((?<!^)\b(\.)?\s*({exclusion_prefix})(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*\b)((\s+-|:)\s+).*?(?=\s*[\(\[\{{](\d{{4}}|Digital)[\)\]\}}])",
        rf"(\b({chapter_expression})?(\.)?\s*(({exclusion_prefix})(\b\d\b)|\d{{2,}})([-_.]\d+)*(x\d+)?(#\d+([-_.]\d+)*)?\b)\s*((\[[^\]]*\]|\([^\)]*\)|\{{[^}}]*\}})|((?<!\w(\s))|(?<!\w))({extensions_expression})(?!\w))",
        r"^((#)?(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*)$",
    )

    volume_number = (
        rf"\b(?<![\[\(\{{])({volume_expression})((\.)|)(\s+)?([0-9]+)(([-_.])([0-9]+)|)+\b"
    )
    volume_keyword = (
        rf"((\s?(\s-\s|)(Part|)+(?<![\[\(\{{])({volume_expression})(\.|)([-_. ]|)([0-9]+)\b)"
        rf"|\s?(\s-\s|)(Part|)({volume_expression})(\.|)([-_. ]|)([0-9]+)([-_.])(\s-\s|)(Part|)({volume_expression})([0-9]+)\s"
        rf"|\s?(\s-\s|)(Part|)({volume_expression})(\.|)([-_. ]|)([0-9]+)([-_.])(\s-\s|)(Part|)({volume_expression})([0-9]+)\s)"
    )
    chapter_number_at_end = (
        rf"(({exclusion_expression})(\.)?(\s+)?(#)?(([0-9]+)(([-_.])([0-9]+)|)+))$"
        if exclusion_expression
        else r"((\.)?(\s+)?(#)?(([0-9]+)(([-_.])([0-9]+)|)+))$"
    )

    return _NumberPatterns(
        volume_number=_compile(volume_number, name="volume number"),
        volume_keyword=_compile(volume_keyword, name="volume keyword"),
        chapter_number_at_end=_compile(chapter_number_at_end, name="chapter number"),
        chapter_searches=tuple(
            _compile(pattern, name="chapter search") for pattern in chapter_searches
        ),
        chapter_number_fallback=_compile(
            r"(?<!^)(?<!\d\.)\b([0-9]+)(([-_.])([0-9]+)|)+(x[0-9]+)?(#([0-9]+)(([-_.])([0-9]+)|)+)?(\.\d+)?\b",
            name="chapter fallback",
        ),
        volume_marker=_compile(
            rf"(\b(?:{volume_expression})([-_.]|)(([0-9]+)((([-_.]|)([0-9]+))+|))"
            rf"((x|#)(\d+)([-_.]\d+)*)*(\s|-|{extensions_expression}))",
            name="volume marker",
        ),
        chapter_marker=_compile(
            rf"(\b(?:{chapter_expression})([-_.]|)(([0-9]+)((([-_.]|)([0-9]+))+|))"
            rf"((x|#)(\d+)([-_.]\d+)*)*(\s|-|{extensions_expression}))",
            name="chapter marker",
        ),
        volume_keywords_expression=volume_expression,
        chapter_keywords_expression=chapter_expression,
        subtitle_exclusion=subtitle_exclusion,
    )


@dataclass(frozen=True, slots=True)
class NumberPolicy:
    """Immutable ordered regex inputs for release-number parsing."""

    volume_keywords: tuple[str, ...] = _DEFAULT_VOLUME_KEYWORDS
    chapter_keywords: tuple[str, ...] = _DEFAULT_CHAPTER_KEYWORDS
    exclusion_keywords: tuple[str, ...] = _DEFAULT_EXCLUSION_KEYWORDS
    subtitle_exclusion_keywords: tuple[str, ...] = _DEFAULT_SUBTITLE_EXCLUSIONS
    _compiled: _NumberPatterns = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "volume_keywords",
            "chapter_keywords",
            "exclusion_keywords",
            "subtitle_exclusion_keywords",
        ):
            values = getattr(self, name)
            if isinstance(values, str):
                raise TypeError(f"{name} must be a tuple of regex strings")
            if not isinstance(values, tuple):
                values = tuple(values)
                object.__setattr__(self, name, values)
            if any(not isinstance(value, str) for value in values):
                raise TypeError(f"{name} must contain strings")
            for value in values:
                if value == "":
                    raise ValueError(f"{name} cannot contain empty patterns")
                _compile(value, name=name)
        object.__setattr__(self, "_compiled", _compile_patterns(self))


@dataclass(frozen=True, slots=True)
class MissingReleaseReport:
    """A bounded report of local integer-volume gaps."""

    status: Literal["complete", "review_required"]
    missing: tuple[int, ...]
    observed: tuple[int, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"complete", "review_required"}:
            raise ValueError("status must be complete or review_required")
        if self.status == "complete" and self.reason is not None:
            raise ValueError("a complete report cannot have a reason")
        if self.status == "review_required" and not self.reason:
            raise ValueError("a review-required report needs a reason")
        if any(type(value) is not int or value < 1 for value in self.missing):
            raise ValueError("missing release numbers must be positive integers")
        if any(type(value) is not int or value < 0 for value in self.observed):
            raise ValueError("observed release numbers must be non-negative integers")


def _prepare_name(name: str) -> str:
    prepared = remove_dual_space(name.replace("_extra", ".5")).strip()
    return replace_underscores(prepared) if "_" in prepared else prepared


def _has_multiple_numbers(name: str) -> bool:
    return len(regex.findall(r"\d+\.0+[0-9]+|\d+\.[0-9]+|\d+", name)) > 1


def _clean_matched_number(value: str, kind: str, policy: NumberPolicy) -> str:
    patterns = policy._compiled
    name = value.strip()

    if "." in name:
        name = regex.sub(r"^\s*(\.)", "", name).strip()
    if ("-" in name or ":" in name) and regex.search(r"(^\d+)", name.strip()):
        name = regex.sub(r"((\s+(-)|:)\s+).*$", "", name).strip()
    if "#" in name and regex.search(r"(\d+#\d+)", name):
        name = regex.sub(r"((#)([0-9]+)(([-_.])([0-9]+)|)+)", "", name).strip()
    if "x" in name.lower():
        name = regex.sub(r"(x[0-9]+)", "", name, flags=regex.IGNORECASE).strip()
    if any(bracket in name for bracket in "()[]{}"):
        name = remove_brackets(name).strip()

    extension = _get_extension(name)
    if extension in _FILE_EXTENSIONS:
        if "-" in name:
            extensions_expression = r"|".join(
                regex.escape(item) for item in _FILE_EXTENSIONS
            )
            name = regex.sub(
                rf"(((\s+)?-(\s+)?([A-Za-z]+))?({extensions_expression}))",
                "",
                name,
                flags=regex.IGNORECASE,
            ).strip()
        else:
            name = name.replace(extension, "").strip()

    if "-" in name:
        if name.startswith("- "):
            name = name[1:].strip()
        if name.endswith(" -"):
            name = name[:-1].strip()
    if name.startswith("#"):
        name = name[1:].strip()

    keyword_expression = (
        patterns.volume_keywords_expression
        if kind == "volume"
        else patterns.chapter_keywords_expression
    )
    if keyword_expression != r"(?!)":
        name = regex.sub(
            rf"\b(?:{keyword_expression})(\.|)([-_. ])?",
            "",
            name,
            flags=regex.IGNORECASE,
        ).strip()
    if keyword_expression != r"(?!)" and regex.search(
        rf"\b[0-9]+(?:{keyword_expression})[0-9]+\b", name, flags=regex.IGNORECASE
    ):
        name = regex.sub(
            rf"(?:{keyword_expression})", ".", name, flags=regex.IGNORECASE
        ).strip()
    return name


_NUMBER_SEQUENCE = regex.compile(
    r"(?<![\d.])(?P<sequence>\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)*)"
)


def _number_tokens(name: str, kind: str, policy: NumberPolicy) -> tuple[str, ...]:
    cleaned = _clean_matched_number(name, kind, policy)
    match = _NUMBER_SEQUENCE.fullmatch(cleaned)
    if match is None:
        return ()
    sequence = match.group("sequence")
    return tuple(sequence.split("-"))


def _part_after_number(name: str, kind: str, policy: NumberPolicy) -> int | Decimal | None:
    prepared = _prepare_name(name)
    if kind == "volume":
        number_match = policy._compiled.volume_number.search(prepared)
        if number_match:
            prepared = prepared[number_match.end():]
        match = regex.search(
            r"\bPart[-_. ]?([0-9]+)\b", prepared, flags=regex.IGNORECASE
        )
        return int(match.group(1)) if match else None

    match_text = _find_number_match(name, kind, policy)
    if match_text is None:
        return None
    match_start = prepared.index(match_text)
    match = regex.search(
        r"\d+(?:\.\d+)?(?:[-_]\d+(?:\.\d+)?)*[x#]([0-9]+(?:[._-][0-9]+)*)",
        prepared[match_start:],
        flags=regex.IGNORECASE,
    )
    if match is None or match.start() >= len(match_text):
        return None
    try:
        part = Decimal(match.group(1).replace("_", "."))
    except InvalidOperation as error:
        raise ValueError("invalid chapter part") from error
    return int(part) if part == part.to_integral_value() else part


def _kind_for_tokens(tokens: tuple[str, ...]) -> Literal["scalar", "range", "set"]:
    if len(tokens) == 1:
        return "scalar"
    if len(tokens) == 2:
        return "range"
    return "set"


def _get_extension(name: str) -> str:
    index = name.rfind(".")
    if index <= 0:
        return ""
    return name[index:]


def _chapter_match_text(name: str, policy: NumberPolicy) -> str | None:
    patterns = policy._compiled
    prepared = _prepare_name(name)
    extensionless = prepared[: -len(_get_extension(prepared))] if _get_extension(prepared) else prepared
    if _has_multiple_numbers(prepared) and ("-" in prepared or "#" in prepared):
        trailing = patterns.chapter_number_at_end.search(extensionless)
        collection = next(
            (
                match
                for pattern in patterns.chapter_searches
                if (match := pattern.search(prepared))
            ),
            None,
        )
        trailing_is_collection = (
            trailing is not None
            and collection is not None
            and trailing.start() < collection.end()
            and len(_number_tokens(collection.group(), "chapter", policy)) > 1
        )
        if trailing and not trailing_is_collection:
            prepared = patterns.chapter_number_at_end.sub("", extensionless)
            if prepared.endswith("-") and not regex.search(
                r"-(\s+)?(#)?([0-9]+)(([-_.])([0-9]+)|)+(x[0-9]+)?(\s+)?-",
                prepared,
            ):
                prepared = prepared[:-1].strip()

    for pattern in patterns.chapter_searches:
        match = pattern.search(prepared)
        if match:
            return match.group().strip()
    return None


def _find_number_match(name: str, kind: str, policy: NumberPolicy) -> str | None:
    prepared = _prepare_name(name)
    if kind == "volume":
        match = policy._compiled.volume_number.search(prepared)
        if match:
            return match.group().strip()
        return None
    return _chapter_match_text(name, policy)


def _contains_volume_keyword(name: str, policy: NumberPolicy) -> bool:
    prepared = name.replace("_extra", ".5")
    prepared = remove_dual_space(prepared).strip()
    if any(bracket in prepared for bracket in "()[]{}"):
        prepared = remove_brackets(prepared)
    if "_" in prepared:
        prepared = replace_underscores(prepared).strip()
    return bool(
        policy._compiled.volume_keyword.search(remove_dual_space(prepared).strip())
    )


def _contains_chapter_keyword(name: str, policy: NumberPolicy) -> bool:
    patterns = policy._compiled
    prepared = name.replace("_extra", ".5")
    prepared = replace_underscores(prepared).strip() if "_" in prepared else prepared
    prepared = remove_dual_space(prepared).strip()
    for pattern in patterns.chapter_searches:
        match = pattern.search(prepared)
        if match:
            result = match.group()
            if not (
                result.startswith(("(", "[", "{"))
                and result.endswith((")", "]", "}"))
                and len(result[1:-1]) == 4
                and result[1:-1].isdigit()
            ):
                return True

    if _contains_volume_keyword(name, policy):
        return False
    without_year = regex.sub(r"(\(|\[|\{)(\d{4})(\)|\]|\})", "", name)
    if any(char.isdigit() for char in without_year):
        without_year = regex.sub(r"\b(?:2\d{3})\b$", "", without_year, flags=regex.IGNORECASE)
        return bool(patterns.chapter_number_fallback.search(without_year))
    return False


def parse_release_number(name: str, kind: str, policy: NumberPolicy) -> ReleaseNumber:
    """Parse the first characterized volume/chapter number without float loss."""

    if not isinstance(name, str):
        raise TypeError("name must be a string")
    if not isinstance(kind, str):
        raise TypeError("kind must be a string")
    if kind not in {"volume", "chapter"}:
        raise ValueError("kind must be 'volume' or 'chapter'")
    if not isinstance(policy, NumberPolicy):
        raise TypeError("policy must be a NumberPolicy")

    match_text = _find_number_match(name, kind, policy)
    if match_text is None:
        return ReleaseNumber.missing(part=_part_after_number(name, kind, policy))

    tokens = _number_tokens(match_text, kind, policy)
    if not tokens:
        return ReleaseNumber.missing(part=_part_after_number(name, kind, policy))

    number_kind = _kind_for_tokens(tokens)
    if number_kind == "range" and Decimal(tokens[0]) > Decimal(tokens[1]):
        tokens = (tokens[1], tokens[0])
    if kind == "volume" and number_kind == "scalar" and Decimal(tokens[0]) >= _SINGLE_VOLUME_YEAR_CUTOFF:
        return ReleaseNumber.missing(part=_part_after_number(name, kind, policy))

    part = _part_after_number(name, kind, policy)
    return ReleaseNumber.from_tokens(number_kind, tokens, part=part)



def detect_missing_releases(
    numbers: Sequence[ReleaseNumber], *, max_expansion: int = 10_000
) -> MissingReleaseReport:
    """Report integral volume gaps from one through the greatest observed value.

    Ranges imply each integral member between their endpoints. Explicit sets do
    not imply members between the values. Decimal values and parts remain
    reportable by their source ReleaseNumber but do not create integer gaps.
    """

    if isinstance(max_expansion, bool) or not isinstance(max_expansion, int):
        raise TypeError("max_expansion must be a positive integer")
    if max_expansion < 1:
        raise ValueError("max_expansion must be positive")
    if not isinstance(numbers, Sequence):
        raise TypeError("numbers must be a sequence of ReleaseNumber values")

    observed: set[int] = set()
    expansion_count = 0
    for number in numbers:
        if not isinstance(number, ReleaseNumber):
            raise TypeError("numbers must contain ReleaseNumber values")
        if number.kind == "missing":
            continue
        if number.kind == "range":
            first, last = number.values
            if first != first.to_integral_value() or last != last.to_integral_value():
                continue
            start, end = int(first), int(last)
            count = end - start + 1
            if count > max_expansion - expansion_count:
                return MissingReleaseReport(
                    "review_required", (), tuple(sorted(observed)), "range_limit"
                )
            expansion_count += count
            observed.update(value for value in range(start, end + 1) if value >= 0)
            continue
        for value in number.values:
            if value == value.to_integral_value():
                integer_value = int(value)
                if integer_value >= 0:
                    observed.add(integer_value)

    greatest = max((value for value in observed if value >= 1), default=0)
    if greatest > max_expansion:
        return MissingReleaseReport(
            "review_required", (), tuple(sorted(value for value in observed if value >= 0)), "range_limit"
        )
    observed_positive = tuple(sorted(value for value in observed if value >= 0))
    present = {value for value in observed if 1 <= value <= greatest}
    missing = tuple(value for value in range(1, greatest + 1) if value not in present)
    if len(missing) > max_expansion:
        return MissingReleaseReport(
            "review_required", (), observed_positive, "range_limit"
        )
    return MissingReleaseReport("complete", missing, observed_positive)


__all__ = ["MissingReleaseReport", "NumberPolicy", "detect_missing_releases", "parse_release_number"]
