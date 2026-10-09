import { createAudioPlayer, setAudioModeAsync, type AudioPlayer } from 'expo-audio';
import { File, Paths } from 'expo-file-system';
import { useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';
import { shisaSpeech } from './shisaSpeech';

/** Keep one read-aloud request active and dispose of its audio when it stops. */
export function useSpeech() {
  const [speech, setSpeech] = useState<{ key: string; pending: boolean; error?: string } | null>(null);
  const active = useRef<{ key: string; stop: () => void } | null>(null);

  useEffect(() => () => active.current?.stop(), []);

  async function speak(key: string, text: string, language: string) {
    const stopping = active.current?.key === key;
    active.current?.stop();
    if (stopping) {
      setSpeech(null);
      return;
    }

    let cancelled = false;
    let player: AudioPlayer | undefined;
    let subscription: { remove: () => void } | undefined;
    let file: File | undefined;
    let objectUrl: string | undefined;
    const controller = new AbortController();
    const timer = setTimeout(() => fail('Speech timed out. Tap the speaker to retry.'), 45_000);

    function stop() {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
      subscription?.remove();
      player?.remove();
      if (file?.exists) file.delete();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      active.current = null;
    }

    function fail(error: string) {
      if (cancelled) return;
      stop();
      setSpeech({ key, pending: false, error });
    }

    active.current = { key, stop };
    setSpeech({ key, pending: true });
    try {
      await setAudioModeAsync({ playsInSilentMode: true, interruptionMode: 'doNotMix' });
      if (cancelled) return;
      const bytes = await shisaSpeech(text, language, controller.signal);
      if (cancelled) return;
      let uri: string;
      if (Platform.OS === 'web') {
        objectUrl = URL.createObjectURL(new Blob([bytes], { type: 'audio/mpeg' }));
        uri = objectUrl;
      } else {
        file = new File(Paths.cache, `hashi-speech-${Date.now()}.mp3`);
        file.write(bytes);
        uri = file.uri;
      }
      player = createAudioPlayer({ uri });
      subscription = player.addListener('playbackStatusUpdate', status => {
        if (cancelled) return;
        if (status.error) fail('Could not play the audio. Tap the speaker to retry.');
        else if (status.didJustFinish) {
          stop();
          setSpeech(null);
        } else if (status.playing) {
          clearTimeout(timer);
          setSpeech(previous => previous?.pending ? { key, pending: false } : previous);
        }
      });
      player.play();
    } catch (error) {
      fail(error instanceof TypeError ? 'Cannot reach Shisa. Check your connection and tap the speaker to retry.'
        : error instanceof Error ? error.message : 'Could not read the message. Tap the speaker to retry.');
    }
  }

  return { speech, speak };
}
