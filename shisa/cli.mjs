#!/usr/bin/env node
// Traduce una conversación entre dos personas con streaming.
//
//   Interactivo:  node --env-file=.env cli.mjs
//                 > a: Hi, how are you?           (A habla en inglés → japonés)
//                 > b: 元気です、ありがとう。      (B habla en japonés → inglés)
//   Archivo:      node --env-file=.env cli.mjs conversacion.txt
//                 (una línea por turno con el mismo formato "a: ..." / "b: ...")
//
// Idiomas: SPEAKER_A=en SPEAKER_B=ja por defecto. Modelo: SHISA_MODEL.

import { readFile } from "node:fs/promises";
import { createInterface } from "node:readline/promises";
import { createConversation } from "./translator.mjs";

const speakers = { a: process.env.SPEAKER_A || "en", b: process.env.SPEAKER_B || "ja" };
const conv = createConversation();

async function turn(line) {
  const m = line.match(/^\s*([ab])\s*:\s*(.+)$/i);
  if (!m) {
    if (line.trim()) console.error('Formato: "a: texto" o "b: texto"');
    return;
  }
  const who = m[1].toLowerCase();
  const from = speakers[who];
  const to = speakers[who === "a" ? "b" : "a"];
  process.stdout.write(`${who.toUpperCase()} [${from}→${to}] `);
  const t0 = performance.now();
  let ttft;
  for await (const delta of conv.translate({ speaker: who.toUpperCase(), text: m[2], from, to })) {
    ttft ??= performance.now() - t0;
    process.stdout.write(delta);
  }
  process.stdout.write(`   (${Math.round(ttft)} ms / ${Math.round(performance.now() - t0)} ms)\n`);
}

const file = process.argv[2];
if (file) {
  for (const line of (await readFile(file, "utf8")).split("\n")) {
    if (line.trim()) console.log(`\n${line.trim()}`);
    await turn(line);
  }
} else {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  console.log(`A=${speakers.a}  B=${speakers.b}  ·  "a: texto" / "b: texto"  ·  Ctrl+D para salir`);
  for await (const line of rl) await turn(line);
}
