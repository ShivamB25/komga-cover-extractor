# -*- coding: utf-8 -*-
"""
Watchdog handler for monitoring file system events.
"""
import os
import threading
import time

import cProfile

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from main import main
from config.constants import (
    compress_image_option,
    convertable_file_extensions,
    convert_to_cbz_toggle,
    delete_unacceptable_files_toggle,
    download_folders,
    file_extensions,
    grouped_notifications,
    image_extensions,
    paths,
    profile_code,
    purple_color,
    sleep_timer,
    transferred_dirs,
    transferred_files,
    unacceptable_keywords,
    watchdog_discover_new_files_check_interval,
    watchdog_file_transferred_check_interval,
)
from filesystem.folder_manager import create_folder_obj, scandir
from integrations.discord_client import (
    DiscordEmbed,
    Embed,
    group_notification,
    handle_fields,
    send_discord_message,
)
from models.file_models import Folder
from utils.helpers import (
    get_all_files_recursively_in_dir_watchdog,
    get_file_extension,
    remove_hidden_files,
    send_message,
)


# Watches the download directory for any changes.
class Watcher:
    def __init__(self):
        self.observers = []
        self.lock = threading.Lock()

    def run(self):
        event_handler = Handler(self.lock)
        for folder in download_folders:
            observer = Observer()
            self.observers.append(observer)
            observer.schedule(event_handler, folder, recursive=True)
            observer.start()

        try:
            while True:
                time.sleep(sleep_timer)
        except Exception as e:
            print(f"ERROR in Watcher.run(): {e}")
            for observer in self.observers:
                observer.stop()
                print("Observer Stopped")
            for observer in self.observers:
                observer.join()
                print("Observer Joined")


# Checks if the file is fully transferred by checking the file size
def is_file_transferred(file_path):
    # Check if the file path exists and is a file
    if not os.path.isfile(file_path):
        return False

    try:
        # Get the file size before waiting for 1 second
        before_file_size = os.path.getsize(file_path)

        # Wait for 1 second
        time.sleep(watchdog_file_transferred_check_interval)

        # Get the file size after waiting for 1 second
        after_file_size = os.path.getsize(file_path)

        # Check if both file sizes are not None and not equal
        if (
            before_file_size is not None
            and after_file_size is not None
            and before_file_size != after_file_size
        ):
            return False

        # If the file size is None or the same, return True, indicating the file transfer is complete
        return True

    except Exception as e:
        send_message(f"ERROR in is_file_transferred(): {e}")
        return False


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


class Handler(FileSystemEventHandler):
    def __init__(self, lock):
        self.lock = lock

    def on_created(self, event):
        with self.lock:
            start_time = time.time()
            global grouped_notifications

            try:
                global transferred_files, transferred_dirs

                extension = get_file_extension(event.src_path)
                base_name = os.path.basename(event.src_path)
                is_hidden = base_name.startswith(".")
                is_valid_file = os.path.isfile(event.src_path)
                in_file_extensions = extension in file_extensions

                if not event.event_type == "created":
                    return None

                if not is_valid_file or extension in image_extensions or is_hidden:
                    return None

                print(f"\n\tEvent Type: {event.event_type}")
                print(f"\tEvent Src Path: {event.src_path}")

                # if not extension was found, return None
                if not extension:
                    print("\t\t -No extension found, skipped.")
                    return None

                # if the event is a directory, return None
                if event.is_directory:
                    print("\t\t -Is a directory, skipped.")
                    return None

                # if transferred_files, and the file is already in transferred_files
                # then it already has been processed, so return None
                elif transferred_files and event.src_path in transferred_files:
                    print("\t\t -Already processed, skipped.")
                    return None

                # check if the extension is not in our accepted file extensions
                elif not in_file_extensions:
                    # if we don't have delete_unacceptable_files_toggle enabled, return None
                    # if delete_unacceptable_files_toggle, we let it past so it can purge it with delete_unacceptable_files()
                    if not delete_unacceptable_files_toggle:
                        print(
                            "\t\t -Not in file extensions and delete_unacceptable_files_toggle is not enabled, skipped."
                        )
                        return None
                    elif (
                        (delete_unacceptable_files_toggle or convert_to_cbz_toggle)
                        and (
                            extension not in unacceptable_keywords
                            and "\\" + extension not in unacceptable_keywords
                        )
                        and not (
                            convert_to_cbz_toggle
                            and extension in convertable_file_extensions
                        )
                    ):
                        print("\t\t -Not in file extensions, skipped.")
                        return None

                # Finally if all checks are passed and the file was just created, we can process it
                # Take any action here when a file is first created.

                send_message("\nStarting Execution (WATCHDOG)", discord=False)

                embed = handle_fields(
                    DiscordEmbed(
                        title="Starting Execution (WATCHDOG)",
                        color=purple_color,
                    ),
                    [
                        {
                            "name": "File Found",
                            "value": f"```{event.src_path}```",
                            "inline": False,
                        }
                    ],
                )

                send_discord_message(
                    None,
                    [Embed(embed, None)],
                )

                print(f"\n\tFile Found: {event.src_path}\n")

                if not os.path.isfile(event.src_path):
                    return None

                # Get a list of all files in the root directory and its subdirectories.
                files = [
                    file
                    for folder in download_folders
                    for file in get_all_files_recursively_in_dir_watchdog(folder)
                ]

                # Check if all files in the root directory and its subdirectories are fully transferred.
                while True:
                    all_files_transferred = True
                    print(f"\nTotal files: {len(files)}")

                    for file in files:
                        print(
                            f"\t[{files.index(file) + 1}/{len(files)}] {os.path.basename(file)}"
                        )

                        if file in transferred_files:
                            print("\t\t-already transferred")
                            continue

                        is_transferred = is_file_transferred(file)

                        if is_transferred:
                            print("\t\t-fully transferred")
                            transferred_files.append(file)
                            dir_path = os.path.dirname(file)
                            if dir_path not in download_folders + transferred_dirs:
                                transferred_dirs.append(os.path.dirname(file))
                        elif not os.path.isfile(file):
                            print("\t\t-file no longer exists")
                            all_files_transferred = False
                            files.remove(file)
                            break
                        else:
                            print("\t\t-still transferreing...")
                            all_files_transferred = False
                            break

                    if all_files_transferred:
                        time.sleep(watchdog_discover_new_files_check_interval)

                        # The current list of files in the root directory and its subdirectories.
                        new_files = [
                            file
                            for folder in download_folders
                            for file in get_all_files_recursively_in_dir_watchdog(
                                folder
                            )
                        ]

                        # If any new files started transferring while we were checking the current files,
                        # then we have more files to check.
                        if files != new_files:
                            all_files_transferred = False
                            if len(new_files) > len(files):
                                print(
                                    f"\tNew transfers: +{len(new_files) - len(files)}"
                                )
                                files = new_files
                            elif len(new_files) < len(files):
                                break
                        elif files == new_files:
                            break

                    time.sleep(watchdog_discover_new_files_check_interval)

                # Proceed with the next steps here.
                print("\nAll files are transferred.")

                # Make sure all items are a folder object
                transferred_dirs = [
                    create_folder_obj(x) if not isinstance(x, Folder) else x
                    for x in transferred_dirs
                ]

            except Exception as e:
                send_message(f"Error with watchdog on_any_event(): {e}", error=True)

            if profile_code == "main()":
                cProfile.run(profile_code, sort="cumtime")
            else:
                main()

            end_time = time.time()
            minute_keyword = ""
            second_keyword = ""

            # get the execution time
            execution_time = end_time - start_time
            minutes, seconds = divmod(execution_time, 60)
            minutes = int(minutes)
            seconds = int(seconds)

            if minutes:
                if minutes == 1:
                    minute_keyword = "minute"
                elif minutes > 1:
                    minute_keyword = "minutes"
            if seconds:
                if seconds == 1:
                    second_keyword = "second"
                elif seconds > 1:
                    second_keyword = "seconds"

            execution_time_message = ""

            if minutes and seconds:
                execution_time_message = (
                    f"{minutes} {minute_keyword} and {seconds} {second_keyword}"
                )
            elif minutes:
                execution_time_message = f"{minutes} {minute_keyword}"
            elif seconds:
                execution_time_message = f"{seconds} {second_keyword}"
            else:
                execution_time_message = "less than 1 second"

            # Terminal Message
            send_message(
                f"\nFinished Execution (WATCHDOG)\n\tExecution Time: {execution_time_message}",
                discord=False,
            )

            # Discord Message
            embed = handle_fields(
                DiscordEmbed(
                    title="Finished Execution (WATCHDOG)",
                    color=purple_color,
                ),
                [
                    {
                        "name": "Execution Time",
                        "value": f"```{execution_time_message}```",
                        "inline": False,
                    }
                ],
            )

            # Add it to the queue
            grouped_notifications = group_notification(
                grouped_notifications, Embed(embed, None)
            )

            # Send any remaining queued notifications to Discord
            if grouped_notifications:
                sent_status = send_discord_message(None, grouped_notifications)
                if sent_status:
                    grouped_notifications = []

            send_message("\nWatching for changes... (WATCHDOG)", discord=False)