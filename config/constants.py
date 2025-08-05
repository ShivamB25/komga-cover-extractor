import os
import regex as re

# Get all the variables in settings.py
import settings as settings_file
from settings import *

# Version of the script
script_version = (2, 5, 31)
script_version_text = "v{}.{}.{}".format(*script_version)

# Paths = existing library
# Download_folders = newly acquired manga/novels
paths = []
download_folders = []

# paths within paths that were passed in with a defined path_type
# EX: "volume" or "chapter"
paths_with_types = []

# download folders within download_folders that were passed in with a defined path_type
download_folders_with_types = []

# global folder_accessor
folder_accessor = None

# To compress the extracted images
compress_image_option = False

# Default image compression value.
# Pass in via cli
image_quality = 40

# Stat-related variables
image_count = 0
errors = []
items_changed = []

# A discord webhook url used to send messages to discord about the changes made.
# Pass in via cli
discord_webhook_url = []

# Two webhooks specific to the bookwalker check.
# One is used for released books, the other is used for upcoming books.
# Intended to be sent to two seperate channels.
# FIRST WEBHOOK = released books
# SECOND WEBHOOK = upcoming books
bookwalker_webhook_urls = []

# Checks the library against bookwalker for new releases.
bookwalker_check = False

# All the release groups stored in release_groups.txt
# Used when renaming files where it has a matching group.
release_groups = []

# All the publishers stored in publishers.txt
# Used when renaming files where it has a matching publisher.
publishers = []

# skipped files that don't have a release group
skipped_release_group_files = []

# skipped files that don't have a publisher
skipped_publisher_files = []

# A quick and dirty fix to avoid non-processed files from
# being moved over to the existing library. Will be removed in the future.
processed_files = []

# Any files moved to the existing library. Used for triggering a library scan in komga.
moved_files = []

# The script's root directory
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# Where logs are written to.
LOGS_DIR = os.path.join(ROOT_DIR, "logs")

# Where the addon scripts are located.
ADDONS_DIR = os.path.join(ROOT_DIR, "addons")

# Docker Status
in_docker = False

# Check if the instance is running in docker.
# If the ROOT_DIR is /app, then it's running in docker.
if ROOT_DIR == "/app":
    in_docker = True
    script_version_text += " • Docker"

# The path location of the blank_white.jpg in the root of the script directory.
blank_white_image_path = (
    os.path.join(ROOT_DIR, "blank_white.jpg")
    if os.path.isfile(os.path.join(ROOT_DIR, "blank_white.jpg"))
    else None
)

blank_black_image_path = (
    os.path.join(ROOT_DIR, "blank_black.png")
    if os.path.isfile(os.path.join(ROOT_DIR, "blank_black.png"))
    else None
)

# Cached paths from the users existing library. Read from cached_paths.txt
cached_paths = []

cached_paths_path = os.path.join(LOGS_DIR, "cached_paths.txt")

# Cached identifier results, aka successful matches via series_id or isbn
cached_identifier_results = []

# watchdog toggle
watchdog_toggle = False

# 7zip extensions
seven_zip_extensions = [".7z"]

# Zip extensions
zip_extensions = [
    ".zip",
    ".cbz",
    ".epub",
]

# Rar extensions
rar_extensions = [".rar", ".cbr"]

# Accepted file extensions for novels
novel_extensions = [".epub"]

# Accepted file extensions for manga
manga_extensions = [x for x in zip_extensions if x not in novel_extensions]

# All the accepted file extensions
file_extensions = novel_extensions + manga_extensions

# All the accepted convertable file extensions for convert_to_cbz(),
# and the watchdog handler.
convertable_file_extensions = seven_zip_extensions + rar_extensions

# All the accepted image extensions
image_extensions = {".jpg", ".jpeg", ".png", ".tbn", ".webp"}

# Type of file formats for manga and novels
file_formats = ["chapter", "volume"]

# stores our folder path modification times
# used for skipping folders that haven't been modified
# when running extract_covers() with watchdog enabled
root_modification_times = {}

# Stores all the new series paths for series that were added to an existing library
moved_folders = []

# Profiles the execution - for dev use
profile_code = ""

# get all of the non-callable variables
settings = [
    var
    for var in dir(settings_file)
    if not callable(getattr(settings_file, var)) and not var.startswith("__")
]

# Libraries to be scanned after files have been moved over.
libraries_to_scan = []


# Library Type class
class LibraryType:
    def __init__(
        self, name, extensions, must_contain, must_not_contain, match_percentage=90
    ):
        self.name = name
        self.extensions = extensions
        self.must_contain = must_contain
        self.must_not_contain = must_not_contain
        self.match_percentage = match_percentage

    # Convert the object to a string representation
    def __str__(self):
        return f"LibraryType(name={self.name}, extensions={self.extensions}, must_contain={self.must_contain}, must_not_contain={self.must_not_contain}, match_percentage={self.match_percentage})"


# The Library Entertainment types
library_types = [
    LibraryType(
        "manga",  # name
        manga_extensions,  # extensions
        [r"\(Digital\)"],  # must_contain
        [
            r"Webtoon",
            r"^(?=.*Digital)((?=.*Compilation)|(?=.*danke-repack))",
        ],  # must_not_contain
        1,  # match_percentage - for classifying a group
    ),
    LibraryType(
        "light novel",  # name
        novel_extensions,  # extensions
        [
            r"\[[^\]]*(Lucaz|Stick|Oak|Yen (Press|On)|J-Novel|Seven Seas|Vertical|One Peace Books|Cross Infinite|Sol Press|Hanashi Media|Kodansha|Tentai Books|SB Creative|Hobby Japan|Impress Corporation|KADOKAWA|Viz Media)[^\]]*\]|(faratnis)"
        ],  # must_contain
        [],  # must_not_contain
    ),
    LibraryType(
        "digital_comps",  # name
        manga_extensions,  # extensions
        [r"^(?=.*Digital)((?=.*Compilation)|(?=.*danke-repack))"],  # must_contain
        [],  # must_not_contain
    ),
]


# The Translation Status source types for a library
translation_source_types = ["official", "fan", "raw"]

# The Library languages
source_languages = [
    "english",
    "japanese",
    "chinese",
    "korean",
]

# Volume Regex Keywords to be used throughout the script
# ORDER IS IMPORTANT, if a single character volume keyword is checked first, then that can break
# cleaning of various bits of input.
volume_keywords = [
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

# Chapter Regex Keywords used throughout the script
chapter_keywords = [
    "Chapters?",
    "Chaps?",
    "Chs?",
    "Cs?",
    "D",
]

# Keywords to be avoided in a chapter regex.
# Helps avoid picking the wrong chapter number
# when no chapter keyword was used before it.
exclusion_keywords = [
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
    # r"(\s)S(\s)",
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

subtitle_exclusion_keywords = [r"-(\s)", r"-", r"-\s[A-Za-z]+\s"]


# Volume Regex Keywords to be used throughout the script
volume_regex_keywords = "(?<![A-Za-z])" + "|(?<![A-Za-z])".join(volume_keywords)

# Exclusion keywords joined by just |
exclusion_keywords_joined = "|".join(exclusion_keywords)

# Subtitle exclusion keywords joined by just |
subtitle_exclusion_keywords_joined = "|".join(subtitle_exclusion_keywords)

# Put the exclusion_keywords_joined inside of (?<!%s)
exclusion_keywords_regex = r"(?<!%s)" % exclusion_keywords_joined

# Put the subtitle_exclusion_keywords_joined inside of (?<!%s)
subtitle_exclusion_keywords_regex = r"(?<!%s)" % subtitle_exclusion_keywords_joined

# Chapter Regex Keywords to be used throughout the script
chapter_regex_keywords = r"(?<![A-Za-z])" + (r"|(?<![A-Za-z])").join(chapter_keywords)

### EXTENION REGEX ###
# File extensions regex to be used throughout the script
file_extensions_regex = "|".join(file_extensions).replace(".", r"\.")
# Manga extensions regex to be used throughout the script
manga_extensions_regex = "|".join(manga_extensions).replace(".", r"\.")
# Novel extensions regex to be used throughout the script
novel_extensions_regex = "|".join(novel_extensions).replace(".", r"\.")
# Image extensions regex to be used throughout the script
image_extensions_regex = "|".join(image_extensions).replace(".", r"\.")

# REMINDER: ORDER IS IMPORTANT, Top to bottom is the order it will be checked in.
# Once a match is found, it will stop checking the rest.
# IMPORTANT: Any change of order or swapping of regexes, requires change in full_chapter_match_attempt_allowed alternative logic!
chapter_searches = [
    r"\b\s-\s*(#)?(\d+)([-_.]\d+)*(x\d+)?\s*-\s",
    r"\b(?<![\[\(\{])(%s)(\.)?\s*(\d+)([-_.]\d+)*(x\d+)?\b(?<!\s(\d+)([-_.]\d+)*(x\d+)?\s.*)"
    % chapter_regex_keywords,
    r"(?<![A-Za-z]|%s)(?<![\[\(\{])(((%s)([-_. ]+)?(\d+)([-_.]\d+)*(x\d+)?)|\s+(\d+)(\.\d+)?(x\d+((\.\d+)+)?)?(\s+|#\d+|%s))"
    % (exclusion_keywords_joined, chapter_regex_keywords, manga_extensions_regex),
    r"((?<!^)\b(\.)?\s*(%s)(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*\b)((\s+-|:)\s+).*?(?=\s*[\(\[\{](\d{4}|Digital)[\)\]\}])"
    % exclusion_keywords_regex,
    r"(\b(%s)?(\.)?\s*((%s)(\d{1,2})|\d{3,})([-_.]\d+)*(x\d+)?(#\d+([-_.]\d+)*)?\b)\s*((\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})|((?<!\w(\s))|(?<!\w))(%s)(?!\w))"
    % (chapter_regex_keywords, exclusion_keywords_regex, file_extensions_regex),
    r"^((#)?(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*)$",
]

# pre-compile the chapter_searches
chapter_search_patterns_comp = [
    re.compile(pattern, flags=re.IGNORECASE) for pattern in chapter_searches
]

# Used in check_for_existing_series() when sending
# a bulk amount of chapter release notifications to discord after the function is done,
# also allows them to be sent in number order.
messages_to_send = []

# Used to store multiple embeds to be sent in one message
grouped_notifications = []

# Discord's maximum amount of embeds that can be sent in one message
discord_embed_limit = 10

# The time to wait before performing the next action in
# the watchdog event handler.
sleep_timer = 10

# The time to wait before scraping another bookwalker page in
# the bookwalker_check feature.
sleep_timer_bk = 2

# The fill values for the chapter and volume files when renaming.
# # VOLUME
zfill_volume_int_value = 2  # 01
zfill_volume_float_value = 4  # 01.0
# # CHAPTER
zfill_chapter_int_value = 3  # 001
zfill_chapter_float_value = 5  # 001.0

# The Discord colors used for the embeds
purple_color = 7615723  # Starting Execution Notification
red_color = 16711680  # Removing File Notification
grey_color = 8421504  # Renaming, Reorganizing, Moving, Series Matching, and Bookwalker Release Notification
yellow_color = 16776960  # Not Upgradeable Notification
green_color = 65280  # Upgradeable and New Release Notification
preorder_blue_color = 5919485  # Bookwalker Preorder Notification

# The similarity score required for a publisher to be considered a match
publisher_similarity_score = 0.9

# Used to store the files and their associated dirs that have been marked as fully transferred
# When using watchdog, this is used to prevent the script from
# trying to process the same file multiple times.
transferred_files = []
transferred_dirs = []

# The logo url for usage in the bookwalker_check discord output
bookwalker_logo_url = "https://play-lh.googleusercontent.com/a7jUyjTxWrl_Kl1FkUSv2FHsSu3Swucpem2UIFDRbA1fmt5ywKBf-gcwe6_zalOqIR7V=w240-h480-rw"

# An alternative matching method that uses the image similarity between covers.
match_through_image_similarity = True

# The required score for two cover images to be considered a match
required_image_similarity_score = 0.9

# Checks the library against bookwalker for new releases.
bookwalker_check = False

# Used when moving the cover between locations.
series_cover_file_names = ["cover", "poster"]

# The required similarity score between the detected cover and the blank image to be considered a match.
# If the similarity score is equal to or greater than this value, the cover will be ignored as
# it is most likely a blank cover.
blank_cover_required_similarity_score = 0.9

# Prompts the user when deleting a lower-ranking duplicate volume when running
# check_for_duplicate_volumes()
manual_delete = False

# The required file type matching percentage between
# the download folder and the existing folder
#
# EX: 90% of the folder's files must have an extension in manga_extensions or novel_extensions
required_matching_percentage = 90

# The similarity score requirement when matching any bracketed release group
# within a file name. Used when rebuilding the file name in reorganize_and_rename.
release_group_similarity_score = 0.8

# searches for and copies an existing volume cover from a volume library over to the chapter library
copy_existing_volume_covers_toggle = False

# The percentage of words in the array of words,
# parsed from a shortened series_name to be kept
# for both series_names being compared.
# EX: 0.7= 70%
short_word_filter_percentage = 0.7

# The amount of time to sleep before checking again if all the files are fully transferred.
# Slower network response times may require a higher value.
watchdog_discover_new_files_check_interval = 5

# The time to sleep between file size checks when determining if a file is fully transferred.
# Slower network response times may require a higher value.
watchdog_file_transferred_check_interval = 1

# The libraries on the user's komga server.
# Used for sending scan reqeusts after files have been moved over.
komga_libraries = []

# Will move new series that couldn't be matched to the library to the appropriate library.
# requires: '--watchdog "True"' and check_for_existing_series_toggle = True
move_new_series_to_library_toggle = False

# Moves any series with a non-matching library type to the appropriate library
# requires: library_types
move_series_to_correct_library_toggle = False

# Used in get_extra_from_group()
publishers_joined = ""
release_groups_joined = ""

# Outputs the covers as WebP format
# instead of jpg format.
output_covers_as_webp = False

series_cover_path = ""

# The cutoff image count limit for a file to be
# considered a chapter.
average_chapter_image_count = 85

# The patterns used when finding a cover image
#
# REMINDER: ORDER IS IMPORTANT, Top to bottom is the order it will be checked in.
# Once a match is found, it will stop checking the rest.
cover_patterns = [
    r"(cover\.([A-Za-z]+))$",
    r"(\b(Cover([0-9]+|)|CoverDesign|page([-_. ]+)?cover)\b)",
    r"(\b(p000|page_000)\b)",
    r"((\s+)0+\.(.{2,}))",
    r"(\bindex[-_. ]1[-_. ]1\b)",
    r"(9([-_. :]+)?7([-_. :]+)?(8|9)(([-_. :]+)?[0-9]){10})",
]

# Pre-compiled regular expressions for cover patterns
compiled_cover_patterns = [
    re.compile(pattern, flags=re.IGNORECASE) for pattern in cover_patterns
]