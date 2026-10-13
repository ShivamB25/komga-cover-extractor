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

Policy is instance-owned: ordered volume/chapter/exclusion/subtitle regexes, cover patterns, classification records, padding widths, ranking and matching thresholds, series artwork roles, archive budgets, transport timeouts, and watch queue/readiness settings can be supplied through the same inputs. Regexes retain the `regex` engine and order; empty optional lists disable their corresponding rules. A library-type record has exactly `name`, `extensions`, `must_contain`, `must_not_contain`, and `match_percentage`. Existing literal defaults are retained from the frozen original, not replaced with new heuristics.

Ratios are bounded to [0,1], percentages to [0,100], counts and widths are positive integers, and archive expansion ratio is a positive finite number. File image quality and return-data quality are independent (40 and 60 respectively); both accept 1–100. Protocol/format support is not configurable: changing a limit does not enable an unsupported decoder. An enabled dormant `move_new_series_to_library_toggle` is rejected as `unsupported_policy`.

`apply`, `allow_remote`, and `allow_torrent_removal` are not configuration keys. Invocation authorization cannot come from TOML/environment, and a serialized plan never grants it. No application mutation command should be inferred from a policy toggle alone.

## Safe legacy migration

```sh
kce migrate-config settings.py --output config.toml
```

Migration parses literal assignments and the reviewed `Keyword` constructor; it never imports or evaluates the source. Two-positional, mixed, and named-only constructors retain the default `file_type="both"`. Unsupported calls, imports, comprehensions, annotations, and unknown legacy settings are rejected. Input is bounded to 1 MiB. The destination must not exist and is created with mode 0600; the input cannot be overwritten. Descriptor setup/write failure removes only the newly owned incomplete output. Nonempty credentials require protected file output rather than stdout. `resturcture_when_renaming` is accepted only at the input boundary; supplying it and the canonical spelling in the same source is an error.
