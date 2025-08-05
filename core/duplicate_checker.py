# -*- coding: utf-8 -*-
"""
Duplicate checking functions and classes.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from config.constants import (
    compiled_searches,
    manual_delete,
    ranked_keywords,
    required_similarity_score,
    transferred_dirs,
    transferred_files,
    yellow_color,
)
from core.upgrade_manager import (
    check_for_duplicate_volumes,
    is_upgradeable,
    upgrade_to_file_class,
    upgrade_to_volume_class,
)
from filesystem.file_operations import process_files_and_folders, remove_file
from filesystem.folder_manager import scandir
from integrations.discord_client import (
    DiscordEmbed,
    Embed,
    group_notification,
    handle_fields,
)
from models.file_models import Keyword
from processing.text_processor import clean_str
from utils.helpers import (
    get_file_hash,
    get_input_from_user,
    send_message,
)
from utils.similarity import similar


# The RankedKeywordResult class is a container for the total score and the keywords
class RankedKeywordResult:
    def __init__(self, total_score, keywords):
        self.total_score = total_score
        self.keywords = keywords

    # to string
    def __str__(self):
        return f"Total Score: {self.total_score}\nKeywords: {self.keywords}"

    def __repr__(self):
        return str(self)


# > This class represents the result of an upgrade check
class UpgradeResult:
    def __init__(self, is_upgrade, downloaded_ranked_result, current_ranked_result):
        self.is_upgrade = is_upgrade
        self.downloaded_ranked_result = downloaded_ranked_result
        self.current_ranked_result = current_ranked_result

    # to string
    def __str__(self):
        return f"Is Upgrade: {self.is_upgrade}\nDownloaded Ranked Result: {self.downloaded_ranked_result}\nCurrent Ranked Result: {self.current_ranked_result}"

    def __repr__(self):
        return str(self)


# Retrieves the ranked keyword score and matching tags
# for the passed releases.
def get_keyword_scores(releases):
    results = []

    for release in releases:
        tags, score = [], 0.0

        for idx, (keyword, compiled_search) in enumerate(
            zip(ranked_keywords, compiled_searches)
        ):
            if keyword.file_type in ["both", release.file_type]:
                search = compiled_search.search(release.name)
                if search:
                    tags.append(Keyword(search.group(), keyword.score))
                    score += keyword.score

        results.append(RankedKeywordResult(score, tags))

    return results


# Checks if the downloaded release is an upgrade for the current release.
def is_upgradeable(downloaded_release, current_release):
    downloaded_release_result = None
    current_release_result = None

    if downloaded_release.name == current_release.name:
        results = get_keyword_scores([downloaded_release])
        downloaded_release_result, current_release_result = results, results
    else:
        results = get_keyword_scores([downloaded_release, current_release])
        downloaded_release_result, current_release_result = results, results

    upgrade_result = UpgradeResult(
        downloaded_release_result.total_score > current_release_result.total_score,
        downloaded_release_result,
        current_release_result,
    )
    return upgrade_result


# Checks for any duplicate releases and deletes the lower ranking one.
def check_for_duplicate_volumes(paths_to_search=[]):
    global grouped_notifications

    if not paths_to_search:
        return

    try:
        for p in paths_to_search:
            if not os.path.exists(p):
                send_message(f"\nERROR: {p} is an invalid path.\n", error=True)
                continue

            print(f"\nSearching {p} for duplicate releases...")
            for root, dirs, files in scandir.walk(p):
                print(f"\t{root}")
                files, dirs = process_files_and_folders(
                    root,
                    files,
                    dirs,
                    just_these_files=transferred_files,
                    just_these_dirs=transferred_dirs,
                )

                if not files:
                    continue

                file_objects = upgrade_to_file_class(
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

                volumes = upgrade_to_volume_class(file_objects)
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

                        volume_series_name = clean_str(file.series_name)

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
                                        compare_volume_series_name = clean_str(
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
                                                or similar(
                                                    volume_series_name,
                                                    compare_volume_series_name,
                                                )
                                                >= required_similarity_score
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
                                                send_message(
                                                    f"\n\t\t\tDuplicate release found in: {upgrade_file.root}"
                                                    f"\n\t\t\tDuplicate: {duplicate_file.name} has a lower score than {upgrade_file.name}"
                                                    f"\n\n\t\t\tDeleting: {duplicate_file.name} inside of {duplicate_file.root}\n",
                                                    discord=False,
                                                )
                                                embed = handle_fields(
                                                    DiscordEmbed(
                                                        title="Duplicate Download Release (Not Upgradeable)",
                                                        color=yellow_color,
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
                                                grouped_notifications = (
                                                    group_notification(
                                                        grouped_notifications,
                                                        Embed(embed, None),
                                                    )
                                                )
                                                user_input = (
                                                    get_input_from_user(
                                                        f'\t\t\tDelete "{duplicate_file.name}"',
                                                        ["y", "n"],
                                                        ["y", "n"],
                                                    )
                                                    if manual_delete
                                                    else "y"
                                                )

                                                if user_input == "y":
                                                    remove_file(
                                                        duplicate_file.path,
                                                    )
                                                else:
                                                    print("\t\t\t\tSkipping...\n")
                                            else:
                                                file_hash = get_file_hash(file.path)
                                                compare_hash = get_file_hash(
                                                    compare_file.path
                                                )
                                                # Check if the file hashes are the same
                                                # instead of defaulting to requiring the user to decide.
                                                if (compare_hash and file_hash) and (
                                                    compare_hash == file_hash
                                                ):
                                                    embed = handle_fields(
                                                        DiscordEmbed(
                                                            title="Duplicate Download Release (HASH MATCH)",
                                                            color=yellow_color,
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
                                                    grouped_notifications = (
                                                        group_notification(
                                                            grouped_notifications,
                                                            Embed(embed, None),
                                                        )
                                                    )
                                                    # Delete the compare file
                                                    remove_file(
                                                        compare_file.path,
                                                    )
                                                else:
                                                    send_message(
                                                        f"\n\t\t\tDuplicate found in: {compare_file.root}"
                                                        f"\n\t\t\t\t{file.name}"
                                                        f"\n\t\t\t\t{compare_file.name}"
                                                        f"\n\t\t\t\t\tRanking scores are equal, REQUIRES MANUAL DECISION.",
                                                        discord=False,
                                                    )
                                                    embed = handle_fields(
                                                        DiscordEmbed(
                                                            title="Duplicate Download Release (REQUIRES MANUAL DECISION)",
                                                            color=yellow_color,
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
                                                    grouped_notifications = (
                                                        group_notification(
                                                            grouped_notifications,
                                                            Embed(embed, None),
                                                        )
                                                    )
                                                    print("\t\t\t\t\tSkipping...")
                                except Exception as e:
                                    send_message(
                                        f"\n\t\t\tError: {e}\n\t\t\tSkipping: {compare_file.name}",
                                        error=True,
                                    )
                                    continue
                    except Exception as e:
                        send_message(
                            f"\n\t\tError: {e}\n\t\tSkipping: {file.name}",
                            error=True,
                        )
                        continue
    except Exception as e:
        send_message(f"\n\t\tError: {e}", error=True)