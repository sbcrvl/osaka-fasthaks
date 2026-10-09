import { useEffect, useRef, useState } from 'react';
import { isServerEvent, type Message } from './protocol';

type Translation = { pending: boolean; text?: string; error?: string };
type PendingRequest = { key: string; messageId: string; targetLanguage: string; timer: ReturnType<typeof setTimeout> };

/** Keep socket recovery and translation requests out of the presentation layer. */
export function useConversation(url: string) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [status, setStatus] = useState<'connecting' | 'connected' | 'reconnecting'>('connecting');
  const [error, setError] = useState('');
  const [translations, setTranslations] = useState<Record<string, Translation>>({});
  const socket = useRef<WebSocket | null>(null);
  const requests = useRef(new Map<string, PendingRequest>());
  const requestCounter = useRef(0);

  useEffect(() => {
    let stopped = false;
    let retry: ReturnType<typeof setTimeout>;
    let connectTimer: ReturnType<typeof setTimeout>;
    const pending = requests.current;

    function failRequests(reason: string) {
      for (const { key, timer } of pending.values()) {
        clearTimeout(timer);
        setTranslations(previous => ({ ...previous, [key]: { pending: false, error: reason } }));
      }
      pending.clear();
    }

    function connect() {
      if (stopped) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        setError('Invalid WebSocket address. Use ws:// or wss://.');
        setStatus('reconnecting');
        return;
      }
      socket.current = ws;
      connectTimer = setTimeout(() => ws.close(), 10_000);
      ws.onopen = () => {
        clearTimeout(connectTimer);
        if (stopped) return;
        setStatus('connected');
        setError('');
      };
      ws.onmessage = ({ data }) => {
        if (stopped) return;
        try {
          const event: unknown = JSON.parse(data);
          if (!isServerEvent(event)) throw new Error('Invalid server event');
          if (event.type === 'snapshot') setMessages(event.messages);
          if (event.type === 'message') setMessages(previous => previous.some(message => message.id === event.message.id) ? previous : [...previous, event.message]);
          if (event.type === 'translation' || (event.type === 'error' && event.requestId)) {
            const request = pending.get(event.requestId!);
            if (!request) return;
            if (event.type === 'translation' && (event.messageId !== request.messageId || event.targetLanguage !== request.targetLanguage)) throw new Error('Mismatched translation response');
            clearTimeout(request.timer);
            pending.delete(event.requestId!);
            setTranslations(previous => ({ ...previous, [request.key]: event.type === 'translation' ? { pending: false, text: event.text } : { pending: false, error: event.error } }));
          } else if (event.type === 'error') setError(event.error);
        } catch {
          setError('The server sent an invalid event. Check WEBSOCKET.md.');
        }
      };
      ws.onerror = () => {
        if (!stopped) setError('Cannot reach the server. Check the address and your Wi-Fi connection.');
        ws.close();
      };
      ws.onclose = () => {
        clearTimeout(connectTimer);
        if (stopped) return;
        setStatus('reconnecting');
        failRequests('Connection lost. Tap Translate to retry.');
        retry = setTimeout(connect, 3000);
      };
    }

    connect();
    return () => {
      stopped = true;
      clearTimeout(retry);
      clearTimeout(connectTimer);
      for (const request of pending.values()) clearTimeout(request.timer);
      pending.clear();
      socket.current?.close();
      socket.current = null;
    };
  }, [url]);

  function translate(message: Message, targetLanguage: string) {
    const key = `${message.id}:${targetLanguage}`;
    if (socket.current?.readyState !== WebSocket.OPEN || translations[key]?.pending || translations[key]?.text) return;
    const requestId = `${Date.now()}-${++requestCounter.current}`;
    setTranslations(previous => ({ ...previous, [key]: { pending: true } }));
    const timer = setTimeout(() => {
      requests.current.delete(requestId);
      setTranslations(previous => ({ ...previous, [key]: { pending: false, error: 'Translation timed out. Tap Translate to retry.' } }));
    }, 45_000);
    requests.current.set(requestId, { key, messageId: message.id, targetLanguage, timer });
    try {
      socket.current.send(JSON.stringify({ type: 'translate', requestId, messageId: message.id, targetLanguage }));
    } catch {
      clearTimeout(timer);
      requests.current.delete(requestId);
      setTranslations(previous => ({ ...previous, [key]: { pending: false, error: 'Could not send the request. Tap Translate to retry.' } }));
    }
  }

  return { messages, status, error, translations, translate };
}
