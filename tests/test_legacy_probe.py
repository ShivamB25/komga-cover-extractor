from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

_PROBE_PATH = Path(__file__).parents[1] / "tools" / "legacy_probe.py"


@pytest.fixture
def probe_module() -> Any:
    before = set(sys.modules)
    module_name = f"legacy_probe_test_module_{id(before)}"
    spec = importlib.util.spec_from_file_location(module_name, _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    added = set(sys.modules) - before
    assert "settings" not in added
    assert "komga_cover_extractor" not in added
    return module


def test_safe_settings_accepts_literals_and_reviewed_keyword(probe_module: Any) -> None:
    report = probe_module.parse_safe_settings(
        dedent(
            """
            enabled = True
            names = ["one", "two"]
            ranked_keywords = [Keyword(r"Digital", 1), Keyword("raw", -2, "chapter")]
            """
        )
    )

    assert report.ok
    assert report.values["enabled"] is True
    assert report.values["names"] == ["one", "two"]
    keywords = report.values["ranked_keywords"]
    assert isinstance(keywords, list)
    assert [(item.name, item.score, item.file_type) for item in keywords] == [
        ("Digital", 1, "both"),
        ("raw", -2, "chapter"),
    ]


def test_safe_settings_rejects_dynamic_expression_without_side_effect(
    probe_module: Any, tmp_path: Path
) -> None:
    sentinel = tmp_path / "settings-side-effect"
    source = f"danger = __import__('pathlib').Path({str(sentinel)!r}).write_text('executed')\n"

    report = probe_module.parse_safe_settings(source)

    assert not report.ok
    assert "danger" not in report.values
    assert any(issue.name == "danger" for issue in report.rejected)
    assert not sentinel.exists()


def test_safe_settings_rejects_unknown_keyword_expression_without_fabrication(
    probe_module: Any, tmp_path: Path
) -> None:
    sentinel = tmp_path / "keyword-side-effect"
    source = f"ranked_keywords = [Keyword(Path({str(sentinel)!r}).write_text('executed'), 1)]\n"

    report = probe_module.parse_safe_settings(source)

    assert not report.ok
    assert "ranked_keywords" not in report.values
    assert any(issue.name == "ranked_keywords" for issue in report.rejected)
    assert not sentinel.exists()


def test_safe_settings_rejects_unreviewed_keyword_class(probe_module: Any) -> None:
    report = probe_module.parse_safe_settings(
        "class Keyword(Base):\n    pass\nranked_keywords = []\n"
    )

    assert not report.ok
    assert "ranked_keywords" in report.values
    assert any(issue.name == "Keyword" for issue in report.rejected)
