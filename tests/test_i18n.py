import pytest

from app.i18n import detect_text_language, resolve_language


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("فردا ساعت ده با علی جلسه بگذار", "fa"),
        ("Schedule a meeting with Ali tomorrow at 10", "en"),
        ("meeting با Ali فردا ساعت 11 بذار", "fa"),   # mixed: Persian grammar
        ("email Sara: گزارش آماده است", "fa"),
        ("send an email to Sara", "en"),
        ("12:30", "fa"),                                # no letters: default
        ("", "fa"),
    ],
)
def test_detect_text_language(text, expected):
    assert detect_text_language(text) == expected


def test_resolve_language():
    assert resolve_language("en", "فردا جلسه") == "en"     # a fixed setting wins
    assert resolve_language("fa", "meeting tomorrow") == "fa"
    assert resolve_language("auto", "meeting tomorrow") == "en"
    assert resolve_language("auto", None) == "fa"