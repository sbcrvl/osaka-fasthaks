"""Speech recognition engines.

All engines take mono float32 audio at 16 kHz and return text plus the
language they heard. When the speaker is not known yet, the caller passes
``language=None`` with the participants' languages as ``candidates`` and the
engine picks the most likely one of those — that choice is also how the
pipeline tells the two speakers apart when they speak different languages.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from ..audio.sources import SAMPLE_RATE, Audio
from ..languages import normalize_language

logger = logging.getLogger(__name__)


@dataclass
class ASRResult:
    text: str
    language: str | None
    language_prob: float | None = None


# Phrases Whisper produces on noise (it learned them from video subtitles).
# Dropped when they are the whole transcript. Phrases people really say in a
# conversation ("thank you") are deliberately not listed.
_HALLUCINATIONS = {
    "thanks for watching!", "thanks for watching.", "thank you for watching.",
    "thank you for watching!", "please subscribe.", "you", ".", "...",
    "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
}
_SUBTITLE_CREDIT = re.compile(r"(amara\.org|subtitles by|字幕|sous-titr|untertitel)", re.IGNORECASE)


def is_hallucination(text: str) -> bool:
    t = text.strip().lower()
    return not t or t in _HALLUCINATIONS or bool(_SUBTITLE_CREDIT.search(t))


class ASREngine(ABC):
    """Base class for speech recognizers."""

    name = "asr"
    #: Whether live partial transcripts are cheap enough to run by default.
    partials_by_default = True

    def warmup(self) -> None:
        """Load models ahead of the first utterance (optional)."""

    @abstractmethod
    def transcribe(
        self,
        audio: Audio,
        language: str | None = None,
        candidates: Sequence[str] = (),
        partial: bool = False,
    ) -> ASRResult: ...

    def describe(self) -> str:
        return self.name


def pick_candidate(probs: dict[str, float], candidates: Sequence[str]) -> tuple[str, float]:
    best = max(candidates, key=lambda c: probs.get(c, 0.0))
    return best, probs.get(best, 0.0)


class FasterWhisperASR(ASREngine):
    """Local Whisper via ``faster-whisper`` (CTranslate2). Works offline."""

    name = "faster-whisper"

    def __init__(self, model: str = "small", device: str = "auto", compute_type: str = "default",
                 beam_size: int = 5) -> None:
        self.model_name = model or "small"
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self._model: Any = None
        self._lock = threading.Lock()

    def warmup(self) -> None:
        self._get_model()

    def _get_model(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as e:
                    raise RuntimeError(
                        "faster-whisper is not installed: pip install 'reachy_mini_intermediator[local-asr]'"
                    ) from e
                logger.info("Loading Whisper model %r (device=%s, compute=%s)...",
                            self.model_name, self.device, self.compute_type)
                self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
                logger.info("Whisper model ready")
            return self._model

    def transcribe(self, audio: Audio, language: str | None = None, candidates: Sequence[str] = (),
                   partial: bool = False) -> ASRResult:
        model = self._get_model()
        kwargs: dict[str, Any] = dict(
            beam_size=1 if partial else self.beam_size,
            vad_filter=False,  # segmentation already happened upstream
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        if partial:
            kwargs["temperature"] = 0.0
        prob: float | None = None
        if language is None and len(candidates) > 1:
            # transcribe() detects the language eagerly but decodes lazily, so a
            # wrong guess costs no decoding: we re-run forced to the best candidate.
            segments, info = model.transcribe(audio, language=None, **kwargs)
            probs = dict(info.all_language_probs or [])
            language, prob = pick_candidate(probs, candidates)
            if info.language != language:
                segments, info = model.transcribe(audio, language=language, **kwargs)
        else:
            language = language or (candidates[0] if candidates else None)
            segments, info = model.transcribe(audio, language=language, **kwargs)
            prob = info.language_probability
        parts = []
        for seg in segments:
            # Skip segments Whisper itself thinks are not speech.
            if getattr(seg, "no_speech_prob", 0.0) > 0.6 and getattr(seg, "avg_logprob", 0.0) < -1.0:
                continue
            parts.append(seg.text)
        text = "".join(parts).strip()
        if is_hallucination(text):
            text = ""
        return ASRResult(text=text, language=language or info.language, language_prob=prob)

    def describe(self) -> str:
        return f"faster-whisper {self.model_name}"


def wav_bytes(audio: Audio) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class OpenAICompatASR(ASREngine):
    """``POST /audio/transcriptions`` on OpenAI or a compatible server.

    Works with OpenAI (``whisper-1``, ``gpt-4o-mini-transcribe``, ...) and with
    self-hosted servers that mimic the API (e.g. speaches / faster-whisper-server),
    which is a convenient way to move recognition onto a GPU box.
    """

    name = "openai"
    partials_by_default = False  # each partial is a network round trip

    def __init__(self, model: str = "whisper-1", base_url: str = "https://api.openai.com/v1",
                 api_key: str | None = None, timeout: float = 20.0) -> None:
        import requests

        self.model = model or "whisper-1"
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._verbose_supported = True

    def _post(self, audio: Audio, language: str | None, verbose: bool) -> dict[str, Any]:
        data: dict[str, Any] = {"model": self.model, "response_format": "verbose_json" if verbose else "json"}
        if language:
            data["language"] = language
        r = self._session.post(
            f"{self.base_url}/audio/transcriptions",
            files={"file": ("utterance.wav", wav_bytes(audio), "audio/wav")},
            data=data, timeout=self.timeout,
        )
        if r.status_code == 400 and verbose:
            raise _VerboseUnsupported(r.text)
        r.raise_for_status()
        return r.json()

    def transcribe(self, audio: Audio, language: str | None = None, candidates: Sequence[str] = (),
                   partial: bool = False) -> ASRResult:
        detect = language is None and len(candidates) > 1
        if detect and self._verbose_supported:
            try:
                body = self._post(audio, None, verbose=True)
            except _VerboseUnsupported:
                logger.info("%s does not return verbose_json; language detection disabled", self.model)
                self._verbose_supported = False
                body = self._post(audio, candidates[0], verbose=False)
                language = candidates[0]
            else:
                language = normalize_language(body.get("language"))
        else:
            language = language or (candidates[0] if candidates else None)
            body = self._post(audio, language, verbose=False)
        text = str(body.get("text", "")).strip()
        if is_hallucination(text):
            text = ""
        return ASRResult(text=text, language=language)

    def describe(self) -> str:
        return f"{self.model} @ {self.base_url}"


class _VerboseUnsupported(Exception):
    pass


class ScriptedASR(ASREngine):
    """Plays back a script instead of recognising speech.

    Each final utterance consumes the next line; partials reveal the line word by
    word. Lines may be prefixed with a language code: ``ja|こんにちは``. Handy for
    rehearsing the UI with a WAV file and for tests.
    """

    name = "script"

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
        self._next = 0
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str) -> "ScriptedASR":
        with open(path, encoding="utf-8") as f:
            return cls(f.readlines())

    def _peek(self) -> tuple[str | None, str]:
        if not self._lines:
            return None, ""
        line = self._lines[self._next % len(self._lines)]
        lang, sep, text = line.partition("|")
        if sep and 1 < len(lang.strip()) <= 5:
            return lang.strip().lower(), text.strip()
        return None, line

    def transcribe(self, audio: Audio, language: str | None = None, candidates: Sequence[str] = (),
                   partial: bool = False) -> ASRResult:
        with self._lock:
            lang, text = self._peek()
            if partial:
                words = text.split() if " " in text else list(text)
                share = min(1.0, (audio.size / SAMPLE_RATE) / 3.0)
                n = max(1, int(len(words) * share))
                joiner = " " if " " in text else ""
                text = joiner.join(words[:n])
            else:
                self._next += 1
        return ASRResult(text=text, language=lang or language or (candidates[0] if candidates else None),
                         language_prob=1.0)


def make_asr(spec: str, settings: Any, hotwords: Sequence[str] = ()) -> Any:
    """Build an engine from ``"kind:arg"`` (see ``--asr``).

    Returns an :class:`ASREngine` (utterance by utterance) or, for
    ``shisa-realtime``, a streaming recognizer.
    """
    kind, _, arg = spec.partition(":")
    kind = kind.strip().lower()
    if kind in ("shisa-realtime", "shisa-rt"):
        from .shisa import ShisaRealtimeASR

        return ShisaRealtimeASR(settings.shisa_api_key, language=arg or settings.shisa_language,
                                default_language=settings.shisa_default_language,
                                detection=settings.shisa_detection)
    if kind == "shisa":
        from .shisa import ShisaASR

        return ShisaASR(settings.shisa_api_key, hotwords=hotwords)
    if kind in ("faster-whisper", "whisper", "fw"):
        return FasterWhisperASR(arg or "small", settings.asr_device, settings.asr_compute_type,
                                settings.asr_beam_size)
    if kind in ("openai", "openai-compat"):
        return OpenAICompatASR(arg or "whisper-1", settings.asr_base_url, settings.asr_api_key)
    if kind == "script":
        return ScriptedASR.from_file(arg)
    raise ValueError(f"unknown ASR engine {spec!r}")
