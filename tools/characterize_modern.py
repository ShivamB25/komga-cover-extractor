"""Compare modern pure parsing against the frozen dataset's three labeled fields."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from komga_cover_extractor.bootstrap import parsing_policy
from komga_cover_extractor.config import load_config
from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.parsing.filenames import parse_release


def original_exclusions(fixture: Path) -> frozenset[str]:
    tree = ast.parse((fixture / 'tests.py').read_text(encoding='utf-8'))
    validator = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'validate_csv')
    assignment = next(node for node in ast.walk(validator) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'skipped_files' for target in node.targets))
    value = ast.literal_eval(assignment.value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError('invalid frozen exclusion literal')
    return frozenset(value)


def compare_corpus(fixture: Path) -> dict[str, Any]:
    policy = parsing_policy(load_config(environ={}))
    exclusions = original_exclusions(fixture)
    counts: Counter[str] = Counter()
    mismatches: list[dict[str, object]] = []
    excluded_observations: list[dict[str, object]] = []
    corrections: list[dict[str, object]] = []
    dispositions: list[dict[str, object]] = []
    rows = eligible = 0
    with (fixture / 'manga_novel_dataset.csv').open(newline='', encoding='utf-8') as stream:
        for index, row in enumerate(csv.DictReader(stream), 1):
            rows += 1
            name = row['File Name']
            excluded = name in exclusions
            if not excluded:
                eligible += 1
            path = Path("/corpus") / name
            release = parse_release(FileObservation(path, root_role="download", at_configured_root=True), policy)
            raw = row['Release Number']
            expected = tuple(Decimal(token) for token in re.findall(r'\d+(?:\.\d+)?', raw))
            number = release.number
            if number.kind == "range" and len(expected) >= 2:
                number_matches = (
                    number.values == (min(expected), max(expected))
                    and all(number.contains(value) for value in expected)
                )
            else:
                number_matches = number.values == expected
            written_sets = (
                tuple(sequence.split("-"))
                for sequence in re.findall(
                    r"(?<![\d.])\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?){2,}",
                    Path(name).stem,
                )
            )
            corrected = number.kind == "set" and number.raw_tokens in written_sets and not excluded
            if corrected:
                counts["intentional_explicit_set"] += 1
                corrections.append({
                    "row": index, "file_name": name, "expected_label": raw,
                    "legacy_number_values": [str(value) for value in expected],
                    "legacy_compared_bounds": [str(min(expected)), str(max(expected))] if expected else [],
                    "written_tokens": list(number.raw_tokens),
                    "actual": number.to_dict(),
                    "rationale": "More than two written hyphen tokens retain exact membership instead of becoming a contiguous min/max range.",
                })
            differences = {}
            if not number_matches and not corrected:
                differences['Release Number'] = {'expected': [str(n) for n in expected], 'actual': release.number.to_dict()}
            for field, actual in (('Series Name', release.display_series), ('File Type', release.kind)):
                if row[field] != actual:
                    differences[field] = {'expected': row[field], 'actual': actual}
            if excluded:
                excluded_observations.append({'row': index, 'file_name': name, 'differences': differences})
            elif differences:
                counts.update(differences.keys())
                mismatches.append({'row': index, 'file_name': name, 'differences': differences})
            dispositions.append({
                "row": index,
                "status": "original_exclusion" if excluded else "mismatch" if differences else "intentional_correction" if corrected else "matched",
            })
    dataset = fixture / 'manga_novel_dataset.csv'
    return {'schema_version': 1, 'dataset_sha256': hashlib.sha256(dataset.read_bytes()).hexdigest(),
            'rows': rows, 'eligible_rows': eligible, 'original_excluded_rows': len(excluded_observations),
            'mismatch_counts': dict(counts), 'mismatches': mismatches,
            'original_excluded_observations': excluded_observations,
            "intentional_corrections": corrections, "row_dispositions": dispositions,
            'status': 'pass' if not mismatches else 'fail',
            'fields': ['Release Number', 'Series Name', 'File Type']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    fixture = Path(__file__).absolute().parent.parent / 'tests/fixtures/legacy'
    report = compare_corpus(fixture)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({key: value for key, value in report.items() if key not in {'mismatches', 'original_excluded_observations', 'row_dispositions', 'intentional_corrections'}}))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
