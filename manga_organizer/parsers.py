"""Contains functions dedicated to parsing information from filenames or metadata."""

import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Optional, Tuple

from unidecode import unidecode

from . import constants
from .utils import (
    chapter_file_name_cleaning,
    check_for_multi_volume_file,
    contains_brackets,
    contains_keyword,
    contains_punctuation,
    contains_unicode,
    convert_to_ascii,
    ends_with_bracket,
    extract_all_numbers,
    get_extensionless_name,
    get_file_extension,
    get_min_and_max_numbers,
    get_subtitle_from_dash,
    has_multiple_numbers,
    has_one_set_of_numbers,
    is_one_shot,
    remove_dual_space,
    remove_punctuation,
    remove_s,
    replace_underscores,
    set_num_as_float_or_int,
    starts_with_bracket,
)


@lru_cache(maxsize=3500)
def get_series_name_from_volume(name: str, root: str, test_mode: bool = False, second: bool = False) -> Optional[str]:
    """Extracts the series name from a volume's filename."""
    if starts_with_bracket(name) and re.search(
        r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+(\s+[A-Za-z]{2})", name
    ):
        name = re.sub(r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+\s+", "", name).strip()

    name = remove_dual_space(name.replace("_extra", ".5")).strip()

    if "one" in name.lower() and "shot" in name.lower():
        name = re.sub(r"(-\s*)Ones?(-|)shot\s*", "", name, flags=re.IGNORECASE).strip()

    name = replace_underscores(name) if "_" in name else name

    if is_one_shot(name, root, test_mode=test_mode):
        name = re.sub(
            r"([-_ ]+|)(((\[|\(|\{).*(\]|\)|\}))|LN)([-_. ]+|)(%s|).*"
            % constants.file_extensions_regex.replace(r"\.", ""),
            "",
            name,
            flags=re.IGNORECASE,
        ).strip()
    else:
        if re.search(
            r"(\b|\s)(?<![A-Za-z])((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(%s)(\.|)([-_. ]|)([0-9]+)(\b|\s).*"
            % constants.volume_regex_keywords,
            name,
            flags=re.IGNORECASE,
        ):
            name = (
                re.sub(
                    r"(\b|\s)(?<![A-Za-z])((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(%s)(\.|)([-_. ]|)([0-9]+)(\b|\s).*"
                    % constants.volume_regex_keywords,
                    "",
                    name,
                    flags=re.IGNORECASE,
                )
            ).strip()
        else:
            name = re.sub(
                r"(\d+)?([-_. ]+)?((\[|\(|\})(.*)(\]|\)|\}))?([-_. ]+)?(%s)$"
                % constants.file_extensions_regex,
                "",
                name,
                flags=re.IGNORECASE,
            ).strip()

    if name.endswith(","):
        name = name[:-1].strip()

    name = re.sub(r"(%s)$" % constants.file_extensions_regex, "", name).strip()

    if name.lower().endswith("complete"):
        name = re.sub(r"(-|:)\s*Complete$", "", name, flags=re.IGNORECASE).strip()

    if (
        not name
        and not second
        and root
        and not contains_keyword(os.path.basename(root))
    ):
        name = get_series_name_from_volume(
            os.path.basename(root), root, test_mode=test_mode, second=True
        )
        name = remove_brackets(name) if contains_brackets(name) else name

    return name


@lru_cache(maxsize=3500)
def get_series_name_from_chapter(name: str, root: str, chapter_number: str = "", second: bool = False) -> Optional[str]:
    """Extracts the series name from a chapter's filename."""
    if starts_with_bracket(name) and re.search(
        r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+(\s+[A-Za-z]{2})", name
    ):
        name = re.sub(r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+\s+", "", name).strip()

    name = name.replace("_extra", ".5")

    if "one" in name.lower() and "shot" in name.lower():
        name = re.sub(r"(-\s*)Ones?(-|)shot\s*", "", name, flags=re.IGNORECASE).strip()

    name = remove_dual_space(name).strip()
    name = get_extensionless_name(name)
    name = replace_underscores(name) if "_" in name else name

    regex_matched = False
    search = next(
        (r for pattern in constants.chapter_search_patterns_comp if (r := pattern.search(name))),
        None,
    )

    if search:
        regex_matched = True
        search = search.group()
        name = name.split(search)[0].strip()

    result = ""
    if name:
        if isinstance(chapter_number, list):
            result = chapter_file_name_cleaning(
                name, chapter_number[0], regex_matched=regex_matched
            )
        else:
            result = chapter_file_name_cleaning(
                name, chapter_number, regex_matched=regex_matched
            )

    if result.endswith(","):
        result = result[:-1].strip()

    if (
        not result
        and not second
        and root
        and not contains_keyword(os.path.basename(root), chapter=True)
    ):
        root_number = get_release_number_cache(os.path.basename(root))
        result = get_series_name_from_chapter(
            os.path.basename(root),
            root,
            root_number if root_number else "",
            second=True,
        )
        result = remove_brackets(result) if contains_brackets(result) else result

    return result


@lru_cache(maxsize=3500)
def get_release_number(file: str, chapter: bool = False) -> Optional[str]:
    """Extracts the release/volume number from a filename."""
    def clean_series_name(name):
        if "." in name:
            name = re.sub(r"^\s*(\.)", "", name, re.IGNORECASE).strip()
        if ("-" in name or ":" in name) and re.search(r"(^\d+)", name.strip()):
            name = re.sub(r"((\s+(-)|:)\s+).*$", "", name, re.IGNORECASE).strip()
        if "#" in name:
            name = re.sub(r"($#)", "", name, re.IGNORECASE).strip()
            if re.search(r"(\d+#\d+)", name):
                name = re.sub(r"((#)([0-9]+)(([-_.])([0-9]+)|)+)", "", name).strip()
        if "x" in name:
            name = re.sub(r"(x[0-9]+)", "", name, re.IGNORECASE).strip()
        if contains_brackets(name):
            name = remove_brackets(name).strip()
        name = re.sub(
            r"(((\s+)?-(\s+)?([A-Za-z]+))?(%s))" % constants.file_extensions_regex,
            "",
            name,
            re.IGNORECASE,
        ).strip()
        if "-" in name:
            if name.startswith("- "):
                name = name[1:].strip()
            if name.endswith(" -"):
                name = name[:-1].strip()
        if name.startswith("#"):
            name = name[1:].strip()
        return name

    results = []
    is_multi_volume = False
    keywords = constants.volume_regex_keywords if not chapter else constants.chapter_regex_keywords
    result = None

    file = remove_dual_space(file.replace("_extra", ".5")).strip()
    file = replace_underscores(file) if "_" in file else file
    is_multi_volume = check_for_multi_volume_file(file, chapter=chapter) if "-" in file else False

    if not chapter:
        result = constants.volume_number_search_pattern.search(file)
    else:
        if has_multiple_numbers(file):
            extension_less_file = get_extensionless_name(file)
            if constants.chapter_number_search_pattern.search(extension_less_file):
                file = constants.chapter_number_search_pattern.sub("", extension_less_file)
                if file.endswith("-") and not re.search(
                    r"-(\s+)?(#)?([0-9]+)(([-_.])([0-9]+)|)+(x[0-9]+)?(\s+)?-", file
                ):
                    file = file[:-1].strip()
        result = next(
            (r for pattern in constants.chapter_search_patterns_comp if (r := pattern.search(file))),
            None,
        )

    if result:
        try:
            file = result.group().strip() if hasattr(result, "group") else ""
            if chapter:
                file = clean_series_name(file)
            if not file.replace(".", "").replace("-", "").isdigit():
                file = re.sub(
                    r"\b({})(\.|)([-_. ])?".format(keywords),
                    "",
                    file,
                    flags=re.IGNORECASE,
                ).strip()
                if not file.replace(".", "").replace("-", "").isdigit() and re.search(
                    r"\b[0-9]+({})[0-9]+\b".format(keywords),
                    file,
                    re.IGNORECASE,
                ):
                    file = (
                        re.sub(
                            r"({})".format(keywords),
                            ".",
                            file,
                            flags=re.IGNORECASE,
                        )
                    ).strip()
            try:
                if is_multi_volume or (
                    ("-" in file or "_" in file)
                    and re.search(
                        r"([0-9]+(\.[0-9]+)?)([-_]([0-9]+(\.[0-9]+)?))+", file
                    )
                ):
                    if not is_multi_volume:
                        is_multi_volume = True
                    multi_numbers = get_min_and_max_numbers(file)
                    if multi_numbers:
                        results.extend(
                            (
                                int(volume_number)
                                if float(volume_number).is_integer()
                                else float(volume_number)
                            )
                            for volume_number in multi_numbers
                        )
                        if len(multi_numbers) == 1:
                            is_multi_volume = False
                            results = (
                                int(results[0])
                                if float(results[0]).is_integer()
                                else float(results[0])
                            )
                else:
                    if file.endswith("0") and ".0" in file:
                        file = file.split(".0")[0]
                    results = int(file) if float(file).is_integer() else float(file)
            except ValueError:
                return ""
        except AttributeError:
            return ""

    if results or results == 0:
        if is_multi_volume:
            return tuple(results)
        elif chapter:
            return results
        elif results < 2000:
            return results
    return ""


def get_release_number_cache(file: str, chapter: bool = False) -> Optional[str]:
    result = get_release_number(file, chapter=chapter)
    return list(result) if isinstance(result, tuple) else result


def get_release_year(name: str, metadata: dict = None) -> Optional[int]:
    """Extracts the release year from a filename."""
    result = None
    match = re.search(r"(\(|\[|\{)(\d{4})(\)|\]|\})", name, re.IGNORECASE)
    if match:
        result = int(re.sub(r"(\(|\[|\{)|(\)|\]|\})", "", match.group()))
    if not result and metadata:
        release_year_from_file = None
        if "Summary" in metadata and "Year" in metadata:
            release_year_from_file = metadata["Year"]
        elif "dc:description" in metadata and "dc:date" in metadata:
            release_year_from_file = metadata["dc:date"].strip()
            release_year_from_file = re.search(r"\d{4}", release_year_from_file)
            release_year_from_file = (
                release_year_from_file.group() if release_year_from_file else None
            )
        if release_year_from_file and release_year_from_file.isdigit():
            result = int(release_year_from_file)
            if result < 1950:
                result = None
    return result


@lru_cache(maxsize=3500)
def get_subtitle_from_title(file, publisher=None) -> Optional[str]:
    """Extracts a subtitle from a title string."""
    subtitle = ""
    without_series_name = re.sub(
        rf"^{re.escape(file.series_name)}", "", file.name, flags=re.IGNORECASE
    ).strip()
    dash_or_colon_search = get_subtitle_from_dash(without_series_name)
    year_or_digital_search = re.search(
        r"([\[\{\(]((\d{4})|(Digital))[\]\}\)])",
        without_series_name,
        re.IGNORECASE,
    )
    publisher_search = None
    if (
        publisher and (publisher.from_meta or publisher.from_name)
    ) and not year_or_digital_search:
        if publisher.from_meta:
            publisher_search = re.search(
                rf"([\[\{{\(\]])({publisher.from_meta})([\]\}}\)])",
                without_series_name,
                re.IGNORECASE,
            )
        if publisher.from_name and not publisher_search:
            publisher_search = re.search(
                rf"([\[\{{\(\]])({publisher.from_name})([\]\}}\)])",
                without_series_name,
                re.IGNORECASE,
            )
    if dash_or_colon_search and (year_or_digital_search or publisher_search):
        subtitle = re.sub(r"(.*)((\s+(-)|:)\s+)", "", without_series_name)
        if subtitle.endswith(file.extension):
            subtitle = get_extensionless_name(subtitle)
        if not publisher_search:
            subtitle = re.sub(
                r"([\[\{\(]((\d{4})|(Digital))[\]\}\)])(.*)",
                "",
                subtitle,
                flags=re.IGNORECASE,
            )
        else:
            if (
                publisher.from_meta
                and publisher_search.group(2).lower().strip()
                == publisher.from_meta.lower().strip()
            ):
                subtitle = re.sub(
                    rf"([\[\{{\(\]])({publisher.from_meta})([\]\}}\)])(.*)",
                    "",
                    subtitle,
                )
            elif (
                publisher.from_name
                and publisher_search.group(2).lower().strip()
                == publisher.from_name.lower().strip()
            ):
                subtitle = re.sub(
                    rf"([\[\{{\(\]])({publisher.from_name})([\]\}}\)])(.*)",
                    "",
                    subtitle,
                )
        subtitle = remove_dual_space(subtitle).strip()
        if re.search(
            rf"{re.escape(subtitle)}",
            os.path.basename(os.path.dirname(file.path)),
            re.IGNORECASE,
        ):
            subtitle = ""
        if file.volume_number and re.search(
            rf"^({constants.volume_regex_keywords})(\s+)?(0+)?{file.volume_number}$",
            subtitle.strip(),
            re.IGNORECASE,
        ):
            subtitle = ""
    return subtitle


def parse_comicinfo_xml(xml_content: str) -> dict:
    """Parses a ComicInfo.xml file content."""
    tags = {}
    if xml_content:
        try:
            tree = ET.fromstring(xml_content)
            tags = {child.tag: child.text for child in tree}
        except ET.ParseError as e:
            print(f"Failed to parse comicinfo.xml: {e}")
            return tags
    return tags


@lru_cache(maxsize=3500)
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
    """Cleans a string by removing unwanted characters."""
    s = string.lower().strip() if not skip_lowercase_convert else string
    s = s.replace(":", " ") if not skip_colon_replace and ":" in s else s
    s = remove_dual_space(s)
    s = remove_brackets(s) if not skip_bracket and contains_brackets(s) else s
    s = unidecode(s) if not skip_unidecode and contains_unicode(s) else s
    s = normalize_str(s) if not skip_normalize else s
    s = remove_punctuation(s) if not skip_punctuation and contains_punctuation(s) else s
    s = remove_s(s) if not skip_remove_s else s
    s = remove_dual_space(s)
    s = convert_to_ascii(s) if not skip_convert_to_ascii else s
    s = replace_underscores(s) if not skip_underscore and "_" in s else s
    return s.strip()


@lru_cache(maxsize=3500)
def normalize_str(
    s: str,
    skip_common_words: bool = False,
    skip_editions: bool = False,
    skip_type_keywords: bool = False,
    skip_japanese_particles: bool = False,
    skip_misc_words: bool = False,
    skip_storefront_keywords: bool = False,
) -> str:
    """Normalizes a string for consistent comparisons."""
    if len(s) <= 1:
        return s
    words_to_remove = []
    if not skip_common_words:
        words_to_remove.extend(["the", "a", "à", "and", "&", "I", "of"])
    if not skip_editions:
        words_to_remove.extend(
            [
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
            ]
        )
    if not skip_type_keywords:
        words_to_remove.extend(
            [
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
            ]
        )
    if not skip_japanese_particles:
        words_to_remove.extend(
            ["wa", "o", "mo", "ni", "e", "de", "ga", "kara", "to", "ya", r"no(?!\.)", "ne", "yo"]
        )
    if not skip_misc_words:
        words_to_remove.extend([r"((\d+)([-_. ]+)?th)", "x", "×", "HD"])
    if not skip_storefront_keywords:
        words_to_remove.extend([r"Book(\s+)?walker"])
    for word in words_to_remove:
        pattern = rf"\b{word}\b" if word not in constants.type_keywords else rf"{word}\s"
        s = re.sub(pattern, " ", s, flags=re.IGNORECASE).strip()
        s = remove_dual_space(s)
    return s.strip()


@lru_cache(maxsize=3500)
def remove_brackets(string: str) -> str:
    """Removes bracketed content from a string."""
    if (
        starts_with_bracket(string)
        and ends_with_bracket(string)
        and constants.bracket_avoidance_pattern.search(string)
    ):
        return string
    string = constants.bracket_removal_pattern.sub("", string).strip()
    ext = get_file_extension(string)
    if ext:
        string = (
            constants.bracket_against_extension_pattern.sub(r"\2", string).strip()
            if contains_brackets(string)
            else string
        )
        string = string.replace(ext, "").strip()
        string = f"{string}{ext}"
    return string