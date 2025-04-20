# komga_cover_extractor/komga_utils.py
import requests
import time
from base64 import b64encode

# Import necessary config variables
# Use try-except for robustness during refactoring
try:
    from .config import komga_ip, komga_port, komga_login_email, komga_login_password
except ImportError:
    print("WARN: Could not import from .config in komga_utils.py, using placeholders.")
    komga_ip, komga_port, komga_login_email, komga_login_password = (
        None,
        None,
        None,
        None,
    )

# Import necessary functions from other utils
try:
    from .log_utils import send_message
except ImportError:
    print("WARN: Could not import send_message from log_utils.py")

    def send_message(msg, error=False, discord=False):
        print(f"{'ERROR: ' if error else ''}{msg}")


# Sends a scan request to Komga for a specific library ID.
def scan_komga_library(library_id):
    """Sends a POST request to trigger a library scan in Komga."""
    if (
        not komga_ip or not komga_login_email or not komga_login_password
    ):  # Use imported config values
        send_message(
            "Komga connection details (IP, email, password) missing in config.",
            error=True,
        )  # Use imported log_utils function
        return False

    komga_url = (
        f"{komga_ip}:{komga_port}" if komga_port else komga_ip
    )  # Use imported config values
    api_url = f"{komga_url}/api/v1/libraries/{library_id}/scan"
    auth_header = "Basic %s" % b64encode(
        f"{komga_login_email}:{komga_login_password}".encode("utf-8")
    ).decode(
        "utf-8"
    )  # Use imported config values

    print(f"\nSending Komga Scan Request for Library ID: {library_id}")
    try:
        response = requests.post(
            api_url,
            headers={
                "Authorization": auth_header,
                "Accept": "*/*",  # Komga API typically accepts this
            },
            timeout=30,  # Add a timeout
        )
        # Komga returns 202 Accepted on successful scan trigger
        if response.status_code == 202:
            send_message(
                f"\tSuccessfully Initiated Scan for Library ID: {library_id}.",
                discord=False,
            )  # Use imported log_utils function
            return True
        else:
            send_message(
                f"\tFailed to Initiate Scan for Library ID: {library_id}. "
                f"Status Code: {response.status_code}. Response: {response.text}",
                error=True,
            )  # Use imported log_utils function
            return False
    except requests.exceptions.RequestException as e:
        send_message(
            f"Failed to connect to Komga to initiate scan for Library ID: {library_id}. ERROR: {e}",
            error=True,
        )  # Use imported log_utils function
        return False
    except Exception as e:
        send_message(
            f"An unexpected error occurred during Komga scan request for Library ID: {library_id}. ERROR: {e}",
            error=True,
        )  # Use imported log_utils function
        return False


# Sends a GET request to Komga to retrieve all libraries.
def get_komga_libraries(first_run=True):
    """Retrieves a list of libraries from the Komga server."""
    results = []
    if (
        not komga_ip or not komga_login_email or not komga_login_password
    ):  # Use imported config values
        send_message(
            "Komga connection details (IP, email, password) missing in config.",
            error=True,
        )  # Use imported log_utils function
        return results

    komga_url = (
        f"{komga_ip}:{komga_port}" if komga_port else komga_ip
    )  # Use imported config values
    api_url = f"{komga_url}/api/v1/libraries"
    auth_header = "Basic %s" % b64encode(
        f"{komga_login_email}:{komga_login_password}".encode("utf-8")
    ).decode(
        "utf-8"
    )  # Use imported config values

    print("\nRetrieving Komga Libraries...")
    try:
        response = requests.get(
            api_url,
            headers={
                "Authorization": auth_header,
                "Accept": "application/json",  # Prefer JSON response
            },
            timeout=30,  # Add a timeout
        )
        response.raise_for_status()  # Raise an exception for bad status codes
        results = response.json()
        print(f"\tSuccessfully retrieved {len(results)} libraries.")
    except requests.exceptions.ConnectionError as e:
        # Specific handling for connection errors, potentially retry logic
        if first_run and isinstance(
            e, requests.exceptions.ConnectionError
        ):  # Basic retry logic example
            send_message(
                f"Connection error retrieving Komga libraries: {e}. Retrying in 60s...",
                error=True,
            )
            time.sleep(60)
            results = get_komga_libraries(first_run=False)  # Recursive call for retry
        else:
            send_message(
                f"Failed to connect to Komga to get libraries. ERROR: {e}", error=True
            )
    except requests.exceptions.RequestException as e:
        send_message(
            f"Failed to get Komga libraries. Status Code: {getattr(e.response, 'status_code', 'N/A')}. Response: {getattr(e.response, 'text', 'N/A')}. ERROR: {e}",
            error=True,
        )  # Use imported log_utils function
    except Exception as e:
        send_message(
            f"An unexpected error occurred while getting Komga libraries. ERROR: {e}",
            error=True,
        )  # Use imported log_utils function

    return results
