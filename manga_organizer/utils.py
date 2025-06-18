import hashlib
import os
import time
import threading
from datetime import datetime
from difflib import SequenceMatcher

from . import services, filesystem

# Stat-related variables
errors = []
items_changed = []


def send_message(
    message,
    discord=True,
    error=False,
    log=True,
    error_file_name="errors.txt",
    changes_file_name="changes.txt",
):
    """Sends a message, prints it, and writes it to a file."""
    print(message)
    if discord:
        services.send_discord_message(message)
    if error:
        errors.append(message)
        if log:
            filesystem.write_to_file(error_file_name, message)
    else:
        items_changed.append(message)
        if log:
            filesystem.write_to_file(changes_file_name, message)


def get_library_type(files, required_match_percentage=None):
    """Determines the files library type."""
    from .config import library_types
    from .filesystem import get_file_extension
    import re

    for library_type in library_types:
        match_count = 0
        for file in files:
            extension = get_file_extension(file)
            if (
                extension in library_type.extensions
                and all(
                    re.search(regex, file, re.IGNORECASE)
                    for regex in library_type.must_contain
                )
                and all(
                    not re.search(regex, file, re.IGNORECASE)
                    for regex in library_type.must_not_contain
                )
            ):
                match_count += 1

        match_percentage = required_match_percentage or library_type.match_percentage
        if len(files) > 0 and match_count / len(files) * 100 >= match_percentage:
            return library_type
    return None


def get_file_size(file_path):
    """Gets the file's file size."""
    if os.path.isfile(file_path):
        file_info = os.stat(file_path)
        return file_info.st_size
    else:
        return None


def similar(a, b):
    """Checks similarity between two strings."""
    a = a.lower().strip()
    b = b.lower().strip()
    if a == "" or b == "":
        return 0.0
    elif a == b:
        return 1.0
    else:
        return SequenceMatcher(None, a, b).ratio()


def set_modification_date(file_path, date):
    """Sets the modification date of the passed file path to the passed date."""
    from .filesystem import get_modification_date

    try:
        os.utime(file_path, (get_modification_date(file_path), date))
    except Exception as e:
        send_message(
            f"ERROR: Could not set modification date of {file_path}\nERROR: {e}",
            error=True,
        )


def get_file_hash(file, is_internal=False, internal_file_name=None):
    """Gets the hash of the passed file and returns it as a string."""
    import zipfile

    try:
        BUF_SIZE = 65536  # 64KB buffer size (adjust as needed)
        hash_obj = hashlib.sha256()

        if is_internal:
            with zipfile.ZipFile(file) as zip:
                with zip.open(internal_file_name) as internal_file:
                    while True:
                        data = internal_file.read(BUF_SIZE)
                        if not data:
                            break
                        hash_obj.update(data)
        else:
            with open(file, "rb") as f:
                while True:
                    data = f.read(BUF_SIZE)
                    if not data:
                        break
                    hash_obj.update(data)

        return hash_obj.hexdigest()
    except FileNotFoundError as e:
        send_message(f"\n\t\t\tError: File not found - {e}", error=True)
        return None
    except KeyError as e:
        send_message(f"\n\t\t\tError: File not found in the zip - {e}", error=True)
        return None
    except Exception as e:
        send_message(f"\n\t\t\tError: {e}", error=True)
        return None


def get_input_from_user(
    prompt, acceptable_values=[], example=None, timeout=90, use_timeout=False
):
    """Gets the user input and checks if it is valid."""

    def input_with_timeout(prompt, shared_variable):
        while not shared_variable.get("done"):
            user_input = input(prompt)
            if user_input and (
                not acceptable_values or user_input in acceptable_values
            ):
                shared_variable["done"] = True
                shared_variable["input"] = user_input

    if example:
        if isinstance(example, list):
            example = f" or ".join(
                [f"{example_item}" for example_item in example[:-1]]
                + [f"{example[-1]}"]
            )
        else:
            example = str(example)
        prompt = f"{prompt} ({example}): "
    else:
        prompt = f"{prompt}: "

    shared_variable = {"input": None, "done": False}

    if use_timeout:
        timer = threading.Timer(timeout, lambda: shared_variable.update({"done": True}))

    input_thread = threading.Thread(
        target=input_with_timeout, args=(prompt, shared_variable)
    )

    input_thread.start()
    if use_timeout:
        timer.start()

    while not shared_variable["done"]:
        input_thread.join(1)

        if use_timeout and not timer.is_alive():
            break

    if use_timeout:
        timer.cancel()

    return shared_variable["input"] if shared_variable["done"] else None