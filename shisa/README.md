# Shisa API — qué hace y cómo la usamos

Para qué la usamos: un **Reachy Mini que hace de intérprete japonés ↔ inglés**. Escucha a una persona,
traduce y dice la traducción en voz alta. Shisa cubre las tres partes del circuito:

```
micrófono → [ASR] voz a texto → [chat] traducción con streaming → [TTS] texto a voz → parlante
```

Todo lo de esta guía se midió contra la API real el **2026-10-09**. Los scripts para repetir cada prueba
están en [probes/](probes/).

**Arrancar** (Node 22 o más nuevo, sin dependencias):

```sh
cd shisa
cp .env.example .env      # y poner la SHISA_API_KEY
npm test                  # 6 tests, sin llamar a la API
npm run translate         # traductor por texto: "a: Hi!" (EN→JA) / "b: こんにちは" (JA→EN)
```

**Relación con [mobile-app/](../mobile-app/) (Hashi):** Hashi traduce texto con `/translate/` y no
reconoce voz. Lo de esta carpeta suma la voz:
- El ASR en tiempo real puede publicar los mensajes de cada persona en el WebSocket de Hashi. Ya trae el
  `language` detectado que pide [WEBSOCKET.md](../mobile-app/WEBSOCKET.md).
- El TTS hace que el robot diga la traducción.

## Acceso

- Key en `shisa/.env` como `SHISA_API_KEY=…` (copiar de `.env.example`; ignorado por git). Los scripts la leen con
  `node --env-file=.env …`.
- Todas las rutas usan el header `Authorization: Bearer $SHISA_API_KEY`.
- Las rutas están repartidas en varias bases:
  - Chat (compatible con OpenAI): `https://api.shisa.ai/openai/v1`
  - Traducción dedicada: `https://api.shisa.ai/translate/`
  - Voz: `https://api.shisa.ai/asr/…` y `https://api.shisa.ai/tts`.
    `/openai/v1/audio/*` **no existe** (404 "Unsupported API endpoint").
  - Tiempo real (WebSocket): `wss://api.shisa.ai/ws/asr/realtime` y `wss://api.shisa.ai/ws/tts/realtime`
- Documentación oficial: [docs.shisa.ai](https://docs.shisa.ai/) (LLM, Translation, ASR, TTS) ·
  [ASR](https://platform.shisa.ai/en/solutions/asr) · [TTS](https://platform.shisa.ai/en/solutions/tts).

## Qué hace y qué no

| Capacidad | ¿Funciona? | Ruta | Evidencia (2026-10-09) |
|---|---|---|---|
| Traducción / chat con streaming | Sí | `POST /openai/v1/chat/completions` | Charlas EN↔JA y ES↔JA correctas |
| Traducción dedicada, con y sin streaming | Sí | `POST /translate/` | Primer dato en 190 ms con streaming. La usa `mobile-app` (Hashi) |
| **Voz a texto en tiempo real + traducción en el mismo socket** | Sí, con idioma fijo | `wss://…/ws/asr/realtime` | Texto parcial mientras se habla; traducción 218 ms después del texto final |
| Respuesta en JSON | Sí | igual, `response_format: {type: "json_object"}` | `chotto` devolvió `{"ja": …, "emocion": "alegria"}` en 304 ms |
| Tool calling (el modelo pide ejecutar una función) | Sí | igual, `tools: [...]` | `chotto`, el 70b y `qwen3.8-flash` pidieron `gesto(alegria)` |
| Ver imágenes | Sí, con `qwen3.8-flash` | igual, contenido `image_url` en base64 | Contestó "Rojo" a un cuadrado rojo. No probado con texto japonés |
| Voz a texto (ASR) | Sí, JA / EN / ZH | `POST /asr/srt/audio_llm` | Ida y vuelta exacta en JA (0,984) y EN (0,996) |
| Texto a voz (TTS) con streaming | Sí, solo con algunas voces | `POST /tts` | Ver la tabla de voces |
| Español en voz | **No** | — | Ninguna de las 77 voces es en español; el ASR lista JA/EN/ZH |
| Embeddings | No | `/openai/v1/embeddings` | 404 "Unsupported API endpoint" |
| Imágenes con el 70b de Shisa | No | — | 400 "is not a multimodal model" |
| Imágenes con `kimi-k3` / `glm-5.2` | Sin determinar | — | 200 con respuesta vacía (probablemente gastaron los 60 tokens pensando) |

## 1. Traducción (chat)

```bash
curl https://api.shisa.ai/openai/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer $SHISA_API_KEY" \
  -d '{"model": "shisa-ai/chotto", "stream": true, "temperature": 0.2,
       "messages": [
         {"role": "system", "content": "Translate the user message from English to natural Japanese. Output only the translation."},
         {"role": "user", "content": "Hi, I am Daniel. Thanks for coming."}]}'
```

Con `stream: true` responde `text/event-stream`: líneas `data: {…}` con el texto en
`choices[0].delta.content`, y al final `data: [DONE]`. Los pedazos pueden cortar un carácter japonés por
la mitad, así que hay que juntar los bytes antes de leerlos. Eso lo resuelve `parseSSE` en
[client.mjs](client.mjs).

**Modelos** (`GET /openai/v1/models`, 15 en total): `shisa-ai/chotto`, `shisa-ai/chotto-26b`,
`shisa-ai/shisa-v2.1-llama3.3-70b`, `shisa-ai/shisa-v2.1-unphi4-14b`, `shisa-ai/shisa-de-1`,
`shisa-ai/shisa-de-2`, `qwen3.7-flash/plus/max`, `qwen3.8-flash/max/27b`, `qwen38-flash-next-nvfp4`,
`kimi-k3` y `glm-5.2`. Los `shisa-ai/*` tienen un contexto de 32k tokens (16k el `unphi4-14b`).

| Modelo | Primera palabra | Turno completo | Para qué |
|---|---|---|---|
| `shisa-ai/chotto` (**por defecto**) | 199–1215 ms | 260–1361 ms | Traducir y responder JSON: el más rápido en esos casos |
| `shisa-ai/chotto-26b` | 319 ms (1 prueba) | 360 ms | Alternativa parecida a `chotto` |
| `shisa-ai/shisa-v2.1-llama3.3-70b` | 193–580 ms | 468–868 ms | Tool calling rápido (870 ms; `chotto` tardó 2,8 s). Traduce más literal ("よろしくお願いします" → "cuídate de mí") |
| `qwen3.8-flash` | — | ~2 s | El único que vimos que entiende imágenes |

La latencia varía bastante entre corridas: el mismo `chotto` dio 199–346 ms en una charla y 313–1215 ms en
otra.

**Contexto de la conversación:** [translator.mjs](translator.mjs) manda los últimos 12 turnos (original →
traducción) en el mensaje de sistema. Así no cambian los nombres, el tono ni el nivel de cortesía.

### Endpoint de traducción dedicado: `/translate/`

Es más simple que el chat: no hay que escribir prompt. Lo usa `mobile-app/server/index.ts` (Hashi).

```bash
curl https://api.shisa.ai/translate/ -H "Authorization: Bearer $SHISA_API_KEY" \
  -F text="ロボットが通訳するんですか？" -F source_lang=ja -F target_lang=en -F stream=true
```

- Va como `multipart/form-data`. Obligatorios: `text` (hasta 10.000 caracteres), `source_lang` y
  `target_lang`. Opcionales: `stream` (`"true"`/`"false"`), `context` (hasta 2000 caracteres, para pasar
  los turnos anteriores), `keywords` (glosario: hasta 20 términos que no se traducen) y `model`.
- Sin streaming devuelve JSON con el texto en `choices[0].message.content`. Con streaming manda SSE con
  `choices[0].delta.content`, igual que el chat.
- Medido: EN→JA 692 ms sin streaming; JA→EN con streaming, primer dato en 190 ms. La documentación dice que
  el modelo por defecto es `chotto`, pero la respuesta informó `shisa-ai/chotto-26b`.
- Se cobra por cada 1000 caracteres de texto de origen. Las cuentas nuevas traen USD 10 de crédito
  ([precios](https://docs.shisa.ai/translation/pricing/)).

## 2. Voz a texto (ASR)

```bash
curl https://api.shisa.ai/asr/srt/audio_llm \
  -H "Content-Type: application/json" -H "Authorization: Bearer $SHISA_API_KEY" \
  -d "{\"audio\": \"$(base64 -i frase.wav)\"}"
# → {"text":"こんにちはダニエルです。…","language":"ja","confidence":0.984}
```

- `audio`: WAV (PCM 16 bit), OGG, MP3 o FLAC, **en base64**, el archivo entero.
- `language` es opcional (`ja`, `en`, `zh`). Sin él, detecta el idioma solo, y eso decide hacia dónde se
  traduce.
- Opcionales según la documentación: `hotwords` (lista de términos a reconocer mejor, como nombres
  propios o "Reachy"), `temperature`, `top_p`, `vad`.
- Medido: 1052 ms (EN) y 1225 ms (JA) para unos 5 s de audio.
- Esta ruta va **por turno**: se manda la frase entera cuando la persona termina de hablar. Para escuchar
  en vivo está el WebSocket de la sección siguiente.

### Voz a texto en tiempo real (WebSocket) — **la mejor opción para el robot**

`wss://api.shisa.ai/ws/asr/realtime` con el header `Authorization: Bearer …`. La key necesita el permiso
`shisa/asr-realtime`; la nuestra lo tiene. Probado con [probes/asr-realtime.mjs](probes/asr-realtime.mjs).

1. Mandar `{"type":"session.update","session":{"input_audio_format":"pcm_s16le","sample_rate":16000,"channels":1,"language":"ja"}}`.
2. Opcional, para traducir en el mismo socket:
   `{"type":"router.translation.update","id":"to-en","enabled":true,"target_langs":["en"]}`.
3. Mandar el audio cada ~100 ms: `{"type":"input_audio.append","audio":"<base64 de PCM s16le mono 16 kHz>"}`.
   Sin encabezado WAV y sin float: hay que convertir el `float32` del Reachy Mini.
4. Llegan los eventos `speech_started`, `asr.partial_result` (texto parcial), `speech_stopped` (fin de
   frase detectado por el servidor), `asr.final_result` y `translation.final_result`.
5. Cerrar con `{"type":"session.close"}`.

Medido (2026-10-09, una frase japonesa de 5,6 s enviada a ritmo real):
- El texto parcial empieza a llegar a los ~2 s de empezar a hablar.
- El fin de frase se detecta ~1,3 s después de que termina el audio (estimado comparando el reloj del
  envío con los tiempos del servidor).
- La traducción llega 218 ms después del texto final:
  "Hello, this is Daniel. There is a meeting tomorrow at 3:00, but how is your availability?"

Ojo con el idioma:
- Con `language: "auto"` y `language_detection_mode: "utterance"` detecta bien el idioma (confianza ≈1,0),
  pero la traducción del socket falla con `translation.error source_language_unknown`.
- **Con idioma fijo funciona.** Para una charla JA↔EN hay dos caminos:
  - Dos sesiones, una por idioma o por micrófono.
  - `auto` sin la traducción del socket, y traducir aparte con `/translate/` usando el idioma detectado.

## 2b. Texto a voz en tiempo real (WebSocket) — sin probar

`wss://api.shisa.ai/ws/tts/realtime` (según [docs](https://docs.shisa.ai/tts/websocket)):
- Se configura con `session.update` (`voice_id`, `format`, `sample_rate`, que por defecto es 24000, y
  `audio_transport`: `binary` o `base64_json`).
- Cada texto se manda completo en un `tts.speak`. **No acepta texto por partes**, y procesa una síntesis a
  la vez por sesión.
- Llegan los eventos `tts.audio.start`, el audio, `tts.audio.done` y `tts.usage`.

Sirve para no abrir una conexión HTTP por cada oración.

## 3. Texto a voz (TTS)

```bash
curl https://api.shisa.ai/tts \
  -H "Content-Type: application/json" -H "Authorization: Bearer $SHISA_API_KEY" \
  -d '{"voice_id": "762fbbc6-4b7e-49b9-a6ea-7506333cb79b", "text": "こんにちは", "format": "pcm", "stream": true, "sample_rate": 16000}' \
  --output voz.pcm
```

- `voice_id` (obligatorio), `text` (hasta 5000 caracteres), `format`: `wav`, `mp3`, `ogg`, `flac` o `pcm`.
  `stream: true` manda el audio a medida que se genera.
- La respuesta es el audio directo, no JSON. En WAV y MP3 sale a 24 kHz mono.
- `sample_rate` no figura en la página, pero la API lo acepta. Con 16000 devolvió PCM que, leído a 16 kHz,
  dura 6,96 s (razonable para la frase). **Falta escucharlo para confirmar**; el Reachy Mini necesita
  16 kHz.
- `GET /tts/voices` devuelve `{voices: [...]}` con `id`, `language`, `gender`, `formats`, `streaming` y
  `sample_rates`. Hay 77 voces: japonés, inglés, chino y coreano.

**No confíes en el campo `streaming` de la lista.** Voces probadas:

| Voz | ID | Streaming | Primer dato |
|---|---|---|---|
| Aiden (JA) | `5c6ae40b-58fe-43f2-9f53-69752a79ebdd` | Sí | 79 ms (WAV, puede ser solo el encabezado) |
| Ono Anna (JA) | `762fbbc6-4b7e-49b9-a6ea-7506333cb79b` | Sí | 406 ms WAV · 333 ms PCM 16 kHz |
| Japonesa e inglesa, joven, enérgica | `293621ff-e6e2-4ce5-bf15-6b23ad66b1f4` | Sí | 568 ms (MP3) |
| Ryan (EN) | `1a5b71d7-05c9-4acd-9cb8-0984ddacd969` | Sí | 87 ms (WAV) |
| JA Female Customer Service | `61ba1141-60aa-4bc3-a3b3-be1ec20700b3` | **No**, aunque la lista dice que sí | 3,1 s sin streaming |
| JA Female Announcements | `e460538b-b236-49bf-891b-6e652c8207fe` | **No**, aunque la lista dice que sí | — |

El tiempo más honesto hasta que suena algo es el de PCM, porque no lleva encabezado: unos 300–600 ms.

## Límites y errores

| Respuesta | Cuándo | Qué hacer |
|---|---|---|
| **429** `Too many requests. Retry after 1.72s` | Tras unos 5–11 pedidos casi seguidos | Esperar lo que dice el mensaje y reintentar. `streamChat` lo hace hasta 3 veces y avisa por pantalla |
| 400 `ErrStreamingNotSupported` | `stream: true` con una voz que no lo soporta | Usar una voz de la tabla de arriba |
| 400 `is not a multimodal model` | Imagen a un modelo que no ve | Usar `qwen3.8-flash` |
| 404 `Unsupported API endpoint` | Rutas de audio o embeddings bajo `/openai/v1` | Voz: usar `/asr` y `/tts` |

Los precios no figuran en las páginas que leímos (remiten al dashboard).

## Cuánto tarda una vuelta completa en el robot

Es una **suma de mediciones por partes**, no una medición de punta a punta. Desde que la persona deja de
hablar hasta que el robot empieza a hablar:

| Paso | Por turno (HTTP) | Tiempo real (WebSocket) |
|---|---|---|
| Detectar que terminó de hablar | ~0,5 s (estimado, no medido) | ~1,3 s (lo detecta el servidor) |
| Voz a texto | 1,0–1,2 s | ya llegó (texto final junto con el fin de frase) |
| Traducción | ~0,2–1,2 s (primera oración) | 0,22 s (en el mismo socket) |
| Primer audio de la voz | ~0,3–0,6 s | ~0,3–0,6 s |
| **Total** | **~2–3,5 s** | **~1,8–2,1 s** |

Mientras el robot habla hay que silenciar su micrófono. Si no, se escucha y se traduce a sí mismo.

## Nuestro código

| Archivo | Qué hace |
|---|---|
| [client.mjs](client.mjs) | `streamChat()`: pide la traducción con streaming, reintenta los 429 y muestra los errores |
| [translator.mjs](translator.mjs) | `createConversation()`: traduce turno por turno con el contexto de la charla |
| [cli.mjs](cli.mjs) | Traductor por texto: `npm run translate` (`a:` inglés → japonés, `b:` japonés → inglés) |
| [client.test.mjs](client.test.mjs) | 6 tests sin llamar a la API: `npm test` |
| [probes/](probes/) | Las pruebas contra la API real de esta guía (cada script dice cómo correrlo) |

Cambiar idiomas o modelo: `SPEAKER_A=en SPEAKER_B=ja SHISA_MODEL=shisa-ai/chotto npm run translate`.

ASR y TTS todavía no tienen código propio, solo las pruebas de [probes/](probes/). El SDK del Reachy Mini
es Python 3.10–3.12, así que el circuito del robot va en Python y reusa lo que dice esta guía.

## Sin verificar

- Si `sample_rate: 16000` en el TTS realmente devuelve 16 kHz (hay que escuchar el audio).
- El TTS por WebSocket: la documentación está leída, pero no lo probamos.
- Si el ASR en tiempo real traduce en el socket con `auto` cuando el backend habilita detección por frase.
- Si `qwen3.8-flash` lee bien texto japonés en fotos.
- Si `kimi-k3` y `glm-5.2` entienden imágenes.
- Precios y límite exacto de pedidos.
