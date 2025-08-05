import os
import shutil
import filetype

from config.constants import (
    manga_extensions,
    rar_extensions,
    image_extensions,
    download_folders,
    paths,
    grey_color,
    red_color,
    grouped_notifications,
)
from utils.helpers import (
    send_message,
    get_extensionless_name,
    remove_images,
    group_notification,
)
from models.file_models import Embed
from integrations.discord_client import DiscordEmbed, handle_fields


# Renames the file.
def rename_file(src, dest, silent=False):
    result = False
    if os.path.isfile(src):
        root = os.path.dirname(src)
        if not silent:
            print(f"\n\t\tRenaming {src}")
        try:
            os.rename(src, dest)
        except Exception as e:
            send_message(
                f"Failed to rename {os.path.basename(src)} to {os.path.basename(dest)}\n\tERROR: {e}",
                error=True,
            )
            return result
        if os.path.isfile(dest):
            result = True
            if not silent:
                send_message(
                    f"\n\t\t{os.path.basename(src)} was renamed to {os.path.basename(dest)}",
                    discord=False,
                )
            if get_file_extension(src) not in image_extensions:
                extensionless_filename_src = get_extensionless_name(src)
                extensionless_filename_dst = get_extensionless_name(dest)
                for image_extension in image_extensions:
                    image_file = extensionless_filename_src + image_extension
                    image_file_rename = extensionless_filename_dst + image_extension
                    if os.path.isfile(image_file):
                        try:
                            rename_file(image_file, image_file_rename, silent=True)
                        except Exception as e:
                            send_message(str(e), error=True)
        else:
            send_message(
                f"Failed to rename {src} to {dest}",
                error=True,
            )
    else:
        send_message(f"File {src} does not exist. Skipping rename.", discord=False)
    return result


# Moves a folder and all of its contents to a new location.
def move_folder(folder, new_location, silent=False):
    global grouped_notifications

    result = False
    try:
        if os.path.isdir(folder):
            folder_name = os.path.basename(folder)
            new_file_path = os.path.join(new_location, folder_name)
            if not os.path.isdir(new_file_path):
                shutil.move(folder, new_location)
                if os.path.isdir(new_file_path):
                    result = True
                    if not silent:
                        send_message(
                            f"\n\t\tMoved Folder: {folder} to {new_location}",
                            discord=False,
                        )
                        embed = handle_fields(
                            DiscordEmbed(
                                title="Moved Folder",
                                color=grey_color,
                            ),
                            fields=[
                                {
                                    "name": "Folder",
                                    "value": f"```{folder_name}```",
                                    "inline": False,
                                },
                                {
                                    "name": "From",
                                    "value": f"```{folder}```",
                                    "inline": False,
                                },
                                {
                                    "name": "To",
                                    "value": f"```{new_location}```",
                                    "inline": False,
                                },
                            ],
                        )
                        grouped_notifications = group_notification(
                            grouped_notifications, Embed(embed, None)
                        )
            else:
                send_message(f"\t\tFolder already exists: {new_file_path}", error=True)
    except Exception as e:
        send_message(
            f"\t\tFailed to move folder: {folder_name} to {new_location} - {e}",
            error=True,
        )
    return result


# Moves a file to a new location.
def move_file(file, new_location, silent=False, highest_index_num=""):
    global grouped_notifications
    result = False
    try:
        if os.path.isfile(file.path):
            new_file_path = os.path.join(new_location, file.name)
            if not os.path.isfile(new_file_path):
                shutil.move(file.path, new_location)
                if os.path.isfile(new_file_path):
                    result = True
                    if not silent:
                        send_message(
                            f"\n\t\tMoved File: {file.name} to {new_location}",
                            discord=False,
                        )
                        embed = handle_fields(
                            DiscordEmbed(
                                title="Moved File",
                                color=grey_color,
                            ),
                            fields=[
                                {
                                    "name": "File",
                                    "value": f"```{file.name}```",
                                    "inline": False,
                                },
                                {
                                    "name": "From",
                                    "value": f"```{file.root}```",
                                    "inline": False,
                                },
                                {
                                    "name": "To",
                                    "value": f"```{new_location}```",
                                    "inline": False,
                                },
                            ],
                        )
                        grouped_notifications = group_notification(
                            grouped_notifications, Embed(embed, None)
                        )
            else:
                send_message(f"\t\tFile already exists: {new_file_path}", error=True)
    except Exception as e:
        send_message(
            f"\t\tFailed to move file: {file.name} to {new_location} - {e}",
            error=True,
        )
    return result


# Replaces an old file.
def replace_file(old_file, new_file, highest_index_num=""):
    global grouped_notifications
    result = False

    try:
        if os.path.isfile(old_file.path) and os.path.isfile(new_file.path):
            file_removal_status = remove_file(old_file.path)
            if not os.path.isfile(old_file.path) and file_removal_status:
                move_file(
                    new_file,
                    old_file.root,
                    silent=True,
                    highest_index_num=highest_index_num,
                )
                if os.path.isfile(os.path.join(old_file.root, new_file.name)):
                    result = True
                    send_message(
                        f"\t\tFile: {new_file.name} was moved to: {old_file.root}",
                        discord=False,
                    )
                    embed = handle_fields(
                        DiscordEmbed(
                            title="Moved File",
                            color=grey_color,
                        ),
                        fields=[
                            {
                                "name": "File",
                                "value": f"```{new_file.name}```",
                                "inline": False,
                            },
                            {
                                "name": "To",
                                "value": f"```{old_file.root}```",
                                "inline": False,
                            },
                        ],
                    )
                    grouped_notifications = group_notification(
                        grouped_notifications, Embed(embed, None)
                    )
                else:
                    send_message(
                        f"\tFailed to replace: {old_file.name} with: {new_file.name}",
                        error=True,
                    )
            else:
                send_message(
                    f"\tFailed to remove old file: {old_file.name}\nUpgrade aborted.",
                    error=True,
                )
        else:
            send_message(
                f"\tOne of the files is missing, failed to replace.\n{old_file.path}{new_file.path}",
                error=True,
            )
    except Exception as e:
        send_message(f"Failed file replacement.\nERROR: {e}", error=True)
    return result


# Removes a file and its associated image files.
def remove_file(full_file_path, silent=False):
    global grouped_notifications

    # Check if the file exists
    if not os.path.isfile(full_file_path):
        # Send an error message if the file doesn't exist
        send_message(f"{full_file_path} is not a file.", error=True)
        return False

    try:
        # Try to remove the file
        os.remove(full_file_path)
    except OSError as e:
        # Send an error message if removing the file failed
        send_message(f"Failed to remove {full_file_path}: {e}", error=True)
        return False

    # Check if the file was successfully removed
    if os.path.isfile(full_file_path):
        # Send an error message if the file still exists
        send_message(f"Failed to remove {full_file_path}.", error=True)
        return False

    if not silent:
        # Send a notification that the file was removed
        send_message(f"File removed: {full_file_path}", discord=False)

        # Create a Discord embed
        embed = handle_fields(
            DiscordEmbed(
                title="Removed File",
                color=red_color,
            ),
            fields=[
                {
                    "name": "File",
                    "value": f"```{os.path.basename(full_file_path)}```",
                    "inline": False,
                },
                {
                    "name": "Location",
                    "value": f"```{os.path.dirname(full_file_path)}```",
                    "inline": False,
                },
            ],
        )

        # Add it to the group of notifications
        grouped_notifications = group_notification(
            grouped_notifications, Embed(embed, None)
        )

    # If the file is not an image, remove associated images
    if get_file_extension(full_file_path) not in image_extensions:
        remove_images(full_file_path)

    return True


# Removes the specified folder and all of its contents.
def remove_folder(folder):
    result = False
    if os.path.isdir(folder) and (folder not in download_folders + paths):
        try:
            shutil.rmtree(folder)
            if not os.path.isdir(folder):
                send_message(f"\t\t\tRemoved {folder}", discord=False)
                result = True
            else:
                send_message(f"\t\t\tFailed to remove {folder}", error=True)
        except Exception as e:
            send_message(f"\t\t\tFailed to remove {folder}: {str(e)}", error=True)
            return result
    return result


# Gets the file's file size
def get_file_size(file_path):
    # Check if the file path exists and is a file
    if os.path.isfile(file_path):
        # Get the file information using os.stat()
        file_info = os.stat(file_path)
        # Return the file size using the st_size attribute of file_info
        return file_info.st_size
    else:
        # If the file path does not exist or is not a file, return None
        return None


# Retrieves the file extension on the passed file
def get_file_extension(file):
    return os.path.splitext(file)


# Gets the predicted file extension from the file header using filetype
def get_header_extension(file):
    extension_from_name = get_file_extension(file)
    if extension_from_name in manga_extensions or extension_from_name in rar_extensions:
        try:
            kind = filetype.guess(file)
            if kind is None:
                return None
            elif f".{kind.extension}" in manga_extensions:
                return ".cbz"
            elif f".{kind.extension}" in rar_extensions:
                return ".cbr"
            else:
                return f".{kind.extension}"
        except Exception as e:
            send_message(str(e), error=True)
            return None
    else:
        return None