"""Isolates all logic for handling archive files (.zip, .cbz, .rar, etc.).

This module provides functions for reading, extracting, and creating
archive files, abstracting the complexities of different archive formats.
"""

import os
import shutil
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from typing import List, Optional

import patoolib
import py7zr
import rarfile
from lxml import etree

from . import constants, filesystem
from .constants import rar_extensions, seven_zip_extensions
from .utils import send_message


def extract(archive_path: str, output_dir: str) -> bool:
    """Extracts an archive file.

    Args:
        archive_path (str): The path to the archive file.
        output_dir (str): The directory to extract the contents to.
    """
    successfull = False
    extension = filesystem.get_file_extension(archive_path)
    try:
        if extension in rar_extensions:
            with rarfile.RarFile(archive_path) as rar:
                rar.extractall(output_dir)
                successfull = True
        elif extension in seven_zip_extensions:
            with py7zr.SevenZipFile(archive_path, "r") as archive:
                archive.extractall(output_dir)
                successfull = True
    except Exception as e:
        send_message(f"Error extracting {archive_path}: {e}", error=True)
    return successfull


def compress(source_dir: str, archive_path: str) -> bool:
    """Compresses a directory into a .cbz archive.

    Args:
        source_dir (str): The directory to compress.
        archive_path (str): The path to the output .cbz file.
    """
    successfull = False
    try:
        with zipfile.ZipFile(archive_path, "w") as zip:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    zip.write(
                        os.path.join(root, file),
                        os.path.join(root[len(source_dir) + 1 :], file),
                    )
            successfull = True
    except Exception as e:
        send_message(f"Error compressing {source_dir}: {e}", error=True)
    return successfull


def get_zip_comment(archive_path: str) -> Optional[str]:
    """Reads the comment from a ZIP archive.

    Args:
        archive_path (str): The path to the .zip file.

    Returns:
        Optional[str]: The comment, or None if not found.
    """
    comment = ""
    try:
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            if zip_ref.comment:
                comment = zip_ref.comment.decode("utf-8")
    except Exception as e:
        send_message(
            f"\tFailed to get zip comment for: {archive_path} - Error: {e}", error=True
        )
    return comment


def contains_comic_info(archive_path: str) -> bool:
    """Checks if a .cbz archive contains a ComicInfo.xml file.

    Args:
        archive_path (str): The path to the archive.

    Returns:
        bool: True if it contains ComicInfo.xml, False otherwise.
    """
    result = False
    try:
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            if "comicinfo.xml" in map(str.lower, zip_ref.namelist()):
                result = True
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        send_message(f"\tFile: {archive_path}\n\t\tERROR: {e}", error=True)
    return result


def convert_to_cbz(archive_path: str) -> None:
    """Converts an archive (e.g., .rar, .7z) to .cbz format.

    Args:
        archive_path (str): The path to the archive to convert.
    """
    source_file = archive_path
    repacked_file = f"{filesystem.get_extensionless_name(source_file)}.cbz"

    # check that the cbz file doesn't already exist
    if os.path.isfile(repacked_file):
        # if the file is zero bytes, delete it and continue, otherwise skip
        if filesystem.get_file_size(repacked_file) == 0:
            send_message(
                "\t\t\tCBZ file is zero bytes, deleting...",
                discord=False,
            )
            filesystem.remove_file(repacked_file)
        elif not zipfile.is_zipfile(repacked_file):
            send_message(
                "\t\t\tCBZ file is not a valid zip file, deleting...",
                discord=False,
            )
            filesystem.remove_file(repacked_file)
        else:
            send_message(
                "\t\t\tCBZ file already exists, skipping...",
                discord=False,
            )
            return

    temp_dir = tempfile.mkdtemp("_source2cbz")

    # if there's already contents in the temp directory, delete it
    if os.listdir(temp_dir):
        send_message(
            f"\t\t\tTemp directory {temp_dir} is not empty, deleting...",
            discord=False,
        )
        filesystem.remove_folder(temp_dir)
        # recreate the temp directory
        temp_dir = tempfile.mkdtemp("source2cbz")

    if not os.path.isdir(temp_dir):
        send_message(
            f"\t\t\tFailed to create temp directory {temp_dir}",
            error=True,
        )
        return

    send_message(
        f"\t\t\tCreated temp directory {temp_dir}",
        discord=False,
    )

    # Extract the archive to the temp directory
    extract_status = extract(source_file, temp_dir)

    if not extract_status:
        send_message(
            f"\t\t\tFailed to extract {source_file}",
            error=True,
        )
        # remove temp directory
        filesystem.remove_folder(temp_dir)
        return

    print(f"\t\t\tExtracted contents to {temp_dir}")

    compress_status = compress(temp_dir, repacked_file)

    if not compress_status:
        # remove temp directory
        filesystem.remove_folder(temp_dir)
        return

    print(f"\t\t\tCompressed to {repacked_file}")

    # Remove temp directory
    filesystem.remove_folder(temp_dir)

    send_message(
        f"\t\t\tConverted {source_file} to {repacked_file}",
        discord=False,
    )

    # remove the source file
    filesystem.remove_file(source_file)


def get_novel_cover(novel_path: str) -> Optional[str]:
    """Retrieves the path of the cover image from a .epub file.

    This function parses the XML structure of an .epub file to find the
    cover image's path. It first looks for the rootfile in META-INF/container.xml,
    then parses the rootfile to find the cover image's ID. Finally, it uses the
    ID to find the cover image's href in the manifest.

    Args:
        novel_path (str): The path to the .epub file.

    Returns:
        Optional[str]: The path to the cover image, or None if not found.
    """
    try:
        with zipfile.ZipFile(novel_path) as z:
            t = etree.fromstring(z.read("META-INF/container.xml"))
            rootfile_path = t.xpath(
                "/u:container/u:rootfiles/u:rootfile",
                namespaces=constants.XML_NAMESPACES,
            )
            if rootfile_path:
                rootfile_path = rootfile_path[0].get("full-path")
                t = etree.fromstring(z.read(rootfile_path))
                cover_id = t.xpath(
                    "//opf:metadata/opf:meta[@name='cover']",
                    namespaces=constants.XML_NAMESPACES,
                )
                if cover_id:
                    cover_id = cover_id[0].get("content")
                    cover_href = t.xpath(
                        f"//opf:manifest/opf:item[@id='{cover_id}']",
                        namespaces=constants.XML_NAMESPACES,
                    )
                    if cover_href:
                        cover_href = cover_href[0].get("href")
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