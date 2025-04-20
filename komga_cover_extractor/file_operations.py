# komga_cover_extractor/file_operations.py
import os
import re
import shutil
import zipfile  # Needed for zipfile.is_zipfile

# TODO: Refactor config imports
try:
    from .config import (
        download_folders,
        paths,
        paths_with_types,
        file_extensions,
        unacceptable_keywords,
        manga_extensions,
        exception_keywords,
        manual_rename,
        rename_zip_to_cbz,
        LOGS_DIR,
        # Global state vars (TODO: Refactor state management)
        # transferred_files, transferred_dirs, grouped_notifications,
        # moved_folders, moved_files, libraries_to_scan, komga_libraries
        # Colors
        yellow_color,
        grey_color,
        # Toggles
        watchdog_toggle,
        send_scan_request_to_komga_libraries_toggle,
    )
except ImportError:
    print(
        "WARN: Could not import from .config, using placeholder values in file_operations."
    )
    download_folders = []
    paths = []
    paths_with_types = []
    file_extensions = [".cbz", ".zip", ".epub"]
    unacceptable_keywords = []
    manga_extensions = [".cbz", ".zip"]
    exception_keywords = []
    manual_rename = False
    rename_zip_to_cbz = False
    LOGS_DIR = "logs"
    yellow_color, grey_color = 0, 0
    watchdog_toggle = False
    send_scan_request_to_komga_libraries_toggle = False


# TODO: Import necessary functions from utility modules
try:
    from .log_utils import send_message, write_to_file
    from .file_utils import (
        process_files_and_folders,
        get_file_extension,
        get_header_extension,
        get_extensionless_name,
        rename_file,
        remove_file,
        move_file,
        create_folder_obj,
        upgrade_to_file_class,
        check_and_delete_empty_folder,
        get_library_type,
        move_folder,  # Added move_folder
    )
    from .string_utils import (
        contains_chapter_keywords,
        contains_volume_keywords,
        check_for_exception_keywords,
        similar,
        clean_str,
        get_series_name,
        get_series_name_from_volume,
        remove_brackets,  # Added remove_brackets
    )
    from .misc_utils import get_input_from_user
    from .discord_utils import handle_fields, group_notification, DiscordEmbed
    from .models import File, Folder, Embed  # Import needed models

    # from .komga_utils import get_komga_libraries # Import if needed directly
except ImportError as e:
    print(f"FATAL: Failed to import dependencies in file_operations: {e}")

    def send_message(msg, error=False, discord=False):
        print(f"{'ERROR: ' if error else ''}{msg}")

    def write_to_file(*args, **kwargs):
        pass

    def process_files_and_folders(r, f, d, **kwargs):
        return f, d

    def get_file_extension(f):
        return os.path.splitext(f)[1]

    def get_header_extension(f):
        return None

    def get_extensionless_name(f):
        return os.path.splitext(f)[0]

    def rename_file(*args, **kwargs):
        return False

    def remove_file(*args, **kwargs):
        return False

    def move_file(*args, **kwargs):
        return False

    def create_folder_obj(*args, **kwargs):
        return None  # Needs Folder model

    def upgrade_to_file_class(*args, **kwargs):
        return []  # Needs File model

    def check_and_delete_empty_folder(*args, **kwargs):
        pass

    def get_library_type(*args, **kwargs):
        return None

    def move_folder(*args, **kwargs):
        return False

    def contains_chapter_keywords(*args, **kwargs):
        return False

    def contains_volume_keywords(*args, **kwargs):
        return False

    def check_for_exception_keywords(*args, **kwargs):
        return False

    def similar(a, b):
        return 0.0

    def clean_str(s, **kwargs):
        return s

    def get_series_name(d):
        return d

    def get_series_name_from_volume(*args, **kwargs):
        return ""

    def remove_brackets(s):
        return s

    def get_input_from_user(*args, **kwargs):
        return "y"  # Default to yes for placeholders

    def handle_fields(e, f):
        return e

    def group_notification(n, e, **kwargs):
        n.append(e)
        return n

    class DiscordEmbed:
        pass

    class File:
        pass

    class Folder:
        pass

    class Embed:
        pass

    # class get_komga_libraries: pass


# TODO: Refactor global state management
# These might need to be passed into functions or managed via a context object
transferred_files = []  # Watchdog state
transferred_dirs = []  # Watchdog state
grouped_notifications = []  # Discord state
moved_folders = []  # State for Komga scan
moved_files = []  # State for Komga scan
libraries_to_scan = []  # State for Komga scan
komga_libraries = []  # State for Komga scan
processed_files = []  # State for tracking processed files during rename/reorg


# Goes through each file in download_folders and checks for an incorrect file extension
# based on the file header. If the file extension is incorrect, it will rename the file.
def correct_file_extensions(folders=download_folders):
    """Checks and corrects file extensions based on file headers."""
    global transferred_files, grouped_notifications  # Manage global state

    print("\nChecking for incorrect file extensions...")

    if not folders:
        print("\tNo download folders specified.")
        return

    for folder in folders:
        if not os.path.isdir(folder):
            print(f"\t{folder} does not exist.")
            continue

        print(f"\tScanning: {folder}")
        # Use scandir for potentially better performance
        try:
            for root, dirs, files in scandir.walk(folder):
                # Use process_files_and_folders for consistent filtering
                files, dirs = process_files_and_folders(
                    root,
                    files,
                    dirs,
                    just_these_files=transferred_files,
                    just_these_dirs=transferred_dirs,
                    is_correct_extensions_feature=file_extensions
                    + [".rar", ".cbr", ".7z"],  # Include convertable
                )

                if not files:
                    continue

                # Upgrade only necessary info for this function
                volumes = upgrade_to_file_class(
                    [f for f in files if os.path.isfile(os.path.join(root, f))],
                    root,
                    skip_get_header_extension=False,  # Need header extension
                    is_correct_extensions_feature=file_extensions
                    + [".rar", ".cbr", ".7z"],
                )

                if not volumes:
                    continue

                for volume in volumes:
                    if not volume.header_extension:
                        continue  # Skip if header couldn't be determined

                    print(
                        f"\n\t\t{volume.name}\n\t\t\tfile extension:   {volume.extension}\n\t\t\theader extension: {volume.header_extension}"
                    )

                    if volume.extension.lower() != volume.header_extension.lower():
                        new_name = (
                            f"{volume.extensionless_name}{volume.header_extension}"
                        )
                        new_path = os.path.join(volume.root, new_name)

                        print(
                            f"\n\t\t\tRenaming File:\n\t\t\t\t{volume.name}\n\t\t\t\t\tto\n\t\t\t\t{new_name}"
                        )

                        user_input = (
                            get_input_from_user("\t\t\tRename", ["y", "n"], ["y", "n"])
                            if manual_rename
                            else "y"
                        )

                        if user_input == "y":
                            rename_status = rename_file(
                                volume.path, new_path, silent=True
                            )  # Use imported file_utils function
                            if rename_status:
                                print("\t\t\tRenamed successfully")
                                # TODO: Add Discord notification via discord_utils
                                # embed = handle_fields(...)
                                # grouped_notifications = group_notification(...)

                                # Update watchdog state if needed
                                if watchdog_toggle:
                                    if volume.path in transferred_files:
                                        transferred_files.remove(volume.path)
                                    if new_path not in transferred_files:
                                        transferred_files.append(new_path)
                            else:
                                send_message(
                                    f"Failed to rename {volume.name} to {new_name}",
                                    error=True,
                                )
                        else:
                            print("\t\t\tSkipped")
        except Exception as e:
            send_message(
                f"Error scanning {folder} for extension correction: {e}", error=True
            )


# Deletes any file with an extension in unacceptable_keywords from the download_folders
def delete_unacceptable_files(folders=download_folders):
    """Deletes files matching unacceptable keywords."""
    global grouped_notifications  # Manage global state

    print("\nSearching for unacceptable files...")

    if not folders:
        print(
            "\tNo download folders specified, skipping deleting unacceptable files..."
        )
        return
    if not unacceptable_keywords:
        print(
            "\tNo unacceptable keywords specified, skipping deleting unacceptable files..."
        )
        return

    try:
        for path in folders:
            if not os.path.isdir(path):
                print(f"\nERROR: {path} is an invalid path.\n")
                continue

            print(f"\tScanning: {path}")
            # Use scandir for potentially better performance
            for root, dirs, files in scandir.walk(path):
                # Use process_files_and_folders for consistent filtering
                # Need to allow all file types initially to check against unacceptable_keywords
                files, dirs = process_files_and_folders(
                    root,
                    files,
                    dirs,
                    just_these_files=transferred_files,
                    just_these_dirs=transferred_dirs,
                    skip_remove_unaccepted_file_types=True,  # Check all files
                    keep_images_in_just_these_files=True,  # Keep images if needed
                )

                for file in files:
                    file_path = os.path.join(root, file)
                    if not os.path.isfile(file_path):
                        continue

                    # Check against unacceptable keywords (regex patterns)
                    for keyword_pattern in unacceptable_keywords:
                        try:
                            unacceptable_keyword_search = re.search(
                                keyword_pattern, file, re.IGNORECASE
                            )
                            if unacceptable_keyword_search:
                                send_message(
                                    f"\tUnacceptable: '{unacceptable_keyword_search.group()}' match found in {file}\n\t\tDeleting file from: {root}",
                                    discord=False,
                                )
                                # TODO: Add Discord notification via discord_utils
                                # embed = handle_fields(...)
                                # grouped_notifications = group_notification(...)
                                remove_file(
                                    file_path
                                )  # Use imported file_utils function
                                break  # Stop checking other keywords for this file
                        except re.error as re_err:
                            send_message(
                                f"Invalid regex pattern in unacceptable_keywords: '{keyword_pattern}'. Error: {re_err}",
                                error=True,
                            )
                            # Optionally skip this pattern and continue
                            continue

            # Clean up empty folders after deletion pass
            # Walk again or use the stored structure if reliable
            for root, dirs, files in scandir.walk(
                path, topdown=False
            ):  # Walk bottom-up for cleanup
                for folder_name in dirs:
                    check_and_delete_empty_folder(
                        os.path.join(root, folder_name)
                    )  # Use imported file_utils function
                # Check the root itself after processing subdirs
                if root == path:  # Only check the initial path folder if needed
                    check_and_delete_empty_folder(root)

    except Exception as e:
        send_message(f"Error during unacceptable file deletion: {e}", error=True)


# Deletes chapter files from the download folder.
def delete_chapters_from_downloads(folders=download_folders):
    """Deletes files identified as chapters from download folders."""
    global grouped_notifications  # Manage global state

    print("\nSearching for chapter files to delete...")

    if not folders:
        print("\tNo download folders specified, skipping deleting chapters...")
        return

    try:
        for path in folders:
            if not os.path.isdir(path):
                print(f"\nERROR: {path} is an invalid path.\n")
                continue

            print(f"\tScanning: {path}")
            # Use scandir for potentially better performance
            for root, dirs, files in scandir.walk(path):
                # Use process_files_and_folders for consistent filtering
                files, dirs = process_files_and_folders(
                    root,
                    files,
                    dirs,
                    chapters=True,  # Allow chapters for this check
                    just_these_files=transferred_files,
                    just_these_dirs=transferred_dirs,
                )

                for file in files:
                    file_path = os.path.join(root, file)
                    if not os.path.isfile(file_path):
                        continue

                    is_chapter = contains_chapter_keywords(
                        file
                    )  # Use imported string_utils function
                    is_volume = contains_volume_keywords(
                        file
                    )  # Use imported string_utils function
                    is_exception = check_for_exception_keywords(
                        file, exception_keywords
                    )  # Use imported string_utils function
                    is_manga = (
                        get_file_extension(file) in manga_extensions
                    )  # Use imported file_utils function

                    if is_chapter and not is_volume and not is_exception and is_manga:
                        send_message(
                            f"\n\t\tFile: {file}\n\t\tLocation: {root}\n\t\tContains chapter keywords/lone numbers and does not contain any volume/exclusion keywords\n\t\tDeleting chapter release.",
                            discord=False,
                        )
                        # TODO: Add Discord notification via discord_utils
                        # embed = handle_fields(...)
                        # grouped_notifications = group_notification(...)
                        remove_file(file_path)  # Use imported file_utils function

            # Clean up empty folders after deletion pass
            for root, dirs, files in scandir.walk(
                path, topdown=False
            ):  # Walk bottom-up
                for folder_name in dirs:
                    check_and_delete_empty_folder(
                        os.path.join(root, folder_name)
                    )  # Use imported file_utils function
                if root == path:
                    check_and_delete_empty_folder(root)

    except Exception as e:
        send_message(f"Error during chapter deletion: {e}", error=True)


# Renames files.
def rename_files(download_folders=download_folders, test_mode=False):
    """Renames files based on extracted metadata and naming conventions."""
    # This function is complex and depends heavily on reorganize_and_rename
    # For now, it acts as a wrapper around reorganize_and_rename
    global processed_files  # Manage global state

    print("\nSearching for files to rename...")

    if not download_folders:
        print("\tNo download folders specified, skipping renaming files...")
        return

    for path in download_folders:
        if not os.path.isdir(path):
            send_message(
                f"\tDownload folder {path} does not exist, skipping...", error=True
            )
            continue

        print(f"\tScanning: {path}")
        # Use scandir for potentially better performance
        for root, dirs, files in scandir.walk(path):
            if test_mode and root not in download_folders:
                continue  # Limit test mode scope

            # Use process_files_and_folders for consistent filtering
            files, dirs = process_files_and_folders(
                root,
                files,
                dirs,
                just_these_files=transferred_files,
                just_these_dirs=transferred_dirs,
                test_mode=test_mode,
            )

            if not files:
                continue

            # Upgrade files to Volume objects for reorganize_and_rename
            volumes = upgrade_to_volume_class(
                upgrade_to_file_class(
                    [
                        f
                        for f in files
                        if os.path.isfile(os.path.join(root, f)) or test_mode
                    ],
                    root,
                    test_mode=test_mode,
                ),
                test_mode=test_mode,
                # Pass necessary skip flags if needed based on reorganize_and_rename requirements
            )

            if not volumes:
                continue

            print(f"\tProcessing folder: {root}")
            # Call the complex renaming logic (currently in string_utils, might move later)
            updated_volumes = reorganize_and_rename(
                volumes, root
            )  # Use imported string_utils function

            # Update processed_files list based on the results of renaming
            # This assumes reorganize_and_rename returns the list of potentially renamed Volume objects
            for vol in updated_volumes:
                if vol.name not in processed_files:
                    processed_files.append(vol.name)


# Creates folders for our stray volumes sitting in the root of the download folder.
def create_folders_for_items_in_download_folder(folders=download_folders):
    """Creates folders for lone files in download directories."""
    global transferred_files, transferred_dirs, grouped_notifications  # Manage global state

    print("\nCreating folders for lone items in download folder...")

    if not folders:
        print("\tNo download folders found.")
        return

    for download_folder in folders:
        if not os.path.isdir(download_folder):
            send_message(
                f"\nERROR: {download_folder} is an invalid path.\n", error=True
            )
            continue

        print(f"\tScanning: {download_folder}")
        try:
            # Use scandir for potentially better performance
            # Get only top-level items
            entries = list(scandir.scandir(download_folder))
            top_level_files = [e.name for e in entries if e.is_file()]
            top_level_dirs = [e.name for e in entries if e.is_dir()]

            # Filter files using process_files_and_folders
            files, _ = process_files_and_folders(
                download_folder,
                top_level_files,
                [],  # Pass empty dirs list
                just_these_files=transferred_files,
                # No just_these_dirs needed as we only care about top-level files
            )

            if not files:
                continue

            # Upgrade to File objects to get basename (series name)
            file_objects = upgrade_to_file_class(files, download_folder)

            for file_obj in file_objects:
                if not file_obj.basename:
                    continue  # Skip if series name couldn't be determined

                target_folder_path = os.path.join(download_folder, file_obj.basename)
                target_file_path = os.path.join(target_folder_path, file_obj.name)
                moved = False

                # Option 1: Move to existing similar folder
                if move_lone_files_to_similar_folder and top_level_dirs:
                    similar_dirs = [
                        d
                        for d in top_level_dirs
                        if similar(clean_str(d), clean_str(file_obj.basename))
                        >= required_similarity_score
                    ]
                    if similar_dirs:
                        chosen_dir = similar_dirs[
                            0
                        ]  # Move to the first similar directory found
                        target_folder_path = os.path.join(download_folder, chosen_dir)
                        target_file_path = os.path.join(
                            target_folder_path, file_obj.name
                        )

                        # Optional: Rename file to match folder name if enabled
                        if (
                            replace_series_name_in_file_name_with_similar_folder_name
                            and file_obj.basename != chosen_dir
                        ):
                            # TODO: Implement file renaming logic here if needed, similar to original script
                            pass  # Placeholder for renaming logic

                        if not os.path.isfile(target_file_path):
                            if move_file(
                                file_obj, target_folder_path
                            ):  # Use imported file_utils function
                                moved = True
                                # Update watchdog state
                                if watchdog_toggle:
                                    if target_file_path not in transferred_files:
                                        transferred_files.append(target_file_path)
                                    if file_obj.path in transferred_files:
                                        transferred_files.remove(file_obj.path)
                                    # Add target folder to transferred_dirs if not already present
                                    if not any(
                                        td.root == target_folder_path
                                        for td in transferred_dirs
                                    ):
                                        transferred_dirs.append(
                                            create_folder_obj(target_folder_path)
                                        )  # Use imported file_utils function
                        else:
                            # File already exists in target, remove the source lone file
                            send_message(
                                f"Duplicate found in {chosen_dir}, removing lone file: {file_obj.name}",
                                discord=False,
                            )
                            remove_file(
                                file_obj.path, silent=True
                            )  # Use imported file_utils function
                            moved = True  # Consider it handled

                # Option 2: Create new folder if not moved to existing
                if not moved:
                    if not os.path.isdir(target_folder_path):
                        try:
                            os.makedirs(target_folder_path)
                            print(f"\tCreated folder: {target_folder_path}")
                        except OSError as e:
                            send_message(
                                f"Failed to create folder {target_folder_path}: {e}",
                                error=True,
                            )
                            continue

                    if not os.path.isfile(target_file_path):
                        if move_file(
                            file_obj, target_folder_path
                        ):  # Use imported file_utils function
                            moved = True
                            # Update watchdog state
                            if watchdog_toggle:
                                if target_file_path not in transferred_files:
                                    transferred_files.append(target_file_path)
                                if file_obj.path in transferred_files:
                                    transferred_files.remove(file_obj.path)
                                if not any(
                                    td.root == target_folder_path
                                    for td in transferred_dirs
                                ):
                                    transferred_dirs.append(
                                        create_folder_obj(target_folder_path)
                                    )  # Use imported file_utils function
                    else:
                        # File already exists, remove the source lone file
                        send_message(
                            f"Duplicate found in new folder {file_obj.basename}, removing lone file: {file_obj.name}",
                            discord=False,
                        )
                        remove_file(
                            file_obj.path, silent=True
                        )  # Use imported file_utils function
                        moved = True  # Handled

        except Exception as e:
            send_message(
                f"Error processing download folder {download_folder}: {e}", error=True
            )


# Renames the folders in our download directory.
def rename_dirs_in_download_folder(paths_to_process=download_folders):
    """Renames directories in download folders based on content analysis."""
    # ... (implementation from original script lines 7104-7450) ...
    # This is complex and depends on local helper functions process_folder,
    # rename_based_on_volumes, rename_based_on_brackets. Needs careful porting.
    print("WARN: rename_dirs_in_download_folder not fully implemented yet.")
    pass  # Placeholder


# Checks existing series within existing libraries to see if their type matches the library they're in
def move_series_to_correct_library(paths_to_search=paths_with_types):
    """Moves series folders to library paths matching their content type."""
    global grouped_notifications, moved_folders, moved_files, libraries_to_scan, komga_libraries  # Manage global state

    print("\nChecking library types and moving series if necessary...")

    if not paths_to_search:
        print("\tNo typed library paths specified.")
        return

    try:
        # Create a mapping from library type name to target paths
        target_paths_map = {}
        for pwt in paths_with_types:
            for (
                lib_type_name
            ) in pwt.library_types:  # Assuming library_types is list of names
                # Store path based on format (volume/chapter) and type (manga/novel)
                key = (
                    tuple(sorted(pwt.path_formats)),
                    lib_type_name,
                )  # Use formats and type as key
                if key not in target_paths_map:
                    target_paths_map[key] = []
                target_paths_map[key].append(pwt.path)

        for current_path_obj in paths_to_search:
            current_path = current_path_obj.path
            if not os.path.isdir(current_path):
                send_message(
                    f"\nERROR: {current_path} is an invalid path.\n", error=True
                )
                continue

            print(f"\nScanning {current_path} for incorrectly placed series...")
            # Use scandir for potentially better performance
            try:
                # Get only top-level directories
                series_dirs = [
                    entry.path
                    for entry in scandir.scandir(current_path)
                    if entry.is_dir() and not entry.name.startswith(".")
                ]
            except Exception as e:
                send_message(f"Error scanning path {current_path}: {e}", error=True)
                continue

            for series_dir_path in series_dirs:
                series_dir_name = os.path.basename(series_dir_path)
                print(f"\tChecking series: {series_dir_name}")

                try:
                    # Analyze content of the series directory
                    series_files = [
                        entry.name
                        for entry in scandir.scandir(series_dir_path)
                        if entry.is_file()
                    ]
                    # Use process_files_and_folders for consistent filtering
                    files, _ = process_files_and_folders(
                        series_dir_path, series_files, []
                    )

                    if not files:
                        print("\t\tNo valid files found, skipping type check.")
                        continue

                    # Upgrade to File objects to determine type
                    file_objects = upgrade_to_file_class(files, series_dir_path)
                    if not file_objects:
                        continue

                    # Determine dominant library type and format (volume/chapter)
                    actual_library_type = get_library_type(
                        [fo.name for fo in file_objects]
                    )  # Use imported file_utils function
                    actual_format = (
                        "chapter"
                        if get_folder_type(file_objects, file_type="chapter") >= 90
                        else "volume"
                    )  # Use imported file_utils function

                    if not actual_library_type:
                        print(
                            "\t\tCould not determine dominant library type for series."
                        )
                        continue

                    # Check if the current path is correct for the determined type/format
                    current_path_formats = tuple(sorted(current_path_obj.path_formats))
                    current_path_lib_types = (
                        current_path_obj.library_types
                    )  # Assuming list of names

                    is_correct_location = (
                        actual_format in current_path_formats
                        and actual_library_type.name in current_path_lib_types
                    )

                    if is_correct_location:
                        # print("\t\tSeries is in the correct library path.")
                        continue

                    # Find the target path
                    target_key = (
                        tuple(sorted([actual_format])),
                        actual_library_type.name,
                    )
                    possible_target_paths = target_paths_map.get(target_key, [])

                    if not possible_target_paths:
                        print(
                            f"\t\tNo suitable target library path found for type '{actual_library_type.name}' and format '{actual_format}'. Skipping move."
                        )
                        continue

                    # Simple strategy: move to the first matching target path found
                    target_path = possible_target_paths[0]
                    new_location_dir = os.path.join(target_path, series_dir_name)

                    print(
                        f"\t\tMismatch detected: Series type '{actual_library_type.name}/{actual_format}' in path for '{'/'.join(current_path_lib_types)}/{'/'.join(current_path_formats)}'."
                    )
                    print(f"\t\tAttempting to move to: {target_path}")

                    if os.path.isdir(new_location_dir):
                        # Target directory exists, check if it's empty or needs merging (complex)
                        # For now, skip if target exists to avoid accidental overwrites/merges
                        print(
                            f"\t\tTarget directory '{new_location_dir}' already exists. Skipping move to prevent conflicts."
                        )
                        # TODO: Implement merge logic or user prompt if needed
                        continue

                    # Move the folder
                    if move_folder(
                        series_dir_path, target_path, silent=True
                    ):  # Use imported file_utils function
                        send_message(
                            f"\t\tMoved '{series_dir_name}' from '{current_path}' to '{target_path}'",
                            discord=False,
                        )
                        # Update state for Komga scan
                        if new_location_dir not in moved_folders:
                            moved_folders.append(new_location_dir)
                        # Add all files within the moved folder to moved_files
                        moved_files.extend(
                            [os.path.join(new_location_dir, f) for f in files]
                        )

                        # TODO: Add Discord notification via discord_utils
                        # embed = handle_fields(...)
                        # grouped_notifications = group_notification(...)

                        # Trigger Komga scan for both source and destination libraries
                        if send_scan_request_to_komga_libraries_toggle:
                            if not komga_libraries:
                                komga_libraries = (
                                    get_komga_libraries()
                                )  # Use imported komga_utils function
                            if komga_libraries:
                                libs_to_scan_now = set()
                                for lib in komga_libraries:
                                    if is_root_present(
                                        lib["root"], current_path
                                    ) or is_root_present(
                                        lib["root"], target_path
                                    ):  # Use helper
                                        libs_to_scan_now.add(lib["id"])
                                for lib_id in libs_to_scan_now:
                                    if lib_id not in libraries_to_scan:
                                        libraries_to_scan.append(
                                            lib_id
                                        )  # Add to global list for final scan
                    else:
                        send_message(
                            f"\t\tFailed to move '{series_dir_name}' to '{target_path}'.",
                            error=True,
                        )

                except Exception as series_err:
                    send_message(
                        f"Error processing series directory {series_dir_path}: {series_err}",
                        error=True,
                    )
                    continue  # Skip to next series directory

    except Exception as e:
        send_message(f"Error during library type check: {e}", error=True)


# Rebuilds the file name by cleaning up, adding, and moving some parts around.
# Originally from komga_cover_extractor.py (lines 4523-4797)
def reorganize_and_rename(files, dir):
    """
    Reorganizes and renames files based on extracted metadata and preferred naming formats.
    Uses volume/chapter keywords, numbers, year, publisher, release group, etc.
    """
    # TODO: Refactor global state access
    global transferred_files, grouped_notifications, processed_files

    # Define bracket modifiers based on file extension type
    modifiers = {
        ext: (
            "[%s]"
            if ext in novel_extensions
            else (
                "(%s)" if ext in manga_extensions else " %s "
            )  # Default space padding if unknown
        )
        for ext in file_extensions
    }
    base_dir = os.path.basename(dir)

    # Use a copy for iteration if modifying the list
    for file in files[:]:
        try:
            # Determine keywords, format, and padding based on file type
            keywords, preferred_naming_format, zfill_int, zfill_float = (
                (
                    chapter_regex_keywords,
                    preferred_chapter_renaming_format,
                    zfill_chapter_int_value,
                    zfill_chapter_float_value,
                )
                if file.file_type == "chapter"
                else (
                    volume_regex_keywords,
                    preferred_volume_renaming_format,
                    zfill_volume_int_value,
                    zfill_volume_float_value,
                )
            )

            # Check if the filename contains a pattern indicating a volume/chapter number
            # This regex seems complex, ensure keywords and file_extensions_regex are correctly defined/imported
            # Simplified check: Does it contain the keyword and a number?
            keyword_num_pattern = (
                rf"\b({keywords})\s*(\d+([._-]\d+)?)\b"  # Basic pattern
            )
            if (
                re.search(keyword_num_pattern, file.name, re.IGNORECASE)
                or file.volume_number != ""
            ):

                # Start building the new name
                rename = f"{file.series_name} {preferred_naming_format}"  # Use file.series_name directly

                # Format the volume/chapter number string
                numbers = []
                if file.multi_volume and isinstance(file.volume_number, list):
                    # Handle multi-volume range (e.g., [1, 5])
                    numbers.extend(str(n) for n in file.volume_number)
                    # Insert hyphens between numbers in the range representation
                    number_string = ""
                    for i, n_str in enumerate(numbers):
                        num_val = set_num_as_float_or_int(n_str, silent=True)
                        fill_type = (
                            zfill_int if isinstance(num_val, int) else zfill_float
                        )
                        number_string += str(n_str).zfill(fill_type)
                        if i < len(numbers) - 1:
                            number_string += "-"

                elif file.volume_number != "":
                    # Handle single volume/chapter number
                    num_val = set_num_as_float_or_int(file.volume_number, silent=True)
                    fill_type = zfill_int if isinstance(num_val, int) else zfill_float
                    number_string = str(file.volume_number).zfill(fill_type)
                else:
                    number_string = ""  # Should not happen if volume_number check passed, but safety first

                rename += number_string

                # Add issue number for manga if enabled
                if (
                    add_issue_number_to_manga_file_name
                    and file.file_type == "volume"
                    and file.extension in manga_extensions
                    and number_string
                ):
                    rename += f" #{number_string}"

                # Add subtitle
                if file.subtitle:
                    rename += f" - {file.subtitle}"

                # Add year
                if file.volume_year:
                    rename += (
                        f" {modifiers.get(file.extension, '(%s)') % file.volume_year}"
                    )
                    # Remove year from extras if it was added
                    file.extras = [
                        item
                        for item in file.extras
                        if not (
                            str(file.volume_year) in item
                            or similar(item.strip("()[]{} "), str(file.volume_year))
                            >= 0.95
                            or re.search(r"([\[\(\{]\d{4}[\]\)\}])", item)
                        )
                    ]

                # Add publisher
                publisher_name = file.publisher.from_meta or file.publisher.from_name
                if publisher_name and add_publisher_name_to_file_name_when_renaming:
                    # Remove publisher from extras
                    for item in file.extras[:]:
                        item_clean = re.sub(r"[\(\)\[\]{}]", "", item).strip()
                        if (
                            similar(item_clean, publisher_name)
                            >= publisher_similarity_score
                        ):
                            try:
                                file.extras.remove(item)
                            except ValueError:
                                pass  # Item might have already been removed
                    rename += (
                        f" {modifiers.get(file.extension, '[%s]') % publisher_name}"
                    )

                # Add premium tag
                if file.is_premium and search_and_add_premium_to_file_name:
                    rename += f" {modifiers.get(file.extension, '[%s]') % 'Premium'}"
                    file.extras = [
                        item for item in file.extras if "premium" not in item.lower()
                    ]

                # Handle release group placement
                release_group_to_add = ""
                if file.release_group:
                    # Check if it should be moved to the end
                    if move_release_group_to_end_of_file_name:
                        release_group_to_add = file.release_group
                        # Remove release group from extras if found
                        for item in file.extras[:]:
                            item_clean = re.sub(r"[\(\)\[\]{}]", "", item).strip()
                            if (
                                similar(item_clean, file.release_group)
                                >= release_group_similarity_score
                            ):
                                try:
                                    file.extras.remove(item)
                                except ValueError:
                                    pass
                    # If not moving to end, add it now if not already present
                    elif not re.search(
                        rf"[\[\({{\)]{re.escape(file.release_group)}[\]\)}}\)]",
                        rename,
                        re.IGNORECASE,
                    ):
                        rename += f" {modifiers.get(file.extension, '[%s]') % file.release_group}"
                        # Also remove from extras
                        for item in file.extras[:]:
                            item_clean = re.sub(r"[\(\)\[\]{}]", "", item).strip()
                            if (
                                similar(item_clean, file.release_group)
                                >= release_group_similarity_score
                            ):
                                try:
                                    file.extras.remove(item)
                                except ValueError:
                                    pass

                # Add remaining extras
                if file.extras:
                    extras_to_add = [
                        extra
                        for extra in file.extras
                        if not re.search(
                            re.escape(extra.strip("()[]{} ")), rename, re.IGNORECASE
                        )
                    ]
                    if extras_to_add:
                        rename += " " + " ".join(extras_to_add)

                # Add release group at the end if flagged
                if release_group_to_add and not re.search(
                    rf"[\[\({{\)]{re.escape(release_group_to_add)}[\]\)}}\)]",
                    rename,
                    re.IGNORECASE,
                ):
                    rename += f" {modifiers.get(file.extension, '[%s]') % release_group_to_add}"

                # Final cleanup
                rename = rename.replace(
                    "*", ""
                )  # Remove potential wildcards if base_dir had them
                rename += file.extension
                rename = remove_dual_space(
                    rename
                ).strip()  # Use imported string_utils function

                # Handle unicode and problematic characters
                if replace_unicode_when_restructuring and contains_unicode(
                    rename
                ):  # Use imported string_utils function
                    rename = unidecode(rename)  # Use imported unidecode
                rename = (
                    rename.replace('"', "'").replace("/", "-").replace(":", " - ")
                )  # Replace problematic chars
                rename = remove_dual_space(rename).strip()  # Clean spaces again

                # --- Renaming Logic ---
                if file.name != rename:
                    rename_path = os.path.join(file.root, rename)

                    if watchdog_toggle:
                        # Update transferred files list for watchdog
                        if rename_path not in transferred_files:
                            transferred_files.append(rename_path)
                        if file.path in transferred_files:
                            transferred_files.remove(file.path)

                    try:
                        send_message(f"\n\t\tBEFORE: {file.name}", discord=False)
                        send_message(f"\t\tAFTER:  {rename}", discord=False)

                        user_input = (
                            get_input_from_user(
                                "\t\tReorganize & Rename", ["y", "n"], ["y", "n"]
                            )
                            if manual_rename
                            else "y"
                        )

                        if user_input == "y":
                            if not os.path.isfile(rename_path):
                                rename_status = rename_file(
                                    file.path, rename_path, silent=True
                                )  # Use imported file_utils function

                                if not rename_status:
                                    send_message(
                                        f"Failed to rename {file.name} to {rename}",
                                        error=True,
                                    )
                                    # Revert watchdog changes if rename failed
                                    if watchdog_toggle:
                                        if rename_path in transferred_files:
                                            transferred_files.remove(rename_path)
                                        if file.path not in transferred_files:
                                            transferred_files.append(file.path)
                                    continue  # Skip to next file

                                send_message(
                                    "\t\t\tSuccessfully reorganized & renamed file.\n",
                                    discord=False,
                                )

                                # Add to processed files list (used elsewhere)
                                if rename not in processed_files:
                                    processed_files.append(rename)

                                # Discord notification
                                if not mute_discord_rename_notifications:
                                    embed = handle_fields(  # Use imported discord_utils function
                                        DiscordEmbed(
                                            title="Reorganized & Renamed File",
                                            color=grey_color,
                                        ),  # Use imported discord_utils function
                                        fields=[
                                            {
                                                "name": "From",
                                                "value": f"```{file.name}```",
                                                "inline": False,
                                            },
                                            {
                                                "name": "To",
                                                "value": f"```{rename}```",
                                                "inline": False,
                                            },
                                            {
                                                "name": "Location",
                                                "value": f"```{file.root}```",
                                                "inline": False,
                                            },
                                        ],
                                    )
                                    grouped_notifications = group_notification(
                                        grouped_notifications, Embed(embed, None)
                                    )  # Use imported discord_utils and models

                                # Update the file object in the list being iterated over
                                # Find index first to avoid issues if list was copied
                                try:
                                    idx = files.index(file)
                                    # Create a new Volume object with updated info
                                    # This requires upgrade_to_volume_class and upgrade_to_file_class
                                    new_volume_list = upgrade_to_volume_class(
                                        upgrade_to_file_class([rename], file.root)
                                    )
                                    if new_volume_list:
                                        files[idx] = new_volume_list[0]
                                except (ValueError, IndexError):
                                    send_message(
                                        f"Could not find/update file object for {file.name} after rename.",
                                        error=True,
                                    )

                            else:
                                # Target file already exists, delete source
                                print(
                                    f"\t\tFile already exists: {rename_path}. Deleting source: {file.name}"
                                )
                                remove_file(
                                    file.path, silent=True
                                )  # Use imported file_utils function
                                # Remove original from files list if iterating over a copy
                                try:
                                    files.remove(file)
                                except ValueError:
                                    pass
                        else:
                            print("\t\t\tSkipping rename...\n")
                            # Add original to processed_files if skipped
                            if file.name not in processed_files:
                                processed_files.append(file.name)

                    except OSError as ose:
                        send_message(f"OS Error during rename: {ose}", error=True)
                        # Revert watchdog changes on error
                        if watchdog_toggle:
                            if rename_path in transferred_files:
                                transferred_files.remove(rename_path)
                            if file.path not in transferred_files:
                                transferred_files.append(file.path)
                else:
                    # File name didn't change, add original to processed
                    if file.name not in processed_files:
                        processed_files.append(file.name)

            else:
                # Filename didn't match the initial keyword/number pattern
                # Add original to processed_files
                if file.name not in processed_files:
                    processed_files.append(file.name)

        except Exception as e:
            send_message(
                f"Error in reorganize_and_rename for file {file.name}: {e}", error=True
            )
            # Ensure file is added to processed_files even on error to prevent reprocessing loops
            if file.name not in processed_files:
                processed_files.append(file.name)

    return files  # Return the potentially updated list of file objects
