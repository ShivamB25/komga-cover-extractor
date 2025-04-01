import os

# Folder Class
class Folder:
    def __init__(self, root, dirs, basename, folder_name, files):
        self.root = root
        self.dirs = dirs
        self.basename = basename
        self.folder_name = folder_name
        self.files = files

    # to string
    def __str__(self):
        return f"Folder(root={self.root}, dirs={self.dirs}, basename={self.basename}, folder_name={self.folder_name}, files={self.files})"

    def __repr__(self):
        return str(self)

# File Class
class File:
    def __init__(
        self,
        name,
        extensionless_name,
        basename,
        extension,
        root,
        path,
        extensionless_path,
        volume_number,
        file_type,
        header_extension,
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

class Publisher:
    def __init__(self, from_meta, from_name):
        self.from_meta = from_meta
        self.from_name = from_name

    # to string
    def __str__(self):
        return f"Publisher(from_meta={self.from_meta}, from_name={self.from_name})"

    def __repr__(self):
        return str(self)

# Volume Class
class Volume:
    def __init__(
        self,
        file_type,
        series_name,
        shortened_series_name,
        volume_year,
        volume_number,
        volume_part,
        index_number,
        release_group,
        name,
        extensionless_name,
        basename,
        extension,
        root,
        path,
        extensionless_path,
        extras,
        publisher,
        is_premium,
        subtitle,
        header_extension,
        multi_volume=None,
        is_one_shot=None,
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
        self.extras = extras
        self.publisher = publisher
        self.is_premium = is_premium
        self.subtitle = subtitle
        self.header_extension = header_extension
        self.multi_volume = multi_volume
        self.is_one_shot = is_one_shot

# Path Class
class Path:
    def __init__(
        self,
        path,
        path_formats,
        path_extensions,
        library_types,
        translation_source_types,
        source_languages,
    ):
        self.path = path
        self.path_formats = path_formats
        self.path_extensions = path_extensions
        self.library_types = library_types
        self.translation_source_types = translation_source_types
        self.source_languages = source_languages

    # to string
    def __str__(self):
        return f"Path(path={self.path}, path_formats={self.path_formats}, path_extensions={self.path_extensions}, library_types={self.library_types}, translation_source_types={self.translation_source_types}, source_languages={self.source_languages})"

    def __repr__(self):
        return str(self)

# The RankedKeywordResult class is a container for the total score and the keywords
class RankedKeywordResult:
    def __init__(self, total_score, keywords):
        self.total_score = total_score
        self.keywords = keywords

    # to string
    def __str__(self):
        return f"Total Score: {self.total_score}\nKeywords: {self.keywords}"

    def __repr__(self):
        return str(self)

# > This class represents the result of an upgrade check
class UpgradeResult:
    def __init__(self, is_upgrade, downloaded_ranked_result, current_ranked_result):
        self.is_upgrade = is_upgrade
        self.downloaded_ranked_result = downloaded_ranked_result
        self.current_ranked_result = current_ranked_result

    # to string
    def __str__(self):
        return f"Is Upgrade: {self.is_upgrade}\nDownloaded Ranked Result: {self.downloaded_ranked_result}\nCurrent Ranked Result: {self.current_ranked_result}"

    def __repr__(self):
        return str(self)

class NewReleaseNotification:
    def __init__(self, number, title, color, fields, webhook, series_name, volume_obj):
        self.number = number
        self.title = title
        self.color = color
        self.fields = fields
        self.webhook = webhook
        self.series_name = series_name
        self.volume_obj = volume_obj

class Result:
    def __init__(self, dir, score):
        self.dir = dir
        self.score = score

    # to string
    def __str__(self):
        return f"dir: {self.dir}, score: {self.score}"

    def __repr__(self):
        return str(self)

# Result class that is used for our image_comparison results from our
# image comparison function
class Image_Result:
    def __init__(self, ssim_score, image_source):
        self.ssim_score = ssim_score
        self.image_source = image_source