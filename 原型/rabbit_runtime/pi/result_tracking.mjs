/** Current-connection receipts around the original Pi prompt Promise. No execution loop. */
import { createHash, randomBytes } from 'node:crypto';

const MAX_ENTRIES = 128, MAX_ANSWER_BYTES = 128 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const sha = text => createHash('sha256').update(text, 'utf8').digest('hex');
const unavailable = (reason = 'current_result_unavailable', state = 'unavailable') =>
  ({ state, reason, answer: null, source: null });
const canonical = value => Array.isArray(value) ? value.map(canonical) :
  value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])])) : value;
const idle = session => !session.isStreaming && !session.isCompacting && session.pendingMessageCount === 0;

export function createTracker() {
  const epoch = randomBytes(16).toString('hex'), entries = new Map();
  let active = null;
  const capabilities = () => ({ protocol: 'rabbit-pi-result/v1', connection_epoch: epoch,
    source_kind: 'pi_prompt_completion', max_answer_bytes: MAX_ANSWER_BYTES, max_entries: MAX_ENTRIES });
  function onCommand(command) {
    if (active && !['get_state', 'get_messages', 'get_available_models', 'get_session_stats',
      'get_last_assistant_text', 'get_commands', 'extension_ui_response',
      'rabbit_task_capabilities', 'rabbit_task_read'].includes(command.type)) active.conflict = true;
  }
  function preflight(command, session, succeeded) {
    if (active?.command === command && active.session.deref() === session) active.preflight = succeeded === true;
  }
  function start(command, session, messages) {
    if (active?.command !== command || active.session.deref() !== session) return;
    try {
      if (active.armed || !active.preflight || !Array.isArray(messages)) throw new Error();
      active.users = new Set(messages.filter(message => message.role === 'user'));
      active.armed = active.users.size > 0;
    } catch { active.conflict = true; }
  }
  function observe(entry, event) {
    if (!entry.armed || !entry.preflight || entry.conflict) return;
    if (event.type === 'agent_start') {
      entry.started = true; entry.end = null; entry.assistant = null;
    } else if (entry.started && event.type === 'message_end') {
      if (event.message?.role === 'user' && !entry.user) {
        if (!entry.users.has(event.message)) { entry.conflict = true; return; }
        entry.user = event.message;
      }
      if (event.message?.role === 'assistant') entry.assistant = event.message;
    } else if (entry.started && event.type === 'agent_end') {
      entry.end = event;
      if (entry.user && event.messages?.includes(entry.user)) entry.bound = true;
    }
  }
  function prompt(command, session, invoke) {
    // Ordinary native calls keep the original Promise and allocate no receipt.
    if (command.rabbit_track_result !== true) return invoke();
    if (command.type !== 'prompt' || !UUID.test(command.id ?? '') || typeof command.message !== 'string'
      || !command.message.trim() || Object.keys(command).some(k => ![
        'type', 'id', 'message', 'images', 'rabbit_track_result'].includes(k))
      || active || !idle(session) || entries.has(command.id) || entries.size >= MAX_ENTRIES)
      return Promise.reject(new Error('Tracked Pi prompt is unavailable.'));
    const logical = { type: 'prompt', message: command.message };
    if (command.images !== undefined) logical.images = command.images;
    const entry = { command, session: new WeakRef(session), sid: session.sessionId,
      digest: sha(JSON.stringify(canonical(logical))), preflight: false, armed: false, users: null, started: false,
      bound: false, conflict: false, user: null, assistant: null, end: null,
      result: unavailable('not_completed') };
    entries.set(command.id, entry); active = entry;
    let unsubscribe;
    try { unsubscribe = session.subscribe(event => { try { observe(entry, event); } catch { entry.conflict = true; } }); }
    catch {
      active = null; entry.command = null; entry.result = unavailable();
      return Promise.reject(new Error('Pi observer unavailable.'));
    }
    return (async () => {
      try {
        const nativeValue = await invoke();
        try {
          const end = entry.end, message = entry.assistant;
          if (entry.conflict || !entry.preflight || !entry.bound || !idle(session)
            || session.sessionId !== entry.sid || end?.willRetry !== false || !message
            || !Array.isArray(end.messages) || end.messages.filter(m => m.role === 'assistant').at(-1) !== message)
            return nativeValue;
          if (message.stopReason === 'length') { entry.result = unavailable('native_length', 'incomplete'); return nativeValue; }
          if (message.stopReason !== 'stop' || !Array.isArray(message.content)
            || message.content.some(p => !p || !['text', 'thinking'].includes(p.type)
              || p.type === 'text' && typeof p.text !== 'string')) return nativeValue;
          const text = message.content.filter(p => p.type === 'text').map(p => p.text).join('');
          const size = Buffer.byteLength(text, 'utf8');
          if (size > MAX_ANSWER_BYTES) entry.result = unavailable('answer_capacity', 'incomplete');
          else if (text.trim()) entry.result = { state: 'available', reason: 'current_connection_result',
            answer: { text, sha256: sha(text), bytes: size }, source: { kind: 'pi_prompt_completion',
              submission_id: command.id, runtime_sid: entry.sid, connection_epoch: epoch,
              request_sha256: entry.digest, terminal_status: 'completed' } };
          return nativeValue;
        } catch {
          // Observation failure must not replace the native Promise outcome.
          entry.result = unavailable();
          return nativeValue;
        }
      } finally {
        try { unsubscribe(); } catch { entry.result = unavailable(); }
        entry.command = entry.users = entry.user = entry.assistant = entry.end = null;
        if (active === entry) active = null;
      }
    })();
  }
  function read(command, session) {
    if (command.type !== 'rabbit_task_read' || typeof command.id !== 'string' || !command.id || command.id.length > 256
      || Object.keys(command).some(k => !['type', 'id', 'session_id', 'submission_id'].includes(k))
      || !UUID.test(command.submission_id ?? '') || command.session_id !== session.sessionId) return unavailable();
    const entry = entries.get(command.submission_id);
    if (!entry || entry.session.deref() !== session || entry.sid !== session.sessionId || active === entry)
      return unavailable();
    return structuredClone(entry.result);
  }
  return Object.freeze({ capabilities, onCommand, preflight, start, prompt, read });
}
