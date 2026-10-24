(() => {
  "use strict";
  const VERSION = 1, MAX_ITEMS = 100, MAX_TEXT = 30000, MAX_CHARACTERS = 512 * 1024;
  const MAX_RAW = 2 * 1024 * 1024, MAX_LANES = 8, RETENTION = 30 * 86400000;
  const agents = window.RabbitAgents, pages = new Set(["conversations", "management", "settings", "specialist", "native-chats"]);
  const forbidden = new Set(["__proto__", "prototype", "constructor"]);
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  function invalid() { throw new Error("invalid-browser-snapshot"); }
  function object(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || value === Object.prototype) invalid();
    const constructor = Object.getOwnPropertyDescriptor(value, "constructor");
    if (constructor && typeof constructor.value === "function" && constructor.value.prototype === value) invalid();
    return value;
  }
  function text(value, maximum, empty = true) { if (typeof value !== "string" || value.length > maximum || (!empty && !value.length) || /[\u0000]/.test(value)) invalid(); return value; }
  function bodyText(value, empty = true) {
    // The service's single-body limit is in Unicode code points. The aggregate
    // browser budget below still counts UTF-16 units, including repeated copies.
    const result = text(value, MAX_TEXT * 2, empty); if (Array.from(result).length > MAX_TEXT) invalid(); return result;
  }
  function id(value, maximum = 128) { const result = text(value, maximum, false); if (!/^[A-Za-z0-9_.:-]+$/.test(result) || forbidden.has(result)) invalid(); return result; }
  function opaqueId(value) {
    // The service accepts 256 Unicode code points, rather than only ASCII or
    // 256 UTF-16 units. These identifiers remain data in JSON and Map keys.
    const result = text(value, 512, false); if (Array.from(result).length > 256) invalid(); return result;
  }
  function attachmentIds(value) {
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.length > 4 || value.some(id => typeof id !== "string" || !/^img_[a-f0-9]{32}$/.test(id)) || new Set(value).size !== value.length) throw new Error("图片材料标识不完整，原草稿已保留。");
    return [...value];
  }
  function taskIntent(value) {
    if (value === undefined) return { mode: "chat", artifact_ids: [] };
    object(value);
    if (!["chat", "files"].includes(value.mode) || !Array.isArray(value.artifact_ids) || value.artifact_ids.length > 4 || value.artifact_ids.some(value => typeof value !== "string" || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value)) || new Set(value.artifact_ids).size !== value.artifact_ids.length) invalid();
    return { mode: value.mode, artifact_ids: [...value.artifact_ids] };
  }
  function memoryProfile(value, strict = false) {
    const memory = object(value), fields = memory.id === "none" ? ["id"] : ["id", "revision", "name", "description", "habits", "program", "connection_id", "mode", "bank_ref"];
    if (strict && (Object.keys(memory).length !== fields.length || fields.some(field => !own(memory, field)))) invalid();
    if (memory.id === "none") return { id: "none" };
    const name = text(memory.name, 80, false), description = text(memory.description, 400), habits = text(memory.habits, 8000);
    if (!name.trim() || Array.from(name).length > 40 || /[\x00-\x1f\x7f]/.test(name) || Array.from(description).length > 200 || Array.from(habits).length > 4000 || memory.program !== "hindsight" || memory.connection_id !== "local/hindsight" || !["read_only", "read_write"].includes(memory.mode) || typeof memory.bank_ref !== "string" || !/^mem_[a-f0-9]{32}$/.test(memory.bank_ref)) invalid();
    return { id: sharedId(memory.id), revision: sharedRevision(memory.revision), name, description, habits, program: "hindsight", connection_id: "local/hindsight", mode: memory.mode, bank_ref: memory.bank_ref };
  }
  function profileSnapshot(value, budget) {
    if (value == null) return null;
    object(value); const ai = object(value.ai_profile), memory = object(value.memory_profile);
    if (![1, 2].includes(value.schema_version) || value.schema_version === 1 && memory.id !== "none" || !Array.isArray(value.used_memory_references) || value.used_memory_references.length) invalid();
    const name = text(ai.name, 80, false); if (!name.trim() || Array.from(name).length > 40 || /[\x00-\x1f\x7f]/.test(name)) invalid();
    if (!agents.isInstance(ai.agent_instance_id) || ai.device_id !== "local" || ai.reasoning_effort !== null && !["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"].includes(ai.reasoning_effort)) invalid();
    const selectedMemory = memoryProfile(memory);
    const result = { schema_version: value.schema_version, ai_profile: { id: sharedId(ai.id), revision: sharedRevision(ai.revision), name, agent_instance_id: ai.agent_instance_id, device_id: "local", model_id: modelId(ai.model_id, agents.backendOfInstance(ai.agent_instance_id)), reasoning_effort: ai.reasoning_effort }, memory_profile: selectedMemory, used_memory_references: [] };
    if (budget) { budget.characters += JSON.stringify(result).length; if (budget.characters > MAX_CHARACTERS) invalid(); }
    return result;
  }
  function profileDraftKey(key) {
    if (key === "__new__") return key;
    if (typeof key === "string" && key.startsWith("history:")) return "history:" + sourceKey(key.slice(8));
    if (typeof key === "string" && key.startsWith("relay:")) return "relay:" + sharedId(key.slice(6));
    const result = opaqueId(key); if (forbidden.has(result)) invalid(); return result;
  }
  function backend(value) { if (!agents.has(value)) invalid(); return value; }
  function modelId(value, selectedBackend) {
    if (value === undefined) return "";
    if (!agents.validModelId(value, selectedBackend)) invalid();
    return value;
  }
  function sourceKey(value) {
    const result = text(value, 526, false), separator = result.indexOf(":");
    backend(result.slice(0, separator)); opaqueId(result.slice(separator + 1)); return result;
  }
  function integer(value, maximum) { if (!Number.isSafeInteger(value) || value < 0 || value > maximum) invalid(); return value; }
  function sharedId(value) {
    const result = text(value, 36, false);
    if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(result)) invalid();
    return result;
  }
  function sharedRevision(value) { if (!Number.isSafeInteger(value) || value < 1) invalid(); return value; }
  function sharedTopic(value) {
    const result = text(value, 540, false);
    if (result.startsWith("new:")) sharedId(result.slice(4));
    else if (result.startsWith("chat:")) opaqueId(result.slice(5));
    else if (result.startsWith("history:")) sourceKey(result.slice(8));
    else invalid();
    return result;
  }
  function sharedTarget(value, topicKey) {
    const target = object(value);
    if (!own(target, "type")) invalid();
    if (target.type === "new") {
      if (!topicKey.startsWith("new:")) invalid();
      return { type: "new" };
    }
    if (target.type === "chat") {
      const chatId = opaqueId(target.id);
      if (topicKey.startsWith("chat:") && topicKey !== `chat:${chatId}`) invalid();
      return { type: "chat", id: chatId };
    }
    if (target.type === "history") {
      const agent = backend(target.agent), sessionId = opaqueId(target.session_id);
      if (topicKey !== `history:${agent}:${sessionId}`) invalid();
      return { type: "history", agent, session_id: sessionId };
    }
    invalid();
  }
  const retiredFields = ["sharedNewTopicId", "sharedDraftBindings", "sharedDraftPending", "sharedLocalTopics", "sharedLocalRoutes"];
  function retireSharedDrafts(input, result) {
    // Only keys already present in this browser are eligible. The old server
    // baseline and unconfirmed shared submissions never become sendable drafts.
    if (!Array.isArray(input.sharedDraftBindings) || input.sharedDraftBindings.length > MAX_ITEMS) return;
    const pending = new Set(), localTopics = new Set();
    if (own(input, "sharedDraftPending")) {
      if (!Array.isArray(input.sharedDraftPending) || input.sharedDraftPending.length > MAX_ITEMS) return;
      try { for (const entry of input.sharedDraftPending) pending.add(sharedId(object(entry).draftId)); } catch (_) { return; }
    }
    if (own(input, "sharedLocalTopics")) {
      if (!Array.isArray(input.sharedLocalTopics) || input.sharedLocalTopics.length > MAX_ITEMS) return;
      try { for (const topic of input.sharedLocalTopics) localTopics.add(sharedTopic(topic)); } catch (_) { return; }
    }
    const targets = new Map(), sources = new Map();
    for (const raw of input.sharedDraftBindings) {
      try {
        const entry = object(raw), draft = sharedId(entry.draftId), topic = sharedTopic(entry.topicKey);
        if (localTopics.has(topic)) pending.add(draft);
        sharedRevision(entry.revision);
        if (pending.has(draft)) continue;
        const target = sharedTarget(entry.target, topic), key = target.type === "new" ? "__new__" : target.type === "history" ? `history:${target.agent}:${target.session_id}` : target.id;
        if (target.type === "chat" && (forbidden.has(key) || key === "__new__" || key.startsWith("history:") || key.startsWith("relay:"))) continue;
        if (!targets.has(draft)) targets.set(draft, new Set()); targets.get(draft).add(key);
        if (!sources.has(key)) sources.set(key, new Set()); sources.get(key).add(draft);
      } catch (_) { /* Bad retired metadata cannot discard ordinary local drafts. */ }
    }
    const groups = [result.drafts, result.draftAttachments, result.taskIntents, result.profileChoices.entries];
    for (const [draft, keys] of targets) {
      if (pending.has(draft) || keys.size !== 1) continue;
      const key = [...keys][0], relay = `relay:${draft}`;
      if (sources.get(key).size !== 1 || groups.some(entries => entries.some(([existing]) => existing === key))) continue;
      for (const entries of groups) for (const entry of entries) if (entry[0] === relay) entry[0] = key;
    }
  }
  function mapEntries(value, keyCheck, valueCheck, budget) {
    if (!Array.isArray(value) || value.length > MAX_ITEMS) invalid();
    const used = new Set();
    return value.map((entry) => {
      if (!Array.isArray(entry) || entry.length !== 2) invalid();
      const key = keyCheck(entry[0]); if (used.has(key)) invalid(); used.add(key);
      const result = valueCheck(entry[1]);
      if (budget) { budget.characters += result.length; if (budget.characters > MAX_CHARACTERS) invalid(); }
      return [key, result];
    });
  }
  function sanitize(input) {
    object(input);
    const result = { selection: { type: "new", id: null }, page: "conversations", backend: "codex", model_id: "", query: "", historyAgent: "all", historyOffset: 0, historyMatch: null, scrollTop: 0, drafts: [], draftAttachments: [], taskIntents: [], profileChoices: { version: 1, entries: [] }, historyChoices: { version: 1, entries: [] }, memoryChatCreation: { version: 1, pending: null }, newCreationId: null, historyCreationIds: [], historyBackendChoices: [], historyModelChoices: [], historyContinuationIds: [], pendingSubmissions: [], sourceImagePending: { version: 1, entries: [] } };
    const budget = { characters: 0 };
    if (own(input, "selection")) {
      const selected = object(input.selection); if (!own(selected, "type")) invalid();
      if (selected.type === "new") { if (own(selected, "id") && selected.id !== null) invalid(); }
      else if (selected.type === "chat") result.selection = { type: "chat", id: opaqueId(selected.id) };
      else if (selected.type === "history") {
        const agent = backend(selected.agent), sessionId = opaqueId(selected.sessionId), key = sourceKey(selected.id);
        if (key !== `${agent}:${sessionId}`) invalid(); result.selection = { type: "history", id: key, agent, sessionId };
      } else invalid();
    }
    if (own(input, "page")) { if (!pages.has(input.page)) invalid(); result.page = input.page; }
    if (own(input, "backend")) result.backend = backend(input.backend);
    result.model_id = modelId(input.model_id, result.backend);
    if (own(input, "query")) result.query = text(input.query, 400);
    if (own(input, "historyAgent")) { if (input.historyAgent !== "all" && !agents.has(input.historyAgent)) invalid(); result.historyAgent = input.historyAgent; }
    if (own(input, "historyOffset")) result.historyOffset = integer(input.historyOffset, 1000000);
    if (own(input, "historyMatch") && input.historyMatch !== null) {
      const match = object(input.historyMatch); result.historyMatch = { messageId: id(match.messageId, 512), index: integer(match.index, 1000000), query: text(match.query, 400) };
    }
    if (own(input, "scrollTop")) { if (!Number.isFinite(input.scrollTop) || input.scrollTop < 0 || input.scrollTop > 1000000000) invalid(); result.scrollTop = input.scrollTop; }
    if (own(input, "drafts")) result.drafts = mapEntries(input.drafts, (key) => {
      if (key === "__new__") return key;
      if (typeof key === "string" && key.startsWith("history:")) return "history:" + sourceKey(key.slice(8));
      if (typeof key === "string" && key.startsWith("relay:")) return "relay:" + sharedId(key.slice(6));
      const result = opaqueId(key); if (forbidden.has(result)) invalid(); return result;
    }, (value) => bodyText(value), budget);
    if (own(input, "draftAttachments")) result.draftAttachments = mapEntries(input.draftAttachments, key => {
      if (key === "__new__") return key;
      if (typeof key === "string" && key.startsWith("history:")) return "history:" + sourceKey(key.slice(8));
      if (typeof key === "string" && key.startsWith("relay:")) return "relay:" + sharedId(key.slice(6));
      const result = opaqueId(key); if (forbidden.has(result)) invalid(); return result;
    }, attachmentIds);
    if (own(input, "taskIntents")) result.taskIntents = mapEntries(input.taskIntents, profileDraftKey, taskIntent);
    if (own(input, "newCreationId") && input.newCreationId !== null) result.newCreationId = id(input.newCreationId, 100);
    if (own(input, "profileChoices")) {
      const choices = object(input.profileChoices); if (choices.version !== 1) invalid();
      result.profileChoices.entries = mapEntries(choices.entries, profileDraftKey, value => profileSnapshot(value, budget));
    }
    if (own(input, "memoryChatCreation")) {
      const journal = object(input.memoryChatCreation); if (journal.version !== 1) invalid();
      if (journal.pending !== null) { const entry = object(journal.pending), snap = profileSnapshot(entry.profile_snapshot, budget); if (!snap) invalid(); result.memoryChatCreation.pending = { clientId: id(entry.clientId, 100), chatId: entry.chatId === null ? null : opaqueId(entry.chatId), profile_snapshot: snap }; }
    }
    if (own(input, "historyCreationIds")) result.historyCreationIds = mapEntries(input.historyCreationIds, sourceKey, (value) => id(value, 100));
    if (own(input, "historyBackendChoices")) result.historyBackendChoices = mapEntries(input.historyBackendChoices, sourceKey, backend);
    if (own(input, "historyChoices")) {
      const choices = object(input.historyChoices);
      if (Object.keys(choices).length !== 2 || !own(choices, "version") || !own(choices, "entries") || choices.version !== 1) invalid();
      result.historyChoices.entries = mapEntries(choices.entries, sourceKey, raw => {
        const choice = object(raw), hasMemory = own(choice, "memory_profile");
        if (Object.keys(choice).length !== (hasMemory ? 3 : 2) || !own(choice, "mode") || !own(choice, "memory_explicit") || !["native", "text"].includes(choice.mode) || typeof choice.memory_explicit !== "boolean" || hasMemory && choice.memory_explicit !== true) invalid();
        const result = { mode: choice.mode, memory_explicit: choice.memory_explicit };
        if (hasMemory) { result.memory_profile = memoryProfile(choice.memory_profile, true); budget.characters += JSON.stringify(result.memory_profile).length; if (budget.characters > MAX_CHARACTERS) invalid(); }
        return result;
      });
    }
    if (own(input, "historyModelChoices")) result.historyModelChoices = mapEntries(input.historyModelChoices, sourceKey, (value) => modelId(value));
    const historicalBackends = new Map(result.historyBackendChoices);
    for (const [key, value] of result.historyModelChoices) modelId(value, historicalBackends.get(key) || key.slice(0, key.indexOf(":")));
    if (own(input, "historyContinuationIds")) result.historyContinuationIds = mapEntries(input.historyContinuationIds, sourceKey, id);
    if (own(input, "pendingSubmissions")) {
      if (!Array.isArray(input.pendingSubmissions) || input.pendingSubmissions.length > MAX_ITEMS) invalid();
      const used = new Set();
      result.pendingSubmissions = input.pendingSubmissions.map((inputPending) => {
        const pending = object(inputPending), chatId = id(pending.chatId);
        if (used.has(chatId)) invalid(); used.add(chatId);
        const content = bodyText(pending.text), images = attachmentIds(pending.attachment_ids); if (!content.trim() && !images.length) invalid();
        budget.characters += content.length; if (budget.characters > MAX_CHARACTERS) invalid();
        const snap = profileSnapshot(pending.profile_snapshot, budget);
        if (snap && (snap.ai_profile.agent_instance_id !== agents.instanceId(pending.backend) || snap.ai_profile.model_id !== (pending.model_id || ""))) invalid();
        const choiceRevision = pending.profile_selection_revision == null ? null : integer(pending.profile_selection_revision, Number.MAX_SAFE_INTEGER);
        return { chatId, text: content, backend: backend(pending.backend), model_id: modelId(pending.model_id, pending.backend), attachment_ids: images, task_intent: taskIntent(pending.task_intent), profile_snapshot: snap, profile_selection_revision: choiceRevision, clientId: id(pending.clientId, 100), uncertain: true };
      });
    }
    if (own(input, "sourceImagePending")) {
      const pending = object(input.sourceImagePending), used = new Set();
      if (pending.version !== 1 || !Array.isArray(pending.entries) || pending.entries.length > 20) invalid();
      result.sourceImagePending.entries = pending.entries.map(raw => {
        const entry = object(raw), chatId = opaqueId(entry.chatId), version = text(entry.plan_version, 64, false);
        if (used.has(chatId) || !/^[a-f0-9]{64}$/.test(version)) invalid(); used.add(chatId);
        return { chatId, plan_version: version, chat_revision: integer(entry.chat_revision, Number.MAX_SAFE_INTEGER) };
      });
    }
    retireSharedDrafts(input, result);
    return result;
  }
  function uuid() {
    const source = typeof crypto !== "undefined" ? crypto : window.crypto;
    try { if (source && typeof source.randomUUID === "function") { const value = source.randomUUID(); if (/^[A-Za-z0-9_-]{1,100}$/.test(value)) return value; } } catch (_) { /* A lane ID is not an authorization credential. */ }
    return `lane-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }
  function create(options = {}) {
    let local = null, session = null, prefix = "", sessionKey = "", laneKey = "", localAccessible = false, sessionAccessible = false, restored = false, notice = "", valid = true, retirement = null;
    const errors = new Set();
    function report(message) { notice = message; if (errors.has(message)) return; errors.add(message); try { if (typeof options.onError === "function") options.onError(message); } catch (_) { /* Reporting must never prevent chatting. */ } }
    try {
      if (typeof options.workspaceId !== "string" || !/^[a-f0-9]{64}$/.test(options.workspaceId)) invalid();
      const location = object(options.location), origin = text(location.origin, 300, false), parsed = new URL(origin);
      if (!["http:", "https:"].includes(parsed.protocol) || parsed.origin !== origin || parsed.username || parsed.password) invalid();
      const pathname = text(location.pathname || "/", 4096, false);
      const proxy = /^\/api\/server\/([A-Za-z0-9_-]{1,128})\/local-web(?:\/|$)/.exec(pathname);
      if (pathname.startsWith("/api/server/") && !proxy) invalid();
      prefix = `rabbit-browser-state:v${VERSION}:${options.workspaceId}:${encodeURIComponent(origin)}:${proxy ? proxy[1] : "direct"}:`;
      sessionKey = prefix + "session";
      laneKey = prefix + "lane:" + uuid();
      try { local = options.localStorage; localAccessible = !!local && typeof local.getItem === "function" && typeof local.setItem === "function"; } catch (_) { localAccessible = false; }
      try { session = options.sessionStorage; sessionAccessible = !!session && typeof session.getItem === "function" && typeof session.setItem === "function"; } catch (_) { sessionAccessible = false; }
    } catch (_) { valid = false; report("当前入口缺少有效的本机标识，刷新恢复暂时不可用；聊天仍可继续。"); }

    function status() { const mode = !valid ? "none" : localAccessible && sessionAccessible ? "full" : sessionAccessible ? "session" : localAccessible ? "local" : "none"; return { available: mode !== "none", mode, restored, notice }; }
    function degraded() {
      if (localAccessible && sessionAccessible) { notice = ""; return; }
      if (sessionAccessible) report("仅本标签的刷新恢复可用；本机恢复副本未保存，关闭页面后恢复可能失效。聊天仍可继续。");
      else if (localAccessible) report("仅本机恢复副本可用；刷新时可能恢复同一入口最近保存的页面。聊天仍可继续。");
      else report("浏览器没有保存这次状态，刷新恢复暂时不可用；请保留未发送文字，聊天仍可继续。");
    }
    function decode(raw) {
      if (typeof raw !== "string" || raw.length > MAX_RAW) invalid();
      const value = object(JSON.parse(raw));
      if (value.version !== VERSION || !Number.isSafeInteger(value.saved_at) || value.saved_at <= 0 || value.saved_at > Date.now() + 300000) invalid();
      if (Date.now() - value.saved_at > RETENTION) return null;
      return { saved_at: value.saved_at, data: sanitize(value.data), retired: retiredFields.some(key => own(value.data, key)) };
    }
    function preserveRetired() {
      if (!retirement) return true;
      try {
        const { storage, key, raw } = retirement, previous = storage.getItem(key);
        if (previous === null) storage.setItem(key, raw);
        if (storage.getItem(key) !== raw) throw Error("retired-copy-unconfirmed");
        retirement = null; return true;
      } catch (_) { report("旧共享状态尚未另存，原恢复副本已保留；暂不写入新的页面状态，避免覆盖未知记录。"); return false; }
    }
    function restoredValue(value, raw, storage, key) {
      if (value.retired) { retirement = { storage, key: key + ".retired-shared.v1", raw }; preserveRetired(); }
      restored = true; return value.data;
    }
    function localRecords() {
      const records = [];
      if (!local || !valid) return records;
      try {
        const length = local.length;
        if (!Number.isSafeInteger(length) || length < 0 || length > 10000) invalid();
        for (let i = 0; i < length; i++) {
          const key = local.key(i);
          if (typeof key !== "string" || !key.startsWith(prefix + "lane:") || !/^[A-Za-z0-9_-]{1,100}$/.test(key.slice((prefix + "lane:").length))) continue;
          try { const raw = local.getItem(key), parsed = decode(raw); if (parsed) records.push({ key, ...parsed, ...(parsed.retired ? { raw } : {}) }); else records.push({ key, expired: true, saved_at: 0 }); }
          catch (_) { report("有一份浏览器恢复副本无法读取，原数据已保留；将尝试最近有效的副本。"); }
        }
        localAccessible = true;
      } catch (_) { localAccessible = false; }
      return records.sort((a, b) => b.saved_at - a.saved_at || b.key.localeCompare(a.key));
    }
    function prune(records, forQuota = false) {
      let kept = 0;
      for (const item of records) {
        if (item.key === laneKey) { kept++; continue; }
        const remove = item.expired || kept >= MAX_LANES || forQuota;
        if (!remove) { kept++; continue; }
        try { local.removeItem(item.key); } catch (_) { /* Never remove outside this workspace's lane namespace. */ }
      }
    }
    function load() {
      if (!valid) return null;
      if (session) {
        try {
          const raw = session.getItem(sessionKey); sessionAccessible = true;
          if (raw !== null) {
            try { const value = decode(raw); if (value) return restoredValue(value, raw, session, sessionKey); }
            catch (_) { report("本标签的恢复副本无法读取，原数据已保留；将尝试本机最近有效的副本。"); }
          }
        } catch (_) { sessionAccessible = false; }
      }
      const records = localRecords();
      const newest = records.find((item) => !item.expired);
      if (newest) return restoredValue(newest, newest.raw, local, newest.key);
      if (!localAccessible || !sessionAccessible) degraded();
      return null;
    }
    function save(snapshot) {
      if (!valid) return false;
      if (retirement) return false;
      let raw;
      try { raw = JSON.stringify({ version: VERSION, saved_at: Date.now(), data: sanitize(snapshot) }); if (raw.length > MAX_RAW) invalid(); }
      catch (_) { report("这次页面状态超过恢复范围或格式不正确，未写入恢复副本；聊天仍可继续。"); return false; }
      let sessionSaved = false, localSaved = false;
      if (session) try { session.setItem(sessionKey, raw); sessionSaved = true; sessionAccessible = true; } catch (_) { sessionAccessible = false; }
      if (local) {
        try { local.setItem(laneKey, raw); localSaved = true; localAccessible = true; }
        catch (error) {
          if (error && (error.name === "QuotaExceededError" || error.name === "NS_ERROR_DOM_QUOTA_REACHED")) {
            const records = localRecords(); prune(records, true);
            try { local.setItem(laneKey, raw); localSaved = true; localAccessible = true; } catch (_) { localAccessible = false; }
          } else localAccessible = false;
        }
        if (localSaved) prune(localRecords());
      }
      degraded();
      return sessionSaved || localSaved;
    }
    return { load, save, status };
  }
  window.RabbitBrowserState = { create };
})();
