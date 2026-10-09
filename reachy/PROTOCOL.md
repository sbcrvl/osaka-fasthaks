# WebSocket protocol (v1)

The intermediator streams the conversation to any number of client apps over one
WebSocket. Every frame is a UTF-8 JSON text frame; every server message carries
`"type"` and `"v": 1`.

```
ws://<host>:8042/ws?p=a     participant A's device
ws://<host>:8042/ws?p=b     participant B's device
ws://<host>:8042/ws         a shared screen, a logger, anything else
```

`p` only tells the server whose device this is (used for push-to-talk); every
client receives the same stream. `<host>` is the robot (`reachy-mini.local`) when
the app runs on the robot, or the machine running the standalone server.

The server keeps the whole conversation. On connect, and after every reconnect,
the first message is a `hello` with the full history, so clients reconnect
freely and rebuild their view from it. Treat everything else as upserts keyed by
utterance `id`; duplicates are harmless.

## Typical sequence

```
→ hello                     history so far
→ status                    ~8 per second while audio flows (level meter, direction)
→ partial  {id: u7, ...}    live text while someone talks (repeats, same id, growing text)
→ final    {id: u7, ...}    replaces the partial; speaker and language now settled
→ translation {id: u7, lang: "en", text: ...}    arrives ~0.5-2 s later
→ status   {speaking: {id: u7, lang: "en"}}       the robot reads it aloud (if enabled)
```

## The utterance object

Sent inside `partial`, `final` and `hello.history`.

| field | type | meaning |
|---|---|---|
| `id` | string | Stable id (`"u7"`). A partial and its final share it. |
| `final` | bool | `false` while the person is still talking. |
| `speaker` | string \| null | Participant id (`"a"`, `"b"`), or `null` if unknown. May change between partial and final, and later through `reassign`. |
| `attribution` | string | How the speaker was decided: `ptt` (push-to-talk), `lang` (language heard), `doa` (direction of the voice), `turn` (turn-taking guess), `typed`, `manual` (reassigned by a client), `live` (provisional, partials only), `none`. |
| `lang` | string \| null | ISO 639-1 code of what was said (`"ja"`). May be `null` in partials. |
| `text` | string | Transcript in the spoken language. |
| `translations` | object | `{lang: text}`; filled in by `translation` messages. Present on finals and in `hello`. |
| `start`, `end` | number | Wall-clock seconds (Unix epoch) of the speech. |
| `doa` | number \| null | Mean direction of the voice in radians (0 = robot's left, π/2 = front, π = right). |

What to show a participant with language `L` for utterance `u`:

* their own utterance: `u.text` (optionally what the other person sees: `u.translations[otherLang]`);
* the other person's: `u.translations[L]` when present, else `u.text` with a "translating" hint;
* if `u.lang == L`, nothing needs translating.

## Server → client messages

### `hello`

```json
{"type": "hello", "v": 1, "server_time": 1791540604.5,
 "info": {"source": "Reachy Mini microphone array", "asr": "Shisa realtime ASR (auto/utterance)",
          "translator": "Shisa translate (shisa-ai/chotto)", "voice": "Shisa TTS → robot speaker",
          "vad": null, "partials": true, "attribution": "auto", "doa": true, "gestures": true,
          "port": 8042, "hosts": ["192.168.1.42", "reachy-mini.local"]},
 "languages": {"en": {"name": "English", "native": "English"}, "ja": {"name": "Japanese", "native": "日本語"}},
 "participants": [{"id": "a", "name": "Aiko", "lang": "ja", "side": "left"},
                  {"id": "b", "name": "Ben", "lang": "en", "side": "right"}],
 "status": {"...": "see status"},
 "history": [{"id": "u1", "final": true, "speaker": "a", "attribution": "lang", "lang": "ja",
              "text": "はじめまして", "translations": {"en": "Nice to meet you"},
              "start": 1791540590.1, "end": 1791540591.4, "doa": 0.42}]}
```

`side` is where the person sits as seen from the robot. `languages` is the list
the server knows display names for; any ISO code can be used.

### `partial` / `final`

```json
{"type": "partial", "v": 1, "utterance": {"id": "u7", "final": false, "speaker": "b", "attribution": "live",
  "lang": null, "text": "Do you know a good", "start": 1791540612.0, "end": 1791540613.2, "doa": null}}
```

A `final` has the same shape with `"final": true` and `"translations": {}`. A
`final` is also re-sent when a client reassigns the speaker (with
`"attribution": "manual"` and translations reset).

### `translation`

```json
{"type": "translation", "v": 1, "id": "u7", "lang": "ja", "text": "この辺でいいラーメン屋を知っていますか？"}
```

On failure `text` is `null` and `"error"` holds a short reason.

### `drop`

`{"type": "drop", "v": 1, "id": "u7"}`: the utterance turned out to be noise;
remove it (it may have shown as a partial).

### `participants`

`{"type": "participants", "v": 1, "participants": [...]}` after any change (name,
language, seat).

### `status`

```json
{"type": "status", "v": 1, "status": {
  "listening": true, "paused": false, "speech": true, "level": 0.42,
  "doa": {"angle": 2.61, "speech": true, "side": "right"},
  "speaker": "b", "ptt": null, "speaking": null, "speak": true, "error": null}}
```

| field | meaning |
|---|---|
| `listening` | Audio is being transcribed (false while paused, while the robot speaks, or if the input failed). |
| `paused` | A client paused listening. |
| `speech` | Someone is talking right now (local voice detection). |
| `level` | Input level 0..1, for a meter. |
| `doa` | Latest direction of sound from the robot's microphone array, or `null` without a robot. |
| `speaker` | Best live guess of who is talking now, or `null`. |
| `ptt` | Participant currently holding push-to-talk, or `null`. |
| `speaking` | `{"id", "lang"}` while the robot reads a translation aloud, else `null`. |
| `speak` | Whether reading aloud is switched on. |
| `error` | Human-readable problem to show (missing key, robot unreachable, ...), or `null`. |

### `cleared`

`{"type": "cleared", "v": 1}`: the conversation was cleared; empty the view.

### `pong`

Reply to `ping`, echoing `t`.

## Client → server messages

| message | effect |
|---|---|
| `{"type": "hello", "participant": "a"}` | Re-identify (e.g. after the user picks a seat); the server answers with a fresh `hello`. |
| `{"type": "ptt", "participant": "a", "active": true}` | Push-to-talk pressed (`false` when released). Whatever is said while held is attributed to that participant. Released automatically if the socket closes. `participant` defaults to the `p` of the connection. |
| `{"type": "text", "participant": "a", "text": "..."}` | A typed message, handled like speech (translated, read aloud). For loud rooms. |
| `{"type": "update_participant", "id": "a", "name": "Aiko", "lang": "ja", "side": "left"}` | Change any of name / language / seat. Choosing a taken seat swaps the two people. |
| `{"type": "reassign", "id": "u7", "speaker": "b"}` | Fix a wrong speaker guess; triggers a new `final` and translations as needed. |
| `{"type": "retranslate", "id": "u7"}` | Ask for the translations again. |
| `{"type": "control", "action": "pause" \| "resume" \| "clear" \| "speak_on" \| "speak_off"}` | Listening on/off, wipe the conversation, robot voice on/off. |
| `{"type": "ping", "t": 123}` | Latency check. |

Unknown messages are ignored.

## REST

| route | returns |
|---|---|
| `GET /api/state` (also `/`) | The `hello` payload. |
| `GET /api/transcript.json` | `{"participants": [...], "utterances": [finals...]}` |
| `GET /api/transcript.md` | The transcript as Markdown (download). |
| `GET /healthz` | `{"ok": true, "clients": 2}` |

CORS is open for GET. WebSocket keep-alive pings are sent every 20 s; a client
that cannot keep up (1000 queued messages) is disconnected and should reconnect.
