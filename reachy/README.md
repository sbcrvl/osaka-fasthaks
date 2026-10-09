# Reachy Mini Intermediator

Reachy Mini sits between two people who don't share a language. It listens with
its microphone array, transcribes, translates, reads the translation aloud, and
streams the whole conversation to client apps over a WebSocket so each person can
follow and scroll back on their own device.

## With the Hashi mobile app (`../mobile-app`)

Hashi's server owns the conversation and translates on demand; the robot is its
**publisher**. Every finished utterance with a known speaker is sent as one
`message` event (participant `a` → `person-1`, `b` → `person-2`):

```bash
cd mobile-app && DEMO=false npm run server          # Hashi on :8080
cd reachy && reachy-mini-intermediator --publish ws://localhost:8080 \
    --a "Ben:en:left" --b "Aiko:ja:right"
```

Messages are immutable on the Hashi side, so a speaker fixed later on the robot
is not re-published. Utterances with no identifiable speaker are not published
(Hashi needs a `userId`). Messages queued while Hashi is down are sent on reconnect.

The robot also serves its own richer stream (partials, status, sound direction)
documented in [PROTOCOL.md](PROTOCOL.md), for debugging or a stage screen.

## Two ways to run the same thing

**As its own process** (any machine on the robot's network, or on the robot):

```bash
pip install -e ".[all]"
export SHISA_API_KEY=shsk:...
reachy-mini-intermediator --a "Aiko:ja:left" --b "Ben:en:right" --robot-host reachy-mini.local
```

The robot is reached through the SDK (microphone, speaker, head) and the daemon's
REST API (sound direction); the daemon and its app manager keep running untouched.

**As a Reachy Mini app** from the dashboard (entry point `reachy_mini_intermediator`).
The dashboard passes no arguments, so configure with environment variables:
`SHISA_API_KEY`, `INTERMEDIATOR_A="Aiko:ja:left"`, `INTERMEDIATOR_B=...`, and any
other flag as `INTERMEDIATOR_<FLAG>`.

Clients connect to `ws://<host>:8042/ws?p=a` (or `?p=b`, or no `p` for a shared screen).

## Shisa

The Shisa side follows what [`../shisa/`](../shisa/README.md) measured against the live API:
realtime ASR with per-utterance language detection, translation done separately
(the socket's own translation fails in that mode), PCM 16 kHz TTS with the tested
voices (Ono Anna for Japanese, Ryan for English), and 429 back-off. The key is
read from the environment or from the first of `reachy/.env`, `shisa/.env`,
`mobile-app/server/.env` that has it (all git-ignored).

## The chain

| step | default with `SHISA_API_KEY` | offline / other options |
|---|---|---|
| audio | robot mic array (`--source reachy`) | `--source mic`, `--source file:talk.wav` |
| speech recognition | Shisa realtime ASR, streamed, with live partials | `faster-whisper:small`, `shisa` (batch), `openai:<model>`, `script:<file>` |
| who spoke | push-to-talk > language heard > sound direction > turn-taking | `--attribution ptt\|lang\|doa\|turn` |
| translation | Shisa translate with the last turns as context | `claude:claude-haiku-5-5`, `openai:<model>` (incl. Ollama), `none` |
| voice | Shisa TTS on the robot speaker; mic ignored while it talks | `--speak local\|off`, `--tts-voices ja=<id>,en=<id>` |
| body | looks at the speaker, then turns to the listener with a nod | `--no-gestures` |

Shisa covers ja / en / zh; with other languages `auto` falls back to Whisper + Claude.
`reachy-mini-intermediator --help` lists every flag.

## Rehearsing without the robot or a key

```bash
reachy-mini-intermediator --source file:two_people.wav --asr script:examples/rehearsal.txt --translator echo
python examples/listen.py ws://localhost:8042/ws
```

## Tests

```bash
pip install -e ".[dev]" && pytest
```
