"""Shisa.AI engines (https://docs.shisa.ai) — Japanese / English / Chinese.

* :class:`ShisaRealtimeASR` streams audio to ``wss://api.shisa.ai/ws/asr/realtime``.
  Shisa does the voice activity detection and sends partial and final
  transcripts back, so the pipeline skips its own segmentation.
* :class:`ShisaASR` sends each locally segmented utterance to the batch
  endpoint ``POST /asr/srt/audio_llm``; it plugs into the same flow as Whisper.
* :class:`ShisaTranslator` uses ``POST /translate/`` with the previous turns as
  ``context`` and the participants' names as ``keywords``.

* :class:`ShisaTTS` speaks translations with ``POST /tts``.

All of them read the key from ``SHISA_API_KEY``. Behaviour follows what the
team measured against the live API on 2026-10-09 (``../shisa/README.md``):

* realtime ASR with ``language: auto`` + ``language_detection_mode: utterance``
  detects the language reliably, but the socket's own translation then fails
  (``source_language_unknown``), so translation is done separately here;
* ~5-11 quick requests trigger ``429 Too many requests. Retry after 1.72s``,
  honoured by :func:`shisa_request`;
* the ``streaming`` flag in the voice list is unreliable, and the tested voices
  are pinned in :data:`DEFAULT_VOICES`.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

from ..audio.sources import SAMPLE_RATE, Audio
from ..languages import language_name, normalize_language
from .asr import ASREngine, ASRResult, is_hallucination, wav_bytes
from .translate import Translator, Turn

logger = logging.getLogger(__name__)

API_BASE = "https://api.shisa.ai"
REALTIME_URL = "wss://api.shisa.ai/ws/asr/realtime"
SHISA_LANGS = ("ja", "en", "zh")

# Voices the team tested (../shisa/README.md): both stream and answer fast.
DEFAULT_VOICES = {
    "ja": "762fbbc6-4b7e-49b9-a6ea-7506333cb79b",  # Ono Anna, ~333 ms to first PCM at 16 kHz
    "en": "1a5b71d7-05c9-4acd-9cb8-0984ddacd969",  # Ryan, ~87 ms to first WAV data
}

MAX_ATTEMPTS = 3


def _retry_after_s(body: str) -> float:
    m = re.search(r"Retry after ([\d.]+)s", body, re.IGNORECASE)
    return float(m.group(1)) if m else 1.0


def shisa_request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
    """``session.request`` that waits out Shisa's 429s (up to 3 attempts)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        r = session.request(method, url, **kwargs)
        if r.status_code != 429 or attempt == MAX_ATTEMPTS:
            return r
        wait = _retry_after_s(r.text)
        logger.info("Shisa rate limit; retrying in %.1f s", wait)
        time.sleep(wait)
    return r


# --------------------------------------------------------------------- batch ASR
class ShisaASR(ASREngine):
    """Batch recognition of one utterance at a time."""

    name = "shisa"
    partials_by_default = False

    def __init__(self, api_key: str | None, base_url: str = API_BASE, hotwords: Sequence[str] = (),
                 timeout: float = 20.0) -> None:
        import requests

        if not api_key:
            raise RuntimeError("Shisa ASR needs an API key: set SHISA_API_KEY")
        self.url = f"{base_url.rstrip('/')}/asr/srt/audio_llm"
        self.hotwords = [h for h in hotwords if h][:20]
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"

    def transcribe(self, audio: Audio, language: str | None = None, candidates: Sequence[str] = (),
                   partial: bool = False) -> ASRResult:
        if language is None and len(candidates) == 1:
            language = candidates[0]
        body: dict[str, Any] = {"audio": base64.b64encode(wav_bytes(audio)).decode("ascii")}
        if language:
            body["language"] = language  # omitted → Shisa detects it
        if self.hotwords:
            body["hotwords"] = self.hotwords
        r = shisa_request(self._session, "POST", self.url, json=body, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        text = str(data.get("text") or "").strip()
        if is_hallucination(text):
            text = ""
        lang = normalize_language(data.get("language")) or language
        if language is None and candidates and lang not in candidates:
            # Detected something neither participant speaks: retry with a hint.
            return self.transcribe(audio, language=candidates[0], candidates=candidates, partial=partial)
        conf = data.get("confidence")
        return ASRResult(text=text, language=lang, language_prob=float(conf) if conf is not None else None)

    def describe(self) -> str:
        return "Shisa ASR"


# ----------------------------------------------------------------- realtime ASR
@dataclass
class StreamEvent:
    """Normalized event from a streaming recognizer (wall-clock times)."""

    kind: str  # speech_started | speech_stopped | partial | final | error | ready | closed
    utterance: str | None = None
    text: str = ""
    language: str | None = None
    t_start: float | None = None
    t_end: float | None = None
    replaces: list[str] = field(default_factory=list)
    result_id: str | None = None
    error: str | None = None
    fatal: bool = False


class StreamingASR:
    """Interface for recognizers that take a continuous stream."""

    name = "streaming"

    def start(self, on_event: Callable[[StreamEvent], None]) -> None:
        raise NotImplementedError

    def send(self, chunk: Audio, t_end: float) -> None:
        """Queue audio whose last sample was captured at wall time ``t_end``."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class ShisaRealtimeASR(StreamingASR):
    """Shisa realtime ASR over WebSocket, with automatic reconnection.

    Audio is converted to 16 kHz mono PCM16 and sent as base64
    ``input_audio.append`` messages every ~100 ms. Server timings
    (``audio_start_ms`` / ``audio_end_ms``) are relative to the first sample of
    the session; they are mapped back to wall-clock time so direction-of-arrival
    and push-to-talk can be matched against each utterance.
    """

    name = "shisa-realtime"
    CHUNK_MS = 100

    def __init__(self, api_key: str | None, language: str = "auto", default_language: str = "ja",
                 detection: str = "utterance", url: str = REALTIME_URL,
                 connect: Callable[..., Any] | None = None) -> None:
        if not api_key:
            raise RuntimeError("Shisa realtime ASR needs an API key: set SHISA_API_KEY")
        self.api_key = api_key
        self.language = language
        self.default_language = default_language
        self.detection = detection
        self.url = url
        self._connect = connect  # injectable for tests
        self._on_event: Callable[[StreamEvent], None] = lambda e: None
        self._ws: Any = None
        self._ws_lock = threading.Lock()
        self._buf: list[np.ndarray] = []
        self._buf_samples = 0
        self._halt = threading.Event()
        self._ready = threading.Event()
        self._session_t0: float | None = None  # wall time of the session's first sample
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ public API
    def start(self, on_event: Callable[[StreamEvent], None]) -> None:
        self._on_event = on_event
        self._thread = threading.Thread(target=self._run, name="shisa-realtime", daemon=True)
        self._thread.start()

    def send(self, chunk: Audio, t_end: float) -> None:
        if not self._ready.is_set():
            return  # audio before the session is up is dropped, not buffered
        if self._session_t0 is None:
            self._session_t0 = t_end - chunk.size / SAMPLE_RATE
        self._buf.append(chunk)
        self._buf_samples += chunk.size
        if self._buf_samples * 1000 < self.CHUNK_MS * SAMPLE_RATE:
            return
        pcm = (np.clip(np.concatenate(self._buf), -1, 1) * 32767).astype("<i2").tobytes()
        self._buf, self._buf_samples = [], 0
        msg = json.dumps({"type": "input_audio.append", "audio": base64.b64encode(pcm).decode("ascii")})
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return
        try:
            ws.send(msg)
        except Exception:
            logger.debug("send failed; reconnecting", exc_info=True)
            self._drop_connection()

    def stop(self) -> None:
        self._halt.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "session.close"}))
            except Exception:
                pass
            self._drop_connection()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def describe(self) -> str:
        lang = self.language if self.language != "auto" else f"auto/{self.detection}"
        return f"Shisa realtime ASR ({lang})"

    # ------------------------------------------------------------- internals
    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect(self.url, {"Authorization": f"Bearer {self.api_key}"})
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            raise RuntimeError("the websockets package is needed for Shisa realtime ASR") from e
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            return connect(self.url, additional_headers=headers, open_timeout=10, max_size=2**22)
        except TypeError:  # websockets < 13 named it extra_headers
            return connect(self.url, extra_headers=headers, open_timeout=10, max_size=2**22)

    def _session_update(self) -> dict[str, Any]:
        session: dict[str, Any] = {
            "input_audio_format": "pcm_s16le",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "language": self.language,
        }
        if self.language == "auto":
            session["default_language"] = self.default_language
            session["language_detection_mode"] = self.detection
        return {"type": "session.update", "session": session}

    def _drop_connection(self) -> None:
        self._ready.clear()
        with self._ws_lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _wall(self, ms: Any) -> float | None:
        if ms is None or self._session_t0 is None:
            return None
        return self._session_t0 + float(ms) / 1000.0

    def _run(self) -> None:
        backoff = 1.0
        while not self._halt.is_set():
            try:
                ws = self._open()
                ws.send(json.dumps(self._session_update()))
                with self._ws_lock:
                    self._ws = ws
                self._session_t0 = None
                backoff = 1.0
                for raw in ws:
                    self._handle(json.loads(raw))
                    if self._halt.is_set():
                        break
            except Exception as e:
                if self._halt.is_set():
                    break
                logger.warning("Shisa realtime connection lost: %s", e)
                self._on_event(StreamEvent("error", error=f"Shisa realtime: {e}"))
            finally:
                self._drop_connection()
            if not self._halt.is_set():
                self._halt.wait(backoff)
                backoff = min(backoff * 2, 15.0)
        self._on_event(StreamEvent("closed"))

    def _handle(self, m: dict[str, Any]) -> None:
        kind = m.get("type")
        utt = m.get("utterance_id")
        utt = str(utt) if utt is not None else None
        if kind == "session.created":
            logger.info("Shisa realtime session %s ready", m.get("session_id"))
            self._ready.set()
            self._on_event(StreamEvent("ready"))
        elif kind == "speech_started":
            self._on_event(StreamEvent("speech_started", utt, t_start=self._wall(m.get("audio_start_ms"))))
        elif kind == "speech_stopped":
            self._on_event(StreamEvent("speech_stopped", utt, t_start=self._wall(m.get("audio_start_ms")),
                                       t_end=self._wall(m.get("audio_end_ms"))))
        elif kind == "asr.partial_result":
            self._on_event(StreamEvent("partial", utt, text=str(m.get("text") or ""),
                                       t_start=self._wall(m.get("audio_start_ms")),
                                       t_end=self._wall(m.get("audio_end_ms")),
                                       result_id=m.get("result_id")))
        elif kind == "asr.final_result":
            self._on_event(StreamEvent("final", utt, text=str(m.get("text") or "").strip(),
                                       language=normalize_language(m.get("language")),
                                       t_start=self._wall(m.get("audio_start_ms")),
                                       t_end=self._wall(m.get("audio_end_ms")),
                                       replaces=[str(r) for r in m.get("replaces") or []],
                                       result_id=m.get("result_id")))
        elif kind == "error":
            fatal = bool(m.get("fatal"))
            text = f"Shisa realtime {m.get('code')}: {m.get('message')}"
            (logger.error if fatal else logger.warning)(text)
            self._on_event(StreamEvent("error", utt, error=text, fatal=fatal))
        elif kind == "session.usage":
            logger.debug("Shisa usage: %s", m.get("usage"))


# --------------------------------------------------------------- text to speech
class ShisaTTS:
    """``POST /tts`` → audio, as mono float32 at 16 kHz.

    Voices come from ``GET /tts/voices``. Unless a voice is pinned per language
    (``--tts-voice ja=<uuid>``), the first voice whose ``language`` mentions the
    target language is used.
    """

    name = "shisa-tts"

    def __init__(self, api_key: str | None, voices: dict[str, str] | None = None, base_url: str = API_BASE,
                 timeout: float = 30.0) -> None:
        import requests

        if not api_key:
            raise RuntimeError("Shisa TTS needs an API key: set SHISA_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.pinned = {**DEFAULT_VOICES, **(voices or {})}
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._catalogue: list[dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def catalogue(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._catalogue is None:
                r = shisa_request(self._session, "GET", f"{self.base_url}/tts/voices", timeout=self.timeout)
                r.raise_for_status()
                body = r.json()
                self._catalogue = list(body.get("voices", body) if isinstance(body, dict) else body)
                logger.info("Shisa TTS: %d voices available", len(self._catalogue))
            return self._catalogue

    @staticmethod
    def _speaks(voice: dict[str, Any], lang: str) -> bool:
        spoken = str(voice.get("language", "")).lower()
        return lang.lower() in spoken.replace(",", " ").replace("&", " ").split() or \
            language_name(lang).lower() in spoken

    def voice_for(self, lang: str) -> dict[str, Any]:
        cat = self.catalogue()
        if lang in self.pinned:
            v = next((v for v in cat if v.get("id") == self.pinned[lang]), None)
            return v or {"id": self.pinned[lang], "formats": ["pcm", "wav"], "sample_rates": [SAMPLE_RATE]}
        usable = [v for v in cat if {"wav", "pcm"} & set(v.get("formats") or [])]
        # Voices dedicated to one language first, then bilingual ones.
        matches = sorted((v for v in usable if self._speaks(v, lang)),
                         key=lambda v: len(str(v.get("language", ""))))
        if not matches:
            raise RuntimeError(f"no Shisa voice for {language_name(lang)}; pin one with --tts-voice {lang}=<id>")
        return matches[0]

    def synthesize(self, text: str, lang: str) -> Audio:
        voice = self.voice_for(lang)
        formats = set(voice.get("formats") or ["wav"])
        # WAV first: its header carries the real rate (24 kHz), resampled here.
        # format=pcm returned 500 ErrBackendUnreachable for every voice on 2026-10-09
        # while WAV worked, and PCM's 16 kHz was never confirmed by ear.
        if "wav" in formats:
            fmt, rate = "wav", 24_000
        else:
            fmt, rate = "pcm", 24_000
        body: dict[str, Any] = {"voice_id": voice["id"], "text": text[:5000], "format": fmt}
        if rate != 24_000:
            body["sample_rate"] = rate
        r = shisa_request(self._session, "POST", f"{self.base_url}/tts", json=body, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Shisa TTS {r.status_code}: {r.text[:200]}")
        return decode_audio(r.content, fmt, rate)

    def describe(self) -> str:
        return "Shisa TTS"


def decode_audio(data: bytes, fmt: str, rate: int) -> Audio:
    """WAV or raw PCM16 mono bytes → float32 at 16 kHz."""
    from ..audio.sources import load_wav_bytes, resample

    if fmt == "wav" or data[:4] == b"RIFF":
        return load_wav_bytes(data)
    pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    return resample(pcm, rate)


# ------------------------------------------------------------------ translation
class ShisaTranslator(Translator):
    """``POST /translate/`` (multipart form). Default model alias ``shisa-ai/chotto``."""

    name = "shisa"
    MAX_CONTEXT = 2000  # codepoints, per the API

    def __init__(self, api_key: str | None, model: str | None = None, base_url: str = API_BASE,
                 keywords: Callable[[], Sequence[str]] | None = None, timeout: float = 20.0) -> None:
        import requests

        if not api_key:
            raise RuntimeError("Shisa translation needs an API key: set SHISA_API_KEY")
        self.url = f"{base_url.rstrip('/')}/translate/"
        self.model = model or None
        self.timeout = timeout
        self.keywords = keywords or (lambda: ())
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"

    @classmethod
    def context_text(cls, context: Sequence[Turn]) -> str:
        lines = [f"{t.speaker} ({language_name(t.lang)}): {t.text}" for t in context]
        out = "\n".join(lines)
        while len(out) > cls.MAX_CONTEXT and lines:
            lines.pop(0)  # keep the most recent turns
            out = "\n".join(lines)
        return out

    def translate(self, text: str, source: str | None, target: str, speaker: str = "Speaker",
                  context: Sequence[Turn] = ()) -> str:
        form: list[tuple[str, tuple[None, str]]] = [
            ("text", (None, text)),
            ("source_lang", (None, source or "auto")),
            ("target_lang", (None, target)),
        ]
        ctx = self.context_text(context)
        if ctx:
            form.append(("context", (None, ctx)))
        for kw in list(dict.fromkeys(k for k in self.keywords() if k))[:20]:
            form.append(("keywords", (None, kw.encode("utf-8")[:100].decode("utf-8", "ignore"))))
        if self.model:
            form.append(("model", (None, self.model)))
        r = shisa_request(self._session, "POST", self.url, files=form, timeout=self.timeout)
        r.raise_for_status()
        return str(r.json()["choices"][0]["message"]["content"]).strip()

    def describe(self) -> str:
        return f"Shisa translate ({self.model or 'shisa-ai/chotto'})"
