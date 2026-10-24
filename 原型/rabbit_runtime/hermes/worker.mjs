// Private JSONL envelope around the unchanged official Hermes WS client.
// Native startup retains config and may run background services; it is not read-only.
import { spawn, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { accessSync, constants, statSync } from 'node:fs'
import { delimiter, isAbsolute, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { JsonRpcGatewayClient, JsonRpcGatewayError } from './vendor/hermes_gateway/json-rpc-gateway.mjs'

const MAX_FRAME = 8 * 1024 * 1024, MAX_BUFFER = 16 * 1024 * 1024
const args = process.argv.slice(2), options = {}
for (let i = 0; i < args.length; i += 2) {
  if (!['--python', '--root', '--pin-default-profile', '--bootstrap', '--native-commit'].includes(args[i]) || options[args[i]] ||
      (args[i] === '--pin-default-profile' ? args[i + 1] !== '1' :
        args[i] === '--native-commit' ? !/^[0-9a-f]{40}$/.test(args[i + 1] ?? '') : !isAbsolute(args[i + 1] ?? ''))) throw new Error('invalid_worker_arguments')
  options[args[i]] = args[i + 1]
}
if (![4, 6, 8, 10].includes(args.length) || !options['--python'] || !options['--root'] ||
    Boolean(options['--bootstrap']) !== Boolean(options['--native-commit']) ||
    !statSync(options['--root']).isDirectory()) throw new Error('invalid_worker_arguments')
accessSync(options['--python'], constants.X_OK)
if (options['--bootstrap']) accessSync(options['--bootstrap'], constants.R_OK)
if (!['darwin', 'linux'].includes(process.platform) || typeof WebSocket !== 'function') throw new Error('unsupported_worker_platform')
function groupMembers() {
  const table = spawnSync('/bin/ps', ['-axo', 'pid=,pgid=,stat='], { encoding: 'utf8', timeout: 1000, maxBuffer: 4 * 1024 * 1024 })
  if (table.error || table.status !== 0) throw new Error('group_observation_failed')
  return table.stdout.trim().split('\n').map(line => line.trim().split(/\s+/))
    .filter(row => Number(row[1]) === process.pid && Number(row[0]) !== table.pid && !row[2]?.startsWith('Z'))
}
const groupOwned = groupMembers().some(row => Number(row[0]) === process.pid)
if (!groupOwned) throw new Error('owned_worker_group_required')
let sequence = 0, ready = false, closing = null, closed = false, backendClosed = false, finishing = false
let stderrBytes = 0, stdoutBytes = 0, stdoutBuffer = '', inputBuffer = '', active = 0
const token = randomBytes(32).toString('hex'), env = { ...process.env }
for (const key of Object.keys(env)) if (/^HERMES_(DESKTOP(?:_.*)?|WEB_DIST|SERVE_HEADLESS|SSH_.*|SUPERVISED_CHILD|S6_SUPERVISED_CHILD|GATEWAY_EXTERNAL_SUPERVISOR|DASHBOARD_SESSION_TOKEN)$/.test(key)) delete env[key]
env.HERMES_DASHBOARD_SESSION_TOKEN = token
env.PYTHONPATH = [options['--root'], env.PYTHONPATH].filter(Boolean).join(delimiter)
const nativeEntry = options['--bootstrap']
  ? ['-B', options['--bootstrap'], '--native-root', options['--root'], '--expected-commit', options['--native-commit'], '--']
  : ['-B', '-m', 'hermes_cli.main']
const backend = spawn(options['--python'], [...nativeEntry, ...(options['--pin-default-profile'] ? ['-p', 'default'] : []), 'serve', '--host', '127.0.0.1', '--port', '0', '--isolated'],
  { cwd: process.cwd(), env, detached: false, stdio: ['ignore', 'pipe', 'pipe'] })
function encode(frame) {
  const text = JSON.stringify(frame, (_key, value) => {
    if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('nonfinite_frame')
    return value
  })
  if (Buffer.byteLength(text) > MAX_FRAME) throw new Error('frame_too_large')
  return text + '\n'
}
function write(frame) {
  if (process.stdout.writableLength > MAX_BUFFER) throw new Error('output_budget_exceeded')
  process.stdout.write(encode(frame))
}
let resolveReady, rejectReady
const readiness = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject })
readiness.catch(() => {})
const readyTimer = setTimeout(() => { rejectReady(new Error('native_ready_timeout')); void stop(true) }, 30000)
const client = new JsonRpcGatewayClient({ requestTimeoutMs: 20000, connectTimeoutMs: 10000 })
client.onAny(event => {
  if (closing) return
  try {
    sequence++; write({ method: 'hermes/event', params: { event_seq: sequence, event } })
    if (event.type === 'gateway.ready') {
      if (typeof event.payload?.replay_epoch !== 'string' || !event.payload.replay_epoch) throw new Error('missing_native_epoch')
      ready = true; clearTimeout(readyTimer); resolveReady()
    }
  } catch { rejectReady(new Error('native_event_invalid')); void stop(true) }
})
client.onState(state => { if (ready && !closing && ['closed', 'error'].includes(state)) void stop(true) })
backend.on('error', () => { rejectReady(new Error('backend_start_failed')); void stop(true) })
backend.on('close', () => { backendClosed = true; if (!closing) { rejectReady(new Error('backend_exited')); void stop(true) } })
backend.stderr.on('data', bytes => { stderrBytes = Math.min(MAX_BUFFER, stderrBytes + bytes.length) })
backend.stdout.on('data', bytes => {
  stdoutBytes += bytes.length; stdoutBuffer += bytes.toString('utf8')
  if (stdoutBytes > MAX_BUFFER || stdoutBuffer.length > 65536) { stdoutBuffer = ''; rejectReady(new Error('backend_stdout_budget')); void stop(true); return }
  let index
  while ((index = stdoutBuffer.indexOf('\n')) >= 0) {
    const line = stdoutBuffer.slice(0, index).replace(/\r$/, ''); stdoutBuffer = stdoutBuffer.slice(index + 1)
    const match = /^HERMES_BACKEND_READY port=([0-9]+)$/.exec(line)
    if (!match) continue
    const port = Number(match[1])
    if (!Number.isInteger(port) || port < 1 || port > 65535 || ready || client.connectionState !== 'idle') {
      rejectReady(new Error('invalid_backend_sentinel')); void stop(true); return
    }
    void client.connect(`ws://127.0.0.1:${port}/api/ws?token=${token}`).catch(() => { rejectReady(new Error('native_connect_failed')); void stop(true) })
  }
})
async function stop(exitWhenDone) {
  if (!closing) closing = Promise.resolve().then(async () => {
    clearTimeout(readyTimer); rejectReady(new Error('worker_closing')); client.close()
    // CodexRpc created this new PGID. Ignore the TERM this owner sends itself.
    try { process.kill(-process.pid, 'SIGTERM') } catch { return false }
    const end = Date.now() + 5000
    do {
      try { if (backendClosed && groupMembers().every(row => Number(row[0]) === process.pid)) { closed = true; return true } } catch { return false }
      await delay(40)
    } while (Date.now() < end)
    return false // Keep the owner alive so its parent can verify and force-close its group.
  })
  const complete = await closing
  if (exitWhenDone && complete) finish()
  return complete
}
function finish() {
  if (finishing) return
  finishing = true
  process.stdin.pause(); process.stdin.destroy()
  process.stdout.end(() => process.exit(0))
}
process.on('SIGTERM', () => { if (!closing) void stop(true) })
process.on('SIGINT', () => { if (!closing) void stop(true) })
process.stdout.on('error', () => { void stop(true) })
process.stdin.on('end', () => { void stop(true) })
process.stdin.on('error', () => { void stop(true) })
async function dispatch(frame) {
  if (!frame || typeof frame !== 'object' || !Number.isInteger(frame.id) || Object.keys(frame).some(key => !['id', 'method', 'params'].includes(key))) throw new Error('invalid_bridge_frame')
  let result
  if (frame.method === 'bridge/close') {
    result = { ok: true, value: { closed: await stop(false) }, event_seq: sequence }
    write({ id: frame.id, result }); if (closed) finish(); return
  }
  try {
    await readiness
    if (closing) throw new Error('worker_closing')
    if (frame.method === 'bridge/ready') result = { ok: true, value: { ready: true }, event_seq: sequence }
    else if (frame.method === 'bridge/request') {
      const p = frame.params
      if (!p || typeof p.method !== 'string' || !p.method || p.method.length > 256 || !Number.isInteger(p.timeout_ms) || p.timeout_ms < 1 || p.timeout_ms > 3600000) throw new Error('invalid_bridge_request')
      const value = await client.request(p.method, p.params ?? {}, p.timeout_ms)
      result = { ok: true, value, event_seq: sequence }
    } else throw new Error('unsupported_bridge_method')
  } catch (error) {
    result = error instanceof JsonRpcGatewayError
      ? { ok: false, kind: 'remote', error: { code: error.code ?? null, message: error.message, data: error.data ?? null }, event_seq: sequence }
      : { ok: false, kind: 'transport', result_unknown: frame.method === 'bridge/request', event_seq: sequence }
  }
  write({ id: frame.id, result })
}
process.stdin.setEncoding('utf8')
process.stdin.on('data', chunk => {
  inputBuffer += chunk
  if (Buffer.byteLength(inputBuffer) > MAX_BUFFER) { inputBuffer = ''; void stop(true); return }
  let index
  while ((index = inputBuffer.indexOf('\n')) >= 0) {
    const line = inputBuffer.slice(0, index); inputBuffer = inputBuffer.slice(index + 1)
    if (Buffer.byteLength(line) > MAX_FRAME || active >= 64) { void stop(true); return }
    active++
    Promise.resolve().then(() => dispatch(JSON.parse(line))).catch(() => { void stop(true) }).finally(() => { active-- })
  }
})
