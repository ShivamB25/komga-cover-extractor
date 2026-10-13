from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path


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

    books = (tmp_path / "Series v01 Part 0.cbz", tmp_path / "Series v02.cbz")
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
