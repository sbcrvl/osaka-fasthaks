// Prueba tool calling, JSON, imágenes y embeddings en varios modelos de chat.
//   ffmpeg -f lavfi -i color=red:s=64x64 -frames:v 1 salida/rojo.png
//   node --env-file=.env probes/capacidades-chat.mjs salida/rojo.png
import { readFile } from "node:fs/promises";
const B = "https://api.shisa.ai/openai/v1", H = { "Content-Type": "application/json", Authorization: "Bearer " + process.env.SHISA_API_KEY };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function call(name, path, body) {
  await sleep(1500);
  const t0 = performance.now();
  const r = await fetch(B + path, { method: "POST", headers: H, body: JSON.stringify(body) });
  const txt = await r.text(); let j; try { j = JSON.parse(txt); } catch {}
  const m = j?.choices?.[0]?.message;
  const out = m ? (m.tool_calls ? "TOOL_CALLS " + JSON.stringify(m.tool_calls.map((t) => t.function)) : m.content) : j?.data ? `embedding dim=${j.data[0]?.embedding?.length}` : txt;
  console.log(`${name.padEnd(34)} ${r.status} ${Math.round(performance.now() - t0)}ms  ${String(out).replace(/\s+/g, " ").slice(0, 220)}`);
}
const tools = [{ type: "function", function: { name: "gesto", description: "Mueve al robot para expresar una emoción", parameters: { type: "object", properties: { emocion: { type: "string", enum: ["alegria", "sorpresa", "tristeza", "duda"] } }, required: ["emocion"] } } }];
const userGesto = [{ role: "system", content: "Eres un robot. Si el usuario expresa una emoción, llama a la herramienta gesto." }, { role: "user", content: "¡Ganamos la hackathon! ¡No lo puedo creer!" }];
for (const model of ["shisa-ai/chotto", "shisa-ai/shisa-v2.1-llama3.3-70b", "qwen3.8-flash"])
  await call(`tools · ${model}`, "/chat/completions", { model, messages: userGesto, tools });
for (const model of ["shisa-ai/chotto", "shisa-ai/shisa-v2.1-llama3.3-70b"])
  await call(`json · ${model}`, "/chat/completions", { model, response_format: { type: "json_object" }, messages: [{ role: "user", content: 'Traduce al japonés y responde JSON {"ja": "...", "emocion": "alegria|sorpresa|tristeza|duda"}: ¡Ganamos la hackathon!' }] });
const img = "data:image/png;base64," + (await readFile(process.argv[2])).toString("base64");
for (const model of ["qwen3.8-flash", "kimi-k3", "glm-5.2", "shisa-ai/shisa-v2.1-llama3.3-70b"])
  await call(`imagen · ${model}`, "/chat/completions", { model, max_tokens: 60, messages: [{ role: "user", content: [{ type: "text", text: "¿De qué color es esta imagen? Una palabra." }, { type: "image_url", image_url: { url: img } }] }] });
await call("embeddings · shisa-ai/chotto", "/embeddings", { model: "shisa-ai/chotto", input: "hola" });
