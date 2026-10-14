"""Pure metadata values shared by bounded archive observations and matching."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class MetadataResult:
    status: Literal['valid', 'absent', 'invalid']
    values: tuple[tuple[str, str], ...] = ()
    reason: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(self, 'values', tuple(tuple(pair) for pair in self.values))
        if self.status != 'valid' and self.values:
            raise ValueError('invalid or absent metadata cannot supply values')


@dataclass(frozen=True, slots=True)
class IdentifierResult:
    status: Literal['valid', 'absent', 'invalid']
    identifiers: tuple[tuple[str, str], ...] = ()
    reason: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(self, 'identifiers', tuple(tuple(pair) for pair in self.identifiers))
        if self.status != 'valid' and self.identifiers:
            raise ValueError('invalid or absent identifier data cannot supply matches')


def extract_identifiers(comment: str, *, max_bytes: int = 4_194_304) -> IdentifierResult:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('metadata budget must be positive')
    if len(comment) > max_bytes or len(comment.encode('utf-8')) > max_bytes:
        return IdentifierResult('invalid', reason='metadata_byte_limit')
    if '\x00' in comment:
        return IdentifierResult('invalid', reason='invalid_identifier_data')
    parts = re.split(r'\bIdentifiers:', comment, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 1:
        if re.search(r'\bidentifiers\b', comment, flags=re.IGNORECASE):
            return IdentifierResult('invalid', reason='malformed_identifier_marker')
        return IdentifierResult('absent', reason='no_identifier_marker')
    if re.search(r'\bIdentifiers:', parts[1], flags=re.IGNORECASE):
        return IdentifierResult('invalid', reason='duplicate_identifier_marker')
    values = tuple(value.strip() for value in parts[1].strip().split(','))
    if not values or any(not value for value in values):
        return IdentifierResult('invalid', reason='empty_identifier')
    return IdentifierResult('valid', tuple(('identifier', value) for value in values))
