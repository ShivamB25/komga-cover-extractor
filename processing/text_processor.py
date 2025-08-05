import re
from functools import lru_cache
import os
import string
from unidecode import unidecode

from processing.file_classifier import (
    is_one_shot,
    check_for_multi_volume_file,
    contains_keyword,
)
from utils.helpers import (
    get_release_number_cache,
    has_multiple_numbers,
    contains_non_numeric,
    get_min_and_max_numbers,
    send_message,
    remove_dual_space,
    replace_underscores,
    get_extensionless_name,
    starts_with_bracket,
    ends_with_bracket,
    get_file_extension,
    contains_unicode,
)
from config.constants import (
    file_extensions_regex,
    volume_regex_keywords,
    chapter_search_patterns_comp,
    chapter_regex_keywords,
    download_folders,
    paths,
    exclusion_keywords_joined,
)


# Determines if the string contains brackets
def contains_brackets(s):
    return bool(re.search(r"[(){}\[\]]", s))


# Pre-combiled remove_brackets() patterns
bracket_removal_pattern = re.compile(
    r"((((?<!-|[A-Za-z]\s|\[)(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})(?!-|\s*[A-Za-z]|\]))(\s+)?)+|([\[\{\(]((\d{4}))[\]\}\)]))",
    re.IGNORECASE,
)
bracket_avoidance_pattern = re.compile(r"^[\(\[\{].*[\)\]\}]$")
bracket_against_extension_pattern = re.compile(
    r"(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})(\.\w+$)"
)


# Removes bracketed content from the string, alongwith any whitespace.
# As long as the bracketed content is not immediately preceded or followed by a dash.
@lru_cache(maxsize=3500)
def remove_brackets(string):
    # Avoid a string that is only a bracket
    # Probably a series name
    # EX: [(OSHI NO KO)]
    if (
        starts_with_bracket(string)
        and ends_with_bracket(string)
        and bracket_avoidance_pattern.search(string)
    ):
        return string

    # Remove all grouped brackets as long as they aren't surrounded by dashes,
    # letters, or square brackets.
    # Regex 1: ([\[\{\(]((\d{4}))[\]\}\)]) - FOR YEAR
    # Regex 2: (((?<!-|[A-Za-z]\s|\[)(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})(?!-|\s*[A-Za-z]|\]))(\s+)?)+ - FOR EVERYTHING ELSE
    string = bracket_removal_pattern.sub("", string).strip()

    # Get file extension
    ext = get_file_extension(string)

    if ext:
        # Remove ending bracket against the extension
        # EX: test (digital).cbz -> test .cbz
        string = (
            bracket_against_extension_pattern.sub(r"\2", string).strip()
            if contains_brackets(string)
            else string
        )

        # Remove the extension
        # EX: test.cbz -> test
        string = string.replace(ext, "").strip()

        # Re-add the extension
        # EX: test -> test.cbz
        string = f"{string}{ext}"

    # Return the modified string
    return string


# Retrieves the series name through various regexes
# Removes the volume number and anything to the right of it, and strips it.
@lru_cache(maxsize=3500)
def get_series_name_from_volume(name, root, test_mode=False, second=False):
    # Remove starting brackets
    # EX: "[WN] Series Name" -> "Series Name"
    if starts_with_bracket(name) and re.search(
        r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+(\s+[A-Za-z]{2})", name
    ):
        # remove the brackets only
        name = re.sub(r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+\s+", "", name).strip()

    # replace _extra
    name = remove_dual_space(name.replace("_extra", ".5")).strip()

    # Replace "- One-shot" after series name
    if "one" in name.lower() and "shot" in name.lower():
        name = re.sub(r"(-\s*)Ones?(-|)shot\s*", "", name, flags=re.IGNORECASE).strip()

    # replace underscores
    name = replace_underscores(name) if "_" in name else name

    # remove brackets
    # name = remove_brackets(name) if contains_brackets(name) else name

    if is_one_shot(name, root, test_mode=test_mode):
        name = re.sub(
            r"([-_ ]+|)(((\[|\(|\{).*(\]|\)|\}))|LN)([-_. ]+|)(%s|).*"
            % file_extensions_regex.replace(r"\.", ""),
            "",
            name,
            flags=re.IGNORECASE,
        ).strip()
    else:
        if re.search(
            r"(\b|\s)(?<![A-Za-z])((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(%s)(\.|)([-_. ]|)([0-9]+)(\b|\s).*"
            % volume_regex_keywords,
            name,
            flags=re.IGNORECASE,
        ):
            name = (
                re.sub(
                    r"(\b|\s)(?<![A-Za-z])((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(%s)(\.|)([-_. ]|)([0-9]+)(\b|\s).*"
                    % volume_regex_keywords,
                    "",
                    name,
                    flags=re.IGNORECASE,
                )
            ).strip()
        else:
            name = re.sub(
                r"(\d+)?([-_. ]+)?((\[|\(|\})(.*)(\]|\)|\}))?([-_. ]+)?(%s)$"
                % file_extensions_regex,
                "",
                name,
                flags=re.IGNORECASE,
            ).strip()

    # Remove a trailing comma at the end of the name
    if name.endswith(","):
        name = name[:-1].strip()

    # remove the file extension if still remaining
    name = re.sub(r"(%s)$" % file_extensions_regex, "", name).strip()

    # Remove "- Complete" from the end
    # "Series Name - Complete" -> "Series Name"
    # EX File: Series Name - Complete v01 [Premium] [Publisher].epub
    if name.lower().endswith("complete"):
        name = re.sub(r"(-|:)\s*Complete$", "", name, flags=re.IGNORECASE).strip()

    # Default to the root folder name if we have nothing left
    # As long as it's not in our download folders or paths
    if (
        not name
        and not second
        and root
        and (
            os.path.basename(root) not in str(download_folders) or not download_folders
        )
        and (os.path.basename(root) not in str(paths) or not paths)
        and not contains_keyword(os.path.basename(root))
    ):
        # Get the series namne from the root folder
        # EX: "Kindaichi 37-sai no Jikenbo -v01-v12-"" -> "Kindaichi 37-sai no Jikenbo"
        name = get_series_name_from_volume(
            os.path.basename(root), root, test_mode=test_mode, second=True
        )

        # Remove any brackets
        name = remove_brackets(name) if contains_brackets(name) else name

    return name


# Cleans the chapter file_name to retrieve the series_name
@lru_cache(maxsize=3500)
def chapter_file_name_cleaning(
    file_name, chapter_number="", skip=False, regex_matched=False
):
    # removes any brackets and their contents
    file_name = (
        remove_brackets(file_name) if contains_brackets(file_name) else file_name
    )

    # Remove any single brackets at the end of the file_name
    # EX: "Death Note - Bonus Chapter (" -> "Death Note - Bonus Chapter"
    file_name = re.sub(r"(\s(([\(\[\{])|([\)\]\}])))$", "", file_name).strip()

    # EX: "006.3 - One Piece" -> "One Piece"
    if regex_matched != 2:
        file_name = re.sub(
            r"(^([0-9]+)(([-_.])([0-9]+)|)+(\s+)?([-_]+)(\s+))", "", file_name
        ).strip()

    # Remove number and dash at the end
    # EX: "Series Name 54 -" -> "Series Name"
    if regex_matched != 0 and file_name.endswith("-"):
        file_name = re.sub(
            r"(#)?([0-9]+)([-_.][0-9]+)*((x|#)([0-9]+)([-_.][0-9]+)*)*\s*-$",
            "",
            file_name,
        ).strip()

    # Remove - at the end of the file_name
    # EX: " One Piece -" -> "One Piece"
    if file_name.endswith("-"):
        file_name = re.sub(r"(?<![A-Za-z])(-\s*)$", "", file_name).strip()

    # Return if we have nothing but a digit left, if not skip
    if file_name.replace("#", "").isdigit() and not skip:
        return ""
    elif file_name.replace("#", "").replace(".", "", 1).isdigit() and not skip:
        return ""

    # if chapter_number and it's at the end of the file_name, remove it
    # EX: "One Piece 001" -> "One Piece"
    if not regex_matched:
        if chapter_number != "" and re.search(
            r"-?(\s+)?((?<!({})(\s+)?)(\s+)?\b#?((0+)?({}|{}))#?$)".format(
                chapter_regex_keywords,
                chapter_number,
                chapter_number,
            ),
            file_name,
        ):
            file_name = re.sub(
                r"-?(\s+)?((?<!({})(\s+)?)(\s+)?\b#?((0+)?({}|{}))#?$)".format(
                    chapter_regex_keywords,
                    chapter_number,
                    chapter_number,
                ),
                "",
                file_name,
            ).strip()

    # Remove any season keywords
    if "s" in file_name.lower() and re.search(
        r"(Season|Sea| S)(\s+)?([0-9]+)$", file_name, re.IGNORECASE
    ):
        file_name = re.sub(
            r"(Season|Sea| S)(\s+)?([0-9]+)$", "", file_name, flags=re.IGNORECASE
        )

    # Remove any subtitle
    # EX: "Series Name 179.1 - Epilogue 01 (2023) (Digital) (release_group).cbz"
    # "179.1 - Epilogue 01" -> "179.1"
    if ("-" in file_name or ":" in file_name) and re.search(
        r"(^\d+)", file_name.strip()
    ):
        file_name = re.sub(r"((\s+(-)|:)\s+).*$", "", file_name, re.IGNORECASE).strip()

    return file_name


# Retrieves the series name from the file name and chapter number
def get_series_name_from_chapter(name, root, chapter_number="", second=False):
    # Remove starting brackets
    # EX: "[WN] Series Name" -> "Series Name"
    if starts_with_bracket(name) and re.search(
        r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+(\s+[A-Za-z]{2})", name
    ):
        # remove the brackets only
        name = re.sub(r"^(\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})+\s+", "", name).strip()

    # Replace _extra
    name = name.replace("_extra", ".5")

    # Replace "- One-shot" after series name
    if "one" in name.lower() and "shot" in name.lower():
        name = re.sub(r"(-\s*)Ones?(-|)shot\s*", "", name, flags=re.IGNORECASE).strip()

    # Remove dual space
    name = remove_dual_space(name).strip()

    # remove the file extension
    name = get_extensionless_name(name)

    # replace underscores
    name = replace_underscores(name) if "_" in name else name

    regex_matched = False
    search = next(
        (r for pattern in chapter_search_patterns_comp if (r := pattern.search(name))),
        None,
    )

    if search:
        regex_matched = True
        search = search.group()
        name = name.split(search).strip()

    result = ""

    if name:
        if isinstance(chapter_number, list):
            result = chapter_file_name_cleaning(
                name, chapter_number, regex_matched=regex_matched
            )
        else:
            result = chapter_file_name_cleaning(
                name, chapter_number, regex_matched=regex_matched
            )

    # Remove a trailing comma at the end of the name
    if result.endswith(","):
        result = result[:-1].strip()

    # Default to the root folder name if we have nothing left
    # As long as it's not in our download folders or paths
    if (
        not result
        and not second
        and root
        and os.path.basename(root) not in str(download_folders + paths)
        and not contains_keyword(os.path.basename(root), chapter=True)
    ):
        root_number = get_release_number_cache(os.path.basename(root))

        # Get series name
        result = get_series_name_from_chapter(
            os.path.basename(root),
            root,
            root_number if root_number else "",
            second=True,
        )

        # Remove any brackets
        result = remove_brackets(result) if contains_brackets(result) else result

    return result


# Finds the volume/chapter number(s) in the file name.
@lru_cache(maxsize=3500)
def get_release_number(file, chapter=False):
    # Cleans up the chapter's series name
    def clean_series_name(name):
        # Removes starting period
        # EX: "series_name. 031 (2023).cbz" -> "'. 031 (2023)"" -> "031 (2023)"
        if "." in name:
            name = re.sub(r"^\s*(\.)", "", name, re.IGNORECASE).strip()

        # Remove any subtitle
        # EX: "series_name 179.1 - Epilogue 01 (2023) (Digital) (release_group).cbz" ->
        # "" 179.1 - Epilogue 01"  -> "179.1"
        if ("-" in name or ":" in name) and re.search(r"(^\d+)", name.strip()):
            name = re.sub(r"((\s+(-)|:)\s+).*$", "", name, re.IGNORECASE).strip()

        # Removes # from the number
        # EX: #001 -> 001
        if "#" in name:
            name = re.sub(r"($#)", "", name, re.IGNORECASE).strip()

            # Removes # from bewteen the numbers
            # EX: 154#3 -> 154
            if re.search(r"(\d+#\d+)", name):
                name = re.sub(r"((#)([0-9]+)(([-_.])([0-9]+)|)+)", "", name).strip()

        # removes part from chapter number
        # EX: 053x1 or c053x1 -> 053 or c053
        if "x" in name:
            name = re.sub(r"(x[0-9]+)", "", name, re.IGNORECASE).strip()

        # removes the bracketed info from the end of the string, empty or not
        if contains_brackets(name):
            name = remove_brackets(name).strip()

        # Removes the - characters.extension from the end of the string, with
        # the dash and characters being optional
        # EX:  - prologue.extension or .extension
        name = re.sub(
            r"(((\s+)?-(\s+)?([A-Za-z]+))?(%s))" % file_extensions_regex,
            "",
            name,
            re.IGNORECASE,
        ).strip()

        if "-" in name:
            # - #404 - -> #404
            if name.startswith("- "):
                name = name[1:].strip()
            if name.endswith(" -"):
                name = name[:-1].strip()

        # remove # at the beginning of the string
        # EX: #001 -> 001
        if name.startswith("#"):
            name = name[1:].strip()

        return name

    results = []
    is_multi_volume = False
    keywords = volume_regex_keywords if not chapter else chapter_regex_keywords
    result = None

    # Replace _extra
    file = remove_dual_space(file.replace("_extra", ".5")).strip()

    # Replace underscores
    file = replace_underscores(file) if "_" in file else file

    is_multi_volume = (
        check_for_multi_volume_file(file, chapter=chapter) if "-" in file else False
    )

    if not chapter:  # Search for a volume number
        result = re.search(
            r"\b(?<![\[\(\{])(%s)((\.)|)(\s+)?([0-9]+)(([-_.])([0-9]+)|)+\b"
            % volume_regex_keywords,
            file,
            re.IGNORECASE,
        )
    else:  # Prep for a chapter search
        if has_multiple_numbers(file):
            extension_less_file = get_extensionless_name(file)

            if re.search(
                r"((%s)(\.)?(\s+)?(#)?(([0-9]+)(([-_.])([0-9]+)|)+))$"
                % exclusion_keywords_joined,
                extension_less_file,
                flags=re.IGNORECASE,
            ):
                file = re.sub(
                    r"((%s)(\.)?(\s+)?(#)?(([0-9]+)(([-_.])([0-9]+)|)+))$"
                    % exclusion_keywords_joined,
                    "",
                    extension_less_file,
                    flags=re.IGNORECASE,
                )

                # remove - at the end of the string
                if file.endswith("-") and not re.search(
                    r"-(\s+)?(#)?([0-9]+)(([-_.])([0-9]+)|)+(x[0-9]+)?(\s+)?-", file
                ):
                    file = file[:-1].strip()

        # Search for a chapter match
        result = next(
            (
                r
                for pattern in chapter_search_patterns_comp
                if (r := pattern.search(file))
            ),
            None,
        )

    if result:
        try:
            file = result.group().strip() if hasattr(result, "group") else ""

            # Clean the series name
            if chapter:
                file = clean_series_name(file)

            # Remove volume/chapter keywords from the file name
            if contains_non_numeric(file):
                file = re.sub(
                    r"\b({})(\.|)([-_. ])?".format(keywords),
                    "",
                    file,
                    flags=re.IGNORECASE,
                ).strip()

                if contains_non_numeric(file) and re.search(
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
                                int(results)
                                if float(results).is_integer()
                                else float(results)
                            )
                else:
                    # Remove trailing ".0" so conversion doesn't fail
                    if file.endswith("0") and ".0" in file:
                        file = file.split(".0")
                    results = int(file) if float(file).is_integer() else float(file)

            except ValueError as v:
                send_message(f"Not a float: {file}: ERROR: {v}", error=True)
        except AttributeError:
            send_message(str(AttributeError.with_traceback), error=True)

    if results or results == 0:
        if is_multi_volume:
            return tuple(results)
        elif chapter:
            return results
        elif results < 2000:
            return results

    return ""


# Parses the individual words from the passed string and returns them as an array
# without punctuation, unidecoded, and in lowercase.
@lru_cache(maxsize=3500)
def parse_words(user_string):
    words = []
    if user_string:
        try:
            translator = str.maketrans("", "", string.punctuation)
            words_no_punct = user_string.translate(translator)
            words_lower = words_no_punct.lower()
            words_no_uni = (
                unidecode(words_lower) if contains_unicode(words_lower) else words_lower
            )
            words_no_uni_split = words_lower.split()
            if words_no_uni_split:
                words = words_no_uni_split
        except Exception as e:
            send_message(f"parse_words(string={user_string}) - Error: {e}", error=True)
    return words