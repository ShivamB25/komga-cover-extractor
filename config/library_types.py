import re
from core.file_utils import get_file_extension

# Library Type class
class LibraryType:
    def __init__(
        self, name, extensions, must_contain, must_not_contain, match_percentage=90
    ):
        self.name = name
        self.extensions = extensions
        self.must_contain = must_contain
        self.must_not_contain = must_not_contain
        self.match_percentage = match_percentage

    # Convert the object to a string representation
    def __str__(self):
        return f"LibraryType(name={self.name}, extensions={self.extensions}, must_contain={self.must_contain}, must_not_contain={self.must_not_contain}, match_percentage={self.match_percentage})"

# Determines the files library type
def get_library_type(files, library_types, required_match_percentage=None):
    """
    Determines the library type of a list of files based on predefined criteria.

    Args:
        files (list): A list of file names (strings).
        library_types (list): A list of LibraryType objects defining the criteria.
        required_match_percentage (int, optional): Override the default match percentage. Defaults to None.

    Returns:
        LibraryType or None: The matched LibraryType object, or None if no match is found.
    """
    if not files:
        return None

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
        # Ensure division by zero doesn't occur if files list is empty (though checked earlier)
        if len(files) > 0 and (match_count / len(files) * 100) >= match_percentage:
            return library_type
    return None