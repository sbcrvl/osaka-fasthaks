// Traductor de conversación: cada turno se traduce con streaming y se guarda
// en el historial, para que los turnos siguientes mantengan nombres, registro
// (keigo / tú-usted) y referencias.

import { streamChat } from "./client.mjs";

export const LANGS = { es: "Spanish", ja: "Japanese", en: "English" };

// Cuántos turnos anteriores se mandan como contexto.
const CONTEXT_TURNS = 12;

export function buildMessages({ history, text, from, to }) {
  const context = history
    .slice(-CONTEXT_TURNS)
    .map((t) => `[${t.speaker}] ${t.text}\n  → ${t.translation}`)
    .join("\n");
  const system = [
    `You are a live interpreter for a conversation. Translate the new message from ${LANGS[from]} to natural ${LANGS[to]}.`,
    "Keep the speaker's tone and politeness level. Keep names as they are.",
    "Output ONLY the translation: no quotes, no notes, no romanization.",
    context && `Conversation so far (original → translation):\n${context}`,
  ]
    .filter(Boolean)
    .join("\n\n");
  return [
    { role: "system", content: system },
    { role: "user", content: text },
  ];
}

export function createConversation({ model } = {}) {
  const history = [];
  return {
    history,
    // Devuelve un async iterable con los fragmentos; al terminar guarda el turno.
    async *translate({ speaker, text, from, to, signal }) {
      if (!LANGS[from] || !LANGS[to]) throw new Error(`Idioma no soportado: ${from} → ${to}`);
      let translation = "";
      for await (const delta of streamChat({ model, signal, messages: buildMessages({ history, text, from, to }) })) {
        translation += delta;
        yield delta;
      }
      history.push({ speaker, from, to, text, translation: translation.trim() });
    },
  };
}
