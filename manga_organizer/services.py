"""Manages all communication with external APIs (Komga, Discord, etc.)."""

from typing import List, Optional, Any
import requests
from .models import Embed
from base64 import b64encode
from urllib.parse import urlparse
import time
from bs4 import BeautifulSoup
from discord_webhook import DiscordEmbed, DiscordWebhook
from functools import lru_cache
import urllib
import re
from datetime import datetime

from . import config

session_objects = {}
webhook_obj = DiscordWebhook(url=None)
last_hook_index = None

def scan_komga_library(library_id: str) -> None:
    """Triggers a library scan in Komga.

    Args:
        library_id (str): The ID of the Komga library to scan.
    """
    if not config.komga_ip:
        print("Komga IP is not set in settings.py. Please set it and try again.")
        return

    if not config.komga_login_email:
        print("Komga Login Email is not set in settings.py. Please set it and try again.")
        return

    if not config.komga_login_password:
        print("Komga Login Password is not set in settings.py. Please set it and try again.")
        return

    komga_url = f"{config.komga_ip}:{config.komga_port}" if config.komga_port else config.komga_ip

    print("\nSending Komga Scan Request:")
    try:
        request = requests.post(
            f"{komga_url}/api/v1/libraries/{library_id}/scan",
            headers={
                "Authorization": "Basic %s"
                % b64encode(
                    f"{config.komga_login_email}:{config.komga_login_password}".encode("utf-8")
                ).decode("utf-8"),
                "Accept": "*/*",
            },
        )
        if request.status_code == 202:
            print(f"\t\tSuccessfully Initiated Scan for: {library_id} Library.")
        else:
            print(
                f"\t\tFailed to Initiate Scan for: {library_id} Library "
                f"Status Code: {request.status_code} Response: {request.text}"
            )
    except Exception as e:
        print(
            f"Failed to Initiate Scan for: {library_id} Komga Library, ERROR: {e}"
        )


def get_komga_libraries(first_run=True) -> List[dict]:
    """Retrieves a list of all libraries from Komga.

    Returns:
        List[dict]: A list of dictionaries, each representing a library.
    """
    results = []

    if not config.komga_ip:
        print("Komga IP is not set in settings.py. Please set it and try again.")
        return

    if not config.komga_login_email:
        print("Komga Login Email is not set in settings.py. Please set it and try again.")
        return

    if not config.komga_login_password:
        print("Komga Login Password is not set in settings.py. Please set it and try again.")
        return

    komga_url = f"{config.komga_ip}:{config.komga_port}" if config.komga_port else config.komga_ip

    try:
        request = requests.get(
            f"{komga_url}/api/v1/libraries",
            headers={
                "Authorization": "Basic %s"
                % b64encode(
                    f"{config.komga_login_email}:{config.komga_login_password}".encode("utf-8")
                ).decode("utf-8"),
                "Accept": "*/*",
            },
        )
        if request.status_code == 200:
            results = request.json()
        else:
            print(
                f"\t\tFailed to Get Komga Libraries "
                f"Status Code: {request.status_code} "
                f"Response: {request.text}"
            )
    except Exception as e:
        if first_run and "104" in str(e):
            time.sleep(60)
            results = get_komga_libraries(first_run=False)
        else:
            print(f"Failed to Get Komga Libraries, ERROR: {e}")
    return results


@lru_cache(maxsize=10)
def pick_webhook(hook, passed_webhook=None, url=None):
    global last_hook_index

    if passed_webhook:
        hook = passed_webhook
    elif url:
        hook = url
    elif config.discord_webhook_url:
        if last_hook_index is None or last_hook_index == len(config.discord_webhook_url) - 1:
            hook = config.discord_webhook_url[0]
        else:
            hook = config.discord_webhook_url[last_hook_index + 1]
        last_hook_index = config.discord_webhook_url.index(hook)

    return hook


def send_discord_message(
    message,
    embeds=[],
    url=None,
    rate_limit=True,
    timestamp=True,
    passed_webhook=None,
    image=None,
    image_local=None,
) -> None:
    """Sends a message to a Discord webhook.

    Args:
        embed (Embed): The embed object to send.
    """
    global grouped_notifications, webhook_obj

    sent_status = False
    hook = None
    hook = pick_webhook(hook, passed_webhook, url)

    try:
        if hook:
            webhook_obj.url = hook

            if rate_limit:
                webhook_obj.rate_limit_retry = rate_limit

            if embeds:
                # Limit the number of embeds to 10
                for index, embed in enumerate(embeds[:10], start=1):
                    if config.script_version_text:
                        embed.embed.set_footer(text=config.script_version_text)

                    if timestamp and (
                        not hasattr(embed.embed, "timestamp")
                        or not embed.embed.timestamp
                    ):
                        embed.embed.set_timestamp()

                    if image and not image_local:
                        embed.embed.set_image(url=image)
                    elif embed.file:
                        file_name = (
                            "cover.jpg" if len(embeds) == 1 else f"cover_{index}.jpg"
                        )
                        webhook_obj.add_file(file=embed.file, filename=file_name)
                        embed.embed.set_image(url=f"attachment://{file_name}")

                    webhook_obj.add_embed(embed.embed)
            elif message:
                webhook_obj.content = message

            webhook_obj.execute()
            sent_status = True
    except Exception as e:
        print(f"{e}")
        # Reset the webhook object
        webhook_obj = DiscordWebhook(url=None)
        return sent_status

    # Reset the webhook object
    webhook_obj = DiscordWebhook(url=None)

    return sent_status


def group_notification(notifications, embed, passed_webhook=None):
    """Groups and sends notifications.
    """
    failed_attempts = 0

    if len(notifications) >= config.discord_embed_limit:
        while notifications:
            message_status = send_discord_message(
                None, notifications, passed_webhook=passed_webhook
            )
            if (
                message_status
                or (failed_attempts >= len(config.discord_webhook_url) and not passed_webhook)
                or (passed_webhook and failed_attempts >= 1)
            ):
                notifications = []
            else:
                failed_attempts += 1

    # Set timestamp on embed
    embed.embed.set_timestamp()

    # Add embed to list
    if embed not in notifications:
        notifications.append(embed)

    return notifications


def handle_fields(embed, fields):
    """Handles fields for Discord notifications.
    """
    if fields:
        # An embed can contain a maximum of 25 fields
        fields = fields[:25]

        for field in fields:
            # A field name/title is limited to 256 characters
            if len(field["name"]) > 256:
                field["name"] = (
                    field["name"][:253] + "..."
                    if not field["name"].endswith("```")
                    else field["name"][:-3][:250] + "...```"
                )

            # The value of the field is limited to 1024 characters
            if len(field["value"]) > 1024:
                field["value"] = (
                    field["value"][:1021] + "..."
                    if not field["value"].endswith("```")
                    else field["value"][:-3][:1018] + "...```"
                )

            embed.add_embed_field(
                name=field["name"],
                value=field["value"],
                inline=field["inline"],
            )
    return embed


def get_session_object(url):
    """Creates and returns a requests Session object.

    Returns:
        requests.Session: A configured requests session.
    """
    domain = urlparse(url).netloc.split(":")[0]
    if domain not in session_objects:
        # Create a new session object and set a default User-Agent header
        session_object = requests.Session()
        session_object.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"
            }
        )
        session_objects[domain] = session_object
    return session_objects[domain]


def scrape_url(url, strainer=None, headers=None, cookies=None, proxy=None):
    """Scrapes content from a URL.

    Args:
        session (requests.Session): The session to use for the request.
        url (str): The URL to scrape.

    Returns:
        Optional[str]: The page content, or None on failure.
    """
    try:
        session_object = get_session_object(url)

        # Create a dictionary of request parameters with only non-None values
        request_params = {
            "url": url,
            "headers": headers,
            "cookies": cookies,
            "proxies": proxy,
            "timeout": 10,
        }
        response = session_object.get(
            **{k: v for k, v in request_params.items() if v is not None}
        )

        # Raise an exception if the status code indicates rate limiting
        if response.status_code == 403:
            raise Exception("Too many requests, we're being rate-limited!")

        soup = None
        if strainer:
            # Use the strainer to parse only specific parts of the HTML document
            soup = BeautifulSoup(response.content, "lxml", parse_only=strainer)
        else:
            soup = BeautifulSoup(response.content, "lxml")

        return soup
    except requests.exceptions.RequestException as e:
        print(f"Error scraping URL: {e}")
        return None


def search_bookwalker(
    query,
    type,
    print_info=False,
    alternative_search=False,
    shortened_search=False,
    total_pages_to_scrape=5,
):
    """Searches for a series on Bookwalker.

    Args:
        session (requests.Session): The session to use for the request.
        series_name (str): The name of the series to search for.

    Returns:
        dict: A dictionary containing search results.
    """
    from .utils import get_shortened_title, clean_str, similar, get_release_number_cache, get_file_part, set_num_as_float_or_int, is_one_shot
    from .models import BookwalkerBook, BookwalkerSeries

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

    done = False
    search_type = type
    count = 0

    page_count = 1
    page_count_url = f"&page={page_count}"

    search = urllib.parse.quote(query)
    base_url = "https://global.bookwalker.jp/search/?word="
    chapter_exclusion_url = "&np=1&qnot%5B%5D=Chapter&x=13&y=16"
    series_only = "&np=0"
    series_url = f"{base_url}{search}{series_only}"
    original_similarity_score = config.required_similarity_score

    # Enables NSFW Search Results
    default_cookies = {
        "glSafeSearch": "1",
        "safeSearch": "111",
    }
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
    }

    if not alternative_search:
        keyword = "\t\tSearch: " if not shortened_search else "\n\t\tShortened Search: "
        category = "MANGA" if search_type.lower() == "m" else "NOVEL"
        series_info = f"({series_url})" if shortened_search else ""

        print(f"{keyword}{query}\n\t\tCategory: {category} {series_info}")

    series_page = scrape_url(
        series_url,
        cookies=default_cookies,
        headers=default_headers,
    )

    series_list_li = []

    if series_page:
        # find ul class o-tile-list in series_page
        series_list_ul = series_page.find_all("ul", class_="o-tile-list")
        if series_list_ul:
            # find all li class="o-tile"
            series_list_li = len(series_list_ul[0].find_all("li", class_="o-tile"))

    if series_list_li == 1:
        config.required_similarity_score = original_similarity_score - 0.03

    while page_count < total_pages_to_scrape + 1:
        page_count_url = f"&page={page_count}"
        url = f"{base_url}{search}{page_count_url}"
        category = ""

        if search_type.lower() == "m":
            category = (
                bookwalker_manga_category
                if not alternative_search
                else bookwalker_intll_manga_category
            )
        elif search_type.lower() == "l":
            category = bookwalker_light_novel_category

        url += category
        series_url += category

        if shortened_search and series_list_li != 1:
            print("\t\t\t- search does not contain exactly one series, skipping...\n")
            return []

        # url += chapter_exclusion_url
        page_count += 1

        # scrape url page
        page = scrape_url(
            url,
            cookies=default_cookies,
            headers=default_headers,
        )

        if not page:
            alternate_page = None
            if search_type.lower() == "m" and not alternative_search:
                alternate_page = scrape_url(
                    url,
                    cookies=default_cookies,
                    headers=default_headers,
                )
            if not alternate_page:
                print("\t\t\tError: Empty page")
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
            highest_num = max(int(li.text) for li in li_list if li.text.isdigit())

            if highest_num == 0:
                print("\t\t\tNo pages found.")
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
                time.sleep(config.sleep_timer_bk / 2)
            if alternate_result:
                return alternate_result
            if not alternative_search:
                print("\t\t\t! NO BOOKS FOUND ON BOOKWALKER !")
            no_book_result_searches.append(query)
            continue

        o_tile_list = list_area_ul.find_all("li", class_="o-tile")
        print(
            f"\t\t\tPage: {page_count - 1} of {total_pages_to_scrape} ({url})\n\t\t\t\tItems: {len(o_tile_list)}"
        )

        for item in o_tile_list:
            preview_image_url = None
            description = ""
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

                tag_dict = {
                    "a-tag-manga": None,
                    "a-tag-light-novel": None,
                    "a-tag-other": None,
                    "a-tag-chapter": None,
                    "a-tag-simulpub": None,
                }

                for i in li_tag_item:
                    for tag_name in tag_dict.keys():
                        if i.find("div", class_=tag_name):
                            tag_dict[tag_name] = i.find("div", class_=tag_name)

                a_tag_chapter = tag_dict["a-tag-chapter"]
                a_tag_simulpub = tag_dict["a-tag-simulpub"]
                a_tag_manga = tag_dict["a-tag-manga"]
                a_tag_light_novel = tag_dict["a-tag-light-novel"]
                a_tag_other = tag_dict["a-tag-other"]

                book_type = a_tag_manga or a_tag_light_novel or a_tag_other

                if book_type:
                    book_type = book_type.get_text()
                    book_type = re.sub(r"\n|\t|\r", "", book_type).strip()
                else:
                    book_type = "Unknown"

                title = o_tile_book_info.find("h2", class_="a-tile-ttl").text.strip()
                original_title = title

                item_index = o_tile_list.index(item)

                if title:
                    print(f"\t\t\t\t\t[{item_index + 1}] {title}")

                    # remove brackets
                    title = (
                        remove_brackets(title) if contains_brackets(title) else title
                    )

                    # unidecode the title
                    title = unidecode(title)

                    # replace any remaining unicode characters in the title with spaces
                    title = re.sub(r"[^\x00-\x7F]+", " ", title)

                    # remove any extra spaces
                    title = remove_dual_space(title).strip()

                if a_tag_chapter or a_tag_simulpub:
                    chapter_releases.append(title)
                    continue

                if (
                    title
                    and ("chapter" in title.lower() or re.search(r"\s#\d+\b", title))
                    and not re.search(r"re([-_. :]+)?zero", title, re.IGNORECASE)
                ):
                    continue

                part = get_file_part(title)

                if part and re.search(r"(\b(Part)([-_. ]+|)\d+(\.\d+)?)", title):
                    title = re.sub(r"(\b(Part)([-_. ]+|)\d+(\.\d+)?)", "", title)
                    title = remove_dual_space(title).strip()

                volume_number = ""

                # Remove single keyword letter from exclusion, and "Book" and "Novel"
                # Single keywords aren't enough to reject a volume and
                # the keywords "Book" and "Novel" are common in one-shot titles
                modified_volume_keywords = [
                    keyword
                    for keyword in config.volume_keywords
                    if len(keyword) > 1 and keyword not in ["Books?", "Novels?"]
                ]
                modified_volume_regex_keywords = (
                    "(?<![A-Za-z])" + "|(?<![A-Za-z])".join(modified_volume_keywords)
                )

                # Checks that the title doesn't contain any numbers
                contains_no_numbers = re.search(r"^[^\d]*$", title)

                # Checks if the title contains any of the volume keywords
                contains_volume_keyword = re.search(
                    r"(\b(%s)([-_. ]|)\b)" % modified_volume_regex_keywords, title
                )

                if not contains_volume_keyword or contains_no_numbers:
                    if not re.search(
                        r"(([0-9]+)((([-_.]|)([0-9]+))+|))(\s+)?-(\s+)?(([0-9]+)((([-_.]|)([0-9]+))+|))",
                        title,
                    ):
                        volume_number = re.search(
                            r"(\b(?!2(?:\d{3})\b)\d+\b(\.?[0-9]+)?([-_][0-9]+\.?[0-9]+)?)$",
                            title,
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
                        volume_number = title_split if title_split else None

                    if volume_number and not isinstance(volume_number, list):
                        if hasattr(volume_number, "group"):
                            volume_number = volume_number.group(1)
                        else:
                            if title not in no_volume_number:
                                no_volume_number.append(title)
                            continue
                    elif title and is_one_shot(title, skip_folder_check=True):
                        volume_number = 1
                    elif not volume_number and not isinstance(volume_number, list):
                        if title not in no_volume_number:
                            no_volume_number.append(title)
                        continue
                else:
                    volume_number = get_release_number_cache(title)

                volume_number = set_num_as_float_or_int(volume_number)

                if not contains_volume_keyword:
                    title = re.sub(
                        r"(\b|\s)((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(%s|)(\.|)([-_. ]|)(((?!2(?:\d{3})\b)\d+)(\b|\s))$.*"
                        % modified_volume_regex_keywords,
                        "",
                        title,
                        flags=re.IGNORECASE,
                    ).strip()
                    if title.endswith(","):
                        title = title[:-1].strip()
                    title = title.replace("\n", "").replace("\t", "")
                    title = re.sub(rf"\b{volume_number}\b", "", title)
                    title = re.sub(r"(\s{2,})", " ", title).strip()
                    title = re.sub(r"(\((.*)\)$)", "", title).strip()
                else:
                    title = re.sub(
                        r"(\b|\s)((\s|)-(\s|)|)(Part|)(\[|\(|\{)?(%s)(\.|)([-_. ]|)([0-9]+)(\b|\s).*"
                        % modified_volume_regex_keywords,
                        "",
                        title,
                        flags=re.IGNORECASE,
                    ).strip()

                shortened_title = get_shortened_title(title)
                shortened_query = get_shortened_title(query)

                clean_shortened_title = (
                    clean_str(shortened_title).lower().strip()
                    if shortened_title
                    else ""
                )
                clean_shortened_query = (
                    clean_str(shortened_query).lower().strip()
                    if shortened_query
                    else ""
                )

                clean_title = clean_str(title).lower().strip()
                clean_query = clean_str(query).lower().strip()

                score = similar(clean_title, clean_query)
                print(f"\t\t\t\t\t\tBookwalker: {clean_title}")
                print(f"\t\t\t\t\t\tLibrary:    {clean_query}")
                print(
                    f"\t\t\t\t\t\tScore: {score} | Match: {score >= config.required_similarity_score} (>= {config.required_similarity_score})"
                )
                print(f"\t\t\t\t\t\tVolume Number: {volume_number}")
                if part:
                    print(f"\t\t\t\t\t\tVolume Part: {part}")

                score_two = 0
                if series_list_li == 1 and not score >= config.required_similarity_score:
                    score_two = similar(clean_shortened_title, clean_query)
                    print(
                        f"\n\t\t\t\t\t\tBookwalker: {clean_shortened_title if shortened_title and clean_shortened_title else clean_title}"
                    )
                    print(
                        f"\t\t\t\t\t\tLibrary:    {clean_query if shortened_title and clean_shortened_title else clean_shortened_query}"
                    )
                    print(
                        f"\t\t\t\t\t\tScore: {score_two} | Match: {score_two >= config.required_similarity_score} (>= {config.required_similarity_score})"
                    )
                    print(f"\t\t\t\t\t\tVolume Number: {volume_number}")
                    if part:
                        print(f"\t\t\t\t\t\tVolume Part: {part}")

                if not (score >= config.required_similarity_score) and not (
                    score_two >= config.required_similarity_score
                ):
                    message = f'"{clean_title}": {score} [{book_type}]'
                    if message not in similarity_match_failures:
                        similarity_match_failures.append(message)
                    config.required_similarity_score = original_similarity_score
                    continue

                # html from url
                page_two = scrape_url(url)

                # parse html
                soup_two = page_two.find("div", class_="product-detail-inner")

                if not soup_two:
                    print("No soup_two")
                    continue

                # Find the book's preview image
                # Find <meta property="og:image" and get the content
                meta_property_og_image = page_two.find("meta", {"property": "og:image"})
                if meta_property_og_image and meta_property_og_image[
                    "content"
                ].startswith("http"):
                    preview_image_url = meta_property_og_image["content"]

                # Backup method for lower resolution preview image
                if not preview_image_url or "ogp-mature" in preview_image_url:
                    # find the img src inside of <div class="book-img">
                    div_book_img = page_two.find("div", class_="book-img")
                    if div_book_img:
                        img_src = div_book_img.find("img")["src"]
                        if img_src and img_src.startswith("http"):
                            preview_image_url = img_src

                # Find the book's description
                div_itemprop_description = page_two.find(
                    "div", {"itemprop": "description"}
                )

                if div_itemprop_description:
                    # find all <p> in div_itemprop_description
                    p_items = div_itemprop_description.find_all("p")
                    if p_items:
                        if len(p_items) > 1:
                            description = "\n".join(
                                p_item.text.strip()
                                for p_item in p_items
                                if p_item["class"][0] != "synopsis-lead"
                                and p_item.text.strip()
                            )
                        else:
                            description = p_items[0].text.strip()

                # find table class="product-detail"
                product_detail = soup_two.find("table", class_="product-detail")

                # find all <td> inside of product-detail
                product_detail_td = product_detail.find_all("td")
                date = ""
                is_released = None

                for detail in product_detail_td:
                    date_match = re.search(
                        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(\s+)?\d{2}([,]+)?\s+\d{4}",
                        detail.text,
                        re.IGNORECASE,
                    )

                    # get the th from the detail
                    th = detail.find_previous_sibling("th")

                    # get the text from the th
                    th_text = th.text

                    if th_text in ["Series Title", "Alternative Title"]:
                        series_title = detail.text

                        # Remove punctuation, convert to lowercase, and strip leading/trailing whitespaces
                        series_title = clean_str(series_title).lower().strip()

                        # Check similarity with the clean_query and clean_shortened_query
                        if not similar(
                            series_title, clean_query
                        ) >= config.required_similarity_score and not similar(
                            series_title, clean_shortened_query
                        ):
                            continue

                    if date_match:
                        # Clean up the date string by removing non-alphanumeric characters
                        date_match = re.sub(r"[^\s\w]", "", date_match.group())

                        # Split the date into its components (month, day, year)
                        date_parts = date_match.split()
                        month = date_parts[0][:3]
                        day = date_parts[1]
                        year = date_parts[2]

                        # Convert the date string to a datetime object with no time information
                        date = datetime.strptime(f"{month} {day} {year}", "%b %d %Y")

                        # Check if the date is in the past (released) or future (not released yet)
                        is_released = date < datetime.now()

                        # Format the date as a string in "YYYY-MM-DD" format
                        date = date.strftime("%Y-%m-%d")

                        # Break out of the loop since a valid date has been found
                        break

                book = BookwalkerBook(
                    title,
                    original_title,
                    volume_number,
                    part,
                    date,
                    is_released,
                    0.00,
                    url,
                    thumbnail,
                    book_type,
                    description,
                    preview_image_url,
                )
                books.append(book)
            except Exception as e:
                print(e)
                errors.append(url)
                continue

        for book in books:
            matching_books = get_all_matching_books(books, book.book_type, book.title)
            if matching_books:
                series_list.append(
                    BookwalkerSeries(
                        book.title,
                        matching_books,
                        len(matching_books),
                        book.book_type,
                    )
                )

    series_list = combine_series(series_list)
    config.required_similarity_score = original_similarity_score

    # print(f"\t\tSleeping for {config.sleep_timer_bk} to avoid being rate-limited...")
    time.sleep(config.sleep_timer_bk)

    if len(series_list) == 1 and len(series_list[0].books) > 0:
        return series_list[0].books
    elif len(series_list) > 1:
        print("\t\t\tNumber of series from bookwalker search is greater than one.")
        print(f"\t\t\tNum: {len(series_list)}")
        return []
    else:
        if not alternative_search:
            print("\t\t\tNo matching books found.")
        return []

def get_all_matching_books(books, book_type, title):
    from .utils import similar, clean_str, get_shortened_title
    matching_books = []
    short_title = get_shortened_title(title)

    for book in books:
        short_title_two = get_shortened_title(book.title)
        if book.book_type == book_type and (
            book.title == title
            or (
                (
                    similar(clean_str(book.title), clean_str(title))
                    >= config.required_similarity_score
                )
                or (
                    (short_title and short_title_two)
                    and similar(
                        clean_str(short_title_two),
                        clean_str(short_title),
                    )
                    >= config.required_similarity_score
                )
            )
        ):
            matching_books.append(book)

    # remove them from books
    for book in matching_books:
        books.remove(book)

    return matching_books

def combine_series(series_list):
    from .utils import similar, clean_str
    combined_series = []

    for series in series_list:
        # Sort books by volume number
        series.books.sort(
            key=lambda x: (str(x.volume_number), str(x.part).strip().split(",")[0])
        )

        # Check if series can be combined with existing combined_series
        combined = False
        for combined_series_item in combined_series:
            if series.book_type == combined_series_item.book_type and (
                series.title.lower().strip()
                == combined_series_item.title.lower().strip()
                or similar(
                    clean_str(series.title).lower().strip(),
                    clean_str(combined_series_item.title).lower().strip(),
                )
                >= config.required_similarity_score
            ):
                combined_series_item.books.extend(series.books)
                combined_series_item.book_count = len(combined_series_item.books)
                combined = True
                break

        # If series cannot be combined, add it to combined_series
        if not combined:
            combined_series.append(series)

    return combined_series