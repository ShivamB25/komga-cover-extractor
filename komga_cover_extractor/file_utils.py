# komga_cover_extractor/file_utils.py
import os
import time
import scandir
import filetype
import hashlib
import zipfile
import shutil
import re

# Assuming these will be moved to config.py or other utils
# Import necessary config variables
from .config import (
    watchdog_file_transferred_check_interval,
    download_folders,
    paths,
    file_extensions,
    image_extensions,
    compress_image_option,
    ignored_folder_names,
    check_for_existing_series_toggle,
    cached_paths,
    series_cover_file_names,
    use_latest_volume_cover_as_series_cover,
    manual_rename,
    mute_discord_rename_notifications,
    watchdog_toggle,
    transferred_files,
    transferred_dirs,
    rar_extensions,
    chapter_support_toggle,
    required_similarity_score,
)

# Import functions from other utility modules
from .log_utils import send_message, write_to_file, check_text_file_for_message
from .misc_utils import get_input_from_user  # Import directly
from .string_utils import (  # Import directly
    is_volume_one,
    filter_non_chapters,
    clean_str,
    remove_brackets,
    contains_volume_keywords,
    contains_chapter_keywords,  # Needed by placeholders
)

# Import models used in this module
from .models import File, Folder, Volume  # Added Volume import

# --- Helper Functions (Kept local for now, review later) ---


# Caches the given path and writes it to a file. Used by clean_and_sort.
def cache_path(path):
    """Appends a path to the cached_paths list and writes it to the cache file."""
    if path in paths + download_folders:  # Use imported config value
        return

    global cached_paths  # Use imported config value
    if path not in cached_paths:
        cached_paths.append(path)
        write_to_file(  # Use imported log_utils function
            "cached_paths.txt",
            path,
            without_timestamp=True,
            check_for_dup=True,
        )


# Note: Kept here for now, consider moving to misc_utils later.
# Determies if two index_numbers are the same
def is_same_index_number(index_one, index_two, allow_array_match=False):
    """Checks if two index numbers are the same, optionally allowing array matching."""
    if (index_one == index_two and index_one != "") or (
        allow_array_match
        and (
            (isinstance(index_one, list) and index_two in index_one)
            or (isinstance(index_two, list) and index_one in index_two)
        )
    ):
        return True
    return False


# Note: Kept here for now, consider moving to string_utils later.
# Checks if the passed string is a volume one.
def is_volume_one(volume_name):
    """Checks if the volume name indicates volume one."""
    # Placeholder implementation - requires volume_regex_keywords from config/string_utils
    # and contains_chapter_keywords from string_utils
    # For now, return False
    return False  # TODO: Implement fully with dependencies


# Note: Kept here for now, consider moving to string_utils later.
# Removes all chapter releases
def filter_non_chapters(files):
    """Filters a list of filenames, removing those identified as chapters."""
    # Placeholder implementation - requires contains_chapter_keywords, contains_volume_keywords
    # from string_utils
    # For now, return the original list
    return files  # TODO: Implement fully with dependencies


# Note: Kept here for now, consider moving to string_utils later.
# Cleans the string by removing punctuation, bracketed info, etc.
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
    """Cleans a string based on various flags."""
    # Placeholder - requires unidecode, normalize_str, remove_punctuation, remove_s, convert_to_ascii, replace_underscores
    s = string.lower().strip() if not skip_lowercase_convert else string
    # TODO: Implement full cleaning logic
    return s.strip()


# Note: Kept here for now, consider moving to string_utils later.
# Removes bracketed content from the string
def remove_brackets(string):
    """Removes bracketed content from a string."""
    # Placeholder - requires complex regex from original script
    # TODO: Implement full bracket removal logic
    return re.sub(r"\(.*?\)|\[.*?\]|\{.*?\}", "", string).strip()


# Note: Kept here for now, consider moving to misc_utils later.
# Gets user input with optional timeout.
def get_input_from_user(
    prompt, acceptable_values=[], example=None, timeout=90, use_timeout=False
):
    """Gets validated input from the user."""
    # Placeholder implementation
    # TODO: Implement fully with threading if needed, or simplify for basic input
    response = input(f"{prompt} ({'/'.join(acceptable_values)}): ")
    if acceptable_values and response not in acceptable_values:
        return None
    return response


# --- File System Functions ---


def get_modification_date(path):
    """Gets the modification time of a file or directory."""
    try:
        return os.path.getmtime(path)
    except OSError as e:
        send_message(f"ERROR getting modification date for {path}: {e}", error=True)
        return 0


def set_modification_date(file_path, date):
    """Sets the modification time of a file."""
    try:
        # os.utime requires access time and modification time
        access_time = get_modification_date(
            file_path
        )  # Keep access time same as current mod time
        os.utime(file_path, (access_time, date))
    except Exception as e:
        send_message(
            f"ERROR: Could not set modification date of {file_path}\nERROR: {e}",
            error=True,
        )


def is_file_transferred(file_path):
    """Checks if a file is fully transferred by comparing size over time."""
    if not os.path.isfile(file_path):
        return False
    try:
        before_file_size = os.path.getsize(file_path)
        time.sleep(
            watchdog_file_transferred_check_interval
        )  # Use imported config value
        # Check again if file still exists after sleep
        if not os.path.isfile(file_path):
            return False  # File was deleted during sleep
        after_file_size = os.path.getsize(file_path)
        return before_file_size == after_file_size
    except Exception as e:
        send_message(f"ERROR in is_file_transferred({file_path}): {e}", error=True)
        return False


def get_file_size(file_path):
    """Gets the size of a file in bytes."""
    if os.path.isfile(file_path):
        try:
            return os.stat(file_path).st_size
        except OSError as e:
            send_message(f"ERROR getting size for {file_path}: {e}", error=True)
            return None
    else:
        return None


def get_file_extension(file):
    """Gets the file extension from a filename."""
    return os.path.splitext(file)[1]


def get_extensionless_name(file):
    """Gets the filename without its extension."""
    return os.path.splitext(file)[0]


def get_header_extension(file):
    """Guesses the file extension based on its header."""
    extension_from_name = get_file_extension(file)
    # Only guess for archive types that might be mislabeled
    if (
        extension_from_name in manga_extensions + rar_extensions
    ):  # Use imported config values
        try:
            kind = filetype.guess(file)
            if kind is None:
                return None
            guessed_ext = f".{kind.extension}"
            if guessed_ext in manga_extensions:
                return ".cbz"  # Standardize zip-based comics to cbz
            elif guessed_ext in rar_extensions:
                return ".cbr"  # Standardize rar-based comics to cbr
            else:
                # Return the guessed extension if it's something else (e.g., .pdf)
                # but only if it's in the globally accepted file_extensions
                return guessed_ext if guessed_ext in file_extensions else None
        except Exception as e:
            send_message(f"Error guessing file type for {file}: {e}", error=True)
            return None
    else:
        # If it's already a known non-archive type (like .epub), trust the extension
        return extension_from_name if extension_from_name in file_extensions else None


def remove_hidden_files(files):
    """Removes hidden files (starting with '.') from a list."""
    return [x for x in files if not x.startswith(".")]


def remove_unaccepted_file_types(files, root, accepted_extensions, test_mode=False):
    """Removes files with unaccepted extensions."""
    return [
        file
        for file in files
        if get_file_extension(file) in accepted_extensions
        and (test_mode or os.path.isfile(os.path.join(root, file)))
    ]


def remove_ignored_folders(dirs):
    """Removes folder names present in the ignored_folder_names list."""
    return [
        x for x in dirs if x not in ignored_folder_names
    ]  # Use imported config value


def remove_hidden_folders(dirs):
    """Removes hidden folders (starting with '.') from a list."""
    return [x for x in dirs if not x.startswith(".")]


def clean_and_sort(
    root,
    files=[],
    dirs=[],
    sort=False,
    chapters=chapter_support_toggle,  # Use imported config value
    just_these_files=[],
    just_these_dirs=[],
    skip_remove_ignored_folders=False,
    skip_remove_hidden_files=False,
    skip_remove_unaccepted_file_types=False,
    skip_remove_hidden_folders=False,
    keep_images_in_just_these_files=False,
    is_correct_extensions_feature=[],
    test_mode=False,
):
    """Cleans and sorts lists of files and directories based on various criteria."""
    # Cache the root path if applicable
    if (
        check_for_existing_series_toggle  # Use imported config value
        and not test_mode
        and root
        not in cached_paths + download_folders + paths  # Use imported config values
        and not any(root.startswith(path) for path in download_folders)
    ):
        cache_path(root)  # Use local function

    # Remove ignored folder names if present in the root path itself
    if (
        ignored_folder_names and not skip_remove_ignored_folders
    ):  # Use imported config value
        ignored_parts = any(
            part for part in root.split(os.sep) if part in ignored_folder_names
        )
        if ignored_parts:
            return [], []  # Return empty lists if root is within an ignored folder path

    # Sort files and directories if requested
    if sort:
        files.sort()
        dirs.sort()

    # Process files
    if files:
        if not skip_remove_hidden_files:
            files = remove_hidden_files(files)  # Use local function
        if not skip_remove_unaccepted_file_types and files:
            accepted_ext = (
                file_extensions  # Use imported config value
                if not is_correct_extensions_feature
                else is_correct_extensions_feature
            )
            files = remove_unaccepted_file_types(
                files, root, accepted_ext, test_mode=test_mode
            )  # Use local function
        if just_these_files and files:
            files = [
                x
                for x in files
                if os.path.join(root, x) in just_these_files
                or (
                    keep_images_in_just_these_files
                    and get_file_extension(x)
                    in image_extensions  # Use imported config value
                )
            ]
        if not chapters and files:
            files = filter_non_chapters(files)  # Use local placeholder

    # Process directories
    if dirs:
        if not skip_remove_hidden_folders:
            dirs = remove_hidden_folders(dirs)  # Use local function
        if not skip_remove_ignored_folders and dirs:
            dirs = remove_ignored_folders(dirs)  # Use local function
        # Filter dirs based on just_these_dirs (similar logic to files if needed)
        # Not implemented in original, assuming not needed unless specified

    return files, dirs


def process_files_and_folders(
    root,
    files=[],
    dirs=[],
    sort=False,
    chapters=chapter_support_toggle,  # Use imported config value
    just_these_files=[],
    just_these_dirs=[],
    skip_remove_unaccepted_file_types=False,
    keep_images_in_just_these_files=False,
    is_correct_extensions_feature=[],
    test_mode=False,
):
    """Wrapper for clean_and_sort, handling watchdog-specific filtering."""
    in_download_folders = (
        watchdog_toggle  # Use imported config value
        and download_folders  # Use imported config value
        and any(x for x in download_folders if root.startswith(x))
    )

    # Apply just_these filters only if the root is within a download folder monitored by watchdog
    effective_just_files = just_these_files if in_download_folders else []
    effective_just_dirs = just_these_dirs if in_download_folders else []
    effective_keep_images = (
        keep_images_in_just_these_files if in_download_folders else False
    )

    files, dirs = clean_and_sort(  # Use local function
        root,
        files,
        dirs,
        sort=sort,
        chapters=chapters,
        just_these_files=effective_just_files,
        just_these_dirs=effective_just_dirs,
        skip_remove_unaccepted_file_types=skip_remove_unaccepted_file_types,
        keep_images_in_just_these_files=effective_keep_images,
        is_correct_extensions_feature=is_correct_extensions_feature,
        test_mode=test_mode,
    )
    return files, dirs


def get_all_folders_recursively_in_dir(dir_path):
    """Recursively gets all folders in a directory, excluding base paths."""
    results = []
    base_paths = set(download_folders + paths)  # Use imported config values
    try:
        for root, dirs, files in scandir.walk(dir_path):
            # Skip the initial paths provided by the user
            if root in base_paths:
                continue
            # Basic filtering (can enhance with clean_and_sort logic if needed)
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".") and d not in ignored_folder_names
            ]  # Use imported config value
            folder_info = {"root": root, "dirs": dirs, "files": files}
            results.append(folder_info)
    except Exception as e:
        send_message(f"Error walking directory {dir_path}: {e}", error=True)
    return results


def get_all_files_in_directory(dir_path):
    """Recursively gets all accepted files in a directory."""
    results = []
    try:
        for root, dirs, files in scandir.walk(dir_path):
            # Use clean_and_sort for consistent filtering
            cleaned_files, _ = clean_and_sort(
                root, files=files, dirs=dirs
            )  # Use local function
            results.extend([os.path.join(root, f) for f in cleaned_files])
    except Exception as e:
        send_message(f"Error walking directory {dir_path}: {e}", error=True)
    return results


def get_all_files_recursively_in_dir_watchdog(dir_path):
    """Recursively gets files for watchdog, excluding images unless needed."""
    results = []
    try:
        for root, dirs, files in scandir.walk(dir_path):
            files = remove_hidden_files(files)  # Use local function
            for file in files:
                file_path = os.path.join(root, file)
                if file_path not in results:
                    extension = get_file_extension(file_path)  # Use local function
                    is_image = (
                        extension in image_extensions
                    )  # Use imported config value
                    # Include non-images, or images only if compression is off AND it's an existing library path
                    is_existing_lib_path = any(
                        dir_path.startswith(p) for p in paths
                    )  # Use imported config value
                    if not is_image or (
                        not compress_image_option and is_existing_lib_path
                    ):  # Use imported config value
                        results.append(file_path)
    except Exception as e:
        send_message(
            f"Error walking directory {dir_path} for watchdog: {e}", error=True
        )
    return results


def get_file_hash(file, is_internal=False, internal_file_name=None):
    """Calculates the SHA256 hash of a file or a file within a zip."""
    try:
        BUF_SIZE = 65536  # 64KB buffer size
        hash_obj = hashlib.sha256()

        if is_internal:
            with zipfile.ZipFile(file) as zip_f:
                with zip_f.open(internal_file_name) as internal_file:
                    while True:
                        data = internal_file.read(BUF_SIZE)
                        if not data:
                            break
                        hash_obj.update(data)
        else:
            with open(file, "rb") as f:
                while True:
                    data = f.read(BUF_SIZE)
                    if not data:
                        break
                    hash_obj.update(data)
        return hash_obj.hexdigest()
    except FileNotFoundError as e:
        send_message(f"Error hashing: File not found - {e}", error=True)
        return None
    except KeyError as e:  # File not found in zip
        send_message(f"Error hashing: File not found in zip - {e}", error=True)
        return None
    except Exception as e:
        send_message(f"Error hashing file {file}: {e}", error=True)
        return None


def remove_file(full_file_path, silent=False):
    """Removes a file and optionally its associated images."""
    global grouped_notifications  # Use imported config value
    if not os.path.isfile(full_file_path):
        if not silent:
            send_message(
                f"{full_file_path} is not a file or does not exist.", error=True
            )
        return False
    try:
        os.remove(full_file_path)
        if os.path.isfile(full_file_path):  # Check if removal failed
            if not silent:
                send_message(
                    f"Failed to remove {full_file_path}.", error=True
                )  # Use imported log_utils function
            return False
        if not silent:
            send_message(
                f"File removed: {full_file_path}", discord=False
            )  # Use imported log_utils function
            # Discord notification responsibility moved to calling function or discord_utils
        # If the file is not an image, remove associated images
        if (
            get_file_extension(full_file_path) not in image_extensions
        ):  # Use local function / imported config value
            remove_images(full_file_path)  # Use local function
        return True
    except OSError as e:
        if not silent:
            send_message(f"Failed to remove {full_file_path}: {e}", error=True)
        return False


def remove_images(path):
    """Removes associated cover/poster images for a given file path."""
    base_path_no_ext = get_extensionless_name(path)  # Use local function
    dir_path = os.path.dirname(path)

    # Remove volume cover (e.g., file_name.jpg)
    for ext in image_extensions:  # Use imported config value
        volume_cover = f"{base_path_no_ext}{ext}"
        if os.path.isfile(volume_cover):
            remove_file(volume_cover, silent=True)  # Use local function

    # Remove series cover (e.g., cover.jpg) under specific conditions
    series_cover_path = None
    for name in series_cover_file_names:  # Use imported config value
        for ext in image_extensions:
            potential_cover = os.path.join(dir_path, f"{name}{ext}")
            if os.path.isfile(potential_cover):
                series_cover_path = potential_cover
                break
        if series_cover_path:
            break

    if series_cover_path:
        volume_cover_for_series = f"{base_path_no_ext}{get_file_extension(series_cover_path)}"  # Use local function
        is_vol_one = is_volume_one(os.path.basename(path))  # Use local placeholder

        should_remove = False
        if (
            not use_latest_volume_cover_as_series_cover and is_vol_one
        ):  # Use imported config value
            should_remove = True
        elif use_latest_volume_cover_as_series_cover and os.path.isfile(
            volume_cover_for_series
        ):
            # Simplified check: remove if modification times or hashes match
            try:
                if get_modification_date(series_cover_path) == get_modification_date(
                    volume_cover_for_series
                ) or get_file_hash(series_cover_path) == get_file_hash(
                    volume_cover_for_series
                ):  # Use local functions
                    should_remove = True
            except Exception as e:
                send_message(
                    f"Error comparing series/volume covers for removal: {e}", error=True
                )

        if should_remove:
            remove_file(series_cover_path, silent=True)  # Use local function


def delete_hidden_files(files, root):
    """Deletes hidden files from a directory."""
    for file in files:
        path = os.path.join(root, file)
        if file.startswith(".") and os.path.isfile(path):
            remove_file(path, silent=True)  # Use local function


def check_and_delete_empty_folder(folder):
    """Checks if a folder is effectively empty and deletes it."""
    if not os.path.isdir(folder):
        return
    try:
        # print(f"\t\tChecking for empty folder: {folder}") # Debugging print
        folder_contents = os.listdir(folder)
        delete_hidden_files(folder_contents, folder)  # Use local function

        # Re-list after deleting hidden files
        folder_contents = [
            item for item in os.listdir(folder) if not item.startswith(".")
        ]

        # Check if it contains subfolders
        if any(os.path.isdir(os.path.join(folder, item)) for item in folder_contents):
            return

        # Check if only a cover/poster file remains
        if len(folder_contents) == 1:
            item_name = folder_contents[0]
            item_path = os.path.join(folder, item_name)
            base_name, ext = os.path.splitext(item_name)
            if (
                base_name in series_cover_file_names and ext in image_extensions
            ):  # Use imported config values
                remove_file(item_path, silent=True)  # Use local function
                folder_contents = []  # Update contents list

        # Check if the folder is now empty and not a base path
        if (
            not folder_contents and folder not in paths + download_folders
        ):  # Use imported config values
            try:
                os.rmdir(folder)
                # print(f"\t\t\tFolder removed: {folder}") # Debugging print
            except OSError as e:
                send_message(f"Failed to remove empty folder {folder}: {e}", error=True)
    except Exception as e:
        send_message(f"Error checking/deleting empty folder {folder}: {e}", error=True)


def move_images(
    file_obj,  # Expecting a File object (or similar with path attributes)
    folder_name,
    highest_index_num="",
    is_chapter_dir=False,
):
    """Moves associated image files for a given file object."""
    # Move volume cover (e.g., file_name.jpg)
    for extension in image_extensions:  # Use imported config value
        image_path = file_obj.extensionless_path + extension
        if os.path.isfile(image_path):
            dest_image_path = os.path.join(folder_name, os.path.basename(image_path))
            try:
                if os.path.isfile(dest_image_path):
                    remove_file(dest_image_path, silent=True)  # Use local function
                shutil.move(image_path, dest_image_path)
            except Exception as e:
                send_message(
                    f"Error moving volume cover {image_path} to {folder_name}: {e}",
                    error=True,
                )

    # Move series cover (e.g., cover.jpg)
    for cover_base_name in series_cover_file_names:  # Use imported config value
        for extension in image_extensions:
            series_cover_src = os.path.join(
                file_obj.root, f"{cover_base_name}{extension}"
            )
            if os.path.isfile(series_cover_src):
                series_cover_dest = os.path.join(
                    folder_name, f"{cover_base_name}{extension}"
                )
                try:
                    # Logic for deciding whether to overwrite or keep existing series cover
                    should_move = True
                    if os.path.isfile(series_cover_dest):
                        is_vol_one = is_volume_one(
                            file_obj.name
                        )  # Use local placeholder
                        is_highest = (
                            is_same_index_number(
                                file_obj.index_number,
                                highest_index_num,
                                allow_array_match=True,
                            )
                            if hasattr(file_obj, "index_number")
                            else False
                        )  # Use local placeholder

                        if (
                            use_latest_volume_cover_as_series_cover
                            and hasattr(file_obj, "file_type")
                            and file_obj.file_type == "volume"
                            and is_highest
                        ):  # Use imported config value
                            # Compare hashes/mod times before overwriting
                            src_mod_time = get_modification_date(
                                series_cover_src
                            )  # Use local function
                            dest_mod_time = get_modification_date(series_cover_dest)
                            if src_mod_time == dest_mod_time:
                                should_move = False  # Keep existing if mod times match
                            else:
                                src_hash = get_file_hash(
                                    series_cover_src
                                )  # Use local function
                                dest_hash = get_file_hash(series_cover_dest)
                                if src_hash == dest_hash:
                                    # Update mod time of dest if hashes match but times differ
                                    set_modification_date(
                                        series_cover_dest, src_mod_time
                                    )  # Use local function
                                    should_move = False  # Keep existing
                                else:
                                    remove_file(
                                        series_cover_dest, silent=True
                                    )  # Remove old if hashes differ
                        elif not use_latest_volume_cover_as_series_cover and is_vol_one:
                            remove_file(
                                series_cover_dest, silent=True
                            )  # Overwrite if using vol 1 logic
                        else:
                            should_move = False  # Don't move if conditions aren't met

                    if should_move:
                        shutil.move(series_cover_src, series_cover_dest)
                    elif os.path.isfile(
                        series_cover_src
                    ):  # If we decided not to move, remove the source
                        remove_file(series_cover_src, silent=True)

                except Exception as e:
                    send_message(
                        f"Error moving series cover {series_cover_src} to {folder_name}: {e}",
                        error=True,
                    )
                # Assuming only one series cover (cover.* or poster.*) exists per source dir
                break  # Stop checking extensions/names for series cover once one is processed


def remove_folder(folder):
    """Removes a folder and its contents."""
    result = False
    if os.path.isdir(folder) and (
        folder not in download_folders + paths
    ):  # Use imported config values
        try:
            shutil.rmtree(folder)
            if not os.path.isdir(folder):
                send_message(f"\t\t\tRemoved {folder}", discord=False)
                result = True
            else:
                send_message(f"\t\t\tFailed to remove {folder}", error=True)
        except Exception as e:
            send_message(f"\t\t\tFailed to remove {folder}: {str(e)}", error=True)
    return result


def move_folder(folder, new_location, silent=False):
    """Moves a folder to a new location."""
    global grouped_notifications  # Use imported config value
    result = False
    if os.path.isdir(folder):
        folder_name = os.path.basename(folder)
        new_folder_path = os.path.join(new_location, folder_name)
        try:
            if not os.path.isdir(new_folder_path):
                shutil.move(folder, new_location)
                if os.path.isdir(new_folder_path):
                    result = True
                    if not silent:
                        send_message(
                            f"\n\t\tMoved Folder: {folder_name} from {os.path.dirname(folder)} to {new_location}",
                            discord=False,
                        )  # Use imported log_utils function
                        # Discord notification responsibility moved to calling function or discord_utils
            else:
                send_message(
                    f"\t\tFolder already exists: {new_folder_path}", error=True
                )  # Use imported log_utils function
        except Exception as e:
            send_message(
                f"\t\tFailed to move folder: {folder_name} to {new_location} - {e}",
                error=True,
            )
    return result


def move_file(
    file_obj,  # Expecting a File object (or similar with path attributes)
    new_location,
    silent=False,
    highest_index_num="",
    is_chapter_dir=False,
):
    """Moves a file and its associated images to a new location."""
    global grouped_notifications  # Use imported config value
    if os.path.isfile(file_obj.path):
        new_file_path = os.path.join(new_location, file_obj.name)
        try:
            # Ensure destination directory exists
            os.makedirs(new_location, exist_ok=True)
            if not os.path.isfile(new_file_path):  # Avoid overwriting existing file
                shutil.move(file_obj.path, new_file_path)
                if os.path.isfile(new_file_path):
                    if not silent:
                        send_message(
                            f"\t\tMoved File: {file_obj.name} to {new_location}",
                            discord=False,
                        )  # Use imported log_utils function
                        # Discord notification responsibility moved to calling function or discord_utils
                    # Update file object path after move before moving images
                    original_path = file_obj.path
                    file_obj.path = new_file_path
                    file_obj.extensionless_path = get_extensionless_name(
                        new_file_path
                    )  # Use local function
                    file_obj.root = new_location
                    move_images(
                        file_obj, new_location, highest_index_num, is_chapter_dir
                    )  # Use local function
                    return True
                else:
                    send_message(
                        f"\t\tMove verification failed for: {new_file_path}", error=True
                    )
                    # Revert file object path if move failed verification
                    file_obj.path = original_path
                    file_obj.extensionless_path = get_extensionless_name(original_path)
                    file_obj.root = os.path.dirname(original_path)
                    return False
            else:
                send_message(
                    f"\t\tFile already exists at destination: {new_file_path}, skipping move.",
                    error=True,
                )
                return False
        except Exception as e:
            send_message(
                f"Error moving file {file_obj.name} to {new_location}: {e}", error=True
            )
            return False
    return False


def replace_file(old_file, new_file, highest_index_num=""):
    """Replaces old_file with new_file."""
    global grouped_notifications, moved_files  # Use imported config values
    result = False
    if os.path.isfile(old_file.path) and os.path.isfile(new_file.path):
        if remove_file(old_file.path):  # Use local function
            if move_file(
                new_file,
                old_file.root,
                silent=True,
                highest_index_num=highest_index_num,
            ):  # Use local function
                result = True
                # moved_files state is managed by the caller (e.g., core_logic or series_matching)
                # moved_files.append(os.path.join(old_file.root, new_file.name))
                send_message(
                    f"\t\tReplaced: {old_file.name} with {new_file.name} in {old_file.root}",
                    discord=False,
                )  # Use imported log_utils function
                # Discord notification responsibility moved to calling function or discord_utils
            else:
                send_message(
                    f"\tFailed to move new file: {new_file.name} after removing old file.",
                    error=True,
                )  # Use imported log_utils function
        else:
            send_message(
                f"\tFailed to remove old file: {old_file.name}. Replacement aborted.",
                error=True,
            )
    else:
        send_message(
            f"\tOne or both files missing for replacement: {old_file.name}, {new_file.name}",
            error=True,
        )
    return result


def normalize_path(path):
    """Normalizes path separators and removes Windows drive letters."""
    path = os.path.normpath(path)
    if ":" in path and os.path.isabs(
        path
    ):  # Check if it looks like a Windows absolute path
        path = path.split(":", 1)[1]  # Remove drive letter part
    return path.replace("\\", "/")


def is_root_present(root_path, target_path):
    """Checks if root_path is a prefix of target_path after normalization."""
    normalized_root = normalize_path(root_path)
    normalized_target = normalize_path(target_path)
    # Ensure comparison accounts for directory boundaries
    return (
        normalized_target.startswith(normalized_root + "/")
        or normalized_target == normalized_root
    )


def correct_file_extensions():
    """Checks and corrects file extensions based on headers."""
    global transferred_files, grouped_notifications  # Use imported config values

    print("\nChecking for incorrect file extensions...")
    if not download_folders:  # Use imported config value
        print("\tNo download folders specified.")
        return

    for folder in download_folders:
        if not os.path.isdir(folder):
            print(f"\t{folder} does not exist.")
            continue
        print(f"\t{folder}")
        for root, dirs, files in scandir.walk(folder):
            # Process only files relevant to watchdog if enabled
            files, dirs = process_files_and_folders(  # Use local function
                root,
                files,
                dirs,
                just_these_files=transferred_files,  # Use imported config value
                just_these_dirs=transferred_dirs,  # Use imported config value
                is_correct_extensions_feature=file_extensions
                + rar_extensions,  # Use imported config values
            )
            # Use upgrade_to_file_class to get header info efficiently
            volumes = upgrade_to_file_class(  # Use imported function (originally local)
                [f for f in files if os.path.isfile(os.path.join(root, f))],
                root,
                skip_get_header_extension=False,  # Ensure header check runs
                is_correct_extensions_feature=file_extensions + rar_extensions,
            )

            if not volumes:
                continue

            for volume in volumes:
                if (
                    not volume.header_extension
                    or volume.extension == volume.header_extension
                ):
                    continue

                print(
                    f"\n\t\t{volume.name}\n\t\t\tfile extension:   {volume.extension}\n\t\t\theader extension: {volume.header_extension}"
                )

                user_input = "y"  # Default to yes unless manual rename is on
                if manual_rename:  # Use imported config value
                    user_input = get_input_from_user(
                        "\t\t\tRename", ["y", "n"], ["y", "n"]
                    )  # Use local placeholder

                if user_input == "y":
                    new_path = f"{volume.extensionless_path}{volume.header_extension}"
                    if rename_file(
                        volume.path, new_path, silent=True
                    ):  # Use imported file_utils function
                        print("\t\t\tRenamed successfully")
                        if (
                            not mute_discord_rename_notifications
                        ):  # Use imported config value
                            # Discord notification responsibility moved
                            pass
                        if watchdog_toggle:  # Use imported config value
                            if volume.path in transferred_files:
                                transferred_files.remove(volume.path)
                            if new_path not in transferred_files:
                                transferred_files.append(new_path)
                    else:
                        print("\t\t\tRename failed.")
                else:
                    print("\t\t\tSkipped")
