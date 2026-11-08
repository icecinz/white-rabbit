const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert');
// Synthetic DOM/API only: no sockets, native history, saved preferences, or model calls.
class Node {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.className = ''; this.hidden = false; this.disabled = false; this.checked = false; this.value = ''; this.id = ''; this._text = ''; this.classList = { toggle: (name, active) => { const classes = this.className.split(/\s+/).filter(Boolean).filter(c => c !== name); if (active) classes.push(name); this.className = classes.join(' '); } }; }
  append(...children) { for (const child of children) this.children.push(child); }
  replaceChildren(...children) { this.children = [...children]; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(_) { throw Error('HTML insertion is forbidden'); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  fire(event) { if (this.disabled) return; for (const callback of this.listeners[event] || []) callback({ target: this }); }
  all(predicate) { return this.children.flatMap(child => [...(predicate(child) ? [child] : []), ...child.all(predicate)]); }
}
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const CACHE_PATH = '/api/management/my-ai';
const isInventoryRoute = route => route === CACHE_PATH || route === '/api/management?refresh=1';
const defaults = { theme: 'system', font_size: 'normal', density: 'comfortable', send_shortcut: 'enter', default_backend: 'codex', show_internal: false, markdown: true, favorites: [] };
function settingsProjection(prefs = {}) {
  const values={theme:['system','light','dark'],density:['comfortable','compact'],send_shortcut:['enter','ctrl-enter'],markdown:[true,false],show_internal:[true,false]};
  const labels={theme:'主题',density:'布局密度',send_shortcut:'发送快捷键',markdown:'统一 Markdown 排版',show_internal:'显示内部记录'};
  return {workspace_id:'a'.repeat(64),scope:'application_preferences',inference_performed:false,revision:prefs.revision||'0',settings:Object.fromEntries(Object.keys(values).map(key=>[key,prefs[key]??defaults[key]])),catalogue:{schema_version:1,read_tool:'rabbit_settings_read',update_tool:'rabbit_settings_update',fields:Object.entries(values).map(([key,allowed_values])=>({key,label:labels[key],description:key==='show_internal'?'只包括已索引的子代理协作、自动审批记录；不含系统、工具和推理正文。':'只改变显示或操作偏好，不修改保存的原文。',type:typeof allowed_values[0],allowed_values,default:defaults[key],surface:['markdown','show_internal'].includes(key)?'ai_only':'human_and_ai',example:{expected_revision:'0',patch:{[key]:key==='markdown'?false:allowed_values[0]}}}))}};
}
const management = { device: { id: 'local', name: '合成 Mac <img>', platform: 'macOS', online: true }, agents: [{ id: 'codex', name: 'Codex', available: true, model: 'synthetic-sol', provider: 'synthetic-codex' }, { id: 'hermes', name: 'Hermes', available: true, model: 'synthetic-deepseek', provider: 'synthetic-hermes' }, { id: 'missing', name: '未接入 <script>', available: false, model: 'none', reason: '尚未接入' }], models: [{ id: 'local/codex/model', name: 'synthetic-sol', agent_id: 'codex', connection: 'Codex 内置连接', available: true }, { id: 'local/hermes/model', name: 'synthetic-deepseek', agent_id: 'hermes', connection: 'Hermes 内置连接', available: true }, { id: 'local/codex/offline', name: '未接入', agent_id: 'missing', connection: '尚未配置', available: false, reason: '尚未配置' }], notice: '只报告这台电脑' };
function setup(customApi, options = {}) {
  const html = new Node('html'), root = new Node('section'), calls = [], selected = { backend: 'codex', model_id: '', chatId: 'fixture', chatTitle: '合成对话' }, emitted = [], inventories = [], media = { matches: false, listeners: [], addEventListener: (_event, callback) => media.listeners.push(callback) };
  const storage = options.storage || new Map(), languageChanges = [];
  let saved = { ...defaults, favorites: [] }, selectCalls = [], rejectSelection = false;
  const api = async (route, request = {}) => { calls.push({ route, options:request });
    if (route === '/api/management/settings') {
      if (options.settingsApi) return options.settingsApi(route,request);
      if (customApi) return settingsProjection(await customApi('/api/preferences',request));
      if (request.method === 'POST') saved={...saved,...JSON.parse(request.body).patch};
      return settingsProjection(saved);
    }
    if (route === CACHE_PATH) { const response = customApi ? await customApi(route,request) : management; return response?.scope === 'saved_and_cached_overview' ? response : { workspace_id: String(options.getWorkspace?.() || 'a'.repeat(64)), scope: 'saved_and_cached_overview', connection_checked: false, memory_read: false, inference_performed: false, management: response, cache: {} }; }
    if (customApi) return customApi(route,request); if (route === '/api/management?refresh=1') return management; if (route === '/api/preferences') { if (request.method === 'POST') saved = { ...saved, ...JSON.parse(request.body).patch }; return saved; } throw Error('Unexpected route ' + route);
  };
  const localStorage = options.blockStorage ? { getItem: () => { throw Error('storage blocked'); }, setItem: () => { throw Error('storage blocked'); } } : { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  const sandbox = { document: { documentElement: html, createElement: tag => new Node(tag) }, window: { matchMedia: () => media, localStorage }, fetch: options.fetch, console };
  if (options.projectOverview) sandbox.window.RabbitMyAI = { project: options.projectOverview };
  require(path.join(__dirname,'test_helpers/agents.cjs')).load(sandbox);vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'rabbit_ui/assets/workspace.js'), 'utf8'), sandbox);
  const instance = sandbox.window.RabbitWorkspace.create({ root, api, getWorkspace: options.getWorkspace || (() => 'a'.repeat(64)), getOverview: options.getOverview, onOpenWork: options.onOpenWork, workStatus: options.workStatus, getSelection: () => selected, selectBackend: async id => { selectCalls.push(id); if (rejectSelection) throw Error('切换失败，请重试。'); selected.backend = id; selected.model_id = ""; }, selectModel: async (id, backend) => { selectCalls.push(backend); if (rejectSelection) throw Error("模型选择失败，请重试。"); selected.backend = backend; selected.model_id = id; }, profiles: options.profiles, profilesEnabled: options.profilesEnabled, onSectionChange: options.onSectionChange, renderSpecialistSettings: options.renderSpecialistSettings, renderNativeSettings: options.renderNativeSettings, onPreferences: value => emitted.push(value), onManagement: value => inventories.push(value), onLanguageChange: value => languageChanges.push(value), onAddResource: options.onAddResource, onManual: options.onManual, onBackToMyAI: options.onBackToMyAI });
  const find = text => root.all(n => n.tagName === 'BUTTON' && n.textContent === text)[0];
  const control = key => root.all(n => n.id === 'rbw-' + key)[0];
  return { html, root, calls, selected, emitted, inventories, media, instance, find, control, selectCalls, storage, languageChanges, workspace: sandbox.window.RabbitWorkspace, setSaved:patch=>{saved={...saved,...patch};}, failSelection: () => { rejectSelection = true; } };
}
(async () => {
  const t = setup(); t.instance.show('management'); await settle();
  assert(t.root.textContent.includes('未接入 <script>')); assert.equal(t.root.all(n => n.tagName === 'IMG' || n.tagName === 'SCRIPT').length, 0); assert(!t.root.textContent.includes('设备')); assert(!t.root.textContent.includes('手机入口')); assert.equal(t.inventories.length, 1); assert.equal(t.inventories[0].agents[0].model, 'synthetic-sol');
  assert.equal(t.instance.getSection().tab, 'agents'); assert.equal(t.root.all(n => n.tagName === 'ARTICLE')[0].dataset.controlInterface,'ai'); assert.equal(t.find('刷新检测').dataset.controlInterface,'ai'); assert.equal(t.root.all(n => n.className === 'rbw-route').length,0); assert.equal(t.find('查看设备详情'),undefined);
  assert.deepEqual(Array.from(t.instance.getNavigation('management'), row => row.id), ['agents','models']);
  console.log('PASS Management: defaults to local Agents; retired device/phone entry absent; safe names and existing inventory retained');

  t.instance.show('management', 'agents'); assert(t.root.all(n => n.tagName === 'ARTICLE').every(n => n.dataset.controlInterface === 'ai')); assert(t.root.all(n => n.tagName === 'ARTICLE').every(n => n.all(child => child.tagName === 'BUTTON').length === 0)); assert.equal(t.selected.backend, 'codex'); assert.equal(t.selectCalls.length, 0); assert(!t.root.textContent.includes('用它新建 AI 配置')); assert(t.root.textContent.includes('synthetic-deepseek'));
  const compactAgent = t.root.all(n => n.tagName === 'ARTICLE')[0];
  const agentMore = compactAgent.children.find(n => n.tagName === 'DETAILS' && n.className === 'rbw-more');
  assert(agentMore && !agentMore.open); assert.equal(agentMore.children[0].textContent, '备注');
  assert(agentMore.textContent.includes('synthetic-codex')); assert(!agentMore.textContent.includes('查看接入详情'));
  assert(!compactAgent.children.filter(n => n.tagName === 'P').some(n => n.textContent.includes('synthetic-codex')));
  assert(!compactAgent.children.some(n => n.className === 'rbw-card-actions'));
  assert(compactAgent.textContent.includes('接入检查通过')); assert(!compactAgent.textContent.includes('当前可用')); assert(t.root.textContent.includes('接入待核对')); assert.equal(t.root.all(n=>n.className.includes('rbw-unavailable')).length,0); assert(!compactAgent.textContent.includes('配置检测可用')); assert(!compactAgent.textContent.includes('已接入 1 台'));
  const beforeDisclosure = t.calls.length; agentMore.open = true; assert.equal(t.calls.length, beforeDisclosure);assert(t.find('刷新检测'));assert.equal(t.root.all(n=>n.tagName==='H2').length,0);
  console.log('PASS Management hierarchy: category heading and route summary are not repeated; notes expand without requests');
  console.log('PASS Agent directory: read-only cards, no profile creation or reply-route switching');

  t.instance.show('management', 'models'); assert.equal(t.root.all(n => n.tagName === 'ARTICLE').length, 3); assert(t.root.all(n => n.tagName === 'ARTICLE').every(n => n.dataset.controlInterface === 'ai'));
  const unavailableCard=t.root.all(n=>n.tagName==='ARTICLE').find(card=>card.dataset.modelId==='local/codex/offline'),notes=unavailableCard.all(n=>n.tagName==='DETAILS')[0];assert(notes&&!notes.open);assert.equal(notes.children[0].textContent,'备注');assert(notes.textContent.includes('尚未配置'));assert(unavailableCard.textContent.includes('0/1 条通路接入检查通过'));assert(unavailableCard.textContent.includes('接入待核对'));assert(!unavailableCard.textContent.includes('不可用'));assert.equal(unavailableCard.all(n=>n.className.includes('rbw-unavailable')).length,0);assert(!notes.textContent.includes('local/codex/offline'));assert.equal(unavailableCard.all(n=>n.tagName==='BUTTON').length,0);
  const search = t.root.all(n => n.tagName === 'INPUT' && n.type === 'search')[0], beforeModels=t.calls.length, beforeSelection=t.selectCalls.length;
  assert.equal(search.dataset.controlInterface,undefined);assert.equal(t.find('收藏'),undefined);assert.equal(t.find('☆ 收藏'),undefined);assert.equal(t.find('跟随 Agent'),undefined);
  search.value='sol';search.fire('input');const readonlyCards=t.root.all(n=>n.tagName==='ARTICLE');assert.equal(readonlyCards.length,1);assert.equal(t.root.all(n=>n.tagName==='INPUT'&&n.type==='search')[0],search);assert(readonlyCards[0].textContent.includes('synthetic-sol'));assert(readonlyCards[0].textContent.includes('1/1 条通路接入检查通过'));assert(readonlyCards[0].textContent.includes('Codex'));assert.equal(readonlyCards[0].all(n=>n.tagName==='BUTTON').length,0);assert.equal(readonlyCards[0].all(n=>n.tagName==='DETAILS').length,0);assert.equal(t.calls.length,beforeModels);assert.equal(t.selectCalls.length,beforeSelection);
  console.log('PASS Model catalog: safe name, availability and Agent list only; local search preserves its field and cannot select or send');

  const multiCatalog={...management,models:[{id:'local/codex/A',agent_id:'codex',name:'模型 A',model:'A',available:true,is_default:true},{id:'local/codex/B',agent_id:'codex',name:'模型 B',model:'B',available:true},{id:'local/hermes/Pro',agent_id:'hermes',name:'模型 Pro',model:'Pro',available:true}]};
  const models=setup(route=>isInventoryRoute(route)?multiCatalog:{...defaults,favorites:['local/codex/A']});models.instance.show('management','models');await settle();
  const modelCards=models.root.all(n=>n.tagName==='ARTICLE');assert.equal(modelCards.length,3);assert(modelCards.every(card=>card.all(n=>n.tagName==='BUTTON').length===0));assert(!models.root.textContent.includes('新建 AI 配置'));assert(!models.root.textContent.includes('跟随'));assert(!models.root.textContent.includes('收藏'));assert.equal(models.selectCalls.length,0);assert.equal(models.selected.model_id,'');assert(models.calls.every(c=>!c.options.method));assert.deepEqual(Array.from(models.instance.preferences().favorites),['local/codex/A']);
  console.log('PASS Model cleanup: no profile creation, fixed/default routing or favorite controls; existing saved preferences remain readable without writes');

  t.instance.show('settings', 'appearance'); await settle(); t.control('theme').value = 'dark'; t.control('theme').fire('change'); await settle(); assert.equal(t.html.dataset.theme, 'dark'); assert(!t.root.textContent.includes('已保存到这台电脑'));assert(t.root.all(n=>n.className==='rbw-feedback')[0].hidden); assert.equal(t.control('font_size'), undefined); assert(!t.root.textContent.includes('文字大小')); assert.equal(t.control('density'), undefined); assert.equal(t.control('markdown'), undefined); t.setSaved({markdown:false,density:'compact'}); t.instance.applyPreferences({markdown:false,density:'compact'}); await settle(); assert.equal(t.html.dataset.font, undefined); assert.equal(t.html.dataset.density, 'compact'); assert.equal(t.instance.preferences().markdown, false); assert.equal(t.emitted.at(-1).markdown, false); t.instance.applyPreferences({font_size: 'large'}); assert.equal(t.html.dataset.font, undefined); assert(!Object.hasOwn(t.instance.preferences(), 'font_size')); assert(!fs.readFileSync(path.join(__dirname, 'rabbit_ui/assets/workspace.css'), 'utf8').includes('data-font'));
  console.log('PASS Appearance: theme remains editable, AI-updated density/Markdown still apply, font-size setting stays removed');

  t.instance.show('settings', 'chat'); assert.equal(t.control('default_backend'),undefined); assert(t.control('send_shortcut')); assert.equal(t.root.all(n=>n.tagName==='SELECT').length,3); const beforeAIUpdate=t.calls.length; t.setSaved({send_shortcut:'ctrl-enter'}); t.instance.applyPreferences({send_shortcut:'ctrl-enter'}); await settle(); assert.equal(t.calls.length,beforeAIUpdate); assert.equal(t.instance.preferences().default_backend, 'codex'); assert.equal(t.instance.preferences().send_shortcut, 'ctrl-enter'); assert.equal(t.selectCalls.length, 0); assert(!t.root.textContent.includes('本对话')); assert(!t.root.textContent.includes('新对话默认')); assert.equal(t.root.all(n=>n.className==='rbw-tabs').length,0);
  console.log('PASS General settings: theme/language/shortcut controls, AI-updated shortcut applied without a browser write or duplicate classification');

  t.instance.show('settings', 'ai-settings'); assert.equal(t.control('show_internal'), undefined); t.setSaved({show_internal:true}); t.instance.applyPreferences({show_internal:true}); await settle(); assert.equal(t.instance.preferences().show_internal, true); assert(t.root.all(n=>n.className==='rbw-ai-settings').length===1); assert(t.root.textContent.includes('当前：显示')); assert(t.root.textContent.includes('当前：紧凑')); assert(!t.root.textContent.includes('当前：Ctrl / ⌘ + Enter 发送')); const aiRows=t.root.all(n=>n.className==='rbw-ai-setting'); assert.deepEqual(aiRows.map(n=>n.dataset.settingKey),['density','markdown','show_internal']); assert(aiRows.every(n=>n.dataset.controlInterface==='ai'&&!n.all(c=>['INPUT','SELECT','BUTTON'].includes(c.tagName)).length)); assert(!t.root.textContent.includes('可以这样说'));assert(!t.root.textContent.includes('主题保留在通用设置')); assert.equal(t.root.all(n => n.tagName === 'BUTTON' && /备份|同步/.test(n.textContent)).length, 0); assert.equal(t.find('连接'), undefined); t.instance.show('settings', 'connection'); assert(t.control('theme')); assert(t.control('send_shortcut')); assert(!t.root.textContent.includes('手机入口')); t.instance.show('management', 'devices'); const beforeRefresh = t.calls.filter(c => c.route === '/api/management?refresh=1').length; t.find('刷新检测').fire('click'); await settle(); assert.equal(t.calls.filter(c => c.route === '/api/management?refresh=1').length, beforeRefresh + 1); assert.equal(t.inventories.length, 2); assert.equal(t.instance.getSection().tab, 'agents'); assert(t.root.textContent.includes('synthetic-sol'));
  console.log('PASS Data and management: obsolete routes fall back safely; local read-only detection remains');

  const entry = setup(); entry.instance.show('management', 'devices'); await settle();
  assert.equal(entry.instance.getSection().tab, 'agents');
  assert(entry.calls.every(c => c.route === '/api/preferences' || c.route === '/api/management/settings' || c.route === '/api/management?refresh=1' || c.route === CACHE_PATH));
  assert(entry.calls.every(c => !c.options.method || c.options.method === 'GET'));
  assert.equal(entry.selectCalls.length, 0); assert.equal(entry.root.all(n => n.tagName === 'IFRAME' || n.tagName === 'IMG').length, 0);
  assert(!entry.root.textContent.includes('手机入口')); assert(!entry.root.textContent.includes('IM.codes')); assert(!entry.root.textContent.includes('唤醒'));
  const readOnlyCalls = entry.calls.length; entry.instance.show('settings', 'chat'); entry.instance.show('management', 'devices'); await settle();
  assert.equal(entry.calls.length, readOnlyCalls+1); assert.equal(entry.calls.at(-1).route, '/api/management/settings');
  console.log('PASS Retired devices route falls back to Agents; local metadata/preferences remain read-only without phone or remote requests');

  t.instance.hide(); assert(t.root.hidden); t.instance.show('management', 'agents'); t.selected.backend = 'hermes'; t.selected.model_id = ''; t.instance.selectChanged(); assert.equal(t.root.all(n=>n.className==='rbw-route').length,0); assert(t.root.textContent.includes('synthetic-deepseek')); assert.equal(t.selectCalls.length, 0); assert(t.root.all(n => n.tagName === 'ARTICLE').every(n => n.all(child => child.tagName === 'BUTTON').length === 0));
  console.log('PASS Agent cards stay read-only while current selection display refreshes');

  t.instance.applyPreferences({ theme: 'system' }); t.media.matches = true; for (const listener of t.media.listeners) listener(); assert.equal(t.html.dataset.theme, 'dark'); t.instance.applyPreferences({ theme: 'light', favorites: ['local/codex/a', 'local/codex/a', 'local/hermes/b'], markdown: 'wrong' }); assert.equal(t.html.dataset.theme, 'light'); assert.deepEqual(Array.from(t.instance.preferences().favorites), ['local/codex/a', 'local/hermes/b']); assert.equal(t.instance.preferences().markdown, false);
  console.log('PASS System theme: matchMedia follows only system preference, invalid preference values ignored');

  const first = deferred(), second = deferred(); let postIndex = 0;
  const r = setup((route, options) => isInventoryRoute(route) ? management : options.method === 'POST' ? (++postIndex === 1 ? first.promise : second.promise) : { ...defaults }); r.instance.show('settings', 'appearance'); await settle(); r.control('theme').value = 'dark'; r.control('theme').fire('change'); r.control('theme').value = 'light'; r.control('theme').fire('change'); assert.equal(postIndex, 1); first.resolve({ ...defaults, theme: 'dark' }); await settle(); assert.equal(postIndex, 2); assert.equal(r.instance.preferences().theme, 'light'); assert.equal(r.html.dataset.theme, 'light'); second.resolve({ ...defaults, theme: 'light' }); await settle(); assert.equal(r.instance.preferences().theme, 'light');
  console.log('PASS Save race: serialized requests, older response cannot replace a newer preference');

  const failed = setup((route, options) => isInventoryRoute(route) ? management : options.method === 'POST' ? Promise.reject(Error('这次保存失败')) : { ...defaults }); failed.instance.show('settings', 'appearance'); await settle(); failed.control('theme').value = 'dark'; failed.control('theme').fire('change'); await settle(); assert.equal(failed.instance.preferences().theme, 'system'); assert.equal(failed.html.dataset.theme, 'light'); assert(failed.root.textContent.includes('尚未确认')); assert(failed.control('theme').disabled);
  console.log('PASS Save failure: original confirmed value restored, error visible');

  const late = deferred(); const l = setup((route) => isInventoryRoute(route) ? management : late.promise); l.instance.show('settings', 'appearance'); assert(l.control('theme').disabled); l.instance.applyPreferences({ ...defaults, theme: 'dark' }); late.resolve({ ...defaults, theme: 'light' }); await settle(); assert.equal(l.instance.preferences().theme, 'dark'); assert.equal(l.html.dataset.theme, 'dark');
  console.log('PASS Preference load race: late load cannot overwrite preferences already applied');

  const revisionA = 'a'.repeat(32), revisionB = 'b'.repeat(32), revisionC = 'c'.repeat(32);
  const metadata = setup((route, options) => isInventoryRoute(route) ? management : options.method === 'POST' ? { ...defaults, theme: JSON.parse(options.body).patch.theme, revision: revisionB, chat_backends: { fixture: 'hermes' } } : { ...defaults, revision: revisionA, chat_backends: { fixture: 'codex' } });
  await metadata.instance.loadPreferences(); assert.equal(metadata.instance.preferences().revision, revisionA); assert.equal(metadata.instance.preferences().chat_backends.fixture, 'codex'); assert.equal(metadata.emitted.at(-1).revision, revisionA); metadata.instance.show('settings', 'appearance'); await settle(); metadata.control('theme').value = 'dark'; metadata.control('theme').fire('change'); await settle(); assert.equal(metadata.instance.preferences().revision, revisionB); assert.equal(metadata.emitted.at(-1).chat_backends.fixture, 'codex'); const postedMetadata = JSON.parse(metadata.calls.find(c => c.options.method === 'POST').options.body).patch; assert.deepEqual(Object.keys(postedMetadata), ['theme']);
  console.log('PASS Preference metadata load/save: revision and per-chat choices reach callbacks, never enter editable patches');

  metadata.instance.applyPreferences({ revision: revisionC, chat_backends: { fixture: 'codex', 'other-chat': 'hermes' } }); const copied = metadata.instance.preferences(); copied.chat_backends.fixture = 'hermes'; assert.equal(metadata.instance.preferences().chat_backends.fixture, 'codex'); assert.equal(metadata.emitted.at(-1).revision, revisionC); assert.equal(metadata.emitted.at(-1).chat_backends['other-chat'], 'hermes');
  console.log('PASS Preference metadata apply: external per-chat choices preserved, returned maps are copied');

  metadata.instance.applyPreferences({ revision: 'invalid-revision', chat_backends: Object.fromEntries([['../secret', 'codex'], ['invalid-backend', 'unknown'], ...Array.from({ length: 600 }, (_, i) => ['valid-' + i, 'hermes'])]), favorites: Array.from({ length: 90 }, (_, i) => 'local/codex/model-' + i) }); assert.equal(metadata.instance.preferences().revision, revisionC); assert.equal(Object.keys(metadata.instance.preferences().chat_backends).length, 512); assert(!Object.hasOwn(metadata.instance.preferences().chat_backends, '../secret')); assert(!Object.hasOwn(metadata.instance.preferences().chat_backends, 'invalid-backend')); assert.equal(metadata.instance.preferences().favorites.length, 64);
  console.log('PASS Preference metadata bounds: revision syntax, safe IDs/backends, maximum mappings/favorites');

  const staleSave = deferred(); const concurrent = setup((route, options) => isInventoryRoute(route) ? management : options.method === 'POST' ? staleSave.promise : { ...defaults, revision: revisionA, chat_backends: { fixture: 'codex' } }); concurrent.instance.show('settings', 'appearance'); await settle(); concurrent.control('theme').value = 'dark'; concurrent.control('theme').fire('change'); concurrent.instance.applyPreferences({ revision: revisionC, chat_backends: { fixture: 'hermes' }, chat_models: {fixture: 'local/hermes/Pro'}, default_models: {codex:'',hermes:'local/hermes/Pro'} }); staleSave.resolve({ ...defaults, default_models:{codex:'',hermes:''}, theme: 'dark', revision: revisionB, chat_backends: { fixture: 'codex' } }); await settle(); assert.equal(concurrent.instance.preferences().revision, revisionC); assert.equal(concurrent.instance.preferences().chat_backends.fixture, 'hermes'); assert.equal(concurrent.instance.preferences().chat_models.fixture, 'local/hermes/Pro'); assert.equal(concurrent.instance.preferences().default_models.hermes,'local/hermes/Pro');
  console.log('PASS Preference metadata race: old settings save cannot replace newer per-chat selection/revision');

  const broken = setup(() => Promise.reject(Error('synthetic offline'))); broken.instance.show('management'); await settle(); assert(broken.root.textContent.includes('连接状态暂时读不到')); assert(broken.find('重试读取设置')); broken.instance.show('settings', 'chat'); assert(broken.control('theme').disabled);
  console.log('PASS Read failures: retry offered, controls disabled until saved preferences are known');

  assert(t.calls.every(c => c.route === '/api/preferences' || c.route === '/api/management/settings' || c.route === '/api/management?refresh=1' || c.route === CACHE_PATH)); for (const c of t.calls.filter(c => c.options.method === 'POST')) { const body = JSON.parse(c.options.body); assert.deepEqual(Object.keys(body), c.route==='/api/management/settings'?['expected_revision','patch']:['patch']); assert(Object.keys(body.patch).every(k => Object.hasOwn(defaults, k))); }
  console.log('PASS API boundary: preferences/management only, exact supported patch fields, no chat/model/history request');

  const bilingual = setup(); bilingual.instance.show('settings', 'appearance'); await settle(); const languageCalls = bilingual.calls.length; bilingual.control('language').value = 'en'; bilingual.control('language').fire('change');
  assert.equal(bilingual.calls.length, languageCalls); assert.equal(bilingual.workspace.language(), 'en'); assert.equal(bilingual.workspace.translate('对话'), 'Chats'); assert.equal(bilingual.workspace.translate('小白兔专员'), 'Rabbit specialist'); assert.equal(bilingual.workspace.translate('PRIVATE synthetic untouched'), 'PRIVATE synthetic untouched'); assert.equal(bilingual.root.getAttribute('lang'), 'en'); assert.equal(bilingual.root.all(n=>n.tagName==='H2').length,0); assert(bilingual.root.textContent.includes('Interface language')); assert(!bilingual.root.textContent.includes('Saved in this browser only')); assert.deepEqual(bilingual.languageChanges, ['zh-CN', 'en']);
  const resumedLanguage = setup(null, { storage: bilingual.storage }); resumedLanguage.instance.show('settings', 'appearance'); await settle(); assert.equal(resumedLanguage.control('language').value, 'en'); assert.equal(resumedLanguage.workspace.translate('主题'), 'Theme'); resumedLanguage.control('language').value = 'zh-CN'; resumedLanguage.control('language').fire('change'); assert(resumedLanguage.root.textContent.includes('主题')); assert.equal(resumedLanguage.workspace.translate('主题'), '主题'); assert.equal(resumedLanguage.storage.get('rabbit.ui.language.v1'), 'zh-CN');
  console.log('PASS Partial languages: explicit fixed UI labels, browser-only persistence, reload and Chinese return, no API calls or unknown preference fields');

  const invalidLanguage = setup(null, {storage: new Map([['rabbit.ui.language.v1', '<script>']])}); invalidLanguage.instance.show('settings', 'appearance'); await settle(); assert.equal(invalidLanguage.workspace.language(), 'zh-CN'); invalidLanguage.control('language').value = 'other'; invalidLanguage.control('language').fire('change'); assert.equal(invalidLanguage.workspace.language(), 'zh-CN'); const blockedLanguage = setup(null, {blockStorage: true}); blockedLanguage.instance.show('settings', 'appearance'); await settle(); const blockedCalls = blockedLanguage.calls.length; blockedLanguage.control('language').value = 'en'; blockedLanguage.control('language').fire('change'); assert.equal(blockedLanguage.workspace.language(), 'en'); assert(blockedLanguage.root.textContent.includes('Browser storage is unavailable')); assert.equal(blockedLanguage.calls.length, blockedCalls); assert.equal(blockedLanguage.root.all(n => n.tagName === 'SCRIPT').length, 0);
  console.log('PASS Language boundary: invalid values refused, blocked preview storage permits page-only switch with truthful retention warning');

  bilingual.instance.show('management', 'agents'); await settle(); assert(bilingual.root.textContent.includes('未接入 <script>')); assert.equal(bilingual.root.all(n=>n.tagName==='IMG').length, 0); bilingual.instance.show('settings', 'appearance'); assert(bilingual.control('theme').dataset.controlInterface==='ai'); assert.equal(bilingual.control('language').dataset.controlInterface,undefined); const namedLikeLabel = setup(route => isInventoryRoute(route) ? {...management,agents:[{...management.agents[0],name:'设置'}]} : {...defaults}, {storage:new Map([['rabbit.ui.language.v1','en']])}); namedLikeLabel.instance.show('management','agents'); await settle(); assert.equal(namedLikeLabel.root.all(n=>n.tagName==='H3')[0].textContent, '设置');
  console.log('PASS User names: exact label-like Agent name remains Chinese in English UI, safe text unchanged, no future specialist or AI-editable capability marker');

  const relationCatalog = { ...management, agents: [...management.agents, {id:'pi',name:'Pi',available:true}], models: [
    {id:'local/codex/Shared',agent_id:'codex',name:'同名 <img>',model:'Shared',provider:'Provider A',available:true,status:'verified'},
    {id:'local/hermes/Shared',agent_id:'hermes',name:'同名 <img>',model:'Shared',provider:'Provider A',available:false},
    {id:'local/codex/SharedAlias',agent_id:'codex',name:'同名 <img>',model:'Shared',provider:'Provider A',available:false},
    {id:'local/pi/Shared',agent_id:'pi',name:'同名 <img>',model:'Shared',provider:'Provider B',available:true},
    {id:'local/pi/Other',agent_id:'pi',name:'同名 <img>',model:'Other',provider:'Provider A',available:true},
    {id:'local/codex/Missing',agent_id:'codex',name:'无提供方',model:'Shared',available:true},
    {id:'local/hermes/Missing',agent_id:'hermes',name:'无提供方',model:'Shared',available:true},
    {id:'local/pi/Case',agent_id:'pi',name:'小写提供方',model:'Shared',provider:'provider a',available:true},
    {id:'local/hermes/Shared',agent_id:'hermes',name:'同名 <img>',model:'Shared',provider:'Provider A',available:false},
  ]};
  const relations = setup(route => isInventoryRoute(route) ? relationCatalog : {...defaults}); relations.instance.show('management', 'agents'); await settle(); const relationCalls = relations.calls.length; const codexCard = relations.root.all(n=>n.tagName==='ARTICLE' && n.all(c=>c.tagName==='H3' && c.textContent==='Codex').length)[0]; const codexDetails = codexCard.all(n=>n.tagName==='DETAILS')[0]; assert.equal(codexDetails.dataset.controlInterface,'ai'); assert(codexDetails.textContent.includes('模型（3）')); assert.equal(codexDetails.open, undefined); assert(codexDetails.textContent.includes('接入检查通过')); codexDetails.open = true; assert.equal(relations.calls.length, relationCalls);
  relations.instance.show('management', 'models');let relationCards=relations.root.all(n=>n.tagName==='ARTICLE');assert.equal(relationCards.length,8);
  const matching=relationCards.find(n=>n.dataset.modelId==='local/codex/Shared'),matchingAgents=matching.all(n=>n.className==='rbw-model-agents')[0];assert.equal(matchingAgents.all(n=>n.tagName==='LI').length,1);assert(!matchingAgents.textContent.includes('Hermes'));assert(matchingAgents.textContent.includes('Codex · 接入检查通过'));assert(matching.textContent.includes('1/1 条通路接入检查通过'));assert.equal(matching.all(n=>n.className==='rbw-state rbw-online').length,1);assert.equal(matching.all(n=>n.className.includes('rbw-unavailable')).length,0);assert(!matchingAgents.textContent.includes('Pi'));assert.equal(matching.all(n=>n.tagName==='DETAILS').length,0);assert.equal(relations.calls.length,relationCalls);assert.equal(relations.selectCalls.length,0);assert.equal(relations.root.all(n=>n.tagName==='IMG'||n.tagName==='SCRIPT').length,0);
  for(const modelId of ['local/codex/Missing','local/hermes/Missing','local/pi/Shared','local/pi/Other','local/pi/Case'])assert.equal(relationCards.find(n=>n.dataset.modelId===modelId).all(n=>n.className==='rbw-model-agents')[0].all(n=>n.tagName==='LI').length,1);
  const relationSearch = relations.root.all(n=>n.tagName==='INPUT'&&n.type==='search')[0]; relationSearch.value='Provider B'; relationSearch.fire('input'); assert.equal(relations.root.all(n=>n.tagName==='ARTICLE').length, 1); assert.equal(relations.calls.length, relationCalls);
  console.log('PASS Catalog relations: exact IDs remain separate across same names/provider/model; only duplicate IDs collapse, with safe text and local search');

  const added = []; const add = setup(null, {onAddResource: async kind => {added.push(kind);}}); add.instance.show('management','agents'); await settle(); const addCalls = add.calls.length; assert(add.find('接入说明').getAttribute('title').includes('手动接入指南')); add.find('接入说明').fire('click'); await settle(); add.instance.show('management','models'); add.find('接入说明').fire('click'); await settle(); assert.deepEqual(added, ['agent','model']); assert.equal(add.calls.length, addCalls); assert.equal(add.selectCalls.length, 0); const noAdd = setup(); noAdd.instance.show('management','agents'); await settle(); noAdd.find('接入说明').fire('click'); assert(noAdd.root.textContent.includes('尚未配置')); assert(noAdd.root.textContent.includes('原生安装与连接检测')); assert(!noAdd.root.textContent.includes('小白兔专员')); assert(noAdd.root.all(n=>n.getAttribute('role')==='status')[0].className.includes('rbw-error')); const failedAdd = setup(null, {onAddResource: async () => {throw Error('synthetic failure');}}); failedAdd.instance.show('management','models'); await settle(); failedAdd.find('接入说明').fire('click'); await settle(); assert(failedAdd.root.textContent.includes('配置指导暂时打不开')); assert.equal(failedAdd.selectCalls.length, 0);
  console.log('PASS Add-resource entry: explicit guidance callbacks only, absent/failed handler truthful, no installation, API request, agent selection or claimed successful connection');
  const groupedInventory = deferred(), manualTopics = [];
  const grouped = setup(route => isInventoryRoute(route) ? groupedInventory.promise : {...defaults}, {onManual: topic => manualTopics.push(topic), onAddResource: async () => {}});
  grouped.instance.show('management', 'agents');
  function assertAgentActionsGrouped() {
    const toolbars = grouped.root.all(node => node.className.split(/\s+/).includes('rbw-toolbar'));
    assert.equal(toolbars.length, 1);
    const group = toolbars[0];
    assert(group.className.split(/\s+/).includes('rbw-action-group'));
    assert(group.children.includes(grouped.find('使用手册')));
    assert(group.children.includes(grouped.find('接入说明')));
    assert(group.children.some(node => node.tagName === 'BUTTON' && ['刷新检测', '正在检测…'].includes(node.textContent)));
  }
  assertAgentActionsGrouped(); grouped.find('使用手册').fire('click'); assert.deepEqual(manualTopics, ['ai']);
  groupedInventory.resolve(management); await settle(); assertAgentActionsGrouped();
  assert.equal(grouped.root.all(node => node.tagName === 'BUTTON' && node.textContent === '接入说明').length, 1);
  assert(grouped.calls.every(call => !call.options.method));
  console.log('PASS Agent actions: refresh, manual and add share one compact toolbar before and after inventory; manual stays usable while loading');
  const inventoryGate = deferred(), workRoot = new Node('section');
  let workVisible = false, externalRefreshes=0;
  const workView = {root:workRoot, show:()=>{assert(workHost.root.all(n=>n===workRoot).length===1);workVisible=true;}, hide:()=>{workVisible=false;},refreshExternal:()=>{externalRefreshes++;return Promise.resolve(false);}};
  const workHost = setup(route => isInventoryRoute(route) ? inventoryGate.promise : {...defaults}, {workStatus:workView});
  workHost.instance.show('management','agents');
  assert(workVisible); assert(workHost.root.all(n=>n===workRoot).length===1);
  workHost.instance.show('settings','general'); assert(!workVisible); assert(!workHost.root.all(n=>n===workRoot).length);
  inventoryGate.resolve(management); await settle(); assert(!workVisible);
  workHost.instance.show('management','agents'); await settle(); assert(workVisible); assert(workHost.root.all(n=>n===workRoot).length===1);
  assert.equal(externalRefreshes,0);workHost.instance.selectChanged();assert.equal(externalRefreshes,0);
  const beforeManual=workHost.calls.filter(c=>c.route==='/api/management?refresh=1').length;workHost.find('刷新检测').fire('click');await settle();assert.equal(externalRefreshes,1);assert.equal(workHost.calls.filter(c=>c.route==='/api/management?refresh=1').length,beforeManual+1);
  workHost.instance.show('management','models'); assert(!workVisible);
  workHost.instance.show('management','agents'); assert(workVisible);
  workHost.instance.hide(); assert(!workVisible); assert(workHost.root.hidden);
  assert(workHost.calls.every(call=>['/api/preferences','/api/management/settings','/api/management?refresh=1',CACHE_PATH].includes(call.route)));
  console.log('PASS Work view lifecycle: mounts before inventory, leaves on tab/page/hide, delayed inventory never restarts hidden observations');
  const managementRefresh=deferred(),externalRefresh=deferred();let loads=0,externalStarts=0;
  const concurrentWork={root:new Node('section'),show(){},hide(){},refreshExternal(){externalStarts++;return externalRefresh.promise;}};
  const concurrentWorkHost=setup(route=>isInventoryRoute(route)?(++loads===1?management:managementRefresh.promise):{...defaults},{workStatus:concurrentWork});
  concurrentWorkHost.instance.show('management','agents');await settle();assert.equal(externalStarts,0);
  concurrentWorkHost.find('刷新检测').fire('click');assert.equal(externalStarts,1);assert.equal(loads,2);concurrentWorkHost.instance.show('management','models');
  managementRefresh.resolve(management);externalRefresh.reject(Error('Invented external failure.'));await settle();assert.equal(externalStarts,1);assert.equal(concurrentWorkHost.instance.getSection().tab,'models');
  console.log('PASS External refresh: only an explicit Agent refresh starts it, in parallel with inventory; unrelated renders and late results never trigger another refresh');
  let letterReads = 0;
  const about = setup(undefined, { fetch: async (url, init) => {
    assert.equal(url, '/old-letter.md'); assert.equal(init.mode, 'same-origin');
    letterReads++;
    return { ok: letterReads > 1, text: async () => '# 一封旧信\n\n虚构的信。' };
  } });
  about.instance.show('settings', 'about'); await settle();
  const letter = about.root.all(n => n.tagName === 'DETAILS' && n.textContent === '一封旧信')[0];
  assert(letter); assert.equal(letterReads, 0);
  letter.open = true; letter.fire('toggle'); await settle();
  assert(letter.textContent.includes('收起后重新展开即可重试'));
  letter.open = false; letter.fire('toggle'); letter.open = true; letter.fire('toggle'); await settle();
  assert.equal(letter.textContent, '一封旧信虚构的信。'); assert.equal(letterReads, 2);
  about.instance.selectChanged(); assert(about.root.all(n => n === letter).length); assert(letter.open);
  letter.open = false; letter.fire('toggle'); letter.open = true; letter.fire('toggle'); await settle();
  assert.equal(letterReads, 2); assert.equal(about.instance.getSection().tab, 'about');
  console.log('PASS About: expanded local letter survives rerender, retries once on reopen, and needs no chat navigation');
  // Blueprint 2.0 object pages use fixed identities and saved/cache reads.
  const scopeA = 'a'.repeat(64), scopeB = 'b'.repeat(64), chatId = '11111111-1111-4111-8111-111111111111';
  const historyId = '22222222-2222-4222-8222-222222222222';
  let detailScope = scopeA;
  const workActions = [], filterChanges = [], liveRoot = new Node('section');
  const activeOverview = { workspace_id: scopeA, agents: [{id:'codex',status:'running',status_label:'进行中'}], works: [
    {key:'native:codex:'+chatId,name:'虚构工作 <script>',agent:'codex',status_label:'可继续',detail:'应用管理的原生对话',actions:[
      {kind:'read',label:'阅读原文',target:{kind:'native',agent:'codex',id:chatId,read_only:true}},
      {kind:'continue',label:'继续这项工作',target:{kind:'native',agent:'codex',id:chatId,read_only:false}}]},
    {key:'history:codex:'+historyId,name:'虚构旧稿',agent:'codex',status_label:'仅可阅读',detail:'已有索引',actions:[
      {kind:'read',label:'阅读旧稿',target:{kind:'history',agent:'codex',session_id:historyId,read_only:true}},
      {kind:'original',label:'在原应用打开',url:'codex://threads/'+historyId,target:{kind:'history',agent:'codex',session_id:historyId}}]},
    {key:'history:hermes:opaque',name:'别的 Agent 工作',agent:'hermes',actions:[]}
  ]};
  const detailHost = setup(route => isInventoryRoute(route) ? relationCatalog : {...defaults}, {
    getWorkspace:()=>detailScope, getOverview:()=>activeOverview,
    onOpenWork:async (value,isCurrent)=>workActions.push({value,isCurrent}),
    workStatus:{root:liveRoot,show(){},hide(){},setAgentFilter(value){filterChanges.push(value);}}
  });
  assert.equal(await detailHost.instance.openObject({kind:'agent',id:'codex'}),true);
  assert.equal(detailHost.instance.getObjectTitle(),'Codex');
  assert.equal(detailHost.root.all(n=>n.className==='rbw-agent-detail').length,1);
  assert(detailHost.find('所有 Agent')); assert(detailHost.root.textContent.includes('虚构工作 <script>'));
  assert(!detailHost.root.textContent.includes('别的 Agent 工作'));
  assert(detailHost.root.textContent.includes('进行中'));
  assert.equal(detailHost.root.all(n=>n===liveRoot).length,1); assert.equal(filterChanges.at(-1),'codex');
  assert.equal(detailHost.calls.filter(c=>c.route==='/api/management?refresh=1').length,0);
  assert.equal(detailHost.calls.filter(c=>c.route===CACHE_PATH).length,1);
  const evidence = detailHost.root.all(n=>n.className==='rbw-more rbw-agent-evidence')[0];
  assert(evidence && !evidence.open); assert(evidence.textContent.includes('本次打开没有发起核对'));
  assert.equal(detailHost.root.all(n=>n.tagName==='SCRIPT'||n.tagName==='IMG').length,0);
  const exactRead = detailHost.find('阅读原文'); exactRead.fire('click'); await settle();
  assert.equal(workActions.length,1); assert.equal(workActions[0].value.target.id,chatId); assert.equal(workActions[0].value.target.read_only,true);
  assert.equal(workActions[0].isCurrent(),true);
  detailHost.find('阅读旧稿').fire('click'); await settle(); assert.equal(workActions[1].value.target.session_id,historyId);
  const nativeLink=detailHost.root.all(n=>n.tagName==='A')[0]; assert.equal(nativeLink.href,'codex://threads/'+historyId);
  const modelChip = detailHost.root.all(n=>n.tagName==='BUTTON'&&n.dataset.modelId==='local/codex/SharedAlias')[0];
  assert(modelChip); modelChip.fire('click'); await settle();
  assert.equal(detailHost.instance.getObject().id,'local/codex/SharedAlias');
  assert.equal(detailHost.root.all(n=>n.tagName==='ARTICLE').length,1);
  assert.equal(detailHost.root.all(n=>n.tagName==='ARTICLE')[0].dataset.modelId,'local/codex/SharedAlias');
  exactRead.fire('click'); await settle(); assert.equal(workActions.length,2); assert.equal(workActions[0].isCurrent(),false);
  assert.equal(detailHost.calls.filter(c=>c.route===CACHE_PATH).length,1,'object navigation reuses cached inventory');
  await detailHost.instance.openObject({kind:'agent',id:'codex'}); const oldScopeRead=detailHost.find('阅读原文');
  detailScope=scopeB; oldScopeRead.fire('click'); await settle(); assert.equal(workActions.length,2); assert.equal(detailHost.instance.getObjectTitle(),'');
  console.log('PASS Agent detail: exact models and work actions, filtered live view, safe text, dated evidence collapsed, no passive probe and no old-target dispatch');

  let backToHome = 0;
  const fromDashboard = setup(route => isInventoryRoute(route) ? relationCatalog : defaults, {
    getOverview: () => ({ ...activeOverview, works: [] }),
    projectOverview: () => activeOverview,
    onBackToMyAI: () => { backToHome++; },
  });
  assert.equal(await fromDashboard.instance.openObject({ kind: 'agent', id: 'codex', fromMyAI: true }), true);
  assert(fromDashboard.root.textContent.includes('虚构旧稿'));
  assert(fromDashboard.find('← 我的 AI'));
  assert.equal(await fromDashboard.instance.openObject({ kind: 'agent', id: 'codex', section: 'connection', fromMyAI: true, backTo: 'usage' }), true);
  assert.equal(fromDashboard.instance.getObjectTitle(), '接入说明');
  assert(fromDashboard.root.textContent.includes('在 Codex 原应用确认本人账号已登录'));
  assert(fromDashboard.root.textContent.includes('回到额度与用量后'));
  assert.equal(fromDashboard.find('刷新检测'), undefined);
  fromDashboard.find('← 额度与用量').fire('click'); assert.equal(backToHome, 1);
  assert.equal(fromDashboard.calls.filter(c => c.route === '/api/management?refresh=1').length, 0);
  console.log('PASS Dashboard details: work reads from its own complete directory and Codex connection returns to the usage page');

  const targetGate = deferred();
  const delayedTargets = setup(route=>isInventoryRoute(route)?targetGate.promise:{...defaults});
  const firstTarget=delayedTargets.instance.openObject({kind:'agent',id:'codex'});
  const secondTarget=delayedTargets.instance.openObject({kind:'agent',id:'hermes'});
  targetGate.resolve(management); assert.equal(await firstTarget,false); assert.equal(await secondTarget,true);
  assert.equal(delayedTargets.instance.getObjectTitle(),'Hermes');
  assert.equal(delayedTargets.root.all(n=>n.className==='rbw-agent-detail')[0].dataset.agentId,'hermes');
  assert.equal(delayedTargets.calls.filter(c=>c.route===CACHE_PATH).length,1);
  const oldScopeGate=deferred(); let movingScope=scopeA, cacheReads=0;
  const moving=setup(route=>isInventoryRoute(route)?(++cacheReads===1?oldScopeGate.promise:management):{...defaults},{getWorkspace:()=>movingScope});
  const obsolete=moving.instance.openObject({kind:'agent',id:'codex'}); movingScope=scopeB;
  oldScopeGate.resolve(management); assert.equal(await obsolete,false); assert.equal(moving.instance.getObjectTitle(),'');
  assert.equal(await moving.instance.openObject({kind:'agent',id:'hermes'}),true); assert.equal(moving.instance.getObjectTitle(),'Hermes');
  assert.equal(cacheReads,2);
  console.log('PASS Object races: late Agent target cannot replace a newer object; workspace change discards old response and reads its own directory');

  const passiveSettings=setup();passiveSettings.instance.show('settings','about');await settle();
  assert(!passiveSettings.calls.some(c=>isInventoryRoute(c.route)),'opening About neither checks nor reads unused Agent directories');
  const manualRefresh=setup();manualRefresh.instance.show('management','agents');await settle();
  assert.equal(manualRefresh.calls.filter(c=>c.route==='/api/management?refresh=1').length,0);
  manualRefresh.find('刷新检测').fire('click');await settle();
  assert.equal(manualRefresh.calls.filter(c=>c.route==='/api/management?refresh=1').length,1);
  console.log('PASS Passive management: fixed cache endpoint only; original refresh detection remains an explicit user action');
  console.log('All synthetic workspace checks passed.');

})().catch(error => { console.error(error); process.exitCode = 1; });
