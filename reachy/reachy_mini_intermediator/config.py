"""Runtime settings, read from the command line or ``INTERMEDIATOR_*`` env vars.

The same :class:`Settings` drives both launch modes:

* standalone (``reachy-mini-intermediator ...``), where the CLI flags are parsed;
* the Reachy Mini app, where the dashboard starts the app without arguments,
  so only environment variables (and defaults) apply.
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass, field
from typing import Any

ENV_PREFIX = "INTERMEDIATOR_"

# Where to look for SHISA_API_KEY & co. when they are not in the environment:
# this folder, then the repo's shisa/ and mobile-app/server/ (all git-ignored).
ENV_FILES = (".env", "../shisa/.env", "../mobile-app/server/.env")


def read_env_files(paths: tuple[str, ...] = ENV_FILES, base: str | None = None) -> dict[str, str]:
    """``KEY=value`` lines from the first files that exist; earlier files win."""
    found: dict[str, str] = {}
    # Relative to the cwd, then to reachy/ itself (editable install), so the key
    # is found whichever directory the command is started from.
    roots = [base] if base else [os.getcwd(), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for path in dict.fromkeys(os.path.normpath(os.path.join(r, rel)) for r in roots for rel in paths):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            found.setdefault(key, value.strip().strip('"').strip("'"))
    return found


@dataclass
class Participant:
    """One side of the conversation."""

    id: str  # "a" or "b"
    name: str
    lang: str  # ISO 639-1 code
    side: str  # "left" or "right" of the robot, as seen from the robot

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "lang": self.lang, "side": self.side}

    @classmethod
    def parse(cls, pid: str, spec: str, default: "Participant") -> "Participant":
        """Parse ``"Name:lang:side"``; any field may be left empty."""
        parts = (spec.split(":") + ["", "", ""])[:3]
        name = parts[0].strip() or default.name
        lang = parts[1].strip().lower() or default.lang
        side = parts[2].strip().lower() or default.side
        if side not in ("left", "right"):
            raise ValueError(f"participant {pid}: side must be 'left' or 'right', got {side!r}")
        return cls(id=pid, name=name, lang=lang, side=side)


def default_participants() -> list[Participant]:
    return [
        Participant(id="a", name="Guest A", lang="en", side="left"),
        Participant(id="b", name="Guest B", lang="ja", side="right"),
    ]


@dataclass
class Settings:
    """Everything that can be configured. Field names map to CLI flags and env vars."""

    # Web server (client pages + WebSocket)
    host: str = "0.0.0.0"
    port: int = 8042

    # Audio input: "reachy", "mic", or "file:<path.wav>"
    source: str = "reachy"
    robot_host: str = "reachy-mini.local"
    daemon_port: int = 8000
    mic_device: str | None = None
    file_loop: bool = False
    file_speed: float = 1.0

    # Speech recognition: "auto", "shisa-realtime", "shisa", "faster-whisper:<model>",
    # "openai:<model>", "script:<path>"
    asr: str = "auto"
    asr_device: str = "auto"
    asr_compute_type: str = "default"
    asr_beam_size: int = 5
    asr_base_url: str = "https://api.openai.com/v1"
    asr_api_key: str | None = None
    partials: str = "auto"  # auto | on | off
    partial_interval: float = 0.8

    # Translation: "auto", "shisa", "claude:<model>", "openai:<model>", "echo", "none"
    translator: str = "auto"
    translator_base_url: str = "https://api.openai.com/v1"
    translator_api_key: str | None = None
    translation_context: int = 6

    # Shisa.AI (https://docs.shisa.ai): --asr shisa-realtime | shisa, --translator shisa
    shisa_api_key: str | None = None
    shisa_language: str = "auto"  # auto | ja | en | zh
    shisa_default_language: str = "ja"
    shisa_detection: str = "utterance"  # utterance | session

    # Publish final utterances to the mobile app's server (Hashi), e.g. ws://192.168.1.20:8080
    publish: str | None = None
    publish_users: str = "a=person-1,b=person-2"

    # Speaking translations aloud: tts auto | shisa | none; speak auto | robot | local | off
    tts: str = "auto"
    tts_voices: str = ""  # "ja=<voice id>,en=<voice id>"
    speak: str = "auto"

    # Voice activity detection / segmentation
    vad_aggressiveness: int = 2
    vad_energy_ratio: float = 1.8
    end_silence_ms: int = 700
    min_utterance_s: float = 0.35
    max_utterance_s: float = 15.0

    # Who is speaking: auto | ptt | lang | doa | turn
    attribution: str = "auto"
    doa: bool = True
    doa_split: float = math.pi / 2  # DoA angle (rad) separating left from right
    doa_min_confidence: float = 0.65

    # Robot body language (needs a robot connection)
    gestures: bool = False
    look_yaw_deg: float = 28.0

    participants: list[Participant] = field(default_factory=default_participants)
    log_level: str = "INFO"

    # ------------------------------------------------------------------ helpers
    @property
    def source_kind(self) -> str:
        return self.source.split(":", 1)[0]

    @property
    def wants_robot(self) -> bool:
        """True when some feature needs a Reachy Mini SDK connection."""
        return self.source_kind == "reachy" or self.gestures

    def publish_map(self) -> dict[str, str]:
        out = {}
        for item in self.publish_users.split(","):
            pid, sep, uid = item.partition("=")
            if sep and pid.strip() and uid.strip():
                out[pid.strip()] = uid.strip()
        return out

    def voice_map(self) -> dict[str, str]:
        out = {}
        for item in self.tts_voices.split(","):
            lang, sep, vid = item.partition("=")
            if sep and lang.strip() and vid.strip():
                out[lang.strip().lower()] = vid.strip()
        return out

    def participant(self, pid: str) -> Participant | None:
        return next((p for p in self.participants if p.id == pid), None)

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        """Build settings from defaults overridden by ``INTERMEDIATOR_*`` variables."""
        return cls.from_args([], environ)

    @classmethod
    def from_args(cls, argv: list[str] | None, environ: dict[str, str] | None = None) -> "Settings":
        if environ is None:
            # Real environment wins over .env files.
            env = {**read_env_files(), **os.environ}
        else:
            env = dict(environ)
        parser = build_parser(env)
        ns = parser.parse_args(argv)
        s = cls()
        for name in vars(ns):
            if name in ("participant_a", "participant_b"):
                continue
            setattr(s, name, getattr(ns, name))
        defaults = default_participants()
        s.participants = [
            Participant.parse("a", ns.participant_a or "", defaults[0]),
            Participant.parse("b", ns.participant_b or "", defaults[1]),
        ]
        # API keys fall back to the providers' conventional variables.
        if s.shisa_api_key is None:
            s.shisa_api_key = env.get("SHISA_API_KEY")
        if s.asr_api_key is None:
            s.asr_api_key = env.get("OPENAI_API_KEY")
        if s.translator_api_key is None:
            if s.translator.startswith("claude"):
                s.translator_api_key = env.get("ANTHROPIC_API_KEY")
            else:
                s.translator_api_key = env.get("OPENAI_API_KEY")
        s.validate()
        return s

    def validate(self) -> None:
        if self.source_kind not in ("reachy", "mic", "file"):
            raise ValueError(f"unknown source {self.source!r} (use reachy, mic or file:<path>)")
        if self.attribution not in ("auto", "ptt", "lang", "doa", "turn"):
            raise ValueError(f"unknown attribution mode {self.attribution!r}")
        if self.speak not in ("auto", "robot", "local", "off") or self.tts not in ("auto", "shisa", "none"):
            raise ValueError("speak must be auto/robot/local/off and tts auto/shisa/none")
        if self.partials not in ("auto", "on", "off"):
            raise ValueError("partials must be auto, on or off")
        if len({p.id for p in self.participants}) != len(self.participants):
            raise ValueError("participant ids must be unique")

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to show to clients (no keys)."""
        return {
            "source": self.source,
            "asr": self.asr,
            "translator": self.translator,
            "attribution": self.attribution,
            "doa": self.doa,
            "gestures": self.gestures,
        }


def _env_default(env: dict[str, str], name: str, default: Any) -> Any:
    raw = env.get(ENV_PREFIX + name.upper())
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def build_parser(env: dict[str, str]) -> argparse.ArgumentParser:
    d = Settings()
    e = lambda name, default: _env_default(env, name, default)  # noqa: E731

    p = argparse.ArgumentParser(
        prog="reachy-mini-intermediator",
        description=(
            "Live transcription + translation between two people, streamed to their "
            "devices over WebSocket. Runs on its own; the robot is just a microphone "
            "(and, optionally, a body). Every flag can also be set with an "
            f"{ENV_PREFIX}<FLAG> environment variable."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    g = p.add_argument_group("server")
    g.add_argument("--host", default=e("host", d.host))
    g.add_argument("--port", type=int, default=e("port", d.port))

    g = p.add_argument_group("audio input")
    g.add_argument("--source", default=e("source", d.source),
                   help="reachy (robot mic via the SDK), mic (this computer), or file:<path.wav>")
    g.add_argument("--robot-host", dest="robot_host", default=e("robot_host", d.robot_host),
                   help="Reachy Mini daemon host (localhost is tried first)")
    g.add_argument("--daemon-port", dest="daemon_port", type=int, default=e("daemon_port", d.daemon_port))
    g.add_argument("--mic-device", dest="mic_device", default=e("mic_device", d.mic_device),
                   help="sounddevice input device name or index (for --source mic)")
    g.add_argument("--file-loop", dest="file_loop", action=argparse.BooleanOptionalAction,
                   default=e("file_loop", d.file_loop))
    g.add_argument("--file-speed", dest="file_speed", type=float, default=e("file_speed", d.file_speed),
                   help="playback speed for file sources (2.0 = twice real time)")

    g = p.add_argument_group("speech recognition")
    g.add_argument("--asr", default=e("asr", d.asr),
                   help="auto, shisa-realtime[:auto|ja|en|zh], shisa, faster-whisper:<tiny|base|small|medium|"
                        "large-v3|...>, openai:<model>, or script:<path.txt>. auto = shisa-realtime when "
                        "SHISA_API_KEY is set, else local faster-whisper")
    g.add_argument("--asr-device", dest="asr_device", default=e("asr_device", d.asr_device),
                   help="faster-whisper device: auto, cpu, cuda")
    g.add_argument("--asr-compute-type", dest="asr_compute_type",
                   default=e("asr_compute_type", d.asr_compute_type),
                   help="faster-whisper compute type: default, int8, float16, ...")
    g.add_argument("--asr-beam-size", dest="asr_beam_size", type=int, default=e("asr_beam_size", d.asr_beam_size))
    g.add_argument("--asr-base-url", dest="asr_base_url", default=e("asr_base_url", d.asr_base_url),
                   help="OpenAI-compatible transcription endpoint (e.g. a local speaches server)")
    g.add_argument("--asr-api-key", dest="asr_api_key", default=e("asr_api_key", d.asr_api_key),
                   help="defaults to $OPENAI_API_KEY")
    g.add_argument("--partials", default=e("partials", d.partials), choices=["auto", "on", "off"],
                   help="stream live (non-final) transcripts; auto = on for local ASR")
    g.add_argument("--partial-interval", dest="partial_interval", type=float,
                   default=e("partial_interval", d.partial_interval), help="seconds of new audio between partials")

    g = p.add_argument_group("translation")
    g.add_argument("--translator", default=e("translator", d.translator),
                   help="auto, shisa[:<model>], claude:<model>, openai:<model> (any OpenAI-compatible chat "
                        "API, e.g. Ollama), echo, none. auto = shisa with SHISA_API_KEY, else claude with "
                        "ANTHROPIC_API_KEY, else none")
    g.add_argument("--translator-base-url", dest="translator_base_url",
                   default=e("translator_base_url", d.translator_base_url),
                   help="base URL for openai:<model> (e.g. http://localhost:11434/v1 for Ollama)")
    g.add_argument("--translator-api-key", dest="translator_api_key",
                   default=e("translator_api_key", d.translator_api_key),
                   help="defaults to $ANTHROPIC_API_KEY or $OPENAI_API_KEY")
    g.add_argument("--translation-context", dest="translation_context", type=int,
                   default=e("translation_context", d.translation_context),
                   help="previous turns given to the translator for context")

    g = p.add_argument_group("Shisa.AI")
    g.add_argument("--shisa-api-key", dest="shisa_api_key", default=e("shisa_api_key", d.shisa_api_key),
                   help="defaults to $SHISA_API_KEY")
    g.add_argument("--shisa-language", dest="shisa_language", choices=["auto", "ja", "en", "zh"],
                   default=e("shisa_language", d.shisa_language),
                   help="realtime ASR language; auto detects it")
    g.add_argument("--shisa-default-language", dest="shisa_default_language", choices=["ja", "en", "zh"],
                   default=e("shisa_default_language", d.shisa_default_language),
                   help="fallback when auto detection is unsure")
    g.add_argument("--shisa-detection", dest="shisa_detection", choices=["utterance", "session"],
                   default=e("shisa_detection", d.shisa_detection),
                   help="auto detection per utterance (bilingual talk) or once per session")

    g = p.add_argument_group("mobile app server")
    g.add_argument("--publish", default=e("publish", d.publish),
                   help="Hashi WebSocket URL to publish final utterances to, e.g. ws://192.168.1.20:8080")
    g.add_argument("--publish-users", dest="publish_users", default=e("publish_users", d.publish_users),
                   help="participant → Hashi userId mapping")

    g = p.add_argument_group("voice (the robot reads translations aloud)")
    g.add_argument("--tts", default=e("tts", d.tts), choices=["auto", "shisa", "none"],
                   help="text to speech engine; auto = shisa when SHISA_API_KEY is set")
    g.add_argument("--tts-voices", dest="tts_voices", default=e("tts_voices", d.tts_voices),
                   help='pin voices per language, e.g. "ja=<voice id>,en=<voice id>" (GET /tts/voices)')
    g.add_argument("--speak", default=e("speak", d.speak), choices=["auto", "robot", "local", "off"],
                   help="where to play it; auto = the robot speaker when connected")

    g = p.add_argument_group("segmentation")
    g.add_argument("--vad-aggressiveness", dest="vad_aggressiveness", type=int, choices=[0, 1, 2, 3],
                   default=e("vad_aggressiveness", d.vad_aggressiveness))
    g.add_argument("--vad-energy-ratio", dest="vad_energy_ratio", type=float,
                   default=e("vad_energy_ratio", d.vad_energy_ratio),
                   help="speech must be this many times louder than the noise floor (0 disables)")
    g.add_argument("--end-silence-ms", dest="end_silence_ms", type=int, default=e("end_silence_ms", d.end_silence_ms))
    g.add_argument("--min-utterance-s", dest="min_utterance_s", type=float,
                   default=e("min_utterance_s", d.min_utterance_s))
    g.add_argument("--max-utterance-s", dest="max_utterance_s", type=float,
                   default=e("max_utterance_s", d.max_utterance_s))

    g = p.add_argument_group("participants")
    g.add_argument("--a", dest="participant_a", default=env.get(ENV_PREFIX + "A"),
                   help='participant A as "Name:lang:side", e.g. "Aiko:ja:left" (default Guest A:en:left)')
    g.add_argument("--b", dest="participant_b", default=env.get(ENV_PREFIX + "B"),
                   help='participant B as "Name:lang:side" (default Guest B:ja:right)')
    g.add_argument("--attribution", default=e("attribution", d.attribution),
                   choices=["auto", "ptt", "lang", "doa", "turn"],
                   help="how to tell who spoke: auto = push-to-talk > language > direction > turn-taking")
    g.add_argument("--doa", action=argparse.BooleanOptionalAction, default=e("doa", d.doa),
                   help="poll the robot's sound direction of arrival")
    g.add_argument("--doa-split", dest="doa_split", type=float, default=e("doa_split", d.doa_split),
                   help="DoA angle in radians separating left (smaller) from right (larger)")
    g.add_argument("--doa-min-confidence", dest="doa_min_confidence", type=float,
                   default=e("doa_min_confidence", d.doa_min_confidence))

    g = p.add_argument_group("robot")
    g.add_argument("--gestures", action=argparse.BooleanOptionalAction, default=e("gestures", d.gestures),
                   help="turn the head toward the speaker, then toward the listener")
    g.add_argument("--look-yaw-deg", dest="look_yaw_deg", type=float, default=e("look_yaw_deg", d.look_yaw_deg))

    p.add_argument("--log-level", dest="log_level", default=e("log_level", d.log_level))
    return p
