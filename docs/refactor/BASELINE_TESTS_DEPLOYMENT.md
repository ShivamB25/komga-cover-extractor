## Evidence boundary
All findings below are static source observations, not passing/failing runtime claims. No files changed. Complete primary source was read; Codegraph omitted requested function bodies even for an exact-name query, so targeted raw reads filled the gaps; that omission was reported through report_issue. Parent owns dependency/runtime decisions and dataset/artwork metadata computations.

## Tests: inventory and every asserted behavior
There are 39 defined test_* functions: 37 contain assertions; test_rename_files and test_sorting_volumes_by_volume_number do not. validate_csv is an additional assertion-bearing helper, not named test_*. The direct runner calls 35 test_* functions plus validate_csv (tests.py:1971-2012). Do not equate its ALL TESTS PASSED message with complete coverage.

| Original function / anchor | Contract actually asserted |
|---|---|
| test_set_num_as_float_or_int :60-72 | Numeric integer/whole-float/fraction preservation, numeric string conversion, numeric/string lists rendered as hyphen-joined strings. Assertions for numeric range strings and mixed decimal list are commented out. Equality to 1 does not assert Python type separately. |
| test_remove_hidden_files :76-81 | Dot-prefixed names removed, visible order retained, singleton/empty inputs. |
| test_remove_ignored_folders :85-86 | isekai retained and test removed; implicitly depends on ignored_folder_names configuration. |
| test_remove_hidden_folders :90-95 | Same dot-name filtering contract for folders, including empty input. |
| test_filter_non_chapters :107-111 | c01 excluded; v01 and year-only book retained, mixed input filtered. |
| test_contains_chapter_keywords :115-151 | Explicit c042, bare 042, separator/underscore variants accepted; v042 and volume metadata containing c1fi7 rejected. |
| test_contains_volume_keywords :156-239 | LN, Light Novel, Novel, Book, Volume, Vols, Vol, V, 第, Disc across compact/space/dot/dot-space and scalar/range variants accepted; bare numbers/ranges and decimal chapter rejected. |
| test_get_file_extension :243-248 | Last suffix including dot, empty extension for no dot, trailing-dot suffix '.', multiple suffixes. |
| test_get_extensionless_name :252-257 | Removes only final suffix; preserves earlier dots; removes trailing dot. |
| test_is_volume_one :263-353 | Volume/chapter keyword variants, spacing/dots, range/set starting at 1, bare 01 forms and spelled Volume One accepted; year-only and chapter 100.4 rejected. |
| test_get_series_name_from_chapter :357-490 | Chapter/Chap/Ch/C and bare numeric scalar/range/set suffix stripping with separator variants; season epilogue 30x1, repeated chapter label, numeric title prefix 15 Minutes, apostrophe title, decimal title/chapter examples preserved. CRITICAL call mismatch: tests pass number as second positional argument, but current function second argument is root (main:2779); chapter_number remains ''. This is source drift, not a proven failure. |
| test_get_folder_type :494-514 | Extension share percentages 100/50/25. Commented file_type tests pass strings even though current implementation expects objects with .file_type (main:2857-2877). |
| test_check_for_multi_volume_file :520-633 | Scalar-vs-multiple recognition across English/Japanese keyword and punctuation variants; chapter=True ranges and explicit multi-number sets; decimal chapter alone is not multi; bare 199-200 chapter is multi. |
| test_convert_list_of_numbers_to_array :637-640 | Despite its name invokes get_min_and_max_numbers; contiguous hyphen lists collapse to endpoints [1,5], [1,6], [1,7]. |
| test_get_release_number_cache :644-969 | Scalar 1, range [1,2], explicit list collapsed to extrema [1,3] across same variant families; chapter 100.4 retained; #1/x1 suffix excluded from number; artbook year span produces empty string; title containing 2.5 still yields [199,200]. |
| test_get_release_year :973-977 | Bracketed 2022 extracted as string, including no extension; unbracketed/no year returns None. |
| test_get_extra_from_group :981-992 | Whitelisted danke-Empire/1r0n returned, nonmatch empty. Defined with assertions but runner call commented at :1990. |
| test_get_file_part :996-1004 | No part gives empty string; bracketed parts 1/2/3 and chapter x2/x3/x4/#5 parsed numerically. |
| test_get_keyword_score :1008-1021 | Three matching rank keywords yield total_score 3. Defined but runner call commented :1992; explicit Keyword construction/config-sensitive dependency. |
| test_remove_dual_space :1025-1026 | Double space collapses. |
| test_normalize_str :1030-1031 | Leading The removed while title case retained. |
| test_clean_str :1035-1036 | Leading article and punctuation removed and text lowercased. |
| test_array_to_string :1054-1058 | Comma/space/hyphen/empty separators render two strings as specified. |
| test_remove_duplicates :1062-1065 | Deduplication retains first occurrence order, including nonadjacent duplicates. |
| test_remove_brackets :1069-1092 | Plain trailing bracketed title text can remain; metadata brackets adjacent to extensions removed; embedded title parentheses, nested OSHI NO KO title and meaningful parenthetical subtitles preserved; year/publisher/group metadata removed. |
| test_replace_underscores :1096-1098 | Underscores become spaces, NOT periods. |
| test_get_extras :1117-1149 | Chapter/volume bracket extras retained in order, duplicate Digital removed, Premium prioritized before Digital/group, decimal chapter without extras yields empty list. |
| test_isfloat :1153-1156 | Numeric integer/decimal strings accepted, multi-dot rejected. |
| test_isint :1160-1163 | Integer string accepted, decimal/multi-dot rejected. |
| test_parse_html_tags :1167-1174 | Paragraph text mapped to tag; repeated dictionary keys mean last p value wins. Source literals with duplicate 'p' keys do NOT assert multiple retained paragraph entries. |
| test_check_for_exception_keywords :1178-1196 | One-shot match case-insensitive; unrelated exception fails. |
| test_has_one_set_of_numbers :1200-1203 | Scalar and hyphen range count as one set, space-separated distinct numbers do not. |
| test_has_multiple_numbers :1207-1212 | Scalar/range false, distinct space-separated numbers true, repeated identical numbers false. Defined but completely omitted from __main__ runner. |
| test_organize_by_first_letter :1406-1561 | Reorders matching initial to insertion positions 1 or 2; exclusions prevent moves; no-match/empty string remain unchanged; singleton/two/three-item lists covered. |
| test_contains_unicode :1944-1948 | ASCII false, accented characters (single/repeated/uppercase-title) true. |
| test_contains_punctuation :1952-1958 | ASCII word false, !/comma true, accented alphabetic title false. |
| test_contains_brackets :1962-1968 | Any tested parenthesis/square/brace combination true, plain word false. |
| validate_csv :1572-1940 | Zero mismatches required for normalized release number, exact series name, inferred file type on non-skipped rows; see corpus semantics below. |

### Disabled and manual material
- Completely commented test_remove_unaccepted_file_types :98-103 has four extension accept/reject assertions and notes real files needed for isfile. It passes ['cbz']/['epub'] without leading dots, whereas actual helper compares dotted get_file_extension values (main:1861-1867). Merely supplying fixtures does not fix this mismatch; preserve original artifact and document adaptation of extension arguments.
- Completely commented test_convert_to_ascii :1039-1050 has ten multilingual identity/accent transliteration examples; commented runner reference :1996 does not define the function.
- Commented assertions in numeric conversion :69-72 and folder-type :503-514 need explicit dormant status, not counted as current executable coverage.
- test_rename_files :1102-1113 overwrites the test module's download_folders with /dl/Manga & Novels, reads LOGS_DIR/test_file_list.txt, then calls rename_files(..., download_folders=..., test_mode=True). It asserts no output; runner call commented :1973. The file and media root are not supplied by checkout. test_mode is not a sandbox.
- test_sorting_volumes_by_volume_number :1215-1403 constructs local Volume objects containing scalar and list numbers, calls sorted(..., key=get_sort_key), discards result, then pass. No ordering expectation exists to preserve. The local Volume :7-56 shadows imported Volume and is only used for this exercise.

## Import-time boundary and safe characterization design
- tests.py:4 wildcard-imports root executable; csv is its only explicit standard-library import, so even os/ROOT_DIR/LOGS_DIR/Keyword and helper names come from main's exported namespace.
- MainAudit's independently read anchors: main:27-43 imports heavy/native/archive/image/network/watch libraries; :46-47 executes settings.py via imports; :128-138 probes asset files; :195-199 introspects settings with dir/getattr; :1789 constructs DiscordWebhook(url=None); :3569-3571 compiles settings-driven ranking regex. Decorators and defaults capture module state throughout. Main DOES NOT parse CLI or run full pipeline during import: final guard :12234-12251 owns startup. These distinctions must be kept precise.
- Proposal: preserve the complete original assertions and literal corpus separately from a safe harness. Import only extracted, explicit pure APIs with reviewed regex/constants/default policy, not root executable or dynamic settings. Definitions-only AST extraction is insufficient because constants/compiled patterns/defaults are required. Configure ignored_folder_names deterministically (include test); preserve regex engine and captured baseline policy until characterized. Reset relevant caches between differing policies.
- Correct the chapter helper binding explicitly to name, fixture-root, chapter_number=number, preserving each original expected series output; record this as harness repair, not evidence original call worked. Corpus already passes the three positional arguments correctly (:1891-1895).
- Use temporary directories for any actual isfile-dependent cases, create harmless empty cbz/epub files for extension filtering (no archive parser needed), set dotted extensions with documented repair, and keep fixtures outside user roots. Legacy rename smoke exercise must remain excluded until source effects are adapted/injected: replace /dl and logs path with fixture-only roots, provide a small filename-list fixture, and define real output assertions only from reviewed behavior. No missing real-media fixture should be fabricated as authentic.
- No sorting assertion exists; do not reclassify no-exception behavior as ordering coverage. Replace only after selecting explicit observable numeric ordering contract, with original no-assertion disposition retained.
- A future collector must explicitly include validate_csv; importing or pytest-collecting unchanged tests.py would still import unsafe main, can expose formerly disabled test functions, and the current helper is not automatically a test by its name. Direct Python -O can strip assertions, so manual success text is not a reliable gate.

## Corpus: input versus expected output
Header (manga_novel_dataset.csv:1): File Name, Release Number, File Type, Series Name, Release Year, Release Part, Index Number, Release Group, Publisher, Subtitle, Extras.
- File Name is raw parser input. Only Release Number, File Type and Series Name are expected outputs actually validated. Release Year/Part, Index Number, Release Group, Publisher, Subtitle, Extras are NOT consumed by validate_csv; do not assert corpus coverage of those fields or treat observed blanks as tested expectations.
- tests.py:1574 locates CSV under imported ROOT_DIR. :1576-1578 silently prints and returns on missing CSV; this permits later ALL TESTS PASSED without corpus coverage. Safe harness should make absence an explicit failure/blocked prerequisite.
- :1583-1833 is exact-filename skip list: skipped rows bypass all three validations. Preserve names and label known exclusions; do not claim those cases passing.
- :1836-1842 uses standard csv.DictReader UTF-8/newline='', materializes all rows. Comment about pandas is stale; pandas is not used here.
- :1845-1862 infers chapter only when no volume keywords AND chapter keywords; otherwise volume. Expected File Type is not used to choose parser, avoiding that form of oracle leakage.
- :1865-1877 expected bracket-prefixed release strings lose brackets, are collapsed through get_min_and_max_numbers and set_num_as_float_or_int. :1880-1888 similarly normalizes parsed result; falsy parsed number plus is_one_shot(skip_folder_check=True) becomes 1. This is compatibility normalization, not raw CSV equality, and risks conflating zero with absence.
- :1891-1895 obtains chapter series with ROOT_DIR + parsed release number, or volume series with test_mode=True. :1898-1914 collects mismatches; :1917-1934 prints details; :1936-1940 asserts all three mismatch lists empty.
- Missing metadata input for this report: exact parsed row count, per-column population/type distribution, malformed-width/duplicate counts, matched/unmatched skip counts and hashes. Requested parent compute them with static stdlib-only tooling; no CSV corpus dump or test execution needed. No row-count claim is made here.

## README and CLI consumers
- README:13 advertises zip/cbz/epub high-resolution cover extraction, while :53-57 labels a comprehensive manager/modular regexes/docs as future/in-progress; advertisement is narrower than actual monolith/addon scope.
- :19-20 recommends Docker Hub image. :24-29 manual clone/pip requirements; unrar optional for advanced RAR; macOS brew rar and Windows UnRAR.dll explicitly untested. No Python version/support matrix promised beyond python3 invocation; Docker specifies 3.13.8, which is a build input, not verified support evidence.
- :32-39 documents root script, -p/--paths, -wh/--webhook, -c/--compress, -cq/--compress_quality; webhook synopsis looks required but bullets call it optional. :45 uses repeated -p, True, quality 40; :50 labels no-compress flags as Without Compression. These are concrete consumers to preserve and compare with MainAudit effective CLI behavior; no command was run.
- :62 links absent CONTRIBUTING.md. No startup shell script, compose file or healthcheck document exists in inventoried surface; Docker contains launcher inline.

## Docker: retain useful practices, distinguish advertised knobs
- Dockerfile:2 pins Python patch/base distro tag python:3.13.8-slim-bookworm (not digest). :5 fixed /app working directory; :8 creates appuser; :11-12 PUID/PGID are ARG build-time values, not runtime identity mapping; :18 applies ids; :25 COPY --chown; :65 USER appuser. Retain non-root runtime/ownership intent and make build-vs-run knobs explicit.
- :15 unbuffered logging useful. :21-22 sets ENV UMASK=022 then RUN umask "$UMASK" in a build shell; no runtime launcher applies it. Do not advertise this as effective runtime umask control.
- :28 enables Debian non-free; :31-36 installs unrar/tzdata/nano/build-essential, pip --no-cache-dir root AND qBittorrent requirements. Preserve native RAR capability and explicit helper dependency installation, subject to parent dependency audit.
- :39-51 MANGA_ISBN true branch downloads/runs Calibre installer, installs browser/Qt/OCR/ICU stack, then references absent addons/manga_isbn/chrome/google-chrome-stable_current_amd64.deb, requirements.txt, python-anilist-1.0.9/. . Branch false does not execute those commands; enabled branch cannot be fulfilled from checkout. No remote script executed here.
- :54-58 EPUB_CONVERTER true installs zip then references absent addons/epub_converter/requirements.txt. These names are build-time knobs referring to unavailable implementations, not working checkout capabilities. addons directory inspection confirms only qbit_torrent_unchecker exists; broader glob also found no optional dirs or launchers.
- :61-62 autoremove and apt-list cleanup are useful existing housekeeping, though separate RUN layers do not reclaim earlier layer bytes. Root application dependencies and optional compiler/toolchain footprint remain parent dependency concerns.
- :68 shell CMD runs qBittorrent in background with both streams discarded (/dev/null), then root main foreground. There is no helper supervisor/health state/wait/trap or explicit signal forwarding. Helper failure need not fail foreground main; diagnostic suppression is actual behavior. Keep helper lifecycle in scope, don't drop it.
- Exact runtime env consumers at :68: PATHS, DOWNLOAD_FOLDERS go to BOTH helper and main; main additionally receives WEBHOOK, BOOKWALKER_CHECK, COMPRESS, COMPRESS_QUALITY, BOOKWALKER_WEBHOOK_URLS, WATCHDOG, WATCHDOG_DISCOVER_NEW_FILES_CHECK_INTERVAL, WATCHDOG_FILE_TRANSFERRED_CHECK_INTERVAL, OUTPUT_COVERS_AS_WEBP, NEW_VOLUME_WEBHOOK. All flags are supplied even when env empty. Parent should record actual empty-string/default behavior against main CLI, not assume unspecified equals absent. PATHS/DOWNLOAD_FOLDERS are quoted single option values; repeated-path/delimiter compatibility belongs to parent contract.
- Firecrawl Developer Index returned official Docker primary source https://www.docker.com/blog/docker-best-practices-choosing-between-run-cmd-and-entrypoint/ explaining shell form typically introduces /bin/sh -c as PID 1 and exec form gives direct signal delivery. This supports lifecycle risk, not a tested shutdown failure on this image.
- .dockerignore:2-14 excludes tests/README/READMETEMPLATE, venv/misc, dotfiles and underscore prefixes. Useful: hidden secrets/Git/codegraph exclusions and no tests in runtime image. Missing: explicit non-hidden handoff/prompt/corpus/config/log exclusions. COPY . . can include local non-hidden handoff/reference material and configured settings.py; do not claim secret-free image merely because dotfiles excluded.

## CI inventory
- .github/workflows/github.yaml:3-7 triggers push main plus workflow_dispatch, whereas baseline branch master and Docker Hub workflow uses master. :14 checkout@main floating ref; :17 login-action@v1; :18-21 GHCR with actor/GITHUB_TOKEN; :23-25 shell docker build/push hardcoded ghcr.io/zachstultz/komga-cover-extractor:latest. No test/lint/type/package gates, explicit token permissions or concurrency policy.
- docker-hub.yml:3-7 triggers BOTH master pushes and pull requests. :9-13 registry/image derived from GitHub repository. :16-23 standalone build job. :25-53 independent push_to_registry has no needs: build and no event guard, logs in using secrets, configures Buildx, derives tags/labels and build-push push:true. A failing build job is not a dependency gate on publication; PR events also schedule publish job (secrets availability may prevent success, but no runtime claim).
- Worth retaining: PR image build feedback, Buildx setup, dynamic registry/repository naming, metadata-derived tags/labels, secret-based credentials, SHA-pinned login and metadata actions (:36, :43), both registry consumer paths if supported. Missing application gates are genuine, but image build CI is NOT absent. Other actions remain version tags or moving branch. No Python/OS matrix or optional-addon build matrix.

## Assets/license/missing-input ledger
- blank_white.jpg and blank_black.png present at root. Main:128-138 resolves file paths only if existent; :8666-8670 compares white/black; :8730-8732 requires both for blank checking. Keep runtime resource availability/location contract during packaging. Binary metadata/hashes requested from parent; not decoded or executed here.
- LICENSE.md:1-21 MIT, copyright 2025 Zach Stultz, retain notice/permission in substantial copies. README:64-68 agrees MIT. No separate artwork provenance notice was found in allowed source; don't invent author/provenance from filenames.
- Absent references: CONTRIBUTING.md; logs/test_file_list.txt; addons/manga_isbn including browser deb/requirements/anilist vendored source; addons/epub_converter/requirements.txt. No supplied real /dl/Manga & Novels fixture. No runtime verification, wheel/container execution, CI results, asset metadata calculation or full corpus statistics claimed.


## Parent-computed corpus and artwork metadata

44,714 rows: 28,051 chapter, 16,663 volume. 46 Series Name labels blank; seven trailing metadata columns entirely unpopulated. Image headers: blank_black.png192x144, blank_white.jpg2160x3072. Hashes in source-manifest.json. Header inspection is not image decode verification.
