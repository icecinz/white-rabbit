/** A narrow JSONL bridge around Pi 0.79.1's official in-memory SDK. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL, fileURLToPath } from 'node:url';

const MAX_CONFIG = 512 * 1024, MAX_INPUT = 1_016_000, MAX_TEXT = 2_000_000;
const MODEL = /^[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,159}(?:\[1m\])?$/;
const THINKING = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh']);
const own = (value, key) => Object.hasOwn(value, key);
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const bytes = value => Buffer.byteLength(value, 'utf8');
const stamp = stat => [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs].map(String);
class PublicError extends Error { constructor(code) { super(code); this.code = code; } }
const fail = code => { throw new PublicError(code); };

function readConfig(filename, optional = false) {
  let fd;
  try { fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK); }
  catch (error) { if (optional && error.code === 'ENOENT') return { value: null, stamp: null }; fail('configuration_invalid'); }
  try {
    const before = fs.fstatSync(fd, { bigint: true });
    if (!before.isFile() || before.size > BigInt(MAX_CONFIG)) fail('configuration_invalid');
    const buffer = Buffer.alloc(Number(before.size) + 1);
    let length = 0, count;
    do { count = fs.readSync(fd, buffer, length, buffer.length - length, null); length += count; }
    while (count && length < buffer.length);
    const after = fs.fstatSync(fd, { bigint: true }), current = fs.lstatSync(filename, { bigint: true });
    if (length > MAX_CONFIG || !current.isFile() || JSON.stringify(stamp(before)) !== JSON.stringify(stamp(after))
        || JSON.stringify(stamp(after)) !== JSON.stringify(stamp(current))) fail('configuration_invalid');
    const decoder = new TextDecoder('utf-8', { fatal: true });
    const value = JSON.parse(decoder.decode(buffer.subarray(0, length)));
    if (!object(value)) fail('configuration_invalid');
    return { value, stamp: stamp(after) };
  } catch (error) { if (error instanceof PublicError) throw error; fail('configuration_invalid'); }
  finally { fs.closeSync(fd); }
}

export async function connection(request, sdkOverride) {
  if (!object(request) || typeof request.agent_dir !== 'string' || typeof request.package !== 'string'
      || typeof request.binary !== 'string' || request.binary !== process.execPath) fail('configuration_invalid');
  const settings = readConfig(path.join(request.agent_dir, 'settings.json'));
  const auth = readConfig(path.join(request.agent_dir, 'auth.json'));
  const custom = readConfig(path.join(request.agent_dir, 'models.json'), true);
  if (custom.value !== null) fail('configuration_invalid');
  const pkg = readConfig(path.join(request.package, 'package.json'));
  if (pkg.value.name !== '@earendil-works/pi-coding-agent' || pkg.value.version !== '0.79.1') fail('configuration_invalid');
  const sdk = sdkOverride ?? await import(pathToFileURL(path.join(request.package, 'dist/index.js')).href);
  const provider = settings.value.defaultProvider, defaultModel = settings.value.defaultModel;
  const thinking = settings.value.defaultThinkingLevel;
  if (typeof provider !== 'string' || !/^[a-zA-Z0-9_-]{1,60}$/.test(provider)
      || !MODEL.test(defaultModel ?? '') || thinking !== undefined && !THINKING.has(thinking)) fail('configuration_invalid');
  const credential = own(auth.value, provider) ? auth.value[provider] : null;
  if (!object(credential) || credential.type !== 'api_key' || typeof credential.key !== 'string'
      || !credential.key.trim() || bytes(credential.key) > 65536 || /[\x00-\x20\x7f$]/.test(credential.key)
      || credential.key.startsWith('!') || /^[A-Z_][A-Z0-9_]*$/.test(credential.key)) fail('configuration_invalid');
  const authStorage = sdk.AuthStorage.inMemory();
  // Runtime keys bypass Pi's shell/environment template resolver altogether.
  authStorage.setRuntimeApiKey(provider, credential.key);
  const registry = sdk.ModelRegistry.inMemory(authStorage);
  const model = registry.find(provider, defaultModel);
  if (!model || model.provider !== provider || model.id !== defaultModel || !MODEL.test(model.id)) fail('configuration_invalid');
  const candidates = registry.getAll().filter(row => row.provider === provider && MODEL.test(row.id));
  const endpoints = Object.create(null);
  for (const candidate of candidates) {
    const endpoint = new URL(candidate.baseUrl);
    if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password
        || endpoint.search || endpoint.hash) fail('configuration_invalid');
    endpoints[candidate.id] = candidate.baseUrl;
  }
  const models = [defaultModel, ...candidates.map(row => row.id)];
  const unique = [...new Set(models)];
  if (unique.length > 200) fail('configuration_invalid');
  const publicRoute = { provider, configured_provider: provider, default_model: defaultModel,
    reasoning_effort: thinking ?? null, base_url: model.baseUrl, binary: request.binary,
    models: unique, model_endpoints: endpoints };
  publicRoute.route_fingerprint = crypto.createHash('sha256').update(JSON.stringify({ ...publicRoute,
    settings: settings.stamp, auth: auth.stamp, custom: custom.stamp, package: pkg.stamp,
    package_path: request.package, agent_dir: request.agent_dir })).digest('hex');
  return { sdk, authStorage, registry, publicRoute };
}

function validate(route, current) {
  if (!object(route) || route.backend !== 'pi' || !MODEL.test(route.model ?? '')
      || !current.models.includes(route.model) || route.model_id !== 'local/pi/' + route.model
      || route.effort_selection !== 'agent_default' || route.image_input !== 'unsupported'
      || route.base_url !== current.model_endpoints[route.model]
      || (route.reasoning_effort ?? null) !== current.reasoning_effort
      || ['provider', 'configured_provider', 'default_model', 'binary',
          'route_fingerprint'].some(key => route[key] !== current[key])) fail('configuration_changed');
}

export function emptyResources(sdk, systemPrompt) {
  const runtime = sdk.createExtensionRuntime();
  return Object.freeze({
    getExtensions: () => ({ extensions: [], errors: [], runtime }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => systemPrompt,
    getAppendSystemPrompt: () => [],
    extendResources: () => fail('isolation_rejected'),
    reload: async () => fail('isolation_rejected'),
  });
}

export async function execute(request, io, sdkOverride) {
  const connectionState = await connection(request, sdkOverride);
  const { sdk, authStorage, registry, publicRoute } = connectionState;
  if (request.operation === 'probe') { io.emit({ type: 'result', result: publicRoute }); return; }
  if (!['validate', 'run'].includes(request.operation)) fail('configuration_invalid');
  validate(request.route, publicRoute);
  if (request.operation === 'validate') { io.emit({ type: 'result', result: { valid: true } }); return; }
  if (typeof request.prompt !== 'string' || !request.prompt.trim() || bytes(request.prompt) > 1_000_000) fail('configuration_invalid');
  const model = registry.find(publicRoute.provider, request.route.model);
  // A different candidate can have a different builtin endpoint. The route
  // fingerprints the native connection, while this exact model is still SDK metadata.
  if (!model || model.provider !== publicRoute.provider || model.id !== request.route.model) fail('configuration_changed');
  const settingsManager = sdk.SettingsManager.inMemory({
    defaultProvider: publicRoute.provider, defaultModel: model.id,
    ...(publicRoute.reasoning_effort !== null ? { defaultThinkingLevel: publicRoute.reasoning_effort } : {}),
    packages: [], enableInstallTelemetry: false, blockImages: true,
    compaction: { enabled: false }, retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
  });
  const sessionManager = sdk.SessionManager.inMemory(process.cwd());
  const resourceLoader = emptyResources(sdk, '你是统一聊天入口中的 Pi 文字助手。仅依据本轮提供的共同对话上下文回答。');
  let session, unsubscribe, finalMessage = null, agentEnded = false, responseFailure = null;
  let lastText = '', lastPublished = '', lastEmit = 0;
  try {
    ({ session } = await sdk.createAgentSession({ cwd: process.cwd(), agentDir: path.join(process.cwd(), 'not-created'),
      authStorage, modelRegistry: registry, settingsManager, sessionManager, resourceLoader, model,
      ...(publicRoute.reasoning_effort !== null ? { thinkingLevel: publicRoute.reasoning_effort } : {}),
      noTools: 'all', tools: [], customTools: [] }));
    if (session.getActiveToolNames().length || sessionManager.isPersisted() || session.sessionFile != null
        || settingsManager.getRetrySettings().enabled || settingsManager.getProviderRetrySettings().maxRetries !== 0
        || resourceLoader.getExtensions().extensions.length) fail('isolation_rejected');
    io.setAbort(async () => { await session.abort(); });
    unsubscribe = session.subscribe(event => {
      try {
        if (!object(event) || typeof event.type !== 'string') fail('invalid_response');
        if (/^(?:tool_execution|auto_retry|auto_compact)/.test(event.type)) fail('isolation_rejected');
        if (event.type === 'message_update') {
          const part = event.assistantMessageEvent;
          if (part?.type?.startsWith('toolcall')) fail('isolation_rejected');
          if (part?.type === 'text_delta' && typeof part.delta === 'string') {
            lastText += part.delta;
            if (bytes(lastText) > MAX_TEXT) fail('invalid_response');
            const now = Date.now();
            if (now - lastEmit >= 100) {
              io.emit({ type: 'text', text: lastText }); lastPublished = lastText; lastEmit = now;
            }
          }
        } else if (event.type === 'message_end' && event.message?.role === 'assistant') {
          const message = event.message;
          if (finalMessage !== null || agentEnded || message.stopReason !== 'stop' || !Array.isArray(message.content)
              || message.provider !== publicRoute.provider || message.model !== request.route.model
              || message.content.some(part => !object(part) || !['text', 'thinking'].includes(part.type)
                  || part.type === 'text' && typeof part.text !== 'string')) fail('invalid_response');
          const text = message.content.filter(part => part.type === 'text').map(part => part.text).join('');
          if (!text.trim() || bytes(text) > MAX_TEXT || !text.startsWith(lastText)) fail('invalid_response');
          finalMessage = { text, responseModel: message.responseModel };
          if (text !== lastPublished) { io.emit({ type: 'text', text }); lastPublished = text; }
          lastText = text;
        } else if (event.type === 'agent_end') {
          const assistants = Array.isArray(event.messages) ? event.messages.filter(message => message.role === 'assistant') : [];
          if (agentEnded || event.willRetry !== false || !finalMessage || !Array.isArray(event.messages)
              || assistants.length !== 1 || assistants[0].stopReason !== 'stop'
              || assistants[0].provider !== publicRoute.provider || assistants[0].model !== request.route.model
              || !Array.isArray(assistants[0].content)
              || assistants[0].content.some(part => !object(part) || !['text', 'thinking'].includes(part.type))
              || assistants[0].content.filter(part => part.type === 'text').map(part => part.text).join('') !== finalMessage.text
              || event.messages.some(message => message.role === 'toolResult')) fail('invalid_response');
          agentEnded = true;
        } else if (event.type === 'turn_end' && (event.toolResults?.length || event.message?.stopReason === 'toolUse')) {
          fail('isolation_rejected');
        }
      } catch (error) {
        responseFailure ??= error instanceof PublicError ? error : new PublicError('invalid_response');
        void session.abort();
      }
    });
    if (io.cancelled()) { await session.abort(); fail('cancelled'); }
    await session.prompt(request.prompt, { expandPromptTemplates: false });
    if (io.cancelled()) fail('cancelled');
    if (responseFailure) throw responseFailure;
    if (!agentEnded || !finalMessage || session.getActiveToolNames().length || sessionManager.isPersisted()) fail('invalid_response');
    const reported = typeof finalMessage.responseModel === 'string' && MODEL.test(finalMessage.responseModel)
      ? finalMessage.responseModel : null;
    io.emit({ type: 'result', result: { text: finalMessage.text, provider: publicRoute.provider,
      model: reported ?? request.route.model, model_source: reported ? 'service_reported' : 'requested_route' } });
  } finally { unsubscribe?.(); session?.dispose(); }
}

async function main() {
  let buffer = Buffer.alloc(0), started = false, done = false, cancelled = false, abort = null, written = 0;
  const emit = event => {
    const data = JSON.stringify(event) + '\n';
    written += bytes(data);
    if (written > 8_000_000) fail('invalid_response');
    fs.writeSync(1, data);
  };
  const finish = () => { done = true; process.stdin.pause(); process.stdin.destroy(); };
  const io = { emit, cancelled: () => cancelled, setAbort: value => { abort = value; if (cancelled) void abort(); } };
  process.stdin.on('data', chunk => {
    try {
      if (done) return;
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > MAX_INPUT) fail('configuration_invalid');
      let index;
      while ((index = buffer.indexOf(10)) !== -1) {
        const line = buffer.subarray(0, index); buffer = buffer.subarray(index + 1);
        const request = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line));
        if (!object(request)) fail('configuration_invalid');
        if (started) {
          if (request.operation !== 'cancel' || Object.keys(request).length !== 1) fail('configuration_invalid');
          cancelled = true; if (abort) void abort();
        } else {
          started = true;
          void execute(request, io).catch(error => {
            emit({ type: 'error', code: error instanceof PublicError ? error.code : 'process_failed' });
            process.exitCode = 1;
          }).finally(finish);
        }
      }
    } catch { emit({ type: 'error', code: 'configuration_invalid' }); process.exitCode = 1; finish(); }
  });
  process.stdin.on('end', () => {
    if (!started || buffer.length) { emit({ type: 'error', code: 'configuration_invalid' }); process.exitCode = 1; finish(); }
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  // Imported libraries may log internal diagnostics. Only emit() owns stdout.
  console.log = console.warn = console.error = () => {};
  await main();
}
