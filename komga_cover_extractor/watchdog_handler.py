# komga_cover_extractor/watchdog_handler.py
import os
import time
import threading
import cProfile  # Keep for profiling if needed
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Import necessary config variables
# Use try-except for robustness during refactoring
try:
    from .config import (
        download_folders,
        sleep_timer,
        file_extensions,
        image_extensions,
        delete_unacceptable_files_toggle,
        convert_to_cbz_toggle,
        unacceptable_keywords,
        convertable_file_extensions,
        watchdog_discover_new_files_check_interval,
        watchdog_file_transferred_check_interval,
        purple_color,
        profile_code,
        compress_image_option,
        paths,  # Added compress_image_option, paths
        transferred_files,
        transferred_dirs,  # Removed grouped_notifications
    )
except ImportError:
    print(
        "WARN: Could not import from .config, using placeholder values in watchdog_handler."
    )
    download_folders = []
    sleep_timer = 10
    file_extensions = [".cbz", ".zip", ".epub"]
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    delete_unacceptable_files_toggle = False
    convert_to_cbz_toggle = False
    unacceptable_keywords = []
    convertable_file_extensions = [".rar", ".7z"]
    watchdog_discover_new_files_check_interval = 5
    watchdog_file_transferred_check_interval = 1
    purple_color = 7615723
    profile_code = ""
    compress_image_option = False
    paths = []
    # State management needs careful handling - these might need to be passed or managed differently
    transferred_files = []
    transferred_dirs = []
    grouped_notifications = []


# Import necessary functions from other utils
try:
    from .log_utils import send_message
except ImportError:
    print("WARN: Could not import from .log_utils, defining placeholder send_message.")

    def send_message(msg, error=False, discord=False):
        print(f"{'ERROR: ' if error else ''}{msg}")


try:
    from .file_utils import (
        get_file_extension,
        is_file_transferred,
        get_all_files_recursively_in_dir_watchdog,
        create_folder_obj,
    )
except ImportError:
    print("WARN: Could not import from .file_utils, defining placeholders.")

    def get_file_extension(f):
        return os.path.splitext(f)[1]

    def is_file_transferred(f):
        return True  # Assume transferred for placeholder

    def get_all_files_recursively_in_dir_watchdog(p):
        return []

    def create_folder_obj(p, **kwargs):
        return Folder(p, [], "", os.path.basename(p), [])  # Needs Folder model


try:
    from .discord_utils import (
        handle_fields,
        send_discord_message,
        group_notification,
        DiscordEmbed,
    )
except ImportError:
    print("WARN: Could not import from .discord_utils, defining placeholders.")

    def handle_fields(e, f):
        return e

    def send_discord_message(*args, **kwargs):
        pass

    def group_notification(n, e, **kwargs):
        n.append(e)
        return n

    class DiscordEmbed:
        pass


try:
    from .models import Embed, Folder  # Import needed models
except ImportError:
    print("WARN: Could not import from .models, defining placeholder classes.")

    class Embed:
        pass

    class Folder:
        def __init__(self, r, d, bn, fn, f):
            self.root = r  # Basic placeholder


# Import main logic function from core_logic
try:
    from . import core_logic
except ImportError:
    print("WARN: Could not import core_logic, defining placeholder main.")

    class core_logic:
        @staticmethod
        def main():
            print("Placeholder core_logic.main() called.")


class Handler(FileSystemEventHandler):
    """Handles file system events detected by the Watcher."""

    def __init__(self, lock):
        self.lock = lock
        # Use lists within the instance to track state for this handler run
        self.run_transferred_files = []
        self.run_transferred_dirs = []
        self.run_grouped_notifications = []

    def on_created(self, event):
        """Called when a file or directory is created."""
        # Use instance lock to prevent concurrent processing of events if needed
        # Though watchdog usually processes events sequentially per observer
        # with self.lock:
        start_time = time.time()

        try:
            # Use instance lists for state during this run
            # global transferred_files, transferred_dirs, grouped_notifications # Remove global usage

            src_path = event.src_path
            extension = get_file_extension(src_path)
            base_name = os.path.basename(src_path)
            is_hidden = base_name.startswith(".")

            # Basic event filtering
            if event.is_directory or is_hidden:
                # print(f"\tWatchdog: Ignoring directory or hidden file: {src_path}")
                return

            if not os.path.isfile(src_path):
                # print(f"\tWatchdog: Ignoring non-file event: {src_path}")
                # Might be a temporary file that got deleted quickly
                return

            if extension in image_extensions:
                # print(f"\tWatchdog: Ignoring image file: {src_path}")
                return

            # Check against already processed files IN THIS RUN
            if src_path in self.run_transferred_files:
                print(
                    f"\tWatchdog: File already processed in this run, skipping: {src_path}"
                )
                return

            # Check file extension validity
            is_valid_convertable = (
                extension in convertable_file_extensions
            )  # Use imported config value
            is_valid_direct = extension in file_extensions  # Use imported config value

            if not is_valid_direct and not (
                convert_to_cbz_toggle and is_valid_convertable
            ):  # Use imported config value
                if delete_unacceptable_files_toggle:  # Use imported config value
                    # Check if it's explicitly unacceptable
                    is_unacceptable = any(
                        re.search(kw, base_name, re.IGNORECASE)
                        for kw in unacceptable_keywords
                    )  # Use imported config value
                    if is_unacceptable:
                        print(
                            f"\tWatchdog: Unacceptable file detected: {src_path}. Will be handled by delete_unacceptable_files."
                        )
                        # Let delete_unacceptable_files handle it later if called
                        return
                    else:
                        # print(f"\tWatchdog: Ignoring file with unhandled extension: {src_path}")
                        return  # Ignore other unknown types if delete toggle is on but keyword doesn't match
                else:
                    # print(f"\tWatchdog: Ignoring file with unaccepted extension: {src_path}")
                    return  # Ignore if delete toggle is off

            # --- File Transfer Check ---
            print(f"\n\tWatchdog: Detected creation: {src_path}")
            send_message(
                "\nStarting Execution (WATCHDOG)", discord=False
            )  # Use imported log_utils function
            # Discord notification responsibility moved

            # Wait for file transfer to complete
            print(f"\tWatchdog: Waiting for file transfer to complete for: {base_name}")
            while not is_file_transferred(src_path):  # Use imported file_utils function
                print(f"\tWatchdog: ...still transferring {base_name}")
                time.sleep(
                    watchdog_file_transferred_check_interval
                )  # Use imported config value
                # Add a check to break if the file disappears during the wait
                if not os.path.isfile(src_path):
                    print(
                        f"\tWatchdog: File disappeared while waiting for transfer: {src_path}"
                    )
                    return  # Stop processing this event

            print(f"\tWatchdog: File transfer complete for: {src_path}")
            self.run_transferred_files.append(src_path)
            dir_path = os.path.dirname(src_path)
            if (
                dir_path not in download_folders
                and dir_path not in self.run_transferred_dirs
            ):  # Use imported config value
                # Store the directory path string, convert to Folder object later if needed
                self.run_transferred_dirs.append(dir_path)

            # --- Trigger Main Logic ---
            # Instead of checking ALL files again, we can potentially trigger logic
            # more directly, perhaps after a short delay to catch related files.
            # The original logic re-scanned everything, which might be necessary
            # if multiple related files trigger events close together.
            # For now, stick to the original approach of triggering main logic
            # after confirming *this* file is transferred.

            print("\n\tWatchdog: Triggering main processing logic...")

            # Pass the detected directories to the main logic
            # Convert paths to Folder objects just before calling main
            folder_objs_to_process = [
                create_folder_obj(d) for d in self.run_transferred_dirs
            ]  # Use imported file_utils function

            # Clear instance state for the next potential run triggered by this handler
            self.run_transferred_files = []
            self.run_transferred_dirs = []

            # Call the main processing function from core_logic
            # Pass necessary state or context if required
            if profile_code == "core_logic.main()":  # Use imported config value
                cProfile.runctx(
                    "core_logic.main(folders_to_process=folder_objs_to_process)",
                    globals(),
                    locals(),
                    sort="cumtime",
                )
            else:
                # Pass the specific folders detected in this run
                core_logic.main(folders_to_process=folder_objs_to_process)

        except Exception as e:
            send_message(
                f"Error in Watchdog Handler on_created for {event.src_path}: {e}",
                error=True,
            )  # Use imported log_utils function
            import traceback

            traceback.print_exc()  # Print stack trace for debugging

        finally:
            # --- Execution Time & Final Notification ---
            end_time = time.time()
            execution_time = end_time - start_time
            minutes, seconds = divmod(execution_time, 60)
            exec_time_str = ""
            if int(minutes) > 0:
                exec_time_str += f"{int(minutes)} min "
            exec_time_str += f"{seconds:.2f} sec"

            send_message(
                f"\nFinished Execution (WATCHDOG)\n\tExecution Time: {exec_time_str}",
                discord=False,
            )  # Use imported log_utils function
            # Discord notification responsibility moved
            self.run_grouped_notifications = []  # Clear notifications for this run

            print("\nWatching for changes... (WATCHDOG)")


class Watcher:
    """Watches specified directories for file system changes."""

    def __init__(self):
        self.observers = []
        self.lock = (
            threading.Lock()
        )  # Lock for potential shared resources if Handler logic needs it

    def run(self):
        """Starts the watchdog observer threads."""
        if not download_folders:  # Use imported config value
            print("Watcher: No download folders configured.")
            return

        event_handler = Handler(self.lock)
        print("\nWatchdog is enabled, starting observers...")
        for folder in download_folders:
            if not os.path.isdir(folder):
                print(
                    f"Watcher: Configured download folder does not exist or is not a directory: {folder}"
                )
                continue

            observer = Observer()
            try:
                # Watch the specified folder recursively
                observer.schedule(event_handler, folder, recursive=True)
                observer.start()
                self.observers.append(observer)
                print(f"Watcher: Started observer on: {folder}")
            except Exception as e:
                print(f"Watcher: Failed to start observer on {folder}: {e}")

        if not self.observers:
            print("Watcher: No observers started.")
            return

        try:
            while True:
                # Keep the main thread alive while observers run in background threads
                time.sleep(
                    sleep_timer
                )  # Use imported config value (or a different interval for main thread)
        except KeyboardInterrupt:  # Allow graceful shutdown with Ctrl+C
            print("\nWatcher: KeyboardInterrupt received, stopping observers...")
        except Exception as e:
            print(f"ERROR in Watcher main loop: {e}")  # Catch other potential errors
        finally:
            self.stop()

    def stop(self):
        """Stops all observer threads."""
        print("Watcher: Stopping observers...")
        for observer in self.observers:
            if observer.is_alive():
                observer.stop()
                print(
                    f"Watcher: Observer stopped for {observer.name}"
                )  # Observer might not have name attr always
        print("Watcher: Joining observer threads...")
        for observer in self.observers:
            observer.join()  # Wait for threads to finish
        print("Watcher: All observers stopped.")
