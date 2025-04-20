# komga_cover_extractor/string_utils.py
import os
import re
import string
from difflib import SequenceMatcher
from functools import lru_cache
from unidecode import unidecode
from titlecase import titlecase

# Assuming these will be moved to config.py or other utils
# Import necessary config variables
# Use try-except for robustness during refactoring
try:
    from .config import (
        volume_keywords,
        chapter_keywords,
        exclusion_keywords,
        subtitle_exclusion_keywords,
        volume_regex_keywords,
        exclusion_keywords_joined,
        subtitle_exclusion_keywords_joined,
        exclusion_keywords_regex,
        subtitle_exclusion_keywords_regex,
        chapter_regex_keywords,
        file_extensions_regex,
        manga_extensions_regex,
        chapter_searches,
        chapter_search_patterns_comp,
        volume_year_regex,
        publishers_joined,
        release_groups_joined,
        publishers_joined_regex,
        release_groups_joined_regex,
        release_group_end_regex,
        required_similarity_score,
        publishers,
        release_groups,
        exception_keywords,
        zfill_volume_int_value,
        zfill_volume_float_value,
        zfill_chapter_int_value,
        zfill_chapter_float_value,
        preferred_volume_renaming_format,
        preferred_chapter_renaming_format,
        add_issue_number_to_manga_file_name,
        manga_extensions,
        novel_extensions,
        file_extensions,
        add_publisher_name_to_file_name_when_renaming,
        search_and_add_premium_to_file_name,
        move_release_group_to_end_of_file_name,
        release_group_similarity_score,
        replace_unicode_when_restructuring,
        manual_rename,
        mute_discord_rename_notifications,
        watchdog_toggle,
        transferred_files,  # Used by reorganize_and_rename
        download_folders,
        paths,  # Used by get_series_name_from_volume, is_one_shot
    )
except ImportError:
    print("WARN: Could not import from .config in string_utils.py, using placeholders.")
    # Define essential placeholders if config fails
    (
        volume_keywords,
        chapter_keywords,
        exclusion_keywords,
        subtitle_exclusion_keywords,
    ) = ([], [], [], [])
    (
        volume_regex_keywords,
        exclusion_keywords_joined,
        subtitle_exclusion_keywords_joined,
    ) = ("", "", "")
    (
        exclusion_keywords_regex,
        subtitle_exclusion_keywords_regex,
        chapter_regex_keywords,
    ) = ("", "", "")
    file_extensions_regex, manga_extensions_regex = "", ""
    chapter_searches, chapter_search_patterns_comp = [], []
    volume_year_regex, publishers_joined, release_groups_joined = "", "", ""
    publishers_joined_regex, release_groups_joined_regex, release_group_end_regex = (
        None,
        None,
        None,
    )
    required_similarity_score = 0.9
    publishers, release_groups, exception_keywords = [], [], []
    (
        zfill_volume_int_value,
        zfill_volume_float_value,
        zfill_chapter_int_value,
        zfill_chapter_float_value,
    ) = (2, 4, 3, 5)
    preferred_volume_renaming_format, preferred_chapter_renaming_format = "", ""
    add_issue_number_to_manga_file_name = False
    manga_extensions, novel_extensions, file_extensions = [], [], []
    add_publisher_name_to_file_name_when_renaming = False
    search_and_add_premium_to_file_name = False
    move_release_group_to_end_of_file_name = False
    release_group_similarity_score = 0.8
    replace_unicode_when_restructuring = False
    manual_rename = False
    mute_discord_rename_notifications = False
    watchdog_toggle = False
    transferred_files = []
    download_folders, paths = [], []


# Import necessary functions from other utils
try:
    from .log_utils import send_message
    from .file_utils import (
        get_file_extension,
        get_extensionless_name,
    )  # Removed rename_file, clean_and_sort
    from .misc_utils import (
        set_num_as_float_or_int,
        isfloat,
        isint,
    )  # Removed get_input_from_user
    from .models import Volume, Publisher  # Import models needed by functions here
    from .metadata_utils import (
        get_internal_metadata,
        check_for_premium_content,
    )  # Import needed metadata functions
    from .archive_utils import get_zip_comment  # Import needed archive functions
except ImportError as e:
    print(f"WARN: Could not import utility functions in string_utils.py: {e}")

    # Define placeholders if imports fail
    def send_message(msg, error=False):
        print(f"{'ERROR: ' if error else ''}{msg}")

    def get_file_extension(f):
        return os.path.splitext(f)[1]

    def get_extensionless_name(f):
        return os.path.splitext(f)[0]

    def set_num_as_float_or_int(n, silent=False):
        return n

    def isfloat(x):
        return False

    def isint(x):
        return False

    class Volume:
        pass

    class Publisher:
        pass

    def get_internal_metadata(*args):
        return None

    def check_for_premium_content(*args):
        return False

    def get_zip_comment(*args):
        return ""


# --- Basic String Checks ---


def starts_with_bracket(s):
    """Checks if a string starts with an opening bracket."""
    return s.startswith(("(", "[", "{"))


def ends_with_bracket(s):
    """Checks if a string ends with a closing bracket."""
    return s.endswith((")", "]", "}"))


# Pre-compile the bracket pattern
brackets_pattern = re.compile(r"[(){}\[\]]")


def contains_brackets(s):
    """Checks if a string contains any brackets."""
    return bool(brackets_pattern.search(s))


# Pre-compile punctuation pattern
punctuation_pattern = re.compile(r"[^\w\s+]")  # Keeps word chars, whitespace, and '+'


def contains_punctuation(s):
    """Checks if a string contains punctuation (excluding whitespace and '+')."""
    return bool(punctuation_pattern.search(s))


def contains_unicode(input_str):
    """Checks if a string contains non-ASCII characters."""
    try:
        input_str.encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def contains_non_numeric(input_string):
    """Checks if a string contains non-numeric characters (excluding '.' for floats)."""
    try:
        float(input_string)
        return False
    except (ValueError, TypeError):
        # If conversion to float fails, check if it's purely digits
        return not input_string.isdigit()


# --- String Cleaning ---

# Pre-compile dual space removal
dual_space_pattern = re.compile(r"(\s{2,})")


@lru_cache(maxsize=3500)
def remove_dual_space(s):
    """Replaces multiple spaces with a single space."""
    if "  " not in s:
        return s
    return dual_space_pattern.sub(" ", s)


@lru_cache(maxsize=3500)
def replace_underscores(name):
    """Replaces underscores with spaces, handling numbers correctly."""
    # Replace underscores between numbers with a period
    name = re.sub(r"(?<=\d)_(?=\d)", ".", name)
    # Replace all other underscores with a space
    name = name.replace("_", " ")
    name = remove_dual_space(name).strip()  # Use local function
    return name


# Pre-compiled remove_brackets() patterns
bracket_removal_pattern = re.compile(
    # Regex 1: Year brackets like (1999) or [2023]
    r"([\[({]\d{4}[\])}])" + "|" +
    # Regex 2: Other brackets not adjacent to hyphens or letters (more conservative)
    r"((?<![-\w])(\[[^\]]*?\]|\([^)]*?\)|\{[^}]*?\})(?![-\w]))",
    re.IGNORECASE,
)
bracket_avoidance_pattern = re.compile(
    r"^[\(\[\{].*[\)\]\}]$"
)  # Avoid removing if string IS a bracket
bracket_against_extension_pattern = re.compile(
    r"(\[[^\]]*?\]|\([^)]*?\)|\{[^}]*?\})(\.\w+)$"
)  # Bracket right before extension


@lru_cache(maxsize=3500)
def remove_brackets(string):
    """Removes bracketed content unless it's the entire string or deemed important."""
    if (
        starts_with_bracket(string)  # Use local function
        and ends_with_bracket(string)  # Use local function
        and bracket_avoidance_pattern.search(string)
    ):
        return string  # Don't remove if the whole string is bracketed

    # More conservative removal - target specific patterns like year first
    string = re.sub(r"[\[({]\d{4}[\])}]", "", string).strip()
    # Then remove other brackets if they seem like metadata (e.g., not part of title)
    # This part is tricky and might need refinement based on common patterns
    # Example: Remove bracket if it contains common keywords like Digital, Scan, etc.
    string = re.sub(
        r"\s*(\[[^\]]*(Digital|Scan|RAW)[^\]]*\]|\([^)]*(Digital|Scan|RAW)[^)]*\))\s*",
        " ",
        string,
        flags=re.IGNORECASE,
    ).strip()

    string = remove_dual_space(string)  # Use local function

    # Handle brackets right before extension
    ext = get_file_extension(string)  # Use imported file_utils function
    if ext:
        string_no_ext = get_extensionless_name(
            string
        )  # Use imported file_utils function
        cleaned_no_ext = bracket_against_extension_pattern.sub(
            "", string_no_ext
        ).strip()
        string = f"{cleaned_no_ext}{ext}"

    return string


@lru_cache(maxsize=3500)
def remove_punctuation(s):
    """Removes punctuation from a string, keeping whitespace and '+'."""
    # Keep hyphens as they might be part of names/titles
    return punctuation_pattern.sub(" ", s).strip()


@lru_cache(maxsize=3500)
def remove_s(s):
    """Removes trailing 's' from words (simple pluralization removal)."""
    # Be careful with this, might remove 's' from names ending in 's'
    return re.sub(r"\b(\w+?)s\b", r"\1", s, flags=re.IGNORECASE).strip()


@lru_cache(maxsize=3500)
def convert_to_ascii(s):
    """Converts a string to ASCII, removing non-ASCII characters."""
    return "".join(i for i in s if ord(i) < 128)


@lru_cache(maxsize=3500)
def normalize_str(
    s,
    skip_common_words=False,
    skip_editions=False,
    skip_type_keywords=False,
    skip_japanese_particles=False,
    skip_misc_words=False,
    skip_storefront_keywords=False,
):
    """Normalizes a string by removing common/unimportant words."""
    if len(s) <= 1:
        return s

    words_to_remove = []
    # Define word lists (consider moving to config if extensive)
    common_words = ["the", "a", "à", "an", "and", "&", "I", "of"]
    editions = [
        "Collection",
        "Master Edition",
        r"\d-in-\d Edition",
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
    type_keywords = [
        r"(?<!^)Novel",
        r"(?<!^)Light Novel",
        r"(?<!^)Manga",
        r"(?<!^)Comic",
        r"(?<!^)LN",
        r"(?<!^)Series",
        r"(?<!^)Volume",
        r"(?<!^)Chapter",
        r"(?<!^)Book",
        r"(?<!^)MANHUA",
    ]
    japanese_particles = [
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
    ]
    misc_words = [r"\d+th", "x", "×", "HD"]
    storefront_keywords = [r"Book\s*walker"]

    if not skip_common_words:
        words_to_remove.extend(common_words)
    if not skip_editions:
        words_to_remove.extend(editions)
    if not skip_type_keywords:
        words_to_remove.extend(type_keywords)
    if not skip_japanese_particles:
        words_to_remove.extend(japanese_particles)
    if not skip_misc_words:
        words_to_remove.extend(misc_words)
    if not skip_storefront_keywords:
        words_to_remove.extend(storefront_keywords)

    # Build a single regex for removal
    # Use word boundaries (\b) carefully, especially with patterns like r"no(?!\.)"
    # Add spaces around removed words to avoid merging adjacent words
    pattern_str = r"\b(?:" + "|".join(words_to_remove) + r")\b"
    # Handle negative lookbehind patterns separately if needed, or adjust main pattern
    # For simplicity here, we'll use the combined pattern, but refinement might be needed
    s = re.sub(pattern_str, " ", s, flags=re.IGNORECASE).strip()

    s = remove_dual_space(s)  # Use local function
    return s.strip()


@lru_cache(maxsize=3500)
def clean_str(
    string,
    skip_lowercase_convert=False,
    skip_colon_replace=False,
    skip_bracket=False,
    skip_unidecode=False,
    skip_normalize=False,
    skip_punctuation=False,
    skip_remove_s=False,
    skip_convert_to_ascii=False,
    skip_underscore=False,
):
    """Applies various cleaning functions to a string."""
    s = string.lower().strip() if not skip_lowercase_convert else string.strip()
    s = (
        s.replace(":", " - ") if not skip_colon_replace and ":" in s else s
    )  # Replace colon with hyphen-space
    s = remove_dual_space(s)  # Use local function
    s = (
        remove_brackets(s) if not skip_bracket and contains_brackets(s) else s
    )  # Use local functions
    s = (
        unidecode(s) if not skip_unidecode and contains_unicode(s) else s
    )  # Use local function
    s = normalize_str(s) if not skip_normalize else s  # Use local function
    s = (
        remove_punctuation(s) if not skip_punctuation and contains_punctuation(s) else s
    )  # Use local functions
    # s = remove_s(s) if not skip_remove_s else s # Removing 's' can be problematic, disable by default
    s = remove_dual_space(s)  # Use local function
    s = (
        convert_to_ascii(s) if not skip_convert_to_ascii and contains_unicode(s) else s
    )  # Use local functions
    s = (
        replace_underscores(s) if not skip_underscore and "_" in s else s
    )  # Use local function
    return remove_dual_space(s).strip()  # Final cleanup


# --- Keyword Checks ---

# Pre-compile volume pattern
volume_regex = re.compile(
    # Simplified: Look for volume keywords followed by numbers
    r"\b(%s)\s*([0-9]+(?:[._-][0-9]+)?)\b"
    % volume_regex_keywords,  # Use imported config value
    re.IGNORECASE,
)


@lru_cache(maxsize=3500)
def contains_volume_keywords(file):
    """Checks if a filename contains volume-related keywords followed by a number."""
    file = file.replace("_extra", ".5")
    file = remove_dual_space(file).strip()  # Use local function
    # Don't remove brackets here, keyword might be inside
    clean_file = (
        replace_underscores(file).strip() if "_" in file else file.strip()
    )  # Use local function
    clean_file = remove_dual_space(clean_file).strip()  # Use local function
    return bool(volume_regex.search(clean_file))


@lru_cache(maxsize=3500)
def contains_chapter_keywords(file_name):
    """Checks if a filename contains chapter-related keywords or number patterns."""
    file_name_clean = file_name.replace("_extra", ".5")
    file_name_clean = (
        replace_underscores(file_name_clean).strip()
        if "_" in file_name_clean
        else file_name_clean
    )  # Use local function
    file_name_clean = remove_dual_space(file_name_clean).strip()  # Use local function

    found = False
    for pattern in chapter_search_patterns_comp:  # Use imported config value
        result = pattern.search(file_name_clean)
        if result:
            result_group = result.group()
            if not (
                starts_with_bracket(result_group)
                and ends_with_bracket(result_group)
                and re.search(r"^\([({[]\d{4}[\])}]\)$", result_group)
            ):  # Use local functions
                found = True
                break

    # Fallback check for lone numbers if no keywords found
    if not found and not contains_volume_keywords(file_name):  # Use local function
        without_year = re.sub(
            volume_year_regex, "", file_name, flags=re.IGNORECASE
        )  # Use imported config value
        without_year = re.sub(
            r"\b(19|20)\d{2}\b", "", without_year
        ).strip()  # Remove potential years more broadly
        # Look for numbers not preceded by volume keywords
        # This regex needs careful testing to avoid false positives
        lone_number_match = re.search(
            r"(?<!(%s)\s*)\b\d+([._]\d+)?\b" % volume_regex_keywords,
            without_year,
            re.IGNORECASE,
        )  # Use imported config value
        if lone_number_match:
            # Further check: is the number likely a volume number based on context?
            # Example: if it's > 500, less likely to be a volume. This is heuristic.
            try:
                num_val = float(lone_number_match.group())
                if num_val < 1000:  # Arbitrary threshold, adjust as needed
                    found = True
            except ValueError:
                pass  # Not a number

    return found


def contains_keyword(file_string, chapter=False):
    """Checks if a string contains a volume or chapter keyword followed by numbers."""
    keywords = (
        chapter_regex_keywords if chapter else volume_regex_keywords
    )  # Use imported config values
    # Simplified regex: keyword followed by number
    pattern = rf"\b({keywords})\s*(\d+([._]\d+)?)\b"
    return bool(re.search(pattern, file_string, re.IGNORECASE))


def check_for_exception_keywords(file_name, exception_keywords_list):
    """Checks if a filename contains any specified exception keywords."""
    if not exception_keywords_list:
        return False
    # Use word boundaries for safer matching
    pattern = r"\b(?:" + "|".join(exception_keywords_list) + r")\b"
    return bool(re.search(pattern, file_name, re.IGNORECASE))


# --- Number/Part Extraction ---

# get_release_number, get_release_number_cache, get_file_part, get_min_and_max_numbers,
# abbreviate_numbers, complete_num_array, has_one_set_of_numbers, has_multiple_numbers,
# extract_all_numbers are complex and depend heavily on config regexes.
# Including simplified placeholders or full implementations with TODOs for refinement.


@lru_cache(maxsize=3500)
def get_release_number(file, chapter=False):
    """Finds the volume/chapter number(s) in the file name."""

    # Cleans up the chapter's series name before number extraction
    def clean_series_name(name):
        # Removes starting period
        if "." in name:
            name = re.sub(r"^\s*(\.)", "", name, re.IGNORECASE).strip()
        # Remove subtitle
        if ("-" in name or ":" in name) and re.search(r"(^\d+)", name.strip()):
            name = re.sub(r"((\s+(-)|:)\s+).*$", "", name, re.IGNORECASE).strip()
        # Removes # from the number
        if "#" in name:
            name = re.sub(r"($#)", "", name, re.IGNORECASE).strip()
            if re.search(r"(\d+#\d+)", name):
                name = re.sub(r"((#)([0-9]+)(([-_.])([0-9]+)|)+)", "", name).strip()
        # removes part like x1
        if "x" in name:
            name = re.sub(r"(x[0-9]+)", "", name, re.IGNORECASE).strip()
        # removes brackets
        if contains_brackets(name):
            name = remove_brackets(name).strip()  # Use outer scope function
        # Removes extension and optional preceding text
        name = re.sub(
            r"(((\s+)?-(\s+)?([A-Za-z]+))?(%s))" % file_extensions_regex,
            "",
            name,
            re.IGNORECASE,
        ).strip()  # Use outer scope config
        # Clean up dangling hyphens
        if "-" in name:
            if name.startswith("- "):
                name = name[1:].strip()
            if name.endswith(" -"):
                name = name[:-1].strip()
        # remove leading #
        if name.startswith("#"):
            name = name[1:].strip()
        return name

    results = []
    is_multi_volume = False
    keywords = (
        volume_regex_keywords if not chapter else chapter_regex_keywords
    )  # Use outer scope config
    result = None

    file = remove_dual_space(
        file.replace("_extra", ".5")
    ).strip()  # Use outer scope function
    file = (
        replace_underscores(file) if "_" in file else file
    )  # Use outer scope function

    # Check for multi-volume pattern first
    is_multi_volume = (
        check_for_multi_volume_file(file, chapter=chapter) if "-" in file else False
    )  # Use outer scope function

    if not chapter:
        # Search for volume number pattern
        result = volume_number_search_pattern.search(
            file
        )  # Use outer scope compiled pattern
    else:
        # Chapter search logic
        if has_multiple_numbers(file):  # Use outer scope function
            extension_less_file = get_extensionless_name(
                file
            )  # Use imported file_utils function
            if chapter_number_search_pattern.search(
                extension_less_file
            ):  # Use outer scope compiled pattern
                file_part = chapter_number_search_pattern.sub("", extension_less_file)
                if file_part.endswith("-") and not re.search(
                    r"-(\s+)?(#)?([0-9]+)(([-_.])([0-9]+)|)+(x[0-9]+)?(\s+)?-",
                    file_part,
                ):
                    file = file_part[
                        :-1
                    ].strip()  # Update file for subsequent search if pattern removed suffix

        # Find the first matching chapter pattern
        result = next(
            (
                r
                for pattern in chapter_search_patterns_comp
                if (r := pattern.search(file))
            ),
            None,
        )  # Use outer scope config

    if result:
        try:
            file_match_str = result.group().strip() if hasattr(result, "group") else ""

            if chapter:
                file_match_str = clean_series_name(file_match_str)  # Use inner helper

            # Remove keywords if they exist and the string isn't purely numeric
            if contains_non_numeric(file_match_str):  # Use outer scope function
                file_match_str = re.sub(
                    r"\b({})(\.|)([-_. ])?".format(keywords),
                    "",
                    file_match_str,
                    flags=re.IGNORECASE,
                ).strip()
                # Handle cases like 1v1
                if contains_non_numeric(file_match_str) and re.search(
                    r"\b[0-9]+({})[0-9]+\b".format(keywords),
                    file_match_str,
                    re.IGNORECASE,
                ):
                    file_match_str = re.sub(
                        r"({})".format(keywords),
                        ".",
                        file_match_str,
                        flags=re.IGNORECASE,
                    ).strip()

            # Extract numbers
            if is_multi_volume or (
                ("-" in file_match_str or "_" in file_match_str)
                and re.search(
                    r"([0-9]+(\.[0-9]+)?)([-_]([0-9]+(\.[0-9]+)?))+", file_match_str
                )
            ):
                if not is_multi_volume:
                    is_multi_volume = True
                multi_numbers = get_min_and_max_numbers(
                    file_match_str
                )  # Use outer scope function
                if multi_numbers:
                    results.extend(
                        set_num_as_float_or_int(num, silent=True)
                        for num in multi_numbers
                    )  # Use imported misc_utils function
                    if len(multi_numbers) == 1:  # If range was like 1-1
                        is_multi_volume = False
                        results = results[0]  # Convert back to single value
            else:
                # Handle single number
                num_str = file_match_str
                if num_str.endswith("0") and ".0" in num_str:
                    num_str = num_str.split(".0")[0]  # Remove trailing .0
                results = set_num_as_float_or_int(
                    num_str, silent=True
                )  # Use imported misc_utils function

        except (ValueError, TypeError, AttributeError) as e:
            send_message(
                f"Error parsing release number from '{result.group() if hasattr(result, 'group') else file}': {e}",
                error=True,
            )  # Use imported log_utils function
            return ""  # Return empty on parsing error

    # Final validation and formatting
    if results or results == 0:
        if is_multi_volume and isinstance(results, list) and len(results) > 1:
            return tuple(results)  # Return tuple for multi-volume ranges
        elif not isinstance(results, list):  # Single number
            # Basic sanity check for volumes (e.g., not a year)
            if not chapter and isinstance(results, (int, float)) and results >= 2000:
                return ""  # Likely a year, not a volume number
            return results
        elif (
            isinstance(results, list) and len(results) == 1
        ):  # Single item list from multi-vol parse
            num = results[0]
            if not chapter and isinstance(num, (int, float)) and num >= 2000:
                return ""
            return num

    return ""  # Return empty string if no valid number found


def get_release_number_cache(file, chapter=False):
    """Cached wrapper for get_release_number."""
    result = get_release_number(file, chapter=chapter)  # Use local function
    # Convert tuple back to list for consistency if needed by callers expecting list for multi-volume
    # However, the original cache function didn't do this, so stick to tuple for cache key.
    return result  # Return tuple or single value


@lru_cache(maxsize=3500)
def get_file_part(file, chapter=False, series_name=None, subtitle=None):
    """Retrieves and returns the file part from the file name."""
    result = ""

    # Define regex patterns locally (as they were in the original script)
    # These rely on volume_regex_keywords from config
    rx_remove_vol_num = re.compile(
        r".*(%s)([-_. ]|)([-_. ]|)([0-9]+)(\b|\s)"
        % volume_regex_keywords,  # Use imported config value
        re.IGNORECASE,
    )
    rx_search_part_num = re.compile(r"(\b(Part)([-_. ]|)([0-9]+)\b)", re.IGNORECASE)
    rx_search_chapters_num = re.compile(
        r"([0-9]+)(([-_.])([0-9]+)|)+((x|#)([0-9]+)(([-_.])([0-9]+)|)+)", re.IGNORECASE
    )
    rx_remove_x_hash_num = re.compile(r"((x|#))", re.IGNORECASE)

    contains_keyword = (
        re.search(r"\bpart\b", file, re.IGNORECASE) if "part" in file.lower() else ""
    )
    contains_indicator = "#" in file or "x" in file

    if not contains_keyword and not contains_indicator:
        return result

    # Remove series name and subtitle if provided to isolate the part info
    if series_name:
        file = re.sub(re.escape(series_name), "", file, flags=re.IGNORECASE).strip()
    if subtitle:
        file = re.sub(re.escape(subtitle), "", file, flags=re.IGNORECASE).strip()

    if not chapter:
        if contains_keyword:
            # Remove volume number part first to avoid confusion
            file_no_vol = rx_remove_vol_num.sub("", file).strip()
            search = rx_search_part_num.search(file_no_vol)
            if search:
                result = search.group(1)  # Get the full "Part X" match
                # Extract just the number
                result = re.sub(
                    r"Part([-_. ]|)+", " ", result, flags=re.IGNORECASE
                ).strip()
    else:  # Chapter logic
        if contains_indicator:
            search = rx_search_chapters_num.search(file)
            if search:
                part_search = re.search(
                    r"((x|#)([0-9]+)(([-_.])([0-9]+)|)+)", search.group(), re.IGNORECASE
                )
                if part_search:
                    # remove the x or # prefix
                    result = rx_remove_x_hash_num.sub("", part_search.group())

    # Convert the extracted number string to int/float
    result = set_num_as_float_or_int(
        result, silent=True
    )  # Use imported misc_utils function

    return result


def get_min_and_max_numbers(string):
    """
    Converts a string containing numbers (potentially ranges like '1-3', '1_5', '2, 4')
    into a list containing only the lowest and highest numbers found.
    EX: "1, 2, 3" -> [1, 3]
    EX: "1-5" -> [1, 5]
    EX: "2" -> [2]
    """
    # initialize an empty list to hold the numbers
    numbers = []

    # replace hyphens, underscores, and commas with spaces using regular expressions
    numbers_search = re.sub(r"[-_,]", " ", string)

    # remove any duplicate spaces
    numbers_search = remove_dual_space(numbers_search).strip()  # Use local function

    # split the resulting string into a list of individual number strings
    numbers_search = numbers_search.split(" ")

    # convert each string in the list to either an integer or a float
    # Use imported set_num_as_float_or_int and filter out empty strings
    numbers_search = [
        set_num_as_float_or_int(num, silent=True) for num in numbers_search if num
    ]
    # Filter out non-numeric results (which set_num_as_float_or_int might return as empty string or original string)
    numbers_search = [num for num in numbers_search if isinstance(num, (int, float))]

    # if the resulting list is not empty, filter it further
    if numbers_search:
        try:
            # get lowest number in list
            lowest_number = min(numbers_search)

            # get highest number in list if more than one number exists
            highest_number = max(numbers_search) if len(numbers_search) > 1 else None

            # discard any numbers in between the lowest and highest number
            numbers = [lowest_number]
            if highest_number is not None and highest_number != lowest_number:
                numbers.append(highest_number)
        except (ValueError, TypeError) as e:
            send_message(
                f"Error finding min/max in get_min_and_max_numbers for '{string}': {e}",
                error=True,
            )  # Use imported log_utils function
            return []  # Return empty list on error

    # return the resulting list of numbers (min and max)
    return numbers


def abbreviate_numbers(numbers):
    """Abbreviates sequential numbers in a list (e.g., [1, 2, 3, 5] -> '1-3, 5')."""
    # TODO: Implement full logic
    return ", ".join(map(str, numbers))  # Placeholder


def complete_num_array(arr):
    """Fills in missing whole numbers in a list (e.g., [1, 5] -> [1, 2, 3, 4, 5])."""
    # TODO: Implement full logic
    return arr  # Placeholder


def has_one_set_of_numbers(string, chapter=False, file=None, subtitle=None):
    """Checks if a string contains exactly one set of volume/chapter numbers."""
    # TODO: Implement full logic using complex regexes
    return (
        len(extract_all_numbers(string, subtitle=subtitle)) == 1
    )  # Placeholder using another function


@lru_cache(maxsize=3500)
def has_multiple_numbers(file_name):
    """Checks if a filename contains more than one distinct number group."""
    # Simplified check
    return len(re.findall(r"\b\d+(?:[._]\d+)?\b", file_name)) > 1


def extract_all_numbers(string, subtitle=None):
    """Extracts all distinct number groups from a string."""
    # TODO: Implement full logic using exclusion_keywords_regex and number cleaning
    # Placeholder using simpler regex
    if subtitle:
        string = remove_dual_space(
            re.sub(rf"(-|:)\s*{re.escape(subtitle)}", "", string, re.IGNORECASE).strip()
        )  # Use local function
    string = (
        replace_underscores(string) if "_" in string else string
    )  # Use local function
    numbers_found = re.findall(r"\b\d+(?:[._]\d+)?\b", string)
    processed_numbers = []
    for num_str in numbers_found:
        num = set_num_as_float_or_int(
            num_str, silent=True
        )  # Use imported misc_utils function
        if num != "":
            processed_numbers.append(num)
    return processed_numbers


# --- Name/Subtitle/Extras Extraction ---

# get_series_name_from_contents, get_series_name_from_volume, get_series_name_from_chapter,
# chapter_file_name_cleaning, get_extra_from_group, get_extras, get_shortened_title,
# get_subtitle_from_dash, get_subtitle_from_title, get_release_year, get_publisher_from_meta,
# get_identifiers, get_series_name are complex. Including placeholders or simplified versions.


@lru_cache(maxsize=3500)
def get_series_name_from_volume(name, root, test_mode=False, second=False):
    """Extracts the series name from a volume filename."""
    # TODO: Implement full logic with fallbacks and folder name check
    cleaned_name = re.sub(
        r"(\b|\s)(?<![A-Za-z])((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(%s)(\.|)([-_. ]|)([0-9]+)(\b|\s).*$"
        % volume_regex_keywords,  # Use imported config value
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    cleaned_name = re.sub(
        r"(%s)$" % file_extensions_regex, "", cleaned_name
    ).strip()  # Use imported config value
    cleaned_name = remove_brackets(cleaned_name)  # Use local function
    return cleaned_name.strip()


@lru_cache(maxsize=3500)
def chapter_file_name_cleaning(
    file_name, chapter_number="", skip=False, regex_matched=False
):
    """Cleans chapter filenames to isolate the series name."""
    # TODO: Implement full cleaning logic based on original function
    return file_name  # Placeholder


@lru_cache(maxsize=3500)
def get_series_name_from_chapter(name, root, chapter_number="", second=False):
    """Extracts the series name from a chapter filename."""
    # TODO: Implement full logic using chapter_search_patterns_comp and chapter_file_name_cleaning
    name = get_extensionless_name(name)  # Use imported file_utils function
    # Basic removal of potential chapter number/keyword at the end
    name = re.sub(
        r"\s+c(h(ap)?)?(\.)?\s*\d+([._]\d+)?\s*$", "", name, flags=re.IGNORECASE
    ).strip()
    name = re.sub(r"\s+\d+([._]\d+)?\s*$", "", name).strip()  # Remove trailing number
    return remove_brackets(name).strip()  # Use local function


def get_series_name_from_contents(
    folder_name, file_names, required_matching_percent=100
):
    """Derives series name by finding common prefix between folder and filenames."""
    # TODO: Implement full logic
    return folder_name  # Placeholder


@lru_cache(maxsize=3500)
def get_extra_from_group(
    name, groups, publisher_m=False, release_group_m=False, series_name=None
):
    """Extracts publisher or release group from brackets in filename."""
    # TODO: Implement full logic using compiled regexes from config
    return ""  # Placeholder


def get_extras(file_name, chapter=False, series_name="", subtitle=""):
    """Extracts bracketed 'extra' information like (Digital), [Yen Press]."""
    # TODO: Implement full logic excluding year, premium, part etc.
    all_brackets = re.findall(r"(\[[^\]]+\]|\([^)]+\)|\{[^}]+\})", file_name)
    # Simple filter example (needs refinement)
    extras = [
        b for b in all_brackets if not re.match(r"^\(?\d{4}\)?$", b.strip("()[]{} "))
    ]
    return remove_duplicates(extras)  # Use local function


@lru_cache(maxsize=3500)
def get_shortened_title(title):
    """Returns the part of the title before a ' - ' or ': ' separator."""
    match = re.search(r"(.*?)(\s+(-|:)\s+)", title)
    return match.group(1).strip() if match else ""  # Return full title if no separator


@lru_cache(maxsize=3500)
def get_subtitle_from_dash(title, replace=False):
    """Extracts the subtitle part after ' - ' or ': '."""
    match = re.search(r"(?:\s+(-|:)\s+)(.*)", title)
    if match:
        subtitle = match.group(2).strip()
        return title[: match.start()].strip() if replace else subtitle
    return "" if not replace else title  # Return empty or full title if no separator


@lru_cache(maxsize=3500)
def get_subtitle_from_title(file, publisher=None):  # Expects Volume object
    """
    Extracts the subtitle from a file.name based on various patterns,
    often involving separators like ' - ' or ':' followed by content
    before year/digital markers or publisher info in brackets.
    (year required in brackets at the end of the subtitle)
    EX: Sword Art Online v13 - Alicization Dividing [2018].epub -> Alicization Dividing
    """
    subtitle = ""

    # remove the series name from the title to isolate potential subtitle part
    # Use re.escape to handle special characters in series name
    try:
        if file.series_name:  # Ensure series_name exists
            without_series_name = re.sub(
                rf"^{re.escape(file.series_name)}", "", file.name, flags=re.IGNORECASE
            ).strip()
        else:
            without_series_name = file.name  # Use full name if series_name is missing
    except re.error as escape_error:
        # Handle potential errors if series_name contains invalid regex chars even after escape
        send_message(
            f"Regex error escaping series name '{file.series_name}': {escape_error}",
            error=True,
        )  # Use imported log_utils
        without_series_name = file.name  # Fallback to full name

    # First Search: Look for content after ' - ' or ': '
    dash_or_colon_search = get_subtitle_from_dash(
        without_series_name
    )  # Use local function

    # Second Search: Look for year (e.g., (2023)) or (Digital) in brackets
    year_or_digital_search = re.search(
        r"([\[\{\(]((\d{4})|(Digital))[\]\}\)])",
        without_series_name,
        re.IGNORECASE,
    )

    # Third Search: Look for publisher in brackets if year/digital wasn't found
    publisher_search = None
    if (
        publisher
        and (publisher.from_meta or publisher.from_name)
        and not year_or_digital_search
    ):
        pub_pattern = ""
        if publisher.from_meta:
            try:
                pub_pattern = re.escape(publisher.from_meta)
                publisher_search = re.search(
                    rf"[\[\{{\(]]({pub_pattern})[\]\}}\)]",
                    without_series_name,
                    re.IGNORECASE,
                )
            except re.error:
                publisher_search = None  # Handle regex error
        if publisher.from_name and not publisher_search:
            try:
                pub_pattern = re.escape(publisher.from_name)
                publisher_search = re.search(
                    rf"[\[\{{\(]]({pub_pattern})[\]\}}\)]",
                    without_series_name,
                    re.IGNORECASE,
                )
            except re.error:
                publisher_search = None  # Handle regex error

    if dash_or_colon_search and (year_or_digital_search or publisher_search):
        # Extract potential subtitle (part after ' - ' or ': ')
        # Use count=1 to only remove the first occurrence after the series name
        subtitle = re.sub(r"(.*?)(\s+(-|:)\s+)", "", without_series_name, count=1)

        # remove the file extension
        if file.extension and subtitle.endswith(
            file.extension
        ):  # Check if extension exists
            subtitle = get_extensionless_name(
                subtitle
            )  # Use imported file_utils function

        # Remove trailing metadata (year/digital or publisher)
        if publisher_search:
            # Remove from the publisher bracket onwards
            try:
                pub_pattern_found = publisher_search.group(
                    1
                )  # The actual matched publisher name
                # Ensure pattern exists before trying to remove
                if pub_pattern_found:
                    subtitle = re.sub(
                        rf"\s*[\[\{{\(]{re.escape(pub_pattern_found)}[\]\}}\)].*",
                        "",
                        subtitle,
                        flags=re.IGNORECASE,
                    ).strip()
            except (re.error, AttributeError):
                pass  # Ignore regex error or if group doesn't exist
        elif year_or_digital_search:
            # Remove from the year/digital bracket onwards
            subtitle = re.sub(
                r"\s*[\[\{\(](?:\d{4}|Digital)[\]\}\)].*",
                "",
                subtitle,
                flags=re.IGNORECASE,
            ).strip()

        # remove any extra spaces
        subtitle = remove_dual_space(subtitle).strip()  # Use local function

        # Final checks:
        # - Ensure subtitle isn't just the volume keyword + number
        # - Ensure subtitle isn't part of the folder name (heuristic to avoid grabbing series parts)
        try:
            folder_basename = os.path.basename(os.path.dirname(file.path))
            # Check if subtitle is non-empty before regex search
            if (
                subtitle
                and folder_basename
                and re.search(
                    rf"\b{re.escape(subtitle)}\b", folder_basename, re.IGNORECASE
                )
            ):
                subtitle = ""  # Likely part of series name if in folder name
        except Exception:
            pass  # Ignore errors in path manipulation

        if file.volume_number and subtitle:
            # Check if subtitle is just like "v01" or "Volume 1"
            vol_num_str = str(file.volume_number)
            # Basic check, might need refinement for float/range numbers
            # Ensure volume_regex_keywords is not empty before using it
            if volume_regex_keywords and re.search(
                rf"^({volume_regex_keywords})(\s+)?(0+)?{re.escape(vol_num_str)}$",
                subtitle.strip(),
                re.IGNORECASE,
            ):  # Use imported config value
                subtitle = ""

    return subtitle


def get_release_year(name, metadata=None):
    """Extracts the release year from filename or metadata."""
    # TODO: Implement full logic including metadata check
    match = re.search(
        volume_year_regex, name, re.IGNORECASE
    )  # Use imported config value
    if match:
        year_str = re.sub(r"[\(\)\[\]{}]", "", match.group())
        if year_str.isdigit():
            return int(year_str)
    return None


def get_publisher_from_meta(metadata):
    """Extracts and cleans publisher name from metadata dictionary."""
    # TODO: Needs metadata_utils.parse_comicinfo_xml / parse_html_tags
    publisher = None
    if metadata:
        if "Publisher" in metadata:
            publisher = metadata["Publisher"]
        elif "dc:publisher" in metadata:
            publisher = metadata["dc:publisher"]
    if publisher:
        publisher = titlecase(publisher)  # Use imported titlecase
        publisher = remove_dual_space(publisher)  # Use local function
        publisher = re.sub(r",?\s+LLC.*", "", publisher, flags=re.IGNORECASE).strip()
        publisher = publisher.replace(":", " - ").strip()
        publisher = remove_dual_space(publisher)  # Use local function
    return publisher


def get_identifiers(zip_comment):
    """Extracts identifiers (like ISBN) from a zip comment string."""
    # TODO: Depends on get_zip_comment from archive_utils
    metadata = []
    if zip_comment and "Identifiers:" in zip_comment:
        try:
            identifiers_part = zip_comment.split("Identifiers:")[1].strip()
            metadata = [
                id_str.strip()
                for id_str in identifiers_part.split(",")
                if id_str.strip()
            ]
        except IndexError:
            pass  # No identifiers found after the keyword
    return metadata


def get_series_name(dir_name):
    """Original, simpler method to clean a directory name for series identification."""
    # TODO: Evaluate if this is still needed or covered by get_series_name_from_volume
    dir_name = remove_dual_space(
        dir_name.replace("_extra", ".5")
    ).strip()  # Use local function
    dir_name = re.sub(
        r"(\b|\s)((\s|)-(\s|)|)(Part|)(%s)([-_. ]|)([-_. ]|)([0-9]+)(\b|\s).*$"
        % volume_regex_keywords,
        "",
        dir_name,
        flags=re.IGNORECASE,
    ).strip()  # Use imported config value
    dir_name = re.sub(
        r"(\([^()]*\))|(\[[^\[\]]*\])|(\{[^\{\}]*\})", "", dir_name
    ).strip()
    dir_name = re.sub(r"[\(\)\[\]{}]", "", dir_name, flags=re.IGNORECASE).strip()
    return dir_name


# --- Comparison ---


@lru_cache(maxsize=3500)
def similar(a, b):
    """Calculates the similarity ratio between two strings."""
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return 0.0
    # Use SequenceMatcher for similarity calculation
    return SequenceMatcher(None, a, b).ratio()


# --- List/Array Helpers ---


def array_to_string(array, separator=", "):
    """Converts a list/tuple or single item to a string."""
    if isinstance(array, (list, tuple)):
        return separator.join([str(x) for x in array])
    return str(array)


# --- Word/Token Processing ---


@lru_cache(maxsize=3500)
def parse_words(user_string):
    """Parses a string into a list of lowercase words without punctuation."""
    words = []
    if user_string:
        try:
            # Keep hyphens as they can be part of words/names
            translator = str.maketrans("", "", string.punctuation.replace("-", ""))
            words_no_punct = user_string.translate(translator)
            words_lower = words_no_punct.lower()
            # Handle unicode conversion safely
            try:
                words_no_uni = (
                    unidecode(words_lower)
                    if contains_unicode(words_lower)
                    else words_lower
                )  # Use local function
            except Exception:
                words_no_uni = words_lower  # Fallback if unidecode fails
            words = words_no_uni.split()
        except Exception as e:
            send_message(
                f"parse_words(string='{user_string}') - Error: {e}", error=True
            )  # Use imported log_utils function
    return words


@lru_cache(maxsize=3500)
def find_consecutive_items(arr1, arr2, count=3):
    """Checks if two tuples/lists share a sequence of 'count' consecutive items."""
    # Ensure inputs are tuples for hashing in set
    tup1 = tuple(arr1)
    tup2 = tuple(arr2)
    if len(tup1) < count or len(tup2) < count:
        return False
    set2 = set(tup2[j : j + count] for j in range(len(tup2) - count + 1))
    for i in range(len(tup1) - count + 1):
        if tup1[i : i + count] in set2:
            return True
    return False


def count_words(strings_list):
    """Counts word occurrences in a list of strings."""
    word_count = {}
    for s in strings_list:
        words = parse_words(s)  # Use local function
        for word in words:
            word_count[word] = word_count.get(word, 0) + 1
    return word_count


def move_strings_to_top(target_item, item_array):
    """Moves items matching the first few words of target_item to the top."""
    # TODO: This seems more like list manipulation, maybe misc_utils?
    target_words = parse_words(target_item)  # Use local function
    if not target_words:
        return item_array

    prefix_len = min(3, len(target_words))
    target_prefix = tuple(target_words[:prefix_len])

    items_to_move = []
    remaining_items = []
    moved_set = set()

    for item in item_array:
        try:
            item_words = parse_words(os.path.basename(item))  # Use local function
            item_prefix = tuple(item_words[:prefix_len])
            if item_prefix == target_prefix and item not in moved_set:
                items_to_move.append(item)
                moved_set.add(item)
            elif item not in moved_set:
                remaining_items.append(item)
        except Exception as e:
            # Handle potential errors during basename or parsing
            send_message(
                f"Error processing item '{item}' in move_strings_to_top: {e}",
                error=True,
            )  # Use imported log_utils function
            if (
                item not in moved_set
            ):  # Ensure item is added to remaining if error occurs
                remaining_items.append(item)

    if not items_to_move:
        return item_array  # Return original if nothing matched

    # Simple sort: exact match first, then others
    clean_target = clean_str(target_item)  # Use local function
    items_to_move.sort(
        key=lambda x: clean_str(os.path.basename(x)) != clean_target
    )  # Use local function

    return items_to_move + remaining_items


# Note: reorganize_and_rename function moved to file_operations.py
