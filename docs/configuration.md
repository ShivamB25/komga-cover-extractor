# Configuration

`kce inspect-config` validates and prints a redacted immutable configuration. It does not scan books, create state/log directories, or contact integrations. `kce doctor` reports local dependency/resource presence; presence is not proof of working native conversion or remote access.

Precedence is explicit CLI overrides, then `KCE_*` environment values, then the selected TOML file, then defaults. Missing CLI values do not override another source. TOML-relative paths use the TOML file's parent; CLI/environment paths use the invocation directory. Repeated `--paths`, `--download-folders`, and `--set` work before and after the command. Repeated dedicated path options combine in order and take precedence over `--set paths=...`; the last explicit assignment to another setting wins. A post-command `--config` overrides a pre-command value.

```sh
kce --config config.toml --paths incoming inspect-config --paths library
kce --set 'volume_keywords=["Volumes?", "V"]' inspect-config
kce --set 'state_dir="state"' --set 'log_dir="logs"' inspect-config
```

Environment collections use TOML literals, not JSON object syntax:

```sh
KCE_COVER_PATTERNS='["cover", "front"]' kce inspect-config
KCE_KOMGA_PATH_MAPPINGS='[{local_root="/books", remote_root="/library"}]' kce inspect-config
```

`state_dir` defaults to `$XDG_STATE_HOME/komga-cover-extractor`, or `~/.local/state/komga-cover-extractor`; `log_dir` defaults to `logs` under the selected state directory. They are scalar paths. Merely loading configuration never creates either directory. Empty root/path strings are rejected rather than interpreted as the current directory.

Policy is instance-owned: ordered volume/chapter/exclusion/subtitle regexes, cover patterns, classification records, padding widths, ranking and matching thresholds, series artwork roles, archive budgets, transport timeouts, and watch queue/readiness settings use the same inputs. Regexes retain the `regex` engine and order. Empty optional lists disable their corresponding rules, except an empty `exception_keywords` list conservatively protects all chapters from explicit chapter deletion, preserving the historical empty-regex outcome. A library-type record has exactly `name`, `extensions`, `must_contain`, `must_not_contain`, and `match_percentage`. Existing defaults are retained rather than replaced with new heuristics.

Ratios are bounded to [0,1], percentages to [0,100], counts and widths are positive integers, and archive expansion ratio is a positive finite number. File image quality and return-data quality are independent (40 and 60 respectively); both accept 1–100. Protocol/format support is not configurable: changing a limit does not enable an unsupported decoder. An enabled dormant `move_new_series_to_library_toggle` is rejected as `unsupported_policy`.

`apply`, `allow_remote`, and `allow_torrent_removal` are not configuration keys. Invocation authorization cannot come from TOML/environment, and a serialized plan never grants it. No application mutation command should be inferred from a policy toggle alone.

`chapter_support_toggle = false` excludes chapters from ordinary grouping, naming, duplicates, matching suitability, and unacceptable-file cleanup; it does not hide physical files from collision checks. Explicit `delete_chapters_from_downloads_toggle` remains separately selectable. Artwork requires both chapter support and `extract_chapter_covers` after archive-based classification. Chapter-to-volume copying uses configured matching thresholds and requires one qualifying target series.

Organizer matching never treats a download-role root, including a dual-role root, as existing-library evidence. A root-level incoming book is not eligible for library matching until grouping actually projects it into a non-root folder. `ignored_folder_names` applies to complete literal parent-path components, including configured root names and ancestors.

`convert_to_cbz_toggle` owns ordinary ZIP-to-CBZ naming through `rename_zip_to_cbz`, independently of `correct_file_extensions_toggle`. Full `run` preprocessing is download-only; focused `convert` retains explicit library selection. Available native member timestamps preserve local civil time with ZIP's two-second precision and 1980–2107 bounds.

## Safe legacy migration

```sh
kce migrate-config settings.py --output config.toml
```

Migration parses literal assignments and the reviewed `Keyword` constructor; it never imports or evaluates the source. Two-positional, mixed, and named-only constructors retain the default `file_type="both"`. Unsupported calls, imports, comprehensions, annotations, and unknown legacy settings are rejected. Input is bounded to 1 MiB. The destination must not exist and is created with mode 0600; the input cannot be overwritten. Descriptor setup/write failure removes only the newly owned incomplete output. Nonempty credentials require protected file output rather than stdout. `resturcture_when_renaming` is accepted only at the input boundary; supplying it and the canonical spelling in the same source is an error.

Remove `cache_each_root_for_each_path_in_paths_at_beginning_toggle` and `short_word_filter_percentage` from canonical TOML, environment and CLI overrides. Fresh inventories replace persisted path caches; the historical shortened-word filter had no decision effect. Safe legacy migration validates and omits the historical cache boolean.

Bare qBittorrent hosts default to HTTP. Explicit HTTP(S) URLs retain prefixes and TLS validation. A complete Komga URL may supply its own port with `komga_port = ""`. Connect, read and BookWalker timeouts must not exceed the 60-second transport budget.
