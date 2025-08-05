import cProfile
import os

from config.settings_manager import (
    check_required_settings,
    parse_my_args,
)
from config.state import (
    download_folders,
    grouped_notifications,
    komga_libraries,
    libraries_to_scan,
    moved_files,
    paths,
    profile_code,
    watchdog_toggle,
)
from core.duplicate_checker import check_for_duplicate_volumes
from core.series_matcher import check_for_existing_series
from core.upgrade_manager import check_for_missing_volumes
from core.watchdog_handler import Watcher
from filesystem.file_operations import (
    delete_unacceptable_files,
    rename_files,
)
from filesystem.folder_manager import (
    create_folders_for_items_in_download_folder,
    rename_dirs_in_download_folder,
)
from filesystem.path_utils import cache_existing_library_paths
from integrations.bookwalker_client import check_for_new_volumes_on_bookwalker
from integrations.discord_client import send_discord_message
from processing.cover_extractor import extract_covers
from utils.helpers import print_stats


def main():
    global komga_libraries
    global libraries_to_scan

    # Optional features below, use at your own risk.
    # Activate them in settings.py
    download_folder_in_paths = False

    # Determines when the cover_extraction should be run
    if download_folders and paths:
        for folder in download_folders:
            if folder in paths:
                download_folder_in_paths = True
                break

    cache_existing_library_paths()

    # Correct any incorrect file extensions
    # if correct_file_extensions_toggle:
    #     correct_file_extensions()

    # Convert any non-cbz supported file to cbz
    # if convert_to_cbz_toggle:
    #     convert_to_cbz()

    # Delete any files with unacceptable keywords in their name
    delete_unacceptable_files()

    # Delete any chapters from the downloads folder
    # if delete_chapters_from_downloads_toggle:
    #     delete_chapters_from_downloads()

    # Generate the release group list
    # if (
    #     generate_release_group_list_toggle
    #     and log_to_file
    #     and paths
    #     and not watchdog_toggle
    #     and not in_docker
    # ):
    #     generate_rename_lists()

    # Rename the files in the download folders
    rename_files()

    # Create folders for items in the download folder
    create_folders_for_items_in_download_folder()

    # Checks for duplicate volumes/chapters in the download folders
    check_for_duplicate_volumes(download_folders)

    # Extract the covers from the files in the download folders
    if paths and download_folder_in_paths:
        extract_covers()
        print_stats()

    # Match the files in the download folders to the files in the library
    if download_folders and paths:
        check_for_existing_series()

    # Rename the root directory folders in the download folder
    if download_folders:
        rename_dirs_in_download_folder()

    if grouped_notifications and not watchdog_toggle:
        send_discord_message(None, grouped_notifications)

    # Extract the covers from the files in the library
    if paths and not download_folder_in_paths:
        if (watchdog_toggle and moved_files) or not watchdog_toggle:
            if watchdog_toggle:
                paths_to_trigger = []
                for path in paths:
                    if moved_files:
                        if (
                            any(
                                moved_file.startswith(path)
                                for moved_file in moved_files
                            )
                            and path not in paths_to_trigger
                        ):
                            paths_to_trigger.append(path)
                            continue

                if paths_to_trigger:
                    extract_covers(paths_to_process=paths_to_trigger)
            else:
                if profile_code == "extract_covers()":
                    cProfile.run(profile_code, sort="cumtime")
                    exit()
                else:
                    extract_covers()
                print_stats()

    # Check for missing volumes in the library (local solution)
    check_for_missing_volumes()

    # Check for missing volumes in the library (bookwalker solution)
    if not watchdog_toggle:
        check_for_new_volumes_on_bookwalker()

    # Sends a scan request to Komga for each library that had a file moved into it.
    if moved_files:
        if not komga_libraries:
            # Retrieve the Komga libraries
            # komga_libraries = get_komga_libraries()
            pass

        for path in moved_files:
            if os.path.isfile(path):
                # Scan the Komga libraries for matching root path
                # and trigger a scan.
                if komga_libraries:
                    for library in komga_libraries:
                        if library["id"] in libraries_to_scan:
                            continue

                        # if is_root_present(library["root"], path):
                        #     libraries_to_scan.append(library["id"])
                        pass

        # Send scan requests to each komga library
        if libraries_to_scan:
            for library_id in libraries_to_scan:
                # scan_komga_library(library_id)
                pass

    # Reset libraries_to_scan
    libraries_to_scan = []


if __name__ == "__main__":
    parse_my_args()  # parses the user's arguments

    check_required_settings()

    if watchdog_toggle and download_folders:
        while True:
            print("\nWatchdog is enabled, watching for changes...")
            watch = Watcher()
            watch.run()
    else:
        if profile_code == "main()":
            # run with cprofile and sort by cumulative time
            cProfile.run(profile_code, sort="cumtime")
            exit()
        else:
            main()