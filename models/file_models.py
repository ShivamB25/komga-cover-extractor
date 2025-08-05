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


# Custom sorting key function, sort by index_number
def get_sort_key(index_number):
    if isinstance(index_number, list):
        return min(index_number)
    else:
        return index_number


# Sorts the volumes by the index number if they're all numbers,
# otherwise it sorts the volumes alphabetically by the file name.
def sort_volumes(volumes):
    if any(isinstance(item.index_number, str) for item in volumes):
        # sort alphabetically by the file name
        return sorted(volumes, key=lambda x: x.name)
    else:
        # sort by the index number
        return sorted(volumes, key=lambda x: get_sort_key(x.index_number))