/** Load-time observation of one pinned RPC handler; native files remain untouched. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import * as module from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const pins = JSON.parse(fs.readFileSync(path.join(here, 'tracked_rpc_pin.json'), 'utf8'));
const hash = data => createHash('sha256').update(data).digest('hex');
const trackerURL = pathToFileURL(path.join(here, 'result_tracking.mjs')).href;

export function transformRpc(source) {
  const replacements = [
    ['export async function runRpcMode(runtimeHost) {',
      'export async function runRpcMode(runtimeHost) {\n    const rabbitTracker = createRabbitTracker();'],
    ['        const id = command.id;\n        switch (command.type) {',
      '        const id = command.id;\n        rabbitTracker.onCommand(command);\n        switch (command.type) {\n'
      + '            case "rabbit_task_capabilities": return success(id, command.type, rabbitTracker.capabilities());\n'
      + '            case "rabbit_task_read": return success(id, command.type, rabbitTracker.read(command, session));'],
    ['                void session\n                    .prompt(command.message, {',
      '                void rabbitTracker.prompt(command, session, () => session.prompt(command.message, {'],
    ['                    preflightResult: (didSucceed) => {',
      '                    rabbitTrackedRun: messages => rabbitTracker.start(command, session, messages),\n'
      + '                    preflightResult: (didSucceed) => {\n                        rabbitTracker.preflight(command, session, didSucceed);'],
    ['                })\n                    .catch((e) => {', '                }))\n                    .catch((e) => {'],
  ];
  for (const [before, after] of replacements) {
    if (source.split(before).length !== 2) throw new Error('Pi RPC observer source mismatch.');
    source = source.replace(before, after);
  }
  return `import { createTracker as createRabbitTracker } from ${JSON.stringify(trackerURL)};\n` + source;
}

export function transformSession(source) {
  const before = '        preflightResult?.(true);\n        await this._runAgentPrompt(messages);';
  if (source.split(before).length !== 2) throw new Error('Pi direct prompt observer source mismatch.');
  return source.replace(before, '        preflightResult?.(true);\n'
    + '        options?.rabbitTrackedRun?.(messages);\n        await this._runAgentPrompt(messages);');
}

// An unsupported native version retains its ordinary CLI. Tracking capability
// then stays absent; the host must refuse a new tracked submission beforehand.
let root, target, sessionTarget, supported = false;
try {
  const cli = fs.realpathSync(process.argv[1]);
  root = path.dirname(path.dirname(cli));
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  supported = pkg.name === pins.name && pkg.version === pins.version
    && Object.entries(pins.files).every(([name, digest]) => hash(fs.readFileSync(path.join(root, name))) === digest);
  target = pathToFileURL(path.join(root, 'dist/modes/rpc/rpc-mode.js')).href;
  sessionTarget = pathToFileURL(path.join(root, 'dist/core/agent-session.js')).href;
} catch { /* Ordinary native CLI remains available. */ }
if (supported && typeof module.registerHooks === 'function') module.registerHooks({ load(url, context, nextLoad) {
  const result = nextLoad(url, context);
  if (url !== target && url !== sessionTarget) return result;
  const source = typeof result.source === 'string' ? result.source : Buffer.from(result.source).toString('utf8');
  const name = url === target ? 'dist/modes/rpc/rpc-mode.js' : 'dist/core/agent-session.js';
  if (hash(source) !== pins.files[name]) throw new Error('Pi observer source changed.');
  return { ...result, source: url === target ? transformRpc(source) : transformSession(source) };
} });
