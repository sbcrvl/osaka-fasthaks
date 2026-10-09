"""Translation engines.

LLM translators receive the last few turns of the conversation so they can
resolve pronouns, dropped subjects (common in Japanese) and misheard words.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

from ..languages import language_name

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """A previous utterance, given to the translator as context."""

    speaker: str
    lang: str | None
    text: str
    translations: dict[str, str]


SYSTEM_PROMPT = (
    "You are the live interpreter in a face-to-face conversation between two people, "
    "mediated by a small robot that listens and shows the translation on their screens. "
    "Translate only the utterance you are given into the requested language. Keep the "
    "speaker's meaning, tone and level of politeness; keep names as spoken. The text "
    "comes from speech recognition: if a word was clearly misheard, translate what the "
    "speaker evidently meant. Reply with the translation alone: no quotes, notes, "
    "romanization or explanations."
)


def build_user_prompt(text: str, source: str | None, target: str, speaker: str, context: Sequence[Turn]) -> str:
    lines = []
    if context:
        lines.append("<conversation_so_far>")
        for t in context:
            lines.append(f"{t.speaker} ({language_name(t.lang)}): {t.text}")
            # Earlier renderings keep names and terms consistent across turns.
            if t.lang != target and t.translations.get(target):
                lines.append(f"  [{language_name(target)}]: {t.translations[target]}")
        lines.append("</conversation_so_far>")
        lines.append("")
    src = language_name(source) if source else "the language it is spoken in"
    lines.append(f'<utterance speaker="{speaker}" language="{src}">')
    lines.append(text)
    lines.append("</utterance>")
    lines.append("")
    lines.append(f"Translate the utterance into {language_name(target)}.")
    return "\n".join(lines)


class Translator(ABC):
    name = "translator"

    @abstractmethod
    def translate(self, text: str, source: str | None, target: str, speaker: str = "Speaker",
                  context: Sequence[Turn] = ()) -> str: ...

    def describe(self) -> str:
        return self.name


class ClaudeTranslator(Translator):
    """Claude via the Anthropic Messages API (``pip install anthropic``)."""

    name = "claude"

    def __init__(self, model: str = "claude-haiku-5-5", api_key: str | None = None, max_tokens: int = 1024) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("the anthropic package is not installed: pip install anthropic") from e
        self.model = model or "claude-haiku-5-5"
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def translate(self, text: str, source: str | None, target: str, speaker: str = "Speaker",
                  context: Sequence[Turn] = ()) -> str:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(text, source, target, speaker, context)}],
        )
        return "".join(getattr(b, "text", "") for b in msg.content).strip()

    def describe(self) -> str:
        return self.model


class OpenAICompatTranslator(Translator):
    """Any ``/chat/completions`` endpoint: OpenAI, Ollama, LM Studio, vLLM, ...

    With Ollama the whole pipeline can run offline:
    ``--translator openai:qwen2.5:7b --translator-base-url http://localhost:11434/v1``.
    """

    name = "openai"

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str | None = None,
                 timeout: float = 30.0) -> None:
        import requests

        if not model:
            raise ValueError("openai translator needs a model name, e.g. openai:gpt-4o-mini")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"

    def translate(self, text: str, source: str | None, target: str, speaker: str = "Speaker",
                  context: Sequence[Turn] = ()) -> str:
        r = self._session.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(text, source, target, speaker, context)},
                ],
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        return str(r.json()["choices"][0]["message"]["content"]).strip()

    def describe(self) -> str:
        return f"{self.model} @ {self.base_url}"


class EchoTranslator(Translator):
    """Marks the text with the target language; for wiring tests and rehearsals."""

    name = "echo"

    def translate(self, text: str, source: str | None, target: str, speaker: str = "Speaker",
                  context: Sequence[Turn] = ()) -> str:
        return f"[{target}] {text}"


def make_translator(spec: str, settings: Any, keywords: Any = None) -> Translator | None:
    """Build a translator from ``"kind:model"`` (see ``--translator``); ``none`` disables it.

    ``keywords`` is a callable returning terms to keep as-is (participant names).
    """
    kind, _, arg = spec.partition(":")
    kind = kind.strip().lower()
    if kind in ("none", "off", ""):
        return None
    if kind == "shisa":
        from .shisa import ShisaTranslator

        return ShisaTranslator(settings.shisa_api_key, model=arg or None, keywords=keywords)
    if kind in ("claude", "anthropic"):
        return ClaudeTranslator(arg or "claude-haiku-5-5", settings.translator_api_key)
    if kind in ("openai", "openai-compat", "ollama"):
        return OpenAICompatTranslator(arg, settings.translator_base_url, settings.translator_api_key)
    if kind == "echo":
        return EchoTranslator()
    raise ValueError(f"unknown translator {spec!r}")
