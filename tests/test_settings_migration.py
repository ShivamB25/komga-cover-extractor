from __future__ import annotations

from pathlib import Path

import pytest

from komga_cover_extractor.legacy_settings import MigrationError, migrate_settings

REVIEWED_KEYWORD = '''
class Keyword:
    def __init__(self, name, score, file_type="both"):
        self.name = name
        self.score = score
        self.file_type = file_type
    def __str__(self):
        return f"Name: {self.name}, Score: {self.score}, File Type: {self.file_type}"
    def __repr__(self):
        return str(self)
'''




def test_keyword_constructor_translation_supports_positional_and_named_fields() -> None:
    migrated = migrate_settings(
        REVIEWED_KEYWORD
        + "ranked_keywords = ["
        + "Keyword(r'volume', 3),"
        + "Keyword(name=r'chapter', score=-2, file_type='chapter'),"
        + "Keyword(name=r'named default', score=4),"
        + "Keyword('bonus', score=1.5, file_type='volume'),"
        + "]\n"
    )
    assert migrated["ranked_keywords"] == [
        {"name": "volume", "score": 3, "file_type": "both"},
        {"name": "chapter", "score": -2, "file_type": "chapter"},
        {"name": "named default", "score": 4, "file_type": "both"},
        {"name": "bonus", "score": 1.5, "file_type": "volume"},
    ]


@pytest.mark.parametrize(
    "expression",
    [
        "Keyword('x', 1, extra='chapter')",
        "Keyword('x', 1, 'chapter', 'unexpected')",
        "Keyword('x', 1, file_type='other')",
        "Keyword('x', name='duplicate', score=1)",
        "Keyword('x', *[1])",
        "Keyword('x', **{'score': 1})",
        "other('x', 1)",
    ],
)
def test_rejects_unreviewed_keyword_constructor_shapes(expression: str) -> None:
    with pytest.raises(MigrationError) as raised:
        migrate_settings(REVIEWED_KEYWORD + f"ranked_keywords = [{expression}]\n")

    assert raised.value.line > 0
    assert "ranked_keywords" in str(raised.value)


def test_rejects_dynamic_code_without_running_sentinel(tmp_path: Path) -> None:
    sentinel = tmp_path / "should-not-exist"
    sources = [
        f"open({str(sentinel)!r}, 'w').write('executed')\n",
        f"import pathlib\npathlib.Path({str(sentinel)!r}).write_text('executed')\n",
        f"def write_sentinel():\n    open({str(sentinel)!r}, 'w').write('executed')\n",
        f"@open({str(sentinel)!r}, 'w')\ndef decorated():\n    pass\n",
        f"value = [open({str(sentinel)!r}, 'w') for _ in [1]]\n",
    ]

    for source in sources:
        with pytest.raises(MigrationError):
            migrate_settings(source)
        assert not sentinel.exists()


def test_rejects_arbitrary_keyword_class_without_running_body(tmp_path: Path) -> None:
    sentinel = tmp_path / "keyword-body-sentinel"
    source = f"""
class Keyword:
    def __init__(self, name, score, file_type="both"):
        open({str(sentinel)!r}, "w").write("executed")
        self.name = name
        self.score = score
        self.file_type = file_type
    def __str__(self):
        return self.name
    def __repr__(self):
        return str(self)
ranked_keywords = []
"""

    with pytest.raises(MigrationError) as raised:
        migrate_settings(source)

    assert "Keyword" in str(raised.value)
    assert not sentinel.exists()


def test_alias_is_canonicalized_and_same_source_conflict_is_rejected() -> None:
    assert migrate_settings("resturcture_when_renaming = True\n") == {
        "restructure_when_renaming": True
    }

    with pytest.raises(MigrationError, match="both occur"):
        migrate_settings(
            "resturcture_when_renaming = True\nrestructure_when_renaming = False\n"
        )


def test_rejects_unknown_names_and_secret_redacts_errors() -> None:
    with pytest.raises(MigrationError) as unknown:
        migrate_settings("not_a_setting = 1\n")
    assert "not_a_setting" in str(unknown.value)

    secret = "super-secret-webhook-token"
    with pytest.raises(MigrationError) as dynamic:
        migrate_settings(f"komga_login_password = get_password({secret!r})\n")
    assert "komga_login_password" in str(dynamic.value)
    assert secret not in str(dynamic.value)


def test_rejects_malformed_deep_and_unhashable_literals_as_migration_errors() -> None:
    with pytest.raises(MigrationError) as malformed:
        migrate_settings("exception_keywords = [\n")
    assert malformed.value.line > 0

    nested = "[" * 40 + "0" + "]" * 40
    with pytest.raises(MigrationError, match="nesting"):
        migrate_settings(f"exception_keywords = {nested}\n")

    with pytest.raises(MigrationError, match="dictionary keys"):
        migrate_settings("ignored_folder_names = {{[]: 1}}\n")


def test_source_size_is_bounded_before_parsing() -> None:
    with pytest.raises(MigrationError, match="1 MiB"):
        migrate_settings("#" * (1_048_576 + 1))
