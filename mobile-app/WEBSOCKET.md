# Hashi WebSocket protocol

Connect to `ws://<server>:8080` locally or `wss://<server>` when hosted. Each frame is a UTF-8 JSON object. The included server limits incoming frames to 64 KiB. There is one shared conversation with exactly two possible user IDs: `person-1` and `person-2`.

## Messages

```json
{
  "id": "message-001",
  "userId": "person-1",
  "text": "Shall we meet at twelve?",
  "language": "en",
  "sentAt": "2026-10-09T09:00:00.000Z"
}
```

| Field | Meaning |
| --- | --- |
| `id` | Unique immutable ID, 1–128 characters. Never reuse an ID for edited text. |
| `userId` | `person-1` or `person-2`; these determine left/right alignment. |
| `text` | Nonblank original text, at most 10,000 JavaScript string units. |
| `language` | Lowercase language tag describing this text, e.g. `en`, `ja`, `en-us`, or `zh-hant`. Format: 2–3 letters followed by optional hyphen-separated 2–8 character alphanumeric subtags. |
| `sentAt` | ISO 8601 timestamp with a timezone, for display. |

The publisher is responsible for identifying each message’s language. The app does not infer language from characters, locale, or translations. Use one dominant language tag for mixed-language messages.

## Server → app

### Initial history and reconnects

Send an authoritative snapshot on every connection, including reconnects. Use an empty array when nobody has spoken yet.

```json
{
  "type": "snapshot",
  "messages": [
    { "id": "message-001", "userId": "person-1", "text": "Hello!", "language": "en", "sentAt": "2026-10-09T09:00:00.000Z" },
    { "id": "message-002", "userId": "person-2", "text": "こんにちは！", "language": "ja", "sentAt": "2026-10-09T09:00:02.000Z" }
  ]
}
```

Array order is conversation order. The app replaces its history with the snapshot. IDs must be unique within it and retain their original meaning across reconnections.

### Live message

```json
{
  "type": "message",
  "message": { "id": "message-003", "userId": "person-1", "text": "How are you?", "language": "en", "sentAt": "2026-10-09T09:00:04.000Z" }
}
```

Live messages append in arrival order. Replayed IDs are ignored by the app. Timestamps do not reorder messages: the last message in conversation order from a user determines their current language.

### Translation success

```json
{
  "type": "translation",
  "requestId": "request-001",
  "messageId": "message-002",
  "targetLanguage": "en",
  "text": "Hello!"
}
```

Reply only to the requesting connection. Echo `requestId`, `messageId`, and `targetLanguage` exactly. Text must be nonblank. A translation never becomes a conversation message or changes anyone’s language.

### Error

```json
{ "type": "error", "requestId": "request-001", "error": "Translation could not be completed. Try again." }
```

Include `requestId` for translation errors so the app can stop that message’s spinner and offer retry. Omit it for connection-wide errors. The app ignores replies to expired or unknown requests. Translation requests time out in 45 seconds; the included Shisa request times out in 35 seconds and does not automatically retry billable requests.

## App → server: translate

```json
{ "type": "translate", "requestId": "request-001", "messageId": "message-002", "targetLanguage": "en" }
```

The app computes `targetLanguage` at button press from the latest **other user’s** original message. The server retrieves source text and source language from the requested message, validates that the target still matches the other user’s latest language, and calls `POST https://api.shisa.ai/translate/` with multipart fields `text`, `source_lang`, `target_lang`, and `stream=false`, using Shisa’s default model. A stale target returns an error so the user can retry against the new target.

- Person 1’s latest message is `en`, Person 2’s latest is `ja`: Japanese messages from Person 2 translate to English; English messages from Person 1 translate to Japanese.
- Person 1 subsequently sends an `es` message: all Person 2 translation buttons now target Spanish, including older messages.
- An old Person 1 message still uses its own original language as the source, even if Person 1 has since changed languages.
- If the other user has not spoken yet, the button is disabled until their language is known.
- If source and target match, the app shows “Already in …” and skips the API call.
- If a target changes while a translation is in flight, its result is cached for the requested target; it is shown only when that target is current again.

The included server allows one active Shisa call per connection. Concurrent uncached requests return a retryable error. Successful translations are cached per immutable message ID and target language. The app also keeps successful results in memory; failures are never cached as successes.

## Publisher → included server: new message

A separate data producer can connect to the same endpoint and send the same `message` event shown above. The server validates it, rejects duplicate IDs, appends it to history, and broadcasts it to all connected clients. The app is receive-only and does not publish messages itself.

For example, from the project directory this switches Person 1’s latest language to Spanish:

```sh
node --input-type=module <<'JS'
import WebSocket from 'ws';
const socket = new WebSocket('ws://localhost:8080');
socket.on('open', () => {
  socket.send(JSON.stringify({
    type: 'message',
    message: { id: `spanish-${Date.now()}`, userId: 'person-1', text: '¡Hasta pronto!', language: 'es', sentAt: new Date().toISOString() }
  }), () => socket.close());
});
JS
```

With `DEMO=false`, publish your own messages for both users. Malformed events show an error without clearing the existing conversation. The app automatically reconnects after a disconnection; the server must send a fresh snapshot to recover any missed messages.
