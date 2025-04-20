import argparse
import os
import sys
import re
import cProfile
import time # Needed for watchdog loop

# --- Import Project Modules ---
# Use try-except blocks for robustness during refactoring
try:
    # Import config variables directly (assuming they are module-level)
    from komga_cover_extractor import config
    # Import necessary functions/classes from other modules
    from komga_cover_extractor.log_utils import send_message
    from komga_cover_extractor.file_utils import get_all_files_in_directory, get_file_extension
    from komga_cover_extractor.string_utils import contains_volume_keywords, contains_chapter_keywords
    from komga_cover_extractor.models import Path, LibraryType # Path needed for process_path
    from komga_cover_extractor.komga_utils import get_komga_libraries
    from komga_cover_extractor import core_logic
    from komga_cover_extractor import watchdog_handler
    # Import settings to check defined variables
    import settings as settings_file
except ImportError as e:
    print(f"FATAL ERROR: Could not import necessary project modules in main.py: {e}")
    sys.exit(1)

# --- Argument Parsing Logic (Adapted from original script) ---

# Helper function for parse_my_args
def process_path(path_arg_list, paths_with_types_list, paths_list, is_download_folders=False):
    """Processes a path argument list and populates config lists."""
    COMMON_EXTENSION_THRESHOLD = 0.3  # 30%

    def process_auto_classification():
        """Attempts auto-classification based on file content."""
        nonlocal path_formats, path_extensions, path_library_types
        CHAPTER_THRESHOLD = 0.9
        VOLUME_THRESHOLD = 0.9

        try:
            files = get_all_files_in_directory(path_str) # Use imported function
            if not files: return

            print("\t\t\t- attempting auto-classification...")
            # Limit files checked for performance if necessary
            files_to_check = files[:int(len(files) * 0.75)] if len(files) >= 100 else files
            print(f"\t\t\t\t- checking {len(files_to_check)} files.")

            # Extensions
            all_extensions = [get_file_extension(file) for file in files_to_check] # Use imported function
            common_exts = get_common_extensions(all_extensions)
            path_extensions.extend(ext for ext in common_exts if ext not in path_extensions)
            # Add full sets if common ones overlap
            for ext_set in [config.manga_extensions, config.novel_extensions]:
                if any(ext in path_extensions for ext in ext_set):
                    path_extensions.extend(e for e in ext_set if e not in path_extensions)
            if not path_extensions: path_extensions.extend(config.file_extensions) # Default if none found
            print(f"\t\t\t\t\t- path extensions: {path_extensions}")

            # Formats (Volume/Chapter)
            all_types = [
                "chapter" if (not contains_volume_keywords(f) and contains_chapter_keywords(f)) else "volume" # Use imported functions
                for f in files_to_check
            ]
            chapter_count = all_types.count("chapter")
            volume_count = all_types.count("volume")
            total_files = len(all_types)
            if total_files > 0:
                 if chapter_count / total_files >= CHAPTER_THRESHOLD: path_formats.append("chapter")
                 elif volume_count / total_files >= VOLUME_THRESHOLD: path_formats.append("volume")
            if not path_formats: path_formats.extend(config.file_formats) # Default
            print(f"\t\t\t\t\t- path formats: {path_formats}")

        except Exception as auto_err:
             send_message(f"Error during auto-classification for {path_str}: {auto_err}", error=True) # Use imported function

    def get_common_extensions(all_extensions):
        """Finds common extensions above a threshold."""
        if not all_extensions: return []
        ext_counts = {ext: all_extensions.count(ext) for ext in set(all_extensions)}
        return [ext for ext, count in ext_counts.items() if count / len(all_extensions) >= COMMON_EXTENSION_THRESHOLD]

    def process_single_type_path(path_to_process):
        """Parses type hints like 'manga', '.cbz', 'volume'."""
        nonlocal path_formats, path_extensions, path_library_types, path_translation_source_types
        parts = [p.strip() for p in path_to_process.split(',')]
        first_part = parts[0]

        if first_part in config.file_formats: path_formats.extend(p for p in parts if p in config.file_formats and p not in path_formats)
        elif first_part.startswith('.') and first_part in config.file_extensions: path_extensions.extend(p for p in parts if p in config.file_extensions and p not in path_extensions)
        elif first_part in [lt.name for lt in config.library_types]: path_library_types.extend(p for p in parts if p in [lt.name for lt in config.library_types] and p not in path_library_types)
        elif first_part in config.translation_source_types: path_translation_source_types.extend(p for p in parts if p in config.translation_source_types and p not in path_translation_source_types)
        # Add source_languages if needed

    # --- Main process_path logic ---
    path_formats = []
    path_extensions = []
    path_library_types = []
    path_translation_source_types = []
    path_source_languages = []
    path_obj = None
    path_str = path_arg_list[0] # The actual directory path

    print(f"\t\tProcessing path: {path_str}")

    if not os.path.isdir(path_str):
        send_message(f"Warning: Path does not exist or is not a directory: {path_str}", error=True) # Use imported function
        return # Skip invalid paths

    if len(path_arg_list) == 1: # No type hints provided
        # Attempt auto-classification only for library paths if enabled
        if (not is_download_folders and config.watchdog_toggle and
            config.auto_classify_watchdog_paths and config.check_for_existing_series_toggle):
             process_auto_classification()
        # Create Path object with detected or default types
        path_obj = Path(
            path_str,
            path_formats=path_formats or config.file_formats,
            path_extensions=path_extensions or config.file_extensions,
            library_types=[lt for lt in config.library_types if lt.name in path_library_types] or config.library_types, # Convert names back to objects
            translation_source_types=path_translation_source_types or config.translation_source_types,
            source_languages=path_source_languages or config.source_languages
        )
    else: # Type hints provided
        for type_hint in path_arg_list[1:]:
            process_single_type_path(type_hint)
        # Create Path object with specified types, falling back to defaults
        path_obj = Path(
            path_str,
            path_formats=path_formats or config.file_formats,
            path_extensions=path_extensions or config.file_extensions,
            library_types=[lt for lt in config.library_types if lt.name in path_library_types] or config.library_types,
            translation_source_types=path_translation_source_types or config.translation_source_types,
            source_languages=path_source_languages or config.source_languages
        )

    # Append to the correct global list in config module
    if not is_download_folders:
        if path_str not in config.paths: config.paths.append(path_str)
        if path_obj and path_obj not in config.paths_with_types: config.paths_with_types.append(path_obj)
    else:
        if path_str not in config.download_folders: config.download_folders.append(path_str)
        if path_obj and path_obj not in config.download_folders_with_types: config.download_folders_with_types.append(path_obj)


# Parses the passed command-line arguments
def parse_my_args():
    """Parses command line arguments and updates config."""
    def parse_bool_argument(arg_value):
        return str(arg_value).lower().strip() == "true"

    parser = argparse.ArgumentParser(description=f"Komga Cover Extractor {config.script_version_text}")
    # Add arguments as in the original script (lines 1329-1414)
    parser.add_argument("-p", "--paths", help="Library paths", action="append", nargs="*", required=False)
    parser.add_argument("-df", "--download_folders", help="Download folders", action="append", nargs="*", required=False)
    parser.add_argument("-wh", "--webhook", help="Discord webhook URL(s)", action="append", nargs="*", required=False)
    parser.add_argument("-bwc", "--bookwalker_check", help="Enable Bookwalker check", required=False)
    parser.add_argument("-c", "--compress", help="Enable image compression", required=False)
    parser.add_argument("-cq", "--compress_quality", help="Image compression quality", required=False)
    parser.add_argument("-bwk_whs", "--bookwalker_webhook_urls", help="Bookwalker webhook URLs", action="append", nargs="*", required=False)
    parser.add_argument("-wd", "--watchdog", help="Enable watchdog mode", required=False)
    parser.add_argument("-nw", "--new_volume_webhook", help="Specific webhook for new volumes", required=False)
    parser.add_argument("-ltf", "--log_to_file", help="Enable logging to file", required=False)
    parser.add_argument("--watchdog_discover_new_files_check_interval", help="Watchdog discovery interval", required=False)
    parser.add_argument("--watchdog_file_transferred_check_interval", help="Watchdog transfer check interval", required=False)
    parser.add_argument("--output_covers_as_webp", help="Output covers as WebP", required=False)
    # Add any other arguments from the original function

    args = parser.parse_args()

    print(f"\nScript Version: {config.script_version_text}")
    print("\nRun Settings:")

    # Process download folders
    if args.download_folders:
        processed_df = []
        for df_list in args.download_folders:
            if df_list:
                # Handle potential splitting characters like \1
                if r"\1" in df_list[0]:
                    processed_df.extend([[p] for p in df_list[0].split(r"\1")])
                else:
                    processed_df.append(df_list)
        print("\tdownload_folders:")
        for df_item in processed_df:
             # Handle potential splitting characters like \0 for type hints
            path_parts = df_item[0].split(r"\0") if r"\0" in df_item[0] else df_item
            process_path(path_parts, config.download_folders_with_types, config.download_folders, is_download_folders=True)
        if config.download_folders_with_types:
             print("\n\tdownload_folders_with_types:")
             # Print details...

    # Process library paths
    if args.paths:
        processed_p = []
        for p_list in args.paths:
            if p_list:
                if r"\1" in p_list[0]:
                    processed_p.extend([[p] for p in p_list[0].split(r"\1")])
                else:
                    processed_p.append(p_list)
        print("\tpaths:")
        for p_item in processed_p:
            path_parts = p_item[0].split(r"\0") if r"\0" in p_item[0] else p_item
            process_path(path_parts, config.paths_with_types, config.paths)
        if config.paths_with_types:
             print("\n\tpaths_with_types:")
             # Print details...

    # Update config based on other arguments
    if args.watchdog:
        if config.download_folders:
            config.watchdog_toggle = parse_bool_argument(args.watchdog)
        else:
            send_message("Watchdog requires download folders to be specified.", error=True)
    print(f"\twatchdog: {config.watchdog_toggle}")

    if args.output_covers_as_webp:
        config.output_covers_as_webp = parse_bool_argument(args.output_covers_as_webp)
    print(f"\toutput_covers_as_webp: {config.output_covers_as_webp}")

    if args.webhook:
        urls = []
        for item in args.webhook:
            if item:
                for hook in item:
                    if hook:
                        if r"\1" in hook: urls.extend(h for h in hook.split(r"\1") if h)
                        else: urls.append(hook)
        config.discord_webhook_url = list(set(urls)) # Unique URLs
        print(f"\twebhooks: {config.discord_webhook_url}")

    if args.bookwalker_check:
        config.bookwalker_check = parse_bool_argument(args.bookwalker_check)
    print(f"\tbookwalker_check: {config.bookwalker_check}")

    if args.compress:
        config.compress_image_option = parse_bool_argument(args.compress)
    print(f"\tcompress: {config.compress_image_option}")

    if args.compress_quality and args.compress_quality.isdigit():
        config.image_quality = int(args.compress_quality)
    print(f"\tcompress_quality: {config.image_quality}")

    if args.bookwalker_webhook_urls:
        bw_urls = []
        for item in args.bookwalker_webhook_urls:
             if item:
                 for hook in item:
                     if hook:
                         if r"\1" in hook: bw_urls.extend(h for h in hook.split(r"\1") if h)
                         else: bw_urls.append(hook)
        config.bookwalker_webhook_urls = list(set(bw_urls))
        print(f"\tbookwalker_webhook_urls: {config.bookwalker_webhook_urls}")

    if args.new_volume_webhook:
        config.new_volume_webhook = args.new_volume_webhook
    print(f"\tnew_volume_webhook: {config.new_volume_webhook}")

    if args.log_to_file:
        config.log_to_file = parse_bool_argument(args.log_to_file)
    print(f"\tlog_to_file: {config.log_to_file}")

    if args.watchdog_discover_new_files_check_interval and args.watchdog_discover_new_files_check_interval.isdigit():
        config.watchdog_discover_new_files_check_interval = int(args.watchdog_discover_new_files_check_interval)
    if args.watchdog_file_transferred_check_interval and args.watchdog_file_transferred_check_interval.isdigit():
        config.watchdog_file_transferred_check_interval = int(args.watchdog_file_transferred_check_interval)

    if config.watchdog_toggle:
        print(f"\t\twatchdog_discover_new_files_check_interval: {config.watchdog_discover_new_files_check_interval}")
        print(f"\t\twatchdog_file_transferred_check_interval: {config.watchdog_file_transferred_check_interval}")

    # Check for required paths
    if not config.paths and not config.download_folders:
        print("\nERROR: No library paths or download folders specified. Use -p or -df arguments.")
        sys.exit(1)

    # Print external settings from settings.py
    print("\nExternal Settings (from settings.py):")
    sensitive_keywords = ["password", "email", "_ip", "token", "user"]
    ignored_settings = ["ranked_keywords", "unacceptable_keywords"] # Add others if needed
    try:
        settings_vars = [var for var in dir(settings_file) if not callable(getattr(settings_file, var)) and not var.startswith("__")]
        for setting_name in settings_vars:
            if setting_name in ignored_settings: continue
            value = getattr(settings_file, setting_name)
            display_value = "********" if value and any(kw in setting_name.lower() for kw in sensitive_keywords) else value
            print(f"\t{setting_name}: {display_value}")
    except Exception as settings_err:
         print(f"\tError reading settings.py: {settings_err}")

    # Load Komga libraries if needed for scanning
    if config.send_scan_request_to_komga_libraries_toggle and (config.check_for_existing_series_toggle or config.move_series_to_correct_library_toggle):
        print("\nFetching Komga libraries...")
        config.komga_libraries = get_komga_libraries() # Use imported function
        if config.komga_libraries:
            print(f"\tFound {len(config.komga_libraries)} Komga libraries.")
        else:
            print("\tCould not fetch Komga libraries. Scan requests will be skipped.")


# Checks that the user has the required settings in settings.py
def check_required_settings():
    """Checks if required settings are present based on script version."""
    # This function might be less relevant if settings are managed differently,
    # but keep it for now if version-specific checks are needed.
    required_settings = {
        # Add settings required by specific versions if necessary
        # "setting_name": (major, minor, patch),
    }
    missing_settings = []
    try:
        settings_vars = [var for var in dir(settings_file) if not callable(getattr(settings_file, var)) and not var.startswith("__")]
        for setting, version in required_settings.items():
            if config.script_version >= version and setting not in settings_vars:
                missing_settings.append(setting)
    except Exception as e:
         print(f"Error checking required settings: {e}") # Avoid exiting if settings.py itself is missing

    if missing_settings:
        msg = f"\nMissing required settings in settings.py for version {config.script_version_text}: \n\t{', '.join(missing_settings)}\nPlease update your settings.py file."
        send_message(msg, error=True) # Use imported function
        print(msg) # Ensure it prints to console too
        # sys.exit(1) # Decide if missing settings should be fatal


# --- Main Execution Block ---
if __name__ == "__main__":
    parse_my_args()  # Parse arguments and update config

    # check_required_settings() # Check if settings are compatible

    if config.watchdog_toggle and config.download_folders:
        print("\nWatchdog mode enabled. Starting observer...")
        try:
            watcher = watchdog_handler.Watcher() # Use imported class
            watcher.run() # This will block the main thread
        except KeyboardInterrupt:
             print("\nKeyboardInterrupt received. Stopping watchdog...")
             # Watcher.run() should handle cleanup in its finally block
        except Exception as e:
             print(f"\nFATAL ERROR in Watchdog mode: {e}")
             sys.exit(1)
    else:
        # Standard single run
        if config.profile_code == "main()":
            print("\nProfiling core_logic.main()...")
            cProfile.runctx("core_logic.main()", globals(), locals(), sort="cumtime")
        else:
            try:
                core_logic.main() # Call the main orchestration function
            except Exception as e:
                 print(f"\nFATAL ERROR during main execution: {e}")
                 print(traceback.format_exc())
                 sys.exit(1)

    print("\nScript finished.")