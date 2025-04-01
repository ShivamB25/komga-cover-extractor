import io
import os
import cv2
import numpy as np
import zipfile
import re # Added for cover_patterns

from PIL import Image
from skimage.metrics import structural_similarity as ssim

# Assuming settings and other necessary modules are imported correctly
from settings import (
    image_quality, blank_white_image_path, blank_black_image_path,
    blank_cover_required_similarity_score, output_covers_as_webp,
    image_extensions, novel_extensions, # Added
    compare_detected_cover_to_blank_images, # Added
    compress_image_option # Added
)
from core.file_utils import get_file_extension, get_extensionless_name, remove_file # Assuming remove_file is here
from core.metadata_utils import get_novel_cover # Assuming get_novel_cover is here

# Regular expressions to match cover patterns
cover_patterns = [
    r"(cover\.([A-Za-z]+))$",
    r"(\b(Cover([0-9]+|)|CoverDesign|page([-_. ]+)?cover)\b)",
    r"(\b(p000|page_000)\b)",
    r"((\s+)0+\.(.{2,}))",
    r"(\bindex[-_. ]1[-_. ]1\b)",
    r"(9([-_. :]+)?7([-_. :]+)?(8|9)(([-_. :]+)?[0-9]){10})", # ISBN pattern
]

# Pre-compiled regular expressions for cover patterns
compiled_cover_patterns = [
    re.compile(pattern, flags=re.IGNORECASE) for pattern in cover_patterns
]


def compress_image(image_path, quality=image_quality, to_jpg=False, raw_data=None): # Use setting default
    """Compresses an image and saves it or returns the data."""
    new_filename = None
    buffer = None
    # Determine save format based on output_covers_as_webp setting
    save_format = "WEBP" if output_covers_as_webp else "JPEG"
    output_ext = ".webp" if output_covers_as_webp else ".jpg"

    try:
        image = Image.open(image_path if not raw_data else io.BytesIO(raw_data))

        # Convert to RGB if necessary (WEBP supports transparency, JPEG doesn't)
        if save_format == "JPEG" and image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        # Optional: Handle transparency for WEBP if needed, otherwise convert
        elif save_format == "WEBP" and image.mode == "P":
             image = image.convert("RGBA") # Convert palette to RGBA for WEBP
        elif save_format == "WEBP" and image.mode == "RGB":
             pass # Keep as RGB if no transparency needed

        filename, input_ext = os.path.splitext(image_path)

        if not raw_data:
            # Always use the desired output extension
            new_filename = f"{filename}{output_ext}"

        # Try to compress and save/return the image
        if not raw_data:
            # Ensure the target directory exists (needed if path is just a name)
            os.makedirs(os.path.dirname(new_filename), exist_ok=True)
            image.save(new_filename, format=save_format, quality=quality, optimize=True)
            # Remove original only if format changed and saving succeeded
            if input_ext.lower() != output_ext.lower() and os.path.isfile(image_path) and os.path.isfile(new_filename):
                 remove_file(image_path, silent=True) # Use remove_file utility
        else:
            buffer = io.BytesIO()
            image.save(buffer, format=save_format, quality=quality)
            return buffer.getvalue()

    except Exception as e:
        print(f"Failed to compress image {image_path}: {e}") # Basic logging
        return None # Indicate failure

    return new_filename if not raw_data else None # Return None if raw_data but failed

def is_image_black_and_white(image, tolerance=15):
    """Determines if a PIL image is black and white or grayscale."""
    try:
        # Convert the image to RGB (ensures consistent handling of image modes)
        image_rgb = image.convert("RGB")

        # Extract pixel data
        pixels = list(image_rgb.getdata())
        if not pixels: return False # Handle empty image

        # Count pixels that are grayscale
        grayscale_count = 0
        for r, g, b in pixels:
            # Check if the pixel is grayscale within the tolerance
            if abs(r - g) <= tolerance and abs(g - b) <= tolerance:
                grayscale_count += 1

        # If a high percentage of pixels are grayscale, consider it B&W/Grayscale
        # Adjust threshold as needed (e.g., 0.95 for stricter check)
        return (grayscale_count / len(pixels)) > 0.9
    except Exception as e:
        print(f"Error checking if image is black and white: {e}") # Basic logging
        return False

def preprocess_image(image):
    """Preprocesses an image (loaded with OpenCV) for SSIM comparison."""
    # Check if the image is already grayscale
    if len(image.shape) == 2 or (len(image.shape) == 3 and image.shape[2] == 1):
        gray_image = image
    else:
        # Convert to grayscale if it's a color image
        gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Apply histogram equalization to improve contrast
    gray_image = cv2.equalizeHist(gray_image)

    # Normalize the image pixel values to the range [0, 1]
    gray_image = gray_image / 255.0

    return gray_image

def compare_images(imageA, imageB, silent=False):
    """Compares two images (loaded with OpenCV) using SSIM."""
    try:
        if imageA is None or imageB is None:
             print("Error: One or both images are None for comparison.")
             return 0.0
        if imageA.shape != imageB.shape:
             print(f"Warning: Image shapes do not match for comparison: {imageA.shape} vs {imageB.shape}. Resizing needed.")
             # Handle resizing or return 0.0 if comparison isn't meaningful
             return 0.0 # Or implement resizing logic here

        if not silent:
            print(f"\t\t\tComparing Image Sizes: {imageA.shape} vs {imageB.shape}")

        # Preprocess images (already done in prep_images_for_similarity)
        # grayA = preprocess_image(imageA)
        # grayB = preprocess_image(imageB)
        grayA = imageA # Assume preprocessed
        grayB = imageB # Assume preprocessed

        # Compute SSIM between the two images
        # Ensure data_range matches the normalization (0-1)
        ssim_score, _ = ssim(grayA, grayB, full=True, data_range=1.0)

        if not silent:
            print(f"\t\t\t\tSSIM: {ssim_score}")

        return ssim_score
    except ValueError as ve:
         # Catch specific SSIM errors like win_size > image size
         print(f"SSIM calculation error: {ve}. Image dimensions might be too small.")
         return 0.0
    except Exception as e:
        print(f"Error comparing images: {e}") # Basic logging
        return 0.0

def prep_images_for_similarity(
    image_path_or_data1, image_path_or_data2, both_cover_data=False, silent=False
):
    """Prepares two images (from path or data) and compares them using SSIM."""

    def resize_images(img1, img2, desired_width=400, desired_height=600):
        # Resize images using OpenCV
        img1_resized = cv2.resize(
            img1, (desired_width, desired_height), interpolation=cv2.INTER_AREA
        )
        img2_resized = cv2.resize(
            img2, (desired_width, desired_height), interpolation=cv2.INTER_AREA
        )
        return img1_resized, img2_resized

    def load_image(path_or_data, is_data):
        # Loads image using OpenCV, handling path or data buffer
        if is_data:
            img = cv2.imdecode(
                np.frombuffer(path_or_data, np.uint8), cv2.IMREAD_UNCHANGED
            )
        else:
            if not os.path.exists(path_or_data):
                 print(f"Error: Image path not found: {path_or_data}")
                 return None
            img = cv2.imread(path_or_data, cv2.IMREAD_UNCHANGED)
        if img is None:
             print(f"Error: Failed to load image from {'data buffer' if is_data else path_or_data}")
        return img

    # Load images
    img1 = load_image(image_path_or_data1, both_cover_data)
    img2 = load_image(image_path_or_data2, True) # Second arg is always data

    if img1 is None or img2 is None:
        return 0.0 # Return 0 if loading failed

    # Resize images to a standard size for comparison
    try:
        img1_resized, img2_resized = resize_images(img1, img2)
    except cv2.error as e:
         print(f"Error resizing images: {e}. Check image validity.")
         return 0.0

    # Preprocess images (convert to grayscale, equalize, normalize)
    gray1 = preprocess_image(img1_resized)
    gray2 = preprocess_image(img2_resized)

    # Compare preprocessed images
    score = compare_images(gray1, gray2, silent=silent)

    return score

# Finds and extracts the internal cover from a manga or novel file.
def find_and_extract_cover(
    file_obj, # Pass the File object
    return_data_only=False,
    silent=False,
    blank_image_check=compare_detected_cover_to_blank_images, # Use setting
):
    """Finds and extracts the cover image from an archive file."""

    # Helper function to filter and sort files in the zip archive
    def filter_and_sort_files(zip_list):
        return sorted(
            [
                x
                for x in zip_list
                if not x.endswith("/") # Is not a directory entry
                and "." in x # Has an extension
                and get_file_extension(x) in image_extensions # Is an image
                and not os.path.basename(x).startswith((".", "__")) # Not hidden/system
            ]
        )

    # Helper function to read image data from the zip file
    def get_image_data(zip_ref, image_path):
        try:
            with zip_ref.open(image_path) as image_file_ref:
                return image_file_ref.read()
        except KeyError:
             print(f"Error: Internal file '{image_path}' not found in archive.")
             return None
        except Exception as e:
             print(f"Error reading internal file '{image_path}': {e}")
             return None

    # Helper function to save image data to a file
    def save_image_data(image_path, image_data):
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(image_path), exist_ok=True)
            with open(image_path, "wb") as image_file_ref_out:
                image_file_ref_out.write(image_data)
            return True
        except Exception as e:
            print(f"Error saving image data to {image_path}: {e}")
            return False

    # Helper function to check if an image is blank using SSIM
    def is_blank_image_ssim(image_data):
        if not image_data: return True # Treat empty data as blank
        # Compare against white blank
        ssim_score_white = prep_images_for_similarity(
            blank_white_image_path, image_data, both_cover_data=False, silent=silent
        ) if blank_white_image_path else 0.0
        # Compare against black blank
        ssim_score_black = prep_images_for_similarity(
            blank_black_image_path, image_data, both_cover_data=False, silent=silent
        ) if blank_black_image_path else 0.0

        is_blank = (ssim_score_white >= blank_cover_required_similarity_score or
                    ssim_score_black >= blank_cover_required_similarity_score)
        if is_blank and not silent:
             print(f"\t\t\tBlank image detected (White SSIM: {ssim_score_white:.3f}, Black SSIM: {ssim_score_black:.3f})")
        return is_blank

    # Helper function to process a cover image and save or return the data
    def process_cover_image(cover_archive_path, image_data):
        if not image_data: return None

        # Determine output extension based on settings
        output_ext = ".webp" if output_covers_as_webp else ".jpg"
        output_path = f"{file_obj.extensionless_path}{output_ext}"

        # Compress (which also handles format conversion)
        processed_data_or_path = compress_image(
            output_path, # Pass potential output path for naming
            quality=image_quality,
            raw_data=image_data
        )

        if return_data_only:
            return processed_data_or_path # This is the compressed data (bytes) or None
        else:
            # If compress_image saved a file, processed_data_or_path is the filename
            if isinstance(processed_data_or_path, str) and os.path.isfile(processed_data_or_path):
                return processed_data_or_path # Return the path to the saved (potentially compressed/converted) file
            # If compress_image returned data (shouldn't happen if raw_data=False), or failed
            elif isinstance(processed_data_or_path, bytes):
                 # Need to save the compressed data manually if compress_image didn't save
                 if save_image_data(output_path, processed_data_or_path):
                      return output_path
                 else:
                      return None # Failed to save
            else:
                 # Compression/saving failed in compress_image
                 return None


    # --- Main function logic ---
    if not os.path.isfile(file_obj.path):
        if not silent: print(f"\nFile not found: {file_obj.path}")
        return None

    if not zipfile.is_zipfile(file_obj.path):
        if not silent: print(f"\nFile is not a valid zip archive: {file_obj.path}")
        return None

    # Get the novel cover path hint if applicable
    novel_cover_hint = ""
    if file_obj.extension in novel_extensions:
        novel_cover_hint = get_novel_cover(file_obj.path) # Use metadata_utils function
        if novel_cover_hint and not silent:
            print(f"\t\tNovel cover hint found: {novel_cover_hint}")

    try:
        with zipfile.ZipFile(file_obj.path, "r") as zip_ref:
            zip_list = filter_and_sort_files(zip_ref.namelist())
            if not zip_list:
                 if not silent: print("\t\tNo image files found in archive.")
                 return None

            # Prioritize novel cover hint
            if novel_cover_hint:
                novel_cover_basename = os.path.basename(novel_cover_hint)
                # Find the full path in zip_list matching the basename hint
                full_hint_path = next((item for item in zip_list if os.path.basename(item) == novel_cover_basename), None)
                if full_hint_path:
                    zip_list.insert(0, zip_list.pop(zip_list.index(full_hint_path))) # Move hint to front

            potential_covers = []
            # First pass: Check for explicit cover patterns and novel hint
            for image_file in zip_list:
                image_basename = os.path.basename(image_file)
                is_novel_cover = novel_cover_hint and image_basename == os.path.basename(novel_cover_hint)
                matches_pattern = any(pattern.search(image_basename) for pattern in compiled_cover_patterns)

                if is_novel_cover or matches_pattern:
                    potential_covers.append(image_file)

            # Process potential covers, checking for blanks
            for cover_path in potential_covers:
                 image_data = get_image_data(zip_ref, cover_path)
                 if not image_data: continue # Skip if read error

                 if blank_image_check and is_blank_image_ssim(image_data):
                     continue # Skip blank image

                 # Found a non-blank potential cover
                 if not silent: print(f"\t\tFound potential cover: {cover_path}")
                 result = process_cover_image(cover_path, image_data)
                 if result: return result # Return path or data

            # Second pass: If no patterned cover found, use the first non-blank image
            if not silent: print("\t\tNo patterned cover found or all were blank. Checking first image...")
            for image_file in zip_list:
                 # Skip already checked potential covers that were blank
                 if image_file in potential_covers and blank_image_check:
                      # We already determined these were blank or processed them
                      continue

                 image_data = get_image_data(zip_ref, image_file)
                 if not image_data: continue

                 if blank_image_check and is_blank_image_ssim(image_data):
                     continue # Skip blank image

                 # Found the first non-blank image
                 if not silent: print(f"\t\tUsing first non-blank image as cover: {image_file}")
                 result = process_cover_image(image_file, image_data)
                 if result: return result # Return path or data

            # If all images were blank or couldn't be processed
            if not silent: print("\t\tNo suitable cover found (all images might be blank or processing failed).")
            return None

    except zipfile.BadZipFile:
        if not silent: print(f"Error: Bad zip file - {file_obj.path}")
        return None
    except FileNotFoundError:
         if not silent: print(f"Error: File not found - {file_obj.path}")
         return None
    except Exception as e:
        if not silent: print(f"Error processing archive {file_obj.path}: {e}")
        return None


# Function to check if the first image in a zip file is black and white
def is_first_image_black_and_white(zip_path):
    """Checks if the first image file inside a zip archive is B&W/Grayscale."""
    try:
        if not zipfile.is_zipfile(zip_path): return False

        with zipfile.ZipFile(zip_path, "r") as zip_file:
            # Filter and sort image files
            image_files = sorted(
                [
                    f for f in zip_file.namelist()
                    if not f.endswith('/') and get_file_extension(f) in image_extensions
                    and not os.path.basename(f).startswith(('.', '__'))
                ]
            )

            if not image_files:
                return False  # No image files in the archive

            first_image_path = image_files[0]
            try:
                with zip_file.open(first_image_path) as image_file:
                    # Read image data into memory
                    image_data = image_file.read()
                    # Open image using PIL from memory
                    pil_image = Image.open(io.BytesIO(image_data))
                    return is_image_black_and_white(pil_image) # Use the PIL-based check
            except Exception as e:
                 print(f"Error reading/processing first image {first_image_path} in {zip_path}: {e}")
                 return False # Treat error as not B&W

    except (zipfile.BadZipFile, FileNotFoundError) as e:
        print(f"Error opening zip file {zip_path}: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error processing zip file {zip_path}: {e}")
        return False


# Converts the passed path of a .webp file to a .jpg file
def convert_webp_to_jpg(webp_file_path):
    """Converts a WEBP image file to JPG, replacing the original."""
    if not webp_file_path or not os.path.isfile(webp_file_path) or get_file_extension(webp_file_path).lower() != '.webp':
        print(f"Invalid input for WEBP to JPG conversion: {webp_file_path}")
        return None

    extensionless_webp_file = get_extensionless_name(webp_file_path)
    jpg_file_path = f"{extensionless_webp_file}.jpg"

    try:
        with Image.open(webp_file_path) as im:
            # Convert to RGB before saving as JPG (handles transparency)
            im.convert("RGB").save(jpg_file_path, "JPEG", quality=image_quality) # Use quality setting

        # Verify conversion and delete original
        if os.path.isfile(jpg_file_path):
            remove_file(webp_file_path, silent=True) # Use file util
            if not os.path.isfile(webp_file_path):
                return jpg_file_path
            else:
                print(f"Warning: Could not delete original WEBP file {webp_file_path}")
                return jpg_file_path # Still return JPG path if conversion succeeded
        else:
            print(f"Error: JPG file not created after conversion: {jpg_file_path}")
            return None
    except Exception as e:
        print(f"Could not convert {webp_file_path} to JPG: {e}")
        # Clean up potentially created JPG file if conversion failed mid-way
        if os.path.exists(jpg_file_path):
             remove_file(jpg_file_path, silent=True)
        return None