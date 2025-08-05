# -*- coding: utf-8 -*-
"""
Series matching logic.
"""
import os
import traceback
import zipfile
from os.path import join

from config.constants import (
    cached_paths,
    download_folders,
    manga_extensions,
    match_through_image_similarity,
    messages_to_send,
    new_volume_webhook,
    output_chapter_covers_to_discord,
    paths,
    paths_with_types,
    processed_files,
    required_image_similarity_score,
    required_similarity_score,
    short_word_filter_percentage,
    transferred_dirs,
    transferred_files,
)
from core.upgrade_manager import check_upgrade
from filesystem.file_operations import (
    check_and_delete_empty_folder,
    clean_and_sort,
    create_folder_obj,
    get_all_folders_recursively_in_dir,
    process_files_and_folders,
)
from filesystem.folder_manager import scandir
from integrations.discord_client import (
    DiscordEmbed,
    Embed,
    group_notification,
    handle_fields,
    pick_webhook,
    send_discord_message,
)
from models.file_models import (
    IdentifierResult,
    upgrade_to_file_class,
    upgrade_to_volume_class,
)
from processing.cover_extractor import find_and_extract_cover
from processing.image_processor import prep_images_for_similarity
from processing.metadata_extractor import get_identifiers, get_zip_comment, get_zip_comment_cache
from processing.text_processor import (
    count_words,
    is_same_index_number,
    match_through_identifiers,
    sort_volumes,
)
from utils.helpers import (
    abbreviate_numbers,
    array_to_string,
    cache_path,
    clean_str,
    complete_num_array,
    find_consecutive_items,
    get_shortened_title,
    get_subtitle_from_dash,
    move_strings_to_top,
    organize_by_first_letter,
    parse_words,
    remove_duplicates,
    send_message,
)
from utils.similarity import similar


# Checks for an existing series by pulling the series name from each elidable file in the downloads_folder
# and comparing it to an existin folder within the user's library.
def check_for_existing_series(
    test_mode=[],
    test_paths=paths,
    test_download_folders=download_folders,
    test_paths_with_types=paths_with_types,
    test_cached_paths=cached_paths,
):
    global cached_paths, cached_identifier_results, messages_to_send, grouped_notifications

    # Groups messages by their series
    def group_similar_series(messages_to_send):
        # Initialize an empty list to store grouped series
        grouped_series = []

        # Iterate through the messages in the input list
        for message in messages_to_send:
            series_name = message.series_name

            # Try to find an existing group with the same series name
            group = next(
                (
                    group
                    for group in grouped_series
                    if group["series_name"] == series_name
                ),
                None,
            )

            if group is not None:
                # If a group exists, append the message to that group
                group["messages"].append(message)
            else:
                # If no group exists, create a new group and add it to the list
                grouped_series.append(
                    {"series_name": series_name, "messages": [message]}
                )

        # Return the list of grouped series
        return grouped_series

    # Determines whether an alternative match
    # will be allowed to be attemtped or not.
    def alternative_match_allowed(
        inner_dir,
        file,
        short_word_filter_percentage,
        required_similarity_score,
        counted_words,
    ):
        # Get the subtitle from the folder name
        folder_subtitle = get_subtitle_from_dash(inner_dir, replace=True)
        folder_subtitle_clean = clean_str(folder_subtitle) if folder_subtitle else ""

        # Get the cleaned subtitle from the file series name
        file_subtitle = get_subtitle_from_dash(file.series_name, replace=True)
        file_subtitle_clean = clean_str(file_subtitle) if file_subtitle else ""

        # Get the shortened folder name
        short_fldr_name = clean_str(get_shortened_title(inner_dir) or inner_dir)

        # Get the shortened series name from the file
        short_file_series_name = clean_str(
            file.shortened_series_name or file.series_name
        )

        if not short_fldr_name or not short_file_series_name:
            return False

        long_folder_words = parse_words(inner_dir)
        long_file_words = parse_words(file.series_name)

        # use parse_words() to get the words from both strings
        short_fldr_name_words = parse_words(short_fldr_name)
        short_file_series_words = parse_words(short_file_series_name)

        file_wrds_mod = short_file_series_words
        fldr_wrds_mod = short_fldr_name_words

        if not file_wrds_mod or not fldr_wrds_mod:
            return False

        # Determine the minimum length between file_wrds_mod and fldr_wrds_mod
        # and calculate short_word_filter_percentage(70%) of the minimum length, ensuring it's at least 1
        shortened_length = max(
            1,
            int(
                min(len(file_wrds_mod), len(fldr_wrds_mod))
                * short_word_filter_percentage
            ),
        )

        # Shorten both arrays to the calculated length
        file_wrds_mod = file_wrds_mod[:shortened_length]
        fldr_wrds_mod = fldr_wrds_mod[:shortened_length]

        folder_name_match = (
            short_fldr_name.lower().strip() == short_file_series_name.lower().strip()
        )
        similar_score_match = (
            similar(short_fldr_name, short_file_series_name)
            >= required_similarity_score
        )
        consecutive_items_match = find_consecutive_items(
            tuple(short_fldr_name_words), tuple(short_file_series_words)
        ) or find_consecutive_items(tuple(long_folder_words), tuple(long_file_words))
        unique_words_match = any(
            [
                i
                for i in long_folder_words
                if i in long_file_words and i in counted_words and counted_words[i] <= 3
            ]
        )
        subtitle_match = (folder_subtitle_clean and file_subtitle_clean) and (
            folder_subtitle_clean == file_subtitle_clean
            or similar(folder_subtitle_clean, file_subtitle_clean)
            >= required_similarity_score
        )

        return (
            folder_name_match
            or similar_score_match
            or consecutive_items_match
            or unique_words_match
            or subtitle_match
        )

    # Attempts an alternative match and returns the cover score
    def attempt_alternative_match(
        file_root, inner_dir, file, required_image_similarity_score
    ):
        # Returns volumes with a matching index number
        def get_matching_volumes(file, img_volumes):
            matching_volumes = [
                volume
                for volume in img_volumes
                if is_same_index_number(
                    volume.index_number, file.index_number, allow_array_match=True
                )
            ]

            if (len(img_volumes) - len(matching_volumes)) <= 10:
                matching_volumes.extend(
                    [volume for volume in img_volumes if volume not in matching_volumes]
                )

            return matching_volumes

        img_volumes = upgrade_to_volume_class(
            upgrade_to_file_class(
                [
                    f
                    for f in os.listdir(file_root)
                    if os.path.isfile(join(file_root, f))
                ],
                file_root,
                clean=True,
            )
        )
        if not img_volumes:
            print("\t\t\tNo volumees found for alternative match.")
            return 0, None

        matching_volumes = get_matching_volumes(file, img_volumes)

        if not matching_volumes:
            print("\t\t\tNo matching volumes found for alternative match.")
            return 0, None

        downloaded_volume_cover_data = find_and_extract_cover(
            file,
            return_data_only=True,
            silent=True,
            blank_image_check=True,
        )

        if not downloaded_volume_cover_data:
            print("\t\t\tNo downloaded volume cover data found.")
            return 0, None

        for matching_volume in matching_volumes:
            print(
                f"\t\t\tMatching volume:\n\t\t\t\t{matching_volume.name}\n\t\t\t\t{file.name}"
            )

            existing_volume_cover_data = find_and_extract_cover(
                matching_volume,
                return_data_only=True,
                silent=True,
                blank_image_check=True,
            )

            if not existing_volume_cover_data:
                print("\t\t\tNo existing volume cover data found.")
                continue

            score = prep_images_for_similarity(
                existing_volume_cover_data,
                downloaded_volume_cover_data,
                both_cover_data=True,
                silent=True,
            )

            print(f"\t\t\tRequired Image Similarity: {required_image_similarity_score}")
            print(f"\t\t\t\tCover Image Similarity Score: {score}")

            if score >= required_image_similarity_score:
                return score, matching_volume
        return 0, None

    if test_mode:
        global download_folders, paths, paths_with_types, cached_paths

        if test_download_folders:
            download_folders = test_download_folders
        if test_paths:
            paths = test_paths
        if test_paths_with_types:
            paths_with_types = test_paths_with_types
        if test_cached_paths:
            cached_paths = test_cached_paths

    cached_image_similarity_results = []

    if not download_folders:
        print("\nNo download folders specified, skipping check_for_existing_series.")
        return

    print("\nChecking download folders for items to match to existing library...")
    for download_folder in download_folders:
        if not os.path.exists(download_folder) and not test_mode:
            print(f"\n\t{download_folder} does not exist, skipping...")
            continue

        # Get all the paths
        folders = (
            get_all_folders_recursively_in_dir(download_folder)
            if not test_mode
            else [{"root": "/test_mode", "dirs": [], "files": test_mode}]
        )

        # Reverse the list so we start with the deepest folders
        # Helps when purging empty folders, since it won't purge a folder containing subfolders
        folders.reverse()

        # an array of unmatched items, used for skipping subsequent series
        # items that won't match
        unmatched_series = []

        for folder in folders:
            root = folder["root"]
            dirs = folder["dirs"]
            files = folder["files"]

            print(f"\n{root}")
            volumes = []

            if not test_mode:
                files, dirs = process_files_and_folders(
                    root,
                    files,
                    dirs,
                    sort=True,
                    just_these_files=transferred_files,
                    just_these_dirs=transferred_dirs,
                )

                if not files:
                    continue

                volumes = upgrade_to_volume_class(
                    upgrade_to_file_class(
                        [f for f in files if os.path.isfile(os.path.join(root, f))],
                        root,
                    )
                )
            else:
                volumes = test_mode

            # Sort the volumes
            volumes = sort_volumes(volumes)

            exclude = None

            similar.cache_clear()

            for file in volumes:
                try:
                    if not file.series_name:
                        print(f"\tSkipping: {file.name}\n\t\t - has no series_name")
                        continue

                    if file.volume_number == "":
                        print(f"\tSkipping: {file.name}\n\t\t - has no volume_number")
                        continue

                    if (
                        file.extension in manga_extensions
                        and not test_mode
                        and not zipfile.is_zipfile(file.path)
                    ):
                        print(
                            f"\tSkipping: {file.name}\n\t\t - is not a valid zip file, possibly corrupted."
                        )
                        continue

                    if not (
                        (file.name in processed_files or not processed_files)
                        and (test_mode or os.path.isfile(file.path))
                    ):
                        continue

                    done = False

                    # 1.1 - Check cached image similarity results
                    if (
                        cached_image_similarity_results
                        and match_through_image_similarity
                    ):
                        for cached_result in cached_image_similarity_results:
                            # split on @@ and get the value to the right
                            last_item = cached_result.split("@@")[-1].strip()

                            target_key = f"{file.series_name} - {file.file_type} - {file.root} - {file.extension}"

                            if target_key in cached_result:
                                print(
                                    "\n\t\tFound cached cover image similarity result."
                                )
                                done = check_upgrade(
                                    os.path.dirname(last_item),
                                    os.path.basename(last_item),
                                    file,
                                    similarity_strings=[
                                        file.series_name,
                                        file.series_name,
                                        "CACHE",
                                        required_image_similarity_score,
                                    ],
                                    image=True,
                                    test_mode=test_mode,
                                )
                                if done:
                                    break
                    if done:
                        continue

                    if unmatched_series and (
                        (not match_through_identifiers or file.file_type == "chapter")
                    ):
                        if (
                            f"{file.series_name} - {file.file_type} - {file.extension}"
                            in unmatched_series
                        ):
                            # print(f"\t\tSkipping: {file.name}...")
                            continue

                    # 1.2 - Check cached identifier results
                    if cached_identifier_results and file.file_type == "volume":
                        found_item = next(
                            (
                                cached_identifier
                                for cached_identifier in cached_identifier_results
                                if cached_identifier.series_name == file.series_name
                            ),
                            None,
                        )
                        if found_item:
                            done = check_upgrade(
                                os.path.dirname(found_item.path),
                                os.path.basename(found_item.path),
                                file,
                                similarity_strings=found_item.matches,
                                isbn=True,
                            )
                            if found_item.path not in cached_paths:
                                cache_path(found_item.path)
                            if done:
                                continue

                    if cached_paths:
                        if exclude:
                            cached_paths = organize_by_first_letter(
                                cached_paths, file.name, 1, exclude
                            )
                        else:
                            cached_paths = organize_by_first_letter(
                                cached_paths, file.name, 1
                            )

                    downloaded_file_series_name = clean_str(
                        file.series_name, skip_bracket=True
                    )

                    # organize the cached paths
                    if cached_paths and file.name != downloaded_file_series_name:
                        if exclude:
                            cached_paths = organize_by_first_letter(
                                cached_paths,
                                downloaded_file_series_name,
                                2,
                                exclude,
                            )
                        else:
                            cached_paths = organize_by_first_letter(
                                cached_paths,
                                downloaded_file_series_name,
                                2,
                            )

                    # Move paths matching the first three words to the top of the list
                    if cached_paths:
                        cached_paths = move_strings_to_top(
                            file.series_name, cached_paths
                        )

                    # 2 - Use the cached paths
                    if cached_paths:
                        print("\n\tChecking path types...")
                        for cached_path_index, p in enumerate(cached_paths[:], start=1):
                            if (
                                not os.path.exists(p)
                                or not os.path.isdir(p)
                                or p in download_folders
                            ):
                                continue

                            # Skip any paths that don't contain the file type or extension
                            if paths_with_types:
                                skip_path = next(
                                    (
                                        item
                                        for item in paths_with_types
                                        if p.startswith(item.path)
                                        and (
                                            file.file_type not in item.path_formats
                                            or file.extension
                                            not in item.path_extensions
                                        )
                                    ),
                                    None,
                                )

                                if skip_path:
                                    print(
                                        f"\t\tSkipping: {p} - Path: {skip_path.path_formats} File: {file.file_type}"
                                        if file.file_type not in skip_path.path_formats
                                        else f"\t\tSkipping: {p} - Path: {skip_path.path_extensions} File: {file.extension}"
                                    )
                                    continue

                            successful_series_name = clean_str(
                                os.path.basename(p), skip_bracket=True
                            )

                            successful_similarity_score = (
                                1
                                if successful_series_name == downloaded_file_series_name
                                else similar(
                                    successful_series_name,
                                    downloaded_file_series_name,
                                )
                            )

                            print(
                                f"\n\t\t-(CACHE)- {cached_path_index} of {len(cached_paths)} - "
                                f'"{file.name}"\n\t\tCHECKING: {downloaded_file_series_name}\n\t\tAGAINST:  {successful_series_name}\n\t\tSCORE:    {successful_similarity_score}'
                            )
                            if successful_similarity_score >= required_similarity_score:
                                send_message(
                                    f'\n\t\tSimilarity between: "{successful_series_name}"\n\t\t\t"{downloaded_file_series_name}" Score: {successful_similarity_score} out of 1.0\n',
                                    discord=False,
                                )
                                done = check_upgrade(
                                    os.path.dirname(p),
                                    os.path.basename(p),
                                    file,
                                    similarity_strings=[
                                        downloaded_file_series_name,
                                        downloaded_file_series_name,
                                        successful_similarity_score,
                                        required_similarity_score,
                                    ],
                                    cache=True,
                                    test_mode=test_mode,
                                )
                                if done:
                                    if test_mode:
                                        return done
                                    if p not in cached_paths:
                                        cache_path(p)
                                    if (
                                        len(volumes) > 1
                                        and p in cached_paths
                                        and p != cached_paths
                                    ):
                                        cached_paths.remove(p)
                                        cached_paths.insert(0, p)
                                        exclude = p
                                    break
                    if done:
                        continue

                    dl_zip_comment = get_zip_comment(file.path) if not test_mode else ""
                    dl_meta = get_identifiers(dl_zip_comment) if dl_zip_comment else []

                    directories_found = []
                    matched_ids = []

                    for path_position, path in enumerate(paths, start=1):
                        if done or not os.path.exists(path) or path in download_folders:
                            continue

                        skip_path = next(
                            (
                                item
                                for item in paths_with_types
                                if (
                                    (
                                        path == item.path
                                        and file.file_type not in item.path_formats
                                    )
                                    or (
                                        path == item.path
                                        and file.extension not in item.path_extensions
                                    )
                                )
                            ),
                            None,  # default value if no match is found
                        )

                        # Skip any paths that don't contain the file type or extension
                        if paths_with_types and skip_path:
                            print(
                                f"\nSkipping path: {path} - Path: "
                                f"{array_to_string(skip_path.path_formats) if file.file_type not in skip_path.path_formats else array_to_string(skip_path.path_extensions)}"
                                f" File: {str(file.file_type) if file.file_type not in skip_path.path_formats else str(file.extension)}"
                            )
                            continue

                        try:
                            os.chdir(path)
                            reorganized = False

                            for root, dirs, files in scandir.walk(path):
                                if (
                                    test_mode
                                    and cached_paths
                                    and root in cached_paths
                                    and root not in paths + download_folders
                                ):
                                    continue

                                if not dirs and (
                                    test_mode or not match_through_identifiers
                                ):
                                    continue

                                if done:
                                    break

                                if (
                                    not match_through_identifiers
                                    and root in cached_paths
                                ):
                                    continue

                                counted_words = count_words(dirs)

                                if not reorganized:
                                    dirs = organize_by_first_letter(
                                        dirs,
                                        file.series_name,
                                        1,
                                        exclude=exclude,
                                    )
                                    dirs = organize_by_first_letter(
                                        dirs,
                                        file.series_name,
                                        2,
                                        exclude=exclude,
                                    )
                                    reorganized = True

                                    # Move paths matching the first three words to the top of the list
                                    dirs = move_strings_to_top(file.series_name, dirs)

                                files, dirs = clean_and_sort(root, files, dirs)
                                file_objects = upgrade_to_file_class(files, root)

                                global folder_accessor
                                folder_accessor = create_folder_obj(
                                    root, dirs, file_objects
                                )

                                print(f"Looking inside: {folder_accessor.root}")
                                if (
                                    folder_accessor.dirs
                                    and root not in cached_paths + download_folders
                                ):
                                    if done:
                                        break

                                    print(f"\n\tLooking for: {file.series_name}")
                                    for dir_position, inner_dir in enumerate(
                                        folder_accessor.dirs, start=1
                                    ):
                                        if done:
                                            break

                                        existing_series_folder_from_library = clean_str(
                                            inner_dir
                                        )

                                        similarity_score = (
                                            1
                                            if (
                                                existing_series_folder_from_library.lower()
                                                == downloaded_file_series_name.lower()
                                            )
                                            else similar(
                                                existing_series_folder_from_library,
                                                downloaded_file_series_name,
                                            )
                                        )

                                        print(
                                            f'\n\t\t-(NOT CACHE)- {dir_position} of {len(folder_accessor.dirs)} - path {path_position} of {len(paths)} - "{file.name}"\n\t\tCHECKING: {downloaded_file_series_name}\n\t\tAGAINST:  {existing_series_folder_from_library}\n\t\tSCORE:    {similarity_score}'
                                        )
                                        file_root = os.path.join(
                                            folder_accessor.root, inner_dir
                                        )
                                        if (
                                            similarity_score
                                            >= required_similarity_score
                                        ):
                                            send_message(
                                                f'\n\t\tSimilarity between: "{existing_series_folder_from_library}" and "{downloaded_file_series_name}" '
                                                f"Score: {similarity_score} out of 1.0\n",
                                                discord=False,
                                            )
                                            done = check_upgrade(
                                                folder_accessor.root,
                                                inner_dir,
                                                file,
                                                similarity_strings=[
                                                    downloaded_file_series_name,
                                                    existing_series_folder_from_library,
                                                    similarity_score,
                                                    required_similarity_score,
                                                ],
                                                test_mode=test_mode,
                                            )
                                            if not done:
                                                continue

                                            if test_mode:
                                                return done

                                            if (
                                                file_root not in cached_paths
                                                and not test_mode
                                            ):
                                                cache_path(file_root)
                                            if (
                                                len(volumes) > 1
                                                and file_root in cached_paths
                                                and file_root != cached_paths
                                            ):
                                                cached_paths.remove(file_root)
                                                cached_paths.insert(
                                                    0,
                                                    file_root,
                                                )
                                            break
                                        elif (
                                            match_through_image_similarity
                                            and not test_mode
                                            and alternative_match_allowed(
                                                inner_dir,
                                                file,
                                                short_word_filter_percentage,
                                                required_similarity_score,
                                                counted_words,
                                            )
                                        ):
                                            print(
                                                "\n\t\tAttempting alternative match through cover image similarity..."
                                            )
                                            print(
                                                f"\t\t\tSeries Names: \n\t\t\t\t{inner_dir}\n\t\t\t\t{file.series_name}"
                                            )
                                            (
                                                score,
                                                matching_volume,
                                            ) = attempt_alternative_match(
                                                file_root,
                                                inner_dir,
                                                file,
                                                required_image_similarity_score,
                                            )

                                            if score >= required_image_similarity_score:
                                                print(
                                                    "\t\tMatch found through cover image similarity."
                                                )
                                                # check all volumes in volumes, if all the volumes in this inner_dir have the same series_name
                                                all_matching = False
                                                same_root_files = [
                                                    item
                                                    for item in volumes
                                                    if item.root == file.root
                                                ]
                                                if same_root_files:
                                                    all_matching = all(
                                                        item.series_name.lower().strip()
                                                        == file.series_name.lower().strip()
                                                        for item in same_root_files
                                                        if item != file
                                                    )
                                                if all_matching:
                                                    print(
                                                        "\t\t\tAll Download Series Names Match, Adding to Cache.\n"
                                                    )
                                                    cached_image_similarity_results.append(
                                                        f"{file.series_name} - {file.file_type} - {file.root} - {file.extension} @@ {os.path.join(folder_accessor.root, inner_dir)}"
                                                    )
                                                done = check_upgrade(
                                                    folder_accessor.root,
                                                    inner_dir,
                                                    file,
                                                    similarity_strings=[
                                                        inner_dir,
                                                        file.series_name,
                                                        score,
                                                        required_image_similarity_score,
                                                    ],
                                                    image=matching_volume,
                                                )
                                                if done:
                                                    break

                                # 3.1 - Use identifier matching
                                if (
                                    not done
                                    and not test_mode
                                    and match_through_identifiers
                                    and root not in download_folders
                                    and dl_meta
                                    and file.file_type == "volume"
                                    and folder_accessor.files
                                ):
                                    print(
                                        f"\n\t\tMatching Identifier Search: {folder_accessor.root}"
                                    )
                                    for f in folder_accessor.files:
                                        if f.root in directories_found:
                                            break

                                        if f.extension != file.extension:
                                            continue

                                        print(f"\t\t\t{f.name}")
                                        existing_file_zip_comment = (
                                            get_zip_comment_cache(f.path)
                                        )
                                        existing_file_meta = get_identifiers(
                                            existing_file_zip_comment
                                        )

                                        if existing_file_meta:
                                            print(f"\t\t\t\t{existing_file_meta}")
                                            if any(
                                                d_meta in existing_file_meta
                                                and f.root not in directories_found
                                                for d_meta in dl_meta
                                            ):
                                                directories_found.append(f.root)
                                                matched_ids.extend(
                                                    [
                                                        dl_meta,
                                                        existing_file_meta,
                                                    ]
                                                )
                                                print(
                                                    f"\n\t\t\t\tMatch found in: {f.root}"
                                                )
                                                break
                                        else:
                                            print("\t\t\t\t[]")
                        except Exception as e:
                            # print stack trace
                            send_message(str(e), error=True)

                    # 3.2 - Process identifier matches
                    if (
                        not done
                        and not test_mode
                        and match_through_identifiers
                        and file.file_type == "volume"
                        and directories_found
                    ):
                        directories_found = remove_duplicates(directories_found)

                        if len(directories_found) == 1:
                            matched_directory = directories_found
                            print(f"\n\t\tMatch found in: {matched_directory}\n")
                            base = os.path.basename(matched_directory)

                            identifier = IdentifierResult(
                                file.series_name,
                                dl_meta,
                                matched_directory,
                                matched_ids,
                            )
                            if identifier not in cached_identifier_results:
                                cached_identifier_results.append(identifier)

                            done = check_upgrade(
                                os.path.dirname(matched_directory),
                                base,
                                file,
                                similarity_strings=matched_ids,
                                isbn=True,
                            )

                            if done:
                                if matched_directory not in cached_paths:
                                    cache_path(matched_directory)
                                if (
                                    len(volumes) > 1
                                    and matched_directory in cached_paths
                                    and matched_directory != cached_paths
                                ):
                                    cached_paths.remove(matched_directory)
                                    cached_paths.insert(0, matched_directory)
                        else:
                            print(
                                "\t\t\tMatching ISBN or Series ID found in multiple directories."
                            )
                            for d in directories_found:
                                print(f"\t\t\t\t{d}")
                            print("\t\t\tDisregarding Matches...")

                    if not done:
                        unmatched_series.append(
                            f"{file.series_name} - {file.file_type} - {file.extension}"
                        )
                        print(
                            f"No match found for: {file.series_name} - {file.file_type} - {file.extension}"
                        )
                except Exception as e:
                    stack_trace = traceback.format_exc()
                    print(stack_trace)
                    send_message(str(e), error=True)

        # purge any empty folders
        if folders and not test_mode:
            for folder in folders:
                check_and_delete_empty_folder(folder["root"])

    series_notifications = []
    webhook_to_use = pick_webhook(None, new_volume_webhook)

    if messages_to_send:
        grouped_by_series_names = group_similar_series(messages_to_send)
        messages_to_send = []

        for grouped_by_series_name in grouped_by_series_names:
            group_messages = grouped_by_series_name["messages"]

            if output_chapter_covers_to_discord:
                for message in group_messages[:]:
                    cover = find_and_extract_cover(
                        message.volume_obj, return_data_only=True
                    )
                    embed = handle_fields(
                        DiscordEmbed(
                            title=message.title,
                            color=message.color,
                        ),
                        fields=message.fields,
                    )

                    if new_volume_webhook:
                        series_notifications = group_notification(
                            series_notifications,
                            Embed(embed, cover),
                            webhook_to_use,
                        )
                    else:
                        grouped_notifications = group_notification(
                            grouped_notifications,
                            Embed(embed, cover),
                            webhook_to_use,
                        )
                    group_messages.remove(message)
            else:
                group_numbers = []
                for item in group_messages:
                    if isinstance(item.number, list):
                        filled_num_array = complete_num_array(item.number)
                        for num in filled_num_array:
                            group_numbers.append(num)
                    else:
                        group_numbers.append(item.number)

                abbreviated_numbers_str = abbreviate_numbers(group_numbers)
                volume_numbers_mts = []
                volume_names_mts = []
                first_item = group_messages
                title = first_item.fields["name"]
                title_2 = first_item.fields["name"]
                series_name = first_item.series_name

                for message in group_messages:
                    if message.fields and len(message.fields) >= 2:
                        # remove ``` from the start and end of the value
                        volume_names_mts.append(
                            message.fields["value"].replace("```", "")
                        )

                if abbreviated_numbers_str and volume_names_mts and series_name:
                    new_fields = [
                        {
                            "name": "Series Name",
                            "value": "```" + series_name + "```",
                            "inline": False,
                        },
                        {
                            "name": title,
                            "value": "```" + abbreviated_numbers_str + "```",
                            "inline": False,
                        },
                        {
                            "name": title_2,
                            "value": "```" + "\n".join(volume_names_mts) + "```",
                            "inline": False,
                        },
                    ]
                    embed = handle_fields(
                        DiscordEmbed(
                            title=first_item.title,
                            color=first_item.color,
                        ),
                        fields=new_fields,
                    )

                    if new_volume_webhook:
                        series_notifications = group_notification(
                            series_notifications,
                            Embed(embed, None),
                            webhook_to_use,
                        )
                    else:
                        grouped_notifications = group_notification(
                            grouped_notifications,
                            Embed(embed, None),
                            webhook_to_use,
                        )

    if series_notifications:
        send_discord_message(
            None,
            series_notifications,
            passed_webhook=webhook_to_use,
        )

    # clear lru_cache for parse_words
    parse_words.cache_clear()

    # clear lru_ache for find_consecutive_items()
    find_consecutive_items.cache_clear()