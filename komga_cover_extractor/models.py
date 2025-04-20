# komga_cover_extractor/models.py
import os

# Import necessary constants/functions from other modules
# Assuming config.py will define these lists/variables
try:
    from .config import (
        file_formats,
        file_extensions,
        library_types,
        translation_source_types,
        source_languages,
    )
except ImportError:
    print("WARN: Could not import from .config in models.py, using placeholders.")
    file_formats = ["chapter", "volume"]
    file_extensions = [".epub", ".zip", ".cbz"]
    library_types = []
    translation_source_types = ["official", "fan", "raw"]
    source_languages = ["english", "japanese", "chinese", "korean"]
# Note: file_utils import is handled within Folder.__init__ to avoid potential circular dependency


class LibraryType:
    """Represents a type of library with specific matching criteria."""

    def __init__(
        self, name, extensions, must_contain, must_not_contain, match_percentage=90
    ):
        self.name = name
        self.extensions = extensions
        self.must_contain = must_contain
        self.must_not_contain = must_not_contain
        self.match_percentage = match_percentage

    def __str__(self):
        return (
            f"LibraryType(name={self.name}, extensions={self.extensions}, "
            f"must_contain={self.must_contain}, must_not_contain={self.must_not_contain}, "
            f"match_percentage={self.match_percentage})"
        )

    def __repr__(self):
        return str(self)


class Folder:
    """Represents a folder with its contents."""

    def __init__(self, root, dirs, basename, folder_name, files):
        self.root = root
        self.dirs = dirs
        self.basename = basename  # Name of the parent directory
        self.folder_name = folder_name  # Name of the current directory
        # Dynamically load files if not provided
        if files is None:
            try:
                # Import locally to avoid circular dependency at module level
                from .file_utils import get_all_files_recursively_in_dir_watchdog

                self.files = get_all_files_recursively_in_dir_watchdog(root)
            except ImportError:
                print(
                    f"WARN: Could not import file_utils in Folder init for {root}. Files list will be empty."
                )
                self.files = []
            except Exception as e:
                print(f"ERROR getting files for Folder {root}: {e}")
                self.files = []
        else:
            self.files = files  # Use provided list

    def __str__(self):
        return (
            f"Folder(root={self.root}, dirs={self.dirs}, basename={self.basename}, "
            f"folder_name={self.folder_name}, files=[{len(self.files)} files])"
        )

    def __repr__(self):
        return str(self)


class File:
    """Represents a file with extracted properties."""

    def __init__(
        self,
        name,
        extensionless_name,
        basename,  # Often the derived series name
        extension,
        root,
        path,
        extensionless_path,
        volume_number,  # Can be int, float, list, or ""
        file_type,  # 'volume' or 'chapter'
        header_extension,  # Guessed extension from header
    ):
        self.name = name
        self.extensionless_name = extensionless_name
        self.basename = basename
        self.extension = extension
        self.root = root
        self.path = path
        self.extensionless_path = extensionless_path
        self.volume_number = volume_number
        self.file_type = file_type
        self.header_extension = header_extension

    def __str__(self):
        return f"File(name='{self.name}', type='{self.file_type}', vol='{self.volume_number}')"

    def __repr__(self):
        return str(self)


class Publisher:
    """Holds publisher information extracted from metadata and filename."""

    def __init__(self, from_meta=None, from_name=None):
        self.from_meta = from_meta
        self.from_name = from_name

    def __str__(self):
        return f"Publisher(from_meta={self.from_meta}, from_name={self.from_name})"

    def __repr__(self):
        return str(self)


class Volume:
    """Represents a volume (or chapter) with detailed extracted information."""

    def __init__(
        self,
        file_type,
        series_name,
        shortened_series_name,
        volume_year,
        volume_number,
        volume_part,
        index_number,  # Used for sorting, combines number and part
        release_group,
        name,
        extensionless_name,
        basename,  # Often same as series_name initially
        extension,
        root,
        path,
        extensionless_path,
        extras,  # List of bracketed info like (Digital)
        publisher,  # Publisher object
        is_premium,
        subtitle,
        header_extension,
        multi_volume=None,  # Boolean indicating if filename suggests multiple volumes (e.g., v01-v03)
        is_one_shot=None,  # Boolean indicating if it's likely a one-shot
    ):
        self.file_type = file_type
        self.series_name = series_name
        self.shortened_series_name = shortened_series_name
        self.volume_year = volume_year
        self.volume_number = volume_number
        self.volume_part = volume_part
        self.index_number = index_number
        self.release_group = release_group
        self.name = name
        self.extensionless_name = extensionless_name
        self.basename = basename
        self.extension = extension
        self.root = root
        self.path = path
        self.extensionless_path = extensionless_path
        self.extras = extras if isinstance(extras, list) else []
        self.publisher = publisher if isinstance(publisher, Publisher) else Publisher()
        self.is_premium = is_premium
        self.subtitle = subtitle
        self.header_extension = header_extension
        self.multi_volume = multi_volume
        self.is_one_shot = is_one_shot

    def __str__(self):
        return f"Volume(name='{self.name}', series='{self.series_name}', type='{self.file_type}', vol='{self.volume_number}', part='{self.volume_part}')"

    def __repr__(self):
        return str(self)


class Path:
    """Represents a configured path with associated library constraints."""

    def __init__(
        self,
        path,
        path_formats=file_formats,  # Use placeholder default
        path_extensions=file_extensions,  # Use placeholder default
        library_types=library_types,  # Use placeholder default
        translation_source_types=translation_source_types,  # Use placeholder default
        source_languages=source_languages,  # Use placeholder default
    ):
        self.path = path
        self.path_formats = path_formats
        self.path_extensions = path_extensions
        self.library_types = library_types
        self.translation_source_types = translation_source_types
        self.source_languages = source_languages

    def __str__(self):
        return (
            f"Path(path={self.path}, formats={self.path_formats}, extensions={self.path_extensions}, "
            f"types={self.library_types}, sources={self.translation_source_types}, langs={self.source_languages})"
        )

    def __repr__(self):
        return str(self)


class Embed:
    """Wrapper for DiscordEmbed object and optional associated file data."""

    def __init__(self, embed, file=None):
        self.embed = embed  # Should be a DiscordEmbed object
        self.file = file  # Optional file data (e.g., for attachments)

    def __repr__(self):
        has_file = "with file" if self.file else "no file"
        return f"Embed({self.embed.title if hasattr(self.embed, 'title') else 'No Title'}, {has_file})"


class Keyword:
    """Simple structure to hold a keyword and its score."""

    def __init__(self, name, score):
        self.name = name
        self.score = score

    def __str__(self):
        return f"Keyword(name='{self.name}', score={self.score})"

    def __repr__(self):
        return str(self)


class RankedKeywordResult:
    """Container for keyword ranking results."""

    def __init__(self, total_score, keywords):
        self.total_score = total_score
        self.keywords = keywords  # List of Keyword objects

    def __str__(self):
        return f"RankedKeywordResult(total_score={self.total_score}, keywords=[{len(self.keywords)} keywords])"

    def __repr__(self):
        return str(self)


class UpgradeResult:
    """Represents the result of an upgrade check between two releases."""

    def __init__(self, is_upgrade, downloaded_ranked_result, current_ranked_result):
        self.is_upgrade = is_upgrade
        self.downloaded_ranked_result = downloaded_ranked_result  # RankedKeywordResult
        self.current_ranked_result = current_ranked_result  # RankedKeywordResult

    def __str__(self):
        return (
            f"UpgradeResult(is_upgrade={self.is_upgrade}, "
            f"dl_score={self.downloaded_ranked_result.total_score}, "
            f"curr_score={self.current_ranked_result.total_score})"
        )

    def __repr__(self):
        return str(self)


class NewReleaseNotification:
    """Data structure for sending new release notifications."""

    def __init__(self, number, title, color, fields, webhook, series_name, volume_obj):
        self.number = number
        self.title = title
        self.color = color
        self.fields = fields
        self.webhook = webhook
        self.series_name = series_name
        self.volume_obj = volume_obj  # The Volume object for this release

    def __repr__(self):
        return (
            f"NewReleaseNotification(series='{self.series_name}', num='{self.number}')"
        )


class BookwalkerBook:
    """Represents a book entry scraped from Bookwalker."""

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

    def __repr__(self):
        return f"BookwalkerBook(title='{self.title}', vol='{self.volume_number}')"


class BookwalkerSeries:
    """Represents a series scraped from Bookwalker, containing multiple books."""

    def __init__(self, title, books, book_count, book_type):
        self.title = title
        self.books = books  # List of BookwalkerBook objects
        self.book_count = book_count
        self.book_type = book_type

    def __repr__(self):
        return f"BookwalkerSeries(title='{self.title}', type='{self.book_type}', count={self.book_count})"


class IdentifierResult:
    """Holds results from matching series based on identifiers (ISBN, etc.)."""

    def __init__(self, series_name, identifiers, path, matches):
        self.series_name = series_name
        self.identifiers = identifiers  # List of identifiers from the source file
        self.path = path  # Path of the matched existing series folder
        self.matches = (
            matches  # List containing [source_identifiers, matched_file_identifiers]
        )

    def __repr__(self):
        return f"IdentifierResult(series='{self.series_name}', path='{self.path}')"


class Image_Result:
    """Holds results from image similarity comparison."""

    def __init__(self, ssim_score, image_source):
        self.ssim_score = ssim_score
        self.image_source = image_source  # Path to the image compared against

    def __repr__(self):
        return f"Image_Result(score={self.ssim_score:.4f}, source='{os.path.basename(self.image_source)}')"
