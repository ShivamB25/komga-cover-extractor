# komga_cover_extractor/image_utils.py
import os
import io
import re
import zipfile
import cv2
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as ssim

# TODO: Ensure these are correctly imported from config module later
from .config import (
    image_extensions,
    novel_extensions,
    output_covers_as_webp,
    compress_image_option,
    image_quality,
    blank_white_image_path,
    blank_black_image_path,
    blank_cover_required_similarity_score,
    compare_detected_cover_to_blank_images,  # Assuming this is a boolean flag in config
)

# TODO: Import necessary functions from other utils
from .log_utils import send_message
from .file_utils import get_file_extension, get_extensionless_name, remove_file

# Import models used in this module
try:
    from .models import (
        File,
    )  # Expected type for 'file' parameter in find_and_extract_cover
except ImportError:
    print("WARN: Could not import File model in image_utils.py")

    class File:
        pass  # Placeholder


# Import needed function from metadata_utils
try:
    from .metadata_utils import (
        get_novel_cover_path,
    )  # Needed for find_and_extract_cover
except ImportError:
    print("WARN: Could not import get_novel_cover_path from metadata_utils.py")

    # Define placeholder if import fails
    def get_novel_cover_path(file_obj):
        print("WARN: Using placeholder get_novel_cover_path")
        return ""


# Compresses an image and saves it to a file or returns the compressed image data.
def compress_image(
    image_path, quality=image_quality, to_jpg=False, raw_data=None
):  # Use imported config value
    """Compresses an image using PIL."""
    new_filename = None
    buffer = None
    save_format = "JPEG"  # Default, change if webp output is desired

    try:
        # Load the image from the file or raw data
        image = Image.open(image_path if not raw_data else io.BytesIO(raw_data))

        # Convert the image to RGB if it has an alpha channel or uses a palette
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")

        filename, ext = os.path.splitext(image_path)

        # Determine output format
        if output_covers_as_webp:  # Use imported config value
            save_format = "WEBP"
            ext = ".webp"
        elif ext.lower() == ".webp":  # If input is webp but output is not, force JPEG
            save_format = "JPEG"
            ext = ".jpg"
        elif to_jpg or ext.lower() == ".png":  # Convert PNGs or if explicitly requested
            save_format = "JPEG"
            ext = ".jpg"
            if not to_jpg:
                to_jpg = True  # Flag for potential original PNG removal

        # Determine the new filename for the compressed image if not working with raw data
        if not raw_data:
            new_filename = f"{filename}{ext}"

        # Try to compress and save the image
        if not raw_data:
            image.save(new_filename, format=save_format, quality=quality, optimize=True)
        else:
            buffer = io.BytesIO()
            image.save(
                buffer, format=save_format, quality=quality, optimize=True
            )  # Added optimize=True
            return buffer.getvalue()

    except Exception as e:
        send_message(
            f"Failed to compress image {image_path}: {e}", error=True
        )  # Use imported log_utils function
        return (
            None if raw_data else image_path
        )  # Return original path or None on failure

    # Remove the original file if it was a PNG converted to JPG
    if (
        to_jpg
        and ext.lower() == ".jpg"
        and image_path.lower().endswith(".png")
        and os.path.isfile(image_path)
    ):
        remove_file(image_path, silent=True)  # Use imported file_utils function

    return new_filename if not raw_data else buffer.getvalue()


# Function to determine if an image is black and white
def is_image_black_and_white(image, tolerance=15):
    """Determines if a PIL image is mostly grayscale and near black/white."""
    try:
        image_rgb = image.convert("RGB")
        pixels = list(image_rgb.getdata())
        if not pixels:
            return False  # Handle empty image

        grayscale_count = 0
        threshold = 0.9  # 90% of pixels must be grayscale

        for r, g, b in pixels:
            # Check if the pixel is grayscale within the tolerance
            if abs(r - g) <= tolerance and abs(g - b) <= tolerance:
                grayscale_count += 1

        # If enough pixels are grayscale, return True
        return (grayscale_count / len(pixels)) >= threshold
    except Exception as e:
        send_message(
            f"Error checking if image is black and white: {e}", error=True
        )  # Use imported log_utils function
        return False


# Function to check if the first image in a zip file is black and white
def is_first_image_black_and_white(zip_path):
    """Checks if the first image file within a zip archive is black and white."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            # Filter for image files and sort
            image_files = sorted(
                [
                    f
                    for f in zip_file.namelist()
                    if not f.endswith("/")
                    and get_file_extension(f)
                    in image_extensions  # Use imported config/file_utils
                ]
            )
            if not image_files:
                return False  # No images in archive

            first_image_name = image_files[0]
            with zip_file.open(first_image_name) as image_file:
                image = Image.open(io.BytesIO(image_file.read()))
                return is_image_black_and_white(image)  # Use local function
    except zipfile.BadZipFile:
        send_message(
            f"Bad zip file, cannot check first image: {zip_path}", error=True
        )  # Use imported log_utils function
        return False  # Treat as not black and white if unreadable
    except Exception as e:
        send_message(
            f"Error processing zip file {zip_path}: {e}", error=True
        )  # Use imported log_utils function
        return False


# Return the number of image files in the archive.
def count_images_in_cbz(file_path):
    """Counts the number of image files within a zip/cbz archive."""
    try:
        with zipfile.ZipFile(file_path, "r") as archive:
            images = [
                f
                for f in archive.namelist()
                if get_file_extension(f).lower()
                in image_extensions  # Use imported config/file_utils
            ]
            return len(images)
    except zipfile.BadZipFile:
        send_message(
            f"Skipping corrupted file for image count: {file_path}", error=True
        )  # Use imported log_utils function
        return 0
    except Exception as e:
        send_message(
            f"Error counting images in {file_path}: {e}", error=True
        )  # Use imported log_utils function
        return 0


# Regular expressions to match cover patterns
cover_patterns = [
    r"(cover\.jpe?g|cover\.png|cover\.webp)$",  # Exact cover filenames
    r"(\b(Cover([0-9]+|)|CoverDesign|page[-_. ]*cover)\b)",  # Keywords
    r"(\b(p|page_)?000\.(jpe?g|png|webp))\b",  # Common first page names
    r"((\s+)0+\.(jpe?g|png|webp))",  # Leading zeros like ' 00.jpg'
    r"(\bindex[-_. ]1[-_. ]1\b)",  # Specific pattern found in some files
    r"(9[-_. :]*7[-_. :]*(8|9)(?:[-_. :]*[0-9]){10})",  # ISBN-like patterns
]
compiled_cover_patterns = [
    re.compile(pattern, flags=re.IGNORECASE) for pattern in cover_patterns
]


# Finds and extracts the internal cover from a manga or novel file.
def find_and_extract_cover(
    file,  # Expects a File object from models.py
    return_data_only=False,
    silent=False,
    blank_image_check=compare_detected_cover_to_blank_images,  # Use imported config value
):
    """Finds and extracts the cover image from an archive file."""
    global image_count  # TODO: Refactor global state, maybe return status?

    # Helper function to filter and sort files in the zip archive
    def filter_and_sort_files(zip_list):
        return sorted(
            [
                x
                for x in zip_list
                if not x.endswith("/")
                and "." in x
                and get_file_extension(x).lower()
                in image_extensions  # Use imported config/file_utils
                and not os.path.basename(x).startswith((".", "__"))
            ]
        )

    # Helper function to read image data from the zip file
    def get_image_data(zip_ref, image_path):
        with zip_ref.open(image_path) as image_file_ref:
            return image_file_ref.read()

    # Helper function to save image data to a file
    def save_image_data(image_path, image_data):
        with open(image_path, "wb") as image_file_ref_out:
            image_file_ref_out.write(image_data)

    # Helper function to process a cover image and save or return the data
    def process_cover_image(cover_path, image_data=None):
        if not image_data:
            return None  # Cannot process without data

        image_extension = get_file_extension(
            os.path.basename(cover_path)
        ).lower()  # Use imported file_utils function
        if image_extension == ".jpeg":
            image_extension = ".jpg"

        output_ext = (
            ".webp" if output_covers_as_webp else ".jpg"
        )  # Use imported config value
        # Ensure output path uses the desired extension
        output_path = f"{file.extensionless_path}{output_ext}"

        # Compress/convert if needed, or just return raw data
        if (
            compress_image_option or output_ext != image_extension
        ):  # Use imported config value
            # Pass output_path to compress_image for correct extension handling if saving
            compressed_data = compress_image(
                output_path, raw_data=image_data
            )  # Use local function
            if return_data_only:
                return (
                    compressed_data if compressed_data else image_data
                )  # Return compressed or original data
            else:
                # compress_image already saved if raw_data was False, but we passed True
                # We need to save the potentially compressed/converted data
                final_data_to_save = compressed_data if compressed_data else image_data
                save_image_data(output_path, final_data_to_save)
                return output_path  # Return path to the saved file
        else:  # No compression or conversion needed
            if return_data_only:
                return image_data
            else:
                save_image_data(output_path, image_data)
                return output_path

    # Helper function to check if an image is blank using SSIM
    def is_blank_image(img_data):
        if not blank_image_check:
            return False  # Skip check if disabled
        if not blank_white_image_path and not blank_black_image_path:
            return False  # Skip if no blank refs

        is_blank = False
        if blank_white_image_path:
            score_white = prep_images_for_similarity(
                blank_white_image_path, img_data, silent=True
            )  # Use local function
            if (
                score_white is not None
                and score_white >= blank_cover_required_similarity_score
            ):  # Use imported config value
                is_blank = True
        if (
            not is_blank and blank_black_image_path
        ):  # Only check black if white didn't match
            score_black = prep_images_for_similarity(
                blank_black_image_path, img_data, silent=True
            )  # Use local function
            if (
                score_black is not None
                and score_black >= blank_cover_required_similarity_score
            ):  # Use imported config value
                is_blank = True

        if is_blank and not silent:
            print("\t\t\tDetected blank image, skipping.")
        return is_blank

    # --- Main function logic ---
    if not os.path.isfile(file.path):
        if not silent:
            send_message(
                f"File not found: {file.path}", error=True
            )  # Use imported log_utils function
        return None

    if not zipfile.is_zipfile(file.path):
        if not silent:
            send_message(
                f"Not a valid zip file: {file.path}", error=True
            )  # Use imported log_utils function
        return None

    # Get novel cover path using the imported function
    novel_cover_rel_path = get_novel_cover_path(file)

    try:
        with zipfile.ZipFile(file.path, "r") as zip_ref:
            zip_list = filter_and_sort_files(zip_ref.namelist())
            if not zip_list:
                if not silent:
                    print("\t\tNo image files found in archive.")
                return None

            # Prioritize novel cover if found
            prioritized_list = zip_list
            if novel_cover_rel_path:
                novel_cover_full_path = next(
                    (
                        item
                        for item in zip_list
                        if os.path.basename(item)
                        == os.path.basename(novel_cover_rel_path)
                    ),
                    None,
                )
                if novel_cover_full_path:
                    prioritized_list = [novel_cover_full_path] + [
                        item for item in zip_list if item != novel_cover_full_path
                    ]

            # Try pattern matching first
            for image_file in prioritized_list:
                image_basename = os.path.basename(image_file)
                is_novel_cover = (
                    novel_cover_rel_path
                    and image_basename == os.path.basename(novel_cover_rel_path)
                )

                match_found = is_novel_cover or any(
                    pattern.search(image_basename)
                    for pattern in compiled_cover_patterns
                )

                if match_found:
                    image_data = get_image_data(zip_ref, image_file)
                    if not is_blank_image(image_data):
                        result = process_cover_image(image_file, image_data)
                        if result:
                            if not return_data_only and not silent:
                                print(
                                    f"\t\tCover extracted: {os.path.basename(result)}"
                                )
                            # image_count += 1 # TODO: Handle stats counting elsewhere
                            return result
                    # If it was blank or processing failed, continue searching

            # Fallback: Use the first non-blank image if no pattern matched
            if not silent:
                print("\t\tNo pattern matched, using first valid image as cover.")
            for image_file in prioritized_list:  # Use prioritized list for fallback too
                image_data = get_image_data(zip_ref, image_file)
                if not is_blank_image(image_data):
                    result = process_cover_image(image_file, image_data)
                    if result:
                        if not return_data_only and not silent:
                            print(
                                f"\t\tCover extracted (fallback): {os.path.basename(result)}"
                            )
                        # image_count += 1 # TODO: Handle stats counting elsewhere
                        return result
                    else:  # If processing the first valid image fails, report error
                        if not silent:
                            send_message(
                                f"Failed to process fallback cover: {image_file}",
                                error=True,
                            )  # Use imported log_utils function
                        return None  # Indicate failure

            # If all images were blank or processing failed
            if not silent:
                print(
                    "\t\tNo suitable cover found (all images might be blank or processing failed)."
                )
            return None

    except zipfile.BadZipFile:
        if not silent:
            send_message(
                f"Bad zip file: {file.path}", error=True
            )  # Use imported log_utils function
        return None
    except Exception as e:
        if not silent:
            send_message(
                f"Error extracting cover from {file.path}: {e}", error=True
            )  # Use imported log_utils function
        return None


# Converts a .webp file to .jpg
def convert_webp_to_jpg(webp_file_path):
    """Converts a WEBP image file to JPG, removing the original."""
    if not webp_file_path or not webp_file_path.lower().endswith(".webp"):
        return None
    if not os.path.isfile(webp_file_path):
        return None

    extensionless_webp_file = get_extensionless_name(
        webp_file_path
    )  # Use imported file_utils function
    jpg_file_path = f"{extensionless_webp_file}.jpg"

    try:
        with Image.open(webp_file_path) as im:
            # Ensure conversion to RGB before saving as JPG
            im.convert("RGB").save(
                jpg_file_path, "JPEG", quality=95
            )  # Save with high quality

        if os.path.isfile(jpg_file_path):
            remove_file(webp_file_path, silent=True)  # Use imported file_utils function
            return jpg_file_path
        else:
            send_message(
                f"ERROR: Failed to verify JPG creation for {webp_file_path}", error=True
            )  # Use imported log_utils function
            return None
    except Exception as e:
        send_message(
            f"Could not convert {webp_file_path} to jpg: {e}", error=True
        )  # Use imported log_utils function
        # Clean up partially created JPG if conversion failed
        if os.path.isfile(jpg_file_path):
            remove_file(jpg_file_path, silent=True)  # Use imported file_utils function
        return None


# --- Image Similarity Helpers ---


def preprocess_image(image):
    """Converts image to grayscale, applies histogram equalization, and normalizes."""
    try:
        # Ensure image is NumPy array
        if isinstance(image, bytes):
            nparr = np.frombuffer(image, np.uint8)
            img_np = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img_np is None:
                raise ValueError("cv2.imdecode returned None")
            image = img_np
        elif not isinstance(image, np.ndarray):
            raise TypeError("Input must be a NumPy array or bytes")

        if len(image.shape) == 3 and image.shape[2] == 3:  # Color image
            gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        elif len(image.shape) == 3 and image.shape[2] == 4:  # Color with Alpha
            gray_image = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        elif len(image.shape) == 2:  # Already grayscale
            gray_image = image
        else:
            raise ValueError(f"Unsupported image shape: {image.shape}")

        # Apply histogram equalization
        gray_image = cv2.equalizeHist(gray_image)
        # Normalize the image to range 0.0 - 1.0
        gray_image = gray_image / 255.0
        return gray_image
    except Exception as e:
        send_message(
            f"Error preprocessing image: {e}", error=True
        )  # Use imported log_utils function
        return None


def compare_images(imageA, imageB, silent=False):
    """Compares two preprocessed grayscale images using SSIM."""
    try:
        # SSIM expects images in range [0, 1] or [0, 255], ensure consistency
        # preprocess_image already normalizes to [0, 1]
        ssim_score = ssim(imageA, imageB, data_range=1.0)
        if not silent:
            print(f"\t\t\t\tSSIM: {ssim_score:.4f}")
        return ssim_score
    except ValueError as ve:  # Handle potential size mismatch errors from ssim
        if "input images must have the same dimensions" in str(ve):
            if not silent:
                print("\t\t\tImage dimension mismatch for SSIM.")
        else:
            send_message(
                f"SSIM calculation error: {ve}", error=True
            )  # Use imported log_utils function
        return 0.0  # Return 0 similarity on error
    except Exception as e:
        send_message(
            f"Error comparing images: {e}", error=True
        )  # Use imported log_utils function
        return 0.0  # Return 0 similarity on error


def prep_images_for_similarity(
    image1_path_or_data, image2_data, both_cover_data=False, silent=False
):
    """Loads, resizes, preprocesses, and compares two images for similarity."""

    def resize_image(img, desired_width=400, desired_height=600):
        """Resizes a single image."""
        return cv2.resize(
            img, (desired_width, desired_height), interpolation=cv2.INTER_AREA
        )

    try:
        # Load/Decode Image 1
        if both_cover_data:  # image1 is data
            img1_np = np.frombuffer(image1_path_or_data, np.uint8)
            img1 = cv2.imdecode(img1_np, cv2.IMREAD_COLOR)
        else:  # image1 is path
            if not os.path.isfile(image1_path_or_data):
                if not silent:
                    print(f"\t\t\tReference image not found: {image1_path_or_data}")
                return None
            img1 = cv2.imread(image1_path_or_data)

        # Decode Image 2 (always data)
        img2_np = np.frombuffer(image2_data, np.uint8)
        img2 = cv2.imdecode(img2_np, cv2.IMREAD_COLOR)

        if img1 is None or img2 is None:
            if not silent:
                print("\t\t\tFailed to load one or both images for comparison.")
            return None

        # Resize
        img1_resized = resize_image(img1)
        img2_resized = resize_image(img2)

        # Preprocess (Grayscale, Equalize, Normalize)
        gray1 = preprocess_image(img1_resized)  # Use local function
        gray2 = preprocess_image(img2_resized)

        if gray1 is None or gray2 is None:
            return None  # Preprocessing failed

        # Compare
        score = compare_images(gray1, gray2, silent=silent)  # Use local function
        return score

    except Exception as e:
        send_message(
            f"Error preparing images for similarity: {e}", error=True
        )  # Use imported log_utils function
        return None
