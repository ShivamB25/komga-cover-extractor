import re
import zipfile
import xml.etree.ElementTree as ET
from lxml import etree
from bs4 import BeautifulSoup
from titlecase import titlecase
import urllib
import os

from utils.helpers import send_message, remove_dual_space
from config.constants import manga_extensions, novel_extensions
from filesystem.archive_handler import get_file_from_zip, contains_comic_info


# Retrieves the internally stored metadata from the file.
# Retrieves the internal metadata from the file based on its extension.
def get_internal_metadata(file_path, extension):
    metadata = None
    try:
        if extension in manga_extensions:
            if contains_comic_info(file_path):
                comicinfo = get_file_from_zip(
                    file_path, ["comicinfo.xml"], ".xml", allow_base=False
                )
                if comicinfo:
                    comicinfo = comicinfo.decode("utf-8")
                    metadata = parse_comicinfo_xml(comicinfo)
        elif extension in novel_extensions:
            regex_searches = [
                r"content.opf",
                r"package.opf",
                r"standard.opf",
                r"volume.opf",
                r"metadata.opf",
                r"978.*.opf",
            ]
            opf = get_file_from_zip(file_path, regex_searches, ".opf")
            if opf:
                metadata = parse_html_tags(opf)
            if not metadata:
                send_message(
                    f"No opf file found in {file_path}. Skipping metadata retrieval.",
                    discord=False,
                )
    except Exception as e:
        send_message(
            f"Failed to retrieve metadata from {file_path}\nERROR: {e}", error=True
        )
    return metadata


# Credit to original source: https://alamot.github.io/epub_cover/
# Modified by me.
# Retrieves the inner novel cover
def get_novel_cover(novel_path):
    namespaces = {
        "calibre": "http://calibre.kovidgoyal.net/2009/metadata",
        "dc": "http://purl.org/dc/elements/1.1/",
        "dcterms": "http://purl.org/dc/terms/",
        "opf": "http://www.idpf.org/2007/opf",
        "u": "urn:oasis:names:tc:opendocument:xmlns:container",
        "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    }

    try:
        with zipfile.ZipFile(novel_path) as z:
            t = etree.fromstring(z.read("META-INF/container.xml"))
            rootfile_path = t.xpath(
                "/u:container/u:rootfiles/u:rootfile", namespaces=namespaces
            )
            if rootfile_path:
                rootfile_path = rootfile_path.get("full-path")
                t = etree.fromstring(z.read(rootfile_path))
                cover_id = t.xpath(
                    "//opf:metadata/opf:meta[@name='cover']", namespaces=namespaces
                )
                if cover_id:
                    cover_id = cover_id.get("content")
                    cover_href = t.xpath(
                        f"//opf:manifest/opf:item[@id='{cover_id}']",
                        namespaces=namespaces,
                    )
                    if cover_href:
                        cover_href = cover_href.get("href")
                        if "%" in cover_href:
                            cover_href = urllib.parse.unquote(cover_href)
                        cover_path = os.path.join(
                            os.path.dirname(rootfile_path), cover_href
                        )
                        return cover_path
                    else:
                        print("\t\t\tNo cover_href found in get_novel_cover()")
                else:
                    print("\t\t\tNo cover_id found in get_novel_cover()")
            else:
                print(
                    "\t\t\tNo rootfile_path found in META-INF/container.xml in get_novel_cover()"
                )
    except Exception as e:
        send_message(str(e), error=True)
    return None


# dynamically parse all tags from comicinfo.xml and return a dictionary of the tags
def parse_comicinfo_xml(xml_file):
    tags = {}
    if xml_file:
        try:
            tree = ET.fromstring(xml_file)
            tags = {child.tag: child.text for child in tree}
        except Exception as e:
            send_message(
                f"Attempted to parse comicinfo.xml: {xml_file}\nERROR: {e}",
                error=True,
            )
            return tags
    return tags


# Retrieves the publisher from the passed in metadata
def get_publisher_from_meta(metadata):
    # Cleans the publisher name
    def clean_publisher_name(name):
        name = titlecase(name)
        name = remove_dual_space(name)
        if "llc" in name.lower():
            name = re.sub(r", LLC.*", "", name, flags=re.IGNORECASE).strip()
        return name

    publisher = None

    if metadata:
        if "Publisher" in metadata:
            publisher = clean_publisher_name(metadata["Publisher"])
        elif "dc:publisher" in metadata:
            publisher = clean_publisher_name(metadata["dc:publisher"])
            publisher = publisher.replace("LLC", "").strip()
            publisher = publisher.replace(":", " - ").strip()
            publisher = remove_dual_space(publisher)

    return publisher


# Get the release year from the file metadata, if present, otherwise from the file name
def get_release_year(name, metadata=None):
    result = None

    match = re.search(r"(\(|\[|\{)(\d{4})(\)|\]|\})", name, re.IGNORECASE)
    if match:
        result = int(re.sub(r"(\(|\[|\{)|(\)|\]|\})", "", match.group()))

    if not result and metadata:
        release_year_from_file = None

        if "Summary" in metadata and "Year" in metadata:
            release_year_from_file = metadata["Year"]
        elif "dc:description" in metadata and "dc:date" in metadata:
            release_year_from_file = metadata["dc:date"].strip()
            release_year_from_file = re.search(r"\d{4}", release_year_from_file)
            release_year_from_file = (
                release_year_from_file.group() if release_year_from_file else None
            )

        if release_year_from_file and release_year_from_file.isdigit():
            result = int(release_year_from_file)
            if result < 1950:
                result = None

    return result


# dynamically parse all html tags and values and return a dictionary of them
def parse_html_tags(html):
    soup = BeautifulSoup(html, "html.parser")
    tags = {tag.name: tag.get_text() for tag in soup.find_all(True)}
    return tags