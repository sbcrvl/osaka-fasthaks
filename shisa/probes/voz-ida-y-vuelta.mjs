// Texto a voz (JA y EN) y después voz a texto del mismo audio. Guarda los audios en la carpeta dada.
//   node --env-file=.env probes/voz-ida-y-vuelta.mjs salida
import { readFile, writeFile } from "node:fs/promises";
const T = process.argv[2], A = { Authorization: "Bearer " + process.env.SHISA_API_KEY };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const vj = await (await fetch("https://api.shisa.ai/tts/voices", { headers: A })).json();
const voices = Array.isArray(vj) ? vj : (vj.voices ?? vj.data);
const VOICES = { ja: "61ba1141-60aa-4bc3-a3b3-be1ec20700b3" /* JA Female Customer Service */, en: "1a5b71d7-05c9-4acd-9cb8-0984ddacd969" /* Ryan - English */ };
for (const [k, id] of Object.entries(VOICES)) console.log(`voz ${k} sample_rates:`, JSON.stringify(voices.find((v) => v.id === id).sample_rates));

async function tts(name, voice_id, text, format, stream) {
  await sleep(1500);
  const t0 = performance.now(); let first, bytes = 0; const parts = [];
  const r = await fetch("https://api.shisa.ai/tts", { method: "POST", headers: { ...A, "Content-Type": "application/json" }, body: JSON.stringify({ voice_id, text, format, stream }) });
  if (!r.ok) { console.log(`TTS ${name}: ${r.status} ${(await r.text()).slice(0, 300)}`); return; }
  for await (const c of r.body) { first ??= performance.now() - t0; bytes += c.length; parts.push(c); }
  const file = `${T}/tts_${name}.${format}`; await writeFile(file, Buffer.concat(parts));
  console.log(`TTS ${name}: ${r.status} ${r.headers.get("content-type")} primer audio ${Math.round(first)}ms total ${Math.round(performance.now() - t0)}ms ${bytes}B`);
  return file;
}
async function asr(name, file, language) {
  await sleep(1500);
  const t0 = performance.now();
  const body = { audio: (await readFile(file)).toString("base64"), ...(language && { language }) };
  const r = await fetch("https://api.shisa.ai/asr/srt/audio_llm", { method: "POST", headers: { ...A, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  console.log(`ASR ${name}: ${r.status} ${Math.round(performance.now() - t0)}ms ${(await r.text()).slice(0, 300)}`);
}
const text = { ja: "こんにちは、ダニエルです。明日三時に会議がありますが、ご都合はいかがですか？", en: "Hi, I'm Daniel. We have the meeting tomorrow at three, does that work for you?" };
for (const k of ["ja", "en"]) {
  const f = await tts(`${k}_stream`, VOICES[k], text[k], "wav", true);
  if (f) await asr(`${k} ida y vuelta (auto)`, f);
}
await tts("ja_nostream", VOICES.ja, text.ja, "wav", false);
