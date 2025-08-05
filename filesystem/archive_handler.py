import os
import rarfile
import py7zr
import zipfile
import tempfile
import re

from utils.helpers import send_message, get_extensionless_name, get_file_extension
from filesystem.file_operations import (
    remove_folder,
    remove_file,
    get_file_size,
    rename_file,
    get_file_hash,
    process_files_and_folders,
)
from config.constants import (
    rar_extensions,
    seven_zip_extensions,
    convertable_file_extensions,
    rename_zip_to_cbz,
    manual_rename,
    download_folders,
    transferred_files,
    transferred_dirs,
    grey_color,
    watchdog_toggle,
    manga_extensions,
)
from utils.helpers import get_input_from_user, group_notification
from models.file_models import Embed
from integrations.discord_client import DiscordEmbed, handle_fields
from processing.metadata_extractor import get_header_extension
from filesystem.folder_manager import scandir


# Extracts a supported archive to a temporary directory.
def extract(file_path, temp_dir, extension):
    successfull = False
    try:
        if extension in rar_extensions:
            with rarfile.RarFile(file_path) as rar:
                rar.extractall(temp_dir)
                successfull = True
        elif extension in seven_zip_extensions:
            with py7zr.SevenZipFile(file_path, "r") as archive:
                archive.extractall(temp_dir)
                successfull = True
    except Exception as e:
        send_message(f"Error extracting {file_path}: {e}", error=True)
    return successfull


# Compresses a directory to a CBZ archive.
def compress(temp_dir, cbz_filename):
    successfull = False
    try:
        with zipfile.ZipFile(cbz_filename, "w") as zip:
            for root, dirs, files in scandir.walk(temp_dir):
                for file in files:
                    zip.write(
                        os.path.join(root, file),
                        os.path.join(root[len(temp_dir) + 1 :], file),
                    )
            successfull = True
    except Exception as e:
        send_message(f"Error compressing {temp_dir}: {e}", error=True)
    return successfull


# Converts supported archives to CBZ.
def convert_to_cbz():
    global transferred_files, grouped_notifications

    print("\nLooking for archives to convert to CBZ...")

    if not download_folders:
        print("\tNo download folders specified.")
        return

    for folder in download_folders:
        if not os.path.isdir(folder):
            print(f"\t{folder} is not a valid directory.")
            continue

        print(f"\t{folder}")
        for root, dirs, files in scandir.walk(folder):
            files, dirs = process_files_and_folders(
                root,
                files,
                dirs,
                just_these_files=transferred_files,
                just_these_dirs=transferred_dirs,
                skip_remove_unaccepted_file_types=True,
                keep_images_in_just_these_files=True,
            )

            for entry in files:
                try:
                    extension = get_file_extension(entry)
                    file_path = os.path.join(root, entry)

                    if not os.path.isfile(file_path):
                        continue

                    print(f"\t\t{entry}")

                    if extension in convertable_file_extensions:
                        source_file = file_path
                        repacked_file = f"{get_extensionless_name(source_file)}.cbz"

                        # check that the cbz file doesn't already exist
                        if os.path.isfile(repacked_file):
                            # if the file is zero bytes, delete it and continue, otherwise skip
                            if get_file_size(repacked_file) == 0:
                                send_message(
                                    "\t\t\tCBZ file is zero bytes, deleting...",
                                    discord=False,
                                )
                                remove_file(repacked_file)
                            elif not zipfile.is_zipfile(repacked_file):
                                send_message(
                                    "\t\t\tCBZ file is not a valid zip file, deleting...",
                                    discord=False,
                                )
                                remove_file(repacked_file)
                            else:
                                send_message(
                                    "\t\t\tCBZ file already exists, skipping...",
                                    discord=False,
                                )
                                continue

                        temp_dir = tempfile.mkdtemp("_source2cbz")

                        # if there's already contents in the temp directory, delete it
                        if os.listdir(temp_dir):
                            send_message(
                                f"\t\t\tTemp directory {temp_dir} is not empty, deleting...",
                                discord=False,
                            )
                            remove_folder(temp_dir)
                            # recreate the temp directory
                            temp_dir = tempfile.mkdtemp("source2cbz")

                        if not os.path.isdir(temp_dir):
                            send_message(
                                f"\t\t\tFailed to create temp directory {temp_dir}",
                                error=True,
                            )
                            continue

                        send_message(
                            f"\t\t\tCreated temp directory {temp_dir}",
                            discord=False,
                        )

                        # Extract the archive to the temp directory
                        extract_status = extract(source_file, temp_dir, extension)

                        if not extract_status:
                            send_message(
                                f"\t\t\tFailed to extract {source_file}",
                                error=True,
                            )
                            # remove temp directory
                            remove_folder(temp_dir)
                            continue

                        print(f"\t\t\tExtracted contents to {temp_dir}")

                        # Get hashes of all files in archive
                        hashes = []
                        for root2, dirs2, files2 in scandir.walk(temp_dir):
                            for file2 in files2:
                                path = os.path.join(root2, file2)
                                hashes.append(get_file_hash(path))

                        compress_status = compress(temp_dir, repacked_file)

                        if not compress_status:
                            # remove temp directory
                            remove_folder(temp_dir)
                            continue

                        print(f"\t\t\tCompressed to {repacked_file}")

                        # Check that the number of files in both archives is the same
                        # Print any files that aren't shared between the two archives
                        source_file_list = []
                        repacked_file_list = []

                        if os.path.isfile(source_file):
                            if extension in rar_extensions:
                                with rarfile.RarFile(source_file) as rar:
                                    for file in rar.namelist():
                                        if get_file_extension(file):
                                            source_file_list.append(file)
                            elif extension in seven_zip_extensions:
                                with py7zr.SevenZipFile(source_file) as seven_zip:
                                    for file in seven_zip.getnames():
                                        if get_file_extension(file):
                                            source_file_list.append(file)

                        if os.path.isfile(repacked_file):
                            with zipfile.ZipFile(repacked_file) as zip:
                                for file in zip.namelist():
                                    if get_file_extension(file):
                                        repacked_file_list.append(file)

                        # sort them
                        source_file_list.sort()
                        repacked_file_list.sort()

                        # print any files that aren't shared between the two archives
                        if (source_file_list and repacked_file_list) and (
                            source_file_list != repacked_file_list
                        ):
                            print(
                                "\t\t\tVerifying that all files are present in both archives..."
                            )
                            for file in source_file_list:
                                if file not in repacked_file_list:
                                    print(f"\t\t\t\t{file} is not in {repacked_file}")
                            for file in repacked_file_list:
                                if file not in source_file_list:
                                    print(f"\t\t\t\t{file} is not in {source_file}")

                            # remove temp directory
                            remove_folder(temp_dir)

                            # remove cbz file
                            remove_file(repacked_file)

                            continue
                        else:
                            print("\t\t\tAll files are present in both archives.")

                        hashes_verified = False

                        # Verify hashes of all files inside the cbz file
                        with zipfile.ZipFile(repacked_file) as zip:
                            for file in zip.namelist():
                                if get_file_extension(file):
                                    hash = get_file_hash(repacked_file, True, file)
                                    if hash and hash not in hashes:
                                        print(f"\t\t\t\t{file} hash did not match")
                                        break
                            else:
                                hashes_verified = True

                        # Remove temp directory
                        remove_folder(temp_dir)

                        if hashes_verified:
                            send_message("\t\t\tHashes verified.", discord=False)
                            send_message(
                                f"\t\t\tConverted {source_file} to {repacked_file}",
                                discord=False,
                            )
                            embed = handle_fields(
                                DiscordEmbed(
                                    title="Converted to CBZ",
                                    color=grey_color,
                                ),
                                fields=[
                                    {
                                        "name": "From",
                                        "value": f"```{os.path.basename(source_file)}```",
                                        "inline": False,
                                    },
                                    {
                                        "name": "To",
                                        "value": f"```{os.path.basename(repacked_file)}```",
                                        "inline": False,
                                    },
                                    {
                                        "name": "Location",
                                        "value": f"```{os.path.dirname(repacked_file)}```",
                                        "inline": False,
                                    },
                                ],
                            )
                            grouped_notifications = group_notification(
                                grouped_notifications, Embed(embed, None)
                            )

                            # remove the source file
                            remove_file(source_file)

                            if watchdog_toggle:
                                if source_file in transferred_files:
                                    transferred_files.remove(source_file)
                                if repacked_file not in transferred_files:
                                    transferred_files.append(repacked_file)
                        else:
                            send_message("\t\t\tHashes did not verify", error=True)
                            # remove cbz file
                            remove_file(repacked_file)

                    elif extension == ".zip" and rename_zip_to_cbz:
                        header_extension = get_header_extension(file_path)
                        # if it's a zip file, then rename it to cbz
                        if (
                            zipfile.is_zipfile(file_path)
                            or header_extension in manga_extensions
                        ):
                            rename_path = f"{get_extensionless_name(file_path)}.cbz"

                            user_input = (
                                get_input_from_user(
                                    "\t\t\tRename to CBZ",
                                    ["y", "n"],
                                    ["y", "n"],
                                )
                                if manual_rename
                                else "y"
                            )

                            if user_input == "y":
                                rename_file(
                                    file_path,
                                    rename_path,
                                )
                                if os.path.isfile(rename_path) and not os.path.isfile(
                                    file_path
                                ):
                                    if watchdog_toggle:
                                        if file_path in transferred_files:
                                            transferred_files.remove(file_path)
                                        if rename_path not in transferred_files:
                                            transferred_files.append(rename_path)
                            else:
                                print("\t\t\t\tSkipping...")
                except Exception as e:
                    send_message(
                        f"Error when correcting extension: {entry}: {e}",
                        error=True,
                    )

                    # if the tempdir exists, remove it
                    if os.path.isdir(temp_dir):
                        remove_folder(temp_dir)

                    # if the cbz file exists, remove it
                    if os.path.isfile(repacked_file):
                        remove_file(repacked_file)


# Retrieve the file specified from the zip file and return the data for it.
def get_file_from_zip(zip_file, searches, extension=None, allow_base=True):
    result = None
    try:
        with zipfile.ZipFile(zip_file, "r") as z:
            # Filter out any item that doesn't end in the specified extension
            file_list = [
                item
                for item in z.namelist()
                if item.endswith(extension) or not extension
            ]

            # Interate through it
            for path in file_list:
                # if allow_base, then change it to the base name of the file
                # otherwise purge the base name
                mod_file_name = (
                    os.path.basename(path).lower()
                    if allow_base
                    else (
                        re.sub(os.path.basename(path), "", path).lower()
                        if re.sub(os.path.basename(path), "", path).lower()
                        else path.lower()
                    )
                )
                found = any(
                    (
                        item
                        for item in searches
                        if re.search(item, mod_file_name, re.IGNORECASE)
                    ),
                )
                if found:
                    result = z.read(path)
                    break
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        send_message(f"Attempted to read file: {zip_file}\nERROR: {e}", error=True)
    return result


# check if zip file contains ComicInfo.xml
def contains_comic_info(zip_file):
    result = False
    try:
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            if "comicinfo.xml" in map(str.lower, zip_ref.namelist()):
                result = True
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        send_message(f"\tFile: {zip_file}\n\t\tERROR: {e}", error=True)
    return result