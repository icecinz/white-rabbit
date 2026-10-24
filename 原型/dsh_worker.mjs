/** One private, non-persistent DSH turn through public SDK services only. */
import { createRequire } from 'node:module';
import { constants, openSync, fstatSync, lstatSync, readSync, closeSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { randomUUID, createHash } from 'node:crypto';

const SYSTEM = '按本轮提供的共享上下文回答；仅返回文字。';
const MAX_CONFIG = 512 * 1024, MAX_PROMPT = 1024 * 1024, MAX_TEXT = 2 * 1024 * 1024;
const MAX_WIRE = 8 * 1024 * 1024, ERROR_RESERVE = 512;
const MODEL = /^[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,159}(?:\[1m\])?$/;
const REF = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
export class BridgeError extends Error { constructor(code) { super(code); this.code = code; } }
const fail = code => { throw new BridgeError(code); };
const plain = x => x !== null && typeof x === 'object' && !Array.isArray(x);

export function jsonWriter(stream = process.stdout, limit = MAX_WIRE) {
  if (!Number.isInteger(limit) || limit < 1024 || limit > MAX_WIRE) fail('invalid_request');
  let written = 0;
  return event => {
    const line = JSON.stringify(event) + '\n', size = Buffer.byteLength(line);
    const budget = event.type === 'error' ? limit : limit - ERROR_RESERVE;
    if (written + size > budget) fail('invalid_response');
    written += size;
    stream.write(line);
  };
}

function document(path, { required = false, secret = false } = {}) {
  let fd;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const before = fstatSync(fd), named = lstatSync(path);
    if (!before.isFile() || before.size > MAX_CONFIG || named.isSymbolicLink() ||
        before.ino !== named.ino || before.dev !== named.dev || (secret && (before.mode & 0o077))) fail('invalid_configuration');
    const buffer = Buffer.alloc(MAX_CONFIG + 1);
    let length = 0;
    while (length < buffer.length) {
      const count = readSync(fd, buffer, length, buffer.length - length, null);
      if (count === 0) break;
      length += count;
    }
    const after = fstatSync(fd), current = lstatSync(path);
    const same = state => state.dev === before.dev && state.ino === before.ino &&
      state.size === before.size && state.mtimeMs === before.mtimeMs && state.ctimeMs === before.ctimeMs;
    if (!same(after) || !same(current) || current.isSymbolicLink()) fail('configuration_changed');
    if (length > MAX_CONFIG) fail('invalid_configuration');
    const bytes = buffer.subarray(0, length);
    return { text: new TextDecoder('utf-8', { fatal: true }).decode(bytes),
      stamp: [before.dev, before.ino, before.size, before.mtimeMs, before.ctimeMs] };
  } catch (error) {
    if (error?.code === 'ENOENT' && !required) return { text: '', stamp: null };
    if (error instanceof BridgeError) throw error;
    fail('invalid_configuration');
  } finally { if (fd !== undefined) closeSync(fd); }
}

export async function sdk(packageRoot) {
  const root = resolve(packageRoot), meta = document(join(root, 'package.json'), { required: true });
  let pkg;
  try { pkg = JSON.parse(meta.text); } catch { fail('sdk_unavailable'); }
  if (pkg.name !== '@deepseek-ai/dsh' || pkg.version !== '0.1.0-rc.6') fail('sdk_unsupported');
  const require = createRequire(join(root, 'package.json'));
  const moduleStamps = [];
  const load = name => {
    const path = require.resolve(name), state = lstatSync(path);
    if (!state.isFile() || state.isSymbolicLink()) fail('sdk_unavailable');
    moduleStamps.push([name, path, state.size, state.mtimeMs, state.ctimeMs]);
    return import(pathToFileURL(path).href);
  };
  try {
    const [cordis, llm, session, agent, prompt, tools, loop, deepseek, yaml] = await Promise.all([
      '@deepseek-ai/cordis', '@deepseek-ai/dsh-llm', '@deepseek-ai/dsh-session', '@deepseek-ai/dsh-agent',
      '@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools', '@deepseek-ai/dsh-agent-loop',
      '@deepseek-ai/dsh-llm-deepseek', 'yaml'].map(load));
    return { cordis, llm, session, agent, prompt, tools, loop, deepseek, yaml, stamp: [root, pkg.version, meta.stamp, moduleStamps] };
  } catch { fail('sdk_unavailable'); }
}

function parse(yaml, text) {
  try {
    const doc = yaml.parseDocument(text, { prettyErrors: false, uniqueKeys: true });
    if (doc.errors.length) fail('invalid_configuration');
    const value = doc.toJS({ maxAliasCount: 0 }) ?? {};
    if (!plain(value)) fail('invalid_configuration');
    return value;
  } catch { fail('invalid_configuration'); }
}

export function connection(library, home) {
  const settingsFile = document(join(home, 'settings.yaml'));
  const credentialsFile = document(join(home, '.credentials.yaml'), { secret: true });
  const settings = parse(library.yaml, settingsFile.text), credentials = parse(library.yaml, credentialsFile.text);
  if (Object.keys(credentials).length > 256 || Object.entries(credentials).some(([k,v]) =>
      !REF.test(k) || typeof v !== 'string' || !v || v.length > 65536)) fail('invalid_configuration');
  const raw = settings['llm-deepseek'] ?? {}, selected = settings['agent-default-model'] ?? {};
  if (!plain(raw) || !plain(selected) || Object.keys(raw).some(k => ![
      'apiKeyEnv','baseURL','thinking','reasoningEffort','maxTokens','defaultContextWindow','models',
      'streamIdleTimeoutMs','retryPolicy'].includes(k)) || Object.keys(selected).some(k =>
      !['provider','model','reasoningEffort'].includes(k))) fail('unsupported_configuration');
  const provider = selected.provider ?? 'deepseek-official', model = selected.model ?? 'deepseek-v4-flash';
  if (provider !== 'deepseek-official' || typeof model !== 'string' || !MODEL.test(model)) fail('unsupported_configuration');
  if (raw.apiKeyEnv !== undefined && (typeof raw.apiKeyEnv !== 'string' || !REF.test(raw.apiKeyEnv))) fail('invalid_configuration');
  if (raw.thinking !== undefined && !['enabled','disabled'].includes(raw.thinking)) fail('invalid_configuration');
  if ([raw.reasoningEffort, selected.reasoningEffort].some(x => x !== undefined && !['off','high','max'].includes(x))) fail('invalid_configuration');
  let options;
  try { options = library.deepseek.resolveAdapterOptions(raw); } catch { fail('invalid_configuration'); }
  if (typeof options.baseURL !== 'string' || options.baseURL.length > 2048 || /[\r\n\0]/.test(options.baseURL)) fail('invalid_configuration');
  let url;
  try { url = new URL(options.baseURL); } catch { fail('invalid_configuration'); }
  if (!['https:','http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash ||
      (url.protocol === 'http:' && !['localhost','127.0.0.1','[::1]'].includes(url.hostname))) fail('invalid_configuration');
  const effort = selected.reasoningEffort ?? (options.defaults.thinking === 'disabled' ? 'off' : options.defaults.reasoningEffort ?? 'high');
  if (options.defaults.thinking === 'disabled' && effort !== 'off') fail('invalid_configuration');
  if (options.models.length > 100 || options.models.some(x => !MODEL.test(x.id) ||
      (x.name !== undefined && (typeof x.name !== 'string' || x.name.length > 200)))) fail('invalid_configuration');
  const value = Object.hasOwn(credentials, options.apiKeyEnv) ? credentials[options.apiKeyEnv] : undefined;
  if (value !== undefined) { try { library.llm.assertUsableApiKey(value, 'rabbit-dsh', options.apiKeyEnv); } catch { fail('invalid_configuration'); } }
  const fingerprint = createHash('sha256').update(JSON.stringify([library.stamp, settingsFile.stamp, credentialsFile.stamp,
      provider, model, options.baseURL, effort])).digest('hex');
  const models = [...new Set([model,...options.models.map(x => x.id)])];
  return { options, credential: value, selection: { provider, model, reasoningEffort: effort }, public: {
    available: Boolean(value), provider, default_model: model, base_url: options.baseURL,
    configured_provider: provider, reasoning_effort: effort, route_fingerprint: fingerprint,
    models, source: 'DSH 原有 settings.yaml 与托管凭据；未配置项沿用官方默认',
    model_source: 'requested_route' } };
}

export async function createHarness(library, config, adapterOverride) {
  const { Context } = library.cordis, ctx = new Context();
  let handle;
  try {
    for (const [plugin, options] of [[library.prompt.default, { includeHarnessIdentity: false, includeRuntimeContext: false }],
      [library.llm.default, {}], [library.session.default, {}], [library.agent.default, {}],
      [library.tools.default, { mode: 'native' }], [library.loop.default, { agents: [] }]]) await ctx.plugin(plugin, options);
    class TextAdapter extends library.deepseek.DeepSeekAdapter {
      async *stream(request) {
        if (request.tools?.length || request.system !== SYSTEM || request.messages.length !== 1 ||
            request.messages[0].role !== 'user' || request.messages[0].content.some(x => x.type !== 'text')) fail('isolation_rejected');
        let streamedBytes = 0;
        for await (const chunk of super.stream(request)) {
          if (chunk.type === 'text-delta' || chunk.type === 'reasoning-delta') {
            if (typeof chunk.text !== 'string' || (streamedBytes += Buffer.byteLength(chunk.text)) > MAX_TEXT) fail('invalid_response');
          }
          if (chunk.type === 'tool-call-delta' || (chunk.type === 'block-start' && chunk.blockType === 'tool-call') ||
              (chunk.type === 'block-end' && chunk.block?.type === 'tool-call') ||
              (chunk.type === 'finish' && chunk.reason?.kind === 'tool-calls')) fail('isolation_rejected');
          yield chunk;
        }
      }
    }
    const adapter = adapterOverride ?? new TextAdapter({ options: () => config.options,
      resolveApiKey: async () => config.credential, resolveUserId: () => randomUUID() });
    ctx.llm.registerAdapter(['deepseek-official'], adapter);
    ctx.systemPrompt.section({ name: 'rabbit:system', order: 0, text: SYSTEM, complete: true });
    handle = await ctx.agents.create({ sessionId: library.session.SessionId(`rabbit-${randomUUID()}`),
      agentOptions: { provider: config.selection.provider, model: config.selection.model },
      setup: scope => { library.agent.installModelSelection(scope, { current: config.selection }); } });
    await handle.agent.whenIdle();
    const assembly = await ctx.systemPrompt.assemble(library.agent.assembleContextFor(handle.agent));
    const omitted = ['sessionPersistence','skills','subagents','settings','credentials','commands','jobs','agentInstructions','sessionTitle'];
    if (assembly.tools.length || library.prompt.renderPrompt(assembly) !== SYSTEM ||
        omitted.some(name => ctx.get(name) !== undefined)) fail('isolation_rejected');
    return { ctx, handle, omitted, async dispose() { await handle.dispose(); await ctx.fiber.dispose(); } };
  } catch (error) { try { await handle?.dispose(); } finally { await ctx.fiber.dispose(); } throw error; }
}

export async function execute(library, config, payload, emit, adapterOverride) {
  if (typeof payload.prompt !== 'string' || !payload.prompt.trim() || Buffer.byteLength(payload.prompt) > MAX_PROMPT ||
      typeof payload.model !== 'string' || !config.public.models.includes(payload.model) ||
      payload.fingerprint !== config.public.route_fingerprint) fail('configuration_changed');
  config = { ...config, selection: { ...config.selection, model: payload.model } };
  const harness = await createHarness(library, config, adapterOverride);
  let text = '', terminal, ended = false, steps = 0, observedError;
  const cancel = () => harness.handle.agent.cancel({ kind: 'user' });
  const publish = event => {
    try { emit(event); } catch (error) {
      observedError = error instanceof BridgeError ? error.code : 'bridge_failed';
      cancel();
    }
  };
  const listener = harness.ctx.on('session/event', (session, event) => {
    if (session.id !== harness.handle.agent.session.id) return;
    if (event.type === 'step/start' && ++steps > 1) { observedError = 'isolation_rejected'; cancel(); }
    if (event.type === 'assistant/chunk') {
      const chunk = event.data.chunk;
      if (chunk.type === 'text-delta') {
        if (typeof chunk.text !== 'string' || Buffer.byteLength(text += chunk.text) > MAX_TEXT) { observedError = 'invalid_response'; cancel(); return; }
        publish({ type: 'text_delta', text: chunk.text });
      }
      if (chunk.type === 'finish') terminal = chunk.reason?.kind;
    }
    if (event.type === 'turn/end') ended = event.data.reason?.kind;
  });
  process.once('SIGTERM', cancel); process.once('SIGINT', cancel);
  try {
    publish({ type: 'ready', tools: [], native_persistence: false, extra_context: false });
    if (observedError) fail(observedError);
    harness.handle.agent.followup(library.llm.createUserMessage({ content: [{ type: 'text', text: payload.prompt }], source: { kind: 'user' } }));
    await harness.handle.agent.whenIdle();
    if (observedError) fail(observedError);
    const events = harness.handle.agent.session.events;
    const answers = events.filter(x => x.type === 'assistant/message').flatMap(x => x.data.message.content);
    if (ended !== 'completed' || terminal !== 'stop' || steps !== 1 || answers.some(x => !['text','reasoning'].includes(x.type))) fail(ended === 'aborted' ? 'cancelled' : 'inference_failed');
    const full = answers.filter(x => x.type === 'text').map(x => x.text).join('');
    if (!full.trim() || full !== text || Buffer.byteLength(full) > MAX_TEXT) fail('invalid_response');
    return { type: 'result', text: full, provider: config.selection.provider,
      model: payload.model, model_source: 'requested_route', finish: 'completed' };
  } finally { process.removeListener('SIGTERM', cancel); process.removeListener('SIGINT', cancel); listener(); await harness.dispose(); }
}

async function main() {
  const [packageRoot, home, mode] = process.argv.slice(2), emit = jsonWriter();
  try {
    const library = await sdk(packageRoot), config = connection(library, home);
    if (mode === 'inspect') { emit({ type: 'inspection', ...config.public }); return; }
    if (mode !== 'run' || !config.credential) fail('connection_unavailable');
    let input = Buffer.alloc(0);
    for await (const chunk of process.stdin) { if (input.length + chunk.length > MAX_PROMPT + 4096) fail('invalid_request'); input = Buffer.concat([input, chunk]); }
    let payload; try { payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(input)); } catch { fail('invalid_request'); }
    emit(await execute(library, config, payload, emit));
  } catch (error) { emit({ type: 'error', code: error instanceof BridgeError ? error.code : 'bridge_failed' }); process.exitCode = 1; }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
