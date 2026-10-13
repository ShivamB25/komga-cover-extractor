from difflib import SequenceMatcher

from komga_cover_extractor.parsing.normalization import (
    clean_str,
    contains_brackets,
    contains_punctuation,
    contains_unicode,
    convert_to_ascii,
    ends_with_bracket,
    normalize_str,
    parse_words,
    remove_brackets,
    remove_dual_space,
    remove_punctuation,
    replace_underscores,
    similar,
    starts_with_bracket,
)


def test_legacy_normalization_examples_are_preserved() -> None:
    assert remove_dual_space("test  test") == "test test"
    assert normalize_str("The Sword Saint") == "Sword Saint"
    assert clean_str("The, Sword, Saint!!!!") == "sword saint"


def test_skipping_type_keywords_does_not_depend_on_local_initialization() -> None:
    assert normalize_str("The Sword", skip_type_keywords=True) == "Sword"
    assert normalize_str(
        "The Sword",
        skip_type_keywords=True,
        skip_editions=True,
        skip_japanese_particles=True,
        skip_misc_words=True,
        skip_storefront_keywords=True,
    ) == "Sword"


def test_normalize_str_preserves_type_keyword_at_series_start() -> None:
    assert normalize_str("Book Girl") == "Book Girl"
    assert normalize_str("The Book Girl") == "Book Girl"
    assert normalize_str("A", skip_common_words=False) == "A"


def test_clean_str_applies_legacy_step_order_and_skip_behavior() -> None:
    assert clean_str("THE: Café (Deluxe) v01_02!") == "cafe v01.02"
    assert clean_str("Title (2018)") == "title"
    assert clean_str("Title (2018)", skip_bracket=True) == "title 2018"
    assert clean_str("The Sword", skip_normalize=True) == "the sword"
    assert clean_str("word!", skip_punctuation=True) == "word!"
    assert clean_str("cats", skip_remove_s=True) == "cats"
    assert clean_str("cats") == "cat"
    assert clean_str(
        "a:b",
        skip_colon_replace=True,
        skip_normalize=True,
        skip_punctuation=True,
    ) == "a:b"
    assert clean_str("  UPPER  ", skip_lowercase_convert=True) == "UPPER"


def test_transliteration_and_ascii_discard_are_distinct() -> None:
    display_text = "Crème brûlée"

    assert parse_words(display_text) == ["creme", "brulee"]
    assert display_text == "Crème brûlée"
    assert clean_str("café", skip_unidecode=True) == "caf"
    assert clean_str(
        "café", skip_unidecode=True, skip_convert_to_ascii=True
    ) == "café"
    assert convert_to_ascii("café 東京") == "caf "
    assert contains_unicode("plain") is False
    assert contains_unicode("café") is True


def test_parse_words_removes_ascii_punctuation_before_transliterating() -> None:
    assert parse_words("") == []
    assert parse_words("  Hello, WORLD!  ") == ["hello", "world"]
    assert parse_words("Crème-brûlée") == ["cremebrulee"]


def test_bracket_helpers_and_legacy_bracket_removal_rules() -> None:
    assert starts_with_bracket("[(OSHI NO KO)]")
    assert ends_with_bracket("[(OSHI NO KO)]")
    assert contains_brackets("title {publisher}")
    assert not contains_brackets("plain title")

    assert remove_brackets("test (test)") == "test (test)"
    assert remove_brackets("test [test]") == "test [test]"
    assert remove_brackets("test {test}") == "test {test}"
    assert remove_brackets("test (test).cbz") == "test.cbz"
    assert (
        remove_brackets("rent-a-(really shy!)-girlfriend (2009)")
        == "rent-a-(really shy!)-girlfriend"
    )
    assert remove_brackets("[(OSHI NO KO)]") == "[(OSHI NO KO)]"
    assert remove_brackets("[(OSHI NO KO)] v01 (2018).cbz") == "[(OSHI NO KO)] v01.cbz"


def test_whitespace_punctuation_and_underscore_helpers_keep_legacy_rules() -> None:
    assert remove_dual_space("test  test") == "test test"
    assert remove_dual_space("test\t\ttest") == "test\t\ttest"
    assert contains_punctuation("hello!")
    assert not contains_punctuation("hello + world")
    assert remove_punctuation(" hello,+world! ") == "hello +world"
    assert replace_underscores("test_test") == "test test"
    assert replace_underscores("test_test_test") == "test test test"
    assert replace_underscores("Series_v01_02_extra") == "Series v01.02 extra"


def test_similar_uses_exact_ordered_sequence_matcher_ratio_and_boundaries() -> None:
    assert similar("", "") == 0.0
    assert similar("  ", "text") == 0.0
    assert similar("  The Sword  ", "the sword") == 1.0

    forward = similar(" tide ", " DIET ")
    reverse = similar("diet", "tide")
    assert forward == SequenceMatcher(None, "tide", "diet").ratio() == 0.25
    assert reverse == SequenceMatcher(None, "diet", "tide").ratio() == 0.5
    assert similar("book", "books") == SequenceMatcher(None, "book", "books").ratio()
    assert similar("book", "books") >= 0.88
    assert similar("book", "books") < 0.90
