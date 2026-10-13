from pathlib import Path

import pytest

from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.parsing.metadata import extract_identifiers


@pytest.mark.parametrize('marker', ['Identifiers:', 'identifiers:', 'IDENTIFIERS:'])
def test_identifier_marker_is_case_insensitive_and_preserves_individual_values(marker: str) -> None:
    result = extract_identifiers(f'Description\n{marker} Anilist:123, ISBN:9780000000000')
    assert result.status == 'valid'
    assert result.identifiers == (('identifier', 'Anilist:123'), ('identifier', 'ISBN:9780000000000'))


@pytest.mark.parametrize('comment', ['Identifiers missing', 'Identifiers:', 'Identifiers:a,,b', 'Identifiers:a Identifiers:b', 'Identifiers:a\x00'])
def test_malformed_identifier_data_cannot_supply_matches(comment: str) -> None:
    result = extract_identifiers(comment)
    assert result.status == 'invalid' and result.identifiers == ()


def test_absent_identifiers_are_distinct_from_malformed_metadata() -> None:
    result = extract_identifiers('Plain archive comment')
    assert result.status == 'absent' and result.identifiers == ()


def test_metadata_byte_limit_counts_utf8_bytes_at_boundary() -> None:
    text = 'Identifiers:é'
    limit = len(text.encode('utf-8'))
    assert extract_identifiers(text, max_bytes=limit).identifiers == (('identifier', 'é'),)
    rejected = extract_identifiers(text, max_bytes=limit - 1)
    assert rejected.status == 'invalid' and rejected.reason == 'metadata_byte_limit'


def test_root_role_and_placement_are_independent_observations() -> None:
    direct = FileObservation(Path('/books/book.cbz'), root_role='both', at_configured_root=True)
    nested = FileObservation(Path('/books/Series/book.cbz'), root_role='both', at_configured_root=False)
    assert direct.root_role == nested.root_role == 'both'
    assert direct.at_configured_root and not nested.at_configured_root
    with pytest.raises(TypeError):
        FileObservation(Path('/books/book.cbz'), fingerprint='invented')
    with pytest.raises(ValueError):
        FileObservation(Path('/books/book.cbz'), root_role='download_root')
