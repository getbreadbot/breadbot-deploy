"""
notifications/i18n.py — Translation helper for Breadbot Telegram bot.

Usage:
    from notifications.i18n import t
    text = t("telegram.lang_picker.prompt", lang="pt")

Keys use dot notation matching the locale JSON structure.
Falls back to English if a key is missing in the target language.
"""

import json
import os
from functools import lru_cache
from loguru import logger

SUPPORTED_LANGS = {"en", "pt", "es"}
DEFAULT_LANG = "en"

_LOCALES_DIR = os.path.join(os.path.dirname(__file__), "..", "locales")


@lru_cache(maxsize=8)
def _load_locale(lang: str) -> dict:
    """Load and cache a locale file. Falls back to English on error."""
    path = os.path.join(_LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Locale file not found: {path}. Falling back to English.")
        if lang != DEFAULT_LANG:
            return _load_locale(DEFAULT_LANG)
        return {}


def _get_nested(data: dict, key_path: str):
    """Traverse dot-notation key path into a nested dict. Returns None if missing."""
    keys = key_path.split(".")
    node = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def t(key_path: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """
    Translate a dot-notation key for the given language.
    Supports {placeholder} substitution via kwargs.
    Falls back to English if the key is missing in the target language.

    Examples:
        t("telegram.commands.paused", lang="pt")
        t("telegram.commands.buy_logged", lang="es", id=42)
    """
    if lang not in SUPPORTED_LANGS:
        lang = DEFAULT_LANG

    # Try target language first, then fall back to English
    value = _get_nested(_load_locale(lang), key_path)
    if value is None and lang != DEFAULT_LANG:
        value = _get_nested(_load_locale(DEFAULT_LANG), key_path)
    if value is None:
        logger.warning(f"Missing translation key: {key_path} (lang={lang})")
        return key_path  # Return the key itself as last resort

    if kwargs:
        try:
            return value.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Translation format error for key {key_path}: {e}")
            return value

    return value
