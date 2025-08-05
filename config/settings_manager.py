# -*- coding: utf-8 -*-
"""
Settings and argument parsing.
"""
import argparse
import re

from config.constants import (
    auto_classify_watchdog_paths,
    blank_black_image_path,
    blank_white_image_path,
    bookwalker_webhook_urls,
    check_for_existing_series_toggle,
    discord_webhook_url,
    download_folders,
    download_folders_with_types,
    file_extensions,
    file_formats,
    in_docker,
    library_types,
    manga_extensions,
    novel_extensions,
    paths,
    paths_with_types,
    script_version,
    script_version_text,
    send_scan_request_to_komga_libraries_toggle,
    settings,
    translation_source_types,
    watchdog_toggle,
)
from config.constants import settings as settings_file
from integrations.discord_client import send_discord_message
from integrations.komga_client import get_komga_libraries
from models.file_models import Path
from utils.helpers import (
    contains_chapter_keywords,
    contains_volume_keywords,
    get_all_files_in_directory,
    get_file_extension,
    send_message,
)


# Processes the user paths
def process_path(path, paths_with_types, paths, is_download_folders=False):
    COMMON_EXTENSION_THRESHOLD = 0.3  # 30%

    # Attempts to automatically classify files based on certain thresholds and criteria.
    def process_auto_classification():
        nonlocal path_formats, path_extensions, path_library_types

        CHAPTER_THRESHOLD = 0.9  # 90%
        VOLUME_THRESHOLD = 0.9  # 90%

        files = get_all_files_in_directory(path_str)

        if files:
            print("\t\t\t- attempting auto-classification...")
            print(f"\t\t\t\t- got {len(files)} files.")
            if len(files) >= 100:
                print("\t\t\t\t\t- trimming files to 75%...")
                files = files[: int(len(files) * 0.75)]
                print(f"\t\t\t\t\t- trimmed to {len(files)} files.")

            print("\t\t\t\t- getting file extensions:")
            all_extensions = [get_file_extension(file) for file in files]

            path_extensions = get_common_extensions(all_extensions)

            path_extension_sets = [manga_extensions, novel_extensions]

            # If no common extensions, use default file extensions
            if not path_extensions:
                print(
                    f"\t\t\t\t\t- no accepted path extensions found, defaulting to: {file_extensions}"
                )
                path_extensions = file_extensions
            else:
                # Extend path extensions with known extension sets
                print(f"\t\t\t\t\t- path extensions found: {path_extensions}")
                print(
                    "\t\t\t\t\t- extending path extensions with known extension sets:"
                )
                print(f"\t\t\t\t\t\t- manga_extensions: {manga_extensions}")
                print(f"\t\t\t\t\t\t- novel_extensions: {novel_extensions}")
                path_extension_sets = [manga_extensions, novel_extensions]
                for ext_set in path_extension_sets:
                    if any(extension in path_extensions for extension in ext_set):
                        path_extensions.extend(
                            ext for ext in ext_set if ext not in path_extensions
                        )
                print(f"\t\t\t\t\t- path extensions: {path_extensions}")

            print("\t\t\t\t- getting path types:")
            all_types = [
                (
                    "chapter"
                    if (
                        not contains_volume_keywords(file)
                        and contains_chapter_keywords(file)
                    )
                    else "volume"
                )
                for file in files
            ]

            chapter_count = all_types.count("chapter")
            volume_count = all_types.count("volume")
            total_files = len(all_types)

            print(f"\t\t\t\t\t- chapter count: {chapter_count}")
            print(f"\t\t\t\t\t- volume count: {volume_count}")
            print(f"\t\t\t\t\t- total files: {total_files}")
            print(
                f"\t\t\t\t\t- chapter percentage: {int(chapter_count / total_files * 100)}%"
            )
            print(
                f"\t\t\t\t\t\t- required chapter percentage: {int(CHAPTER_THRESHOLD * 100)}%"
            )
            print(
                f"\t\t\t\t\t- volume percentage: {int(volume_count / total_files * 100)}%"
            )
            print(
                f"\t\t\t\t\t\t- required volume percentage: {int(VOLUME_THRESHOLD * 100)}%"
            )

            path_formats = [
                (
                    "chapter"
                    if chapter_count / total_files >= CHAPTER_THRESHOLD
                    else (
                        "volume"
                        if volume_count / total_files >= VOLUME_THRESHOLD
                        else file_formats
                    )
                )
            ]

            print(f"\t\t\t\t\t- path types: {path_formats}")

    # Gets the common extensions from a list of extensions
    def get_common_extensions(all_extensions):
        nonlocal COMMON_EXTENSION_THRESHOLD
        common_extensions = [
            ext
            for ext in set(all_extensions)
            if all_extensions.count(ext) / len(all_extensions)
            >= COMMON_EXTENSION_THRESHOLD
        ]
        return common_extensions if common_extensions else []

    # Determines what type of path it is, and assigns it to the appropriate list
    def process_single_type_path(path_to_process):
        nonlocal path_formats, path_extensions, path_library_types, path_translation_source_types
        if path_to_process.split(",").strip() in file_formats:
            path_formats = [
                path_type.strip() for path_type in path_to_process.split(",")
            ]
        elif re.search(r"\.\w{1,4}", path_to_process):
            path_extensions = [
                ext.strip()
                for ext in path_to_process.split(",")
                if ext.strip() in file_extensions
            ]
        elif path_to_process.split(",").strip() in [x.name for x in library_types]:
            path_library_types = [
                library_type.strip() for library_type in path_to_process.split(",")
            ]
        elif path_to_process.split(",").strip() in translation_source_types:
            path_translation_source_types = [
                translation_source_type.strip()
                for translation_source_type in path_to_process.split(",")
            ]

    path_formats = []
    path_extensions = []
    path_library_types = []
    path_translation_source_types = []
    path_source_languages = []
    path_obj = None

    path_str = path
    print(f"\t\t{path_str}")

    if len(path) == 1:
        if (
            watchdog_toggle
            and auto_classify_watchdog_paths
            and check_for_existing_series_toggle
            and not (download_folders and path_str in download_folders)
        ):
            process_auto_classification()
            path_obj = Path(
                path_str,
                path_formats=path_formats or [],
                path_extensions=path_extensions or [],
                library_types=path_library_types or [],
                translation_source_types=path_translation_source_types or [],
                source_languages=path_source_languages or [],
            )
    else:
        # process all paths except for the first one
        for path_to_process in path[1:]:
            process_single_type_path(path_to_process)

        path_obj = Path(
            path_str,
            path_formats=path_formats or file_formats,
            path_extensions=path_extensions or file_extensions,
            library_types=path_library_types or library_types,
            translation_source_types=path_translation_source_types
            or translation_source_types,
            source_languages=path_source_languages or [],
        )

    if not is_download_folders:
        paths.append(path_str)

        if path_obj:
            paths_with_types.append(path_obj)
    else:
        download_folders.append(path_str)

        if path_obj:
            download_folders_with_types.append(path_obj)


# Parses the passed command-line arguments
def parse_my_args():
    # Function to parse boolean arguments from string values
    def parse_bool_argument(arg_value):
        return str(arg_value).lower().strip() == "true"

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

    parser = parser.parse_args()

    print(f"\nScript Version: {script_version_text}")

    print("\nRun Settings:")

    if parser.download_folders is not None:
        new_download_folders = []
        for download_folder in parser.download_folders:
            if download_folder:
                if r"\1" in download_folder:
                    split_download_folders = download_folder.split(r"\1")
                    new_download_folders.extend(
                        [split_download_folder]
                        for split_download_folder in split_download_folders
                    )
                else:
                    new_download_folders.append(download_folder)

        parser.download_folders = new_download_folders

        print("\tdownload_folders:")
        for download_folder in parser.download_folders:
            if download_folder:
                if r"\0" in download_folder:
                    download_folder = download_folder.split(r"\0")
                process_path(
                    download_folder,
                    download_folders_with_types,
                    download_folders,
                    is_download_folders=True,
                )

        if download_folders_with_types:
            print("\n\tdownload_folders_with_types:")
            for item in download_folders_with_types:
                print(f"\t\tpath: {str(item.path)}")
                print(f"\t\t\tformats: {str(item.path_formats)}")
                print(f"\t\t\textensions: {str(item.path_extensions)}")

    if parser.watchdog:
        if download_folders:
            watchdog_toggle = parse_bool_argument(parser.watchdog)
        else:
            send_message(
                "Watchdog was toggled, but no download folders were passed to the script.",
                error=True,
            )

    if parser.paths is not None:
        new_paths = []
        for path in parser.paths:
            if path and r"\1" in path:
                split_paths = path.split(r"\1")
                new_paths.extend([split_path] for split_path in split_paths)
            else:
                new_paths.append(path)

        parser.paths = new_paths
        print("\tpaths:")
        for path in parser.paths:
            if path:
                if r"\0" in path:
                    path = path.split(r"\0")
                process_path(path, paths_with_types, paths)

        if paths_with_types:
            print("\n\tpaths_with_types:")
            for item in paths_with_types:
                print(f"\t\tpath: {str(item.path)}")
                print(f"\t\t\tformats: {str(item.path_formats)}")
                print(f"\t\t\textensions: {str(item.path_extensions)}")

    print(f"\twatchdog: {watchdog_toggle}")

    if watchdog_toggle:
        global watchdog_discover_new_files_check_interval, watchdog_file_transferred_check_interval
        if parser.watchdog_discover_new_files_check_interval:
            if parser.watchdog_discover_new_files_check_interval.isdigit():
                watchdog_discover_new_files_check_interval = int(
                    parser.watchdog_discover_new_files_check_interval
                )

        if parser.watchdog_file_transferred_check_interval:
            if parser.watchdog_file_transferred_check_interval.isdigit():
                watchdog_file_transferred_check_interval = int(
                    parser.watchdog_file_transferred_check_interval
                )
        print(
            f"\t\twatchdog_discover_new_files_check_interval: {watchdog_discover_new_files_check_interval}"
        )
        print(
            f"\t\twatchdog_file_transferred_check_interval: {watchdog_file_transferred_check_interval}"
        )

    if parser.output_covers_as_webp:
        global output_covers_as_webp
        output_covers_as_webp = parse_bool_argument(parser.output_covers_as_webp)
    print(f"\toutput_covers_as_webp: {output_covers_as_webp}")

    if not parser.paths and not parser.download_folders:
        print("No paths or download folders were passed to the script.")
        print("Exiting...")
        exit()

    if parser.webhook is not None:
        for item in parser.webhook:
            if item:
                for hook in item:
                    if hook:
                        if r"\1" in hook:
                            hook = hook.split(r"\1")
                        if isinstance(hook, str):
                            if hook and hook not in discord_webhook_url:
                                discord_webhook_url.append(hook)
                        elif isinstance(hook, list):
                            for url in hook:
                                if url and url not in discord_webhook_url:
                                    discord_webhook_url.append(url)
        print(f"\twebhooks: {str(discord_webhook_url)}")

    if parser.bookwalker_check:
        global bookwalker_check
        bookwalker_check = parse_bool_argument(parser.bookwalker_check)
    print(f"\tbookwalker_check: {bookwalker_check}")

    if parser.compress:
        global compress_image_option
        compress_image_option = parse_bool_argument(parser.compress)
    print(f"\tcompress: {compress_image_option}")

    if parser.compress_quality:
        global image_quality
        image_quality = int(parser.compress_quality)
    print(f"\tcompress_quality: {image_quality}")

    if parser.bookwalker_webhook_urls is not None:
        global bookwalker_webhook_urls
        for url in parser.bookwalker_webhook_urls:
            if url:
                for hook in url:
                    if hook:
                        if r"\1" in hook:
                            hook = hook.split(r"\1")
                        if isinstance(hook, str):
                            if hook and hook not in bookwalker_webhook_urls:
                                bookwalker_webhook_urls.append(hook)
                        elif isinstance(hook, list):
                            for url_in_hook in hook:
                                if (
                                    url_in_hook
                                    and url_in_hook not in bookwalker_webhook_urls
                                ):
                                    bookwalker_webhook_urls.append(url_in_hook)
        print(f"\tbookwalker_webhook_urls: {bookwalker_webhook_urls}")

    if parser.new_volume_webhook:
        global new_volume_webhook
        new_volume_webhook = parser.new_volume_webhook
    print(f"\tnew_volume_webhook: {new_volume_webhook}")

    if parser.log_to_file:
        global log_to_file
        log_to_file = parse_bool_argument(parser.log_to_file)
    print(f"\tlog_to_file: {log_to_file}")

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

    print(f"\tin_docker: {in_docker}")
    print(f"\tblank_black_image_path: {blank_black_image_path}")
    print(f"\tblank_white_image_path: {blank_white_image_path}")

    if (
        send_scan_request_to_komga_libraries_toggle
        and check_for_existing_series_toggle
        and watchdog_toggle
    ):
        komga_libraries = get_komga_libraries()
        komga_library_paths = (
            [x["root"] for x in komga_libraries] if komga_libraries else []
        )
        print(f"\tkomga_libraries: {komga_library_paths}")


# Checks that the user has the required settings in settings.py
# Will become obselete once I figure out an automated way of
# parsing and updating the user's settings.py file.
def check_required_settings():
    required_settings = {
        "uncheck_non_qbit_upgrades_toggle": (2, 5, 0),
        "qbittorrent_ip": (2, 5, 0),
        "qbittorrent_port": (2, 5, 0),
        "qbittorrent_username": (2, 5, 0),
        "qbittorrent_password": (2, 5, 0),
        "delete_unacceptable_torrent_titles_in_qbit": (2, 5, 0),
    }

    missing_settings = [
        setting
        for setting, version in required_settings.items()
        if script_version == version and setting not in settings
    ]

    if missing_settings:
        send_discord_message(
            f"\nMissing settings in settings.py: \n\t{','.join(missing_settings)}\nPlease update your settings.py file.",
        )

        print("\nMissing settings in settings.py:")
        for setting in missing_settings:
            print(f"\t{setting}")
        print("Please update your settings.py file.\n")
        exit()