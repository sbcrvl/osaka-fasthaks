export type UserId = 'person-1' | 'person-2';

/** An immutable utterance; language describes the original text. */
export type Message = {
  id: string;
  userId: UserId;
  text: string;
  language: string;
  sentAt: string;
};

export type ServerEvent =
  | { type: 'snapshot'; messages: Message[] }
  | { type: 'message'; message: Message }
  | { type: 'translation'; requestId: string; messageId: string; targetLanguage: string; text: string }
  | { type: 'error'; requestId?: string; error: string };

/** Reject malformed network data before it reaches the chat or translation API. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function isLanguage(value: unknown): value is string {
  return typeof value === 'string' && /^[a-z]{2,3}(-[a-z0-9]{2,8})*$/.test(value);
}

export function isMessage(value: unknown): value is Message {
  return isRecord(value) && typeof value.id === 'string' && value.id.length > 0 && value.id.length <= 128
    && (value.userId === 'person-1' || value.userId === 'person-2')
    && typeof value.text === 'string' && value.text.trim().length > 0 && value.text.length <= 10_000
    && isLanguage(value.language) && typeof value.sentAt === 'string' && Number.isFinite(Date.parse(value.sentAt));
}

export function isServerEvent(value: unknown): value is ServerEvent {
  if (!isRecord(value)) return false;
  switch (value.type) {
    case 'snapshot': return Array.isArray(value.messages) && value.messages.every(isMessage)
      && new Set(value.messages.map(message => message.id)).size === value.messages.length;
    case 'message': return isMessage(value.message);
    case 'translation': return typeof value.requestId === 'string' && typeof value.messageId === 'string'
      && isLanguage(value.targetLanguage) && typeof value.text === 'string' && value.text.trim().length > 0;
    case 'error': return typeof value.error === 'string' && (value.requestId === undefined || typeof value.requestId === 'string');
    default: return false;
  }
}

export const otherUser = (userId: UserId): UserId => userId === 'person-1' ? 'person-2' : 'person-1';
const languageNames: Record<string, string> = { en: 'English', ja: 'Japanese', es: 'Spanish', fr: 'French', de: 'German', ko: 'Korean', zh: 'Chinese', sv: 'Swedish' };
export const languageName = (language: string) => languageNames[language.split('-')[0]] ?? language;
