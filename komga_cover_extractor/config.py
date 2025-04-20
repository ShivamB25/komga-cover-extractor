# komga_cover_extractor/config.py
import os
import re
from .log_utils import get_lines_from_file, send_message # Assuming get_lines_from_file is moved or available

# --- Version ---
script_version = (2, 5, 30) # Assuming this is static config
script_version_text = "v{}.{}.{}".format(*script_version)

# --- Paths ---
# These will be populated by argument parsing in main.py
paths = []
download_folders = []
paths_with_types = []
download_folders_with_types = []

# --- Directories ---
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # Get project root
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
ADDONS_DIR = os.path.join(ROOT_DIR, "addons")

# --- Feature Toggles ---
correct_file_extensions_toggle = False
convert_to_cbz_toggle = False
delete_unacceptable_files_toggle = False
delete_chapters_from_downloads_toggle = False
rename_files_in_download_folders_toggle = False
create_folders_for_items_in_download_folder_toggle = False
rename_dirs_in_download_folder_toggle = False
check_for_duplicate_volumes_toggle = False
extract_covers_toggle = True
check_for_existing_series_toggle = False
check_for_missing_volumes_toggle = False
cache_each_root_for_each_path_in_paths_at_beginning_toggle = False
send_scan_request_to_komga_libraries_toggle = False
uncheck_non_qbit_upgrades_toggle = False
log_to_file = False
watchdog_toggle = False # This will be set by args in main.py
auto_classify_watchdog_paths = False
move_new_series_to_library_toggle = False
move_series_to_correct_library_toggle = False
generate_release_group_list_toggle = False # For manual list generation
chapter_support_toggle = False # EXPERIMENTAL
rename_chapters_with_preferred_chapter_keyword = False
extract_chapter_covers = False
compare_detected_cover_to_blank_images = False
use_latest_volume_cover_as_series_cover = False
rename_zip_to_cbz = True
delete_unacceptable_torrent_titles_in_qbit = False
match_through_identifiers = False
match_through_image_similarity = True
copy_existing_volume_covers_toggle = False

# --- Renaming & Processing ---
preferred_volume_renaming_format = "v"
preferred_chapter_renaming_format = "c"
add_volume_one_number_to_one_shots = False
add_issue_number_to_manga_file_name = False
manual_rename = True
resturcture_when_renaming = False
search_and_add_premium_to_file_name = False
add_publisher_name_to_file_name_when_renaming = False
move_release_group_to_end_of_file_name = False
replace_unicode_when_restructuring = False
mute_discord_rename_notifications = False
move_lone_files_to_similar_folder = False
replace_series_name_in_file_name_with_similar_folder_name = False

# --- File Types & Extensions ---
seven_zip_extensions = [".7z"]
zip_extensions = [".zip", ".cbz", ".epub"]
rar_extensions = [".rar", ".cbr"]
novel_extensions = [".epub"]
manga_extensions = [x for x in zip_extensions if x not in novel_extensions]
file_extensions = novel_extensions + manga_extensions
convertable_file_extensions = seven_zip_extensions + rar_extensions
image_extensions = {".jpg", ".jpeg", ".png", ".tbn", ".webp"}
file_formats = ["chapter", "volume"]

# --- Keywords & Regex ---
# Volume Keywords
volume_keywords = ["LN", "Light Novels?", "Novels?", "Books?", "Volumes?", "Vols?", "Discs?", "Tomo", "Tome", "Von", "V", "第", "T"]
volume_regex_keywords = "(?<![A-Za-z])" + "|(?<![A-Za-z])".join(volume_keywords)
# Chapter Keywords
chapter_keywords = ["Chapters?", "Chaps?", "Chs?", "Cs?", "D"]
chapter_regex_keywords = r"(?<![A-Za-z])" + (r"|(?<![A-Za-z])").join(chapter_keywords)
# Exclusion Keywords (for chapter number detection)
exclusion_keywords = [r"(\s)Part(\s)", r"(\s)Episode(\s)", r"(\s)Season(\s)", r"(\s)Arc(\s)", r"(\s)Prologue(\s)", r"(\s)Epilogue(\s)", r"(\s)Omake(\s)", r"(\s)Extra(\s)", r"(\s)- Special(\s)", r"(\s)Side Story(\s)", r"(\s)Act(\s)", r"(\s)Special Episode(\s)", r"(\s)Ep(\s)", r"(\s)- Version(\s)", r"(\s)Ver(\s)", r"(\s)PT\.", r"(\s)PT(\s)", r",", r"(\s)×", r"\d\s*-\s*", r"\bNo.", r"\bNo.(\s)", r"\bBonus(\s)", r"(\]|\}|\)) -", r"\bZom(\s)", r"Tail -", r"꞉", r":", r"\d\."]
exclusion_keywords_joined = "|".join(exclusion_keywords)
exclusion_keywords_regex = r"(?<!%s)" % exclusion_keywords_joined
# Subtitle Exclusion Keywords
subtitle_exclusion_keywords = [r"-(\s)", r"-", r"-\s[A-Za-z]+\s"]
subtitle_exclusion_keywords_joined = "|".join(subtitle_exclusion_keywords)
subtitle_exclusion_keywords_regex = r"(?<!%s)" % subtitle_exclusion_keywords_joined
# Chapter Search Patterns (Order matters!)
chapter_searches = [
    r"\b\s-\s*(#)?(\d+)([-_.]\d+)*(x\d+)?\s*-\s",
    r"\b(?<![\[\(\{])(%s)(\.)?\s*(\d+)([-_.]\d+)*(x\d+)?\b(?<!\s(\d+)([-_.]\d+)*(x\d+)?\s.*)" % chapter_regex_keywords,
    r"(?<![A-Za-z]|%s)(?<![\[\(\{])(((%s)([-_. ]+)?(\d+)([-_.]\d+)*(x\d+)?)|\s+(\d+)(\.\d+)?(x\d+((\.\d+)+)?)?(\s+|#\d+|%s))" % (exclusion_keywords_joined, chapter_regex_keywords, "|".join(manga_extensions).replace(".", r"\.")), # manga_extensions_regex
    r"((?<!^)\b(\.)?\s*(%s)(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*\b)((\s+-|:)\s+).*?(?=\s*[\(\[\{](\d{4}|Digital)[\)\]\}])" % exclusion_keywords_regex,
    r"(\b(%s)?(\.)?\s*((%s)(\d{1,2})|\d{3,})([-_.]\d+)*(x\d+)?(#\d+([-_.]\d+)*)?\b)\s*((\[[^\]]*\]|\([^\)]*\)|\{[^}]*\})|((?<!\w(\s))|(?<!\w))(%s)(?!\w))" % (chapter_regex_keywords, exclusion_keywords_regex, "|".join(file_extensions).replace(".", r"\.")), # file_extensions_regex
    r"^((#)?(\d+)([-_.]\d+)*((x|#)(\d+)([-_.]\d+)*)*)$",
]
chapter_search_patterns_comp = [re.compile(pattern, flags=re.IGNORECASE) for pattern in chapter_searches]
# Other Regex
volume_year_regex = r"(\(|\[|\{)(\d{4})(\)|\]|\})"
file_extensions_regex = "|".join(file_extensions).replace(".", r"\.")
manga_extensions_regex = "|".join(manga_extensions).replace(".", r"\.")
novel_extensions_regex = "|".join(novel_extensions).replace(".", r"\.")
image_extensions_regex = "|".join(image_extensions).replace(".", r"\.")

# --- Similarity & Scoring ---
required_similarity_score = 0.9790
publisher_similarity_score = 0.9
release_group_similarity_score = 0.8
required_image_similarity_score = 0.9
blank_cover_required_similarity_score = 0.9
required_matching_percentage = 90 # For folder type matching
short_word_filter_percentage = 0.7

# --- Keyword Ranking (for Upgrades) ---
class Keyword: # Define locally for config use
    def __init__(self, name, score, file_type="both"):
        self.name = name
        self.score = score
        self.file_type = file_type
    def __str__(self): return f"Name: {self.name}, Score: {self.score}, File Type: {self.file_type}"
    def __repr__(self): return str(self)

ranked_keywords = [] # Define your Keyword objects here as in settings.py

# --- Library Types ---
# Define LibraryType locally or import from models? Import seems better.
from .models import LibraryType
library_types = [
    LibraryType("manga", manga_extensions, [r"\(Digital\)"], [r"Webtoon", r"^(?=.*Digital)((?=.*Compilation)|(?=.*danke-repack))"], 1),
    LibraryType("light novel", novel_extensions, [r"\[[^\]]*(Lucaz|Stick|Oak|Yen (Press|On)|J-Novel|Seven Seas|Vertical|One Peace Books|Cross Infinite|Sol Press|Hanashi Media|Kodansha|Tentai Books|SB Creative|Hobby Japan|Impress Corporation|KADOKAWA|Viz Media)[^\]]*\]|(faratnis)"], []),
    LibraryType("digital_comps", manga_extensions, [r"^(?=.*Digital)((?=.*Compilation)|(?=.*danke-repack))"], []),
]
translation_source_types = ["official", "fan", "raw"]
source_languages = ["english", "japanese", "chinese", "korean"]

# --- Misc ---
ignored_folder_names = []
unacceptable_keywords = [] # List of regex patterns
average_chapter_image_count = 85
series_cover_file_names = ["cover", "poster"]
zfill_volume_int_value = 2
zfill_volume_float_value = 4
zfill_chapter_int_value = 3
zfill_chapter_float_value = 5
sleep_timer = 10 # General sleep timer
sleep_timer_bk = 2 # Bookwalker sleep timer
watchdog_discover_new_files_check_interval = 5
watchdog_file_transferred_check_interval = 1
profile_code = "" # For cProfile
in_docker = ROOT_DIR == "/app" # Docker check
if in_docker: script_version_text += " • Docker"

# --- Image Handling ---
compress_image_option = False
image_quality = 40
output_covers_as_webp = False
blank_white_image_path = os.path.join(ROOT_DIR, "blank_white.jpg") if os.path.isfile(os.path.join(ROOT_DIR, "blank_white.jpg")) else None
blank_black_image_path = os.path.join(ROOT_DIR, "blank_black.png") if os.path.isfile(os.path.join(ROOT_DIR, "blank_black.png")) else None

# --- Discord ---
discord_webhook_url = [] # Populated by args
bookwalker_webhook_urls = [] # Populated by args
new_volume_webhook = None # Populated by args
discord_embed_limit = 10
bookwalker_logo_url = "https://play-lh.googleusercontent.com/a7jUyjTxWrl_Kl1FkUSv2FHsSu3Swucpem2UIFDRbA1fmt5ywKBf-gcwe6_zalOqIR7V=w240-h480-rw"
# Colors
purple_color = 7615723
red_color = 16711680
grey_color = 8421504
yellow_color = 16776960
green_color = 65280
preorder_blue_color = 5919485

# --- Komga ---
komga_ip = ""
komga_port = ""
komga_login_email = ""
komga_login_password = ""
komga_libraries = [] # Populated by API call in main.py/core_logic.py

# --- qBittorrent ---
qbittorrent_ip = ""
qbittorrent_port = ""
qbittorrent_target_category = ""
qbittorrent_username = ""
qbittorrent_password = ""

# --- Dynamic Lists (Loaded from files) ---
release_groups = []
publishers = []
skipped_release_group_files = []
skipped_publisher_files = []

def load_list_from_file(filename, target_list):
    """Loads lines from a file in the LOGS_DIR into the target list."""
    filepath = os.path.join(LOGS_DIR, filename)
    if os.path.isfile(filepath):
        try:
            lines = get_lines_from_file(filepath) # Use imported log_utils function
            target_list.extend(line for line in lines if line not in target_list)
            print(f"\tLoaded {len(lines)} items from {filename}")
        except Exception as e:
            send_message(f"Error loading {filename}: {e}", error=True) # Use imported log_utils function

# Load lists on module import
load_list_from_file("release_groups.txt", release_groups)
load_list_from_file("publishers.txt", publishers)
load_list_from_file("skipped_release_group_files.txt", skipped_release_group_files)
load_list_from_file("skipped_publisher_files.txt", skipped_publisher_files)

# --- Derived Regex (after lists are loaded) ---
publishers_joined = "|".join(map(re.escape, publishers)) if publishers else ""
release_groups_joined = "|".join(map(re.escape, release_groups)) if release_groups else ""

publishers_joined_regex = re.compile(rf"(?<=[\(\[\{{])({publishers_joined})(?=[\)\]\}}])", re.IGNORECASE) if publishers_joined else None
release_groups_joined_regex = re.compile(rf"(?<=[\(\[\{{])({release_groups_joined})(?=[\)\]\}}])", re.IGNORECASE) if release_groups_joined else None
# Construct the regex string first, then compile
# Construct the regex string first using an f-string, escaping regex braces
_rg_end_pattern_fstring = f"-(?! )([^\\(\\)\\[\\]\\{{}}\\+]+)(?:{file_extensions_regex})$" if file_extensions_regex else None
# Now compile the resulting string
release_group_end_regex = re.compile(_rg_end_pattern_fstring, re.IGNORECASE) if _rg_end_pattern_fstring else None

# --- State Variables (Should ideally not be here) ---
# These are runtime state, not config. They need proper management.
# Defining them here might cause issues if multiple parts of the code modify them.
# Consider a dedicated state module or class.
transferred_files = []
transferred_dirs = []
# grouped_notifications = [] # Managed by discord_utils
# moved_files = [] # Managed by core_logic/series_matching
# moved_folders = [] # Managed by file_operations
# libraries_to_scan = [] # Managed by core_logic
# cached_paths = [] # Managed by file_utils/core_logic
# cached_identifier_results = [] # Managed by series_matching
# cached_image_similarity_results = [] # Managed by series_matching