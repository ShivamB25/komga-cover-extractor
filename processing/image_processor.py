import os
import io
from PIL import Image
import cv2
from skimage.metrics import structural_similarity as ssim

from utils.helpers import send_message
from filesystem.file_operations import remove_file


# Compresses an image and saves it to a file or returns the compressed image data.
def compress_image(image_path, quality=60, to_jpg=False, raw_data=None):
    new_filename = None
    buffer = None
    save_format = "JPEG"

    # Load the image from the file or raw data
    image = Image.open(image_path if not raw_data else io.BytesIO(raw_data))

    # Convert the image to RGB if it has an alpha channel or uses a palette
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")

    filename, ext = os.path.splitext(image_path)

    if ext == ".webp":
        save_format = "WEBP"

    # Determine the new filename for the compressed image
    if not raw_data:
        if to_jpg or ext.lower() == ".png":
            ext = ".jpg"
            if not to_jpg:
                to_jpg = True
        new_filename = f"{filename}{ext}"

    # Try to compress and save the image
    try:
        if not raw_data:
            image.save(new_filename, format=save_format, quality=quality, optimize=True)
        else:
            buffer = io.BytesIO()
            image.save(buffer, format=save_format, quality=quality)
            return buffer.getvalue()
    except Exception as e:
        # Log the error and continue
        send_message(f"Failed to compress image {image_path}: {e}", error=True)

    # Remove the original file if it's a PNG that was converted to JPG
    if to_jpg and ext.lower() == ".jpg" and os.path.isfile(image_path):
        os.remove(image_path)

    # Return the path to the compressed image file, or the compressed image data
    return new_filename if not raw_data else buffer.getvalue()


# Comapres two images using SSIM
def compare_images(imageA, imageB, silent=False):
    try:
        if not silent:
            print(f"\t\t\tBlank Image Size: {imageA.shape}")
            print(f"\t\t\tInternal Cover Size: {imageB.shape}")

        # Preprocess images
        grayA = preprocess_image(imageA)
        grayB = preprocess_image(imageB)

        # Compute SSIM between the two images
        ssim_score = ssim(grayA, grayB, data_range=1.0)

        if not silent:
            print(f"\t\t\t\tSSIM: {ssim_score}")

        return ssim_score
    except Exception as e:
        send_message(str(e), error=True)
        return 0


# Preps the image for comparison
def preprocess_image(image):
    # Check if the image is already grayscale
    if len(image.shape) == 2 or (len(image.shape) == 3 and image.shape == 1):
        gray_image = image
    else:
        # Convert to grayscale if it's a color image
        gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Apply histogram equalization
    gray_image = cv2.equalizeHist(gray_image)

    # Normalize the image
    gray_image = gray_image / 255.0

    return gray_image


# Function to determine if an image is black and white with better handling for halftones
def is_image_black_and_white(image, tolerance=15):
    """
    Determines if an image is black and white by verifying that
    most pixels are grayscale (R == G == B) and fall within the black or white range.

    Args:
        image (PIL.Image): The image to check.
        tolerance (int): The allowed difference between R, G, and B for a pixel to be considered grayscale.

    Returns:
        bool: True if the image is black and white or grayscale, False otherwise.
    """
    try:
        # Convert the image to RGB (ensures consistent handling of image modes)
        image_rgb = image.convert("RGB")

        # Extract pixel data
        pixels = list(image_rgb.getdata())

        # Count pixels that are grayscale and black/white
        grayscale_count = 0

        for r, g, b in pixels:
            # Check if the pixel is grayscale within the tolerance
            if abs(r - g) <= tolerance and abs(g - b) <= tolerance:
                # Further check if it is black or white
                if r == 0 or r == 255:
                    grayscale_count += 1
                elif 0 < r < 255:
                    grayscale_count += 1

        # If enough pixels are grayscale or black/white, return True
        if grayscale_count / len(pixels) > 0.9:
            return True

        return False  # Otherwise, it's not black and white
    except Exception as e:
        send_message(f"Error checking if image is black and white: {e}", error=True)
        return False


# Converts the passed path of a .webp file to a .jpg file
# returns the path of the new .jpg file or none if the conversion failed
def convert_webp_to_jpg(webp_file_path):
    if webp_file_path:
        extenionless_webp_file = os.path.splitext(webp_file_path)
        jpg_file_path = f"{extenionless_webp_file}.jpg"

        try:
            with Image.open(webp_file_path) as im:
                im.convert("RGB").save(jpg_file_path)
            # verify that the conversion worked
            if os.path.isfile(jpg_file_path):
                # delete the .webp file
                remove_file(webp_file_path, silent=True)
                # verify that the .webp file was deleted
                if not os.path.isfile(webp_file_path):
                    return jpg_file_path
                else:
                    send_message(
                        f"ERROR: Could not delete {webp_file_path}", error=True
                    )
            else:
                send_message(
                    f"ERROR: Could not convert {webp_file_path} to jpg", error=True
                )
        except Exception as e:
            send_message(
                f"Could not convert {webp_file_path} to jpg\nERROR: {e}",
                error=True,
            )
    return None