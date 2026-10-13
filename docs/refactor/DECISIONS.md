# Decisions

## Runtime assessment (verified development environment)

Target ordinary GIL CPython 3.14. `uv python install 3.14` installed 3.14.6; the generated lock and project environment use it. Native dependency imports succeeded on 3.14.4 and 3.14.6 on Linux x86_64. This is not full application, archive-format, Docker, or cross-platform verification. No older production support claim. [Official Python 3.14 documentation](https://docs.python.org/3.14/whatsnew/3.14.html) confirms stable release and annotation changes. Full dependency inventory and outcomes: [DEPENDENCIES.md](DEPENDENCIES.md).

Unchanged root pins fail `uv pip compile requirements.txt --python-version 3.14 --only-binary :all:` at Brotli 1.1.0. This proves missing binary compatibility under that constraint, not impossibility of source installation. Candidate direct requirements resolved successfully on 3.14 with compiled packages constrained to wheels and pure titlecase source builds allowed. Candidates are not yet production declarations or behavior verification.

Retain regex 2025.9.18 and SequenceMatcher order/thresholds. Preserve Pillow12 supported Python3.14 according to [official matrix](https://pillow.readthedocs.io/en/stable/installation/python-support.html). NumPy2.3.2 added Python3.14 wheels: [release notes](https://numpy.org/devdocs/release/2.3.2-notes.html). Candidate OpenCV stays below major5, uses modern NumPy; old 4.12 constrains NumPy below2.3. Firecrawl Developer Index was used to locate compatibility evidence; resolver/runtime evidence is authoritative for this host.

## Isolation

Read entire original code before import. bwrap network namespaces are available. Characterization must run with no host home/media exposure and writeable disposable scratch only. `test_mode` or subprocess alone is not isolation. Do not run original default entry point outside sandbox.

## Master prompt reconciliation

Read all 1,624 lines, including the complete symbol map and templates. The eight substantive embedded documents match the split handoff verbatim. [Reconciliation record](MASTER_PROMPT_RECONCILIATION.md). The proposed module diagram is a responsibility guide, not an instruction to create empty files. WP01 tooling is not a production replacement or completed migration.
