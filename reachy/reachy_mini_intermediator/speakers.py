"""Deciding who said an utterance.

Signals, strongest first (``--attribution auto``):

1. **push-to-talk** — a participant holds the "I'm talking" button on their phone;
2. **language** — when the two people speak different languages, the language
   Whisper hears identifies the speaker (and it is needed anyway to pick the
   translation direction);
3. **direction of arrival** — the robot's ReSpeaker array reports where speech
   comes from; with one person on each side of the robot that is enough;
4. **turn-taking** — otherwise assume people alternate.

The decision is made before recognition when possible, so Whisper can be told
which language to expect, which is both faster and more accurate.
"""

from __future__ import annotations

import collections
import math
import threading
from dataclasses import dataclass
from typing import Callable

from .config import Participant
from .engines.asr import ASRResult
from .session import Session


class DoATracker:
    """Recent direction-of-arrival readings, timestamped with wall-clock time."""

    def __init__(self, split: float = math.pi / 2, horizon_s: float = 60.0) -> None:
        self.split = split
        self._samples: collections.deque[tuple[float, float, bool]] = collections.deque(maxlen=int(horizon_s * 12))
        self._lock = threading.Lock()

    def add(self, t: float, angle: float, speech: bool) -> None:
        with self._lock:
            self._samples.append((t, angle, speech))

    def latest(self) -> tuple[float, float, bool] | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def side_of(self, angle: float) -> str:
        # DoA convention: 0 rad = robot's left, pi/2 = front, pi = right.
        return "left" if angle < self.split else "right"

    def estimate(self, t0: float, t1: float) -> tuple[str | None, float, float | None]:
        """Majority side among speech readings in ``[t0, t1]``: (side, confidence, mean angle)."""
        with self._lock:
            window = [(a, s) for (t, a, s) in self._samples if t0 <= t <= t1]
        voiced = [a for a, s in window if s] or [a for a, _ in window]
        if not voiced:
            return None, 0.0, None
        left = sum(1 for a in voiced if self.side_of(a) == "left")
        right = len(voiced) - left
        side = "left" if left >= right else "right"
        conf = max(left, right) / len(voiced)
        # Circular mean for display/calibration.
        mean = math.atan2(sum(math.sin(a) for a in voiced), sum(math.cos(a) for a in voiced))
        return side, conf, mean


@dataclass
class Attribution:
    speaker: str | None  # participant id
    method: str  # ptt | lang | doa | turn | typed | none
    doa: float | None = None


Transcribe = Callable[[str | None, list[str]], ASRResult]


class Attributor:
    def __init__(self, session: Session, mode: str = "auto", doa: DoATracker | None = None,
                 doa_min_confidence: float = 0.65) -> None:
        self.session = session
        self.mode = mode
        self.doa = doa
        self.doa_min_confidence = doa_min_confidence
        self.last_speaker: str | None = None
        self._commit = True  # only touched from the single recognition thread

    def _allowed(self, method: str) -> bool:
        return self.mode == "auto" or self.mode == method

    def doa_guess(self, t0: float, t1: float) -> tuple[Participant | None, float | None]:
        if self.doa is None:
            return None, None
        side, conf, mean = self.doa.estimate(t0, t1)
        if side is None or conf < self.doa_min_confidence:
            return None, mean
        return self.session.by_side(side), mean

    def resolve(self, t0: float, t1: float, transcribe: Transcribe,
                commit: bool = True) -> tuple[Attribution, ASRResult]:
        """Attribute ``[t0, t1]`` and transcribe it with the best language hint.

        ``commit=False`` (partial transcripts) leaves the turn-taking memory untouched.
        """
        self._commit = commit
        langs = self.session.languages()
        doa_p, doa_angle = self.doa_guess(t0, t1)

        if self._allowed("ptt"):
            pid = self.session.ptt_speaker(t0, t1)
            p = self.session.participant(pid)
            if p is not None:
                return self._done(Attribution(p.id, "ptt", doa_angle), transcribe(p.lang, langs))

        if self._allowed("lang") and len(langs) > 1:
            result = transcribe(None, langs)
            p = self.session.by_lang(result.language)
            if p is not None:
                return self._done(Attribution(p.id, "lang", doa_angle), result)
            # Language not tied to one person: fall through but reuse the text.
            return self._done(self._fallback(doa_p, doa_angle), result)

        if self._allowed("doa") and doa_p is not None:
            return self._done(Attribution(doa_p.id, "doa", doa_angle), transcribe(doa_p.lang, langs))

        attr = self._fallback(None if self.mode == "turn" else doa_p, doa_angle)
        p = self.session.participant(attr.speaker)
        return self._done(attr, transcribe(p.lang if p else None, langs))

    def _fallback(self, doa_p: Participant | None, doa_angle: float | None) -> Attribution:
        if doa_p is not None and self._allowed("doa"):
            return Attribution(doa_p.id, "doa", doa_angle)
        if self._allowed("turn"):
            nxt = self.session.other(self.last_speaker) if self.last_speaker else None
            if nxt is None:
                parts = self.session.participants()
                nxt = parts[0] if parts else None
            if nxt is not None:
                return Attribution(nxt.id, "turn", doa_angle)
        return Attribution(None, "none", doa_angle)

    def _done(self, attr: Attribution, result: ASRResult) -> tuple[Attribution, ASRResult]:
        if self._commit and attr.speaker is not None and result.text:
            self.last_speaker = attr.speaker
        return attr, result
