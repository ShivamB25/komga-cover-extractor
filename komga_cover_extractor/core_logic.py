# komga_cover_extractor/core_logic.py
import os
import re
import time
import cProfile
import traceback

# TODO: Refactor config imports - use a central config object?
try:
    from .config import (
        paths, download_folders, paths_with_types,
        check_for_existing_series_toggle,
        cache_each_root_for_each_path_in_paths_at_beginning_toggle,
        correct_file_extensions_toggle, convert_to_cbz_toggle,
        delete_unacceptable_files_toggle, delete_chapters_from_downloads_toggle,
        rename_files_in_download_folders_toggle,
        create_folders_for_items_in_download_folder_toggle,
        check_for_duplicate_volumes_toggle, extract_covers_toggle,
        rename_dirs_in_download_folder_toggle, move_series_to_correct_library_toggle,
        check_for_missing_volumes_toggle, bookwalker_check,
        send_scan_request_to_komga_libraries_toggle, profile_code,
        watchdog_toggle, LOGS_DIR
    )
except ImportError:
    print("WARN: Could not import from .config, using placeholder values in core_logic.")
    # Define essential placeholders if config fails
    paths = []
    download_folders = []
    paths_with_types = []
    check_for_existing_series_toggle = False
    cache_each_root_for_each_path_in_paths_at_beginning_toggle = False
    correct_file_extensions_toggle = False
    convert_to_cbz_toggle = False
    delete_unacceptable_files_toggle = False
    delete_chapters_from_downloads_toggle = False
    rename_files_in_download_folders_toggle = False
    create_folders_for_items_in_download_folder_toggle = False
    check_for_duplicate_volumes_toggle = False
    extract_covers_toggle = False
    rename_dirs_in_download_folder_toggle = False
    move_series_to_correct_library_toggle = False
    check_for_missing_volumes_toggle = False
    bookwalker_check = False
    send_scan_request_to_komga_libraries_toggle = False
    profile_code = ""
    watchdog_toggle = False
    LOGS_DIR = "logs"


# TODO: Import necessary functions from utility modules
try:
    from .log_utils import send_message
    from .file_utils import get_lines_from_file, cache_path # Import only what main needs directly
    from .komga_utils import get_komga_libraries, scan_komga_library
    # Import functions from the new modules
    from .cover_extraction import extract_covers, print_stats
    from .series_matching import check_for_existing_series, check_for_duplicate_volumes
    from .file_operations import (
        correct_file_extensions, delete_unacceptable_files,
        delete_chapters_from_downloads, rename_files, create_folders_for_items_in_download_folder,
        rename_dirs_in_download_folder, move_series_to_correct_library
    )
    from .archive_utils import convert_to_cbz # Moved convert_to_cbz here from file_operations import
    from .misc_checks import check_for_missing_volumes
    from .bookwalker_utils import check_for_new_volumes_on_bookwalker
    from .discord_utils import send_discord_message # If main sends final notifications
    # Models are likely used indirectly via imported functions
except ImportError as e:
    print(f"FATAL: Failed to import core dependencies in core_logic: {e}")
    def send_message(msg, error=False, discord=False): print(f"{'ERROR: ' if error else ''}{msg}")
    # Add more placeholders as needed or raise SystemExit

# --- Global State (TODO: Refactor this) ---
# These variables were global in the original script. They need careful management.
# Some might belong in a shared context/state object, others might be passed as parameters.
cached_paths = []
processed_files = []
moved_files = []
libraries_to_scan = []
grouped_notifications = [] # Assuming discord_utils manages this list now
# transferred_files = [] # Likely managed by watchdog_handler
# transferred_dirs = [] # Likely managed by watchdog_handler
komga_libraries = [] # Fetched from Komga API

# --- Helper Functions ---

# Normalize path separators and remove Windows drive letters if present.
def normalize_path(path):
    path = os.path.normpath(path)
    # More robust check for Windows drive letters
    if ":" in path and os.path.sep == '\\':
        path = path.split(":", 1)[1]
    # Convert backslashes to forward slashes for uniform comparison
    return path.replace("\\", "/")

# Check if root_path is a prefix of target_path, handling Windows and Linux paths.
def is_root_present(root_path, target_path):
    root_path_norm = normalize_path(root_path)
    target_path_norm = normalize_path(target_path)
    # Ensure comparison considers directory boundaries
    # Check if target starts with root + separator OR if they are identical
    return target_path_norm.startswith(root_path_norm + '/') or target_path_norm == root_path_norm

# Prints execution time
def print_execution_time(start_time, function_name):
    execution_time = time.time() - start_time
    minutes, seconds = divmod(execution_time, 60)
    exec_time_str = ""
    if int(minutes) > 0: exec_time_str += f"{int(minutes)} min "
    exec_time_str += f"{seconds:.2f} sec"
    print(f"\nExecution time for {function_name}: {exec_time_str}")


# --- Main Orchestration Function ---

def main(folders_to_process=None):
    """Main orchestration function."""
    global cached_paths, processed_files, moved_files, libraries_to_scan, komga_libraries
    # Reset state potentially modified by previous runs if necessary
    processed_files = []
    moved_files = []
    libraries_to_scan = []
    # grouped_notifications should be managed by discord_utils

    start_time = time.time()
    send_message("\nStarting Execution", discord=False)
    # TODO: Send initial Discord notification if needed via discord_utils

    # --- Pre-checks and Setup ---
    cached_paths_path = os.path.join(LOGS_DIR, "cached_paths.txt")
    if check_for_existing_series_toggle and not cached_paths and os.path.isfile(cached_paths_path):
        cached_paths = get_lines_from_file(cached_paths_path, ignore=paths + download_folders, check_paths=True)
        cached_paths = [x for x in cached_paths if os.path.isdir(x)] # Validate paths
        if cached_paths: print(f"\n\tLoaded {len(cached_paths)} cached paths")

    if (cache_each_root_for_each_path_in_paths_at_beginning_toggle or not cached_paths) and paths and check_for_existing_series_toggle:
        cache_existing_library_paths() # Call placeholder/imported function
        # if cached_paths: print(f"\n\tLoaded {len(cached_paths)} cached paths") # Redundant if cache_existing_library_paths prints

    # --- Core Processing Steps ---
    # Determine which folders to process (all downloads or specific ones from watchdog)
    # If watchdog passes specific folders, use those, otherwise use configured download_folders
    process_target_folders = folders_to_process if folders_to_process is not None else download_folders

    if correct_file_extensions_toggle and process_target_folders:
        correct_file_extensions(folders=process_target_folders) # Call placeholder/imported function

    if convert_to_cbz_toggle and process_target_folders:
        convert_to_cbz() # Assumes it works on configured download_folders

    if delete_unacceptable_files_toggle and process_target_folders:
        delete_unacceptable_files(folders=process_target_folders) # Call placeholder/imported function

    if delete_chapters_from_downloads_toggle and process_target_folders:
        delete_chapters_from_downloads(folders=process_target_folders) # Call placeholder/imported function

    if rename_files_in_download_folders_toggle and process_target_folders:
        rename_files(download_folders=process_target_folders) # Call placeholder/imported function

    if create_folders_for_items_in_download_folder_toggle and process_target_folders:
        create_folders_for_items_in_download_folder(folders=process_target_folders) # Call placeholder/imported function

    if check_for_duplicate_volumes_toggle and process_target_folders:
        check_for_duplicate_volumes(paths_to_search=process_target_folders) # Call placeholder/imported function

    # Determine if cover extraction should run on download folders
    download_folder_in_paths = any(folder in paths for folder in download_folders)
    if extract_covers_toggle and paths and download_folder_in_paths and process_target_folders:
        extract_covers(paths_to_process=process_target_folders) # Call placeholder/imported function

    if check_for_existing_series_toggle and process_target_folders and paths:
        # Pass specific folders if provided by watchdog
        check_for_existing_series(test_download_folders=process_target_folders) # Call placeholder/imported function

    if rename_dirs_in_download_folder_toggle and process_target_folders:
        rename_dirs_in_download_folder(paths_to_process=process_target_folders) # Call placeholder/imported function

    # --- Library Path Processing (Only if not watchdog or if files were moved) ---
    library_paths_to_process = paths # Default to all library paths
    if watchdog_toggle and moved_files:
        # Determine which library paths were affected by moved files
        affected_library_paths = set()
        for moved in moved_files:
             if not os.path.exists(moved): continue # Skip if file no longer exists
             for lib_path in paths:
                 if is_root_present(lib_path, moved): # Use helper function
                     affected_library_paths.add(lib_path)
                     break
        library_paths_to_process = list(affected_library_paths)
        if library_paths_to_process:
            print(f"\nProcessing affected library paths: {library_paths_to_process}")
        else:
             print("\nNo library paths affected by moved files.")


    if library_paths_to_process: # Only proceed if there are library paths to process
        if move_series_to_correct_library_toggle and paths_with_types:
             move_series_to_correct_library(paths_to_search=library_paths_to_process) # Call placeholder/imported function

        if extract_covers_toggle:
            extract_covers(paths_to_process=library_paths_to_process) # Call placeholder/imported function

    # --- Post-processing Steps (Run regardless of watchdog?) ---
    if check_for_missing_volumes_toggle and paths: # Check library paths
        check_for_missing_volumes() # Call placeholder/imported function

    if bookwalker_check and not watchdog_toggle: # Usually run manually
        check_for_new_volumes_on_bookwalker() # Call placeholder/imported function

    # --- Komga Scan ---
    if send_scan_request_to_komga_libraries_toggle and moved_files:
        print("\nTriggering Komga library scans...")
        if not komga_libraries: # Fetch if not already fetched
            komga_libraries = get_komga_libraries() # Use imported komga_utils function

        scanned_libs = set()
        if komga_libraries: # Ensure libraries were fetched successfully
            for path in moved_files:
                if not os.path.exists(path): continue # Skip if file was deleted/moved again
                for library in komga_libraries:
                    if library["id"] in scanned_libs: continue
                    # Use normalized path checking
                    if is_root_present(library["root"], path): # Use helper function
                        scan_komga_library(library["id"]) # Use imported komga_utils function
                        scanned_libs.add(library["id"])
                        # Once a library containing the path is found and scanned,
                        # no need to check other libraries for the same path.
                        break
        else:
             send_message("Could not fetch Komga libraries to trigger scan.", error=True)


    # --- Final Notifications & Stats ---
    # Final grouped notifications are handled by discord_utils now, no need to send here explicitly
    # if grouped_notifications:
    #     send_discord_message(None, grouped_notifications)
    #     grouped_notifications = [] # Clear notifications

    print_stats() # Call placeholder/imported function

    end_time = time.time()
    print_execution_time(start_time, "main()") # Use local helper function

# --- Entry Point Simulation ---
# The original script had an __main__ block. We simulate its core logic call here.
# The actual entry point will be in the new main.py script later.
# if __name__ == "__main__":
#     # Simulating argument parsing and setup if needed for testing core_logic directly
#     # parse_my_args() # This would need to be moved or simulated
#     # check_required_settings() # This would need to be moved or simulated
#
#     if profile_code == "main()": # Use imported config value
#         cProfile.runctx("main()", globals(), locals(), sort="cumtime")
#     else:
#         main()