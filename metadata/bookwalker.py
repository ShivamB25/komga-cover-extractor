import re
import time
import urllib.parse
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from unidecode import unidecode

# Assuming these will be moved to appropriate modules or handled via config
from settings import (
    required_similarity_score,
    sleep_timer_bk,
    bookwalker_webhook_urls,
    log_to_file,
    bookwalker_logo_url,
    discord_embed_limit,
    grey_color,
    preorder_blue_color,
)
from core.string_utils import (
    similar,
    clean_str,
    get_shortened_title,
    get_subtitle_from_dash,
    remove_brackets,
    contains_brackets,
    remove_dual_space,
    get_file_part, # Added based on usage in search_bookwalker
    set_num_as_float_or_int, # Added based on usage in search_bookwalker
    is_one_shot, # Added based on usage in search_bookwalker
)
from messaging.discord_messenger import (
    send_discord_message,
    handle_fields,
    group_notification,
    Embed,
) # Assuming discord functions are moved here
from messaging.log_manager import write_to_file # Assuming log functions are moved here

# Global state - TODO: Refactor to avoid global state
grouped_notifications = []

class BookwalkerBook:
    def __init__(
        self,
        title,
        original_title,
        volume_number,
        part,
        date,
        is_released,
        price,
        url,
        thumbnail,
        book_type,
        description,
        preview_image_url,
    ):
        self.title = title
        self.original_title = original_title
        self.volume_number = volume_number
        self.part = part
        self.date = date
        self.is_released = is_released
        self.price = price
        self.url = url
        self.thumbnail = thumbnail
        self.book_type = book_type
        self.description = description
        self.preview_image_url = preview_image_url

class BookwalkerSeries:
    def __init__(self, title, books, book_count, book_type):
        self.title = title
        self.books = books
        self.book_count = book_count
        self.book_type = book_type

# our session objects, one for each domain
session_objects = {}

# Returns a session object for the given URL
def get_session_object(url):
    domain = urllib.parse.urlparse(url).netloc.split(":")[0]
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

# Makes a GET request to the given URL using a reusable session object,
# and returns a BeautifulSoup object representing the parsed HTML response.
def scrape_url(url, strainer=None, headers=None, cookies=None, proxy=None):
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
        # TODO: Replace send_message with proper logging
        print(f"Error scraping URL: {e}")
        return None

# Groups all books with a matching title and book_type.
def get_all_matching_books(books, book_type, title):
    matching_books = []
    short_title = get_shortened_title(title)

    for book in books:
        short_title_two = get_shortened_title(book.title)
        if book.book_type == book_type and (
            book.title == title
            or (
                (
                    similar(clean_str(book.title), clean_str(title))
                    >= required_similarity_score
                )
                or (
                    (short_title and short_title_two)
                    and similar(
                        clean_str(short_title_two),
                        clean_str(short_title),
                    )
                    >= required_similarity_score
                )
            )
        ):
            matching_books.append(book)

    # remove them from books
    for book in matching_books:
        if book in books: # Check if book still exists before removing
             books.remove(book)

    return matching_books

# combine series in series_list that have the same title and book_type
def combine_series(series_list):
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
                >= required_similarity_score
            ):
                combined_series_item.books.extend(series.books)
                combined_series_item.book_count = len(combined_series_item.books)
                combined = True
                break

        # If series cannot be combined, add it to combined_series
        if not combined:
            combined_series.append(series)

    return combined_series

# Searches bookwalker with the user inputted query and returns the results.
def search_bookwalker(
    query,
    type,
    print_info=False,
    alternative_search=False,
    shortened_search=False,
    total_pages_to_scrape=5,
):
    global required_similarity_score # TODO: Pass as argument or get from config

    # The books returned from the search
    books = []
    # The searches that results in no book results
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
    original_similarity_score = required_similarity_score

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
        required_similarity_score = original_similarity_score - 0.03

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
                time.sleep(sleep_timer_bk / 2)
            if alternate_result:
                return alternate_result
            if not alternative_search:
                print("\t\t\t! NO BOOKS FOUND ON BOOKWALKER !")
                write_to_file(
                    "bookwalker_no_results.txt",
                    query,
                    without_timestamp=True,
                    check_for_dup=True,
                )
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

                part = get_file_part(title) # Assuming get_file_part is available

                if part and re.search(r"(\b(Part)([-_. ]+|)\d+(\.\d+)?)", title):
                    title = re.sub(r"(\b(Part)([-_. ]+|)\d+(\.\d+)?)", "", title)
                    title = remove_dual_space(title).strip()

                volume_number = ""

                # TODO: Import volume_keywords or pass it
                # modified_volume_keywords = [
                #     keyword
                #     for keyword in volume_keywords
                #     if len(keyword) > 1 and keyword not in ["Books?", "Novels?"]
                # ]
                # modified_volume_regex_keywords = (
                #     "(?<![A-Za-z])" + "|(?<![A-Za-z])".join(modified_volume_keywords)
                # )
                modified_volume_regex_keywords = "volume|vol|book|tome|tomo" # Placeholder

                contains_no_numbers = re.search(r"^[^\d]*$", title)
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
                         title_split = [re.sub(r"[^0-9.]", "", x) for x in title_split]
                         title_split = [
                             set_num_as_float_or_int(x.strip()) for x in title_split
                         ]
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
                    # TODO: Import get_release_number_cache or implement similar logic
                    # volume_number = get_release_number_cache(title)
                    volume_number = re.search(r'\d+', title) # Placeholder
                    if volume_number:
                        volume_number = volume_number.group()
                    else:
                        volume_number = ""


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
                    f"\t\t\t\t\t\tScore: {score} | Match: {score >= required_similarity_score} (>= {required_similarity_score})"
                )
                print(f"\t\t\t\t\t\tVolume Number: {volume_number}")
                if part:
                    print(f"\t\t\t\t\t\tVolume Part: {part}")

                score_two = 0
                if series_list_li == 1 and not score >= required_similarity_score:
                    score_two = similar(clean_shortened_title, clean_query)
                    print(
                        f"\n\t\t\t\t\t\tBookwalker: {clean_shortened_title if shortened_title and clean_shortened_title else clean_title}"
                    )
                    print(
                        f"\t\t\t\t\t\tLibrary:    {clean_query if shortened_title and clean_shortened_title else clean_shortened_query}"
                    )
                    print(
                        f"\t\t\t\t\t\tScore: {score_two} | Match: {score_two >= required_similarity_score} (>= {required_similarity_score})"
                    )
                    print(f"\t\t\t\t\t\tVolume Number: {volume_number}")
                    if part:
                        print(f"\t\t\t\t\t\tVolume Part: {part}")

                if not (score >= required_similarity_score) and not (
                    score_two >= required_similarity_score
                ):
                    message = f'"{clean_title}": {score} [{book_type}]'
                    if message not in similarity_match_failures:
                        similarity_match_failures.append(message)
                    required_similarity_score = original_similarity_score
                    continue

                # html from url
                page_two = scrape_url(url)

                # parse html
                soup_two = page_two.find("div", class_="product-detail-inner")

                if not soup_two:
                    print("No soup_two")
                    continue

                # Find the book's preview image
                meta_property_og_image = page_two.find("meta", {"property": "og:image"})
                if meta_property_og_image and meta_property_og_image[
                    "content"
                ].startswith("http"):
                    preview_image_url = meta_property_og_image["content"]

                if not preview_image_url or "ogp-mature" in preview_image_url:
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

                    th = detail.find_previous_sibling("th")
                    th_text = th.text

                    if th_text in ["Series Title", "Alternative Title"]:
                        series_title = detail.text
                        series_title = clean_str(series_title).lower().strip()

                        if not similar(
                            series_title, clean_query
                        ) >= required_similarity_score and not similar(
                            series_title, clean_shortened_query
                        ):
                            continue

                    if date_match:
                        date_match = re.sub(r"[^\s\w]", "", date_match.group())
                        date_parts = date_match.split()
                        month = date_parts[0][:3]
                        day = date_parts[1]
                        year = date_parts[2]
                        date = datetime.strptime(f"{month} {day} {year}", "%b %d %Y")
                        is_released = date < datetime.now()
                        date = date.strftime("%Y-%m-%d")
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
                # TODO: Replace send_message with proper logging
                print(f"Error processing book item: {e}")
                errors.append(url)
                continue

        for book in books[:]: # Iterate over a copy
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
    required_similarity_score = original_similarity_score

    time.sleep(sleep_timer_bk)

    if len(series_list) == 1 and len(series_list[0].books) > 0:
        return series_list[0].books
    elif len(series_list) > 1:
        print("\t\t\tNumber of series from bookwalker search is greater than one.")
        print(f"\t\t\tNum: {len(series_list)}")
        return []
    else:
        if not alternative_search:
            print("\t\t\tNo matching books found.")
            write_to_file(
                "bookwalker_no_matching_books.txt",
                query,
                without_timestamp=True,
                check_for_dup=True,
            )
        return []

# Checks the library against bookwalker for any missing volumes that are released or on pre-order
def check_for_new_volumes_on_bookwalker(library_volumes, series_name, volume_type):
    global discord_embed_limit, grouped_notifications # TODO: Refactor global state

    # Prints info about the item
    def print_item_info(item):
        print(f"\t\t{item.title}")
        print(f"\t\tType: {item.book_type}")
        print(f"\t\tVolume {item.volume_number}")
        print(f"\t\tDate: {item.date}")
        print(f"\t\tURL: {item.url}\n")

    # Writes info about the item to a file
    def log_item_info(item, file_name):
        message = f"{item.date} | {item.title} | Volume {item.volume_number} | {item.book_type} | {item.url}"
        write_to_file(
            f"{file_name.lower().replace('-', '_')}.txt",
            message,
            without_timestamp=True,
            overwrite=False,
        )

    # Creates a Discord embed for the item
    def create_embed(item, color, webhook_index):
        global grouped_notifications

        embed = handle_fields(
            DiscordEmbed(
                title=f"{item.title} Volume {item.volume_number}",
                color=color,
            ),
            fields=[
                {
                    "name": "Type",
                    "value": item.book_type,
                    "inline": False,
                },
                {
                    "name": "Release Date",
                    "value": item.date,
                    "inline": False,
                },
            ],
        )

        if item.description:
            embed.fields.append(
                {
                    "name": "Description",
                    "value": unidecode(item.description),
                    "inline": False,
                }
            )

        embed.url = item.url

        if item.preview_image_url:
            embed.set_image(url=item.preview_image_url)
            embed.set_thumbnail(url=item.preview_image_url)

        if bookwalker_logo_url and item.url:
            embed.set_author(
                name="Bookwalker", url=item.url, icon_url=bookwalker_logo_url
            )

        if bookwalker_webhook_urls and len(bookwalker_webhook_urls) == 2:
            grouped_notifications = group_notification(
                grouped_notifications,
                Embed(embed, None),
                passed_webhook=bookwalker_webhook_urls[webhook_index],
            )

    # Processes the items
    def process_items(items, file_name, color, webhook_index):
        if not items:
            return

        print(f"\n{file_name.capitalize()}:")
        for item in items:
            print_item_info(item)
            log_item_info(item, file_name)
            create_embed(item, color, webhook_index)

        if grouped_notifications:
            if bookwalker_webhook_urls and len(bookwalker_webhook_urls) == 2:
                send_discord_message(
                    None,
                    grouped_notifications,
                    passed_webhook=bookwalker_webhook_urls[webhook_index],
                )
                grouped_notifications.clear() # Clear after sending for this webhook

    def filter_and_compare_volumes(
        volumes, bookwalker_volumes, volume_type, consider_parts=False
    ):
        if volume_type == "l" and volumes and bookwalker_volumes:
            for vol in bookwalker_volumes[:]:
                for existing_vol in volumes:
                    if (
                        (
                            vol.volume_number == existing_vol.volume_number
                            or (
                                isinstance(existing_vol.volume_number, list)
                                and vol.volume_number in existing_vol.volume_number
                            )
                            or (
                                isinstance(vol.volume_number, list)
                                and existing_vol.volume_number in vol.volume_number
                            )
                        )
                        and (
                            (vol.part and not existing_vol.volume_part)
                            or (
                                not consider_parts
                                and not vol.part
                                and not existing_vol.volume_part
                            )
                        )
                        and vol in bookwalker_volumes
                    ):
                        bookwalker_volumes.remove(vol)
        return bookwalker_volumes

    def update_bookwalker_volumes(volumes, bookwalker_volumes):
        if volumes and bookwalker_volumes:
            bookwalker_volumes = [
                vol
                for vol in bookwalker_volumes
                if not any(
                    (
                        vol.volume_number == existing_vol.volume_number
                        or (
                            isinstance(existing_vol.volume_number, list)
                            and vol.volume_number in existing_vol.volume_number
                        )
                        or (
                            isinstance(vol.volume_number, list)
                            and existing_vol.volume_number in vol.volume_number
                        )
                    )
                    and existing_vol.volume_part == vol.part
                    for existing_vol in volumes
                )
            ]
        return bookwalker_volumes

    # Writes info about the missing volumes to a log file.
    def log_missing_volumes(series, volumes, bookwalker_volumes):
        write_to_file(
            "bookwalker_missing_volumes.txt",
            f"{series} - Existing Volumes: {len(volumes)}, Bookwalker Volumes: {len(bookwalker_volumes)}\n",
            without_timestamp=True,
            check_for_dup=True,
        )

    # Prints info about the new/upcoming releases
    def print_releases(bookwalker_volumes, released, pre_orders):
        # Sort them by volume_number
        bookwalker_volumes = sorted(
            bookwalker_volumes, key=lambda x: get_sort_key(x.volume_number) # Assuming get_sort_key is available
        )

        for vol in bookwalker_volumes:
            if vol.is_released:
                print("\n\t\t\t[RELEASED]")
                released.append(vol)
            else:
                print("\n\t\t\t[PRE-ORDER]")
                pre_orders.append(vol)

            print(f"\t\t\tTitle: {vol.original_title}")
            print(f"\t\t\tVolume Number: {vol.volume_number}")
            if vol.part:
                print(f"\t\t\tPart: {vol.part}")
            print(f"\t\t\tDate: {vol.date}")
            if vol == bookwalker_volumes[-1]:
                print(f"\t\t\tURL: {vol.url} \n")
            else:
                print(f"\t\t\tURL: {vol.url}")

    # Sorts and logs the releases and pre-orders
    def sort_and_log_releases(released, pre_orders):
        pre_orders.sort(
            key=lambda x: datetime.strptime(x.date, "%Y-%m-%d"), reverse=True
        )
        released.sort(
            key=lambda x: datetime.strptime(x.date, "%Y-%m-%d"), reverse=False
        )

        if log_to_file:
            # TODO: Define LOGS_DIR or pass it
            # released_path = os.path.join(LOGS_DIR, "released.txt")
            # pre_orders_path = os.path.join(LOGS_DIR, "pre-orders.txt")
            # if os.path.isfile(released_path):
            #     remove_file(released_path, silent=True) # Assuming remove_file is available
            # if os.path.isfile(pre_orders_path):
            #     remove_file(pre_orders_path, silent=True)
            pass # Placeholder

        process_items(released, "Released", grey_color, 0)
        process_items(pre_orders, "Pre-orders", preorder_blue_color, 1)

    original_limit = discord_embed_limit
    discord_embed_limit = 1 # TODO: Why limit to 1?

    pre_orders = []
    released = []

    print("\nChecking for new volumes on bookwalker...")

    bookwalker_volumes = search_bookwalker(series_name, volume_type, False)
    shortened_series_title = get_shortened_title(series_name)

    if shortened_series_title:
        shortened_bookwalker_volumes = search_bookwalker(
            shortened_series_title,
            volume_type,
            False,
            shortened_search=True,
        )
        if shortened_bookwalker_volumes:
            bookwalker_volumes.extend(
                vol
                for vol in shortened_bookwalker_volumes
                if not any(
                    vol.url == compare_vol.url
                    for compare_vol in bookwalker_volumes
                )
            )

    if not bookwalker_volumes:
        print(f"\tNo Bookwalker volumes found for {series_name}")
        return

    bookwalker_volumes = filter_and_compare_volumes(
        library_volumes, bookwalker_volumes, volume_type, consider_parts=True
    )

    print(f"\t\tExisting Volumes: {len(library_volumes)}")
    print(f"\t\tBookwalker Volumes: {len(bookwalker_volumes)}\n")

    bookwalker_volumes = filter_and_compare_volumes(
        library_volumes, bookwalker_volumes, volume_type
    )

    if not bookwalker_volumes:
        print(f"\tNo new Bookwalker volumes found for {series_name}")
        return

    if len(library_volumes) > len(bookwalker_volumes):
        log_missing_volumes(series_name, library_volumes, bookwalker_volumes)

    bookwalker_volumes = update_bookwalker_volumes(library_volumes, bookwalker_volumes)

    if not bookwalker_volumes:
        print(f"\tNo new Bookwalker volumes after update for {series_name}")
        return

    print("\t\tNew/Upcoming Releases on Bookwalker:")
    print_releases(bookwalker_volumes, released, pre_orders)

    sort_and_log_releases(released, pre_orders)
    discord_embed_limit = original_limit

# Placeholder for get_sort_key if not defined elsewhere
def get_sort_key(index_number):
    if isinstance(index_number, list):
        # Attempt to get the minimum numeric value, handle non-numeric gracefully
        numeric_items = [item for item in index_number if isinstance(item, (int, float))]
        return min(numeric_items) if numeric_items else float('inf') # Or some other default
    elif isinstance(index_number, (int, float)):
        return index_number
    else:
        return float('inf') # Default for non-numeric or unexpected types