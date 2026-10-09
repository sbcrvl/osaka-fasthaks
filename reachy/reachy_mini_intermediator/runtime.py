"""Wires settings into a running server: audio source, engines, robot, web.

Both launchers end up here:

* :mod:`.cli` (standalone process, any machine on the robot's network);
* :mod:`.main` (Reachy Mini app, started from the dashboard), which hands over
  its already-connected ``ReachyMini``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import socket
import threading
from typing import Any

from .audio.sources import AudioSource, MicSource, ReachyMiniSource, WavFileSource
from .config import Settings
from .engines.asr import make_asr
from .engines.shisa import SHISA_LANGS
from .engines.translate import make_translator
from .hub import Hub
from .pipeline import Pipeline
from .robot import DoAPoller, Gestures, connection_mode_for, daemon_host_for
from .session import Session
from .speakers import DoATracker
from .publisher import HashiPublisher
from .voice import LocalSpeaker, RobotSpeaker, Voice

logger = logging.getLogger(__name__)


def lan_addresses() -> list[str]:
    """Best-effort list of this machine's LAN addresses, for join links."""
    addrs: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: nothing is sent
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    host = socket.gethostname()
    if host and not host.endswith(".local"):
        host += ".local"
    if host:
        addrs.append(host)
    return [a for a in dict.fromkeys(addrs) if a and not a.startswith("127.")]


def resolve_engines(settings: Settings) -> tuple[str, str]:
    """Turn ``auto`` into concrete engine specs, based on available keys and packages."""
    langs = {p.lang for p in settings.participants}
    shisa_ok = bool(settings.shisa_api_key) and langs <= set(SHISA_LANGS)
    asr = settings.asr
    if asr == "auto":
        if shisa_ok:
            asr = "shisa-realtime"
        elif importlib.util.find_spec("faster_whisper") is not None:
            asr = "faster-whisper:small"
        elif settings.asr_api_key:
            asr = "openai:whisper-1"
        else:
            asr = "faster-whisper:small"  # fails with an install hint in the UI
    translator = settings.translator
    if translator == "auto":
        if shisa_ok:
            translator = "shisa"
        elif settings.translator_api_key or os.environ.get("ANTHROPIC_API_KEY"):
            translator = "claude:claude-haiku-5-5"
        else:
            translator = "none"
            logger.warning("No translation engine configured (set SHISA_API_KEY or ANTHROPIC_API_KEY); "
                           "showing transcripts only")
    return asr, translator


class Runtime:
    """Owns every long-lived component and their start/stop order."""

    def __init__(self, settings: Settings, mini: Any = None) -> None:
        self.settings = settings
        self.mini = mini
        self._owns_mini = False
        self.session = Session(settings.participants)
        self.hub = Hub(self.session)
        self.doa: DoATracker | None = None
        self.doa_poller: DoAPoller | None = None
        self.gestures: Gestures | None = None
        self.voice: Voice | None = None
        self.publisher: HashiPublisher | None = None
        self.pipeline: Pipeline | None = None
        self._errors: list[str] = []

    # ---------------------------------------------------------------- build
    def _connect_robot(self) -> None:
        if self.mini is not None or not self.settings.wants_robot:
            return
        from reachy_mini import ReachyMini

        mode = connection_mode_for(self.settings.robot_host)
        logger.info("Connecting to Reachy Mini at %s (mode %s)...", self.settings.robot_host, mode)
        # Always "default": "no_media" would make the daemon release the audio
        # hardware, which also stops the microphone for everyone else.
        self.mini = ReachyMini(host=self.settings.robot_host, port=self.settings.daemon_port,
                               connection_mode=mode, media_backend="default")
        self._owns_mini = True

    def _make_source(self) -> AudioSource:
        s = self.settings
        if s.source_kind == "reachy":
            return ReachyMiniSource(self.mini, s.robot_host, s.daemon_port)
        if s.source_kind == "mic":
            return MicSource(s.mic_device)
        path = s.source.split(":", 1)[1] if ":" in s.source else ""
        if not path:
            raise ValueError("file source needs a path: --source file:conversation.wav")
        return WavFileSource(path, loop=s.file_loop, speed=s.file_speed)

    def build(self) -> None:
        s = self.settings
        asr_spec, translator_spec = resolve_engines(s)
        names = lambda: [p.name for p in self.session.participants() if not p.name.startswith("Guest ")]  # noqa: E731

        try:
            self._connect_robot()
        except Exception as e:
            logger.exception("Could not connect to the robot")
            self._errors.append(f"Robot unavailable: {e}")

        source = None
        try:
            source = self._make_source()
        except Exception as e:
            self._errors.append(f"Audio input unavailable: {e}")

        asr = None
        try:
            asr = make_asr(asr_spec, s, hotwords=names())
        except Exception as e:
            logger.error("Speech recognition unavailable: %s", e)
            self._errors.append(f"Speech recognition unavailable: {e}")

        translator = None
        try:
            translator = make_translator(translator_spec, s, keywords=names)
        except Exception as e:
            logger.error("Translation unavailable: %s", e)
            self._errors.append(f"Translation unavailable: {e}")

        if s.doa and self.mini is not None:
            self.doa = DoATracker(split=s.doa_split)
            host = daemon_host_for(self.mini, s.robot_host, s.daemon_port)
            self.doa_poller = DoAPoller(self.doa, host, s.daemon_port)

        if s.gestures and self.mini is not None:
            self.gestures = Gestures(self.mini, s.look_yaw_deg)

        try:
            self.voice = self._make_voice()
        except Exception as e:
            logger.error("Voice unavailable: %s", e)
            self._errors.append(f"Voice unavailable: {e}")

        if s.publish:
            def publish_error(msg: str | None) -> None:
                current = self.hub.status.get("error") or ""
                if msg is not None or current.startswith("App server"):
                    self.hub.emit_status(error=msg)

            self.publisher = HashiPublisher(
                s.publish, s.publish_map(),
                languages=lambda pid: (self.session.participant(pid).lang if self.session.participant(pid) else None),
                on_error=publish_error)

        if source is None:
            asr = None  # nothing to listen to; typed messages still work
        self.pipeline = Pipeline(s, self.session, self.hub, source, asr, translator, self.doa, self.gestures,
                                 self.voice, self.publisher)
        self.hub.info = {
            "source": source.describe() if source else None,
            "asr": asr.describe() if asr else None,
            "translator": translator.describe() if translator else None,
            "vad": self.pipeline.segmenter.classifier.backend if asr and not self.pipeline.streaming else None,
            "partials": self.pipeline.partials,
            "attribution": s.attribution,
            "doa": self.doa is not None,
            "gestures": self.gestures is not None,
            "publish": s.publish,
            "voice": f"{self.voice.tts.describe()} → {self.voice.speaker.describe()}" if self.voice else None,
            "port": s.port,
            "hosts": lan_addresses(),
        }
        logger.info("Audio: %s | ASR: %s | translation: %s", self.hub.info["source"], self.hub.info["asr"],
                    self.hub.info["translator"])

    def _make_voice(self) -> Voice | None:
        s = self.settings
        tts_kind = s.tts
        if tts_kind == "auto":
            tts_kind = "shisa" if s.shisa_api_key else "none"
        speak = s.speak
        if speak == "auto":
            speak = "robot" if self.mini is not None else "off"
        if tts_kind == "none" or speak == "off":
            return None
        if speak == "robot" and self.mini is None:
            raise RuntimeError("--speak robot needs a robot connection")
        from .engines.shisa import ShisaTTS

        tts = ShisaTTS(s.shisa_api_key, voices=s.voice_map())
        speaker = RobotSpeaker(self.mini) if speak == "robot" else LocalSpeaker()

        def mute(until: float) -> None:
            self.session.mic_muted_until = max(self.session.mic_muted_until, until)

        return Voice(tts, speaker,
                     on_state=lambda st: self.hub.emit_status(speaking=st),
                     on_error=lambda msg: self.hub.emit_status(error=msg),
                     mute_mic=mute, enabled=lambda: self.session.speak_enabled)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Called from the server's startup, once the event loop exists."""
        if self.pipeline is None:
            self.build()
        if self._errors:
            self.hub.emit_status(error=" · ".join(self._errors))
        if self.doa_poller is not None:
            self.doa_poller.start()
        if self.gestures is not None:
            self.gestures.start()
        if self.voice is not None:
            self.voice.start()
        if self.publisher is not None:
            self.publisher.start()
        assert self.pipeline is not None
        self.pipeline.start()

    def stop(self) -> None:
        if self.pipeline is not None:
            self.pipeline.stop()
        if self.doa_poller is not None:
            self.doa_poller.stop()
        if self.voice is not None:
            self.voice.stop()
        if self.publisher is not None:
            self.publisher.stop()
        if self.gestures is not None:
            self.gestures.stop()
            self.gestures.join(timeout=3.0)
        if self._owns_mini and self.mini is not None:
            try:
                self.mini.__exit__(None, None, None)
            except Exception:
                logger.debug("closing ReachyMini failed", exc_info=True)

    def serve(self, stop_event: threading.Event | None = None) -> None:
        """Run the web server in this thread until it exits or ``stop_event`` is set."""
        import uvicorn

        from .server import create_app

        self.build()
        config = uvicorn.Config(create_app(self), host=self.settings.host, port=self.settings.port,
                                log_level=self.settings.log_level.lower(), ws_ping_interval=20)
        server = uvicorn.Server(config)
        if stop_event is not None:
            def watch() -> None:
                stop_event.wait()
                server.should_exit = True

            threading.Thread(target=watch, name="stop-watch", daemon=True).start()
        server.run()
