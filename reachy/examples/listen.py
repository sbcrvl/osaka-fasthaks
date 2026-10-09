"""Minimal reference client: prints the conversation as it streams.

    python examples/listen.py ws://reachy-mini.local:8042/ws?p=a

Handy for checking the server before the real client app is connected, and as
a reading aid for PROTOCOL.md. Needs ``pip install websockets``.
"""

import json
import sys

from websockets.sync.client import connect

url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8042/ws"
people: dict[str, dict] = {}
seen_partial = False


def name(pid):
    return people.get(pid, {}).get("name", "?")


with connect(url) as ws:
    for raw in ws:
        m = json.loads(raw)
        kind = m["type"]
        if kind in ("hello", "participants"):
            people = {p["id"]: p for p in m["participants"]}
            if kind == "hello":
                seats = ", ".join("{name} ({lang}, {side})".format(**p) for p in people.values())
                print(f"connected: {seats}")
                for u in m["history"]:
                    print(f"  {name(u['speaker'])}: {u['text']}  {u.get('translations', {})}")
        elif kind == "partial":
            u = m["utterance"]
            print(f"\r  … {name(u['speaker'])}: {u['text']}", end="", flush=True)
            seen_partial = True
        elif kind == "final":
            u = m["utterance"]
            print(("\r" if seen_partial else "") + f"  {name(u['speaker'])} [{u['lang']}, {u['attribution']}]: {u['text']}")
            seen_partial = False
        elif kind == "translation":
            print(f"      → {m['lang']}: {m['text'] if m['text'] is not None else 'failed: ' + m.get('error', '')}")
        elif kind == "status" and m["status"].get("error"):
            print(f"  ! {m['status']['error']}")
