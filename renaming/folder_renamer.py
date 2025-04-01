import os
import re
import shutil

# Assuming these are moved or imported appropriately
from settings import (
    download_folders, # Used in rename_dirs_in_download_folder
    manual_rename, # Used in rename_dirs_in_download_folder
    volume_regex_keywords, # Used in get_series_name, rename_based_on_brackets
    required_similarity_score, # Used in rename_based_on_volumes
)
from core.file_utils import remove_hidden_files # Used in check_and_delete_empty_folder
from core.string_utils import remove_dual_space, remove_brackets, similar, clean_str # Used in rename_based_on_volumes, get_series_name, rename_based_on_brackets
# from core.models import Volume, File # Needed for upgrade_to_volume_class, upgrade_to_file_class
# from core.volume_processor import upgrade_to_volume_class, upgrade_to_file_class # Assuming these are moved
# from messaging.log_manager import send_message # If logging/printing is needed
# from processing.file_folder_processor import process_files_and_folders, check_and_delete_empty_folder # Assuming these are moved

# Placeholder for functions that might be needed from other modules
def upgrade_to_volume_class(files, root=None, **kwargs): # Simplified placeholder
    # Needs actual implementation based on core.models.Volume
    print(f"Placeholder: Upgrading {len(files)} files to Volume objects in {root}")
    return files # Return the input for now

def upgrade_to_file_class(files, root=None, **kwargs): # Simplified placeholder
     # Needs actual implementation based on core.models.File
     print(f"Placeholder: Upgrading {len(files)} files to File objects in {root}")
     # Basic File object creation
     file_objects = []
     for f in files:
         try:
             # Basic extraction, needs refinement based on actual File class needs
             name = os.path.basename(f)
             ext = os.path.splitext(name)[1]
             ext_less = os.path.splitext(name)[0]
             # Placeholder values for other attributes
             file_objects.append(
                 File(name, ext_less, ext_less, ext, root, os.path.join(root, name), os.path.join(root, ext_less), "", "", "")
             )
         except: # Catch potential errors during placeholder creation
             pass
     return file_objects


def process_files_and_folders(root, files, dirs, **kwargs): # Simplified placeholder
    print(f"Placeholder: Processing files/folders in {root}")
    return files, dirs # Return unmodified for now

def check_and_delete_empty_folder(folder): # Simplified placeholder
    print(f"Placeholder: Checking/Deleting empty folder {folder}")
    try:
        if os.path.exists(folder) and not os.listdir(folder):
            os.rmdir(folder)
            print(f"\tRemoved empty folder: {folder}")
    except Exception as e:
        print(f"\tError checking/deleting folder {folder}: {e}")


def get_input_from_user(prompt, acceptable_values=[], example=None, timeout=90, use_timeout=False): # Simplified placeholder
    return input(f"{prompt} ({example}): ")

def send_message(message, discord=False, error=False): # Simplified placeholder
    print(message)

def remove_file(path, silent=False): # Simplified placeholder
    print(f"Placeholder: Removing file {path}")
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception as e:
        print(f"Error removing file {path}: {e}")
    return False

def move_file(file_obj, new_location, **kwargs): # Simplified placeholder
     print(f"Placeholder: Moving file {file_obj.name} to {new_location}")
     try:
         shutil.move(file_obj.path, new_location)
         return True
     except Exception as e:
         print(f"Error moving file {file_obj.name}: {e}")
         return False

# --- Functions moved from the original script ---

# Renames the folder
def rename_folder(src, dest):
    result = None
    if os.path.isdir(src):
        if not os.path.isdir(dest):
            try:
                os.rename(src, dest)
            except Exception as e:
                send_message(str(e), error=True) # Use send_message placeholder
            if os.path.isdir(dest):
                send_message(
                    f"\n\t\t{os.path.basename(src)} was renamed to {os.path.basename(dest)}\n",
                    discord=False,
                )
                result = dest
            else:
                # Use send_message placeholder, include error 'e' if available
                send_message(
                    f"Failed to rename {src} to {dest}", # Error 'e' might not be defined here
                    error=True,
                )
        else:
            send_message(
                f"Folder {dest} already exists. Skipping rename.", discord=False
            )
    else:
        send_message(f"Folder {src} does not exist. Skipping rename.", discord=False)
    return result

# Removes the duplicate after determining it's upgrade status, otherwise, it upgrades
# NOTE: This function was complex and had many dependencies. It's simplified here.
# It might belong in a different module (e.g., processing) depending on final structure.
def remove_duplicate_releases(
    original_releases, downloaded_releases, image_similarity_match=False
):
     # ... (Simplified placeholder or full implementation if needed here) ...
     print("Placeholder: Removing duplicate releases...")
     # Basic logic: remove the downloaded one if it exists
     new_downloaded = downloaded_releases[:]
     for dl_rel in downloaded_releases:
         if os.path.exists(dl_rel.path):
              remove_file(dl_rel.path, silent=True)
              new_downloaded.remove(dl_rel)
     return original_releases, new_downloaded # Return potentially modified lists


# Removes any unnecessary junk through regex in the folder name and returns the result
# !OLD METHOD!: Only used for cleaning a folder name as a backup if no volumes were found inside the folder
# when renaming folders in the dowload directory.
def get_series_name(dir):
    dir = remove_dual_space(dir.replace("_extra", ".5")).strip()
    dir = (
        re.sub(
            r"(\b|\s)((\s|)-(\s|)|)(Part|)(%s)([-_. ]|)([-_. ]|)([0-9]+)(\b|\s).*"
            % volume_regex_keywords, # Use imported/defined constant
            "",
            dir,
            flags=re.IGNORECASE,
        )
    ).strip()
    dir = (re.sub(r"(\([^()]*\))|(\[[^\[\]]*\])|(\{[^\{\}]*\})", "", dir)).strip()
    dir = (re.sub(r"(\(|\)|\[|\]|{|})", "", dir, flags=re.IGNORECASE)).strip()
    return dir


# Renames the folders in our download directory.
# If volume releases are available, it will rename based on those.
# Otherwise it will fallback to just cleaning the name of any brackets.
def rename_dirs_in_download_folder(paths_to_process=download_folders):
    # global grouped_notifications # Removed global dependency

    # Processes the passed folder
    def process_folder(download_folder):
        # Renames the root folder based on the volumes
        def rename_based_on_volumes(root):
            # global transferred_dirs, transferred_files # Removed global dependency
            nonlocal matching, volume_one, volume_one_series_name, volumes # Keep nonlocal

            dirname = os.path.dirname(root)
            basename = os.path.basename(root)
            result = False

            if not volumes:
                print("\t\t\t\tno volumes detected for folder rename.")
                return result # Return result consistently

            # Sort volumes by name
            volumes.sort(key=lambda x: x.name)
            first_volume = volumes[0]

            if not first_volume.series_name:
                print(
                    f"\t\t\t\t{first_volume.name} does not have a series name, skipping..."
                )
                return result # Return result consistently

            # Find volumes with matching series_name
            matching = [
                v
                for v in volumes[1:]
                if v.series_name.lower() == first_volume.series_name.lower()
                or similar(
                    clean_str(v.series_name),
                    clean_str(first_volume.series_name),
                )
                >= required_similarity_score
            ]

            if (not matching and len(volumes) == 1) or (
                len(matching) + 1 >= len(volumes) * 0.8 and len(volumes) > 1
            ):
                volume_one = volumes[0]
            else:
                print(
                    f"\t\t\t\t{len(matching)} out of {len(volumes)} volumes match the first volume's series name."
                )
                return result # Return result consistently

            # Set the series_name for use by the backup renamer, if needed
            if volume_one.series_name:
                volume_one_series_name = volume_one.series_name

                # Series name is the same as the current folder name, skip
                if volume_one.series_name == basename:
                    print(
                        f"\t\t\t\t{volume_one.series_name} is the same as the current folder name, skipping..."
                    )
                    return result # Return result consistently

            if not (
                similar(
                    remove_brackets(volume_one.series_name),
                    remove_brackets(basename),
                )
                >= 0.25
                or similar(volume_one.series_name, basename) >= 0.25
            ):
                print(
                    f"\t\t\t\t{volume_one.series_name} is not similar enough to {basename}, skipping..."
                )
                return result # Return result consistently

            send_message(
                f"\n\tBEFORE: {basename}\n\tAFTER:  {volume_one.series_name}",
                discord=False,
            )

            print("\t\tFILES:")
            for v in volumes:
                print(f"\t\t\t{v.name}")

            user_input = (
                get_input_from_user("\tRename", ["y", "n"], ["y", "n"])
                if manual_rename
                else "y"
            )

            if user_input != "y":
                send_message("\t\tSkipping...\n", discord=False)
                return result # Return result consistently

            new_folder = os.path.join(dirname, volume_one.series_name)

            # New folder doesn't exist, rename to it
            if not os.path.exists(new_folder):
                new_folder_path = rename_folder(root, new_folder)
                # TODO: Handle transferred_files/dirs update if needed externally
                result = bool(new_folder_path) # Set result based on rename success
            else:
                # New folder exists, move files to it
                for v in volumes:
                    target_file_path = os.path.join(new_folder, v.name)

                    # File doesn't exist in the new folder, move it
                    if not os.path.isfile(target_file_path):
                        move_file(v, new_folder)
                        # TODO: Handle transferred_files update if needed externally
                    else:
                        # File exists in the new folder, delete the one that would've been moved
                        print(
                            f"\t\t\t\t{v.name} already exists in {volume_one.series_name}"
                        )
                        remove_file(v.path, silent=True)
                        # TODO: Handle transferred_files update if needed externally
                result = True # Assume success if moving/deleting happens

            check_and_delete_empty_folder(root) # Use placeholder
            return result

        # Backup: Rename by just removing excess brackets from the folder name
        def rename_based_on_brackets(root):
            nonlocal matching, volume_one, volume_one_series_name, volumes # Keep nonlocal
            # global transferred_dirs, transferred_files # Removed global dependency

            # Cleans up the folder name
            def clean_folder_name(folder_name):
                folder_name = get_series_name(folder_name)  # start with the folder name
                folder_name = re.sub(r"([A-Za-z])(_)", r"\1 ", folder_name)  # A_ -> A
                folder_name = re.sub(
                    r"([A-Za-z])(\:)", r"\1 -", folder_name  # A: -> A -
                )
                folder_name = folder_name.replace("?", "")  # remove question marks
                folder_name = remove_dual_space(
                    folder_name
                ).strip()  # remove dual spaces
                return folder_name

            # Searches for a matching regex in the folder name
            def search_for_regex_in_folder_name(folder_name):
                searches = [
                    r"((\s\[|\]\s)|(\s\(|\)\s)|(\s\{|\}\s))",
                    r"(\s-\s|\s-)$",
                    r"(\bLN\b)",
                    r"(\b|\s)((\s|)-(\s|)|)(Part|)(%s|)(\.|)([-_. ]|)(([0-9]+)((([-_.]|)([0-9]+))+|))(\b|\s)"
                    % volume_regex_keywords, # Use imported/defined constant
                    r"\bPremium\b",
                    r":",
                    r"([A-Za-z])(_)",
                    r"([?])",
                ]
                return any(
                    re.search(search, folder_name, re.IGNORECASE) for search in searches
                )

            result = False
            dirname = os.path.dirname(root)
            basename = os.path.basename(root)

            if not search_for_regex_in_folder_name(basename):
                print(
                    f"\t\t\t\t{basename} does not match any of the regex searches, skipping..."
                )
                return result # Return result consistently

            # Cleanup the folder name
            dir_clean = clean_folder_name(basename)

            if not dir_clean:
                print(f"\t\t\t\t{basename} was cleaned to nothing, skipping...")
                return result # Return result consistently

            if dir_clean == basename:
                print(
                    f"\t\t\t\t{basename} is the same as the current folder name, skipping..."
                )
                return result # Return result consistently

            new_folder_path = os.path.join(dirname, dir_clean)

            # New folder doesn't exist, rename to it
            if not os.path.isdir(new_folder_path):
                send_message(
                    f"\n\tBEFORE: {basename}",
                    discord=False,
                )
                send_message(f"\tAFTER:  {dir_clean}", discord=False)

                user_input = (
                    get_input_from_user("\tRename", ["y", "n"], ["y", "n"])
                    if manual_rename
                    else "y"
                )

                if user_input != "y":
                    send_message(
                        "\t\tSkipping...\n",
                        discord=False,
                    )
                    return result # Return result consistently

                new_folder_path_two = rename_folder(
                    os.path.join(
                        dirname,
                        basename,
                    ),
                    os.path.join(
                        dirname,
                        dir_clean,
                    ),
                )
                # TODO: Handle transferred_files/dirs update if needed externally
                result = bool(new_folder_path_two) # Set result based on rename success
            else:
                # New folder exists, move files to it
                # Use scandir for efficiency if available, otherwise os.walk
                try:
                    import scandir
                    walker = scandir.walk(root)
                except ImportError:
                    walker = os.walk(root)

                for current_root, current_dirs, current_files in walker:
                    # Use placeholder for file object creation
                    folder_accessor_two = {"root": current_root, "files": upgrade_to_file_class(current_files, current_root)}

                    for file in folder_accessor_two["files"]:
                        new_location_folder = os.path.join(
                            dirname,
                            dir_clean,
                        )
                        new_file_path = os.path.join(
                            new_location_folder,
                            file.name,
                        )
                        # New file doesn't exist in the new folder, move it
                        if not os.path.isfile(new_file_path):
                            move_file(
                                file,
                                new_location_folder,
                            )
                            # TODO: Handle transferred_files update if needed externally
                        else:
                            # File exists in the new folder, delete the one that would've been moved
                            send_message(
                                f"File: {file.name} already exists in: {new_location_folder}\nRemoving duplicate from downloads.",
                                error=True,
                            )
                            remove_file(file.path, silent=True)
                            # TODO: Handle transferred_files update if needed externally

                    check_and_delete_empty_folder(current_root) # Use placeholder
                result = True # Assume success if moving/deleting happens
            return result

        # Get all the paths
        # Use scandir for efficiency if available, otherwise os.walk
        try:
            import scandir
            walker = scandir.walk(download_folder)
        except ImportError:
            walker = os.walk(download_folder)

        folder_list = []
        for root, dirs, files in walker:
             folder_list.append({"root": root, "dirs": dirs, "files": files})


        # Reverse the list so we start with the deepest folders
        folder_list.reverse()

        for folder in folder_list:
            root = folder["root"]
            dirs = folder["dirs"]
            files = folder["files"]

            if not os.path.isdir(root):
                continue

            if root in download_folders: # Check against the original list
                continue

            # TODO: Handle transferred_dirs check if needed externally

            try:
                files, dirs = process_files_and_folders( # Use placeholder
                    root,
                    files,
                    dirs,
                    # just_these_files=transferred_files, # Removed global dependency
                    # just_these_dirs=transferred_dirs, # Removed global dependency
                )

                volumes = upgrade_to_volume_class( # Use placeholder
                    upgrade_to_file_class(files, root), root # Use placeholder
                )

                matching = []
                dirname = os.path.dirname(root)
                basename = os.path.basename(root)
                done = False
                volume_one = None
                volume_one_series_name = None

                # Main: Rename based on common series_name from volumes
                if volumes:
                    done = rename_based_on_volumes(root)
                if (
                    not done
                    and (
                        not volume_one_series_name or volume_one_series_name != basename
                    )
                    and dirname in download_folders # Check against the original list
                    # and not re.search(basename, root, re.IGNORECASE) # This check seems problematic, removed for now
                ):
                    done = rename_based_on_brackets(root)
            except Exception as e:
                send_message(
                    f"Error renaming folder: {root} - {e}", # Include root in error
                    error=True,
                )
            check_and_delete_empty_folder(root) # Use placeholder

    print("\nLooking for folders to rename...")
    print("\tDownload Paths:")
    for path in paths_to_process:
        print(f"\t\t{path}")
        if not os.path.exists(path):
            if not path:
                send_message(
                    "No download folders specified, skipping renaming folders...",
                    error=True,
                )
            else:
                send_message(
                    f"Download folder {path} does not exist, skipping renaming folders...",
                    error=True,
                )
            continue
        process_folder(path)