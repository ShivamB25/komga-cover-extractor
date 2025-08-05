# -*- coding: utf-8 -*-
"""
Bookwalker client for scraping and checking new releases.
"""
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config.constants import (
    required_similarity_score,
)
from utils.helpers import (
    clean_str,
    send_message,
)
from utils.similarity import similar


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
    domain = urlparse(url).netloc.split(":")
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
        send_message(f"Error scraping URL: {e}", error=True)
        return None


# combine series in series_list that have the same title and book_type
def combine_series(series_list):
    combined_series = []

    for series in series_list:
        # Sort books by volume number
        series.books.sort(
            key=lambda x: (str(x.volume_number), str(x.part).strip().split(","))
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