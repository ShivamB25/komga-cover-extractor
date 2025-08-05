# -*- coding: utf-8 -*-
"""
Komga API client.
"""
import os
import re
import time
from base64 import b64encode

import requests

from settings import (
    komga_ip,
    komga_login_email,
    komga_login_password,
    komga_port,
)
from utils.helpers import send_message


# Sends scan requests to komga for all passed-in libraries
# Reqiores komga settings to be set in settings.py
def scan_komga_library(library_id):
    if not komga_ip:
        send_message(
            "Komga IP is not set in settings.py. Please set it and try again.",
            error=True,
        )
        return

    if not komga_login_email:
        send_message(
            "Komga Login Email is not set in settings.py. Please set it and try again.",
            error=True,
        )
        return

    if not komga_login_password:
        send_message(
            "Komga Login Password is not set in settings.py. Please set it and try again.",
            error=True,
        )
        return

    komga_url = f"{komga_ip}:{komga_port}" if komga_port else komga_ip

    print("\nSending Komga Scan Request:")
    try:
        request = requests.post(
            f"{komga_url}/api/v1/libraries/{library_id}/scan",
            headers={
                "Authorization": "Basic %s"
                % b64encode(
                    f"{komga_login_email}:{komga_login_password}".encode("utf-8")
                ).decode("utf-8"),
                "Accept": "*/*",
            },
        )
        if request.status_code == 202:
            send_message(
                f"\t\tSuccessfully Initiated Scan for: {library_id} Library.",
                discord=False,
            )
        else:
            send_message(
                f"\t\tFailed to Initiate Scan for: {library_id} Library "
                f"Status Code: {request.status_code} Response: {request.text}",
                error=True,
            )
    except Exception as e:
        send_message(
            f"Failed to Initiate Scan for: {library_id} Komga Library, ERROR: {e}",
            error=True,
        )


# Sends a GET library request to Komga for all libraries using
# {komga_url}/api/v1/libraries
# Requires komga settings to be set in settings.py
def get_komga_libraries(first_run=True):
    results = []

    if not komga_ip:
        send_message(
            "Komga IP is not set in settings.py. Please set it and try again.",
            error=True,
        )
        return

    if not komga_login_email:
        send_message(
            "Komga Login Email is not set in settings.py. Please set it and try again.",
            error=True,
        )
        return

    if not komga_login_password:
        send_message(
            "Komga Login Password is not set in settings.py. Please set it and try again.",
            error=True,
        )
        return

    komga_url = f"{komga_ip}:{komga_port}" if komga_port else komga_ip

    try:
        request = requests.get(
            f"{komga_url}/api/v1/libraries",
            headers={
                "Authorization": "Basic %s"
                % b64encode(
                    f"{komga_login_email}:{komga_login_password}".encode("utf-8")
                ).decode("utf-8"),
                "Accept": "*/*",
            },
        )
        if request.status_code == 200:
            results = request.json()
        else:
            send_message(
                f"\t\tFailed to Get Komga Libraries "
                f"Status Code: {request.status_code} "
                f"Response: {request.text}",
                error=True,
            )
    except Exception as e:
        # if first, and error code 104, then try again after sleeping
        if first_run and "104" in str(e):
            time.sleep(60)
            results = get_komga_libraries(first_run=False)
        else:
            send_message(
                f"Failed to Get Komga Libraries, ERROR: {e}",
                error=True,
            )
    return results


# Normalize path separators and remove Windows drive letters if present.
def normalize_path(path):
    path = os.path.normpath(path)

    # Remove Windows drive letters (e.g., "Z:\example\path" -> "\example\path")
    if ":" in path:
        path = re.sub(r"^[A-Za-z]:", "", path)

    # Convert backslashes to forward slashes for uniform comparison
    return path.replace("\\", "/")


# Check if root_path is a prefix of target_path, handling Windows and Linux paths.
def is_root_present(root_path, target_path):
    root_path = normalize_path(root_path)
    target_path = normalize_path(target_path)

    return root_path in target_path