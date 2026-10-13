from __future__ import annotations

from pathlib import Path

from komga_cover_extractor.application.planning import build_file_plan, project_release
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.libraries import LibraryRoot
from komga_cover_extractor.domain.operations import ArtifactSpec
from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release


def test_projection_changes_name_and_role_but_keeps_the_observed_bytes(tmp_path: Path) -> None:
    downloads, library = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    book = downloads / "Series Volume 1.cbz"
    book.write_bytes(b"observed archive bytes")
    source = fingerprint(book, downloads)
    release = parse_release(FileObservation(book, fingerprint=source, root_role="download"), ParsingPolicy())
    target = library / "Series" / "Series v01.cbz"

    projected = project_release(release, target, parsing=ParsingPolicy(),
                                roots=(LibraryRoot(downloads, roles=("download",)), LibraryRoot(library)))

    assert projected.name == "Series v01.cbz"
    assert projected.display_series == "Series"
    assert projected.number.raw_tokens == ("01",)
    assert projected.observation.root_role == "library"
    assert projected.observation.fingerprint is source
    assert projected.observation.fingerprint.path == book
    assert release.path == book
    assert book.read_bytes() == b"observed archive bytes"
    assert not target.exists()


def test_parent_directories_are_explicit_ordered_dependencies(tmp_path: Path) -> None:
    book = tmp_path / "book.cbz"
    book.write_bytes(b"book")
    source = fingerprint(book, tmp_path)
    target = tmp_path / "Series" / "nested" / "book.cbz"

    result = build_file_plan({book: source}, {book: target}, {}, roots=(LibraryRoot(tmp_path),),
                             directories=(tmp_path,), policy_version="test")

    assert result.exit_code == 0
    assert [(member.kind, member.destination) for member in result.plans[0].operations] == [
        ("mkdir", tmp_path / "Series"), ("mkdir", tmp_path / "Series" / "nested"), ("move", target),
    ]
    assert result.plans[0].operations[1].depends_on == ("mkdir-0",)
    assert result.plans[0].operations[2].depends_on == ("mkdir-0", "mkdir-1")
    assert result.plans[0].operations[2].source == source
    assert not target.parent.exists()


def test_different_sources_cannot_converge_on_one_output(tmp_path: Path) -> None:
    one, two, target = tmp_path / "one.cbz", tmp_path / "two.cbz", tmp_path / "target.cbz"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    result = build_file_plan({one: fingerprint(one, tmp_path), two: fingerprint(two, tmp_path)},
                             {one: target, two: target}, {}, roots=(LibraryRoot(tmp_path),),
                             directories=(tmp_path,), policy_version="test")
    assert [(decision.status, decision.reason, decision.path) for decision in result.decisions] == [
        ("conflict", "multiple_planned_outputs", target),
    ]
    assert result.plans == ()
    assert (one.read_bytes(), two.read_bytes()) == (b"one", b"two")


def test_component_containment_does_not_authorize_a_similar_prefix(tmp_path: Path) -> None:
    root = tmp_path / "books"
    root.mkdir()
    book = root / "book.cbz"
    book.write_bytes(b"original")
    target = tmp_path / "books-old" / "book.cbz"
    result = build_file_plan({book: fingerprint(book, root)}, {book: target}, {},
                             roots=(LibraryRoot(root),), directories=(root,), policy_version="test")
    assert [(decision.status, decision.reason) for decision in result.decisions] == [
        ("permission_denied", "destination_outside_selected_roots"),
    ]
    assert book.read_bytes() == b"original"
    assert not target.parent.exists()


def test_observed_directory_content_prevents_cover_only_or_hidden_file_cleanup(tmp_path: Path) -> None:
    folder = tmp_path / "Series"
    folder.mkdir()
    book, cover, hidden = folder / "book.cbz", folder / "cover.jpg", folder / ".notes"
    book.write_bytes(b"book")
    cover.write_bytes(b"custom art")
    hidden.write_bytes(b"hidden user notes")
    files = {path: fingerprint(path, tmp_path) for path in (book, cover, hidden)}

    result = build_file_plan(files, {}, {book: "duplicate"}, roots=(LibraryRoot(tmp_path),),
                             directories=(tmp_path, folder), policy_version="test")

    assert [(member.kind, member.destination) for member in result.plans[0].operations] == [("quarantine", book)]
    assert cover.read_bytes() == b"custom art"
    assert hidden.read_bytes() == b"hidden user notes"


def test_two_file_cycle_uses_staged_observed_replacements(tmp_path: Path) -> None:
    one, two = tmp_path / "one.cbz", tmp_path / "two.cbz"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    files = {path: fingerprint(path, tmp_path) for path in (one, two)}
    result = build_file_plan(files, {one: two, two: one}, {}, roots=(LibraryRoot(tmp_path),),
                             directories=(tmp_path,), policy_version="cycle")
    assert [(member.source.path, member.destination, member.collision, member.destination_before) for member in result.plans[0].operations] == [
        (one, two, "replace", files[two]), (two, one, "replace", files[one]),
    ]
    applied = TransactionExecutor(tmp_path.parent / (tmp_path.name + "-state"), (tmp_path,)).apply(
        result.plans[0], authorization=RunAuthorization(apply=True),
    )
    assert applied.committed == 1
    assert (one.read_bytes(), two.read_bytes()) == (b"two", b"one")


def test_conversion_artifact_replaces_an_intermediate_name_without_reading_it(tmp_path: Path) -> None:
    root, scratch = tmp_path / "library", tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    original, prepared = root / "Series Volume 1.cbr", scratch / "converted.cbz"
    original.write_bytes(b"original unconverted bytes")
    prepared.write_bytes(b"verified prepared conversion bytes")
    source, artifact = fingerprint(original, root), fingerprint(prepared, scratch)
    target = root / "Series" / "Series v01.cbz"

    result = build_file_plan({original: source}, {original: target}, {}, roots=(LibraryRoot(root),),
                             directories=(root,), source_artifacts={original: ArtifactSpec(source=artifact)},
                             policy_version="conversion-composition")

    operations = result.plans[0].operations
    assert [(member.kind, member.destination) for member in operations] == [
        ("mkdir", root / "Series"), ("publish", target), ("quarantine", original),
    ]
    assert operations[1].source == source
    assert operations[1].artifact.source == artifact
    assert operations[2].depends_on == ("mkdir-0", "converted-book-0")
    assert [(permission.path, permission.write) for permission in result.plans[0].roots] == [(root, True), (scratch, False)]
    assert not target.exists()
    assert original.read_bytes() == b"original unconverted bytes"


def test_source_change_refuses_before_state_or_destination_creation(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "book.cbz"
    source.write_bytes(b"old")
    result = build_file_plan({source: fingerprint(source, root)}, {source: root / "renamed.cbz"}, {},
                             roots=(LibraryRoot(root),), directories=(root,), policy_version="test")
    source.write_bytes(b"new")
    state = tmp_path / "state"

    applied = TransactionExecutor(state, (root,)).apply(result.plans[0], authorization=RunAuthorization(apply=True))

    assert applied.decisions[0].status == "stale_plan"
    assert source.read_bytes() == b"new"
    assert not (root / "renamed.cbz").exists()
    assert not state.exists()
