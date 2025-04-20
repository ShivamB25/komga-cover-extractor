# komga_cover_extractor/misc_checks.py
import os
import scandir

# TODO: Refactor config imports
try:
    from .config import paths, download_folders
except ImportError:
    print("WARN: Could not import from .config, using placeholder values in misc_checks.")
    paths = []
    download_folders = []

# TODO: Import necessary functions from utility modules
try:
    from .file_utils import process_files_and_folders, upgrade_to_file_class, upgrade_to_volume_class # Added upgrade_to_volume_class
except ImportError as e:
    print(f"FATAL: Failed to import dependencies in misc_checks: {e}")
    def process_files_and_folders(r, f, d, **kwargs): return f, d
    def upgrade_to_file_class(*args, **kwargs): return []
    def upgrade_to_volume_class(*args, **kwargs): return []


# Checks for any missing volumes between the lowest volume of a series and the highest volume.
def check_for_missing_volumes(paths_to_search=paths):
    """Checks series folders for missing sequential volume numbers."""
    print("\nChecking for missing volumes...")

    if not paths_to_search:
        print("\tNo paths found.")
        return

    for path in paths_to_search:
        if not os.path.isdir(path) or path in download_folders: # Use imported config value
            if not os.path.isdir(path): print(f"\nERROR: {path} is an invalid path.\n")
            continue

        print(f"\nScanning Path: {path}")
        # Use scandir for potentially better performance
        try:
            # Get only top-level directories (series folders)
            series_dirs = [entry.path for entry in scandir.scandir(path) if entry.is_dir() and not entry.name.startswith('.')]
        except Exception as e:
             print(f"Error scanning path {path}: {e}")
             continue

        for series_dir_path in series_dirs:
            root = series_dir_path
            print(f"\tChecking Series: {os.path.basename(root)}")

            try:
                # Get files directly within the series directory
                series_files = [entry.name for entry in scandir.scandir(root) if entry.is_file()]
                # Use process_files_and_folders for consistent filtering
                filtered_files, _ = process_files_and_folders(root, series_files, [], chapters=False) # Use imported file_utils function

                if not filtered_files:
                    print("\t\tNo valid volume files found.")
                    continue

                # Upgrade to Volume objects
                volumes = upgrade_to_volume_class( # Use imported file_utils function
                    upgrade_to_file_class(filtered_files, root), # Use imported file_utils function
                    skip_release_year=True, skip_file_part=True, skip_release_group=True,
                    skip_extras=True, skip_publisher=True, skip_premium_content=True,
                    skip_subtitle=True
                )

                # Filter out volumes without valid numeric index numbers
                valid_volumes = [
                    vol for vol in volumes
                    if vol.index_number != "" and not isinstance(vol.index_number, str) # Ensure index is numeric (int, float, or list of them)
                ]

                if len(valid_volumes) < 2:
                    # print("\t\tLess than 2 valid volumes found, cannot check sequence.")
                    continue

                # Extract all individual volume numbers, handling multi-volume entries
                volume_numbers = set()
                for vol in valid_volumes:
                    if isinstance(vol.index_number, list):
                        try:
                            # Fill range for multi-volume entries (e.g., [1, 3] -> 1, 2, 3)
                            min_v, max_v = int(min(vol.index_number)), int(max(vol.index_number))
                            volume_numbers.update(range(min_v, max_v + 1))
                        except (ValueError, TypeError):
                            print(f"\t\tWarning: Invalid range in multi-volume {vol.name}")
                    elif isinstance(vol.index_number, (int, float)):
                         # Add single volume number (convert float like 1.0 to int 1)
                         try:
                             volume_numbers.add(int(vol.index_number))
                         except (ValueError, TypeError):
                              print(f"\t\tWarning: Invalid index number {vol.index_number} in {vol.name}")

                if len(volume_numbers) < 2:
                    # print("\t\tLess than 2 unique volume numbers found.")
                    continue

                # Determine the range to check (from 1 up to the highest found volume)
                try:
                    lowest_volume_number = 1 # Assume series start at 1
                    highest_volume_number = max(volume_numbers)
                except ValueError:
                     print("\t\tCould not determine highest volume number.")
                     continue # Skip if max fails (e.g., empty set after filtering)


                # Find missing numbers in the sequence
                expected_range = set(range(lowest_volume_number, highest_volume_number + 1))
                missing_numbers = sorted(list(expected_range - volume_numbers))

                if not missing_numbers:
                    # print("\t\tNo missing volumes detected in sequence.")
                    continue

                print(f"\t\tMissing Volume Numbers: {', '.join(map(str, missing_numbers))}")

            except Exception as series_err:
                 print(f"Error processing series directory {root}: {series_err}")
                 continue # Skip to next series directory

    except Exception as e:
        print(f"Error during missing volumes check: {e}")