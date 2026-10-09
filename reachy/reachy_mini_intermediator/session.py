"""State shared between the web clients and the audio pipeline.

Written from the server's event loop (client messages) and read from the
pipeline's worker threads, hence the lock.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import replace

from .config import Participant


class Session:
    def __init__(self, participants: list[Participant]) -> None:
        self._lock = threading.Lock()
        self._participants = {p.id: replace(p) for p in participants}
        self._order = [p.id for p in participants]
        # Push-to-talk presses: (participant id, start, end or None while held).
        self._ptt: collections.deque[list] = collections.deque(maxlen=64)
        self._paused = False
        self.mic_muted_until = 0.0  # set while the robot speaks
        self.speak_enabled = True  # robot reads translations aloud (when a voice is configured)

    # ------------------------------------------------------------ participants
    def participants(self) -> list[Participant]:
        with self._lock:
            return [replace(self._participants[pid]) for pid in self._order]

    def participant(self, pid: str | None) -> Participant | None:
        if pid is None:
            return None
        with self._lock:
            p = self._participants.get(pid)
            return replace(p) if p else None

    def update_participant(self, pid: str, name: str | None = None, lang: str | None = None,
                           side: str | None = None) -> Participant | None:
        with self._lock:
            p = self._participants.get(pid)
            if p is None:
                return None
            if name is not None and name.strip():
                p.name = name.strip()[:40]
            if lang is not None and lang.strip():
                p.lang = lang.strip().lower()[:8]
            if side in ("left", "right"):
                p.side = side
                # Two people cannot sit on the same side: swap the other one.
                for other in self._participants.values():
                    if other.id != pid and other.side == side:
                        other.side = "right" if side == "left" else "left"
            return replace(p)

    def other(self, pid: str | None) -> Participant | None:
        with self._lock:
            for oid in self._order:
                if oid != pid:
                    return replace(self._participants[oid])
        return None

    def by_side(self, side: str) -> Participant | None:
        with self._lock:
            return next((replace(p) for p in self._participants.values() if p.side == side), None)

    def by_lang(self, lang: str | None) -> Participant | None:
        """The participant speaking ``lang``, if exactly one does."""
        with self._lock:
            matches = [p for p in self._participants.values() if p.lang == lang]
            return replace(matches[0]) if len(matches) == 1 else None

    def languages(self) -> list[str]:
        with self._lock:
            out: list[str] = []
            for pid in self._order:
                lang = self._participants[pid].lang
                if lang not in out:
                    out.append(lang)
            return out

    # ------------------------------------------------------------ push-to-talk
    def set_ptt(self, pid: str, active: bool, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            if pid not in self._participants:
                return
            open_press = next((p for p in reversed(self._ptt) if p[0] == pid and p[2] is None), None)
            if active and open_press is None:
                self._ptt.append([pid, now, None])
            elif not active and open_press is not None:
                open_press[2] = now

    def ptt_active(self) -> str | None:
        with self._lock:
            held = [p for p in self._ptt if p[2] is None]
            return held[-1][0] if held else None

    def ptt_speaker(self, t0: float, t1: float, slack: float = 0.4) -> str | None:
        """The participant whose push-to-talk press overlaps ``[t0, t1]`` the most."""
        overlap: dict[str, float] = {}
        with self._lock:
            for pid, start, end in self._ptt:
                end = time.time() if end is None else end
                o = min(t1, end + slack) - max(t0, start - slack)
                if o > 0:
                    overlap[pid] = overlap.get(pid, 0.0) + o
        if not overlap:
            return None
        return max(overlap, key=lambda k: overlap[k])

    # ------------------------------------------------------------------- pause
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
