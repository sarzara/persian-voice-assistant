import pytest
from pydantic import ValidationError

from app.config import Settings


def test_allowed_user_ids_parsed_from_comma_list(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", " 111, 222 ,,333 ")
    assert Settings(_env_file=None).allowed_user_ids == frozenset({111, 222, 333})


def test_empty_allowlist_means_nobody(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    assert Settings(_env_file=None).allowed_user_ids == frozenset()


def test_invalid_allowlist_rejected(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "111,@sahar")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_database_url_escapes_special_characters(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    url = Settings(_env_file=None).database_url()
    assert url.startswith("postgresql+psycopg://")
    assert "p%40ss%3Aw%2Frd%231@db:5432" in url


def test_secrets_not_exposed_in_repr(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:very-secret")
    monkeypatch.setenv("RESEND_API_KEY", "re_very_secret")
    rendered = repr(Settings(_env_file=None))
    assert "very-secret" not in rendered and "re_very_secret" not in rendered


def test_env_example_lists_every_setting():
    """Guard: adding a setting without documenting it in .env.example fails CI."""
    import re
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", example.read_text(encoding="utf-8"), re.M))
    required = {(f.alias or name).upper() for name, f in Settings.model_fields.items()}
    assert required <= documented, f"undocumented: {sorted(required - documented)}"
