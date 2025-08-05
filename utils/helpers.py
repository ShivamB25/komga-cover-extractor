import os
import re
from functools import lru_cache

from unidecode import unidecode

from config.constants import ignored_folder_names
from processing.text_processor import contains_unicode


# Converts the passed volume_number into a float or an int.
def set_num_as_float_or_int(volume_number, silent=False):
    if volume_number == "":
        return ""

    try:
        if isinstance(volume_number, list):
            result = "-".join(
                [
                    (
                        str(int(float(num)))
                        if float(num) == int(float(num))
                        else str(float(num))
                    )
                    for num in volume_number
                ]
            )
            return result
        elif isinstance(volume_number, str) and "." in volume_number:
            volume_number = float(volume_number)
        else:
            if float(volume_number) == int(volume_number):
                volume_number = int(volume_number)
            else:
                volume_number = float(volume_number)
    except Exception as e:
        if not silent:
            print(
                f"Failed to convert volume number to float or int: {volume_number}\nERROR: {e}",
            )
        return ""
    return volume_number


# Check the text file line by line for the passed message
def check_text_file_for_message(text_file, message):
    # Open the file in read mode using a context manager
    with open(text_file, "r") as f:
        # Check if any line in the file matches the message
        return any(line.strip() == message.strip() for line in f)


# Removes hidden files
def remove_hidden_files(files):
    return [x for x in files if not x.startswith(".")]


# Removes any unaccepted file types
def remove_unaccepted_file_types(files, root, accepted_extensions, test_mode=False):
    return [
        file
        for file in files
        if get_file_extension(file) in accepted_extensions
        and (os.path.isfile(os.path.join(root, file)) or test_mode)
    ]


# Removes any folder names in the ignored_folder_names
def remove_ignored_folders(dirs):
    return [x for x in dirs if x not in ignored_folder_names]


# Remove hidden folders from the list
def remove_hidden_folders(dirs):
    return [x for x in dirs if not x.startswith(".")]


# Determines if the string starts with a bracket
def starts_with_bracket(s):
    return s.startswith(("(", "[", "{"))


# Determines if the string ends with a bracket
def ends_with_bracket(s):
    return s.endswith((")", "]", "}"))


# Pre-compile the bracket pattern
brackets_pattern = re.compile(r"[(){}\[\]]")


# Determines if the string contains brackets
def contains_brackets(s):
    return bool(brackets_pattern.search(s))


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


# Retrieves the file extension on the passed file
def get_file_extension(file):
    return os.path.splitext(file)


# Returns an extensionless name
def get_extensionless_name(file):
    return os.path.splitext(file)


# Sets the modification date of the passed file path to the passed date.
def set_modification_date(file_path, date):
    try:
        os.utime(file_path, (get_modification_date(file_path), date))
    except Exception as e:
        print(
            f"ERROR: Could not set modification date of {file_path}\nERROR: {e}",
        )


# Determies if two index_numbers are the same
def is_same_index_number(index_one, index_two, allow_array_match=False):
    if (index_one == index_two and index_one != "") or (
        allow_array_match
        and (
            (isinstance(index_one, list) and index_two in index_one)
            or (isinstance(index_two, list) and index_one in index_two)
        )
    ):
        return True
    return False


# Pre-compile dual space removal
dual_space_pattern = re.compile(r"(\s{2,})")


# Replaces any pesky double spaces
@lru_cache(maxsize=3500)
def remove_dual_space(s):
    if "  " not in s:
        return s

    return dual_space_pattern.sub(" ", s)


# Removes common words to improve string matching accuracy between a series_name
# from a file name, and a folder name, useful for when releasers sometimes include them,
# and sometimes don't.
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
    if len(s) <= 1:
        return s

    words_to_remove = []

    if not skip_common_words:
        common_words = [
            "the",
            "a",
            "à",
            "and",
            "&",
            "I",
            "of",
        ]
        words_to_remove.extend(common_words)

    if not skip_editions:
        editions = [
            "Collection",
            "Master Edition",
            "(2|3|4|5)-in-1 Edition",
            "Edition",
            "Exclusive",
            "Anniversary",
            "Deluxe",
            # "Omnibus",
            "Digital",
            "Official",
            "Anthology",
            "Limited",
            "Complete",
            "Collector",
            "Ultimate",
            "Special",
        ]
        words_to_remove.extend(editions)

    if not skip_type_keywords:
        # (?<!^) = Cannot start with this word.
        # EX: "Book Girl" light novel series.
        type_keywords = [
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
        words_to_remove.extend(type_keywords)

    if not skip_japanese_particles:
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
        words_to_remove.extend(japanese_particles)

    if not skip_misc_words:
        misc_words = [r"((\d+)([-_. ]+)?th)", "x", "×", "HD"]
        words_to_remove.extend(misc_words)

    if not skip_storefront_keywords:
        storefront_keywords = [
            r"Book(\s+)?walker",
        ]
        words_to_remove.extend(storefront_keywords)

    for word in words_to_remove:
        pattern = rf"\b{word}\b" if word not in type_keywords else rf"{word}\s"
        s = re.sub(pattern, " ", s, flags=re.IGNORECASE).strip()

        s = remove_dual_space(s)

    return s.strip()


# Removes the s from any words that end in s
@lru_cache(maxsize=3500)
def remove_s(s):
    return re.sub(r"\b(\w+)(s)\b", r"\1", s, flags=re.IGNORECASE).strip()


# Precompiled
punctuation_pattern = re.compile(r"[^\w\s+]")


# Determines if the string contains punctuation
def contains_punctuation(s):
    return bool(punctuation_pattern.search(s))


# Returns a string without punctuation.
@lru_cache(maxsize=3500)
def remove_punctuation(s):
    return re.sub(r"[^\w\s+]", " ", s).strip()


# Cleans the string by removing punctuation, bracketed info, and replacing underscores with periods.
# Converts the string to lowercase and removes leading/trailing whitespace.
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
    # Convert to lower and strip
    s = string.lower().strip() if not skip_lowercase_convert else string

    # replace : with space
    s = s.replace(":", " ") if not skip_colon_replace and ":" in s else s

    # remove uneccessary spacing
    s = remove_dual_space(s)

    # Remove bracketed info
    s = remove_brackets(s) if not skip_bracket and contains_brackets(s) else s

    # Remove unicode
    s = unidecode(s) if not skip_unidecode and contains_unicode(s) else s

    # normalize the string
    s = normalize_str(s) if not skip_normalize else s

    # Remove punctuation
    s = remove_punctuation(s) if not skip_punctuation and contains_punctuation(s) else s

    # remove trailing s
    s = remove_s(s) if not skip_remove_s else s

    # remove dual spaces
    s = remove_dual_space(s)

    # convert to ascii
    s = convert_to_ascii(s) if not skip_convert_to_ascii else s

    # Replace underscores with periods
    s = replace_underscores(s) if not skip_underscore and "_" in s else s

    return s.strip()


# convert string to acsii
@lru_cache(maxsize=3500)
def convert_to_ascii(s):
    return "".join(i for i in s if ord(i) < 128)


# convert array to string separated by whatever is passed in the separator parameter
def array_to_string(array, separator=", "):
    if isinstance(array, list):
        return separator.join([str(x) for x in array])
    elif isinstance(array, (int, float, str)):
        return separator.join([str(array)])
    else:
        return str(array)


# Converts an array to a string seperated array with subsequent whole numbers abbreviated.
def abbreviate_numbers(numbers):
    result = []
    temp_range = []

    for i in range(len(numbers)):
        # Check if the current number is an integer and if it's part of a sequential range
        if i == 0 or (
            isinstance(numbers[i], int)
            and isinstance(numbers[i - 1], int)
            and numbers[i] == numbers[i - 1] + 1
        ):
            temp_range.append(numbers[i])
        else:
            # Handle ranges and individual numbers
            if len(temp_range) > 1:
                result.append(f"{temp_range}-{temp_range[-1]}")
            else:
                result.extend(map(str, temp_range))
            temp_range = [numbers[i]]

    # Handle the last part
    if len(temp_range) > 1:
        result.append(f"{temp_range}-{temp_range[-1]}")
    else:
        result.extend(map(str, temp_range))

    return ", ".join(result)


# Fills in missing whole nubmers between
# the lowest and the highest number.
def complete_num_array(arr):
    if not arr:  # Handle empty arrays
        return arr

    # Find the minimum and maximum numbers in the array
    min_num = int(min(arr))
    max_num = int(max(arr))

    # Generate a complete range of whole numbers between min and max
    complete_arr = list(range(min_num, max_num + 1))

    return complete_arr


# remove duplicates elements from the passed in list
def remove_duplicates(items):
    return list(dict.fromkeys(items))


# Regex out underscore from passed string and return it
@lru_cache(maxsize=3500)
def replace_underscores(name):
    # Replace underscores that are preceded and followed by a number with a period
    name = re.sub(r"(?<=\d)_(?=\d)", ".", name)

    # Replace all other underscores with a space
    name = name.replace("_", " ")
    name = remove_dual_space(name).strip()

    return name


# Check if the input value can be converted to a float
def isfloat(x):
    try:
        a = float(x)
    except (TypeError, ValueError):
        return False
    else:
        return True


# Check if the input value can be converted to an integer
def isint(x):
    try:
        a = float(x)
        b = int(a)
    except (TypeError, ValueError):
        return False
    else:
        return a == b


# Retrieves the modification date of the passed file path.
def get_modification_date(path):
    return os.path.getmtime(path)


# Normalize path separators and remove Windows drive letters if present.
def normalize_path(path):
    path = os.path.normpath(path)

    # Remove Windows drive letters (e.g., "Z:\example\path" -> "\example\path")
    if ":" in path:
        path = re.sub(r"^[A-Za-z]:", "", path)

    # Convert backslashes to forward slashes for uniform comparison
    return path.replace("\\", "/")


# Check if root_path is a prefix of target_path, handling Windows and Linux paths.
def is_root_present(root_path, target_path):
    root_path = normalize_path(root_path)
    target_path = normalize_path(target_path)

    return root_path in target_path