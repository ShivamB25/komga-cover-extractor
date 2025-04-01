import time
import threading
from watchdog.observers import Observer

# Assuming Handler and settings are imported correctly
from watchdog.file_event_handler import Handler
from settings import download_folders, sleep_timer # Import necessary settings

class Watcher:
    def __init__(self):
        self.observers = []
        # Use a reentrant lock if the handler might recursively trigger events
        # that need the same lock. Otherwise, a standard Lock is fine.
        self.lock = threading.RLock() # Or threading.Lock()

    def run(self):
        event_handler = Handler(self.lock) # Pass the lock to the handler

        if not download_folders:
            print("No download folders configured for Watchdog.")
            return

        print("Starting Watchdog observers...")
        for folder in download_folders:
            if not os.path.isdir(folder):
                 print(f"Watchdog: Folder not found or not a directory: {folder}. Skipping.")
                 continue

            observer = Observer()
            try:
                # Schedule the event handler for the specified folder
                # recursive=True means it will watch subdirectories as well
                observer.schedule(event_handler, folder, recursive=True)
                observer.start()
                self.observers.append(observer)
                print(f"Watching folder: {folder}")
            except Exception as e:
                 print(f"Error starting observer for {folder}: {e}")


        if not self.observers:
             print("No observers started. Watchdog not running.")
             return

        try:
            # Keep the main thread alive while observers run in background threads
            while True:
                # Check if any observers have stopped unexpectedly
                for observer in self.observers:
                    if not observer.is_alive():
                        print(f"Observer for {observer.path} stopped unexpectedly.")
                        # Optionally try to restart the observer or handle the error
                time.sleep(sleep_timer) # Use sleep_timer from settings
        except KeyboardInterrupt:
            print("\nKeyboardInterrupt received. Stopping observers...")
            self.stop()
        except Exception as e:
            print(f"An error occurred in the Watcher run loop: {e}")
            self.stop()

    def stop(self):
        print("Stopping Watchdog observers...")
        for observer in self.observers:
            if observer.is_alive():
                observer.stop()
                print(f"Observer stopped for: {observer.path}") # Assuming observer has path attribute
            # It's good practice to join after stopping to ensure the thread cleans up
            try:
                 observer.join()
                 print(f"Observer joined for: {observer.path}")
            except RuntimeError as e:
                 print(f"Error joining observer thread for {observer.path}: {e}") # Handle cases where join might fail

        self.observers = [] # Clear the list of observers
        print("All observers stopped.")

# Example of how to run the watcher (if this script were the main entry point)
# if __name__ == "__main__":
#     # Make sure download_folders is populated before running
#     if download_folders:
#         watcher = Watcher()
#         watcher.run()
#     else:
#         print("Please configure download_folders in settings.py to run the watcher.")