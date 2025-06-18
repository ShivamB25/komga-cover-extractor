"""A centralized location for all static values and constants.

This module prevents the use of magic numbers or hardcoded strings in the rest
of the application, making the codebase cleaner and easier to maintain.
"""

from typing import List, Pattern, Dict
import re

# File Extensions
SEVEN_ZIP_EXTENSIONS: List[str] = [".7z"]
ZIP_EXTENSIONS: List[str] = [".zip", ".cbz", ".epub"]
RAR_EXTENSIONS: List[str] = [".rar", ".cbr"]
NOVEL_EXTENSIONS: List[str] = [".epub"]
IMAGE_EXTENSIONS: List[str] = ['.jpg', '.jpeg', '.png', '.tbn', '.webp']

# Regex Patterns
VOLUME_KEYWORDS: List[str] = [
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
]
CHAPTER_KEYWORDS: List[str] = [
    "Chapters?",
    "Chaps?",
    "Chs?",
    "Cs?",
    "D",
]
EXCLUSION_KEYWORDS: List[str] = [
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
    r"Tail -",
    r"꞉",
    r":",
    r"\d\.",
]

# Helper variables for chapter search patterns
_manga_extensions = [x for x in ZIP_EXTENSIONS if x not in NOVEL_EXTENSIONS]
_file_extensions = NOVEL_EXTENSIONS + _manga_extensions
_chapter_regex_keywords = r"(?<![A-Za-z])" + (r"|(?<![A-Za-z])").join(CHAPTER_KEYWORDS)
_exclusion_keywords_joined = "|".join(EXCLUSION_KEYWORDS)
_exclusion_keywords_regex = r"(?<!%s)" % _exclusion_keywords_joined
_manga_extensions_regex = "|".join(_manga_extensions).replace(".", r"\.")
_file_extensions_regex = "|".join(_file_extensions).replace(".", r"\.")

_chapter_searches = [
    r"\b\s-\s*(#)?(\d+)([-_.]\d+)*(x\d+)?\s*-\s",
    r"\b(?<![\[\(\{])(%s)(\.)?\s*(\d+)([-_.]\d+)*(x\d+)?\b(?<!\s(\d+)([-_.]\d+)*(x\d+)?\s.*)"
    % _chapter_regex_keywords,
    r"(?<![A-Za-z]|%s)(?<![\[\(\{])(((%s)([-_. ]+)?(\d+)([-_.]\d+)*(x\d+)?)|\s+(\d+)(\.\d+)?(x\d+((\.\d+)+)?)?(\s+|#\d+|%s))"
    % (_exclusion_keywords_joined, _chapter_regex_keywords, _manga_extensions_regex),
    r"((?<!^)\b(\.)?\s*(%s)(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*\b)((\s+-|:)\s+).*?(?=\s*[\(\[\{](\d{4}|Digital)[\)\]\}])"
    % _exclusion_keywords_regex,
    r"(\b(%s)?(\.)?\s*((%s)(\d{1,2})|\d{3,})([-_.]\d+)*(x\d+)?(#\d+([-_.]\d+)*)?\b)\s*((\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})|((?<!\w(\s))|(?<!\w))(%s)(?!\w))"
    % (_chapter_regex_keywords, _exclusion_keywords_regex, _file_extensions_regex),
    r"^((#)?(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*)$",
]

CHAPTER_SEARCH_PATTERNS_COMP: List[Pattern[str]] = [
    re.compile(pattern, flags=re.IGNORECASE) for pattern in _chapter_searches
]

_cover_patterns = [
    r"(cover\.([A-Za-z]+))$",
    r"(\b(Cover([0-9]+|)|CoverDesign|page([-_. ]+)?cover)\b)",
    r"(\b(p000|page_000)\b)",
    r"((\s+)0+\.(.{2,}))",
    r"(\bindex[-_. ]1[-_. ]1\b)",
    r"(9([-_. :]+)?7([-_. :]+)?(8|9)(([-_. :]+)?[0-9]){10})",
]

COMPILED_COVER_PATTERNS: List[Pattern[str]] = [
    re.compile(pattern, flags=re.IGNORECASE) for pattern in _cover_patterns
]


# Discord
DISCORD_COLORS: Dict[str, int] = {
    "purple": 7615723,
    "red": 16711680,
    "grey": 8421504,
    "yellow": 16776960,
    "green": 65280,
    "preorder_blue": 5919485,
}
BOOKWALKER_LOGO_URL: str = "https://play-lh.googleusercontent.com/a7jUyjTxWrl_Kl1FkUSv2FHsSu3Swucpem2UIFDRbA1fmt5ywKBf-gcwe6_zalOqIR7V=w240-h480-rw"

# XML Namespaces
XML_NAMESPACES: Dict[str, str] = {
    "calibre": "http://calibre.kovidgoyal.net/2009/metadata",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "opf": "http://www.idpf.org/2007/opf",
    "u": "urn:oasis:names:tc:opendocument:xmlns:container",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}