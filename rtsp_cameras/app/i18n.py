"""Translations for the add-on web interface."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import DEFAULT_LANGUAGE, LANGUAGE_AUTO, SUPPORTED_LANGUAGES, Settings
from .ha import HomeAssistantClient

_LOGGER = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).with_name("locales")


def normalize_language(value: Any, default: str | None = None) -> str | None:
    """Map a language tag such as pl-PL or pt_BR to a supported language code."""
    text = str(value or "").strip().lower().replace("_", "-")
    if not text or text == LANGUAGE_AUTO:
        return default
    base = text.split("-")[0]
    return base if base in SUPPORTED_LANGUAGES else default


def _first_accept_language(header: str | None) -> str | None:
    """Return the most preferred supported language of an Accept-Language header."""
    if not header:
        return None
    entries: list[tuple[float, int, str]] = []
    for position, part in enumerate(header.split(",")):
        chunk = part.strip()
        if not chunk:
            continue
        language, _, parameters = chunk.partition(";")
        weight = 1.0
        parameter = parameters.strip()
        if parameter.startswith("q=") or parameter.startswith("Q="):
            try:
                weight = float(parameter[2:])
            except ValueError:
                weight = 1.0
        entries.append((weight, position, language.strip()))
    for _, _, language in sorted(entries, key=lambda item: (-item[0], item[1])):
        code = normalize_language(language)
        if code:
            return code
    return None


class Translations:
    """Hold every locale bundle and resolve dotted translation keys."""

    def __init__(self, locales_dir: Path | None = None) -> None:
        """Load all locale files from disk."""
        self.locales_dir = locales_dir or LOCALES_DIR
        self.bundles: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """Read every supported locale file."""
        for language in SUPPORTED_LANGUAGES:
            path = self.locales_dir / f"{language}.json"
            data: Any = {}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as err:
                _LOGGER.error("Cannot load translations from %s: %s", path, err)
            self.bundles[language] = data if isinstance(data, dict) else {}

    def bundle(self, language: str | None) -> dict[str, Any]:
        """Return the bundle of a language, falling back to English."""
        code = normalize_language(language) or DEFAULT_LANGUAGE
        bundle = self.bundles.get(code)
        if bundle:
            return bundle
        return self.bundles.get(DEFAULT_LANGUAGE, {})

    def translate(self, key: str, language: str | None, **values: Any) -> str:
        """Translate a key, falling back to English and finally to the key itself."""
        for candidate in (normalize_language(language), DEFAULT_LANGUAGE):
            if not candidate:
                continue
            text = self._lookup(self.bundle(candidate), key)
            if text is not None:
                return self._format(text, values)
        return key

    @staticmethod
    def _lookup(bundle: dict[str, Any], key: str) -> str | None:
        """Walk a dotted key inside a bundle."""
        node: Any = bundle
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None

    @staticmethod
    def _format(text: str, values: dict[str, Any]) -> str:
        """Apply placeholders without ever raising on bad input."""
        if not values:
            return text
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return text

    def error(self, code: str, language: str | None, **values: Any) -> str:
        """Translate an API error code with a generic fallback."""
        key = f"errors.{code}"
        translated = self.translate(key, language, **values)
        if translated == key:
            return self.translate("errors.generic", language)
        return translated


async def detect_language(
    settings: Settings,
    ha_client: HomeAssistantClient | None = None,
    accept_language: str | None = None,
    requested: str | None = None,
) -> str:
    """Resolve the interface language.

    Order of priority: explicit request, add-on option, Home Assistant
    configuration, browser header, then English.
    """
    explicit = normalize_language(requested)
    if explicit:
        return explicit
    configured = normalize_language(settings.language)
    if configured:
        return configured
    if ha_client is not None and ha_client.enabled:
        detected = normalize_language(await ha_client.async_language())
        if detected:
            return detected
    from_header = _first_accept_language(accept_language)
    if from_header:
        return from_header
    return DEFAULT_LANGUAGE