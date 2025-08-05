import re
from functools import lru_cache

import os

from utils.helpers import (
    get_file_extension,
    replace_underscores,
    remove_dual_space,
    starts_with_bracket,
    ends_with_bracket,
)
from filesystem.file_operations import clean_and_sort
from processing.text_processor import check_for_exception_keywords
from config.constants import (
    library_types,
    volume_regex_keywords,
    chapter_regex_keywords,
    chapter_search_patterns_comp,
    volume_year_regex,
    exception_keywords,
    download_folders,
)
from processing.text_processor import remove_brackets, contains_brackets


# Determines the files library type
def get_library_type(files, required_match_percentage=None):
    for library_type in library_types:
        match_count = 0
        for file in files:
            extension = get_file_extension(file)
            if (
                extension in library_type.extensions
                and all(
                    re.search(regex, file, re.IGNORECASE)
                    for regex in library_type.must_contain
                )
                and all(
                    not re.search(regex, file, re.IGNORECASE)
                    for regex in library_type.must_not_contain
                )
            ):
                match_count += 1

        match_percentage = required_match_percentage or library_type.match_percentage
        if match_count / len(files) * 100 >= match_percentage:
            return library_type
    return None


# check if volume file name is a chapter
@lru_cache(maxsize=3500)
def contains_chapter_keywords(file_name):
    # Replace "_extra"
    file_name_clean = file_name.replace("_extra", ".5")

    # Replace underscores
    file_name_clean = (
        replace_underscores(file_name_clean).strip()
        if "_" in file_name_clean
        else file_name_clean
    )

    # Remove dual spaces
    file_name_clean = remove_dual_space(file_name_clean).strip()

    # Use compiled patterns for searching
    found = False
    for pattern in chapter_search_patterns_comp:
        result = pattern.search(file_name_clean)
        if result:
            result = result.group()
            if not (
                starts_with_bracket(result)
                and ends_with_bracket(result)
                and re.search(r"^((\(|\{|\[)\d{4}(\]|\}|\)))$", result)
            ):
                found = True
                break

    if not found and not contains_volume_keywords(file_name):
        # Remove volume year
        without_year = re.sub(volume_year_regex, "", file_name, flags=re.IGNORECASE)

        # Remove any 2000-2999 numbers at the end
        without_year = re.sub(r"\b(?:2\d{3})\b$", "", without_year, flags=re.IGNORECASE)

        # Check for chapter numbers
        chapter_numbers_found = re.search(
            r"(?<!^)(?<!\d\.)\b([0-9]+)(([-_.])([0-9]+)|)+(x[0-9]+)?(#([0-9]+)(([-_.])([0-9]+)|)+)?(\.\d+)?\b",
            without_year,
        )
        if chapter_numbers_found:
            found = True

    return found


# Checks if the passed string contains volume keywords
@lru_cache(maxsize=3500)
def contains_volume_keywords(file):
    # Replace _extra
    file = file.replace("_extra", ".5")

    # Remove dual spaces
    file = remove_dual_space(file).strip()

    # Remove brackets
    clean_file = remove_brackets(file) if contains_brackets(file) else file

    # Replace underscores
    clean_file = (
        replace_underscores(clean_file).strip()
        if "_" in clean_file
        else clean_file.strip()
    )

    # Remove dual spaces
    clean_file = remove_dual_space(clean_file).strip()

    return bool(re.search(volume_regex_keywords, clean_file, re.IGNORECASE))


# Checks if the passed string is a volume one.
@lru_cache(maxsize=3500)
def is_volume_one(volume_name):
    keywords = volume_regex_keywords

    if contains_chapter_keywords(volume_name) and not contains_volume_keywords(
        volume_name
    ):
        keywords = chapter_regex_keywords + "|"

    if re.search(
        r"(\b(%s)([-_. ]|)(\s+)?(One|1|01|001|0001)(([-_.]([0-9]+))+)?\b)" % keywords,
        volume_name,
        re.IGNORECASE,
    ):
        return True

    return False


# Checks for volume keywords and chapter keywords.
# If neither are present, the volume is assumed to be a one-shot volume.
def is_one_shot(file_name, root=None, skip_folder_check=False, test_mode=False):
    files = []

    if test_mode:
        skip_folder_check = True

    if (
        contains_volume_keywords(file_name)
        or contains_chapter_keywords(file_name)
        or check_for_exception_keywords(file_name, exception_keywords)
    ):
        return False

    if not skip_folder_check:
        files = clean_and_sort(root, os.listdir(root))

    if (len(files) == 1 or skip_folder_check) or (
        download_folders and root in download_folders
    ):
        return True

    return False


# Determines if a volume file is a multi-volume file or not
# EX: TRUE == series_title v01-03.cbz
# EX: FALSE == series_title v01.cbz
@lru_cache(maxsize=3500)
def check_for_multi_volume_file(file_name, chapter=False):
    # Set the list of keywords to search for
    keywords = volume_regex_keywords if not chapter else chapter_regex_keywords + "|"

    # Search for a multi-volume or multi-chapter pattern in the file name, ignoring any bracketed information in the name
    if "-" in file_name and re.search(
        # Use regular expressions to search for the pattern of multiple volumes or chapters
        r"(\b({})(\.)?(\s+)?([0-9]+(\.[0-9]+)?)([-]([0-9]+(\.[0-9]+)?))+\b)".format(
            keywords
        ),
        remove_brackets(file_name) if contains_brackets(file_name) else file_name,
        re.IGNORECASE,  # Ignore case when searching
    ):
        # If the pattern is found, return True
        return True
    else:
        # If the pattern is not found, return False
        return False