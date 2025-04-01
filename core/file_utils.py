import os
import zipfile
import py7zr
import rarfile
import scandir
import filetype
import hashlib
import time
import re # Added for normalize_path
import shutil # Added for remove_folder

# Assuming settings are imported or passed as needed
from settings import (
    manga_extensions, rar_extensions, image_extensions, file_extensions,
    seven_zip_extensions, # Added
    ignored_folder_names, # Added
    watchdog_file_transferred_check_interval # Added for is_file_transferred
)

def get_file_extension(file):
    """Retrieves the file extension from a filename."""
    return os.path.splitext(file)[1]

def get_extensionless_name(file):
    """Returns the filename without its extension."""
    return os.path.splitext(file)[0]

def get_header_extension(file):
    """Gets the predicted file extension from the file header using filetype."""
    extension_from_name = get_file_extension(file)
    # Only check archives that might have misleading extensions
    if extension_from_name in manga_extensions or extension_from_name in rar_extensions:
        try:
            kind = filetype.guess(file)
            if kind is None:
                return None
            # Standardize common archive types
            elif f".{kind.extension}" in manga_extensions:
                return ".cbz" # Treat zip-based manga archives as cbz
            elif f".{kind.extension}" in rar_extensions:
                return ".cbr" # Treat rar-based manga archives as cbr
            else:
                return f".{kind.extension}"
        except Exception as e:
            print(f"Error guessing file type for {file}: {e}") # Basic logging
            return None
    else:
        # For non-archives or types we don't specifically check, return None
        return None

def get_file_size(file_path):
    """Gets the size of a file in bytes."""
    if os.path.isfile(file_path):
        try:
            file_info = os.stat(file_path)
            return file_info.st_size
        except OSError as e:
            print(f"Error getting size for {file_path}: {e}")
            return None
    else:
        return None

def remove_hidden_files(files):
    """Removes hidden files (starting with '.') from a list of filenames."""
    return [x for x in files if not x.startswith(".")]

def remove_unaccepted_file_types(files, root, accepted_extensions, test_mode=False):
    """Removes files with unaccepted extensions from a list."""
    accepted_set = set(accepted_extensions)
    return [
        file
        for file in files
        if get_file_extension(file) in accepted_set
        and (os.path.isfile(os.path.join(root, file)) or test_mode)
    ]

def extract(file_path, temp_dir, extension):
    """Extracts a supported archive to a temporary directory."""
    successfull = False
    try:
        print(f"Extracting {file_path} to {temp_dir}...") # Added print
        if extension in rar_extensions:
            with rarfile.RarFile(file_path) as rar:
                rar.extractall(temp_dir)
                successfull = True
        elif extension in seven_zip_extensions: # Use constant
            with py7zr.SevenZipFile(file_path, "r") as archive:
                archive.extractall(temp_dir)
                successfull = True
        if successfull:
             print("Extraction successful.")
        else:
             print("Extraction failed (unsupported extension or error).")
    except rarfile.BadRarFile:
         print(f"Error: Bad RAR file - {file_path}")
    except py7zr.exceptions.Bad7zFile:
         print(f"Error: Bad 7z file - {file_path}")
    except FileNotFoundError:
         print(f"Error: File not found - {file_path}")
    except Exception as e:
        print(f"Error extracting {file_path}: {e}") # Basic logging
    return successfull

def compress(temp_dir, cbz_filename):
    """Compresses a directory to a CBZ archive."""
    successfull = False
    try:
        print(f"Compressing {temp_dir} to {cbz_filename}...") # Added print
        with zipfile.ZipFile(cbz_filename, "w", zipfile.ZIP_DEFLATED) as zipf: # Use compression
            for root, dirs, files in scandir.walk(temp_dir):
                # Sort files for consistent order within CBZ
                files.sort()
                for file in files:
                    file_path = os.path.join(root, file)
                    # Arcname is the path inside the zip file
                    arcname = os.path.relpath(file_path, temp_dir)
                    zipf.write(file_path, arcname)
            successfull = True
        if successfull:
            print("Compression successful.")
        else:
            print("Compression failed.")
    except Exception as e:
        print(f"Error compressing {temp_dir}: {e}") # Basic logging
        # Clean up potentially incomplete cbz file
        if os.path.exists(cbz_filename):
            try:
                os.remove(cbz_filename)
            except OSError:
                pass
    return successfull


def contains_comic_info(zip_file):
    """Checks if a zip file contains a ComicInfo.xml file."""
    result = False
    if not zipfile.is_zipfile(zip_file): # Check if it's a valid zip first
        print(f"Warning: {zip_file} is not a valid zip file.")
        return False
    try:
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            # Case-insensitive check
            result = any(name.lower() == 'comicinfo.xml' for name in zip_ref.namelist())
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        print(f"Error checking ComicInfo for {zip_file}: {e}") # Basic logging
    except Exception as e: # Catch other potential zip errors
        print(f"Unexpected error checking ComicInfo for {zip_file}: {e}")
    return result

def get_file_hash(file, is_internal=False, internal_file_name=None):
    """Calculates the SHA256 hash of a file or a file within a zip archive."""
    try:
        BUF_SIZE = 65536  # 64KB buffer size
        hash_obj = hashlib.sha256()

        if is_internal:
            if not zipfile.is_zipfile(file):
                 print(f"Error: {file} is not a valid zip file for internal hashing.")
                 return None
            with zipfile.ZipFile(file) as zip_f:
                try:
                    with zip_f.open(internal_file_name) as internal_file:
                        while True:
                            data = internal_file.read(BUF_SIZE)
                            if not data:
                                break
                            hash_obj.update(data)
                except KeyError:
                     print(f"Error: Internal file '{internal_file_name}' not found in {file}")
                     return None
        else:
            if not os.path.isfile(file):
                 print(f"Error: File not found for hashing - {file}")
                 return None
            with open(file, "rb") as f:
                while True:
                    data = f.read(BUF_SIZE)
                    if not data:
                        break
                    hash_obj.update(data)

        return hash_obj.hexdigest()
    except FileNotFoundError:
        # This case should be caught by the is_internal check or the os.path.isfile check
        print(f"Error: File not found during hash - {file}")
        return None
    except Exception as e:
        print(f"Error hashing file {file}: {e}") # Basic logging
        return None

# --- New functions added ---

def get_all_folders_recursively_in_dir(dir_path, base_folders_to_ignore=[]):
    """Recursively gets all folder paths within a directory, ignoring base folders."""
    results = []
    try:
        for root, dirs, files in scandir.walk(dir_path):
            # Skip the initial base folders passed in
            if root in base_folders_to_ignore:
                continue
            # Add the current root (which is a folder)
            # Exclude hidden folders if necessary (optional, add check if needed)
            if not os.path.basename(root).startswith('.'):
                 results.append(root)
    except Exception as e:
        print(f"Error walking directory {dir_path}: {e}")
    return results

def get_all_files_in_directory(dir_path, accepted_extensions=None):
    """Recursively gets all file paths within a directory, optionally filtering by extension."""
    results = []
    accepted_set = set(accepted_extensions) if accepted_extensions else None
    try:
        for root, dirs, files in scandir.walk(dir_path):
            # Process files in the current directory
            for file in files:
                if file.startswith('.'): # Skip hidden files
                    continue
                if accepted_set:
                    if get_file_extension(file) in accepted_set:
                        results.append(os.path.join(root, file))
                else:
                    results.append(os.path.join(root, file))
    except Exception as e:
        print(f"Error walking directory {dir_path}: {e}")
    return results

def get_all_files_recursively_in_dir_watchdog(dir_path):
    """Recursively gets all non-image file paths for watchdog."""
    # This function seems specific, might need refinement based on actual watchdog needs
    results = []
    try:
        for root, dirs, files in scandir.walk(dir_path):
            files = remove_hidden_files(files) # Use existing util
            for file in files:
                file_path = os.path.join(root, file)
                if os.path.isfile(file_path): # Ensure it's a file
                    extension = get_file_extension(file_path)
                    if extension not in image_extensions: # Exclude images
                        results.append(file_path)
    except Exception as e:
        print(f"Error walking directory {dir_path} for watchdog: {e}")
    return results

def is_file_transferred(file_path):
    """Checks if the file is fully transferred by comparing size over an interval."""
    if not os.path.isfile(file_path):
        return False # File doesn't exist

    try:
        before_file_size = get_file_size(file_path)
        if before_file_size is None: return False # Error getting size

        time.sleep(watchdog_file_transferred_check_interval) # Use setting

        # Check if file still exists after sleep
        if not os.path.isfile(file_path):
             print(f"Warning: File {file_path} disappeared during transfer check.")
             return False # Treat as not transferred if it vanished

        after_file_size = get_file_size(file_path)
        if after_file_size is None: return False # Error getting size

        # If size is stable (and non-zero, optionally), assume transferred
        # Add check for zero size if needed: and before_file_size > 0
        return before_file_size == after_file_size
    except Exception as e:
        print(f"Error checking if file transferred {file_path}: {e}")
        return False

def remove_ignored_folders(dirs):
    """Removes folder names present in the ignored_folder_names list."""
    ignored_set = set(ignored_folder_names) # Use setting
    return [d for d in dirs if d not in ignored_set]

def remove_hidden_folders(dirs):
    """Removes hidden folders (starting with '.') from a list of directory names."""
    return [d for d in dirs if not d.startswith(".")]

def get_modification_date(path):
    """Retrieves the modification date (timestamp) of the passed file/folder path."""
    try:
        return os.path.getmtime(path)
    except OSError as e:
        print(f"Error getting modification date for {path}: {e}")
        return None

def set_modification_date(file_path, date_timestamp):
    """Sets the modification date of the passed file path to the passed timestamp."""
    try:
        # os.utime takes (atime, mtime)
        current_atime = os.path.getatime(file_path) # Keep current access time
        os.utime(file_path, (current_atime, date_timestamp))
        return True
    except OSError as e:
        print(f"Error setting modification date for {file_path}: {e}")
        return False
    except Exception as e: # Catch other potential errors
        print(f"Unexpected error setting modification date for {file_path}: {e}")
        return False

def get_lines_from_file(file_path, ignore=[], check_paths=False, paths_to_check=[]):
    """Read all lines from a text file, excluding specified lines or paths."""
    results = []
    try:
        with open(file_path, "r", encoding='utf-8') as file: # Specify encoding
            ignore_set = set(ignore)
            paths_tuple = tuple(paths_to_check) if check_paths else None

            for line in file:
                line = line.strip()
                if not line or line in ignore_set or line in results:
                    continue
                if check_paths and paths_tuple and not line.startswith(paths_tuple):
                    continue
                results.append(line)
    except FileNotFoundError:
        print(f"File not found: {file_path}.") # Basic logging
        return []
    except Exception as ex:
        print(f"Error reading {file_path}: {ex}") # Basic logging
        return []
    return results

def normalize_path(path):
    """Normalize path separators and remove Windows drive letters if present."""
    path = os.path.normpath(path)
    # Remove Windows drive letters (e.g., "Z:\example\path" -> "\example\path")
    if ":" in path and os.name == 'nt': # Check for OS specifically
        path = re.sub(r"^[A-Za-z]:", "", path)
    # Convert backslashes to forward slashes for uniform comparison
    return path.replace("\\", "/")

def is_root_present(root_path, target_path):
    """Check if root_path is a prefix of target_path, handling path normalization."""
    normalized_root = normalize_path(root_path)
    normalized_target = normalize_path(target_path)
    # Ensure comparison considers directory boundaries
    # Check if target starts with root + separator or is exactly root
    return normalized_target == normalized_root or \
           normalized_target.startswith(normalized_root + "/")

def remove_folder(folder):
    """Removes the specified folder and all of its contents."""
    result = False
    if os.path.isdir(folder): # Check if it exists and is a directory
        try:
            shutil.rmtree(folder)
            # Verify removal
            if not os.path.exists(folder):
                print(f"\tRemoved folder: {folder}") # Basic logging
                result = True
            else:
                print(f"\tFailed to verify removal of {folder}") # Basic logging
        except OSError as e:
            print(f"\tError removing folder {folder}: {e}") # Basic logging
        except Exception as e:
            print(f"\tUnexpected error removing folder {folder}: {e}") # Basic logging
    else:
        print(f"\tFolder not found or not a directory: {folder}") # Basic logging
    return result