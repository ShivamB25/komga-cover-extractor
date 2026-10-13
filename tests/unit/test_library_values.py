from pathlib import Path

import pytest

from komga_cover_extractor.domain.libraries import (
    LibraryRoot,
    LibrarySnapshot,
    PathMapping,
    is_within_root,
    map_local_path,
    normalize_library_roots,
    normalize_path_mappings,
)


def test_containment_uses_components_not_string_prefix() -> None:
    books = LibraryRoot(Path("/books"))

    assert books.contains(Path("/books"))
    assert books.contains(Path("/books/series/one.cbz"))
    assert not books.contains(Path("/books-old/series/one.cbz"))
    assert not is_within_root(Path("/bookstore"), Path("/books"))


def test_roots_are_absolute_lexical_and_do_not_resolve_symlinks(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem resolution must not occur")

    monkeypatch.setattr(Path, "resolve", forbidden)
    root = LibraryRoot(Path("/media/../books/link"))

    assert root.path == Path("/books/link")
    assert root.path.is_absolute()


def test_identical_roots_merge_roles_in_first_seen_order() -> None:
    normalized = normalize_library_roots(
        (
            LibraryRoot(Path("/books"), extensions=(".cbz",), roles=("library",)),
            LibraryRoot(Path("/books/../books"), extensions=(".cbz",), roles=("downloads",)),
        )
    )

    assert normalized == (
        LibraryRoot(Path("/books"), extensions=(".cbz",), roles=("library", "downloads")),
    )


def test_duplicate_root_with_conflicting_settings_is_rejected() -> None:
    with pytest.raises(ValueError, match="conflicting settings"):
        normalize_library_roots(
            (
                LibraryRoot(Path("/books"), formats=("volume",)),
                LibraryRoot(Path("/books"), formats=("chapter",)),
            )
        )


def test_distinct_nested_roots_are_rejected() -> None:
    with pytest.raises(ValueError, match="nested library roots"):
        normalize_library_roots((LibraryRoot(Path("/books")), LibraryRoot(Path("/books/sub"))))


def test_path_mapping_chooses_longest_component_prefix() -> None:
    mappings = (
        PathMapping(Path("/data"), "/remote/library"),
        PathMapping(Path("/data/books"), "/remote/books"),
    )

    assert map_local_path(Path("/data/books/series/book.cbz"), mappings) == (
        "/remote/books/series/book.cbz"
    )
    assert map_local_path(Path("/data/books-old/book.cbz"), mappings) == (
        "/remote/library/books-old/book.cbz"
    )
    assert map_local_path(Path("/other/book.cbz"), mappings) is None


def test_conflicting_duplicate_mappings_are_rejected() -> None:
    with pytest.raises(ValueError, match="conflicting mappings"):
        normalize_path_mappings(
            (
                PathMapping(Path("/books"), "/library"),
                PathMapping(Path("/books/./"), "/other"),
            )
        )


def test_identical_duplicate_mappings_are_coalesced() -> None:
    mappings = normalize_path_mappings(
        (
            PathMapping(Path("/books"), "/library"),
            PathMapping(Path("/books"), "/library"),
        )
    )

    assert len(mappings) == 1


def test_remote_paths_use_posix_join_and_keep_drive_prefixes() -> None:
    unix_remote = PathMapping(Path("/books"), "/srv/komga/books")
    drive_remote = PathMapping(Path("/books"), "C:\\Komga\\Books")

    assert unix_remote.map_path(Path("/books/Series/Book.cbz")) == "/srv/komga/books/Series/Book.cbz"
    assert drive_remote.remote_root == "C:/Komga/Books"
    assert drive_remote.map_path(Path("/books/Series/Book.cbz")) == "C:/Komga/Books/Series/Book.cbz"


def test_value_collections_are_immutable_tuple_inputs() -> None:
    with pytest.raises(TypeError, match="tuple of strings"):
        LibraryRoot(Path("/books"), extensions=[".cbz"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LibrarySnapshot(roots=[LibraryRoot(Path("/books"))])  # type: ignore[arg-type]
