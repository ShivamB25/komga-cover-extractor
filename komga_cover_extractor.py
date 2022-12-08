import os
import re
import zipfile
import shutil
import string
import regex as re
import sys
import argparse
import subprocess
import urllib.request
import calendar
import requests
import time
import scandir
import random
import xml.etree.ElementTree as ET
import io
from PIL import Image
from PIL import ImageFile
from lxml import etree
from genericpath import isfile
from posixpath import join
from difflib import SequenceMatcher
from datetime import datetime
from discord_webhook import DiscordWebhook, DiscordEmbed
from bs4 import BeautifulSoup, SoupStrainer
from settings import *
from langdetect import detect
from titlecase import titlecase
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from base64 import b64encode
from unidecode import unidecode
from io import BytesIO

script_version = "1.3.1"

# Paths = existing library
# Download_folders = newly aquired manga/novels
paths = []
download_folders = []

# global folder_accessor
folder_accessor = None

# whether or not to compress the extractred images
compress_image_option = False

# Default image compression value.
# Pass in via cli
image_quality = 60

# Stat-related
file_count = 0
cbz_count = 0
epub_count = 0
image_count = 0
cbz_internal_covers_found = 0
poster_found = 0
errors = []
items_changed = []

# A discord webhook url used to send messages to discord about the changes made.
# Pass in via cli
discord_webhook_url = []


# Two webhooks specific to the bookwalker check.
# One is used for released books, the other is used for upcoming books.
# Intended to be sent to two seperate channels.
# FIRST WEBHOOK = released books
# SECOND WEBHOOK = upcoming books
bookwalker_webhook_urls = []

# Whether or not to check the library against bookwalker for new releases.
bookwalker_check = False

# All the release groups stored in release_groups.txt
# Used when renaming files where it has a matching group.
release_groups = []

# skipped files that don't have a release group
skipped_files = []

# Newly released volumes that aren't currently in the library.
new_releases_on_bookwalker = []

# A quick and dirty fix to avoid non-processed files from
# being moved over to the existing library. Will be removed in the future.
processed_files = []

# Any files moved to the existing library.
# Used when determining whether or not to trigger a library scan in komga.
moved_files = []

# All extensions that aren't in this list will be ignored when searching
# an epubs internal contents.
internal_epub_extensions = [".xhtml", ".opf", ".ncx", ".xml", ".html"]

# Where logs are written to.
ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Cached paths from the users existing library. Read from cached_paths.txt
cached_paths = []

# Cached identifier results, aka successful matches via series_id or isbn
cached_identifier_results = []

# watchdog toggle
watchdog_toggle = False


# Folder Class
class Folder:
    def __init__(self, root, dirs, basename, folder_name, files):
        self.root = root
        self.dirs = dirs
        self.basename = basename
        self.folder_name = folder_name
        self.files = files


# File Class
class File:
    def __init__(
        self,
        name,
        extensionless_name,
        basename,
        extension,
        root,
        path,
        extensionless_path,
    ):
        self.name = name
        self.extensionless_name = extensionless_name
        self.basename = basename
        self.extension = extension
        self.root = root
        self.path = path
        self.extensionless_path = extensionless_path


# Volume Class
class Volume:
    def __init__(
        self,
        volume_type,
        series_name,
        volume_year,
        volume_number,
        volume_part,
        is_fixed,
        release_group,
        name,
        extensionless_name,
        basename,
        extension,
        root,
        path,
        extensionless_path,
        extras,
        revision_number,
        multi_volume=None,
        is_one_shot=None,
    ):
        self.volume_type = volume_type
        self.series_name = series_name
        self.volume_year = volume_year
        self.volume_number = volume_number
        self.volume_part = volume_part
        self.is_fixed = is_fixed
        self.release_group = release_group
        self.name = name
        self.extensionless_name = extensionless_name
        self.basename = basename
        self.extension = extension
        self.root = root
        self.path = path
        self.extensionless_path = extensionless_path
        self.extras = extras
        self.revision_number = revision_number
        self.multi_volume = multi_volume
        self.is_one_shot = is_one_shot


# It watches the download directory for any changes.
class Watcher:
    def __init__(self):
        self.observer = Observer()

    def run(self):
        event_handler = Handler()
        if download_folders:
            self.observer.schedule(event_handler, download_folders[0], recursive=True)
            self.observer.start()
            try:
                while True:
                    time.sleep(5)
            except:
                self.observer.stop()
                print("Observer Stopped")

            self.observer.join()


class Handler(FileSystemEventHandler):
    @staticmethod
    def on_any_event(event):
        if (
            not event.is_directory
            and not os.path.basename(event.src_path).startswith(".")
            and event.event_type == "created"
            and os.path.isfile(event.src_path)
            and re.sub("\.", "", get_file_extension(os.path.basename(event.src_path)))
            in file_extensions
        ):
            time.sleep(5)
            fields = []
            if os.path.isfile(event.src_path):
                fields = [
                    {
                        "name": "File Found:",
                        "value": "```" + str(event.src_path) + "```",
                        "inline": False,
                    }
                ]
                send_discord_message(
                    None,
                    "Starting Script (WATCHDOG) (EXPERIMENTAL)",
                    color=7615723,
                    fields=fields,
                )
                main()


# get age of file and return in minutes based on creation time
def get_creation_age(file):
    return int(time.time() - os.path.getctime(file)) / 60


# return line count of a file
def get_line_count(file_path):
    with open(file_path, "r") as file:
        return sum(1 for _ in file)


# Read all the lines of a text file and return them
def read_lines_from_file(file_path):
    lines = []
    with open(file_path, "r") as file:
        for line in file:
            line = line.strip()
            if line:
                lines.append(line)
    return lines


volume_keywords = [
    "LN",
    "Light Novel",
    "Novel",
    "Book",
    "Volume",
    "Vol",
    "V",
    "第",
    "Disc",
]

volume_one_number_keywords = ["One", "1", "01", "001", "0001"]

new_volume_webhook = None

# Parses the passed command-line arguments
def parse_my_args():
    global paths
    global download_folders
    global discord_webhook_url
    parser = argparse.ArgumentParser(
        description="Scans for covers in the cbz and epub files."
    )
    parser.add_argument(
        "-p",
        "--paths",
        help="The path/paths to be scanned for cover extraction.",
        action="append",
        nargs="*",
        required=False,
    )
    parser.add_argument(
        "-df",
        "--download_folders",
        help="The download folder/download folders for processing, renaming, and moving of downloaded files. (Optional, still in testing, requires manual uncommenting of optional method calls at the bottom of the script.)",
        action="append",
        nargs="*",
        required=False,
    )
    parser.add_argument(
        "-wh",
        "--webhook",
        action="append",
        nargs="*",
        help="The discord webhook url for notifications about changes and errors.",
        required=False,
    )
    parser.add_argument(
        "-bwc",
        "--bookwalker_check",
        help="Checks for new releases on bookwalker.",
        required=False,
    )
    parser.add_argument(
        "-c",
        "--compress",
        help="Whether or not to compress the extracted cover images.",
        required=False,
    )
    parser.add_argument(
        "-cq",
        "--compress_quality",
        help="The quality of the compressed cover images.",
        required=False,
    )
    parser.add_argument(
        "-bwk_whs",
        "--bookwalker_webhook_urls",
        help="The webhook urls for the bookwalker check.",
        action="append",
        nargs="*",
        required=False,
    )
    parser.add_argument(
        "-wd",
        "--watchdog",
        help="Whether or not to use the watchdog library to watch for file changes in the download folders.",
        required=False,
    )
    parser.add_argument(
        "-nw",
        "--new_volume_webhook",
        help="If passed in, the new volume release notification will be redirected to this single discord webhook channel.",
        required=False,
    )
    parser = parser.parse_args()
    if not parser.paths and not parser.download_folders:
        print("No paths or download folders were passed to the script.")
        print("Exiting...")
        exit()
    if parser.paths is not None:
        paths = []
        for path in parser.paths:
            for p in path:
                paths.append(p)
    if parser.download_folders is not None:
        download_folders = []
        for download_folder in parser.download_folders:
            for folder in download_folder:
                download_folders.append(folder)
    if parser.webhook is not None:
        discord_webhook_url = []
        for item in parser.webhook:
            for hook in item:
                if hook not in discord_webhook_url:
                    discord_webhook_url.append(hook)
    if parser.bookwalker_check is not None:
        if (
            parser.bookwalker_check == 1
            or parser.bookwalker_check.lower() == "true"
            or parser.bookwalker_check.lower() == "yes"
        ):
            global bookwalker_check
            bookwalker_check = True
    if parser.compress is not None:
        if (
            parser.compress == 1
            or parser.compress.lower() == "true"
            or parser.compress.lower() == "yes"
        ):
            global compress_image_option
            compress_image_option = True
    if parser.compress_quality is not None:
        global image_quality
        image_quality = set_num_as_float_or_int(parser.compress_quality)
    if parser.bookwalker_webhook_urls is not None:
        global bookwalker_webhook_urls
        bookwalker_webhook_urls = []
        for item in parser.bookwalker_webhook_urls:
            for hook in item:
                if hook not in bookwalker_webhook_urls:
                    bookwalker_webhook_urls.append(hook)
    if parser.watchdog is not None:
        if (
            parser.watchdog == 1
            or parser.watchdog.lower() == "true"
            or parser.watchdog.lower() == "yes"
        ):
            if download_folders:
                global watchdog_toggle
                watchdog_toggle = True
            else:
                send_error_message(
                    "Watchdog was enabled, but no download folders were passed to the script."
                )
    if parser.new_volume_webhook is not None:
        global new_volume_webhook
        new_volume_webhook = parser.new_volume_webhook


def set_num_as_float_or_int(volume_number):
    try:
        if volume_number != "":
            if isinstance(volume_number, list):
                result = ""
                for num in volume_number:
                    if float(num) == int(num):
                        if num == volume_number[-1]:
                            result += str(int(num))
                        else:
                            result += str(int(num)) + "-"
                    else:
                        if num == volume_number[-1]:
                            result += str(float(num))
                        else:
                            result += str(float(num)) + "-"
                return result
            elif isinstance(volume_number, str) and re.search(r"\.", volume_number):
                volume_number = float(volume_number)
            else:
                if float(volume_number) == int(volume_number):
                    volume_number = int(volume_number)
                else:
                    volume_number = float(volume_number)
    except Exception as e:
        send_error_message(
            "Failed to convert volume number to float or int: " + str(volume_number)
        )
        send_error_message(e)
        return ""
    return volume_number


# Handles image compression
def compress_image(image_path, quality=image_quality, to_jpg=False, raw_data=None):
    img = None
    filename = None
    extension = None
    new_filename = None
    if not raw_data:
        img = Image.open(image_path)
    else:
        img = Image.open(io.BytesIO(raw_data))
    if not raw_data:
        filename, ext = os.path.splitext(image_path)
        extension = get_file_extension(image_path)
        if extension == ".png" and not raw_data:
            to_jpg = True
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    if to_jpg and not raw_data:
        new_filename = f"{filename}.jpg"
    elif not raw_data:
        new_filename = f"{filename}" + extension
    try:
        if not raw_data:
            img.save(new_filename, quality=quality, optimize=True)
        else:
            # compress the image data using BytesIO and return the data
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=50)
            # return the compressed image data
            return buffer.getvalue()
        if extension == ".png" and (
            os.path.isfile(new_filename) and os.path.isfile(image_path) and not raw_data
        ):
            os.remove(image_path)
            return image_path
    except OSError as ose:
        send_error_message(
            "\t\tFailed to compress image: " + image_path + " \n\t\tERROR: " + str(ose)
        )


# Check the text file line by line for the passed message
def check_text_file_for_message(text_file, message):
    with open(text_file, "r") as f:
        for line in f:
            if message.strip() == line.strip():
                return True
    return False


# Appends, sends, and prints our error message
def send_error_message(error, discord=True):
    print(error)
    if discord != False:
        send_discord_message(error)
    errors.append(error)
    write_to_file("errors.txt", error)


# Appends, sends, and prints our change message
def send_change_message(message):
    print(message)
    send_discord_message(message)
    items_changed.append(message)
    write_to_file("changes.txt", message)


last_hook_index = None

# Sends a discord message using the users webhook url
def send_discord_message(
    message,
    title=None,
    url=None,
    rate_limit=True,
    color=None,
    proxies={},
    fields=[],
    timestamp=True,
    passed_webhook=None,
    image=None,
    image_local=None,
):
    hook = None
    global script_version
    global discord_webhook_url
    global last_hook_index
    if not passed_webhook:
        if discord_webhook_url:
            if not last_hook_index and last_hook_index != 0:
                hook = discord_webhook_url[0]
            else:
                if last_hook_index == len(discord_webhook_url) - 1:
                    hook = discord_webhook_url[0]
                else:
                    hook = discord_webhook_url[last_hook_index + 1]
        if url:
            hook = url
        elif hook:
            last_hook_index = discord_webhook_url.index(hook)
    else:
        hook = passed_webhook
    webhook = DiscordWebhook()
    embed = None
    try:
        if hook:
            if color and not embed:
                embed = DiscordEmbed()
                embed.color = color
            elif color and embed:
                embed.color = color
            if title and not embed:
                embed = DiscordEmbed()
                embed.title = title
            elif title and embed:
                embed.title = title
            if message and not embed:
                webhook.content = message
            elif message and embed:
                embed.description = message
            webhook.url = hook
            if rate_limit:
                webhook.rate_limit_retry = rate_limit
            if embed:
                if fields:
                    for field in fields:
                        embed.add_embed_field(
                            name=field["name"],
                            value=field["value"],
                            inline=field["inline"],
                        )
                if script_version:
                    embed.set_footer(text="v" + script_version)
                if timestamp:
                    embed.set_timestamp()
                if image and not image_local:
                    embed.set_image(url=image)
                elif image_local and not image:
                    webhook.add_file(file=image_local, filename="cover.jpg")
                    embed.set_image(url="attachment://cover.jpg")
                webhook.add_embed(embed)
            if proxies:
                webhook.proxies = proxies
            response = webhook.execute()
        else:
            return
    except Exception as e:
        send_error_message(e, discord=False)


# Removes hidden files
def remove_hidden_files(files, root):
    for file in files[:]:
        if file.startswith("."):
            files.remove(file)


# Removes any unaccepted file types
def remove_unaccepted_file_types(files, root):
    for file in files[:]:
        extension = re.sub("\.", "", get_file_extension(file))
        if extension not in file_extensions and os.path.isfile(
            os.path.join(root, file)
        ):
            files.remove(file)


# Removes any folder names in the ignored_folders
def remove_ignored_folders(dirs):
    if len(ignored_folder_names) != 0:
        dirs[:] = [d for d in dirs if d not in ignored_folder_names]


# Remove hidden folders from the list
def remove_hidden_folders(root, dirs):
    for folder in dirs[:]:
        if folder.startswith(".") and os.path.isdir(os.path.join(root, folder)):
            dirs.remove(folder)


# Removes all chapter releases
def remove_all_chapters(files):
    for file in files[:]:
        if contains_chapter_keywords(file) and not contains_volume_keywords(file):
            files.remove(file)


# Cleans up the files array before usage
def clean_and_sort(root, files=None, dirs=None, sort=False):
    if files:
        if sort:
            files.sort()
        remove_hidden_files(files, root)
        remove_unaccepted_file_types(files, root)
        remove_all_chapters(files)
    if dirs:
        if sort:
            dirs.sort()
        remove_hidden_folders(root, dirs)
        remove_ignored_folders(dirs)


# Retrieves the file extension on the passed file
def get_file_extension(file):
    return os.path.splitext(file)[1]


# Returns an extensionless name
def get_extensionless_name(file):
    return os.path.splitext(file)[0]


# Trades out our regular files for file objects
def upgrade_to_file_class(files, root):
    clean_and_sort(root, files)
    results = []
    for file in files:
        file_obj = File(
            file,
            get_extensionless_name(file),
            get_series_name_from_file_name(file, root),
            get_file_extension(file),
            root,
            os.path.join(root, file),
            get_extensionless_name(os.path.join(root, file)),
        )
        results.append(file_obj)
    return results


# Updates our output stats
def update_stats(file):
    global file_count
    if (file.name).endswith(".cbz") and os.path.isfile(file.path):
        global cbz_count
        file_count += 1
        cbz_count += 1
    if (file.name).endswith(".epub") and os.path.isfile(file.path):
        global epub_count
        file_count += 1
        epub_count += 1


# Gets and returns the basename
def get_base_name(item):
    return os.path.basename(item)


# Removes hidden files with the base name.
def remove_hidden_files_with_basename(files):
    for file in files[:]:
        base = os.path.basename(file)
        if file.startswith(".") or base.startswith("."):
            files.remove(file)


# Credit to original source: https://alamot.github.io/epub_cover/
# Modified by me.
# Retrieves the inner epub cover
def get_epub_cover(epub_path):
    try:
        namespaces = {
            "calibre": "http://calibre.kovidgoyal.net/2009/metadata",
            "dc": "http://purl.org/dc/elements/1.1/",
            "dcterms": "http://purl.org/dc/terms/",
            "opf": "http://www.idpf.org/2007/opf",
            "u": "urn:oasis:names:tc:opendocument:xmlns:container",
            "xsi": "http://www.w3.org/2001/XMLSchema-instance",
        }
        with zipfile.ZipFile(epub_path) as z:
            t = etree.fromstring(z.read("META-INF/container.xml"))
            rootfile_path = t.xpath(
                "/u:container/u:rootfiles/u:rootfile", namespaces=namespaces
            )[0].get("full-path")
            t = etree.fromstring(z.read(rootfile_path))
            cover_id = t.xpath(
                "//opf:metadata/opf:meta[@name='cover']", namespaces=namespaces
            )[0].get("content")
            cover_href = t.xpath(
                "//opf:manifest/opf:item[@id='" + cover_id + "']", namespaces=namespaces
            )[0].get("href")
            if re.search(r"%", cover_href):
                cover_href = urllib.parse.unquote(cover_href)
            cover_path = os.path.join(os.path.dirname(rootfile_path), cover_href)
            return cover_path
        return None
    except Exception as e:
        return None


# Checks if the passed string is a volume one.
def is_volume_one(volume_name):
    for vk in volume_keywords:
        for vonk in volume_one_number_keywords:
            if re.search(
                rf"(\b({vk})([-_. ]|)({vonk})(([-_.]([0-9]+))+)?\b)",
                volume_name,
                re.IGNORECASE,
            ):
                return True
    return False


# Checks if the passed string contains volume keywords
def contains_volume_keywords(file):
    return re.search(
        r"((\s?(\s-\s|)(Part|)+(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ]|)([0-9]+)\b)|\s?(\s-\s|)(Part|)(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ]|)([0-9]+)([-_.])(\s-\s|)(Part|)(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)([0-9]+)\s|\s?(\s-\s|)(Part|)(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ]|)([0-9]+)([-_.])(\s-\s|)(Part|)(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)([0-9]+)\s)",
        remove_underscore_from_name(file),
        re.IGNORECASE,
    )


# Checks for volume keywords and chapter keywords.
# If neither are present, the volume is assumed to be a one-shot volume.
def is_one_shot_bk(file_name):
    number_status = re.search(r"\d+", file_name)
    chapter_file_status = contains_chapter_keywords(file_name)
    exception_keyword_status = check_for_exception_keywords(file_name)
    if (not number_status and not chapter_file_status) and (
        not exception_keyword_status
    ):
        return True
    return False


# Checks for volume keywords and chapter keywords.
# If neither are present, the volume is assumed to be a one-shot volume.
def is_one_shot(file_name, root):
    files = os.listdir(root)
    clean_and_sort(root, files)
    continue_logic = False
    if len(files) == 1 or root == download_folders[0]:
        continue_logic = True
    if continue_logic == True:
        volume_file_status = contains_volume_keywords(file_name)
        chapter_file_status = contains_chapter_keywords(file_name)
        exception_keyword_status = check_for_exception_keywords(file_name)
        if (not volume_file_status and not chapter_file_status) and (
            not exception_keyword_status
        ):
            return True
    return False


# Checks similarity between two strings.
def similar(a, b):
    if a == "" or b == "":
        return 0.0
    else:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# Moves the image into a folder if said image exists. Also checks for a cover/poster image and moves that.
def move_images(file, folder_name):
    for extension in image_extensions:
        image = file.extensionless_path + "." + extension
        if os.path.isfile(image):
            shutil.move(image, folder_name)
        for cover_file_name in series_cover_file_names:
            cover_image_file_name = cover_file_name + "." + extension
            cover_image_file_path = os.path.join(file.root, cover_image_file_name)
            if os.path.isfile(cover_image_file_path):
                if not os.path.isfile(os.path.join(folder_name, cover_image_file_name)):
                    shutil.move(cover_image_file_path, folder_name)
                else:
                    remove_file(cover_image_file_path)


# Retrieves the series name through various regexes
# Removes the volume number and anything to the right of it, and strips it.
def get_series_name_from_file_name(name, root):
    if is_one_shot(name, root):
        name = re.sub(
            r"([-_ ]+|)(((\[|\(|\{).*(\]|\)|\}))|LN)([-_. ]+|)(epub|cbz|).*",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip()
    else:
        if re.search(
            r"(\b|\s)((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ]|)([0-9]+)(\b|\s).*",
            name,
            flags=re.IGNORECASE,
        ):
            name = (
                re.sub(
                    r"(\b|\s)((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ]|)([0-9]+)(\b|\s).*",
                    "",
                    name,
                    flags=re.IGNORECASE,
                )
            ).strip()
        else:
            name = re.sub(
                r"(\d+)?([-_. ]+)?((\[|\(|\})(.*)(\]|\)|\}))?([-_. ]+)?(\.cbz|\.epub)$",
                "",
                name,
                flags=re.IGNORECASE,
            ).strip()
    return name


# Creates folders for our stray volumes sitting in the root of the download folder.
def create_folders_for_items_in_download_folder():
    for download_folder in download_folders:
        if os.path.exists(download_folder):
            try:
                for root, dirs, files in scandir.walk(download_folder):
                    clean_and_sort(root, files, dirs)
                    global folder_accessor
                    file_objects = upgrade_to_file_class(files, root)
                    folder_accessor = Folder(
                        root,
                        dirs,
                        os.path.basename(os.path.dirname(root)),
                        os.path.basename(root),
                        file_objects,
                    )
                    for file in folder_accessor.files:
                        if re.sub(
                            r"\.", "", file.extension
                        ) in file_extensions and os.path.basename(
                            download_folder
                        ) == os.path.basename(
                            file.root
                        ):
                            done = False
                            if move_lone_files_to_similar_folder and dirs:
                                for dir in dirs:
                                    if (
                                        similar(
                                            remove_underscore_from_name(
                                                remove_punctuation(dir)
                                            )
                                            .strip()
                                            .lower(),
                                            remove_underscore_from_name(
                                                remove_punctuation(file.basename)
                                            )
                                            .strip()
                                            .lower(),
                                        )
                                        >= required_similarity_score
                                    ):
                                        if (
                                            replace_series_name_in_file_name_with_similar_folder_name
                                            and file.basename != dir
                                        ):
                                            # replace the series name in the file name with the folder name and rename the file
                                            new_file_name = re.sub(
                                                file.basename,
                                                dir,
                                                file.name,
                                                flags=re.IGNORECASE,
                                            )
                                            # create file object
                                            new_file_obj = File(
                                                new_file_name,
                                                get_extensionless_name(new_file_name),
                                                get_series_name_from_file_name(
                                                    new_file_name, root
                                                ),
                                                get_file_extension(new_file_name),
                                                root,
                                                os.path.join(root, new_file_name),
                                                get_extensionless_name(
                                                    os.path.join(root, new_file_name)
                                                ),
                                            )
                                            # if it doesn't already exist
                                            if not os.path.isfile(
                                                os.path.join(
                                                    file.root, new_file_obj.name
                                                )
                                            ):
                                                rename_file(
                                                    file.path,
                                                    new_file_obj.path,
                                                    file.root,
                                                    file.extensionless_path,
                                                    new_file_obj.extensionless_path,
                                                )
                                                file = new_file_obj
                                            else:
                                                # if it does exist, delete the file
                                                remove_file(file.path)
                                        # check that the file doesn't already exist in the folder
                                        if os.path.isfile(
                                            file.path
                                        ) and not os.path.isfile(
                                            os.path.join(root, dir, file.name)
                                        ):
                                            # it doesn't, we move it and the image associated with it, to that folder
                                            move_file(file, os.path.join(root, dir))
                                            done = True
                                            break
                                        else:
                                            # it does, so we remove the duplicate file
                                            remove_file(os.path.join(root, file.name))
                                            done = True
                                            break
                            if not done:
                                similarity_result = similar(file.name, file.basename)
                                write_to_file(
                                    "changes.txt",
                                    "Similarity Result between: "
                                    + file.name
                                    + " and "
                                    + file.basename
                                    + " was "
                                    + str(similarity_result),
                                )
                                folder_location = os.path.join(file.root, file.basename)
                                does_folder_exist = os.path.exists(folder_location)
                                if not does_folder_exist:
                                    os.mkdir(folder_location)
                                    move_file(file, folder_location)
                                else:
                                    move_file(file, folder_location)
            except Exception as e:
                send_error_message(e)
        else:
            if download_folder == "":
                send_error_message("\nERROR: Path cannot be empty.")
            else:
                print("\nERROR: " + download_folder + " is an invalid path.\n")


# Returns the percentage of files that are epub, to the total amount of files
def get_epub_percent_for_folder(files):
    epub_folder_count = 0
    for file in files:
        if (file).endswith(".epub"):
            epub_folder_count += 1
    epub_percent = (
        (epub_folder_count / (len(files)) * 100) if epub_folder_count != 0 else 0
    )
    return epub_percent


# Returns the percentage of files that are cbz, to the total amount of files
def get_cbz_percent_for_folder(files):
    cbz_folder_count = 0
    for file in files:
        if (file).endswith(".cbz"):
            cbz_folder_count += 1
    cbz_percent = (
        (cbz_folder_count / (len(files)) * 100) if cbz_folder_count != 0 else 0
    )
    return cbz_percent


def check_for_multi_volume_file(file_name):
    if re.search(
        r"(\b(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.)?(\s+)?([0-9]+(\.[0-9]+)?)([-_]([0-9]+(\.[0-9]+)?))+\b)",
        remove_bracketed_info_from_name(file_name),
        re.IGNORECASE,
    ):
        return True
    else:
        return False


def convert_list_of_numbers_to_array(string):
    numbers = []
    numbers_search = re.sub(r"[-_]", " ", string)
    numbers_search = remove_dual_space(numbers_search).strip()
    numbers_search = numbers_search.split(" ")
    # convert them to numbers using set_num_as_float_or_int
    numbers_search = [set_num_as_float_or_int(num) for num in numbers_search]
    if numbers_search:
        # get lowest number in list
        lowest_number = min(numbers_search)
        # get highest number in list
        highest_number = max(numbers_search)
        # discard any numbers inbetween the lowest and highest number
        if lowest_number and highest_number:
            numbers = [lowest_number, highest_number]
        elif lowest_number and not highest_number:
            numbers = [lowest_number]
        elif highest_number and not lowest_number:
            numbers = [highest_number]
    return numbers


# Finds the volume number and strips out everything except that number
def remove_everything_but_volume_num(files):
    results = []
    is_multi_volume = False
    for file in files[:]:
        is_multi_volume = check_for_multi_volume_file(file)
        result = re.search(
            r"\b(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)((\.)|)(\s+)?([0-9]+)(([-_.])([0-9]+)|)+\b",
            file,
            re.IGNORECASE,
        )
        if result:
            try:
                file = result
                if hasattr(file, "group"):
                    file = file.group()
                else:
                    file = ""
                file = re.sub(
                    r"\b(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ])?",
                    "",
                    file,
                    flags=re.IGNORECASE,
                ).strip()
                if re.search(
                    r"\b[0-9]+(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)[0-9]+\b",
                    file,
                    re.IGNORECASE,
                ):
                    file = (
                        re.sub(
                            r"(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)",
                            ".",
                            file,
                            flags=re.IGNORECASE,
                        )
                    ).strip()
                try:
                    if is_multi_volume:
                        volume_numbers = convert_list_of_numbers_to_array(file)
                        if volume_numbers:
                            if len(volume_numbers) > 1:
                                for volume_number in volume_numbers:
                                    results.append(float(volume_number))
                            elif len(volume_numbers) == 1:
                                results.append(float(volume_numbers[0]))
                                is_multi_volume = False
                    else:
                        results.append(float(file))
                except ValueError:
                    message = "Not a float: " + files[0]
                    print(message)
                    write_to_file("errors.txt", message)
            except AttributeError:
                print(str(AttributeError.with_traceback))
        else:
            files.remove(file)
    if is_multi_volume == True and len(results) != 0:
        return results
    elif len(results) != 0 and (len(results) == len(files)):
        return results[0]
    elif len(results) == 0:
        return ""


# Retrieves the release year
def get_volume_year(name):
    result = re.search(r"\((\d{4})\)", name, re.IGNORECASE)
    if hasattr(result, "group"):
        result = result.group(1).strip()
        try:
            result = int(result)
        except ValueError as ve:
            print(ve)
    else:
        result == ""
    return result


# Determines whether or not the release is a fixed release
def is_fixed_volume(name):
    if re.search(
        r"(\(|\[|\{)(f|fix(ed)?)([-_. :]+)?([0-9]+)?(\)|\]|\})", name, re.IGNORECASE
    ):
        return True
    else:
        return False


# Retrieves the release_group on the file name
def get_release_group(name):
    global release_groups
    result = ""
    if release_groups:
        for group in release_groups:
            group_escaped = re.escape(group)
            left_brackets = r"(\(|\[|\{)"
            right_brackets = r"(\)|\]|\})"
            search = re.search(
                rf"{left_brackets}{group_escaped}{right_brackets}", name, re.IGNORECASE
            )
            if search:
                result = search.group()
                if result:
                    # remove any brackets that it starts with or ends with
                    result = re.sub(rf"^{left_brackets}|{right_brackets}$", "", result)
                break
    return result


# Checks the extension and returns accordingly
def get_type(name):
    if str(name).endswith(".cbz"):
        return "manga"
    elif str(name).endswith(".epub"):
        return "light novel"


# Retrieves and returns the volume part from the file name
def get_volume_part(file):
    result = ""
    file = re.sub(
        r".*(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)([-_. ]|)([-_. ]|)([0-9]+)(\b|\s)",
        "",
        file,
        flags=re.IGNORECASE,
    ).strip()
    search = re.search(r"(\b(Part)([-_. ]|)([0-9]+)\b)", file, re.IGNORECASE)
    if search:
        result = search.group(1)
        result = re.sub(r"(\b(Part)([-_. ]|)\b)", "", result, flags=re.IGNORECASE)
        try:
            return float(result)
        except ValueError:
            print("Not a float: " + file)
            result = ""
    return result


# Trades out our regular files for file objects
def upgrade_to_volume_class(files):
    results = []
    for file in files:
        volume_obj = Volume(
            get_type(file.extension),
            get_series_name_from_file_name(file.name, file.root),
            get_volume_year(file.name),
            remove_everything_but_volume_num([file.name]),
            get_volume_part(file.name),
            is_fixed_volume(file.name),
            get_release_group(file.name),
            file.name,
            file.extensionless_name,
            file.basename,
            file.extension,
            file.root,
            file.path,
            file.extensionless_path,
            get_extras(file.name, file.root),
            None,
            check_for_multi_volume_file(file.name),
            is_one_shot=is_one_shot(file.name, file.root),
        )
        results.append(volume_obj)
    for obj in results:
        if obj.is_one_shot:
            obj.volume_number = 1
    return results


# The RankedKeywordResult class is a container for the total score and the keywords
class RankedKeywordResult:
    def __init__(self, total_score, keywords):
        self.total_score = total_score
        self.keywords = keywords


# > This class represents the result of an upgrade check
class UpgradeResult:
    def __init__(self, is_upgrade, downloaded_ranked_result, current_ranked_result):
        self.is_upgrade = is_upgrade
        self.downloaded_ranked_result = downloaded_ranked_result
        self.current_ranked_result = current_ranked_result


# Retrieves the release_group score from the list, using a high similarity
def get_keyword_score(name, download_folder=False):
    tags = []
    score = 0.0
    for keyword in ranked_keywords:
        search = re.search(keyword.name, name, re.IGNORECASE)
        if search:
            tags.append(Keyword(search.group(0), keyword.score))
            score += keyword.score
    return RankedKeywordResult(score, tags)


# Checks if the downloaded release is an upgrade for the current release.
def is_upgradeable(downloaded_release, current_release):
    downloaded_release_result = get_keyword_score(downloaded_release.name)
    current_release_result = get_keyword_score(current_release.name)
    upgrade_result = UpgradeResult(
        downloaded_release_result.total_score > current_release_result.total_score,
        downloaded_release_result,
        current_release_result,
    )
    return upgrade_result


# Deletes hidden files, used when checking if a folder is empty.
def delete_hidden_files(files, root):
    for file in files[:]:
        if (str(file)).startswith(".") and os.path.isfile(os.path.join(root, file)):
            remove_file(os.path.join(root, file))


# Removes the old series and cover image
def remove_images(path):
    for image_extension in image_extensions:
        if is_volume_one(os.path.basename(path)):
            for cover_file_name in series_cover_file_names:
                cover_file_name = os.path.join(
                    os.path.dirname(path), cover_file_name + "." + image_extension
                )
                if os.path.isfile(cover_file_name):
                    remove_file(cover_file_name, silent=True)
        volume_image_cover_file_name = (
            get_extensionless_name(path) + "." + image_extension
        )
        if os.path.isfile(volume_image_cover_file_name):
            remove_file(volume_image_cover_file_name, silent=True)


# Removes a file
def remove_file(full_file_path, silent=False):
    try:
        os.remove(full_file_path)
        if not os.path.isfile(full_file_path):
            if not silent:
                print("\t\t\tFile Removed: " + full_file_path)
                send_discord_message(
                    "File: " + "```" + full_file_path + "```",
                    "Removed File",
                    color=16711680,
                )
            remove_images(full_file_path)
            return True
        else:
            send_error_message("\n\t\t\tFailed to remove file: " + full_file_path)
            return False
    except OSError as e:
        send_error_message(e)


# Move a file
def move_file(file, new_location, silent=False):
    try:
        if os.path.isfile(file.path):
            shutil.move(file.path, new_location)
            if os.path.isfile(os.path.join(new_location, file.name)):
                if not silent:
                    print("\t\tMoved File: " + file.name + " to " + new_location)
                    send_discord_message(
                        "File: "
                        + "```"
                        + file.name
                        + "```"
                        + "To:"
                        + "```"
                        + new_location
                        + "```",
                        "Moved File",
                        color=8421504,
                    )
                move_images(file, new_location)
            else:
                send_error_message(
                    "\t\tFailed to move: "
                    + os.path.join(file.root, file.name)
                    + " to: "
                    + new_location
                )
    except OSError as e:
        send_error_message(e)


# Replaces an old file.
def replace_file(old_file, new_file):
    try:
        if os.path.isfile(old_file.path) and os.path.isfile(new_file.path):
            file_removal_status = remove_file(old_file.path)
            if not os.path.isfile(old_file.path) and file_removal_status:
                move_file(new_file, old_file.root, silent=True)
                if os.path.isfile(os.path.join(old_file.root, new_file.name)):
                    print(
                        "\t\tFile: " + new_file.name + " was moved to: " + old_file.root
                    )
                    send_discord_message(
                        "File: "
                        + "```"
                        + new_file.name
                        + "```"
                        + "To:"
                        + "```"
                        + old_file.root
                        + "```",
                        "Moved File",
                        color=8421504,
                    )
                else:
                    send_error_message(
                        "\tFailed to replace: "
                        + old_file.name
                        + " with: "
                        + new_file.name
                    )
            else:
                send_error_message(
                    "\tFailed to remove old file: "
                    + old_file.name
                    + "\nUpgrade aborted."
                )
        else:
            send_error_message(
                "\tOne of the files is missing, failed to replace.\n"
                + old_file.path
                + new_file.path
            )
    except Exception as e:
        send_error_message(e)
        send_error_message("Failed file replacement.")


# execute command with subprocess and reutrn the output
def execute_command(command):
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE)
        output, error = process.communicate()
        return output.decode("utf-8")
    except Exception as e:
        send_error_message(e)


# Removes the duplicate after determining it's upgrade status, otherwise, it upgrades
def remove_duplicate_releases_from_download(original_releases, downloaded_releases):
    global moved_files
    for download in downloaded_releases[:]:
        if (
            not isinstance(download.volume_number, int)
            and not isinstance(download.volume_number, float)
            and not download.multi_volume
        ):
            send_error_message("\n\t\tVolume number empty/missing in: " + download.name)
            send_error_message("\t\tAvoiding file, could be a chapter.")
            downloaded_releases.remove(download)
        if len(downloaded_releases) != 0:
            for original in original_releases[:]:
                if (
                    (
                        isinstance(download.volume_number, float)
                        and isinstance(original.volume_number, float)
                    )
                    or (
                        download.volume_number
                        and download.multi_volume
                        and isinstance(download.volume_number, list)
                    )
                ) and os.path.isfile(original.path):
                    fields = []
                    if (
                        download.volume_number != ""
                        and original.volume_number != ""
                        and download.volume_part == original.volume_part
                        and (
                            download.volume_number == original.volume_number
                            or (
                                (
                                    (
                                        (
                                            download.multi_volume
                                            and download.volume_number
                                            and not original.multi_volume
                                            and download.volume_number[0]
                                            == original.volume_number
                                        )
                                    )
                                    or (
                                        original.multi_volume
                                        and original.volume_number
                                        and not download.multi_volume
                                        and original.volume_number[0]
                                        == download.volume_number
                                    )
                                )
                                and allow_matching_single_volumes_with_multi_volumes
                            )
                        )
                    ):
                        if allow_matching_single_volumes_with_multi_volumes and (
                            (download.multi_volume and not original.multi_volume)
                            or (original.multi_volume and not download.multi_volume)
                        ):
                            send_change_message(
                                "\n\t\tallow_matching_single_volumes_with_multi_volumes=True"
                            )
                        upgrade_status = is_upgradeable(download, original)
                        original_file_tags = (
                            upgrade_status.current_ranked_result.keywords
                        )
                        if original_file_tags:
                            original_file_tags = ", ".join(
                                [
                                    tag.name + " (" + str(tag.score) + ")"
                                    for tag in upgrade_status.current_ranked_result.keywords
                                ]
                            )
                        else:
                            original_file_tags = "None"
                        downloaded_file_tags = (
                            upgrade_status.downloaded_ranked_result.keywords
                        )
                        if downloaded_file_tags:
                            downloaded_file_tags = ", ".join(
                                [
                                    tag.name + " (" + str(tag.score) + ")"
                                    for tag in upgrade_status.downloaded_ranked_result.keywords
                                ]
                            )
                        else:
                            downloaded_file_tags = "None"
                        fields = [
                            {
                                "name": "From:",
                                "value": "```" + original.name + "```",
                                "inline": False,
                            },
                            {
                                "name": "Score:",
                                "value": str(
                                    upgrade_status.current_ranked_result.total_score
                                ),
                                "inline": True,
                            },
                            {
                                "name": "Tags:",
                                "value": str(original_file_tags),
                                "inline": True,
                            },
                            {
                                "name": "To:",
                                "value": "```" + download.name + "```",
                                "inline": False,
                            },
                            {
                                "name": "Score:",
                                "value": str(
                                    upgrade_status.downloaded_ranked_result.total_score
                                ),
                                "inline": True,
                            },
                            {
                                "name": "Tags:",
                                "value": str(downloaded_file_tags),
                                "inline": True,
                            },
                        ]
                        if not upgrade_status.is_upgrade:
                            print(
                                "\t\tNOT UPGRADE: "
                                + download.name
                                + " is not an upgrade to: "
                                + original.name
                                + "\n\t\tDeleting: "
                                + download.name
                                + " from download folder."
                            )
                            send_discord_message(
                                None,
                                "Upgrade Process (Not Upgrade)",
                                color=16776960,
                                fields=fields,
                            )
                            if download in downloaded_releases:
                                downloaded_releases.remove(download)
                            remove_file(download.path)
                        else:
                            print(
                                "\t\tUPGRADE: "
                                + download.name
                                + " is an upgrade to: "
                                + original.name
                                + "\n\tUpgrading "
                                + original.name
                            )
                            send_discord_message(
                                None,
                                "Upgrade Process (Upgrade)",
                                color=65280,
                                fields=fields,
                            )
                            if download.multi_volume and not original.multi_volume:
                                for original_volume in original_releases[:]:
                                    for volume_number in download.volume_number:
                                        if (
                                            volume_number != original.volume_number
                                            and (
                                                original_volume.volume_number
                                                == volume_number
                                                and original_volume.volume_part
                                                == original.volume_part
                                            )
                                        ):
                                            remove_file(original_volume.path)
                                            original_releases.remove(original_volume)
                            replace_file(original, download)
                            moved_files.append(download)
                            if download in downloaded_releases:
                                downloaded_releases.remove(download)
                    elif (download.volume_number == original.volume_number) and (
                        (download.volume_number != "" and original.volume_number != "")
                        and (not download.volume_part and original.volume_part)
                    ):
                        upgrade_status = is_upgradeable(download, original)
                        if not upgrade_status.is_upgrade:
                            print(
                                "\t\tNOT UPGRADE: "
                                + download.name
                                + " is not an upgrade to: "
                                + original.name
                                + "\n\t\tDeleting: "
                                + download.name
                                + " from download folder."
                            )
                            send_discord_message(
                                None,
                                "Upgrade Process (Not Upgrade)",
                                color=16776960,
                                fields=fields,
                            )
                            if download in downloaded_releases:
                                downloaded_releases.remove(download)
                            remove_file(download.path)
                        else:
                            print(
                                "\t\tUPGRADE: "
                                + download.name
                                + " is an upgrade to: "
                                + original.name
                                + "\n\tUpgrading "
                                + original.name
                            )
                            send_discord_message(
                                None,
                                "Upgrade Process (Upgrade)",
                                color=65280,
                                fields=fields,
                            )
                            send_change_message(
                                "\t\tRemoving remaining part files with matching volume numbers:"
                            )
                            clone_original_releases = original_releases.copy()
                            clone_original_releases.remove(original)
                            for v in clone_original_releases:
                                if (
                                    (download.volume_number == v.volume_number)
                                    and (
                                        download.volume_number != ""
                                        and v.volume_number != ""
                                    )
                                    and (not download.volume_part and v.volume_part)
                                ):
                                    remove_file(v.path)
                                    original_releases.remove(v)
                            replace_file(original, download)
                            moved_files.append(download)
                            if download in downloaded_releases:
                                downloaded_releases.remove(download)


# Checks if the folder is empty, then deletes if it is
def check_and_delete_empty_folder(folder):
    try:
        print("\t\tChecking for empty folder: " + folder)
        delete_hidden_files(os.listdir(folder), folder)
        folder_contents = os.listdir(folder)
        remove_hidden_files(folder_contents, folder)
        if len(folder_contents) == 0 and (
            folder not in paths and folder not in download_folders
        ):
            try:
                print("\t\t\tRemoving empty folder: " + folder)
                os.rmdir(folder)
            except OSError as e:
                send_error_message(e)
    except Exception as e:
        send_error_message(e)


# Writes a log file
def write_to_file(
    file, message, without_date=False, overwrite=False, check_for_dup=False
):
    if log_to_file:
        message = re.sub("\t|\n", "", str(message), flags=re.IGNORECASE).strip()
        contains = False
        if check_for_dup and os.path.isfile(os.path.join(ROOT_DIR, file)):
            contains = check_text_file_for_message(
                os.path.join(ROOT_DIR, file), message
            )
        if not contains or overwrite:
            try:
                file_path = os.path.join(ROOT_DIR, file)
                append_write = ""
                if os.path.exists(file_path):
                    if not overwrite:
                        append_write = "a"  # append if already exists
                    else:
                        append_write = "w"
                else:
                    append_write = "w"  # make a new file if not
                try:
                    if append_write != "":
                        now = datetime.now()
                        dt_string = now.strftime("%d/%m/%Y %H:%M:%S")
                        file = open(file_path, append_write)
                        if without_date:
                            file.write("\n " + message)
                        else:
                            file.write("\n" + dt_string + " " + message)
                        file.close()
                except Exception as e:
                    send_error_message(e)
            except Exception as e:
                send_error_message(e)


# Checks for any missing volumes between the lowest volume of a series and the highest volume.
def check_for_missing_volumes():
    print("\nChecking for missing volumes...")
    paths_clean = [p for p in paths if p not in download_folders]
    for path in paths_clean:
        if os.path.exists(path):
            os.chdir(path)
            # get list of folders from path directory
            path_dirs = [f for f in os.listdir(path) if os.path.isdir(f)]
            clean_and_sort(path, dirs=path_dirs)
            global folder_accessor
            folder_accessor = Folder(
                path,
                path_dirs,
                os.path.basename(os.path.dirname(path)),
                os.path.basename(path),
                [""],
            )
            for dir in folder_accessor.dirs:
                current_folder_path = os.path.join(folder_accessor.root, dir)
                existing_dir_full_file_path = os.path.dirname(
                    os.path.join(folder_accessor.root, dir)
                )
                existing_dir = os.path.join(existing_dir_full_file_path, dir)
                clean_existing = os.listdir(existing_dir)
                clean_and_sort(existing_dir, clean_existing)
                existing_dir_volumes = upgrade_to_volume_class(
                    upgrade_to_file_class(
                        [
                            f
                            for f in clean_existing
                            if os.path.isfile(os.path.join(existing_dir, f))
                        ],
                        existing_dir,
                    )
                )
                for existing in existing_dir_volumes[:]:
                    if (
                        not isinstance(existing.volume_number, int)
                        and not isinstance(existing.volume_number, float)
                        and not isinstance(existing.volume_number, list)
                    ):
                        existing_dir_volumes.remove(existing)
                if len(existing_dir_volumes) >= 2:
                    volume_numbers = []
                    volume_numbers_second = []
                    for volume in existing_dir_volumes:
                        if volume.volume_number != "" or not volume.volume_number:
                            volume_numbers.append(volume.volume_number)
                    for item in volume_numbers:
                        if isinstance(item, list):
                            low_num = int(min(item))
                            high_num = int(max(item))
                            # get inbetween numbers
                            for i in range(low_num, high_num + 1):
                                if i not in volume_numbers_second:
                                    volume_numbers_second.append(i)
                            for i in item:
                                if i not in volume_numbers_second:
                                    volume_numbers_second.append(i)
                        else:
                            if item not in volume_numbers_second:
                                volume_numbers_second.append(item)
                    volume_numbers_second.sort()
                    if len(volume_numbers_second) >= 2:
                        lowest_volume_number = 1
                        highest_volume_number = int(max(volume_numbers_second))
                        volume_num_range = list(
                            range(lowest_volume_number, highest_volume_number + 1)
                        )
                        for number in volume_numbers_second:
                            if number in volume_num_range:
                                volume_num_range.remove(number)
                        if len(volume_num_range) != 0:
                            for number in volume_num_range:
                                message = (
                                    "\t"
                                    + os.path.basename(current_folder_path)
                                    + ": Volume "
                                    + str(number)
                                )
                                if volume.extension == ".cbz":
                                    message += " [MANGA]"
                                elif volume.extension == ".epub":
                                    message += " [NOVEL]"
                                print(message)
                                write_to_file("missing_volumes.txt", message)


# Renames the file.
def rename_file(
    src, dest, root, extensionless_filename_src, extensionless_filename_dst
):
    if os.path.isfile(src):
        print("\n\t\tRenaming " + src)
        try:
            os.rename(src, dest)
        except Exception as e:
            send_error_message(e)
        if os.path.isfile(dest):
            print(
                "\t\t"
                + extensionless_filename_src
                + " was renamed to "
                + extensionless_filename_dst
            )
            # if not mute_discord_rename_notifications:
            # send_discord_message(
            #     "From: "
            #     + "```"
            #     + extensionless_filename_src
            #     + "```"
            #     + "To:"
            #     + "```"
            #     + extensionless_filename_dst
            #     + "```",
            #     "Renamed File",
            #     color=8421504,
            # )
            for image_extension in image_extensions:
                image_file = extensionless_filename_src + "." + image_extension
                image_file_rename = extensionless_filename_dst + "." + image_extension
                if os.path.isfile(os.path.join(root, image_file)):
                    try:
                        os.rename(
                            os.path.join(root, image_file),
                            os.path.join(root, image_file_rename),
                        )
                    except Exception as e:
                        send_error_message(e)
        else:
            send_error_message(
                "Failed to rename " + src + " to " + dest + "\n\tERROR: " + str(e)
            )


def reorganize_and_rename(files, dir):
    global manual_rename
    base_dir = os.path.basename(dir)
    for file in files:
        try:
            if re.search(
                r"(\b(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)([-_.]|)(([0-9]+)((([-_.]|)([0-9]+))+|))(\s|\.epub|\.cbz))",
                file.name,
                re.IGNORECASE,
            ):
                comic_info_xml = ""
                epub_info_html = ""
                if file.extension == ".cbz":
                    contains_comic_info = check_if_zip_file_contains_comic_info_xml(
                        file.path
                    )
                    if contains_comic_info:
                        comicinfo = get_file_from_zip(
                            file.path, "comicinfo.xml", allow_base=False
                        )
                        tags = None
                        if comicinfo:
                            comicinfo = comicinfo.decode("utf-8")
                            # not parsing pages correctly
                            comic_info_xml = parse_comicinfo_xml(comicinfo)
                elif file.extension == ".epub":
                    epub_content_opf = get_file_from_zip(file.path, "content.opf")
                    epub_package_opf = get_file_from_zip(file.path, "package.opf")
                    if epub_content_opf:
                        epub_info_html = parse_html_tags(epub_content_opf)
                    elif epub_package_opf:
                        epub_info_html = parse_html_tags(epub_package_opf)
                release_year_from_file = ""
                publisher = ""
                if comic_info_xml:
                    if "Year" in comic_info_xml:
                        release_year_from_file = comic_info_xml["Year"]
                        if release_year_from_file and release_year_from_file.isdigit():
                            release_year_from_file = int(release_year_from_file)
                    if "Publisher" in comic_info_xml:
                        publisher = titlecase(comic_info_xml["Publisher"])
                        publisher = remove_dual_space(publisher)
                        publisher = re.sub(r", LLC.*", "", publisher)
                elif epub_info_html:
                    if "dc:date" in epub_info_html:
                        release_year_from_file = epub_info_html["dc:date"].strip()
                        release_year_from_file = re.search(
                            r"\d{4}", release_year_from_file
                        )
                        if release_year_from_file:
                            release_year_from_file = release_year_from_file.group(0)
                            if (
                                release_year_from_file
                                and release_year_from_file.isdigit()
                            ):
                                release_year_from_file = int(release_year_from_file)
                    if "dc:publisher" in epub_info_html:
                        publisher = titlecase(epub_info_html["dc:publisher"])
                        publisher = remove_dual_space(publisher)
                        publisher = re.sub(r", LLC.*", "", publisher).strip()
                        publisher = re.sub(r"LLC", "", publisher).strip()
                rename = ""
                rename += base_dir
                rename += " " + preferred_volume_renaming_format
                number = None
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
                    if not isinstance(number, str) and number.is_integer():
                        if number < 10:
                            volume_number = str(int(number)).zfill(2)
                            number_string += volume_number
                        else:
                            volume_number = str(int(number))
                            number_string += volume_number
                    elif isinstance(number, float):
                        if number < 10:
                            volume_number = str(number).zfill(4)
                            number_string += volume_number
                        else:
                            volume_number = str(number)
                            number_string += volume_number
                    elif isinstance(number, str) and number == "-":
                        number_string += "-"
                if number_string:
                    rename += number_string
                if (
                    number_string
                    and add_issue_number_to_cbz_file_name
                    and file.extension == ".cbz"
                ):
                    rename += " #" + number_string
                if file.extension == ".cbz":
                    if isinstance(file.volume_year, int):
                        rename += " (" + str(file.volume_year) + ")"
                    elif release_year_from_file and isinstance(
                        release_year_from_file, int
                    ):
                        file.volume_year = release_year_from_file
                        rename += " (" + str(file.volume_year) + ")"
                elif file.extension == ".epub":
                    if isinstance(file.volume_year, int):
                        rename += " [" + str(file.volume_year) + "]"
                    elif release_year_from_file and isinstance(
                        release_year_from_file, int
                    ):
                        file.volume_year = release_year_from_file
                        rename += " [" + str(file.volume_year) + "]"
                if publisher:
                    for item in file.extras[:]:
                        score = similar(
                            re.sub(
                                r"(Entertainment|Pictures?|LLC|Americas?|USA?|International|Books?|Comics?|Media|Club|On|Press|Enix Manga|Enix|[-_.,\(\[\{\)\]\}])",
                                "",
                                item,
                            ).strip(),
                            re.sub(
                                r"(Entertainment|Pictures?|LLC|Americas?|USA?|International|Books?|Comics?|Media|Club|On|Press|Enix Manga|Enix|[-_.,\(\[\{\)\]\}])",
                                "",
                                publisher,
                            ).strip(),
                        )
                        if (
                            re.search(
                                publisher,
                                item,
                                re.IGNORECASE,
                            )
                            or score >= 0.90
                        ):
                            file.extras.remove(item)
                    if add_publisher_name_to_file_name_when_renaming:
                        if file.extension == ".cbz":
                            rename += " (" + publisher + ")"
                        elif file.extension == ".epub":
                            rename += " [" + publisher + "]"
                if file.volume_year:
                    for item in file.extras[:]:
                        score = similar(
                            item,
                            str(file.volume_year),
                        )
                        if (
                            re.search(
                                str(file.volume_year),
                                item,
                                re.IGNORECASE,
                            )
                            or score >= required_similarity_score
                            or re.search(r"([\[\(\{]\d{4}[\]\)\}])", item)
                        ):
                            file.extras.remove(item)
                if release_year_from_file:
                    for item in file.extras[:]:
                        score = similar(
                            item,
                            str(release_year_from_file),
                        )
                        if (
                            re.search(
                                str(release_year_from_file),
                                item,
                                re.IGNORECASE,
                            )
                            or score >= required_similarity_score
                            or re.search(r"([\[\(\{]\d{4}[\]\)\}])", item)
                        ):
                            file.extras.remove(item)
                if file.release_group and file.release_group != publisher:
                    for item in file.extras[:]:
                        # escape any regex characters
                        item_escaped = re.escape(item)
                        score = similar(
                            item,
                            file.release_group,
                        )
                        left_brackets = r"(\(|\[|\{)"
                        right_brackets = r"(\)|\]|\})"
                        if (
                            re.search(
                                rf"{left_brackets}{item_escaped}{right_brackets}",
                                file.release_group,
                                re.IGNORECASE,
                            )
                            or score >= release_group_similarity_score
                        ):
                            file.extras.remove(item)
                if len(file.extras) != 0:
                    for extra in file.extras:
                        rename += " " + extra
                release_group_escaped = None
                if file.release_group:
                    release_group_escaped = re.escape(file.release_group)
                if release_group_escaped and not re.search(
                    rf"\b{release_group_escaped}\b", rename, re.IGNORECASE
                ):
                    if file.extension == ".cbz":
                        rename += " (" + file.release_group + ")"
                    elif file.extension == ".epub":
                        rename += " [" + file.release_group + "]"
                rename += file.extension
                if file.name != rename:
                    processed_files.append(rename)
                    try:
                        print("\n\t\tOriginal: " + file.name)
                        print("\t\tRename:   " + rename)
                        if not manual_rename:
                            rename_file(
                                file.path,
                                os.path.join(file.root, rename),
                                file.root,
                                file.extensionless_name,
                                get_extensionless_name(rename),
                            )
                            print("\n\t\tRenamed: " + file.name + " to \n\t\t" + rename)
                            if not mute_discord_rename_notifications:
                                send_discord_message(
                                    "From: "
                                    + "```"
                                    + file.name
                                    + "```"
                                    + "To:"
                                    + "```"
                                    + rename
                                    + "```",
                                    "Reorganized & Renamed File",
                                    color=8421504,
                                )
                        else:
                            user_input = input("\tRename (y or n): ")
                            if (
                                user_input.lower().strip() == "y"
                                or user_input.lower().strip() == "yes"
                            ):
                                rename_file(
                                    file.path,
                                    os.path.join(file.root, rename),
                                    file.root,
                                    file.extensionless_name,
                                    get_extensionless_name(rename),
                                )
                                print(
                                    "\n\t\tRenamed: "
                                    + file.name
                                    + " to \n\t\t"
                                    + rename
                                )
                                if not mute_discord_rename_notifications:
                                    send_discord_message(
                                        "From: "
                                        + "```"
                                        + file.name
                                        + "```"
                                        + "To:"
                                        + "```"
                                        + rename
                                        + "```",
                                        "Renamed File",
                                        color=8421504,
                                    )
                        file.series_name = get_series_name_from_file_name(
                            rename, file.root
                        )
                        file.volume_year = get_volume_year(rename)
                        file.volume_number = remove_everything_but_volume_num([rename])
                        file.name = rename
                        file.extensionless_name = get_extensionless_name(rename)
                        file.basename = os.path.basename(rename)
                        file.path = os.path.join(file.root, rename)
                        file.extensionless_path = os.path.splitext(file.path)[0]
                        file.extras = get_extras(rename, file.root)
                    except OSError as ose:
                        send_error_message(ose)
        except Exception as e:
            send_error_message(
                "Failed to rename "
                + file.name
                + ": "
                + str(e)
                + " with reoganize_and_rename"
            )


# Replaces any pesky double spaces
def remove_dual_space(s):
    return re.sub("(\s{2,})", " ", s)


# Removes common words to improve string matching accuracy between a series_name
# from a file name, and a folder name, useful for when releasers sometimes include them,
# and sometimes don't.
def remove_common_words(s):
    if len(s) > 1:
        common_words_to_remove = [
            "the",
            "a",
            "à",
            "and",
            "&",
            "I",
            "Complete",
            "Series",
            "of",
            "Novel",
            "Light Novel",
            "Manga",
            "Comic",
            "Collection",
            "Master Edition",
            "Edition",
            "Exclusive",
            "((\d+)([-_. ]+)?th)",
            "Anniversary",
            "Deluxe",
            "Omnibus",
            "Digital",
            "Official",
            "Anthology",
            "LN",
            "wa",
            "o",
            "mo",
            "ni",
            "e",
            "de",
            "ga",
            "kara",
            "to",
            "ya",
            "no",
            "ne",
            "yo",
        ]
        for word in common_words_to_remove:
            s = re.sub(rf"\b{word}\b", " ", s, flags=re.IGNORECASE).strip()
            s = remove_dual_space(s)
    return s.strip()


# Replaces all numbers
def remove_numbers(s):
    return re.sub("([0-9]+)", "", s, flags=re.IGNORECASE)


# Returns a string without punctuation.
def remove_punctuation(s, disable_lang=False):
    s = re.sub(r":", "", s)
    language = ""
    if not disable_lang and not s.isdigit():
        language = detect_language(s)
    if language and language != "en" and not disable_lang:
        return remove_dual_space(remove_common_words(re.sub(r"[^\w\s+]", " ", s)))
    else:
        return convert_to_ascii(
            unidecode(
                remove_dual_space(remove_common_words(re.sub(r"[^\w\s+]", " ", s)))
            )
        )


# detect language of the passed string using langdetect
def detect_language(s):
    language = ""
    if s:
        try:
            language = detect(s)
        except Exception as e:
            send_error_message(e)
            return language
    return language


# convert string to acsii
def convert_to_ascii(s):
    return "".join(i for i in s if ord(i) < 128)


class Result:
    def __init__(self, dir, score):
        self.dir = dir
        self.score = score


# gets the toc.xhtml or copyright.xhtml file from the epub file and checks for premium content
def get_toc_or_copyright(file):
    bonus_content_found = False
    with zipfile.ZipFile(file, "r") as zf:
        for name in zf.namelist():
            if os.path.basename(name) == "toc.xhtml":
                toc_file = zf.open(name)
                toc_file_contents = toc_file.read()
                lines = toc_file_contents.decode("utf-8")
                search = re.search(
                    r"(Bonus\s+((Color\s+)?Illustrations?|(Short\s+)?Stories))",
                    lines,
                )
                if search:
                    bonus_content_found = search.group(0)
                    break
            elif os.path.basename(name) == "copyright.xhtml":
                cop_file = zf.open(name)
                cop_file_contents = cop_file.read()
                lines = cop_file_contents.decode("utf-8")
                search = re.search(
                    r"(Premium(\s)+(E?-?Book|Epub))", lines, re.IGNORECASE
                )
                if search:
                    bonus_content_found = search.group(0)
                    break
    return bonus_content_found


# Checks for and returns the path of an associated cover file
def find_cover(file):
    files = os.listdir(file.root)
    remove_hidden_files(files, file.root)
    # remove any files from files with list comprehension that don't start with file.extensionless_name
    if files:
        files = [f for f in files if f.startswith(file.extensionless_name)]
        for f in files:
            for extension in image_extensions:
                search = file.extensionless_name + "." + extension
                if f == search:
                    return os.path.join(file.root, f)
    return None


def check_upgrade(
    existing_root, dir, file, similarity_strings=None, cache=False, isbn=False
):
    global moved_files
    global new_volume_webhook
    existing_dir = os.path.join(existing_root, dir)
    clean_existing = os.listdir(existing_dir)
    clean_and_sort(existing_dir, clean_existing)
    cbz_percent_download_folder = get_cbz_percent_for_folder([file.name])
    cbz_percent_existing_folder = get_cbz_percent_for_folder(clean_existing)
    epub_percent_download_folder = get_epub_percent_for_folder([file.name])
    epub_percent_existing_folder = get_epub_percent_for_folder(clean_existing)
    print(
        "\t\tRequired Folder Matching Percent: "
        + str(required_matching_percentage)
        + "%"
    )
    print(
        "\t\t\tDownload Folder CBZ Percent: " + str(cbz_percent_download_folder) + "%"
    )
    print(
        "\t\t\tExisting Folder CBZ Percent: " + str(cbz_percent_existing_folder) + "%"
    )
    print(
        "\t\t\tDownload Folder EPUB Percent: " + str(epub_percent_download_folder) + "%"
    )
    print(
        "\t\t\tExisting Folder EPUB Percent: " + str(epub_percent_existing_folder) + "%"
    )
    if (
        (cbz_percent_download_folder and cbz_percent_existing_folder)
        >= required_matching_percentage
    ) or (
        (epub_percent_download_folder and epub_percent_existing_folder)
        >= required_matching_percentage
    ):
        download_dir_volumes = [file]
        if resturcture_when_renaming:
            reorganize_and_rename(download_dir_volumes, existing_dir)
        existing_dir_volumes = upgrade_to_volume_class(
            upgrade_to_file_class(
                [
                    f
                    for f in clean_existing
                    if os.path.isfile(os.path.join(existing_dir, f))
                ],
                existing_dir,
            )
        )
        fields = []
        if similarity_strings:
            if not isbn:
                fields = [
                    {
                        "name": "Existing Series Location:",
                        "value": "```" + existing_dir + "```",
                        "inline": False,
                    },
                    {
                        "name": "Downloaded File Series Name:",
                        "value": "```" + similarity_strings[0] + "```",
                        "inline": True,
                    },
                    {
                        "name": "Existing Library Folder Name:",
                        "value": "```" + similarity_strings[1] + "```",
                        "inline": False,
                    },
                    {
                        "name": "Similarity Score:",
                        "value": "```" + str(similarity_strings[2]) + "```",
                        "inline": True,
                    },
                    {
                        "name": "Required Score:",
                        "value": "```>=" + str(similarity_strings[3]) + "```",
                        "inline": True,
                    },
                ]
            else:
                if similarity_strings and len(similarity_strings) >= 2:
                    fields = [
                        {
                            "name": "Existing Series Location:",
                            "value": "```" + existing_dir + "```",
                            "inline": False,
                        },
                        {
                            "name": "Identifiers:",
                            "value": "Downloaded File:"
                            + "```"
                            + str(similarity_strings[0])
                            + "```"
                            + "Existing Library:"
                            + "```"
                            + str(similarity_strings[1])
                            + "```",
                            "inline": True,
                        },
                    ]
                else:
                    send_error_message(
                        "Error: similarity_strings is not long enough to be valid."
                        + str(similarity_strings)
                        + " File: "
                        + file.name
                    )
        if cache:
            print("\n\t\tFound existing series from cache: " + existing_dir)
            if fields:
                send_discord_message(
                    None, "Found Series Match (CACHE)", color=8421504, fields=fields
                )
        elif isbn:
            print("\n\t\tFound existing series: " + existing_dir)
            if fields:
                send_discord_message(
                    None,
                    "Found Series Match (Matching Identifier)",
                    color=8421504,
                    fields=fields,
                )
        else:
            print("\n\t\tFound existing series: " + existing_dir)
            if fields:
                send_discord_message(
                    None, "Found Series Match", color=8421504, fields=fields
                )
        remove_duplicate_releases_from_download(
            existing_dir_volumes,
            download_dir_volumes,
        )
        if len(download_dir_volumes) != 0:
            volume = download_dir_volumes[0]
            if isinstance(
                volume.volume_number,
                float,
            ) or isinstance(volume.volume_number, list):
                print(
                    "\t\t\tVolume "
                    + str(volume.volume_number)
                    + ": "
                    + volume.name
                    + " does not exist in: "
                    + existing_dir
                    + "\n\t\t\tMoving: "
                    + volume.name
                    + " to "
                    + existing_dir
                )
                cover = find_and_extract_cover(volume, return_data_only=True)
                fields = [
                    {
                        "name": "Volume Number(s)",
                        "value": "```" + str(volume.volume_number) + "```",
                        "inline": False,
                    },
                    {
                        "name": "Volume Name",
                        "value": "```" + volume.name + "```",
                        "inline": False,
                    },
                ]
                if cover:
                    send_discord_message(
                        None,
                        "New Volume Release",
                        color=65280,
                        fields=fields,
                        image_local=cover,
                        passed_webhook=new_volume_webhook,
                    )
                else:
                    send_discord_message(
                        None,
                        "New Volume Release",
                        color=65280,
                        fields=fields,
                        passed_webhook=new_volume_webhook,
                    )
                # send_discord_message(
                #     "File: "
                #     + "```"
                #     + volume.name
                #     + "```"
                #     + "To: "
                #     + "```"
                #     + existing_dir
                #     + "```",
                #     "Moving File",
                #     color=8421504,
                # )
                move_file(volume, existing_dir)
                moved_files.append(volume)
                check_and_delete_empty_folder(volume.root)
                return True
        else:
            check_and_delete_empty_folder(file.root)
            return True
    else:
        print("\t\tNo match found.")
        return False


# remove duplicates elements from the passed in list
def remove_duplicates(items):
    return list(dict.fromkeys(items))


# Return the zip comment for the passed zip file
def get_zip_comment(zip_file):
    try:
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            if zip_ref.comment:
                return zip_ref.comment.decode("utf-8")
            else:
                return ""
    except Exception as e:
        send_error_message(str(e))
        send_error_message("\tFailed to get zip comment for: " + zip_file)
        write_to_file("errors.txt", str(e))
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        print(exc_type, fname, exc_tb.tb_lineno)
        return ""


# Removes bracket info from the end of a string.
def remove_bracketed_info_from_name(name):
    return re.sub(r"([\{\[\(][^\)\]\}]*[\)\]\}])", "", name).strip()


# Used for printing the total execution time in seconds.
# Useful for when testing new code changes intended
# to speedup the existing code.
def print_execution_time(start_time, extra_msg=None):
    message = ""
    if len(processed_files) > 1:
        if extra_msg:
            message = (
                " seconds for "
                + str(len(processed_files))
                + " files, with all features, for: "
                + message
            )
        else:
            message = (
                " seconds for "
                + str(len(processed_files))
                + " files, with all features."
            )
    send_change_message(
        "\n\t\t\tTotal execution time: "
        + str(
            round(
                (time.time() - start_time),
                2,
            )
        )
        + message
        + "\n"
        + str(processed_files)
    )


# Checks for any duplicate volumes and deletes the lower ranking one.
def check_for_duplicate_volumes(paths_to_search=[]):
    try:
        for p in paths_to_search:
            if os.path.exists(p):
                print("\nSearching " + p + " for duplicate volumes...")
                for root, dirs, files in scandir.walk(p):
                    clean_and_sort(root, files, dirs)
                    volumes = upgrade_to_volume_class(
                        upgrade_to_file_class(
                            [f for f in files if os.path.isfile(os.path.join(root, f))],
                            root,
                        )
                    )
                    for file in volumes:
                        try:
                            if os.path.isfile(file.path):
                                print("\n\tChecking: " + file.name)
                                volume_series_name = (
                                    remove_underscore_from_name(
                                        remove_punctuation(
                                            remove_bracketed_info_from_name(
                                                file.series_name
                                            )
                                        )
                                    )
                                    .lower()
                                    .strip()
                                )
                                for root, dirs, files in scandir.walk(file.root):
                                    clean_and_sort(root, files, dirs)
                                    compare_volumes = upgrade_to_volume_class(
                                        upgrade_to_file_class(
                                            [
                                                f
                                                for f in files
                                                if os.path.isfile(os.path.join(root, f))
                                            ],
                                            root,
                                        )
                                    )
                                    for compare_file in compare_volumes:
                                        try:
                                            if (
                                                os.path.isfile(compare_file.path)
                                                and file.name != compare_file.name
                                            ):
                                                print(
                                                    "\t\tAgainst:  " + compare_file.name
                                                )
                                                compare_volume_series_name = (
                                                    (
                                                        remove_underscore_from_name(
                                                            remove_punctuation(
                                                                remove_bracketed_info_from_name(
                                                                    compare_file.series_name
                                                                )
                                                            )
                                                        )
                                                    )
                                                    .lower()
                                                    .strip()
                                                )
                                                if (
                                                    file.root == compare_file.root
                                                    and (
                                                        file.volume_number
                                                        and compare_file.volume_number
                                                    )
                                                    and file.volume_number
                                                    == compare_file.volume_number
                                                    and file.volume_part
                                                    == compare_file.volume_part
                                                    and file.extension
                                                    == compare_file.extension
                                                    and similar(
                                                        volume_series_name,
                                                        compare_volume_series_name,
                                                    )
                                                    >= required_similarity_score
                                                ):
                                                    main_file_upgrade_status = (
                                                        is_upgradeable(
                                                            file, compare_file
                                                        ).is_upgrade
                                                    )
                                                    compare_file_upgrade_status = (
                                                        is_upgradeable(
                                                            compare_file, file
                                                        ).is_upgrade
                                                    )
                                                    if main_file_upgrade_status:
                                                        print(
                                                            "\n\t\tDuplicate volume found in: "
                                                            + file.root
                                                            + "\n\t\tDuplicate: "
                                                            + compare_file.name
                                                            + " has a lower score than "
                                                            + file.name
                                                            + "\n\t\tDeleting: "
                                                            + compare_file.name
                                                            + " inside of "
                                                            + compare_file.root
                                                        )
                                                        send_discord_message(
                                                            "Location: "
                                                            + "```"
                                                            + file.root
                                                            + "```"
                                                            + "Duplicate: "
                                                            + "```"
                                                            + compare_file.name
                                                            + "```"
                                                            + "has a lower score than: "
                                                            + "```"
                                                            + file.name
                                                            + "```",
                                                            "Duplicate Volume (NOT UPGRADE)",
                                                            color=16776960,
                                                        )
                                                        send_discord_message(
                                                            "File: "
                                                            + "```"
                                                            + compare_file.name
                                                            + "```"
                                                            + "Location: "
                                                            + "```"
                                                            + compare_file.root
                                                            + "```",
                                                            "Deleting File",
                                                            color=16711680,
                                                        )
                                                        if not manual_delete:
                                                            remove_file(
                                                                compare_file.path
                                                            )
                                                        elif (
                                                            input(
                                                                "\t\t\tDelete: "
                                                                + compare_file.name
                                                                + "? (y/n): "
                                                            )
                                                            == "y"
                                                        ):
                                                            remove_file(
                                                                compare_file.path
                                                            )
                                                        else:
                                                            send_change_message(
                                                                "\t\t\tBased on user input, Skipping: "
                                                                + compare_file.name
                                                            )
                                                        check_and_delete_empty_folder(
                                                            compare_file.root
                                                        )
                                                    elif compare_file_upgrade_status:
                                                        print(
                                                            "\n\t\tDuplicate volume found in: "
                                                            + compare_file.root
                                                            + "\n\t\tDuplicate: "
                                                            + file.name
                                                            + " has a lower score than "
                                                            + compare_file.name
                                                            + "\n\t\tDeleting: "
                                                            + file.name
                                                            + " inside of "
                                                            + file.root
                                                        )
                                                        send_discord_message(
                                                            "Location: "
                                                            + "```"
                                                            + compare_file.root
                                                            + "```"
                                                            + "Duplicate: "
                                                            + "```"
                                                            + file.name
                                                            + "```"
                                                            + "has a lower score than: "
                                                            + "```"
                                                            + compare_file.name
                                                            + "```",
                                                            "Duplicate Volume (NOT UPGRADE)",
                                                            color=16776960,
                                                        )
                                                        if not manual_delete:
                                                            remove_file(file.path)
                                                        elif (
                                                            input(
                                                                "\t\t\tDelete: "
                                                                + file.name
                                                                + "? (y/n): "
                                                            )
                                                            == "y"
                                                        ):
                                                            remove_file(file.path)
                                                        else:
                                                            send_change_message(
                                                                "\t\t\tBased on user input, Skipping: "
                                                                + file.name
                                                            )
                                                        check_and_delete_empty_folder(
                                                            file.root
                                                        )
                                                    else:
                                                        print(
                                                            "\n\t\tDuplicate found in: "
                                                            + compare_file.root
                                                            + "\n\t\t\t"
                                                            + file.name
                                                            + "\n\t\t\t"
                                                            + compare_file.name
                                                            + "\n\t\t\t\tRanking scores are equal, REQUIRES MANUAL DECISION."
                                                        )
                                                        send_discord_message(
                                                            "Location: "
                                                            + "```"
                                                            + compare_file.root
                                                            + "```"
                                                            + "Duplicate: "
                                                            + "```"
                                                            + file.name
                                                            + "```"
                                                            + "has an equal score to: "
                                                            + "```"
                                                            + compare_file.name
                                                            + "```",
                                                            "Duplicate Volume (REQUIRES MANUAL DECISION)",
                                                            color=16776960,
                                                        )
                                                        print("\t\t\t\tSkipping...")
                                        except Exception as e:
                                            send_error_message(
                                                "\n\t\tError: "
                                                + str(e)
                                                + "\n\t\tSkipping: "
                                                + compare_file.name
                                            )
                                            continue
                        except Exception as e:
                            send_error_message(
                                "\n\tError: " + str(e) + "\n\tSkipping: " + file.name
                            )
                            continue
            else:
                print("\n\tPath does not exist: " + p)
    except Exception as e:
        send_error_message("\n\tError: " + str(e))


# regex out underscore from passed string and return it
def remove_underscore_from_name(name):
    name = re.sub(r"_", " ", name)
    name = remove_dual_space(name).strip()
    return name


# Reorganizes the passed array list by pulling the first letter of the string passed
# and inserting all matched items into the passed position of the array list
def organize_array_list_by_first_letter(
    array_list, string, position_to_insert_at, exclude=None
):
    if string:
        first_letter_of_file_name = string[0]
        for item in array_list:
            if item != exclude or not exclude:
                name = os.path.basename(item)
                first_letter_of_dir = name[0]
                if (
                    first_letter_of_dir.lower() == first_letter_of_file_name.lower()
                    and item != array_list[position_to_insert_at]
                ):
                    array_list.remove(item)
                    array_list.insert(position_to_insert_at, item)
    else:
        send_error_message(
            "First letter of file name was not found, skipping reorganization of array list."
        )
    return array_list


class IdentifierResult:
    def __init__(self, series_name, identifiers, path, matches):
        self.series_name = series_name
        self.identifiers = identifiers
        self.path = path
        self.matches = matches


# Checks for an existing series by pulling the series name from each elidable file in the downloads_folder
# and comparing it to an existin folder within the user's library.
def check_for_existing_series():
    global cached_paths
    global cached_identifier_results
    if download_folders:
        print("\nChecking download folders for items to match to existing library...")
        for download_folder in download_folders:
            if os.path.exists(download_folder):
                for root, dirs, files in scandir.walk(download_folder):
                    clean_and_sort(root, files, dirs)
                    volumes = upgrade_to_volume_class(
                        upgrade_to_file_class(
                            [f for f in files if os.path.isfile(os.path.join(root, f))],
                            root,
                        )
                    )
                    exclude = None
                    for file in volumes:
                        if (
                            file.name in processed_files or not processed_files
                        ) and os.path.isfile(file.path):
                            if (
                                cached_identifier_results
                                and match_through_isbn_or_series_id
                            ):
                                found = False
                                for cached_identifier in cached_identifier_results:
                                    if (
                                        cached_identifier.series_name
                                        == file.series_name
                                    ):
                                        check_upgrade(
                                            os.path.dirname(cached_identifier.path),
                                            os.path.basename(cached_identifier.path),
                                            file,
                                            similarity_strings=cached_identifier.matches,
                                            isbn=True,
                                        )
                                        if cached_identifier.path not in cached_paths:
                                            cached_paths.append(cached_identifier.path)
                                            write_to_file(
                                                "cached_paths.txt",
                                                cached_identifier.path,
                                                without_date=True,
                                                check_for_dup=False,
                                            )
                                        found = True
                                        break
                                if found:
                                    continue
                            if cached_paths:
                                if exclude:
                                    cached_paths = organize_array_list_by_first_letter(
                                        cached_paths, file.name, 1, exclude
                                    )
                                else:
                                    cached_paths = organize_array_list_by_first_letter(
                                        cached_paths, file.name, 1
                                    )
                            downloaded_file_series_name = (
                                (str(file.series_name)).lower()
                            ).strip()
                            downloaded_file_series_name = (
                                (
                                    remove_underscore_from_name(
                                        remove_punctuation(downloaded_file_series_name)
                                    )
                                )
                                .strip()
                                .lower()
                            )
                            if (
                                cached_paths
                                and file.name != downloaded_file_series_name
                            ):
                                if exclude:
                                    cached_paths = organize_array_list_by_first_letter(
                                        cached_paths,
                                        downloaded_file_series_name,
                                        2,
                                        exclude,
                                    )
                                else:
                                    cached_paths = organize_array_list_by_first_letter(
                                        cached_paths,
                                        downloaded_file_series_name,
                                        2,
                                    )
                            done = False
                            if cached_paths:
                                print("\n\tChecking cached paths for match...")
                                for p in cached_paths:
                                    if (
                                        os.path.exists(p)
                                        and os.path.isdir(p)
                                        and p not in download_folders
                                    ):
                                        successful_file_series_name = (
                                            (str(os.path.basename(p))).lower()
                                        ).strip()
                                        successful_file_series_name = (
                                            (
                                                remove_underscore_from_name(
                                                    remove_punctuation(
                                                        successful_file_series_name
                                                    )
                                                )
                                            )
                                            .strip()
                                            .lower()
                                        )
                                        successful_similarity_score = similar(
                                            successful_file_series_name,
                                            downloaded_file_series_name,
                                        )
                                        print(
                                            "\t\tCHECKING: "
                                            + downloaded_file_series_name
                                            + "\n\t\tAGAINST:  "
                                            + successful_file_series_name
                                            + "\n\t\tSCORE: "
                                            + str(successful_similarity_score)
                                            + "\n"
                                        )
                                        if (
                                            successful_similarity_score
                                            >= required_similarity_score
                                        ):
                                            write_to_file(
                                                "changes.txt",
                                                (
                                                    '\t\tSimilarity between: "'
                                                    + successful_file_series_name
                                                    + '" and "'
                                                    + downloaded_file_series_name
                                                    + '"'
                                                ),
                                            )
                                            write_to_file(
                                                "changes.txt",
                                                (
                                                    "\tSimilarity Score: "
                                                    + str(successful_similarity_score)
                                                    + " out of 1.0"
                                                ),
                                            )
                                            print(
                                                '\t\tSimilarity between: "'
                                                + successful_file_series_name
                                                + '" and "'
                                                + downloaded_file_series_name
                                                + '" Score: '
                                                + str(successful_similarity_score)
                                                + " out of 1.0\n"
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
                                            )
                                            if done:
                                                if p not in cached_paths:
                                                    cached_paths.append(p)
                                                    write_to_file(
                                                        "cached_paths.txt",
                                                        p,
                                                        without_date=True,
                                                        check_for_dup=False,
                                                    )
                                                if (
                                                    len(volumes) > 1
                                                    and p in cached_paths
                                                    and p != cached_paths[0]
                                                ):
                                                    cached_paths.remove(p)
                                                    cached_paths.insert(0, p)
                                                    exclude = p
                                                break
                            if done:
                                continue
                            download_file_zip_comment = get_zip_comment(file.path)
                            download_file_meta = None
                            if download_file_zip_comment and re.search(
                                r"Identifiers", download_file_zip_comment, re.IGNORECASE
                            ):
                                # split on Identifiers: and only keep the second half
                                download_file_zip_comment = (
                                    download_file_zip_comment.split("Identifiers:")[1]
                                ).strip()
                                if re.search(r",", download_file_zip_comment):
                                    download_file_meta = (
                                        download_file_zip_comment.split(",")
                                    )
                                else:
                                    download_file_meta = [
                                        download_file_zip_comment,
                                    ]
                                if download_file_meta:
                                    download_file_meta = [
                                        x
                                        for x in download_file_meta
                                        if not re.search(r"NONE", x, re.IGNORECASE)
                                    ]
                                # strip whitespace from each item in the list
                                download_file_meta = [
                                    x.strip() for x in download_file_meta
                                ]
                            directories_found = []
                            matched_ids = []
                            for path in paths:
                                if (
                                    os.path.exists(path)
                                    and not done
                                    and path not in download_folders
                                ):
                                    try:
                                        os.chdir(path)
                                        for root, dirs, files in scandir.walk(path):
                                            clean_and_sort(root, files, dirs)
                                            file_objects = upgrade_to_file_class(
                                                files, root
                                            )
                                            global folder_accessor
                                            folder_accessor = Folder(
                                                root,
                                                dirs,
                                                os.path.basename(os.path.dirname(root)),
                                                os.path.basename(root),
                                                file_objects,
                                            )
                                            if (
                                                root not in cached_paths
                                                and root not in paths
                                                and root not in download_folders
                                            ):
                                                if path not in download_folders:
                                                    if (
                                                        cached_paths
                                                        and root not in cached_paths
                                                    ):
                                                        write_to_file(
                                                            "cached_paths.txt",
                                                            root,
                                                            without_date=True,
                                                            check_for_dup=True,
                                                        )
                                                    else:
                                                        write_to_file(
                                                            "cached_paths.txt",
                                                            root,
                                                            without_date=True,
                                                            check_for_dup=True,
                                                        )
                                                if done:
                                                    break
                                                print("\nFile: " + file.name)
                                                print(
                                                    "Looking for: " + file.series_name
                                                )
                                                print(
                                                    "\tInside of: "
                                                    + folder_accessor.root
                                                )
                                                for dir in folder_accessor.dirs:
                                                    existing_series_folder_from_library = (
                                                        (
                                                            remove_underscore_from_name(
                                                                remove_punctuation(
                                                                    remove_bracketed_info_from_name(
                                                                        dir
                                                                    )
                                                                )
                                                            )
                                                        )
                                                        .strip()
                                                        .lower()
                                                    )
                                                    similarity_score = similar(
                                                        existing_series_folder_from_library,
                                                        downloaded_file_series_name,
                                                    )
                                                    print(
                                                        "\t\tCHECKING: "
                                                        + downloaded_file_series_name
                                                        + "\n\t\tAGAINST:  "
                                                        + existing_series_folder_from_library
                                                        + "\n\t\tSCORE: "
                                                        + str(similarity_score)
                                                        + "\n"
                                                    )
                                                    if (
                                                        similarity_score
                                                        >= required_similarity_score
                                                    ):
                                                        write_to_file(
                                                            "changes.txt",
                                                            (
                                                                '\t\tSimilarity between: "'
                                                                + existing_series_folder_from_library
                                                                + '" and "'
                                                                + downloaded_file_series_name
                                                                + '"'
                                                            ),
                                                        )
                                                        write_to_file(
                                                            "changes.txt",
                                                            (
                                                                "\tSimilarity Score: "
                                                                + str(similarity_score)
                                                                + " out of 1.0"
                                                            ),
                                                        )
                                                        print(
                                                            '\t\tSimilarity between: "'
                                                            + existing_series_folder_from_library
                                                            + '" and "'
                                                            + downloaded_file_series_name
                                                            + '" Score: '
                                                            + str(similarity_score)
                                                            + " out of 1.0\n"
                                                        )
                                                        done = check_upgrade(
                                                            folder_accessor.root,
                                                            dir,
                                                            file,
                                                            similarity_strings=[
                                                                downloaded_file_series_name,
                                                                existing_series_folder_from_library,
                                                                similarity_score,
                                                                required_similarity_score,
                                                            ],
                                                        )
                                                        if done:
                                                            if (
                                                                folder_accessor.root
                                                                not in cached_paths
                                                            ):
                                                                cached_paths.append(
                                                                    folder_accessor.root
                                                                )
                                                                write_to_file(
                                                                    "cached_paths.txt",
                                                                    folder_accessor.root,
                                                                    without_date=True,
                                                                    check_for_dup=False,
                                                                )
                                                            if (
                                                                len(volumes) > 1
                                                                and folder_accessor.root
                                                                in cached_paths
                                                                and folder_accessor.root
                                                                != cached_paths[0]
                                                            ):
                                                                cached_paths.remove(
                                                                    folder_accessor.root
                                                                )
                                                                cached_paths.insert(
                                                                    0,
                                                                    folder_accessor.root,
                                                                )
                                                            break
                                                        else:
                                                            continue
                                            if (
                                                not done
                                                and match_through_isbn_or_series_id
                                                and root not in paths
                                                and root not in download_folders
                                                and download_file_meta
                                            ):
                                                if folder_accessor.files:
                                                    if done:
                                                        break
                                                    for f in folder_accessor.files:
                                                        if (
                                                            f.extension
                                                            == file.extension
                                                        ):
                                                            existing_file_zip_comment = get_zip_comment(
                                                                f.path
                                                            )
                                                            existing_file_meta = None
                                                            if (
                                                                existing_file_zip_comment
                                                                and re.search(
                                                                    r"Identifiers",
                                                                    existing_file_zip_comment,
                                                                    re.IGNORECASE,
                                                                )
                                                            ):
                                                                # split on Identifiers: and only keep the second half
                                                                existing_file_zip_comment = (
                                                                    existing_file_zip_comment.split(
                                                                        "Identifiers:"
                                                                    )[
                                                                        1
                                                                    ]
                                                                ).strip()
                                                                if re.search(
                                                                    r",",
                                                                    existing_file_zip_comment,
                                                                ):
                                                                    existing_file_meta = existing_file_zip_comment.split(
                                                                        ","
                                                                    )
                                                                else:
                                                                    existing_file_meta = [
                                                                        existing_file_zip_comment
                                                                    ]
                                                            if existing_file_meta:
                                                                existing_file_meta = [
                                                                    x
                                                                    for x in existing_file_meta
                                                                    if not re.search(
                                                                        r"NONE",
                                                                        x,
                                                                        re.IGNORECASE,
                                                                    )
                                                                ]
                                                            if existing_file_meta:
                                                                # strip whitespace from each item in the list
                                                                existing_file_meta = [
                                                                    x.strip()
                                                                    for x in existing_file_meta
                                                                ]
                                                                for (
                                                                    d_meta
                                                                ) in download_file_meta:
                                                                    for (
                                                                        e_meta
                                                                    ) in existing_file_meta:
                                                                        print(
                                                                            (
                                                                                "\t\t("
                                                                                + str(
                                                                                    d_meta
                                                                                )
                                                                                + " - "
                                                                                + str(
                                                                                    e_meta
                                                                                )
                                                                                + ")"
                                                                            ),
                                                                            end="\r",
                                                                        )
                                                                        if (
                                                                            d_meta
                                                                            == e_meta
                                                                        ):
                                                                            directories_found.append(
                                                                                f.root
                                                                            )
                                                                            if (
                                                                                download_file_meta
                                                                                not in matched_ids
                                                                            ):
                                                                                matched_ids.append(
                                                                                    download_file_meta
                                                                                )
                                                                            if (
                                                                                existing_file_meta
                                                                                not in matched_ids
                                                                            ):
                                                                                matched_ids.append(
                                                                                    existing_file_meta
                                                                                )
                                    except Exception as e:
                                        send_error_message(e)
                            if not done and match_through_isbn_or_series_id:
                                if directories_found:
                                    directories_found = remove_duplicates(
                                        directories_found
                                    )
                                    if len(directories_found) == 1:
                                        print(
                                            "\n\t\t\tMach found in: "
                                            + directories_found[0]
                                        )
                                        base = os.path.basename(directories_found[0])
                                        identifier = IdentifierResult(
                                            file.series_name,
                                            download_file_meta,
                                            directories_found[0],
                                            matched_ids,
                                        )
                                        if identifier not in cached_identifier_results:
                                            cached_identifier_results.append(identifier)
                                        done = check_upgrade(
                                            os.path.dirname(directories_found[0]),
                                            base,
                                            file,
                                            similarity_strings=matched_ids,
                                            isbn=True,
                                        )
                                        if done:
                                            if directories_found[0] not in cached_paths:
                                                cached_paths.append(
                                                    directories_found[0]
                                                )
                                                write_to_file(
                                                    "cached_paths.txt",
                                                    directories_found[0],
                                                    without_date=True,
                                                    check_for_dup=False,
                                                )
                                            if (
                                                len(volumes) > 1
                                                and directories_found[0] in cached_paths
                                                and directories_found[0]
                                                != cached_paths[0]
                                            ):
                                                cached_paths.remove(
                                                    directories_found[0]
                                                )
                                                cached_paths.insert(
                                                    0, directories_found[0]
                                                )
                                    else:
                                        print(
                                            "\t\t\tMatching ISBN or Series ID found in multiple directories."
                                        )
                                        for d in directories_found:
                                            print("\t\t\t\t" + d)
                                        print("\t\t\tDisregarding Matches...")
                            elif not done:
                                print("\t\t\tNo match found in: " + root)


# Removes any unnecessary junk through regex in the folder name and returns the result
def get_series_name(dir):
    dir = (
        re.sub(
            r"(\b|\s)((\s|)-(\s|)|)(Part|)(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)([-_. ]|)([-_. ]|)([0-9]+)(\b|\s).*",
            "",
            dir,
            flags=re.IGNORECASE,
        )
    ).strip()
    dir = (re.sub(r"(\([^()]*\))|(\[[^\[\]]*\])|(\{[^\{\}]*\})", "", dir)).strip()
    dir = (re.sub(r"(\(|\)|\[|\]|{|})", "", dir, flags=re.IGNORECASE)).strip()
    return dir


# Renames the folders in our download directory.
# If volume releases are available, it will rename based on those.
# Otherwise it will fallback to just cleaning the name of any brackets.
def rename_dirs_in_download_folder():
    print("\nLooking for folders to rename...")
    for download_folder in download_folders:
        if os.path.exists(download_folder):
            try:
                download_folder_dirs = [
                    f
                    for f in os.listdir(download_folder)
                    if os.path.isdir(join(download_folder, f))
                ]
                download_folder_files = [
                    f
                    for f in os.listdir(download_folder)
                    if os.path.isfile(join(download_folder, f))
                ]
                clean_and_sort(
                    download_folder, download_folder_files, download_folder_dirs
                )
                global folder_accessor
                file_objects = upgrade_to_file_class(
                    download_folder_files[:], download_folder
                )
                folder_accessor = Folder(
                    download_folder,
                    download_folder_dirs[:],
                    os.path.basename(os.path.dirname(download_folder)),
                    os.path.basename(download_folder),
                    file_objects,
                )
                for folderDir in folder_accessor.dirs[:]:
                    done = False
                    full_file_path = os.path.join(folder_accessor.root, folderDir)
                    volumes = upgrade_to_volume_class(
                        upgrade_to_file_class(
                            [
                                f
                                for f in os.listdir(full_file_path)
                                if os.path.isfile(join(full_file_path, f))
                            ],
                            full_file_path,
                        )
                    )
                    volume_one = None
                    matching = []
                    if volumes:
                        # sort by name
                        if len(volumes) > 1:
                            volumes = sorted(volumes, key=lambda x: x.name)
                        first_series_name = volumes[0].series_name
                        if first_series_name:
                            # clone volumes list and remove the first reslut
                            clone_list = volumes[:]
                            if clone_list and len(clone_list) > 1:
                                clone_list.remove(volumes[0])
                            # check that at least 90% of the volumes have similar series_names
                            for v in clone_list:
                                if (
                                    similar(v.series_name, first_series_name)
                                    >= required_similarity_score
                                ):
                                    matching.append(v)
                                else:
                                    print(
                                        "\t\t"
                                        + v.series_name
                                        + " does not match "
                                        + first_series_name
                                    )
                            if (len(matching) >= len(volumes) * 0.9) and len(
                                volumes
                            ) == 1:
                                volume_one = matching[0]
                            elif (len(matching) + 1 >= len(volumes) * 0.8) and len(
                                volumes
                            ) > 1:
                                volume_one = matching[0]
                            else:
                                print(
                                    "\t\t"
                                    + str(len(matching))
                                    + " out of "
                                    + str(len(volumes))
                                    + " volumes match the first volume's series name."
                                )
                        else:
                            print(
                                "\t\tCould not find series name for: " + volumes[0].path
                            )
                        if (
                            volume_one
                            and volume_one.series_name != folderDir
                            and similar(
                                remove_bracketed_info_from_name(volume_one.series_name),
                                remove_bracketed_info_from_name(folderDir),
                            )
                            >= 0.25
                        ):
                            print("\n\tBEFORE: " + folderDir)
                            print("\tAFTER:  " + volume_one.series_name)
                            if volumes:
                                print("\t\tVOLUMES:")
                                for v in volumes:
                                    print("\t\t\t" + v.name)
                            user_input = ""
                            if manual_rename:
                                user_input = input(
                                    "\nRename (y or n or i (input rename all volumes' series names and folder) ): "
                                )
                            else:
                                user_input = "y"
                            try:
                                if user_input.lower() == "y":
                                    # if the direcotry doesn't exist, then rename to it
                                    if not os.path.exists(
                                        os.path.join(
                                            folder_accessor.root,
                                            volume_one.series_name,
                                        )
                                    ):
                                        try:
                                            os.rename(
                                                os.path.join(
                                                    folder_accessor.root, folderDir
                                                ),
                                                os.path.join(
                                                    folder_accessor.root,
                                                    volume_one.series_name,
                                                ),
                                            )
                                            done = True
                                            print(
                                                "\t\tRenamed "
                                                + folderDir
                                                + " to "
                                                + volume_one.series_name
                                            )
                                        except Exception as e:
                                            print(
                                                "\t\tCould not rename "
                                                + folderDir
                                                + " to "
                                                + volume_one.series_name
                                            )
                                            print(e)
                                    else:
                                        # move the files to the already existing directory if they don't already exist, otherwise delete them
                                        for v in volumes:
                                            if not os.path.isfile(
                                                os.path.join(
                                                    folder_accessor.root,
                                                    volume_one.series_name,
                                                    v.name,
                                                )
                                            ):
                                                move_file(
                                                    v,
                                                    os.path.join(
                                                        folder_accessor.root,
                                                        volume_one.series_name,
                                                    ),
                                                )
                                            else:
                                                print(
                                                    "\t\t"
                                                    + v.name
                                                    + " already exists in "
                                                    + volume_one.series_name
                                                )
                                                remove_file(v.path)
                                        # check for an empty folder, and delete it if it is
                                        check_and_delete_empty_folder(v.root)
                                        done = True
                                elif user_input.lower() == "i":
                                    print("\tInput mode selected.")
                                    print(
                                        "\n\tWARNING: This will rename all voluems with similar series names and the folder name to the user inputted series name"
                                    )
                                    print(
                                        "\tONLY USE IF YOU KNOW WHAT YOU ARE DOING, otherwise enter nothing or 'q' to quit"
                                    )
                                    print(
                                        "\n\tCurrent Series Name: "
                                        + volume_one.series_name
                                    )
                                    series_user_input = input(
                                        "\tReplacement Series Name: "
                                    )
                                    if (
                                        series_user_input != ""
                                        and series_user_input != "q"
                                    ):
                                        if len(volumes) > 1:
                                            matching.append(volumes[0])
                                        for v in matching:
                                            new_file_name = re.sub(
                                                v.series_name,
                                                series_user_input,
                                                v.name,
                                            )
                                            new_file_path = os.path.join(
                                                v.root, new_file_name
                                            )
                                            if not os.path.isfile(new_file_path):
                                                rename_file(
                                                    v.path,
                                                    new_file_path,
                                                    v.root,
                                                    v.extensionless_name,
                                                    get_extensionless_name(
                                                        new_file_name
                                                    ),
                                                )
                                            else:
                                                print(
                                                    "\t\t"
                                                    + new_file_name
                                                    + " already exists."
                                                )
                                        # if the folder doesn't already exist, rename it to series_user_input
                                        if volume_one.root != os.path.join(
                                            download_folder, series_user_input
                                        ):
                                            if not os.path.isdir(
                                                os.path.join(
                                                    download_folder,
                                                    series_user_input,
                                                )
                                            ):
                                                os.rename(
                                                    volume_one.root,
                                                    os.path.join(
                                                        download_folder,
                                                        series_user_input,
                                                    ),
                                                )
                                            else:
                                                print(
                                                    "\t\tFolder: "
                                                    + os.path.join(
                                                        download_folder,
                                                        series_user_input,
                                                    )
                                                    + " already exists."
                                                )
                                    done = True
                                else:
                                    print("Skipping...")
                            except Exception as e:
                                print(e)
                                print("Skipping...")
                    if not done:
                        download_folder_basename = os.path.basename(download_folder)
                        if re.search(
                            download_folder_basename, full_file_path, re.IGNORECASE
                        ):
                            if (
                                re.search(
                                    r"((\s\[|\]\s)|(\s\(|\)\s)|(\s\{|\}\s))",
                                    folderDir,
                                    re.IGNORECASE,
                                )
                                or re.search(r"(\s-\s|\s-)$", folderDir, re.IGNORECASE)
                                or re.search(r"(\bLN\b)", folderDir, re.IGNORECASE)
                                or re.search(
                                    r"(\b|\s)((\s|)-(\s|)|)(Part|)(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?|)(\.|)([-_. ]|)(([0-9]+)((([-_.]|)([0-9]+))+|))(\b|\s)",
                                    folderDir,
                                    re.IGNORECASE,
                                )
                                or re.search(r"\bPremium\b", folderDir, re.IGNORECASE)
                                or re.search(r":", folderDir, re.IGNORECASE)
                                or re.search(r"([A-Za-z])(_)", folderDir, re.IGNORECASE)
                                or re.search(r"([?])", folderDir)
                            ):
                                dir_clean = get_series_name(folderDir)
                                dir_clean = re.sub(r"([A-Za-z])(_)", r"\1 ", dir_clean)
                                # replace : with - in dir_clean
                                dir_clean = re.sub(
                                    r"([A-Za-z])(\:)", r"\1 -", dir_clean
                                )
                                dir_clean = re.sub(r"([?])", "", dir_clean)
                                # remove dual spaces from dir_clean
                                dir_clean = remove_dual_space(dir_clean).strip()
                                if not os.path.isdir(
                                    os.path.join(folder_accessor.root, dir_clean)
                                ):
                                    print("\tBEFORE: " + folderDir)
                                    print("\tAFTER:  " + dir_clean + "\n")
                                    user_input = ""
                                    if manual_rename:
                                        user_input = input("Rename (y or n): ")
                                    else:
                                        user_input = "y"
                                    try:
                                        if user_input.lower().strip() == "y":
                                            try:
                                                os.rename(
                                                    os.path.join(
                                                        folder_accessor.root,
                                                        folderDir,
                                                    ),
                                                    os.path.join(
                                                        folder_accessor.root,
                                                        dir_clean,
                                                    ),
                                                )
                                            except Exception as e:
                                                send_error_message(
                                                    "Error renaming folder: " + str(e)
                                                )
                                        else:
                                            continue
                                    except OSError as e:
                                        send_error_message(e)
                                elif (
                                    os.path.isdir(
                                        os.path.join(folder_accessor.root, dir_clean)
                                    )
                                    and dir_clean != ""
                                ):
                                    if os.path.join(
                                        folder_accessor.root, folderDir
                                    ) != os.path.join(folder_accessor.root, dir_clean):
                                        for root, dirs, files in scandir.walk(
                                            os.path.join(
                                                folder_accessor.root, folderDir
                                            ),
                                        ):
                                            remove_hidden_files(files, root)
                                            file_objects = upgrade_to_file_class(
                                                files, root
                                            )
                                            folder_accessor2 = Folder(
                                                root,
                                                dirs,
                                                os.path.basename(os.path.dirname(root)),
                                                os.path.basename(root),
                                                file_objects,
                                            )
                                            for file in folder_accessor2.files:
                                                new_location_folder = os.path.join(
                                                    download_folder, dir_clean
                                                )
                                                if not os.path.isfile(
                                                    os.path.join(
                                                        new_location_folder,
                                                        file.name,
                                                    )
                                                ):
                                                    move_file(
                                                        file,
                                                        os.path.join(
                                                            download_folder,
                                                            dir_clean,
                                                        ),
                                                    )
                                                else:
                                                    send_error_message(
                                                        "File: "
                                                        + file.name
                                                        + " already exists in: "
                                                        + os.path.join(
                                                            download_folder,
                                                            dir_clean,
                                                        )
                                                    )
                                                    send_error_message(
                                                        "Removing duplicate from downloads."
                                                    )
                                                    remove_file(
                                                        os.path.join(
                                                            folder_accessor2.root,
                                                            file.name,
                                                        )
                                                    )
                                            check_and_delete_empty_folder(
                                                folder_accessor2.root
                                            )
            except Exception as e:
                send_error_message(e)
        else:
            if download_folder == "":
                send_error_message("\nERROR: Path cannot be empty.")
            else:
                print("\nERROR: " + download_folder + " is an invalid path.\n")


def add_to_list(item, list):
    if item != "" and not item in list:
        list.append(item)


def get_extras(file_name, root):
    extension = get_file_extension(file_name)
    series_name = get_series_name_from_file_name(file_name, root)
    if (
        re.search(re.escape(series_name), file_name, re.IGNORECASE)
        and series_name != ""
    ):
        file_name = re.sub(
            re.escape(series_name), "", file_name, flags=re.IGNORECASE
        ).strip()
    results = re.findall(r"(\{|\(|\[)(.*?)(\]|\)|\})", file_name, flags=re.IGNORECASE)
    modified = []
    keywords = [
        "Premium",
        "Complete",
        "Fanbook",
        "Short Stories",
        "Short Story",
        "Omnibus",
    ]
    keywords_two = ["Extra", "Arc"]
    for result in results:
        combined = ""
        for r in result:
            combined += r
        add_to_list(combined, modified)
    for item in modified[:]:
        if re.search(
            r"(\{|\(|\[)(Premium|J-Novel Club Premium)(\]|\)|\})", item, re.IGNORECASE
        ) or re.search(r"\((\d{4})\)", item, re.IGNORECASE):
            modified.remove(item)
        if re.search(
            r"(\{|\(|\[)(Omnibus|Omnibus Edition)(\]|\)|\})", item, re.IGNORECASE
        ):
            modified.remove(item)
        if re.search(r"(Extra)(\]|\)|\})", item, re.IGNORECASE):
            modified.remove(item)
        if re.search(r"(\{|\(|\[)Part([-_. ]|)([0-9]+)(\]|\)|\})", item, re.IGNORECASE):
            modified.remove(item)
        if re.search(
            r"(\{|\(|\[)Season([-_. ]|)([0-9]+)(\]|\)|\})", item, re.IGNORECASE
        ):
            modified.remove(item)
        if re.search(
            r"(\{|\(|\[)(Chapter|Ch|Chpt|Chpter|C)([-_. ]|)([0-9]+)(\.[0-9]+|)(([-_. ]|)([0-9]+)(\.[0-9]+|)|)(\]|\)|\})",
            item,
            re.IGNORECASE,
        ):
            modified.remove(item)
    for keyword in keywords:
        if re.search(rf"\b{keyword}\b", file_name, re.IGNORECASE):
            if extension == ".epub":
                add_to_list("[" + keyword.strip() + "]", modified)
            elif extension == ".cbz":
                add_to_list("(" + keyword.strip() + ")", modified)
    for keyword_two in keywords_two:
        if re.search(
            rf"(([A-Za-z]|[0-9]+)|)+ {keyword_two}([-_ ]|)([0-9]+|([A-Za-z]|[0-9]+)+|)",
            file_name,
            re.IGNORECASE,
        ):
            result = re.search(
                rf"(([A-Za-z]|[0-9]+)|)+ {keyword_two}([-_ ]|)([0-9]+|([A-Za-z]|[0-9]+)+|)",
                file_name,
                re.IGNORECASE,
            ).group()
            if result != "Episode " or (
                result != "Arc " | result != "arc " | result != "ARC "
            ):
                if extension == ".epub":
                    add_to_list("[" + result.strip() + "]", modified)
                elif extension == ".cbz":
                    add_to_list("(" + result.strip() + ")", modified)
    if re.search(r"(\s|\b)Part([-_. ]|)([0-9]+)", file_name, re.IGNORECASE):
        result = re.search(
            r"(\s|\b)Part([-_. ]|)([0-9]+)", file_name, re.IGNORECASE
        ).group()
        if extension == ".epub":
            add_to_list("[" + result.strip() + "]", modified)
        elif extension == ".cbz":
            add_to_list("(" + result.strip() + ")", modified)
    if re.search(r"(\s|\b)Season([-_. ]|)([0-9]+)", file_name, re.IGNORECASE):
        result = re.search(
            r"(\s|\b)Season([-_. ]|)([0-9]+)", file_name, re.IGNORECASE
        ).group()
        if extension == ".epub":
            add_to_list("[" + result.strip() + "]", modified)
        elif extension == ".cbz":
            add_to_list("(" + result.strip() + ")", modified)
    if re.search(
        r"(\s|\b)(Chapter|Ch|Chpt|Chpter|C)([-_. ]|)([0-9]+)(\.[0-9]+|)(([-_. ]|)([0-9]+)(\.[0-9]+|)|)(\s|\b)",
        file_name,
        re.IGNORECASE,
    ):
        result = re.search(
            r"(\s|\b)(Chapter|Ch|Chpt|Chpter|C)([-_. ]|)([0-9]+)(\.[0-9]+|)(([-_. ]|)([0-9]+)(\.[0-9]+|)|)(\s|\b)",
            file_name,
            re.IGNORECASE,
        ).group()
        if extension == ".epub":
            add_to_list("[" + result.strip() + "]", modified)
        elif extension == ".cbz":
            add_to_list("(" + result.strip() + ")", modified)
    # Move Premium to the beginning
    for item in modified:
        if re.search(r"Premium", item, re.IGNORECASE):
            modified.remove(item)
            modified.insert(0, item)
    return modified


def isfloat(x):
    try:
        a = float(x)
    except (TypeError, ValueError):
        return False
    else:
        return True


def isint(x):
    try:
        a = float(x)
        b = int(a)
    except (TypeError, ValueError):
        return False
    else:
        return a == b


# Determines if the file name contains an issue number.
def contains_issue_number(file_name, volume_number):
    issue_number = "#" + volume_number
    if re.search(issue_number, file_name, re.IGNORECASE):
        return True
    else:
        return False


# check if zip file contains ComicInfo.xml
def check_if_zip_file_contains_comic_info_xml(zip_file):
    with zipfile.ZipFile(zip_file, "r") as zip_ref:
        list = zip_ref.namelist()
        for name in list:
            if name.lower() == "ComicInfo.xml".lower():
                return True
    return False


# retrieve the file specified from the zip file and return the data for it
def get_file_from_zip(zip_file, file_name, allow_base=True):
    result = None
    try:
        with zipfile.ZipFile(zip_file, "r") as z:
            list = z.namelist()
            for file in list:
                if allow_base:
                    if os.path.basename(file).lower() == file_name.lower():
                        result = z.read(file)
                        break
                else:
                    if file.lower() == file_name.lower():
                        result = z.read(file)
                        break
    except Exception as e:
        send_error_message(e)
        send_error_message("Attempted to read file: " + file_name)
    return result


# dynamically parse all tags from comicinfo.xml and return a dictionary of the tags
def parse_comicinfo_xml(xml_file):
    tags = {}
    if xml_file:
        try:
            tree = ET.fromstring(xml_file)
            for child in tree:
                tags[child.tag] = child.text
        except Exception as e:
            send_error_message(e)
            send_error_message("Attempted to parse comicinfo.xml")
            return tags
    return tags


# dynamically parse all html tags and values and return a dictionary of them
def parse_html_tags(html):
    soup = BeautifulSoup(html, "html.parser")
    tags = {}
    for tag in soup.find_all(True):
        tags[tag.name] = tag.get_text()
    return tags


# Renames files.
def rename_files_in_download_folders(only_these_files=[]):
    global manual_rename
    global cached_paths
    for path in download_folders:
        if os.path.exists(path):
            for root, dirs, files in scandir.walk(path):
                if (
                    root not in cached_paths
                    and root not in download_folders
                    and root not in paths
                    and path not in download_folders
                    and check_for_existing_series_toggle
                ):
                    write_to_file(
                        "cached_paths.txt", root, without_date=True, check_for_dup=True
                    )
                clean_and_sort(root, files, dirs)
                volumes = upgrade_to_volume_class(
                    upgrade_to_file_class(
                        [f for f in files if os.path.isfile(os.path.join(root, f))],
                        root,
                    )
                )
                print("\nLocation: " + root)
                print("Searching for files to rename...")
                for file in volumes:
                    if only_these_files and file.name not in only_these_files:
                        continue
                    try:
                        result = re.search(
                            r"(\s+)?\-?(\s+)?(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.\s?|\s?|)(([0-9]+)((([-_.]|)([0-9]+))+|))(\]|\)|\})?(\s|巻?\.epub|巻?\.cbz)",
                            file.name,
                            re.IGNORECASE,
                        )
                        if result or (
                            file.is_one_shot
                            and add_volume_one_number_to_one_shots == True
                        ):
                            if file.is_one_shot:
                                result = preferred_volume_renaming_format + "01"
                            else:
                                result = result.group().strip()
                            result = re.sub(r"[\[\(\{\]\)\}\_]", "", result).strip()
                            keyword = re.search(
                                r"(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)",
                                result,
                                re.IGNORECASE,
                            )
                            keyword = keyword.group(0)
                            result = re.sub(
                                rf"(-)(\s+)?{keyword}",
                                keyword,
                                result,
                                flags=re.IGNORECASE,
                                count=1,
                            ).strip()
                            for ext in file_extensions:
                                result = re.sub("\." + ext, "", result).strip()
                            results = re.split(
                                r"(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)",
                                result,
                                flags=re.IGNORECASE,
                            )
                            modified = []
                            for r in results[:]:
                                r = r.strip()
                                if r == "" or r == ".":
                                    results.remove(r)
                                else:
                                    found = re.search(
                                        r"([0-9]+)((([-_.])([0-9]+))+|)",
                                        r,
                                        re.IGNORECASE,
                                    )
                                    if found:
                                        r = found.group()
                                        if file.multi_volume:
                                            volume_numbers = (
                                                convert_list_of_numbers_to_array(r)
                                            )
                                            for number in volume_numbers:
                                                if number == volume_numbers[-1]:
                                                    modified.append(number)
                                                else:
                                                    modified.append(number)
                                                    modified.append("-")
                                        else:
                                            try:
                                                if isint(r):
                                                    r = int(r)
                                                    modified.append(r)
                                                elif isfloat(r):
                                                    r = float(r)
                                                    modified.append(r)
                                            except ValueError as ve:
                                                print(ve)
                                    if r and isinstance(r, str):
                                        if re.search(
                                            r"(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)",
                                            r,
                                            re.IGNORECASE,
                                        ):
                                            modified.append(
                                                re.sub(
                                                    r"(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)",
                                                    preferred_volume_renaming_format,
                                                    r,
                                                    flags=re.IGNORECASE,
                                                )
                                            )
                            if ((len(modified) == 2 and len(results) == 2)) or (
                                file.multi_volume
                                and (
                                    len(modified) == len(results) + len(volume_numbers)
                                )
                            ):
                                combined = ""
                                for item in modified:
                                    if type(item) == int:
                                        if item < 10:
                                            item = str(item).zfill(2)
                                        combined += str(item)
                                    elif type(item) == float:
                                        if item < 10:
                                            item = str(item).zfill(4)
                                        combined += str(item)
                                    elif isinstance(item, str):
                                        combined += item
                                without_keyword = re.sub(
                                    r"(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)",
                                    "",
                                    combined,
                                    flags=re.IGNORECASE,
                                )
                                if (
                                    file.extension == ".cbz"
                                    and add_issue_number_to_cbz_file_name
                                ):
                                    combined += " " + "#" + without_keyword
                                elif (
                                    file.extension == ".epub"
                                    and search_and_add_premium_to_file_name
                                ):
                                    if not re.search(
                                        r"\bPremium\b", file.name, re.IGNORECASE
                                    ) and (
                                        check_for_bonus_xhtml(file.path)
                                        or get_toc_or_copyright(file.path)
                                    ):
                                        print(
                                            "\nBonus content found inside epub, adding [Premium] to file name."
                                        )
                                        combined += " [Premium]"
                                if not file.is_one_shot:
                                    replacement = re.sub(
                                        r"((?<![A-Za-z]+)[-_. ]\s+|)(\[|\(|\{)?(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ]|)(([0-9]+)((([-_.]|)([0-9]+))+|))(\s#(([0-9]+)((([-_.]|)([0-9]+))+|)))?(\]|\)|\})?",
                                        combined,
                                        file.name,
                                        flags=re.IGNORECASE,
                                        count=1,
                                    )
                                else:
                                    base = re.sub(
                                        r"(.epub|.cbz)",
                                        "",
                                        file.basename,
                                        flags=re.IGNORECASE,
                                    ).strip()
                                    replacement = base + " " + combined
                                    if file.volume_year:
                                        replacement += (
                                            " (" + str(file.volume_year) + ")"
                                        )
                                    extras = get_extras(file.name, file.root)
                                    for extra in extras:
                                        replacement += " " + extra
                                    replacement += file.extension
                                replacement = re.sub(r"([?])", "", replacement).strip()
                                replacement = remove_dual_space(
                                    re.sub(r"_", " ", replacement)
                                ).strip()
                                # replace : with - in dir_clean
                                replacement = re.sub(
                                    r"([A-Za-z])(\:)", r"\1 -", replacement
                                )
                                # remove dual spaces from dir_clean
                                replacement = remove_dual_space(replacement)
                                if file.name != replacement:
                                    try:
                                        if not (
                                            os.path.isfile(
                                                os.path.join(root, replacement)
                                            )
                                        ):
                                            user_input = ""
                                            if not manual_rename:
                                                user_input = "y"
                                            else:
                                                print("\n" + file.name)
                                                print(replacement)
                                                user_input = input(
                                                    "\tRename (y or n): "
                                                )
                                            if user_input == "y":
                                                try:
                                                    os.rename(
                                                        os.path.join(root, file.name),
                                                        os.path.join(root, replacement),
                                                    )
                                                except OSError as e:
                                                    send_error_message(
                                                        e,
                                                        "Error renaming file: "
                                                        + file.name
                                                        + " to "
                                                        + replacement,
                                                    )
                                                if os.path.isfile(
                                                    os.path.join(root, replacement)
                                                ):
                                                    print(
                                                        "\tSuccessfully renamed file: \n\t\t"
                                                        + file.name
                                                        + " to "
                                                        + replacement
                                                    )
                                                    if (
                                                        not mute_discord_rename_notifications
                                                    ):
                                                        send_discord_message(
                                                            "From: "
                                                            + "```"
                                                            + file.name
                                                            + "```"
                                                            + "To: "
                                                            + "```"
                                                            + replacement
                                                            + "```",
                                                            "Renamed File",
                                                            color=8421504,
                                                        )
                                                    for (
                                                        image_extension
                                                    ) in image_extensions:
                                                        image_file = (
                                                            file.extensionless_name
                                                            + "."
                                                            + image_extension
                                                        )
                                                        if os.path.isfile(
                                                            os.path.join(
                                                                root, image_file
                                                            )
                                                        ):
                                                            extensionless_replacement = get_extensionless_name(
                                                                replacement
                                                            )
                                                            replacement_image = (
                                                                extensionless_replacement
                                                                + "."
                                                                + image_extension
                                                            )
                                                            try:
                                                                os.rename(
                                                                    os.path.join(
                                                                        root, image_file
                                                                    ),
                                                                    os.path.join(
                                                                        root,
                                                                        replacement_image,
                                                                    ),
                                                                )
                                                            except OSError as ose:
                                                                send_error_message(ose)
                                                else:
                                                    send_error_message(
                                                        "\nRename failed on: "
                                                        + file.name
                                                    )
                                        if os.path.isfile(
                                            os.path.join(root, replacement)
                                        ):
                                            file = upgrade_to_volume_class(
                                                upgrade_to_file_class(
                                                    [replacement],
                                                    root,
                                                )
                                            )[0]
                                    except OSError as ose:
                                        send_error_message(ose)
                            else:
                                send_error_message(
                                    "More than two for either array: " + file.name
                                )
                                print("Modified Array:")
                                for i in modified:
                                    print("\t" + str(i))
                                print("Results Array:")
                                for b in results:
                                    print("\t" + str(b))
                    except Exception as e:
                        send_error_message(
                            "\nERROR: " + str(e) + " (" + file.name + ")"
                        )
                    processed_files.append(file.name)
                    if resturcture_when_renaming:
                        reorganize_and_rename([file], file.series_name)
        else:
            if path == "":
                print("\nERROR: Path cannot be empty.")
            else:
                print("\nERROR: " + path + " is an invalid path.\n")


# check if volume file name is a chapter
def contains_chapter_keywords(file_name):
    file_name_clean = re.sub(r"c1fi7", "", file_name, re.IGNORECASE)
    file_name_clean = remove_dual_space(
        re.sub(r"(_)", " ", file_name_clean).strip()
    ).strip()
    return re.search(
        r"(((ch|c|d|chapter|chap)([-_. ]+)?([0-9]+))|\s+([0-9]+)(\.[0-9]+)?(x\d+((\.\d+)+)?)?(\s+|#\d+|\.cbz))",
        file_name_clean,
        re.IGNORECASE,
    )


# Checks for any exception keywords that will prevent the chapter release from being deleted.
def check_for_exception_keywords(file_name):
    return re.search(r"Extra|One(-|)shot", file_name, re.IGNORECASE)


# Deletes chapter files from the download folder.
def delete_chapters_from_downloads():
    try:
        for path in download_folders:
            if os.path.exists(path):
                os.chdir(path)
                for root, dirs, files in scandir.walk(path):
                    if (
                        root not in cached_paths
                        and root not in download_folders
                        and root not in paths
                        and path not in download_folders
                        and check_for_existing_series_toggle
                    ):
                        write_to_file(
                            "cached_paths.txt",
                            root,
                            without_date=True,
                            check_for_dup=True,
                        )
                    # clean_and_sort(root, files, dirs)
                    remove_ignored_folders(dirs)
                    remove_hidden_files(files, root)
                    for file in files:
                        if (
                            contains_chapter_keywords(file)
                            and not contains_volume_keywords(file)
                        ) and not (check_for_exception_keywords(file)):
                            if file.endswith(".cbz") or file.endswith(".zip"):
                                print(
                                    "\n\t\tFile: "
                                    + file
                                    + "\n\t\tLocation: "
                                    + root
                                    + "\n\t\tContains chapter keywords/lone numbers and does not contain any volume/exclusion keywords"
                                    + "\n\t\tDeleting chapter release."
                                )
                                send_discord_message(
                                    "File: "
                                    + "```"
                                    + file
                                    + "```"
                                    + "Location: "
                                    + "```"
                                    + root
                                    + "```"
                                    + "Checks: "
                                    + "```"
                                    + "Contains chapter keywords/lone numbers ✓\n"
                                    + "Does not contain volume keywords ✓\n"
                                    + "Does not contain any exclusion keywords ✓"
                                    + "```",
                                    "Chapter Release Found",
                                    color=8421504,
                                )
                                remove_file(os.path.join(root, file))
                for root, dirs, files in scandir.walk(path):
                    clean_and_sort(root, files, dirs)
                    for dir in dirs:
                        check_and_delete_empty_folder(os.path.join(root, dir))
            else:
                if path == "":
                    print("\nERROR: Path cannot be empty.")
                else:
                    print("\nERROR: " + path + " is an invalid path.\n")
    except Exception as e:
        send_error_message(e)


# remove all non-images from list of files
def remove_non_images(files):
    clean_list = []
    for file in files:
        extension = re.sub(r"\.", "", get_file_extension(os.path.basename(file)))
        if extension in image_extensions:
            clean_list.append(file)
    return clean_list


# Finds and extracts the internal cover from a cbz or epub file
def find_and_extract_cover(file, return_data_only=False):
    # check if the file is a valid zip file
    if zipfile.is_zipfile(file.path):
        epub_cover_path = ""
        if file.extension == ".epub":
            epub_cover_path = get_epub_cover(file.path)
            if epub_cover_path:
                epub_cover_path = os.path.basename(epub_cover_path)
                epub_cover_extension = re.sub(
                    r"\.", "", get_file_extension(epub_cover_path)
                )
                if epub_cover_extension not in image_extensions:
                    epub_cover_path = ""
        with zipfile.ZipFile(file.path, "r") as zip_ref:
            zip_list = zip_ref.namelist()
            zip_list = [
                x
                for x in zip_list
                if not os.path.basename(x).startswith(".")
                and not os.path.basename(x).startswith("__")
            ]
            zip_list = remove_non_images(zip_list)
            # remove anything that isn't a file
            zip_list = [
                x for x in zip_list if not x.endswith("/") and re.search(r"\.", x)
            ]
            # remove any non-supported image files from the list
            for item in zip_list:
                extension = get_file_extension(item)
                extension = re.sub(r"\.", "", extension)
                if extension not in image_extensions:
                    zip_list.remove(item)
            zip_list.sort()
            # parse zip_list and check each os.path.basename for epub_cover_path if epub_cover_path exists, then put it at the front of the list
            if epub_cover_path:
                for item in zip_list:
                    if os.path.basename(item) == epub_cover_path:
                        zip_list.remove(item)
                        zip_list.insert(0, item)
                        break
            cover_searches = [
                r"(cover\.([A-Za-z]+))$",
                r"(\b(Cover([0-9]+|)|CoverDesign|page([-_. ]+)?cover)\b)",
                r"(\b(p000|page_000)\b)",
                r"((\s+)0+\.(.{2,}))",
                r"(\bindex[-_. ]1[-_. ]1\b)",
                r"(9([-_. :]+)?7([-_. :]+)?(8|9)(([-_. :]+)?[0-9]){10})",
            ]
            image_data = None
            if zip_list and cover_searches:
                for image_file in zip_list:
                    for search in cover_searches:
                        if (
                            epub_cover_path
                            and os.path.basename(image_file) == epub_cover_path
                        ) or re.search(
                            search, os.path.basename(image_file), re.IGNORECASE
                        ):
                            print("\t\tCover Found: " + image_file)
                            image_extension = get_file_extension(
                                os.path.basename(image_file)
                            )
                            if image_extension == ".jpeg":
                                image_extension = ".jpg"
                            with zip_ref.open(image_file) as image_file_ref:
                                # save image_file_ref as file.extensionless_name + image_extension to file.root
                                if not return_data_only:
                                    with open(
                                        os.path.join(
                                            file.root,
                                            file.extensionless_name + image_extension,
                                        ),
                                        "wb",
                                    ) as image_file_ref_out:
                                        image_file_ref_out.write(image_file_ref.read())
                                else:
                                    image_data = image_file_ref.read()
                            if compress_image_option or return_data_only:
                                if not return_data_only:
                                    compress_image(
                                        os.path.join(
                                            file.root,
                                            file.extensionless_name + image_extension,
                                        )
                                    )
                                    image_extension = ".jpg"
                                    return file.extensionless_name + image_extension
                                elif return_data_only and image_data:
                                    compress_result_data = compress_image(
                                        os.path.join(
                                            file.root,
                                            file.extensionless_name + image_extension,
                                        ),
                                        raw_data=image_data,
                                    )
                                    if compress_result_data:
                                        return compress_result_data
                                    else:
                                        return image_data
                                else:
                                    return None
                print("\t\tNo cover found, defaulting to first image: " + zip_list[0])
                default_cover_path = zip_list[0]
                image_extension = get_file_extension(
                    os.path.basename(default_cover_path)
                )
                if image_extension == ".jpeg":
                    image_extension = ".jpg"
                with zip_ref.open(default_cover_path) as default_cover_ref:
                    # save image_file_ref as file.extensionless_name + image_extension to file.root
                    if not return_data_only:
                        with open(
                            os.path.join(
                                file.root,
                                file.extensionless_name + image_extension,
                            ),
                            "wb",
                        ) as default_cover_ref_out:
                            default_cover_ref_out.write(default_cover_ref.read())
                    else:
                        image_data = default_cover_ref.read()
                if compress_image_option or return_data_only:
                    if not return_data_only:
                        compress_image(
                            os.path.join(
                                file.root,
                                file.extensionless_name + image_extension,
                            )
                        )
                        image_extension = ".jpg"
                        return file.extensionless_name + image_extension
                    elif return_data_only and image_data:
                        compress_result_data = compress_image(
                            os.path.join(
                                file.root,
                                file.extensionless_name + image_extension,
                            ),
                            raw_data=image_data,
                        )
                        if compress_result_data:
                            return compress_result_data
                        else:
                            return image_data
                    else:
                        return None
    else:
        send_error_message("\nFile: " + file.name + " is not a valid zip file.")
    return False


# Checks if a volume series cover exists in the passed Directory
def check_for_series_cover(path):
    # get list of files from directory
    files = os.listdir(path)
    # remove hidden files
    remove_hidden_files(files, path)
    for file in files:
        lower_extensionless_name_base = os.path.basename(
            get_extensionless_name(file).lower()
        )
        # file name without extension
        if lower_extensionless_name_base == "cover":
            return True
    return False


# Extracts the covers out from our cbz and epub files
def extract_covers():
    global cached_paths
    print("\nLooking for covers to extract...")
    for path in paths:
        if os.path.exists(path):
            os.chdir(path)
            for root, dirs, files in scandir.walk(path):
                if (
                    root not in cached_paths
                    and root not in download_folders
                    and root not in paths
                    and path not in download_folders
                    and check_for_existing_series_toggle
                ):
                    write_to_file(
                        "cached_paths.txt", root, without_date=True, check_for_dup=True
                    )
                global folder_accessor
                clean_and_sort(root, files, dirs)
                remove_ignored_folders(dirs)
                remove_hidden_files(files, root)
                print("\nRoot: " + root)
                print("Dirs: " + str(dirs))
                print("Files: " + str(files))
                folder_accessor = Folder(
                    root,
                    dirs,
                    os.path.basename(os.path.dirname(root)),
                    os.path.basename(root),
                    upgrade_to_file_class(files, root),
                )
                global image_count
                for file in folder_accessor.files:
                    update_stats(file)
                    try:
                        has_cover = False
                        printed = False
                        cover = ""
                        for extension in image_extensions:
                            potential_cover = os.path.join(
                                file.root, file.extensionless_path + "." + extension
                            )
                            if os.path.isfile(potential_cover):
                                cover = potential_cover
                                has_cover = True
                                break
                        if not has_cover:
                            if not printed:
                                print("\tFile: " + file.name)
                                printed = True
                            print("\t\tFile does not have a cover.")
                            result = find_and_extract_cover(file)
                            if result:
                                image_count += 1
                                print("\t\tCover successfully extracted.")
                                has_cover = True
                                cover = result
                            else:
                                print("\t\tCover not found.")
                        else:
                            image_count += 1
                        if (
                            (
                                is_volume_one(file.name)
                                or is_one_shot(file.name, file.root)
                            )
                            and not check_for_series_cover(file.root)
                            and has_cover
                            and cover
                        ):
                            if not printed:
                                print("\tFile: " + file.name)
                                printed = True
                            print("\t\tMissing volume one cover.")
                            print("\t\tFound volume one cover.")
                            cover_extension = get_file_extension(
                                os.path.basename(cover)
                            )
                            if os.path.isfile(
                                os.path.join(file.root, os.path.basename(cover))
                            ):
                                shutil.copy(
                                    os.path.join(file.root, os.path.basename(cover)),
                                    os.path.join(file.root, "cover" + cover_extension),
                                )
                                print("\t\tCopied cover as series cover.")
                            else:
                                print(
                                    "\t\tCover does not exist at: "
                                    + str(
                                        os.path.join(file.root, os.path.basename(cover))
                                    )
                                )
                    except Exception as e:
                        send_error_message(
                            "\nERROR in extract_covers(): "
                            + str(e)
                            + " with file: "
                            + file.name
                        )
        else:
            if path == "":
                print("\nERROR: Path cannot be empty.")
            else:
                print("\nERROR: " + path + " is an invalid path.\n")


def print_stats():
    print("\nFor all paths.")
    print("Total Files Found: " + str(file_count))
    print("\t" + str(cbz_count) + " were cbz files")
    print("\t" + str(epub_count) + " were epub files")
    print("\tof those we found that " + str(image_count) + " had a cover image file.")
    if len(errors) != 0:
        print("\nErrors (" + str(len(errors)) + "):")
        for error in errors:
            print("\t" + str(error))


# Deletes any file with an extension in unaccepted_file_extensions from the download_folers
def delete_unacceptable_files():
    global cached_paths
    if unaccepted_file_extensions:
        print("Searching for unacceptable files...")
        try:
            for path in download_folders:
                if os.path.exists(path):
                    os.chdir(path)
                    for root, dirs, files in scandir.walk(path):
                        if (
                            root not in cached_paths
                            and root not in download_folders
                            and root not in paths
                            and path not in download_folders
                            and check_for_existing_series_toggle
                        ):
                            write_to_file(
                                "cached_paths.txt",
                                root,
                                without_date=True,
                                check_for_dup=True,
                            )
                        # clean_and_sort(root, files, dirs)
                        remove_ignored_folders(dirs)
                        remove_hidden_files(files, root)
                        for file in files:
                            extension = get_file_extension(file)
                            if (
                                unaccepted_file_extensions
                                and extension
                                and extension in unaccepted_file_extensions
                            ):
                                remove_file(os.path.join(root, file))
                                if not os.path.isfile(os.path.join(root, file)):
                                    print(
                                        "\t\tSuccessfully removed unacceptable file: "
                                        + os.path.join(root, file)
                                    )
                                else:
                                    send_error_message(
                                        "\t\tFailed to remove unacceptable file: "
                                        + os.path.join(root, file)
                                    )
                            elif unacceptable_keywords:
                                for keyword in unacceptable_keywords:
                                    if re.search(keyword, file, re.IGNORECASE):
                                        print(
                                            "\tUnacceptable: "
                                            + keyword
                                            + " match found in "
                                            + file
                                            + "\n\t\tDeleting file from: "
                                            + root
                                        )
                                        send_discord_message(
                                            "Keyword/Regex: "
                                            + "```"
                                            + keyword
                                            + "```"
                                            + "File: "
                                            + "```"
                                            + file
                                            + "```"
                                            + "Location: "
                                            + "```"
                                            + root
                                            + "```",
                                            "Unacceptable File Found",
                                            color=16711680,
                                        )
                                        remove_file(os.path.join(root, file))
                                        if not os.path.isfile(os.path.join(root, file)):
                                            print(
                                                "\t\t\tSuccessfully removed unacceptable file: "
                                                + os.path.join(root, file)
                                            )
                                        else:
                                            send_error_message(
                                                "\t\t\tFailed to remove unacceptable file: "
                                                + os.path.join(root, file)
                                            )
                                        break
                    for root, dirs, files in scandir.walk(path):
                        clean_and_sort(root, files, dirs)
                        for dir in dirs:
                            check_and_delete_empty_folder(os.path.join(root, dir))
                else:
                    if path == "":
                        print("\nERROR: Path cannot be empty.")
                    else:
                        print("\nERROR: " + path + " is an invalid path.\n")
        except Exception as e:
            send_error_message(e)


class BookwalkerBook:
    def __init__(
        self,
        title,
        volume_number,
        part,
        date,
        is_released,
        price,
        url,
        thumbnail,
        book_type,
    ):
        self.title = title
        self.volume_number = volume_number
        self.part = part
        self.date = date
        self.is_released = is_released
        self.price = price
        self.url = url
        self.thumbnail = thumbnail
        self.book_type = book_type


class BookwalkerSeries:
    def __init__(self, title, books, book_count, book_type):
        self.title = title
        self.books = books
        self.book_count = book_count
        self.book_type = book_type


# our session object, helps speed things up
# when scraping
session_object = None


def scrape_url(url, strainer=None, headers=None, cookies=None):
    try:
        global session_object
        if not session_object:
            session_object = requests.Session()
        page_obj = None
        if headers and cookies:
            page_obj = session_object.get(url, headers=headers, cookies=cookies)
        elif headers and not cookies:
            page_obj = session_object.get(url, headers=headers)
        else:
            page_obj = session_object.get(url)
        if page_obj and page_obj.status_code == 403:
            print("\nTOO MANY REQUESTS, WE'RE BEING RATE-LIMTIED!")
        soup = None
        if strainer and page_obj:
            soup = BeautifulSoup(page_obj.text, "lxml", parse_only=strainer)
        else:
            soup = BeautifulSoup(page_obj.text, "lxml")
        return soup
    except Exception as e:
        print("Error: " + str(e))
        return ""


def get_all_matching_books(books, book_type, title):
    matching_books = []
    for book in books:
        if book.book_type == book_type and book.title == title:
            matching_books.append(book)
    # remove them from books
    for book in matching_books:
        books.remove(book)
    return matching_books


# combine series in series_list that have the same title and book_type
def combine_series(series_list):
    combined_series = []
    for series in series_list:
        if len(combined_series) == 0:
            combined_series.append(series)
        else:
            for combined_series_item in combined_series:
                if (
                    similar(
                        remove_punctuation(series.title).lower().strip(),
                        remove_punctuation(combined_series_item.title).lower().strip(),
                    )
                    >= required_similarity_score
                    and series.book_type == combined_series_item.book_type
                ):
                    combined_series_item.books.extend(series.books)
                    combined_series_item.book_count = len(combined_series_item.books)
                    # combined_series_item.books = sorted(
                    #     combined_series_item.books, key=lambda x: x.volume_number
                    # )
                    break
            if series not in combined_series and not (
                similar(
                    remove_punctuation(series.title).lower().strip(),
                    remove_punctuation(combined_series[0].title).lower().strip(),
                )
                >= required_similarity_score
            ):
                combined_series.append(series)
    return combined_series


def search_bookwalker(query, type, print_info=False, alternative_search=False):
    sleep_timer = 8
    # The total amount of pages to scrape
    total_pages_to_scrape = 5
    # The books returned from the search
    books = []
    # The searches that  results in no book results
    no_book_result_searches = []
    # The series compiled from all the books
    series_list = []
    # The books without a volume number (probably one-shots)
    no_volume_number = []
    # Releases that were identified as chapters
    chapter_releases = []
    # Similarity matches that did not meet the required similarity score
    similarity_match_failures = []
    # Errors encountered while scraping
    errors = []
    bookwalker_manga_category = "&qcat=2"
    bookwalker_light_novel_category = "&qcat=3"
    bookwalker_intll_manga_category = "&qcat=11"
    startTime = datetime.now()
    done = False
    search_type = type
    count = 0
    page_count = 1
    page_count_url = "&page=" + str(page_count)
    search = urllib.parse.quote(query)
    base_url = "https://global.bookwalker.jp/search/?word="
    if not alternative_search:
        if search_type.lower() == "m":
            print("\tChecking: " + query + " [MANGA]")
        elif search_type.lower() == "l":
            print("\tChecking: " + query + " [NOVEL]")
    while page_count < total_pages_to_scrape + 1:
        page_count_url = "&page=" + str(page_count)
        alternate_url = ""
        url = base_url + search + page_count_url
        if search_type.lower() == "m":
            if not alternative_search:
                url += bookwalker_manga_category
            else:
                url += bookwalker_intll_manga_category
        elif search_type.lower() == "l":
            url += bookwalker_light_novel_category
        page_count += 1
        # scrape url page
        page = scrape_url(
            url,
            cookies={"glSafeSearch": "1", "safeSearch": "111"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
            },
        )
        if not page:
            alternate_page = None
            if search_type.lower() == "m" and not alternative_search:
                alternate_page = scrape_url(
                    url,
                    cookies={"glSafeSearch": "1", "safeSearch": "111"},
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
                    },
                )
            if not alternate_page:
                print("\t\tError: Empty page")
                errors.append("Empty page")
                continue
            else:
                page = alternate_page
        # parse page
        soup = page
        # get total pages
        pager_area = soup.find("div", class_="pager-area")
        if pager_area:
            # find <ul class="clearfix"> in pager-area
            ul_list = pager_area.find("ul", class_="clearfix")
            # find all the <li> in ul_list
            li_list = ul_list.find_all("li")
            # find the highest number in li_list values
            highest_num = 0
            for li in li_list:
                text = li.text
                # if text is a number
                if text.isdigit():
                    if int(text) > highest_num:
                        highest_num = int(text)
            if highest_num == 0:
                print("\t\tNo pages found.")
                errors.append("No pages found.")
                continue
            elif highest_num < total_pages_to_scrape:
                total_pages_to_scrape = highest_num
        else:
            total_pages_to_scrape = 1
        list_area = soup.find(
            "div", class_="book-list-area book-result-area book-result-area-1"
        )
        list_area_ul = soup.find("ul", class_="o-tile-list")
        if list_area_ul is None:
            alternate_result = None
            if search_type.lower() == "m" and not alternative_search:
                alternate_result = search_bookwalker(
                    query, type, print_info, alternative_search=True
                )
                time.sleep(sleep_timer / 2)
            if alternate_result:
                return alternate_result
            if not alternative_search:
                print("\t\t! NO BOOKS FOUND ON BOOKWALKER !")
            no_book_result_searches.append(query)
            continue
        o_tile_list = list_area_ul.find_all("li", class_="o-tile")
        for item in o_tile_list:
            try:
                o_tile_book_info = item.find("div", class_="o-tile-book-info")
                o_tile_thumb_box = o_tile_book_info.find(
                    "div", class_="m-tile-thumb-box"
                )
                # get href from o_tile_thumb_box
                a_title_thumb = o_tile_thumb_box.find("a", class_="a-tile-thumb-img")
                url = a_title_thumb.get("href")
                img_clas = a_title_thumb.find("img")
                # get data-srcset 2x from img_clas
                img_srcset = img_clas.get("data-srcset")
                img_srcset = re.sub(r"\s\d+x", "", img_srcset)
                img_srcset_split = img_srcset.split(",")
                img_srcset_split = [x.strip() for x in img_srcset_split]
                thumbnail = img_srcset_split[1]
                ul_tag_box = o_tile_book_info.find("ul", class_="m-tile-tag-box")
                li_tag_item = ul_tag_box.find_all("li", class_="m-tile-tag")
                a_tag_chapter = None
                a_tag_simulpub = None
                a_tag_manga = None
                a_tag_light_novel = None
                a_tag_other = None
                for i in li_tag_item:
                    if i.find("div", class_="a-tag-manga"):
                        a_tag_manga = i.find("div", class_="a-tag-manga")
                    elif i.find("div", class_="a-tag-light-novel"):
                        a_tag_light_novel = i.find("div", class_="a-tag-light-novel")
                    elif i.find("div", class_="a-tag-other"):
                        a_tag_other = i.find("div", class_="a-tag-other")
                    elif i.find("div", class_="a-tag-chapter"):
                        a_tag_chapter = i.find("div", class_="a-tag-chapter")
                    elif i.find("div", class_="a-tag-simulpub"):
                        a_tag_simulpub = i.find("div", class_="a-tag-simulpub")
                if a_tag_manga:
                    book_type = a_tag_manga.get_text()
                elif a_tag_light_novel:
                    book_type = a_tag_light_novel.get_text()
                elif a_tag_other:
                    book_type = a_tag_other.get_text()
                else:
                    book_type = "Unknown"
                book_type = re.sub(r"\n|\t|\r", "", book_type).strip()
                title = o_tile_book_info.find("h2", class_="a-tile-ttl").text
                title = re.sub(r"[\n\t\r]", " ", title)
                # replace any unicode characters in the title with spaces
                title = re.sub(r"[^\x00-\x7F]+", " ", title)
                title = remove_dual_space(title).strip()
                if (
                    title
                    and re.search(r"Chapter", title, re.IGNORECASE)
                    and not re.search(r"re([-_. :]+)?zero", title, re.IGNORECASE)
                ):
                    continue
                part = ""
                part_search = get_volume_part(title)
                if part_search:
                    part_search = set_num_as_float_or_int(part_search)
                    if part_search:
                        part = set_num_as_float_or_int(part_search)
                if a_tag_chapter or a_tag_simulpub:
                    chapter_releases.append(title)
                    continue
                if part and re.search(r"(\b(Part)([-_. ]|)\b)", title):
                    title = re.sub(r"(\b(Part)([-_. ]|)\b)", " ", title)
                    title = re.sub(str(part), " ", title)
                    title = remove_dual_space(title).strip()
                volume_number = ""
                if not re.search(r"(\b(Vol)([-_. ]|)\b)", title):
                    if not re.search(
                        r"(([0-9]+)((([-_.]|)([0-9]+))+|))(\s+)?-(\s+)?(([0-9]+)((([-_.]|)([0-9]+))+|))",
                        title,
                    ):
                        volume_number = re.search(
                            r"([0-9]+(\.?[0-9]+)?([-_][0-9]+\.?[0-9]+)?)$", title
                        )
                    else:
                        title_split = title.split("-")
                        # remove anyting that isn't a number or a period
                        title_split = [re.sub(r"[^0-9.]", "", x) for x in title_split]
                        # clean any extra spaces in the volume_number and set_as_float_or_int
                        title_split = [
                            set_num_as_float_or_int(x.strip()) for x in title_split
                        ]
                        # remove empty results from the list
                        title_split = [x for x in title_split if x]
                        if title_split:
                            volume_number = title_split
                        else:
                            volume_number = None
                    if volume_number and not isinstance(volume_number, list):
                        if hasattr(volume_number, "group"):
                            volume_number = volume_number.group(1)
                            volume_number = set_num_as_float_or_int(volume_number)
                        else:
                            if title not in no_volume_number:
                                no_volume_number.append(title)
                            continue
                    elif title and is_one_shot_bk(title):
                        volume_number = 1
                    elif not volume_number and not isinstance(volume_number, list):
                        if title not in no_volume_number:
                            no_volume_number.append(title)
                        continue
                else:
                    volume_number = remove_everything_but_volume_num([title])
                if not re.search(r"(\b(Vol)([-_. ]|)\b)", title):
                    title = re.sub(
                        r"(\b|\s)((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?|)(\.|)([-_. ]|)(([0-9]+)(\b|\s))$.*",
                        "",
                        title,
                        flags=re.IGNORECASE,
                    ).strip()
                    if re.search(r",$", title):
                        title = re.sub(r",$", "", title).strip()
                    title = title.replace("\n", "").replace("\t", "")
                    title = re.sub(rf"\b{volume_number}\b", "", title)
                    title = re.sub(r"(\s{2,})", " ", title).strip()
                    title = re.sub(r"(\((.*)\)$)", "", title).strip()
                else:
                    title = re.sub(
                        r"(\b|\s)((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(LN|Light Novels?|Novels?|Books?|Volumes?|Vols?|V|第|Discs?)(\.|)([-_. ]|)([0-9]+)(\b|\s).*",
                        "",
                        title,
                        flags=re.IGNORECASE,
                    ).strip()
                clean_title = remove_punctuation(title).lower().strip()
                clean_query = remove_punctuation(query).lower().strip()
                score = similar(clean_title, clean_query)
                if not (score >= required_similarity_score):
                    message = (
                        '"'
                        + clean_title
                        + '"'
                        + ": "
                        + str(score)
                        + " ["
                        + book_type
                        + "]"
                    )
                    if message not in similarity_match_failures:
                        similarity_match_failures.append(message)
                    continue
                # html from url
                page_two = scrape_url(
                    url, SoupStrainer("div", class_="product-detail-inner")
                )
                # print(str((datetime.now() - startTime)))
                # parse html
                soup_two = page_two
                # find table class="product-detail"
                product_detail = soup_two.find("table", class_="product-detail")
                # print(str((datetime.now() - startTime)))
                # find all <td> inside of product-detail
                product_detail_td = product_detail.find_all("td")
                date = ""
                for detail in product_detail_td:
                    date = re.search(
                        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(\s+)?\d{2}([,]+)?\s+\d{4}",
                        detail.text,
                        re.IGNORECASE,
                    )
                    if date:
                        date = date.group(0)
                        date = re.sub(r"[^\s\w]", "", date)
                        month = date.split()[0]
                        if len(month) != 3:
                            month = month[:3]
                        abbr_to_num = {
                            name: num
                            for num, name in enumerate(calendar.month_abbr)
                            if num
                        }
                        month = abbr_to_num[month]
                        day = date.split()[1]
                        year = date.split()[2]
                        date = datetime(int(year), month, int(day))
                        if date < datetime.now():
                            is_released = True
                        else:
                            is_released = False
                        date = date.strftime("%Y-%m-%d")
                        break
                book = BookwalkerBook(
                    title,
                    volume_number,
                    part,
                    date,
                    is_released,
                    0.00,
                    url,
                    thumbnail,
                    book_type,
                )
                books.append(book)
            except Exception as e:
                send_error_message(e)
                errors.append(url)
                continue
        # if books is not None and len(books) > 1:
        #     books = sorted(books, key=lambda x: x.volume_number)
        for book in books:
            matching_books = get_all_matching_books(books, book.book_type, book.title)
            if len(matching_books) > 0:
                series_list.append(
                    BookwalkerSeries(
                        book.title,
                        matching_books,
                        len(matching_books),
                        book.book_type,
                    )
                )
        if len(series_list) > 0 and print_info:
            print("Total Series: " + str(len(series_list)) + "\n")
            for series in series_list:
                if series_list.index(series) != 0:
                    print("\n")
                print(
                    "["
                    + str(series_list.index(series) + 1)
                    + "] "
                    + series.title
                    + " ["
                    + str(series.book_type)
                    + "]"
                    + " ("
                    + str(series.book_count)
                    + ")"
                )
                print("\tBooks:")
                for book in series.books:
                    # print the current books index
                    if not book.is_released:
                        print(
                            "\t\t("
                            + str(series.books.index(book) + 1)
                            + ")------------------------------ [PRE-ORDER]"
                        )
                    else:
                        print(
                            "\t\t("
                            + str(series.books.index(book) + 1)
                            + ")------------------------------"
                        )
                    print("\t\t\tNumber: " + str(book.volume_number))
                    # print("\t\t\tTitle: " + book.title)
                    print("\t\t\tDate: " + book.date)
                    print("\t\t\tReleased: " + str(book.is_released))
                    # print("\t\t\tPrice: " + str(book.price))
                    print("\t\t\tURL: " + book.url)
                    print("\t\t\tThumbnail: " + book.thumbnail)
                    # print("\t\t\tBook Type: " + book.book_type)
            else:
                print("\n\tNo results found.")

            if len(no_volume_number) > 0:
                print("\nNo Volume Results (" + str(len(no_volume_number)) + "):")
                for title in no_volume_number:
                    print("\t" + title)
                no_volume_number = []
            if len(chapter_releases) > 0:
                print("\nChapter Releases: (" + str(len(chapter_releases)) + ")")
                for title in chapter_releases:
                    print("\t" + title)
                chapter_releases = []
            if len(similarity_match_failures) > 0:
                print(
                    "\nSimilarity Match Failures "
                    + "("
                    + str(len(similarity_match_failures))
                    + "):"
                )
                for title in similarity_match_failures:
                    print(
                        "\t["
                        + str(similarity_match_failures.index(title) + 1)
                        + "] "
                        + title
                    )
                similarity_match_failures = []
            if len(no_book_result_searches) > 0:
                print(
                    "\nNo Book Result Searches ("
                    + str(len(no_book_result_searches))
                    + "):"
                )
                for url in no_book_result_searches:
                    print("\t" + url)
                no_book_result_searches = []
    series_list = combine_series(series_list)
    # print("\tSleeping for " + str(sleep_timer) + " to avoid being rate-limited...")
    time.sleep(sleep_timer)
    if len(series_list) == 1:
        if len(series_list[0].books) > 0:
            return series_list[0].books
    elif len(series_list) > 1:
        print("\t\tNumber of series from bookwalker search is greater than one.")
        print("\t\tNum: " + str(len(series_list)))
        return None
    else:
        if not alternative_search:
            print("\t\tNo matching books found.")
        return None


# Checks the library against bookwalker for any missing volumes that are released or on pre-order
# Doesn't work with NSFW results atm.
def check_for_new_volumes_on_bookwalker():
    print("\nChecking for new volumes on bookwalker...")
    paths_clean = [p for p in paths if p not in download_folders]
    for path in paths_clean:
        if os.path.exists(path):
            os.chdir(path)
            # get list of folders from path directory
            path_dirs = [f for f in os.listdir(path) if os.path.isdir(f)]
            path_dirs.sort()
            clean_and_sort(path, dirs=path_dirs)
            global folder_accessor
            folder_accessor = Folder(
                path,
                path_dirs,
                os.path.basename(os.path.dirname(path)),
                os.path.basename(path),
                [""],
            )
            for dir in folder_accessor.dirs:
                current_folder_path = os.path.join(folder_accessor.root, dir)
                existing_dir_full_file_path = os.path.dirname(
                    os.path.join(folder_accessor.root, dir)
                )
                existing_dir = os.path.join(existing_dir_full_file_path, dir)
                clean_existing = os.listdir(existing_dir)
                clean_and_sort(existing_dir, clean_existing)
                existing_dir_volumes = upgrade_to_volume_class(
                    upgrade_to_file_class(
                        [
                            f
                            for f in clean_existing
                            if os.path.isfile(os.path.join(existing_dir, f))
                        ],
                        existing_dir,
                    )
                )
                bookwalker_volumes = None
                type = None
                if (
                    get_cbz_percent_for_folder([f.name for f in existing_dir_volumes])
                    >= 70
                ):
                    type = "m"
                elif (
                    get_epub_percent_for_folder([f.name for f in existing_dir_volumes])
                    >= 70
                ):
                    type = "l"
                # if len(new_releases_on_bookwalker) >= 10:  # used for quick testing
                #     break
                if type and dir:
                    bookwalker_volumes = search_bookwalker(dir, type, False)
                if existing_dir_volumes and bookwalker_volumes:
                    for existing_vol in existing_dir_volumes:
                        for bookwalker_vol in bookwalker_volumes[:]:
                            if (
                                existing_vol.volume_number
                                == bookwalker_vol.volume_number
                                and existing_vol.volume_part == bookwalker_vol.part
                            ):
                                bookwalker_volumes.remove(bookwalker_vol)
                    if len(bookwalker_volumes) > 0:
                        new_releases_on_bookwalker.extend(bookwalker_volumes)
                        for vol in bookwalker_volumes:
                            if vol.is_released:
                                print("\n\t\t[RELEASED]")
                            else:
                                print("\n\t\t[PRE-ORDER]")
                            print(
                                "\t\tVolume Number: "
                                + str(set_num_as_float_or_int(vol.volume_number))
                            )
                            print("\t\tDate: " + vol.date)
                            if vol == bookwalker_volumes[-1]:
                                print("\t\tURL: " + vol.url + "\n")
                            else:
                                print("\t\tURL: " + vol.url)
    pre_orders = []
    released = []
    if len(new_releases_on_bookwalker) > 0:
        for release in new_releases_on_bookwalker:
            if release.is_released:
                released.append(release)
            else:
                pre_orders.append(release)
    pre_orders.sort(key=lambda x: x.date, reverse=True)
    released.sort(key=lambda x: x.date, reverse=False)
    # Get rid of the old released and pre-orders and replace them with new ones.
    if log_to_file:
        if os.path.isfile(os.path.join(ROOT_DIR, "released.txt")):
            os.remove(os.path.join(ROOT_DIR, "released.txt"))
        if os.path.isfile(os.path.join(ROOT_DIR, "pre-orders.txt")):
            os.remove(os.path.join(ROOT_DIR, "pre-orders.txt"))
    if len(released) > 0:
        # send_discord_message(
        #     "/clear amount:500", passed_webhook=bookwalker_webhook_urls[0]
        # )
        print("\nNew Releases:")
        for r in released:
            print("\t" + r.title)
            print("\tVolume " + str(set_num_as_float_or_int(r.volume_number)))
            print("\tDate: " + r.date)
            print("\tURL: " + r.url)
            print("\n")
            message = (
                r.date
                + " "
                + r.title
                + " Volume "
                + str(set_num_as_float_or_int(r.volume_number))
                + " "
                + r.url
            )
            write_to_file("released.txt", message, without_date=True, overwrite=False)
            if bookwalker_webhook_urls and len(bookwalker_webhook_urls) == 2:
                send_discord_message(message, passed_webhook=bookwalker_webhook_urls[0])
    if len(pre_orders) > 0:
        # if bookwalker_webhook_urls and len(bookwalker_webhook_urls) == 1:
        #     send_discord_message(
        #         "/clear amount:500", passed_webhook=bookwalker_webhook_urls[0]
        #     )
        # elif bookwalker_webhook_urls and len(bookwalker_webhook_urls) == 2:
        #     send_discord_message(
        #         "/clear amount:500", passed_webhook=bookwalker_webhook_urls[1]
        #     )
        print("\nPre-orders:")
        for p in pre_orders:
            print("\t" + p.title)
            print("\tVolume: " + str(set_num_as_float_or_int(p.volume_number)))
            print("\tDate: " + p.date)
            print("\tURL: " + p.url)
            print("\n")
            message = (
                p.date
                + " "
                + p.title
                + " Volume "
                + str(set_num_as_float_or_int(p.volume_number))
                + " "
                + p.url
            )
            write_to_file("pre-orders.txt", message, without_date=True, overwrite=False)
            if bookwalker_webhook_urls and len(bookwalker_webhook_urls) == 2:
                send_discord_message(message, passed_webhook=bookwalker_webhook_urls[1])
            elif bookwalker_webhook_urls and len(bookwalker_webhook_urls) == 1:
                send_discord_message(message, passed_webhook=bookwalker_webhook_urls[0])


# Checks the epub for bonus.xhtml or bonus[0-9].xhtml
# then returns whether or not it was found.
def check_for_bonus_xhtml(zip):
    zip = zipfile.ZipFile(zip)
    list = zip.namelist()
    for item in list:
        base = os.path.basename(item)
        if re.search(r"(bonus([0-9]+)?\.xhtml)", base, re.IGNORECASE):
            return True
    return False


# caches all roots encountered when walking paths
def cache_paths():
    global cached_paths
    for path in paths:
        if os.path.exists(path):
            if path not in download_folders:
                try:
                    for root, dirs, files in scandir.walk(path):
                        if (
                            (root != path and root not in cached_paths)
                            and (not root.startswith(".") and not root.startswith("_"))
                            and (os.path.exists(root) and os.path.isdir(root))
                        ):
                            cached_paths.append(root)
                            write_to_file(
                                "cached_paths.txt",
                                root,
                                without_date=True,
                                check_for_dup=True,
                            )
                except Exception as e:
                    send_error_message(e)
        else:
            if path == "":
                send_error_message("\nERROR: Path cannot be empty.")
            else:
                print("\nERROR: " + path + " is an invalid path.\n")


# Sends scan requests to komga for all libraries in komga_library_ids
# Reqiores komga settings to be set in settings.py
def scan_komga_libraries():
    komga_url = f"{komga_ip}:{komga_port}"
    if komga_library_ids and komga_url and komga_login_email and komga_login_password:
        for library_id in komga_library_ids:
            try:
                request = requests.post(
                    f"{komga_url}/api/v1/libraries/{library_id}/scan",
                    headers={
                        "Authorization": "Basic %s"
                        % b64encode(
                            f"{komga_login_email}:{komga_login_password}".encode(
                                "utf-8"
                            )
                        ).decode("utf-8"),
                        "Accept": "*/*",
                    },
                )
                if request.status_code == 202:
                    print(
                        "Successfully Initiated Scan for: " + library_id + " Library."
                    )
                else:
                    send_error_message(
                        "Failed to Initiate Scan for: "
                        + library_id
                        + " Library"
                        + " Status Code: "
                        + str(request.status_code)
                        + " Response: "
                        + request.text
                    )
            except Exception as e:
                send_error_message(
                    "Failed to Initiate Scan for: " + library_id + " Komga Library."
                )


def generate_release_group_list_file():
    print("\nGenerating Release Group List File...")
    global release_groups
    global skipped_files
    for path in paths:
        if os.path.exists(path):
            try:
                skipped_file_volumes = []
                for root, dirs, files in scandir.walk(path):
                    clean_and_sort(root, files, dirs, sort=True)
                    if files:
                        volumes = upgrade_to_volume_class(
                            upgrade_to_file_class(
                                [
                                    f
                                    for f in files
                                    if os.path.isfile(os.path.join(root, f))
                                ],
                                root,
                            )
                        )
                        for file in volumes:
                            print("\n\tChecking: " + file.name)
                            found = False
                            if file.name not in skipped_files:
                                if skipped_files and not skipped_file_volumes:
                                    skipped_file_volumes = upgrade_to_volume_class(
                                        upgrade_to_file_class(
                                            [f for f in skipped_files],
                                            root,
                                        )
                                    )
                                if skipped_file_volumes:
                                    for skipped_file in skipped_file_volumes:
                                        if skipped_file.extras:
                                            # sort alphabetically
                                            skipped_file.extras.sort()
                                            # remove any year from the extras
                                            for extra in skipped_file.extras[:]:
                                                if re.search(
                                                    r"([\[\(\{]\d{4}[\]\)\}])",
                                                    extra,
                                                    re.IGNORECASE,
                                                ):
                                                    skipped_file.extras.remove(extra)
                                                    break
                                        if file.extras:
                                            # sort alphabetically
                                            file.extras.sort()
                                            # remove any year from the extras
                                            for extra in file.extras[:]:
                                                if re.search(
                                                    r"([\[\(\{]\d{4}[\]\)\}])",
                                                    extra,
                                                    re.IGNORECASE,
                                                ):
                                                    file.extras.remove(extra)
                                                    break
                                        if (
                                            file.extras == skipped_file.extras
                                            and file.extension == skipped_file.extension
                                            and file.series_name
                                            == skipped_file.series_name
                                        ):
                                            print(
                                                "\t\tSkipping: "
                                                + file.name
                                                + " because it has the same extras, extension, and series name as: "
                                                + skipped_file.name
                                                + " (in skipped_files.txt)"
                                            )
                                            found = True
                                            write_to_file(
                                                "skipped_files.txt",
                                                file.name,
                                                without_date=True,
                                                check_for_dup=True,
                                            )
                                            if file.name not in skipped_files:
                                                skipped_files.append(file.name)
                                                skipped_file_volume = (
                                                    upgrade_to_volume_class(
                                                        upgrade_to_file_class(
                                                            [file.name], root
                                                        )
                                                    )
                                                )
                                                if (
                                                    skipped_file_volume
                                                    and skipped_file_volume
                                                    not in skipped_file_volumes
                                                ):
                                                    skipped_file_volumes.append(
                                                        skipped_file_volume[0]
                                                    )
                                            break
                                if release_groups and not found:
                                    for group in release_groups:
                                        left_brackets = r"(\(|\[|\{)"
                                        right_brackets = r"(\)|\]|\})"
                                        group_escaped = re.escape(group)
                                        if re.search(
                                            rf"{left_brackets}{group_escaped}{right_brackets}",
                                            file.name,
                                            re.IGNORECASE,
                                        ):
                                            print(
                                                '\t\tFound: "'
                                                + group
                                                + '" skipping file.'
                                            )
                                            found = True
                                            break
                                if not found:
                                    # ask the user what the release group is, then write it to the file, add it to the list, and continue. IF the user inputs "none" then skip it.
                                    # loop until the user inputs a valid response
                                    while True:
                                        print(
                                            "\t\tCould not find a release group for: \n\t\t\t"
                                            + file.name
                                        )
                                        group = input(
                                            "\n\t\tPlease enter the release group "
                                            + '("none" to add to skipped_files.txt, "skip" to skip): '
                                        )
                                        if group == "none":
                                            print(
                                                "\t\t\tAdding to skipped_files.txt and skipping in the future..."
                                            )
                                            write_to_file(
                                                "skipped_files.txt",
                                                file.name,
                                                without_date=True,
                                                check_for_dup=True,
                                            )
                                            if file.name not in skipped_files:
                                                skipped_files.append(file.name)
                                                skipped_file_vol = (
                                                    upgrade_to_volume_class(
                                                        upgrade_to_file_class(
                                                            [file.name], root
                                                        )
                                                    )
                                                )
                                                if (
                                                    skipped_file_vol
                                                    and skipped_file_vol
                                                    not in skipped_file_volumes
                                                ):
                                                    skipped_file_volumes.append(
                                                        skipped_file_vol[0]
                                                    )
                                            break
                                        elif group == "skip":
                                            print("\t\t\tSkipping...")
                                            break
                                        elif group:
                                            # print back what the user entered
                                            print("\t\t\tYou entered: " + group)
                                            write_to_file(
                                                "release_groups.txt",
                                                group,
                                                without_date=True,
                                                check_for_dup=True,
                                            )
                                            if group not in release_groups:
                                                release_groups.append(group)
                                            break
                                        else:
                                            print("\t\t\tInvalid input.")
                            else:
                                print("\t\tSkipping... File is in skipped_files.txt")
            except Exception as e:
                send_error_message(e)
        else:
            if path == "":
                send_error_message("\nERROR: Path cannot be empty.")
            else:
                print("\nERROR: " + path + " is an invalid path.\n")


# Optional features below, use at your own risk.
# Activate them in settings.py
def main():
    global bookwalker_check
    global cached_paths
    global processed_files
    global moved_files
    global release_groups
    global skipped_files
    processed_files = []
    moved_files = []
    if (
        os.path.isfile(os.path.join(ROOT_DIR, "cached_paths.txt"))
        and check_for_existing_series_toggle
    ):
        cached_paths = read_lines_from_file(os.path.join(ROOT_DIR, "cached_paths.txt"))
    if (
        (
            cache_each_root_for_each_path_in_paths_at_beginning_toggle
            or not os.path.isfile(os.path.join(ROOT_DIR, "cached_paths.txt"))
        )
        and paths
        and check_for_existing_series_toggle
    ):
        cache_paths()
    if os.path.isfile(os.path.join(ROOT_DIR, "release_groups.txt")):
        release_groups_read = read_lines_from_file(
            os.path.join(ROOT_DIR, "release_groups.txt")
        )
        if release_groups_read:
            release_groups = release_groups_read
    if os.path.isfile(os.path.join(ROOT_DIR, "skipped_files.txt")):
        skipped_files_read = read_lines_from_file(
            os.path.join(ROOT_DIR, "skipped_files.txt")
        )
        if skipped_files_read:
            skipped_files = skipped_files_read
    if delete_unacceptable_files_toggle and (
        download_folders and (unaccepted_file_extensions or unacceptable_keywords)
    ):
        delete_unacceptable_files()
    if delete_chapters_from_downloads_toggle and download_folders:
        delete_chapters_from_downloads()
    if generate_release_group_list_toggle and log_to_file and paths:
        generate_release_group_list_file()
    if rename_files_in_download_folders_toggle and download_folders:
        rename_files_in_download_folders()
    if create_folders_for_items_in_download_folder_toggle and download_folders:
        create_folders_for_items_in_download_folder()
    if rename_dirs_in_download_folder_toggle and download_folders:
        rename_dirs_in_download_folder()
    if check_for_duplicate_volumes_toggle and download_folders:
        check_for_duplicate_volumes(download_folders)
    if extract_covers_toggle and paths:
        extract_covers()
    if check_for_existing_series_toggle and download_folders and paths:
        check_for_existing_series()
        if send_scan_request_to_komga_libraries_toggle and moved_files:
            scan_komga_libraries()
    if check_for_missing_volumes_toggle and paths:
        check_for_missing_volumes()
    if bookwalker_check:
        # currently slowed down to avoid rate limiting,
        # advised not to run on each use, but rather once a week
        check_for_new_volumes_on_bookwalker()  # checks the library against bookwalker for any missing volumes that are released or on pre-order
    if extract_covers_toggle and paths:
        print_stats()


if __name__ == "__main__":
    parse_my_args()  # parses the user's arguments
    if watchdog_toggle:
        print("\nWatchdog is enabled, watching for changes...\n")
        watch = Watcher()
        watch.run()
    else:
        main()
