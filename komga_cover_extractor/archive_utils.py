# komga_cover_extractor/archive_utils.py
import os
import zipfile
import rarfile
import py7zr
import tempfile
import scandir
import re
from functools import lru_cache

# TODO: Ensure these are correctly imported from config module later
# Import necessary config variables
# Use try-except for robustness during refactoring
try:
    from .config import (
        rar_extensions, seven_zip_extensions, convertable_file_extensions,
        download_folders, transferred_files, transferred_dirs, manual_rename,
        watchdog_toggle # Added watchdog_toggle
    )
except ImportError:
     print("WARN: Could not import from .config in archive_utils.py, using placeholders.")
     rar_extensions, seven_zip_extensions, convertable_file_extensions = [], [], []
     download_folders, transferred_files, transferred_dirs = [], [], []
     manual_rename = False
     watchdog_toggle = False

# Import necessary functions from other utils
try:
    from .log_utils import send_message
    from .file_utils import (
    get_file_extension, get_extensionless_name, remove_folder, remove_file,
    get_file_size, process_files_and_folders, get_header_extension, rename_file
    )
    from .misc_utils import get_input_from_user # Needed for convert_to_cbz
    # Import models if needed (currently not directly used here)
    # from .models import Embed
except ImportError as e:
     print(f"WARN: Could not import utility functions in archive_utils.py: {e}")
     # Define placeholders if imports fail
     def send_message(msg, error=False): print(f"{'ERROR: ' if error else ''}{msg}")
     def get_file_extension(f): return os.path.splitext(f)[1]
     def get_extensionless_name(f): return os.path.splitext(f)[0]
     def remove_folder(*args, **kwargs): pass
     def remove_file(*args, **kwargs): return False
     def get_file_size(p): return 0
     def process_files_and_folders(r, f, d, **kwargs): return f, d
     def get_header_extension(f): return None
     def rename_file(*args, **kwargs): return False
     def get_input_from_user(*args, **kwargs): return "y"


# --- Archive Extraction/Compression ---

def extract(file_path, temp_dir, extension):
    """Extracts RAR or 7z archives to a temporary directory."""
    success = False
    try:
        if extension in rar_extensions: # Use imported config value
            # Ensure rarfile command is available if needed, or unrar library is installed
            rarfile.UNRAR_TOOL = "unrar" # Or specify path if not in PATH
            with rarfile.RarFile(file_path) as rar:
                rar.extractall(temp_dir)
                success = True
        elif extension in seven_zip_extensions: # Use imported config value
            with py7zr.SevenZipFile(file_path, "r") as archive:
                archive.extractall(temp_dir)
                success = True
        else:
             send_message(f"Unsupported extension for extraction: {extension}", error=True) # Use imported log_utils function
    except rarfile.NeedFirstVolume:
         send_message(f"Extraction failed: Multi-volume RAR detected, cannot process {file_path}", error=True)
    except rarfile.BadRarFile:
         send_message(f"Extraction failed: Bad RAR file {file_path}", error=True)
    except py7zr.exceptions.Bad7zFile:
         send_message(f"Extraction failed: Bad 7z file {file_path}", error=True)
    except FileNotFoundError as fnf_error:
         # Handle case where unrar tool might be missing
         if extension in rar_extensions and 'unrar' in str(fnf_error):
             send_message(f"Extraction failed: 'unrar' command not found or not executable. Please install it or check PATH.", error=True)
         else:
             send_message(f"Extraction failed: File not found during extraction {file_path}: {fnf_error}", error=True)
    except Exception as e:
        send_message(f"Error extracting {file_path}: {e}", error=True) # Use imported log_utils function
    return success

def compress(temp_dir, cbz_filename):
    """Compresses the contents of a directory into a CBZ (zip) file."""
    success = False
    try:
        with zipfile.ZipFile(cbz_filename, "w", zipfile.ZIP_DEFLATED) as zipf: # Use compression
            for root, dirs, files in scandir.walk(temp_dir):
                # Filter hidden files/dirs (like .DS_Store or __MACOSX)
                files = [f for f in files if not f.startswith(('.', '__'))]
                dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
                for file in files:
                    file_path = os.path.join(root, file)
                    # Arcname is the path inside the zip file
                    arcname = os.path.relpath(file_path, temp_dir)
                    zipf.write(file_path, arcname)
            success = True
    except Exception as e:
        send_message(f"Error compressing {temp_dir} to {cbz_filename}: {e}", error=True) # Use imported log_utils function
        # Clean up potentially incomplete cbz file
        if os.path.isfile(cbz_filename):
            remove_file(cbz_filename, silent=True) # Use imported file_utils function
    return success

# Converts supported archives (RAR, 7z) to CBZ.
def convert_to_cbz():
    """Finds and converts RAR/7z archives in download folders to CBZ."""
    # Manage state locally or pass if needed, avoid global for grouped_notifications
    global transferred_files # Still needed for watchdog interaction

    print("\nLooking for archives to convert to CBZ...")
    if not download_folders: # Use imported config value
        print("\tNo download folders specified.")
        return

    converted_files = []

    for folder in download_folders:
        if not os.path.isdir(folder):
            print(f"\t{folder} is not a valid directory.")
            continue

        print(f"\tScanning: {folder}")
        for root, dirs, files in scandir.walk(folder):
            # Use process_files_and_folders for consistent filtering, including watchdog lists
            files, dirs = process_files_and_folders( # Use imported file_utils function
                root, files, dirs,
                just_these_files=transferred_files, # Use imported config value
                just_these_dirs=transferred_dirs, # Use imported config value
                skip_remove_unaccepted_file_types=True, # Allow convertable types
                keep_images_in_just_these_files=True # Keep images if needed by other steps
            )

            for entry in files:
                try:
                    extension = get_file_extension(entry) # Use imported file_utils function
                    file_path = os.path.join(root, entry)

                    if not os.path.isfile(file_path): continue
                    if file_path in converted_files: continue # Skip if already processed in this run

                    # print(f"\t\tChecking: {entry}") # Debugging print

                    if extension in convertable_file_extensions: # Use imported config value
                        source_file = file_path
                        repacked_file = f"{get_extensionless_name(source_file)}.cbz" # Use imported file_utils function

                        # Check if CBZ already exists and is valid
                        if os.path.isfile(repacked_file):
                            if get_file_size(repacked_file) == 0: # Use imported file_utils function
                                send_message("\t\t\tCBZ file is zero bytes, deleting...", discord=False) # Use imported log_utils function
                                remove_file(repacked_file, silent=True) # Use imported file_utils function
                            elif not zipfile.is_zipfile(repacked_file):
                                send_message("\t\t\tCBZ file is not a valid zip file, deleting...", discord=False) # Use imported log_utils function
                                remove_file(repacked_file, silent=True) # Use imported file_utils function
                            else:
                                # Valid CBZ exists, potentially remove original if desired (add config flag?)
                                # For now, just skip conversion and maybe remove original later if flag set
                                # print("\t\t\tCBZ file already exists, skipping conversion.")
                                # Optionally remove original source file here if config allows
                                # if remove_original_after_conversion: remove_file(source_file, silent=True)
                                continue # Skip to next file

                        print(f"\t\tFound convertable archive: {entry}")
                        user_input = "y"
                        if manual_rename: # Use imported config value (assuming this flag controls conversion prompt too)
                             user_input = get_input_from_user("\t\t\tConvert to CBZ?", ["y", "n"], ["y", "n"]) # Use imported misc_utils function

                        if user_input != 'y':
                             print("\t\t\tSkipping conversion.")
                             continue

                        temp_dir = tempfile.mkdtemp("_source2cbz")
                        print(f"\t\t\tCreated temp directory {temp_dir}")

                        # Extract
                        if not extract(source_file, temp_dir, extension): # Use local function
                            remove_folder(temp_dir) # Use imported file_utils function
                            continue
                        print(f"\t\t\tExtracted contents to {temp_dir}")

                        # Compress
                        if not compress(temp_dir, repacked_file): # Use local function
                            remove_folder(temp_dir) # Use imported file_utils function
                            continue
                        print(f"\t\t\tCompressed to {repacked_file}")

                        # Verification (Optional but recommended, e.g., compare file lists/hashes)
                        print("\t\t\tVerification step placeholder.")

                        # Cleanup
                        remove_folder(temp_dir) # Use imported file_utils function
                        print(f"\t\t\tRemoved temp directory {temp_dir}")

                        # Remove original file
                        if remove_file(source_file, silent=True): # Use imported file_utils function
                            print(f"\t\t\tRemoved original file {source_file}")
                            converted_files.append(repacked_file) # Track converted file

                            # Update watchdog list if necessary
                            if watchdog_toggle: # Use imported config value
                                if source_file in transferred_files: transferred_files.remove(source_file) # Use imported config value
                                if repacked_file not in transferred_files: transferred_files.append(repacked_file)

                            # Discord notification responsibility moved
                        else:
                            send_message(f"\t\t\tFailed to remove original file {source_file}", error=True) # Use imported log_utils function

                except Exception as e:
                    send_message(f"Error during conversion process for {entry}: {e}", error=True) # Use imported log_utils function
                    # Ensure temp dir is cleaned up on error
                    if 'temp_dir' in locals() and os.path.isdir(temp_dir):
                        remove_folder(temp_dir) # Use imported file_utils function


# --- Zip File Inspection ---

# Return the zip comment for the passed zip file
def get_zip_comment(zip_file):
    """Reads and returns the comment from a zip file."""
    comment = ""
    try:
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            if zip_ref.comment:
                # Attempt to decode comment, handle potential errors
                try:
                    comment = zip_ref.comment.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        comment = zip_ref.comment.decode("cp437") # Try common fallback encoding
                    except Exception:
                         comment = str(zip_ref.comment) # Fallback to raw string representation
    except zipfile.BadZipFile:
         send_message(f"Bad zip file, cannot read comment: {zip_file}", error=True) # Use imported log_utils function
    except FileNotFoundError:
         send_message(f"File not found, cannot read comment: {zip_file}", error=True) # Use imported log_utils function
    except Exception as e:
        send_message(f"Error reading zip comment for {zip_file}: {e}", error=True) # Use imported log_utils function
    return comment

# check if zip file contains ComicInfo.xml
@lru_cache(maxsize=3500) # Cache this as it's read-only per file
def contains_comic_info(zip_file):
    """Checks if a zip archive contains a ComicInfo.xml file."""
    result = False
    try:
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            # Case-insensitive check
            result = any(name.lower() == "comicinfo.xml" for name in zip_ref.namelist())
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        send_message(f"Cannot check for ComicInfo.xml in {zip_file}: {e}", error=True) # Use imported log_utils function
    except Exception as e: # Catch other potential zip errors
         send_message(f"Error accessing zip {zip_file} for ComicInfo check: {e}", error=True)
    return result

# Retrieve the file specified from the zip file and return the data for it.
def get_file_from_zip(zip_file_path, searches, extension=None, allow_base=True):
    """Reads the content of a specific file within a zip archive based on search criteria."""
    result = None
    try:
        with zipfile.ZipFile(zip_file_path, "r") as z:
            # Normalize searches to lowercase for case-insensitive matching
            lower_searches = [s.lower() for s in searches]

            # Filter list first by extension if provided
            file_list = z.namelist()
            if extension:
                file_list = [item for item in file_list if item.lower().endswith(extension.lower())]

            for path in file_list:
                # Determine the name to check based on allow_base
                name_to_check = os.path.basename(path).lower() if allow_base else path.lower()

                # Check if any search pattern matches
                if any(re.search(search_pattern, name_to_check, re.IGNORECASE) for search_pattern in lower_searches):
                    result = z.read(path)
                    break # Found the first match
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        send_message(f"Cannot read from zip {zip_file_path}: {e}", error=True) # Use imported log_utils function
    except Exception as e:
         send_message(f"Error reading file from zip {zip_file_path}: {e}", error=True)
    return result