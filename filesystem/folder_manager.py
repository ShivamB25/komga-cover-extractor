import os
import scandir

from models.file_models import Folder
from utils.helpers import (
    get_file_extension,
    remove_hidden_files,
    remove_unaccepted_file_types,
    send_message,
)
from config.constants import (
    download_folders,
    paths,
    file_extensions,
    image_extensions,
    compress_image_option,
    ignored_folder_names,
)
from filesystem.file_operations import remove_file


# Recursively gets all the folders in a directory
def get_all_folders_recursively_in_dir(dir_path):
    results = []

    for root, dirs, files in scandir.walk(dir_path):
        if root in download_folders + paths:
            continue

        folder_info = {"root": root, "dirs": dirs, "files": files}

        results.append(folder_info)

    return results


# Recursively gets all the files in a directory
def get_all_files_in_directory(dir_path):
    results = []
    for root, dirs, files in scandir.walk(dir_path):
        files = remove_hidden_files(files)
        files = remove_unaccepted_file_types(files, root, file_extensions)
        results.extend(files)
    return results


# Resursively gets all files in a directory for watchdog
def get_all_files_recursively_in_dir_watchdog(dir_path):
    results = []
    for root, dirs, files in scandir.walk(dir_path):
        files = remove_hidden_files(files)
        for file in files:
            file_path = os.path.join(root, file)
            if file_path not in results:
                extension = get_file_extension(file_path)
                if extension not in image_extensions:
                    results.append(file_path)
                elif not compress_image_option and (
                    download_folders and dir_path in paths
                ):
                    results.append(file_path)
    return results


# Generates a folder object for a given root
def create_folder_obj(root, dirs=None, files=None):
    return Folder(
        root,
        dirs if dirs is not None else [],
        os.path.basename(os.path.dirname(root)),
        os.path.basename(root),
        get_all_files_recursively_in_dir_watchdog(root) if files is None else files,
    )


# Removes any folder names in the ignored_folder_names
def remove_ignored_folders(dirs):
    return [x for x in dirs if x not in ignored_folder_names]


# Remove hidden folders from the list
def remove_hidden_folders(dirs):
    return [x for x in dirs if not x.startswith(".")]


# Deletes hidden files, used when checking if a folder is empty.
def delete_hidden_files(files, root):
    for file in files:
        path = os.path.join(root, file)
        if (str(file)).startswith(".") and os.path.isfile(path):
            remove_file(path, silent=True)


# Checks if the given folder is empty and deletes it if it meets the conditions.
def check_and_delete_empty_folder(folder):
    # Check if the folder exists
    if not os.path.exists(folder):
        return

    try:
        print(f"\t\tChecking for empty folder: {folder}")

        # List the contents of the folder
        folder_contents = os.listdir(folder)

        # Delete hidden files in the folder
        delete_hidden_files(folder_contents, folder)

        # Check if the folder contains subfolders
        contains_subfolders = any(
            os.path.isdir(os.path.join(folder, item)) for item in folder_contents
        )

        # If it contains subfolders, exit
        if contains_subfolders:
            return

        # Remove hidden files from the list
        folder_contents = remove_hidden_files(folder_contents)

        # Check if there is only one file starting with "cover."
        if len(folder_contents) == 1 and folder_contents.startswith("cover."):
            cover_file_path = os.path.join(folder, folder_contents)

            # Remove the "cover." file
            remove_file(cover_file_path, silent=True)

            # Update the folder contents
            folder_contents = os.listdir(folder)
            folder_contents = remove_hidden_files(folder_contents)

        # Check if the folder is now empty and not in certain predefined paths
        if len(folder_contents) == 0 and folder not in paths + download_folders:
            try:
                print(f"\t\tRemoving empty folder: {folder}")
                os.rmdir(folder)

                if not os.path.exists(folder):
                    print(f"\t\t\tFolder removed: {folder}")
                else:
                    print(f"\t\t\tFailed to remove folder: {folder}")
            except OSError as e:
                send_message(str(e), error=True)
    except Exception as e:
        send_message(str(e), error=True)


# Renames the folder
def rename_folder(src, dest):
    result = None
    if os.path.isdir(src):
        if not os.path.isdir(dest):
            try:
                os.rename(src, dest)
            except Exception as e:
                send_message(str(e), error=True)
            if os.path.isdir(dest):
                send_message(
                    f"\n\t\t{os.path.basename(src)} was renamed to {os.path.basename(dest)}\n",
                    discord=False,
                )
                result = dest
            else:
                send_message(
                    f"Failed to rename {src} to {dest}",
                    error=True,
                )
        else:
            send_message(
                f"Folder {dest} already exists. Skipping rename.", discord=False
            )
    else:
        send_message(f"Folder {src} does not exist. Skipping rename.", discord=False)
    return result