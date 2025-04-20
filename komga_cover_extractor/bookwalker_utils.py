# komga_cover_extractor/bookwalker_utils.py
import os
import re
import time
import urllib.parse
from datetime import datetime
from functools import lru_cache

# Import necessary config variables
# Use try-except for robustness during refactoring
try:
    from .config import (
        required_similarity_score,
        sleep_timer_bk,
        bookwalker_webhook_urls,
        bookwalker_logo_url,
        log_to_file,
        discord_embed_limit,
        grey_color,
        preorder_blue_color,
        paths,
        download_folders,
        paths_with_types,
        manga_extensions,
        novel_extensions,
    )
except ImportError:
    print(
        "WARN: Could not import from .config, using placeholder values in bookwalker_utils."
    )
    required_similarity_score = 0.9
    sleep_timer_bk = 2
    bookwalker_webhook_urls = []
    bookwalker_logo_url = ""
    log_to_file = False
    discord_embed_limit = 10
    grey_color = 8421504
    preorder_blue_color = 5919485
    paths = []
    download_folders = []
    paths_with_types = []
    manga_extensions = [".cbz", ".zip"]
    novel_extensions = [".epub"]


# Import necessary functions from other utils
try:
    from .log_utils import send_message, write_to_file
except ImportError:
    print("WARN: Could not import from .log_utils, defining placeholder send_message.")

    def send_message(msg, error=False, discord=False):
        print(f"{'ERROR: ' if error else ''}{msg}")

    def write_to_file(*args, **kwargs):
        pass  # No-op


try:
    from .misc_utils import scrape_url
except ImportError:
    print("WARN: Could not import from .misc_utils, defining placeholder scrape_url.")

    def scrape_url(*args, **kwargs):
        return None


try:
    from .string_utils import (
        get_shortened_title,
        clean_str,
        similar,
        get_release_number_cache,
        get_file_part,
        is_one_shot,
        remove_brackets,
        contains_brackets,
        get_sort_key,
        normalize_str,
        unidecode,  # Added normalize_str, unidecode
    )
except ImportError:
    print("WARN: Could not import from .string_utils, defining placeholders.")

    def get_shortened_title(t):
        return t

    def clean_str(s, **kwargs):
        return s.lower().strip()

    def similar(a, b):
        return 0.0

    def get_release_number_cache(f, **kwargs):
        return ""

    def get_file_part(f, **kwargs):
        return ""

    def is_one_shot(f, **kwargs):
        return False

    def remove_brackets(s):
        return s

    def contains_brackets(s):
        return False

    def get_sort_key(n):
        return n

    def normalize_str(s, **kwargs):
        return s

    def unidecode(s):
        return s


try:
    from .file_utils import (
        get_all_folders_recursively_in_dir,
        clean_and_sort,
        upgrade_to_file_class,
        get_folder_type,
    )  # Added get_folder_type
except ImportError:
    print("WARN: Could not import from .file_utils, defining placeholders.")

    def get_all_folders_recursively_in_dir(p):
        return []

    def clean_and_sort(r, files=[], dirs=[], **kwargs):
        return files, dirs

    def upgrade_to_file_class(f, r, **kwargs):
        return []  # Needs File model

    def get_folder_type(files, **kwargs):
        return 0


try:
    from .models import (
        BookwalkerBook,
        BookwalkerSeries,
        Volume,
        File,
        Publisher,
        Embed,
    )  # Import needed models
except ImportError:
    print("WARN: Could not import from .models, defining placeholder classes.")

    class BookwalkerBook:
        pass

    class BookwalkerSeries:
        pass

    class Volume:
        pass

    class File:
        pass

    class Publisher:
        pass

    class Embed:
        pass


try:
    from .discord_utils import (
        handle_fields,
        group_notification,
        send_discord_message,
        DiscordEmbed,
    )
except ImportError:
    print("WARN: Could not import from .discord_utils, defining placeholders.")

    def handle_fields(e, f):
        return e

    def group_notification(n, e, **kwargs):
        n.append(e)
        return n

    def send_discord_message(*args, **kwargs):
        pass

    class DiscordEmbed:
        pass


# --- Helper Functions (Specific to Bookwalker logic) ---


# TODO: Implement full logic for get_all_matching_books based on original script
def get_all_matching_books(books, book_type, title):
    """Placeholder: Finds books matching a given title and type."""
    # Simplified logic: exact match for now
    matching = [b for b in books if b.book_type == book_type and b.title == title]
    for m in matching:
        if m in books:
            books.remove(m)  # Ensure removal from original list
    return matching


# TODO: Implement full logic for combine_series based on original script
def combine_series(series_list):
    """Placeholder: Combines series with similar titles/types."""
    # Simple pass-through for now
    return series_list


# TODO: Implement full logic for print_releases based on original script
def print_releases(bookwalker_volumes, released_list, pre_orders_list):
    """Placeholder: Separates volumes into released/pre-order lists."""
    for vol in bookwalker_volumes:
        if vol.is_released:
            released_list.append(vol)
        else:
            pre_orders_list.append(vol)


# TODO: Implement full logic for sort_and_log_releases based on original script
def sort_and_log_releases(released_list, pre_orders_list):
    """Placeholder: Sorts and logs/processes final lists."""
    # Sorting logic can be added here if needed before final processing
    pass


# --- Main Bookwalker Functions ---


# Searches bookwalker with the user inputted query and returns the results.
def search_bookwalker(
    query,
    book_type_code,  # Expect 'm' for manga, 'l' for light novel
    print_info=False,  # Keep for potential debugging?
    alternative_search=False,  # Flag for trying different category
    shortened_search=False,  # Flag for searching shortened title
    total_pages_to_scrape=5,
):
    """Searches Bookwalker Global for a given query and book type."""
    # Use global config value, but allow temporary modification
    current_required_similarity_score = required_similarity_score

    books = []
    no_book_result_searches = []
    series_list = []
    no_volume_number = []
    chapter_releases = []
    similarity_match_failures = []
    errors = []

    # Define Bookwalker category codes
    categories = {
        "m": "&qcat=2",  # Manga
        "l": "&qcat=3",  # Light Novel
        "m_alt": "&qcat=11",  # Intl Manga (alternative)
    }
    category_code = categories.get(
        book_type_code if not alternative_search else "m_alt", categories["m"]
    )  # Default to manga

    search_query_encoded = urllib.parse.quote(query)
    base_url = "https://global.bookwalker.jp/search/?word="
    series_only_url_part = "&np=0"  # Parameter to search for series titles
    series_check_url = f"{base_url}{search_query_encoded}{series_only_url_part}"

    # Cookies to potentially bypass safe search
    default_cookies = {"glSafeSearch": "1", "safeSearch": "111"}
    default_headers = {"User-Agent": "Mozilla/5.0"}  # Simplified UA

    if not alternative_search:
        keyword = "\t\tSearch: " if not shortened_search else "\n\t\tShortened Search: "
        category_name = "MANGA" if book_type_code == "m" else "NOVEL"
        series_info = f"({series_check_url}{category_code})" if shortened_search else ""
        print(f"{keyword}{query}\n\t\tCategory: {category_name} {series_info}")

    # --- Check number of series results first (only if not alternative search) ---
    series_list_count = 0
    if not alternative_search:
        try:
            series_page_soup = scrape_url(
                f"{series_check_url}{category_code}",
                cookies=default_cookies,
                headers=default_headers,
            )
            if series_page_soup:
                series_list_ul = series_page_soup.find("ul", class_="o-tile-list")
                if series_list_ul:
                    series_list_count = len(
                        series_list_ul.find_all("li", class_="o-tile")
                    )
                    print(
                        f"\t\tFound {series_list_count} series results for '{query}'."
                    )
        except Exception as e:
            send_message(f"Error checking series count for '{query}': {e}", error=True)

    # Adjust similarity score if only one series result is found
    if series_list_count == 1:
        print(
            "\t\tOnly one series found, slightly lowering similarity requirement for volume matching."
        )
        current_required_similarity_score = required_similarity_score - 0.03
    elif shortened_search and series_list_count != 1:
        print(
            "\t\t\tShortened search requires exactly one series result, skipping...\n"
        )
        return []  # Skip shortened search if series count isn't 1

    # --- Scrape search result pages ---
    current_page = 1
    max_pages_to_scrape = total_pages_to_scrape  # Initialize max pages

    while current_page <= max_pages_to_scrape:
        page_url_part = f"&page={current_page}"
        search_url = f"{base_url}{search_query_encoded}{page_url_part}{category_code}"

        print(
            f"\t\t\tScraping Page: {current_page} / {max_pages_to_scrape} ({search_url})"
        )
        page_soup = scrape_url(
            search_url, cookies=default_cookies, headers=default_headers
        )

        if not page_soup:
            if book_type_code == "m" and not alternative_search:
                print("\t\t\tPrimary manga category failed, trying alternative...")
                alt_results = search_bookwalker(
                    query,
                    book_type_code,
                    print_info,
                    alternative_search=True,
                    shortened_search=shortened_search,
                    total_pages_to_scrape=total_pages_to_scrape,
                )
                return alt_results  # Return results from alternative search
            else:
                errors.append(f"Empty page for {search_url}")
                break  # Stop if page is empty and no alternative

        # Update max pages based on pagination info, only on the first page scrape
        if current_page == 1:
            pager_area = page_soup.find("div", class_="pager-area")
            if pager_area:
                page_numbers = [
                    int(li.text)
                    for li in pager_area.select("ul.clearfix > li")
                    if li.text.isdigit()
                ]
                if page_numbers:
                    highest_page = max(page_numbers)
                    max_pages_to_scrape = min(highest_page, total_pages_to_scrape)
                    print(f"\t\t\tAdjusted max pages to scrape: {max_pages_to_scrape}")
                else:
                    max_pages_to_scrape = 1
            else:
                max_pages_to_scrape = 1

        list_area_ul = page_soup.find("ul", class_="o-tile-list")
        if not list_area_ul:
            if current_page == 1 and not alternative_search:
                print("\t\t\t! NO BOOKS FOUND ON THIS PAGE !")
                no_book_result_searches.append(query)
            break

        book_tiles = list_area_ul.find_all("li", class_="o-tile")
        print(f"\t\t\t\tItems on page: {len(book_tiles)}")

        for item_index, item in enumerate(book_tiles):
            try:
                title_tag = item.find("h2", class_="a-tile-ttl")
                link_tag = item.find("a", class_="a-tile-thumb-img")
                img_tag = link_tag.find("img") if link_tag else None
                tag_box = item.find("ul", class_="m-tile-tag-box")

                if not title_tag or not link_tag or not img_tag or not tag_box:
                    continue

                original_title = title_tag.text.strip()
                book_url = link_tag.get("href")
                img_srcset = img_tag.get("data-srcset", img_tag.get("src", ""))
                thumbnail = img_srcset.split(",")[-1].strip().split(" ")[0]

                tags = tag_box.find_all("li", class_="m-tile-tag")
                is_chapter = any(
                    t.find("div", class_=["a-tag-chapter", "a-tag-simulpub"])
                    for t in tags
                )
                book_type_tag = next(
                    (
                        t.text.strip()
                        for t in tags
                        if t.find(
                            "div",
                            class_=["a-tag-manga", "a-tag-light-novel", "a-tag-other"],
                        )
                    ),
                    "Unknown",
                )

                if is_chapter:
                    chapter_releases.append(original_title)
                    continue

                cleaned_title_for_match = clean_str(
                    original_title, skip_bracket=True, skip_punctuation=True
                )
                cleaned_query_for_match = clean_str(
                    query, skip_bracket=True, skip_punctuation=True
                )

                volume_number = get_release_number_cache(original_title)
                part = get_file_part(original_title)
                series_name_from_title = get_series_name_from_volume(
                    original_title, root=None, test_mode=True
                )

                score = similar(cleaned_title_for_match, cleaned_query_for_match)
                short_title = get_shortened_title(series_name_from_title)
                short_query = get_shortened_title(query)
                score_short = (
                    similar(clean_str(short_title), clean_str(short_query))
                    if short_title and short_query
                    else 0.0
                )

                if (
                    score < current_required_similarity_score
                    and score_short < current_required_similarity_score
                ):
                    if (
                        f'"{cleaned_title_for_match}": {score:.2f} [{book_type_tag}]'
                        not in similarity_match_failures
                    ):
                        similarity_match_failures.append(
                            f'"{cleaned_title_for_match}": {score:.2f} [{book_type_tag}]'
                        )
                    continue

                if volume_number == "":
                    if is_one_shot(original_title, skip_folder_check=True):
                        volume_number = 1
                    else:
                        if original_title not in no_volume_number:
                            no_volume_number.append(original_title)
                        continue

                # --- Scrape individual book page ---
                book_page_soup = scrape_url(
                    book_url, cookies=default_cookies, headers=default_headers
                )
                release_date_str = ""
                is_released = None
                description = ""
                preview_image_url = thumbnail

                if book_page_soup:
                    og_image = book_page_soup.find("meta", property="og:image")
                    if og_image and og_image.get("content", "").startswith("http"):
                        preview_image_url = og_image["content"]
                    elif not preview_image_url or not preview_image_url.startswith(
                        "http"
                    ):
                        preview_image_url = None  # Invalidate if thumbnail wasn't a URL

                    desc_div = book_page_soup.find("div", itemprop="description")
                    if desc_div:
                        description = desc_div.get_text("\n", strip=True)

                    date_td = next(
                        (
                            td
                            for td in book_page_soup.select("table.product-detail td")
                            if re.search(
                                r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
                                td.text,
                            )
                        ),
                        None,
                    )
                    if date_td:
                        date_match = re.search(
                            r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
                            date_td.text,
                            re.IGNORECASE,
                        )
                        if date_match:
                            try:
                                date_obj = datetime.strptime(
                                    date_match.group(), "%b %d %Y"
                                )
                                release_date_str = date_obj.strftime("%Y-%m-%d")
                                is_released = date_obj < datetime.now()
                            except ValueError:
                                pass

                book = BookwalkerBook(
                    series_name_from_title,
                    original_title,
                    volume_number,
                    part,
                    release_date_str,
                    is_released,
                    0.00,
                    book_url,
                    thumbnail,
                    book_type_tag,
                    description,
                    preview_image_url,
                )
                books.append(book)
                time.sleep(sleep_timer_bk / 4)  # Small delay

            except Exception as e:
                send_message(
                    f"Error processing item {item_index} on page {current_page} ({book_url if 'book_url' in locals() else 'unknown URL'}): {e}",
                    error=True,
                )
                errors.append(f"Item {item_index}, Page {current_page}: {e}")
                continue

        if not book_tiles:
            break  # Stop if no more books on page
        current_page += 1
        time.sleep(sleep_timer_bk / 2)  # Delay between pages

    # --- Process collected books ---
    if not books and not alternative_search and book_type_code == "m":
        print("\t\tNo manga books found, trying alternative category...")
        alt_results = search_bookwalker(
            query,
            book_type_code,
            print_info,
            alternative_search=True,
            shortened_search=shortened_search,
            total_pages_to_scrape=total_pages_to_scrape,
        )
        return alt_results

    unique_books = {book.url: book for book in books}.values()
    temp_books = list(unique_books)
    while temp_books:
        book = temp_books.pop(0)
        matching = get_all_matching_books(
            temp_books, book.book_type, book.title
        )  # Use local helper
        current_series_books = [book] + matching
        series_list.append(
            BookwalkerSeries(
                book.title,
                current_series_books,
                len(current_series_books),
                book.book_type,
            )
        )  # Use imported model

    series_list = combine_series(series_list)  # Use local helper
    # Restore original similarity score if it was modified
    # required_similarity_score = original_similarity_score # This global modification is tricky, avoid if possible

    # --- Return results ---
    if len(series_list) == 1:
        series_list[0].books.sort(
            key=lambda x: (get_sort_key(x.volume_number), str(x.part))
        )
        return series_list[0].books
    elif len(series_list) > 1:
        print(
            f"\t\t\tWarning: Found multiple series matching '{query}'. Returning empty list."
        )
        return []
    else:
        if not alternative_search and query not in no_book_result_searches:
            print(f"\t\t\tNo matching series found for '{query}' after processing.")
        return []


# Checks the library against bookwalker for any missing volumes
def check_for_new_volumes_on_bookwalker():
    """Scans library paths and compares against Bookwalker for new releases."""
    global discord_embed_limit  # Use imported config value

    # Helper: Prints info about a BookwalkerBook
    def print_item_info(item):
        print(f"\t\t{item.original_title}")
        print(f"\t\tType: {item.book_type}")
        print(
            f"\t\tVolume {item.volume_number}"
            + (f" Part {item.part}" if item.part else "")
        )
        print(f"\t\tDate: {item.date}")
        print(f"\t\tURL: {item.url}\n")

    # Helper: Logs item info to a file
    def log_item_info(item, file_name):
        message = (
            f"{item.date} | {item.original_title} | Volume {item.volume_number}"
            + (f" Part {item.part}" if item.part else "")
            + f" | {item.book_type} | {item.url}"
        )
        write_to_file(
            f"{file_name.lower().replace(' ', '_')}.txt",
            message,
            without_timestamp=True,
            check_for_dup=True,
        )

    # Helper: Creates a Discord embed for a BookwalkerBook
    def create_embed(item, color):
        embed = DiscordEmbed(
            title=f"{item.original_title} - Vol {item.volume_number}"
            + (f" Pt {item.part}" if item.part else ""),
            color=color,
            url=item.url,
        )
        fields = [
            {"name": "Type", "value": item.book_type, "inline": False},
            {
                "name": "Release Date",
                "value": item.date if item.date else "Unknown",
                "inline": False,
            },
        ]
        if item.description:
            desc = unidecode(item.description)
            max_len = 500
            fields.append(
                {
                    "name": "Description",
                    "value": (desc[:max_len] + "...") if len(desc) > max_len else desc,
                    "inline": False,
                }
            )

        embed = handle_fields(embed, fields)

        if item.preview_image_url:
            embed.set_image(url=item.preview_image_url)
        if bookwalker_logo_url:
            embed.set_author(
                name="Bookwalker",
                url="https://global.bookwalker.jp/",
                icon_url=bookwalker_logo_url,
            )

        return Embed(embed)

    # Helper: Processes lists of released/pre-order items
    def process_items(items, list_name, color, webhook_index):
        if not items:
            return
        print(f"\n--- {list_name} ({len(items)}) ---")
        current_notifications = []
        webhook_url = (
            bookwalker_webhook_urls[webhook_index]
            if bookwalker_webhook_urls and len(bookwalker_webhook_urls) > webhook_index
            else None
        )
        for item in items:
            print_item_info(item)
            if log_to_file:
                log_item_info(item, list_name)
            if webhook_url:
                embed_wrapper = create_embed(item, color)
                current_notifications = group_notification(
                    current_notifications, embed_wrapper, passed_webhook=webhook_url
                )

        if current_notifications and webhook_url:
            send_discord_message(
                None, embeds=current_notifications, passed_webhook=webhook_url
            )

    # Helper: Determines volume type ('m' or 'l') based on file extensions
    def determine_volume_type(volume_list):
        # Use get_folder_type from file_utils for consistency
        manga_percent = get_folder_type(
            [vol.name for vol in volume_list], extensions=manga_extensions
        )  # Use imported file_utils function
        novel_percent = get_folder_type(
            [vol.name for vol in volume_list], extensions=novel_extensions
        )  # Use imported file_utils function

        if manga_percent >= 70:
            return "m"  # Threshold from original logic
        if novel_percent >= 70:
            return "l"
        return None  # Mixed or unknown
        # manga_count = sum(1 for vol in volume_list if vol.extension in manga_extensions)
        # novel_count = sum(1 for vol in volume_list if vol.extension in novel_extensions)
        total = len(volume_list)
        if total == 0:
            return None
        if manga_count / total >= 0.7:
            return "m"
        if novel_count / total >= 0.7:
            return "l"
        return None  # Mixed or unknown

    # Helper: Filters bookwalker volumes based on existing library volumes
    def filter_bookwalker_volumes(library_volumes, bookwalker_volumes):
        if not library_volumes or not bookwalker_volumes:
            return bookwalker_volumes

        # Create a set of existing (number, part) tuples for faster lookup
        existing_set = set()
        for vol in library_volumes:
            num = (
                vol.index_number
            )  # Assumes index_number combines vol and part correctly
            if num != "":
                try:
                    existing_set.add(float(num))
                except (ValueError, TypeError):
                    pass  # Ignore if index_number isn't numeric

        # Filter bookwalker list
        filtered_bw_volumes = []
        for bw_vol in bookwalker_volumes:
            try:
                bw_num = float(bw_vol.volume_number) + (
                    float(bw_vol.part) / 10 if bw_vol.part else 0.0
                )
                if bw_num not in existing_set:
                    filtered_bw_volumes.append(bw_vol)
            except (ValueError, TypeError):
                continue  # Skip if bookwalker volume number/part is invalid

        return filtered_bw_volumes

    # --- Main Function Logic ---
    original_limit = discord_embed_limit
    discord_embed_limit = 1  # Send one embed per message

    all_released = []
    all_pre_orders = []

    print("\nChecking library against Bookwalker for new volumes...")
    paths_to_scan = [p.path for p in paths_with_types] if paths_with_types else paths
    paths_to_scan = [p for p in paths_to_scan if p not in download_folders]

    if not paths_to_scan:
        print("\tNo library paths configured to scan.")
        return

    for path_index, path in enumerate(paths_to_scan, start=1):
        if not os.path.exists(path):
            print(f"\n\tPath does not exist: {path}")
            continue

        print(f"\nScanning Path {path_index}/{len(paths_to_scan)}: {path}")
        try:
            series_dirs = [
                entry.name
                for entry in scandir.scandir(path)
                if entry.is_dir() and not entry.name.startswith(".")
            ]
        except Exception as e:
            send_message(f"Error scanning path {path}: {e}", error=True)
            continue

        if not series_dirs:
            print("\tNo series directories found in this path.")
            continue

        series_dirs.sort()

        for dir_index, series_dir in enumerate(series_dirs, start=1):
            root = os.path.join(path, series_dir)
            print(f"\n\t[Series {dir_index}/{len(series_dirs)}] Checking: {series_dir}")

            try:
                series_files = [
                    entry.name for entry in scandir.scandir(root) if entry.is_file()
                ]
                series_files, _ = clean_and_sort(
                    root, files=series_files, chapters=False, sort=True
                )

                if not series_files:
                    print("\t\tNo valid volume files found.")
                    continue

                volumes = upgrade_to_volume_class(
                    upgrade_to_file_class(series_files, root),
                    skip_release_year=True,
                    skip_file_part=False,
                    skip_release_group=True,
                    skip_extras=True,
                    skip_publisher=True,
                    skip_premium_content=True,
                    skip_subtitle=True,
                    skip_multi_volume=False,
                )

                if not volumes:
                    continue

                volume_type = determine_volume_type(volumes)
                if not volume_type:
                    print("\t\tCould not determine volume type (manga/novel).")
                    continue

                series_name_query = normalize_str(
                    series_dir, skip_type_keywords=True, skip_editions=True
                )
                series_name_query = unidecode(series_name_query)

                bookwalker_volumes = search_bookwalker(series_name_query, volume_type)

                if not bookwalker_volumes:
                    continue

                missing_volumes = filter_bookwalker_volumes(volumes, bookwalker_volumes)

                if missing_volumes:
                    print(
                        f"\t\tFound {len(missing_volumes)} potential new/missing volumes on Bookwalker."
                    )
                    print_releases(
                        missing_volumes, all_released, all_pre_orders
                    )  # Use local helper
                # else: # Reduce noise, only print if new found
                #     print("\t\tLibrary seems up-to-date with Bookwalker for this series.")

            except Exception as e:
                send_message(f"Error processing series '{series_dir}': {e}", error=True)
                continue

    # --- Final Summary ---
    print("\n--- Bookwalker Check Summary ---")
    # Sort and process final lists
    sort_and_log_releases(all_released, all_pre_orders)  # Use local helper

    discord_embed_limit = original_limit  # Restore original limit
