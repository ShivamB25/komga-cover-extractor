# Dependency and runtime assessment

CPython3.14 is the modernization target; ordinary GIL build, Linux x86_64. `uv python install 3.14` installed3.14.6; locked `.venv` created with3.14.6. Candidate import smoke succeeded on3.14.4 and3.14.6 (no product compatibility inference). Old Docker3.13.8 remains unchanged pending deployment migration; not a newly supported runtime claim.

All root pins inventoried below. Supporting pins are not classified unused merely because not directly imported. `pyproject.toml` legacy dependency group is an isolated characterization environment, not replacement production dependencies. Existing requirements consumers remain authoritative for unchanged legacy deployment. No production architecture replaced yet.

| Existing dependency | Role / decision |
|---|---|
| `beautifulsoup4==4.14.2` | HTML parsing (direct). |
| `Brotli==1.1.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `certifi==2025.10.5` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `charset-normalizer==3.4.4` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `discord-webhook==1.4.1` | Discord transport (direct). |
| `filetype==1.2.0` | header detection (direct). |
| `idna==3.11` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `imageio==2.37.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `inflate64==1.0.3` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `lazy_loader==0.4` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `lxml==6.0.2` | XML/HTML metadata (direct). |
| `multivolumefile==0.2.3` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `networkx==3.4.2` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `numpy==2.2.6` | image preprocessing (direct). |
| `opencv-python-headless==4.12.0.88` | image preprocessing (direct). |
| `packaging==25.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `pillow==12.0.0` | image decode/encode (direct). |
| `psutil==7.1.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `py7zr==1.0.0` | 7z conversion (direct). |
| `pybcj==1.0.6` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `pycryptodomex==3.23.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `pyppmd==1.2.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `pyzstd==0.18.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `rarfile==4.2` | RAR conversion (direct; native unrar unavailable). |
| `regex==2025.9.18` | legacy pattern semantics (direct; retained). |
| `requests==2.32.5` | BookWalker and Komga HTTP (direct). |
| `scandir==1.10.0` | legacy traversal backport (direct; retain until behavior comparison). |
| `scikit-image==0.25.2` | SSIM (direct). |
| `scipy==1.15.3` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `soupsieve==2.8` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `texttable==1.7.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `tifffile==2025.5.10` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `titlecase==2.4.1` | filename formatting (direct). |
| `typing_extensions==4.15.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `Unidecode==1.4.0` | transliteration (direct). |
| `urllib3==2.5.0` | Supporting/transitive dependency; resolve from retained direct consumers rather than independently pinning. |
| `watchdog==6.0.0` | watching (direct). |

Addon eight pins are inventoried with exact SDK behavior in BASELINE_SETTINGS_ADDON.md. Retain qbittorrent-api2025.7.0 initially; actual file priority/start/delete semantics were checked against pinned official source.

## Actual resolution/install results

- Original binary-only3.14 resolve fails first at Brotli1.1.0. This does not prove source build impossible.
- Candidate compiled-wheel resolve succeeds after allowing pure titlecase2.4.1 source build. Scandir1.10.0 also built/imported successfully; no reason to remove it before traversal characterization.
- Actual uv lock contains52 packages (universal graph), uv sync --locked --group legacy installed47 on Linux x86_64. Native imports verified cv2 4.14.0 / numpy2.5.3 / scipy1.18.1 / skimage0.26.0 / Pillow12.0.0. Other imported modules: regex,lxml,py7zr,rarfile,scandir,watchdog,qbittorrentapi.
- Original NumPy2.2.6/OpenCV4.12 constraints are not a viable3.14 binary pair; NumPy2.3.2 release notes establish3.14 support. Candidate keeps OpenCV below major5; no SSIM preprocessing changes authorized.
- Candidate changes py7zr1.0→1.1.3 and skimage0.25.2→0.26.0 for current compatibility; archive conversion and SSIM behavior still require actual synthetic tests before production promotion.
- New developer gates: pytest9.1.1, Ruff0.16.6, mypy2.3.1 in actual installed environment. Strict mypy for all new code; original untyped code is not claimed type-clean.
- Full native-format functionality, optional resource budgets, Linux ARM64/Windows/macOS/musl/NAS and actual Docker execution remain unverified. Package advisories have not been audited; no unsupported security claim about age.

## Official and primary evidence

- https://docs.python.org/3.14/whatsnew/3.14.html
- https://pillow.readthedocs.io/en/stable/installation/python-support.html
- https://numpy.org/devdocs/release/2.3.2-notes.html
- https://pypi.org/pypi/opencv-python-headless/json (current major5 deliberately not adopted)
- https://docs.astral.sh/uv/concepts/projects/dependencies/
- Firecrawl Developer Index queries for Python3.14/native support and OpenCV NumPy constraints; broad initial result was weak, followed by focused NumPy official release evidence and actual resolver/import experiments.
- Context7 /astral-sh/uv confirmed dependency-groups, locked sync and non-package project behavior.
