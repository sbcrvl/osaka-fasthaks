import Constants from 'expo-constants';
import { StatusBar } from 'expo-status-bar';
import { useRef, useState } from 'react';
import { ActivityIndicator, FlatList, KeyboardAvoidingView, Modal, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { languageName, otherUser, type Message, type UserId } from './protocol';
import { useConversation } from './useConversation';

const host = Constants.expoConfig?.hostUri?.split(':')[0] ?? 'localhost';
const defaultUrl = process.env.EXPO_PUBLIC_WS_URL ?? `ws://${host}:8080`;
const people: Record<UserId, string> = { 'person-1': 'Person 1', 'person-2': 'Person 2' };

/** A quiet shared view of two people speaking across languages. */
function Conversation({ url, onConnect }: { url: string; onConnect: (url: string) => void }) {
  const [draft, setDraft] = useState(url);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [addressError, setAddressError] = useState('');
  const { messages, status, error, translations, translate } = useConversation(url);
  const list = useRef<FlatList<Message>>(null);
  const nearBottom = useRef(true);
  const latest: Partial<Record<UserId, string>> = {};
  for (const message of messages) latest[message.userId] = message.language;
  const connected = status === 'connected';

  function connect() {
    try {
      const address = new URL(draft.trim());
      if (!['ws:', 'wss:'].includes(address.protocol) || !address.hostname) throw new Error();
      onConnect(address.toString());
      nearBottom.current = true;
      setSettingsOpen(false);
      setAddressError('');
    } catch {
      setAddressError('Enter a valid ws:// or wss:// address.');
    }
  }

  function renderMessage({ item: message }: { item: Message }) {
    const right = message.userId === 'person-2';
    const target = latest[otherUser(message.userId)];
    const sameLanguage = target === message.language;
    const translation = target ? translations[`${message.id}:${target}`] : undefined;
    const label = !target ? 'Waiting for the other person' : sameLanguage ? `Already in ${languageName(target)}` : translation?.pending ? 'Translating…' : translation?.text ? `Translated to ${languageName(target)}` : `Translate to ${languageName(target)}`;
    const disabled = !connected || !target || sameLanguage || translation?.pending || !!translation?.text;
    return (
      <View style={[styles.messageRow, right && styles.rightRow]}>
        <Text style={styles.messageMeta}>{people[message.userId]} · {languageName(message.language)}</Text>
        <View style={[styles.bubble, right ? styles.rightBubble : styles.leftBubble]}>
          <Text selectable style={styles.messageText}>{message.text}</Text>
          {translation?.text && (
            <View style={styles.translation}>
              <Text style={styles.translationLabel}>{languageName(target!)}</Text>
              <Text selectable style={styles.messageText}>{translation.text}</Text>
            </View>
          )}
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`${label}: ${message.text}`}
            accessibilityState={{ disabled: !!disabled, busy: translation?.pending }}
            disabled={!!disabled}
            onPress={() => target && translate(message, target)}
            style={({ pressed }) => [styles.translateButton, pressed && styles.pressed]}
          >
            {translation?.pending ? <ActivityIndicator size="small" color="#25685F" /> : <Text style={styles.translateIcon}>{translation?.text ? '✓' : '文'}</Text>}
            <Text style={[styles.translateText, !target && styles.muted]}>{label}</Text>
          </Pressable>
          {translation?.error && <Text accessibilityRole="alert" style={styles.inlineError}>{translation.error}</Text>}
        </View>
        <Text style={styles.time}>{new Date(message.sentAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</Text>
      </View>
    );
  }

  return (
    <SafeAreaView style={styles.safeArea}>
      <StatusBar style="dark" />
      <View style={styles.screen}>
        <View style={styles.people}>
          {(['person-1', 'person-2'] as const).map((userId, index) => (
            <View key={userId} style={[styles.person, index === 1 && styles.secondPerson]}>
              <View style={[styles.avatar, index === 1 && styles.secondAvatar]}><Text style={styles.avatarText}>{index + 1}</Text></View>
              <View><Text style={styles.personName}>{people[userId]}</Text><Text style={styles.personLanguage}>{latest[userId] ? languageName(latest[userId]!) : 'Waiting to speak'}</Text></View>
            </View>
          ))}
        </View>

        {!!error && <Text accessibilityRole="alert" style={styles.errorBanner}>{error}</Text>}
        <FlatList
          ref={list}
          data={messages}
          renderItem={renderMessage}
          keyExtractor={message => message.id}
          style={styles.list}
          contentContainerStyle={styles.messages}
          onScroll={({ nativeEvent }) => { nearBottom.current = nativeEvent.contentSize.height - nativeEvent.layoutMeasurement.height - nativeEvent.contentOffset.y < 100; }}
          scrollEventThrottle={100}
          onContentSizeChange={() => { if (nearBottom.current) list.current?.scrollToEnd({ animated: true }); }}
          ListHeaderComponent={<Text style={styles.sectionLabel}>THE CONVERSATION</Text>}
          ListEmptyComponent={
            <View style={styles.empty}>
              <Text style={styles.emptyCharacter}>橋</Text>
              <Text style={styles.emptyTitle}>Every conversation starts somewhere.</Text>
              <Text style={styles.emptyCopy}>{connected ? 'Waiting for the first message.' : 'Start the server, then connect to join the conversation.'}</Text>
            </View>
          }
        />
        <View style={styles.footer}>
          <Text style={styles.footerText}>Translate into the other person’s latest language.</Text>
          <Pressable accessibilityRole="button" onPress={() => { setDraft(url); setAddressError(''); setSettingsOpen(true); }} style={({ pressed }) => [styles.settingsButton, pressed && styles.pressed]}><Text style={styles.translateText}>Server settings</Text></Pressable>
        </View>
      </View>

      <Modal visible={settingsOpen} transparent animationType="fade" onRequestClose={() => setSettingsOpen(false)}>
        <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.overlay}>
          <View style={styles.sheet}>
            <Text accessibilityRole="header" style={styles.sheetTitle}>Connect to a conversation</Text>
            <Text style={styles.sheetCopy}>On your phone, use your computer’s Wi-Fi address and port 8080.</Text>
            <Text style={styles.inputLabel}>WEBSOCKET ADDRESS</Text>
            <TextInput accessibilityLabel="WebSocket address" autoCapitalize="none" autoCorrect={false} keyboardType="url" value={draft} onChangeText={setDraft} placeholder="ws://192.168.1.20:8080" placeholderTextColor="#737C78" style={styles.input} onSubmitEditing={connect} returnKeyType="go" />
            {!!addressError && <Text accessibilityRole="alert" style={styles.inlineError}>{addressError}</Text>}
            <View style={styles.sheetActions}>
              <Pressable accessibilityRole="button" onPress={() => setSettingsOpen(false)} style={styles.cancel}><Text style={styles.cancelText}>Cancel</Text></Pressable>
              <Pressable accessibilityRole="button" onPress={connect} style={({ pressed }) => [styles.connectButton, pressed && styles.pressed]}><Text style={styles.connectButtonText}>Connect</Text></Pressable>
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>
    </SafeAreaView>
  );
}

export default function App() {
  const [url, setUrl] = useState(defaultUrl);
  return <SafeAreaProvider><Conversation key={url} url={url} onConnect={setUrl} /></SafeAreaProvider>;
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: '#FAF8F3' },
  screen: { flex: 1, width: '100%', maxWidth: 760, alignSelf: 'center' },
  people: { flexDirection: 'row', marginHorizontal: 22, marginTop: 18, padding: 16, borderRadius: 20, backgroundColor: '#F0EEE6', gap: 12 },
  person: { flex: 1, flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap', gap: 10 },
  secondPerson: { borderLeftWidth: 1, borderLeftColor: '#DADDD2', paddingLeft: 16 },
  avatar: { width: 36, height: 36, borderRadius: 18, alignItems: 'center', justifyContent: 'center', backgroundColor: '#D6E5DD' },
  secondAvatar: { backgroundColor: '#EED9C9' },
  avatarText: { fontSize: 14, fontWeight: '600', color: '#364C42' },
  personName: { fontSize: 13, fontWeight: '600', color: '#34453D' },
  personLanguage: { fontSize: 12, color: '#66726B', marginTop: 3 },
  list: { flex: 1 },
  messages: { paddingHorizontal: 22, paddingTop: 26, paddingBottom: 24, gap: 22, flexGrow: 1 },
  sectionLabel: { textAlign: 'center', fontSize: 10, fontWeight: '600', letterSpacing: 2, color: '#66726B', marginBottom: 6 },
  messageRow: { alignItems: 'flex-start' },
  rightRow: { alignItems: 'flex-end' },
  messageMeta: { color: '#66726B', fontSize: 11, marginBottom: 7, paddingHorizontal: 3 },
  bubble: { maxWidth: '92%', paddingHorizontal: 17, paddingTop: 15, paddingBottom: 4, borderRadius: 20 },
  leftBubble: { backgroundColor: '#E7EEE6', borderTopLeftRadius: 5 },
  rightBubble: { backgroundColor: '#F3E6DA', borderTopRightRadius: 5 },
  messageText: { fontSize: 16, lineHeight: 25, color: '#263D35' },
  translation: { marginTop: 14, paddingTop: 12, borderTopWidth: 1, borderTopColor: '#CAD4C8', gap: 5 },
  translationLabel: { fontSize: 10, letterSpacing: 1, fontWeight: '700', color: '#25685F' },
  translateButton: { flexDirection: 'row', alignItems: 'center', gap: 7, minHeight: 44, paddingVertical: 10 },
  translateIcon: { fontSize: 15, color: '#25685F' },
  translateText: { fontSize: 12, fontWeight: '600', color: '#25685F', flexShrink: 1 },
  muted: { color: '#66726B' },
  time: { color: '#66726B', fontSize: 10, paddingHorizontal: 3, marginTop: 6 },
  footer: { paddingHorizontal: 22, paddingVertical: 16, borderTopWidth: 1, borderTopColor: '#E4E5DA' },
  footerText: { fontSize: 11, color: '#66726B', textAlign: 'center' },
  errorBanner: { marginHorizontal: 22, marginTop: 14, padding: 12, backgroundColor: '#F9E6DB', borderRadius: 12, color: '#873E24', fontSize: 13 },
  inlineError: { color: '#873E24', fontSize: 12, lineHeight: 18, paddingBottom: 10 },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', paddingVertical: 40, gap: 12 },
  emptyCharacter: { color: '#A3B4A6', fontSize: 60 },
  emptyTitle: { color: '#34453D', fontSize: 18, textAlign: 'center' },
  emptyCopy: { color: '#66726B', fontSize: 14, textAlign: 'center', lineHeight: 22 },
  settingsButton: { minHeight: 44, alignSelf: 'center', justifyContent: 'center', paddingHorizontal: 16 },
  overlay: { flex: 1, backgroundColor: '#172C2666', alignItems: 'center', justifyContent: 'center', padding: 24 },
  sheet: { width: '100%', maxWidth: 440, borderRadius: 24, padding: 24, backgroundColor: '#FAF8F3' },
  sheetTitle: { color: '#243D37', fontSize: 22, fontWeight: '600' },
  sheetCopy: { color: '#66726B', fontSize: 14, lineHeight: 21, marginTop: 10, marginBottom: 24 },
  inputLabel: { color: '#66726B', fontSize: 10, letterSpacing: 1, marginBottom: 9 },
  input: { color: '#243D37', borderColor: '#D3D9CE', borderWidth: 1, borderRadius: 12, padding: 14, fontSize: 14, marginBottom: 12 },
  sheetActions: { flexDirection: 'row', justifyContent: 'flex-end', gap: 12, marginTop: 10 },
  cancel: { minHeight: 44, paddingHorizontal: 14, justifyContent: 'center' },
  cancelText: { fontSize: 14, color: '#66726B' },
  connectButton: { minHeight: 44, paddingHorizontal: 22, justifyContent: 'center', backgroundColor: '#25685F', borderRadius: 12 },
  connectButtonText: { color: '#FFFFFF', fontSize: 14, fontWeight: '600' },
  pressed: { opacity: 0.65 },
});
