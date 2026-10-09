// Cliente mínimo de la API de Shisa (compatible con OpenAI) con streaming SSE.

export const SHISA_BASE_URL = "https://api.shisa.ai/openai/v1";

export class ShisaError extends Error {
  constructor(status, body) {
    super(`Shisa API ${status}: ${body.slice(0, 500)}`);
    this.status = status;
  }
}

// Convierte un stream de bytes SSE en los fragmentos de texto de `delta.content`.
// Las líneas pueden llegar cortadas entre chunks, por eso se acumula en `buf`.
export async function* parseSSE(byteStream) {
  const dec = new TextDecoder();
  let buf = "";
  for await (const chunk of byteStream) {
    buf += dec.decode(chunk, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (data === "[DONE]") return;
      const json = JSON.parse(data);
      if (json.error) throw new ShisaError("stream", JSON.stringify(json.error));
      const delta = json.choices?.[0]?.delta?.content;
      if (delta) yield delta;
    }
  }
}

const MAX_ATTEMPTS = 3;

// La API responde 429 con "Retry after 1.72s" en el cuerpo.
function retryAfterMs(body) {
  const m = body.match(/Retry after ([\d.]+)s/i);
  return m ? Math.ceil(Number(m[1]) * 1000) : 1000;
}

// Llama a /chat/completions con stream: true y devuelve los fragmentos de texto.
// Un 429 se reintenta (antes de empezar el stream) avisando por onRetry.
export async function* streamChat({
  messages,
  model = process.env.SHISA_MODEL || "shisa-ai/chotto",
  apiKey = process.env.SHISA_API_KEY,
  temperature = 0.2,
  signal,
  fetchImpl = fetch,
  onRetry = ({ attempt, waitMs }) => console.error(`\n[shisa] 429 rate limit, reintento ${attempt} en ${waitMs} ms`),
}) {
  if (!apiKey) throw new Error("Falta SHISA_API_KEY (copiar .env.example a .env y correr con --env-file=.env)");
  for (let attempt = 1; ; attempt++) {
    const res = await fetchImpl(`${SHISA_BASE_URL}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({ model, messages, temperature, stream: true }),
      signal,
    });
    if (res.ok) return yield* parseSSE(res.body);
    const body = await res.text();
    if (res.status !== 429 || attempt >= MAX_ATTEMPTS) throw new ShisaError(res.status, body);
    const waitMs = retryAfterMs(body);
    onRetry({ attempt, waitMs });
    await new Promise((r) => setTimeout(r, waitMs));
  }
}
