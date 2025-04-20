# komga_cover_extractor/series_matching.py
import os
import re
import time
import traceback
from datetime import datetime

# TODO: Refactor config imports
try:
    from .config import (
        paths, download_folders, paths_with_types,
        required_similarity_score, match_through_identifiers,
        match_through_image_similarity, required_image_similarity_score,
        rename_files_in_download_folders_toggle, resturcture_when_renaming,
        output_chapter_covers_to_discord, new_volume_webhook, manual_delete,
        short_word_filter_percentage, LOGS_DIR,
        # Global state vars (TODO: Refactor state management)
        # moved_files, grouped_notifications, cached_paths, cached_identifier_results,
        # Colors
        yellow_color, green_color, grey_color,
        # Toggles
        watchdog_toggle
    )
except ImportError:
    print("WARN: Could not import from .config, using placeholder values in series_matching.")
    paths = []
    download_folders = []
    paths_with_types = []
    required_similarity_score = 0.9
    match_through_identifiers = False
    match_through_image_similarity = False
    required_image_similarity_score = 0.9
    rename_files_in_download_folders_toggle = False
    resturcture_when_renaming = False
    output_chapter_covers_to_discord = False
    new_volume_webhook = None
    manual_delete = False
    short_word_filter_percentage = 0.7
    LOGS_DIR = "logs"
    yellow_color, green_color, grey_color = 0, 0, 0
    watchdog_toggle = False


# TODO: Import necessary functions from utility modules
try:
    from .log_utils import send_message, write_to_file
    from .file_utils import (
        process_files_and_folders, get_file_size, remove_file, move_file,
        create_folder_obj, upgrade_to_file_class, check_and_delete_empty_folder,
        cache_path, upgrade_to_volume_class, sort_volumes # Added sort_volumes
    )
    from .string_utils import (
        similar, clean_str, get_subtitle_from_dash, get_shortened_title,
        parse_words, find_consecutive_items, count_words, move_strings_to_top,
        array_to_string, abbreviate_numbers, complete_num_array, is_same_index_number,
        reorganize_and_rename # Keep reorganize_and_rename here if check_upgrade needs it directly? Or move check_upgrade later?
    )
    from .image_utils import find_and_extract_cover, prep_images_for_similarity
    from .archive_utils import get_zip_comment, get_zip_comment_cache, get_identifiers as get_ids_from_comment
    from .discord_utils import handle_fields, group_notification, send_discord_message, DiscordEmbed
    from .models import File, Folder, Volume, Embed, UpgradeResult, RankedKeywordResult, Keyword, IdentifierResult, Image_Result, NewReleaseNotification # Import needed models
    # from .core_logic import get_keyword_scores, is_upgradeable # Import if check_upgrade is moved
except ImportError as e:
    print(f"FATAL: Failed to import dependencies in series_matching: {e}")
    def send_message(msg, error=False, discord=False): print(f"{'ERROR: ' if error else ''}{msg}")
    def write_to_file(*args, **kwargs): pass
    def process_files_and_folders(r, f, d, **kwargs): return f, d
    def get_file_size(p): return 0
    def remove_file(*args, **kwargs): return False
    def move_file(*args, **kwargs): return False
    def create_folder_obj(*args, **kwargs): return None
    def upgrade_to_file_class(*args, **kwargs): return []
    def check_and_delete_empty_folder(*args, **kwargs): pass
    def cache_path(*args, **kwargs): pass
    def upgrade_to_volume_class(*args, **kwargs): return []
    def sort_volumes(v): return v
    def similar(a, b): return 0.0
    def clean_str(s, **kwargs): return s
    def get_subtitle_from_dash(*args, **kwargs): return ""
    def get_shortened_title(t): return t
    def parse_words(s): return s.split()
    def find_consecutive_items(*args, **kwargs): return False
    def count_words(l): return {}
    def move_strings_to_top(t, a): return a
    def array_to_string(a, **kwargs): return str(a)
    def abbreviate_numbers(n): return str(n)
    def complete_num_array(a): return a
    def is_same_index_number(*args, **kwargs): return False
    def reorganize_and_rename(f, d): return f
    def find_and_extract_cover(*args, **kwargs): return None
    def prep_images_for_similarity(*args, **kwargs): return 0.0
    def get_zip_comment(p): return ""
    def get_zip_comment_cache(p): return ""
    def get_ids_from_comment(c): return []
    def handle_fields(e, f): return e
    def group_notification(n, e, **kwargs): n.append(e); return n
    def send_discord_message(*args, **kwargs): pass
    class DiscordEmbed: pass
    class File: pass
    class Folder: pass
    class Volume: pass
    class Embed: pass
    class UpgradeResult: pass
    class RankedKeywordResult: pass
    class Keyword: pass
    class IdentifierResult: pass
    class Image_Result: pass
    class NewReleaseNotification: pass


# --- Global State (TODO: Refactor this) ---
moved_files = []
messages_to_send = [] # For chapter notifications
grouped_notifications = []
cached_paths = []
cached_identifier_results = []
cached_image_similarity_results = [] # Assuming this state is managed here


# --- Helper Functions (Potentially move to appropriate utils later) ---

# Placeholder for get_keyword_scores needed by is_upgradeable
# TODO: Implement actual keyword scoring logic, possibly in string_utils or a dedicated module
def get_keyword_scores(releases):
    results = []
    for release in releases:
        # Dummy implementation: score is 0
        results.append(RankedKeywordResult(0.0, []))
    return results

# Checks if the downloaded release is an upgrade for the current release.
# Moved from original script (lines 5366-5508)
def is_upgradeable(downloaded_release, current_release):
    """Checks if downloaded_release is an upgrade compared to current_release based on keyword scores."""
    downloaded_release_result = None
    current_release_result = None

    if downloaded_release.name == current_release.name:
        # If names are identical, scores will be too, no upgrade possible based on score
        results = get_keyword_scores([downloaded_release]) # Use local placeholder/imported function
        downloaded_release_result = results[0]
        current_release_result = results[0] # Same result object
    else:
        results = get_keyword_scores([downloaded_release, current_release]) # Use local placeholder/imported function
        downloaded_release_result = results[0]
        current_release_result = results[1]

    # Determine upgrade status based on total score
    is_upgrade = downloaded_release_result.total_score > current_release_result.total_score

    upgrade_result = UpgradeResult(
        is_upgrade,
        downloaded_release_result,
        current_release_result,
    )
    return upgrade_result


# Removes the duplicate after determining it's upgrade status, otherwise, it upgrades
# Moved from original script (lines 3954-4130)
def remove_duplicate_releases(
    original_releases, downloaded_releases, image_similarity_match=False
):
    """Compares releases and removes/replaces duplicates based on upgrade status."""
    global moved_files, grouped_notifications # Manage global state

    # Helper function (originally local)
    def get_file_tags_info(result):
        tags = result.keywords
        if tags:
            # Ensure tag object has name and score attributes
            return ", ".join([f"{getattr(tag, 'name', '?')} ({getattr(tag, 'score', '?')})" for tag in tags])
        return "None"

    # Helper function (originally local)
    def get_file_size_info(path):
        size_bytes = get_file_size(path) # Use imported file_utils function
        return f"{round(size_bytes / 1000000, 1)} MB" if size_bytes is not None else None

    new_original_releases = original_releases[:] # Use slicing for copy
    new_downloaded_releases = downloaded_releases[:]

    for download in downloaded_releases:
        # Ensure download object is still valid and file exists
        if download not in new_downloaded_releases or not os.path.isfile(download.path):
            continue

        if download.index_number == "":
            send_message(f"\n\t\t{download.file_type.capitalize()} number empty/missing in: {download.name}", error=True)
            continue

        # Determine highest index number (assuming get_highest_release is available)
        try:
            all_indices = [item.index_number for item in new_original_releases + new_downloaded_releases if item.index_number != ""]
            hashable_indices = [tuple(idx) if isinstance(idx, list) else idx for idx in all_indices]
            # highest_index_num = get_highest_release(tuple(hashable_indices), is_chapter_directory=False) # Assuming volume check here
            # TODO: get_highest_release needs to be imported or defined
            highest_index_num = "" # Placeholder
        except Exception:
             highest_index_num = ""

        for original in original_releases:
            if original not in new_original_releases or not os.path.isfile(original.path): continue
            if not os.path.isfile(download.path): break # Download removed previously

            if download.file_type != original.file_type: continue

            indices_match = is_same_index_number(download.index_number, original.index_number) # Use imported function
            image_match_cond = (image_similarity_match and hasattr(image_similarity_match, "name") and image_similarity_match.name == original.name)

            if not (indices_match or image_match_cond): continue

            try:
                upgrade_status = is_upgradeable(download, original) # Use local function
            except Exception as upgrade_err:
                 send_message(f"Error checking upgrade status for {download.name} vs {original.name}: {upgrade_err}", error=True)
                 continue

            original_file_tags = get_file_tags_info(upgrade_status.current_ranked_result)
            downloaded_file_tags = get_file_tags_info(upgrade_status.downloaded_ranked_result)
            original_file_size = get_file_size_info(original.path)
            downloaded_file_size = get_file_size_info(download.path)

            fields = [
                {"name": "From", "value": f"```{original.name}```", "inline": False},
                {"name": "Score", "value": str(upgrade_status.current_ranked_result.total_score), "inline": True},
                {"name": "Tags", "value": str(original_file_tags), "inline": True},
            ]
            if original_file_size: fields.append({"name": "Size", "value": str(original_file_size), "inline": True})
            fields.extend([
                {"name": "To", "value": f"```{download.name}```", "inline": False},
                {"name": "Score", "value": str(upgrade_status.downloaded_ranked_result.total_score), "inline": True},
                {"name": "Tags", "value": str(downloaded_file_tags), "inline": True},
            ])
            if downloaded_file_size: fields.append({"name": "Size", "value": str(downloaded_file_size), "inline": True})

            status = "Not Upgradeable" if not upgrade_status.is_upgrade else "Upgrade"
            verb = "not an" if not upgrade_status.is_upgrade else "an"
            action = "Deleting" if not upgrade_status.is_upgrade else "Upgrading"
            color = yellow_color if not upgrade_status.is_upgrade else green_color

            send_message(f"\t\t{status}: {download.name} is {verb} upgrade to: {original.name}\n\t{action}: {download.name} from download folder.", discord=False)

            try:
                embed = handle_fields(DiscordEmbed(title=f"Upgrade Process ({status})", color=color), fields=fields) # Use imported discord_utils
                grouped_notifications = group_notification(grouped_notifications, Embed(embed, None)) # Use imported discord_utils
            except Exception as discord_err:
                 send_message(f"Error creating/grouping Discord embed for upgrade check: {discord_err}", error=True)

            if upgrade_status.is_upgrade:
                # Handle multi-volume upgrades (remove other parts)
                if download.multi_volume and not original.multi_volume and isinstance(download.volume_number, list):
                    files_to_remove = [
                        orig_vol for orig_vol in new_original_releases
                        if orig_vol.path != original.path
                        and orig_vol.volume_number in download.volume_number
                        and orig_vol.volume_part == original.volume_part
                    ]
                    for file_to_remove in files_to_remove:
                        if remove_file(file_to_remove.path): # Use imported file_utils
                             if file_to_remove in new_original_releases: new_original_releases.remove(file_to_remove)

                # Replace the original file with the download
                # TODO: replace_file needs to be defined or imported
                # replace_file_status = replace_file(original, download, highest_index_num=highest_index_num)
                replace_file_status = False # Placeholder
                print("WARN: replace_file function not implemented yet in series_matching.")


                if replace_file_status:
                    new_path = os.path.join(original.root, download.name)
                    if new_path not in moved_files: moved_files.append(new_path)
                    if download in new_downloaded_releases: new_downloaded_releases.remove(download)
                    if original in new_original_releases: new_original_releases.remove(original)
                    break
                else:
                     send_message(f"Upgrade failed for {download.name} -> {original.name}. Check logs.", error=True)
                     break
            else: # Not an upgrade, remove the downloaded file
                user_input = get_input_from_user(f'\t\t\tDelete "{download.name}"', ["y", "n"], ["y", "n"]) if manual_delete else "y" # Use imported misc_utils
                if user_input == 'y':
                    if remove_file(download.path): # Use imported file_utils
                        if download in new_downloaded_releases: new_downloaded_releases.remove(download)
                        break
                    else:
                         send_message(f"Failed to delete non-upgrade file: {download.path}", error=True)
                         break
                else:
                     print("\t\t\tSkipping deletion.")
                     break

    return new_original_releases, new_downloaded_releases


# --- Core Series Matching and Duplicate Check Logic ---

# Checks for an existing series by comparing downloaded files to library folders.
# Moved from original script (lines 6112-7084)
def check_for_existing_series(
    test_mode=[], # For testing specific files
    test_paths=paths,
    test_download_folders=download_folders,
    test_paths_with_types=paths_with_types,
    test_cached_paths=cached_paths,
):
    """Matches downloaded files to existing library series folders."""
    global cached_paths, cached_identifier_results, messages_to_send, grouped_notifications # Manage global state
    global cached_image_similarity_results # Manage image cache state

    # Helper: Groups chapter notification messages by series
    def group_similar_series(messages):
        # ... (implementation from original script lines 6121-6150) ...
        grouped = {}
        for msg in messages:
            series = msg.series_name
            if series not in grouped: grouped[series] = []
            grouped[series].append(msg)
        # Convert dict to list of dicts expected by original code
        return [{"series_name": series, "messages": msgs} for series, msgs in grouped.items()]


    # Helper: Determines if an alternative matching method should be attempted
    def alternative_match_allowed(inner_dir, file, short_word_filter_percentage, required_similarity_score, counted_words):
        # ... (implementation from original script lines 6152-6236) ...
        # Depends on: get_subtitle_from_dash, clean_str, get_shortened_title, parse_words, similar, find_consecutive_items
        folder_subtitle = get_subtitle_from_dash(inner_dir, replace=True) # Use imported string_utils
        folder_subtitle_clean = clean_str(folder_subtitle) if folder_subtitle else "" # Use imported string_utils

        file_subtitle = get_subtitle_from_dash(file.series_name, replace=True) # Use imported string_utils
        file_subtitle_clean = clean_str(file_subtitle) if file_subtitle else "" # Use imported string_utils

        short_fldr_name = clean_str(get_shortened_title(inner_dir) or inner_dir) # Use imported string_utils
        short_file_series_name = clean_str(file.shortened_series_name or file.series_name) # Use imported string_utils

        if not short_fldr_name or not short_file_series_name: return False

        long_folder_words = parse_words(inner_dir) # Use imported string_utils
        long_file_words = parse_words(file.series_name) # Use imported string_utils
        short_fldr_name_words = parse_words(short_fldr_name) # Use imported string_utils
        short_file_series_words = parse_words(short_file_series_name) # Use imported string_utils

        if not short_fldr_name_words or not short_file_series_words: return False

        # Shorten word lists for comparison
        shortened_length = max(1, int(min(len(short_fldr_name_words), len(short_file_series_words)) * short_word_filter_percentage))
        file_wrds_mod = short_file_series_words[:shortened_length]
        fldr_wrds_mod = short_fldr_name_words[:shortened_length]

        # Perform various matching checks
        folder_name_match = short_fldr_name.lower().strip() == short_file_series_name.lower().strip()
        similar_score_match = similar(short_fldr_name, short_file_series_name) >= required_similarity_score # Use imported string_utils
        consecutive_items_match = find_consecutive_items(tuple(short_fldr_name_words), tuple(short_file_series_words)) or \
                                  find_consecutive_items(tuple(long_folder_words), tuple(long_file_words)) # Use imported string_utils
        unique_words_match = any(i in long_file_words and i in counted_words and counted_words[i] <= 3 for i in long_folder_words)
        subtitle_match = (folder_subtitle_clean and file_subtitle_clean) and \
                         (folder_subtitle_clean == file_subtitle_clean or similar(folder_subtitle_clean, file_subtitle_clean) >= required_similarity_score) # Use imported string_utils

        return folder_name_match or similar_score_match or consecutive_items_match or unique_words_match or subtitle_match


    # Helper: Attempts matching via image similarity
    def attempt_alternative_match(file_root, inner_dir, file, required_image_similarity_score):
        # ... (implementation from original script lines 6239-6320) ...
        # Depends on: upgrade_to_volume_class, upgrade_to_file_class, is_same_index_number,
        #             find_and_extract_cover, prep_images_for_similarity
        try:
            img_volumes = upgrade_to_volume_class( # Use imported file_utils
                upgrade_to_file_class( # Use imported file_utils
                    [f.name for f in scandir.scandir(file_root) if f.is_file()],
                    file_root, clean=True
                )
            )
            if not img_volumes: return 0, None

            # Find volumes in the target directory with the same index number
            matching_volumes = [vol for vol in img_volumes if is_same_index_number(vol.index_number, file.index_number, allow_array_match=True)] # Use imported string_utils

            if not matching_volumes: return 0, None

            downloaded_cover_data = find_and_extract_cover(file, return_data_only=True, silent=True, blank_image_check=True) # Use imported image_utils
            if not downloaded_cover_data: return 0, None

            for matching_volume in matching_volumes:
                print(f"\t\t\tComparing covers: {matching_volume.name} vs {file.name}")
                existing_cover_data = find_and_extract_cover(matching_volume, return_data_only=True, silent=True, blank_image_check=True) # Use imported image_utils
                if not existing_cover_data: continue

                score = prep_images_for_similarity(existing_cover_data, downloaded_cover_data, both_cover_data=True, silent=True) # Use imported image_utils
                print(f"\t\t\t\tCover Image Similarity Score: {score} (Required: {required_image_similarity_score})")

                if score >= required_image_similarity_score:
                    return score, matching_volume # Return score and the specific volume matched

            return 0, None # No match found
        except Exception as e:
             send_message(f"Error during alternative match for {file.name} in {file_root}: {e}", error=True)
             return 0, None


    # Helper: Performs the upgrade check and file operations
    def check_upgrade(existing_root, dir_name, file, similarity_strings=None, cache=False, isbn=False, image=False, test_mode=False):
        # ... (implementation from original script lines 5333-5628) ...
        # Depends on: get_folder_type, upgrade_to_volume_class, upgrade_to_file_class,
        #             reorganize_and_rename, handle_fields, group_notification, send_message,
        #             remove_duplicate_releases, check_and_delete_empty_folder, Embed, DiscordEmbed
        global moved_files, messages_to_send, grouped_notifications # Manage global state

        existing_dir = os.path.join(existing_root, dir_name)
        if not os.path.isdir(existing_dir):
             send_message(f"Target directory {existing_dir} not found during upgrade check.", error=True)
             return False # Cannot proceed if target dir doesn't exist

        # Get existing files in the target directory
        try:
            existing_files_names = [entry.name for entry in scandir.scandir(existing_dir) if entry.is_file()]
            clean_existing_objs = upgrade_to_volume_class( # Use imported file_utils
                upgrade_to_file_class(existing_files_names, existing_dir, clean=True), # Use imported file_utils
                skip_release_year=True, skip_release_group=True, skip_extras=True,
                skip_publisher=True, skip_premium_content=True, skip_subtitle=True
            )
        except Exception as e:
             send_message(f"Error reading/processing existing files in {existing_dir}: {e}", error=True)
             return False

        # --- Type/Format Matching ---
        # Reuse logic from original check_upgrade if needed, or simplify
        # For now, assume type matching was handled before calling this

        # --- Rename downloaded file if needed ---
        download_dir_volumes = [file] # Start with the single file passed in
        if rename_files_in_download_folders_toggle and resturcture_when_renaming:
            # Ensure reorganize_and_rename is available and works correctly
            try:
                download_dir_volumes = reorganize_and_rename(download_dir_volumes, existing_dir) # Use imported string_utils
            except Exception as rename_err:
                 send_message(f"Error during pre-upgrade rename for {file.name}: {rename_err}", error=True)
                 # Decide how to handle rename failure - skip upgrade?

        # --- Send Discord Notification about Match ---
        fields = [{"name": "Existing Series Location", "value": f"```{existing_dir}```", "inline": False}]
        # Add similarity_strings details to fields... (logic from original)
        if similarity_strings:
             # ... (add fields based on isbn, image flags as in original) ...
             pass # Placeholder for field logic

        title = "Found Series Match" + (" (CACHE)" if cache else "") + (" (Identifier)" if isbn else "") + (" (Cover Match)" if image else "")
        try:
            embed = handle_fields(DiscordEmbed(title=title, color=grey_color), fields=fields) # Use imported discord_utils
            grouped_notifications = group_notification(grouped_notifications, Embed(embed, None)) # Use imported discord_utils
        except Exception as discord_err:
             send_message(f"Error creating/grouping Discord embed for series match: {discord_err}", error=True)

        # --- Remove Duplicates/Perform Upgrade ---
        if test_mode: return clean_existing_objs # Return existing for testing

        clean_existing_objs, download_dir_volumes = remove_duplicate_releases( # Use local function
            clean_existing_objs, download_dir_volumes, image_similarity_match=image
        )

        # --- Handle New Volumes ---
        if download_dir_volumes: # If the downloaded volume wasn't removed as a duplicate
            volume = download_dir_volumes[0]
            if isinstance(volume.volume_number, (float, int, list)): # Check for valid number
                release_type = volume.file_type.capitalize()
                send_message(f"\t\t\t{release_type} {array_to_string(volume.volume_number)}: {volume.name} does not exist in: {existing_dir}\n\t\t\tMoving: {volume.name} to {existing_dir}", discord=False) # Use imported string_utils

                # Extract cover for notification if needed
                cover_data = None
                if volume.file_type == "volume" or (volume.file_type == "chapter" and output_chapter_covers_to_discord and not new_volume_webhook):
                     cover_data = find_and_extract_cover(volume, return_data_only=True, silent=True) # Use imported image_utils

                # Build fields for new release notification
                new_release_fields = [
                    {"name": f"{release_type} Number(s)", "value": f"```{array_to_string(volume.volume_number)}```", "inline": False}, # Use imported string_utils
                    {"name": f"{release_type} Name(s)", "value": f"```{volume.name}```", "inline": False},
                ]
                if volume.volume_part and volume.file_type == "volume":
                     new_release_fields.insert(1, {"name": f"{release_type} Part", "value": f"```{volume.volume_part}```", "inline": False})

                # Determine highest index for cover moving logic
                try:
                    all_indices = [item.index_number for item in clean_existing_objs + download_dir_volumes if item.index_number != ""]
                    hashable_indices = [tuple(idx) if isinstance(idx, list) else idx for idx in all_indices]
                    # highest_index_num = get_highest_release(tuple(hashable_indices), is_chapter_directory=False) # Assuming volume check
                    # TODO: get_highest_release needs import/definition
                    highest_index_num = "" # Placeholder
                except Exception: highest_index_num = ""

                # Move the new file
                move_status = move_file(volume, existing_dir, highest_index_num=highest_index_num) # Use imported file_utils

                if move_status:
                    check_and_delete_empty_folder(volume.root) # Use imported file_utils
                    new_file_path = os.path.join(existing_dir, volume.name)
                    if new_file_path not in moved_files: moved_files.append(new_file_path) # Manage global state

                    # Send Discord notification for new release
                    try:
                        embed = handle_fields(DiscordEmbed(title=f"New {release_type}(s) Added", color=green_color), fields=new_release_fields) # Use imported discord_utils
                        if new_volume_webhook:
                             # Special handling for single webhook (maybe queue differently?)
                             # For now, send directly or queue via messages_to_send
                             if volume.file_type == "chapter":
                                 messages_to_send.append(NewReleaseNotification(volume.index_number, f"New {release_type}(s) Added", green_color, new_release_fields, new_volume_webhook, volume.series_name, volume)) # Use imported models
                             else:
                                 send_discord_message(None, [Embed(embed, cover_data)], passed_webhook=new_volume_webhook) # Use imported discord_utils
                        else:
                             grouped_notifications = group_notification(grouped_notifications, Embed(embed, cover_data)) # Use imported discord_utils
                    except Exception as discord_err:
                         send_message(f"Error creating/sending Discord embed for new release: {discord_err}", error=True)

                    return True # Indicate success (file moved)
                else:
                     send_message(f"Failed to move new file {volume.name} to {existing_dir}", error=True)
                     return False # Indicate failure
            else:
                 # Invalid volume number, likely already logged by remove_duplicate_releases helper
                 return False # Indicate failure or already handled
        else:
            # Downloaded volume was removed as a duplicate
            check_and_delete_empty_folder(file.root) # Check original download folder if empty
            return True # Indicate success (duplicate handled)

    # --- Main check_for_existing_series Logic ---
    if test_mode:
        # Simplified setup for testing
        download_folders_local = ["/test_download"]
        paths_local = ["/test_library"]
        paths_with_types_local = []
        cached_paths_local = []
    else:
        download_folders_local = test_download_folders
        paths_local = test_paths
        paths_with_types_local = test_paths_with_types
        cached_paths_local = test_cached_paths

    if not download_folders_local:
        print("\nNo download folders specified, skipping check_for_existing_series.")
        return

    print("\nChecking download folders for items to match to existing library...")
    unmatched_series = [] # Track series that fail matching to avoid re-checking

    for download_folder in download_folders_local:
        if not os.path.isdir(download_folder) and not test_mode:
            print(f"\n\t{download_folder} does not exist, skipping...")
            continue

        # Get folders/files to process (either all or specific ones from watchdog)
        folders_to_scan = []
        if isinstance(download_folder, Folder): # If watchdog passed Folder objects
             folders_to_scan = [{"root": download_folder.root, "dirs": download_folder.dirs, "files": [f.name for f in download_folder.files]}]
        elif os.path.isdir(download_folder): # If it's a path string
             # Get all folders recursively (original behavior)
             # folders_to_scan = get_all_folders_recursively_in_dir(download_folder) # Use imported file_utils
             # Simplified: Process only top-level for now to avoid excessive scanning if not needed by watchdog
             try:
                 entries = list(scandir.scandir(download_folder))
                 top_files = [e.name for e in entries if e.is_file()]
                 top_dirs = [e.name for e in entries if e.is_dir()]
                 folders_to_scan = [{"root": download_folder, "dirs": top_dirs, "files": top_files}]
                 # Add subdirectories if needed, or rely on watchdog passing specific folders
                 folders_to_scan.extend([{"root": os.path.join(download_folder, d), "dirs": [], "files": []} for d in top_dirs]) # Basic structure for subdirs
             except Exception as e:
                  send_message(f"Error scanning download folder {download_folder}: {e}", error=True)
                  continue
        elif test_mode: # Handle test mode structure
             folders_to_scan = [{"root": "/test_mode", "dirs": [], "files": test_mode}]


        folders_to_scan.reverse() # Process deepest first

        for folder_info in folders_to_scan:
            root = folder_info["root"]
            # If processing subdirs, need to fetch files/dirs for them
            if root != download_folder and not folder_info["files"] and not folder_info["dirs"]:
                 try:
                     entries = list(scandir.scandir(root))
                     folder_info["files"] = [e.name for e in entries if e.is_file()]
                     folder_info["dirs"] = [e.name for e in entries if e.is_dir()]
                 except Exception as e:
                      send_message(f"Error scanning subfolder {root}: {e}", error=True)
                      continue

            dirs = folder_info["dirs"]
            files = folder_info["files"]

            print(f"\nProcessing: {root}")
            volumes = []

            if not test_mode:
                files, dirs = process_files_and_folders(root, files, dirs, sort=True, just_these_files=transferred_files, just_these_dirs=transferred_dirs)
                if not files: continue
                volumes = upgrade_to_volume_class(upgrade_to_file_class([f for f in files if os.path.isfile(os.path.join(root, f))], root))
            else:
                volumes = test_mode # Use test data directly

            volumes = sort_volumes(volumes) # Use imported file_utils function
            exclude = None # Path excluded in this iteration if match found

            # Clear caches for each folder? Or keep global cache? Keep global for now.
            # similar.cache_clear()

            for file in volumes:
                try:
                    if not file.series_name or file.volume_number == "": continue
                    if not (test_mode or os.path.isfile(file.path)): continue # Ensure file exists

                    done = False # Flag indicating if file was matched and processed

                    # --- Skip Checks ---
                    series_key = f"{file.series_name} - {file.file_type} - {file.extension}"
                    if series_key in unmatched_series and not match_through_identifiers and not match_through_image_similarity:
                        continue # Skip if previously failed non-ID/image match

                    # --- Identifier Matching (Volumes only) ---
                    if match_through_identifiers and file.file_type == "volume" and not done:
                        # Check cache first
                        cached_id_match = next((ci for ci in cached_identifier_results if ci.series_name == file.series_name), None)
                        if cached_id_match:
                             done = check_upgrade(os.path.dirname(cached_id_match.path), os.path.basename(cached_id_match.path), file, similarity_strings=cached_id_match.matches, isbn=True)
                             if done and cached_id_match.path not in cached_paths: cache_path(cached_id_match.path) # Use imported file_utils
                             if done: continue # Move to next file

                        # Perform live identifier check if not cached
                        dl_zip_comment = get_zip_comment(file.path) if not test_mode else "" # Use imported archive_utils
                        dl_meta_ids = get_ids_from_comment(dl_zip_comment) if dl_zip_comment else [] # Use imported archive_utils

                        if dl_meta_ids:
                            # Search library paths for matches
                            # This requires iterating through library files, getting their comments/IDs
                            # This logic was complex in the original, needs careful porting or simplification
                            # Placeholder: Assume no live ID match found for now
                            pass # TODO: Implement live identifier matching across library paths

                    # --- Image Similarity Matching (if enabled and not done) ---
                    # Placeholder: Assume image matching logic is complex and needs separate implementation/call
                    if match_through_image_similarity and not done:
                         # TODO: Implement image similarity matching logic
                         pass

                    # --- Standard Similarity Matching (if not done) ---
                    if not done:
                        downloaded_file_series_name = clean_str(file.series_name, skip_bracket=True) # Use imported string_utils
                        # Search cached paths first
                        if cached_paths_local:
                             # Reorganize cache based on current file
                             current_cached_paths = cached_paths_local[:] # Work on a copy
                             current_cached_paths = move_strings_to_top(file.series_name, current_cached_paths) # Use imported string_utils
                             current_cached_paths = move_strings_to_top(downloaded_file_series_name, current_cached_paths) # Use imported string_utils

                             for p in current_cached_paths:
                                 if not os.path.isdir(p) or p in download_folders_local: continue
                                 # Path filtering based on type (logic from original)
                                 # ...
                                 successful_series_name = clean_str(os.path.basename(p), skip_bracket=True) # Use imported string_utils
                                 successful_similarity_score = similar(successful_series_name, downloaded_file_series_name) # Use imported string_utils

                                 if successful_similarity_score >= required_similarity_score:
                                     done = check_upgrade(os.path.dirname(p), os.path.basename(p), file, similarity_strings=[downloaded_file_series_name, successful_series_name, successful_similarity_score, required_similarity_score], cache=True, test_mode=test_mode)
                                     if done:
                                         if p not in cached_paths: cache_path(p) # Use imported file_utils
                                         # Cache reordering logic...
                                         exclude = p
                                         break # Found match in cache

                        # Search library paths if not found in cache
                        if not done and paths_local:
                             counted_words_global = {} # Cache word counts across library?
                             for path_root in paths_local:
                                 if done: break
                                 if not os.path.isdir(path_root) or path_root in download_folders_local: continue
                                 # Path filtering based on type...
                                 try:
                                     # Walk through library path
                                     for lib_root, lib_dirs, _ in scandir.walk(path_root):
                                         if done: break
                                         # Filtering logic for lib_dirs...
                                         counted_words = count_words(lib_dirs) # Use imported string_utils
                                         # Reorganize lib_dirs...
                                         lib_dirs = move_strings_to_top(file.series_name, lib_dirs) # Use imported string_utils

                                         for inner_dir in lib_dirs:
                                             existing_series_folder = clean_str(inner_dir) # Use imported string_utils
                                             similarity_score = similar(existing_series_folder, downloaded_file_series_name) # Use imported string_utils

                                             if similarity_score >= required_similarity_score:
                                                 # Found potential match by name similarity
                                                 done = check_upgrade(lib_root, inner_dir, file, similarity_strings=[downloaded_file_series_name, existing_series_folder, similarity_score, required_similarity_score], test_mode=test_mode)
                                                 if done:
                                                      match_path = os.path.join(lib_root, inner_dir)
                                                      if match_path not in cached_paths: cache_path(match_path) # Use imported file_utils
                                                      # Cache reordering...
                                                      exclude = match_path
                                                      break # Found match
                                             # Alternative match logic (image/complex name)
                                             elif alternative_match_allowed(inner_dir, file, short_word_filter_percentage, required_similarity_score, counted_words):
                                                  # Placeholder for calling alternative match (e.g., image)
                                                  # score, matching_volume = attempt_alternative_match(...)
                                                  # if score >= required_image_similarity_score:
                                                  #    done = check_upgrade(...)
                                                  #    if done: break
                                                  pass # Placeholder

                                 except Exception as walk_err:
                                      send_message(f"Error walking library path {path_root}: {walk_err}", error=True)


                    # --- Handle Unmatched ---
                    if not done:
                        unmatched_series.append(series_key)
                        print(f"\tNo match found for: {file.series_name}")

                except Exception as file_err:
                    send_message(f"Error processing file {file.name}: {file_err}\n{traceback.format_exc()}", error=True)
                    continue # Skip to next file

        # --- Final Chapter Notifications ---
        # Group and send chapter notifications accumulated in messages_to_send
        if messages_to_send:
            grouped_by_series = group_similar_series(messages_to_send) # Use local helper
            messages_to_send = [] # Clear original list
            series_notifications = [] # Temp list for current webhook batch

            webhook_to_use = new_volume_webhook # Use the specific chapter webhook if set

            for group in grouped_by_series:
                 # Logic to combine messages into single embed per series (from original)
                 # ... (build combined fields) ...
                 combined_fields = [] # Placeholder
                 first_item = group["messages"][0]
                 embed = handle_fields(DiscordEmbed(title=first_item.title, color=first_item.color), fields=combined_fields) # Use imported discord_utils

                 if webhook_to_use:
                     series_notifications = group_notification(series_notifications, Embed(embed, None), passed_webhook=webhook_to_use) # Use imported discord_utils
                 else:
                     grouped_notifications = group_notification(grouped_notifications, Embed(embed, None)) # Use imported discord_utils

            # Send remaining notifications for the chapter webhook
            if series_notifications and webhook_to_use:
                send_discord_message(None, series_notifications, passed_webhook=webhook_to_use) # Use imported discord_utils

    # Clear caches if needed at the end
    # parse_words.cache_clear()
    # find_consecutive_items.cache_clear()


# Checks for any duplicate releases and deletes the lower ranking one.
# Moved from original script (lines 5667-5939)
def check_for_duplicate_volumes(paths_to_search=[]):
    """Checks for and removes lower-ranking duplicate files within series folders."""
    global grouped_notifications # Manage global state

    if not paths_to_search:
        print("\nNo paths specified for duplicate check.")
        return

    print("\nChecking for duplicate volumes/chapters...")
    try:
        for p in paths_to_search:
            if not os.path.isdir(p):
                send_message(f"\nERROR: {p} is an invalid path.\n", error=True)
                continue

            print(f"\nScanning {p} for duplicate releases...")
            # Use scandir for potentially better performance
            for root, dirs, files in scandir.walk(p):
                print(f"\tProcessing: {root}")
                # Use process_files_and_folders for consistent filtering
                files, dirs = process_files_and_folders(
                    root, files, dirs,
                    just_these_files=transferred_files, # Use global state
                    just_these_dirs=transferred_dirs   # Use global state
                )

                if not files or len(files) < 2: continue # Need at least 2 files to have duplicates

                # Upgrade to Volume objects for comparison
                volumes = upgrade_to_volume_class( # Use imported file_utils
                    upgrade_to_file_class( # Use imported file_utils
                        [f for f in files if os.path.isfile(os.path.join(root, f))], root
                    )
                    # Add skip flags if needed
                )

                if not volumes or len(volumes) < 2: continue

                # Find potential duplicates (same index, root, extension, type, series)
                potential_duplicates = {}
                for vol in volumes:
                    if vol.index_number == "": continue
                    key = (vol.index_number, vol.root, vol.extension, vol.file_type, vol.series_name.lower())
                    if key not in potential_duplicates: potential_duplicates[key] = []
                    potential_duplicates[key].append(vol)

                # Process groups with more than one item
                for key, group in potential_duplicates.items():
                    if len(group) > 1:
                        print(f"\t\tFound {len(group)} potential duplicates for index {key[0]} in {key[1]}")
                        # Sort group by score (descending) to easily find the best
                        # Requires is_upgradeable and get_keyword_scores
                        try:
                             # Simple sort: keep the one with the longest name as a heuristic if scoring fails
                             group.sort(key=lambda x: len(x.name), reverse=True)
                             best_volume = group[0]
                             duplicates_to_remove = group[1:]
                             print(f"\t\t\tBest candidate: {best_volume.name}")
                        except Exception as sort_err:
                             send_message(f"Error sorting duplicates for {key}: {sort_err}", error=True)
                             continue # Skip this group if sorting fails

                        for duplicate_file in duplicates_to_remove:
                            print(f"\t\t\tDuplicate found: {duplicate_file.name}")
                            # TODO: Add Discord notification logic from original
                            # embed = handle_fields(...)
                            # grouped_notifications = group_notification(...)

                            user_input = get_input_from_user(f'\t\t\tDelete "{duplicate_file.name}"', ["y", "n"], ["y", "n"]) if manual_delete else "y" # Use imported misc_utils

                            if user_input == 'y':
                                remove_file(duplicate_file.path) # Use imported file_utils
                            else:
                                print("\t\t\t\tSkipping deletion...")

    except Exception as e:
        send_message(f"Error during duplicate volume check: {e}", error=True)
# Checks for an existing series by pulling the series name from each elidable file in the downloads_folder
# and comparing it to an existin folder within the user's library.
# Moved from original script (lines 6112-7084)
def check_for_existing_series(
    test_mode=[], # For testing specific files
    test_paths=paths,
    test_download_folders=download_folders,
    test_paths_with_types=paths_with_types,
    test_cached_paths=cached_paths,
):
    """Matches downloaded files to existing library series folders."""
    # TODO: Refactor global state access
    global cached_paths, cached_identifier_results, messages_to_send, grouped_notifications
    global cached_image_similarity_results # Manage image cache state
    global moved_files, processed_files # Need access to update state

    # Helper: Groups chapter notification messages by series
    def group_similar_series(messages):
        grouped = {}
        for msg in messages:
            # Ensure msg has series_name attribute
            series = getattr(msg, 'series_name', 'Unknown Series')
            if series not in grouped: grouped[series] = []
            grouped[series].append(msg)
        # Convert dict to list of dicts expected by original code
        return [{"series_name": series, "messages": msgs} for series, msgs in grouped.items()]


    # Helper: Determines if an alternative matching method should be attempted
    def alternative_match_allowed(inner_dir, file, short_word_filter_percentage, required_similarity_score, counted_words):
        # Depends on: get_subtitle_from_dash, clean_str, get_shortened_title, parse_words, similar, find_consecutive_items
        folder_subtitle = get_subtitle_from_dash(inner_dir, replace=True) # Use imported string_utils
        folder_subtitle_clean = clean_str(folder_subtitle) if folder_subtitle else "" # Use imported string_utils

        file_subtitle = get_subtitle_from_dash(file.series_name, replace=True) # Use imported string_utils
        file_subtitle_clean = clean_str(file_subtitle) if file_subtitle else "" # Use imported string_utils

        short_fldr_name = clean_str(get_shortened_title(inner_dir) or inner_dir) # Use imported string_utils
        short_file_series_name = clean_str(file.shortened_series_name or file.series_name) # Use imported string_utils

        if not short_fldr_name or not short_file_series_name: return False

        long_folder_words = parse_words(inner_dir) # Use imported string_utils
        long_file_words = parse_words(file.series_name) # Use imported string_utils
        short_fldr_name_words = parse_words(short_fldr_name) # Use imported string_utils
        short_file_series_words = parse_words(short_file_series_name) # Use imported string_utils

        if not short_fldr_name_words or not short_file_series_words: return False

        # Shorten word lists for comparison
        shortened_length = max(1, int(min(len(short_fldr_name_words), len(short_file_series_words)) * short_word_filter_percentage))
        file_wrds_mod = short_file_series_words[:shortened_length]
        fldr_wrds_mod = short_fldr_name_words[:shortened_length]

        # Perform various matching checks
        folder_name_match = short_fldr_name.lower().strip() == short_file_series_name.lower().strip()
        similar_score_match = similar(short_fldr_name, short_file_series_name) >= required_similarity_score # Use imported string_utils
        consecutive_items_match = find_consecutive_items(tuple(short_fldr_name_words), tuple(short_file_series_words)) or \
                                  find_consecutive_items(tuple(long_folder_words), tuple(long_file_words)) # Use imported string_utils
        unique_words_match = any(i in long_file_words and i in counted_words and counted_words[i] <= 3 for i in long_folder_words)
        subtitle_match = (folder_subtitle_clean and file_subtitle_clean) and \
                         (folder_subtitle_clean == file_subtitle_clean or similar(folder_subtitle_clean, file_subtitle_clean) >= required_similarity_score) # Use imported string_utils

        return folder_name_match or similar_score_match or consecutive_items_match or unique_words_match or subtitle_match


    # Helper: Attempts matching via image similarity
    def attempt_alternative_match(file_root, inner_dir, file, required_image_similarity_score):
        # Depends on: upgrade_to_volume_class, upgrade_to_file_class, is_same_index_number,
        #             find_and_extract_cover, prep_images_for_similarity
        try:
            img_volumes = upgrade_to_volume_class( # Use imported file_utils
                upgrade_to_file_class( # Use imported file_utils
                    [f.name for f in scandir.scandir(file_root) if f.is_file()],
                    file_root, clean=True
                )
            )
            if not img_volumes: return 0, None

            # Find volumes in the target directory with the same index number
            matching_volumes = [vol for vol in img_volumes if is_same_index_number(vol.index_number, file.index_number, allow_array_match=True)] # Use imported string_utils

            if not matching_volumes: return 0, None

            downloaded_cover_data = find_and_extract_cover(file, return_data_only=True, silent=True, blank_image_check=True) # Use imported image_utils
            if not downloaded_cover_data: return 0, None

            for matching_volume in matching_volumes:
                print(f"\t\t\tComparing covers: {matching_volume.name} vs {file.name}")
                existing_cover_data = find_and_extract_cover(matching_volume, return_data_only=True, silent=True, blank_image_check=True) # Use imported image_utils
                if not existing_cover_data: continue

                score = prep_images_for_similarity(existing_cover_data, downloaded_cover_data, both_cover_data=True, silent=True) # Use imported image_utils
                print(f"\t\t\t\tCover Image Similarity Score: {score} (Required: {required_image_similarity_score})")

                if score >= required_image_similarity_score:
                    return score, matching_volume # Return score and the specific volume matched

            return 0, None # No match found
        except Exception as e:
             send_message(f"Error during alternative match for {file.name} in {file_root}: {e}", error=True)
             return 0, None


    # Helper: Performs the upgrade check and file operations
    # This was defined *within* check_for_existing_series in the original code
    def check_upgrade(existing_root, dir_name, file, similarity_strings=None, cache=False, isbn=False, image=False, test_mode=False):
        # Depends on: get_folder_type, upgrade_to_volume_class, upgrade_to_file_class,
        #             reorganize_and_rename, handle_fields, group_notification, send_message,
        #             remove_duplicate_releases, check_and_delete_empty_folder, Embed, DiscordEmbed
        global moved_files, messages_to_send, grouped_notifications # Manage global state

        existing_dir = os.path.join(existing_root, dir_name)
        if not os.path.isdir(existing_dir):
             send_message(f"Target directory {existing_dir} not found during upgrade check.", error=True)
             return False # Cannot proceed if target dir doesn't exist

        # Get existing files in the target directory
        try:
            existing_files_names = [entry.name for entry in scandir.scandir(existing_dir) if entry.is_file()]
            clean_existing_objs = upgrade_to_volume_class( # Use imported file_utils
                upgrade_to_file_class(existing_files_names, existing_dir, clean=True), # Use imported file_utils
                skip_release_year=True, skip_release_group=True, skip_extras=True,
                skip_publisher=True, skip_premium_content=True, skip_subtitle=True
            )
        except Exception as e:
             send_message(f"Error reading/processing existing files in {existing_dir}: {e}", error=True)
             return False

        # --- Type/Format Matching ---
        # Reuse logic from original check_upgrade if needed, or simplify
        # For now, assume type matching was handled before calling this

        # --- Rename downloaded file if needed ---
        download_dir_volumes = [file] # Start with the single file passed in
        if rename_files_in_download_folders_toggle and resturcture_when_renaming:
            # Ensure reorganize_and_rename is available and works correctly
            try:
                # NOTE: reorganize_and_rename was moved to file_operations.py
                # It needs to be imported or called differently if it's no longer in string_utils
                from .file_operations import reorganize_and_rename as reorg_rename_op
                download_dir_volumes = reorg_rename_op(download_dir_volumes, existing_dir)
            except ImportError:
                 send_message("Could not import reorganize_and_rename from file_operations for pre-upgrade rename.", error=True)
            except Exception as rename_err:
                 send_message(f"Error during pre-upgrade rename for {file.name}: {rename_err}", error=True)
                 # Decide how to handle rename failure - skip upgrade?

        # --- Send Discord Notification about Match ---
        fields = [{"name": "Existing Series Location", "value": f"```{existing_dir}```", "inline": False}]
        # Add similarity_strings details to fields... (logic from original)
        if similarity_strings:
             if not isbn and not image:
                 if len(similarity_strings) >= 4:
                     fields.extend([
                         {"name": "Downloaded File Series Name", "value": f"```{similarity_strings[0]}```", "inline": True},
                         {"name": "Existing Library Folder Name", "value": f"```{similarity_strings[1]}```", "inline": False},
                         {"name": "Similarity Score", "value": f"```{similarity_strings[2]}```", "inline": True},
                         {"name": "Required Score", "value": f"```>= {similarity_strings[3]}```", "inline": True},
                     ])
             elif isbn and len(similarity_strings) >= 2:
                  fields.extend([
                      {"name": "Downloaded File Identifiers", "value": "```" + "\n".join(similarity_strings[0]) + "```", "inline": False},
                      {"name": "Existing Library File Identifiers", "value": "```" + "\n".join(similarity_strings[1]) + "```", "inline": False},
                  ])
             elif image and len(similarity_strings) == 4:
                  fields.extend([
                      {"name": "Existing Folder Name", "value": f"```{similarity_strings[0]}```", "inline": True},
                      {"name": "File Series Name", "value": f"```{similarity_strings[1]}```", "inline": True},
                      {"name": "Image Similarity Score", "value": f"```{similarity_strings[2]}```", "inline": False},
                      {"name": "Required Score", "value": f"```>={similarity_strings[3]}```", "inline": True},
                  ])

        title = "Found Series Match" + (" (CACHE)" if cache else "") + (" (Identifier)" if isbn else "") + (" (Cover Match)" if image else "")
        try:
            embed = handle_fields(DiscordEmbed(title=title, color=grey_color), fields=fields) # Use imported discord_utils
            grouped_notifications = group_notification(grouped_notifications, Embed(embed, None)) # Use imported discord_utils
        except Exception as discord_err:
             send_message(f"Error creating/grouping Discord embed for series match: {discord_err}", error=True)

        # --- Remove Duplicates/Perform Upgrade ---
        if test_mode: return clean_existing_objs # Return existing for testing

        clean_existing_objs, download_dir_volumes = remove_duplicate_releases( # Use function defined in this module
            clean_existing_objs, download_dir_volumes, image_similarity_match=image
        )

        # --- Handle New Volumes ---
        if download_dir_volumes: # If the downloaded volume wasn't removed as a duplicate
            volume = download_dir_volumes[0]
            if isinstance(volume.volume_number, (float, int, list)): # Check for valid number
                release_type = volume.file_type.capitalize()
                send_message(f"\t\t\t{release_type} {array_to_string(volume.volume_number)}: {volume.name} does not exist in: {existing_dir}\n\t\t\tMoving: {volume.name} to {existing_dir}", discord=False) # Use imported string_utils

                # Extract cover for notification if needed
                cover_data = None
                if volume.file_type == "volume" or (volume.file_type == "chapter" and output_chapter_covers_to_discord and not new_volume_webhook):
                     cover_data = find_and_extract_cover(volume, return_data_only=True, silent=True) # Use imported image_utils

                # Build fields for new release notification
                new_release_fields = [
                    {"name": f"{release_type} Number(s)", "value": f"```{array_to_string(volume.volume_number)}```", "inline": False}, # Use imported string_utils
                    {"name": f"{release_type} Name(s)", "value": f"```{volume.name}```", "inline": False},
                ]
                if volume.volume_part and volume.file_type == "volume":
                     new_release_fields.insert(1, {"name": f"{release_type} Part", "value": f"```{volume.volume_part}```", "inline": False})

                # Determine highest index for cover moving logic
                try:
                    all_indices = [item.index_number for item in clean_existing_objs + download_dir_volumes if item.index_number != ""]
                    hashable_indices = [tuple(idx) if isinstance(idx, list) else idx for idx in all_indices]
                    # highest_index_num = get_highest_release(tuple(hashable_indices), is_chapter_directory=False) # Assuming volume check
                    # TODO: get_highest_release needs import/definition
                    highest_index_num = "" # Placeholder
                except Exception: highest_index_num = ""

                # Move the new file
                move_status = move_file(volume, existing_dir, highest_index_num=highest_index_num) # Use imported file_utils

                if move_status:
                    check_and_delete_empty_folder(volume.root) # Use imported file_utils
                    new_file_path = os.path.join(existing_dir, volume.name)
                    if new_file_path not in moved_files: moved_files.append(new_file_path) # Manage global state

                    # Send Discord notification for new release
                    try:
                        embed = handle_fields(DiscordEmbed(title=f"New {release_type}(s) Added", color=green_color), fields=new_release_fields) # Use imported discord_utils
                        if new_volume_webhook:
                             # Special handling for single webhook (maybe queue differently?)
                             # For now, send directly or queue via messages_to_send
                             if volume.file_type == "chapter":
                                 messages_to_send.append(NewReleaseNotification(volume.index_number, f"New {release_type}(s) Added", green_color, new_release_fields, new_volume_webhook, volume.series_name, volume)) # Use imported models
                             else:
                                 send_discord_message(None, [Embed(embed, cover_data)], passed_webhook=new_volume_webhook) # Use imported discord_utils
                        else:
                             grouped_notifications = group_notification(grouped_notifications, Embed(embed, cover_data)) # Use imported discord_utils
                    except Exception as discord_err:
                         send_message(f"Error creating/sending Discord embed for new release: {discord_err}", error=True)

                    return True # Indicate success (file moved)
                else:
                     send_message(f"Failed to move new file {volume.name} to {existing_dir}", error=True)
                     return False # Indicate failure
            else:
                 # Invalid volume number, likely already logged by remove_duplicate_releases helper
                 return False # Indicate failure or already handled
        else:
            # Downloaded volume was removed as a duplicate
            check_and_delete_empty_folder(file.root) # Check original download folder if empty
            return True # Indicate success (duplicate handled)


    # --- Main check_for_existing_series Logic ---
    if test_mode:
        # Simplified setup for testing
        download_folders_local = ["/test_download"]
        paths_local = ["/test_library"]
        paths_with_types_local = []
        cached_paths_local = []
    else:
        download_folders_local = test_download_folders
        paths_local = test_paths
        paths_with_types_local = test_paths_with_types
        cached_paths_local = test_cached_paths

    if not download_folders_local:
        print("\nNo download folders specified, skipping check_for_existing_series.")
        return

    print("\nChecking download folders for items to match to existing library...")
    unmatched_series = [] # Track series that fail matching to avoid re-checking

    for download_folder in download_folders_local:
        if not os.path.isdir(download_folder) and not test_mode:
            print(f"\n\t{download_folder} does not exist, skipping...")
            continue

        # Get folders/files to process (either all or specific ones from watchdog)
        folders_to_scan = []
        if isinstance(download_folder, Folder): # If watchdog passed Folder objects
             folders_to_scan = [{"root": download_folder.root, "dirs": download_folder.dirs, "files": [f.name for f in download_folder.files]}]
        elif os.path.isdir(download_folder): # If it's a path string
             try:
                 entries = list(scandir.scandir(download_folder))
                 top_files = [e.name for e in entries if e.is_file()]
                 top_dirs = [e.name for e in entries if e.is_dir()]
                 folders_to_scan = [{"root": download_folder, "dirs": top_dirs, "files": top_files}]
                 folders_to_scan.extend([{"root": os.path.join(download_folder, d), "dirs": [], "files": []} for d in top_dirs]) # Basic structure for subdirs
             except Exception as e:
                  send_message(f"Error scanning download folder {download_folder}: {e}", error=True)
                  continue
        elif test_mode: # Handle test mode structure
             folders_to_scan = [{"root": "/test_mode", "dirs": [], "files": test_mode}]


        folders_to_scan.reverse() # Process deepest first

        for folder_info in folders_to_scan:
            root = folder_info["root"]
            # If processing subdirs, need to fetch files/dirs for them
            if root != download_folder and not folder_info["files"] and not folder_info["dirs"]:
                 try:
                     entries = list(scandir.scandir(root))
                     folder_info["files"] = [e.name for e in entries if e.is_file()]
                     folder_info["dirs"] = [e.name for e in entries if e.is_dir()]
                 except Exception as e:
                      send_message(f"Error scanning subfolder {root}: {e}", error=True)
                      continue

            dirs = folder_info["dirs"]
            files = folder_info["files"]

            print(f"\nProcessing: {root}")
            volumes = []

            if not test_mode:
                files, dirs = process_files_and_folders(root, files, dirs, sort=True, just_these_files=transferred_files, just_these_dirs=transferred_dirs)
                if not files: continue
                volumes = upgrade_to_volume_class(upgrade_to_file_class([f for f in files if os.path.isfile(os.path.join(root, f))], root))
            else:
                volumes = test_mode # Use test data directly

            volumes = sort_volumes(volumes) # Use imported file_utils function
            exclude = None # Path excluded in this iteration if match found

            # Clear caches for each folder? Or keep global cache? Keep global for now.
            # similar.cache_clear()

            for file in volumes:
                try:
                    if not file.series_name or file.volume_number == "": continue
                    if not (test_mode or os.path.isfile(file.path)): continue # Ensure file exists

                    done = False # Flag indicating if file was matched and processed

                    # --- Skip Checks ---
                    series_key = f"{file.series_name} - {file.file_type} - {file.extension}"
                    if series_key in unmatched_series and not match_through_identifiers and not match_through_image_similarity:
                        continue # Skip if previously failed non-ID/image match

                    # --- Identifier Matching (Volumes only) ---
                    if match_through_identifiers and file.file_type == "volume" and not done:
                        # Check cache first
                        cached_id_match = next((ci for ci in cached_identifier_results if ci.series_name == file.series_name), None)
                        if cached_id_match:
                             done = check_upgrade(os.path.dirname(cached_id_match.path), os.path.basename(cached_id_match.path), file, similarity_strings=cached_id_match.matches, isbn=True)
                             if done and cached_id_match.path not in cached_paths: cache_path(cached_id_match.path) # Use imported file_utils
                             if done: continue # Move to next file

                        # Perform live identifier check if not cached
                        dl_zip_comment = get_zip_comment(file.path) if not test_mode else "" # Use imported archive_utils
                        dl_meta_ids = get_ids_from_comment(dl_zip_comment) if dl_zip_comment else [] # Use imported archive_utils

                        if dl_meta_ids:
                            # Search library paths for matches
                            # This requires iterating through library files, getting their comments/IDs
                            # This logic was complex in the original, needs careful porting or simplification
                            # Placeholder: Assume no live ID match found for now
                            pass # TODO: Implement live identifier matching across library paths

                    # --- Image Similarity Matching (if enabled and not done) ---
                    if match_through_image_similarity and not done:
                         # Check cache first
                         cached_img_match = next((ci for ci in cached_image_similarity_results if f"{file.series_name} - {file.file_type} - {file.root} - {file.extension}" in ci), None)
                         if cached_img_match:
                              last_item = cached_img_match.split("@@")[-1].strip()
                              print("\n\t\tFound cached cover image similarity result.")
                              done = check_upgrade(os.path.dirname(last_item), os.path.basename(last_item), file, similarity_strings=[file.series_name, file.series_name, "CACHE", required_image_similarity_score], image=True, test_mode=test_mode)
                              if done: continue

                         # Perform live image similarity check
                         # Requires iterating through potential library folders
                         # Placeholder: Assume no live image match found for now
                         pass # TODO: Implement live image similarity matching

                    # --- Standard Similarity Matching (if not done) ---
                    if not done:
                        downloaded_file_series_name = clean_str(file.series_name, skip_bracket=True) # Use imported string_utils
                        # Search cached paths first
                        if cached_paths_local:
                             # Reorganize cache based on current file
                             current_cached_paths = cached_paths_local[:] # Work on a copy
                             current_cached_paths = move_strings_to_top(file.series_name, current_cached_paths) # Use imported string_utils
                             current_cached_paths = move_strings_to_top(downloaded_file_series_name, current_cached_paths) # Use imported string_utils

                             for p in current_cached_paths:
                                 if not os.path.isdir(p) or p in download_folders_local: continue
                                 # Path filtering based on type (logic from original)
                                 # ...
                                 successful_series_name = clean_str(os.path.basename(p), skip_bracket=True) # Use imported string_utils
                                 successful_similarity_score = similar(successful_series_name, downloaded_file_series_name) # Use imported string_utils

                                 if successful_similarity_score >= required_similarity_score:
                                     done = check_upgrade(os.path.dirname(p), os.path.basename(p), file, similarity_strings=[downloaded_file_series_name, successful_series_name, successful_similarity_score, required_similarity_score], cache=True, test_mode=test_mode)
                                     if done:
                                         if p not in cached_paths: cache_path(p) # Use imported file_utils
                                         # Cache reordering logic...
                                         exclude = p
                                         break # Found match in cache

                        # Search library paths if not found in cache
                        if not done and paths_local:
                             counted_words_global = {} # Cache word counts across library?
                             for path_root in paths_local:
                                 if done: break
                                 if not os.path.isdir(path_root) or path_root in download_folders_local: continue
                                 # Path filtering based on type...
                                 try:
                                     # Walk through library path
                                     for lib_root, lib_dirs, _ in scandir.walk(path_root):
                                         if done: break
                                         # Filtering logic for lib_dirs...
                                         counted_words = count_words(lib_dirs) # Use imported string_utils
                                         # Reorganize lib_dirs...
                                         lib_dirs = move_strings_to_top(file.series_name, lib_dirs) # Use imported string_utils

                                         for inner_dir in lib_dirs:
                                             existing_series_folder = clean_str(inner_dir) # Use imported string_utils
                                             similarity_score = similar(existing_series_folder, downloaded_file_series_name) # Use imported string_utils

                                             if similarity_score >= required_similarity_score:
                                                 # Found potential match by name similarity
                                                 done = check_upgrade(lib_root, inner_dir, file, similarity_strings=[downloaded_file_series_name, existing_series_folder, similarity_score, required_similarity_score], test_mode=test_mode)
                                                 if done:
                                                      match_path = os.path.join(lib_root, inner_dir)
                                                      if match_path not in cached_paths: cache_path(match_path) # Use imported file_utils
                                                      # Cache reordering...
                                                      exclude = match_path
                                                      break # Found match
                                             # Alternative match logic (image/complex name)
                                             elif alternative_match_allowed(inner_dir, file, short_word_filter_percentage, required_similarity_score, counted_words):
                                                  # Placeholder for calling alternative match (e.g., image)
                                                  # score, matching_volume = attempt_alternative_match(...)
                                                  # if score >= required_image_similarity_score:
                                                  #    done = check_upgrade(...)
                                                  #    if done: break
                                                  pass # Placeholder

                                 except Exception as walk_err:
                                      send_message(f"Error walking library path {path_root}: {walk_err}", error=True)


                    # --- Handle Unmatched ---
                    if not done:
                        unmatched_series.append(series_key)
                        print(f"\tNo match found for: {file.series_name}")

                except Exception as file_err:
                    send_message(f"Error processing file {file.name}: {file_err}\n{traceback.format_exc()}", error=True)
                    continue # Skip to next file

        # --- Final Chapter Notifications ---
        # Group and send chapter notifications accumulated in messages_to_send
        if messages_to_send:
            grouped_by_series = group_similar_series(messages_to_send) # Use local helper
            messages_to_send = [] # Clear original list
            series_notifications = [] # Temp list for current webhook batch

            webhook_to_use = new_volume_webhook # Use the specific chapter webhook if set

            for group in grouped_by_series:
                 # Logic to combine messages into single embed per series (from original)
                 # ... (build combined fields) ...
                 combined_fields = [] # Placeholder
                 first_item = group["messages"][0]
                 # Ensure necessary attributes exist on first_item before creating embed
                 embed_title = getattr(first_item, 'title', 'New Chapter(s)')
                 embed_color = getattr(first_item, 'color', grey_color) # Default color
                 embed = handle_fields(DiscordEmbed(title=embed_title, color=embed_color), fields=combined_fields) # Use imported discord_utils

                 if webhook_to_use:
                     series_notifications = group_notification(series_notifications, Embed(embed, None), passed_webhook=webhook_to_use) # Use imported discord_utils
                 else:
                     grouped_notifications = group_notification(grouped_notifications, Embed(embed, None)) # Use imported discord_utils

            # Send remaining notifications for the chapter webhook
            if series_notifications and webhook_to_use:
                send_discord_message(None, series_notifications, passed_webhook=webhook_to_use) # Use imported discord_utils

    # Clear caches if needed at the end
    # parse_words.cache_clear()
    # find_consecutive_items.cache_clear()