# Refactor status

The available-source modernization is complete on `refactor/function-first-modernization`, based on master `ceb171b7e6aa0a073bc5e0b8f07a0ee8cdcbd879`. The exhaustive advisor review accounted for all 555 persisted notes, including the displayed 364-note batch. Seven surviving defects were repaired without adding a DDO/DTO hierarchy. Current source passes 1,545 tests; strict typing covers 62 files, Ruff lint and canonical exports pass. Seven installed environments and the actual non-root image share the current wheel/source identity. The current actual-book benchmark passes all20 cases/five pairs per size with exact originals/output/pixels; modern is slower in this observed fixture. Acceptance, formatting and unavailable scope remain explicit.

The refactor is function-first. It has no DDO/DTO hierarchy, compatibility API shims or legacy runtime fallback. Original application, settings, tests, dataset, blank resources, license and add-on inputs remain frozen under `tests/fixtures/legacy/`. The sandbox binds those originals rather than the replacement launchers. Existing untracked modern source, docs and tests belonged to the user before this resume.

## Work packages

| Package | Connected implementation | Exercised status |
|---|---|---|
| WP00 baseline/runtime audit | Static inventories and authoritative lock | Complete audit. Host tests use ordinary-GIL CPython 3.14.6. The final container uses CPython 3.14.8. |
| WP01 characterization | Frozen-source sandbox and modern differential runner | Complete characterization. Fast/all retain four historical failures. Modern corpus accounts for 44,714 rows, 44,465 eligible, 249 excluded and four explicit-set corrections. Only three populated labels are asserted. |
| WP02 runtime and configuration | Canonical loader, safe migration, policy and installed CLI | Current source and all seven refreshed core/extra application proofs pass. |
| WP03 pure parsing and ranking | Shared function APIs used by application and torrent decisions | Corpus and source gate pass. Installed P04 exact part 1/9/10/12 and decimal token/rendering proof passes. |
| WP04 publication and recovery | Shared executor, journal and real `recover` CLI | Local Linux failure, real kill, fresh recovery, occupied cycles, source preservation and recovery-count proofs pass. Completed and cleanup-only recovery commit zero new groups. |
| WP05 metadata and artwork | `inspect`, `extract` and full-run composition | Actual WebP, lexical selection, EPUB identity, byte-only behavior, chapter association and owned-artwork crash paths pass. Installed first/latest part cohorts preserve pixels and original archives. |
| WP06 matching and organizer | `organize`, `duplicates`, root-role adapters and canonical `run` | Actual preview/apply/repeat and early/late-art composition pass. Final projection and current-group ownership regressions pass. |
| WP07 native conversion | `convert`, header correction and canonical run sequencing | Actual compressed RAR and generated 7z, nested manifests, source retention, decoder cancellation and fresh recovery pass. |
| WP08 gaps and dictionaries | `missing-releases`, `rename-lists`, PTY maintenance, logging and profiling | Full source and actual installed proofs pass, including literal publisher startup/refresh/rendering, complete catalog gaps and unchanged read-only import manifests. |
| WP09 integrations and torrent add-on | Owned HTTP clients, durable delivery, release checks and `qbit` | Seven final real transport scenarios pass. Actual loopback priority/start/removal authority, retry and bare-host HTTP compatibility pass. No live-service claim. |
| WP10 watcher | Bounded observer/coordinator, shared pipeline and durable jobs | Native inotify lifecycle, overflow, cancellation, retry timing, negative/review retention and apply admission fairness pass. Completed durable audit history remains uncapped. |
| WP11 invocation and deployment | Modern commands, guarded legacy launchers, real `serve`, wheel/image and gated CI | Seven current environments and real non-root image/UMASK/two-child lifecycle pass. All-layer and exact producer/configuration audits pass. Source packaging explicitly selects README/docs/eight frozen inputs; actual manifest binding is retained outside the self-referential artifact. No registry publication. |
| WP12 review and evidence | Ownership/census/acceptance audits, source/static/runtime gates and paired benchmarks | All555 advisor notes have dispositions. Current1545 tests, strict62-file typing, Ruff/export, installed before/after, real transport and actual10/1000 five-pair proofs pass. Formatter119 fails without an unrelated sweep. No speedup/all-platform claim. |

## Current exercised gates

Current advisor-corrected checks:

- Offline source suite: 1,545 passed in 158.51 seconds, `/tmp/kce-release-proof-owner-context/tests-advisor-pr-final.xml`. The first integrated invocation failed one obsolete test patching the removed caller-wide subreaper helper; that implementation-specific case was deleted, not re-pinned.
- Strict mypy passes62 source/proof files, including changed benchmark/comparator tools. Frozen-excluded Ruff lint and canonical root/add-on requirement exports pass. Read-only formatting still exits1:119 files would change,7 are already formatted.
- The immutable before wheel exposes publisher startup/refresh/rendering, uppercase grayscale, selected-gap, source-lock and caller-orphan defects. Current installed proofs pass9 policy/import cases,8 actual-PID worker scenarios and byte-identical hot-journal refusal.
- All7 established core/extra environments were refreshed with only the application wheel. All cold imports, dependency inventories, architecture boundaries and real capability checks pass. The all environment also runs the reusable bare-host and explicit-prefix CLI-to-wire proof, unsafe zero-wire refusal and helper shutdown.
- Actual image proof passes on CPython3.14.8 at UID/GID1000 with network disabled, unchanged preview volumes, decoded WebP/source retention, runtime UMASK027→0640, literal PID1 argv, invalid008 refusal, two ready children, SIGTERM143 and helper401 restart failure without mutator POSTs.
- Every saved runtime layer is inspected:11 layers/16820 paths,57 exact current-wheel package/resource members and exact launchers. Only the wheel and exact Docker wheel-stage requirements are copied from `/dist`; the old benign ignore marker is no longer copied.
- Bounded content proof scans3,631,121 owned bytes with five named credential patterns and zero matches. Exact base-plus-declared environment values, full runtime config,13 source-derived history instructions and9 bounded OCI metadata JSON payloads/16,056 bytes pass. Actual SLSAv1 public parameters are checked, not an empty v0 lookup.
- All55 installed bytecode members equal recompilation by the actual image interpreter. The console entrypoint is byte-identical to the actual pip26.2.1 `PipScriptMaker`, not generic distlib's different template.
- Exact installer requirements SHA is`a36abb47dc0a4eddcbcceae439929aac21bab5f0a88b6474856d5e1db8d0be77`; comparison uses the actual Docker uv0.11.32 producer bytes without stripping annotations.
- Current actual10/1000 five-pair benchmark passes20 cases in3002.85s with the unchanged900s case deadline. Every1000-book case preserves1000 originals/publishes1100 exact images; current modern median590.5700s versus frozen1.5023s is slower observed work. Modern has3100 fresh supervisor launches plus3100 decoder forks/6201 instrumented Python processes per1000-book case; no hidden safety/work reduction.
- Current installed transport rechecks all7 real scenarios in138.41s with unchanged60s deadlines and20/64MiB boundaries; all owned processes are reaped.
- Final deployment diff review replaces the hardcoded upstream GHCR namespace with the lowercased current repository. Actual local tagging binds the same image; no registry push or package-permission claim.
- All555 stable note IDs and grouped outcomes are in [advisor-review.json](advisor-review.json). Final staged diff check retains10 style warnings outside immutable CRLF fixtures; no unrelated formatting or frozen-byte rewrite.

Historical operational, source and benchmark receipts below retain their original outcomes. A bounded named-pattern/source audit is not an exhaustive third-party entropy, secret or vulnerability assessment.

## Historical failures retained

The earlier 848-test and 39-file type checkpoints are superseded. The later 1,442-pass suite failed five sandbox startup cases under a foreign HOME. A subsequent 1,499-case run passed 1,473 and failed 26 stale recovery-count expectations. Both invocations remain failed historical gates. The final 1,502-test source gate passed after repair.

Frozen fast/all still exits 1 for `test_remove_ignored_folders`, `test_get_extra_from_group`, `test_get_keyword_score` and `test_has_multiple_numbers`. Two original exercises remain `not_asserted`. These are distinct from corrected modern expectations.

The transaction receipt `local://transaction-gap-first-five.json` passed its first five scenarios but failed later on a fixture prerequisite. Separate receipts passed scenarios six and seven. `local://watch-long-run-after.json` passed six scenarios but failed its lifecycle fixture with `NameError`. The separate corrected `local://watch-lifecycle-proof.json` passed. Neither failed outer invocation is relabeled as a pass.

The prohibited automatic Ruff edit reported 138 transformations. Native audit reviewed 71 source and 54 proof diagnostics and restored one intentional imported `pytestmark`. Thirteen sorter transformations lack individually reconstructible pre-fix snapshots of preexisting untracked user work. This is not a full-file equivalence certificate. Frozen originals were never edited. Subsequent edits use native tools.

## Historical available-source closure

The following records describe the previous wheel/source snapshot, not the newly repaired worker or dictionary boundaries.

- P04 and D11 passed their installed operational proofs. The receipt is `local://documentation-numeric-proof.json`.
- Final actual10/1,000-book benchmark passes five pairs per size and20 actual cases. Each1000-book case preserves1000 archives and publishes1100 exact images. Modern median548.5885s versus frozen1.4781s is slower observed work, not a speedup or equal-work claim.
- README and all maintained docs are included in the development source distribution. Package verification retains all eight exact frozen inputs and produces the byte-identical tested runtime wheel.
- The previous stage-final image and actual container passed (`local://container-stage-final-proof.json`) on sourceSHA`532bf94d9fc6ef728d38c6ad7af3d84dd6ff0f30e8781d23c0e1f38010d1de3f` and wheelSHA`f94889b50c2d40971d76ea1f7c6fcb68c8319f6b6990d0f7b2b030fc739007c3`. Current advisor-corrected artifacts instead share sourceSHA`8c1029b29cdf7b680f75deebb6b1d3c3fb2a15b126414a272d95773f8a9303d8` and wheelSHA`1d6c0b0100fbd29712a5119dbc0844c869ae538e84a61d3c4ba146c49c36def8`.
- Final census retains330 exact frozen symbol identities,53 literal settings,129 initialization rows/128 names,66 features and168 unique acceptance IDs. The previous tracked-only diff was10 files/1287 insertions/15060 deletions; the PR also includes the preexisting untracked modern source/tests/docs. Those inputs lack a whole-file baseline, so no equivalence certificate is claimed.

Independent owner, acceptance and reviewer audits occurred. Model roles were automatic because no Task model selector was available. No cross-family review claim is made.

## Unavailable scope and limits

`MISSING_INPUTS.md` records absent licensed manga_isbn and epub_converter sources/assets. Their named refusal modes are verified, but their implementations cannot be claimed. Windows/NTFS, macOS, ARM64, NAS/nonlocal filesystems, live remote deployments and a current real BookWalker DOM fixture are unavailable or unauthorized.

The paired numeric benchmark passed on generated 10- and 1,000-filename sets. Modern startup and process-lifetime RSS are lower in that fixture, while cold parsing is slower. Warm legacy LRU hits do not equal modern parser evaluations. No full-application speedup, bottleneck, completed-history memory bound or full-platform parity is claimed.
