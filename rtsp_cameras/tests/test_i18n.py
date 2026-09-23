"""Tests for the interface and integration translations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, Settings
from app.i18n import (
    Translations,
    detect_language,
    normalize_language,
)
from app.i18n import _first_accept_language as first_accept_language
from app.main import API_ERRORS

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
LOCALES_DIR = TESTS_DIR.parent / "app" / "locales"
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "rtsp_cameras"


def flatten(bundle: dict, prefix: str = "") -> set[str]:
    """Return every dotted leaf key of a nested translation bundle."""
    keys: set[str] = set()
    for key, value in bundle.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= flatten(value, f"{path}.")
        else:
            keys.add(path)
    return keys


def read_json(path: Path) -> dict:
    """Read a JSON document from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bundles() -> dict[str, dict]:
    """All interface bundles keyed by language."""
    return {
        code: read_json(LOCALES_DIR / f"{code}.json") for code in SUPPORTED_LANGUAGES
    }


def test_every_interface_language_has_the_same_keys(bundles: dict[str, dict]) -> None:
    reference = flatten(bundles[DEFAULT_LANGUAGE])
    assert len(reference) > 80
    for code, bundle in bundles.items():
        assert flatten(bundle) == reference, f"{code} differs from {DEFAULT_LANGUAGE}"


def test_every_api_error_code_is_translated(bundles: dict[str, dict]) -> None:
    for code in API_ERRORS:
        for language, bundle in bundles.items():
            assert code in bundle["errors"], f"{language} misses errors.{code}"


def test_integration_translations_are_complete() -> None:
    strings = read_json(INTEGRATION_DIR / "strings.json")
    reference = flatten(strings)
    assert reference
    for code in SUPPORTED_LANGUAGES:
        data = read_json(INTEGRATION_DIR / "translations" / f"{code}.json")
        assert flatten(data) == reference, f"integration translation {code} is incomplete"


def test_translate_falls_back_to_english() -> None:
    translations = Translations(LOCALES_DIR)
    assert translations.translate("app.title", "pl") == "RTSP Camera Manager"
    assert translations.translate("app.title", "fr") == "RTSP Camera Manager"
    assert translations.translate("missing.key", "de") == "missing.key"
    assert translations.translate("camera.delete_confirm", "pl", name="Brama") == (
        "Usunąć kamerę „Brama”?"
    )


def test_error_helper_has_a_generic_fallback() -> None:
    translations = Translations(LOCALES_DIR)
    assert translations.error("does_not_exist", "en") == translations.translate(
        "errors.generic", "en"
    )
    assert translations.error("not_found", "pl") == "Nie znaleziono kamery"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("pl-PL", "pl"),
        ("de_AT", "de"),
        ("es", "es"),
        ("EN", "en"),
        ("auto", None),
        ("fr", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_language(value: str | None, expected: str | None) -> None:
    assert normalize_language(value) == expected


def test_accept_language_picks_the_best_supported_entry() -> None:
    assert first_accept_language("fr-FR,de;q=0.8,en;q=0.5") == "de"
    assert first_accept_language("pl-PL,pl;q=0.9") == "pl"
    assert first_accept_language("fr-CH") is None
    assert first_accept_language("") is None
    assert first_accept_language(None) is None


class FakeClient:
    """Minimal stand-in for the Home Assistant client."""

    def __init__(self, language: str | None, enabled: bool = True) -> None:
        self.language = language
        self.enabled = enabled

    async def async_language(self) -> str | None:
        """Return the configured language."""
        return self.language


def test_detect_language_priority(settings: Settings) -> None:
    assert (
        asyncio.run(
            detect_language(settings, FakeClient("de"), accept_language="pl", requested="es")
        )
        == "es"
    )

    settings.language = "pl"
    assert asyncio.run(detect_language(settings, FakeClient("de"), accept_language="en")) == "pl"

    settings.language = "auto"
    assert asyncio.run(detect_language(settings, FakeClient("de"), accept_language="pl")) == "de"

    assert (
        asyncio.run(detect_language(settings, FakeClient(None), accept_language="pl,en;q=0.5"))
        == "pl"
    )
    assert asyncio.run(detect_language(settings, FakeClient(None), accept_language="fr")) == "en"
    assert asyncio.run(detect_language(settings, FakeClient("de", enabled=False))) == "en"
