import os
import settings as settings_file

# Get all the variables in settings.py
settings = [
    var
    for var in dir(settings_file)
    if not callable(getattr(settings_file, var)) and not var.startswith("__")
]

# Function to parse boolean arguments from string values
def parse_bool_argument(arg_value):
    return str(arg_value).lower().strip() == "true"

# Loads and provides access to settings
class SettingsManager:
    def __init__(self, args):
        self.args = args
        self.load_settings()

    def load_settings(self):
        # Load settings from settings.py
        self.load_external_settings()
        # Load settings from command line arguments
        self.load_cli_settings()

    def load_external_settings(self):
        # Print all the settings from settings.py
        print("\nExternal Settings:")
        sensitive_keywords = ["password", "email", "_ip", "token", "user"]
        ignored_settings = ["ranked_keywords", "unacceptable_keywords"]

        for setting in settings:
            if setting in ignored_settings:
                continue

            value = getattr(settings_file, setting)
            setattr(self, setting, value) # Set the attribute on the instance

            if value and any(keyword in setting.lower() for keyword in sensitive_keywords):
                value = "********"

            print(f"\t{setting}: {value}")

    def load_cli_settings(self):
        print("\nRun Settings:")
        # Process paths
        self.paths = []
        self.paths_with_types = []
        if self.args.paths is not None:
            new_paths = []
            for path in self.args.paths:
                if path and r"\1" in path[0]:
                    split_paths = path[0].split(r"\1")
                    new_paths.extend([split_path] for split_path in split_paths)
                else:
                    new_paths.append(path)

            self.args.paths = new_paths
            print("\tpaths:")
            for path in self.args.paths:
                if path:
                    if r"\0" in path[0]:
                        path = path[0].split(r"\0")
                    self.process_path(path, self.paths_with_types, self.paths)

            if self.paths_with_types:
                print("\n\tpaths_with_types:")
                for item in self.paths_with_types:
                    print(f"\t\tpath: {str(item.path)}")
                    print(f"\t\t\tformats: {str(item.path_formats)}")
                    print(f"\t\t\textensions: {str(item.path_extensions)}")

        # Process download folders
        self.download_folders = []
        self.download_folders_with_types = []
        if self.args.download_folders is not None:
            new_download_folders = []
            for download_folder in self.args.download_folders:
                if download_folder:
                    if r"\1" in download_folder[0]:
                        split_download_folders = download_folder[0].split(r"\1")
                        new_download_folders.extend(
                            [split_download_folder]
                            for split_download_folder in split_download_folders
                        )
                    else:
                        new_download_folders.append(download_folder)

            self.args.download_folders = new_download_folders

            print("\tdownload_folders:")
            for download_folder in self.args.download_folders:
                if download_folder:
                    if r"\0" in download_folder[0]:
                        download_folder = download_folder[0].split(r"\0")
                    self.process_path(
                        download_folder,
                        self.download_folders_with_types,
                        self.download_folders,
                        is_download_folders=True,
                    )

            if self.download_folders_with_types:
                print("\n\tdownload_folders_with_types:")
                for item in self.download_folders_with_types:
                    print(f"\t\tpath: {str(item.path)}")
                    print(f"\t\t\tformats: {str(item.path_formats)}")
                    print(f"\t\t\textensions: {str(item.path_extensions)}")

        # Process webhook URLs
        self.discord_webhook_url = []
        if self.args.webhook is not None:
            for item in self.args.webhook:
                if item:
                    for hook in item:
                        if hook:
                            if r"\1" in hook:
                                hook = hook.split(r"\1")
                            if isinstance(hook, str):
                                if hook and hook not in self.discord_webhook_url:
                                    self.discord_webhook_url.append(hook)
                            elif isinstance(hook, list):
                                for url in hook:
                                    if url and url not in self.discord_webhook_url:
                                        self.discord_webhook_url.append(url)
            print(f"\twebhooks: {str(self.discord_webhook_url)}")

        # Process Bookwalker check
        self.bookwalker_check = False
        if self.args.bookwalker_check:
            self.bookwalker_check = parse_bool_argument(self.args.bookwalker_check)
        print(f"\tbookwalker_check: {self.bookwalker_check}")

        # Process compress option
        self.compress_image_option = False
        if self.args.compress:
            self.compress_image_option = parse_bool_argument(self.args.compress)
        print(f"\tcompress: {self.compress_image_option}")

        # Process compress quality
        self.image_quality = 40 # Default value
        if self.args.compress_quality:
            self.image_quality = int(self.args.compress_quality)
        print(f"\tcompress_quality: {self.image_quality}")

        # Process Bookwalker webhook URLs
        self.bookwalker_webhook_urls = []
        if self.args.bookwalker_webhook_urls is not None:
            for url in self.args.bookwalker_webhook_urls:
                if url:
                    for hook in url:
                        if hook:
                            if r"\1" in hook:
                                hook = hook.split(r"\1")
                            if isinstance(hook, str):
                                if hook and hook not in self.bookwalker_webhook_urls:
                                    self.bookwalker_webhook_urls.append(hook)
                            elif isinstance(hook, list):
                                for url_in_hook in hook:
                                    if (
                                        url_in_hook
                                        and url_in_hook not in self.bookwalker_webhook_urls
                                    ):
                                        self.bookwalker_webhook_urls.append(url_in_hook)
            print(f"\tbookwalker_webhook_urls: {self.bookwalker_webhook_urls}")

        # Process Watchdog toggle
        self.watchdog_toggle = False
        if self.args.watchdog:
            if self.download_folders:
                self.watchdog_toggle = parse_bool_argument(self.args.watchdog)
            else:
                print("Watchdog was toggled, but no download folders were passed to the script.")
        print(f"\twatchdog: {self.watchdog_toggle}")

        # Process Watchdog intervals
        self.watchdog_discover_new_files_check_interval = 5 # Default
        self.watchdog_file_transferred_check_interval = 1 # Default
        if self.watchdog_toggle:
            if self.args.watchdog_discover_new_files_check_interval:
                if self.args.watchdog_discover_new_files_check_interval.isdigit():
                    self.watchdog_discover_new_files_check_interval = int(
                        self.args.watchdog_discover_new_files_check_interval
                    )

            if self.args.watchdog_file_transferred_check_interval:
                if self.args.watchdog_file_transferred_check_interval.isdigit():
                    self.watchdog_file_transferred_check_interval = int(
                        self.args.watchdog_file_transferred_check_interval
                    )
            print(
                f"\t\twatchdog_discover_new_files_check_interval: {self.watchdog_discover_new_files_check_interval}"
            )
            print(
                f"\t\twatchdog_file_transferred_check_interval: {self.watchdog_file_transferred_check_interval}"
            )

        # Process new volume webhook
        self.new_volume_webhook = None
        if self.args.new_volume_webhook:
            self.new_volume_webhook = self.args.new_volume_webhook
        print(f"\tnew_volume_webhook: {self.new_volume_webhook}")

        # Process log to file toggle
        self.log_to_file = True # Default
        if self.args.log_to_file:
            self.log_to_file = parse_bool_argument(self.args.log_to_file)
        print(f"\tlog_to_file: {self.log_to_file}")

        # Process output covers as webp toggle
        self.output_covers_as_webp = False # Default
        if self.args.output_covers_as_webp:
            self.output_covers_as_webp = parse_bool_argument(self.args.output_covers_as_webp)
        print(f"\toutput_covers_as_webp: {self.output_covers_as_webp}")

        # Check if paths or download folders are provided
        if not self.paths and not self.download_folders:
            print("No paths or download folders were passed to the script.")
            print("Exiting...")
            exit()

    def process_path(self, path_data, paths_with_types_list, paths_list, is_download_folders=False):
        # Placeholder for the actual path processing logic
        # This needs to be adapted from the original process_path function
        # For now, just add the path string
        path_str = path_data[0]
        if not is_download_folders:
            paths_list.append(path_str)
        else:
            paths_list.append(path_str)
        print(f"\t\t{path_str}") # Print the processed path

    def get(self, setting_name, default=None):
        return getattr(self, setting_name, default)