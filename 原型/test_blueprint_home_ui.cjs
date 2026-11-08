'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
// Exercise the actual host functions and page with invented data; no browser,
// model, native process, HTTP listener or application data is used here.
const assets = path.join(__dirname, 'rabbit_ui/assets');
const host = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');
const pageSource = fs.readFileSync(path.join(assets, 'my_ai_page.js'), 'utf8');
function body(source, begin, end) { const a = source.indexOf(begin), b = source.indexOf(end, a); assert(a > 0 && b > a, 'test needs the actual host function'); return source.slice(a, b); }
const clone = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let n = 0; n < 5; n++) await Promise.resolve(); };
class Node {
  constructor(tag, document) { this.tag = tag; this.document = document; this.children = []; this.dataset = {}; this.events = {}; this.parentNode = null; this.textContent = ''; this.className = ''; this.attrs = {}; }
  append(...nodes) { for (const node of nodes) { if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(item => item !== node); this.children.push(node); node.parentNode = this; } }
  replaceChildren(...nodes) { this.children.forEach(node => { node.parentNode = null; }); this.children = []; this.append(...nodes); }
  addEventListener(type, callback) { this.events[type] = callback; }
  setAttribute(key, value) { this.attrs[key] = value; }
  contains(node) { return walk(this).includes(node); }
  querySelectorAll(selector) { return walk(this).filter(node => selector === '[data-dashboard-focus]' ? node.dataset.dashboardFocus : selector === `[data-dashboard-focus="${node.dataset.dashboardFocus}"]`); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.document.activeElement = this; }
  click() { const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }; this.events.click?.(event); return event; }
}
function walk(node) { return [node, ...node.children.flatMap(walk)]; }
function control(root, label) { return walk(root).find(node => node.tag === 'button' && node.textContent === label); }
function sample(scope = 'fixture') {
  const resource = (kind, n) => ({ id: `${kind}-${n}`, name: `${kind} ${n}`, status: kind === 'agent' && n === 4 ? 'running' : 'unverified', status_label: '待核对', target: { kind, id: `${kind}-${n}` }, model_count: 0, agent_name: '虚构 Agent', program: 'hindsight' });
  return { workspace_id: scope, empty: false, agents: [1, 2, 3, 4].map(n => resource('agent', n)),
    models: [1, 2, 3, 4].map(n => resource('model', n)), memories: [1, 2, 3, 4].map(n => resource('memory', n)), works: [], issues: [] };
}
function pageFixture(provider = async scope => sample(scope), sections = {}) {
  let scope = 'fixture', nextTimer = 0;
  const document = { createElement: tag => new Node(tag, document) };
  document.body = document.activeElement = document.createElement('body');
  const root = document.createElement('section'), actions = [], requests = [], timers = new Map();
  const window = { RabbitMyAI: { DASHBOARD_PATH: '/api/management/my-ai?view=dashboard', project: value => clone(value) } };
  vm.runInNewContext(pageSource, { window, document, Promise,
    setTimeout(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; }, clearTimeout(id) { timers.delete(id); } });
  const page = window.RabbitMyAIPage.create({ root, getWorkspace: () => scope,
    api: async (...args) => { requests.push(args); return provider(scope); }, onAction: action => actions.push(action), sections });
  return { page, root, document, actions, requests, scope(value) { scope = value; }, expire() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(item => item.callback()); }, timers };
}
function pollFixture(initialPage = 'my-ai', deferred = false) {
  let release;
  const calls = [], profiles = [], quiet = [];
  const state = { page: initialPage, openVersion: 0, selected: { type: 'chat', id: 'fictional-chat' }, profilesRevision: null, selectedRevision: null,
    libraryWatchRevision: 'same', workspaceSections: {}, preferences: { revision: '0' }, status: { workspace_id: 'fixture' } };
  const response = { profiles_revision: 'changed', library_revision: 'same', selected: { id: 'fictional-chat', revision: 'changed', running: false } };
  const sandbox = { state, document: { hidden: false }, profilesEnabled: () => true, profiles: { load: async flag => profiles.push(flag) },
    readNativeWorkStatus() {}, AbortController, URLSearchParams, api: async route => { calls.push(route); return deferred ? new Promise(resolve => { release = resolve; }) : response; },
    workspace: null, nativeChats: null, myAI: { refresh: async options => quiet.push(options) }, updateComposer() {}, plain: value => String(value || ''), $: () => ({}), schedulePoll() {} };
  vm.runInNewContext(body(host, '  function profileDirectoryNeeded() {', '  function sourceCanContinue(') + body(host, '  async function syncCurrentView() {', '  function historyRangeLabel(') + '\nthis.run=syncCurrentView;', sandbox);
  return { run: sandbox.run, state, calls, profiles, quiet, release() { release(response); } };
}
let passed = 0;
async function test(name, run) { await run(); console.log(`ok ${++passed} - ${name}`); }
(async () => {
  await test('home polling reads cached metadata without profiles or the old selected chat', async () => {
    const f = pollFixture(); await f.run();
    assert.equal(f.profiles.length, 0); assert.equal(f.calls.length, 1); assert(f.calls[0].startsWith('/api/changes'));
    assert.equal(f.quiet.length, 1); assert.equal(f.quiet[0].quiet, true);
  });
  await test('a conversation poll landing after navigation to Home cannot load profiles', async () => {
    const f = pollFixture('conversations', true), pending = f.run(); await settle(); f.state.page = 'my-ai'; f.release(); await pending;
    assert.equal(f.profiles.length, 0); assert.equal(f.calls.length, 1); assert.equal(f.quiet.length, 0);
  });
  await test('read-only history and cached Agent details never poll the editable profile directory', async () => {
    for (const page of ['conversations', 'management']) {
      const f = pollFixture(page); f.state.selected = { type: 'history', id: 'codex:fictional-history' };
      f.state.historyReadOnly = true; f.state.workspaceSections.management = 'agents'; await f.run();
      assert.equal(f.profiles.length, 0); assert.equal(f.calls.length, 1);
    }
  });
  await test('missing or foreign profile directory preserves the saved draft configuration', async () => {
    for (const directory of [null, { workspace_id: 'foreign' }]) {
      const id = 'fictional-chat', saved = { ai_profile: { id: 'saved-config' }, memory_profile: { id: 'none' } };
      const state = { status: { workspace_id: 'fixture' }, selected: { type: 'chat', id }, profileSelectionSync: 'pending',
        profileSelection: { ai_profile_id: 'saved-config', memory_profile_id: 'none' }, profile_snapshot: saved, profileChoices: new Map([[id, saved]]), profilesInventory: directory };
      const sandbox = { state, profiles: { get() {}, getMemory() {} }, commonProfileBusy: () => false, draftKey: () => id,
        adoptProfile() { throw Error('no unknown profile adoption'); }, toProfileSnapshot() {}, sameProfile() {}, invalidateContextPreview() {}, checkpointBrowserState() {} };
      vm.runInNewContext(body(host, '  function applyObservedProfileSelection() {', '  function acceptProfileCatalog(') + '\nthis.run=applyObservedProfileSelection;', sandbox); sandbox.run();
      assert.equal(state.profile_snapshot, saved); assert.equal(state.profileChoices.get(id), saved); assert.equal(state.profileSelectionSync, 'pending');
    }
  });
  await test('home shows at most three Agent summaries and expands to the exact Agent objects', async () => {
    const f = pageFixture(); await f.page.show();
    const summaries = walk(f.root).filter(node => node.className === 'my-ai-agent-name');
    assert.equal(summaries.length, 3);
    assert.equal(summaries[0].textContent, 'agent 4', 'working Agent is prioritized');
    assert(!control(f.root, 'model 4')); assert(!control(f.root, '最近工作'));
    control(f.root, '展开 ↓').click();
    assert.equal(walk(f.root).filter(node => node.className === 'my-ai-agent-row').length, 4);
    control(f.root, 'agent 4').click(); await settle();
    assert.deepEqual(clone(f.actions[0]), { kind: 'manage', target: { kind: 'agent', id: 'agent-4', fromMyAI: true } });
    f.page.hide(); await f.page.show();
    assert.equal(f.document.activeElement, control(f.root, 'agent 4'));
    f.page.hide();
  });
  await test('workspace changes discard old objects and old retained controls cannot dispatch', async () => {
    let release; const f = pageFixture(async scope => scope === 'fixture' ? sample(scope) : new Promise(resolve => { release = resolve; }));
    await f.page.show(); control(f.root, '展开 ↓').click(); const old = control(f.root, 'agent 1'); f.scope('new-workspace'); const pending = f.page.show();
    assert(!control(f.root, 'agent 1')); old.click(); await settle(); assert.equal(f.actions.length, 0);
    release(sample('new-workspace')); await pending; control(f.root, '展开 ↓').click(); control(f.root, 'agent 1').click(); await settle(); assert.equal(f.actions.length, 1);
    f.page.hide();
  });
  await test('simultaneous overview reads share one HTTP request', async () => {
    let release; const f = pageFixture(() => new Promise(resolve => { release = resolve; }));
    const first = f.page.show(), second = f.page.refresh(); assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0][0], '/api/management/my-ai?view=dashboard'); assert.equal(f.requests[0][1].method, 'GET'); assert.equal(f.requests[0][2], false);
    release(sample()); await Promise.all([first, second]); f.page.hide();
  });
  await test('unchanged quiet refresh preserves rendered controls and their actions', async () => {
    const f = pageFixture(); await f.page.show(); control(f.root, '展开 ↓').click(); const old = control(f.root, 'agent 1');
    await f.page.refresh({ quiet: true }); assert.equal(control(f.root, 'agent 1'), old, 'no full redraw for unchanged facts');
    old.click(); await settle(); assert.equal(f.actions.length, 1, 'the preserved control remains usable'); f.page.hide();
  });
  await test('execution facts expire without running another check or model request', async () => {
    const f = pageFixture(); await f.page.show(); assert(walk(f.root).some(node => node.dataset.state === 'running'));
    assert([...f.timers.values()].some(timer => timer.delay === 6000)); const requests = f.requests.length; f.expire();
    assert(!walk(f.root).some(node => node.dataset.state === 'running')); assert.equal(f.requests.length, requests); f.page.hide();
  });
  await test('return restores the asynchronous source entry and rejects detached visit controls', async () => {
    const renders = [];
    const f = pageFixture(undefined, { usage: { render(mount, context) { renders.push({ mount, context }); } } });
    const ready = ({ mount, context }) => {
      const trigger = f.document.createElement('button');
      trigger.dataset.dashboardFocus = 'usage-codex';
      mount.append(trigger); context.onReady(); return trigger;
    };
    await f.page.show();
    const first = renders.at(-1), trigger = ready(first);
    trigger.focus(); f.root.scrollTop = 210;
    first.context.open('codex');
    assert.equal(f.root.dataset.dashboardView, 'usage');
    assert.equal(renders.at(-1).context.focusId, 'codex');
    first.context.open('foreign');
    assert.equal(renders.at(-1).context.focusId, 'codex');
    const back = control(f.root, '← 我的 AI'); back.click();
    assert.equal(f.root.dataset.dashboardView, 'home');
    assert.equal(f.document.activeElement, back);
    const restored = ready(renders.at(-1));
    assert.equal(f.document.activeElement, restored);
    assert.equal(f.root.scrollTop, 210);
    renders.at(-1).context.open('codex'); control(f.root, '← 我的 AI').click();
    const moved = control(f.root, '展开 ↓'); moved.focus(); ready(renders.at(-1));
    assert.equal(f.document.activeElement, moved, 'late cards cannot take back user focus');
    f.page.hide();
  });
  await test('passive sidebar availability does not read or start the specialist', async () => {
    const source = fs.readFileSync(path.join(assets, 'specialist.js'), 'utf8');
    let reads = 0;
    const sandbox = { chatId: null, passive: false, enabled: false, composerMount: {}, destroyed: false, workspace: 'fixture',
      initialComposerRead: false, timer: null, syncWorkspace() {}, hide() {}, render() {}, clearTimeout() {}, refresh() { reads++; } };
    vm.runInNewContext(body(source, '    function setEnabled(value, options = {}) {', '    function setMaterialInputs(') + '\nthis.run=setEnabled;', sandbox);
    sandbox.run(true, { passive: true }); assert.equal(reads, 0);
    sandbox.run(true, { passive: false }); assert.equal(reads, 1, 'explicit active mode can read the existing specialist');
  });
  console.log(`PASS ${passed} Blueprint Home UI groups`);
})().catch(error => { console.error(error); process.exitCode = 1; });
