"""Streaming speech segmentation.

Audio arrives in arbitrary chunk sizes, is re-blocked into 30 ms frames and
classified as speech or not. A small state machine turns the frame decisions
into utterances:

* an utterance starts once most frames in a 300 ms window are speech (the
  window is kept as pre-roll so the first syllable is not cut);
* it ends after ``end_silence_ms`` of non-speech, or is cut at
  ``max_utterance_s`` so long monologues still get transcribed;
* utterances shorter than ``min_utterance_s`` are dropped as noise.

Frame classification uses WebRTC VAD when available, gated by an adaptive
energy threshold so far-away chatter in a busy demo hall does not trigger it.
Without ``webrtcvad`` an energy-only detector is used.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field

import numpy as np

from .sources import SAMPLE_RATE, Audio

logger = logging.getLogger(__name__)

FRAME_MS = 30
FRAME_LEN = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples


class FrameClassifier:
    """Speech / non-speech for one 30 ms frame, with an adaptive noise floor."""

    def __init__(self, aggressiveness: int = 2, energy_ratio: float = 1.8) -> None:
        self.energy_ratio = energy_ratio
        self.noise_floor = 0.003
        self._vad = None
        try:
            import webrtcvad

            self._vad = webrtcvad.Vad(int(aggressiveness))
        except ImportError:
            logger.warning("webrtcvad not installed; using an energy-only voice detector")

    @property
    def backend(self) -> str:
        return "webrtcvad" if self._vad is not None else "energy"

    def __call__(self, frame: Audio, in_speech: bool) -> tuple[bool, float]:
        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
        if self._vad is not None:
            pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype("<i2").tobytes()
            voiced = self._vad.is_speech(pcm, SAMPLE_RATE)
            if voiced and self.energy_ratio > 0:
                voiced = rms > self.noise_floor * self.energy_ratio
        else:
            voiced = rms > max(self.noise_floor * max(self.energy_ratio, 1.0) * 1.7, 0.008)
        if not voiced and not in_speech:
            # Track the background level only while nobody talks.
            alpha = 0.05 if rms > self.noise_floor else 0.2
            self.noise_floor = max(0.0008, (1 - alpha) * self.noise_floor + alpha * rms)
        return voiced, rms


@dataclass
class SpeechStart:
    index: int
    t_start: float  # wall clock (epoch seconds)
    continuation: bool = False


@dataclass
class SpeechEnd:
    index: int
    audio: Audio = field(repr=False)
    t_start: float
    t_end: float
    forced: bool = False  # cut by max_utterance_s rather than by silence


@dataclass
class SpeechDiscard:
    index: int


Event = SpeechStart | SpeechEnd | SpeechDiscard


class Segmenter:
    """Feed audio chunks in, get :class:`SpeechStart` / :class:`SpeechEnd` events out."""

    def __init__(
        self,
        aggressiveness: int = 2,
        energy_ratio: float = 1.8,
        end_silence_ms: int = 700,
        min_utterance_s: float = 0.35,
        max_utterance_s: float = 15.0,
        start_window_ms: int = 300,
        start_ratio: float = 0.6,
        keep_trailing_ms: int = 240,
    ) -> None:
        self.classifier = FrameClassifier(aggressiveness, energy_ratio)
        self._start_frames = max(1, start_window_ms // FRAME_MS)
        self._start_ratio = start_ratio
        self._end_frames = max(1, end_silence_ms // FRAME_MS)
        self._keep_trailing = max(0, keep_trailing_ms // FRAME_MS)
        self._min_samples = int(min_utterance_s * SAMPLE_RATE)
        self._max_samples = int(max_utterance_s * SAMPLE_RATE)
        self._pending = np.zeros(0, dtype=np.float32)
        self._ring: collections.deque[tuple[Audio, bool]] = collections.deque(maxlen=self._start_frames)
        self._frames: list[Audio] = []
        self._silence_run = 0
        self._in_speech = False
        self._index = -1
        self._t_start = 0.0
        self.level = 0.0  # smoothed RMS for meters

    # --------------------------------------------------------------- properties
    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def utterance_index(self) -> int:
        return self._index

    @property
    def current_samples(self) -> int:
        return sum(f.size for f in self._frames) if self._in_speech else 0

    def current_audio(self) -> Audio:
        """Audio of the utterance in progress (copy), for partial transcripts."""
        if not self._in_speech or not self._frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._frames)

    # ------------------------------------------------------------------- input
    def reset(self) -> list[Event]:
        """Drop everything (e.g. when listening is paused)."""
        events: list[Event] = []
        if self._in_speech:
            events.append(SpeechDiscard(self._index))
        self._pending = np.zeros(0, dtype=np.float32)
        self._ring.clear()
        self._frames = []
        self._in_speech = False
        self._silence_run = 0
        return events

    def flush(self, now: float) -> list[Event]:
        """Close the utterance in progress (end of a file, shutdown)."""
        if not self._in_speech:
            return []
        return [self._end(now, forced=True)]

    def feed(self, chunk: Audio, now: float) -> list[Event]:
        """Process a chunk whose last sample was captured at wall time ``now``."""
        events: list[Event] = []
        data = np.concatenate([self._pending, chunk]) if self._pending.size else chunk
        n_frames = data.size // FRAME_LEN
        self._pending = data[n_frames * FRAME_LEN :].copy()
        # Wall time of the end of each frame, counted back from `now`.
        t_last_frame_end = now - self._pending.size / SAMPLE_RATE

        for i in range(n_frames):
            frame = data[i * FRAME_LEN : (i + 1) * FRAME_LEN]
            t_frame_end = t_last_frame_end - (n_frames - 1 - i) * FRAME_MS / 1000.0
            voiced, rms = self.classifier(frame, self._in_speech)
            self.level = 0.7 * self.level + 0.3 * rms

            if not self._in_speech:
                self._ring.append((frame, voiced))
                n_voiced = sum(1 for _, v in self._ring if v)
                if len(self._ring) == self._ring.maxlen and n_voiced >= self._start_ratio * self._ring.maxlen:
                    self._begin(t_frame_end - len(self._ring) * FRAME_MS / 1000.0, events)
                continue

            self._frames.append(frame.copy())
            self._silence_run = 0 if voiced else self._silence_run + 1
            if self._silence_run >= self._end_frames:
                events.append(self._end(t_frame_end, forced=False))
            elif self.current_samples >= self._max_samples:
                events.append(self._end(t_frame_end, forced=True))
                # Keep listening: the speaker is probably still talking.
                self._index += 1
                self._in_speech = True
                self._frames = []
                self._silence_run = 0
                self._t_start = t_frame_end
                events.append(SpeechStart(self._index, self._t_start, continuation=True))
        return events

    # ----------------------------------------------------------------- helpers
    def _begin(self, t_start: float, events: list[Event]) -> None:
        self._index += 1
        self._in_speech = True
        self._frames = [f.copy() for f, _ in self._ring]  # pre-roll
        self._ring.clear()
        self._silence_run = 0
        self._t_start = t_start
        events.append(SpeechStart(self._index, t_start))

    def _end(self, t_end: float, forced: bool) -> Event:
        frames = self._frames
        if not forced and self._silence_run > self._keep_trailing:
            drop = self._silence_run - self._keep_trailing
            frames = frames[: len(frames) - drop]
            t_end -= drop * FRAME_MS / 1000.0
        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        index, t_start = self._index, self._t_start
        self._frames = []
        self._in_speech = False
        self._silence_run = 0
        if audio.size < self._min_samples:
            return SpeechDiscard(index)
        return SpeechEnd(index, audio, t_start, t_end, forced)
