# Hashi (橋)

A small Expo Go app that receives a two-person conversation over WebSocket. Tap a message’s Translate button to translate its original text into the language of the **other person’s latest message**. Originals remain visible. Translations use the Shisa translation API with its default model.

## Run

```sh
cd ~/Projects/test/hashi/mobile-app
mise install
npm install
npm run server
```

In a second terminal:

```sh
cd ~/Projects/test/hashi/mobile-app
npm start
```

Open the QR code in **Expo Go matching SDK 57**, with the phone and computer on the same Wi-Fi. The app automatically uses the Expo development server’s LAN hostname with port `8080`. Allow local network access when prompted. If that address is incorrect, tap **Server settings** at the top of the screen above the people and enter a phone address printed by the socket server. On a physical phone, `localhost` refers to the phone.

Use [Expo’s download page](https://expo.dev/go) for a matching Expo Go build. SDK compatibility follows [Expo’s version guidance](https://docs.expo.dev/troubleshooting/expo-go-version-mismatch/). `npm run ios` opens the iOS simulator, `npm run android` opens the Android emulator, and `npm run web` opens a browser preview. For an Android emulator, use `ws://10.0.2.2:8080` if automatic discovery cannot reach the server. Expo tunnels only tunnel Metro; the WebSocket server still needs its own reachable address.

The server immediately sends four example messages, then streams two more after 8 and 16 seconds. Restart it to restart the demo. Any other socket client can publish messages through the documented protocol. Set `DEMO=false` in `server/.env` for an empty conversation supplied entirely by your own publisher.

## Configuration

The supplied Shisa token is already in `server/.env`, a file ignored by Git with owner-only permissions. It is loaded only by the Node server and never imported into the Expo app. For a fresh checkout, copy `server/.env.example` to `server/.env` and set `SHISA_API_KEY`. Set `PORT` there to change the server port.

Optionally copy `.env.example` to `.env` and set `EXPO_PUBLIC_WS_URL` to change the initial socket address. **Only the socket URL belongs in this public Expo environment file.** Restart Metro after changing it. The in-app address override lasts for the current app session.

The app needs a server implementing [WEBSOCKET.md](./WEBSOCKET.md), including translation requests. The included server uses [Shisa’s translation API](https://docs.shisa.ai/translation/endpoints/) through Node’s native `fetch`, with Shisa’s default `shisa-ai/chotto` model. Only the selected message is sent to Shisa when Translate is tapped. Language pairs must be supported by Shisa; rejected pairs show a retryable error. Successful translations are cached in server memory by message ID and target language; API errors, timeouts, and incomplete results remain retryable.

This is a local development server: it accepts unauthenticated publishers and translation requests on the LAN. Before hosting it publicly, add authentication and TLS (`wss://`). Conversation history and translation caches are in memory and reset when the server restarts.

## Checks and structure

```sh
npm run typecheck
npm run lint
npx expo-doctor
```

Verified with TypeScript, ESLint, Expo Doctor, iOS/Android/web exports, and live Shisa translation requests including a language switch. The app has not been visually checked or run on a physical device.

`npm audit` currently reports 22 inherited Expo/React Native dependency findings (15 high, 7 moderate), rooted in `braces`, `node-forge`, and an older `uuid`. Compatible automatic fixes were applied. The remaining suggested force-fix downgrades Expo to SDK 44, so it was not applied; revisit these when upstream patches are available.

- `App.tsx`: one chat screen and a socket address dialog, built with React Native components and Expo-compatible safe areas.
- `useConversation.ts`: native WebSocket connection, retry every three seconds, response correlation, translation state, and timeouts.
- `protocol.ts`: shared types and runtime validation for network events.
- `server/index.ts`: demo feed, message publishing, and Shisa translation using native HTTP requests.
- `WEBSOCKET.md`: the wire format for replacing or integrating the server.

No router, UI framework, database, authentication flow, message composer, speech recognition, or automatic translation is included.
