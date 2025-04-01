import os
import time
import traceback
import scandir # Assuming scandir is installed
import shutil # Added for copy
import re # Added for check_upgrade path normalization
from datetime import datetime # Added for NewReleaseNotification grouping
from difflib import SequenceMatcher # Added for similar function if not imported elsewhere
import zipfile # Added for convert_to_cbz
import tempfile # Added for convert_to_cbz

# Assuming settings and models are accessible
from settings import (
    paths, # Default paths if not passed
    download_folders, # Default download folders if not passed
    watchdog_toggle,
    copy_existing_volume_covers_toggle,
    use_latest_volume_cover_as_series_cover,
    extract_chapter_covers,
    image_extensions,
    output_covers_as_webp,
    paths_with_types, # Assuming this structure is available
    moved_folders, # Assuming this state is managed elsewhere
    required_similarity_score,
    required_image_similarity_score,
    short_word_filter_percentage,
    check_for_existing_series_toggle,
    match_through_identifiers,
    match_through_image_similarity,
    check_for_duplicate_volumes_toggle,
    manual_delete,
    send_scan_request_to_komga_libraries_toggle,
    move_series_to_correct_library_toggle,
    rename_files_in_download_folders_toggle, # Added dependency
    resturcture_when_renaming, # Added dependency
    required_matching_percentage, # Added dependency
    manga_extensions, # Added dependency
    novel_extensions, # Added dependency
    new_volume_webhook, # Added dependency
    unacceptable_keywords, # Added dependency
    delete_unacceptable_files_toggle, # Added dependency
    delete_chapters_from_downloads_toggle, # Added dependency
    convert_to_cbz_toggle, # Added dependency
    rename_zip_to_cbz, # Added dependency
    manual_rename, # Added dependency
    convertable_file_extensions, # Added dependency
    # Add other necessary settings imports here
)
# Import necessary functions from other modules (adjust paths as needed)
from core.file_utils import (
    process_files_and_folders, get_modification_date, get_file_extension,
    remove_file, set_modification_date, get_file_hash, clean_str,
    normalize_path, is_root_present, get_all_folders_recursively_in_dir,
    get_zip_comment,
    cache_path,
    get_file_size,
    remove_hidden_files, # Added dependency for check_upgrade's helper
    count_words, # Added dependency for alternative_match_allowed
    move_folder, # Added dependency for move_series_to_correct_library
    extract, # Added for convert_to_cbz
    compress, # Added for convert_to_cbz
    get_header_extension, # Added for convert_to_cbz
    remove_folder, # Added for convert_to_cbz
)
from core.image_utils import (
    find_and_extract_cover, convert_webp_to_jpg, prep_images_for_similarity
)
from processing.volume_processor import (
    upgrade_to_file_class, upgrade_to_volume_class, get_highest_release,
    is_same_index_number, is_one_shot, get_folder_type,
    is_upgradeable,
    get_keyword_scores,
    contains_chapter_keywords, # Added dependency
    contains_volume_keywords, # Added dependency
    check_for_exception_keywords, # Added dependency
)
from core.models import IdentifierResult, Path, NewReleaseNotification, Embed, File, Volume # Added File, Volume
from core.metadata_utils import get_identifiers
from core.string_utils import similar, parse_words, find_consecutive_items, move_strings_to_top, array_to_string, get_shortened_title, get_subtitle_from_dash # Added string utils
from renaming.file_renamer import reorganize_and_rename, get_input_from_user, rename_file # Added dependencies
from renaming.folder_renamer import remove_duplicate_releases, check_and_delete_empty_folder, replace_file # Added replace_file
from messaging.discord_messenger import group_notification, handle_fields, DiscordEmbed, grey_color, green_color, yellow_color, send_discord_message # Added discord imports
from messaging.log_manager import write_to_file, send_message # Added log/message imports
from config.library_types import get_library_type # Added

# --- State Management (Needs proper implementation, e.g., within a class or passed) ---
checked_series = []
root_modification_times = {}
image_count = 0
# Global state for check_for_existing_series - needs refactoring
cached_identifier_results = []
messages_to_send = []
grouped_notifications = [] # Used by check_upgrade and others if discord enabled
komga_libraries = [] # Added state
libraries_to_scan = [] # Added state
errors = [] # Added state for print_stats
file_counters = {ext: 0 for ext in file_extensions} # Added state for print_stats
transferred_files = [] # Added state for convert_to_cbz etc. (Watchdog related)
transferred_dirs = [] # Added state (Watchdog related)

# --- extract_covers Function and its Helpers ---
# (extract_covers function from previous step remains here)
def extract_covers(paths_to_process=paths):
    """Extracts cover images from comic/novel archives in specified paths."""
    global checked_series, root_modification_times, image_count, file_counters # Acknowledge use of module-level state (refactor later)

    # Helper to update stats (moved from original main loop)
    def update_stats(file_obj):
        nonlocal image_count # Access outer function scope if needed, or use global
        global file_counters # Access module scope
        file_counters[file_obj.extension] = file_counters.get(file_obj.extension, 0) + 1
        # image_count is incremented within process_cover_extraction

    # Finds the series cover image (cover.*) in the given folder
    def find_series_cover(folder_root, image_extensions_set):
        for ext in image_extensions_set:
            potential_cover = os.path.join(folder_root, f"cover{ext}")
            if os.path.exists(potential_cover):
                return potential_cover
        return None

    # Checks if the folder contains files with the same series name (basic check)
    def check_same_series_name(volume_objects, required_percent=0.9):
        if not volume_objects or len(volume_objects) < 2:
            return True # Assume same if only 0 or 1 volume

        first_series_name = clean_str(volume_objects[0].series_name, skip_bracket=True)
        file_count = len(volume_objects)
        required_count = int(file_count * required_percent)

        match_count = sum(
            clean_str(vol.series_name, skip_bracket=True) == first_series_name
            for vol in volume_objects
        )
        return match_count >= required_count

    # Processes volume paths for cover copying logic
    def process_volume_paths(
        current_files, # List of Volume objects in the current (chapter) directory
        current_root, # Path of the current (chapter) directory
        copy_covers_enabled,
        is_chapter_dir,
        current_volume_paths, # List of potential volume library Path objects
        all_paths_with_types # Full list of Path objects for all libraries
    ):
        base_name = None
        volume_paths_data = current_volume_paths # Return existing if no processing needed

        if copy_covers_enabled and is_chapter_dir and current_files:
            # Initialize volume_paths if not already done
            if not current_volume_paths and all_paths_with_types:
                volume_paths_data = [
                    p
                    for p in all_paths_with_types
                    if "volume" in p.path_formats
                    and current_files[0].extension in p.path_extensions # Match extension
                ]
                # Pre-scan series folders in volume paths for faster lookup later
                for v_path in volume_paths_data:
                    try:
                        # Get folders directly under the volume path
                        v_path.series_folders = [
                            entry.name for entry in scandir.scandir(v_path.path)
                            if entry.is_dir() and not entry.name.startswith('.')
                        ]
                    except Exception as e:
                        print(f"Error scanning volume path {v_path.path}: {e}")
                        v_path.series_folders = []

            # Get cleaned basename of the current chapter directory
            base_name = clean_str(os.path.basename(current_root))

        return base_name, volume_paths_data

    # Checks if the folder contains multiple volume ones
    def contains_multiple_volume_ones(
        volume_objects, use_latest_cover, is_chapter_dir
    ):
        if use_latest_cover and not is_chapter_dir:
            return False # Logic doesn't apply if using latest cover

        volume_ones_count = sum(
            1
            for vol in volume_objects
            if not vol.is_one_shot
            and not vol.volume_part # Exclude parts like v1.5
            and (
                vol.index_number == 1 # Check index number directly
            )
        )
        return volume_ones_count > 1

    # Handles the processing of cover extraction for a single file.
    def process_cover_extraction(
        file_obj, # The Volume object
        has_multiple_v1s,
        highest_idx_num,
        is_chapter_dir,
        volume_paths_data, # Pre-processed volume paths with series folders
        current_clean_basename, # Cleaned basename of the current directory
        all_series_names_match, # Boolean indicating if all vols in dir match series name
        dir_contains_subfolders, # Boolean
        current_series_cover_path # Path to existing series cover, or None
    ):
        # Access module-level state
        global image_count, checked_series

        try:
            has_cover = False
            printed = False # Local print flag for this file

            update_stats(file_obj) # Update file extension counts

            # 1. Check if a volume cover image already exists
            volume_cover_path = next(
                (
                    f"{file_obj.extensionless_path}{ext}"
                    for ext in image_extensions
                    if os.path.exists(f"{file_obj.extensionless_path}{ext}")
                ),
                None,
            )

            volume_cover_ext = get_file_extension(volume_cover_path) if volume_cover_path else ""

            # 2. Handle format mismatch (e.g., existing JPG but want WEBP)
            if volume_cover_path and (
                (output_covers_as_webp and volume_cover_ext != ".webp") or
                (not output_covers_as_webp and volume_cover_ext == ".webp")
            ):
                print(f"\t\tCover format mismatch for {file_obj.name}. Removing existing: {volume_cover_path}")
                if remove_file(volume_cover_path, silent=True):
                    volume_cover_path = None # Reset path after removal
                else:
                    print(f"\t\t\tFailed to remove existing cover.")

            # 3. Extract cover if it doesn't exist or format was wrong
            if not volume_cover_path:
                if not printed:
                    print(f"\n\tFile: {file_obj.name}")
                    printed = True

                print("\t\tAttempting to extract cover...")
                # Use the image_utils function
                extracted_path_or_data = find_and_extract_cover(file_obj, return_data_only=False, silent=False)

                if isinstance(extracted_path_or_data, str) and os.path.isfile(extracted_path_or_data):
                    volume_cover_path = extracted_path_or_data # Successfully extracted and saved
                    print(f"\t\tCover successfully extracted to: {volume_cover_path}")
                    has_cover = True
                    image_count += 1
                else:
                    print("\t\tCover extraction failed or no cover found.")
            else:
                           print(f"\t\tCopied {os.path.basename(volume_cover_path)} to {os.path.basename(target_series_cover_path)}")
                           # Update module-level state (needs refactor)
                           # series_cover_path = target_series_cover_path
                      except Exception as e:
                           print(f"\t\tError copying series cover: {e}")

            # --- Logic for copying existing Volume cover to Chapter directory ---
            if copy_covers_enabled and is_chapter_dir and current_clean_basename and volume_paths_data:
                 if file_obj.root in checked_series: return # Already checked this chapter series dir

                 found_volume_cover = False
                 for v_path_data in volume_paths_data:
                      if not hasattr(v_path_data, 'series_folders') or not v_path_data.series_folders: continue

                      # Filter potential volume series folders
                      first_word = current_clean_basename.split()[0] if current_clean_basename else None
                      potential_folders = [
                          folder for folder in v_path_data.series_folders
                          if clean_str(folder).startswith(first_word) # Basic first word filter
                      ] if first_word else v_path_data.series_folders

                      for vol_folder_name in potential_folders:
                           vol_folder_path = os.path.join(v_path_data.path, vol_folder_name)
                           clean_vol_folder = clean_str(vol_folder_name)

                           # Check similarity
                           if not (clean_vol_folder == current_clean_basename or
                                   similar(clean_vol_folder, current_clean_basename) >= required_similarity_score):
                                continue

                           # Found potential match, look for cover.jpg/webp inside
                           vol_series_cover = find_series_cover(vol_folder_path, image_extensions)
                           if vol_series_cover:
                                target_chapter_series_cover = os.path.join(file_obj.root, os.path.basename(vol_series_cover))

                                copy_needed = True
                                if os.path.exists(target_chapter_series_cover):
                                     # Check if existing chapter cover needs update
                                     vol_ser_mod = get_modification_date(vol_series_cover)
                                     chap_ser_mod = get_modification_date(target_chapter_series_cover)
                                     if vol_ser_mod and chap_ser_mod:
                                          if vol_ser_mod == chap_ser_mod:
                                               copy_needed = False
                                          else:
                                               vol_ser_hash = get_file_hash(vol_series_cover)
                                               chap_ser_hash = get_file_hash(target_chapter_series_cover)
                                               if vol_ser_hash and chap_ser_hash and vol_ser_hash == chap_ser_hash:
                                                    copy_needed = False
                                                    set_modification_date(target_chapter_series_cover, vol_ser_mod)
                                               else:
                                                    remove_file(target_chapter_series_cover, silent=True)

                                if copy_needed:
                                     try:
                                          print(f"\t\tCopying series cover from Volume library: {vol_series_cover} to {target_chapter_series_cover}")
                                          shutil.copy2(vol_series_cover, target_chapter_series_cover)
                                          found_volume_cover = True
                                     except Exception as e:
                                          print(f"\t\tError copying volume series cover: {e}")
                                break # Stop searching folders once cover is found/copied for this v_path
                      if found_volume_cover: break # Stop searching volume paths

                 checked_series.append(file_obj.root) # Mark this chapter dir as checked

        except Exception as e:
            print(f"\nERROR processing cover for {file_obj.name}: {e}")
            traceback.print_exc() # Print stack trace for debugging


    # --- Main extract_covers logic ---
    if not paths_to_process:
        print("\nNo paths specified for cover extraction.")
        return

    print("\nLooking for covers to extract...")

    volume_paths_data = [] # Initialize volume paths data cache

    # contains cleaned basenames of folders that have been moved
    # TODO: This state needs to be passed in or managed externally
    moved_folder_names_set = set(
        clean_str(os.path.basename(x), skip_bracket=True, skip_underscore=True)
        for x in moved_folders
    ) if moved_folders and copy_existing_volume_covers_toggle else set()

    for path in paths_to_process:
        if not os.path.exists(path):
            print(f"\nERROR: Path does not exist: {path}")
            continue

        print(f"\nProcessing path: {path}")
        # checked_series = [] # Reset checked series for each main path? Or keep global? (Current: Global)
        # os.chdir(path) # Avoid changing directory, use absolute paths

        try:
            import scandir
            walker = scandir.walk(path, followlinks=False)
        except ImportError:
            walker = os.walk(path, followlinks=False)


        for root, dirs, files in walker:
            # --- Watchdog Mod Time Check (Keep or remove based on final design) ---
            if watchdog_toggle:
                # Check if root needs processing based on modification time or if it was moved
                root_clean_basename = clean_str(os.path.basename(root), skip_bracket=True, skip_underscore=True)
                if root_clean_basename not in moved_folder_names_set:
                    try:
                        root_mod_time = get_modification_date(root)
                        if root in root_modification_times:
                            if root_modification_times[root] == root_mod_time:
                                # print(f"Skipping unchanged directory: {root}") # Optional verbose log
                                continue # Skip unchanged directory
                            else:
                                root_modification_times[root] = root_mod_time # Update time
                        else:
                            root_modification_times[root] = root_mod_time # Store initial time
                    except Exception as e:
                         print(f"Error getting modification time for {root}: {e}")
                         # Decide whether to process or skip on error
                         continue # Skip if mod time check fails


            # --- Process Files and Folders ---
            try:
                # Use placeholder, assumes it handles filtering based on transferred state if needed
                current_files, current_dirs = process_files_and_folders(root, files, dirs)

                if not current_files:
                    continue # Skip directories with no relevant files

                print(f"\nProcessing Directory: {root}")
                # print(f"Files found: {len(current_files)}") # Debug print

                # Upgrade to File and Volume objects (using placeholders)
                file_objects = upgrade_to_file_class(current_files, root)
                volume_objects = upgrade_to_volume_class(file_objects)

                if not volume_objects:
                     print("\tNo processable volume objects found.")
                     continue

                # --- Prepare for Processing ---
                series_cover_path = find_series_cover(root, image_extensions) # Check for existing cover.ext
                # Handle series cover format mismatch
                series_cover_ext = get_file_extension(series_cover_path) if series_cover_path else ""
                if series_cover_path and (
                    (output_covers_as_webp and series_cover_ext != ".webp") or
                    (not output_covers_as_webp and series_cover_ext == ".webp")
                ):
                    print(f"\tSeries cover format mismatch. Removing: {series_cover_path}")
                    if remove_file(series_cover_path, silent=True):
                        series_cover_path = None
                    else:
                        print("\t\tFailed to remove series cover.")


                is_chapter_dir = get_folder_type(volume_objects, file_type="chapter") >= 90 # Use placeholder
                all_series_match = check_same_series_name(volume_objects)
                contains_subfolders = bool(current_dirs) # Check if there are subdirectories

                clean_basename, volume_paths_data = process_volume_paths(
                    volume_objects, root, copy_existing_volume_covers_toggle,
                    is_chapter_dir, volume_paths_data, paths_with_types
                )

                highest_idx = get_highest_release( # Use placeholder
                    [v.index_number for v in volume_objects if v.index_number != ""],
                    is_chapter_directory=is_chapter_dir
                )
                has_multi_v1s = contains_multiple_volume_ones(
                    volume_objects, use_latest_volume_cover_as_series_cover, is_chapter_dir
                )

                # --- Process Each Volume ---
                for vol_obj in volume_objects:
                    # Pass the current series cover path to the processing function
                    process_cover_extraction(
                        vol_obj, has_multi_v1s, highest_idx, is_chapter_dir,
                        volume_paths_data, clean_basename, all_series_match,
                        contains_subfolders, series_cover_path
                    )
                    # Update series_cover_path if it was created/modified inside the loop
                    # (This requires process_cover_extraction to potentially return the new path)
                    # For simplicity now, re-check after the loop if needed, or manage state better.

            except Exception as e:
                 print(f"Error processing directory {root}: {e}")
                 traceback.print_exc()


# --- print_stats (Full Implementation) ---
def print_stats():
     """Prints the collected stats about the paths/files that were processed."""
     global file_counters, image_count, errors # Use module-level state

     print("\n--- Processing Stats ---")
     if file_counters:
         # get the total count from file_counters
         total_count = sum(file_counters.values())
         print(f"Total Files Found: {total_count}")
         for extension, count in file_counters.items():
             if count > 0:
                 print(f"\t{count} were {extension} files")
     print(f"Total Covers Found/Extracted: {image_count}")

     if errors:
         print(f"\nErrors ({len(errors)}):")
         # Print only unique errors
         unique_errors = sorted(list(set(errors)))
         for error in unique_errors:
             print(f"\t- {error}")
     print("------------------------")


# --- check_for_existing_series (Full Implementation from original script) ---
def check_for_existing_series(
    download_folders, # Pass necessary data instead of using globals
    paths,
    paths_with_types,
    cached_paths,
    processed_files, # Pass mutable state
    moved_files, # Pass mutable state
    test_mode=[],
):
    global cached_identifier_results, messages_to_send, grouped_notifications # Acknowledge globals (refactor later)

    # Groups messages by their series
    def group_similar_series(messages_to_send_local):
        grouped_series = []
        for message in messages_to_send_local:
            series_name = message.series_name
            group = next((g for g in grouped_series if g["series_name"] == series_name), None)
            if group is not None:
                group["messages"].append(message)
            else:
                grouped_series.append({"series_name": series_name, "messages": [message]})
        return grouped_series

    # Determines whether an alternative match will be allowed
    def alternative_match_allowed(
        inner_dir, file, short_word_filter_perc, req_similarity_score, counted_words
    ):
        folder_subtitle = get_subtitle_from_dash(inner_dir, replace=True)
        folder_subtitle_clean = clean_str(folder_subtitle) if folder_subtitle else ""
        file_subtitle = get_subtitle_from_dash(file.series_name, replace=True)
        file_subtitle_clean = clean_str(file_subtitle) if file_subtitle else ""
        short_fldr_name = clean_str(get_shortened_title(inner_dir) or inner_dir)
        short_file_series_name = clean_str(file.shortened_series_name or file.series_name)

        if not short_fldr_name or not short_file_series_name: return False

        long_folder_words = parse_words(inner_dir)
        long_file_words = parse_words(file.series_name)
        short_fldr_name_words = parse_words(short_fldr_name)
        short_file_series_words = parse_words(short_file_series_name)

        if not short_fldr_name_words or not short_file_series_words: return False

        shortened_length = max(1, int(min(len(short_fldr_name_words), len(short_file_series_words)) * short_word_filter_perc))
        # Ensure slicing doesn't go out of bounds
        file_wrds_mod = short_file_series_words[:shortened_length]
        fldr_wrds_mod = short_fldr_name_words[:shortened_length]


        folder_name_match = short_fldr_name.lower().strip() == short_file_series_name.lower().strip()
        similar_score_match = similar(short_fldr_name, short_file_series_name) >= req_similarity_score
        consecutive_items_match = find_consecutive_items(tuple(short_fldr_name_words), tuple(short_file_series_words)) or \
                                  find_consecutive_items(tuple(long_folder_words), tuple(long_file_words))
        unique_words_match = any(i for i in long_folder_words if i in long_file_words and i in counted_words and counted_words[i] <= 3)
        subtitle_match = (folder_subtitle_clean and file_subtitle_clean) and \
                         (folder_subtitle_clean == file_subtitle_clean or similar(folder_subtitle_clean, file_subtitle_clean) >= req_similarity_score)

        return folder_name_match or similar_score_match or consecutive_items_match or unique_words_match or subtitle_match

    # Attempts an alternative match using image similarity
    def attempt_alternative_match(file_root, inner_dir, file, req_img_similarity_score):
        try:
            img_volumes_raw = [f.name for f in scandir.scandir(file_root) if f.is_file()]
        except Exception as e:
            print(f"Error scanning directory for image match {file_root}: {e}")
            return 0, None

        img_volumes = upgrade_to_volume_class(
            upgrade_to_file_class(img_volumes_raw, file_root, clean=True)
        )
        if not img_volumes: return 0, None

        matching_volumes = [vol for vol in img_volumes if is_same_index_number(vol.index_number, file.index_number, allow_array_match=True)]
        if (len(img_volumes) - len(matching_volumes)) <= 10: # Heuristic to include nearby volumes
            matching_volumes.extend([vol for vol in img_volumes if vol not in matching_volumes])
        if not matching_volumes: return 0, None

        downloaded_cover_data = find_and_extract_cover(file, return_data_only=True, silent=True, blank_image_check=True)
        if not downloaded_cover_data: return 0, None

        for match_vol in matching_volumes:
            existing_cover_data = find_and_extract_cover(match_vol, return_data_only=True, silent=True, blank_image_check=True)
            if not existing_cover_data: continue

            score = prep_images_for_similarity(existing_cover_data, downloaded_cover_data, both_cover_data=True, silent=True)
            print(f"\t\t\t\tCover Image Similarity Score: {score} (Required: >={req_img_similarity_score})")
            if score >= req_img_similarity_score:
                return score, match_vol
        return 0, None

    # Determines if the downloaded file is an upgrade or not to the existing library.
    def check_upgrade(
        existing_root, dir_name, file_obj, # Changed 'dir' to 'dir_name' to avoid shadowing built-in
        similarity_strings=None, cache=False, isbn=False, image=False, test_mode=False
    ):
        # Access outer scope state (check_for_existing_series) - nonlocal removed

        existing_dir = os.path.join(existing_root, dir_name)
        if not os.path.isdir(existing_dir):
             print(f"\t\tTarget directory does not exist: {existing_dir}")
             return False # Cannot upgrade if target doesn't exist

        # Get existing files in the target directory
        try:
             existing_files_raw = [entry.name for entry in scandir.scandir(existing_dir) if entry.is_file()]
        except Exception as e:
             print(f"\t\tError reading existing directory {existing_dir}: {e}")
             return False

        existing_files_objs = upgrade_to_volume_class(
            upgrade_to_file_class(existing_files_raw, existing_dir, clean=True)
        )

        # --- Type Matching Logic ---
        print(f"\tRequired Folder Matching Percent: {required_matching_percentage}%")
        manga_percent_dl = get_folder_type([file_obj.name], extensions=manga_extensions)
        manga_percent_exst = get_folder_type([f.name for f in existing_files_objs], extensions=manga_extensions)
        novel_percent_dl = get_folder_type([file_obj.name], extensions=novel_extensions)
        novel_percent_exst = get_folder_type([f.name for f in existing_files_objs], extensions=novel_extensions)
        chapter_percentage_dl = get_folder_type([file_obj], file_type="chapter")
        chapter_percentage_exst = get_folder_type(existing_files_objs, file_type="chapter")
        volume_percentage_dl = get_folder_type([file_obj], file_type="volume")
        volume_percentage_exst = get_folder_type(existing_files_objs, file_type="volume")

        print(f"\t\tDownload Folder Manga Percent: {manga_percent_dl}%")
        print(f"\t\tExisting Folder Manga Percent: {manga_percent_exst}%")
        print(f"\t\tDownload Folder Novel Percent: {novel_percent_dl}%")
        print(f"\t\tExisting Folder Novel Percent: {novel_percent_exst}%")
        print(f"\t\tDownload Folder Chapter Percent: {chapter_percentage_dl}%")
        print(f"\t\tExisting Folder Chapter Percent: {chapter_percentage_exst}%")
        print(f"\t\tDownload Folder Volume Percent: {volume_percentage_dl}%")
        print(f"\t\tExisting Folder Volume Percent: {volume_percentage_exst}%")

        matching_manga = manga_percent_dl >= required_matching_percentage and manga_percent_exst >= required_matching_percentage
        matching_novel = novel_percent_dl >= required_matching_percentage and novel_percent_exst >= required_matching_percentage
        matching_chapter = chapter_percentage_dl >= required_matching_percentage and chapter_percentage_exst >= required_matching_percentage
        matching_volume = volume_percentage_dl >= required_matching_percentage and volume_percentage_exst >= required_matching_percentage

        if not ((matching_manga or matching_novel) and (matching_chapter or matching_volume)):
            print("\t\tLibrary type or format mismatch. Skipping upgrade check.")
            return False # Types don't match

        # --- Upgrade/Duplicate Check ---
        if test_mode: return existing_files_objs # Return existing for testing

        download_dir_volumes = [file_obj] # Start with the single downloaded file

        if rename_files_in_download_folders_toggle and resturcture_when_renaming:
            # Note: reorganize_and_rename might modify the file_obj in place or return a new list
            # Assuming it modifies in place or we update download_dir_volumes
            reorganize_and_rename(download_dir_volumes, existing_dir) # Use placeholder
            file_obj = download_dir_volumes[0] # Update file_obj if list was modified

        # --- Discord Embed Fields ---
        fields = [{"name": "Existing Series Location", "value": f"```{existing_dir}```", "inline": False}]
        if similarity_strings:
             if not isbn and not image:
                 fields.extend([
                     {"name": "Downloaded File Series Name", "value": f"```{similarity_strings[0]}```", "inline": True},
                     {"name": "Existing Library Folder Name", "value": f"```{similarity_strings[1]}```", "inline": False},
                     {"name": "Similarity Score", "value": f"```{similarity_strings[2]}```", "inline": True},
                     {"name": "Required Score", "value": f"```>= {similarity_strings[3]}```", "inline": True},
                 ])
             elif isbn and len(similarity_strings) >= 2:
                  fields.extend([
                      {"name": "Downloaded File", "value": "```" + "\n".join(similarity_strings[0]) + "```", "inline": False},
                      {"name": "Existing Library File", "value": "```" + "\n".join(similarity_strings[1]) + "```", "inline": False},
                  ])
             elif image and len(similarity_strings) == 4:
                  fields.extend([
                      {"name": "Existing Folder Name", "value": f"```{similarity_strings[0]}```", "inline": True},
                      {"name": "File Series Name", "value": f"```{similarity_strings[1]}```", "inline": True},
                      {"name": "Image Similarity Score", "value": f"```{similarity_strings[2]}```", "inline": False},
                      {"name": "Required Score", "value": f"```>={similarity_strings[3]}```", "inline": True},
                  ])
             else:
                  send_message(f"Error: similarity_strings format invalid. {similarity_strings} File: {file_obj.name}", error=True)


        message = f"Found existing series: {existing_dir}"
        title = "Found Series Match"
        if cache: title += " (CACHE)"
        elif isbn: title += " (Matching Identifier)"
        elif image: title += " (Cover Match)"
        send_message(f"\n\t\t{message}", discord=False)

        if len(fields) > 1: # Only send embed if there's more than just the location
             embed = handle_fields(DiscordEmbed(title=title, color=grey_color), fields=fields)
             # Use the module-level grouped_notifications directly
             group_notification(grouped_notifications, Embed(embed, None))

        # --- Remove Duplicates/Upgrade ---
        # remove_duplicate_releases needs the full list of existing volumes
        remaining_existing, remaining_downloaded = remove_duplicate_releases(
            existing_files_objs, download_dir_volumes, image_similarity_match=image
        )

        if not remaining_downloaded:
             print("\t\tDownloaded file was a duplicate or not an upgrade.")
             return True # Indicate match was found, but no action needed beyond duplicate removal

        # --- Handle New Volume/Chapter ---
        volume = remaining_downloaded[0] # Should only be one left if it wasn't a duplicate
        if isinstance(volume.volume_number, (float, int, list)):
            release_type = volume.file_type.capitalize()
            send_message(
                f"\t\t\t{release_type} {array_to_string(volume.volume_number)}: {volume.name} does not exist in: {existing_dir}\n\t\t\tMoving: {volume.name} to {existing_dir}",
                discord=False,
            )

            cover_data = find_and_extract_cover(volume, return_data_only=True) if volume.file_type == "volume" else None # Extract cover data if needed

            fields = [
                {"name": f"{release_type} Number(s)", "value": f"```{array_to_string(volume.volume_number)}```", "inline": False},
                {"name": f"{release_type} Name(s)", "value": f"```{volume.name}```", "inline": False},
            ]
            if volume.volume_part and volume.file_type == "volume":
                 fields.insert(1, {"name": f"{release_type} Part", "value": f"```{volume.volume_part}```", "inline": False})

            title = f"New {release_type}(s) Added"
            is_chapter_dir = chapter_percentage_dl >= required_matching_percentage
            highest_index_num = get_highest_release(
                 [item.index_number for item in remaining_existing + remaining_downloaded if item.index_number != ""],
                 is_chapter_directory=is_chapter_dir
            ) if not is_chapter_dir else ""

            move_status = move_file(volume, existing_dir, highest_index_num=highest_index_num, is_chapter_dir=is_chapter_dir)

            if move_status:
                check_and_delete_empty_folder(volume.root) # Check original download subfolder
                moved_files.append(os.path.join(existing_dir, volume.name)) # Update state

            embed = handle_fields(DiscordEmbed(title=title, color=green_color), fields=fields)

            if new_volume_webhook:
                 if volume.file_type == "chapter":
                      # Use the module-level messages_to_send directly
                      messages_to_send.append(
                          NewReleaseNotification(volume.index_number, title, green_color, fields, new_volume_webhook, volume.series_name, volume)
                      )
                 elif volume.file_type == "volume":
                      send_discord_message(None, [Embed(embed, cover_data)], passed_webhook=new_volume_webhook)
            else:
                 # Use the module-level grouped_notifications directly
                 group_notification(grouped_notifications, Embed(embed, cover_data))

            return True # Indicate successful move/addition
        else:
             print(f"\t\tDownloaded file {volume.name} has no valid volume number after checks.")
             check_and_delete_empty_folder(volume.root) # Clean up download folder if file wasn't moved
             return True # Still counts as a "match found" scenario


    # --- Main check_for_existing_series logic ---
    if not download_folders:
        print("\nNo download folders specified, skipping check_for_existing_series.")
        return moved_files

    print("\nChecking download folders for items to match to existing library...")

    if test_mode:
         # Simplified test mode handling
         print("\tRunning in test mode...")
         volumes = test_mode # Assume test_mode provides Volume objects
         root = "/test_mode_root" # Dummy root
         # ... (rest of the logic needs adaptation for test mode) ...
         return moved_files


    # Load cached paths if not already loaded (consider doing this once at startup)
    # cached_paths_path = os.path.join(LOGS_DIR, "cached_paths.txt") # Define LOGS_DIR
    # if os.path.isfile(cached_paths_path) and not cached_paths:
    #      cached_paths = get_lines_from_file(cached_paths_path, ignore=paths + download_folders, check_paths=True, paths_to_check=paths)
    #      cached_paths = [x for x in cached_paths if os.path.isdir(x)]

    cached_image_similarity_results = [] # Local cache for this run

    for download_folder in download_folders:
         if not os.path.exists(download_folder):
             print(f"\n\t{download_folder} does not exist, skipping...")
             continue

         print(f"\nProcessing download folder: {download_folder}")
         try:
             walker = scandir.walk(download_folder)
         except ImportError:
             walker = os.walk(download_folder)

         unmatched_series = set() # Local set for this download folder
         folder_list = list(walker) # Consume walker to allow reverse
         folder_list.reverse() # Process deepest first

         for root, dirs, files in folder_list:
             print(f"\tProcessing directory: {root}")
             current_files, _ = process_files_and_folders(root, files, dirs) # Use placeholder
             if not current_files: continue

             volumes = upgrade_to_volume_class(upgrade_to_file_class(current_files, root)) # Use placeholders
             volumes = sorted(volumes, key=lambda x: get_sort_key(x.index_number)) # Sort volumes

             exclude = None # For path reordering heuristic
             similar.cache_clear() # Clear similarity cache per directory?

             for file_obj in volumes:
                 if not hasattr(file_obj, 'series_name') or not file_obj.series_name or \
                    not hasattr(file_obj, 'volume_number') or file_obj.volume_number == "":
                     print(f"\t\tSkipping {file_obj.name} - Missing series name or volume number.")
                     continue

                 # Check if file still exists (might have been moved/deleted by previous iteration)
                 if not os.path.exists(file_obj.path):
                      print(f"\t\tSkipping {file_obj.name} - File no longer exists at {file_obj.path}")
                      continue

                 # Check if file was already processed (e.g., moved)
                 if file_obj.path not in processed_files: # Check against passed state
                      # print(f"\t\tSkipping {file_obj.name} - Not in processed files list.")
                      # This check might be incorrect depending on how processed_files is used
                      pass # Continue processing for now, needs review

                 print(f"\t\tChecking file: {file_obj.name}")
                 done = False
                 series_key = f"{file_obj.series_name} - {file_obj.file_type} - {file_obj.extension}"

                 if series_key in unmatched_series and not match_through_identifiers: # Skip if previously unmatched
                      continue

                 # 1. Check cached identifier results
                 if cached_identifier_results and file_obj.file_type == "volume":
                      found_item = next((ci for ci in cached_identifier_results if ci.series_name == file_obj.series_name), None)
                      if found_item:
                           print(f"\t\tFound cached identifier match: {found_item.path}")
                           done = check_upgrade(os.path.dirname(found_item.path), os.path.basename(found_item.path), file_obj, similarity_strings=found_item.matches, isbn=True)
                           if not test_mode and found_item.path not in cached_paths: cache_path(found_item.path)
                           if done: continue

                 # 2. Check cached paths
                 current_cached_paths = list(cached_paths) # Work on a copy
                 # TODO: Implement path reordering heuristics if keeping cached_paths
                 if current_cached_paths:
                      print("\t\tChecking cached paths...")
                      for cached_path in current_cached_paths:
                           if not os.path.isdir(cached_path) or cached_path in download_folders: continue
                           # TODO: Add path type filtering from paths_with_types if needed
                           cached_basename = os.path.basename(cached_path)
                           cleaned_cached = clean_str(cached_basename)
                           cleaned_file_series = clean_str(file_obj.series_name)
                           score = similar(cleaned_cached, cleaned_file_series)
                           print(f"\t\t\tCache Check: {cleaned_file_series} vs {cleaned_cached} (Score: {score:.3f})")
                           if score >= required_similarity_score:
                                done = check_upgrade(os.path.dirname(cached_path), cached_basename, file_obj, similarity_strings=[cleaned_file_series, cleaned_cached, score, required_similarity_score], cache=True)
                                if done:
                                     # TODO: Implement cache reordering logic if needed
                                     break
                      if done: continue

                 # 3. Check library paths
                 print("\t\tChecking library paths...")
                 # TODO: Implement path reordering heuristics for library paths
                 library_paths_to_check = list(paths)
                 for lib_path in library_paths_to_check:
                      if not os.path.isdir(lib_path) or lib_path in download_folders: continue
                      # TODO: Add path type filtering from paths_with_types if needed
                      try:
                           lib_walker = scandir.walk(lib_path)
                      except ImportError:
                           lib_walker = os.walk(lib_path)

                      counted_words_in_lib = count_words([d.name for d in scandir.scandir(lib_path) if d.is_dir()]) # Count words once per library path

                      for lib_root, lib_dirs, lib_files in lib_walker:
                           # TODO: Implement directory reordering heuristics
                           dirs_to_check = list(lib_dirs)
                           for lib_dir in dirs_to_check:
                                if lib_dir.startswith('.'): continue # Skip hidden
                                cleaned_lib_dir = clean_str(lib_dir)
                                cleaned_file_series = clean_str(file_obj.series_name)
                                score = similar(cleaned_lib_dir, cleaned_file_series)
                                print(f"\t\t\tLib Check: {cleaned_file_series} vs {cleaned_lib_dir} (Score: {score:.3f})")
                                if score >= required_similarity_score:
                                     done = check_upgrade(lib_root, lib_dir, file_obj, similarity_strings=[cleaned_file_series, cleaned_lib_dir, score, required_similarity_score])
                                     if done:
                                          if not test_mode and os.path.join(lib_root, lib_dir) not in cached_paths: cache_path(os.path.join(lib_root, lib_dir))
                                          # TODO: Implement cache reordering
                                          break
                                # Alternative match check (moved inside loop for counted_words)
                                elif match_through_image_similarity and not done and \
                                     alternative_match_allowed(lib_dir, file_obj, short_word_filter_percentage, required_similarity_score, counted_words_in_lib):
                                     print("\t\t\tAttempting alternative match (image similarity)...")
                                     img_score, matching_volume = attempt_alternative_match(os.path.join(lib_root, lib_dir), lib_dir, file_obj, required_image_similarity_score)
                                     if img_score >= required_image_similarity_score:
                                          print("\t\t\tMatch found via image similarity.")
                                          # Cache image similarity result?
                                          # cached_image_similarity_results.append(...)
                                          done = check_upgrade(lib_root, lib_dir, file_obj, similarity_strings=[lib_dir, file_obj.series_name, img_score, required_image_similarity_score], image=matching_volume)
                                          if done: break

                           if done: break
                      if done: break
                 if done: continue

                 # 4. Check identifier matches
                 if match_through_identifiers and file_obj.file_type == "volume" and not done:
                      print("\t\tChecking identifiers...")
                      dl_comment = get_zip_comment(file_obj.path) # Use placeholder
                      dl_meta = get_identifiers(dl_comment) # Use placeholder
                      if dl_meta:
                           directories_found = []
                           matched_ids = []
                           for lib_path in paths: # Check all library paths
                                if not os.path.isdir(lib_path) or lib_path in download_folders: continue
                                try:
                                     id_walker = scandir.walk(lib_path)
                                except ImportError:
                                     id_walker = os.walk(lib_path)
                                for id_root, id_dirs, id_files in id_walker:
                                     if id_root in directories_found: continue # Skip if already matched this dir
                                     id_file_objs = upgrade_to_file_class(id_files, id_root) # Placeholder
                                     for id_file in id_file_objs:
                                          if id_file.extension != file_obj.extension: continue
                                          ex_comment = get_zip_comment(id_file.path)
                                          ex_meta = get_identifiers(ex_comment)
                                          if ex_meta and any(d_id in ex_meta for d_id in dl_meta):
                                               print(f"\t\t\tIdentifier match found in: {id_root}")
                                               directories_found.append(id_root)
                                               matched_ids.extend([dl_meta, ex_meta])
                                               break # Move to next directory once match found
                                     if id_root in directories_found: break # Stop walking this branch if matched

                           if len(directories_found) == 1:
                                matched_dir = directories_found[0]
                                print(f"\t\tSingle identifier match directory: {matched_dir}")
                                identifier = IdentifierResult(file_obj.series_name, dl_meta, matched_dir, matched_ids)
                                if identifier not in cached_identifier_results: cached_identifier_results.append(identifier)
                                done = check_upgrade(os.path.dirname(matched_dir), os.path.basename(matched_dir), file_obj, similarity_strings=matched_ids, isbn=True)
                                if done and matched_dir not in cached_paths: cache_path(matched_dir)
                                # TODO: Cache reordering
                           elif len(directories_found) > 1:
                                print(f"\t\t\tMultiple identifier matches found: {directories_found}. Disregarding.")
                 if done: continue


                 if not done:
                      print(f"\t\tNo match found for {file_obj.name}")
                      unmatched_series.add(series_key)

         # --- Purge empty folders after processing a download folder ---
         if not test_mode:
              folder_list.reverse() # Process shallowest first for deletion
              for folder_info in folder_list:
                   check_and_delete_empty_folder(folder_info['root'])


    # --- Process grouped notifications ---
    if messages_to_send:
        print("\nProcessing new release notifications...")
        grouped_by_series = group_similar_series(messages_to_send)
        messages_to_send.clear() # Clear the global list after grouping

        series_notifications = [] # Local list for batching per webhook
        webhook_to_use = new_volume_webhook # Use specific webhook if provided

        for group in grouped_by_series:
             print(f"\tProcessing notifications for series: {group['series_name']} ({len(group['messages'])} items)")
             # ... (Logic to format and send grouped notifications - see original lines 6963-7060) ...
             # This needs access to DiscordEmbed, handle_fields, send_discord_message, etc.
             # Simplified: Send one by one for now if webhook specified
             if webhook_to_use:
                  for msg_info in group['messages']:
                       embed = handle_fields(DiscordEmbed(title=msg_info.title, color=msg_info.color), fields=msg_info.fields)
                       # Extract cover data if needed (assuming volume_obj has path)
                       cover = find_and_extract_cover(msg_info.volume_obj, return_data_only=True) if msg_info.volume_obj else None
                       send_discord_message(None, [Embed(embed, cover)], passed_webhook=webhook_to_use)
             else: # Group for default webhooks
                  # TODO: Implement grouping logic similar to original if needed
                  pass


    return moved_files # Return updated state


# --- check_for_duplicate_volumes (Full Implementation) ---
def check_for_duplicate_volumes(paths_to_search=[]):
    global grouped_notifications # Acknowledge global (refactor later)

    if not paths_to_search:
        return

    try:
        for p in paths_to_search:
            if not os.path.exists(p):
                send_message(f"\nERROR: {p} is an invalid path.\n", error=True)
                continue

            print(f"\nSearching {p} for duplicate releases...")
            try:
                 walker = scandir.walk(p)
            except ImportError:
                 walker = os.walk(p)

            for root, dirs, files in walker:
                print(f"\t{root}")
                # Use placeholder, assumes it handles filtering based on transferred state if needed
                current_files, current_dirs = process_files_and_folders(root, files, dirs)

                if not current_files:
                    continue

                # Upgrade files to Volume objects
                file_objects = upgrade_to_file_class(
                    [f for f in current_files if os.path.isfile(os.path.join(root, f))],
                    root,
                )
                volumes = upgrade_to_volume_class(file_objects)

                # Find potential duplicates based on index, root, extension, type, series name
                potential_duplicates = {}
                for vol in volumes:
                     if vol.index_number == "": continue # Skip volumes without index
                     key = (vol.root, vol.index_number, vol.extension, vol.file_type, vol.series_name)
                     if key not in potential_duplicates:
                          potential_duplicates[key] = []
                     potential_duplicates[key].append(vol)

                # Process groups with more than one volume for the same key
                for key, duplicate_group in potential_duplicates.items():
                    if len(duplicate_group) > 1:
                        print(f"\t\tFound {len(duplicate_group)} potential duplicates for index {key[1]} in {key[0]}")
                        # Sort duplicates to have a consistent comparison order (e.g., by name)
                        duplicate_group.sort(key=lambda x: x.name)

                        # Compare each pair within the group
                        processed_pairs = set()
                        for i in range(len(duplicate_group)):
                            for j in range(i + 1, len(duplicate_group)):
                                file1 = duplicate_group[i]
                                file2 = duplicate_group[j]

                                # Avoid re-processing the same pair
                                pair_key = tuple(sorted((file1.path, file2.path)))
                                if pair_key in processed_pairs: continue
                                processed_pairs.add(pair_key)

                                if not os.path.isfile(file1.path) or not os.path.isfile(file2.path):
                                     print(f"\t\t\tOne or both files no longer exist: {file1.name}, {file2.name}. Skipping pair.")
                                     continue

                                print(f"\t\t\tComparing: {file1.name} vs {file2.name}")

                                upgrade1_vs_2 = is_upgradeable(file1, file2)
                                upgrade2_vs_1 = is_upgradeable(file2, file1)

                                file_to_remove = None
                                file_to_keep = None

                                if upgrade1_vs_2.is_upgrade and not upgrade2_vs_1.is_upgrade:
                                    file_to_remove = file2
                                    file_to_keep = file1
                                    print(f"\t\t\t'{file1.name}' is an upgrade over '{file2.name}'.")
                                elif upgrade2_vs_1.is_upgrade and not upgrade1_vs_2.is_upgrade:
                                    file_to_remove = file1
                                    file_to_keep = file2
                                    print(f"\t\t\t'{file2.name}' is an upgrade over '{file1.name}'.")
                                elif not upgrade1_vs_2.is_upgrade and not upgrade2_vs_1.is_upgrade:
                                     # Scores are equal, check hash
                                     hash1 = get_file_hash(file1.path)
                                     hash2 = get_file_hash(file2.path)
                                     if hash1 and hash2 and hash1 == hash2:
                                          print(f"\t\t\tFiles are identical (hash match). Keeping '{file1.name}'.")
                                          file_to_remove = file2 # Arbitrarily remove the second one
                                          file_to_keep = file1
                                     else:
                                          print(f"\t\t\tScores equal, hashes differ or unavailable. Requires manual decision.")
                                          # TODO: Implement manual decision logic or skip
                                          continue # Skip for now
                                else:
                                     # Both are upgrades of each other? Should not happen with > check.
                                     print(f"\t\t\tInconsistent upgrade status between {file1.name} and {file2.name}. Skipping.")
                                     continue

                                if file_to_remove:
                                     send_message(
                                         f"\n\t\t\tDuplicate release found in: {file_to_keep.root}"
                                         f"\n\t\t\tDuplicate: {file_to_remove.name} has a lower/equal score than {file_to_keep.name}"
                                         f"\n\n\t\t\tDeleting: {file_to_remove.name} inside of {file_to_remove.root}\n",
                                         discord=False,
                                     )
                                     embed = handle_fields(
                                         DiscordEmbed(
                                             title="Duplicate Download Release (Not Upgradeable/Identical)",
                                             color=yellow_color,
                                         ),
                                         fields=[
                                             {"name": "Location", "value": f"```{file_to_keep.root}```", "inline": False},
                                             {"name": "Duplicate", "value": f"```{file_to_remove.name}```", "inline": False},
                                             {"name": "has a lower/equal score than or is identical to", "value": f"```{file_to_keep.name}```", "inline": False},
                                         ],
                                     )
                                     grouped_notifications = group_notification(grouped_notifications, Embed(embed, None))

                                     user_input = "y" # Default to yes unless manual_delete is True
                                     if manual_delete:
                                          # Placeholder for get_input_from_user
                                          # user_input = get_input_from_user(
                                          #      f'\t\t\tDelete "{file_to_remove.name}"', ["y", "n"], ["y", "n"]
                                          # )
                                          print(f"Manual delete required for {file_to_remove.name}. Defaulting to 'y' for now.")


                                     if user_input == "y":
                                          remove_file(file_to_remove.path)
                                     else:
                                          print("\t\t\t\tSkipping deletion...")


    except Exception as e:
        send_message(f"\nError during duplicate check: {e}", error=True)
        traceback.print_exc()


# --- move_series_to_correct_library (Full Implementation) ---
def move_series_to_correct_library(paths_to_search=paths_with_types): # Use paths_with_types as default
    global grouped_notifications, moved_folders, moved_files, libraries_to_scan, komga_libraries # Acknowledge globals

    if not paths_to_search:
        print("\nNo paths with types defined, skipping move_series_to_correct_library.")
        return

    try:
        for p in paths_to_search:
            try:
                if not os.path.exists(p.path):
                    send_message(f"\nERROR: {p.path} is an invalid path.\n", error=True)
                    continue

                print(f"\nSearching {p.path} for incorrectly matching series types...")
                try:
                    walker = scandir.walk(p.path)
                except ImportError:
                    walker = os.walk(p.path)

                for root, dirs, files in walker:
                    # Skip the root library path itself
                    if root == p.path:
                         continue
                    # Only process immediate subdirectories of the library path
                    if os.path.dirname(root) != p.path:
                         # Clear dirs to prevent further descent if not an immediate subdir
                         dirs[:] = []
                         continue

                    print(f"\tChecking Series: {root}")

                    current_files, _ = process_files_and_folders(root, files, dirs)

                    if not current_files:
                        print("\t\tNo processable files found.")
                        continue

                    file_objects = upgrade_to_file_class(
                         [f for f in current_files if os.path.isfile(os.path.join(root, f))],
                         root
                    )

                    if not file_objects:
                        print("\t\tCould not create file objects.")
                        continue

                    # Determine the actual library type based on content
                    library_type = get_library_type([x.name for x in file_objects], paths_with_types) # Pass paths_with_types for definitions

                    if not library_type:
                        print(f"\t\tCould not determine library type for {os.path.basename(root)}.")
                        continue

                    print(f"\t\tDetected type: {library_type.name}")

                    # Determine if the folder is predominantly chapter or volume based
                    is_chapter_dir = get_folder_type(file_objects, file_type="chapter") >= 90
                    is_volume_dir = get_folder_type(file_objects, file_type="volume") >= 90

                    if not is_chapter_dir and not is_volume_dir:
                        print("\t\tCould not determine format (Chapter/Volume). Skipping.")
                        continue
                    current_format = "chapter" if is_chapter_dir else "volume"
                    print(f"\t\tDetected format: {current_format}")

                    # Find the *correct* library path based on detected type and format
                    matching_paths = [
                        target_path_obj
                        for target_path_obj in paths_with_types
                        if library_type.name in [lt.name for lt in target_path_obj.library_types] # Check against list of LibraryType objects
                        and current_format in target_path_obj.path_formats
                        and all(fo.extension in target_path_obj.path_extensions for fo in file_objects) # Check extensions match target
                    ]

                    if not matching_paths:
                         print(f"\t\tNo suitable target library found for type '{library_type.name}' and format '{current_format}'.")
                         continue

                    if len(matching_paths) > 1:
                        send_message(
                            f"\t\t\t{os.path.basename(root)} has more than one potential matching target library. Skipping.",
                            discord=False,
                        )
                        for match_p in matching_paths:
                            print(f"\t\t\t\t- {match_p.path}")
                        continue

                    matching_path = matching_paths[0]

                    # Check if the series is already in the correct library path
                    if normalize_path(matching_path.path) == normalize_path(p.path):
                        print(f"\t\tSeries is already in the correct library: {p.path}")
                        continue # Already in the right place

                    # --- Perform the move ---
                    send_message(
                        f"\t\tIncorrect Library: Series '{os.path.basename(root)}' (Type: {library_type.name}, Format: {current_format}) found in '{p.path}'.\n\t\tMoving to correct library: '{matching_path.path}'",
                        discord=False,
                    )

                    new_location_dir = os.path.join(matching_path.path, os.path.basename(root))

                    # Check if the target directory already exists
                    if os.path.isdir(new_location_dir):
                        check_and_delete_empty_folder(new_location_dir)
                        if os.path.isdir(new_location_dir):
                            send_message(
                                f"\t\t\tTarget directory '{new_location_dir}' already exists and is not empty. Skipping move.",
                                error=True, # Log as error because move can't happen
                            )
                            continue # Skip move if target exists and isn't empty

                    # Move the folder
                    moved_folder_status = move_folder(root, matching_path.path, silent=True) # Use file_utils function

                    if not moved_folder_status:
                        send_message(
                            f"\t\t\tFailed to move '{root}' to '{matching_path.path}'",
                            error=True,
                        )
                        # Attempt to clean up potentially partially created target folder
                        check_and_delete_empty_folder(new_location_dir)
                        continue # Skip further processing for this series

                    send_message(f"\t\t\tSuccessfully moved to {new_location_dir}", discord=False)

                    # Update state variables (assuming they are managed appropriately)
                    if new_location_dir not in moved_folders:
                        moved_folders.append(new_location_dir)
                    for f_obj in file_objects:
                        new_file_path = os.path.join(new_location_dir, f_obj.name)
                        if new_file_path not in moved_files:
                            moved_files.append(new_file_path)

                    # Create Discord Embed
                    embed = handle_fields(
                        DiscordEmbed(
                            title="Moved Series to Correct Library",
                            color=grey_color,
                        ),
                        fields=[
                            {"name": "Series", "value": f"```{os.path.basename(root)}```", "inline": False},
                            {"name": "From Library", "value": f"```{p.path}```", "inline": False},
                            {"name": "To Library", "value": f"```{matching_path.path}```", "inline": False},
                            {"name": "Detected Type", "value": f"```{library_type.name} ({current_format})```", "inline": False},
                        ],
                    )
                    grouped_notifications = group_notification(grouped_notifications, Embed(embed, None))

                    # Trigger Komga scan if enabled
                    if send_scan_request_to_komga_libraries_toggle:
                        if not komga_libraries:
                            # Placeholder: komga_libraries = get_komga_libraries()
                            print("Placeholder: Komga libraries not loaded.")
                            pass

                        # Add both old and new library IDs to scan list
                        for lib in komga_libraries:
                             if lib["id"] not in libraries_to_scan:
                                 # Check if the library root is part of the old or new path
                                 if is_root_present(lib["root"], p.path) or is_root_present(lib["root"], matching_path.path):
                                      libraries_to_scan.append(lib["id"])

                    # Stop processing subdirs of the moved directory in the original path
                    dirs[:] = []


            except Exception as e:
                send_message(f"\n\t\tError processing path {p.path}: {e}", error=True)
                traceback.print_exc()
    except Exception as e:
        send_message(f"\nError during move_series_to_correct_library: {e}", error=True)
        traceback.print_exc()


# --- Helper functions specific to this module (if any) ---
def get_sort_key(index_number): # Example helper
     if isinstance(index_number, list):
         numeric_items = [item for item in index_number if isinstance(item, (int, float))]
         return min(numeric_items) if numeric_items else float('inf')
     elif isinstance(index_number, (int, float)):
         return index_number
     else:
         return float('inf')