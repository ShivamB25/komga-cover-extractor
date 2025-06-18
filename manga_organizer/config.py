"""Handles loading, validation, and access to configuration settings.

This module is responsible for parsing command-line arguments and integrating
them with settings from a configuration file.
"""

import argparse
import os
from typing import Any

# Get all the variables in settings.py
try:
    import settings as settings_file
    from settings import *
except ImportError:
    settings_file = None


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
settings = []
if settings_file:
    settings = [
        var
        for var in dir(settings_file)
        if not callable(getattr(settings_file, var)) and not var.startswith("__")
    ]

# Libraries to be scanned after files have been moved over.
libraries_to_scan = []


def parse_bool_argument(value: Any) -> bool:
    """Helper function to parse boolean arguments.

    Args:
        value: The value to parse.

    Returns:
        bool: The parsed boolean value.
    """
    return str(value).lower().strip() == "true"


def parse_my_args() -> argparse.Namespace:
    """Parses command-line arguments.

    Returns:
        argparse.Namespace: An object containing the parsed command-line arguments.
    """
    global paths
    global download_folders
    global discord_webhook_url
    global paths_with_types
    global komga_libraries
    global watchdog_toggle

    parser = argparse.ArgumentParser(
        description=f"Scans for and extracts covers from {', '.join(file_extensions)} files."
    )
    parser.add_argument(
        "-p",
        "--paths",
        help="The path/paths to be scanned for cover extraction.",
        action="append",
        nargs="*",
        required=False,
    )
    parser.add_argument(
        "-df",
        "--download_folders",
        help="The download folder/download folders for processing, renaming, and moving of downloaded files. (Optional, still in testing, requires manual uncommenting of optional method calls at the bottom of the script.)",
        action="append",
        nargs="*",
        required=False,
    )
    parser.add_argument(
        "-wh",
        "--webhook",
        action="append",
        nargs="*",
        help="The discord webhook url for notifications about changes and errors.",
        required=False,
    )
    parser.add_argument(
        "-bwc",
        "--bookwalker_check",
        help="Checks for new releases on bookwalker.",
        required=False,
    )
    parser.add_argument(
        "-c",
        "--compress",
        help="Compresses the extracted cover images.",
        required=False,
    )
    parser.add_argument(
        "-cq",
        "--compress_quality",
        help="The quality of the compressed cover images.",
        required=False,
    )
    parser.add_argument(
        "-bwk_whs",
        "--bookwalker_webhook_urls",
        help="The webhook urls for the bookwalker check.",
        action="append",
        nargs="*",
        required=False,
    )
    parser.add_argument(
        "-wd",
        "--watchdog",
        help="Uses the watchdog library to watch for file changes in the download folders.",
        required=False,
    )
    parser.add_argument(
        "-nw",
        "--new_volume_webhook",
        help="If passed in, the new volume release notification will be redirected to this single discord webhook channel.",
        required=False,
    )
    parser.add_argument(
        "-ltf",
        "--log_to_file",
        help="Whether or not to log the changes and errors to a file.",
        required=False,
    )
    parser.add_argument(
        "--watchdog_discover_new_files_check_interval",
        help="The amount of seconds to sleep before checking again if all the files are fully transferred.",
        required=False,
    )
    parser.add_argument(
        "--watchdog_file_transferred_check_interval",
        help="The seconds to sleep between file size checks when determining if a file is fully transferred.",
        required=False,
    )
    parser.add_argument(
        "--output_covers_as_webp",
        help="Outputs the covers as WebP format instead of jpg format.",
        required=False,
    )

    return parser.parse_args()


def load_settings() -> None:
    """Loads settings from the settings.py file and integrates with args.
    """
    # Print all the settings from settings.py
    print("\nExternal Settings:")

    # print all of the variables
    sensitive_keywords = ["password", "email", "_ip", "token", "user"]
    ignored_settings = ["ranked_keywords", "unacceptable_keywords"]

    for setting in settings:
        if setting in ignored_settings:
            continue

        value = getattr(settings_file, setting)

        if value and any(keyword in setting.lower() for keyword in sensitive_keywords):
            value = "********"

        print(f"\t{setting}: {value}")