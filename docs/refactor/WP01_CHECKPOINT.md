# WP01 characterization checkpoint

Baseline original commit: ceb171b7e6aa0a073bc5e0b8f07a0ee8cdcbd879. Linux x86_64, ordinary CPython3.14.6. No original production/test/data/deployment file changed: all17initial tracked hashes rechecked equal.

## Real commands and outcomes

- `uv run --locked ruff check tools tests`: pass.
- `uv run --locked ruff format --check tools tests`: four files already formatted.
- `uv run --locked mypy tools`: success, two source files.
- `uv run --locked --group legacy pytest`:13passed, no failures/skips.
- LSP diagnostics for tools: no errors; basedpyright warnings remain (dynamic legacy module access/Any, unused-return annotations). Mypy is the chosen primary checker; no warnings were suppressed to claim a clean LSP result.
- `uv run --locked --group legacy python tools/characterize_legacy.py --mode fast --output docs/refactor/baseline-fast-results.json`: exit1, intentionally propagating four original assertion failures.
- `uv run --locked --group legacy python tools/characterize_legacy.py --mode all --output docs/refactor/baseline-results.json`: exit1 for the same four original failures. Corpus passes; original validate_csv also passes. Runtime10.51seconds is an observed command duration, not a comparative performance benchmark.

## Baseline outcomes

33of37assertion-bearing original functions pass. Four fail: test_remove_ignored_folders (baseline ignored list empty), test_get_extra_from_group (dormant original runner), test_get_keyword_score (dormant three-argument call no longer matches current one-argument signature), test_has_multiple_numbers (omitted original runner). These are preserved failures, not rewritten goldens. Original chapter test actually passes despite latent wrong second-argument binding; do not call it a reproduced failure.

Two separately identified adapted cases pass: explicit ignored_folder_names=['test'];108chapter calls rebound to name/fixture-root/chapter_number. Two original exercises contain no assertions and are reported not_asserted, not passing: manual rename (missing unsafe /dl fixture) and discarded sorting result.

CSV:44714rows accounted;44465validated against the actual three labeled fields;249original named exclusions explicitly not asserted. Zero field mismatches. Seven metadata columns are unpopulated/unvalidated. No claim all44714rows pass. Full results, source hashes and dependency versions in baseline-results.json.

## Safety proof

Eight sandbox tests plus five safe-settings tests prove sentinel/home exclusion, denied networking/read-only sources, writable owned scratch, absent-sandbox refusal, timeout behavior, malformed/failed report propagation, >256KiBcomplete JSON capture and explicit overflow failure, nonexecuting settings parsing and reviewed Keyword shapes. Native imports ran in the real sandbox through actual legacy probe. No actual library/torrent/webhook/server touched.

Five separate exact-AST pure reproductions in pure-risk-results.json prove R08/R12/R15/R16/R17; none fixed yet. Remaining R01-R25 have source-backed required scenarios in SAFETY_REGRESSIONS.md and acceptance-matrix.json; no destructive failure-injection coverage claimed yet.

## Limits

This is a working characterization tool, not a modernized application or completed feature parity. Whole main code is loaded only inside OS sandbox with safe injected settings; new production code must not depend on it. Original requirements/Docker remain unchanged. WP02 now changes development metadata; final gates must rerun against the final lock. WP01results record exact runtime versions; the pre-WP02lock hash was not separately captured.
