// Endpoint de traducción dedicado /translate/ (multipart), con y sin streaming.
//   node --env-file=.env probes/translate-endpoint.mjs
const A = { Authorization: "Bearer " + process.env.SHISA_API_KEY };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function run(name, fields) {
  await sleep(1500);
  const body = new FormData(); for (const [k, v] of Object.entries(fields)) body.set(k, v);
  const t0 = performance.now(); let first; const parts = [];
  const r = await fetch("https://api.shisa.ai/translate/", { method: "POST", headers: A, body });
  const dec = new TextDecoder();
  for await (const c of r.body) { first ??= performance.now() - t0; parts.push(dec.decode(c, { stream: true })); }
  const txt = parts.join("");
  console.log(`${name}: ${r.status} ${r.headers.get("content-type")} primer dato ${Math.round(first)}ms total ${Math.round(performance.now() - t0)}ms chunks=${parts.length}\n   ${txt.replace(/\s+/g, " ").slice(0, 400)}`);
}
await run("en→ja stream=false", { text: "Hi, I'm Daniel. Thanks for coming to our hackathon demo.", source_lang: "en", target_lang: "ja", stream: "false" });
await run("ja→en stream=true", { text: "ロボットが通訳するんですか？すごいですね。", source_lang: "ja", target_lang: "en", stream: "true" });
await run("es→ja stream=false", { text: "Hola, ¿cómo estás?", source_lang: "es", target_lang: "ja", stream: "false" });
