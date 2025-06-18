"""Encapsulates all direct interactions with the file system.

This module provides a layer of abstraction for file operations, making the
rest of the code independent of the underlying file system implementation.
"""

import os
import shutil
import time
from typing import List, Optional
import datetime

import scandir


def get_all_folders_recursively_in_dir(dir_path: str) -> List[dict]:
    """Scans a directory and returns a list of all sub-folders.
    Args:
        dir_path (str): The path to the directory to scan.
    Returns:
        List[dict]: A list of dictionaries, each containing folder information.
    """
    results = []
    for root, dirs, files in scandir.walk(dir_path):
        # Assuming `download_folders` and `paths` are defined elsewhere,
        # possibly in a config file.
        # if root in download_folders + paths:
        #     continue
        folder_info = {"root": root, "dirs": dirs, "files": files}
        results.append(folder_info)
    return results


def get_all_files_in_directory(dir_path: str) -> List[str]:
    """Gets all files in a given directory (recursive).
    Args:
        dir_path (str): The path to the directory.
    Returns:
        List[str]: A list of filenames.
    """
    results = []
    for root, dirs, files in scandir.walk(dir_path):
        # Assuming these functions are defined elsewhere to handle file filtering.
        # files = remove_hidden_files(files)
        # files = remove_unaccepted_file_types(files, root, file_extensions)
        results.extend(files)
    return results


def write_to_file(
    file: str,
    message: str,
    without_timestamp: bool = False,
    overwrite: bool = False,
    check_for_dup: bool = False,
    write_to: Optional[str] = None,
    can_write_log: bool = True,
) -> bool:
    """Writes content to a file.
    Args:
        file (str): The name of the file.
        message (str): The content to write.
        without_timestamp (bool): Whether to exclude a timestamp.
        overwrite (bool): Whether to overwrite the file if it exists.
        check_for_dup (bool): Whether to check for duplicate messages.
        write_to (str, optional): The directory to write to. Defaults to LOGS_DIR.
        can_write_log (bool): Whether logging is enabled.
    Returns:
        bool: True if the write was successful, False otherwise.
    """
    # Assuming LOGS_DIR is defined in a config file.
    # logs_dir_loc = write_to or LOGS_DIR
    logs_dir_loc = write_to or "."  # Simplified for now
    if not os.path.exists(logs_dir_loc):
        os.makedirs(logs_dir_loc)

    if can_write_log and logs_dir_loc:
        log_file_path = os.path.join(logs_dir_loc, file)
        # Simplified logic, original depends on check_text_file_for_message
        if check_for_dup and os.path.isfile(log_file_path):
            with open(log_file_path, "r") as f:
                if message in f.read():
                    return False

        append_write = "a" if os.path.exists(log_file_path) and not overwrite else "w"
        with open(log_file_path, append_write) as f:
            if without_timestamp:
                f.write(f"\n {message}")
            else:
                now = datetime.datetime.now()
                dt_string = now.strftime("%d/%m/%Y %H:%M:%S")
                f.write(f"\n{dt_string} {message}")
        return True
    return False


def get_lines_from_file(
    file_path: str, ignore: List[str] = [], check_paths: bool = False
) -> List[str]:
    """Reads all lines from a file.
    Args:
        file_path (str): The path to the file.
        ignore (List[str]): A list of lines to ignore.
        check_paths (bool): Whether to check if lines start with paths from a predefined list.
    Returns:
        List[str]: A list of lines from the file.
    """
    results = []
    try:
        with open(file_path, "r") as file:
            for line in file:
                line = line.strip()
                if not line or line in ignore:
                    continue
                # Assuming `paths` is defined elsewhere for check_paths
                # if check_paths and paths and not line.startswith(tuple(paths)):
                #     continue
                results.append(line)
    except FileNotFoundError:
        return []
    return results


def remove_file(filepath: str, silent: bool = False) -> bool:
    """Removes a file.
    Args:
        filepath (str): The path to the file to remove.
        silent (bool): If True, suppresses output messages.
    Returns:
        bool: True if the file was removed, False otherwise.
    """
    if not os.path.isfile(filepath):
        if not silent:
            print(f"{filepath} is not a file.")
        return False
    try:
        os.remove(filepath)
    except OSError:
        return False
    return not os.path.isfile(filepath)


def move_file(source: str, destination: str) -> bool:
    """Moves a file from a source to a destination.
    Args:
        source (str): The source file path.
        destination (str): The destination file path.
    Returns:
        bool: True if the move was successful, False otherwise.
    """
    try:
        if os.path.isfile(source):
            shutil.move(source, destination)
            return os.path.isfile(os.path.join(destination, os.path.basename(source)))
    except OSError:
        return False
    return False


def rename_file(old_path: str, new_path: str) -> bool:
    """Renames a file.
    Args:
        old_path (str): The original file path.
        new_path (str): The new file path.
    Returns:
        bool: True if the rename was successful, False otherwise.
    """
    if os.path.isfile(old_path):
        try:
            os.rename(old_path, new_path)
            return os.path.isfile(new_path)
        except Exception:
            return False
    return False


def is_file_transferred(filepath: str) -> bool:
    """Checks if a file has been fully transferred by checking its size over a short interval.
    Args:
        filepath (str): The path to the file.
    Returns:
        bool: True if the file is fully transferred, False otherwise.
    """
    if not os.path.isfile(filepath):
        return False
    try:
        before_file_size = os.path.getsize(filepath)
        time.sleep(1)  # watchdog_file_transferred_check_interval
        after_file_size = os.path.getsize(filepath)
        return before_file_size == after_file_size
    except Exception:
        return False


def get_modification_date(filepath: str) -> Optional[datetime.datetime]:
    """Gets the last modification date of a file.
    Args:
        filepath (str): The path to the file.
    Returns:
        datetime.datetime: The last modification date, or None if an error occurs.
    """
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
    except Exception:
        return None