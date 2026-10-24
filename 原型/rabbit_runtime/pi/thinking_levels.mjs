/* One offline query of the installed Pi SDK's own pure capability function.
 * This helper is launched with a fixed filesystem allowlist and no child,
 * worker, addon, or WASI permission. Node 24 has no network permission switch;
 * standard network entry points are disabled before importing the SDK file.
 * This is a fixed trusted-module helper, not a sandbox for arbitrary JS.
 */
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import http2 from 'node:http2';
import dgram from 'node:dgram';
import dns from 'node:dns';
import { syncBuiltinESMExports } from 'node:module';
import { pathToFileURL } from 'node:url';

const LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh']);
const MAX_BYTES = 512 * 1024, MAX_MODELS = 4096;
// A row with all six native levels adds at most 52 bytes over the minimal
// input row. Keep a separate bounded output allowance for that exact projection.
const MAX_OUTPUT_BYTES = MAX_BYTES + MAX_MODELS * 64;
const denyNetwork = () => { throw Error('Offline helper cannot use network APIs.'); };
for (const [module, methods] of [
  [net, ['connect', 'createConnection', 'createServer']],
  [tls, ['connect', 'createServer']],
  [http, ['request', 'get', 'createServer']],
  [https, ['request', 'get', 'createServer']],
  [http2, ['connect', 'createServer', 'createSecureServer']],
  [dgram, ['createSocket']],
]) for (const method of methods) module[method] = denyNetwork;
net.Socket.prototype.connect = denyNetwork;
net.Server.prototype.listen = denyNetwork;
for (const module of [dns, dns.promises]) for (const name of Object.keys(module)) {
  if (/^(lookup|resolve)/.test(name) && typeof module[name] === 'function') module[name] = denyNetwork;
}
globalThis.fetch = denyNetwork;
globalThis.WebSocket = class { constructor() { denyNetwork(); } };
syncBuiltinESMExports();

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const identity = (value, limit) => typeof value === 'string' && value.trim().length > 0
  && Array.from(value).length <= limit && !/[\p{Cc}\p{Cf}\p{Cs}]/u.test(value);
function model(value) {
  if (!object(value) || Object.keys(value).some(key => !['provider', 'id', 'reasoning', 'thinkingLevelMap'].includes(key))
      || !identity(value.provider, 256) || !identity(value.id, 1024) || typeof value.reasoning !== 'boolean') throw Error('Invalid input.');
  if (Object.hasOwn(value, 'thinkingLevelMap')) {
    if (!object(value.thinkingLevelMap) || Object.keys(value.thinkingLevelMap).some(key => !LEVELS.has(key)
        || value.thinkingLevelMap[key] !== null && (typeof value.thinkingLevelMap[key] !== 'string'
          || Array.from(value.thinkingLevelMap[key]).length > 128 || /[\x00-\x1f\x7f]/u.test(value.thinkingLevelMap[key])))) throw Error('Invalid mapping.');
  }
  return value;
}

try {
  if (process.argv.length !== 3 || !process.argv[2].endsWith('/dist/models.js')) throw Error('Invalid SDK entry.');
  const chunks = []; let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length; if (bytes > MAX_BYTES) throw Error('Input exceeds capacity.');
    chunks.push(chunk);
  }
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!object(input) || Object.keys(input).sort().join(',') !== 'models,schema_version' || input.schema_version !== 1
      || !Array.isArray(input.models) || input.models.length > MAX_MODELS) throw Error('Invalid envelope.');
  const models = input.models.map(model);
  if (new Set(models.map(row => JSON.stringify([row.provider, row.id]))).size !== models.length) throw Error('Duplicate identity.');
  const sdk = await import(pathToFileURL(process.argv[2]).href);
  if (typeof sdk.getSupportedThinkingLevels !== 'function') throw Error('Native export unavailable.');
  const results = models.map(row => {
    const levels = sdk.getSupportedThinkingLevels(row);
    if (!Array.isArray(levels) || levels.length > LEVELS.size || levels.some(level => !LEVELS.has(level))
        || new Set(levels).size !== levels.length) throw Error('Invalid native capability.');
    return { provider: row.provider, model: row.id, thinking_levels: levels };
  });
  const output = JSON.stringify({ schema_version: 1, models: results });
  if (Buffer.byteLength(output, 'utf8') > MAX_OUTPUT_BYTES) throw Error('Output exceeds capacity.');
  process.stdout.write(output + '\n');
} catch (_) {
  // Never forward module diagnostics, paths, model bodies, or stderr contents.
  process.stderr.write('Pi offline capability query failed.\n');
  process.exitCode = 1;
}
