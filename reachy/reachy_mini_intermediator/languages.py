"""Language codes the intermediator understands, with display names.

Codes are ISO 639-1, which is what Whisper reports. Names are used in the
translation prompt and in the web client's language picker.
"""

from __future__ import annotations

LANGUAGES: dict[str, dict[str, str]] = {
    # code: {"name": English name, "native": endonym}
    "en": {"name": "English", "native": "English"},
    "ja": {"name": "Japanese", "native": "日本語"},
    "fr": {"name": "French", "native": "Français"},
    "es": {"name": "Spanish", "native": "Español"},
    "de": {"name": "German", "native": "Deutsch"},
    "it": {"name": "Italian", "native": "Italiano"},
    "pt": {"name": "Portuguese", "native": "Português"},
    "zh": {"name": "Chinese", "native": "中文"},
    "ko": {"name": "Korean", "native": "한국어"},
    "nl": {"name": "Dutch", "native": "Nederlands"},
    "ru": {"name": "Russian", "native": "Русский"},
    "ar": {"name": "Arabic", "native": "العربية"},
    "hi": {"name": "Hindi", "native": "हिन्दी"},
    "th": {"name": "Thai", "native": "ไทย"},
    "vi": {"name": "Vietnamese", "native": "Tiếng Việt"},
    "id": {"name": "Indonesian", "native": "Bahasa Indonesia"},
    "tr": {"name": "Turkish", "native": "Türkçe"},
    "pl": {"name": "Polish", "native": "Polski"},
    "uk": {"name": "Ukrainian", "native": "Українська"},
}

# Some transcription APIs report the language as an English name
# ("japanese") rather than a code; map those back to codes.
_NAME_TO_CODE = {info["name"].lower(): code for code, info in LANGUAGES.items()}


def language_name(code: str | None) -> str:
    """Return the English name for a code, or the code itself if unknown."""
    if not code:
        return "unknown"
    return LANGUAGES.get(code, {}).get("name", code)


def normalize_language(value: str | None) -> str | None:
    """Turn ``"Japanese"``, ``"ja"`` or ``"ja-JP"`` into ``"ja"``."""
    if not value:
        return None
    v = value.strip().lower()
    if v in _NAME_TO_CODE:
        return _NAME_TO_CODE[v]
    return v.split("-")[0].split("_")[0] or None
