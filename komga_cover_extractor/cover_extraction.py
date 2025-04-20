# komga_cover_extractor/cover_extraction.py
import os
import shutil

# TODO: Refactor config imports
try:
    from .config import (
        paths, download_folders, paths_with_types, image_extensions,
        output_covers_as_webp, use_latest_volume_cover_as_series_cover,
        extract_chapter_covers,
        # Global state vars (TODO: Refactor state management)
        # transferred_files, transferred_dirs,
        # Colors (if needed by helpers called from here)
        # Toggles
        watchdog_toggle, copy_existing_volume_covers_toggle
    )
except ImportError:
    print("WARN: Could not import from .config, using placeholder values in cover_extraction.")
    paths = []
    download_folders = []
    paths_with_types = []
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    output_covers_as_webp = False
    use_latest_volume_cover_as_series_cover = False
    extract_chapter_covers = False
    watchdog_toggle = False
    copy_existing_volume_covers_toggle = False


# TODO: Import necessary functions from utility modules
try:
    from .log_utils import send_message
    from .file_utils import (
        process_files_and_folders, get_file_extension, remove_file,
        get_modification_date, set_modification_date, get_file_hash,
        create_folder_obj, upgrade_to_file_class, get_folder_type,
        upgrade_to_volume_class # Moved from core_logic temporarily
    )
    from .image_utils import find_and_extract_cover, convert_webp_to_jpg
    from .string_utils import clean_str, similar, is_same_index_number # Added is_same_index_number
    # from .core_logic import get_highest_release # Import if needed, or keep local
    from .models import File, Folder, Volume # Import needed models
except ImportError as e:
    print(f"FATAL: Failed to import dependencies in cover_extraction: {e}")
    def send_message(msg, error=False, discord=False): print(f"{'ERROR: ' if error else ''}{msg}")
    def process_files_and_folders(r, f, d, **kwargs): return f, d
    def get_file_extension(f): return os.path.splitext(f)[1]
    def remove_file(*args, **kwargs): return False
    def get_modification_date(p): return 0
    def set_modification_date(*args, **kwargs): pass
    def get_file_hash(p, *args): return None
    def create_folder_obj(*args, **kwargs): return None
    def upgrade_to_file_class(*args, **kwargs): return []
    def get_folder_type(*args, **kwargs): return 0
    def upgrade_to_volume_class(*args, **kwargs): return []
    def find_and_extract_cover(*args, **kwargs): return None
    def convert_webp_to_jpg(*args, **kwargs): return None
    def clean_str(s, **kwargs): return s
    def similar(a, b): return 0.0
    def is_same_index_number(*args, **kwargs): return False
    class File: pass
    class Folder: pass
    class Volume: pass


# --- Global State (TODO: Refactor this) ---
image_count = 0
checked_series = [] # Tracks series folders checked for covers in a run
root_modification_times = {} # Watchdog state
series_cover_path = "" # Path to the current series cover being checked/used
file_counters = {ext: 0 for ext in image_extensions | set(file_extensions)} # Stats counter
errors = [] # Error tracking (might move to log_utils)
items_changed = [] # Change tracking (might move to log_utils)
# State potentially needed from watchdog_handler if called from there
transferred_files = []
transferred_dirs = []


# --- Helper Functions ---

# Returns the highest volume number and volume part number of a release in a list of volume releases
# Moved here from core_logic as it's primarily used by extract_covers
@lru_cache(maxsize=3500)
def get_highest_release(releases, is_chapter_directory=False):
    highest_num = ""
    if use_latest_volume_cover_as_series_cover and not is_chapter_directory:
        numeric_releases = []
        for item in releases:
            # Ensure item is hashable before processing
            current_item = item
            if isinstance(current_item, list): current_item = tuple(current_item)

            if isinstance(current_item, (int, float)):
                numeric_releases.append(current_item)
            elif isinstance(current_item, tuple):
                 sub_numeric = [sub_item for sub_item in current_item if isinstance(sub_item, (int, float))]
                 if sub_numeric:
                     numeric_releases.append(max(sub_numeric))

        if numeric_releases:
            highest_num = max(numeric_releases)
    return highest_num


# Updates our output stats
def update_stats(file):
    """Increments the counter for the file's extension."""
    global file_counters
    if file.extension in file_counters:
        file_counters[file.extension] += 1
    else:
        # Log if an unexpected extension is encountered
        # send_message(f"Warning: Encountered unexpected extension '{file.extension}' during stats update.", error=True)
        pass


# --- Core Cover Extraction Logic ---

# Handles the processing of cover extraction for a single file.
def process_cover_extraction(
    file, # Expects a Volume object
    has_multiple_volume_ones,
    highest_index_number,
    is_chapter_directory,
    volume_paths, # List of paths to volume libraries (for chapter cover copying)
    clean_basename, # Cleaned basename of the current directory
    same_series_name, # Boolean indicating if all files in dir have same series name
    contains_subfolders # Boolean indicating if current dir has subfolders
):
    """Processes a single file for cover extraction and series cover handling."""
    global image_count, series_cover_path, checked_series # Manage global state

    # Helper to find the first word of a string
    def get_first_word(input_string):
        words = input_string.split()
        return words[0] if words else None

    # Helper to filter series folders by first word
    def filter_series_by_first_word(filtered_series, first_word):
        return [
            folder for folder in filtered_series
            if clean_str(folder).lower().startswith(first_word) # Use imported string_utils function
        ]

    try:
        update_stats(file) # Use local helper
        has_cover = False
        printed = False # Flag to avoid printing file name multiple times

        # 1. Check if a cover image already exists for this file
        cover = next(
            (
                f"{file.extensionless_path}{extension}"
                for extension in image_extensions
                if os.path.exists(f"{file.extensionless_path}{extension}")
            ),
            "",
        )
        cover_extension = get_file_extension(cover) if cover else "" # Use imported file_utils function

        # 2. Handle WebP conversion if necessary
        if cover_extension:
            needs_conversion = (output_covers_as_webp and cover_extension != ".webp") or \
                               (not output_covers_as_webp and cover_extension == ".webp")
            if needs_conversion:
                print(f"\t\tCover '{os.path.basename(cover)}' needs format conversion.")
                if remove_file(cover, silent=True): # Use imported file_utils function
                    cover = "" # Clear cover path as it was removed
                    cover_extension = ""
                else:
                    send_message(f"Failed to remove existing cover '{os.path.basename(cover)}' for conversion.", error=True)
                    # Continue without cover extraction for this file if removal failed

        # 3. Extract cover if it doesn't exist or was removed for conversion
        if not cover:
            if not printed:
                print(f"\n\tFile: {file.name}")
                printed = True
            print("\t\tAttempting to extract cover...")
            result = find_and_extract_cover(file) # Use imported image_utils function

            if result:
                # Handle potential WebP conversion after extraction
                if result.lower().endswith(".webp") and not output_covers_as_webp:
                    print("\t\tExtracted cover is WebP, converting to JPG...")
                    conversion_result = convert_webp_to_jpg(result) # Use imported image_utils function
                    if conversion_result:
                        print("\t\tCover successfully converted to JPG.")
                        cover = conversion_result
                    else:
                        print("\t\tCover conversion failed. Cleaning up extracted WebP.")
                        if os.path.isfile(result): remove_file(result, silent=True) # Use imported file_utils function
                        cover = "" # No valid cover
                elif result.lower().endswith((".jpg", ".jpeg", ".png")) and output_covers_as_webp:
                     # TODO: Implement JPG/PNG to WebP conversion if needed after extraction
                     print(f"\t\tExtracted {get_file_extension(result)}, desired WebP (conversion not implemented yet). Using original.")
                     cover = result # Use extracted file for now
                else:
                    cover = result # Use the extracted file path directly

                if cover:
                    print(f"\t\tCover successfully extracted/processed: {os.path.basename(cover)}\n")
                    has_cover = True
                    image_count += 1 # Manage global state
                else:
                     print("\t\tCover extraction/processing failed.")
            else:
                print("\t\tCover could not be extracted.")
        else:
            has_cover = True
            # image_count was already incremented if cover existed initially? No, only on extraction.
            # Let's increment here if cover exists and wasn't just extracted.
            # This logic needs refinement - maybe count existing covers separately?
            # For now, let's assume image_count tracks *extracted* covers.
            # If cover existed, we don't increment image_count here.
            pass


        # 4. Handle Series Cover Logic (if applicable)
        # Conditions: volume file, not chapter dir, cover exists, not multiple vol1s,
        #             correct volume (1 or latest based on config)
        should_update_series_cover = (
            file.file_type == "volume" and
            not is_chapter_directory and
            cover and # A valid cover must exist for this volume
            series_cover_path and # A series cover must already exist
            not has_multiple_volume_ones and
            (
                (use_latest_volume_cover_as_series_cover and is_same_index_number(file.index_number, highest_index_number, allow_array_match=True)) or
                (not use_latest_volume_cover_as_series_cover and (file.index_number == 1 or (isinstance(file.index_number, list) and 1 in file.index_number)))
            )
        )

        if should_update_series_cover:
            try:
                current_series_cover_mod_date = get_modification_date(series_cover_path) # Use imported file_utils function
                volume_cover_mod_date = get_modification_date(cover) # Use imported file_utils function

                if current_series_cover_mod_date != volume_cover_mod_date:
                    current_series_hash = get_file_hash(series_cover_path) # Use imported file_utils function
                    volume_cover_hash = get_file_hash(cover) # Use imported file_utils function

                    if current_series_hash != volume_cover_hash:
                        print("\t\tSeries cover differs from designated volume cover. Replacing...")
                        if remove_file(series_cover_path, silent=True): # Use imported file_utils function
                            try:
                                shutil.copy2(cover, series_cover_path) # copy2 preserves metadata like mod time
                                print(f"\t\tReplaced series cover with: {os.path.basename(cover)}")
                            except Exception as copy_err:
                                send_message(f"Failed to copy new series cover: {copy_err}", error=True)
                                series_cover_path = "" # Invalidate path if copy failed
                        else:
                            send_message("Failed to remove old series cover for replacement.", error=True)
                            series_cover_path = "" # Invalidate path
                    else:
                        # Hashes match, update mod time of series cover to match volume cover
                        print("\t\tSeries cover hash matches volume cover. Updating modification time.")
                        set_modification_date(series_cover_path, volume_cover_mod_date) # Use imported file_utils function
            except Exception as series_cover_err:
                 send_message(f"Error comparing/updating series cover: {series_cover_err}", error=True)


        # 5. Copy cover from Volume library to Chapter library (if enabled)
        # Conditions: Chapter dir, copy toggle enabled, series not already checked, volume paths exist
        if is_chapter_directory and copy_existing_volume_covers_toggle and file.root not in checked_series and volume_paths and clean_basename:
            print(f"\tAttempting to copy volume cover for chapter series: {clean_basename}")
            found_volume_cover_source = False
            for v_path_obj in volume_paths:
                if found_volume_cover_source: break # Stop if cover found

                # Filter potential source folders
                source_series_folders = getattr(v_path_obj, 'series_folders', []) # Get pre-filtered list if available
                if not source_series_folders: continue # Skip if no folders for this path

                first_word = get_first_word(clean_basename) # Use local helper
                if first_word:
                    source_series_folders = filter_series_by_first_word(source_series_folders, first_word) # Use local helper

                for folder_name in source_series_folders:
                    if similar(clean_str(folder_name), clean_basename) >= required_similarity_score: # Use imported string_utils function
                        volume_series_path = os.path.join(v_path_obj.path, folder_name)
                        # Look for cover.ext or poster.ext in the volume series path
                        potential_cover = next(
                            (os.path.join(volume_series_path, f"{cover_name}{ext}")
                             for cover_name in series_cover_file_names # Use imported config value
                             for ext in image_extensions
                             if os.path.isfile(os.path.join(volume_series_path, f"{cover_name}{ext}"))),
                            None
                        )

                        if potential_cover:
                            target_series_cover_path = os.path.join(file.root, f"cover{get_file_extension(potential_cover)}") # Use imported file_utils function
                            copy_needed = True
                            if os.path.isfile(target_series_cover_path):
                                # Compare hashes if target exists
                                if get_file_hash(target_series_cover_path) == get_file_hash(potential_cover): # Use imported file_utils function
                                    copy_needed = False
                                    print("\t\tChapter series already has matching cover.")
                                else:
                                     print("\t\tChapter series cover differs. Replacing.")
                                     remove_file(target_series_cover_path, silent=True) # Use imported file_utils function

                            if copy_needed:
                                try:
                                    shutil.copy2(potential_cover, target_series_cover_path) # copy2 preserves metadata
                                    print(f"\t\tCopied cover from '{folder_name}' to chapter series.")
                                    series_cover_path = target_series_cover_path # Update global path
                                    found_volume_cover_source = True
                                    break # Found cover, stop searching this path obj
                                except Exception as copy_err:
                                    send_message(f"Failed to copy cover from volume library: {copy_err}", error=True)
                        # else: print(f"\t\tNo cover found in potential volume match: {folder_name}") # Debugging

            checked_series.append(file.root) # Mark this chapter series as checked


        # 6. Create series cover if it doesn't exist (and conditions met)
        # Conditions: Not chapter dir, cover exists, no series cover yet, not multiple vol1s,
        #             correct volume (1 or latest), series names match in folder
        should_create_series_cover = (
            not is_chapter_directory and
            has_cover and cover and # Ensure cover extraction was successful
            not series_cover_path and # Only if series cover doesn't exist
            not contains_subfolders and # Don't create in root if subfolders exist
            file.root not in download_folders and # Don't create in download folders
            not has_multiple_volume_ones and
            same_series_name and # Ensure consistency within the folder
            (
                (use_latest_volume_cover_as_series_cover and is_same_index_number(file.index_number, highest_index_number, allow_array_match=True)) or
                (not use_latest_volume_cover_as_series_cover and (file.index_number == 1 or (isinstance(file.index_number, list) and 1 in file.index_number)))
            )
        )

        if should_create_series_cover:
            if not printed: print(f"\n\tFile: {file.name}") # Print filename if not already done
            print("\t\tSeries cover missing. Creating from designated volume cover.")
            target_series_cover_path = os.path.join(file.root, f"cover{get_file_extension(cover)}") # Use imported file_utils function
            try:
                shutil.copy2(cover, target_series_cover_path) # copy2 preserves metadata
                print(f"\t\tCreated series cover: {os.path.basename(target_series_cover_path)}")
                series_cover_path = target_series_cover_path # Update global path
            except Exception as copy_err:
                 send_message(f"Failed to create series cover: {copy_err}", error=True)


    except Exception as e:
        send_message(f"\nERROR in process_cover_extraction() for file {file.name}: {e}\n{traceback.format_exc()}", error=True)


# Extracts the covers out from manga and novel files.
def extract_covers(paths_to_process=paths):
    """Iterates through paths and triggers cover extraction for each file."""
    global checked_series, root_modification_times, series_cover_path # Manage global state

    if not paths_to_process:
        print("\nNo paths specified for cover extraction.")
        return

    print("\nLooking for covers to extract...")
    volume_paths = [] # Initialize volume paths list for chapter cover copying

    for path in paths_to_process:
        if not os.path.isdir(path):
            print(f"\nERROR: {path} is an invalid path.\n")
            continue

        print(f"\nScanning Path: {path}")
        checked_series = [] # Reset checked series for each main path
        # Use scandir for potentially better performance
        try:
            for root, dirs, files in scandir.walk(path):
                # --- Watchdog Modification Check ---
                if watchdog_toggle:
                    # Check modification time only if watchdog is enabled
                    root_mod_time = get_modification_date(root) # Use imported file_utils function
                    if root in root_modification_times and root_modification_times[root] == root_mod_time:
                        # print(f"\tSkipping unmodified directory: {root}") # Debugging print
                        continue # Skip if modification time hasn't changed
                    root_modification_times[root] = root_mod_time # Update mod time

                # --- File & Directory Filtering ---
                files, dirs = process_files_and_folders(
                    root, files, dirs,
                    just_these_files=transferred_files, # Use global state
                    just_these_dirs=transferred_dirs   # Use global state
                )
                contains_subfolders = bool(dirs)

                if not files: continue # Skip if no relevant files

                print(f"\nProcessing Directory: {root}")
                # print(f"Files found: {len(files)}") # Debugging print

                # --- Prepare Data for Processing ---
                # Upgrade files to Volume objects
                volume_objects = upgrade_to_volume_class(
                    upgrade_to_file_class(
                        [f for f in files if os.path.isfile(os.path.join(root, f))],
                        root
                    ),
                    # Pass necessary skip flags if needed
                    skip_release_year=True, skip_release_group=True, skip_extras=True,
                    skip_publisher=True, skip_premium_content=True, skip_subtitle=True,
                    skip_multi_volume=True # Multi-volume info not needed for cover extraction itself
                )

                if not volume_objects: continue

                # Create Folder object (though maybe not strictly needed here if we pass volume_objects)
                folder_accessor = create_folder_obj(root, dirs, volume_objects) # Use imported file_utils function

                # --- Series Cover & Type Determination ---
                series_cover_path = next( # Find existing series cover
                    (os.path.join(root, f"cover{ext}") for ext in image_extensions if os.path.exists(os.path.join(root, f"cover{ext}"))),
                    None
                )
                # Handle potential series cover format conversion needs
                if series_cover_path:
                    series_cover_extension = get_file_extension(series_cover_path) # Use imported file_utils function
                    needs_conversion = (output_covers_as_webp and series_cover_extension != ".webp") or \
                                       (not output_covers_as_webp and series_cover_extension == ".webp")
                    if needs_conversion:
                         print(f"\tSeries cover '{os.path.basename(series_cover_path)}' needs format conversion.")
                         if remove_file(series_cover_path, silent=True): # Use imported file_utils function
                             series_cover_path = None # Clear path as it was removed
                         else:
                             send_message("Failed to remove series cover for conversion.", error=True)


                is_chapter_directory = get_folder_type(volume_objects, file_type="chapter") >= 90 # Use imported file_utils function
                same_series_name = len(set(vol.series_name.lower() for vol in volume_objects if vol.series_name)) <= 1 # Check if all series names are the same (or only one unique name exists)

                # --- Prepare Volume Paths for Chapter Cover Copying ---
                base_name = None
                if copy_existing_volume_covers_toggle and is_chapter_directory:
                    if not volume_paths: # Initialize only once
                         volume_paths = [
                             pwt for pwt in paths_with_types
                             if "volume" in pwt.path_formats
                             # Add extension check if needed: and any(ext in pwt.path_extensions for ext in ...)
                         ]
                         # Pre-scan series folders in volume paths for faster lookup later
                         for vp in volume_paths:
                             try:
                                 vp.series_folders = [entry.name for entry in scandir.scandir(vp.path) if entry.is_dir() and not entry.name.startswith('.')]
                             except Exception as scan_err:
                                 send_message(f"Error scanning volume path {vp.path}: {scan_err}", error=True)
                                 vp.series_folders = []
                    base_name = clean_str(os.path.basename(root)) # Use imported string_utils function

                # --- Determine Highest Release & Multiple Vol1s ---
                try:
                    all_indices = [item.index_number for item in volume_objects if item.index_number != ""]
                    hashable_indices = [tuple(idx) if isinstance(idx, list) else idx for idx in all_indices]
                    highest_index_number = get_highest_release(tuple(hashable_indices), is_chapter_directory=is_chapter_directory) if not is_chapter_directory else ""
                except Exception as high_err:
                     send_message(f"Error determining highest release in {root}: {high_err}", error=True)
                     highest_index_number = ""

                has_multiple_volume_ones = (not use_latest_volume_cover_as_series_cover or is_chapter_directory) and \
                                           sum(1 for vol in volume_objects if not vol.is_one_shot and not vol.volume_part and (vol.index_number == 1 or (isinstance(vol.index_number, list) and 1 in vol.index_number))) > 1

                # --- Process Each Volume ---
                for file_obj in volume_objects:
                    # Check if cover extraction is needed for this file type
                    if file_obj.file_type == "volume" or (file_obj.file_type == "chapter" and extract_chapter_covers):
                        process_cover_extraction(
                            file_obj,
                            has_multiple_volume_ones,
                            highest_index_number,
                            is_chapter_directory,
                            volume_paths,
                            base_name, # Pass cleaned basename
                            same_series_name,
                            contains_subfolders
                        )

        except Exception as e:
            send_message(f"Error walking directory {path}: {e}\n{traceback.format_exc()}", error=True)


# Prints the collected stats about the paths/files that were processed.
def print_stats():
    """Prints the final statistics of the cover extraction process."""
    global file_counters, image_count, errors # Access global state

    print("\n--- Cover Extraction Stats ---")
    total_files = sum(file_counters.values())
    print(f"Total Files Scanned (relevant types): {total_files}")
    if total_files > 0:
        for ext, count in file_counters.items():
            if count > 0:
                print(f"\t{ext}: {count}")
    print(f"Covers Extracted/Created: {image_count}")

    if errors:
        print(f"\nErrors Encountered ({len(errors)}):")
        unique_errors = sorted(list(set(errors)))
        for error in unique_errors[:10]: # Limit printed errors
            print(f"\t{error[:200]}{'...' if len(error) > 200 else ''}")
        if len(unique_errors) > 10:
             print(f"\t... and {len(unique_errors) - 10} more errors.")
    print("--- End Stats ---")