# Refactoring Plan for komga_cover_extractor

This document outlines the plan to refactor the `komga_cover_extractor.py` script into smaller, more manageable modules, using `main.py` as the new entry point. The original `komga_cover_extractor.py` will be left untouched initially.

## Rationale

The current script (`komga_cover_extractor.py`) has grown large and handles multiple responsibilities. Refactoring will improve maintainability, readability, testability, and overall code organization by separating concerns into distinct modules.

## Revised Proposed File Structure

Based on initial refactoring attempts, the `utils.py` module became too large. This revised structure breaks utilities down further:

```
komga_cover_extractor/
├── main.py                   # NEW Entry point, arg parsing, setup, main loop/watchdog trigger
├── komga_cover_extractor.py  # Original script (to be kept initially, eventually deprecated/removed)
├── config.py                 # Global variables, constants, regex patterns, file extensions
├── models.py                 # Class definitions (Volume, File, Path, LibraryType, etc.)
├── file_utils.py             # File/directory operations (listing, moving, removing, checking existence/size/transfer, path manipulation)
├── string_utils.py           # String cleaning, normalization, comparison, regex helpers, name/number/part extraction
├── log_utils.py              # Logging and console message functions (send_message, write_to_file)
├── misc_utils.py             # Other utilities (execute_command, get_input_from_user, timing, etc.)
├── image_utils.py            # Image processing functions (compression, similarity, cover extraction helpers)
├── archive_utils.py          # Archive handling (zip, rar, 7z, cbz conversion)
├── metadata_utils.py         # Metadata extraction and parsing (EPUB, ComicInfo)
├── discord_utils.py          # Discord integration functions
├── komga_utils.py            # Komga API functions
├── bookwalker_utils.py       # Bookwalker scraping functions
├── core_logic.py             # Core business logic orchestration (renaming, duplicate checks, series matching, cover extraction workflow)
└── watchdog_handler.py       # Watcher and Handler classes for file system monitoring
```

## Revised Module Breakdown

*   **`main.py`**: Handles argument parsing (`parse_my_args`), initial setup, and decides whether to run `core_logic.main()` directly or start the `watchdog_handler.Watcher`.
*   **`config.py`**: Centralizes configuration, constants, regexes, global state.
*   **`models.py`**: Contains all custom class definitions.
*   **`file_utils.py`**: Focuses on file system interactions (CRUD operations, path checks, directory walking).
*   **`string_utils.py`**: Focuses on text manipulation, cleaning, comparison, and extraction of specific parts like volume numbers, series names, subtitles, extras based on patterns.
*   **`log_utils.py`**: Handles logging to files and console output via `send_message`.
*   **`misc_utils.py`**: Contains miscellaneous helper functions like command execution, user input, timing functions, web scraping session management.
*   **`image_utils.py`**: Image-specific operations.
*   **`archive_utils.py`**: Archive-specific operations.
*   **`metadata_utils.py`**: Metadata-specific operations.
*   **`discord_utils.py`**, **`komga_utils.py`**, **`bookwalker_utils.py`**: External API/service interactions.
*   **`core_logic.py`**: Orchestrates the overall workflow by calling functions from various utility and API modules. Contains the main business logic steps.
*   **`watchdog_handler.py`**: File system event monitoring.

## Revised Dependency Diagram (Conceptual)

```mermaid
graph TD
    A[main.py] --> B(config.py);
    A --> C(models.py);
    A --> D(core_logic.py);
    A --> E(watchdog_handler.py);
    A --> F(log_utils.py); # For initial messages/errors
    A --> G(argparse);
    A --> H(komga_utils.py); # For arg parsing library check

    D --> B;
    D --> C;
    D --> F;
    D --> I(file_utils.py);
    D --> J(string_utils.py);
    D --> K(misc_utils.py);
    D --> L(image_utils.py);
    D --> M(archive_utils.py);
    D --> N(metadata_utils.py);
    D --> O(discord_utils.py);
    D --> H;
    D --> P(bookwalker_utils.py);

    E --> D; # Watchdog triggers core logic
    E --> B;
    E --> F;
    E --> I;
    E --> K; # For timing?
    E --> O; # For notifications

    I --> F; # File utils might log errors
    J --> F; # String utils might log errors
    K --> F; # Misc utils might log errors
    L --> F; # Image utils might log errors
    M --> F; # Metadata utils might log errors
    O --> F; # Discord utils might log errors
    H --> F; # Komga utils might log errors
    P --> F; # Bookwalker utils might log errors

    L --> B; L --> I; # Image utils use config, file utils
    M --> B; M --> I; M --> L; M --> J; # Metadata uses config, files, archives, strings
    O --> B; # Discord uses config
    H --> B; # Komga uses config
    P --> B; P --> K; P --> J; # Bookwalker uses config, web (misc), strings

    I --> B; # File utils use config
    J --> B; # String utils use config
    K --> B; # Misc utils use config

    C --> B; # Models might use config defaults? (Less likely now)
```

## Lessons Learned from Initial Attempt

1.  **Overly Large Utility Module (`utils.py`)**: Grouping too many unrelated functions (file I/O, string manipulation, logging, web requests, etc.) into a single `utils.py` made it unwieldy and violated the single-responsibility principle. The revised plan creates more specific utility modules (`file_utils.py`, `string_utils.py`, `log_utils.py`, `misc_utils.py`).
2.  **`apply_diff` Tool Unreliability for Large Changes**: Attempting to move large blocks of code and fix numerous import errors simultaneously using `apply_diff` resulted in repeated failures, syntax errors, and inconsistent file states.
3.  **Need for Smaller, Incremental Steps**: Refactoring should proceed by moving smaller, related groups of functions/classes at a time, followed by verification (e.g., running static analysis or tests if available).
4.  **Careful Import Management**: When moving code, imports in *both* the source and destination files, as well as any files *using* the moved code, must be meticulously updated. Using `write_to_file` requires providing the *complete* final content, including all necessary imports.
5.  **Use `write_to_file` for Major Restructuring**: For creating new files or significantly rewriting existing ones during refactoring, `write_to_file` is more reliable than `apply_diff`, provided the *entire* correct content (including imports) is supplied.

## Next Steps (Revised)

1.  **Create New Files**: Create the empty files outlined in the *Revised Proposed File Structure*.
2.  **Move Code Incrementally**:
    *   Start with `config.py` (already done).
    *   Move classes to `models.py` (already done).
    *   Move logging/messaging (`send_message`, `write_to_file`) to `log_utils.py`. Update dependents.
    *   Move file system functions to `file_utils.py`. Update dependents.
    *   Move string functions to `string_utils.py`. Update dependents.
    *   Move remaining utils to `misc_utils.py`. Update dependents.
    *   Move image functions to `image_utils.py` (already done). Update dependents.
    *   Move archive functions to `archive_utils.py` (already done). Update dependents.
    *   Move metadata functions to `metadata_utils.py` (already done). Update dependents.
    *   Move Discord functions to `discord_utils.py` (already done). Update dependents.
    *   Move Komga functions to `komga_utils.py` (already done). Update dependents.
    *   Move Bookwalker functions to `bookwalker_utils.py` (already done). Update dependents.
    *   Move Watchdog classes to `watchdog_handler.py` (already done). Update dependents.
    *   Move core logic orchestration functions (`main`, `extract_covers`, `check_for_existing_series`, etc.) to `core_logic.py`. Update dependents.
    *   Create `main.py` with argument parsing and entry point logic (partially done).
3.  **Verify Imports**: After each significant move, carefully check and correct import statements across all modified files.
