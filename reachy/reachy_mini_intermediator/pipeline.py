"""Audio → utterances → transcripts → translations.

Threads:

* **capture** reads the audio source, segments it, schedules recognition jobs
  and publishes the input level / speech state for the clients' meters;
* **recognition** runs one job at a time (Whisper is CPU/GPU bound), skipping
  partial jobs that went stale;
* a small **translation** pool calls the translator so network latency never
  delays the next transcript.

With a streaming recognizer (Shisa realtime) the capture thread forwards the
audio as it comes and the recognizer's own events (speech started, partial,
final) replace local segmentation and the recognition thread.
"""

from __future__ import annotations

import collections
import itertools
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .audio.sources import SAMPLE_RATE, Audio, AudioSource
from .audio.vad import Segmenter, SpeechDiscard, SpeechEnd, SpeechStart
from .config import Settings
from .engines.asr import ASREngine, ASRResult
from .engines.shisa import StreamEvent, StreamingASR
from .engines.translate import Translator, Turn
from .hub import Hub
from .session import Session
from .speakers import Attribution, Attributor, DoATracker

logger = logging.getLogger(__name__)


@dataclass
class _Job:
    kind: str  # "partial" | "final"
    uid: str
    audio: Audio = field(repr=False)
    t_start: float
    t_end: float


@dataclass
class _Live:
    """Per-utterance state while it is being spoken."""

    uid: str
    t_start: float
    speaker: str | None = None
    lang: str | None = None
    method: str = "none"
    partial_samples: int = 0
    partial_pending: bool = False
    ended: bool = False


class Pipeline:
    def __init__(self, settings: Settings, session: Session, hub: Hub, source: AudioSource | None,
                 asr: ASREngine | StreamingASR | None, translator: Translator | None,
                 doa: DoATracker | None = None, gestures: Any = None, voice: Any = None,
                 publisher: Any = None) -> None:
        self.settings = settings
        self.session = session
        self.hub = hub
        self.source = source
        self.asr = asr
        self.translator = translator
        self.doa = doa
        self.gestures = gestures
        self.voice = voice
        self.publisher = publisher
        self.attributor = Attributor(session, settings.attribution, doa, settings.doa_min_confidence)
        self.segmenter = Segmenter(
            aggressiveness=settings.vad_aggressiveness,
            energy_ratio=settings.vad_energy_ratio,
            end_silence_ms=settings.end_silence_ms,
            min_utterance_s=settings.min_utterance_s,
            max_utterance_s=settings.max_utterance_s,
        )
        self.streaming = isinstance(asr, StreamingASR)
        if settings.partials == "auto":
            self.partials = self.streaming or bool(getattr(asr, "partials_by_default", False))
        else:
            self.partials = settings.partials == "on"
        self._streams: dict[str, dict[str, Any]] = {}  # streaming utterance id -> {uid, n, ...}
        self._partial_step = int(settings.partial_interval * SAMPLE_RATE)

        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._current_uid: str | None = None
        self._live: dict[str, _Live] = {}
        self._live_lock = threading.Lock()
        self._seq = itertools.count(1)
        self._finals: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self._finals_lock = threading.Lock()
        self._translate_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="translate")
        self._halt = threading.Event()
        self._threads: list[threading.Thread] = []
        self.source_done = threading.Event()
        hub.on_command = self.handle_command

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.asr is None:
            # No recognizer: typed messages and translation still work.
            self.hub.emit_status(listening=False)
            return
        self._threads = [threading.Thread(target=self._capture_loop, name="capture", daemon=True)]
        if self.streaming:
            self.asr.start(self._on_stream_event)  # type: ignore[union-attr]
        else:
            self._threads.append(threading.Thread(target=self._recognition_loop, name="recognition", daemon=True))
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._halt.set()
        self._jobs.put(None)
        if self.streaming:
            self.asr.stop()  # type: ignore[union-attr]
        for t in self._threads:
            t.join(timeout=5.0)
        self._translate_pool.shutdown(wait=False, cancel_futures=True)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until queued recognition and translation work is done (tests, file runs)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._live_lock:
                busy = bool(self._live) or bool(self._streams)
            if not busy and self._jobs.empty() and self._translate_pool._work_queue.empty():  # noqa: SLF001
                time.sleep(0.2)
                with self._live_lock:
                    if not self._live and self._jobs.empty():
                        return True
            time.sleep(0.05)
        return False

    # --------------------------------------------------------------- capture
    def _capture_loop(self) -> None:
        try:
            self.source.start()
        except Exception as e:
            logger.exception("Audio source failed to start")
            self.hub.emit_status(error=f"Audio input unavailable: {e}", listening=False)
            return
        self.hub.emit_status(listening=not self.session.paused, error=None)
        last_status = 0.0
        was_paused = self.session.paused
        try:
            while not self._halt.is_set():
                chunk = self.source.read(timeout=0.2)
                now = time.time()
                if chunk is None:
                    if self.source.finished:
                        for ev in self.segmenter.flush(now):
                            self._on_event(ev)
                        self.source_done.set()
                        self._halt.wait(0.2)
                    continue
                paused = self.session.paused or now < self.session.mic_muted_until
                if paused:
                    if not was_paused:
                        for ev in self.segmenter.reset():
                            self._on_event(ev)
                    was_paused = True
                    if self.streaming:
                        # Silence keeps the recognizer's timeline aligned with ours.
                        self.asr.send(np.zeros_like(chunk), now)  # type: ignore[union-attr]
                    if now - last_status > 0.5:
                        last_status = now
                        self.hub.emit_status(listening=False, speech=False, level=0.0)
                    continue
                was_paused = False
                if self.streaming:
                    self.asr.send(chunk, now)  # type: ignore[union-attr]
                    self.segmenter.feed(chunk, now)  # only for the level meter
                else:
                    for ev in self.segmenter.feed(chunk, now):
                        self._on_event(ev)
                    self._maybe_schedule_partial(now)
                if now - last_status > 0.12:
                    last_status = now
                    self._publish_status()
        except Exception as e:
            logger.exception("Audio capture stopped")
            self.hub.emit_status(error=f"Audio capture stopped: {e}", listening=False)
        finally:
            try:
                self.source.stop()
            except Exception:
                logger.debug("source stop failed", exc_info=True)

    def _publish_status(self) -> None:
        doa = self.doa.latest() if self.doa is not None else None
        level = float(min(1.0, self.segmenter.level * 8.0))  # rough 0..1 for meters
        self.hub.emit_status(
            listening=not self.session.paused,
            speech=self.segmenter.in_speech,
            level=round(level, 3),
            doa={"angle": round(doa[1], 3), "speech": doa[2], "side": self.doa.side_of(doa[1])}
            if doa is not None and self.doa is not None else None,
        )

    def _on_event(self, ev: Any) -> None:
        if isinstance(ev, SpeechStart):
            uid = f"u{next(self._seq)}"
            with self._live_lock:
                self._live[uid] = _Live(uid=uid, t_start=ev.t_start)
                self._current_uid = uid
            self._speech_started(ev.t_start)
        elif isinstance(ev, SpeechEnd):
            uid = self._current_uid
            with self._live_lock:
                live = self._live.get(uid)
                if live is not None:
                    live.ended = True
            self._jobs.put(_Job("final", uid, ev.audio, ev.t_start, ev.t_end))
            self.hub.emit_status(speaker=None)
        elif isinstance(ev, SpeechDiscard):
            uid = self._current_uid
            if uid is not None:
                with self._live_lock:
                    had_partial = self._live.pop(uid, None)
                if had_partial is not None and had_partial.partial_samples:
                    self.hub.emit_drop(uid)
            self.hub.emit_status(speaker=None)

    def _live_speaker(self, t_start: float) -> Any:
        """Best guess of who is talking right now, from push-to-talk or direction."""
        ptt = self.session.participant(self.session.ptt_active())
        if ptt is not None:
            return ptt
        if self.doa is not None:
            side, conf, _ = self.doa.estimate(t_start - 0.2, time.time())
            if side is not None and conf >= self.settings.doa_min_confidence:
                return self.session.by_side(side)
        return None

    def _speech_started(self, t_start: float) -> None:
        speaker = self._live_speaker(t_start)
        if self.gestures is not None:
            self.gestures.listen_to(speaker.side if speaker else None)
        self.hub.emit_status(speaker=speaker.id if speaker else None)

    # ------------------------------------------------- streaming recognition
    def _stream_state(self, utt: str | None) -> dict[str, Any]:
        key = utt or "_"
        with self._live_lock:
            st = self._streams.get(key)
            if st is None:
                st = {"uid": f"u{next(self._seq)}", "t_start": time.time(), "speaker": None}
                self._streams[key] = st
            return st

    def _on_stream_event(self, ev: StreamEvent) -> None:
        """Events from a streaming recognizer (its receive thread)."""
        try:
            if ev.kind == "ready":
                self.hub.emit_status(error=None)
            elif ev.kind == "speech_started":
                st = self._stream_state(ev.utterance)
                st["t_start"] = ev.t_start or time.time()
                self._speech_started(st["t_start"])
            elif ev.kind == "partial":
                if not self.partials or not ev.text.strip():
                    return
                st = self._stream_state(ev.utterance)
                if st["speaker"] is None:
                    sp = self._live_speaker(st["t_start"])
                    st["speaker"] = sp.id if sp else None
                self.hub.emit_utterance(self._utterance_dict(
                    st["uid"], st["speaker"], "live", None, ev.text.strip(),
                    ev.t_start or st["t_start"], ev.t_end or time.time(), final=False))
            elif ev.kind == "final":
                self._stream_final(ev)
            elif ev.kind == "speech_stopped":
                self.hub.emit_status(speaker=None)
            elif ev.kind == "error":
                self.hub.emit_status(error=ev.error)
        except Exception:
            logger.exception("Failed to handle streaming event %s", ev.kind)

    def _stream_final(self, ev: StreamEvent) -> None:
        st = self._stream_state(ev.utterance)
        uid = st["uid"]
        t_start = ev.t_start or st["t_start"]
        t_end = ev.t_end or time.time()
        with self._live_lock:
            # A long utterance can produce several finals; partials that follow
            # this one start a fresh entry with a new id.
            self._streams.pop(ev.utterance or "_", None)
        if not ev.text:
            self.hub.emit_drop(uid)
            return
        known = ASRResult(ev.text, ev.language)
        attr, _ = self.attributor.resolve(t_start, t_end, lambda _lang, _cands: known)
        self.hub.emit_status(error=None)
        self._finalize(uid, attr, ev.language or self._lang_of(attr.speaker), ev.text, t_start, t_end)

    def _lang_of(self, pid: str | None) -> str | None:
        p = self.session.participant(pid)
        return p.lang if p else None

    def _maybe_schedule_partial(self, now: float) -> None:
        if not self.partials or not self.segmenter.in_speech:
            return
        uid = self._current_uid
        n = self.segmenter.current_samples
        with self._live_lock:
            live = self._live.get(uid) if uid else None
            if live is None or live.partial_pending or n - live.partial_samples < self._partial_step:
                return
            if n < int(0.6 * SAMPLE_RATE):
                return  # too short to say anything useful
            live.partial_samples = n
            live.partial_pending = True
        self._jobs.put(_Job("partial", live.uid, self.segmenter.current_audio(), live.t_start, now))

    # ----------------------------------------------------------- recognition
    def _recognition_loop(self) -> None:
        try:
            self.asr.warmup()
        except Exception as e:
            logger.exception("Speech recognition failed to load")
            self.hub.emit_status(error=f"Speech recognition unavailable: {e}")
        while not self._halt.is_set():
            job = self._jobs.get()
            if job is None:
                return
            try:
                if job.kind == "partial":
                    self._run_partial(job)
                else:
                    self._run_final(job)
            except Exception as e:
                logger.exception("Recognition job failed")
                self.hub.emit_status(error=f"Recognition error: {e}")
                if job.kind == "final":
                    with self._live_lock:
                        self._live.pop(job.uid, None)
                    self.hub.emit_drop(job.uid)

    def _transcriber(self, audio: Audio, partial: bool):
        def run(language: str | None, candidates: list[str]) -> ASRResult:
            return self.asr.transcribe(audio, language=language, candidates=candidates, partial=partial)
        return run

    def _run_partial(self, job: _Job) -> None:
        with self._live_lock:
            live = self._live.get(job.uid)
            if live is None or live.ended:
                return  # the final is coming; skip stale work
        try:
            if live.lang is None:
                attr, result = self.attributor.resolve(job.t_start, job.t_end,
                                                       self._transcriber(job.audio, True), commit=False)
                live.speaker, live.method, live.lang = attr.speaker, attr.method, result.language
            else:
                result = self.asr.transcribe(job.audio, language=live.lang, candidates=self.session.languages(),
                                             partial=True)
            with self._live_lock:
                if live.ended or job.uid not in self._live:
                    return
            if result.text:
                self.hub.emit_utterance(self._utterance_dict(job.uid, live.speaker, live.method, result.language,
                                                             result.text, job.t_start, job.t_end, final=False))
        finally:
            live.partial_pending = False

    def _run_final(self, job: _Job) -> None:
        attr, result = self.attributor.resolve(job.t_start, job.t_end, self._transcriber(job.audio, False))
        with self._live_lock:
            self._live.pop(job.uid, None)
        if not result.text:
            self.hub.emit_drop(job.uid)
            return
        self._finalize(job.uid, attr, result.language, result.text, job.t_start, job.t_end)

    # ---------------------------------------------------------- finalization
    def _utterance_dict(self, uid: str, speaker: str | None, method: str, lang: str | None, text: str,
                        t_start: float, t_end: float, final: bool, doa: float | None = None) -> dict[str, Any]:
        return {
            "id": uid,
            "speaker": speaker,
            "attribution": method,
            "lang": lang,
            "text": text,
            "start": round(t_start, 3),
            "end": round(t_end, 3),
            "final": final,
            "doa": round(doa, 3) if doa is not None else None,
        }

    def _finalize(self, uid: str, attr: Attribution, lang: str | None, text: str, t_start: float,
                  t_end: float) -> None:
        u = self._utterance_dict(uid, attr.speaker, attr.method, lang, text, t_start, t_end, final=True,
                                 doa=attr.doa)
        u["translations"] = {}
        with self._finals_lock:
            self._finals[uid] = u
            while len(self._finals) > 200:
                self._finals.popitem(last=False)
        self.hub.emit_utterance(u)
        if self.publisher is not None:
            self.publisher.publish(u)
        self._translate(u)

    def _targets(self, u: dict[str, Any]) -> list[str]:
        speaker = self.session.participant(u.get("speaker"))
        targets = []
        for p in self.session.participants():
            if speaker is not None and p.id == speaker.id:
                continue
            if p.lang != u.get("lang") and p.lang not in targets:
                targets.append(p.lang)
        return targets

    def _context(self, before: str) -> list[Turn]:
        n = self.settings.translation_context
        if n <= 0:
            return []
        with self._finals_lock:
            items = list(self._finals.values())
        idx = next((i for i, x in enumerate(items) if x["id"] == before), len(items))
        turns = []
        for x in items[max(0, idx - n):idx]:
            p = self.session.participant(x.get("speaker"))
            turns.append(Turn(p.name if p else "Someone", x.get("lang"), x["text"], dict(x.get("translations", {}))))
        return turns

    def _translate(self, u: dict[str, Any]) -> None:
        targets = self._targets(u)
        if not targets:
            return
        if self.translator is None:
            return
        speaker = self.session.participant(u.get("speaker"))
        speaker_name = speaker.name if speaker else "Speaker"
        context = self._context(u["id"])
        for target in targets:
            self._translate_pool.submit(self._translate_one, u, target, speaker_name, context)

    def _translate_one(self, u: dict[str, Any], target: str, speaker_name: str, context: list[Turn]) -> None:
        try:
            text = self.translator.translate(u["text"], u.get("lang"), target, speaker_name, context)  # type: ignore[union-attr]
        except Exception as e:
            logger.exception("Translation failed")
            self.hub.emit_translation(u["id"], target, None, error=str(e)[:200])
            return
        with self._finals_lock:
            if u["id"] in self._finals:
                self._finals[u["id"]].setdefault("translations", {})[target] = text
        self.hub.emit_translation(u["id"], target, text)
        listener = self.session.other(u.get("speaker")) if u.get("speaker") else None
        if self.gestures is not None and listener is not None:
            self.gestures.relay_to(listener.side)
        if self.voice is not None and u.get("attribution") != "manual":
            self.voice.say(u["id"], text, target)

    # -------------------------------------------------------- client commands
    def handle_command(self, msg: dict[str, Any]) -> None:
        """Commands from the web clients (runs on a worker thread)."""
        kind = msg.get("type")
        if kind == "text":
            # Typed instead of spoken: useful in a loud demo hall.
            p = self.session.participant(msg.get("participant"))
            text = str(msg.get("text", "")).strip()[:1000]
            if p is None or not text:
                return
            now = time.time()
            uid = f"u{next(self._seq)}"
            self._finalize(uid, Attribution(p.id, "typed"), p.lang, text, now, now)
        elif kind == "reassign":
            # Fix a wrong speaker guess from the UI.
            uid, pid = msg.get("id"), msg.get("speaker")
            p = self.session.participant(pid)
            with self._finals_lock:
                u = self._finals.get(uid)
                if u is None or p is None:
                    return
                u.update(speaker=p.id, attribution="manual", translations={})
                u = dict(u)
            self.attributor.last_speaker = p.id
            self.hub.emit_utterance(u)
            self._translate(u)
        elif kind == "retranslate":
            with self._finals_lock:
                u = self._finals.get(msg.get("id"))
                u = dict(u) if u else None
            if u is not None:
                self._translate(u)
