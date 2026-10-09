"""Audio inputs. Every source yields mono float32 chunks at 16 kHz.

* :class:`ReachyMiniSource` reads the robot's microphone array through the
  Reachy Mini SDK. The SDK picks the transport on its own: direct GStreamer
  access when this process runs on the robot, WebRTC when it runs on another
  machine on the network. Either way this process stays separate from the
  daemon.
* :class:`MicSource` reads a microphone on this computer (``sounddevice``).
* :class:`WavFileSource` replays a WAV file in real time, for rehearsals and
  tests.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import numpy.typing as npt

SAMPLE_RATE = 16_000
Audio = npt.NDArray[np.float32]

logger = logging.getLogger(__name__)


def to_mono(samples: np.ndarray) -> Audio:
    """Average channels; accepts ``(n,)`` or ``(n, channels)``."""
    a = np.asarray(samples, dtype=np.float32)
    if a.ndim == 2:
        a = a.mean(axis=1)
    return a.astype(np.float32, copy=False)


def resample(samples: Audio, src_rate: int, dst_rate: int = SAMPLE_RATE) -> Audio:
    """Linear-interpolation resampler; good enough for speech recognition."""
    if src_rate == dst_rate or samples.size == 0:
        return samples
    n_out = int(round(samples.size * dst_rate / src_rate))
    x_old = np.arange(samples.size, dtype=np.float64)
    x_new = np.linspace(0, samples.size - 1, n_out)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class AudioSource(ABC):
    """Pull-based audio input."""

    name = "audio"
    #: Set when a finite source (a file) has delivered everything.
    finished = False

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def read(self, timeout: float = 0.2) -> Audio | None:
        """Return the next chunk, or ``None`` if nothing arrived within ``timeout``."""

    @abstractmethod
    def stop(self) -> None: ...

    def describe(self) -> str:
        return self.name


class ReachyMiniSource(AudioSource):
    """Robot microphone array through the Reachy Mini SDK.

    Pass an existing ``ReachyMini`` (app mode), or let the source open its own
    connection (standalone mode). The ReSpeaker delivers 16 kHz stereo with echo
    cancellation applied, so the robot does not transcribe its own speaker.
    """

    name = "reachy"

    def __init__(self, mini: Any = None, host: str = "reachy-mini.local", port: int = 8000) -> None:
        self._mini = mini
        self._owns_mini = mini is None
        self._host = host
        self._port = port
        self._rate = SAMPLE_RATE

    @property
    def mini(self) -> Any:
        return self._mini

    def start(self) -> None:
        if self._mini is None:
            from reachy_mini import ReachyMini  # heavy import, only when needed

            from ..robot import connection_mode_for

            logger.info("Connecting to Reachy Mini at %s...", self._host)
            self._mini = ReachyMini(host=self._host, port=self._port, media_backend="default",
                                    connection_mode=connection_mode_for(self._host))
            self._mini.__enter__()
        self._mini.media.start_recording()
        self._rate = int(self._mini.media.get_input_audio_samplerate() or SAMPLE_RATE)
        logger.info("Recording from the robot microphone at %d Hz", self._rate)

    def read(self, timeout: float = 0.2) -> Audio | None:
        deadline = time.monotonic() + timeout
        while True:
            sample = self._mini.media.get_audio_sample()
            if sample is not None and len(sample):
                return resample(to_mono(sample), self._rate)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def stop(self) -> None:
        if self._mini is None:
            return
        try:
            self._mini.media.stop_recording()
        except Exception:  # pragma: no cover - best effort on shutdown
            logger.debug("stop_recording failed", exc_info=True)
        if self._owns_mini:
            try:
                self._mini.__exit__(None, None, None)
            except Exception:  # pragma: no cover
                logger.debug("closing ReachyMini failed", exc_info=True)
            self._mini = None

    def describe(self) -> str:
        return "Reachy Mini microphone array"


class MicSource(AudioSource):
    """A microphone on this computer (needs the ``sounddevice`` package)."""

    name = "mic"

    def __init__(self, device: str | int | None = None) -> None:
        self._device: str | int | None = int(device) if isinstance(device, str) and device.isdigit() else device
        self._q: queue.Queue[Audio] = queue.Queue(maxsize=200)
        self._stream: Any = None

    def start(self) -> None:
        import sounddevice as sd

        def callback(indata: np.ndarray, frames: int, t: Any, status: Any) -> None:
            if status:
                logger.debug("mic status: %s", status)
            try:
                self._q.put_nowait(to_mono(indata).copy())
            except queue.Full:
                pass  # drop rather than block the audio thread

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=480,
            device=self._device, callback=callback,
        )
        self._stream.start()
        logger.info("Recording from local microphone %s", self._device or "(default)")

    def read(self, timeout: float = 0.2) -> Audio | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def describe(self) -> str:
        return f"local microphone {self._device or '(default)'}"


def load_wav_bytes(data: bytes) -> Audio:
    """Decode an in-memory PCM WAV into mono float32 at 16 kHz."""
    import io

    return load_wav(io.BytesIO(data))  # type: ignore[arg-type]


def load_wav(path: Any) -> Audio:
    """Read a PCM WAV file (path or file object) into mono float32 at 16 kHz."""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported WAV sample width: {width} bytes")
    if channels > 1:
        data = data.reshape(-1, channels)
    return resample(to_mono(data), rate)


class WavFileSource(AudioSource):
    """Replays a WAV file paced like a live microphone."""

    name = "file"
    CHUNK = 480  # 30 ms

    def __init__(self, path: str, loop: bool = False, speed: float = 1.0, tail_silence_s: float = 1.5) -> None:
        self._path = path
        self._loop = loop
        self._speed = max(speed, 0.01)
        self._audio = np.zeros(0, dtype=np.float32)
        self._tail = np.zeros(int(tail_silence_s * SAMPLE_RATE), dtype=np.float32)
        self._pos = 0
        self._t_next = 0.0
        self._stopped = threading.Event()
        self.finished = False

    def start(self) -> None:
        # Trailing silence lets the segmenter close the last utterance.
        self._audio = np.concatenate([load_wav(self._path), self._tail])
        self._pos = 0
        self._t_next = time.monotonic()
        logger.info("Replaying %s (%.1f s)", self._path, self._audio.size / SAMPLE_RATE)

    def read(self, timeout: float = 0.2) -> Audio | None:
        if self.finished or self._stopped.is_set():
            time.sleep(min(timeout, 0.05))
            return None
        if self._pos >= self._audio.size:
            if self._loop:
                self._pos = 0
            else:
                self.finished = True
                return None
        delay = self._t_next - time.monotonic()
        if delay > 0:
            if delay > timeout:
                time.sleep(timeout)
                return None
            time.sleep(delay)
        chunk = self._audio[self._pos : self._pos + self.CHUNK]
        self._pos += chunk.size
        self._t_next = max(self._t_next, time.monotonic() - 0.5) + chunk.size / SAMPLE_RATE / self._speed
        return chunk

    def stop(self) -> None:
        self._stopped.set()

    def describe(self) -> str:
        return f"file {self._path}"
