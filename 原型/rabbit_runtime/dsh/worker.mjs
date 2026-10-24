// Original DSH web profile + official patch plugin. No replacement Agent loop.
// Direct execution is the bounded JSONL parent; native CLI imports the default plugin.
import { fork, spawnSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { accessSync, constants, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { isDeepStrictEqual } from 'node:util'

const SELF = fileURLToPath(import.meta.url)
const MAX_FRAME = 2 * 1024 * 1024, MAX_BUFFER = 16 * 1024 * 1024
const MAX_HISTORY = 1024 * 1024, MAX_HISTORY_EVENTS = 2000
const METHODS = new Set(['host.describe', 'agentPreset.list', 'session.create', 'session.resume',
  'session.info', 'session.inspect', 'session.flush', 'session.history', 'session.models',
  'session.prompt', 'session.cancel', 'respond'])
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k)
const SAFE_ERRORS = new Set(['invalid_native_header', 'session_identity_mismatch', 'session_not_live',
  'native_header_changed', 'native_snapshot_missing', 'invalid_native_revision', 'native_location_unavailable',
  'native_prefix_invalid', 'native_owner_changed', 'native_flush_incomplete', 'session_already_exists', 'native_location_changed',
  'native_preset_required', 'native_preset_changed', 'session_preset_not_frozen', 'prompt_already_attempted',
  'invalid_bridge_request', 'invalid_native_response', 'native_carrier_rejected', 'native_request_rejected',
  'invalid_native_history', 'native_history_capacity', 'native_history_event_capacity'])
const safeError = error => SAFE_ERRORS.has(error?.message) ? error.message : 'native_extension_failed'
function object(value) { return value !== null && typeof value === 'object' && !Array.isArray(value) }
function token(value) { return typeof value === 'string' && value.length > 0 && value.length <= 256 && !/[\s\x00-\x1f\x7f]/u.test(value) }
function exactKeys(value, allowed) { return object(value) && Object.keys(value).every(k => allowed.includes(k)) }
function encode(value) {
  const out = JSON.stringify(value, (_key, v) => {
    if (typeof v === 'number' && !Number.isFinite(v)) throw new Error('nonfinite_frame')
    return v
  })
  if (typeof out !== 'string' || Buffer.byteLength(out) > MAX_FRAME) throw new Error('frame_too_large')
  return out
}
function finiteText(value, max) {
  if (typeof value !== 'string' || value.length > max || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/u.test(value)) throw new Error('invalid_native_metadata')
  return value
}
function nativePath(value) {
  return typeof value === 'string' && value.length <= 8192 && isAbsolute(value) &&
    !value.split('/').includes('..') && !/[\x00-\x1f\x7f\uD800-\uDFFF]/u.test(value)
}
function header(meta) {
  if (!object(meta) || !token(meta.id) || !Number.isSafeInteger(meta.version) || !Number.isSafeInteger(meta.createdAt)) throw new Error('invalid_native_header')
  const out = { id: meta.id, version: meta.version, createdAt: meta.createdAt }
  for (const key of ['cwd', 'parentSession', 'agentPreset']) if (own(meta, key)) out[key] = finiteText(meta[key], key === 'cwd' ? 8192 : 256)
  for (const key of ['seedLength', 'delegationDepth']) if (own(meta, key)) {
    if (!Number.isSafeInteger(meta[key]) || meta[key] < 0) throw new Error('invalid_native_header')
    out[key] = meta[key]
  }
  if (own(meta, 'origin')) { if (meta.origin !== 'subagent') throw new Error('invalid_native_header'); out.origin = meta.origin }
  return out
}
function sameHeader(a, b) {
  // Original JSONL toHeaderLine persists a missing live depth as zero.
  return isDeepStrictEqual({ ...a, delegationDepth: a.delegationDepth ?? 0 },
    { ...b, delegationDepth: b.delegationDepth ?? 0 })
}
function publicHost(value) {
  if (!object(value)) throw new Error('invalid_native_host')
  const out = {}
  for (const key of ['version', 'cwd', 'provider', 'model']) if (own(value, key)) out[key] = finiteText(value[key], key === 'cwd' ? 8192 : 512)
  if (own(value, 'attachedSessions')) {
    if (!Number.isSafeInteger(value.attachedSessions) || value.attachedSessions < 0) throw new Error('invalid_native_host')
    out.attachedSessions = value.attachedSessions
  }
  if (own(value, 'canOpenPath')) { if (typeof value.canOpenPath !== 'boolean') throw new Error('invalid_native_host'); out.canOpenPath = value.canOpenPath }
  return out
}
function publicRoster(value) {
  if (!object(value) || !Array.isArray(value.presets) || value.presets.length > 128) throw new Error('invalid_native_roster')
  const ids = new Set()
  const presets = value.presets.map(row => {
    if (!object(row) || !token(row.id) || ids.has(row.id) || !['system', 'user'].includes(row.trust) || typeof row.isDefault !== 'boolean') throw new Error('invalid_native_roster')
    ids.add(row.id)
    const out = { id: row.id, trust: row.trust, isDefault: row.isDefault }
    for (const key of ['name', 'description', 'broken']) if (own(row, key)) out[key] = finiteText(row[key], 4096)
    return out
  })
  const defaults = presets.filter(p => p.isDefault)
  if (presets.length ? defaults.length !== 1 : defaults.length !== 0) throw new Error('ambiguous_native_default')
  return { presets, default_agent_preset: defaults[0]?.id ?? null }
}
/** Presentation only. Native logs, final messages and original seq numbers stay intact. */
export function projectHistory(value, { measure = value => Buffer.byteLength(JSON.stringify(value)), maxBytes = MAX_HISTORY } = {}) {
  if (!object(value) || !Array.isArray(value.events) || typeof value.hasMore !== 'boolean' ||
      typeof measure !== 'function' || !Number.isSafeInteger(maxBytes) || maxBytes <= 0) throw new Error('invalid_native_history')
  const source = value.events
  for (let i = 0; i < source.length; i++) {
    const seq = source[i]?.event?.seq
    if (!Number.isSafeInteger(seq) || seq < 0 || i > 0 && seq <= source[i - 1].event.seq) throw new Error('invalid_native_history')
  }
  const events = source.filter(row => row.event.type !== 'assistant/chunk')
  const inherited = value.omitted_stream_chunks ?? 0, prior = value.omitted_history_events ?? 0
  if (![inherited, prior].every(n => Number.isSafeInteger(n) && n >= 0) ||
      !Number.isSafeInteger(inherited + source.length - events.length)) throw new Error('invalid_native_history')
  const window = value.source_window ?? { first_seq: source[0]?.event.seq ?? null,
    last_seq: source.at(-1)?.event.seq ?? null, event_count: source.length }
  if (!exactKeys(window, ['first_seq', 'last_seq', 'event_count']) || Object.keys(window).length !== 3 ||
      !Number.isSafeInteger(window.event_count) || window.event_count < source.length ||
      (window.event_count === 0 ? window.first_seq !== null || window.last_seq !== null :
        !Number.isSafeInteger(window.first_seq) || !Number.isSafeInteger(window.last_seq) || window.first_seq < 0 || window.last_seq < window.first_seq) ||
      source.length && (window.first_seq > source[0].event.seq || window.last_seq < source.at(-1).event.seq)) throw new Error('invalid_native_history')
  function result(rows, dropped) {
    return { ...value, events: rows, hasMore: value.hasMore || dropped > 0 || prior > 0,
      omitted_stream_chunks: inherited + source.length - events.length, source_window: { ...window },
      ...(prior || dropped ? { omitted_history_events: prior + dropped } : {}) }
  }
  if (measure(result([], events.length)) > maxBytes) throw new Error('native_history_capacity')
  for (const row of events) if (measure(result([row], Math.max(0, events.length - 1))) > maxBytes) throw new Error('native_history_event_capacity')
  let low = 0, high = Math.min(events.length, MAX_HISTORY_EVENTS)
  while (low < high) {
    const count = Math.floor((low + high + 1) / 2)
    if (measure(result(events.slice(-count), events.length - count)) <= maxBytes) low = count
    else high = count - 1
  }
  return result(low ? events.slice(-low) : [], events.length - low)
}

/** Testable narrow extension over injected ORIGINAL services and fetch carrier. */
export function createBridge(ctx, config, { toFetchHandler, resolveSessionPreset, send }) {
  if (!exactKeys(config, ['packageRoot', 'cwd', 'sessionId', 'agentPreset', 'expectedPath']) || !isAbsolute(config.cwd ?? '') ||
      !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/u.test(config.sessionId ?? '') ||
      own(config, 'agentPreset') && config.agentPreset !== null && !token(config.agentPreset) ||
      own(config, 'expectedPath') && !nativePath(config.expectedPath)) throw new Error('invalid_plugin_scope')
  const fixedId = config.sessionId, fixedCwd = config.cwd
  let frozen = own(config, 'agentPreset'), preset = config.agentPreset ?? null, disposed = false
  const submitted = new Set(), pendingInteractions = new Map(), abort = new AbortController(), tasks = [], disposers = []
  const handler = toFetchHandler(ctx.apiProxy)
  function emit(stream, rpcId, payload) {
    if (disposed) return
    if (stream === 'mux' && payload?.sessionId === fixedId) {
      if (['approval/requested', 'question/requested'].includes(payload.type)) {
        if (!token(rpcId) || pendingInteractions.size >= 1024 && !pendingInteractions.has(rpcId)) throw new Error('native_interaction_budget')
        pendingInteractions.set(rpcId, { type: payload.type, approvalId: payload.approvalId })
      } else if (payload.type === 'question/resolved') pendingInteractions.delete(payload.questionRpcId)
      else if (payload.type === 'approval/resolved') for (const [key, value] of pendingInteractions) {
        if (value.type === 'approval/requested' && value.approvalId === payload.approvalId) pendingInteractions.delete(key)
      }
    }
    const event = { stream, rpcId, payload }; encode(event); send(event)
  }
  function identity(meta, events) {
    const h = header(meta)
    if (h.id !== fixedId || h.cwd !== fixedCwd || !frozen || (resolveSessionPreset({ header: meta, events }) ?? null) !== preset) throw new Error('session_identity_mismatch')
    return h
  }
  function scope(p, full = false) {
    if (!object(p) || p.sessionId !== fixedId) throw new Error('session_identity_mismatch')
    if (full && (!exactKeys(p, ['sessionId', 'cwd', 'agentPreset']) || p.cwd !== fixedCwd || !own(p, 'agentPreset') ||
        p.agentPreset !== null && !token(p.agentPreset))) throw new Error('session_identity_mismatch')
    if (full && frozen && p.agentPreset !== preset) throw new Error('session_identity_mismatch')
  }
  function live() {
    const agent = ctx.agents.get(fixedId), session = ctx.sessions.get(fixedId)
    if (!agent || !session || agent.session !== session) throw new Error('session_not_live')
    identity(session.header, session.events)
    return { agent, session }
  }
  async function native(method, payload, rpcId, signal) {
    const response = await handler.fetch(new Request(`http://127.0.0.1/api/${method}`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, signal,
      body: encode({ type: 'client-request', rpcId, method, payload })
    }))
    if (response.status !== 200) throw new Error('native_carrier_rejected')
    const value = await response.json()
    if (!object(value) || value.type !== 'server-response' || value.rpcId !== rpcId || !object(value.result) || typeof value.result.ok !== 'boolean') throw new Error('invalid_native_response')
    if (method === 'session.history' && value.result.ok === true) {
      // Account for every actual carrier envelope, not just the events array.
      const measure = history => {
        const result = { ...value.result, value: history }, reply = { rpcId, result }
        return Math.max(...[
          { ...value, result },
          { kind: 'response', id: Number.MAX_SAFE_INTEGER, ok: true, value: reply },
          { id: Number.MAX_SAFE_INTEGER, result: { ok: true, value: reply, event_seq: Number.MAX_SAFE_INTEGER } }
        ].map(frame => Buffer.byteLength(JSON.stringify(frame))))
      }
      value.result = { ...value.result, value: projectHistory(value.result.value, { measure }) }
    }
    encode(value)
    return { rpcId, result: value.result }
  }
  function success(response) {
    if (response.result.ok !== true) throw new Error('native_request_rejected')
    return response.result.value
  }
  async function persisted() {
    const inspection = await ctx.sessionPersistence.inspect(fixedId, abort.signal)
    const h = identity(inspection.meta, inspection.events)
    const physical = await ctx.sessionPersistence.readFrom(fixedId, 0, abort.signal)
    identity(physical.meta, physical.events)
    const physicalHeader = header(physical.meta)
    if (!sameHeader(physicalHeader, h)) throw new Error('native_header_changed')
    const snapshots = await ctx.sessionPersistence.listSnapshots(abort.signal)
    const rows = snapshots.filter(row => row.header.id === fixedId)
    if (rows.length !== 1) throw new Error('native_snapshot_missing')
    identity(rows[0].header, inspection.events)
    if (typeof rows[0].revision !== 'string' || rows[0].revision.length > 16384) throw new Error('invalid_native_revision')
    const location = ctx.sessionPersistence.locate(physical.meta)
    if (!location || !token(location.kind) || !isAbsolute(location.path ?? '')) throw new Error('native_location_unavailable')
    const events = physical.events
    if (!Array.isArray(events) || events.some((event, i) => event.seq !== i)) throw new Error('native_prefix_invalid')
    return { session_id: fixedId, cwd: fixedCwd, agent_preset: preset, header: physicalHeader,
      durable_through_seq: events.length - 1, materialized: true,
      location: { kind: location.kind, path: finiteText(location.path, 8192) },
      revision: createHash('sha256').update(rows[0].revision).digest('hex') }
  }
  async function ready() {
    const host = publicHost(success(await native('host.describe', {}, randomUUID(), abort.signal)))
    const roster = publicRoster(success(await native('agentPreset.list', {}, randomUUID(), abort.signal)))
    return { schema_version: 1, ready: true, host, ...roster }
  }
  async function request(method, p, rpcId = randomUUID(), signal = abort.signal) {
    if (disposed || !METHODS.has(method) || !token(rpcId) || !object(p)) throw new Error('invalid_bridge_request')
    if (['host.describe', 'agentPreset.list'].includes(method)) {
      if (Object.keys(p).length) throw new Error('invalid_bridge_request')
      const response = await native(method, p, rpcId, signal)
      if (response.result.ok) response.result.value = method === 'host.describe' ? publicHost(response.result.value) : publicRoster(response.result.value)
      return response
    }
    if (method === 'respond') {
      const pending = pendingInteractions.get(rpcId)
      if (!pending || !exactKeys(p, ['type', 'rpcId', 'result']) || p.type !== 'client-response' || p.rpcId !== rpcId || typeof p.result?.ok !== 'boolean') throw new Error('invalid_native_response')
      if (p.result.ok) {
        if (p.result.value?.sessionId !== fixedId) throw new Error('session_identity_mismatch')
        if (pending.type === 'approval/requested' && (p.result.value.approvalId !== pending.approvalId ||
          !['allowed-once', 'rejected'].includes(p.result.value.outcome))) throw new Error('invalid_native_response')
      } else if (pending.type !== 'question/requested' || !exactKeys(p.result.error, ['code', 'message', 'details']) ||
        p.result.error.code !== 'cancelled' || typeof p.result.error.message !== 'string' || p.result.error.message.length > 512 ||
        !object(p.result.error.details) || Object.keys(p.result.error.details).length) throw new Error('invalid_native_response')
      live()
      const response = await handler.fetch(new Request('http://127.0.0.1/api/respond', { method: 'POST',
        headers: { 'content-type': 'application/json' }, signal, body: encode(p) }))
      if (response.status !== 200) throw new Error('native_carrier_rejected')
      const receipt = await response.json(); encode(receipt)
      if (receipt.accepted === true) pendingInteractions.delete(rpcId)
      return { rpcId, result: { ok: true, value: receipt } }
    }
    scope(p, ['session.create', 'session.resume', 'session.info', 'session.inspect', 'session.flush'].includes(method))
    if (method === 'session.create' || method === 'session.resume') {
      if (!frozen) { frozen = true; preset = p.agentPreset }
      if (method === 'session.create') {
        if (own(config, 'expectedPath')) throw new Error('session_already_exists')
        const roster = await ready()
        if (preset === null && roster.default_agent_preset !== null) throw new Error('native_preset_required')
        // Native create can resume an old row. Explicit new MUST prove absent instead.
        const rows = await ctx.sessionPersistence.listSnapshots(signal)
        if (rows.some(row => row.header.id === fixedId) || ctx.sessions.get(fixedId) || ctx.agents.get(fixedId)) throw new Error('session_already_exists')
        const response = await native(method, { sessionId: fixedId, cwd: fixedCwd, ...(preset === null ? {} : { agentPreset: preset }) }, rpcId, signal)
        if (!response.result.ok) return response
        live(); return response
      }
      // The original model-list resolver is strict cold resume with original preset setup.
      const original = await persisted()
      if (own(config, 'expectedPath') && original.location.path !== config.expectedPath) throw new Error('native_location_changed')
      const roster = await ready()
      if (preset === null && roster.default_agent_preset !== null) throw new Error('native_preset_changed')
      const response = await native('session.models', { sessionId: fixedId }, rpcId, signal)
      if (!response.result.ok) return response
      const attached = live(), restored = await persisted()
      if (!sameHeader(header(attached.session.header), original.header) || !sameHeader(restored.header, original.header) ||
          !isDeepStrictEqual(restored.location, original.location)) throw new Error('native_header_changed')
      return { rpcId, result: { ok: true, value: { sessionId: fixedId, agentPreset: preset } } }
    }
    if (!frozen) throw new Error('session_preset_not_frozen')
    if (method === 'session.inspect') {
      const value = await persisted()
      return { rpcId, result: { ok: true, value: { ...value, through_seq: value.durable_through_seq, persisted: true } } }
    }
    const before = live()
    if (method === 'session.info') {
      let disk = null
      const rows = await ctx.sessionPersistence.listSnapshots(signal)
      if (rows.some(row => row.header.id === fixedId)) disk = await persisted()
      const after = live(); if (before.agent !== after.agent || before.session !== after.session) throw new Error('native_owner_changed')
      return { rpcId, result: { ok: true, value: { session_id: fixedId, cwd: fixedCwd, agent_preset: preset,
        live: true, running: before.agent.status !== 'idle', native_state: before.agent.status,
        persisted: disk !== null, materialized: disk !== null, header: header(before.session.header), through_seq: before.session.seq - 1,
        ...(disk ? { location: disk.location, revision: disk.revision, durable_through_seq: disk.durable_through_seq } : {}) } } }
    }
    if (method === 'session.flush') {
      const through = before.session.seq - 1
      await ctx.sessions.flush(before.session)
      const after = live(); if (before.agent !== after.agent || before.session !== after.session) throw new Error('native_owner_changed')
      const value = await persisted()
      if (value.durable_through_seq < through) throw new Error('native_flush_incomplete')
      return { rpcId, result: { ok: true, value: { ...value, through_seq: through, flushed: true, persisted: true } } }
    }
    if (method === 'session.prompt') {
      if (submitted.has(rpcId) || submitted.size >= 100000) throw new Error('prompt_already_attempted')
      submitted.add(rpcId) // An unknown native call is never silently submitted again.
    }
    const response = await native(method, p, rpcId, signal)
    const after = live(); if (before.agent !== after.agent || before.session !== after.session) throw new Error('native_owner_changed')
    return response
  }
  function lifecycle(type, payload) {
    const { agent, message, turn } = payload
    if (agent?.session?.id !== fixedId) return
    identity(agent.session.header, agent.session.events)
    if (!token(message?.id)) throw new Error('invalid_native_message')
    const value = { type, sessionId: fixedId, messageId: message.id,
      sourceRpcId: typeof message.source?.rpcId === 'string' ? message.source.rpcId : null }
    if (type === 'agent/inbox/inserted') {
      const event = [...agent.session.events].reverse().find(e => e.type === 'agent/inbox/spliced' && e.data.inserted?.some(m => m.id === message.id))
      if (!event) throw new Error('native_insert_evidence_missing')
      value.insertSeq = event.seq
    }
    if (type === 'agent/inbox/claimed') {
      const event = [...agent.session.events].reverse().find(e => e.type === 'turn/start' && e.data.turn === turn)
      if (!event || !Number.isSafeInteger(turn)) throw new Error('native_claim_evidence_missing')
      value.turn = turn; value.turnStartSeq = event.seq
    }
    emit('lifecycle', null, value)
  }
  function startStreams() {
    for (const stream of ['mux', 'host']) {
      const iterable = ctx.apiProxy.events[stream]({ rpcId: randomUUID(), payload: {} }, abort.signal)
      tasks.push((async () => { for await (const frame of iterable) emit(stream, frame.rpcId, frame.payload)
        if (!disposed) throw new Error('native_stream_ended') })())
    }
    for (const type of ['agent/inbox/inserted', 'agent/inbox/claimed', 'agent/inbox/discarded']) disposers.push(ctx.on(type, p => lifecycle(type, p)))
    return tasks
  }
  async function close() { disposed = true; abort.abort(); for (const dispose of disposers) dispose(); await Promise.allSettled(tasks) }
  return { ready, request, startStreams, close }
}

export default {
  name: 'rabbit-owned-native', inject: ['apiProxy', 'sessions', 'sessionPersistence', 'agents'],
  apply(ctx, config) {
    if (typeof process.send !== 'function') throw new Error('private_native_ipc_required')
    let bridge, pendingBytes = 0, ending = false
    function ipc(value) {
      if (ending || !process.connected) throw new Error('native_ipc_closed')
      const size = Buffer.byteLength(encode(value)); pendingBytes += size
      if (pendingBytes > MAX_BUFFER) throw new Error('native_ipc_budget')
      process.send(value, error => { pendingBytes -= size; if (error) void fail() })
    }
    async function fail() { if (ending) return; ending = true; try { await bridge?.close() } finally { process.kill(process.pid, 'SIGTERM') } }
    const onMessage = message => {
      if (!exactKeys(message, ['kind', 'id', 'method', 'params', 'rpcId', 'timeoutMs']) || message.kind !== 'request' || !Number.isSafeInteger(message.id) || !bridge) { void fail(); return }
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), message.timeoutMs)
      Promise.resolve().then(() => bridge.request(message.method, message.params, message.rpcId, controller.signal))
        .then(value => ipc({ kind: 'response', id: message.id, ok: true, value }),
          error => ipc({ kind: 'response', id: message.id, ok: false, resultUnknown: true, code: safeError(error) }))
        .catch(() => { void fail() }).finally(() => clearTimeout(timer))
    }
    process.on('message', onMessage); process.on('disconnect', fail)
    ctx.effect(() => () => { ending = true; process.off('message', onMessage); process.off('disconnect', fail); return bridge?.close() })
    // Return apply first: waiting for the original loader from inside apply would deadlock.
    setImmediate(async () => {
      try {
        await ctx.get('loader')?.await()
        const base = join(config.packageRoot, 'node_modules/@deepseek-ai')
        const [{ toFetchHandler }, { resolveSessionPreset }] = await Promise.all([
          import(pathToFileURL(join(base, 'dsh-host-apiproxy/lib/index.js'))),
          import(pathToFileURL(join(base, 'dsh-agent-presets/lib/index.js')))])
        bridge = createBridge(ctx, config, { toFetchHandler, resolveSessionPreset, send: event => ipc({ kind: 'event', event }) })
        for (const task of bridge.startStreams()) task.catch(() => { void fail() })
        ipc({ kind: 'ready', value: await bridge.ready() })
      } catch { try { ipc({ kind: 'failed' }) } catch {} void fail() }
    })
  }
}

async function parentMain() {
  const args = process.argv.slice(2), options = {}
  for (let i = 0; i < args.length; i += 2) {
    if (!['--cli', '--root', '--session-id', '--cwd', '--version', '--preset', '--expected-native-path'].includes(args[i]) || own(options, args[i]) || typeof args[i + 1] !== 'string') throw new Error('invalid_worker_arguments')
    options[args[i]] = args[i + 1]
  }
  if (![10, 12, 14].includes(args.length) || !['--cli', '--root', '--cwd'].every(k => isAbsolute(options[k] ?? '')) ||
      !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/u.test(options['--session-id'] ?? '') || !statSync(options['--cwd']).isDirectory() ||
      options['--preset'] !== undefined && !token(options['--preset']) ||
      options['--expected-native-path'] !== undefined && !nativePath(options['--expected-native-path'])) throw new Error('invalid_worker_arguments')
  const meta = JSON.parse(readFileSync(join(options['--root'], 'package.json'), 'utf8'))
  if (meta.name !== '@deepseek-ai/dsh' || meta.version !== options['--version'] || realpathSync(options['--cli']) !== realpathSync(join(options['--root'], 'lib/bin.js'))) throw new Error('native_installation_mismatch')
  accessSync(options['--cli'], constants.R_OK)
  function members() {
    const table = spawnSync('/bin/ps', ['-axo', 'pid=,pgid=,stat='], { encoding: 'utf8', timeout: 1000, maxBuffer: MAX_BUFFER })
    if (table.error || table.status !== 0) throw new Error('group_observation_failed')
    return table.stdout.trim().split('\n').map(line => line.trim().split(/\s+/u)).filter(row => Number(row[1]) === process.pid && Number(row[0]) !== table.pid && !row[2]?.startsWith('Z'))
  }
  if (!members().some(row => Number(row[0]) === process.pid)) throw new Error('owned_worker_group_required')
  const temp = mkdtempSync(join(tmpdir(), 'rabbit-dsh-carrier-')), patch = join(temp, 'native.patch.json')
  writeFileSync(patch, JSON.stringify([{ insert: [{ id: 'rabbit-owned-native', name: SELF,
    config: { packageRoot: options['--root'], cwd: options['--cwd'], sessionId: options['--session-id'],
      ...(options['--preset'] === undefined ? {} : { agentPreset: options['--preset'] }),
      ...(options['--expected-native-path'] === undefined ? {} : { expectedPath: options['--expected-native-path'] }) } }] }]), { mode: 0o600 })
  const child = fork(options['--cli'], ['--profile', 'web', '--patch', patch, '--host', '127.0.0.1', '--port', '0'], {
    execPath: process.execPath, execArgv: [], cwd: options['--cwd'], env: { ...process.env },
    detached: false, stdio: ['ignore', 'pipe', 'pipe', 'ipc'] })
  let sequence = 0, childClosed = false, closing, finished = false, readyInfo, input = '', active = 0, next = 0, ipcBytes = 0
  let resolveReady, rejectReady
  const readiness = new Promise((yes, no) => { resolveReady = yes; rejectReady = no }); readiness.catch(() => {})
  const pending = new Map(), counters = { stdout_bytes: 0, stderr_bytes: 0 }
  const timer = setTimeout(() => { rejectReady(new Error('native_ready_timeout')); void stop(true) }, 50000)
  function write(frame) {
    if (process.stdout.writableLength > MAX_BUFFER) throw new Error('output_budget_exceeded')
    process.stdout.write(encode(frame) + '\n')
  }
  child.on('message', message => {
    try {
      encode(message)
      if (message.kind === 'ready') {
        if (readyInfo) throw new Error('duplicate_native_ready')
        readyInfo = message.value; clearTimeout(timer); resolveReady()
      } else if (message.kind === 'event') {
        if (closing) return
        sequence++; write({ method: 'dsh/event', params: { event_seq: sequence, event: message.event } })
      } else if (message.kind === 'response') {
        const row = pending.get(message.id); if (!row) return
        pending.delete(message.id); clearTimeout(row.timer)
        message.ok ? row.resolve(message.value) : row.reject(new Error(SAFE_ERRORS.has(message.code) ? message.code : 'native_extension_failed'))
      } else throw new Error('native_bridge_failed')
    } catch { rejectReady(new Error('native_bridge_failed')); void stop(true) }
  })
  for (const [name, stream] of [['stdout_bytes', child.stdout], ['stderr_bytes', child.stderr]]) stream.on('data', bytes => { counters[name] = Math.min(Number.MAX_SAFE_INTEGER, counters[name] + bytes.length) })
  child.on('error', () => { rejectReady(new Error('native_start_failed')); void stop(true) })
  child.on('close', () => { childClosed = true; if (!closing) { rejectReady(new Error('native_exited')); void stop(true) } })
  function call(p) {
    return new Promise((yes, no) => {
      const id = ++next, message = { kind: 'request', id, method: p.method, params: p.params,
        rpcId: p.rpc_id ?? randomUUID(), timeoutMs: p.timeout_ms }
      const size = Buffer.byteLength(encode(message)); ipcBytes += size
      if (ipcBytes > MAX_BUFFER) { ipcBytes -= size; no(new Error('native_ipc_budget')); return }
      const row = { resolve: yes, reject: no, timer: setTimeout(() => { pending.delete(id); no(new Error('native_request_timeout')) }, p.timeout_ms) }
      pending.set(id, row)
      child.send(message, error => { ipcBytes -= size; if (error) { pending.delete(id); clearTimeout(row.timer); no(new Error('native_ipc_failed')) } })
    })
  }
  async function stop(exitWhenDone) {
    if (!closing) closing = Promise.resolve().then(async () => {
      clearTimeout(timer); rejectReady(new Error('worker_closing'))
      for (const row of pending.values()) { clearTimeout(row.timer); row.reject(new Error('worker_closing')) } pending.clear()
      try { process.kill(-process.pid, 'SIGTERM') } catch { return false }
      const until = Date.now() + 7000
      do {
        try { if (childClosed && members().every(row => Number(row[0]) === process.pid)) { rmSync(temp, { recursive: true, force: true }); return true } } catch { return false }
        await delay(40)
      } while (Date.now() < until)
      return false // Parent retains the owned PGID and performs physical cleanup.
    })
    const complete = await closing
    if (complete && exitWhenDone) finish()
    return complete
  }
  function finish() { if (finished) return; finished = true; process.stdin.pause(); process.stdin.destroy(); process.stdout.end(() => process.exit(0)) }
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => { if (!closing) void stop(true) })
  process.stdin.on('end', () => { void stop(true) }); process.stdin.on('error', () => { void stop(true) }); process.stdout.on('error', () => { void stop(true) })
  async function dispatch(frame) {
    if (!exactKeys(frame, ['id', 'method', 'params']) || !Number.isSafeInteger(frame.id)) throw new Error('invalid_bridge_frame')
    if (frame.method === 'bridge/close') {
      const closed = await stop(false)
      write({ id: frame.id, result: { ok: true, value: { closed, ...counters }, event_seq: sequence } }); if (closed) finish(); return
    }
    let result
    try {
      await readiness
      if (closing) throw new Error('worker_closing')
      let value
      if (frame.method === 'bridge/ready') value = readyInfo
      else if (frame.method === 'bridge/request') {
        const p = frame.params
        if (!exactKeys(p, ['method', 'params', 'rpc_id', 'timeout_ms']) || !METHODS.has(p.method) || !object(p.params) ||
            !Number.isSafeInteger(p.timeout_ms) || p.timeout_ms < 1 || p.timeout_ms > 3600000 ||
            p.rpc_id !== null && !token(p.rpc_id) || p.method === 'session.prompt' && p.rpc_id === null) throw new Error('invalid_bridge_request')
        value = await call(p)
      } else throw new Error('unsupported_bridge_method')
      result = { ok: true, value, event_seq: sequence }
    } catch (error) { result = { ok: false, code: safeError(error), result_unknown: frame.method === 'bridge/request', event_seq: sequence } }
    write({ id: frame.id, result })
  }
  process.stdin.setEncoding('utf8'); process.stdin.on('data', chunk => {
    input += chunk
    if (Buffer.byteLength(input) > MAX_BUFFER) { input = ''; void stop(true); return }
    let index
    while ((index = input.indexOf('\n')) >= 0) {
      const line = input.slice(0, index); input = input.slice(index + 1)
      if (Buffer.byteLength(line) > MAX_FRAME || active >= 64) { void stop(true); return }
      active++; Promise.resolve().then(() => dispatch(JSON.parse(line))).catch(() => { void stop(true) }).finally(() => active--)
    }
  })
}
if (process.argv[1] && resolve(process.argv[1]) === SELF) parentMain().catch(() => { process.exitCode = 1 })
