import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSSE, streamChat, ShisaError } from "./client.mjs";

const enc = new TextEncoder();
const event = (content) => `data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\n`;

async function collect(iter) {
  const out = [];
  for await (const x of iter) out.push(x);
  return out;
}

// Parte el texto en chunks de `size` bytes, cortando eventos y caracteres multibyte por la mitad.
function chunked(text, size) {
  const bytes = enc.encode(text);
  return (async function* () {
    for (let i = 0; i < bytes.length; i += size) yield bytes.slice(i, i + size);
  })();
}

test("parseSSE junta eventos y caracteres japoneses cortados entre chunks", async () => {
  const raw = event("こんにちは") + event("、元気") + ": keep-alive\n\n" + event("ですか？") + "data: [DONE]\n\n";
  for (const size of [1, 3, 7, 1000]) {
    assert.equal((await collect(parseSSE(chunked(raw, size)))).join(""), "こんにちは、元気ですか？", `size=${size}`);
  }
});

test("parseSSE lanza si el stream trae un error", async () => {
  const raw = `data: ${JSON.stringify({ error: { message: "rate limit" } })}\n\n`;
  await assert.rejects(collect(parseSSE(chunked(raw, 1000))), ShisaError);
});

test("streamChat lanza con el status si la API responde error (sin fallback silencioso)", async () => {
  const fetchImpl = async () => new Response("invalid api key", { status: 401 });
  await assert.rejects(
    collect(streamChat({ messages: [], apiKey: "x", fetchImpl })),
    (e) => e instanceof ShisaError && e.status === 401,
  );
});

test("streamChat reintenta un 429 respetando 'Retry after' y avisa", async () => {
  let calls = 0;
  const retries = [];
  const fetchImpl = async () =>
    ++calls === 1
      ? new Response('{"error":"Too many requests. Retry after 0.05s"}', { status: 429 })
      : new Response(ReadableStream.from(chunked(event("ok") + "data: [DONE]\n\n", 5)));
  const out = await collect(streamChat({ messages: [], apiKey: "k", fetchImpl, onRetry: (r) => retries.push(r) }));
  assert.deepEqual(out, ["ok"]);
  assert.equal(calls, 2);
  assert.deepEqual(retries, [{ attempt: 1, waitMs: 50 }]);
});

test("streamChat se rinde tras 3 intentos con 429", async () => {
  let calls = 0;
  const fetchImpl = async () => (calls++, new Response("Retry after 0.01s", { status: 429 }));
  await assert.rejects(collect(streamChat({ messages: [], apiKey: "k", fetchImpl, onRetry: () => {} })), (e) => e.status === 429);
  assert.equal(calls, 3);
});

test("streamChat manda stream: true, modelo y Authorization", async () => {
  let sent;
  const fetchImpl = async (url, init) => {
    sent = { url, init };
    return new Response(ReadableStream.from(chunked(event("hola") + "data: [DONE]\n\n", 5)));
  };
  const out = await collect(streamChat({ messages: [{ role: "user", content: "x" }], model: "m", apiKey: "k", fetchImpl }));
  assert.deepEqual(out, ["hola"]);
  assert.equal(sent.url, "https://api.shisa.ai/openai/v1/chat/completions");
  assert.equal(sent.init.headers.Authorization, "Bearer k");
  assert.deepEqual(JSON.parse(sent.init.body), { model: "m", messages: [{ role: "user", content: "x" }], temperature: 0.2, stream: true });
});
