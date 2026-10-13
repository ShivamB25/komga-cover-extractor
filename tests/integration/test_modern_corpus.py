import csv
import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from komga_cover_extractor.domain.releases import ReleaseNumber

spec = importlib.util.spec_from_file_location(
    "characterize_modern", Path(__file__).parents[2] / "tools/characterize_modern.py",
)
assert spec is not None and spec.loader is not None
corpus_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus_tool)


def write_corpus(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    (tmp_path / 'tests.py').write_text('def validate_csv():\n    skipped_files = []\n', encoding='utf-8')
    with (tmp_path / 'manga_novel_dataset.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(('File Name', 'Release Number', 'File Type', 'Series Name'))
        writer.writerows(rows)


def test_corpus_distinguishes_range_bounds_from_explicit_membership(tmp_path: Path) -> None:
    rows = [
        ('Story v01-03.cbz', '[1, 2, 3]', 'volume', 'Story'),
        ('Story v01-03.cbz', '[1, 3]', 'volume', 'Story'),
        ('Story v01-1.5-03.cbz', '[1, 1.5, 3]', 'volume', 'Story'),
        ('Story v01-1.5-03.cbz', '[1, 3]', 'volume', 'Story'),
    ]
    write_corpus(tmp_path, rows)
    report = corpus_tool.compare_corpus(tmp_path)
    assert report['status'] == 'pass'
    assert report['mismatches'] == []
    assert report['mismatch_counts'] == {'intentional_explicit_set': 2}
    assert [correction["row"] for correction in report["intentional_corrections"]] == [3, 4]
    for correction, label, values in zip(
        report["intentional_corrections"],
        ("[1, 1.5, 3]", "[1, 3]"),
        (["1", "1.5", "3"], ["1", "3"]),
        strict=True,
    ):
        assert correction["expected_label"] == label
        assert correction["legacy_number_values"] == values
        assert correction["legacy_compared_bounds"] == ["1", "3"]
        assert correction["written_tokens"] == ["01", "1.5", "03"]
        assert correction["actual"] == {
            "kind": "set", "values": ["1", "1.5", "3"], "raw_tokens": ["01", "1.5", "03"], "part": None,
        }
    assert report['row_dispositions'] == [
        {'row': 1, 'status': 'matched'}, {'row': 2, 'status': 'matched'},
        {'row': 3, 'status': 'intentional_correction'}, {'row': 4, 'status': 'intentional_correction'},
    ]


@pytest.mark.parametrize(
    ("name", "label", "legacy_values", "tokens", "actual_values"),
    [
        ("Story v01-03-05.cbz", "[1, 2, 3, 4, 5]", ["1", "2", "3", "4", "5"], ["01", "03", "05"], ["1", "3", "5"]),
        ("Story v01-03-05.cbz", "[1, 5]", ["1", "5"], ["01", "03", "05"], ["1", "3", "5"]),
        ("Story v01.01-01.10-03.00.cbz", "[1.01, 3]", ["1.01", "3"], ["01.01", "01.10", "03.00"], ["1.01", "1.10", "3.00"]),
    ],
)
def test_written_set_correction_retains_legacy_label_and_exact_tokens(
    tmp_path: Path, name: str, label: str, legacy_values: list[str],
    tokens: list[str], actual_values: list[str],
) -> None:
    write_corpus(tmp_path, [(name, label, "volume", "Story")])
    report = corpus_tool.compare_corpus(tmp_path)
    assert report["status"] == "pass"
    assert report["mismatches"] == []
    assert report["row_dispositions"] == [{"row": 1, "status": "intentional_correction"}]
    correction, = report["intentional_corrections"]
    assert correction["file_name"] == name
    assert correction["expected_label"] == label
    assert correction["legacy_number_values"] == legacy_values
    assert correction["written_tokens"] == tokens
    assert correction["actual"] == {
        "kind": "set", "values": actual_values, "raw_tokens": tokens, "part": None,
    }


def test_corrections_do_not_hide_unrelated_number_series_or_type_mismatches(tmp_path: Path) -> None:
    write_corpus(tmp_path, [
        ("Story v02.cbz", "1", "volume", "Story"),
        ("Story v01-03.cbz", "[1, 4]", "volume", "Story"),
        ("Story v03-01.cbz", "[1, 3]", "volume", "Story"),
        ("Story v01-03-05.cbz", "[1, 5]", "volume", "Other"),
        ("Story v01-03-05.cbz", "[1, 5]", "chapter", "Story"),
    ])
    report = corpus_tool.compare_corpus(tmp_path)
    assert report["status"] == "fail"
    assert report["mismatch_counts"] == {
        "Release Number": 3, "Series Name": 1, "File Type": 1, "intentional_explicit_set": 2,
    }
    assert report["row_dispositions"] == [
        {"row": 1, "status": "mismatch"}, {"row": 2, "status": "mismatch"},
        {"row": 3, "status": "mismatch"}, {"row": 4, "status": "mismatch"},
        {"row": 5, "status": "mismatch"},
    ]
    assert report["mismatches"] == [
        {"row": 1, "file_name": "Story v02.cbz", "differences": {"Release Number": {
            "expected": ["1"], "actual": {"kind": "scalar", "values": ["2"], "raw_tokens": ["02"], "part": None},
        }}},
        {"row": 2, "file_name": "Story v01-03.cbz", "differences": {"Release Number": {
            "expected": ["1", "4"], "actual": {"kind": "range", "values": ["1", "3"], "raw_tokens": ["01", "03"], "part": None},
        }}},
        {"row": 3, "file_name": "Story v03-01.cbz", "differences": {"Release Number": {
            "expected": ["1", "3"], "actual": {"kind": "set", "values": ["3", "1"], "raw_tokens": ["03", "01"], "part": None},
        }}},
        {"row": 4, "file_name": "Story v01-03-05.cbz", "differences": {
            "Series Name": {"expected": "Other", "actual": "Story"},
        }},
        {"row": 5, "file_name": "Story v01-03-05.cbz", "differences": {
            "File Type": {"expected": "chapter", "actual": "volume"},
        }},
    ]
    assert [correction["row"] for correction in report["intentional_corrections"]] == [4, 5]


@pytest.mark.parametrize(
    ("wrong_tokens", "actual_values"),
    [
        (("01", "03", "04"), ["1", "3", "4"]),
        (("01", "03", "05"), ["1", "3", "5"]),
    ],
)
def test_wrong_or_incomplete_written_members_are_not_intentional_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_tokens: tuple[str, ...],
    actual_values: list[str],
) -> None:
    write_corpus(tmp_path, [("Story v01-03-05-07.cbz", "[1, 7]", "volume", "Story")])
    parse_release = corpus_tool.parse_release

    def wrong_number(observation, policy):
        return replace(parse_release(observation, policy), number=ReleaseNumber.explicit_set(wrong_tokens))

    monkeypatch.setattr(corpus_tool, "parse_release", wrong_number)
    report = corpus_tool.compare_corpus(tmp_path)
    assert report["status"] == "fail"
    assert report["intentional_corrections"] == []
    assert report["row_dispositions"] == [{"row": 1, "status": "mismatch"}]
    mismatch, = report["mismatches"]
    assert mismatch["differences"] == {"Release Number": {
        "expected": ["1", "7"],
        "actual": {"kind": "set", "values": actual_values, "raw_tokens": list(wrong_tokens), "part": None},
    }}


def test_original_exclusion_keeps_observed_difference_without_reclassification(tmp_path: Path) -> None:
    write_corpus(tmp_path, [("Story v01-03-05.cbz", "[1, 5]", "volume", "Story")])
    (tmp_path / "tests.py").write_text(
        "def validate_csv():\n    skipped_files = ['Story v01-03-05.cbz']\n", encoding="utf-8",
    )
    report = corpus_tool.compare_corpus(tmp_path)
    assert report["status"] == "pass"
    assert report["eligible_rows"] == 0
    assert report["intentional_corrections"] == []
    assert report["row_dispositions"] == [{"row": 1, "status": "original_exclusion"}]
    assert report["original_excluded_observations"] == [{
        "row": 1, "file_name": "Story v01-03-05.cbz", "differences": {"Release Number": {
            "expected": ["1", "5"],
            "actual": {"kind": "set", "values": ["1", "3", "5"], "raw_tokens": ["01", "03", "05"], "part": None},
        }},
    }]
