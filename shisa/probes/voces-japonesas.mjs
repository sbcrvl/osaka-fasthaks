// Qué voces japonesas aceptan streaming, PCM a 16 kHz y transcripción de ida y vuelta.
//   node --env-file=.env probes/voces-japonesas.mjs salida
import { readFile, writeFile } from "node:fs/promises";
const T = process.argv[2], A = { Authorization: "Bearer " + process.env.SHISA_API_KEY };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = "こんにちは、ダニエルです。明日三時に会議がありますが、ご都合はいかがですか？";
async function tts(name, voice_id, extra) {
  await sleep(1500);
  const t0 = performance.now(); let first, bytes = 0; const parts = [];
  const r = await fetch("https://api.shisa.ai/tts", { method: "POST", headers: { ...A, "Content-Type": "application/json" }, body: JSON.stringify({ voice_id, text, ...extra }) });
  if (!r.ok) { console.log(`TTS ${name}: ${r.status} ${(await r.text()).slice(0, 160)}`); return; }
  for await (const c of r.body) { first ??= performance.now() - t0; bytes += c.length; parts.push(c); }
  const file = `${T}/tts_ja_${name}.${extra.format}`; await writeFile(file, Buffer.concat(parts));
  console.log(`TTS ${name}: ${r.status} ${r.headers.get("content-type")} primer audio ${Math.round(first)}ms total ${Math.round(performance.now() - t0)}ms ${bytes}B`);
  return file;
}
const voces = {
  "Ono Anna": "762fbbc6-4b7e-49b9-a6ea-7506333cb79b",
  "JA&EN energetic F": "293621ff-e6e2-4ce5-bf15-6b23ad66b1f4",
  "JA Female Announcements": "e460538b-b236-49bf-891b-6e652c8207fe",
  "Aiden JA": "5c6ae40b-58fe-43f2-9f53-69752a79ebdd",
};
let ok;
for (const [n, id] of Object.entries(voces)) ok = (await tts(`${n}`.replace(/\W+/g, "_"), id, { format: n.startsWith("JA&EN") ? "mp3" : "wav", stream: true })) ?? ok;
await tts("Ono_Anna_pcm16k", voces["Ono Anna"], { format: "pcm", stream: true, sample_rate: 16000 });
await sleep(1500);
const f = ok ?? `${T}/tts_ja_nostream.wav`;
const t0 = performance.now();
const r = await fetch("https://api.shisa.ai/asr/srt/audio_llm", { method: "POST", headers: { ...A, "Content-Type": "application/json" }, body: JSON.stringify({ audio: (await readFile(f)).toString("base64") }) });
console.log(`ASR ja ida y vuelta (${f.split("/").pop()}): ${r.status} ${Math.round(performance.now() - t0)}ms ${(await r.text()).slice(0, 300)}`);
