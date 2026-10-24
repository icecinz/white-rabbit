(() => {
  "use strict";
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  const agents = window.RabbitAgents;
  const efforts = new Set(["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]);
  const piNativePrefix = "local/pi/native/", piNativeReference = /^local\/pi\/native\/[a-f0-9]{64}$(?![\s\S])/;
  const piThinkingLevels = new Set(["off", "minimal", "low", "medium", "high", "xhigh"]);
  const piEffortSource = "Pi 原生 getSupportedThinkingLevels；none 对应原生 off，不代表关闭厂商思考。";
  const piUnknownEffortSource = "Pi 原生思考强度尚未核验。";
  const hermesNativePrefix = "local/hermes/native/", hermesNativeReference = /^local\/hermes\/native\/[a-f0-9]{64}$(?![\s\S])/;
  const hermesRequestEfforts = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
  const hermesEffortSource = "Hermes 原生 parse_reasoning_effort；原生请求强度，提供商可能映射。";
  const reservedNativeModel = value => value.startsWith(piNativePrefix) || value.startsWith(hermesNativePrefix);
  const fieldNames = ["name", "agent_instance_id", "device_id", "model_id", "reasoning_effort"];
  const memoryFields = ["name", "description", "habits", "program", "connection_id", "mode"];
  const memoryModes = new Set(["read_only", "read_write"]), bankStates = new Set(["empty", "ready", "unavailable", "unknown"]);
  const nativeMemorySourceId = "hermes_hindsight_current", nativeMemoryKind = "hermes_hindsight_readonly";
  const nativeSourceRef = /^nativesrc_[a-f0-9]{32}$/;
  const agentName = id => agents.labels[agents.backendOfInstance(id)] || "未知助手";
  const memoryModeLabel = mode => mode === "read_write" ? "读取并记住" : "只读取";
  function memoryProfile(value, publicFields = true) {
    if (value?.id === "none") return publicFields ? { id: "none", name: "不使用长期记忆", available: true, detail: typeof value.detail === "string" ? value.detail.slice(0, 1000) : "不查询或写入长期记忆；当前聊天的上文照常保留。" } : { id: "none" };
    if (!value || typeof value.id !== "string" || !uuid.test(value.id) || !Number.isSafeInteger(value.revision) || value.revision < 1 || typeof value.name !== "string" || value.name.length > 80 || !value.name.trim() || Array.from(value.name).length > 40 || /[\x00-\x1f\x7f]/.test(value.name) || typeof value.description !== "string" || value.description.length > 400 || Array.from(value.description).length > 200 || value.description.includes("\0") || typeof value.habits !== "string" || value.habits.length > 8000 || Array.from(value.habits).length > 4000 || value.habits.includes("\0") || value.program !== "hindsight" || value.connection_id !== "local/hindsight" || !memoryModes.has(value.mode) || typeof value.bank_ref !== "string" || !/^mem_[a-f0-9]{32}$/.test(value.bank_ref)) throw new Error("记忆配置没有读完整，请重新读取。");
    const result = { id: value.id, revision: value.revision, name: value.name, description: value.description, habits: value.habits, program: "hindsight", connection_id: "local/hindsight", mode: value.mode, bank_ref: value.bank_ref };
    if (publicFields) {
      if (typeof value.available !== "boolean" || !bankStates.has(value.bank_state)) throw new Error("记忆配置连接状态没有读完整，请重新读取。");
      result.available = value.available; result.bank_state = value.bank_state;
      for (const field of ["reason", "connection_label", "bank_label", "created_at", "updated_at"]) if (typeof value[field] === "string" && value[field].length <= 500 || value[field] === null) result[field] = value[field];
      if (value.source_kind !== undefined || value.locked_mode !== undefined || value.native_source_ref !== undefined) {
        if (value.source_kind !== nativeMemoryKind || value.locked_mode !== true || value.mode !== "read_only") throw new Error("原有记忆的只读绑定没有读完整，请重新读取。");
        result.source_kind = nativeMemoryKind; result.locked_mode = true;
        if (value.native_source_ref !== undefined) { if (typeof value.native_source_ref !== "string" || !nativeSourceRef.test(value.native_source_ref)) throw new Error("原有记忆的来源引用没有读完整，请重新读取。"); result.native_source_ref = value.native_source_ref; }
      }
    }
    return result;
  }
  function profile(value, publicFields = true) {
    if (!value || typeof value.id !== "string" || !uuid.test(value.id) || !Number.isSafeInteger(value.revision) || value.revision < 1 || typeof value.name !== "string" || !value.name.trim() || Array.from(value.name).length > 40 || /[\x00-\x1f\x7f]/.test(value.name) || !agents.isInstance(value.agent_instance_id) || value.device_id !== "local" || typeof value.model_id !== "string" || value.model_id.length > 220 || !agents.validModelId(value.model_id, agents.backendOfInstance(value.agent_instance_id)) || value.reasoning_effort !== null && !efforts.has(value.reasoning_effort)) throw new Error("AI 配置没有读完整，请重新读取。");
    const result = { id: value.id, revision: value.revision };
    for (const field of fieldNames) result[field] = value[field];
    if (publicFields) {
      if (typeof value.available !== "boolean") throw new Error("AI 配置连接状态没有读完整，请重新读取。");
      // Native references are provider/model identities, never text model names.
      result.available = value.available && !reservedNativeModel(value.model_id);
      for (const field of ["reason", "effective_model", "effective_effort", "created_at", "updated_at"]) if (typeof value[field] === "string" && value[field].length <= 500 || value[field] === null) result[field] = value[field];
      if (value.native_available !== undefined || value.native_reason !== undefined) {
        if (typeof value.native_available !== "boolean" || value.native_reason !== null && (typeof value.native_reason !== "string" || value.native_reason.length > 1000 || /[\x00-\x1f\x7f]/.test(value.native_reason))) throw new Error("原生配置目录状态没有读完整，请重新读取。");
        result.native_available = value.native_available; result.native_reason = value.native_reason;
      }
    }
    return result;
  }
  function snapshot(value) {
    if (value == null) return null;
    if (![1, 2].includes(value.schema_version) || value.schema_version === 1 && value.memory_profile?.id !== "none" || !Array.isArray(value.used_memory_references) || value.used_memory_references.length) throw new Error("这份配置快照暂不支持，请重新打开原草稿。");
    return { schema_version: value.schema_version, ai_profile: profile(value.ai_profile, false), memory_profile: memoryProfile(value.memory_profile, false), used_memory_references: [] };
  }
  function memoryRecall(value) {
    if (value == null) return null;
    const recall = value, bounded = (n, limit = 1000000) => Number.isSafeInteger(n) && n >= 0 && n <= limit;
    if (!recall || !["skipped", "succeeded", "failed"].includes(recall.state) || !Array.isArray(recall.used_references) || recall.used_references.length > 16 || !bounded(recall.included_facts, 16) || recall.included_facts !== recall.used_references.length || !bounded(recall.omitted_facts)) throw new Error("本轮记忆状态没有读完整。");
    let characters = 0; const seen = new Set();
    const used_references = recall.used_references.map(row => {
      if (!row || typeof row.reference_id !== "string" || !/^memref_[a-f0-9]{32}$/.test(row.reference_id) || seen.has(row.reference_id) || typeof row.memory_profile_id !== "string" || !uuid.test(row.memory_profile_id) || !Number.isSafeInteger(row.memory_profile_revision) || row.memory_profile_revision < 1 || !["world", "experience", "observation"].includes(row.type) || typeof row.text !== "string" || row.text.length > 16384 || !row.text.trim() || row.text.includes("\0") || typeof row.retrieved_at !== "string" || row.retrieved_at.length > 64 || /[\x00-\x1f\x7f]/.test(row.retrieved_at) || row.document_ref !== null && (typeof row.document_ref !== "string" || !/^memdoc_[a-f0-9]{32}$/.test(row.document_ref))) throw new Error("本轮记忆来源没有读完整。");
      characters += Array.from(row.text).length; if (characters > 8192) throw new Error("本轮记忆来源超出阅读容量。"); seen.add(row.reference_id);
      return { reference_id: row.reference_id, memory_profile_id: row.memory_profile_id, memory_profile_revision: row.memory_profile_revision, type: row.type, text: row.text, retrieved_at: row.retrieved_at, document_ref: row.document_ref };
    });
    if (recall.state !== "succeeded" && used_references.length || recall.characters !== undefined && (!bounded(recall.characters, 8192) || recall.characters !== characters)) throw new Error("本轮记忆数量与正文不一致。");
    const publicRecall = { state: recall.state, used_references, included_facts: recall.included_facts, omitted_facts: recall.omitted_facts };
    if (recall.characters !== undefined) publicRecall.characters = recall.characters;
    if (typeof recall.retrieved_at === "string" && recall.retrieved_at.length <= 64 && !/[\x00-\x1f\x7f]/.test(recall.retrieved_at)) publicRecall.retrieved_at = recall.retrieved_at;
    return publicRecall;
  }
  function memoryInfo(value) {
    if (value == null) return null;
    const publicRecall = memoryRecall(value.recall), retain = value.retain, bounded = (n, limit = 1000000) => Number.isSafeInteger(n) && n >= 0 && n <= limit;
    if (!publicRecall || !retain || !["disabled", "not_applicable", "queued", "submitting", "processing", "completed", "failed", "unknown"].includes(retain.state)) throw new Error("本轮记忆状态没有读完整。");
    const publicRetain = { state: retain.state };
    if (retain.operation_id != null) { if (typeof retain.operation_id !== "string" || !uuid.test(retain.operation_id)) throw new Error("记忆任务标识没有读完整。"); publicRetain.operation_id = retain.operation_id; }
    if (retain.error_code != null) { if (typeof retain.error_code !== "string" || retain.error_code.length > 160 || /[\x00-\x1f\x7f]/.test(retain.error_code)) throw new Error("记忆任务状态没有读完整。"); publicRetain.error_code = retain.error_code; }
    if (retain.fact_count != null) { if (!bounded(retain.fact_count)) throw new Error("记忆任务事实数量无效。"); publicRetain.fact_count = retain.fact_count; }
    return { recall: publicRecall, retain: publicRetain };
  }
  function sameSnapshot(a, b) { const first = snapshot(a), second = snapshot(b); if (first == null || second == null) return first == null && second == null; return JSON.stringify(first.ai_profile) === JSON.stringify(second.ai_profile) && JSON.stringify(first.memory_profile) === JSON.stringify(second.memory_profile); }
  function selection(value) {
    if (!value || !Number.isSafeInteger(value.revision) || value.revision < 0 || value.ai_profile_id !== null && (typeof value.ai_profile_id !== "string" || !uuid.test(value.ai_profile_id)) || value.memory_profile_id !== "none" && (typeof value.memory_profile_id !== "string" || !uuid.test(value.memory_profile_id))) throw new Error("这段聊天的配置选择没有读完整，请重新打开。");
    return { revision: value.revision, ai_profile_id: value.ai_profile_id, memory_profile_id: value.memory_profile_id };
  }
  function nativeMemorySource(value) {
    const count = n => n === null || Number.isSafeInteger(n) && n >= 0;
    if (!value || value.id !== nativeMemorySourceId || typeof value.name !== "string" || !value.name.trim() || Array.from(value.name).length > 80 || /[\x00-\x1f\x7f]/.test(value.name) || typeof value.available !== "boolean" || typeof value.reason !== "string" || value.reason.length > 1000 || /[\x00-\x1f\x7f]/.test(value.reason) || !count(value.facts) || !count(value.documents) || value.linked_profile_id !== null && (typeof value.linked_profile_id !== "string" || !uuid.test(value.linked_profile_id))) throw new Error("原有记忆来源没有读完整，请刷新配置。");
    const result = { id: nativeMemorySourceId, name: value.name, available: value.available, reason: value.reason, facts: value.facts, documents: value.documents, linked_profile_id: value.linked_profile_id };
    if (value.source_ref !== undefined) { if (typeof value.source_ref !== "string" || !nativeSourceRef.test(value.source_ref)) throw new Error("原有记忆的来源引用没有读完整，请刷新配置。"); result.source_ref = value.source_ref; }
    return result;
  }
  function nativeCapabilities(value) {
    if (value === undefined) return undefined;
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some(key => !["codex", "pi", "hermes"].includes(key))) throw new Error("原生配置能力快照没有读完整。");
    const result = {}, nativeText = (text, limit) => typeof text === "string" && text.trim() && Array.from(text).length <= limit && !/[\p{Cc}\p{Cf}\p{Cs}]/u.test(text);
    const exactFields = (object, fields) => object && typeof object === "object" && !Array.isArray(object) && Object.keys(object).length === fields.length && fields.every(field => Object.hasOwn(object, field));
    for (const backend of ["codex", "pi", "hermes"]) {
      const source = value[backend]; if (source === undefined) continue;
      const label = agents.labels[backend], expectedSource = { codex: "codex/model/list", pi: "pi/get_available_models", hermes: "hermes/model.options" }[backend];
      if (!source || typeof source !== "object" || Array.isArray(source) || source.source !== expectedSource || typeof source.enabled !== "boolean" || typeof source.catalogue_available !== "boolean"
          || source.observed_at !== null && (typeof source.observed_at !== "string" || !source.observed_at || source.observed_at.length > 100 || /[\x00-\x1f\x7f]/.test(source.observed_at))
          || source.source_chat_id !== null && (typeof source.source_chat_id !== "string" || !uuid.test(source.source_chat_id)) || !Array.isArray(source.models) || source.models.length > (backend === "codex" ? 512 : 4096)) throw new Error(`原生 ${label} 目录快照没有读完整。`);
      if (backend === "hermes" && (!exactFields(source, ["enabled", "source", "catalogue_available", "observed_at", "source_chat_id", "models"])
          || source.catalogue_available && (!source.enabled || !nativeText(source.observed_at, 80) || !uuid.test(source.source_chat_id))
          || !source.catalogue_available && (source.models.length || source.observed_at !== null || source.source_chat_id !== null))) throw new Error("原生 Hermes 目录状态没有读完整。");
      const models = source.models.map(row => {
        if (backend === "hermes") {
          if (!exactFields(row, ["id", "agent_id", "provider", "model", "name", "supported_efforts", "default_effort", "explicit_effort_supported", "effort_source"])
              || row.agent_id !== "hermes" || typeof row.id !== "string" || !hermesNativeReference.test(row.id)
              || !nativeText(row.provider, 256) || !nativeText(row.model, 1024) || !nativeText(row.name, 1539)
              || !Array.isArray(row.supported_efforts) || JSON.stringify(row.supported_efforts) !== JSON.stringify(hermesRequestEfforts)
              || row.default_effort !== null || row.explicit_effort_supported !== true || row.effort_source !== hermesEffortSource) throw new Error("原生 Hermes 请求强度目录没有读完整。");
          return { id: row.id, agent_id: "hermes", provider: row.provider, model: row.model, name: row.name, supported_efforts: [...row.supported_efforts], default_effort: null, explicit_effort_supported: true, effort_source: row.effort_source };
        }
        if (backend === "pi") {
          // Synchronous shape/provenance validation only. The server recomputes
          // this digest from exact provider/model before accepting a profile.
          if (!row || row.agent_id !== "pi" || typeof row.id !== "string" || !piNativeReference.test(row.id) || !nativeText(row.provider, 256) || !nativeText(row.model, 1024) || !nativeText(row.name, 512)
              || !Array.isArray(row.native_thinking_levels) || row.native_thinking_levels.length > 6 || row.native_thinking_levels.some(level => !piThinkingLevels.has(level)) || new Set(row.native_thinking_levels).size !== row.native_thinking_levels.length
              || !Array.isArray(row.supported_efforts) || JSON.stringify(row.supported_efforts) !== JSON.stringify(row.native_thinking_levels.map(level => level === "off" ? "none" : level))
              || row.default_effort !== null || row.explicit_effort_supported !== (row.native_thinking_levels.length > 0) || ![piEffortSource, piUnknownEffortSource].includes(row.effort_source)
              || row.effort_source === piUnknownEffortSource && row.native_thinking_levels.length) throw new Error("原生 Pi 模型能力没有读完整。");
          return { id: row.id, agent_id: "pi", provider: row.provider, model: row.model, name: row.name, supported_efforts: [...row.supported_efforts], default_effort: null, explicit_effort_supported: row.explicit_effort_supported, native_thinking_levels: [...row.native_thinking_levels], effort_source: row.effort_source };
        }
        if (!row || row.agent_id !== "codex" || !row.id || !agents.validModelId(row.id, "codex") || typeof row.model !== "string" || row.model !== row.id.slice("local/codex/".length)
            || typeof row.name !== "string" || !row.name || row.name.length > 500 || /[\x00-\x1f\x7f]/.test(row.name)
            || !Array.isArray(row.supported_efforts) || row.supported_efforts.some(item => !efforts.has(item)) || new Set(row.supported_efforts).size !== row.supported_efforts.length
            || row.default_effort !== null && !efforts.has(row.default_effort) || typeof row.explicit_effort_supported !== "boolean") throw new Error("原生 Codex 模型能力没有读完整。");
        return { id: row.id, agent_id: "codex", model: row.model, name: row.name, supported_efforts: [...row.supported_efforts], default_effort: row.default_effort, explicit_effort_supported: row.explicit_effort_supported };
      });
      if (new Set(models.map(row => row.id)).size !== models.length || backend !== "codex" && new Set(models.map(row => JSON.stringify([row.provider, row.model]))).size !== models.length) throw new Error(`原生 ${label} 模型目录标识重复。`);
      result[backend] = { enabled: source.enabled, source: source.source, catalogue_available: source.catalogue_available, observed_at: source.observed_at, source_chat_id: source.source_chat_id, models };
    }
    return result;
  }
  function catalog(value) {
    if (!value || !Array.isArray(value.ai_profiles) || value.ai_profiles.length > 512 || !Array.isArray(value.memory_profiles) || value.memory_profiles.length < 1 || value.memory_profiles.length > 512 || value.default_ai_profile_id !== null && (typeof value.default_ai_profile_id !== "string" || !uuid.test(value.default_ai_profile_id)) || value.default_memory_profile_id !== "none" || typeof value.revision !== "string" || !/^[a-f0-9]{64}$/.test(value.revision)) throw new Error("配置列表没有读完整，请重新读取。");
    const defaultRevision = value.default_ai_profile_revision === undefined ? 0 : value.default_ai_profile_revision;
    if (!Number.isSafeInteger(defaultRevision) || defaultRevision < 0 || value.workspace_id !== undefined && (typeof value.workspace_id !== "string" || !value.workspace_id)) throw new Error("配置列表的工作区或默认版本无效。");
    const rows = value.ai_profiles.map(row => profile(row));
    if (new Set(rows.map(row => row.id)).size !== rows.length || value.default_ai_profile_id !== null && !rows.some(row => row.id === value.default_ai_profile_id)) throw new Error("配置列表标识不完整，请重新读取。");
    const memories = value.memory_profiles.map(row => memoryProfile(row));
    if (new Set(memories.map(row => row.id)).size !== memories.length || !memories.some(row => row.id === "none")) throw new Error("记忆配置列表标识不完整，请重新读取。");
    const connections = value.memory_connections === undefined ? [] : value.memory_connections;
    if (!Array.isArray(connections) || connections.length > 1 || connections.some(row => row?.id !== "local/hindsight" || row.program !== "hindsight" || typeof row.available !== "boolean")) throw new Error("记忆连接列表没有读完整，请重新读取。");
    const memory_connections = connections.map(row => { const result = { id: row.id, program: row.program, available: row.available }; for (const field of ["label", "reason", "processing_notice"]) if (typeof row[field] === "string" && row[field].length <= 1000) result[field] = row[field]; return result; });
    const nativeSources = value.native_memory_sources === undefined ? [] : value.native_memory_sources;
    if (!Array.isArray(nativeSources) || nativeSources.length > 1) throw new Error("原有记忆来源列表没有读完整，请刷新配置。");
    const native_memory_sources = nativeSources.map(nativeMemorySource);
    if (native_memory_sources.some(source => source.linked_profile_id && !memories.some(row => row.id === source.linked_profile_id && row.source_kind === nativeMemoryKind && row.native_source_ref === source.source_ref))) throw new Error("原有记忆的接入配置没有读完整，请刷新配置。");
    const native_profile_capabilities = nativeCapabilities(value.native_profile_capabilities);
    if (native_profile_capabilities !== undefined && !value.workspace_id) throw new Error("原生配置能力快照缺少所属工作区。");
    return { ai_profiles: rows, memory_profiles: memories, memory_connections, native_memory_sources, default_ai_profile_id: value.default_ai_profile_id, default_ai_profile_revision: defaultRevision, default_memory_profile_id: "none", revision: value.revision, ...(value.workspace_id === undefined ? {} : { workspace_id: value.workspace_id }), ...(native_profile_capabilities === undefined ? {} : { native_profile_capabilities }) };
  }
  function create({ api, root, getSelection, onChoose, onManage, onCatalog = () => {}, getInventory = () => null, getCapabilities = () => ({}), getWorkspace = () => "", getNativeAgents = () => [], onChooseNative = null }) {
    let data = null, loading = null, requestVersion = 0, picker = null, editor = null, manager = null, managerKind = "ai", returnFocus = null;
    let workspace = "", pendingCreation = null, creationBusy = false, creationInvalid = false, creationNotice = "", defaultBusy = false, defaultBlocked = false, defaultNotice = "";
    const nativeAttach = { saving: false, unknownSources: new Set(), confirmedProfileId: null, confirmedSourceRef: undefined, errorSourceRef: undefined, error: "", notice: "" };
    const nativeRefresh = { version: 0, open: false, listing: false, reading: false, loaded: false, rows: [], selected: "", notice: "", error: "" };
    const node = (tag, name, text) => { const result = document.createElement(tag); if (name) result.className = name; if (text !== undefined) result.textContent = text; return result; };
    const action = (text, callback, name = "rbw-button rbw-subtle") => { const result = node("button", name, text); result.type = "button"; result.addEventListener("click", callback); return result; };
    const aiControl = control => { control.dataset.controlInterface = "ai"; control.setAttribute("title", "支持 AI 程序接口；人也可直接操作。"); return control; };
    const humanControl = control => { control.dataset.controlInterface = "human"; control.setAttribute("title", ""); return control; };
    const readable = (error, fallback) => typeof error?.message === "string" && /[\u4e00-\u9fff]/.test(error.message) && error.message.length < 300 ? error.message : fallback;
    const creationKey = () => "rabbit.ai-profile-creation.v1." + encodeURIComponent(workspace);
    function creationRecord(raw, requireHash = true) {
      if (!raw || raw.version !== 1 || Object.hasOwn(raw, "submitted") && typeof raw.submitted !== "boolean" || requireHash && (typeof raw.request_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(raw.request_sha256)) || !uuid.test(raw.creation_id) || !["create", "copy"].includes(raw.action) || typeof raw.body_json !== "string" || raw.body_json.length > 8000) throw Error("待核创建记录无效。");
      const source = raw.action === "copy" ? profile(raw.source, false) : null;
      if (raw.action === "create" && raw.source !== null) throw Error("待核创建来源无效。");
      const route = source ? `/api/profiles/ai/${source.id}/copy` : "/api/profiles/ai", body = JSON.parse(raw.body_json);
      if (raw.route !== route || !body || typeof body !== "object" || Array.isArray(body) || body.creation_id !== raw.creation_id) throw Error("待核创建入口无效。");
      const allowed = source ? [...fieldNames, "revision", "creation_id"] : [...fieldNames, "creation_id"];
      if (Object.keys(body).some(key => !allowed.includes(key)) || !Object.hasOwn(body, "name") || source && body.revision !== source.revision || !source && fieldNames.some(key => !Object.hasOwn(body, key))) throw Error("待核创建参数无效。");
      const value = profile({ ...(source ? fields(source) : {}), ...body, id: raw.creation_id, revision: 1 }, false);
      if (fieldNames.some(key => typeof value[key] === "string" && /[\ud800-\udfff]/u.test(value[key]))) throw Error("创建参数包含无效Unicode，尚未提交。");
      return { version: 1, creation_id: raw.creation_id, action: raw.action, source, route, body_json: raw.body_json, value: fields(value), request_sha256: raw.request_sha256 || null, submitted: raw.submitted === undefined ? true : raw.submitted };
    }
    async function creationHash(record) {
      const subtle = window.crypto?.subtle || globalThis.crypto?.subtle;
      if (!subtle || typeof TextEncoder !== "function") throw Error("当前浏览器不能安全核对创建指纹，尚未提交；原表单保留。");
      const body = JSON.parse(record.body_json), changes = Object.fromEntries(fieldNames.filter(key => Object.hasOwn(body, key)).map(key => [key, key === "name" ? body[key].trim() : body[key]]));
      const domain = record.action === "create" ? { action: "create", profile: expectedCreation(record) } : { action: "copy", profile_id: record.source.id, revision: record.source.revision, changes };
      const canonical = JSON.stringify(domain, (_key, value) => value && typeof value === "object" && !Array.isArray(value) ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value);
      const bytes = await subtle.digest("SHA-256", new TextEncoder().encode(canonical));
      return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, "0")).join("");
    }
    async function verifyCreationHash(record) { const digest = await creationHash(record); if (digest !== record.request_sha256) throw Error("待核创建指纹与原参数不一致，尚未提交；请保留原记录核对。"); return digest; }
    function syncWorkspace() {
      const id = String(getWorkspace() || ""); if (id === workspace) return;
      workspace = id; requestVersion++; loading = null; data = null;
      resetNativeRefresh();
      pendingCreation = null; creationBusy = creationInvalid = false; creationNotice = ""; defaultBusy = defaultBlocked = false; defaultNotice = "";
      if (editor?.kind === "ai") editor = null;
      if (id) try { const saved = window.sessionStorage.getItem(creationKey()); if (saved !== null) pendingCreation = creationRecord(JSON.parse(saved)); }
      catch (_) { creationInvalid = true; creationNotice = "浏览器的待核创建记录无法读取；尚未提交新的创建或复制，请先保留并核对原记录。"; }
    }
    function sameWorkspace(id) { return id === workspace && id === String(getWorkspace() || ""); }
    function saveCreation(record) {
      if (!workspace) return false;
      try { if (record) window.sessionStorage.setItem(creationKey(), JSON.stringify(record)); else window.sessionStorage.removeItem(creationKey()); pendingCreation = record; return true; }
      catch (_) { return false; }
    }
    function expectedCreation(record) { return { ...record.value, name: record.value.name.trim() }; }
    function checkCreationProfile(record, received) {
      const saved = profile(received, false), expected = expectedCreation(record);
      if (saved.revision !== 1 || record.source && saved.id === record.source.id || fieldNames.some(key => saved[key] !== expected[key])) throw Error("创建确认与原始参数不一致；原请求继续保留，尚未覆盖当前配置。");
      return saved;
    }
    async function acknowledgeCreation(record, received) {
      const saved = checkCreationProfile(record, received), scope = workspace;
      creationNotice = `已确认当时保存“${saved.name}”（第 ${saved.revision} 版）。`;
      if (!saveCreation(null)) creationNotice += " 浏览器暂时不能清除原回执，请保留原编号再核对；没有另建。";
      if (editor?.kind === "ai" && editor.creation_id === record.creation_id) editor = null;
      // Receipt snapshots are historical acknowledgements, never a replacement
      // for the current catalog or its dynamic availability calculation.
      try {
        await load(true); if (!sameWorkspace(scope)) return;
        const latest = get(saved.id);
        creationNotice += !latest ? " 当前列表未能找到这份配置；不代表当时未保存。" : JSON.stringify(profile(latest, false)) !== JSON.stringify(saved) ? " 当前配置已变化，请查看列表中的最新版本。" : " 当前列表已读取。";
      } catch (_) { if (sameWorkspace(scope)) creationNotice += " 当前列表暂不可读；这不改变当时已保存的确认。"; }
      if (sameWorkspace(scope)) renderManager();
    }
    async function submitCreation(record, captured = null) {
      syncWorkspace(); if (creationBusy || !sameWorkspace(workspace) || !workspace || pendingCreation?.creation_id !== record.creation_id) return;
      const scope = workspace, priorSubmitted = record.submitted; creationBusy = true; creationNotice = ""; if (captured) { captured.saving = true; captured.uncertain = true; captured.error = ""; } renderManager();
      try {
        await verifyCreationHash(record); if (!sameWorkspace(scope)) return;
        // A new rejection can settle only this first attempt, never an older
        // unknown submission. Persist the conservative marker before the POST.
        record = { ...record, submitted: true };
        if (!saveCreation(record)) throw Error("浏览器不能保存本次提交标记，尚未提交；原请求保留。");
        const result = await api(record.route, { method: "POST", body: record.body_json }, false);
        if (!sameWorkspace(scope)) return; await acknowledgeCreation(record, result.profile);
      } catch (error) {
        if (!sameWorkspace(scope)) return;
        const rejected = error.accepted === false;
        if (rejected && !priorSubmitted && saveCreation(null)) { if (captured && editor === captured) captured.uncertain = false; creationNotice = "这次创建已明确拒绝，原表单保留；没有自动重试。"; }
        else if (rejected && priorSubmitted) creationNotice = "本次重试已被拒绝；之前创建的结果仍待核对，原编号和原参数保留，没有另建。";
        else creationNotice = readable(error, "暂时无法确认是否保存；原编号和原参数已保留，没有自动重复保存。请核对创建结果，或明确重试原请求。");
        if (captured && editor === captured) captured.error = creationNotice;
      } finally { if (sameWorkspace(scope)) { creationBusy = false; if (captured && editor === captured) captured.saving = false; renderManager(); } }
    }
    async function checkCreation() {
      syncWorkspace(); const record = pendingCreation, scope = workspace; if (!record || creationBusy || !scope) return;
      creationBusy = true; creationNotice = ""; renderManager();
      try {
        await verifyCreationHash(record); if (!sameWorkspace(scope)) return;
        const value = await api("/api/management/ai-profile-creations/" + record.creation_id);
        if (!sameWorkspace(scope)) return;
        if (!value || value.workspace_id !== scope || value.scope !== "saved_parameters" || !Object.hasOwn(value, "receipt")) throw Error("创建回执工作区或格式不匹配；原请求保留。");
        const receipt = value.receipt;
        if (receipt === null) { creationNotice = "尚未查到已保存回执；不能据此断言失败。原编号和原参数保留，可明确重试原请求。"; return; }
        if (!receipt || receipt.creation_id !== record.creation_id || receipt.action !== record.action || typeof receipt.request_sha256 !== "string" || receipt.request_sha256 !== record.request_sha256) throw Error("创建回执身份或指纹不匹配；原请求保留。");
        await acknowledgeCreation(record, receipt.profile);
      } catch (error) { if (sameWorkspace(scope)) creationNotice = readable(error, "创建结果暂不可读取；原请求保留，没有自动重试。"); }
      finally { if (sameWorkspace(scope)) { creationBusy = false; renderManager(); } }
    }
    function creationPanel() {
      const box = node("section", "profile-editor");
      box.append(node("h3", "", pendingCreation ? "AI 配置创建待核对" : "AI 配置创建提示"));
      if (pendingCreation) {
        const record = pendingCreation; box.append(node("p", "profile-note", `原${record.action === "copy" ? "复制" : "创建"}请求：${record.value.name}。只核对或重试这份原参数，不会自动发送。`));
        const details = node("details", "profile-identifiers"); details.append(node("summary", "", "查看原请求"), node("pre", "", JSON.stringify({ creation_id: record.creation_id, action: record.action, source_id: record.source?.id || null, source_revision: record.source?.revision || null, request_sha256: record.request_sha256, parameters: record.value }, null, 2))); box.append(details);
        const actions = node("div", "profile-actions"), check = aiControl(action("核对创建结果", checkCreation)), retry = aiControl(action("重试原创建请求", () => submitCreation(record))); check.disabled = retry.disabled = creationBusy || creationInvalid; actions.append(check, retry); box.append(actions);
      }
      if (creationNotice) box.append(node("p", "profile-note", creationNotice)); return box;
    }
    function get(id) { return data?.ai_profiles.find(row => row.id === id) || null; }
    function getMemory(id) { return data?.memory_profiles.find(row => row.id === id) || null; }
    function memoryEnabled() { return getCapabilities().memory === "hindsight" || !!data?.memory_profiles.some(row => row.id !== "none"); }
    function current() { return getSelection() || {}; }
    async function load(force = false) {
      syncWorkspace();
      if (!force && data) return data;
      if (loading) return loading;
      const version = ++requestVersion;
      loading = (async () => {
        if (pendingCreation && !creationBusy) try { await verifyCreationHash(pendingCreation); if (version === requestVersion) creationInvalid = false; }
        catch (_) { if (version === requestVersion) { creationInvalid = true; creationNotice = "待核创建指纹无法验证；原请求保留，尚未提交核对或重试。"; } }
        try { const received = catalog(await api("/api/profiles")); if (received.workspace_id !== undefined && workspace && received.workspace_id !== workspace) throw Error("配置列表不属于当前工作区，原请求保留。"); if (version === requestVersion && workspace === String(getWorkspace() || "")) { data = received; if (!defaultBusy) defaultBlocked = false; reconcileNativeAttach(); onCatalog(received); if (picker) renderPicker(); renderManager(); } return received; }
        finally { if (version === requestVersion) loading = null; }
      })();
      return loading;
    }
    async function loadFresh() {
      syncWorkspace();
      const scope = workspace, previous = loading;
      if (previous) try { await previous; } catch (_) { /* The new read must not reuse an earlier failed or stale request. */ }
      if (!sameWorkspace(scope)) throw Error("工作区已变化；没有继续读取旧配置目录。");
      return load(true);
    }
    function close(restoreFocus = true) {
      picker = null; root.hidden = true; root.replaceChildren();
      for (const id of ["choose-ai-profile", "choose-memory-profile"]) document.getElementById(id)?.setAttribute("aria-expanded", "false");
      const shell = document.querySelector(".app-shell"); if (shell) shell.inert = false;
      if (restoreFocus && returnFocus?.isConnected !== false) returnFocus?.focus(); returnFocus = null;
    }
    function renderPicker() {
      if (!picker) return;
      const captured = picker, now = current(), list = captured.kind === "memory" ? data?.memory_profiles || [] : data?.ai_profiles || [], nativeMemory = captured.kind === "memory" && now.native_history === true;
      const panel = node("div", "profile-picker-panel"), head = node("header", "profile-picker-header");
      const title = node("h2", "", captured.kind === "memory" ? "记忆配置" : "AI 配置"); title.id = "profile-picker-title";
      head.append(title, action("关闭", close)); panel.append(head);
      const memoryNote = captured.kind !== "memory" ? "" : nativeMemory
        ? memoryEnabled() ? "这里选择额外提供给原生 Agent 的只读共享记忆；原 Agent 自身记忆仍按原配置工作。" : "目前尚未接入共享记忆；原 Agent 自身记忆仍按原配置工作。"
        : memoryEnabled() ? "长期记忆与当前聊天上文是两件事。换记忆配置后，前面可见的消息仍可能影响回答；新选择从下一次发送生效。" : "长期记忆与当前聊天上文是两件事。目前只有不使用长期记忆；其他记忆程序尚未接入。";
      panel.append(node("p", "profile-note", captured.kind === "memory" ? memoryNote : now.ai_profile_id == null && data?.default_ai_profile_id === null ? "请选择 AI 配置，下一次发送时使用。当前回答和已有消息不会改变。" : "选择已保存的名字，下一次发送时使用。当前回答和已有消息不会改变。"));
      if (!data) panel.append(node("p", "profile-note", captured.error || "正在读取已保存配置…"));
      const collection = node("div", "profile-choice-list");
      for (const row of list) {
        const selected = row.id === captured.selected;
        const control = action("", () => { captured.selected = row.id; renderPicker(); }, "profile-choice"); control.setAttribute("aria-pressed", String(selected)); control.disabled = row.available !== true || captured.saving;
        control.append(node("strong", "", nativeMemory && row.id === "none" ? "不额外带入共享记忆" : row.name), node("span", "profile-note", captured.kind === "memory" ? row.id === "none" ? nativeMemory ? "不额外带入共享记忆；原 Agent 自身记忆沿用原配置。" : row.detail : `Hindsight · ${memoryModeLabel(row.mode)} · 第 ${row.revision} 版${row.available ? "" : ` · ${row.reason || "目前不可用"}`}` : `${row.available ? "可选择 · 发送时验证" : row.reason || "目前不可用"} · 第 ${row.revision} 版`)); collection.append(control);
      }
      const native = nativeChoices(captured);
      let savedHost = panel;
      if (native.length) {
        panel.append(node("h3", "", "原生配置"), node("p", "profile-note", "带上当前草稿、兼容的材料和只读记忆，打开原生对话；不会自动发送。所选配置在发送时再次核对。"));
        const nativeList = node("div", "profile-choice-list");
        for (const row of nativeSavedChoices(captured)) {
          const control = action("", () => applyNative(captured, agents.backendOfInstance(row.agent_instance_id), row), "profile-choice profile-native-saved-choice");
          control.dataset.profileId = row.id; control.disabled = captured.saving || captured.nativeAttempted || now.busy;
          control.append(node("strong", "", row.name), node("span", "profile-note", `${agentName(row.agent_instance_id)} · 第 ${row.revision} 版 · 发送时核对原生配置`)); nativeList.append(control);
        }
        for (const row of native) {
          const control = action(row.label, () => applyNative(captured, row.agent), "profile-choice profile-native-choice");
          control.dataset.agent = row.agent; control.disabled = captured.saving || captured.nativeAttempted || now.busy;
          nativeList.append(control);
        }
        panel.append(nativeList);
        const saved = node("details", "profile-saved-choices"); saved.open = captured.savedOpen === true;
        saved.append(node("summary", "", "统一文字配置"), node("p", "profile-note", "使用共享上下文的文字连接；不提供完整原生工具。"));
        saved.addEventListener("toggle", () => { if (picker === captured) captured.savedOpen = saved.open; });
        panel.append(saved); savedHost = saved;
      }
      savedHost.append(collection);
      if (captured.error) { const note = node("p", "profile-error", captured.error); note.setAttribute("role", "alert"); panel.append(note); }
      const buttons = node("div", "profile-actions");
      const apply = action(captured.saving ? "正在选择…" : captured.kind === "memory" && (now.existing || nativeMemory) ? "在这段对话继续" : "用于下一次发送", () => applyPicker(captured), "rbw-button rbw-primary");
      apply.disabled = captured.saving || !list.some(row => row.id === captured.selected && row.available === true) || now.busy;
      buttons.append(action("取消", close), action("管理这些配置", () => { const kind = captured.kind; close(); onManage(kind); }), apply);
      if (captured.kind === "memory" && !nativeMemory) { const fresh = action(captured.saving ? "正在创建…" : "新开空白对话", () => applyPicker(captured, true)); fresh.disabled = apply.disabled; buttons.append(fresh); panel.append(node("p", "profile-note", "新开空白对话时，当前未发送内容留在原话题。不会把已有聊天或旧库资料写入新记忆库。")); }
      savedHost.append(buttons); root.replaceChildren(panel);
    }
    function nativeChoices(captured) {
      if (captured.kind !== "ai" || current().existing !== false || typeof onChooseNative !== "function") return [];
      let values; try { values = getNativeAgents(); } catch (_) { return []; }
      if (!Array.isArray(values)) return [];
      const seen = new Set();
      return values.filter(row => row && agents.has(row.agent) && typeof row.label === "string" && row.label.trim() && row.label.length <= 80 && !/[\x00-\x1f\x7f]/.test(row.label) && !seen.has(row.agent) && seen.add(row.agent));
    }
    function nativeSavedChoices(captured) {
      const available = new Set(nativeChoices(captured).map(row => row.agent));
      return (data?.ai_profiles || []).filter(row => row.native_available === true && ["codex", "pi", "hermes"].includes(agents.backendOfInstance(row.agent_instance_id)) && available.has(agents.backendOfInstance(row.agent_instance_id)));
    }
    async function applyNative(captured, agent, saved = null) {
      if (picker !== captured || captured.saving || captured.nativeAttempted || current().busy || current().view !== captured.view || !nativeChoices(captured).some(row => row.agent === agent)) return;
      const scope = String(getWorkspace() || "");
      if (!scope) return;
      const selected = saved === null ? null : profile(saved, false), profileCurrent = () => selected === null || nativeSavedChoices(captured).some(row => JSON.stringify(profile(row, false)) === JSON.stringify(selected));
      if (!profileCurrent()) return;
      const sameView = () => picker === captured && current().view === captured.view && current().existing === false && String(getWorkspace() || "") === scope;
      const isCurrent = () => sameView() && nativeChoices(captured).some(row => row.agent === agent) && profileCurrent();
      const changedNotice = "原生入口或所选配置已变化，没有继续应用或发送。已创建的会话会保留在列表，请先核对。";
      captured.saving = captured.nativeAttempted = true; captured.error = ""; renderPicker();
      try {
        const ok = await onChooseNative(agent, captured.view, isCurrent, selected);
        if (!isCurrent()) { if (sameView()) captured.error = changedNotice; return; }
        if (ok === true) close(false);
        else captured.error = "新建结果待核对，请到设置的 Agent 会话查看；这里没有重复创建或发送。";
      } catch (error) {
        if (error?.nativeCreationUnsent === true) captured.nativeAttempted = false;
        if (sameView()) captured.error = isCurrent() ? readable(error, "新建结果待核对，请到设置的 Agent 会话查看；这里没有重复创建或发送。") : changedNotice;
      } finally { captured.saving = false; if (picker === captured) renderPicker(); }
    }
    async function applyPicker(captured, newChat = false) {
      if (picker !== captured || current().busy || captured.saving) return;
      captured.saving = true; captured.error = ""; renderPicker();
      try { const chosen = captured.kind === "memory" ? captured.selected === "none" ? "none" : getMemory(captured.selected) : get(captured.selected); if (!chosen) throw new Error("这份配置已变化，请重新读取。"); const ok = await onChoose(captured.kind, chosen, captured.view, { newChat }); if (picker !== captured) return; if (ok === false) { captured.error = "没有完成选择，原配置和草稿保留。"; return; } close(); }
      catch (error) { if (picker === captured) captured.error = readable(error, "没有完成选择，原配置和草稿保留。"); }
      finally { captured.saving = false; if (picker === captured) renderPicker(); }
    }
    async function open(kind) {
      if (!["ai", "memory"].includes(kind) || current().busy) return;
      returnFocus = document.activeElement;
      const selected = current(); picker = { kind, selected: kind === "memory" ? selected.memory_profile_id || "none" : selected.ai_profile_id, view: selected.view, saving: false, error: "" }; root.hidden = false;
      document.getElementById(kind === "memory" ? "choose-memory-profile" : "choose-ai-profile")?.setAttribute("aria-expanded", "true");
      const shell = document.querySelector(".app-shell"); if (shell) shell.inert = true;
      renderPicker(); root.querySelector("button")?.focus();
      try { await load(true); } catch (error) { if (picker) { picker.error = readable(error, "配置列表暂时无法读取，原选择和草稿仍保留。"); renderPicker(); } }
    }
    function fields(value) { return Object.fromEntries(fieldNames.map(key => [key, value[key]])); }
    function inventory() { return getInventory() || {}; }
    // Only the workspace-validated profile catalog may authorize native
    // editing choices. Management inventory has no equivalent scope marker.
    function nativeSource(backend) {
      return workspace && data?.workspace_id === workspace && workspace === String(getWorkspace() || "")
        ? data?.native_profile_capabilities?.[backend] || null : null;
    }
    function resetNativeRefresh() {
      nativeRefresh.version++; Object.assign(nativeRefresh, { open: false, listing: false, reading: false, loaded: false, rows: [], selected: "", notice: "", error: "" });
    }
    const nativeConnectionKey = row => `${row.agent}:${row.chat_id}:${row.epoch}`;
    function nativeConnectionRows(value, scope) {
      const exact = (row, keys) => row && typeof row === "object" && !Array.isArray(row) && Object.keys(row).length === keys.length && keys.every(key => Object.hasOwn(row, key));
      if (!exact(value, ["workspace_id", "connections"]) || value.workspace_id !== scope || !Array.isArray(value.connections) || value.connections.length > 4) throw Error("原生连接列表身份或格式不完整，请重新读取。");
      const seen = new Set(), counts = { codex: 0, pi: 0, hermes: 0 };
      return value.connections.map(row => {
        if (!exact(row, ["agent", "chat_id", "title", "epoch"]) || typeof row.agent !== "string" || !Object.hasOwn(counts, row.agent)
            || nativeSource(row.agent)?.enabled !== true || typeof row.chat_id !== "string" || row.chat_id.length !== 36 || !uuid.test(row.chat_id)
            || typeof row.epoch !== "string" || row.epoch.length !== 32 || !/^[a-f0-9]{32}$/.test(row.epoch)
            || typeof row.title !== "string" || !row.title.trim() || Array.from(row.title).length > 80 || /[\x00-\x1f\x7f\ud800-\udfff]/u.test(row.title)
            || seen.has(row.agent + ":" + row.chat_id) || ++counts[row.agent] > (row.agent === "codex" ? 2 : 1)) throw Error("原生连接列表身份或格式不完整，请重新读取。");
        seen.add(row.agent + ":" + row.chat_id); return { agent: row.agent, chat_id: row.chat_id, title: row.title, epoch: row.epoch };
      });
    }
    function currentNativeRefresh(version, scope, host) {
      return sameWorkspace(scope) && nativeRefresh.version === version && manager === host && managerKind === "ai" && host?.isConnected !== false && nativeRefresh.open;
    }
    async function readNativeConnections() {
      syncWorkspace(); if (!workspace || !nativeRefresh.open || nativeRefresh.listing || managerKind !== "ai") return;
      const scope = workspace, host = manager, version = ++nativeRefresh.version;
      Object.assign(nativeRefresh, { listing: true, reading: false, loaded: false, rows: [], selected: "", notice: "", error: "" }); renderManager();
      try {
        const value = await api("/api/management/native-model-connections");
        if (!currentNativeRefresh(version, scope, host)) return;
        nativeRefresh.rows = nativeConnectionRows(value, scope); nativeRefresh.loaded = true;
        nativeRefresh.notice = nativeRefresh.rows.length ? "请选择一段本次读到的连接，再更新模型目录。" : "本次没有读到可用连接；请先在原生对话连接，或稍后重新读取。";
      } catch (error) {
        if (currentNativeRefresh(version, scope, host)) nativeRefresh.error = readable(error, "原生连接列表暂不可读；没有打开或创建会话。");
      } finally { if (currentNativeRefresh(version, scope, host)) { nativeRefresh.listing = false; renderManager(); } }
    }
    async function refreshNativeModels() {
      syncWorkspace();
      const row = nativeRefresh.rows.find(item => nativeConnectionKey(item) === nativeRefresh.selected);
      if (!row || !nativeRefresh.open || nativeRefresh.listing || nativeRefresh.reading || nativeSource(row.agent)?.enabled !== true || managerKind !== "ai") return;
      const scope = workspace, host = manager, version = ++nativeRefresh.version, key = nativeConnectionKey(row);
      const current = () => currentNativeRefresh(version, scope, host) && nativeRefresh.selected === key;
      nativeRefresh.reading = true; nativeRefresh.notice = nativeRefresh.error = ""; renderManager();
      try {
        const query = new URLSearchParams({ agent: row.agent, chat_id: row.chat_id, epoch: row.epoch });
        const value = await api("/api/management/native-model-catalog?" + query);
        if (!current()) return;
        if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join(",") !== "agent,chat_id,epoch,native_catalog_ready,workspace_id"
            || value.workspace_id !== scope || value.agent !== row.agent || value.chat_id !== row.chat_id || value.epoch !== row.epoch || value.native_catalog_ready !== true) throw Error("原生目录确认不属于所选连接，请重新读取连接列表。");
        const profileVersion = requestVersion, received = catalog(await api("/api/profiles"));
        if (!current()) return;
        const source = received.native_profile_capabilities?.[row.agent];
        if (received.workspace_id !== scope || source?.enabled !== true || source.catalogue_available !== true || source.source_chat_id !== row.chat_id) throw Error("原生目录来源已变化；原配置保留，请重新读取连接列表。");
        if (profileVersion !== requestVersion) throw Error("配置列表在读取期间已变化；请刷新配置核对，没有自动重读。");
        // Invalidate an older ordinary catalog read; it must not overwrite this
        // explicitly scoped result after it arrives. No saved parameters change.
        requestVersion++; loading = null; data = received; reconcileNativeAttach(); onCatalog(received); if (picker) renderPicker();
        nativeRefresh.notice = `已读取 ${agents.labels[row.agent]} 目录快照（${source.models.length} 项）；实际可用性在发送时重新核验。本次只更新候选，不修改已保存参数。`;
      } catch (error) {
        if (current()) nativeRefresh.error = readable(error, "模型目录暂未完整核验；原配置保留，请重新读取连接列表。");
      } finally { if (current()) { nativeRefresh.reading = false; renderManager(); } }
    }
    function nativeRefreshPanel() {
      const scope = workspace, host = manager, version = nativeRefresh.version;
      const current = () => sameWorkspace(scope) && manager === host && managerKind === "ai" && nativeRefresh.version === version;
      const details = aiControl(node("details", "profile-identifiers")); details.open = nativeRefresh.open;
      details.append(humanControl(node("summary", "", "更新原生模型目录")), node("p", "profile-note", "读取已连接对话的候选目录，不打开会话或发送消息；也可在原生对话点“更新配置”。"), node("p", "profile-note", "未指定的参数沿用原生当前设置。目录支持不代表账号已获调用授权，实际可用性在发送时重新核验。"));
      details.addEventListener("toggle", () => {
        if (!current() || nativeRefresh.open === details.open) return;
        nativeRefresh.open = details.open;
        if (!details.open) { nativeRefresh.version++; nativeRefresh.listing = nativeRefresh.reading = false; renderManager(); }
      });
      const actions = node("div", "profile-actions"), read = aiControl(action(nativeRefresh.listing ? "正在读取连接…" : "读取已连接对话", () => { if (current()) return readNativeConnections(); }));
      read.disabled = nativeRefresh.listing; actions.append(read); details.append(actions);
      if (nativeRefresh.loaded && nativeRefresh.rows.length) {
        details.append(selectField("原生连接", [["", "请选择已连接对话"], ...nativeRefresh.rows.map(row => [nativeConnectionKey(row), `${agents.labels[row.agent]} · ${row.title}`])], nativeRefresh.selected, value => {
          if (!current() || !nativeRefresh.open) return;
          nativeRefresh.version++; nativeRefresh.selected = nativeRefresh.rows.some(row => nativeConnectionKey(row) === value) ? value : "";
          nativeRefresh.reading = false; nativeRefresh.notice = nativeRefresh.error = ""; renderManager();
        }));
        const update = aiControl(action(nativeRefresh.reading ? "正在更新目录…" : "更新模型目录", () => { if (current()) return refreshNativeModels(); }));
        update.disabled = nativeRefresh.listing || nativeRefresh.reading || !nativeRefresh.rows.some(row => nativeConnectionKey(row) === nativeRefresh.selected);
        details.append(update);
      }
      if (nativeRefresh.notice) details.append(node("p", "profile-note", nativeRefresh.notice));
      if (nativeRefresh.error) { const error = node("p", "profile-error", nativeRefresh.error); error.setAttribute("role", "alert"); details.append(error); }
      return details;
    }
    function connectedAgents() {
      const rows = (inventory().agents || []).filter(row => agents.has(row?.id)).map(row => ({ ...row }));
      for (const backend of ["codex", "pi", "hermes"]) if (nativeSource(backend)?.enabled === true) { const row = rows.find(row => row.id === backend); if (row) row.available = true; else rows.push({ id: backend, available: true }); }
      return rows;
    }
    function modelFor(value) { const backend = agents.backendOfInstance(value.agent_instance_id); return value.model_id ? inventory().models?.find(row => row.id === value.model_id && row.agent_id === backend) : inventory().agents?.find(row => row.id === backend); }
    function nativeModel(value) { const source = nativeSource(agents.backendOfInstance(value.agent_instance_id)); return source?.enabled && source.catalogue_available && value.model_id ? source.models.find(row => row.id === value.model_id) : null; }
    function effortOptions(source) { return source?.explicit_effort_supported === true && Array.isArray(source.supported_efforts) ? source.supported_efforts.filter(item => efforts.has(item)) : []; }
    function textAvailable(value) { if (reservedNativeModel(value.model_id)) return false; const agent = inventory().agents?.find(row => row.id === agents.backendOfInstance(value.agent_instance_id)); return agent?.available === true && (!value.model_id || modelFor(value)?.available === true); }
    function supported(value) { return [...new Set([...(textAvailable(value) ? effortOptions(modelFor(value)) : []), ...effortOptions(nativeModel(value)), ...(value.agent_instance_id === "local/hermes" && !value.model_id && nativeSource("hermes")?.enabled ? hermesRequestEfforts : [])])]; }
    function nativeSupported(value) {
      if (nativeSource(agents.backendOfInstance(value.agent_instance_id))?.enabled !== true) return false;
      if (!value.model_id) return value.reasoning_effort === null || value.agent_instance_id === "local/hermes" && hermesRequestEfforts.includes(value.reasoning_effort);
      const model = nativeModel(value); return !!model && (value.reasoning_effort === null || effortOptions(model).includes(value.reasoning_effort));
    }
    function modelChoices(backend) {
      const rows = new Map((inventory().models || []).filter(row => row.agent_id === backend && !reservedNativeModel(row.id)).map(row => [row.id, { ...row, available: inventory().agents?.some(agent => agent.id === backend && agent.available === true) && row.available === true }]));
      const source = nativeSource(backend); if (source?.enabled && source.catalogue_available) for (const row of source.models) { const text = rows.get(row.id); rows.set(row.id, { ...(text || row), available: true }); }
      return [...rows.values()];
    }
    function modelLabel(row) { return row.agent_id === "hermes" && row.provider ? `${row.provider} · ${row.model}` : row.agent_id === "pi" && row.provider ? `${row.provider} · ${row.name}${row.name === row.model ? "" : ` (${row.model})`}` : row.name || row.model || row.id; }
    function nativeHermesSelection(value) { return value.agent_instance_id === "local/hermes" && (value.model_id.startsWith(hermesNativePrefix) || !value.model_id && nativeSource("hermes")?.enabled === true); }
    function effortLabel(value) { return nativeHermesSelection(value) ? value.reasoning_effort === "none" ? "none（原生关闭请求）" : value.reasoning_effort ?? "沿用原生当前请求强度" : value.agent_instance_id === "local/pi" && value.model_id.startsWith(piNativePrefix) && value.reasoning_effort === "none" ? "off（不指定推理等级）" : value.reasoning_effort ?? "Agent 默认"; }
    function savedModelLabel(value) { const model = nativeModel(value); return model ? modelLabel(model) : reservedNativeModel(value.model_id) ? "已保存的原生模型" : value.model_id ? value.model_id.slice((value.agent_instance_id + "/").length) : nativeSource(agents.backendOfInstance(value.agent_instance_id))?.enabled ? "沿用原生当前设置" : "跟随 Agent 配置"; }
    function validEditor(value) {
      const validName = value.name.trim() && Array.from(value.name).length <= 40 && !/[\x00-\x1f\x7f]/.test(value.name);
      const nameOnly = editor?.original && ["agent_instance_id", "device_id", "model_id", "reasoning_effort"].every(key => value[key] === editor.original[key]);
      const textSupported = textAvailable(value) && (value.reasoning_effort === null || effortOptions(modelFor(value)).includes(value.reasoning_effort));
      return !!(validName && value.device_id === "local" && (nameOnly || textSupported || nativeSupported(value)));
    }
    function selectField(label, values, value, change) {
      const field = node("label", "profile-field"), control = node("select"); field.append(node("span", "", label));
      for (const [id, text, disabled] of values) { const option = node("option", "", text); option.value = id; option.disabled = disabled === true; option.selected = id === value; control.append(option); }
      control.value = value; control.addEventListener("change", () => change(control.value)); field.append(control); return field;
    }
    function openEditor(existing = null, copy = false) {
      syncWorkspace(); if ((!existing || copy) && (pendingCreation || creationInvalid || creationBusy)) return false;
      const original = existing ? profile(existing) : null;
      const connections = connectedAgents(), first = connections.find(row => row.available === true) || connections[0];
      editor = { kind: "ai", workspace, original, copy, saving: false, error: "", uncertain: false, value: original ? fields(original) : { name: "", agent_instance_id: first ? agents.instanceId(first.id) : "", device_id: "local", model_id: "", reasoning_effort: null } };
      if (copy) editor.value.name = Array.from(editor.value.name + " 副本").slice(0, 40).join(""); renderManager(); manager?.querySelector("input")?.focus(); return true;
    }
    function editorPanel() {
      const captured = editor, value = captured.value, panel = aiControl(node("section", "profile-editor")), head = node("div", "rbw-title-row");
      head.append(node("h3", "", captured.copy ? "复制 AI 配置" : captured.original ? "编辑 AI 配置" : "新建 AI 配置"), humanControl(action("收起", () => { if (!captured.saving) { editor = null; renderManager(); } }))); panel.append(head);
      const grid = node("div", "profile-editor-grid"), nameField = node("label", "profile-field profile-wide"), name = node("input"); name.value = value.name; name.maxLength = 80; name.placeholder = "给这份配置起个名字"; name.setAttribute("aria-label", "AI 配置名称"); nameField.append(node("span", "", "名称"), name); grid.append(nameField);
      name.disabled = captured.saving || !!captured.creation_id && !!pendingCreation;
      name.addEventListener("input", () => { if (name.disabled) return; value.name = name.value; refreshSave(); });
      const backend = agents.backendOfInstance(value.agent_instance_id);
      const agentChoices = connectedAgents().map(row => [agents.instanceId(row.id), `本机 ${agents.labels[row.id]}${row.available === true ? "" : "（暂不可用）"}`, row.available !== true]);
      if (agents.isInstance(value.agent_instance_id) && !agentChoices.some(row => row[0] === value.agent_instance_id)) agentChoices.push([value.agent_instance_id, `原配置 · ${agentName(value.agent_instance_id)}（当前未接入）`, true]);
      if (!agentChoices.length) agentChoices.push(["", "暂无已接入 Agent", true]);
      grid.append(selectField("Agent", agentChoices, value.agent_instance_id, id => { value.agent_instance_id = id; value.model_id = ""; value.reasoning_effort = null; renderManager(); }));
      const choices = [["", nativeSource(backend)?.enabled ? "沿用原生当前设置" : "跟随 Agent 配置"], ...modelChoices(backend).map(row => [row.id, modelLabel(row), row.available !== true])];
      if (value.model_id && !choices.some(row => row[0] === value.model_id)) choices.push([value.model_id, "原模型当前未报告", true]);
      grid.append(selectField("大模型", choices, value.model_id, id => { value.model_id = id; value.reasoning_effort = null; renderManager(); }));
      const strength = [["", nativeHermesSelection(value) ? "沿用原生当前请求强度" : "Agent 默认"], ...supported(value).map(id => [id, effortLabel({ ...value, reasoning_effort: id })])];
      if (value.reasoning_effort && !strength.some(row => row[0] === value.reasoning_effort)) strength.push([value.reasoning_effort, `${value.reasoning_effort}（当前未确认支持）`, true]);
      grid.append(selectField(nativeHermesSelection(value) ? "原生请求强度" : "强度", strength, value.reasoning_effort ?? "", id => { value.reasoning_effort = id || null; renderManager(); }));
      const device = node("div", "profile-field"); device.append(node("span", "", "运行设备"), node("strong", "", inventory().device?.name || "这台电脑")); grid.append(device); panel.append(grid);
      panel.append(node("p", "profile-note", "保存只修改配置；原生对话遇到新版本需重新选择，已有回答和冻结草稿不变。"));
      if (backend === "pi" && nativeModel(value)?.native_thinking_levels.includes("off")) panel.append(node("p", "profile-note", "Pi 的 off 表示不指定推理等级，不代表关闭厂商思考；Agent 默认沿用原生当前设置。"));
      if (backend === "hermes" && nativeSource("hermes")?.enabled) panel.append(node("p", "profile-note", "Hermes 原生请求强度；提供商可能映射。未指定时沿用原生当前设置；none 是原生关闭请求。"));
      for (const control of grid.querySelectorAll("select")) control.disabled = captured.saving || !!captured.creation_id && !!pendingCreation;
      aiControl(name); for (const control of grid.querySelectorAll("select")) aiControl(control);
      const error = node("p", "profile-error", captured.error); error.hidden = !captured.error; error.setAttribute("role", "alert"); panel.append(error);
      const actions = humanControl(node("div", "profile-actions")), save = aiControl(action(captured.saving ? "正在保存…" : "保存配置", () => saveEditor(captured), "rbw-button rbw-primary"));
      const refreshSave = () => { save.disabled = captured.saving || captured.uncertain || !validEditor(value); };
      refreshSave(); actions.append(action("取消", () => { if (!captured.saving) { editor = null; renderManager(); } }), save); panel.append(actions); return panel;
    }
    async function saveEditor(captured) {
      syncWorkspace();
      if (editor !== captured || captured.workspace !== workspace || captured.saving || captured.uncertain || !validEditor(captured.value)) return;
      const original = captured.original, nameOnly = original && ["agent_instance_id", "device_id", "model_id", "reasoning_effort"].every(key => captured.value[key] === original[key]);
      const body = { ...(nameOnly ? { name: captured.value.name } : fields(captured.value)), ...(original ? { revision: original.revision } : {}) };
      const route = original ? `/api/profiles/ai/${original.id}${captured.copy ? "/copy" : ""}` : "/api/profiles/ai";
      if (!original || captured.copy) {
        if (pendingCreation || creationInvalid || creationBusy) return;
        const creationId = window.crypto?.randomUUID?.() || globalThis.crypto?.randomUUID?.();
        if (!workspace || !uuid.test(creationId || "")) { captured.error = "当前工作区或稳定创建编号不可用，尚未提交；原表单保留。"; renderManager(); return; }
        const scope = workspace; captured.saving = true; captured.error = ""; renderManager();
        try {
          const record = creationRecord({ version: 1, creation_id: creationId, action: captured.copy ? "copy" : "create", source: original ? profile(original, false) : null, submitted: false, route, body_json: JSON.stringify({ ...body, creation_id: creationId }) }, false);
          record.request_sha256 = await creationHash(record);
          if (editor !== captured || !sameWorkspace(scope)) return;
          if (!saveCreation(record)) throw Error("浏览器不能保存创建回执，尚未提交；原表单保留。");
          captured.creation_id = creationId; await submitCreation(record, captured);
        } catch (error) { if (editor === captured && sameWorkspace(scope)) captured.error = readable(error, "创建参数暂时不能核对，尚未提交；原表单保留。"); }
        finally { if (editor === captured && sameWorkspace(scope)) { captured.saving = false; renderManager(); } }
        return;
      }
      const scope = workspace; captured.saving = true; captured.error = ""; renderManager();
      try {
        const received = profile((await api(route, { method: "PUT", body: JSON.stringify(body) }, false)).profile);
        if (!sameWorkspace(scope)) return;
        if (data) data = { ...data, ai_profiles: [...data.ai_profiles.filter(row => row.id !== received.id), received] };
        if (editor === captured) editor = null; renderManager(); onCatalog(data, received);
        await load(true).catch(() => { if (sameWorkspace(scope) && manager?.isConnected !== false) manager?.append(node("p", "profile-note", "配置已保存；列表暂时未刷新，可稍后重新读取。")); });
      } catch (error) {
        if (editor !== captured || !sameWorkspace(scope)) return;
        captured.error = error.status === 409 ? "这份配置已在另一入口更新。当前表单仍保留，请刷新列表后重新核对；没有覆盖新版本。" : readable(error, "暂时无法确认是否保存。请刷新配置列表查看，没有自动重复保存。");
        captured.uncertain = !error.status || error.status >= 500;
      } finally { if (sameWorkspace(scope)) { captured.saving = false; renderManager(); } }
    }
    function memoryEditFields(value) { return Object.fromEntries(memoryFields.map(key => [key, value[key]])); }
    function openMemoryEditor(existing = null, copy = false) {
      const original = existing ? memoryProfile(existing) : null;
      editor = { kind: "memory", original, copy, lockedMode: !copy && original?.locked_mode === true, saving: false, error: "", uncertain: false, value: original ? memoryEditFields(original) : { name: "", description: "", habits: "", program: "hindsight", connection_id: "local/hindsight", mode: "read_only" } };
      if (copy && original?.source_kind === nativeMemoryKind && original.description === "只读Hermes原有记忆内容；Hermes仍可按原设置更新，本入口不提交保存。") editor.value.description = "";
      if (copy) editor.value.name = Array.from(editor.value.name + " 副本").slice(0, 40).join(""); renderManager(); manager?.querySelector("input")?.focus();
    }
    function validMemoryEditor(value) {
      return !!(typeof value.name === "string" && value.name.trim() && Array.from(value.name).length <= 40 && !/[\x00-\x1f\x7f]/.test(value.name) && typeof value.description === "string" && Array.from(value.description).length <= 200 && !value.description.includes("\0") && typeof value.habits === "string" && Array.from(value.habits).length <= 4000 && !value.habits.includes("\0") && value.program === "hindsight" && value.connection_id === "local/hindsight" && memoryModes.has(value.mode));
    }
    function memoryEditorPanel() {
      const captured = editor, value = captured.value, panel = aiControl(node("section", "profile-editor")), head = node("div", "rbw-title-row");
      head.append(node("h3", "", captured.copy ? "复制为新空记忆库" : captured.original ? "编辑记忆配置" : "新建记忆配置"), humanControl(action("收起", () => { if (!captured.saving) { editor = null; renderManager(); } }))); panel.append(head);
      const grid = node("div", "profile-editor-grid");
      function textField(label, field, limit, multiline = false) {
        const wrapper = node("label", "profile-field profile-wide"), control = node(multiline ? "textarea" : "input"); control.value = value[field]; control.maxLength = limit * 2; if (multiline) control.rows = field === "habits" ? 5 : 2; control.setAttribute("aria-label", label); wrapper.append(node("span", "", `${label}${field === "name" ? "" : "（可选）"}`), control);
        control.addEventListener("input", () => { value[field] = control.value; refreshSave(); }); grid.append(wrapper);
      }
      textField("记忆配置名称", "name", 40); textField("用途说明", "description", 200, true);
      const program = node("div", "profile-field"); program.append(node("span", "", "记忆程序"), node("strong", "", "Hindsight")); grid.append(program);
      const connection = data?.memory_connections.find(row => row.id === "local/hindsight"), connectionField = node("div", "profile-field"); connectionField.append(node("span", "", "服务连接"), node("strong", "", connection?.label || "这台 Mac 的 Hindsight")); if (connection && !connection.available) connectionField.append(node("p", "profile-note", connection.reason || "连接当前不可用；可保存待连接配置。")); grid.append(connectionField);
      if (captured.lockedMode) {
        const modeField = node("div", "profile-field"); modeField.append(node("span", "", "使用方式"), node("strong", "", "只读取（原有记忆固定只读）")); grid.append(modeField);
      } else grid.append(selectField("使用方式", [["read_only", "只读取"], ["read_write", "读取并记住"]], value.mode, mode => { value.mode = mode; renderManager(); }));
      textField("身份与习惯", "habits", 4000, true); panel.append(grid);
      panel.append(node("p", "profile-note", captured.copy ? "会建立一个新的独立记忆范围；不复制原库的数据，也不共用原库。" : captured.original ? "修改名称和使用规则保留配置 ID 与原记忆库。更换程序或迁移资料须另外处理。" : "保存后建立独立记忆范围。默认只读取，不会导入已有聊天或其他记忆库。"));
      if (captured.lockedMode) panel.append(node("p", "profile-note", "小白兔只读取Hermes原有记忆内容，不保存新交互或修改原库。Hermes仍可按原设置更新这份记忆。"));
      panel.append(node("p", "profile-note", value.mode === "read_write" ? connection?.processing_notice || "记忆保存会使用本机 Hindsight 配置的外部 DeepSeek 提取。本轮允许保存的新文字会提交给该处理连接；不会自动搬入整段旧聊天。" : "只读取不会保存本轮新交互。当前聊天上文仍会带入；身份与习惯是本轮额外说明。"));
      const error = node("p", "profile-error", captured.error); error.hidden = !captured.error; error.setAttribute("role", "alert"); panel.append(error);
      const actions = humanControl(node("div", "profile-actions")), save = aiControl(action(captured.saving ? "正在保存…" : captured.copy ? "保存新空库配置" : "保存记忆配置", () => saveMemoryEditor(captured), "rbw-button rbw-primary"));
      const refreshSave = () => { save.disabled = captured.saving || captured.uncertain || !validMemoryEditor(value) || captured.lockedMode && value.mode !== "read_only"; }; refreshSave(); actions.append(action("取消", () => { if (!captured.saving) { editor = null; renderManager(); } }), save); panel.append(actions); return panel;
    }
    async function saveMemoryEditor(captured) {
      if (editor !== captured || captured.saving || captured.uncertain || !validMemoryEditor(captured.value) || captured.lockedMode && captured.value.mode !== "read_only") return;
      captured.saving = true; captured.error = ""; renderManager();
      const original = captured.original;
      let body = memoryEditFields(captured.value);
      if (original) body = { revision: original.revision, ...Object.fromEntries((captured.copy ? ["name", "description", "habits", "mode"] : memoryFields.filter(key => captured.value[key] !== original[key])).map(key => [key, captured.value[key]])) };
      if (original && !captured.copy && Object.keys(body).length === 1) body.name = captured.value.name;
      const route = original ? `/api/profiles/memory/${original.id}${captured.copy ? "/copy" : ""}` : "/api/profiles/memory";
      try {
        const received = memoryProfile((await api(route, { method: original && !captured.copy ? "PUT" : "POST", body: JSON.stringify(body) }, false)).profile);
        if (captured.copy && (received.id === original.id || received.bank_ref === original.bank_ref || received.locked_mode || received.source_kind === nativeMemoryKind || received.available && received.bank_state !== "empty")) throw new Error("新空库的身份或状态暂时无法确认，请刷新列表查看；没有自动重复复制。");
        if (data) data = { ...data, memory_profiles: [...data.memory_profiles.filter(row => row.id !== received.id), received] };
        if (editor === captured) editor = null; renderManager(); onCatalog(data, received, "memory");
        await load(true).catch(() => { if (manager?.isConnected !== false) manager?.append(node("p", "profile-note", "记忆配置已保存；列表暂时未刷新，可稍后重新读取。")); });
      } catch (error) {
        if (editor !== captured) return;
        captured.error = error.status === 409 ? "记忆配置已在另一入口更新。表单仍保留，请刷新列表后重新核对，没有覆盖新版本。" : readable(error, "暂时无法确认记忆配置是否保存。请刷新列表查看，没有自动重复创建或复制记忆库。");
        captured.uncertain = !error.status || error.status >= 500;
      } finally { captured.saving = false; renderManager(); }
    }
    function currentNativeSource() { return data?.native_memory_sources.find(row => row.id === nativeMemorySourceId); }
    function nativeSourceKey(source) { return source?.source_ref ?? "legacy-native-source"; }
    function linkedNativeProfile(source = currentNativeSource()) {
      return source?.linked_profile_id || (source && nativeAttach.confirmedProfileId && nativeAttach.confirmedSourceRef === source.source_ref ? nativeAttach.confirmedProfileId : null);
    }
    function reconcileNativeAttach() {
      const source = currentNativeSource(), key = nativeSourceKey(source);
      if (source?.linked_profile_id && nativeAttach.unknownSources.has(key)) {
        nativeAttach.unknownSources.delete(key); nativeAttach.error = "";
        nativeAttach.confirmedProfileId = source.linked_profile_id; nativeAttach.confirmedSourceRef = source.source_ref;
        nativeAttach.notice = "已确认接入Hermes原有记忆，只读取。聊天中的原选择和草稿保留，请自行选择这份配置。";
      }
    }
    async function attachNativeMemory() {
      const source = currentNativeSource(), key = nativeSourceKey(source);
      if (!source?.available || linkedNativeProfile(source) || nativeAttach.saving || nativeAttach.unknownSources.has(key)) return;
      nativeAttach.saving = true; nativeAttach.error = ""; nativeAttach.notice = ""; renderManager();
      try {
        const body = source.source_ref === undefined ? {} : { source_ref: source.source_ref };
        const received = memoryProfile((await api("/api/profiles/memory/native-hermes", { method: "POST", body: JSON.stringify(body) }, false)).profile);
        if (received.source_kind !== nativeMemoryKind || received.locked_mode !== true || received.native_source_ref !== source.source_ref) throw new Error("原有记忆的接入结果暂时无法确认，请刷新配置查看。");
        nativeAttach.confirmedProfileId = received.id; nativeAttach.confirmedSourceRef = source.source_ref;
        if (data) data = { ...data, memory_profiles: [...data.memory_profiles.filter(row => row.id !== received.id), received], native_memory_sources: data.native_memory_sources.map(row => row.id === nativeMemorySourceId && row.source_ref === source.source_ref ? { ...row, linked_profile_id: received.id } : row) };
        nativeAttach.notice = "已接入Hermes原有记忆，只读取。聊天中的原选择和草稿保留，请自行选择这份配置。";
        onCatalog(data); renderManager();
        await load(true).catch(() => { nativeAttach.notice += " 列表暂时未刷新，可稍后重新读取。"; });
        if (!data?.memory_profiles.some(row => row.id === received.id)) nativeAttach.notice += " 列表尚未显示新配置，请刷新配置。";
      } catch (error) {
        const uncertain = !error.status || error.status >= 500;
        if (uncertain) nativeAttach.unknownSources.add(key);
        nativeAttach.errorSourceRef = source.source_ref;
        nativeAttach.error = readable(error, uncertain ? "暂时无法确认是否接入。请刷新配置检查已有接入，没有自动重复提交。" : "原有记忆暂时没有接入，原选择和草稿保留。");
        if (uncertain) await load(true).catch(() => {});
      } finally { nativeAttach.saving = false; renderManager(); }
    }
    function renderMemoryManager() {
      const cards = node("div", "profile-card-grid"), enabled = memoryEnabled();
      const none = aiControl(node("article", "rbw-card")); none.append(node("h3", "", "不使用长期记忆"), node("p", "profile-note", "不查询、不写入长期记忆。当前聊天上文照常保留。"), node("p", "profile-note", enabled ? "新对话默认使用这一项；你可以明确选择已保存的记忆配置。其他记忆程序尚未接入。" : "其他记忆程序尚未接入，暂不能创建、复制或切换记忆库。")); cards.append(none);
      function appendNativeCounts(card, source) {
        const counts = [source.facts === null ? "事实数未确认" : `${source.facts} 条事实`, source.documents === null ? "文档数未确认" : `${source.documents} 份文档`];
        card.append(node("p", "rbw-card-meta", `服务统计：${counts.join(" · ")}。不表示已全部读取。`));
      }
      for (const source of data?.native_memory_sources || []) {
        if (source.linked_profile_id) continue;
        const card = aiControl(node("article", "rbw-card")); card.dataset.nativeMemorySourceId = source.id;
        card.append(node("h3", "", source.name), node("p", "profile-note", "接入后可给不同AI使用。小白兔只读取原有内容；Hermes仍可按原设置更新，不迁移或复制原库。"));
        appendNativeCounts(card, source);
        if (!source.available) card.append(node("p", "profile-note", source.reason || "原有记忆暂不可用，接入与草稿没有改变。"));
        const linked = linkedNativeProfile(source), uncertain = nativeAttach.unknownSources.has(nativeSourceKey(source)), attach = action(linked ? "已接入Hermes原有记忆（只读）" : nativeAttach.saving ? "正在接入…" : "接入Hermes原有记忆（只读）", attachNativeMemory, "rbw-button rbw-primary"); attach.disabled = !!linked || !source.available || nativeAttach.saving || uncertain; card.append(attach);
        if (uncertain) card.append(node("p", "profile-note", "接入结果尚未确认；刷新只检查配置列表，不会重复提交。"));
        else if (nativeAttach.unknownSources.size) card.append(node("p", "profile-note", "之前来源的接入结果仍待确认。当前来源须明确接入，不会自动重复提交。"));
        if (nativeAttach.error && nativeAttach.errorSourceRef === source.source_ref) { const note = node("p", "profile-error", nativeAttach.error); note.setAttribute("role", "alert"); card.append(note); }
        if (nativeAttach.notice && nativeAttach.confirmedSourceRef === source.source_ref) card.append(node("p", "profile-note", nativeAttach.notice)); cards.append(card);
      }
      for (const row of data?.memory_profiles || []) {
        if (row.id === "none") continue;
        const card = aiControl(node("article", "rbw-card")); card.dataset.memoryProfileId = row.id;
        const bankLabel = { empty: "当前记忆库为空", ready: "已有记忆资料", unavailable: "记忆库暂不可用", unknown: "记忆库状态尚未确认" }[row.bank_state];
        card.append(node("h3", "", row.name), node("p", "profile-note", `${current().memory_profile_id === row.id ? "本对话已选 · " : ""}第 ${row.revision} 版 · Hindsight · ${memoryModeLabel(row.mode)}`));
        if (row.description) card.append(node("p", "rbw-card-meta", row.description));
        card.append(node("p", "rbw-card-meta", `${row.connection_label || "这台 Mac 的 Hindsight"} · ${row.bank_label || "独立记忆范围"} · ${bankLabel}`));
        if (row.locked_mode) card.append(node("p", "profile-note", "Hermes原有记忆，只读接入；Hermes仍可按原设置更新。"));
        const nativeSource = row.source_kind === nativeMemoryKind && data?.native_memory_sources.find(source => source.linked_profile_id === row.id);
        if (nativeSource) {
          appendNativeCounts(card, nativeSource);
          if (nativeAttach.notice && nativeAttach.confirmedProfileId === row.id && nativeAttach.confirmedSourceRef === nativeSource.source_ref) card.append(node("p", "profile-note", nativeAttach.notice));
        }
        if (!row.available) card.append(node("p", "profile-note", row.reason || "当前不可用；配置和草稿仍保留。"));
        const actions = humanControl(node("div", "profile-actions")); actions.append(action("编辑记忆配置", () => openMemoryEditor(row))); card.append(actions);
        const details = node("details", "profile-identifiers"); details.append(humanControl(node("summary", "", "更多")), node("p", "", `配置 ID：${row.id}`), node("p", "", `记忆范围引用：${row.bank_ref}`));
        const secondary = humanControl(node("div", "profile-actions")); secondary.append(action("复制为新空库", () => openMemoryEditor(row, true))); details.append(secondary); card.append(details); cards.append(card);
      }
      manager.append(cards); if (editor?.kind === "memory") manager.append(memoryEditorPanel());
    }
    function renderManagement(container, kind = "ai") { if (manager !== container || managerKind !== kind) resetNativeRefresh(); manager = container; managerKind = kind; renderManager(true); if (!data) load().catch(error => { if (manager?.isConnected !== false) { manager.append(aiControl(node("p", "profile-error", readable(error, "配置列表暂时无法读取，已有配置仍保留。")))); } }); }
    function renderManager(force = false) {
      syncWorkspace();
      if (!manager || !force && manager.isConnected === false) return;
      const top = node("div", "rbw-title-row"); top.append(aiControl(node("h3", "", managerKind === "memory" ? "记忆配置" : "AI 配置")));
      const refresh = aiControl(action("刷新配置", () => load(true).catch(error => { manager?.append(aiControl(node("p", "profile-error", readable(error, "配置列表暂时无法读取。")))); }))); top.append(refresh);
      if (managerKind === "ai") { const add = action("新建配置", () => openEditor(), "rbw-button rbw-primary"); add.disabled = !!pendingCreation || creationInvalid || creationBusy; top.append(add); } else if (memoryEnabled()) top.append(action("新建记忆配置", () => openMemoryEditor(), "rbw-button rbw-primary")); manager.replaceChildren(top);
      if (managerKind === "memory") {
        renderMemoryManager(); return;
      }
      const nativeBackends = ["codex", "pi", "hermes"].filter(backend => nativeSource(backend)?.enabled === true), nativeLabels = nativeBackends.map(backend => "原生 " + agents.labels[backend]).join("、");
      manager.append(aiControl(node("p", "profile-note", nativeBackends.length
        ? `这里管理同一份已保存的 AI 参数；普通对话与${nativeLabels}分别选择。管理读取不会启动 Agent。`
        : "这里管理已保存的 AI 参数，在对话中选择配置后使用。")));
      if (pendingCreation || creationInvalid || creationNotice) manager.append(creationPanel());
      if (defaultNotice) manager.append(aiControl(node("p", "profile-note", defaultNotice)));
      const cards = node("div", "profile-card-grid");
      for (const row of data?.ai_profiles || []) {
        const card = aiControl(node("article", "rbw-card")), selected = current().ai_profile_id === row.id;
        const native = nativeSource(agents.backendOfInstance(row.agent_instance_id))?.enabled === true;
        card.dataset.profileId = row.id; card.append(node("h3", "", row.name), node("p", "profile-note", `${selected ? "本对话已选 · " : ""}第 ${row.revision} 版 · 普通对话${row.available ? "可用" : "未接通"}${native ? row.native_available === true ? " · 原生目录支持" : " · 原生目录待核对" : ""}`), node("p", "rbw-card-meta", `${agentName(row.agent_instance_id)} · ${savedModelLabel(row)} · 强度 ${effortLabel(row)}`));
        const reasons = [];
        if (!row.available && row.reason) reasons.push("普通对话：" + row.reason);
        if (native && row.native_available !== true && row.native_reason) reasons.push(`原生 ${agentName(row.agent_instance_id)}：` + row.native_reason);
        const actions = humanControl(node("div", "profile-actions")); actions.append(action("编辑", () => openEditor(row))); card.append(actions);
        const details = node("details", "profile-identifiers"); details.append(humanControl(node("summary", "", "更多")), node("p", "rbw-card-meta", `执行设备：${inventory().device?.name || "这台电脑"}`));
        if (reasons.length) details.append(node("p", "profile-note", "状态说明"), ...reasons.map(reason => node("p", "profile-note", reason)));
        const secondary = humanControl(node("div", "profile-actions")); const copyButton = action("复制配置", () => openEditor(row, true)); copyButton.disabled = !!pendingCreation || creationInvalid || creationBusy; secondary.append(copyButton);
        const defaultRevision = data.default_ai_profile_revision, scope = workspace;
        const makeDefault = action(data.default_ai_profile_id === row.id ? "新对话默认" : "设为新对话默认", async () => {
          if (!sameWorkspace(scope) || defaultBusy || defaultBlocked) return;
          defaultBusy = true; defaultNotice = ""; renderManager();
          try {
            const received = catalog(await api("/api/profiles/default", { method: "POST", body: JSON.stringify({ ai_profile_id: row.id, default_revision: defaultRevision }) }, false));
            if (!sameWorkspace(scope)) return;
            if (received.workspace_id !== undefined && received.workspace_id !== scope) throw Error("默认配置回读工作区不匹配，请刷新核对。");
            if (data && data.default_ai_profile_revision > received.default_ai_profile_revision) defaultNotice = "这次默认选择已确认保存；页面已读取更新版本，继续保留当前显示。";
            else data = received; onCatalog(data);
          } catch (error) {
            if (sameWorkspace(scope)) { defaultBlocked = true; defaultNotice = error.status === 409 ? `“${row.name}”尚未设为默认：另一入口已更新默认选择。请先刷新配置，再明确选择；没有覆盖新版本。` : readable(error, "默认配置暂时没有确认，请先刷新列表核对；没有自动重复保存。"); }
          } finally { if (sameWorkspace(scope)) { defaultBusy = false; renderManager(); } }
        }); aiControl(makeDefault); makeDefault.disabled = defaultBusy || defaultBlocked || data.default_ai_profile_id === row.id || !row.available; secondary.append(makeDefault); details.append(secondary); card.append(details); cards.append(card);
      }
      if (!data) cards.append(aiControl(node("p", "profile-note", "正在读取已保存配置…")));
      else if (!data.ai_profiles.length) cards.append(aiControl(node("p", "profile-note", "还没有 AI 配置。接入可用 Agent 后，可以新建配置；已有聊天和草稿仍保留。")));
      manager.append(cards); if (editor?.kind === "ai") manager.append(editorPanel());
      if (nativeBackends.length) manager.append(nativeRefreshPanel());
    }
    function setInventory() { renderManager(); }
    function newFor(value) { if (!openEditor()) return; editor.value = { ...editor.value, ...value, device_id: "local", reasoning_effort: null }; renderManager(); }
    root.hidden = true;
    root.addEventListener("click", event => { if (event.target === root) close(); });
    root.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      if (event.key === "Tab") { const controls = [...root.querySelectorAll("button")].filter(control => !control.disabled); if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); } else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); } }
    });
    return { load, loadFresh, get, getMemory, open, close, renderManagement, setInventory, newFor, catalog: () => data, selectChanged: () => { if (picker) renderPicker(); renderManager(); } };
  }
  // One compact nameplate for all Agents. It only presents supplied metadata;
  // activating it opens a guide, never changes configuration or performs I/O.
  function renderStatus(root, { agent, device, model, effort, memory, status, observed, language = "zh-CN", onGuide = null } = {}) {
    if (!root) return;
    const english = language === "en", text = (zh, en) => english ? en : zh;
    const value = v => typeof v === "string" && v.trim() ? v.trim().replace(/\s+/g, " ") : null;
    let requested = value(model);
    if (requested && /^local\/(codex|hermes|pi)\/native\/[a-f0-9]{64}$/.test(requested)) requested = text("已选原生模型", "Selected native model");
    else if (requested) requested = requested.replace(/^local\/(codex|hermes|pi|claude|dsh)\//, "");
    const label = [value(agent) || text("Agent 待选择", "Choose an Agent"),
      requested || text("沿用原生配置", "Keep native settings"), value(effort)].filter(Boolean).join(" · ");
    const title = [text("下轮请求配置：", "Next-turn requested configuration: ") + label,
      text("选择不代表原生已执行。", "The selection does not prove native execution."),
      value(memory) && text("记忆：", "Memory: ") + value(memory),
      text("状态：", "Status: ") + (value(status) || text("待核对", "Unverified")),
      value(observed) && text("原生读回：", "Native report: ") + value(observed)].filter(Boolean).join("\n");
    root._rabbitStatusOnGuide = typeof onGuide === "function" ? onGuide : null;
    const stamp = JSON.stringify([language, label, title]);
    if (root._rabbitStatusStamp === stamp) return;
    root._rabbitStatusStamp = stamp;
    root.classList?.add("profile-status");
    root.setAttribute("data-control-interface", "ai");
    root.setAttribute("aria-label", text("对话配置状态", "Conversation configuration status"));
    root.setAttribute("lang", language);
    let plate = root._rabbitNameplate;
    if (!plate || plate.parentNode !== root) {
      plate = document.createElement("button"); plate.type = "button"; plate.className = "profile-nameplate";
      plate.setAttribute("data-control-interface", "ai");
      plate.addEventListener("click", event => {
        if (!root._rabbitStatusOnGuide) return; // The host may delegate guide navigation.
        event.preventDefault?.(); event.stopPropagation?.(); root._rabbitStatusOnGuide();
      });
      root._rabbitNameplate = plate; root.replaceChildren(plate);
    }
    plate.setAttribute("aria-label", text("切换大模型指南", "Open model-switching guide"));
    plate.setAttribute("title", title); plate.textContent = label;
  }
  // The backend owns a managed next-turn selection. This small state machine
  // never changes a frozen send or performs I/O; each native renderer retains
  // its own durable draft, compatibility catalogue and native send protocol.
  function createNativeSelection({ agent, parseProfile }) {
    return createNativeChoice({ agent, parseProfile, agents: ["codex", "pi", "hermes"],
      field: "configuration_selection", failureField: "configuration_selection_error", references: ["ai_profile_id", "ai_profile_revision"],
      fields: ["id", "revision", "name", "agent_instance_id", "device_id", "model_id", "reasoning_effort"],
      belongs: value => value.agent_instance_id === "local/" + agent, memory: false });
  }
  function createNativeMemorySelection({ agent }) {
    return createNativeChoice({ agent, agents: ["codex"], field: "memory_selection", failureField: "memory_selection_error",
      references: ["memory_profile_id", "memory_profile_revision"],
      fields: ["id", "revision", "name", "description", "habits", "program", "connection_id", "mode", "bank_ref", "locked_mode", "source_kind"],
      parseProfile: value => { if (!window.RabbitNativeMemory) throw Error("记忆配置组件尚未就绪。"); return window.RabbitNativeMemory.choice(value, { readOnly: true }); },
      belongs: () => true, memory: true });
  }
  function createNativeChoice({ agent, parseProfile, agents, field, failureField, references: [idField, revisionField], fields, belongs, memory }) {
    const object = value => !!value && typeof value === "object" && !Array.isArray(value);
    const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
    const canonical = value => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
    const copy = value => JSON.parse(JSON.stringify(value));
    let target = null, scope = null, applied = null, catalogKey = null, dirty = false, error = "";
    if (!agents.includes(agent) || typeof parseProfile !== "function") throw Error("原生配置状态入口无效。");
    const key = () => target && canonical(target.selection);
    function decode(value, identity) {
      if (!exact(value, ["workspace_id", "agent", "chat_id", "selection", "profile", "profile_current"])
          || value.workspace_id !== identity.workspace || value.chat_id !== identity.chatId || value.agent !== agent || typeof value.profile_current !== "boolean"
          || !exact(value.selection, ["revision", idField, revisionField])) throw Error();
      const selected = value.selection, revision = selected.revision;
      if (!Number.isSafeInteger(revision) || revision < 0 || selected[idField] !== null && (typeof selected[idField] !== "string" || !uuid.test(selected[idField]))
          || selected[idField] === null && selected[revisionField] !== null || selected[idField] !== null && (!Number.isSafeInteger(selected[revisionField]) || selected[revisionField] < 1)) throw Error();
      if (revision === 0 || selected[idField] === null) {
        if (selected[idField] !== null || selected[revisionField] !== null || value.profile !== null || value.profile_current !== true) throw Error();
      } else if (!value.profile_current) { if (value.profile !== null) throw Error(); }
      else if (!exact(value.profile, fields) || !belongs(value.profile) || value.profile.id !== selected[idField] || value.profile.revision !== selected[revisionField]
          || canonical(parseProfile(value.profile)) !== canonical(value.profile)) throw Error();
      return copy(value);
    }
    function reset(saved, identity) {
      target = null; scope = identity; applied = catalogKey = null; dirty = false; error = "";
      if (saved !== undefined) { const parsed = decode(saved, identity); if (parsed.selection.revision < 1) throw Error("保存的原生配置状态无效；原记录保留。"); target = parsed; }
    }
    function observe(view) {
      try {
        if (Object.hasOwn(view, failureField)) throw Error();
        if (!Object.hasOwn(view, field)) { if (target) throw Error(); error = ""; return; }
        const next = decode(view[field], scope);
        if (target && (next.selection.revision < target.selection.revision || next.selection.revision === target.selection.revision
            && (canonical(next.selection) !== canonical(target.selection) || next.profile_current && target.profile_current && canonical(next.profile) !== canonical(target.profile)))) throw Error();
        if (next.selection.revision === 0) { error = ""; return; }
        dirty ||= canonical(next) !== canonical(target); target = next; error = "";
      } catch (_) { error = "state"; }
    }
    function synchronize({ blocked, apply, save }) {
      if (!target || error) return;
      if (blocked || !target.profile_current) {
        if (dirty) { if (save()) dirty = false; else error = "storage"; }
        return;
      }
      if (applied !== key() || dirty) {
        if (apply(copy(target.profile))) { applied = key(); dirty = false; }
        else error = "storage";
      }
    }
    const managed = () => target !== null;
    const ready = () => !error && (!target || target.profile_current && applied === key());
    const checkpoint = () => target ? copy(target) : undefined;
    const controlled = () => managed() || !!error;
    function message(language = "zh-CN") {
      const en = language === "en";
      if (memory) return error === "storage" ? en ? "Browser storage cannot save the next-turn memory choice. The original draft and receipts remain; sending is disabled." : "浏览器不能保存下轮记忆配置；原草稿和回执保留，暂不能发送。"
        : !ready() ? en ? "Next-turn memory configuration needs verification; the original draft and receipts remain." : "下轮记忆配置待核对；原草稿和回执保留。"
        : managed() ? en ? "The Rabbit specialist manages the next-turn shared memory choice." : "下轮共享记忆配置由小白兔专员管理。" : "";
      return error === "storage" ? en ? "Browser storage is unavailable. The original draft, materials and receipts remain; sending is disabled." : "浏览器不能保存下轮配置；原草稿、材料和回执保留，暂不能发送。"
        : !ready() ? en ? "Next-turn configuration needs verification; the original draft and receipts remain." : "下轮配置待核对；原草稿和回执保留。"
        : managed() ? en ? "The Rabbit specialist manages the next-turn AI configuration. Shared memory stays independently selectable." : "下轮 AI 配置由小白兔专员管理；共享记忆仍可单独选择。" : "";
    }
    return Object.freeze({ reset, observe, synchronize, ready, managed, controlled, message, checkpoint,
      matchesProfile: value => !target || ready() && canonical(value === null ? null : parseProfile(value)) === canonical(target.profile),
      takeCatalogRefresh: available => { if (!available || !managed() || !ready() || catalogKey === key()) return false; catalogKey = key(); return true; },
      catalogStarted: () => { if (managed() && ready()) catalogKey = key(); }
    });
  }
  window.RabbitProfiles = { create, profile, memoryProfile, nativeMemorySource, memoryRecall, memoryInfo, snapshot, sameSnapshot, selection, catalog, renderStatus, createNativeSelection, createNativeMemorySelection };
})();
