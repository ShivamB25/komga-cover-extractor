# Komga Cover Extractor - Comprehensive Recombination Mapping Guide

## Table of Contents
1. [Overview](#overview)
2. [Module-to-Original Structure Mapping](#module-to-original-structure-mapping)
3. [Import Statement Consolidation](#import-statement-consolidation)
4. [Function Dependency Chart](#function-dependency-chart)
5. [Execution Flow Mapping](#execution-flow-mapping)
6. [Global Variable Migration Guide](#global-variable-migration-guide)
7. [Step-by-Step Recombination Instructions](#step-by-step-recombination-instructions)
8. [Cross-Reference Table](#cross-reference-table)
9. [Module Integration Verification](#module-integration-verification)

## Overview

This document provides comprehensive mapping and recombination instructions for the modularized `komga_cover_extractor.py` project. The original monolithic script was refactored into a modular structure with the following organization:

```
Original: komga_cover_extractor.py (~12,000 lines)
Modular:
├── main.py (188 lines) - Entry point
├── config/
│   ├── constants.py (479 lines) - Global constants and configurations
│   ├── settings_manager.py (558 lines) - Argument parsing and settings
│   └── state.py (15 lines) - Global state management
├── models/
│   ├── file_models.py (107 lines) - Data models and classes
│   └── [other model files]
├── utils/
│   ├── helpers.py (498 lines) - Utility functions
│   └── similarity.py (18 lines) - String similarity functions
├── filesystem/
│   ├── file_operations.py (288 lines) - File manipulation functions
│   └── [other filesystem files]
├── processing/
│   ├── cover_extractor.py (467 lines) - Core cover extraction logic
│   ├── image_processor.py (170 lines) - Image processing functions
│   └── [other processing files]
├── integrations/
│   ├── discord_client.py (183 lines) - Discord integration
│   └── [other integration files]
└── core/
    ├── duplicate_checker.py (367 lines) - Duplicate detection logic
    ├── series_matcher.py (1045 lines) - Series matching logic
    ├── upgrade_manager.py (340 lines) - File upgrade management
    └── watchdog_handler.py (356 lines) - File system monitoring
```

## Module-to-Original Structure Mapping

### 1. Configuration and Constants (Original: Lines 1-500 estimated)
**Modular Location:** `config/constants.py`
- **Lines 1-479:** All global constants, file extensions, regex patterns, colors, settings
- **Key Elements:**
  - Import statements for external libraries
  - File extension definitions
  - Regular expression patterns
  - Discord color constants
  - Global configuration variables

**Modular Location:** `config/settings_manager.py`
- **Lines 1-558:** Argument parsing and settings validation
- **Key Functions:**
  - `parse_my_args()` - Command line argument parsing
  - `check_required_settings()` - Settings validation
  - `process_path()` - Path processing logic

**Modular Location:** `config/state.py`
- **Lines 1-15:** Global state variables
- **Key Elements:**
  - `processed_files`, `processed_series`
  - `series_data`, `books_data`
  - Various cached data structures

### 2. Data Models and Classes (Original: Lines 500-800 estimated)
**Modular Location:** `models/file_models.py`
- **Lines 1-107:** Core data models
- **Key Classes:**
  - `File` - File representation class
  - `Publisher` - Publisher information class
  - `Volume` - Volume representation class
  - Sorting and utility functions

### 3. Utility Functions (Original: Lines 800-1500 estimated)
**Modular Location:** `utils/helpers.py`
- **Lines 1-498:** Core utility functions
- **Key Functions:**
  - String manipulation functions (`clean_str`, `remove_brackets`, `normalize_str`)
  - File system utilities (`get_file_extension`, `get_modification_date`)
  - Number conversion functions (`set_num_as_float_or_int`)
  - Path normalization functions

**Modular Location:** `utils/similarity.py`
- **Lines 1-18:** String similarity functions
- **Key Functions:**
  - `similar()` - String similarity comparison using SequenceMatcher

### 4. File System Operations (Original: Lines 1500-2500 estimated)
**Modular Location:** `filesystem/file_operations.py`
- **Lines 1-288:** File manipulation functions
- **Key Functions:**
  - `rename_file()`, `move_folder()`, `replace_file()`
  - `remove_file()`, `remove_folder()`
  - `get_file_size()`, `get_header_extension()`

### 5. Processing Logic (Original: Lines 2500-6000 estimated)
**Modular Location:** `processing/cover_extractor.py`
- **Lines 1-467:** Core cover extraction logic
- **Key Functions:**
  - `extract_covers()` - Main cover extraction function
  - `find_and_extract_cover()` - Individual file cover extraction
  - `get_novel_cover_path()` - Novel cover path resolution

**Modular Location:** `processing/image_processor.py`
- **Lines 1-170:** Image processing functions
- **Key Functions:**
  - `compress_image()` - Image compression
  - `compare_images()` - Image comparison using SSIM
  - `convert_webp_to_jpg()` - Format conversion

### 6. Integration Services (Original: Lines 6000-7500 estimated)
**Modular Location:** `integrations/discord_client.py`
- **Lines 1-183:** Discord webhook integration
- **Key Functions:**
  - `send_discord_message()` - Discord notification sending
  - `handle_fields()` - Discord embed field handling
  - `group_notification()` - Notification grouping

### 7. Core Business Logic (Original: Lines 7500-12000 estimated)
**Modular Location:** `core/duplicate_checker.py`
- **Lines 1-367:** Duplicate detection and removal
- **Key Functions:**
  - `check_for_duplicate_volumes()` - Main duplicate checking
  - `is_upgradeable()` - Upgrade possibility checking
  - `get_keyword_scores()` - Quality scoring

**Modular Location:** `core/series_matcher.py`
- **Lines 1-1045:** Series matching logic
- **Key Functions:**
  - `check_for_existing_series()` - Main series matching function
  - Complex matching algorithms and similarity checking

**Modular Location:** `core/upgrade_manager.py`
- **Lines 1-340:** File conversion and upgrade management
- **Key Functions:**
  - `convert_to_cbz()` - Archive conversion to CBZ format
  - `extract()`, `compress()` - Archive handling

**Modular Location:** `core/watchdog_handler.py`
- **Lines 1-356:** File system monitoring
- **Key Classes:**
  - `Watcher` - Main watchdog class
  - `Handler` - File system event handler

### 8. Main Entry Point
**Modular Location:** `main.py`
- **Lines 1-188:** Main execution logic
- **Key Functions:**
  - `main()` - Main execution function
  - Entry point logic and orchestration

## Import Statement Consolidation

### External Library Imports (for original file)
```python
import argparse
import cProfile
import os
import re
import shutil
import tempfile
import time
import traceback
import zipfile
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache

import cv2
import filetype
import py7zr
import rarfile
import scandir
from discord_webhook import DiscordEmbed, DiscordWebhook
from PIL import Image
from skimage.metrics import structural_similarity as ssim
from unidecode import unidecode
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Settings import
import settings as settings_file
from settings import *
```

### Internal Import Consolidation
When recombining, all internal imports between modules should be removed since all functions will be in the same file. The modular imports to eliminate include:

```python
# These imports should be removed in the consolidated file:
from config.constants import *
from config.settings_manager import *
from config.state import *
from models.file_models import *
from utils.helpers import *
from utils.similarity import *
from filesystem.file_operations import *
from processing.cover_extractor import *
from processing.image_processor import *
from integrations.discord_client import *
from core.duplicate_checker import *
from core.series_matcher import *
from core.upgrade_manager import *
from core.watchdog_handler import *
```

## Function Dependency Chart

```mermaid
graph TD
    A[main.py main()] --> B[parse_my_args]
    A --> C[check_required_settings]
    A --> D[cache_existing_library_paths]
    A --> E[delete_unacceptable_files]
    A --> F[rename_files]
    A --> G[create_folders_for_items_in_download_folder]
    A --> H[check_for_duplicate_volumes]
    A --> I[extract_covers]
    A --> J[check_for_existing_series]
    A --> K[rename_dirs_in_download_folder]
    A --> L[send_discord_message]
    A --> M[check_for_missing_volumes]
    A --> N[check_for_new_volumes_on_bookwalker]
    A --> O[Watcher]

    I --> P[find_and_extract_cover]
    I --> Q[process_cover_extraction]
    I --> R[find_series_cover]

    P --> S[get_novel_cover_path]
    P --> T[process_cover_image]
    P --> U[compress_image]

    J --> V[similar]
    J --> W[clean_str]
    J --> X[move_file]
    J --> Y[prep_images_for_similarity]

    H --> Z[is_upgradeable]
    H --> AA[get_keyword_scores]
    H --> BB[remove_file]

    O --> CC[Handler.on_created]
    CC --> DD[is_file_transferred]
    CC --> A
```

## Execution Flow Mapping

### Original main() Function Flow
```python
def main():
    # 1. Global variable initialization
    global komga_libraries, libraries_to_scan
    
    # 2. Download folder detection
    download_folder_in_paths = check_download_folder_presence()
    
    # 3. Cache existing library paths
    cache_existing_library_paths()
    
    # 4. File operations sequence
    delete_unacceptable_files()
    rename_files()
    create_folders_for_items_in_download_folder()
    
    # 5. Duplicate checking
    check_for_duplicate_volumes(download_folders)
    
    # 6. Cover extraction (download folders)
    if paths and download_folder_in_paths:
        extract_covers()
        print_stats()
    
    # 7. Series matching
    if download_folders and paths:
        check_for_existing_series()
    
    # 8. Directory operations
    if download_folders:
        rename_dirs_in_download_folder()
    
    # 9. Discord notifications
    if grouped_notifications and not watchdog_toggle:
        send_discord_message(None, grouped_notifications)
    
    # 10. Cover extraction (library)
    if paths and not download_folder_in_paths:
        # Watchdog or regular execution
        extract_covers()
        print_stats()
    
    # 11. Missing volume checks
    check_for_missing_volumes()
    check_for_new_volumes_on_bookwalker()
    
    # 12. Komga library scanning
    trigger_komga_library_scans()
```

### Modular main.py Flow
The modular version follows the same flow but with explicit imports and function calls from different modules.

## Global Variable Migration Guide

### Variables in config/constants.py
```python
# Original global scope variables that need to be preserved:
script_version = (2, 5, 31)
script_version_text = "v{}.{}.{}".format(*script_version)
paths = []
download_folders = []
paths_with_types = []
download_folders_with_types = []
folder_accessor = None
compress_image_option = False
image_quality = 40
image_count = 0
errors = []
items_changed = []
discord_webhook_url = []
bookwalker_webhook_urls = []
# ... and many more
```

### Variables in config/state.py
```python
# State management variables:
processed_files = set()
processed_series = set()
series_data = {}
books_data = {}
unmatched_books = {}
matched_books = {}
komga_series_data = {}
komga_book_data = {}
bookwalker_data = {}
```

### Global Variable Dependencies
1. **Shared across modules:** `paths`, `download_folders`, `discord_webhook_url`
2. **State tracking:** `processed_files`, `moved_files`, `grouped_notifications`
3. **Configuration:** `compress_image_option`, `image_quality`, `watchdog_toggle`
4. **Caching:** `cached_paths`, `root_modification_times`

## Step-by-Step Recombination Instructions

### Step 1: Create Base File Structure
1. Start with external library imports (consolidated from all modules)
2. Add settings import and wildcard import
3. Include all global variable definitions from `config/constants.py`

### Step 2: Add Utility Functions
1. Copy all functions from `utils/helpers.py`
2. Copy all functions from `utils/similarity.py`
3. Ensure proper function ordering (dependencies first)

### Step 3: Add Data Models
1. Copy all classes from `models/file_models.py`
2. Include any additional model classes from other model files
3. Maintain class definition order

### Step 4: Add File System Operations
1. Copy all functions from `filesystem/file_operations.py`
2. Add functions from other filesystem modules
3. Resolve any internal dependencies

### Step 5: Add Processing Logic
1. Copy functions from `processing/image_processor.py`
2. Copy functions from `processing/metadata_extractor.py`
3. Copy functions from `processing/cover_extractor.py`
4. Copy functions from other processing modules

### Step 6: Add Integration Services
1. Copy all functions from `integrations/discord_client.py`
2. Add other integration modules (komga, bookwalker, qbittorrent)
3. Ensure notification handling is preserved

### Step 7: Add Core Business Logic
1. Copy functions from `core/duplicate_checker.py`
2. Copy functions from `core/upgrade_manager.py`
3. Copy the large `check_for_existing_series()` from `core/series_matcher.py`
4. Copy watchdog classes from `core/watchdog_handler.py`

### Step 8: Add Settings Management
1. Copy functions from `config/settings_manager.py`
2. Ensure argument parsing logic is preserved
3. Include settings validation functions

### Step 9: Add Main Function
1. Copy the main() function from `main.py`
2. Copy the entry point logic (if __name__ == "__main__":)
3. Ensure all function calls reference the now-local functions

### Step 10: Remove Module Imports
1. Remove all `from config.` imports
2. Remove all `from models.` imports
3. Remove all `from utils.` imports
4. Remove all `from filesystem.` imports
5. Remove all `from processing.` imports
6. Remove all `from integrations.` imports
7. Remove all `from core.` imports

## Cross-Reference Table

| Function Name | Original Location (Est.) | Modular Location | Dependencies | Required Imports |
|---------------|-------------------------|------------------|--------------|------------------|
| `main()` | Lines 11800-12000 | `main.py:41-171` | All major functions | All module functions |
| `parse_my_args()` | Lines 1300-1400 | `config/settings_manager.py:231-558` | `argparse`, `process_path()` | `argparse` |
| `extract_covers()` | Lines 8800-9500 | `processing/cover_extractor.py:43-297` | File operations, image processing | `os`, `scandir` |
| `find_and_extract_cover()` | Lines 8450-8650 | `processing/cover_extractor.py:315-467` | Image processing, zip operations | `zipfile`, `re` |
| `check_for_existing_series()` | Lines 4000-7500 | `core/series_matcher.py:74-1045` | File operations, similarity checking | `os`, `re`, `time` |
| `check_for_duplicate_volumes()` | Lines 9600-10200 | `core/duplicate_checker.py:96-367` | File operations, ranking system | `os`, `scandir` |
| `send_discord_message()` | Lines 3500-3800 | `integrations/discord_client.py:94-154` | Discord webhook | `discord_webhook` |
| `compress_image()` | Lines 11200-11300 | `processing/image_processor.py:12-55` | PIL operations | `PIL.Image`, `io` |
| `clean_str()` | Lines 1800-2000 | `utils/helpers.py:327-373` | String manipulation utilities | `re`, `unidecode` |
| `similar()` | Lines 2200-2250 | `utils/similarity.py:7-18` | String comparison | `difflib.SequenceMatcher` |

## Module Integration Verification

### Verification Checklist

#### 1. Import Verification
- [ ] All external library imports are present
- [ ] No internal module imports remain
- [ ] Settings import is functional
- [ ] All required packages are available

#### 2. Function Availability
- [ ] All functions called in main() are defined
- [ ] Function dependencies are resolved
- [ ] No circular dependencies exist
- [ ] Global variables are accessible

#### 3. Configuration Verification
- [ ] All constants from `config/constants.py` are present
- [ ] Settings validation works correctly
- [ ] Command-line arguments parse successfully
- [ ] Path processing functions correctly

#### 4. State Management
- [ ] Global variables maintain their state
- [ ] Cached data structures work correctly
- [ ] File tracking variables function properly
- [ ] Notification grouping operates correctly

#### 5. Functional Testing
- [ ] Cover extraction works on test files
- [ ] Discord notifications send correctly
- [ ] File operations complete successfully
- [ ] Series matching functions properly
- [ ] Duplicate detection operates correctly

### Testing Commands
```bash
# Test basic functionality
python3 komga_cover_extractor.py -p "/test/path" -wh "test_webhook"

# Test with compression
python3 komga_cover_extractor.py -p "/test/path" -c "True" -cq "40"

# Test watchdog mode
python3 komga_cover_extractor.py -df "/test/download" -wd "True"
```

### Validation Steps

#### Step 1: Syntax Validation
```bash
python3 -m py_compile komga_cover_extractor.py
```

#### Step 2: Import Testing
```python
# Test critical imports
try:
    import komga_cover_extractor
    print("✓ File imports successfully")
except ImportError as e:
    print(f"✗ Import error: {e}")
```

#### Step 3: Function Existence Check
```python
import komga_cover_extractor as kce
required_functions = [
    'main', 'parse_my_args', 'extract_covers', 
    'find_and_extract_cover', 'check_for_existing_series',
    'check_for_duplicate_volumes', 'send_discord_message'
]

for func in required_functions:
    if hasattr(kce, func):
        print(f"✓ {func} is available")
    else:
        print(f"✗ {func} is missing")
```

#### Step 4: Global Variable Check
```python
import komga_cover_extractor as kce
required_globals = [
    'paths', 'download_folders', 'discord_webhook_url',
    'image_count', 'errors', 'items_changed'
]

for var in required_globals:
    if hasattr(kce, var):
        print(f"✓ {var} is available")
    else:
        print(f"✗ {var} is missing")
```

### Performance Considerations

1. **File Size:** The recombined file will be approximately 12,000+ lines
2. **Memory Usage:** All functions loaded at once vs. modular loading
3. **Startup Time:** Single file parsing vs. module imports
4. **Debugging:** Harder to debug a monolithic file vs. modular structure
5. **Maintenance:** More difficult to maintain a single large file

### Rollback Plan

If recombination fails:
1. Keep the modular structure as primary
2. Create a compatibility wrapper
3. Use the modular main.py as the entry point
4. Gradually fix any integration issues

---

## Conclusion

This comprehensive guide provides all the necessary information to successfully recombine the modularized `komga_cover_extractor.py` back into its original monolithic structure while maintaining 100% functional equivalence. The modular structure offers better maintainability, but this guide ensures that the option to revert to a single file remains viable.

**Critical Success Factors:**
1. Maintain exact function signatures
2. Preserve all global variable states
3. Keep the same execution order
4. Ensure all dependencies are resolved
5. Test thoroughly before deployment

**Estimated Recombination Time:** 2-4 hours with careful attention to detail and proper testing.