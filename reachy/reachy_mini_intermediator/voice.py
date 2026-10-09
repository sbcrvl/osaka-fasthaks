"""Speaking translations aloud: the robot as an interpreter.

After a translation arrives it is synthesized (Shisa TTS) and played on the
robot's speaker, one at a time, in conversation order. While the robot talks,
transcription ignores the microphone so the robot does not transcribe itself
(the ReSpeaker's echo cancellation already removes most of it; this makes it
certain).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from .audio.sources import SAMPLE_RATE, Audio

logger = logging.getLogger(__name__)


def loudest(audio: Audio, peak: float = 0.98) -> Audio:
    """Scale to full volume: Shisa voices peak at only 0.2-0.5 of full scale."""
    top = float(np.abs(audio).max()) if audio.size else 0.0
    return (audio * (peak / top)).astype(np.float32) if top > 1e-4 else audio


class RobotSpeaker:
    """Plays audio on the Reachy Mini speaker through the SDK (local or WebRTC)."""

    def __init__(self, mini: Any) -> None:
        self.mini = mini
        self._started = False

    def play(self, audio: Audio) -> float:
        media = self.mini.media
        if not self._started:
            media.start_playing()
            self._started = True
        rate = int(media.get_output_audio_samplerate() or SAMPLE_RATE)
        if rate != SAMPLE_RATE:
            from .audio.sources import resample

            audio = resample(audio, SAMPLE_RATE, rate)
        step = rate // 10  # 100 ms chunks keep the player's buffer short
        for i in range(0, audio.size, step):
            media.push_audio_sample(np.ascontiguousarray(audio[i : i + step], dtype=np.float32))
        return audio.size / rate

    def describe(self) -> str:
        return "robot speaker"


class LocalSpeaker:
    """Plays audio on this computer (``sounddevice``), for robot-less rehearsals."""

    def play(self, audio: Audio) -> float:
        import sounddevice as sd

        sd.play(audio, SAMPLE_RATE)
        return audio.size / SAMPLE_RATE

    def describe(self) -> str:
        return "local speaker"


class Voice(threading.Thread):
    """Queue of things to say; synthesizes and plays them in order."""

    def __init__(self, tts: Any, speaker: Any, on_state: Callable[[dict[str, Any] | None], None],
                 on_error: Callable[[str], None] | None = None,
                 mute_mic: Callable[[float], None] | None = None,
                 enabled: Callable[[], bool] = lambda: True, tail_s: float = 0.4) -> None:
        super().__init__(name="voice", daemon=True)
        self.tts = tts
        self.speaker = speaker
        self.on_state = on_state
        self.on_error = on_error or (lambda msg: None)
        self.mute_mic = mute_mic
        self.tail_s = tail_s
        self.enabled = enabled
        self._q: queue.Queue[tuple[str, str, str] | None] = queue.Queue(maxsize=20)
        self._halt = threading.Event()

    def say(self, uid: str, text: str, lang: str) -> None:
        if not self.enabled() or not text.strip():
            return
        try:
            self._q.put_nowait((uid, text, lang))
        except queue.Full:
            logger.warning("Voice queue full; skipping %s", uid)

    def stop(self) -> None:
        self._halt.set()
        self._q.put(None)

    def run(self) -> None:
        while not self._halt.is_set():
            item = self._q.get()
            if item is None:
                return
            uid, text, lang = item
            if not self.enabled():
                continue
            try:
                audio = loudest(self.tts.synthesize(text, lang))
                duration = audio.size / SAMPLE_RATE
                if self.mute_mic is not None:
                    self.mute_mic(time.time() + duration + self.tail_s)
                self.on_state({"id": uid, "lang": lang})
                self.speaker.play(audio)
                self._halt.wait(duration + 0.1)  # pushes return early; wait for the sound
            except Exception as e:
                logger.warning("Could not speak %s: %s", uid, e)
                self.on_error(f"Text to speech failed: {str(e)[:200]}")
                time.sleep(0.2)
            finally:
                self.on_state(None)
