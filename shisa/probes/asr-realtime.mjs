// ASR en tiempo real por WebSocket con traducción en el mismo socket. Necesita PCM s16le mono 16 kHz:
//   ffmpeg -i frase.wav -af apad=pad_dur=1.5 -ar 16000 -ac 1 -f s16le frase.pcm
//   ASR_LANG=ja node --env-file=.env probes/asr-realtime.mjs frase.pcm
// Con un idioma fijo traduce; con "auto" la traducción falla (source_language_unknown, 2026-10-09).
import { readFile } from "node:fs/promises";
const pcm = await readFile(process.argv[2]);
const t0 = performance.now(); const ms = () => Math.round(performance.now() - t0);
const ws = new WebSocket("wss://api.shisa.ai/ws/asr/realtime", { headers: { Authorization: "Bearer " + process.env.SHISA_API_KEY } });
const send = (o) => ws.send(JSON.stringify(o));
ws.onopen = async () => {
  console.log(`${ms()}ms open`);
  send({ type: "session.update", session: { input_audio_format: "pcm_s16le", sample_rate: 16000, channels: 1, language: process.env.ASR_LANG || "ja" } });
  send({ type: "router.translation.update", id: "to-en", enabled: true, target_langs: ["en"] });
  const chunk = 3200; // 100 ms a 16 kHz s16le mono
  for (let i = 0; i < pcm.length; i += chunk) {
    send({ type: "input_audio.append", audio: pcm.subarray(i, i + chunk).toString("base64") });
    await new Promise((r) => setTimeout(r, 100)); // ritmo de tiempo real
  }
  console.log(`${ms()}ms audio enviado`);
  setTimeout(() => send({ type: "session.close" }), 3000);
};
ws.onmessage = (e) => { const j = JSON.parse(e.data); const { type, ...rest } = j; console.log(`${ms()}ms ${type} ${JSON.stringify(rest).slice(0, 260)}`); };
ws.onerror = (e) => console.log(`${ms()}ms error`, e.message ?? e);
ws.onclose = (e) => { console.log(`${ms()}ms close ${e.code} ${e.reason}`); process.exit(0); };
setTimeout(() => { console.log("timeout"); process.exit(1); }, 30000);
