import os
import re


# Returns an extensionless name
def get_extensionless_name(file):
    return os.path.splitext(file)


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