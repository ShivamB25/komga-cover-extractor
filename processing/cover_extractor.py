import os
import zipfile
import scandir

from processing.text_processor import clean_str
from utils.helpers import (
    get_highest_release,
    send_message,
    get_file_extension,
    get_modification_date,
)
from config.constants import (
    image_extensions,
    moved_folders,
    output_covers_as_webp,
    paths,
    root_modification_times,
    use_latest_volume_cover_as_series_cover,
    extract_chapter_covers,
    copy_existing_volume_covers_toggle,
    paths_with_types,
    novel_extensions,
    compiled_cover_patterns,
    blank_white_image_path,
    blank_black_image_path,
    compare_detected_cover_to_blank_images,
    blank_cover_required_similarity_score,
    compress_image_option,
    image_quality,
    watchdog_toggle,
    transferred_files,
    transferred_dirs,
)
from filesystem.file_operations import remove_file
from processing.cover_extractor import process_cover_extraction
from processing.image_processor import (
    compress_image,
    prep_images_for_similarity,
)
from processing.metadata_extractor import get_novel_cover
from filesystem.folder_manager import create_folder_obj, process_files_and_folders
from models.file_models import upgrade_to_file_class, upgrade_to_volume_class


# Extracts the covers out from our manga and novel files.
def extract_covers(paths_to_process=paths):
    global checked_series, root_modification_times
    global series_cover_path

    # Finds the series cover image in the given folder
    def find_series_cover(folder_accessor, image_extensions):
        result = next(
            (
                os.path.join(folder_accessor.root, f"cover{ext}")
                for ext in image_extensions
                if os.path.exists(os.path.join(folder_accessor.root, f"cover{ext}"))
            ),
            None,
        )
        return result

    # Checks if the folder contains files with the same series name
    def check_same_series_name(files, required_percent=0.9):
        result = False

        if files:
            compare_series = clean_str(files.series_name, skip_bracket=True)
            file_count = len(files)
            required_count = int(file_count * required_percent)
            result = (
                sum(
                    clean_str(x.series_name, skip_bracket=True) == compare_series
                    for x in files
                )
                >= required_count
            )
        return result

    # Processes the volume paths based on the given parameters
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
            # Set the value of volume_paths
            if not volume_paths and paths_with_types:
                volume_paths = [
                    x
                    for x in paths_with_types
                    if "volume" in x.path_formats
                    and files.extension in x.path_extensions
                ]
                for v_path in volume_paths:
                    # Get all the folders in v_path.path
                    volume_series_folders = [
                        x for x in os.listdir(v_path.path) if not x.startswith(".")
                    ]
                    v_path.series_folders = volume_series_folders

            base_name = clean_str(os.path.basename(root))

        return base_name, volume_paths

    # Checks if the folder contains multiple volume ones
    def contains_multiple_volume_ones(
        files, use_latest_volume_cover_as_series_cover, is_chapter_directory
    ):
        result = False

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
            result = volume_ones > 1
        return result

    if not paths_to_process:
        print("\nNo paths to process.")
        return

    print("\nLooking for covers to extract...")

    # Only volume defined paths in the paths_with_types list
    # Used for copying existing volume covers from a
    # volume library to a chapter library
    volume_paths = []

    # contains cleaned basenames of folders that have been moved
    moved_folder_names = (
        [
            clean_str(
                os.path.basename(x),
                skip_bracket=True,
                skip_underscore=True,
            )
            for x in moved_folders
        ]
        if moved_folders and copy_existing_volume_covers_toggle
        else []
    )

    # Iterate over each path
    for path in paths_to_process:
        if not os.path.exists(path):
            print(f"\nERROR: {path} is an invalid path.\n")
            continue

        checked_series = []
        os.chdir(path)

        # Traverse the directory tree rooted at the path
        for root, dirs, files in scandir.walk(path):
            if watchdog_toggle:
                if not moved_folder_names or (
                    clean_str(
                        os.path.basename(root),
                        skip_bracket=True,
                        skip_underscore=True,
                    )
                    not in moved_folder_names
                ):
                    root_mod_time = get_modification_date(root)
                    if root in root_modification_times:
                        # Modification time hasn't changed; continue to the next iteration
                        if root_modification_times[root] == root_mod_time:
                            continue
                        else:
                            # update the modification time for the root
                            root_modification_times[root] = root_mod_time
                    else:
                        # Store the modification time for the root
                        root_modification_times[root] = root_mod_time

            files, dirs = process_files_and_folders(
                root,
                files,
                dirs,
                just_these_files=transferred_files,
                just_these_dirs=transferred_dirs,
            )

            contains_subfolders = dirs

            global folder_accessor

            print(f"\nRoot: {root}")
            print(f"Files: {files}")

            if not files:
                continue

            # Upgrade files to file classes
            file_objects = upgrade_to_file_class(files, root)

            # Upgrade file objects to a volume classes
            volume_objects = upgrade_to_volume_class(
                file_objects,
                skip_release_year=True,
                skip_release_group=True,
                skip_extras=True,
                skip_publisher=True,
                skip_premium_content=True,
                skip_subtitle=True,
                skip_multi_volume=True,
            )

            # Create a folder accessor object
            folder_accessor = create_folder_obj(root, dirs, volume_objects)

            # Get the series cover
            series_cover_path = find_series_cover(folder_accessor, image_extensions)
            series_cover_extension = (
                get_file_extension(series_cover_path) if series_cover_path else ""
            )

            if series_cover_extension and (
                (output_covers_as_webp and series_cover_extension != ".webp")
                or (not output_covers_as_webp and series_cover_extension == ".webp")
            ):
                # Remove the existing series cover image
                remove_status = remove_file(series_cover_path, silent=True)
                if remove_status:
                    series_cover_path = ""

            # Set the directory type
            is_chapter_directory = folder_accessor.files.file_type == "chapter"

            # Check if all the series_name values are the same for all volumes
            same_series_name = check_same_series_name(folder_accessor.files)

            # Used when filtering the series_folders of each paths_with_types
            # by the first letter of a cleaned up name
            clean_basename, volume_paths = process_volume_paths(
                folder_accessor.files,
                folder_accessor.root,
                copy_existing_volume_covers_toggle,
                is_chapter_directory,
                volume_paths,
                paths_with_types,
            )

            # Get the highest volume number and part number
            highest_index_number = (
                get_highest_release(
                    tuple(
                        [
                            (
                                item.index_number
                                if not isinstance(item.index_number, list)
                                else tuple(item.index_number)
                            )
                            for item in folder_accessor.files
                        ]
                    ),
                    is_chapter_directory=is_chapter_directory,
                )
                if not is_chapter_directory
                else ""
            )

            if highest_index_number:
                print(f"\n\t\tHighest Index Number: {highest_index_number}")

            # Check if it contains multiple volume ones
            has_multiple_volume_ones = contains_multiple_volume_ones(
                folder_accessor.files,
                use_latest_volume_cover_as_series_cover,
                is_chapter_directory,
            )

            # Process cover extraction for each file
            [
                process_cover_extraction(
                    file,
                    has_multiple_volume_ones,
                    highest_index_number,
                    is_chapter_directory,
                    volume_paths,
                    clean_basename,
                    same_series_name,
                    contains_subfolders,
                )
                for file in folder_accessor.files
                if file.file_type == "volume"
                or (file.file_type == "chapter" and extract_chapter_covers)
            ]


# Returns the path of the cover image for a novel file, if it exists.
def get_novel_cover_path(file):
    if file.extension not in novel_extensions:
        return ""

    novel_cover_path = get_novel_cover(file.path)
    if not novel_cover_path:
        return ""

    if get_file_extension(novel_cover_path) not in image_extensions:
        return ""

    return os.path.basename(novel_cover_path)


# Finds and extracts the internal cover from a manga or novel file.
def find_and_extract_cover(
    file,
    return_data_only=False,
    silent=False,
    blank_image_check=compare_detected_cover_to_blank_images,
):
    # Helper function to filter and sort files in the zip archive
    def filter_and_sort_files(zip_list):
        return sorted(
            [
                x
                for x in zip_list
                if not x.endswith("/")
                and "." in x
                and get_file_extension(x) in image_extensions
                and not os.path.basename(x).startswith((".", "__"))
            ]
        )

    # Helper function to read image data from the zip file
    def get_image_data(image_path):
        with zip_ref.open(image_path) as image_file_ref:
            return image_file_ref.read()

    # Helper function to save image data to a file
    def save_image_data(image_path, image_data):
        with open(image_path, "wb") as image_file_ref_out:
            image_file_ref_out.write(image_data)

    # Helper function to process a cover image and save or return the data
    def process_cover_image(cover_path, image_data=None):
        image_extension = get_file_extension(os.path.basename(cover_path))
        if image_extension == ".jpeg":
            image_extension = ".jpg"

        if output_covers_as_webp and image_extension != ".webp":
            image_extension = ".webp"

        output_path = os.path.join(file.root, file.extensionless_name + image_extension)

        if not return_data_only:
            save_image_data(output_path, image_data)
            if compress_image_option:
                result = compress_image(output_path, image_quality)
                return result if result else output_path
            return output_path
        elif image_data:
            compressed_data = compress_image(output_path, raw_data=image_data)
            return compressed_data if compressed_data else image_data
        return None

    # Helper function to check if an image is blank
    def is_blank_image(image_data):
        ssim_score_white = prep_images_for_similarity(
            blank_white_image_path, image_data, silent=silent
        )
        ssim_score_black = prep_images_for_similarity(
            blank_black_image_path, image_data, silent=silent
        )

        return (
            ssim_score_white is not None
            and ssim_score_black is not None
            and (
                ssim_score_white >= blank_cover_required_similarity_score
                or ssim_score_black >= blank_cover_required_similarity_score
            )
        )

    # Check if the file exists
    if not os.path.isfile(file.path):
        send_message(f"\nFile: {file.path} does not exist.", error=True)
        return None

    # Check if the input file is a valid zip file
    if not zipfile.is_zipfile(file.path):
        send_message(f"\nFile: {file.path} is not a valid zip file.", error=True)
        return None

    # Get the novel cover path if the file has a novel extension
    novel_cover_path = (
        get_novel_cover_path(file) if file.extension in novel_extensions else ""
    )

    # Open the zip file
    with zipfile.ZipFile(file.path, "r") as zip_ref:
        # Filter and sort files in the zip archive
        zip_list = filter_and_sort_files(zip_ref.namelist())

        # Move the novel cover to the front of the list, if it exists
        if novel_cover_path:
            novel_cover_basename = os.path.basename(novel_cover_path)
            for i, item in enumerate(zip_list):
                if os.path.basename(item) == novel_cover_basename:
                    zip_list.pop(i)
                    zip_list.insert(0, item)
                    break

        # Set of blank images
        blank_images = set()

        # Iterate through the files in the zip archive
        for pattern in compiled_cover_patterns:
            # Check if the file matches any cover pattern
            for image_file in zip_list:
                image_basename = os.path.basename(image_file)
                is_novel_cover = novel_cover_path and image_basename == novel_cover_path

                if (
                    is_novel_cover
                    or pattern.pattern == image_basename
                    or pattern.search(image_basename)
                ):
                    # Check if the image is blank
                    if (
                        blank_image_check
                        and blank_white_image_path
                        and blank_black_image_path
                    ):
                        image_data = get_image_data(image_file)
                        if is_blank_image(image_data):
                            blank_images.add(image_file)
                            break
                    image_data = get_image_data(image_file)
                    result = process_cover_image(image_file, image_data)
                    if result:
                        return result

        # Find a non-blank default cover
        default_cover_path = None
        for test_file in zip_list:
            if test_file in blank_images:
                continue

            image_data = get_image_data(test_file)

            # Check if the user has enabled the option to compare detected covers to blank images
            if blank_image_check:
                if not is_blank_image(image_data):
                    default_cover_path = test_file
                    break
            else:
                default_cover_path = test_file
                break

        # Process the default cover if found
        if default_cover_path:
            image_data = get_image_data(default_cover_path)
            result = process_cover_image(default_cover_path, image_data)
            if result:
                return result

    return False