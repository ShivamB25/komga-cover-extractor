"""Pure filename and series-name normalization helpers.

The rules in this module intentionally preserve the order and defaults of the
legacy matcher. They do not read configuration, inspect the filesystem, or
perform network I/O.
"""

from __future__ import annotations

import os
import string
from difflib import SequenceMatcher

import regex as re
from unidecode import unidecode

_COMMON_WORDS = ("the", "a", "à", "and", "&", "I", "of")
_EDITION_WORDS = (
    "Collection",
    "Master Edition",
    "(2|3|4|5)-in-1 Edition",
    "Edition",
    "Exclusive",
    "Anniversary",
    "Deluxe",
    "Digital",
    "Official",
    "Anthology",
    "Limited",
    "Complete",
    "Collector",
    "Ultimate",
    "Special",
)
_TYPE_KEYWORDS = (
    "(?<!^)Novel",
    "(?<!^)Light Novel",
    "(?<!^)Manga",
    "(?<!^)Comic",
    "(?<!^)LN",
    "(?<!^)Series",
    "(?<!^)Volume",
    "(?<!^)Chapter",
    "(?<!^)Book",
    "(?<!^)MANHUA",
)
_JAPANESE_PARTICLES = (
    "wa",
    "o",
    "mo",
    "ni",
    "e",
    "de",
    "ga",
    "kara",
    "to",
    "ya",
    r"no(?!\.)",
    "ne",
    "yo",
)
_MISC_WORDS = (r"((\d+)([-_. ]+)?th)", "x", "×", "HD")
_STOREFRONT_KEYWORDS = (r"Book(\s+)?walker",)

_DUAL_SPACE_PATTERN = re.compile(r"(\s{2,})")
_BRACKET_REMOVAL_PATTERN = re.compile(
    r"((((?<!-|[A-Za-z]\s|\[)(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})(?!-|\s*[A-Za-z]|\]))(\s+)?)+|([\[\{\(]((\d{4}))[\]\}\)]))",
    re.IGNORECASE,
)
_BRACKET_AVOIDANCE_PATTERN = re.compile(r"^[\(\[\{].*[\)\]\}]$")
_BRACKET_AGAINST_EXTENSION_PATTERN = re.compile(
    r"(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})(\.\w+$)"
)
_PUNCTUATION_PATTERN = re.compile(r"[^\w\s+]")
_ASCII_PUNCTUATION_TRANSLATION = str.maketrans("", "", string.punctuation)


def remove_dual_space(s: str) -> str:
    """Collapse repeated whitespace only when two literal spaces are present."""
    if "  " not in s:
        return s
    return _DUAL_SPACE_PATTERN.sub(" ", s)


def normalize_str(
    s: str,
    skip_common_words: bool = False,
    skip_editions: bool = False,
    skip_type_keywords: bool = False,
    skip_japanese_particles: bool = False,
    skip_misc_words: bool = False,
    skip_storefront_keywords: bool = False,
) -> str:
    """Remove the ordered legacy set of generic series-name words."""
    if len(s) <= 1:
        return s

    words_to_remove: list[str] = []
    if not skip_common_words:
        words_to_remove.extend(_COMMON_WORDS)
    if not skip_editions:
        words_to_remove.extend(_EDITION_WORDS)
    if not skip_type_keywords:
        words_to_remove.extend(_TYPE_KEYWORDS)
    if not skip_japanese_particles:
        words_to_remove.extend(_JAPANESE_PARTICLES)
    if not skip_misc_words:
        words_to_remove.extend(_MISC_WORDS)
    if not skip_storefront_keywords:
        words_to_remove.extend(_STOREFRONT_KEYWORDS)

    for word in words_to_remove:
        pattern = rf"{word}\s" if word in _TYPE_KEYWORDS else rf"\b{word}\b"
        s = re.sub(pattern, " ", s, flags=re.IGNORECASE).strip()
        s = remove_dual_space(s)

    return s.strip()


def contains_brackets(s: str) -> bool:
    """Return whether any supported round, square, or curly bracket occurs."""
    return any(bracket in s for bracket in ("(", ")", "{", "}", "[", "]"))


def starts_with_bracket(s: str) -> bool:
    return s.startswith(("(", "[", "{"))


def ends_with_bracket(s: str) -> bool:
    return s.endswith((")", "]", "}"))


def remove_brackets(value: str) -> str:
    """Remove bracketed metadata while retaining title-like and dash groups."""
    if (
        starts_with_bracket(value)
        and ends_with_bracket(value)
        and _BRACKET_AVOIDANCE_PATTERN.search(value)
    ):
        return value

    value = _BRACKET_REMOVAL_PATTERN.sub("", value).strip()
    extension = os.path.splitext(value)[1]
    if extension:
        if contains_brackets(value):
            value = _BRACKET_AGAINST_EXTENSION_PATTERN.sub(r"\2", value).strip()
        value = value.replace(extension, "").strip()
        value = f"{value}{extension}"
    return value


def contains_unicode(input_str: str) -> bool:
    """Return whether the string contains any non-ASCII code point."""
    return not input_str.isascii()


def contains_punctuation(s: str) -> bool:
    """Return whether the legacy punctuation expression matches the string."""
    return bool(_PUNCTUATION_PATTERN.search(s))


def remove_punctuation(s: str) -> str:
    """Replace punctuation other than plus signs with spaces, then trim."""
    return _PUNCTUATION_PATTERN.sub(" ", s).strip()


def remove_s(s: str) -> str:
    """Remove the final ``s`` from each word, as the legacy matcher does."""
    if "s" not in s.lower():
        return s
    return re.sub(r"\b(\w+)(s)\b", r"\1", s, flags=re.IGNORECASE).strip()


def convert_to_ascii(s: str) -> str:
    """Discard non-ASCII code points without transliterating them."""
    return "".join(character for character in s if ord(character) < 128)


def replace_underscores(name: str) -> str:
    """Use dots for numeric separators and spaces for other underscores."""
    name = re.sub(r"(?<=\d)_(?=\d)", ".", name)
    name = name.replace("_", " ")
    return remove_dual_space(name).strip()


def clean_str(
    string: str,
    skip_lowercase_convert: bool = False,
    skip_colon_replace: bool = False,
    skip_bracket: bool = False,
    skip_unidecode: bool = False,
    skip_normalize: bool = False,
    skip_punctuation: bool = False,
    skip_remove_s: bool = False,
    skip_convert_to_ascii: bool = False,
    skip_underscore: bool = False,
) -> str:
    """Apply the ordered legacy cleaning steps with their original skip flags."""
    s = string.lower().strip() if not skip_lowercase_convert else string
    if not skip_colon_replace and ":" in s:
        s = s.replace(":", " ")
    s = remove_dual_space(s)
    if not skip_bracket and contains_brackets(s):
        s = remove_brackets(s)
    if not skip_unidecode and contains_unicode(s):
        s = unidecode(s)
    if not skip_normalize:
        s = normalize_str(s)
    if not skip_punctuation and contains_punctuation(s):
        s = remove_punctuation(s)
    if not skip_remove_s:
        s = remove_s(s)
    s = remove_dual_space(s)
    if not skip_convert_to_ascii:
        s = convert_to_ascii(s)
    if not skip_underscore and "_" in s:
        s = replace_underscores(s)
    return s.strip()


def parse_words(user_string: str) -> list[str]:
    """Split lowercased, ASCII-punctuation-free transliteration into words."""
    if not user_string:
        return []
    words = user_string.translate(_ASCII_PUNCTUATION_TRANSLATION).lower()
    if contains_unicode(words):
        words = unidecode(words)
    return words.split()


def similar(a: str, b: str) -> float:
    """Return the legacy case-insensitive SequenceMatcher similarity ratio."""
    a = a.lower().strip()
    b = b.lower().strip()
    if a == "" or b == "":
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
