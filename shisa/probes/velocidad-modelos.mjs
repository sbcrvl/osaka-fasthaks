// Mide tiempo al primer token y total de una traducción EN→JA con streaming.
//   node --env-file=.env probes/velocidad-modelos.mjs shisa-ai/chotto
const model = process.argv[2];
const t0 = performance.now(); let first = null, out = "", chunks = 0;
const r = await fetch("https://api.shisa.ai/openai/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.SHISA_API_KEY },
  body: JSON.stringify({ model, stream: true, temperature: 0.2, messages: [
    { role: "system", content: "You are a translator. Translate the user's message from English to natural Japanese. Output only the translation." },
    { role: "user", content: "Hi, how are you? We have the meeting tomorrow at three, does that work for you?" } ] }),
});
console.log("status", r.status, r.headers.get("content-type"));
if (!r.ok) { console.log(await r.text()); process.exit(1); }
const dec = new TextDecoder(); let buf = "";
for await (const c of r.body) {
  buf += dec.decode(c, { stream: true });
  let i; while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
    if (!line.startsWith("data:")) continue;
    const d = line.slice(5).trim(); if (d === "[DONE]") continue;
    const delta = JSON.parse(d).choices?.[0]?.delta?.content ?? "";
    if (delta) { first ??= performance.now() - t0; chunks++; out += delta; }
  }
}
console.log(JSON.stringify({ model, ttft_ms: Math.round(first), total_ms: Math.round(performance.now() - t0), chunks, out }));
