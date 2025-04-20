# komga_cover_extractor/metadata_utils.py
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from lxml import etree
from bs4 import BeautifulSoup
import urllib.parse

# Import necessary config variables
# Use try-except for robustness during refactoring
try:
    from .config import manga_extensions, novel_extensions, image_extensions
except ImportError:
    print("WARN: Could not import from .config in metadata_utils.py, using placeholders.")
    manga_extensions, novel_extensions, image_extensions = [], [], []

# Import necessary functions from other utils
try:
    from .log_utils import send_message
    from .archive_utils import contains_comic_info, get_file_from_zip # Use functions from archive_utils
    from .file_utils import get_file_extension # Use function from file_utils
except ImportError as e:
     print(f"WARN: Could not import utility functions in metadata_utils.py: {e}")
     # Define placeholders if imports fail
     def send_message(msg, error=False): print(f"{'ERROR: ' if error else ''}{msg}")
     def contains_comic_info(*args): return False
     def get_file_from_zip(*args, **kwargs): return None
     def get_file_extension(f): return os.path.splitext(f)[1]


# Dynamically parse all tags from comicinfo.xml and return a dictionary of the tags
def parse_comicinfo_xml(xml_string):
    """Parses a ComicInfo.xml string into a dictionary."""
    tags = {}
    if not xml_string:
        return tags
    try:
        # Ensure xml_string is bytes for ET.fromstring if it's not already
        if isinstance(xml_string, str):
            xml_string = xml_string.encode('utf-8')
        tree = ET.fromstring(xml_string)
        # Handle potential namespaces if necessary, though ComicInfo usually doesn't use them heavily
        tags = {child.tag.split('}')[-1]: child.text for child in tree if child.text is not None}
    except ET.ParseError as e:
        send_message(f"Failed to parse ComicInfo XML: {e}", error=True) # Use imported log_utils function
    except Exception as e:
        send_message(f"Unexpected error parsing ComicInfo XML: {e}", error=True) # Use imported log_utils function
    return tags

# Dynamically parse all html tags and values and return a dictionary of them
def parse_html_tags(html_content):
    """Parses HTML content (like OPF) into a dictionary of tag_name: text_content."""
    tags = {}
    if not html_content:
        return tags
    try:
        soup = BeautifulSoup(html_content, "lxml") # Use lxml for speed
        # Iterate through all tags and get their name and stripped text content
        for tag in soup.find_all(True):
            # Handle tags with multiple children or complex content if needed
            # For simple key-value pairs, get_text(strip=True) is often sufficient
            tag_name = tag.name
            tag_text = tag.get_text(strip=True)
            if tag_name and tag_text:
                # Handle potential duplicate tags if necessary (e.g., multiple dc:creator)
                # For now, last one wins
                tags[tag_name] = tag_text
    except Exception as e:
        send_message(f"Error parsing HTML/OPF content: {e}", error=True) # Use imported log_utils function
    return tags

# Retrieves the internally stored metadata from the file.
def get_internal_metadata(file_path, extension):
    """Retrieves internal metadata (ComicInfo or OPF) based on file extension."""
    metadata = None
    try:
        if extension in manga_extensions: # Use imported config value
            if contains_comic_info(file_path): # Use imported archive_utils function
                comicinfo_data = get_file_from_zip( # Use imported archive_utils function
                    file_path, ["comicinfo.xml"], ".xml", allow_base=False
                )
                if comicinfo_data:
                    # No need to decode here if parse_comicinfo_xml handles bytes
                    metadata = parse_comicinfo_xml(comicinfo_data) # Use local function
        elif extension in novel_extensions: # Use imported config value
            # Common OPF file names/patterns within EPUBs
            opf_searches = [
                r"content\.opf$", r"package\.opf$", r"standard\.opf$",
                r"volume\.opf$", r"metadata\.opf$", r"\.opf$" # General fallback
            ]
            # Use get_file_from_zip to find the OPF file content
            opf_content = get_file_from_zip(file_path, opf_searches, ".opf", allow_base=False) # Use imported archive_utils function
            if opf_content:
                metadata = parse_html_tags(opf_content) # Use local function
            # else:
            #     send_message(f"No OPF file found in {file_path}.", discord=False) # Use imported log_utils function
    except Exception as e:
        send_message(f"Failed to retrieve metadata from {file_path}: {e}", error=True) # Use imported log_utils function
    return metadata

# Credit to original source: https://alamot.github.io/epub_cover/
# Modified.
# Retrieves the relative path of the cover image within an EPUB.
def get_novel_cover(novel_path):
    """Finds the cover image path specified in EPUB metadata."""
    # Namespaces commonly used in OPF files
    namespaces = {
        "opf": "http://www.idpf.org/2007/opf",
        "dc": "http://purl.org/dc/elements/1.1/",
        "u": "urn:oasis:names:tc:opendocument:xmlns:container" # For container.xml
    }
    try:
        with zipfile.ZipFile(novel_path) as z:
            # 1. Find the rootfile path from container.xml
            try:
                container = z.read("META-INF/container.xml")
                container_tree = etree.fromstring(container)
                rootfile_path = container_tree.xpath(
                    "/u:container/u:rootfiles/u:rootfile/@full-path", namespaces=namespaces
                )
                if not rootfile_path:
                    # send_message(f"No rootfile found in container.xml for {novel_path}", error=True) # Use imported log_utils function
                    return None
                rootfile_path = rootfile_path[0]
            except KeyError:
                 send_message(f"META-INF/container.xml not found in {novel_path}", error=True)
                 return None

            # 2. Read and parse the rootfile (OPF)
            try:
                opf_content = z.read(rootfile_path)
                opf_tree = etree.fromstring(opf_content)
            except KeyError:
                 send_message(f"Root file '{rootfile_path}' not found in {novel_path}", error=True)
                 return None
            except etree.XMLSyntaxError as e:
                 send_message(f"Error parsing OPF file '{rootfile_path}' in {novel_path}: {e}", error=True)
                 return None

            # 3. Find the cover ID from metadata
            # Common ways cover is specified: <meta name="cover" content="cover-id"/> or <meta property="cover-image">#cover-id</meta>
            cover_id = opf_tree.xpath("//opf:metadata/opf:meta[@name='cover']/@content", namespaces=namespaces)
            if not cover_id:
                 # Try alternate property-based lookup if needed (less common for cover)
                 pass # Add alternative xpath if necessary

            if cover_id:
                cover_id = cover_id[0]
                # 4. Find the manifest item with that ID to get the href
                cover_href = opf_tree.xpath(
                    f"//opf:manifest/opf:item[@id='{cover_id}']/@href", namespaces=namespaces
                )
                if cover_href:
                    cover_href = cover_href[0]
                    # 5. Construct the full path relative to the OPF file
                    # Unquote URL encoding like %20
                    cover_href_decoded = urllib.parse.unquote(cover_href)
                    # Join with the directory of the OPF file
                    cover_path = os.path.join(os.path.dirname(rootfile_path), cover_href_decoded)
                    # Normalize path separators
                    return os.path.normpath(cover_path).replace("\\", "/")
                else:
                    # send_message(f"Manifest item with id '{cover_id}' not found in {novel_path}", error=True)
                    pass
            else:
                 # send_message(f"Cover metadata 'meta name=\"cover\"' not found in {novel_path}", error=True)
                 pass

    except zipfile.BadZipFile:
        send_message(f"Bad zip file: {novel_path}", error=True) # Use imported log_utils function
    except Exception as e:
        send_message(f"Error reading EPUB {novel_path}: {e}", error=True) # Use imported log_utils function
    return None


# Checks specific files within an EPUB for premium content indicators.
def is_premium_volume(file_path):
    """Checks known files within an EPUB for 'Premium' indicators."""
    bonus_content_found = False
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            filelist_lower = [name.lower() for name in zf.namelist()]

            # Check for specific bonus file patterns often indicating premium
            if any(re.search(r"bonus_?\d*\.xhtml", name) for name in filelist_lower):
                 # Further check if signup page exists (common in J-Novel Club premium)
                 if any("signup" in name for name in filelist_lower):
                     bonus_content_found = True

            # If no bonus files found, check toc or copyright for explicit mentions
            if not bonus_content_found:
                for name in zf.namelist():
                    base_name_lower = os.path.basename(name).lower()
                    if base_name_lower not in ["toc.xhtml", "copyright.xhtml"]:
                        continue

                    try:
                        with zf.open(name) as file_content_stream:
                            # Read only a portion to check for keywords, avoid loading huge files
                            content_sample = file_content_stream.read(2048).decode("utf-8", errors='ignore')
                            if base_name_lower == "toc.xhtml":
                                # Look for bonus sections in ToC, common in JNC premium
                                if "j-novel" in content_sample.lower() and re.search(
                                    r"Bonus\s+(?:(?:Color\s+)?Illustrations?|(?:Short\s+)?Stories)",
                                    content_sample, re.IGNORECASE):
                                    bonus_content_found = True
                                    break
                            elif base_name_lower == "copyright.xhtml":
                                # Look for explicit "Premium" edition text
                                if re.search(r"Premium(?:\s+E?-?(?:Book|pub))?", content_sample, re.IGNORECASE):
                                    bonus_content_found = True
                                    break
                    except Exception as read_err:
                         send_message(f"Error reading {name} in {file_path}: {read_err}", error=True) # Use imported log_utils function

    except zipfile.BadZipFile:
        send_message(f"Bad zip file, cannot check premium status: {file_path}", error=True) # Use imported log_utils function
    except Exception as e:
        send_message(f"Error checking premium status for {file_path}: {e}", error=True) # Use imported log_utils function
    return bonus_content_found


# Checks if the epub file contains any premium content.
def check_for_premium_content(file_path, extension):
    """Checks filename and internal EPUB files for premium indicators."""
    if extension not in novel_extensions: # Use imported config value
        return False
    # Check filename first
    if re.search(r"\bPremium\b", os.path.basename(file_path), re.IGNORECASE):
        return True
    # Check internal files if filename doesn't indicate premium
    return is_premium_volume(file_path) # Use local function