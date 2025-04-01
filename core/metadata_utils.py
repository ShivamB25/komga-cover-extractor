import zipfile
import xml.etree.ElementTree as ET
import urllib.parse

from bs4 import BeautifulSoup

from settings import manga_extensions, novel_extensions

def get_file_from_zip(zip_file, searches, extension=None, allow_base=True):
    result = None
    try:
        with zipfile.ZipFile(zip_file, "r") as z:
            file_list = [
                item
                for item in z.namelist()
                if item.endswith(extension) or not extension
            ]

            for path in file_list:
                mod_file_name = (
                    os.path.basename(path).lower()
                    if allow_base
                    else (
                        re.sub(os.path.basename(path), "", path).lower()
                        if re.sub(os.path.basename(path), "", path).lower()
                        else path.lower()
                    )
                )
                found = any(
                    (
                        item
                        for item in searches
                        if re.search(item, mod_file_name, re.IGNORECASE)
                    ),
                )
                if found:
                    result = z.read(path)
                    break
    except (zipfile.BadZipFile, FileNotFoundError):
        pass
    return result

def parse_comicinfo_xml(xml_file):
    tags = {}
    if xml_file:
        try:
            tree = ET.fromstring(xml_file)
            tags = {child.tag: child.text for child in tree}
        except Exception:
            return tags
    return tags

def parse_html_tags(html):
    soup = BeautifulSoup(html, "html.parser")
    tags = {tag.name: tag.get_text() for tag in soup.find_all(True)}
    return tags

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
                rootfile_path = rootfile_path[0].get("full-path")
                t = etree.fromstring(z.read(rootfile_path))
                cover_id = t.xpath(
                    "//opf:metadata/opf:meta[@name='cover']", namespaces=namespaces
                )
                if cover_id:
                    cover_id = cover_id[0].get("content")
                    cover_href = t.xpath(
                        f"//opf:manifest/opf:item[@id='{cover_id}']",
                        namespaces=namespaces,
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
                    print("\t\t\tNo cover_id found in get_novel_cover()")
            else:
                print(
                    "\t\t\tNo rootfile_path found in META-INF/container.xml in get_novel_cover()"
                )
    except Exception:
        pass
    return None

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
    except Exception:
        pass
    return metadata