# Main-production WP00 audit

## Evidence and scope

All anchors below are `komga_cover_extractor.py:<line>` unless a different path is named. Full production source was read across bounded contiguous sections, including the clipped 5459–5815 section. No original application/settings/tests/add-on was imported or executed; no runtime correctness claim is made. Settings internals, add-on internals and test/deployment internals remain peer-owned.

Baseline is master `ceb171b7e6aa0a073bc5e0b8f07a0ee8cdcbd879`. Parent's static byte comparison reports checkout has exactly one terminal LF absent from upload. `docs/refactor/source-manifest.json` records checkout 470796 bytes, SHA256 `00e507f11af2b6078f3c374ac0be95908ca44d89d5b93b77c5e299a84208c23e`; handoff baseline records upload 470795 bytes, SHA256 `87610a4c83f7e1244b647e9763cb9a5f875862f3fe768efeefc838f9a80f1c9e`. Source line anchors therefore align. Parent independently counted 163 top-level functions/18 classes. No semantic checkout/upload divergence was found by that comparison.

Codegraph was called first for indexed source and again for named entry points/pipeline dependencies. Its capped answers supplied blast-radius evidence but omitted much requested source, so targeted reads filled all gaps. It confirms main/add-on callers of `check_for_existing_series`. Firecrawl Developer Index search returned the upstream README/repository but only a short cover-extraction description, not evidence establishing all legacy behavior: https://github.com/zachstultz/komga-cover-extractor/blob/master/README.md and https://github.com/zachstultz/komga-cover-extractor . Static checkout is authoritative for the following claims; no library-version/security claim is inferred from those search results.

## 1. Import-time execution: exact safe and unsafe boundaries

### Statements executed by ordinary import

- 2–25: standard-library imports. These do not call this application's pipeline; they still need a controlled importer/environment in a future harness.
- 27–43: unconditional heavyweight/native/optional third-party imports: cv2, filetype, numpy, py7zr, rarfile, regex, requests, scandir, BeautifulSoup, DiscordEmbed/DiscordWebhook, lxml.etree, Pillow, skimage SSIM, titlecase, unidecode, watchdog event/observer classes. A parser-only consumer currently requires these imports to succeed. The `regex as re` import at 32 replaces stdlib `re` from 7; preserving third-party regex behavior is essential.
- 46–47: `import settings as settings_file; from settings import *` executes arbitrary local settings Python and exports its namespace. Do not treat this as data parsing or presume personal settings are harmless.
- 50–119: version/state/root assignments, including `ROOT_DIR = dirname(abspath(__file__))` at 110, log/add-on paths at 113/116; many settings names are immediately shadowed by local defaults.
- 123–125: `/app` pathname inference changes Docker flag/version string; no container detection beyond that equality.
- 128–138: `os.path.isfile` probes for the two bundled blank images. These are actual filesystem reads at import, not image decoding.
- 195–199: `dir(settings_file)` and repeated `getattr`/callable checks collect settings names. Dynamic module attribute hooks are another reason not to evaluate untrusted settings.
- 222–247: `LibraryType(...)` instances are constructed after reviewed class definition 206–218. Constructor only assigns fields; this is safe in-memory policy construction, not service initialization.
- 329–375 and 512–514, then 1895–1899, 1957–1964, 2012–2022, 2981–2992, 3281–3283, 3332–3342, 3569–3571, 4849, 4977: assembled/compiled regex globals. These must survive characterization extraction. Ranked pattern compilation at 3569–3571 depends on settings objects and can fail import for malformed expressions.
- 649–653, 713, 2080, 2166, 4240, 6217–6220, 7232, 7838, 8619, 8808, 10779, 11738: definition-time captures, detailed below. Decorators create caches throughout the module; definitions are not inert if defaults/decorators/bases have unresolved or effectful dependencies.
- 1789: `webhook_obj = DiscordWebhook(url=None)` constructs a concrete client at import. There is no explicit `.execute()` here; network transmission by this application occurs inside `send_discord_message`, not at this line. Constructor-internal behavior was not independently verified.
- 1767, 3269/3272 vicinity, 5689, 8799, 9641 and remaining plain literal/global assignments initialize state/constants. `session_objects = {}` does NOT open a Requests session; sessions are created lazily in `get_session_object` 9645–9656.

### What does NOT happen on ordinary main-module import

`parse_my_args`, settings check, watcher construction/run and main invocation are all inside `if __name__ == '__main__'` at 12234–12251. There is no top-level CLI parse, library traversal, cwd change, media mutation, session request, observer start, or log-directory creation outside definitions in this file. Do not confuse main with the add-on's different import lifecycle.

### Safe declaration loading for WP01

An explicit reviewed AST/declaration allowlist can preserve source order, module globals, aliases, compiled patterns, models, function defaults and cache decorators while excluding settings imports 46–47, resource probes 128–138, settings introspection 195–199, concrete webhook construction 1789 and the guard 12234–12251. Do not merely keep FunctionDef/ClassDef nodes: that loses regex globals, extension/keyword order, classes used by constant construction and default bindings.

Provide trusted settings literals and reviewed Keyword-shaped records before dependent declarations; do not evaluate arbitrary settings expressions. Supply sandbox-root-derived `__file__`, ROOT_DIR/LOGS_DIR, explicit blank-resource paths, and controlled settings namespace. Keep real third-party regex, SequenceMatcher, unidecode/titlecase semantics where needed. Heavy effects may be injected as fail-closed dependencies or explicit recorders for a narrowly declared seam; never represent fake image/network behavior as original integration verification. All required declaration-time bases/defaults must be present before definitions execute. Preserve originals' cache decorators and object identity initially; reset by creating a fresh module per scenario, not by hoping all mutable globals can be reset correctly.

Parent reports a working `bwrap --unshare-net` capability. Future WP01 can additionally enforce an OS-level deny-network namespace, read-only source/interpreter mounts, a disposable writable temp/log root, restricted HOME/cwd, no configured secrets/media mounts, and process/time/resource limits. The declaration allowlist addresses initialization semantics; an OS sandbox addresses accidental reachable effects. Neither `test_mode` nor monkeypatching alone is a sandbox.

## 2. Captured defaults and effective configuration

### Every settings-derived definition default in main

| Definition | Captured input | Baseline value supplied by settings peer |
|---|---|---|
| `send_message` 709–727 | `log=log_to_file` at 713 | False (`settings.py:149`) |
| `clean_and_sort` 2075–2158 | `chapters=chapter_support_toggle` at 2080 | False (`settings.py:174`) |
| `process_files_and_folders` 2161–2195 | same at 2166 | False |
| `write_to_file` 4233–4284 | `can_write_log=log_to_file` at 4240 | False |
| `find_and_extract_cover` 8615–8767 | `blank_image_check=compare_detected_cover_to_blank_images` at 8619 | False (`settings.py:193`) |

These captures precede CLI parsing. `--log_to_file True` changes the global at 1604–1607 but does not update either logging default; some explicit runtime guards see the new value while actual default-bound writes still see False. Conversely imported True could continue logging after a CLI False. `write_to_file` creates LOGS_DIR at 4245–4251 BEFORE `if can_write_log` at 4253, so even a disabled write can create the directory.

Other captured shared-object defaults: `Path.__init__` 649–653 captures file_formats, file_extensions, library_types, translation_source_types, source_languages; `check_for_existing_series` 6215–7207 captures paths/downloads/typed roots/cache lists; `rename_dirs_in_download_folder` 7232 and `rename_files` 7837–8503 capture downloads; `extract_covers` 8808 captures paths; `cache_existing_library_paths` 10778–10809 captures three root/cache lists; `move_series_to_correct_library` 11738 captures typed paths. Rebinding a global does not rebind these defaults. CLI appending to the original lists generally makes the CLI path work; test/add-on rebinding can diverge. Ten top-level functions also have literal mutable defaults (handoff baseline list), and their identity must not be casually changed during characterization.

Settings-dependent import expressions beyond defaults: `compiled_searches` 3569–3571 consumes `ranked_keywords` (peer baseline [] at settings:141); `Keyword(...)` is used at 3587; all imported settings used by function bodies remain live globals unless later overwritten. Full values of body-read settings belong to peer's SETTINGS_MIGRATION evidence, not reevaluation in this audit.

### Root-owned effective defaults (overriding any identically named wildcard import)

- paths/download_folders/typed paths all []; compression False, quality 40; hooks [], BookWalker False (assigned twice, 92/431), profile empty, watchdog False (55–192).
- accepted ZIP formats `.zip/.cbz/.epub`; novels `.epub`, manga `.zip/.cbz`; RAR `.rar/.cbr`, seven-zip `.7z`; convertible set only RAR/7z; images set `.jpg/.jpeg/.png/.tbn/.webp` (152–181). Extension membership often remains case-sensitive despite some unrelated `.lower()` checks.
- chapter and volume keywords/pattern order defined at 264–375; volume padding 2/4, chapter padding 3/5 at 398–402.
- library classification policy 222–247: manga threshold 1%, light novel/default 90%, digital_comps/default 90%; translation statuses official/fan/raw and languages english/japanese/chinese/korean 251–259.
- embeds 10; watcher idle sleep 10 seconds; BookWalker sleep 2 seconds; publisher score .9; image matching enabled/.9 threshold; blank similarity .9; manual duplicate deletion False; folder matching 90%; release-group similarity .8; copy volume covers False; short-word filter .7; watch discovery interval 5 and size interval 1; move-new-series False; move-correct-library False; WebP False; average chapter-image limit 85; series art names cover/poster (386–496).

### Body-read settings/config consumers to preserve

Pipeline toggles at 12035–12200 plus parser/renamer/ranking policies: `correct_file_extensions_toggle`, `convert_to_cbz_toggle`, `rename_zip_to_cbz`, `delete_unacceptable_files_toggle`, `unacceptable_keywords`, `delete_chapters_from_downloads_toggle`, `exception_keywords`, `generate_release_group_list_toggle`, `rename_files_in_download_folders_toggle`, `create_folders_for_items_in_download_folder_toggle`, `check_for_duplicate_volumes_toggle`, `extract_covers_toggle`, `check_for_existing_series_toggle`, `rename_dirs_in_download_folder_toggle`, `check_for_missing_volumes_toggle`, `cache_each_root_for_each_path_in_paths_at_beginning_toggle`, `send_scan_request_to_komga_libraries_toggle`, `chapter_support_toggle`, `extract_chapter_covers`, `use_latest_volume_cover_as_series_cover`, `compare_detected_cover_to_blank_images`, `auto_classify_watchdog_paths`, `ignored_folder_names`, `ranked_keywords`/`Keyword`, `required_similarity_score`, `match_through_identifiers`, `manual_rename`, `resturcture_when_renaming`, `preferred_volume_renaming_format`, `preferred_chapter_renaming_format`, `rename_chapters_with_preferred_chapter_keyword`, `add_volume_one_number_to_one_shots`, `add_issue_number_to_manga_file_name`, `add_publisher_name_to_file_name_when_renaming`, `search_and_add_premium_to_file_name`, `move_release_group_to_end_of_file_name`, `replace_unicode_when_restructuring`, `move_lone_files_to_similar_folder`, `replace_series_name_in_file_name_with_similar_folder_name`, `mute_discord_rename_notifications`, `output_chapter_covers_to_discord`, `log_to_file`, Komga IP/port/email/password. Preserve misspelled `resturcture_when_renaming`; it is an executable settings name, not a CLI option.

## 3. Exact CLI and startup surfaces

`parse_my_args` 1344–1643 owns one argparse parser, not subcommands. `-h/--help` is argparse-generated, but imports/default initialization occur before help can run.

| Options | Actual parsing |
|---|---|
| `-p/--paths`, `-df/--download_folders` | `action=append,nargs='*'`; repeated groups; first token supports literal backslash-one `\\1` to split roots, literal backslash-zero `\\0` to split root/restriction metadata (1449–1507). Not NUL bytes. Empty groups may be ignored. |
| `-wh/--webhook`, `-bwk_whs/--bookwalker_webhook_urls` | append/star groups; split literal `\\1`, deduplicate; not comma splitting (1543–1602). |
| `-bwc/--bookwalker_check`, `-c/--compress`, `-wd/--watchdog`, `-ltf/--log_to_file`, `--output_covers_as_webp` | value-taking options, no store_true action; only stripped case-insensitive text `true` is True, everything else False (1346–1347). |
| `-cq/--compress_quality` | direct int conversion; no range validation (1575–1578). |
| `-nw/--new_volume_webhook` | one string, routes new releases (1604 vicinity). |
| `--watchdog_discover_new_files_check_interval`, `--watchdog_file_transferred_check_interval` | applied only with effective watchdog; `.isdigit()` then int, including zero; invalid numeric strings silently ignored (1512–1530). |

No --dry-run, --apply, inspection-only mode, structured JSON output, config-path option, explicit remote-write authorization or mutation authorization exists. No environment is read by this main; deployment translates environment into CLI arguments.

Parsing order matters: downloads processed first; watchdog set next only if downloads exist; libraries processed afterward. Thus auto-classification in `process_path` 1146–1326 can run for library roots, but initial download paths were processed before watchdog became true. `process_path` traverses filesystem via `get_all_files_in_directory` during classification: >=100 files trimmed to first 75%; extension threshold 30%; chapter/volume threshold 90%; mixed types can become nested `[file_formats]` at 1221–1233, not a flat list. Restrictions accept format, extension, library-type and translation-status tokens; source-language tokens have no parsing branch. Unknown tokens can silently do nothing.

If neither argparse paths nor downloads is truthy, print and `exit()` at 1538–1541. No actual path validity is required by argument parsing. Missing downloads for watchdog can invoke `send_message` before this CLI invocation's hooks/log flag have been processed. CLI prints webhook secrets outright at 1562/1602/1607 vicinity and only filters selected external-settings names at 1617–1628.

Before returning, parser may GET Komga libraries if scan toggle AND existing-series toggle AND watchdog (1630–1643). Empty/failing GET can leave `komga_library_paths` undefined but still printed at 1643. `check_required_settings` 12206–12231 requires exact version equality with `(2,5,0)`, while script version is `(2,5,39)`, so its listed missing-setting enforcement is ineffective at this version.

Guard at 12234: parse → optional settings check → if watchdog/downloads, an outer infinite loop constructs/runs Watcher; otherwise profile-main or main. Watch mode does not perform an initial main pass before waiting for created events.

## 4. Exact ordered main pipeline

`main` 11945–12200:

1. Reset `processed_files` and `moved_files`; detect exact-string overlap between library roots and downloads (11961–11980). Other counters/queues/caches are not all reset.
2. Load cached paths if file exists, matching enabled and cache empty; filter existence. Otherwise recursively cache libraries when configured OR cache file absent, with roots and matching enabled/cache empty (11982–12011). Cache can write logs.
3. Read release-group/publisher lists when files exist; build escaped joined patterns and compile them regardless of whether lists are nonempty (12013–12039).
4. Header correction if its toggle.
5. Convert if its toggle.
6. Delete unacceptable files if its toggle.
7. Delete chapters if its toggle.
8. Generate rename lists only if generate toggle AND global log_to_file AND paths AND not watchdog AND not Docker; load skipped lists first.
9. Rename downloads if toggle.
10. Group lone files if toggle.
11. Duplicate checks if toggle AND downloads.
12. Extract covers and print stats if extraction toggle AND paths AND any exact download root is in paths. This calls extraction on all default library paths, not only the overlapping root.
13. Match/upgrade existing series if matching toggle AND downloads AND paths.
14. Rename download directories if toggle AND downloads.
15. In watch mode, purge nonexistent transferred-file paths and nonexistent transferred-folder objects.
16. Reclassify/move library series only if root-owned move-correct toggle AND library types AND typed paths AND `moved_files`. It is not a standalone unconditional correction pass.
17. Flush grouped Discord notifications if not watchdog; this call does not itself clear grouped_notifications.
18. Non-overlap library-cover path: extraction toggle AND paths AND no download overlap, plus nonwatch OR watch with moved_files. Watch without cover-copy policy narrows roots via `moved_file.startswith(path)`; otherwise all paths. `profile_code == 'extract_covers()'` invokes cProfile then exits only in this branch.
19. Local missing-volume check if toggle (also possible in watch).
20. BookWalker check if flag AND not watchdog.
21. Komga scan scheduling only if scan toggle AND matching toggle AND moved_files: lazy GET; select libraries for existing moved paths using substring-based `is_root_present`; POST scans.
22. Reset libraries_to_scan; clear contains_comic_info cache. Matching itself clears parse_words/find_consecutive_items/get_zip_comment_cache at 7200–7207. There is no durable transaction/job/outbox state.

Planning migration must account for post-rename paths and resulting archive/file models between stages. An unordered dispatch table would not preserve this workflow.

## 5. User-visible feature inventory

| Feature | Original implementation and actual behavior |
|---|---|
| Comic/novel cover discovery | `find_and_extract_cover` 8615–8767 validates existence/ZIP then lists eligible members, lexical sorts paths; case-sensitive image extension acceptance; hidden basename exclusions. |
| EPUB cover metadata | `get_novel_cover` 2339–2385 follows container rootfile → OPF metadata name=cover → manifest id and URL-unquotes href. No explicit EPUB3 cover-image property branch. `get_novel_cover_path` 8600–8611 loses directories. |
| Comic ordering | At 8722 manga extension independently qualifies every image, so first lexical image wins ahead of named-cover precedence. Patterns 502–514 matter for other selection paths. |
| Blank covers | `find_and_extract_cover` 8615–8767/8729–8759 compares white and black resource SSIM. Primary blank break repeats first candidate across pattern passes; fallback skips recorded blanks. Missing resource behavior differs between guarded primary path and fallback. |
| SSIM matching preprocessing | `prep_images_for_similarity` 11270–11324 decodes with OpenCV, resizes to 400×600, reconciles channels; `preprocess_image` 11229–11243 grayscale/equalizes histogram/normalizes; `compare_images` 11247–11266 uses SSIM data_range=1 and returns 0 on its caught errors. |
| Return-bytes covers | `find_and_extract_cover` 8615–8767 always calls compression for return_data_only, using helper default quality 60 rather than global quality 40. It is not raw archive byte parity and does not honor compression-disabled as no transform. |
| File output names/format | 8643–8655 writes beside book using extensionless name, jpeg→jpg suffix, optional webp suffix. With compression disabled, suffix can change without encoding. |
| Compression/conversion | `compress_image` 1683–1725 handles RGBA/P→RGB, PNG→JPG, WEBP save when suffix `.webp`; `convert_webp_to_jpg` 9066–9094. Failure/source-deletion risks below. |
| Chapter extraction policy | `clean_and_sort` 2075–2158 first filters all chapters by captured chapter_support; `extract_covers` 8808–9061 separately requires extract_chapter_covers. Enabling latter alone is insufficient. |
| Existing artwork reuse | `process_cover_extraction` 9103–9485 reuses existing sidecar and counts it, or extracts. Format mismatch deletes old artwork first. Successful WebP→JPG branch updates local result but does not set cover/has_cover/image_count there, delaying series-cover behavior to a later pass. |
| Series-art first/latest policy | `get_highest_release` 8772–8795; `extract_covers` 8808–9061 checks ambiguity and type; `process_cover_extraction` 9103–9485 and 9420–9479 updates/copies artwork based on index/policy, mtime/hash, root restrictions. |
| Parts/ranges/ambiguity in art | Multiple unparted non-one-shot volume ones suppress first-volume policy; highest releases can inspect range endpoints; new-series art creation has stricter index==1 branch than update check. No-subfolders, not download root, same-series predicate required for standard initial art creation. |
| Volume→chapter art copying | `extract_covers.process_volume_paths` 8840 vicinity and `process_cover_extraction` 9103–9485 scan volume-typed roots, filter first word, fuzzy-match names, use v1 or first sorted volume sidecar, compare mtimes/hashes, copy and mark checked. Dormant by root default False but implementation exists. |
| Sidecar lifecycle | `move_images` 2515–2598 handles same-stem images plus cover/poster, first/latest policy and timestamps; `rename_file` 4386–4425 recursively renames same-stem images; `remove_file` 3730–3786 cascades into `remove_images` 3640–3679. Book deletion may delete series artwork. |
| Filename type/number parser | `contains_volume_keywords` 2027–2047, `contains_chapter_keywords` 1904–1948, `get_release_number` 2966–3146, cache wrapper 3150–3152. `_extra`→.5, regex order, floats/ints/missing string, range endpoints, volumes >=2000 rejected. `.0` splitting can truncate 1.01. |
| Series names and multilingual normalization | volume parser 2614–2703; chapter parser 2779–2855; contents-prefix fallback 2230–2257; `clean_str` 4994–5039 ordered lower/colon/space/bracket/unidecode/normalize/punctuation/plural/ascii/underscore steps; `normalize_str` 4865–4963 categories. |
| One-shots/type inference | `is_one_shot` 2413–2434 uses keywords/exceptions/folder cardinality unless test flags; `upgrade_to_volume_class` 3406–3552 may classify manga as chapter if first archive image grayscale OR <=85 image count. One-shot gets number1. |
| Release metadata | `get_release_year` 3156–3186 filename year first; metadata year requires summary/description, validates year >=1950; `get_internal_metadata` 4516–4549 handles ComicInfo or OPF heuristic search; XML/HTML maps 7814–7833. |
| Part/index semantics | `get_file_part` 3259–3301 parses Part, x/# chapter suffixes; model upgrade folds integer release part into volume + part/10 at 3536–3549. |
| Extras/publisher/groups/premium/subtitle | `get_extra_from_group` 3202–3242; `get_publisher_from_meta` 3305–3325; `get_extras` 7608–7696; premium detection 4553–4566 and 5282–5324; subtitle 9786–9881. Premium EPUB detection examines bonus/signup names or toc/copyright text, not generic metadata only. |
| Keyword ranking | `get_keyword_scores` 3576–3593 sums matching keyword scores for both/specific release type; `is_upgradeable` 3612–3628 requires strictly greater downloaded score; equal names reuse one result. Empty configured ranks mean no ranking-based upgrade. |
| Download cleanup | `delete_unacceptable_files` 9510–9599 matches configured regex against whole filename, not merely extension as its comment says; `delete_chapters_from_downloads` 8513–8596 removes manga chapters unless exception keywords. Both perform empty-folder cleanup. |
| Empty-folder cleanup | `check_and_delete_empty_folder` 4179–4229 deletes hidden files first, sole cover.* second, then empty non-root dirs. This is more than removing empty directories. |
| Header correction | `get_header_extension` 2204–2221 uses filetype only for manga/RAR-named files; `correct_file_extensions` 11642–11733 renames and moves sidecar names; extension updates in transferred_files occur inside unmuted-notification branch. |
| Archive conversion | `extract` 11328–11341 delegates RAR/7z extractall; `compress` 11345–11358 writes ZIP with default storage mode; `convert_to_cbz` 11362–11637 compares names/hash membership then removes source. Valid existing CBZ skipped, zero/invalid CBZ deleted first. ZIP→CBZ rename requires convert feature AND rename_zip_to_cbz. |
| Rename keywords/padding/one-shot/issues | `rename_files` 7837–8503 supports preferred chapter/volume tokens, padding, range endpoints, `_extra`, issue number, one-shot number, punctuation cleanup, optional manual prompt and Discord suppression. `test_mode` writes planned names to logs; it is not effect-free. |
| Full filename restructuring | `reorganize_and_rename` 4575–4845 rebuilds from directory/series name plus number/subtitle/year/publisher/premium/extras/group; optional transliteration, quote/slash cleanup; caller requires misspelled resturcture flag. |
| Lone-file grouping | `create_folders_for_items_in_download_folder` 5043–5201 matches existing similar folders, optionally substitutes series name, creates new folder otherwise, moves artwork and updates watch state. Root selection compares basenames, not full path identity. |
| Directory rename/merge | `rename_dirs_in_download_folder` 7232–7604 uses >=80% volume-name agreement plus .25 folder similarity; manual rename; existing-target merge deletes same-named incoming books. Backup bracket cleaner exists but its final guard includes `not re.search(basename,root)`, normally false since root contains basename; do not advertise as reliably reachable. |
| Duplicate downloads/manual review | `check_for_duplicate_volumes` 5768–6039 compares same root/type/ext/number/name series, uses identity sets, deletes lower-ranked or equal-hash duplicate; equal-score/different-hash emits review-required message and skips. Only lower-ranked branch consults manual_delete. |
| Existing-series matching | `check_for_existing_series` 6215–7207: cached image result, cached identifier result, prioritized cached-path name similarity, full walk/name match, gated cover similarity, then unique identifier directory match. Ambiguous multiple identifier roots rejected. Name/cover first successful usable match wins; caches and path ordering affect selection. |
| Match suitability/upgrades | `check_upgrade` 5339–5680 checks both medium and chapter/volume folder percentages; optional rename; `remove_duplicate_releases` 3981–4175 compares release indexes, removes nonupgrades, replaces upgrades; collection upgrade can remove component books before replacement. New-release notification risk below. |
| Library restrictions/classification | `process_path` 1146–1326 and `get_library_type` 731–752. Match filters enforce format/extensions via raw startswith/exact-root logic, not language/source provenance. Classification is filename regex policy, not rich media metadata. |
| Move to correct library | `move_series_to_correct_library` 11738–11920 selects single typed destination and moves folder/records scans. Current extension predicate uses all(filtered matching objects), not all-files-have-allowed-extension; empty filtered list is truthy. Zero destinations indexes [0] and is caught/logged. |
| Local missing releases | `check_for_missing_volumes` 4288–4382 checks from volume1 through integer max, expands ranges and prints absent integers; despite comment it starts at 1 rather than current minimum. No remote access needed, but scans/cwd/cache/log paths remain reachable. |
| BookWalker release/preorder comparison | `search_bookwalker` 9885–10443 and `check_for_new_volumes_on_bookwalker` 10447–10774 perform paginated/category/alternate/shortened searches, filter chapters, parse title/parts/date/detail images, group series, compare local numbers/parts, log missing/released/preorders, route exactly two hooks. Price field is hardcoded 0.00. |
| BookWalker transport | `get_session_object` 9645–9656 caches sessions by stripped domain; `scrape_url` 9661–9691 GET timeout10, no general raise_for_status, 403 raises generic rate-limit Exception not caught by its RequestException handler. No live DOM/server compatibility established. |
| Watch/readiness | `Watcher.run` 676–694 starts recursive observers; `Handler.on_created` 858–1099 handles created files only, serializes entire scan/readiness/pipeline/notifications under one lock. Size equality once suffices; no modified/moved/deleted handlers or startup reconciliation. Nonaccepted convertible input is still rejected unless delete_unacceptable_files is enabled in first gate. |
| Watch caches/state | lists transferred_files/dirs, moved_files/folders, root_modification_times, processed_files; directory mtime extraction skip 8930–8948. `just_these_dirs` is passed around but unused by clean_and_sort body. |
| Discord output | `send_message` 709, `handle_fields` 1737, `pick_webhook` 1772, `send_discord_message` 1793, `group_notification` 3685. Field truncation, <=10 embeds, versions/timestamps, JPG-named attachments regardless actual bytes, optional dedicated release webhook and grouped chapter numbers. Rotation cached; notification queue can be dropped after failure budget. |
| Komga scans | GET 10871–10927, POST 10814–10865, mapping11924–11940 and pipeline12162 onward. Basic auth, 200/202 checks, no timeout; GET retries once after60sec if exception text contains104. No explicit local/server prefix mapping. |
| Logs/statistics/profiling | log writer4233; cache_path2060; print_stats9489; profile branches1028,12148,12246. Counts accumulate across main passes; image_count counts existing covers too, not extraction count. `print_execution_time`8803 and `execute_command`3964 have no main caller in source. |
| Dormant/partial surfaces | move_new_series_to_library_toggle478 has no read/caller and no moving-new-series implementation; source_languages stored but never parsed/enforced; translation-source restrictions parsed/stored but never enforced; root-owned move/copy toggles default False with no CLI exposure. These are not all working advertised features. |

## 6. R01–R25 disposition, static evidence only

| ID | Evidence and precise impact |
|---|---|
| R01 | replace_file3906–3915 deletes old book/sidecars before move; failed move cannot restore source. |
| R02 | compress_image1707–1722 catches save failure then deletes original for to_jpg; caller can also target same JPG path and delete it when to_jpg=True. |
| R03 | restructure4820–4823 and rename8467–8474 delete incoming on target-name collision without content equality. |
| R04 | lone grouping5136–5171, directory merges7360–7365/7517–7523 similarly delete incoming collisions. |
| R05 | 8643–8661 changes suffix without transcoding when file-output compression disabled; bytes mode always reencodes at helper quality60. |
| R06 | 8600–8611/8703–8709 discard full EPUB path; duplicate basenames can select wrong image. |
| R07 | 8715–8741 manga condition overrides named-cover priority; lexical-first behavior to characterize, not assume defective expectation. |
| R08 | 1771–1786 caches stateful webhook selector; repeated identical argument tuple returns old hook without rotating. |
| R09 | 756–784 single equal-size pair; Handler858–1031 locks sleeps/scans/main and only created events. |
| R10 | Komga requests10840–10849/10898–10907 have no explicit timeout. BookWalker does have timeout10; do not generalize absence to all HTTP. |
| R11 | extract11328–11341 has no app validation/budgets before extractall; cover/member reads also unbounded. Native-library traversal protections/version contract remain unverified rather than asserted absent. |
| R12 | normalize_path11924 strips drive; is_root_present11936 returns substring membership. Prefix strings elsewhere similarly not containment authorization. |
| R13 | root_modification_times8937–8946 uses directory mtime, missing in-place file changes. |
| R14 | raw webhooks printed by parse_my_args1543–1607; substring redaction covers password/email/_ip/token/user only, not webhook credentials. |
| R15 | normalize_str4912–4958 defines type_keywords only when not skip_type_keywords, then reads it in loop for other categories: enabled skip can raise UnboundLocalError. |
| R16 | parse_words6116–6131 calculates words_no_uni then splits words_lower, preserving non-ASCII unexpectedly. |
| R17 | get_identifiers6101–6110 tests lowercase guard but splits exact `Identifiers:`; lower/mixedcase can raise IndexError. |
| R18 | get_zip_comment_cache5694 maxsize=None keyed by pathname, no freshness; cleared at end of completed matching7207, so not necessarily forever for every normal pass, but unbounded/stale within pass and for external consumers/failure paths. |
| R19 | upgrade_to_volume_class3536–3549 part/10 mixes identity dimensions; decimal release parser3158 vicinity can truncate through `.0` split. |
| R20 | search_bookwalker9885–10443 mutates global threshold, restores only selected branches/end, not finally; recursive alternate early return and exceptions need characterization. The shortened early return with count!=1 does not itself follow the count==1 decrement; don't assert every early return leaks. |
| R21 | settings execution46–47 and captured defaults713/2080/2166/4240/8619; root-list captures6217 etc. |
| R22 | File535/Volume575 have identity equality/hash; duplicate set comprehensions5798–5825 rely on it. Mechanical value dataclass migration changes semantics/order. |
| R23 | get_input_from_user4458–4511 starts non-daemon input thread; timeout flips shared flag but cannot interrupt blocking input or join completion. |
| R24 | series-format replacement9010 vicinity, sidecar9151–9158, series mismatch9242–9253 delete artwork before replacement ready; move_images repeats pattern. |
| R25 | check_upgrade5628–5674 checks move_status only for state updates, then emits New Added notification and returns True regardless failure; grouping may flush before actual mutation in several other paths too. |

Additional concrete bounded candidates: parser's uninitialized komga_library_paths on empty GET1643; rename_file/rename_folder error strings reference e after exception scope4400–4424/4437–4445; convert_to_cbz exception cleanup11630–11637 may use uninitialized or previous-iteration temp_dir/repacked_file; conversion verifies hash membership rather than path→hash correspondence and treats absent hash as not a mismatch; normal ZIP publication is directly into final file, without staging/rollback. These are static targets, not runtime reproductions.

## 7. Consumers and proposed bounded WP01 inputs

Root consumers: `tests.py:4`, add-on`:20` wildcard imports; Dockerfile:68 launches executable and add-on; README:33/45/50 documents executable. No package API/__all__ isolates exports. Wildcards export not only helpers/models but imported modules, `re` alias, settings values, roots and mutable state. Preserve actual consumers in compatibility extraction; peer audit owns exhaustive used-symbol set and test mismatches. Required chapter parser root argument is at2779.

First harness batch should be strictly bounded and nonmutating:

1. Declaration import characterization: reviewed AST nodes, controlled settings records, explicit dependencies; assert no original settings execution, no CLI parse, no concrete client construction/resource probe from original init. Retain global policy/default identity/cache semantics in fresh fixture modules.
2. Existing parser/normalizer/ranker expectations selected from peer inventory, preserving test input schema rather than inventing expected CSV outputs. Core functions: number/type/name/part/extras/subtitle/clean_str/is_upgradeable. Use synthetic filename strings and explicit fake roots; pass test_mode to file/volume adapters only where reviewed, plus fail-closed OS sandbox.
3. Boundary examples: chapter0, 1.01, x/# parts, part10/12, explicit 1-3 versus 1-2-3 input, `_extra`, bracketed legitimate title, year exclusions, accented/Japanese title tokens, normalize skip_type_keywords, case-variant Identifiers. Record observed legacy quirks separately from new corrected expectations.
4. Captured defaults: construct controlled False/True policy modules before definitions, then rebind settings/global/list references to document stale default behavior; differentiate mutation of captured list from replacement.
5. CLI parser in future sandbox only: synthetic args with disposable empty roots and recording network/filesystem/log dependencies; repeat path groups, literal delimiters, booleans true/false/invalid, intervals0/negative/float, invalid quality, empty Komga result. Do not run real guard, Watcher, input thread or configured parser against actual roots.
6. Ordered orchestration seam: load original main definition but replace effectful stages with explicit recording functions and use sandbox stat/read fixtures for cache/list prerequisites. Characterize condition combinations: shared root versus separate roots, watchdog moved_files/no moved_files, copy-cover toggle, list generation Docker/watch/log restrictions. These recorders prove orchestration decisions only, not operation implementation.
7. Later bounded archive/cover fixtures may be generated synthetic tiny ZIP/EPUB with duplicate cover basenames, lexical/named candidates, blank images, known image signatures and encoder failure recorder. RAR/7z native tool/version support needs separate verified prerequisites; no real media fixtures.

Explicitly unsafe existing seams: `rename_files(test_mode=True)` still walks/checks roots, appends processed_files and calls write_to_file8360 vicinity; `check_for_existing_series(test_mode=...)` still checks cache/library existence, may scandir/chdir/cache through check_upgrade and clean_and_sort; `check_upgrade(test_mode=True)` scans and upgrades real existing files before its early return; `upgrade_to_file_class(test_mode=True)` disables headers but cannot make every error callback or caller safe. `test_mode` is selective behavior, not an isolation boundary.

Missing inputs for runtime claims: actual supported dependency/native-tool versions in a disposable environment; reviewed literal legacy-settings migration shape/Keyword contract (peer-owned); original-test expected-output inventory and CSV schema (peer-owned); sanitized Komga/qBittorrent API fixtures/server-version contracts; saved BookWalker DOM fixtures with provenance; filesystem capability/permission/mapping policy; user-approved intentional changes to every risk above. None blocks this completed static WP00 audit. No tests or runtime proof were attempted under the explicit skip-validation instruction.

## Integrator anchor reconciliation

Named top-level definition spans reconciled against the checkout AST/source-manifest after scout delivery. Narrative branch-line references still require fresh source inspection before implementation; static audits are not runtime verification.
