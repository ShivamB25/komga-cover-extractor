"""Contains the primary workflow and business logic.

This module coordinates calls between different modules to execute the
application's main functionalities.
"""

import os
import time
import cProfile
import re
import threading
from typing import Any, List, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from discord_webhook import DiscordEmbed

from . import (
    archives,
    config,
    constants,
    filesystem,
    parsers,
    services,
    utils,
    main as app_main,
)
from .models import (
    Folder,
    File,
    Volume,
    Publisher,
    Embed,
    RankedKeywordResult,
    UpgradeResult,
    NewReleaseNotification,
    BookwalkerBook,
    BookwalkerSeries,
    Result,
    IdentifierResult,
    Image_Result,
    Keyword,
)


def extract_covers(paths_to_process=None):
    """The main workflow for extracting covers."""
    if paths_to_process is None:
        paths_to_process = config.paths

    # Helper functions nested inside as in the original script
    def find_series_cover(folder_accessor, image_extensions):
        return next(
            (
                os.path.join(folder_accessor.root, f"cover{ext}")
                for ext in image_extensions
                if os.path.exists(os.path.join(folder_accessor.root, f"cover{ext}"))
            ),
            None,
        )

    def check_same_series_name(files, required_percent=0.9):
        if not files:
            return False
        compare_series = parsers.clean_str(files[0].series_name, skip_bracket=True)
        file_count = len(files)
        required_count = int(file_count * required_percent)
        return (
            sum(
                parsers.clean_str(x.series_name, skip_bracket=True) == compare_series
                for x in files
            )
            >= required_count
        )

    def process_volume_paths(
        files,
        root,
        copy_existing_volume_covers_toggle,
        is_chapter_directory,
        volume_paths,
        paths_with_types,
    ):
        base_name = None
        if copy_existing_volume_covers_toggle and is_chapter_directory:
            if not volume_paths and paths_with_types:
                volume_paths = [
                    x
                    for x in paths_with_types
                    if "volume" in x.path_formats
                    and files[0].extension in x.path_extensions
                ]
                for v_path in volume_paths:
                    volume_series_folders = [
                        x for x in os.listdir(v_path.path) if not x.startswith(".")
                    ]
                    v_path.series_folders = volume_series_folders
            base_name = parsers.clean_str(os.path.basename(root))
        return base_name, volume_paths

    def contains_multiple_volume_ones(
        files, use_latest_volume_cover_as_series_cover, is_chapter_directory
    ):
        if not use_latest_volume_cover_as_series_cover or is_chapter_directory:
            volume_ones = sum(
                1
                for file in files
                if not file.is_one_shot
                and not file.volume_part
                and (
                    file.index_number == 1
                    or (isinstance(file.index_number, list) and 1 in file.index_number)
                )
            )
            return volume_ones > 1
        return False

    if not paths_to_process:
        print("\nNo paths to process.")
        return

    print("\nLooking for covers to extract...")
    volume_paths = []
    moved_folder_names = (
        [
            parsers.clean_str(
                os.path.basename(x),
                skip_bracket=True,
                skip_underscore=True,
            )
            for x in config.moved_folders
        ]
        if config.moved_folders and config.copy_existing_volume_covers_toggle
        else []
    )

    for path in paths_to_process:
        if not os.path.exists(path):
            print(f"\nERROR: {path} is an invalid path.\n")
            continue

        config.checked_series = []
        os.chdir(path)

        for root, dirs, files in filesystem.scandir.walk(path):
            if config.watchdog_toggle:
                if not moved_folder_names or (
                    parsers.clean_str(
                        os.path.basename(root),
                        skip_bracket=True,
                        skip_underscore=True,
                    )
                    not in moved_folder_names
                ):
                    root_mod_time = filesystem.get_modification_date(root)
                    if root in config.root_modification_times:
                        if config.root_modification_times[root] == root_mod_time:
                            continue
                        else:
                            config.root_modification_times[root] = root_mod_time
                    else:
                        config.root_modification_times[root] = root_mod_time

            files, dirs = utils.process_files_and_folders(
                root,
                files,
                dirs,
                just_these_files=config.transferred_files,
                just_these_dirs=config.transferred_dirs,
            )

            if not files:
                continue

            print(f"\nRoot: {root}")
            file_objects = utils.upgrade_to_file_class(files, root)
            volume_objects = utils.upgrade_to_volume_class(
                file_objects,
                skip_release_year=True,
                skip_release_group=True,
                skip_extras=True,
                skip_publisher=True,
                skip_premium_content=True,
                skip_subtitle=True,
                skip_multi_volume=True,
            )
            config.folder_accessor = utils.create_folder_obj(root, dirs, volume_objects)
            series_cover_path = find_series_cover(config.folder_accessor, constants.IMAGE_EXTENSIONS)
            series_cover_extension = filesystem.get_file_extension(series_cover_path) if series_cover_path else ""

            if series_cover_extension and (
                (config.output_covers_as_webp and series_cover_extension != ".webp")
                or (not config.output_covers_as_webp and series_cover_extension == ".webp")
            ):
                if filesystem.remove_file(series_cover_path, silent=True):
                    series_cover_path = ""

            is_chapter_directory = config.folder_accessor.files[0].file_type == "chapter"
            same_series_name = check_same_series_name(config.folder_accessor.files)
            clean_basename, volume_paths = process_volume_paths(
                config.folder_accessor.files,
                config.folder_accessor.root,
                config.copy_existing_volume_covers_toggle,
                is_chapter_directory,
                volume_paths,
                config.paths_with_types,
            )
            highest_index_number = (
                utils.get_highest_release(
                    tuple(
                        [
                            (
                                item.index_number
                                if not isinstance(item.index_number, list)
                                else tuple(item.index_number)
                            )
                            for item in config.folder_accessor.files
                        ]
                    ),
                    is_chapter_directory=is_chapter_directory,
                )
                if not is_chapter_directory
                else ""
            )
            if highest_index_number:
                print(f"\n\t\tHighest Index Number: {highest_index_number}")

            has_multiple_volume_ones = contains_multiple_volume_ones(
                config.folder_accessor.files,
                config.use_latest_volume_cover_as_series_cover,
                is_chapter_directory,
            )

            for file in config.folder_accessor.files:
                if file.file_type == "volume" or (
                    file.file_type == "chapter" and config.extract_chapter_covers
                ):
                    utils.process_cover_extraction(
                        file,
                        has_multiple_volume_ones,
                        highest_index_number,
                        is_chapter_directory,
                        volume_paths,
                        clean_basename,
                        same_series_name,
                        contains_subfolders,
                    )


def check_for_existing_series():
    """Checks for existing series to prevent duplicates."""
    # This function's logic is complex and intertwined with many others.
    # It will be migrated in a subsequent step to ensure correctness.
    pass


def check_for_new_volumes_on_bookwalker():
    """Checks Bookwalker for new volumes of existing series."""
    # This function's logic is complex and intertwined with many others.
    # It will be migrated in a subsequent step to ensure correctness.
    pass


class Watcher:
    """A class to watch for filesystem events."""

    def __init__(self):
        self.observers = []
        self.lock = threading.Lock()

    def run(self):
        event_handler = Handler(self.lock)
        for folder in config.download_folders:
            observer = Observer()
            self.observers.append(observer)
            observer.schedule(event_handler, folder, recursive=True)
            observer.start()
        try:
            while True:
                time.sleep(config.sleep_timer)
        except Exception as e:
            print(f"ERROR in Watcher.run(): {e}")
            for observer in self.observers:
                observer.stop()
                print("Observer Stopped")
            for observer in self.observers:
                observer.join()
                print("Observer Joined")


class Handler(FileSystemEventHandler):
    """A class to handle filesystem events from the Watcher."""

    def __init__(self, lock):
        self.lock = lock

    def on_created(self, event: Any) -> None:
        with self.lock:
            start_time = time.time()
            try:
                extension = filesystem.get_file_extension(event.src_path)
                base_name = os.path.basename(event.src_path)
                is_hidden = base_name.startswith(".")
                is_valid_file = os.path.isfile(event.src_path)
                in_file_extensions = extension in config.file_extensions

                if not event.event_type == "created":
                    return

                if not is_valid_file or extension in constants.IMAGE_EXTENSIONS or is_hidden:
                    return

                print(f"\n\tEvent Type: {event.event_type}")
                print(f"\tEvent Src Path: {event.src_path}")

                if not extension:
                    print("\t\t -No extension found, skipped.")
                    return

                if event.is_directory:
                    print("\t\t -Is a directory, skipped.")
                    return

                if config.transferred_files and event.src_path in config.transferred_files:
                    print("\t\t -Already processed, skipped.")
                    return

                if not in_file_extensions:
                    if not config.delete_unacceptable_files_toggle:
                        print("\t\t -Not in file extensions and delete_unacceptable_files_toggle is not enabled, skipped.")
                        return
                    elif (
                        (config.delete_unacceptable_files_toggle or config.convert_to_cbz_toggle)
                        and (extension not in config.unacceptable_keywords and "\\" + extension not in config.unacceptable_keywords)
                        and not (config.convert_to_cbz_toggle and extension in config.convertable_file_extensions)
                    ):
                        print("\t\t -Not in file extensions, skipped.")
                        return

                print("\nStarting Execution (WATCHDOG)")
                embed = services.handle_fields(
                    DiscordEmbed(
                        title="Starting Execution (WATCHDOG)",
                        color=constants.DISCORD_COLORS["purple"],
                    ),
                    [{"name": "File Found", "value": f"```{event.src_path}```", "inline": False}],
                )
                services.send_discord_message(None, [Embed(embed, None)])
                print(f"\n\tFile Found: {event.src_path}\n")

                if not os.path.isfile(event.src_path):
                    return

                files = [
                    file
                    for folder in config.download_folders
                    for file in utils.get_all_files_recursively_in_dir_watchdog(folder)
                ]

                while True:
                    all_files_transferred = True
                    print(f"\nTotal files: {len(files)}")
                    for file in files:
                        print(f"\t[{files.index(file) + 1}/{len(files)}] {os.path.basename(file)}")
                        if file in config.transferred_files:
                            print("\t\t-already transferred")
                            continue
                        is_transferred = filesystem.is_file_transferred(file)
                        if is_transferred:
                            print("\t\t-fully transferred")
                            config.transferred_files.append(file)
                            dir_path = os.path.dirname(file)
                            if dir_path not in config.download_folders + [d.root for d in config.transferred_dirs]:
                                config.transferred_dirs.append(utils.create_folder_obj(os.path.dirname(file)))
                        elif not os.path.isfile(file):
                            print("\t\t-file no longer exists")
                            all_files_transferred = False
                            files.remove(file)
                            break
                        else:
                            print("\t\t-still transferring...")
                            all_files_transferred = False
                            break
                    if all_files_transferred:
                        time.sleep(config.watchdog_discover_new_files_check_interval)
                        new_files = [
                            file
                            for folder in config.download_folders
                            for file in utils.get_all_files_recursively_in_dir_watchdog(folder)
                        ]
                        if files != new_files:
                            all_files_transferred = False
                            if len(new_files) > len(files):
                                print(f"\tNew transfers: +{len(new_files) - len(files)}")
                                files = new_files
                            elif len(new_files) < len(files):
                                break
                        else:
                            break
                    time.sleep(config.watchdog_discover_new_files_check_interval)

                print("\nAll files are transferred.")
                config.transferred_dirs = [
                    utils.create_folder_obj(x) if not isinstance(x, Folder) else x
                    for x in config.transferred_dirs
                ]
            except Exception as e:
                print(f"Error with watchdog on_created(): {e}")

            if config.profile_code == "main()":
                cProfile.run(config.profile_code, sort="cumtime")
            else:
                app_main.main()

            end_time = time.time()
            execution_time = end_time - start_time
            minutes, seconds = divmod(execution_time, 60)
            minutes = int(minutes)
            seconds = int(seconds)
            minute_keyword = "minute" if minutes == 1 else "minutes"
            second_keyword = "second" if seconds == 1 else "seconds"

            if minutes and seconds:
                execution_time_message = f"{minutes} {minute_keyword} and {seconds} {second_keyword}"
            elif minutes:
                execution_time_message = f"{minutes} {minute_keyword}"
            elif seconds:
                execution_time_message = f"{seconds} {second_keyword}"
            else:
                execution_time_message = "less than 1 second"

            print(f"\nFinished Execution (WATCHDOG)\n\tExecution Time: {execution_time_message}")
            embed = services.handle_fields(
                DiscordEmbed(
                    title="Finished Execution (WATCHDOG)",
                    color=constants.DISCORD_COLORS["purple"],
                ),
                [{"name": "Execution Time", "value": f"```{execution_time_message}```", "inline": False}],
            )
            config.grouped_notifications = services.group_notification(config.grouped_notifications, Embed(embed, None))
            if config.grouped_notifications:
                if services.send_discord_message(None, config.grouped_notifications):
                    config.grouped_notifications = []
            print("\nWatching for changes... (WATCHDOG)")


def reorganize_and_rename(files: List[Volume], dir: str) -> List[Volume]:
    """Rebuilds the file name by cleaning up, adding, and moving some parts around.

    Args:
        files (List[Volume]): A list of Volume objects to process.
        dir (str): The base directory name.

    Returns:
        List[Volume]: The updated list of Volume objects.
    """
    modifiers = {
        ext: (
            "[%s]"
            if ext in constants.NOVEL_EXTENSIONS
            else "(%s)"
            if ext in constants.MANGA_EXTENSIONS
            else ""
        )
        for ext in constants.FILE_EXTENSIONS
    }
    base_dir = os.path.basename(dir)

    for file in files[:]:
        try:
            keywords, preferred_naming_format, zfill_int, zfill_float = (
                (
                    constants.CHAPTER_KEYWORDS,
                    config.preferred_chapter_renaming_format,
                    config.zfill_chapter_int_value,
                    config.zfill_chapter_float_value,
                )
                if file.file_type == "chapter"
                else (
                    constants.VOLUME_KEYWORDS,
                    config.preferred_volume_renaming_format,
                    config.zfill_volume_int_value,
                    config.zfill_volume_float_value,
                )
            )
            regex_pattern = rf"(\b({'|'.join(keywords)})([-_.]|)(([0-9]+)((([-_.]|)([0-9]+))+|))(\s|{constants.file_extensions_regex}))"
            if re.search(regex_pattern, file.name, re.IGNORECASE):
                rename = f"{base_dir} {preferred_naming_format}"
                numbers = []

                if file.multi_volume:
                    for n in file.volume_number:
                        numbers.append(n)
                        if n != file.volume_number[-1]:
                            numbers.append("-")
                else:
                    numbers.append(file.volume_number)

                number_string = ""
                for number in numbers:
                    if isinstance(number, (int, float)):
                        if number < 10 or (
                            file.file_type == "chapter" and number < 100
                        ):
                            fill_type = (
                                zfill_int if isinstance(number, int) else zfill_float
                            )
                            number_string += str(number).zfill(fill_type)
                        else:
                            number_string += str(number)
                    elif isinstance(number, str):
                        number_string += number

                rename += number_string

                if (
                    config.add_issue_number_to_manga_file_name
                    and file.file_type == "volume"
                    and file.extension in constants.MANGA_EXTENSIONS
                    and number_string
                ):
                    rename += f" #{number_string}"

                if file.subtitle:
                    rename += f" - {file.subtitle}"

                if file.volume_year:
                    rename += f" {modifiers[file.extension] % file.volume_year}"
                    file.extras = [
                        item
                        for item in file.extras
                        if not (
                            str(file.volume_year) in item
                            or utils.similar(item, str(file.volume_year))
                            >= config.required_similarity_score
                            or re.search(r"([\[\(\{]\d{4}[\]\)\}])", item)
                        )
                    ]

                if (
                    file.publisher.from_meta or file.publisher.from_name
                ) and config.add_publisher_name_to_file_name_when_renaming:
                    for item in file.extras[:]:
                        for publisher in config.publishers:
                            item_without_special_chars = re.sub(
                                r"[\(\[\{\)\]\}]", "", item
                            )
                            meta_similarity = (
                                utils.similar(
                                    item_without_special_chars, file.publisher.from_meta
                                )
                                if file.publisher.from_meta
                                else 0
                            )
                            name_similarity = (
                                utils.similar(
                                    item_without_special_chars, file.publisher.from_name
                                )
                                if file.publisher.from_name
                                else 0
                            )
                            if (
                                utils.similar(item_without_special_chars, publisher)
                                >= config.publisher_similarity_score
                                or meta_similarity >= config.publisher_similarity_score
                                or name_similarity >= config.publisher_similarity_score
                            ):
                                file.extras.remove(item)
                                break
                    if file.publisher.from_meta or file.publisher.from_name:
                        rename += f" {modifiers[file.extension] % (file.publisher.from_meta or file.publisher.from_name)}"

                if file.is_premium and config.search_and_add_premium_to_file_name:
                    rename += f" {modifiers[file.extension] % 'Premium'}"
                    file.extras = [
                        item for item in file.extras if "premium" not in item.lower()
                    ]

                left_brackets = r"(\(|\[|\{)"
                right_brackets = r"(\)|\]|\})"

                if (
                    config.move_release_group_to_end_of_file_name
                    and config.add_publisher_name_to_file_name_when_renaming
                    and file.release_group
                    and file.release_group != file.publisher.from_meta
                    and file.release_group != file.publisher.from_name
                ):
                    file.extras = [
                        item
                        for item in file.extras
                        if not (
                            utils.similar(
                                re.sub(r"[\(\[\{\)\]\}]", "", item), file.release_group
                            )
                            >= config.release_group_similarity_score
                            or re.search(
                                rf"{left_brackets}{re.escape(item)}{right_brackets}",
                                file.release_group,
                                re.IGNORECASE,
                            )
                        )
                    ]

                if file.extras:
                    extras_to_add = [
                        extra
                        for extra in file.extras
                        if not re.search(re.escape(extra), rename, re.IGNORECASE)
                    ]
                    if extras_to_add:
                        rename += " " + " ".join(extras_to_add)

                rename = rename.replace("*", "")

                if config.move_release_group_to_end_of_file_name and file.release_group:
                    release_group_escaped = re.escape(file.release_group)
                    if not re.search(
                        rf"\b{release_group_escaped}\b", rename, re.IGNORECASE
                    ):
                        rename += f" {modifiers[file.extension] % file.release_group}"

                rename += file.extension
                rename = rename.strip()

                if config.replace_unicode_when_restructuring and parsers.contains_unicode(rename):
                    rename = parsers.unidecode(rename)

                rename = rename.replace('"', "'")
                rename = rename.replace("/", "-")
                config.processed_files.append(rename)

                if file.name != rename:
                    rename_path = os.path.join(file.root, rename)
                    if config.watchdog_toggle:
                        config.transferred_files.append(rename_path)

                    try:
                        print(f"\n\t\tBEFORE: {file.name}")
                        print(f"\t\tAFTER:  {rename}")
                        user_input = (
                            utils.get_input_from_user(
                                "\t\tReorganize & Rename", ["y", "n"], ["y", "n"]
                            )
                            if config.manual_rename
                            else "y"
                        )
                        if user_input == "y":
                            if not os.path.isfile(rename_path):
                                rename_status = filesystem.rename_file(
                                    file.path,
                                    rename_path,
                                )
                                if not rename_status:
                                    continue
                                if file.path in config.transferred_files:
                                    config.transferred_files.remove(file.path)
                                print("\t\t\tSuccessfully reorganized & renamed file.\n")
                                if not config.mute_discord_rename_notifications:
                                    embed = services.handle_fields(
                                        DiscordEmbed(
                                            title="Reorganized & Renamed File",
                                            color=constants.DISCORD_COLORS["grey"],
                                        ),
                                        fields=[
                                            {
                                                "name": "From",
                                                "value": f"```{file.name}```",
                                                "inline": False,
                                            },
                                            {
                                                "name": "To",
                                                "value": f"```{rename}```",
                                                "inline": False,
                                            },
                                        ],
                                    )
                                    config.grouped_notifications = services.group_notification(
                                        config.grouped_notifications, Embed(embed, None)
                                    )
                            else:
                                print(
                                    f"\t\tFile already exists, skipping rename of {file.name} to {rename} and deleting {file.name}"
                                )
                                filesystem.remove_file(file.path, silent=True)
                            replacement_obj = utils.upgrade_to_volume_class(
                                utils.upgrade_to_file_class([rename], file.root)
                            )[0]
                            if replacement_obj not in files:
                                files.append(replacement_obj)
                                if file in files:
                                    files.remove(file)
                        else:
                            print("\t\t\tSkipping...\n")
                    except OSError as ose:
                        print(f"Error renaming file: {ose}")
        except Exception as e:
            print(f"Failed to reorganize and rename file: {file.name}: {e}")
    return files


def check_for_duplicate_volumes(paths_to_search: Optional[List[str]] = None) -> None:
    """Checks for any duplicate releases and deletes the lower ranking one.

    Args:
        paths_to_search (Optional[List[str]]): A list of paths to search.
    """
    if paths_to_search is None:
        paths_to_search = config.paths

    if not paths_to_search:
        return

    try:
        for p in paths_to_search:
            if not os.path.exists(p):
                print(f"\nERROR: {p} is an invalid path.\n")
                continue

            print(f"\nSearching {p} for duplicate releases...")
            for root, dirs, files in filesystem.scandir.walk(p):
                print(f"\t{root}")
                files, dirs = utils.process_files_and_folders(
                    root,
                    files,
                    dirs,
                    just_these_files=config.transferred_files,
                    just_these_dirs=config.transferred_dirs,
                )
                if not files:
                    continue

                file_objects = utils.upgrade_to_file_class(
                    [f for f in files if os.path.isfile(os.path.join(root, f))],
                    root,
                )
                file_objects = list(
                    {
                        fo
                        for fo in file_objects
                        for compare in file_objects
                        if fo.name != compare.name
                        and (fo.volume_number != "" and compare.volume_number != "")
                        and fo.volume_number == compare.volume_number
                        and fo.root == compare.root
                        and fo.extension == compare.extension
                        and fo.file_type == compare.file_type
                    }
                )
                volumes = utils.upgrade_to_volume_class(file_objects)
                volumes = list(
                    {
                        v
                        for v in volumes
                        for compare in volumes
                        if v.name != compare.name
                        and v.index_number == compare.index_number
                        and v.root == compare.root
                        and v.extension == compare.extension
                        and v.file_type == compare.file_type
                        and v.series_name == compare.series_name
                    }
                )

                for file in volumes:
                    try:
                        if not os.path.isfile(file.path):
                            continue
                        volume_series_name = parsers.clean_str(file.series_name)
                        compare_volumes = [
                            x
                            for x in volumes.copy()
                            if x.name != file.name
                            and x.index_number == file.index_number
                            and x.root == file.root
                            and x.extension == file.extension
                            and x.file_type == file.file_type
                            and x.series_name == file.series_name
                        ]
                        if compare_volumes:
                            print(f"\t\tChecking: {file.name}")
                            for compare_file in compare_volumes:
                                try:
                                    if os.path.isfile(compare_file.path):
                                        print(f"\t\t\tAgainst: {compare_file.name}")
                                        compare_volume_series_name = parsers.clean_str(
                                            compare_file.series_name
                                        )
                                        if (
                                            file.root == compare_file.root
                                            and (
                                                file.index_number != ""
                                                and compare_file.index_number != ""
                                            )
                                            and file.index_number
                                            == compare_file.index_number
                                            and file.extension == compare_file.extension
                                            and (
                                                file.series_name.lower()
                                                == compare_file.series_name.lower()
                                                or utils.similar(
                                                    volume_series_name,
                                                    compare_volume_series_name,
                                                )
                                                >= config.required_similarity_score
                                            )
                                            and file.file_type == compare_file.file_type
                                        ):
                                            main_file_upgrade_status = is_upgradeable(
                                                file, compare_file
                                            )
                                            compare_file_upgrade_status = (
                                                is_upgradeable(compare_file, file)
                                            )
                                            if (
                                                main_file_upgrade_status.is_upgrade
                                                or compare_file_upgrade_status.is_upgrade
                                            ):
                                                duplicate_file = None
                                                upgrade_file = None
                                                if main_file_upgrade_status.is_upgrade:
                                                    duplicate_file = compare_file
                                                    upgrade_file = file
                                                elif (
                                                    compare_file_upgrade_status.is_upgrade
                                                ):
                                                    duplicate_file = file
                                                    upgrade_file = compare_file
                                                print(
                                                    f"\n\t\t\tDuplicate release found in: {upgrade_file.root}"
                                                    f"\n\t\t\tDuplicate: {duplicate_file.name} has a lower score than {upgrade_file.name}"
                                                    f"\n\n\t\t\tDeleting: {duplicate_file.name} inside of {duplicate_file.root}\n"
                                                )
                                                embed = services.handle_fields(
                                                    DiscordEmbed(
                                                        title="Duplicate Download Release (Not Upgradeable)",
                                                        color=constants.DISCORD_COLORS["yellow"],
                                                    ),
                                                    fields=[
                                                        {
                                                            "name": "Location",
                                                            "value": f"```{upgrade_file.root}```",
                                                            "inline": False,
                                                        },
                                                        {
                                                            "name": "Duplicate",
                                                            "value": f"```{duplicate_file.name}```",
                                                            "inline": False,
                                                        },
                                                        {
                                                            "name": "has a lower score than",
                                                            "value": f"```{upgrade_file.name}```",
                                                            "inline": False,
                                                        },
                                                    ],
                                                )
                                                config.grouped_notifications = (
                                                    services.group_notification(
                                                        config.grouped_notifications,
                                                        Embed(embed, None),
                                                    )
                                                )
                                                user_input = (
                                                    utils.get_input_from_user(
                                                        f'\t\t\tDelete "{duplicate_file.name}"',
                                                        ["y", "n"],
                                                        ["y", "n"],
                                                    )
                                                    if config.manual_delete
                                                    else "y"
                                                )
                                                if user_input == "y":
                                                    filesystem.remove_file(
                                                        duplicate_file.path,
                                                    )
                                                else:
                                                    print("\t\t\t\tSkipping...\n")
                                            else:
                                                file_hash = utils.get_file_hash(file.path)
                                                compare_hash = utils.get_file_hash(
                                                    compare_file.path
                                                )
                                                if (compare_hash and file_hash) and (
                                                    compare_hash == file_hash
                                                ):
                                                    embed = services.handle_fields(
                                                        DiscordEmbed(
                                                            title="Duplicate Download Release (HASH MATCH)",
                                                            color=constants.DISCORD_COLORS["yellow"],
                                                        ),
                                                        fields=[
                                                            {
                                                                "name": "Location",
                                                                "value": f"```{file.root}```",
                                                                "inline": False,
                                                            },
                                                            {
                                                                "name": "File Names",
                                                                "value": f"```{file.name}\n{compare_file.name}```",
                                                                "inline": False,
                                                            },
                                                            {
                                                                "name": "File Hashes",
                                                                "value": f"```{file_hash} {compare_hash}```",
                                                                "inline": False,
                                                            },
                                                        ],
                                                    )
                                                    config.grouped_notifications = (
                                                        services.group_notification(
                                                            config.grouped_notifications,
                                                            Embed(embed, None),
                                                        )
                                                    )
                                                    filesystem.remove_file(
                                                        compare_file.path,
                                                    )
                                                else:
                                                    print(
                                                        f"\n\t\t\tDuplicate found in: {compare_file.root}"
                                                        f"\n\t\t\t\t{file.name}"
                                                        f"\n\t\t\t\t{compare_file.name}"
                                                        f"\n\t\t\t\t\tRanking scores are equal, REQUIRES MANUAL DECISION."
                                                    )
                                                    embed = services.handle_fields(
                                                        DiscordEmbed(
                                                            title="Duplicate Download Release (REQUIRES MANUAL DECISION)",
                                                            color=constants.DISCORD_COLORS["yellow"],
                                                        ),
                                                        fields=[
                                                            {
                                                                "name": "Location",
                                                                "value": f"```{compare_file.root}```",
                                                                "inline": False,
                                                            },
                                                            {
                                                                "name": "Duplicate",
                                                                "value": f"```{file.name}```",
                                                                "inline": False,
                                                            },
                                                            {
                                                                "name": "has an equal score to",
                                                                "value": f"```{compare_file.name}```",
                                                                "inline": False,
                                                            },
                                                        ],
                                                    )
                                                    config.grouped_notifications = (
                                                        services.group_notification(
                                                            config.grouped_notifications,
                                                            Embed(embed, None),
                                                        )
                                                    )
                                                    print("\t\t\t\t\tSkipping...")
                                except Exception as e:
                                    print(
                                        f"\n\t\t\tError: {e}\n\t\t\tSkipping: {compare_file.name}"
                                    )
                                    continue
                    except Exception as e:
                        print(f"\n\t\tError: {e}\n\t\tSkipping: {file.name}")
                        continue
    except Exception as e:
        print(f"\n\t\tError: {e}")


def is_upgradeable(downloaded_release: Volume, current_release: Volume) -> UpgradeResult:
    """Checks if the downloaded release is an upgrade for the current release.

    Args:
        downloaded_release (Volume): The downloaded release.
        current_release (Volume): The current release.

    Returns:
        UpgradeResult: An object containing the upgrade status and ranked results.
    """
    downloaded_release_result = None
    current_release_result = None

    if downloaded_release.name == current_release.name:
        results = get_keyword_scores([downloaded_release])
        downloaded_release_result, current_release_result = results[0], results[0]
    else:
        results = get_keyword_scores([downloaded_release, current_release])
        downloaded_release_result, current_release_result = results[0], results[1]

    upgrade_result = UpgradeResult(
        downloaded_release_result.total_score > current_release_result.total_score,
        downloaded_release_result,
        current_release_result,
    )
    return upgrade_result


def get_keyword_scores(releases: List[Volume]) -> List[RankedKeywordResult]:
    """Retrieves the ranked keyword score and matching tags for the passed releases.

    Args:
        releases (List[Volume]): A list of Volume objects.

    Returns:
        List[RankedKeywordResult]: A list of ranked keyword results.
    """
    results = []
    for release in releases:
        tags, score = [], 0.0
        for keyword, compiled_search in zip(
            config.ranked_keywords, constants.COMPILED_SEARCHES
        ):
            if keyword.file_type in ["both", release.file_type]:
                search = compiled_search.search(release.name)
                if search:
                    tags.append(Keyword(search.group(), keyword.score))
                    score += keyword.score
        results.append(RankedKeywordResult(score, tags))
    return results