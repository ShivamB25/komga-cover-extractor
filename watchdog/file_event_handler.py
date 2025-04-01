import os
import time
import threading
import cProfile # Keep if profiling is intended

from watchdog.events import FileSystemEventHandler

# Assuming settings and other necessary modules are imported correctly
from settings import (
    delete_unacceptable_files_toggle,
    convert_to_cbz_toggle,
    unacceptable_keywords, # Needs definition
    convertable_file_extensions, # Needs definition
    image_extensions, # Needs definition
    file_extensions, # Needs definition
    download_folders, # Needs definition
    paths, # Needs definition
    watchdog_discover_new_files_check_interval,
    watchdog_file_transferred_check_interval,
    profile_code, # Needs definition
    sleep_timer, # Needs definition
)
from core.file_utils import get_file_extension, get_file_size # Assuming these are available
# from core.models import Folder # Assuming Folder model is available
# from messaging.discord_messenger import DiscordEmbed, handle_fields, group_notification, Embed # If Discord needed
# from messaging.log_manager import send_message # If logging needed
# from main import main # Assuming main function is accessible for triggering processing

# Placeholder for functions/classes that need proper import/definition
def is_file_transferred(path): # Placeholder
    print(f"Placeholder: Checking if {path} is transferred")
    time.sleep(0.1) # Simulate check
    return True

def get_all_files_recursively_in_dir_watchdog(dir_path): # Placeholder
    print(f"Placeholder: Getting files recursively for watchdog in {dir_path}")
    results = []
    for root, _, files in os.walk(dir_path):
        for f in files:
            results.append(os.path.join(root, f))
    return results

def create_folder_obj(path, **kwargs): # Placeholder
    print(f"Placeholder: Creating Folder object for {path}")
    # Needs actual Folder object creation
    return {"root": path, "files": get_all_files_recursively_in_dir_watchdog(path)} # Basic dict representation

def main(): # Placeholder for the main processing function
     print("Placeholder: Running main processing function...")

class Handler(FileSystemEventHandler):
    def __init__(self, lock):
        self.lock = lock
        self.transferred_files = [] # Instance variable to track processed files
        self.transferred_dirs = [] # Instance variable to track processed dirs

    def on_created(self, event):
        with self.lock:
            start_time = time.time()
            # global grouped_notifications # Avoid global state if possible

            try:
                # Use instance variables instead of global
                # global transferred_files, transferred_dirs

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

                if not extension:
                    print("\t\t -No extension found, skipped.")
                    return None

                if event.is_directory:
                    print("\t\t -Is a directory, skipped.")
                    return None

                elif self.transferred_files and event.src_path in self.transferred_files:
                    print("\t\t -Already processed, skipped.")
                    return None

                elif not in_file_extensions:
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

                print("\nStarting Execution (WATCHDOG)") # Use logging placeholder

                # Placeholder for Discord notification
                # embed = handle_fields(...)
                # send_discord_message(...)

                print(f"\n\tFile Found: {event.src_path}\n")

                if not os.path.isfile(event.src_path):
                    return None

                # Get a list of all files in the root directory and its subdirectories.
                files_to_check = []
                for folder in download_folders: # Use download_folders from settings
                    files_to_check.extend(get_all_files_recursively_in_dir_watchdog(folder)) # Use placeholder

                # Check if all files are fully transferred.
                while True:
                    all_files_transferred = True
                    print(f"\nTotal files to check: {len(files_to_check)}")

                    current_files_in_loop = list(files_to_check) # Iterate over a copy

                    for file_path in current_files_in_loop:
                        if not os.path.exists(file_path): # Check if file still exists
                             print(f"\t\t- File no longer exists: {file_path}")
                             files_to_check.remove(file_path) # Remove from original list
                             all_files_transferred = False # Re-evaluate after removal
                             break # Restart inner loop

                        print(
                            f"\t[{current_files_in_loop.index(file_path) + 1}/{len(current_files_in_loop)}] {os.path.basename(file_path)}"
                        )

                        if file_path in self.transferred_files:
                            print("\t\t-already transferred")
                            continue

                        is_transferred = is_file_transferred(file_path) # Use placeholder

                        if is_transferred:
                            print("\t\t-fully transferred")
                            self.transferred_files.append(file_path)
                            dir_path = os.path.dirname(file_path)
                            # Check against download_folders and already transferred dirs
                            if dir_path not in download_folders and dir_path not in [d['root'] for d in self.transferred_dirs if isinstance(d, dict)]: # Adapt check based on create_folder_obj output
                                self.transferred_dirs.append(create_folder_obj(dir_path)) # Use placeholder
                        else:
                            print("\t\t-still transferring...")
                            all_files_transferred = False
                            break # Break inner loop to wait

                    if not all_files_transferred:
                         # If a file was removed or still transferring, wait before re-checking
                         time.sleep(watchdog_file_transferred_check_interval)
                         # Update files_to_check in case new files appeared during the wait
                         new_files_check = []
                         for folder in download_folders:
                             new_files_check.extend(get_all_files_recursively_in_dir_watchdog(folder))
                         files_to_check = list(set(new_files_check)) # Update and remove duplicates
                         continue # Continue the while loop

                    # If all files in the current list are transferred, wait and check for new files
                    if all_files_transferred:
                        print(f"\nAll {len(files_to_check)} files appear transferred. Waiting {watchdog_discover_new_files_check_interval}s for potential new files...")
                        time.sleep(watchdog_discover_new_files_check_interval)

                        new_files = []
                        for folder in download_folders:
                            new_files.extend(get_all_files_recursively_in_dir_watchdog(folder))

                        # Check if new files were added during the wait
                        if set(new_files) == set(files_to_check):
                            print("\nNo new files detected. Proceeding with processing.")
                            break # Exit while loop, all files transferred
                        else:
                            print(f"\nNew files detected. Re-evaluating transfer status. (+{len(set(new_files)) - len(set(files_to_check))})")
                            files_to_check = list(set(new_files)) # Update the list to check
                            # Reset transferred status for the loop? Maybe not, just check new ones.
                            all_files_transferred = False # Force re-check

                # Proceed with the next steps here.
                print("\nAll files are transferred.")

                # Ensure all items are folder objects/dicts (adjust based on create_folder_obj)
                # self.transferred_dirs = [
                #     create_folder_obj(x['root']) if isinstance(x, dict) else x
                #     for x in self.transferred_dirs
                # ]

            except Exception as e:
                 print(f"Error in watchdog handler: {e}") # Use logging placeholder
                 traceback.print_exc() # Print stack trace for debugging

            # Trigger main processing logic
            if profile_code == "main()": # Use profile_code from settings
                cProfile.run("main()", sort="cumtime") # Assuming main() is globally accessible
            else:
                main() # Call the main processing function

            end_time = time.time()
            execution_time = end_time - start_time
            minutes, seconds = divmod(execution_time, 60)
            minutes = int(minutes)
            seconds = int(seconds)

            minute_keyword = "minute" if minutes == 1 else "minutes"
            second_keyword = "second" if seconds == 1 else "seconds"

            execution_time_message = ""
            if minutes and seconds:
                execution_time_message = f"{minutes} {minute_keyword} and {seconds} {second_keyword}"
            elif minutes:
                execution_time_message = f"{minutes} {minute_keyword}"
            elif seconds:
                execution_time_message = f"{seconds} {second_keyword}"
            else:
                execution_time_message = "less than 1 second"

            # Terminal Message
            print(
                f"\nFinished Execution (WATCHDOG)\n\tExecution Time: {execution_time_message}"
            ) # Use logging placeholder

            # Placeholder for Discord notification
            # embed = handle_fields(...)
            # grouped_notifications = group_notification(...)
            # if grouped_notifications: send_discord_message(...)

            # Reset state for the next event if needed, or manage externally
            # self.transferred_files.clear()
            # self.transferred_dirs.clear()

            print("\nWatching for changes... (WATCHDOG)") # Use logging placeholder