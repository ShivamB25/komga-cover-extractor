from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.bootstrap import open_pipeline
from komga_cover_extractor.config import validate_config


def _run(tmp_path: Path, names: tuple[str, ...]) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    library = tmp_path / "library"
    folder = library / "Series"
    folder.mkdir(parents=True)
    for name in names:
        with zipfile.ZipFile(folder / name, "w") as archive:
            archive.writestr("page.txt", b"unchanged archive asset")
    before = {path: path.read_bytes() for path in folder.iterdir()}
    environment = {key: value for key, value in os.environ.items() if not key.startswith("KCE_") and key != "PYTHONPATH"}
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "komga_cover_extractor", "missing-releases",
         "--set", "paths=" + json.dumps([str(library)]),
         "--set", "state_dir=" + json.dumps(str(tmp_path / "state")),
         "--set", "chapter_support_toggle=true"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20,
    )
    assert {path: path.read_bytes() for path in folder.iterdir()} == before
    assert not (tmp_path / "state").exists()
    return completed, json.loads(completed.stdout)


def test_cli_reports_set_holes_without_treating_decimals_or_parts_as_new_volumes(tmp_path: Path) -> None:
    completed, payload = _run(tmp_path, (
        "Series v01-03-05.cbz", "Series v03 Part 2.cbz",
        "Series c002.5.cbz", "Series v02.5.cbz",
    ))
    assert completed.returncode == 0, payload
    assert payload["missing_releases"][0]["missing_volumes"] == [2, 4]
    assert payload["missing_releases"][0]["observed_volumes"] == [1, 3, 5]
    explicit = next(release for release in payload["releases"] if release["number"]["kind"] == "set")
    assert explicit["number"]["values"] == ["1", "3", "5"]
    chapters = [release for release in payload["releases"] if release["kind"] == "chapter"]
    assert chapters[0]["number"]["values"] == ["2.5"]


def test_cli_refuses_unbounded_gap_report_without_truncated_missing_list(tmp_path: Path) -> None:
    completed, payload = _run(tmp_path, ("Series v01-10001.cbz",))
    assert completed.returncode == 3, payload
    report = payload["missing_releases"][0]
    assert report["status"] == "review_required" and report["reason"] == "range_limit"
    assert report["missing_volumes"] == []


@pytest.mark.parametrize("catalog_available", [True, False])
def test_public_selected_book_gap_report_uses_the_complete_available_catalog(tmp_path: Path, catalog_available: bool) -> None:
    library = tmp_path / "library"
    folder = library / "Series"
    folder.mkdir(parents=True)
    other = library / "Other"
    other.mkdir()
    books = (folder / "Series v01.cbz", folder / "Series v03.cbz", other / "Other v02.cbz")
    for book in books:
        with zipfile.ZipFile(book, "w") as archive:
            archive.writestr("page.txt", b"unchanged archive asset")
    original = {book: book.read_bytes() for book in books}
    paths = [str(library)]
    if not catalog_available:
        paths.append(str(tmp_path / "unavailable-library"))
    config = validate_config({
        "paths": paths, "state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs"),
    })
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest("missing-releases", books=(books[1],)))
    assert tuple(release.path for release in result.releases) == (books[1],)
    assert (result.planned, result.committed, result.failed) == (0, 0, 0)
    if catalog_available:
        assert result.exit_code == 0
        assert {row["directory"]: (row["observed_volumes"], row["missing_volumes"])
                for row in result.missing_releases} == {
            str(folder): ([1, 3], [2]),
            str(other): ([2], [1]),
        }
    else:
        assert result.exit_code == 2
        assert result.missing_releases == ()
        assert any((item.status, item.reason) == ("missing_dependency", "library_observation_unavailable")
                   for item in result.decisions)
    assert {book: book.read_bytes() for book in books} == original
    assert set(folder.iterdir()) == set(books[:2])
    assert set(other.iterdir()) == {books[2]}
    assert not config.state_dir.exists() and not config.log_dir.exists()
