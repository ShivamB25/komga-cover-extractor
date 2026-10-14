from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


def _run_cli(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KCE_")
    }
    return subprocess.run(
        [sys.executable, "-I", "-m", "komga_cover_extractor", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def _zip_book(path: Path) -> bytes:
    image_module = pytest.importorskip("PIL.Image")
    output = io.BytesIO()
    image_module.new("RGB", (16, 16), "red").save(output, format="PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("01.png", output.getvalue())
        archive.writestr("ComicInfo.xml", "<ComicInfo><Publisher>Fixture Press</Publisher></ComicInfo>")
    return path.read_bytes()


def _tree_bytes(root: Path) -> dict[Path, bytes | None]:
    return {path.relative_to(root): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def test_root_and_subcommand_repeated_options_preserve_precedence(tmp_path: Path) -> None:
    config_before = tmp_path / "before.toml"
    config_after = tmp_path / "after.toml"
    config_before.write_text("image_quality = 20\n", encoding="utf-8")
    config_after.write_text("image_quality = 30\n", encoding="utf-8")
    result = _run_cli(
        tmp_path,
        "--config",
        str(config_before),
        "--set",
        "image_quality=40",
        "--paths",
        "before",
        "inspect-config",
        "--config",
        str(config_after),
        "--set",
        "image_quality=50",
        "--paths",
        "after",
        "--set",
        'paths=["from-set"]',
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["config"]["image_quality"] == 50
    assert payload["config"]["paths"] == [
        str((tmp_path / "before").resolve()),
        str((tmp_path / "after").resolve()),
    ]
    assert payload["sources"]["image_quality"] == "cli"


def test_absent_cli_options_preserve_selected_config(tmp_path: Path) -> None:
    config = tmp_path / "selected.toml"
    config.write_text('image_quality = 27\npaths = ["from-toml"]\n', encoding="utf-8")
    result = _run_cli(tmp_path, "--config", str(config), "inspect-config")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["config"]["image_quality"] == 27
    assert payload["config"]["paths"] == [str((tmp_path / "from-toml").resolve())]
    assert payload["sources"]["paths"] == "toml"


def test_invalid_cli_input_has_no_filesystem_effect(tmp_path: Path) -> None:
    output = tmp_path / "out.toml"
    result = _run_cli(
        tmp_path,
        "migrate-config",
        "--output",
        str(output),
        "missing-settings.py",
    )

    assert result.returncode == 2
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert result.stdout == ""


def test_migration_output_is_exclusive_and_does_not_echo_secret(tmp_path: Path) -> None:
    source = tmp_path / "settings.py"
    output = tmp_path / "migrated.toml"
    secret = "do-not-print-this-secret"
    source.write_text(
        f'komga_login_password = "{secret}"\n', encoding="utf-8"
    )
    output.write_text("keep this file\n", encoding="utf-8")

    result = _run_cli(
        tmp_path, "migrate-config", str(source), "--output", str(output)
    )

    assert result.returncode == 2
    assert output.read_text(encoding="utf-8") == "keep this file\n"
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_migration_cleans_owned_output_when_descriptor_setup_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "settings.py"
    source.write_text("required_similarity_score = 0.8\n", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KCE_")
    }
    script = """
import os
import sys
from komga_cover_extractor.cli import main

def fail(*args, **kwargs):
    raise OSError("injected descriptor setup failure")

setattr(os, sys.argv[1], fail)
raise SystemExit(main(["migrate-config", sys.argv[2], "--output", sys.argv[3]]))
"""

    for operation in ("fchmod", "fdopen"):
        output = tmp_path / f"{operation}.toml"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                operation,
                str(source),
                str(output),
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert not output.exists()
        assert result.stdout == ""


def test_migration_creates_new_protected_output_without_changing_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "settings.py"
    output = tmp_path / "migrated.toml"
    original = "required_similarity_score = 0.8\n"
    source.write_text(original, encoding="utf-8")

    result = _run_cli(
        tmp_path, "migrate-config", str(source), "--output", str(output)
    )

    assert result.returncode == 0, result.stderr
    assert source.read_text(encoding="utf-8") == original
    assert "required_similarity_score = 0.8" in output.read_text(encoding="utf-8")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_migration_source_over_one_mib_is_rejected_without_output(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    output = tmp_path / "migrated.toml"
    source.write_bytes(b"#" * ((1 << 20) + 1))

    result = _run_cli(
        tmp_path, "migrate-config", str(source), "--output", str(output)
    )

    assert result.returncode == 2
    assert not output.exists()




def test_inspect_cli_reports_parsed_release_without_writing_state(tmp_path: Path) -> None:
    import zipfile

    library = tmp_path / "library"
    library.mkdir()
    book = library / "Series v01.10.cbz"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo><Title>Series</Title></ComicInfo>")
    state = tmp_path / "state"
    original = book.read_bytes()
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    process = _run_cli(
        tmp_path, "--paths", str(library), "--set", f'state_dir="{state}"',
        "inspect", str(book), "--json",
    )
    assert process.returncode == 0, process.stderr
    release, = json.loads(process.stdout)["releases"]
    assert release["path"] == str(book)
    assert release["series"] == "Series" and release["kind"] == "volume"
    assert release["number"] == {
        "kind": "scalar", "values": ["1.10"], "raw_tokens": ["01.10"], "part": None,
    }
    assert book.read_bytes() == original
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    assert not state.exists()


def test_inspect_isolates_invalid_part_without_losing_valid_book(tmp_path: Path) -> None:
    import zipfile

    books = (tmp_path / "Series c001x1.2.3.cbz", tmp_path / "Series v02.cbz")
    for book in books:
        with zipfile.ZipFile(book, "w") as archive:
            archive.writestr("ComicInfo.xml", "<ComicInfo/>")
    original = {book.name: book.read_bytes() for book in books}
    process = _run_cli(tmp_path, "inspect", *(str(book) for book in books), "--json")
    assert process.returncode == 2
    payload = json.loads(process.stdout)
    assert [release["path"] for release in payload["releases"]] == [str(books[1])]
    assert any(decision["status"] == "invalid_input" and decision["path"] == str(books[0]) for decision in payload["decisions"])
    assert {book.name: book.read_bytes() for book in books} == original
    assert set(path.name for path in tmp_path.iterdir()) == set(original)


def test_profile_output_cannot_write_inside_explicit_book_root_without_configured_paths(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    book = library / "Series v01.cbz"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("page.txt", b"book asset")
    original = book.read_bytes()
    output = library / "selected.prof"
    result = _run_cli(tmp_path, "extract", str(book), "--set", "paths=[]", "--profile", str(output))
    assert result.returncode == 2
    assert not output.exists() and book.read_bytes() == original


def test_profile_and_plan_output_collision_is_rejected_before_named_output(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    book = library / "Series v01.cbz"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("page.txt", b"book asset")
    output = tmp_path / "shared-output"
    result = _run_cli(
        tmp_path, "extract", str(book), "--profile", str(output), "--plan-output", str(output),
    )
    assert result.returncode == 2
    assert not output.exists()


@pytest.mark.parametrize("command", ["inspect", "extract", "convert"])
@pytest.mark.parametrize("reverse", [False, True])
def test_selected_nested_zip_books_keep_complete_coverage_without_configured_roots(
    tmp_path: Path, command: str, reverse: bool,
) -> None:
    pytest.importorskip("lxml")
    library, other = tmp_path / "library", tmp_path / "other"
    books = (
        library / "Parent v01.zip",
        library / "Nested" / "Child v02.zip",
        other / "Other v03.zip",
    )
    original = {book: _zip_book(book) for book in books}
    before = _tree_bytes(tmp_path)
    selected = tuple(reversed(books)) if reverse else books
    arguments = (
        command, *(str(book) for book in selected), "--set", "paths=[]",
        "--set", "download_folders=[]", "--set", "state_dir=" + json.dumps(str(tmp_path / "state")),
        "--set", "log_dir=" + json.dumps(str(tmp_path / "logs")),
        "--set", "compare_detected_cover_to_blank_images=false",
        "--set", "series_cover_file_names=[]", "--set", "compress_image_option=false",
        "--set", "output_covers_as_webp=false",
        "--set", "manual_rename=false", "--json",
    )
    preview = _run_cli(tmp_path, *arguments)
    assert preview.returncode == 0, (preview.stdout, preview.stderr)
    payload = json.loads(preview.stdout)
    assert {release["path"]: release["number"]["values"] for release in payload["releases"]} == {
        str(books[0]): ["1"], str(books[1]): ["2"], str(books[2]): ["3"],
    }
    assert payload["counts"]["planned"] == (0 if command == "inspect" else 3)
    assert (payload["counts"]["committed"], payload["counts"]["failed"]) == (0, 0)
    assert _tree_bytes(tmp_path) == before
    if command == "inspect":
        assert payload["plans"] == []
        return
    suffix = ".png" if command == "extract" else ".cbz"
    destinations = {str(book.with_suffix(suffix)) for book in books}
    assert {operation["destination"] for plan in payload["plans"] for operation in plan["operations"]} == destinations
    applied = _run_cli(tmp_path, *arguments, "--apply")
    assert applied.returncode == 0, (applied.stdout, applied.stderr)
    outcome = json.loads(applied.stdout)
    assert (outcome["counts"]["committed"], outcome["counts"]["failed"]) == (3, 0)
    for book in books:
        target = book.with_suffix(suffix)
        if command == "extract":
            with zipfile.ZipFile(book) as archive:
                assert target.read_bytes() == archive.read("01.png")
            assert book.read_bytes() == original[book]
        else:
            assert target.read_bytes() == original[book]
            assert not book.exists()


@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_configured_nested_roots_are_still_rejected(tmp_path: Path, reverse: bool) -> None:
    library = tmp_path / "library"
    nested = library / "Nested"
    nested.mkdir(parents=True)
    marker = nested / "keep.txt"
    marker.write_bytes(b"Explicit nested roots remain invalid.")
    before = _tree_bytes(tmp_path)
    roots = (nested, library) if reverse else (library, nested)
    completed = _run_cli(
        tmp_path, "inspect", "--paths", str(roots[0]), "--paths", str(roots[1]),
        "--set", "state_dir=" + json.dumps(str(tmp_path / "state")),
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["decisions"][0]["status"] == "invalid_input"
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("private_key", ["state_dir", "log_dir"])
@pytest.mark.parametrize("relation", ["equal", "ancestor"])
@pytest.mark.parametrize("inferred", [False, True])
@pytest.mark.parametrize("command", ["inspect", "extract"])
def test_private_directories_reject_configured_and_inferred_media_before_any_write(
    tmp_path: Path, private_key: str, relation: str, inferred: bool, command: str,
) -> None:
    media = tmp_path / "private" / "media"
    media.mkdir(parents=True)
    book = media / "Series v01.zip"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo/>")
    private = media if relation == "equal" else media.parent
    before = _tree_bytes(tmp_path)
    arguments = [
        command, "--set", "state_dir=" + json.dumps(str(tmp_path / "state")),
        "--set", "log_dir=" + json.dumps(str(tmp_path / "logs")),
        "--set", private_key + "=" + json.dumps(str(private)), "--set", "log_to_file=true",
    ]
    if inferred:
        arguments.extend((str(book), "--set", "paths=[]", "--set", "download_folders=[]"))
    else:
        arguments.extend(("--paths", str(media)))
    if command == "extract":
        arguments.append("--apply")
    completed = _run_cli(tmp_path, *arguments)
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["decisions"][0]["status"] == "invalid_input"
    assert (payload["counts"]["planned"], payload["counts"]["committed"]) == (0, 0)
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("private_key", ["state_dir", "log_dir"])
def test_watch_rejects_private_ancestor_before_starting_or_creating_state(tmp_path: Path, private_key: str) -> None:
    media = tmp_path / "private" / "media"
    media.mkdir(parents=True)
    marker = media / "keep.txt"
    marker.write_bytes(b"The watch command must not start for an excluded root.")
    before = _tree_bytes(tmp_path)
    completed = _run_cli(
        tmp_path, "watch", "--paths", str(media), "--apply",
        "--set", "state_dir=" + json.dumps(str(tmp_path / "state")),
        "--set", "log_dir=" + json.dumps(str(tmp_path / "logs")),
        "--set", private_key + "=" + json.dumps(str(media.parent)), "--set", "log_to_file=true",
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["decisions"][0]["status"] == "invalid_input"
    assert _tree_bytes(tmp_path) == before


def test_inferred_ancestor_does_not_authorize_a_symlinked_selected_parent(tmp_path: Path) -> None:
    library, external = tmp_path / "library", tmp_path / "external"
    library.mkdir()
    external.mkdir()
    books = (library / "Series v01.zip", external / "Series v02.zip")
    for book in books:
        with zipfile.ZipFile(book, "w") as archive:
            archive.writestr("ComicInfo.xml", "<ComicInfo/>")
    alias = library / "Nested"
    alias.symlink_to(external, target_is_directory=True)
    before = _tree_bytes(tmp_path)
    completed = _run_cli(
        tmp_path, "inspect", str(books[0]), str(alias / books[1].name),
        "--set", "paths=[]", "--set", "download_folders=[]",
        "--set", "state_dir=" + json.dumps(str(tmp_path / "state")),
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert any(decision["reason"] == "symlink_traversal" for decision in payload["decisions"])
    assert _tree_bytes(tmp_path) == before
