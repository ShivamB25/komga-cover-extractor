import re
import string
from difflib import SequenceMatcher
from functools import lru_cache

from titlecase import titlecase
from unidecode import unidecode

from settings import ranked_keywords, exception_keywords, volume_regex_keywords, chapter_regex_keywords, file_extensions_regex, subtitle_exclusion_keywords_regex

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
            "no(?!\.)",
            "ne",
            "yo",
        ]
        words_to_remove.extend(japanese_particles)

    if not skip_misc_words:
        misc_words = ["((\d+)([-_. ]+)?th)", "x", "×", "HD"]
        words_to_remove.extend(misc_words)

    if not skip_storefront_keywords:
        storefront_keywords = [
            "Book(\s+)?walker",
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

# Regex out underscore from passed string and return it
@lru_cache(maxsize=3500)
def replace_underscores(name):
    # Replace underscores that are preceded and followed by a number with a period
    name = re.sub(r"(?<=\d)_(?=\d)", ".", name)

    # Replace all other underscores with a space
    name = name.replace("_", " ")
    name = remove_dual_space(name).strip()

    return name

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
                result.append(f"{temp_range[0]}-{temp_range[-1]}")
            else:
                result.extend(map(str, temp_range))
            temp_range = [numbers[i]]

    # Handle the last part
    if len(temp_range) > 1:
        result.append(f"{temp_range[0]}-{temp_range[-1]}")
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

# Gives the user a short version of the title, if a dash or colon is present.
# EX: Series Name - Subtitle -> Series Name
def get_shortened_title(title):
    shortened_title = ""
    if ("-" in title or ":" in title) and re.search(r"((\s+(-)|:)\s+)", title):
        shortened_title = re.sub(r"((\s+(-)|:)\s+.*)", "", title).strip()
    return shortened_title

# Extracts the subtitle from a title that contains a dash or colon.
# If replace is True, it removes the subtitle from the title.
# Example: get_subtitle_from_dash("Series Name - Subtitle", replace=True) -> "Series Name"
def get_subtitle_from_dash(title, replace=False):
    has_match = (
        re.search(r"((\s+(-)|:)\s+)", title) if ("-" in title or ":" in title) else None
    )
    if replace and has_match:
        return re.sub(r"(.*)((\s+(-)|:)\s+)", "", title)
    return has_match.group() if has_match else ""

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
        except Exception:
            pass
    return words

# Finds a number of consecutive items in both arrays, or returns False if none are found.
@lru_cache(maxsize=3500)
def find_consecutive_items(arr1, arr2, count=3):
    if len(arr1) < count or len(arr2) < count:
        return False

    for i in range(len(arr1) - count + 1):
        for j in range(len(arr2) - count + 1):
            if arr1[i : i + count] == arr2[j : j + count]:
                return True
    return False

# Counts the occurrence of each word in a list of strings.
def count_words(strings_list):
    word_count = {}

    for string in strings_list:
        # Remove punctuation and convert to lowercase
        words = parse_words(string)

        # Count the occurrence of each word
        for word in words:
            word_count[word] = word_count.get(word, 0) + 1

    return word_count

# Moves strings in item_array that match the first three words of target_item to the top of the array.
def move_strings_to_top(target_item, item_array):
    # Convert to lower and strip
    target_words = target_item.lower().strip()

    # Unidecode if applicable
    target_words = (
        unidecode(target_words) if contains_unicode(target_words) else target_words
    )

    # Parse into words
    target_words = parse_words(target_words)

    if not target_words:
        return item_array

    # Find items in item_array that match the first three words of target_item
    items_to_move = [
        item
        for item in item_array
        if parse_words(
            os.path.basename(
                unidecode(item).lower().strip()
                if contains_unicode(item)
                else item.lower().strip()
            )
        )[:3]
        == target_words[:3]
    ]

    if not items_to_move:
        return item_array

    clean_target_item = clean_str(target_item)

    # Sort items_to_move by the basename matching the target_item
    if len(items_to_move) >= 2:
        items_to_move = sorted(
            items_to_move,
            key=lambda x: clean_str(os.path.basename(x)) != clean_target_item,
        )

    # Insert items_to_move at the beginning of item_array
    item_array = items_to_move + item_array

    # Remove duplicates
    item_array = list(dict.fromkeys(item_array))

    return item_array

# Checks similarity between two strings.
@lru_cache(maxsize=3500)
def similar(a, b):
    # convert to lowercase and strip
    a = a.lower().strip()
    b = b.lower().strip()

    # evaluate
    if a == "" or b == "":
        return 0.0
    elif a == b:
        return 1.0
    else:
        return SequenceMatcher(None, a, b).ratio()

# Determines if the string contains unicode characters.
# or rather non-ascii characters.
def contains_unicode(input_str):
    return not input_str.isascii()

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
    ext = os.path.splitext(string)[1]

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

# Determines if the string starts with a bracket
def starts_with_bracket(s):
    return s.startswith(("(", "[", "{"))

# Determines if the string ends with a bracket
def ends_with_bracket(s):
    return s.endswith((")", "]", "}"))

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
    except Exception:
        if not silent:
            print(f"Failed to convert volume number to float or int: {volume_number}")
        return ""
    return volume_number

# Checks for any exception keywords that will prevent the chapter release from being deleted.
def check_for_exception_keywords(file_name, exception_keywords):
    pattern = "|".join(exception_keywords)
    return bool(re.search(pattern, file_name, re.IGNORECASE))