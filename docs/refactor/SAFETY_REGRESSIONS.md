# Safety regression ownership

All R01–R25 require safe reproductions and intended-fix tests in the relevant package. R08, R12, R15, R16 and R17 were reproduced with reviewed exact AST function bodies inside a network/filesystem sandbox on CPython 3.14.4; see `pure-risk-results.json`, including the full experiment source. None fixed yet. Remaining risks are static findings, not reproduced failures. Expected unsafe observations must be distinct from desired safety invariants; do not require future code to preserve them.



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


Desired outcomes and failure-injection requirements are recorded in acceptance-matrix.json and refactor-handoff/ACCEPTANCE_TESTS.md. WP01 first runs original parser assertions/full corpus; destructive reproducers must use the verified sandbox and temporary fixtures.

## Executed pure-boundary reproducers

- R08: three identical calls select the first synthetic channel three times; required rotation does not occur.
- R12: `/books` is accepted as present in `/books-old/series`.
- R15: `normalize_str("The Sample Manga", skip_type_keywords=True)` raises `UnboundLocalError`.
- R16: `parse_words("Café")` returns `["café"]`, despite computing transliteration.
- R17: `get_identifiers("identifiers: synthetic-123")` raises `IndexError`.

The experiment imported no original application or settings module, preserved decorators and injected only reviewed pure dependencies/constants. No filesystem publication, remote service, destructive workflow, or desired-fix pass is claimed.
