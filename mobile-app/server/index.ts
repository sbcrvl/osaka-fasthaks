import { networkInterfaces } from 'node:os';
import { WebSocket, WebSocketServer } from 'ws';
import { isLanguage, isMessage, isRecord, otherUser, type Message, type ServerEvent } from '../protocol.ts';

const apiKey = process.env.SHISA_API_KEY;
if (!apiKey) throw new Error('Set SHISA_API_KEY in server/.env before starting the server.');
const port = Number(process.env.PORT ?? 8080);
const server = new WebSocketServer({ port, host: '0.0.0.0', maxPayload: 64 * 1024 });
const history: Message[] = [];
const cache = new Map<string, string>();
const demo: [Message['userId'], string, string][] = [
  ['person-1', 'en', 'Hey! Have you picked a place for lunch?'],
  ['person-2', 'ja', '駅の近くに新しいラーメン屋さんができたよ。'],
  ['person-1', 'en', 'That sounds great. Shall we meet at twelve?'],
  ['person-2', 'ja', 'いいね！１２時に駅の前で会おう。'],
  ['person-1', 'en', 'Perfect! I’m looking forward to it.'],
  ['person-2', 'ja', '私も！あとでメニューを送るね。'],
];

function send(socket: WebSocket, event: ServerEvent) {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(event));
}

function publish(message: Message) {
  history.push(message);
  for (const socket of server.clients) send(socket, { type: 'message', message });
}

function demoMessage(index: number): Message {
  const [userId, language, text] = demo[index];
  return { id: `demo-${index + 1}`, userId, language, text, sentAt: new Date().toISOString() };
}

if (process.env.DEMO !== 'false') for (let index = 0; index < 4; index++) history.push(demoMessage(index));
let demoStarted = false;

server.on('connection', socket => {
  socket.on('error', () => console.error('A WebSocket connection failed.'));
  send(socket, { type: 'snapshot', messages: history });
  if (!demoStarted && process.env.DEMO !== 'false') {
    demoStarted = true;
    setTimeout(() => publish(demoMessage(4)), 8000);
    setTimeout(() => publish(demoMessage(5)), 16000);
  }
  let translating = false;
  socket.on('message', async raw => {
    let requestId: string | undefined;
    try {
      const event: unknown = JSON.parse(raw.toString());
      if (!isRecord(event)) throw new Error('Expected a JSON object.');
      if (typeof event.requestId === 'string' && event.requestId.length <= 128) requestId = event.requestId;
      if (event.type === 'message') {
        if (!isMessage(event.message)) throw new Error('Invalid message. See WEBSOCKET.md.');
        const message = event.message;
        if (history.some(item => item.id === message.id)) throw new Error('Message IDs must be unique.');
        publish(message);
        return;
      }
      if (event.type !== 'translate' || !requestId || typeof event.messageId !== 'string' || !isLanguage(event.targetLanguage)) throw new Error('Invalid translation request.');
      const message = history.find(message => message.id === event.messageId);
      if (!message) throw new Error('The message no longer exists.');
      const recipient = history.findLast(item => item.userId === otherUser(message.userId));
      if (!recipient) throw new Error('Wait for the other person to speak first.');
      if (recipient.language !== event.targetLanguage) throw new Error('The other person changed languages. Tap Translate again.');
      const targetLanguage = event.targetLanguage;
      const key = `${message.id}:${targetLanguage}`;
      let text = cache.get(key);
      if (message.language === targetLanguage) text = message.text;
      if (!text) {
        if (translating) throw new Error('A translation is already in progress. Try again shortly.');
        translating = true;
        try {
          const body = new FormData();
          body.set('text', message.text);
          body.set('source_lang', message.language);
          body.set('target_lang', targetLanguage);
          body.set('stream', 'false');
          const response = await fetch('https://api.shisa.ai/translate/', {
            method: 'POST',
            headers: { Authorization: `Bearer ${apiKey}` },
            body,
            signal: AbortSignal.timeout(35_000),
          });
          if (!response.ok) throw new Error(`Shisa request failed (${response.status}). Check the server API key, supported languages, or quota.`);
          const result: unknown = await response.json();
          const choice = isRecord(result) && Array.isArray(result.choices) ? result.choices[0] : undefined;
          const translated = isRecord(choice) && isRecord(choice.message) ? choice.message.content : undefined;
          if (!isRecord(choice) || choice.finish_reason !== 'stop' || typeof translated !== 'string' || !translated.trim()) throw new Error('Shisa translation did not complete. Try again.');
          text = translated.trim();
          cache.set(key, text);
        } finally {
          translating = false;
        }
      }
      send(socket, { type: 'translation', requestId, messageId: message.id, targetLanguage, text });
    } catch (error) {
      const description = error instanceof Error && ['TimeoutError', 'AbortError'].includes(error.name) ? 'Shisa translation timed out. Try again.'
        : error instanceof TypeError ? 'Cannot reach Shisa. Try again.'
          : error instanceof SyntaxError ? 'Invalid JSON response or request. See WEBSOCKET.md.'
            : error instanceof Error ? error.message : 'Unable to process the request.';
      send(socket, { type: 'error', ...(requestId ? { requestId } : {}), error: description });
    }
  });
});

server.on('listening', () => {
  console.log(`Hashi server: ws://localhost:${port}`);
  for (const interfaces of Object.values(networkInterfaces())) {
    for (const address of interfaces ?? []) if (address.family === 'IPv4' && !address.internal) console.log(`Phone address: ws://${address.address}:${port}`);
  }
});
server.on('error', error => {
  console.error(`Server failed: ${error.message}`);
  process.exit(1);
});
