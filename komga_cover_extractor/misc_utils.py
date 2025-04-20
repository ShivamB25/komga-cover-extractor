# komga_cover_extractor/misc_utils.py
import subprocess
import sys
import threading
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# TODO: Ensure log_utils is correctly imported and used
from .log_utils import send_message


# Executes a command and prints the output to the console.
def execute_command(command):
    """Executes a shell command and streams its output."""
    process = None
    try:
        # Use shell=True carefully, ensure command is safe if constructed from external input
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            text=True,
            bufsize=1,
        )
        while True:
            output = process.stdout.readline()
            if output == "" and process.poll() is not None:
                break
            if output:
                # Print directly to stdout without using send_message to avoid potential recursion
                print(output.strip())
                sys.stdout.flush()
        rc = process.poll()
        return rc
    except Exception as e:
        send_message(
            f"Error executing command '{command}': {e}", error=True
        )  # Use imported log_utils function
        return None  # Indicate failure


# Gets the user input and checks if it is valid
def get_input_from_user(
    prompt, acceptable_values=[], example=None, timeout=90, use_timeout=False
):
    """Gets validated input from the user, optionally with a timeout."""

    # Function that gets user input and stores it in the shared_variable
    def input_with_timeout(prompt_str, shared_variable_dict):
        try:
            user_input = input(prompt_str)
            if not acceptable_values or user_input in acceptable_values:
                shared_variable_dict["input"] = user_input
        except EOFError:  # Handle cases where input stream is closed (e.g., piping)
            pass  # Input will remain None
        finally:
            shared_variable_dict["done"] = (
                True  # Signal completion regardless of input validity
            )

    # Format the prompt
    if example:
        example_str = (
            f" or ".join(map(str, example))
            if isinstance(example, list)
            else str(example)
        )
        prompt_str = f"{prompt} ({example_str}): "
    else:
        prompt_str = f"{prompt}: "

    shared_variable = {"input": None, "done": False}
    timer = None
    input_thread = threading.Thread(
        target=input_with_timeout, args=(prompt_str, shared_variable)
    )
    input_thread.daemon = True  # Allow program to exit even if this thread is blocked
    input_thread.start()

    if use_timeout:
        timer = threading.Timer(timeout, lambda: shared_variable.update({"done": True}))
        timer.start()

    # Wait for the thread to finish or timeout
    while not shared_variable["done"]:
        input_thread.join(0.1)  # Check frequently without blocking indefinitely
        if use_timeout and timer and not timer.is_alive():
            print("\nInput timed out.")
            break  # Exit loop if timer finished

    if timer:
        timer.cancel()  # Cancel timer if input was received before timeout

    return shared_variable["input"]


# takes a time.time, gets the current time and prints the execution time,
def print_execution_time(start_time, function_name):
    """Calculates and prints the execution time of a function."""
    end_time = time.time()
    execution_time = end_time - start_time
    print(f"\nExecution time for: {function_name}: {execution_time:.4f} seconds")


# --- Web Scraping Helpers ---

# Session objects dictionary, one for each domain
session_objects = {}


def get_session_object(url):
    """Returns a reusable requests.Session object for a given URL's domain."""
    try:
        domain = urlparse(url).netloc.split(":")[0]
        if domain not in session_objects:
            session_object = requests.Session()
            session_object.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"
                }
            )
            session_objects[domain] = session_object
        return session_objects[domain]
    except Exception as e:
        send_message(
            f"Error getting session object for {url}: {e}", error=True
        )  # Use imported log_utils function
        return None


def scrape_url(url, strainer=None, headers=None, cookies=None, proxy=None):
    """Makes a GET request and returns a BeautifulSoup object."""
    try:
        session_object = get_session_object(url)  # Use local function
        if not session_object:
            return None

        request_params = {
            "headers": headers,
            "cookies": cookies,
            "proxies": proxy,
            "timeout": 10,  # Standard timeout
        }
        # Filter out None values before passing to requests
        filtered_params = {k: v for k, v in request_params.items() if v is not None}

        response = session_object.get(url, **filtered_params)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)

        # Use lxml for parsing speed
        soup = BeautifulSoup(response.content, "lxml", parse_only=strainer)
        return soup

    except requests.exceptions.RequestException as e:
        send_message(
            f"Error scraping URL {url}: {e}", error=True
        )  # Use imported log_utils function
        return None
    except Exception as e:  # Catch other potential errors during scraping/parsing
        send_message(f"Unexpected error scraping {url}: {e}", error=True)
        return None


# --- Type Conversion/Checking ---


def set_num_as_float_or_int(volume_number, silent=False):
    """Converts a string number or list of string numbers to int/float."""
    if volume_number == "":
        return ""
    try:
        if isinstance(volume_number, list):
            # Handle ranges represented as lists
            processed_nums = []
            for num_str in volume_number:
                num = float(num_str)
                processed_nums.append(int(num) if num.is_integer() else num)
            # Reconstruct range string if needed, or just return list?
            # Original returned a string like "1-3", let's stick to that for now.
            if len(processed_nums) == 2:
                return f"{processed_nums[0]}-{processed_nums[1]}"
            elif len(processed_nums) == 1:
                return processed_nums[0]
            else:  # Unexpected list format
                return ""
        elif isinstance(volume_number, str):
            num = float(volume_number)
            return int(num) if num.is_integer() else num
        elif isinstance(volume_number, (int, float)):
            return (
                int(volume_number)
                if float(volume_number).is_integer()
                else float(volume_number)
            )
    except (ValueError, TypeError) as e:
        if not silent:
            send_message(
                f"Failed to convert number: {volume_number}\nERROR: {e}", error=True
            )  # Use imported log_utils function
        return ""
    return volume_number  # Fallback


def isfloat(x):
    """Checks if a value can be converted to a float."""
    try:
        float(x)
    except (TypeError, ValueError):
        return False
    else:
        return True


def isint(x):
    """Checks if a value can be converted to an integer."""
    try:
        a = float(x)
        b = int(a)
    except (TypeError, ValueError):
        return False
    else:
        return a == b


# --- Comparison ---


def is_same_index_number(index_one, index_two, allow_array_match=False):
    """Checks if two index numbers are the same, optionally allowing array matching."""
    # Ensure consistent types for comparison if possible
    try:
        num_one = set_num_as_float_or_int(index_one, silent=True)
        num_two = set_num_as_float_or_int(index_two, silent=True)
    except:  # If conversion fails, fallback to original values
        num_one = index_one
        num_two = index_two

    if num_one == num_two and num_one != "":
        return True
    elif allow_array_match:
        # Ensure we are comparing lists/tuples correctly
        list_one = (
            index_one
            if isinstance(index_one, (list, tuple))
            else [num_one] if num_one != "" else []
        )
        list_two = (
            index_two
            if isinstance(index_two, (list, tuple))
            else [num_two] if num_two != "" else []
        )

        # Check for overlap
        if list_one and list_two:
            set_one = set(
                set_num_as_float_or_int(i, silent=True)
                for i in list_one
                if set_num_as_float_or_int(i, silent=True) != ""
            )
            set_two = set(
                set_num_as_float_or_int(i, silent=True)
                for i in list_two
                if set_num_as_float_or_int(i, silent=True) != ""
            )
            if set_one.intersection(set_two):
                return True
    return False
