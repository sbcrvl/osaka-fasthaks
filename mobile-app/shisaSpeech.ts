import { isRecord, languageName } from './protocol.ts';

const voiceIds = new Map<string, string>();

/** Choose a matching Shisa voice and return playable MP3 bytes. */
export async function shisaSpeech(text: string, language: string, signal: AbortSignal) {
  const apiKey = process.env.EXPO_PUBLIC_SHISA_API_KEY;
  if (!apiKey) throw new Error('Set EXPO_PUBLIC_SHISA_API_KEY in the app’s .env and restart Expo.');
  if ([...text].length > 5000) throw new Error('Shisa can read messages up to 5,000 characters long.');
  const headers = { Authorization: `Bearer ${apiKey}` };
  const name = languageName(language);
  let voiceId = voiceIds.get(name);
  if (!voiceId) {
    const response = await fetch('https://api.shisa.ai/tts/voices', { headers, signal });
    if (!response.ok) throw new Error(`Shisa voice lookup failed (${response.status}). Tap the speaker to retry.`);
    const result: unknown = await response.json();
    const voice = isRecord(result) && Array.isArray(result.voices) ? result.voices.find(voice => isRecord(voice)
      && typeof voice.id === 'string' && typeof voice.language === 'string'
      && voice.language.split(' & ').includes(name) && Array.isArray(voice.formats) && voice.formats.includes('mp3')) : undefined;
    if (!isRecord(voice) || typeof voice.id !== 'string') throw new Error(`Shisa has no MP3 voice for ${name}.`);
    voiceId = voice.id;
    voiceIds.set(name, voiceId);
  }
  const response = await fetch('https://api.shisa.ai/tts', {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ voice_id: voiceId, text, format: 'mp3', stream: false }),
    signal,
  });
  if (!response.ok) throw new Error(`Shisa speech failed (${response.status}). Tap the speaker to retry.`);
  if (!response.headers.get('content-type')?.startsWith('audio/')) throw new Error('Shisa returned invalid audio. Tap the speaker to retry.');
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (!bytes.length) throw new Error('Shisa returned empty audio. Tap the speaker to retry.');
  return bytes;
}
