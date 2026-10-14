from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from threading import Event
from typing import Literal

import pytest

from komga_cover_extractor.application import organize as workflow
from komga_cover_extractor.application.organize import (
    OPTIONS,
    observe_organization_files,
    plan_organization,
)
from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.bootstrap import open_pipeline
from komga_cover_extractor.config import validate_config
from komga_cover_extractor.domain.decisions import RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.domain.releases import (
    FileObservation,
    ReleaseSnapshot,
)
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.matching.ranking import RankingPolicy, RankRule
from komga_cover_extractor.matching.series import MatchingPolicy
from komga_cover_extractor.parsing.filenames import (
    ParsingPolicy,
    RenamePolicy,
    parse_release,
)

PARSING = ParsingPolicy(chapter_support_toggle=True)
RENAMING = RenamePolicy(rename_chapters_with_preferred_chapter_keyword=True)
MATCHING = MatchingPolicy(match_through_image_similarity=False, match_through_identifiers=False)
RANKING = RankingPolicy((RankRule(r"Digital", 3), RankRule(r"HD", 5)))


@pytest.fixture
def roots(tmp_path: Path) -> tuple[LibraryRoot, ...]:
    downloads, library = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    return (LibraryRoot(downloads, roles=("download",)), LibraryRoot(library))


def _options(**values: bool) -> dict[str, bool]:
    return {**dict.fromkeys(OPTIONS, False), **values}


def _book(root: LibraryRoot, relative: str, data: bytes = b"synthetic page") -> ReleaseSnapshot:
    path = root.path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("pages/page.txt", data)
        if path.suffix == ".epub":
            archive.writestr("mimetype", b"application/epub+zip")
            archive.writestr("META-INF/container.xml",
                             b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                             b'<rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/>'
                             b'</rootfiles></container>')
            archive.writestr("OPS/package.opf",
                             b'<package xmlns="http://www.idpf.org/2007/opf"><manifest/></package>')
        archive.comment = b"Synthetic organizer fixture."
    siblings = tuple(sorted(path.parent.iterdir()))
    return parse_release(FileObservation(path, siblings=siblings, root_role=root.roles[0],
                                         fingerprint=fingerprint(path, root.path), at_configured_root=path.parent == root.path), PARSING)


def _catalog(roots: tuple[LibraryRoot, ...], *releases: ReleaseSnapshot) -> LibrarySnapshot:
    return LibrarySnapshot(tuple(releases), roots)


def _run(
    roots: tuple[LibraryRoot, ...], selected: tuple[ReleaseSnapshot, ...], *, apply: bool = False,
    command: str = "organize", options: dict[str, bool] | None = None, interactive: bool = False,
    ready_downloads: tuple[FileFingerprint, ...] | None = None,
    **overrides: object,
) -> RunResult:
    pytest.importorskip("PIL", reason="pipeline organizer observations require covers")
    pytest.importorskip("lxml", reason="pipeline organizer metadata requires covers")
    base = roots[0].path.parent
    values: dict[str, object] = {
        "paths": [str(root.path) for root in roots if "library" in root.roles],
        "download_folders": [str(root.path) for root in roots if "download" in root.roles],
        "state_dir": str(base / "state"), "log_dir": str(base / "logs"),
        "chapter_support_toggle": True, "rename_chapters_with_preferred_chapter_keyword": True,
        "match_through_image_similarity": False, "match_through_identifiers": False,
        "library_types": [],
        "ranked_keywords": [{"name": "Digital", "score": 3, "file_type": "both"},
                            {"name": "HD", "score": 5, "file_type": "both"}],
        "correct_file_extensions_toggle": False, "convert_to_cbz_toggle": False,
        "generate_release_group_list_toggle": False, "extract_covers_toggle": False,
        "check_for_missing_volumes_toggle": False, "bookwalker_check": False, "log_to_file": False,
        "discord_webhook_url": [], "new_volume_webhook": "", "bookwalker_webhook_urls": [],
        "send_scan_request_to_komga_libraries_toggle": False,
        **(options or _options()),
    }
    values.update(overrides)
    config = validate_config(values, base_dir=base)
    request = RunRequest(command, books=tuple(release.path for release in selected), roots=roots,
                         authorization=RunAuthorization(apply=apply), interactive=interactive,
                         ready_downloads=ready_downloads)
    with open_pipeline(config, request=request) as pipeline:
        return pipeline.run(request)


def _plan(roots, selected, catalog, *, options=None, **kwargs):
    files, directories = observe_organization_files(roots, (), Event())
    return plan_organization(selected, catalog, roots=roots, parsing=PARSING, renaming=RENAMING,
                             matching=MATCHING, ranking=RANKING, options=options or _options(), files=files,
                             directories=directories, policy_version="organizer-plan-test", **kwargs)


def _tree(root: Path) -> tuple[tuple[str, bytes | None], ...]:
    return tuple((str(path.relative_to(root)), path.read_bytes() if path.is_file() else None)
                 for path in sorted(root.rglob("*")))


def _retained_bytes(state_dir: Path, operation_id: str) -> dict[Path, bytes]:
    store = StateStore(state_dir)
    return {member.source.path: member.cleanup_path.read_bytes()
            for member in store.list_members(operation_id)
            if member.source is not None and member.cleanup_path is not None}


def _report(result, index=0):
    return result.as_dict()["plans"][index]


def _retained_groups(result, state_dir: Path) -> dict[Path, bytes]:
    return {path: data for report in result.plan_reports
            for path, data in _retained_bytes(state_dir, report.operation_id).items()}


def _png(color: str) -> bytes:
    image = pytest.importorskip("PIL.Image", reason="real organizer artwork fixtures require covers")
    output = io.BytesIO()
    image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


def test_preview_collapses_rename_and_grouping_to_original_byte_edges(roots) -> None:
    book = _book(roots[0], "Series Volume 1.cbz")
    jpg, png = book.path.with_suffix(".jpg"), book.path.with_suffix(".png")
    jpg.write_bytes(b"first associated sidecar")
    png.write_bytes(b"second associated sidecar")
    before = _tree(roots[0].path.parent)

    result = _run(roots, (book,), options=_options(
        rename_files_in_download_folders_toggle=True, create_folders_for_items_in_download_folder_toggle=True,
    ))

    assert result.exit_code == 0
    assert result.planned == 1 and result.committed == 0
    target = roots[0].path / "Series" / "Series v01.cbz"
    moves = [member for member in _report(result)["operations"] if member["kind"] == "move"]
    assert {(Path(member["source"]["root"]) / member["source"]["relative_path"], Path(member["destination"])) for member in moves} == {
        (book.path, target), (jpg, target.with_suffix(".jpg")), (png, target.with_suffix(".png")),
    }
    assert all(member["source"]["root"] == str(roots[0].path) for member in moves)
    assert result.releases[0].path == target
    assert result.releases[0].observation.fingerprint.path == book.path
    assert _tree(roots[0].path.parent) == before
    assert not (roots[0].path.parent / "state").exists()


def test_apply_moves_a_book_and_every_same_stem_sidecar_as_one_group(roots) -> None:
    book = _book(roots[0], "Series Volume 1.cbz", b"original page")
    source_bytes = book.path.read_bytes()
    jpg, png = book.path.with_suffix(".jpg"), book.path.with_suffix(".png")
    jpg.write_bytes(b"jpeg bytes are preserved without re-encoding")
    png.write_bytes(b"png bytes are preserved without re-encoding")
    result = _run(roots, (book,), apply=True, options=_options(
        rename_files_in_download_folders_toggle=True, create_folders_for_items_in_download_folder_toggle=True,
    ))
    target = roots[0].path / "Series" / "Series v01.cbz"
    assert result.exit_code == 0 and result.committed == 1
    assert target.read_bytes() == source_bytes
    assert target.with_suffix(".jpg").read_bytes() == b"jpeg bytes are preserved without re-encoding"
    assert target.with_suffix(".png").read_bytes() == b"png bytes are preserved without re-encoding"
    assert not book.path.exists()
    assert result.releases[0].observation.fingerprint == fingerprint(target, roots[0].path)
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert retained[book.path] == source_bytes
    assert retained[jpg] == b"jpeg bytes are preserved without re-encoding"
    assert retained[png] == b"png bytes are preserved without re-encoding"


def test_rename_collision_preserves_different_books_and_both_artworks(roots) -> None:
    incoming = _book(roots[0], "Series Volume 1.cbz", b"different incoming page")
    existing = _book(roots[0], "Series v01.cbz", b"existing page")
    incoming.path.with_suffix(".png").write_bytes(b"incoming image")
    existing.path.with_suffix(".png").write_bytes(b"existing image")
    before = _tree(roots[0].path.parent)
    result = _run(roots, (incoming, existing), apply=True,
                  options=_options(rename_files_in_download_folders_toggle=True))
    assert result.exit_code == 3 and result.committed == 0
    assert "multiple_planned_outputs" in {decision.reason for decision in result.decisions}
    assert _tree(roots[0].path.parent) == before


def test_equal_byte_rename_collision_can_quarantine_only_the_redundant_input(roots) -> None:
    incoming = _book(roots[0], "Series Volume 1.cbz", b"same page")
    existing = _book(roots[0], "Series v01.cbz", b"same page")
    existing_bytes = existing.path.read_bytes()
    assert incoming.path.read_bytes() == existing_bytes
    result = _run(roots, (incoming, existing), apply=True,
                  options=_options(rename_files_in_download_folders_toggle=True))
    assert result.committed == 1 and result.exit_code == 0
    assert existing.path.read_bytes() == existing_bytes
    assert not incoming.path.exists()
    assert _retained_groups(result, roots[0].path.parent / "state")[incoming.path] == existing_bytes


def test_ranked_duplicates_retire_lower_books_with_their_sidecars(roots) -> None:
    low = _book(roots[1], "Series/Series v01 (Scan).cbz", b"scan")
    high = _book(roots[1], "Series/Series v01 (Digital).cbz", b"digital")
    sidecar = low.path.with_suffix(".png")
    sidecar.write_bytes(b"associated scan art")
    low_bytes, high_bytes = low.path.read_bytes(), high.path.read_bytes()
    result = _run(roots, (low, high), apply=True, command="duplicates")
    assert result.exit_code == 0 and result.committed == 1
    assert high.path.read_bytes() == high_bytes
    assert not low.path.exists() and not sidecar.exists()
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert retained[low.path] == low_bytes and retained[sidecar] == b"associated scan art"
    assert [release.path for release in result.releases] == [high.path]


def test_equal_rank_different_byte_duplicates_require_a_concrete_choice(roots) -> None:
    one = _book(roots[1], "Series/Series v01 (A).cbz", b"one")
    two = _book(roots[1], "Series/Series v01 (B).cbz", b"two")
    before = _tree(roots[0].path.parent)
    result = _run(roots, (one, two), apply=True, command="duplicates")
    assert result.exit_code == 3 and result.committed == 0
    assert "duplicate_choice_required" in {decision.reason for decision in result.decisions}
    assert _tree(roots[0].path.parent) == before
    chosen = _plan(roots, (one, two), _catalog(roots, one, two), command="duplicates",
                   choices={("duplicate", min(one.path, two.path)): str(two.path)})
    assert [(member.kind, member.source.path) for member in chosen.plans[0].operations] == [("quarantine", one.path)]


def test_identical_duplicates_use_a_stable_observed_keeper(roots) -> None:
    one = _book(roots[1], "Series/Series v01 (A).cbz", b"same")
    two = _book(roots[1], "Series/Series v01 (B).cbz", b"same")
    result = _run(roots, (two, one), command="duplicates")
    assert result.exit_code == 0
    assert [(member["kind"], Path(member["source"]["root"]) / member["source"]["relative_path"]) for member in _report(result)["operations"]] == [("quarantine", two.path)]
    assert [release.path for release in result.releases] == [one.path]


def test_manual_delete_and_rename_do_not_touch_stdin_without_interactive_tty(roots, monkeypatch) -> None:
    low = _book(roots[0], "Series Volume 1 (Scan).cbz", b"scan")
    high = _book(roots[0], "Series v01 (Digital).cbz", b"digital")
    class UnreadableInput:
        def isatty(self):
            return False
        def readline(self, *args):
            raise AssertionError("noninteractive organization read stdin")
    monkeypatch.setattr(workflow.sys, "stdin", UnreadableInput())
    result = _run(roots, (low, high), apply=True, interactive=True,
                  options=_options(rename_files_in_download_folders_toggle=True,
                                   check_for_duplicate_volumes_toggle=True, manual_delete=True, manual_rename=True))
    assert result.exit_code == 3 and result.committed == 0
    assert {"manual_rename_required", "manual_retire_required"} <= {decision.reason for decision in result.decisions}
    assert low.path.exists() and high.path.exists()
    assert not (roots[0].path.parent / "state").exists()


def test_manual_duplicate_skip_finishes_without_repeating_the_prompt(roots, monkeypatch) -> None:
    one = _book(roots[1], "Series/Series v01 (A).cbz", b"one")
    two = _book(roots[1], "Series/Series v01 (B).cbz", b"two")
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    reader, writer = os.pipe()
    with os.fdopen(reader, "r", encoding="utf-8") as input_stream:
        try:
            os.write(writer, b"skip\n")
        finally:
            os.close(writer)
        with monkeypatch.context() as terminal:
            terminal.setattr(input_stream, "isatty", lambda: True)
            terminal.setattr(workflow.sys, "stdin", input_stream)
            terminal.setattr(workflow.sys, "stderr", Terminal())
            result = _run(roots, (one, two), apply=True,
                          command="duplicates", interactive=True)
    assert result.exit_code == 0 and result.committed == 0
    assert "duplicate_choice_declined" in {decision.reason for decision in result.decisions}
    assert one.path.exists() and two.path.exists()


def test_matching_strict_upgrade_keeps_every_original_until_the_group_commits(roots) -> None:
    old = _book(roots[1], "Series/Series v01 (Scan).cbz", b"old scan")
    incoming = _book(roots[0], "Incoming/Series Volume 1 (Digital).cbz", b"new digital")
    old_bytes, new_bytes = old.path.read_bytes(), incoming.path.read_bytes()
    old_sidecar, incoming_sidecar = old.path.with_suffix(".png"), incoming.path.with_suffix(".png")
    old_sidecar.write_bytes(b"old associated artwork")
    incoming_sidecar.write_bytes(b"incoming associated artwork")
    result = _run(roots, (incoming,), apply=True,
                  options=_options(rename_files_in_download_folders_toggle=True, check_for_existing_series_toggle=True))
    target = old.parent / "Series v01 (Digital).cbz"
    assert result.exit_code == 0 and result.committed == 1
    assert target.read_bytes() == new_bytes
    assert target.with_suffix(".png").read_bytes() == b"incoming associated artwork"
    assert not old.path.exists() and not incoming.path.exists()
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert retained[old.path] == old_bytes and retained[incoming.path] == new_bytes
    assert retained[old_sidecar] == b"old associated artwork"
    assert retained[incoming_sidecar] == b"incoming associated artwork"


def test_complete_range_upgrade_retires_all_components_after_publication(roots) -> None:
    components = tuple(_book(roots[1], f"Series/Series v0{number} (Scan).cbz", str(number).encode()) for number in (1, 2, 3))
    collection = _book(roots[0], "Incoming/Series v01-03 (Digital).cbz", b"complete collection")
    originals = {release.path: release.path.read_bytes() for release in components}
    collection_bytes = collection.path.read_bytes()
    result = _run(roots, (collection,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    target = components[0].parent / collection.name
    assert result.exit_code == 0 and result.committed == 1
    assert target.read_bytes() == collection_bytes
    assert not any(path.exists() for path in originals)
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert {path: retained[path] for path in originals} == originals
    publications = [member["member_id"] for member in _report(result)["operations"] if member["kind"] in {"move", "publish"}]
    assert all(set(publications) <= set(member["depends_on"]) for member in _report(result)["operations"] if member["kind"] == "quarantine")


def test_explicit_collection_members_do_not_retire_holes(roots) -> None:
    components = tuple(_book(roots[1], f"Series/Series v0{number} (Scan).cbz", str(number).encode()) for number in (1, 2, 3, 4, 5))
    collection = _book(roots[0], "Incoming/Series v01-03-05 (Digital).cbz", b"explicit members")
    result = _run(roots, (collection,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    assert result.exit_code == 0 and result.committed == 1
    assert [release.number.values[0] for release in components if release.path.exists()] == [2, 4]
    assert not collection.path.exists()
    with zipfile.ZipFile(components[0].parent / collection.name) as archive:
        assert archive.read("pages/page.txt") == b"explicit members"


@pytest.mark.parametrize("incoming_name, component_names", [
    ("Series v02 (Digital).cbz", ("Series v01-03 (Scan).cbz",)),
    ("Series v01-03 (Digital).cbz", ("Series v01 (Scan).cbz", "Series v02 (HD).cbz", "Series v03 (Scan).cbz")),
])
def test_partial_or_inconsistently_ranked_collections_preserve_every_candidate(roots, incoming_name, component_names) -> None:
    for name in component_names:
        _book(roots[1], "Series/" + name, name.encode())
    incoming = _book(roots[0], "Incoming/" + incoming_name, b"incoming")
    before = _tree(roots[0].path.parent)
    result = _run(roots, (incoming,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    assert result.exit_code == 3 and result.committed == 0
    assert {"partial_collection_overlap", "duplicate_choice_required"}.intersection(decision.reason for decision in result.decisions)
    assert _tree(roots[0].path.parent) == before


def test_failure_before_staging_does_not_retire_collection_components(roots, monkeypatch) -> None:
    components = tuple(_book(roots[1], f"Series/Series v0{number} (Scan).cbz", str(number).encode()) for number in (1, 2, 3))
    collection = _book(roots[0], "Incoming/Series v01-03 (Digital).cbz", b"new")
    originals = {release.path: release.path.read_bytes() for release in (*components, collection)}
    class FailingExecutor(TransactionExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, checkpoint=self.fail_stage)
        @staticmethod
        def fail_stage(phase, member):
            if phase == "before_stage":
                raise OSError("injected staging failure")
    monkeypatch.setattr(workflow, "TransactionExecutor", FailingExecutor)
    result = _run(roots, (collection,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    assert result.committed == 0 and result.failed == 1
    assert {path: path.read_bytes() for path in originals} == originals
    assert not (components[0].parent / collection.name).exists()


def test_two_name_matches_in_the_same_tier_do_not_choose_a_directory(roots) -> None:
    one = _book(roots[1], "First/Series/Series v01.cbz", b"one")
    two = _book(roots[1], "Second/Series/Series v01.cbz", b"two")
    incoming = _book(roots[0], "Incoming/Series v02.cbz", b"new")
    before = _tree(roots[0].path.parent)
    result = _run(roots, (incoming,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    assert result.exit_code == 3 and result.committed == 0
    ambiguous = next(decision for decision in result.decisions if decision.status == "ambiguous")
    assert ambiguous.candidates == (str(one.parent), str(two.parent))
    assert _tree(roots[0].path.parent) == before


def test_unavailable_catalog_does_not_authorize_a_match_or_retirement(roots) -> None:
    incoming = _book(roots[0], "Incoming/Series v01 (Digital).cbz", b"incoming")
    existing = _book(roots[1], "Series/Series v01 (Scan).cbz", b"old scan")
    with zipfile.ZipFile(existing.path, "w") as archive:
        archive.writestr("ComicInfo.xml", b"<ComicInfo><Publisher>malformed")
    originals = {release.path: release.path.read_bytes() for release in (incoming, existing)}
    result = _run(roots, (incoming,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    assert result.exit_code == 2 and result.committed == 0
    assert "library_observation_unavailable" in {decision.reason for decision in result.decisions}
    assert {path: path.read_bytes() for path in originals} == originals
    with zipfile.ZipFile(incoming.path) as archive:
        assert archive.read("pages/page.txt") == b"incoming"
    assert not (roots[0].path.parent / "state").exists()


def test_lone_file_similar_folder_ambiguity_keeps_the_lone_file(roots) -> None:
    incoming = _book(roots[0], "Series v02.cbz", b"incoming")
    (roots[0].path / "Series").mkdir()
    (roots[0].path / "SERIES").mkdir()
    result = _run(roots, (incoming,), apply=True,
                  options=_options(create_folders_for_items_in_download_folder_toggle=True, move_lone_files_to_similar_folder=True))
    assert result.exit_code == 3 and result.committed == 0
    assert "multiple_similar_download_folders" in {decision.reason for decision in result.decisions}
    assert incoming.path.exists()


def test_directory_rename_keeps_nested_assets_hidden_files_and_custom_art(roots) -> None:
    book = _book(roots[0], "Series [Batch]/Series v01.cbz", b"one")
    original_folder = book.parent
    (original_folder / "assets").mkdir()
    (original_folder / "assets" / "license.txt").write_bytes(b"nested license")
    (original_folder / ".notes").write_bytes(b"hidden notes")
    (original_folder / "poster.png").write_bytes(b"untracked custom poster")
    original_bytes = book.path.read_bytes()
    result = _run(roots, (book,), apply=True,
                  options=_options(rename_dirs_in_download_folder_toggle=True))
    target = roots[0].path / "Series"
    assert result.exit_code == 0 and result.committed == 1
    assert (target / book.name).read_bytes() == original_bytes
    assert (target / "assets" / "license.txt").read_bytes() == b"nested license"
    assert (target / ".notes").read_bytes() == b"hidden notes"
    assert (target / "poster.png").read_bytes() == b"untracked custom poster"
    assert not original_folder.exists()


def test_directory_merge_different_byte_collision_keeps_both_folders(roots) -> None:
    incoming = _book(roots[0], "Series [Batch]/Series v01.cbz", b"incoming")
    existing = _book(roots[0], "Series/Series v01.cbz", b"existing")
    before = _tree(roots[0].path.parent)
    result = _run(roots, (incoming, existing), apply=True,
                  options=_options(rename_dirs_in_download_folder_toggle=True))
    assert result.exit_code == 3 and result.committed == 0
    assert "multiple_planned_outputs" in {decision.reason for decision in result.decisions}
    assert _tree(roots[0].path.parent) == before


def test_unacceptable_and_chapter_filters_preserve_exception_volume_and_hidden_inputs(roots) -> None:
    bad = _book(roots[0], "Series v01 (BANNED).cbz", b"bad")
    chapter = _book(roots[0], "Series c000.cbz", b"chapter zero")
    extra = _book(roots[0], "Series c001 Omake.cbz", b"exception chapter")
    volume = _book(roots[0], "Series v02.cbz", b"volume")
    (roots[0].path / "BANNED.txt").write_bytes(b"unacceptable non-book")
    (roots[0].path / ".BANNED").write_bytes(b"hidden file remains")
    originals = {path: path.read_bytes() for path in (bad.path, chapter.path, roots[0].path / "BANNED.txt")}
    result = _run(roots, (), apply=True,
                  options=_options(delete_unacceptable_files_toggle=True, delete_chapters_from_downloads_toggle=True),
                  unacceptable_keywords=(r"BANNED",))
    assert result.exit_code == 0 and result.committed == 3
    assert not any(path.exists() for path in originals)
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert {path: retained[path] for path in originals} == originals
    assert extra.path.exists() and volume.path.exists()
    assert (roots[0].path / ".BANNED").read_bytes() == b"hidden file remains"


def test_parts_decimals_and_extra_markers_do_not_collapse_as_duplicates(roots) -> None:
    names = ("Series v01.01.cbz", "Series v01.10.cbz",
             "Series v01 Part 10.cbz", "Series v01 Part 12.cbz",
             "Series v02.5.cbz", "Series v02_extra.cbz")
    releases = tuple(_book(roots[1], "Series/" + name, name.encode()) for name in names)
    result = _run(roots, releases, apply=True, command="duplicates")
    assert result.exit_code == 0 and result.committed == 0
    assert {release.path for release in result.releases} == {release.path for release in releases}
    assert {release.number.part for release in result.releases} == {None, 10, 12}
    assert all(release.path.exists() for release in releases)
    assert not (roots[0].path.parent / "state").exists()


def test_shared_book_stem_blocks_sidecar_moves_before_any_publication(roots) -> None:
    comic = _book(roots[0], "Series Volume 1.cbz", b"comic")
    novel = _book(roots[0], "Series Volume 1.epub", b"novel")
    image = comic.path.with_suffix(".png")
    image.write_bytes(b"ambiguous associated image")
    before = _tree(roots[0].path.parent)
    result = _run(roots, (comic, novel), apply=True,
                  options=_options(rename_files_in_download_folders_toggle=True))
    assert result.exit_code == 3 and result.committed == 0
    assert "shared_book_sidecar_stem" in {decision.reason for decision in result.decisions}
    assert _tree(roots[0].path.parent) == before


def test_book_source_change_is_not_reinterpreted_as_the_original_observation(roots) -> None:
    release = _book(roots[0], "Series Volume 1.cbz", b"old")
    with release.path.open("ab") as stream:
        stream.write(b"external modification")
    before = release.path.read_bytes()
    files, directories = observe_organization_files(roots, (), Event())
    result = workflow.organize_group(
        (release,), _catalog(roots), roots=roots, parsing=PARSING, renaming=RENAMING, matching=MATCHING,
        ranking=RANKING, options=_options(rename_files_in_download_folders_toggle=True),
        state_dir=roots[0].path.parent / "state", policy_version="organizer-test",
        authorization=RunAuthorization(apply=True), cancel=Event(), files=files, directories=directories,
    )
    assert result.exit_code == 3 and result.committed == 0
    assert result.decisions[0].reason == "release_source_fingerprint_changed"
    assert release.path.read_bytes() == before
    assert not (roots[0].path / "Series v01.cbz").exists()
    assert not (roots[0].path.parent / "state").exists()


@pytest.mark.parametrize("match_case", ["new-release", "upgrade", "ambiguous-first", "ambiguous-second"])
def test_changed_library_match_evidence_is_stale_before_organizer_publication(
    roots: tuple[LibraryRoot, ...], match_case: str,
) -> None:
    ambiguous = match_case.startswith("ambiguous")
    folders = ("First/Series", "Second/Series") if ambiguous else ("Series",)
    existing = tuple(_book(roots[1], f"{folder}/Series v01 (Scan).cbz", folder.encode()) for folder in folders)
    incoming = _book(roots[0], f"Incoming/Series v0{2 if match_case == 'new-release' else 1} (Digital).cbz", b"incoming")
    for release in (*existing, incoming):
        release.path.with_suffix(".png").write_bytes(b"associated artwork for " + release.name.encode())
    catalog = _catalog(roots, *existing)
    files, directories = observe_organization_files(roots, (), Event())
    state = roots[0].path.parent / "state"
    options = _options(check_for_existing_series_toggle=True)
    before = _tree(roots[0].path.parent)

    prepared = workflow.prepare_organization_group(
        (incoming,), catalog, roots=roots, parsing=PARSING, renaming=RENAMING, matching=MATCHING,
        ranking=RANKING, options=options, state_dir=state, policy_version="organizer-test",
        cancel=Event(), files=files, directories=directories,
    )

    if ambiguous:
        decision = next(decision for decision in prepared.decisions if decision.status == "ambiguous")
        assert decision.candidates == tuple(str(release.parent) for release in existing)
        assert prepared.plans == ()
    else:
        assert (prepared.exit_code, prepared.planned) == (0, 1)
        operations = prepared.plans[0].operations
        assert (incoming.path, existing[0].parent / incoming.name) in {
            (operation.source.path, operation.destination) for operation in operations
            if operation.kind == "move"
        }
        assert {operation.source.path for operation in operations if operation.kind == "quarantine"} == (
            {existing[0].path, existing[0].path.with_suffix(".png")} if match_case == "upgrade" else set()
        )
    assert _tree(roots[0].path.parent) == before

    changed = existing[1 if match_case == "ambiguous-second" else 0].path
    info = changed.stat()
    original_bytes = changed.read_bytes()
    changed_bytes = original_bytes[:-1] + b"!"
    changed.write_bytes(changed_bytes)
    os.utime(changed, ns=(info.st_atime_ns, info.st_mtime_ns))
    after_external_change = _tree(roots[0].path.parent)

    result = workflow.organize_group(
        (incoming,), catalog, roots=roots, parsing=PARSING, renaming=RENAMING, matching=MATCHING,
        ranking=RANKING, options=options, state_dir=state, policy_version="organizer-test",
        authorization=RunAuthorization(apply=True), cancel=Event(), files=files, directories=directories,
    )

    assert (result.exit_code, result.planned, result.committed, result.failed) == (3, 0, 0, 0)
    assert [(decision.status, decision.reason, decision.path) for decision in result.decisions] == [
        ("stale_plan", "affected_source_fingerprint_changed", changed),
    ]
    assert changed.read_bytes() == changed_bytes
    assert (changed.stat().st_size, changed.stat().st_mtime_ns) == (info.st_size, info.st_mtime_ns)
    assert _tree(roots[0].path.parent) == after_external_change
    assert not state.exists()


@pytest.mark.parametrize("changed_source", ["incoming", "existing"])
def test_prepared_upgrade_revalidates_both_books_before_move_or_retirement(
    roots: tuple[LibraryRoot, ...], changed_source: str,
) -> None:
    existing = _book(roots[1], "Series/Series v01 (Scan).cbz", b"old scan")
    incoming = _book(roots[0], "Incoming/Series v01 (Digital).cbz", b"new digital")
    for release in (existing, incoming):
        release.path.with_suffix(".png").write_bytes(b"associated artwork for " + release.name.encode())
    originals = {release.path: release.path.read_bytes() for release in (existing, incoming)}
    files, directories = observe_organization_files(roots, (), Event())
    state = roots[0].path.parent / "state"
    prepared = workflow.prepare_organization_group(
        (incoming,), _catalog(roots, existing), roots=roots, parsing=PARSING, renaming=RENAMING,
        matching=MATCHING, ranking=RANKING, options=_options(check_for_existing_series_toggle=True),
        state_dir=state, policy_version="organizer-test", cancel=Event(), files=files, directories=directories,
    )
    assert (prepared.exit_code, prepared.planned, prepared.committed) == (0, 1, 0)
    operations = prepared.plans[0].operations
    target = existing.parent / incoming.name
    assert (incoming.path, target) in {
        (operation.source.path, operation.destination) for operation in operations if operation.kind == "move"
    }
    assert {operation.source.path for operation in operations if operation.kind == "quarantine"} == {
        existing.path, existing.path.with_suffix(".png"),
    }
    changed = incoming.path if changed_source == "incoming" else existing.path
    info = changed.stat()
    originals[changed] = originals[changed][:-1] + b"!"
    changed.write_bytes(originals[changed])
    os.utime(changed, ns=(info.st_atime_ns, info.st_mtime_ns))
    before = _tree(roots[0].path.parent)

    result = workflow.apply_organization(
        prepared, roots=roots, state_dir=state, authorization=RunAuthorization(apply=True), cancel=Event(),
    )

    assert (result.exit_code, result.committed, result.failed) == (3, 0, 0)
    assert [(decision.status, decision.reason, decision.path) for decision in result.decisions
            if decision.status == "stale_plan"] == [
        ("stale_plan", "source_fingerprint_changed", changed),
    ]
    assert {path: path.read_bytes() for path in originals} == originals
    assert _tree(roots[0].path.parent) == before
    assert not target.exists() and not target.with_suffix(".png").exists()
    assert not state.exists()


def test_symlink_is_refused_before_state_initialization(roots) -> None:
    release = _book(roots[0], "Series Volume 1.cbz")
    (roots[0].path / "alias").symlink_to(roots[1].path, target_is_directory=True)
    result = _run(roots, (), apply=True,
                  options=_options(rename_files_in_download_folders_toggle=True))
    assert result.exit_code == 3 and result.committed == 0
    assert result.decisions[0].reason == "symlink_traversal"
    assert release.path.exists()
    assert not (roots[0].path.parent / "state").exists()


def test_staged_run_segments_collapse_into_one_original_source_plan(roots) -> None:
    old = _book(roots[1], "Series/Series v01 (Scan).cbz", b"old")
    incoming = _book(roots[0], "Series Volume 1 (Digital).cbz", b"new")
    settings = _options(rename_files_in_download_folders_toggle=True,
                        create_folders_for_items_in_download_folder_toggle=True, check_for_existing_series_toggle=True)
    before = _plan(roots, (incoming,), _catalog(roots, old), options=settings, stage="before_matching", command="run")
    projected_incoming = next(release for release in before.releases if release.observation.fingerprint.path == incoming.path)
    assert projected_incoming.path == roots[0].path / "Series" / "Series v01 (Digital).cbz"
    after = _plan(roots, (projected_incoming,), _catalog(roots, old), options=settings,
                  previous_plans=before.plans, stage="after_matching", command="run")
    target = old.parent / "Series v01 (Digital).cbz"
    book_member = next(member for member in after.plans[0].operations if member.source == incoming.observation.fingerprint)
    assert book_member.destination == target
    assert book_member.source.path == incoming.path
    assert not any(member.destination == projected_incoming.path for member in after.plans[0].operations)
    assert after.planned == 1 and not target.exists()


def test_header_conversion_and_organization_compose_without_publishing_the_header_path(roots) -> None:
    py7zr = pytest.importorskip("py7zr", reason="actual conversion composition requires the conversion extra")
    original = _book(roots[0], "Series Volume 1.cbr")
    with py7zr.SevenZipFile(original.path, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        archive.writestr(b"source page", "nested/page.txt")
    original_bytes = original.path.read_bytes()
    header_path = original.path.with_suffix(".cb7")
    converted_path = original.path.with_suffix(".cbz")
    result = _run(roots, (original,), apply=True, command="run",
                  options=_options(rename_files_in_download_folders_toggle=True,
                                   create_folders_for_items_in_download_folder_toggle=True),
                  correct_file_extensions_toggle=True, convert_to_cbz_toggle=True)
    target = roots[0].path / "Series" / "Series v01.cbz"
    assert result.exit_code == 0 and result.committed == 1
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert archive.read("nested/page.txt") == b"source page"
    assert not header_path.exists() and not converted_path.exists() and not original.path.exists()
    assert _retained_groups(result, roots[0].path.parent / "state")[original.path] == original_bytes
    publications = [member for member in _report(result)["operations"] if member["kind"] == "publish"]
    assert [Path(member["destination"]) for member in publications] == [target]


def test_custom_series_art_blocks_upgrade_instead_of_guessing_ownership(roots) -> None:
    old = _book(roots[1], "Series/Series v01 (Scan).cbz", b"old")
    incoming = _book(roots[0], "Incoming/Series v01 (Digital).cbz", b"new")
    poster = old.parent / "poster.png"
    poster.write_bytes(_png("green"))
    incoming.path.with_suffix(".png").write_bytes(_png("blue"))
    before = _tree(roots[0].path.parent)
    result = _run(roots, (incoming,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    assert result.exit_code == 3 and result.committed == 0
    assert "custom_series_artwork" in {decision.reason for decision in result.decisions}
    assert _tree(roots[0].path.parent) == before


def test_owned_series_art_replacement_is_in_the_book_publication_group(roots) -> None:
    old = _book(roots[1], "Series/Series v01 (Scan).cbz", b"old")
    incoming = _book(roots[0], "Incoming/Series v01 (Digital).cbz", b"new")
    old_image, new_image = _png("red"), _png("blue")
    cover = old.parent / "cover.png"
    incoming.path.with_suffix(".png").write_bytes(new_image)
    initial = OperationPlan("initial-artwork", "initial-owned-artwork", (
        Operation("cover", "publish", cover, source=old.observation.fingerprint,
                  artifact=ArtifactSpec(content=old_image, media_type="image/png")),
    ), tuple(RootPermission(root.path) for root in roots))
    initialized = TransactionExecutor(roots[0].path.parent / "state", tuple(root.path for root in roots)).apply(
        initial, authorization=RunAuthorization(apply=True),
    )
    assert initialized.committed == 1
    old_cover_time = cover.stat().st_mtime_ns
    os.utime(incoming.path.with_suffix(".png"), ns=(old_cover_time, old_cover_time))
    result = _run(roots, (incoming,), apply=True,
                  options=_options(check_for_existing_series_toggle=True))
    assert result.exit_code == 0 and result.committed == 1
    assert cover.read_bytes() == new_image
    art_member = next(member for member in _report(result)["operations"] if member["reason"] == "owned_series_artwork")
    book_members = [member["member_id"] for member in _report(result)["operations"] if member["kind"] == "move"]
    assert set(book_members) <= set(art_member["depends_on"])
    store = StateStore(roots[0].path.parent / "state")
    retained_cover = next(member.backup_path for member in store.list_members(_report(result)["operation_id"])
                          if member.destination == cover)
    assert retained_cover.read_bytes() == old_image


def test_type_routing_requires_all_extensions_and_one_destination(roots, tmp_path) -> None:
    correct = tmp_path / "digital"
    correct.mkdir()
    match_roots = (roots[0], roots[1],
                   LibraryRoot(correct, library_types=("Digital",), formats=("volume",), extensions=(".cbz",)))
    old = _book(match_roots[1], "Series/Series v01 (Scan).cbz", b"old")
    incoming = _book(match_roots[0], "Incoming/Series v01 (Digital).cbz", b"new")
    types = [
        {"name": "Digital", "extensions": [".cbz"], "must_contain": [r"Digital"],
         "must_not_contain": [], "match_percentage": 90},
        {"name": "Scan", "extensions": [".cbz"], "must_contain": [r"Scan"],
         "must_not_contain": [], "match_percentage": 90},
    ]
    result = _run(match_roots, (incoming,), apply=True, library_types=types,
                  options=_options(check_for_existing_series_toggle=True, move_series_to_correct_library_toggle=True))
    assert result.exit_code == 3 and result.committed == 1
    assert "multiple_suitable_destinations" in {decision.reason for decision in result.decisions}
    assert (old.parent / incoming.name).exists()
    assert not (correct / "Series" / incoming.name).exists()


def test_flat_download_root_produces_independent_book_groups_not_one_root_group(roots) -> None:
    one = _book(roots[0], "First Volume 1.cbz", b"first")
    two = _book(roots[0], "Second Volume 1.cbz", b"second")
    result = _run(roots, (one, two), apply=True,
                  options=_options(rename_files_in_download_folders_toggle=True))
    assert result.exit_code == 0 and result.committed == 2 and result.planned == 2
    assert len(result.plan_reports) == 2 and result.plans == ()
    assert (roots[0].path / "First v01.cbz").read_bytes() == _retained_groups(result, roots[0].path.parent / "state")[one.path]
    assert (roots[0].path / "Second v01.cbz").read_bytes() == _retained_groups(result, roots[0].path.parent / "state")[two.path]
    assert all(len(report.as_dict()["operations"]) == 1 for report in result.plan_reports)


def test_previously_generated_sidecar_follows_the_final_matched_book_path(roots) -> None:
    existing = _book(roots[1], "Series/Series v01.cbz", b"existing")
    incoming = _book(roots[0], "Incoming/Series v02.cbz", b"new")
    picture = _png("blue")
    with zipfile.ZipFile(incoming.path, "a") as archive:
        archive.writestr("01.png", picture)
    intermediate = incoming.path.with_suffix(".png")
    early_roots = (LibraryRoot(roots[0].path, roles=("library", "download")), roots[1])
    result = _run(early_roots, (incoming,), apply=True, command="run", extract_covers_toggle=True,
                  options=_options(check_for_existing_series_toggle=True))
    target = existing.parent / incoming.name
    assert result.exit_code == 0 and result.committed == 1
    with zipfile.ZipFile(target) as archive:
        assert archive.read("pages/page.txt") == b"new"
    assert target.with_suffix(".png").read_bytes() == picture
    assert not intermediate.exists() and not incoming.path.exists()
    assert _report(result)["operations"][0]["reason"] == "existing_series_new_release"


def test_focused_organize_routes_a_selected_existing_series_without_a_prior_move(roots, tmp_path) -> None:
    target_root = tmp_path / "digital"
    target_root.mkdir()
    selected_roots = (roots[0], LibraryRoot(roots[1].path, library_types=("Scan",)),
                      LibraryRoot(target_root, library_types=("Digital",), formats=("volume",), extensions=(".cbz",)))
    release = _book(selected_roots[1], "Series/Series v01 (Digital).cbz", b"wrong root")
    types = [
        {"name": "Digital", "extensions": [".cbz"], "must_contain": [r"Digital"],
         "must_not_contain": [], "match_percentage": 90},
        {"name": "Scan", "extensions": [".cbz"], "must_contain": [r"Scan"],
         "must_not_contain": [], "match_percentage": 90},
    ]
    result = _run(selected_roots, (release,), apply=True, library_types=types,
                  options=_options(move_series_to_correct_library_toggle=True))
    destination = target_root / "Series" / release.name
    assert result.exit_code == 0 and result.committed == 1
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("pages/page.txt") == b"wrong root"
    assert not release.path.exists()
    assert "directory_route" in {member["reason"] for member in _report(result)["operations"] if member["kind"] == "move"}


def test_hash_failure_is_an_operational_failure_not_duplicate_identity(roots, monkeypatch) -> None:
    one = _book(roots[1], "Series/Series v01 (A).cbz", b"one")
    two = _book(roots[1], "Series/Series v01 (B).cbz", b"two")
    original_fingerprint = workflow.fingerprint
    def unreadable(path, root):
        if path == one.path:
            raise OSError("injected read failure")
        return original_fingerprint(path, root)
    monkeypatch.setattr(workflow, "fingerprint", unreadable)
    result = _run(roots, (one, two), apply=True, command="duplicates")
    assert result.exit_code == 1 and result.committed == 0
    assert any(decision.status == "failed" and decision.reason == "filesystem_observation_failed"
               for decision in result.decisions)
    assert one.path.exists() and two.path.exists()
    with zipfile.ZipFile(one.path) as archive:
        assert archive.read("pages/page.txt") == b"one"
    with zipfile.ZipFile(two.path) as archive:
        assert archive.read("pages/page.txt") == b"two"
    assert not (roots[0].path.parent / "state").exists()


def test_filter_stage_leaves_renaming_for_the_dictionary_insertion_point(roots) -> None:
    bad = _book(roots[0], "Bad Volume 1.cbz", b"bad")
    retained = _book(roots[0], "Series Volume 1.cbz", b"retained")
    settings = _options(delete_unacceptable_files_toggle=True, rename_files_in_download_folders_toggle=True)
    first = _plan(roots, (bad, retained), _catalog(roots), options=settings, command="run", stage="filters",
                  unacceptable_keywords=(r"^Bad",))
    assert [(member.kind, member.destination) for plan in first.plans for member in plan.operations] == [("quarantine", bad.path)]
    second = _plan(roots, (retained,), _catalog(roots), options=settings, command="run", stage="rename_and_duplicates",
                   previous_plans=first.plans, unacceptable_keywords=(r"^Bad",))
    assert {(member.kind, member.destination) for plan in second.plans for member in plan.operations} == {
        ("quarantine", bad.path), ("move", roots[0].path / "Series v01.cbz"),
    }
    assert retained.path.exists() and not (roots[0].path / "Series v01.cbz").exists()


@pytest.mark.parametrize("consumer", ["rename", "group", "duplicates", "matching"])
@pytest.mark.parametrize("chapter_support", [False, True], ids=["disabled", "enabled"])
def test_ordinary_chapter_consumers_respect_support(
    roots: tuple[LibraryRoot, ...], consumer: str, chapter_support: bool,
) -> None:
    if consumer == "rename":
        chapter = _book(roots[0], "Incoming/Series Chapter 1.cbz", b"chapter input")
        target = roots[0].path / "Incoming/Series c001.cbz"
        settings = _options(rename_files_in_download_folders_toggle=True)
        selected = (chapter,)
    elif consumer == "group":
        chapter = _book(roots[0], "Series c001.cbz", b"chapter input")
        target = roots[0].path / "Series/Series c001.cbz"
        settings = _options(create_folders_for_items_in_download_folder_toggle=True)
        selected = (chapter,)
    elif consumer == "duplicates":
        chapter = _book(roots[0], "Incoming/Series c001.cbz", b"chapter input")
        preferred = _book(roots[0], "Incoming/Series c001 (Digital).cbz", b"preferred chapter")
        target = preferred.path
        settings = _options(check_for_duplicate_volumes_toggle=True)
        selected = (chapter, preferred)
    else:
        chapter = _book(roots[0], "Incoming/Series c002.cbz", b"chapter input")
        _book(roots[1], "Series/Series c001.cbz", b"existing chapter")
        target = roots[1].path / "Series/Series c002.cbz"
        settings = _options(check_for_existing_series_toggle=True)
        selected = (chapter,)
    original = chapter.path.read_bytes()
    sidecar = chapter.path.with_suffix(".png")
    picture = _png("red")
    sidecar.write_bytes(picture)
    before = _tree(roots[0].path.parent)

    result = _run(roots, selected, apply=True, options=settings, chapter_support_toggle=chapter_support)

    assert (result.exit_code, result.committed) == (0, int(chapter_support))
    if not chapter_support:
        assert _tree(roots[0].path.parent) == before
        assert chapter.path.read_bytes() == original
        assert sidecar.read_bytes() == picture
        if consumer != "duplicates":
            assert not target.exists()
    else:
        assert not chapter.path.exists() and not sidecar.exists()
        with zipfile.ZipFile(target) as archive:
            assert archive.read("pages/page.txt") == (
                b"preferred chapter" if consumer == "duplicates" else b"chapter input"
            )
        retained = _retained_groups(result, roots[0].path.parent / "state")
        assert retained[chapter.path] == original
        assert retained[sidecar] == picture
        if consumer != "duplicates":
            assert target.with_suffix(".png").read_bytes() == picture


@pytest.mark.parametrize("chapter_support", [False, True], ids=["disabled", "enabled"])
def test_unacceptable_cleanup_retains_disabled_chapters_but_filters_volumes(
    roots: tuple[LibraryRoot, ...], chapter_support: bool,
) -> None:
    chapter = _book(roots[0], "Chapters/Series c001 [bad].cbz", b"tagged chapter")
    volume = _book(roots[0], "Volumes/Series v01 [bad].cbz", b"tagged volume")
    chapter_bytes, volume_bytes = chapter.path.read_bytes(), volume.path.read_bytes()

    result = _run(
        roots, (), apply=True, options=_options(delete_unacceptable_files_toggle=True),
        chapter_support_toggle=chapter_support, unacceptable_keywords=[r"\[bad\]"],
    )

    assert (result.exit_code, result.committed) == (0, 2 if chapter_support else 1)
    assert not volume.path.exists()
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert retained[volume.path] == volume_bytes
    if chapter_support:
        assert not chapter.path.exists()
        assert retained[chapter.path] == chapter_bytes
    else:
        assert chapter.path.read_bytes() == chapter_bytes
        assert chapter.path not in retained
        with zipfile.ZipFile(chapter.path) as archive:
            assert archive.read("pages/page.txt") == b"tagged chapter"


@pytest.mark.parametrize("chapter_support", [False, True], ids=["disabled", "enabled"])
def test_disabled_catalog_chapters_do_not_vote_against_volume_intake(
    roots: tuple[LibraryRoot, ...], chapter_support: bool,
) -> None:
    chapter = _book(roots[1], "Series/Series c001.cbz", b"existing chapter")
    volume = _book(roots[1], "Series/Series v01.cbz", b"existing volume")
    incoming = _book(roots[0], "Incoming/Series v02.cbz", b"new volume")
    originals = {release.path: release.path.read_bytes() for release in (chapter, volume, incoming)}
    target = roots[1].path / "Series/Series v02.cbz"

    result = _run(
        roots, (incoming,), apply=True, options=_options(check_for_existing_series_toggle=True),
        chapter_support_toggle=chapter_support,
    )

    assert (result.exit_code, result.committed) == (0, 0 if chapter_support else 1)
    assert chapter.path.read_bytes() == originals[chapter.path]
    assert volume.path.read_bytes() == originals[volume.path]
    if chapter_support:
        assert incoming.path.read_bytes() == originals[incoming.path]
        assert not target.exists()
        assert "no_suitable_series_folder_or_root" in {decision.reason for decision in result.decisions}
    else:
        assert not incoming.path.exists()
        assert target.read_bytes() == originals[incoming.path]
        with zipfile.ZipFile(target) as archive:
            assert archive.read("pages/page.txt") == b"new volume"
        assert _retained_groups(result, roots[0].path.parent / "state")[incoming.path] == originals[incoming.path]


@pytest.mark.parametrize("exceptions", [(), (r"Extra",)], ids=["empty-protects-all", "explicit-exception"])
def test_explicit_chapter_filter_overrides_support_without_activating_empty_exceptions(
    roots: tuple[LibraryRoot, ...], exceptions: tuple[str, ...],
) -> None:
    chapter = _book(roots[0], "Incoming/Series c001.cbz", b"ordinary chapter")
    extra = _book(roots[0], "Incoming/Series c002 Extra.cbz", b"exception chapter")
    volume = _book(roots[0], "Incoming/Series v01.cbz", b"volume")
    originals = {release.path: release.path.read_bytes() for release in (chapter, extra, volume)}
    before = _tree(roots[0].path.parent)

    result = _run(
        roots, (), apply=True, options=_options(delete_chapters_from_downloads_toggle=True),
        chapter_support_toggle=False, exception_keywords=exceptions,
    )

    assert (result.exit_code, result.committed) == (0, 1 if exceptions else 0)
    assert extra.path.read_bytes() == originals[extra.path]
    assert volume.path.read_bytes() == originals[volume.path]
    if exceptions:
        assert not chapter.path.exists()
        assert _retained_groups(result, roots[0].path.parent / "state")[chapter.path] == originals[chapter.path]
    else:
        assert chapter.path.read_bytes() == originals[chapter.path]
        assert _tree(roots[0].path.parent) == before


@pytest.mark.parametrize("duplicates", [False, True], ids=["matching-only", "duplicate-feature"])
def test_dual_role_downloads_are_not_existing_library_duplicate_evidence(
    roots: tuple[LibraryRoot, ...], duplicates: bool,
) -> None:
    dual = LibraryRoot(roots[0].path, roles=("library", "download"))
    selected_roots = (dual, roots[1])
    low = _book(dual, "Series/Series v01.cbz", b"plain download")
    high = _book(dual, "Series/Series v01 (Digital).cbz", b"digital download")
    low_bytes, high_bytes = low.path.read_bytes(), high.path.read_bytes()
    before = _tree(roots[0].path.parent)

    result = _run(selected_roots, (), command="run", apply=True, options=_options(
        check_for_existing_series_toggle=True, check_for_duplicate_volumes_toggle=duplicates,
    ))

    assert (result.exit_code, result.committed) == (0, int(duplicates))
    assert high.path.read_bytes() == high_bytes
    with zipfile.ZipFile(high.path) as archive:
        assert archive.read("pages/page.txt") == b"digital download"
    if duplicates:
        assert not low.path.exists()
        assert _retained_groups(result, roots[0].path.parent / "state")[low.path] == low_bytes
    else:
        assert low.path.read_bytes() == low_bytes
        assert _tree(roots[0].path.parent) == before


@pytest.mark.parametrize("ignored_at", ["root", "ancestor", "descendant"])
def test_ignored_folder_components_bound_actual_cleanup(
    roots: tuple[LibraryRoot, ...], ignored_at: str,
) -> None:
    if ignored_at == "descendant":
        selected_roots = roots
        protected = _book(roots[0], "Excluded/Series v01 [bad].cbz", b"excluded book")
    else:
        excluded_path = roots[0].path.parent / ("Excluded" if ignored_at == "root" else "Excluded/books")
        excluded_root = LibraryRoot(excluded_path, roles=("download",))
        selected_roots = (*roots, excluded_root)
        protected = _book(excluded_root, "Series v01 [bad].cbz", b"excluded book")
    filtered = _book(roots[0], "Series v02 [bad].cbz", b"eligible book")
    protected_bytes, filtered_bytes = protected.path.read_bytes(), filtered.path.read_bytes()

    result = _run(
        selected_roots, (), apply=True, options=_options(delete_unacceptable_files_toggle=True),
        unacceptable_keywords=[r"\[bad\]"], ignored_folder_names=["Excluded"],
    )

    assert (result.exit_code, result.committed) == (0, 1)
    assert protected.path.read_bytes() == protected_bytes
    with zipfile.ZipFile(protected.path) as archive:
        assert archive.read("pages/page.txt") == b"excluded book"
    assert not filtered.path.exists()
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert retained[filtered.path] == filtered_bytes
    assert protected.path not in retained


def test_primary_folder_match_intakes_without_filename_alias_ambiguity(roots) -> None:
    existing = _book(roots[1], "Series/Series v01.cbz", b"existing series")
    collection = _book(roots[1], "Other Collection/Series v03.cbz", b"collection member")
    incoming = _book(roots[0], "Incoming/Series v02.cbz", b"new series release")
    originals = {release.path: release.path.read_bytes() for release in (existing, collection, incoming)}

    result = _run(roots, (incoming,), apply=True, options=_options(check_for_existing_series_toggle=True))

    assert (result.exit_code, result.committed) == (0, 1)
    target = roots[1].path / "Series/Series v02.cbz"
    assert target.read_bytes() == originals[incoming.path]
    with zipfile.ZipFile(target) as archive:
        assert archive.read("pages/page.txt") == b"new series release"
    assert existing.path.read_bytes() == originals[existing.path]
    assert collection.path.read_bytes() == originals[collection.path]
    assert not incoming.path.exists()
    assert not (collection.parent / incoming.name).exists()
    assert _retained_groups(result, roots[0].path.parent / "state")[incoming.path] == originals[incoming.path]


@pytest.mark.parametrize("grouping", [False, True], ids=["root-left-alone", "group-then-match"])
def test_root_level_matching_requires_actual_prior_grouping(
    roots: tuple[LibraryRoot, ...], grouping: bool,
) -> None:
    existing = _book(roots[1], "Series/Series v01.cbz", b"existing series")
    incoming = _book(roots[0], "Series v02.cbz", b"root-level download")
    incoming_bytes = incoming.path.read_bytes()
    existing_bytes = existing.path.read_bytes()
    before = _tree(roots[0].path.parent)
    target = roots[1].path / "Series/Series v02.cbz"

    result = _run(roots, (incoming,), apply=True, options=_options(
        create_folders_for_items_in_download_folder_toggle=grouping,
        check_for_existing_series_toggle=True,
    ))

    assert (result.exit_code, result.committed) == (0, int(grouping))
    assert existing.path.read_bytes() == existing_bytes
    if grouping:
        assert not incoming.path.exists()
        assert target.read_bytes() == incoming_bytes
        with zipfile.ZipFile(target) as archive:
            assert archive.read("pages/page.txt") == b"root-level download"
        assert _retained_groups(result, roots[0].path.parent / "state")[incoming.path] == incoming_bytes
        assert not (roots[0].path / "Series/Series v02.cbz").exists()
    else:
        assert incoming.path.read_bytes() == incoming_bytes
        assert not target.exists()
        assert _tree(roots[0].path.parent) == before


@pytest.mark.parametrize("evidence", ["cover", "identifier"])
@pytest.mark.parametrize("strict_upgrade", [False, True], ids=["equal-rank-review", "higher-rank-upgrade"])
def test_unique_archive_evidence_authorizes_only_a_strict_same_number_upgrade(
    roots: tuple[LibraryRoot, ...], evidence: str, strict_upgrade: bool,
) -> None:
    image = pytest.importorskip("PIL.Image", reason="real cover evidence requires raster fixtures")
    if evidence == "cover":
        pytest.importorskip("cv2", reason="real cover evidence requires image similarity")
        pytest.importorskip("skimage", reason="real cover evidence requires image similarity")
    picture = image.new("RGB", (32, 48), "white")
    for y in range(48):
        for x in range(32):
            picture.putpixel((x, y), (30 + x * 6, 20 + y * 4, (x * 17 + y * 13) % 256))
    encoded = io.BytesIO()
    picture.save(encoded, "PNG")
    existing = _book(roots[1], "Dragon Lore/Dragon Lore v01.cbz", b"old book")
    incoming = _book(
        roots[0], f"Incoming/Dragon Lorey v01 ({'Digital' if strict_upgrade else 'Other'}).cbz", b"new book",
    )
    for release in (existing, incoming):
        with zipfile.ZipFile(release.path, "a") as archive:
            archive.writestr("01.png", encoded.getvalue())
            if evidence == "identifier":
                archive.comment = b"Identifiers: 12345"
    old_bytes, new_bytes = existing.path.read_bytes(), incoming.path.read_bytes()
    assert old_bytes != new_bytes
    before = _tree(roots[0].path.parent)
    settings = _options(check_for_existing_series_toggle=True)

    result = _run(
        roots, (incoming,), apply=True, options=settings,
        match_through_image_similarity=evidence == "cover", match_through_identifiers=evidence == "identifier",
    )

    matched = next(decision for decision in result.decisions if decision.reason == "series_match")
    assert f"evidence={'cover_similarity_match' if evidence == 'cover' else 'unique_identifier_match'}" in matched.candidates
    if evidence == "cover":
        assert "score=1.0" in matched.candidates
    if not strict_upgrade:
        assert (result.exit_code, result.committed) == (3, 0)
        assert "duplicate_choice_required" in {decision.reason for decision in result.decisions}
        assert existing.path.read_bytes() == old_bytes
        assert incoming.path.read_bytes() == new_bytes
        assert _tree(roots[0].path.parent) == before
    else:
        assert (result.exit_code, result.committed) == (0, 1)
        target = roots[1].path / "Dragon Lore/Dragon Lorey v01 (Digital).cbz"
        assert target.read_bytes() == new_bytes
        with zipfile.ZipFile(target) as archive:
            assert archive.read("pages/page.txt") == b"new book"
        assert not existing.path.exists() and not incoming.path.exists()
        assert not any(target.with_suffix(suffix).exists() for suffix in (".jpg", ".png", ".webp"))
        retained = _retained_groups(result, roots[0].path.parent / "state")
        assert retained[existing.path] == old_bytes
        assert retained[incoming.path] == new_bytes
        repeated = _run(
            roots, (), apply=True, options=settings,
            match_through_image_similarity=evidence == "cover", match_through_identifiers=evidence == "identifier",
        )
        assert (repeated.exit_code, repeated.planned, repeated.committed) == (0, 0, 0)
        assert target.read_bytes() == new_bytes


@pytest.mark.parametrize("readiness", ["ordinary", "empty", "stale", "current"])
def test_download_mutation_requires_the_exact_ready_fingerprint_when_context_is_supplied(
    roots: tuple[LibraryRoot, ...], readiness: str,
) -> None:
    book = _book(roots[0], "Incoming/Series Volume 1.cbz", b"ready book")
    ready = book.observation.fingerprint
    assert ready is not None
    if readiness == "stale":
        with zipfile.ZipFile(book.path, "a") as archive:
            archive.writestr("later.txt", b"writer resumed after readiness")
    original = book.path.read_bytes()
    context = None if readiness == "ordinary" else () if readiness == "empty" else (ready,)
    before = _tree(roots[0].path.parent)

    result = _run(
        roots, (book,), apply=True, ready_downloads=context,
        options=_options(rename_files_in_download_folders_toggle=True),
    )

    target = roots[0].path / "Incoming/Series v01.cbz"
    if readiness in {"ordinary", "current"}:
        assert (result.exit_code, result.committed) == (0, 1)
        assert not book.path.exists()
        assert target.read_bytes() == original
        with zipfile.ZipFile(target) as archive:
            assert archive.read("pages/page.txt") == b"ready book"
        assert _retained_groups(result, roots[0].path.parent / "state")[book.path] == original
    else:
        assert (result.exit_code, result.committed) == (0, 0)
        assert book.path.read_bytes() == original
        assert not target.exists()
        assert _tree(roots[0].path.parent) == before


def test_ready_scan_noop_does_not_rank_or_revalidate_a_growing_unready_digital_peer(roots) -> None:
    scan = _book(roots[0], "Series/Series v01 (Scan).cbz", b"settled scan")
    digital = _book(roots[0], "Series/Series v01 (Digital).cbz", b"unfinished digital")
    files, directories = observe_organization_files(roots, (), Event())
    with zipfile.ZipFile(digital.path, "a") as archive:
        archive.writestr("completion.txt", b"digital now complete")
    scan_bytes, digital_bytes = scan.path.read_bytes(), digital.path.read_bytes()
    before = _tree(roots[0].path.parent)
    settings = _options(check_for_duplicate_volumes_toggle=True)

    waiting = workflow.organize_group(
        (scan,), _catalog(roots, scan, digital), roots=roots, parsing=PARSING, renaming=RENAMING,
        matching=MATCHING, ranking=RANKING, options=settings, state_dir=roots[0].path.parent / "state",
        policy_version="ready-duplicates", authorization=RunAuthorization(apply=True), cancel=Event(),
        files=files, directories=directories, scope_sources=frozenset(files), ready_downloads=(files[scan.path],),
    )

    assert (waiting.exit_code, waiting.planned, waiting.committed) == (0, 0, 0)
    assert ("observed", "watch_candidate_not_ready", digital.path) in {
        (decision.status, decision.reason, decision.path) for decision in waiting.decisions
    }
    assert scan.path.read_bytes() == scan_bytes
    assert digital.path.read_bytes() == digital_bytes
    assert _tree(roots[0].path.parent) == before
    ready = (fingerprint(scan.path, roots[0].path), fingerprint(digital.path, roots[0].path))

    completed = _run(roots, (digital,), apply=True, options=settings, ready_downloads=ready)

    assert (completed.exit_code, completed.committed) == (0, 1)
    assert not scan.path.exists()
    assert digital.path.read_bytes() == digital_bytes
    with zipfile.ZipFile(digital.path) as archive:
        assert archive.read("completion.txt") == b"digital now complete"
    assert _retained_groups(completed, roots[0].path.parent / "state")[scan.path] == scan_bytes


@pytest.mark.parametrize("roles", [("library",), ("library", "download")], ids=["library-only", "dual-role"])
def test_empty_ready_context_withholds_download_authority_without_disabling_library_cleanup(
    roots: tuple[LibraryRoot, ...], roles: tuple[Literal["library", "download"], ...],
) -> None:
    root = LibraryRoot(roots[0].path, roles=roles)
    selected_roots = (root, roots[1])
    scan = _book(root, "Series/Series v01 (Scan).cbz", b"library scan")
    digital = _book(root, "Series/Series v01 (Digital).cbz", b"library digital")
    scan_bytes, digital_bytes = scan.path.read_bytes(), digital.path.read_bytes()
    before = _tree(root.path.parent)

    result = _run(selected_roots, (scan, digital), apply=True, command="duplicates", ready_downloads=())

    assert digital.path.read_bytes() == digital_bytes
    with zipfile.ZipFile(digital.path) as archive:
        assert archive.read("pages/page.txt") == b"library digital"
    if roles == ("library",):
        assert (result.exit_code, result.committed) == (0, 1)
        assert not scan.path.exists()
        assert _retained_groups(result, root.path.parent / "state")[scan.path] == scan_bytes
    else:
        assert (result.exit_code, result.committed) == (0, 0)
        assert scan.path.read_bytes() == scan_bytes
        assert _tree(root.path.parent) == before


def test_requested_folder_rename_waits_for_every_actual_file_participant(roots) -> None:
    scan = _book(roots[0], "Series [Batch]/Series v01 (Scan).cbz", b"settled scan")
    digital = _book(roots[0], "Series [Batch]/Series v01 (Digital).cbz", b"pending digital")
    note = scan.parent / ".notes"
    note.write_bytes(b"hidden user note")
    originals = {path: path.read_bytes() for path in (scan.path, digital.path, note)}
    before = _tree(roots[0].path.parent)
    settings = _options(rename_dirs_in_download_folder_toggle=True)
    context: tuple[FileFingerprint, ...] = (fingerprint(scan.path, roots[0].path),)

    waiting = _run(roots, (scan,), apply=True, options=settings, ready_downloads=context)

    assert (waiting.exit_code, waiting.planned, waiting.committed) == (3, 0, 0)
    deferred = next(decision for decision in waiting.decisions if decision.reason == "watch_scope_not_ready")
    assert deferred.status == "delivery_deferred"
    assert deferred.path == roots[0].path / "Series [Batch]"
    assert set(deferred.candidates) == {str(digital.path), str(note)}
    assert _tree(roots[0].path.parent) == before
    context = tuple(fingerprint(path, roots[0].path) for path in originals)

    completed = _run(roots, (scan,), apply=True, options=settings, ready_downloads=context)

    assert (completed.exit_code, completed.committed) == (0, 1)
    target = roots[0].path / "Series"
    assert (target / "Series v01 (Scan).cbz").read_bytes() == originals[scan.path]
    assert (target / "Series v01 (Digital).cbz").read_bytes() == originals[digital.path]
    assert (target / ".notes").read_bytes() == b"hidden user note"
    assert not scan.parent.exists()
    retained = _retained_groups(completed, roots[0].path.parent / "state")
    assert {path: retained[path] for path in originals} == originals


@pytest.mark.parametrize("identical", [False, True], ids=["different-byte-conflict", "identical-target-waits"])
def test_unready_rename_target_remains_visible_without_authorizing_its_cleanup(
    roots: tuple[LibraryRoot, ...], identical: bool,
) -> None:
    incoming = _book(roots[0], "Incoming/Series Volume 1.cbz", b"ready incoming")
    target = _book(roots[0], "Incoming/Series v01.cbz", b"unready target")
    if identical:
        target.path.write_bytes(incoming.path.read_bytes())
    originals = {release.path: release.path.read_bytes() for release in (incoming, target)}
    before = _tree(roots[0].path.parent)

    result = _run(
        roots, (incoming,), apply=True, options=_options(rename_files_in_download_folders_toggle=True),
        ready_downloads=(fingerprint(incoming.path, roots[0].path),),
    )

    assert (result.exit_code, result.planned, result.committed) == (3, 0, 0)
    assert ("watch_scope_not_ready" if identical else "multiple_planned_outputs") in {
        decision.reason for decision in result.decisions
    }
    assert {path: path.read_bytes() for path in originals} == originals
    with zipfile.ZipFile(incoming.path) as archive:
        assert archive.read("pages/page.txt") == b"ready incoming"
    assert _tree(roots[0].path.parent) == before


@pytest.mark.parametrize("sidecar_ready", [False, True], ids=["sidecar-pending", "whole-group-ready"])
def test_upgrade_does_not_retire_the_library_book_before_incoming_sidecar_readiness(
    roots: tuple[LibraryRoot, ...], sidecar_ready: bool,
) -> None:
    existing = _book(roots[1], "Series/Series v01.cbz", b"retained library book")
    incoming = _book(roots[0], "Incoming/Series v01 (Digital).cbz", b"ready upgrade")
    sidecar = incoming.path.with_suffix(".png")
    picture = _png("blue")
    sidecar.write_bytes(picture)
    old_bytes, new_bytes = existing.path.read_bytes(), incoming.path.read_bytes()
    before = _tree(roots[0].path.parent)
    context: tuple[FileFingerprint, ...] = (fingerprint(incoming.path, roots[0].path),)
    if sidecar_ready:
        context += (fingerprint(sidecar, roots[0].path),)

    result = _run(
        roots, (incoming,), apply=True, options=_options(check_for_existing_series_toggle=True),
        ready_downloads=context,
    )

    target = roots[1].path / "Series/Series v01 (Digital).cbz"
    if not sidecar_ready:
        assert (result.exit_code, result.planned, result.committed) == (3, 0, 0)
        deferred = next(decision for decision in result.decisions if decision.reason == "watch_scope_not_ready")
        assert (deferred.status, deferred.path, deferred.candidates) == (
            "delivery_deferred", incoming.path, (str(sidecar),),
        )
        assert existing.path.read_bytes() == old_bytes
        assert incoming.path.read_bytes() == new_bytes
        assert sidecar.read_bytes() == picture
        assert not target.exists()
        assert _tree(roots[0].path.parent) == before
    else:
        assert (result.exit_code, result.committed) == (0, 1)
        assert target.read_bytes() == new_bytes
        assert target.with_suffix(".png").read_bytes() == picture
        with zipfile.ZipFile(target) as archive:
            assert archive.read("pages/page.txt") == b"ready upgrade"
        assert not existing.path.exists() and not incoming.path.exists() and not sidecar.exists()
        retained = _retained_groups(result, roots[0].path.parent / "state")
        assert retained[existing.path] == old_bytes
        assert retained[incoming.path] == new_bytes
        assert retained[sidecar] == picture


def test_disabled_catalog_chapter_does_not_block_the_matched_upgrade_artwork_group(roots) -> None:
    old = _book(roots[1], "Series/Series v01 (Scan).cbz", b"old scan")
    chapter = _book(roots[1], "Series/Series c001.cbz", b"disabled catalog chapter")
    incoming = _book(roots[0], "Incoming/Series v01 (Digital).cbz", b"new digital")
    old_bytes, chapter_bytes, new_bytes = (release.path.read_bytes() for release in (old, chapter, incoming))
    cover = old.parent / "cover.png"
    old_picture, new_picture = _png("red"), _png("blue")
    incoming.path.with_suffix(".png").write_bytes(new_picture)
    seeded = TransactionExecutor(roots[0].path.parent / "state", tuple(root.path for root in roots)).apply(
        OperationPlan("initial-artwork", "chapter-catalog-owned-artwork", (
            Operation("cover", "publish", cover, source=old.observation.fingerprint,
                      artifact=ArtifactSpec(content=old_picture, media_type="image/png")),
        ), tuple(RootPermission(root.path) for root in roots)),
        authorization=RunAuthorization(apply=True),
    )
    assert seeded.committed == 1

    result = _run(
        roots, (incoming,), apply=True, options=_options(check_for_existing_series_toggle=True),
        chapter_support_toggle=False,
    )

    assert (result.exit_code, result.committed) == (0, 1)
    target = roots[1].path / "Series/Series v01 (Digital).cbz"
    assert target.read_bytes() == new_bytes
    assert target.with_suffix(".png").read_bytes() == new_picture
    assert cover.read_bytes() == new_picture
    assert chapter.path.read_bytes() == chapter_bytes
    with zipfile.ZipFile(chapter.path) as archive:
        assert archive.read("pages/page.txt") == b"disabled catalog chapter"
    assert not old.path.exists() and not incoming.path.exists()
    retained = _retained_groups(result, roots[0].path.parent / "state")
    assert retained[old.path] == old_bytes and retained[incoming.path] == new_bytes
    members = StateStore(roots[0].path.parent / "state").list_members(_report(result)["operation_id"])
    prior_cover = next(member.backup_path for member in members if member.destination == cover)
    assert prior_cover is not None and prior_cover.read_bytes() == old_picture


@pytest.mark.parametrize("command", ("duplicates", "run"))
def test_owned_download_series_art_waits_for_its_exact_ready_fingerprint(
    roots: tuple[LibraryRoot, ...], command: str,
) -> None:
    scan = _book(roots[0], "Series/Series v01 (Scan).cbz", b"ready scan")
    digital = _book(roots[0], "Series/Series v01 (Digital).cbz", b"ready upgrade")
    scan_bytes, digital_bytes = scan.path.read_bytes(), digital.path.read_bytes()
    old_image, new_image = _png("red"), _png("blue")
    cover = scan.parent / "cover.png"
    sidecar = digital.path.with_suffix(".png")
    sidecar.write_bytes(new_image)
    state_dir = roots[0].path.parent / "state"
    seeded = TransactionExecutor(state_dir, tuple(root.path for root in roots)).apply(
        OperationPlan("owned-cover-readiness", "seed-owned-cover-readiness", (
            Operation("cover", "publish", cover, source=scan.observation.fingerprint,
                      artifact=ArtifactSpec(content=old_image, media_type="image/png")),
        ), tuple(RootPermission(root.path) for root in roots)),
        authorization=RunAuthorization(apply=True),
    )
    assert seeded.committed == 1
    context = tuple(fingerprint(path, roots[0].path) for path in (scan.path, digital.path, sidecar))
    before = tuple(_tree(root.path) for root in roots)
    operations_before = tuple(record.operation_id for record in StateStore(state_dir).list_operations())
    waiting = _run(
        roots, (digital,), apply=True, command=command,
        options=_options(check_for_duplicate_volumes_toggle=True), ready_downloads=context,
    )
    assert (waiting.exit_code, waiting.planned, waiting.committed) == (3, 0, 0)
    assert any(decision.reason == "watch_scope_not_ready" for decision in waiting.decisions)
    assert tuple(_tree(root.path) for root in roots) == before
    assert tuple(record.operation_id for record in StateStore(state_dir).list_operations()) == operations_before
    complete = _run(
        roots, (digital,), apply=True, command=command,
        options=_options(check_for_duplicate_volumes_toggle=True),
        ready_downloads=(*context, fingerprint(cover, roots[0].path)),
    )
    assert (complete.exit_code, complete.committed, complete.failed) == (0, 1, 0)
    assert not scan.path.exists() and digital.path.read_bytes() == digital_bytes
    assert sidecar.read_bytes() == new_image and cover.read_bytes() == new_image
    retained = _retained_groups(complete, state_dir)
    assert retained[scan.path] == scan_bytes
    backups = [member.backup_path for report in complete.plan_reports
               for member in StateStore(state_dir).list_members(report.operation_id)
               if member.destination == cover and member.backup_path is not None]
    assert len(backups) == 1 and backups[0].read_bytes() == old_image
