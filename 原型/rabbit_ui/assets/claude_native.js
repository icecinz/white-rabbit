/* Claude SDK records and permissions retain Claude identities and original JSON. */
(() => {
  "use strict";
  const object = value => !!value && typeof value === "object" && !Array.isArray(value);
  const uuid = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
  const text = value => typeof value === "string" && !/[\ud800-\udfff]/u.test(value);
  const sha = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const epochValid = value => typeof value === "string" && value.length > 0 && value.length <= 128 || Number.isSafeInteger(value) && value >= 0;
  const terminal = new Set(["completed", "failed", "interrupted", "rejected"]);
  const statuses = new Set(["submitted_unknown", "accepted", ...terminal]);
  const requestStates = new Set(["pending", "unknown", "answered", "expired"]);
  const encode = value => JSON.stringify(value);
  const canonical = value => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
  const memoryCodec = () => { if (!window.RabbitNativeMemory) throw Error("共享记忆组件尚未加载；原选择与草稿保留。"); return window.RabbitNativeMemory; };
  const node = (tag, cls, value) => { const result = document.createElement(tag); if (cls) result.className = cls; if (value !== undefined) result.textContent = String(value); if (["button", "input", "select", "textarea", "summary"].includes(tag)) result.setAttribute("data-control-interface", "human"); return result; };
  function create({ root, api, getWorkspace = () => "", onError = () => {}, openDocument = null, session, materialFiles = null, materialInputs = false, settingsRoot = null }) {
    const chatId = session?.id;
    if (!root || typeof api !== "function" || !uuid(chatId)) throw Error("Claude原生页面入口无效。");
    const apiRoot = "/api/claude-chats/" + chatId;
    if (settingsRoot !== null && typeof settingsRoot.append !== "function") throw Error("原生对话设置入口无效。");
    const settingsPanel = settingsRoot ? node("section", "rbs-settings") : null;
    if (settingsPanel) { settingsPanel.setAttribute("data-control-interface", "ai"); settingsRoot.append(settingsPanel); }
    let workspace = "", storageKey = "", draft = "", draftMaterials = [], draftRevision = 0, pending = null, answerJournal = Object.create(null), storageInvalid = false, materialsEnabled = materialInputs === true;
    let enabled = true, visible = false, destroyed = false, language = "zh-CN", generation = 0, writeVersion = 0, reading = false, writing = false, posting = false, readPending = false, timer = null;
    let snapshot = null, readable = false, errorText = "", notice = "", displayScope = null;
    let openingForSend = false, autoOpenBlocked = false, viewVersion = 0, filePicker = null;
    let selectedMemory = null, memoryRows = [], memoryReadable = false, memoryReading = false, memoryRequest = 0, memoryVersion = 0, memoryError = "", memorySourcesError = "";
    const memoryEvidence = new Map(), memoryCards = new Map(), memoryContexts = new Map();
    const requestNodes = new Map(), burned = new Set(); let digests = new Map();
    const handoffView = window.RabbitHandoffView?.create();
    const materialDraft = window.RabbitClaudeMaterialDraft;
    const nativeMaterials = window.RabbitNativeMaterials?.create({ api, files: materialFiles, getWorkspace, onError });
    const materialRefs = value => { if (materialDraft) return materialDraft.refs(value); if (value === undefined || Array.isArray(value) && !value.length) return []; throw Error("Claude文件组件尚未就绪，整份草稿保留。"); };
    const materialsReady = () => !draftMaterials.length || materialsEnabled && !!materialDraft && !!nativeMaterials;
    const tr = (zh, en) => language === "en" ? en : zh;
    function button(zh, en, callback) { const result = node("button", "rbs-button", tr(zh, en)); result.type = "button"; result.setAttribute("data-claude-zh", zh); result.setAttribute("data-claude-en", en); result.addEventListener("click", callback); return result; }
    const statusCard = node("section", "profile-status"), fallbackSettings = settingsPanel ? null : node("details", "rbs-setting-details"), fallbackTitle = fallbackSettings ? node("summary") : null;
    if (fallbackSettings) fallbackSettings.append(fallbackTitle);
    const page = node("div", "rbs-page"), head = node("header", "rbs-header"), connection = node("p", "rbs-status"), notices = node("div", "rbs-notices"), messages = node("div", "rbs-feed"), requests = node("section", "rbs-requests"), rawRecords = node("details", "rbs-item"), rawSummary = node("summary"), rawPre = node("pre", "rbs-native"), composer = node("div", "rbs-composer"), input = node("textarea", "rbs-input"), composerActions = node("div", "rbs-actions");
    const openButton = button("连接 Claude", "Connect Claude", () => mutate("open", {})), disconnectButton = button("断开连接", "Disconnect", disconnect), sendButton = button("发送", "Send", submit);
    const recovery = settingsPanel ? node("details", "rbs-setting-details") : null, recoveryLabel = recovery ? node("summary") : null;
    if (recovery) { const controls = node("div", "rbs-actions"); controls.append(openButton, disconnectButton); recovery.append(recoveryLabel, controls); }
    const materialBox = node("section", "rbn-materials");
    page.setAttribute("data-control-interface", "ai"); composer.setAttribute("data-control-interface", "human"); materialBox.setAttribute("data-control-interface", "ai");
    const operationRecords = node("details", "rbs-operation-records"), operationSummary = node("summary"), historyTools = node("div", "rbs-feed");
    const moreMenu = node("details", "rbs-chat-more"), moreSummary = node("summary");
    operationRecords.append(operationSummary, historyTools); moreMenu.append(moreSummary, connection);
    sendButton.className += " rbs-primary rbs-submit";
    input.rows = 3; input.setAttribute("aria-label", "Claude原生独立草稿"); if (!settingsPanel) moreMenu.append(openButton, disconnectButton); head.append(statusCard, moreMenu); rawRecords.className += " rbs-originals"; rawRecords.append(rawSummary, rawPre); moreMenu.append(rawRecords); composerActions.append(sendButton); composer.append(input, materialBox, composerActions); page.append(head, notices, messages, operationRecords, requests, composer); root.append(page); root.hidden = true;
    const memoryBox = node("div", "rbs-profile-controls"), memoryLabel = node("label", "rbs-field"), memoryTitle = node("span"), memorySelect = node("select", "rbs-control"), memoryInfo = node("p", "rbs-note"), memorySources = node("section", "rbs-feed");
    const memoryRefresh = button("更新记忆配置", "Refresh memory configurations", loadMemoryProfiles);
    memoryBox.setAttribute("data-control-interface", "ai");
    for (const control of [openButton, disconnectButton, sendButton, memoryRefresh]) control.setAttribute("data-control-interface", "ai");
    memoryLabel.append(memoryTitle, memorySelect); memoryBox.append(memoryLabel, memoryRefresh, memoryInfo); if (settingsPanel) settingsPanel.append(memoryBox, recovery); else { fallbackSettings.append(memoryBox); composer.replaceChildren(fallbackSettings, input, materialBox, composerActions); } page.replaceChildren(head, notices, messages, operationRecords, memorySources, requests, composer);
    function memoryBlocked() { return !enabled || destroyed || writing || !!pending || storageInvalid || unsettled() || pendingRequest() || snapshot?.activity.state !== "idle"; }
    function memoryReady() { return selectedMemory === null || !!materialDraft && !!window.RabbitNativeMemory && memoryReadable && memoryRows.some(row => row.mode === "read_only" && canonical(row) === canonical(selectedMemory)); }
    memorySelect.addEventListener("change", () => {
      if (memoryBlocked()) { render(); return; }
      const row = memoryRows.find(value => value.id === memorySelect.value && value.mode === "read_only");
      if (memorySelect.value && (!memoryReadable || !row)) { render(); return; }
      const previous = selectedMemory; selectedMemory = row ? memoryCodec().choice(row, { readOnly: true }) : null; memoryVersion++;
      if (!save()) { selectedMemory = previous; memoryError = "浏览器不能保存共享记忆选择；原选择与草稿保留。"; } render();
    });
    async function loadMemoryProfiles() {
      syncWorkspace(); if (destroyed || !enabled || !workspace || writing || !Object.hasOwn(snapshot || {}, "memory_intents")) return;
      const stamp = generation, request = ++memoryRequest, writes = writeVersion, version = memoryVersion;
      const current = () => sameScope(stamp) && request === memoryRequest && writes === writeVersion && version === memoryVersion;
      memoryReading = true; render();
      try {
        const value = await api("/api/management/memory-profiles"); if (!current()) return;
        if (!exact(value, ["workspace_id", "scope", "connection_checked", "inference_performed", "memory_profiles"]) || value.workspace_id !== workspace || value.scope !== "saved_memory_parameters" || value.connection_checked !== false || value.inference_performed !== false || !Array.isArray(value.memory_profiles) || value.memory_profiles.length > 128) throw Error();
        const rows = value.memory_profiles.map(row => memoryCodec().choice(row)); if (new Set(rows.map(row => row.id)).size !== rows.length) throw Error();
        memoryRows = rows; memoryReadable = true; memoryError = "";
      } catch (_) { if (current()) { memoryReadable = false; memoryError = "共享记忆目录暂不可读；原选择与草稿保留。"; } }
      finally { if (sameScope(stamp) && request === memoryRequest) { memoryReading = false; render(); } }
    }
    if (materialFiles && materialDraft && nativeMaterials?.textInput) {
      filePicker = nativeMaterials.textInput({ state: fileInputState, append: ref => {
        if (!fileInputState() || ref?.kind !== "text") throw Error("当前Claude草稿不可添加文件；原材料保留。");
        const previous = draftMaterials, revision = draftRevision;
        draftMaterials = materialRefs([...draftMaterials, ref]); draftRevision++;
        if (!save()) { draftMaterials = previous; draftRevision = revision; throw Error("浏览器不能保存文件选择；原材料保留。"); }
        return true;
      }, changed: render });
      if (filePicker) composerActions.replaceChildren(filePicker.root, sendButton);
    }
    function fileInputState() {
      if (!materialsEnabled || !materialDraft || !nativeMaterials || !workspace || workspace !== String(getWorkspace() || "") || !enabled || destroyed
          || !visible || root.hidden || document.hidden || !readable || writing || openingForSend || pending || storageInvalid || unsettled() || pendingRequest()
          || snapshot?.activity.state !== "idle" || Object.values(answerJournal).includes("submitted_unknown")) return null;
      return { key: canonical([workspace, chatId, generation, viewVersion, draftRevision, draft, draftMaterials, selectedMemory, memoryVersion, snapshot?.epoch, snapshot?.binding?.session_id]), refs: materialRefs(draftMaterials) };
    }
    function save() { if (!storageKey || storageInvalid) return false; try { window.sessionStorage.setItem(storageKey, encode({ version: selectedMemory || pending?.memory_profile ? 3 : materialDraft ? 2 : 1, draft, ...(selectedMemory || pending?.memory_profile ? { selected_memory: selectedMemory } : {}), ...(materialDraft ? { material_refs: draftMaterials, draft_revision: draftRevision } : {}), pending, answer_journal: answerJournal })); return true; } catch (_) { return false; } }
    function syncWorkspace() {
      const value = String(getWorkspace() || ""); if (value === workspace) return;
      save(); workspace = value; generation++; writeVersion++; openingForSend = autoOpenBlocked = false; viewVersion++;
      storageKey = value ? "rabbit.claude-native.page.v1." + encodeURIComponent(value) + "." + chatId : "";
      draft = ""; draftMaterials = []; draftRevision = 0; pending = null; selectedMemory = null; memoryRows = []; memoryReadable = memoryReading = false; memoryError = memorySourcesError = ""; memoryRequest++; memoryVersion++; memoryEvidence.clear(); memoryCards.clear(); memoryContexts.clear(); answerJournal = Object.create(null); storageInvalid = false; snapshot = null; readable = false; errorText = notice = ""; reading = writing = posting = readPending = false; digests.clear(); requestNodes.clear(); burned.clear(); nativeMaterials?.setScope(encode([workspace, chatId, generation, null]));
      try {
        const stored = storageKey && JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
        if (stored) {
          if (!object(stored) || ![1, 2, 3].includes(stored.version) || stored.version >= 2 && !materialDraft || !text(stored.draft) || stored.draft.length > 200000 || !object(stored.answer_journal) || Object.keys(stored.answer_journal).length > 128) throw Error();
          draft = stored.draft;
          if (stored.version === 3) { selectedMemory = stored.selected_memory === null ? null : memoryCodec().choice(stored.selected_memory, { readOnly: true }); }
          if (stored.version >= 2) { draftMaterials = materialRefs(stored.material_refs); if (!Number.isSafeInteger(stored.draft_revision) || stored.draft_revision < 0) throw Error(); draftRevision = stored.draft_revision; }
          if (stored.pending !== null) { const item = stored.pending; if (item?.material_refs !== undefined) { if (stored.version < 2) throw Error(); pending = materialDraft.restore(item, workspace, chatId); } else { if (!object(item) || !uuid(item.client_id) || !text(item.message) || !item.message.trim() || item.message.length > 200000 || !sha(item.request_sha256) || !["submitted_unknown", "accepted"].includes(item.status) || item.draft_revision !== undefined && (!Number.isSafeInteger(item.draft_revision) || item.draft_revision < 0)) throw Error(); pending = { client_id: item.client_id, message: item.message, request_sha256: item.request_sha256, status: item.status, draft_revision: item.draft_revision === undefined ? draftRevision : item.draft_revision }; } }
          for (const [digest, status] of Object.entries(stored.answer_journal)) { if (!sha(digest) || status !== "submitted_unknown") throw Error(); answerJournal[digest] = status; }
        }
      } catch (_) { storageInvalid = true; errorText = "浏览器保存的Claude回执无法读取；原记录保留，尚未提交。"; }
      input.value = draft;
    }
    const sameScope = stamp => !destroyed && stamp === generation && workspace === String(getWorkspace() || "");
    async function digest(value) { if (!window.crypto?.subtle || typeof TextEncoder !== "function") throw Error("当前浏览器不能保存Claude请求指纹，尚未提交。"); const bytes = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)); return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join(""); }
    const promptHash = message => digest(encode({ message, type: "prompt" }));
    function operation(value) { return object(value) && uuid(value.client_id) && ["prompt", "interrupt"].includes(value.kind) && statuses.has(value.status) && sha(value.request_sha256) && (value.session_id === null || uuid(value.session_id)) && (value.chat_id === undefined || value.chat_id === chatId); }
    function promptOperations(value, client) { return [value.operation, ...(value.operations || [])].filter(row => row?.kind === "prompt" && row.client_id === client); }
    function settle(value, materialProof = false) {
      if (!pending) return;
      if (pending.material_refs && !materialProof) return;
      const matches = promptOperations(value, pending.client_id);
      if (matches.some(row => !operation(row) || row.request_sha256 !== pending.request_sha256)) throw Error("Claude原生回执与原消息不匹配；原草稿和编号保留。");
      const item = matches[0]; if (!item || item.status === "submitted_unknown") return;
      if (item.status === "accepted") { const old = pending; pending = { ...pending, status: "accepted" }; if (!save()) { pending = old; throw Error("Claude接收状态暂不能保存，原消息和回执保留。"); } return; }
      if (terminal.has(item.status)) {
        const old = pending, oldDraft = draft, oldRefs = draftMaterials;
        if ((pending.material_refs ? item.status === "completed" : item.status !== "rejected") && draft === pending.message && encode(draftMaterials) === encode(pending.material_refs || []) && (pending.draft_revision === undefined || pending.draft_revision === draftRevision)) { draft = ""; draftMaterials = []; input.value = ""; }
        pending = null;
        if (!save()) { pending = old; draft = oldDraft; draftMaterials = oldRefs; input.value = draft; throw Error("Claude结果已核对，但本地回执暂不能清除；整份草稿保留，不会重复发送。"); }
      }
    }
    async function confirmMaterials(value, stamp, writes, exact = false) {
      const original = pending, scope = encode([snapshot?.epoch, snapshot?.binding?.session_id]);
      if (!original?.material_refs) return;
      const proof = await materialDraft.confirm(original, value, workspace, chatId, exact);
      if (!sameScope(stamp) || writes !== writeVersion || pending !== original || scope !== encode([snapshot?.epoch, snapshot?.binding?.session_id])) return;
      if (!proof) return;
      if (proof.failed) {
        pending = null;
        if (!save()) { pending = original; throw Error("共享记忆失败回执暂不能保存；原草稿与编号保留。"); }
        notice = tr("共享记忆准备失败，尚未发送；草稿保留，可修复后手动发送。", "Shared-memory preparation failed before sending. The draft remains; fix it before sending manually."); return;
      }
      pending = proof.pending;
      if (!save()) { pending = original; throw Error("Claude材料指纹暂不能保存，整份草稿与原编号保留。"); }
      settle({ operation: proof.operation, operations: [] }, true);
    }
    function ready() { return enabled && !destroyed && readable && snapshot?.connection.state === "ready"; }
    function unsettled() { return !![snapshot?.operation, ...(snapshot?.operations || [])].find(row => row?.kind === "prompt" && ["submitted_unknown", "accepted"].includes(row.status)); }
    function pendingRequest() { return snapshot?.requests.some(row => ["pending", "unknown"].includes(row.state)) === true; }
    function idle() { return ready() && uuid(snapshot?.binding?.session_id) && snapshot?.activity.state === "idle" && !unsettled() && !pendingRequest(); }
    // Only the Send gesture may connect a confirmed unopened, idle session.
    function canOpenForSend() {
      return !filePicker?.busy() && enabled && !destroyed && visible && !root.hidden && !document.hidden && readable && !autoOpenBlocked
        && snapshot?.connection.state === "not_open" && !snapshot.connection.problem && snapshot.activity.state === "idle"
        && !pending && !unsettled() && !pendingRequest() && !storageInvalid;
    }
    async function connectForSend() {
      const stamp = generation, view = viewVersion, message = draft, revision = draftRevision,
        memory = canonical(selectedMemory), version = memoryVersion, refs = canonical(draftMaterials);
      if (typeof TextEncoder !== "function" || new TextEncoder().encode(message).length > 128 * 1024) {
        errorText = "消息超过Claude单次发送容量；原稿保留，请缩短后发送。"; render(); return false;
      }
      if (!save()) { errorText = "浏览器不能保存Claude草稿，尚未连接或发送；请保持页面打开。"; render(); return false; }
      openingForSend = autoOpenBlocked = true; render();
      try {
        const opened = await mutate("open", {});
        if (!sameScope(stamp)) return false;
        const currentView = view === viewVersion && visible && !root.hidden && !document.hidden;
        const proof = opened?.view || opened;
        if (!proof || proof.schema_version !== 1 || proof.agent !== "claude" || proof.workspace_id !== workspace || proof.chat_id !== chatId || proof.connection?.state !== "ready") { if (currentView) notice = tr("连接结果待核对；原稿保留，请在设置中核对连接，没有自动发送或重试。", "Connection outcome needs verification. The draft remains; check the connection in Settings. Nothing was sent or retried."); return false; }
        autoOpenBlocked = false;
        return currentView && !filePicker?.busy() && idle() && !pending && !storageInvalid && draft === message && draftRevision === revision
          && memoryVersion === version && canonical(selectedMemory) === memory && canonical(draftMaterials) === refs;
      } finally { if (sameScope(stamp)) { openingForSend = false; render(); } }
    }
    function activeClient() { return uuid(snapshot?.activity?.client_id) ? snapshot.activity.client_id : pending?.client_id || [snapshot?.operation, ...(snapshot?.operations || [])].find(row => row?.kind === "prompt" && ["submitted_unknown", "accepted"].includes(row.status))?.client_id; }
    function requestIdentity(value, record) { return encode([workspace, chatId, value.epoch, value.binding?.session_id, record.epoch, record.token, record.session_id, record.tool_use_id, record.tool_name, record.input]); }
    function requestFingerprint(record) { return requestIdentity(snapshot, record); }
    async function consume(value, stamp, writes = writeVersion) {
      const current = () => sameScope(stamp) && writes === writeVersion;
      if (!current()) return;
      if (!object(value) || value.schema_version !== 1 || value.agent !== "claude" || value.workspace_id !== workspace || value.chat_id !== chatId || !epochValid(value.epoch)
          || !object(value.connection) || !["not_open", "opening", "ready", "failed", "closed"].includes(value.connection.state)
          || !(value.binding === null || object(value.binding) && value.binding.chat_id === chatId && text(value.binding.cwd) && !!value.binding.cwd && (value.binding.session_id === null || uuid(value.binding.session_id)) && typeof value.binding.persisted === "boolean")
          || !object(value.activity) || !["idle", "running", "unknown"].includes(value.activity.state) || !(value.activity.client_id === null || uuid(value.activity.client_id))
          || !Array.isArray(value.native_records) || value.native_records.length > 2000 || !value.native_records.every(row => object(row) && text(row.sdk_type) && !!row.sdk_type && row.sdk_type.length <= 80 && Object.hasOwn(row, "data"))
          || !Array.isArray(value.requests) || value.requests.length > 128 || !value.requests.every(row => object(row) && text(row.token) && !!row.token && row.token.length <= 256 && requestStates.has(row.state) && typeof row.answerable === "boolean" && epochValid(row.epoch) && text(row.tool_name) && !!row.tool_name && row.tool_name.length <= 256 && object(row.input) && text(row.tool_use_id) && !!row.tool_use_id && row.tool_use_id.length <= 256 && uuid(row.session_id) && row.session_id === value.binding?.session_id)
          || !Array.isArray(value.operations) || !value.operations.every(operation) || !(value.operation === null || operation(value.operation))) throw Error("Claude原生视图身份或格式不完整。");
      if (new TextEncoder().encode(encode(value.native_records)).length > 2 * 1024 * 1024 || new TextEncoder().encode(encode(value.requests)).length > 512 * 1024) throw Error("Claude原生记录超过本页读取容量；原稿保留，没有静默截断。");
      if (new Set(value.requests.map(row => row.token)).size !== value.requests.length) throw Error("Claude原生询问编号重复，尚未答复。");
      const lookup = value.operation_lookup;
      if (lookup !== undefined && (!object(lookup) || Object.keys(lookup).sort().join(",") !== "checked,client_id,found" || !uuid(lookup.client_id) || lookup.checked !== true || typeof lookup.found !== "boolean")) throw Error("Claude回执查询没有精确核对。");
      if ([value.operation, ...value.operations].some(row => row?.session_id && value.binding?.session_id && row.session_id !== value.binding.session_id)) throw Error("Claude回执不属于这段原生会话。");
      const next = new Map();
      for (const record of value.requests) { try { next.set(record.token, await digest(requestIdentity(value, record))); } catch (_) { /* Original request stays visible but cannot be answered. */ } if (!current()) return; }
      try { if (pending?.material_refs) { await materialDraft.verify(pending, workspace, chatId); if (value.binding?.session_id !== pending.session_id) throw Error(); } else if (pending && await promptHash(pending.message) !== pending.request_sha256) throw Error(); }
      catch (_) { if (current()) storageInvalid = true; throw Error("Claude原消息或材料指纹无法核对；原记录保留，不会重新提交。"); }
      if (!current()) return;
      if (snapshot && (snapshot.epoch !== value.epoch || snapshot.binding?.session_id !== value.binding?.session_id)) { requestNodes.clear(); burned.clear(); }
      settle(value); if (!storageInvalid) errorText = ""; snapshot = value; digests = next; readable = true; updateMemoryEvidence(value);
    }
    function fail(error, fallback) { const message = String(error?.message || ""); errorText = message && message.length < 300 ? message : fallback; onError(error); }
    function schedule() { clearTimeout(timer); timer = null; if (visible && enabled && !destroyed && !document.hidden) timer = setTimeout(refresh, 1000); }
    async function refresh() {
      syncWorkspace(); if (destroyed || !enabled || !workspace) return;
      if (reading || posting) { readPending = true; return; }
      const stamp = generation, writes = writeVersion; reading = true;
      try { const value = await api(apiRoot + (pending ? "?client_id=" + pending.client_id : "")); if (sameScope(stamp) && writes === writeVersion) { await consume(value, stamp, writes); if (pending?.material_refs && !storageInvalid && sameScope(stamp) && writes === writeVersion) { const receipt = await api(apiRoot + "/receipt?client_id=" + pending.client_id); await confirmMaterials(receipt, stamp, writes, true); } } }
      catch (error) { if (sameScope(stamp) && writes === writeVersion) { readable = false; fail(error, "Claude原生状态暂不可读取；原请求保留。"); } }
      finally { if (sameScope(stamp)) { reading = false; render(); const again = readPending; readPending = false; if (again) await refresh(); else schedule(); } }
    }
    async function mutate(action, body) {
      syncWorkspace(); if (destroyed || !enabled || !readable || writing || !workspace) return null;
      const stamp = generation, writes = ++writeVersion; writing = posting = true; errorText = ""; render();
      try {
        const value = await api(apiRoot + "/" + action, { method: "POST", body: encode(body) }); if (!sameScope(stamp)) return null;
        if (action === "send") { if (pending?.material_refs) await confirmMaterials(value, stamp, writes); else { if (!operation(value.operation) || value.operation.kind !== "prompt" || value.operation.client_id !== body.client_id || value.operation.request_sha256 !== pending?.request_sha256 || value.operation.session_id !== snapshot?.binding?.session_id) throw Error("Claude发送确认与原消息不匹配，原编号保留。"); settle({ operation: value.operation, operations: [] }); } }
        if (value.view) await consume(value.view, stamp, writes); else if (value.schema_version === 1) await consume(value, stamp, writes);
        if (action === "open") { notice = ""; if (ready()) autoOpenBlocked = false; }
        if (action === "disconnect" && value.disconnected === true) { autoOpenBlocked = false; notice = tr("已断开；原生会话仍保留，后台工具退出不作额外推断。", "Disconnected; the native session remains. Background-tool exit is not inferred."); }
        return value;
      } catch (error) {
        if (sameScope(stamp)) { if (action === "send" && error.accepted === false && !pending?.memory_profile && pending?.client_id === body.client_id && pending.status === "submitted_unknown") { const old = pending; pending = null; if (!save()) pending = old; } fail(error, "结果仍待核对，没有自动重复提交。"); }
        return null;
      } finally { if (sameScope(stamp)) { writing = posting = false; render(); await refresh(); if (action === "open") await loadMemoryProfiles(); } }
    }
    async function send() {
      syncWorkspace(); if (filePicker?.busy() || openingForSend || !(idle() || canOpenForSend()) || writing || pending || storageInvalid || !materialsReady() || !memoryReady() || !text(draft) || !draft.trim()) return;
      if (!idle()) { if (await connectForSend()) await send(); return; }
      const view = viewVersion;
      const stamp = generation, message = draft, refs = materialRefs(draftMaterials), memory = selectedMemory && memoryCodec().choice(selectedMemory, { readOnly: true }), revision = draftRevision, epoch = snapshot.epoch, sessionId = snapshot.binding.session_id, client = window.crypto?.randomUUID?.(); if (!uuid(client)) { errorText = "无法生成稳定Claude请求编号，尚未发送。"; render(); return; }
      writing = true; render();
      try {
        if (typeof TextEncoder !== "function" || new TextEncoder().encode(message).length > 128 * 1024) throw Error("消息超过Claude单次发送容量；原稿保留，请缩短后发送。");
        for (const ref of refs) await nativeMaterials.read(ref, { refresh: true });
        const prepared = refs.length || memory ? await materialDraft.prepare(client, message, refs, workspace, chatId, sessionId, revision, memory) : { client_id: client, message, request_sha256: await promptHash(message), status: "submitted_unknown", ...(materialDraft ? { draft_revision: revision } : {}) };
        if (!sameScope(stamp) || filePicker?.busy() || view !== viewVersion || !visible || root.hidden || document.hidden || !idle() || pending || draft !== message || draftRevision !== revision || encode(refs) !== encode(draftMaterials) || epoch !== snapshot.epoch || sessionId !== snapshot.binding.session_id || !materialsReady() || !memoryReady() || canonical(memory) !== canonical(selectedMemory)) return;
        pending = prepared;
        if (!save()) { pending = null; throw Error("浏览器不能保存Claude回执，尚未发送；原稿保留。"); }
      } catch (error) { if (sameScope(stamp)) fail(error, "原消息无法保存，尚未提交。"); }
      finally { if (sameScope(stamp)) { writing = false; render(); } }
      if (sameScope(stamp) && pending?.client_id === client) await mutate("send", pending.material_refs ? JSON.parse(pending.body_json) : { client_id: client, message });
    }
    function submit() {
      syncWorkspace();
      if (!enabled || destroyed || !visible || root.hidden || document.hidden) return;
      return !draft.trim() && (snapshot?.activity.state === "running") ? interrupt() : send();
    }
    async function interrupt() { const client = activeClient(); if (!ready() || snapshot.activity.state !== "running" || writing || !uuid(client)) return; const value = await mutate("interrupt", { client_id: client }); if (value) notice = tr("已提交停止请求，等待Claude原生结果；不能据此推断后台工具已退出。", "Interrupt requested; waiting for the native outcome. Background-tool exit is not inferred."); render(); }
    async function disconnect() { if (!readable || writing || pending || unsettled() || pendingRequest() || !["idle"].includes(snapshot?.activity.state)) return; await mutate("disconnect", {}); }
    function putDraft(value) {
      syncWorkspace();
      if (!object(value) || value.workspace_id !== workspace || !workspace || value.chat_id !== chatId || !enabled || destroyed || filePicker?.busy() || writing || pending || storageInvalid || unsettled() || pendingRequest() || snapshot?.activity.state === "running" || snapshot?.activity.state === "unknown" || draft !== "" || input.value !== "" || draftMaterials.length) throw Error("Claude原生草稿已有内容、请求尚未结束或所属对话已变化；没有覆盖。");
      if (Object.hasOwn(value, "ai_profile")) throw Error("Claude原生对话沿用本机配置；目标模型配置没有放入草稿。");
      const hasMemory = Object.hasOwn(value, "memory_profile"), memory = hasMemory ? value.memory_profile === null ? null : memoryCodec().choice(value.memory_profile, { readOnly: true }) : selectedMemory;
      if (hasMemory && selectedMemory !== null && canonical(memory) !== canonical(selectedMemory)) throw Error("Claude原生对话已有不同的共享记忆选择；没有覆盖。");
      const refs = materialRefs(value.material_refs);
      if (!text(value.text) || !(value.text.trim() || value.text === "" && (refs.length || hasMemory)) || typeof TextEncoder !== "function" || new TextEncoder().encode(value.text).length > 128 * 1024) throw Error("交接文字无效或超过Claude发送容量。");
      if (refs.length && (!materialsEnabled || !nativeMaterials || !value.material_preview)) throw Error("Claude文本文件交接尚未启用或缺少完整预览；整份未放入。");
      if (value.material_preview !== undefined) { if (!refs.length || !nativeMaterials) throw Error("Claude材料预览缺少所选版本；整份未放入。"); nativeMaterials.seed(refs, value.material_preview); }
      const previous = { draft, input: input.value, refs: draftMaterials, memory: selectedMemory, revision: draftRevision };
      draft = value.text; draftMaterials = refs; selectedMemory = memory; draftRevision++; input.value = draft;
      if (!save()) { draft = previous.draft; input.value = previous.input; draftMaterials = previous.refs; selectedMemory = previous.memory; draftRevision = previous.revision; throw Error("浏览器不能保存Claude原生草稿；原内容保留，尚未发送。"); }
      if (hasMemory) memoryVersion++; render(); return true;
    }
    async function answer(record, result, fingerprint) {
      const current = snapshot?.requests.find(row => row.token === record.token);
      if (!ready() || writing || storageInvalid || !current || current.tool_name === "AskUserQuestion" || current.state !== "pending" || current.answerable !== true || current.epoch !== snapshot.epoch || requestFingerprint(current) !== fingerprint || burned.has(current.token)) return;
      const stamp = generation; writing = true; render();
      try {
        const hash = await digest(fingerprint), latest = snapshot?.requests.find(row => row.token === record.token);
        if (!sameScope(stamp) || !ready() || !latest || latest.state !== "pending" || latest.answerable !== true || latest.epoch !== snapshot.epoch || requestFingerprint(latest) !== fingerprint || Object.hasOwn(answerJournal, hash)) return;
        if (Object.keys(answerJournal).length >= 128) throw Error("Claude尚未核对的答复记录过多，尚未答复。");
        answerJournal[hash] = "submitted_unknown"; if (!save()) { delete answerJournal[hash]; throw Error("浏览器不能保存Claude答复回执，尚未提交。"); }
        burned.add(record.token); const writes = ++writeVersion; posting = true;
        const value = await api(apiRoot + "/answer", { method: "POST", body: encode({ token: record.token, epoch: snapshot.epoch, result }) });
        if (sameScope(stamp) && (value.view || value.schema_version === 1)) await consume(value.view || value, stamp, writes);
      } catch (error) { if (sameScope(stamp)) fail(error, "Claude答复结果尚未确认，不会重复答复。"); }
      finally { if (sameScope(stamp)) { writing = posting = false; render(); await refresh(); } }
    }
    function updateMemoryEvidence(value) {
      memoryEvidence.clear(); memoryContexts.clear(); memorySourcesError = "";
      try {
        if (value.memory_intents === undefined) return;
        if (!Array.isArray(value.memory_intents) || value.memory_intents.length > 128) throw Error();
        for (const source of value.memory_intents) {
          const intent = memoryCodec().intent(source, { workspaceId: workspace, chatId, clientKind: "uuid" });
          if (memoryEvidence.has(intent.client_id)) throw Error();
          const expected = pending?.client_id === intent.client_id ? pending : null;
          if (expected && (!expected.memory_profile || expected.raw_request_sha256 !== intent.request_sha256 || canonical(memoryCodec().snapshot(expected.memory_profile)) !== canonical(intent.memory_profile))) throw Error();
          const ops = promptOperations(value, intent.client_id), unique = [...new Map(ops.map(row => [canonical(row), row])).values()];
          if (unique.length > 1) throw Error();
          const op = unique[0], attached = !!(op && operation(op) && op.session_id === value.binding?.session_id && ["accepted", "completed", "failed", "interrupted"].includes(op.status) && intent.state === "ready" && sha(intent.derived_inputs_sha256) && op.request_sha256 === intent.derived_inputs_sha256);
          if (op && intent.state === "failed") throw Error();
          memoryEvidence.set(intent.client_id, { intent, attached });
          if (attached) memoryContexts.set(memoryCodec().contextBlock(intent), true);
        }
      } catch (_) { memoryEvidence.clear(); memoryContexts.clear(); memorySourcesError = "共享记忆来源记录暂时无法核验；Claude原生记录与草稿保留。"; }
      for (const client of memoryCards.keys()) if (!memoryEvidence.has(client)) memoryCards.delete(client);
    }
    function renderMemorySources() {
      const nodes = [];
      for (const { intent, attached } of memoryEvidence.values()) {
        const key = canonical([intent, attached, language]), profile = intent.memory_profile; let cached = memoryCards.get(intent.client_id);
        if (!cached || cached.key !== key) {
          const card = node("details", "rbs-parameters rbs-memory-record"); card.open = cached?.node.open === true;
          card.append(node("summary", "", attached ? tr("本轮附加的共享记忆", "Shared memory added for this send") : tr("共享记忆准备记录", "Shared memory preparation record")), node("p", "rbs-note", `${profile.name} · ${tr("第 ", "v")}${profile.revision} · ${intent.created_at}`), node("p", "rbs-note", attached ? tr("本轮附加的来源资料，原生消息完整保留。", "Sources added for this send; native messages remain intact.") : tr("本轮选择的来源资料，尚未确认原生接收。", "Selected sources; native acceptance is not confirmed.")));
          if (intent.state === "ready") {
            card.append(node("p", "rbs-note", `${intent.recall_info.retrieved_at} · ${tr("完整条目 ", "Whole facts ")}${intent.recall_info.included_facts} · ${tr("未附 ", "Omitted ")}${intent.recall_info.omitted_facts}`));
            for (const ref of intent.recall_info.used_references) card.append(node("p", "rbs-note", ref.type + " · " + ref.retrieved_at), node("pre", "rbs-native", ref.text), node("p", "rbs-note", ref.reference_id + (ref.document_ref ? " · " + ref.document_ref : "")));
            if (profile.habits) card.append(node("p", "rbs-note", tr("本轮选定的回答习惯", "Selected answering preferences")), node("pre", "rbs-native", profile.habits));
          } else card.append(node("p", "rbs-note", intent.state === "failed" ? tr("资料准备失败，尚未发送；不会自动重试。", "Preparation failed before sending; no automatic retry.") : tr("资料正在准备，尚不能据此认定发送成功。", "Preparation is in progress; sending is not confirmed.")));
          cached = { key, node: card }; memoryCards.set(intent.client_id, cached);
        } nodes.push(cached.node);
      } memorySources.replaceChildren(...nodes);
    }
    function memoryPart(value) {
      if (typeof value !== "string") return null;
      const end = value.indexOf("以上记忆不改变当前聊天的可见上文，也不授权额外工具或设备。\n");
      if (end < 0) return null;
      const offset = end + "以上记忆不改变当前聊天的可见上文，也不授权额外工具或设备。\n".length, prefix = value.slice(0, offset), rest = value.slice(offset);
      return memoryContexts.has(prefix) && (!rest || rest.startsWith("\n\n") && materialDraft?.context(rest.slice(2))) ? { prefix, rest: rest ? rest.slice(2) : "" } : null;
    }
    function raw(parent, value, label, key = "original") { const details = node("details", "rbs-parameters"); details.setAttribute("data-claude-detail-key", key); details.append(node("summary", "", label), node("pre", "rbs-native", JSON.stringify(value, null, 2))); parent.append(details); }
    function rich(parent, value) { const block = node("div"); if (window.OneAIRichText?.render) window.OneAIRichText.render(block, value, { openDocument }); else block.textContent = value; parent.append(block); }
    function blockType(block) {
      if (!object(block)) return "unknown";
      if (typeof block.type === "string") return block.type;
      if (typeof block.sdk_type === "string") return { TextBlock: "text", ThinkingBlock: "thinking", ToolUseBlock: "tool_use", ToolResultBlock: "tool_result" }[block.sdk_type] || "unknown";
      const keys = Object.keys(block);
      if (keys.length === 1 && text(block.text)) return "text";
      if (keys.every(key => ["thinking", "signature"].includes(key)) && text(block.thinking) && text(block.signature)) return "thinking";
      if (keys.every(key => ["id", "name", "input"].includes(key)) && text(block.id) && text(block.name) && object(block.input)) return "tool_use";
      if (keys.every(key => ["tool_use_id", "content", "is_error"].includes(key)) && text(block.tool_use_id)) return "tool_result";
      return "unknown";
    }
    function getTextSnapshot() {
      if (!ready() || !workspace || workspace !== String(getWorkspace() || "") || snapshot.workspace_id !== workspace
          || snapshot.chat_id !== chatId || !uuid(snapshot.binding?.session_id) || snapshot.binding.persisted !== true) throw Error("Claude 原生身份或已保存视图尚未核对，不能读取文字快照。");
      const sid = snapshot.binding.session_id, records = [], seen = new Set();
      let incomplete = snapshot.history?.available === false || snapshot.history?.has_more === true || snapshot.native_records_omitted > 0;
      const identifier = value => text(value) && value.length > 0 && value.length <= 256 && !/[\x00-\x1f\x7f]/.test(value);
      function append(record, role, identity) {
        const data = record.data;
        if (!object(data) || data.truncated === true || data.display_truncated === true || data.display_placeholder === true
            || data.message?.truncated === true || data.message?.display_truncated === true || data.message?.display_placeholder === true
            || data.error != null || !identifier(identity)) { incomplete = true; return; }
        const content = record.sdk_type === "SessionMessage" ? data.message?.content : data.content;
        const parts = typeof content === "string" ? [{ type: "text", text: content }] : content;
        if (!Array.isArray(parts)) { incomplete = true; return; }
        for (const [index, original] of parts.entries()) {
          if (blockType(original) !== "text") continue;
          const part = typeof original.sdk_type === "string" ? original.data : original;
          if (!object(part) || !text(part.text) || original.truncated === true || original.display_truncated === true
              || part.truncated === true || part.display_truncated === true || part.display_omitted === true) { incomplete = true; continue; }
          const message_id = identity + "/text/" + index;
          if (seen.has(message_id)) { incomplete = true; continue; }
          seen.add(message_id);
          if (part.text.length) records.push({ message_id, role, content: part.text });
        }
      }
      let batch = [], boundary = false;
      for (const record of snapshot.native_records) {
        const data = record.data;
        if (!object(data)) { incomplete = true; continue; }
        if (data.isMeta === true || data.isCompactSummary === true || data.is_compact_summary === true) continue;
        if (record.sdk_type === "SessionMessage" && ["user", "assistant"].includes(data.type)) {
          // Complete saved SDK records are not reconstructed from later text.
          if (data.session_id !== sid || !identifier(data.uuid)) { incomplete = true; continue; }
          append(record, data.type, "uuid/" + data.uuid);
        } else if (record.sdk_type === "UserMessage") {
          const parts = typeof data.content === "string" ? [{ type: "text", text: data.content }] : data.content;
          if (data.parent_tool_use_id != null || !Array.isArray(parts) || !parts.some(part => blockType(part) === "text")) continue;
          if (batch.length) incomplete = true;
          batch = [{ record, role: "user" }]; boundary = identifier(data.uuid);
        } else if (record.sdk_type === "AssistantMessage") {
          if (data.parent_tool_use_id != null) continue;
          if (!boundary) { incomplete = true; continue; }
          batch.push({ record, role: "assistant" });
        } else if (record.sdk_type === "ResultMessage") {
          if (!batch.length) continue;
          const complete = boundary && uuid(data.uuid) && data.session_id === sid && data.is_error === false
            && (data.subtype === undefined || data.subtype === "success") && !["aborted_streaming", "aborted_tools"].includes(data.terminal_reason)
            && (data.origin === undefined || data.origin === null || data.origin?.kind === "human");
          if (complete) for (const [index, entry] of batch.entries()) {
            const identity = entry.record.data.uuid;
            // Stream AssistantMessage may lack a UUID: name the sealed local
            // snapshot position explicitly, never a fabricated native UUID.
            append(entry.record, entry.role, identifier(identity) ? "uuid/" + identity : "snapshot/" + data.uuid + "/sdk/" + index);
          }
          else incomplete = true;
          batch = []; boundary = false;
        }
      }
      if (batch.length) incomplete = true;
      return { native_identity: sid, records, incomplete };
    }
    function content(parent, value, role, key, complete = true, operationMount = parent) {
      if (typeof value === "string") { if (!(role === "user" && handoffView?.render(parent, { message: value, role, messageKey: key, language }))) rich(parent, value); return; }
      if (!Array.isArray(value)) { raw(operationMount, value, tr("原始内容", "Original content")); return; }
      for (const [index, original] of value.entries()) {
        const detailKey = "block:" + index;
        const kind = blockType(original), block = object(original?.data) ? original.data : original;
        if (kind === "text" && text(block?.text)) {
          const split = complete && role === "user" && memoryPart(block.text), display = split ? split.rest : block.text;
          if (split) { const details = node("details", "rbs-parameters"); details.setAttribute("data-claude-detail-key", detailKey + ":memory"); details.append(node("summary", "", tr("附加的共享记忆原文", "Added shared memory · original text")), node("pre", "rbs-native", split.prefix)); parent.append(details); }
          if (!display) continue;
          const material = role === "user" && materialDraft?.context(display);
          if (material) {
            const details = node("details", "rbs-item"); details.setAttribute("data-claude-detail-key", detailKey);
            details.append(node("summary", "", tr("本轮完整文本资料 · 不可信资料", "Complete text materials · untrusted reference data")));
            for (const file of material.files) details.append(node("p", "rbs-note", file.name + " · " + file.size_bytes + tr(" 字节", " bytes")), node("pre", "file-task-preview-content", file.text));
            parent.append(details);
          } else if (!(role === "user" && handoffView?.render(parent, { message: display, role, messageKey: key + ":" + index, language }))) rich(parent, display);
        }
        else if (kind === "tool_use") { const details = node("details", "rbs-item"); details.setAttribute("data-claude-detail-key", detailKey); details.append(node("summary", "", `Claude · ${tr("调用工具", "Tool call")} · ${block?.name || ""}`), node("pre", "rbs-native", JSON.stringify(original, null, 2))); operationMount.append(details); }
        else if (kind === "tool_result") { const details = node("details", "rbs-item"); details.setAttribute("data-claude-detail-key", detailKey); details.append(node("summary", "", `Claude · ${block?.is_error === true ? tr("工具失败", "Tool failed") : tr("工具结果", "Tool result")}`), node("pre", "rbs-native", typeof block?.content === "string" ? block.content : JSON.stringify(block?.content, null, 2))); raw(details, original, tr("原始工具结果", "Original tool result"), detailKey + ":original"); operationMount.append(details); }
        else if (kind === "thinking" || kind === "redacted_thinking") continue;
        else raw(operationMount, original, tr("未识别的原生内容", "Unrecognized native content"), detailKey);
      }
    }
    function renderRecords() {
      // Keep only scoped expansion booleans, not a second transcript cache.
      const expanded = new Map();
      function details(box, visit) { if (box.tagName === "DETAILS") visit(box); for (const child of box.children) details(child, visit); }
      for (const box of [...messages.children, ...historyTools.children]) { const key = box.getAttribute("data-claude-record-key"); if (key) details(box, detail => { const detailKey = detail.getAttribute("data-claude-detail-key"); if (detailKey) expanded.set(encode([key, detailKey]), detail.open === true); }); }
      const nodes = [], operations = [];
      for (const [index, record] of (snapshot?.native_records || []).entries()) {
        const data = record.data, key = encode([index, record.sdk_type, data?.uuid ?? null]);
        const sessionMessage = record.sdk_type === "SessionMessage" && object(data) && ["user", "assistant"].includes(data.type) && object(data.message);
        const role = record.sdk_type === "UserMessage" ? "user" : record.sdk_type === "AssistantMessage" ? "assistant" : sessionMessage ? data.type : null;
        if (role && object(data)) {
          const nativeContent = sessionMessage ? data.message.content : data.content;
          const toolResultsOnly = role === "user" && Array.isArray(nativeContent) && nativeContent.length > 0 && nativeContent.every(block => blockType(block) === "tool_result");
          const error = record.sdk_type === "AssistantMessage" && data.error != null;
          const box = node("article", "rbs-item" + (role === "user" && !toolResultsOnly ? " rbs-user" : ""));
          if (!toolResultsOnly) { box.className += " rbs-message-" + role; box.setAttribute("data-message-role", role); box.setAttribute("aria-label", role === "user" ? tr("用户消息", "User message") : tr("AI消息", "AI message")); }
          if (toolResultsOnly || error) box.append(node("p", "rbs-item-role", toolResultsOnly ? tr("工具结果", "Tool results") : tr("Claude错误", "Claude error")));
          const body = node("div", "rbs-item-body"), operationBox = node("article", "rbs-item"); operationBox.setAttribute("data-claude-record-key", key);
          content(body, nativeContent, role, key, data.truncated !== true && data.display_truncated !== true, operationBox);
          if (error) body.append(node("pre", "rbs-error", typeof data.error === "string" ? data.error : encode(data.error)));
          if (body.children.length) box.append(body);
          box.setAttribute("data-claude-record-key", key);
          if (toolResultsOnly) { if (body.children.length) operationBox.append(body); } else if (body.children.length || error || role === "user") nodes.push(box);
          if (operationBox.children.length) operations.push(operationBox);
        } else if (record.sdk_type === "ResultMessage" && object(data) && data.is_error === true) {
          const box = node("details", "rbs-item"); box.open = true;
          box.setAttribute("data-claude-record-key", key); box.setAttribute("data-claude-detail-key", "result-error"); box.append(node("summary", "", tr("Claude本轮失败", "Claude turn failed") + " · " + String(data.subtype || "")), node("pre", "rbs-native", JSON.stringify(data, null, 2))); nodes.push(box);
        }
        // System/init, stream, successful result, and future protocol shapes
        // remain inspectable together in the original record view below.
      }
      for (const box of [...nodes, ...operations]) { const key = box.getAttribute("data-claude-record-key"); details(box, detail => { const detailKey = detail.getAttribute("data-claude-detail-key"), identity = encode([key, detailKey]); if (detailKey && expanded.has(identity)) detail.open = expanded.get(identity); }); }
      messages.replaceChildren(...nodes); historyTools.replaceChildren(...operations); operationRecords.hidden = operations.length === 0;
    }
    function renderRequests() {
      const nodes = [], keys = new Set();
      for (const record of snapshot?.requests || []) {
        const fingerprint = requestFingerprint(record), key = record.token; keys.add(key); let card = requestNodes.get(key);
        if (!card || card.fingerprint !== fingerprint) {
          const box = node("article", "rbs-request"); box.append(node("h3", "", `Claude · ${record.tool_name}`)); raw(box, record, tr("原始权限询问", "Original permission request")); const note = node("p", "rbs-note"); box.append(note);
          if (record.tool_name !== "AskUserQuestion") { const controls = node("div", "rbs-actions"); controls.append(button("拒绝", "Deny", () => answer(record, { behavior: "deny" }, fingerprint)), button("仅这次允许", "Allow once", () => answer(record, { behavior: "allow" }, fingerprint))); box.append(controls); }
          card = { node: box, fingerprint, note }; requestNodes.set(key, card);
        }
        const hash = digests.get(key), supported = record.tool_name !== "AskUserQuestion", blocked = !supported || !ready() || writing || storageInvalid || record.state !== "pending" || record.answerable !== true || record.epoch !== snapshot.epoch || !hash || burned.has(key) || Object.hasOwn(answerJournal, hash);
        for (const control of card.node.querySelectorAll("button")) control.disabled = blocked;
        card.note.textContent = !supported ? tr("此原生询问暂需在Claude原生入口处理；这里只展示原始内容，没有自动答复。", "Handle this question in Claude's native entry point. Original content is shown here; no answer is invented.") : record.state !== "pending" ? tr("该询问已结束或仍待核对，旧答复入口不会重新启用。", "This request ended or remains unconfirmed. Old controls are not re-enabled.") : burned.has(key) || hash && Object.hasOwn(answerJournal, hash) ? tr("答复结果尚未确认，不会重复提交。", "The answer is unconfirmed and will not be repeated.") : !record.answerable || record.epoch !== snapshot.epoch ? tr("询问身份尚未确认，不能答复。", "The request identity is unconfirmed and cannot be answered.") : ""; card.note.hidden = !card.note.textContent; nodes.push(card.node);
      }
      for (const key of requestNodes.keys()) if (!keys.has(key)) requestNodes.delete(key); requests.replaceChildren(...nodes);
    }
    function renderProfileStatus() {
      const renderStatus = window.RabbitProfiles?.renderStatus;
      statusCard.hidden = typeof renderStatus !== "function"; connection.hidden = !statusCard.hidden;
      if (fallbackTitle) fallbackTitle.textContent = tr("备用选择", "Fallback choices");
      if (statusCard.hidden) return;
      const status = !readable || storageInvalid || autoOpenBlocked || snapshot?.disconnect_unconfirmed === true ? tr("待核对", "Needs verification")
        : openingForSend || snapshot?.connection.state === "opening" ? tr("正在连接", "Connecting")
        : snapshot?.connection.state === "failed" ? tr("连接失败", "Connection failed")
        : pendingRequest() ? tr("等待确认", "Waiting for confirmation")
        : snapshot?.activity?.state === "running" ? tr("正在回复", "Replying")
        : pending || unsettled() ? tr("待核对", "Needs verification")
        : idle() ? tr("空闲", "Idle")
        : ["not_open", "closed"].includes(snapshot?.connection.state) ? tr("未连接", "Not connected") : tr("待核对", "Needs verification");
      const inherited = tr("沿用原生配置", "Use native settings");
      renderStatus(statusCard, { agent: "Claude Code", model: inherited, effort: null,
        memory: selectedMemory ? `${selectedMemory.name} · ${tr("只读", "Read only")}${memoryReady() ? "" : tr("（待核对）", " (needs verification)")}` : tr("不附加共享记忆", "No added shared memory"),
        status, observed: "", language });
    }
    function render() {
      renderProfileStatus();
      moreSummary.textContent = tr("更多", "More"); operationSummary.textContent = tr("操作记录", "Operation records");
      const scope = encode([workspace, snapshot?.epoch ?? null, snapshot?.binding?.session_id ?? null]); if (displayScope !== scope) { displayScope = scope; messages.replaceChildren(); historyTools.replaceChildren(); rawRecords.open = false; moreMenu.open = operationRecords.open = false; requestNodes.clear(); }
      handoffView?.setScope(encode([workspace, chatId, scope])); root.setAttribute("lang", language); settingsPanel?.setAttribute("lang", language);
      if (recoveryLabel) recoveryLabel.textContent = tr("连接与恢复", "Connection and recovery");
      nativeMaterials?.setScope(encode([workspace, chatId, generation, snapshot?.epoch ?? null]));
      for (const control of [...page.querySelectorAll("button"), ...(settingsPanel?.querySelectorAll("button") || [])]) { const value = control.getAttribute(language === "en" ? "data-claude-en" : "data-claude-zh"); if (value) control.textContent = value; }
      input.placeholder = tr("给 Claude 发消息…", "Message Claude…"); connection.textContent = readable ? `Claude · ${snapshot.connection.state} · ${snapshot.activity.state}` : tr("Claude状态待核对", "Claude status unconfirmed");
      openButton.hidden = ready(); openButton.disabled = !enabled || destroyed || !readable || openingForSend || writing || !!pending || unsettled() || pendingRequest();
      if (openingForSend) connection.textContent = tr("正在连接 Claude…", "Connecting Claude…");
      sendButton.disabled = !!filePicker?.busy() || openingForSend || !(idle() || canOpenForSend()) || writing || !!pending || storageInvalid || !materialsReady() || !memoryReady() || !draft.trim(); const stopping = !draft.trim() && snapshot?.activity.state === "running";
      sendButton.textContent = stopping ? tr("停止", "Stop") : tr("发送", "Send");
      sendButton.setAttribute("aria-label", stopping ? tr("停止本轮", "Stop current turn") : tr("发送", "Send"));
      sendButton.setAttribute("data-composer-action", stopping ? "stop" : "send");
      if (stopping) sendButton.disabled = !ready() || !uuid(activeClient()) || writing || openingForSend;
      disconnectButton.hidden = !snapshot || !["ready", "failed", "closed"].includes(snapshot.connection.state); disconnectButton.disabled = !enabled || destroyed || !readable || writing || !!pending || unsettled() || pendingRequest() || snapshot?.activity.state !== "idle";
      memoryTitle.textContent = tr("共享记忆", "Shared memory"); memorySelect.setAttribute("aria-label", tr("原生 Claude 共享记忆", "Native Claude shared memory"));
      memorySelect.replaceChildren(); const none = node("option", "", tr("不附加小白兔共享记忆", "Do not add Rabbit shared memory")); none.value = ""; memorySelect.append(none);
      const memoryStale = selectedMemory !== null && !memoryReady(), savedMemory = selectedMemory && "saved:" + selectedMemory.id + ":" + selectedMemory.revision;
      if (memoryStale) { const option = node("option", "", selectedMemory.name + tr(" · 原选第 ", " · saved v") + selectedMemory.revision + tr(" 版（待重新选择）", " (review required)")); option.value = savedMemory; option.disabled = true; memorySelect.append(option); }
      for (const row of memoryRows) { const option = node("option", "", row.name + tr(" · 第 ", " · v") + row.revision + tr(" 版", "") + (row.mode === "read_only" ? tr(" · 只读", " · read only") : tr(" · 本入口不支持读写", " · read-write unavailable here"))); option.value = row.id; option.disabled = !memoryReadable || row.mode !== "read_only"; memorySelect.append(option); }
      memorySelect.value = memoryStale ? savedMemory : selectedMemory?.id || ""; memorySelect.disabled = memoryBlocked(); memoryRefresh.disabled = !enabled || destroyed || writing || memoryReading;
      memoryInfo.textContent = [tr("Claude 原生记忆照常；这里只为下一次发送附加只读资料。不附加不会清除原生上文。", "Claude native memory stays unchanged. Read-only sources are added for the next send. Choosing none does not erase earlier context."), memoryStale ? tr("原选版本已变化或尚未核实，请重新选择；原稿保留。", "The saved version changed or is unverified. Choose explicitly; the draft remains.") : "", memoryReading ? tr("正在读取保存的参数…", "Reading saved parameters…") : "", memoryError].filter(Boolean).join(" ");
      memoryBox.hidden = !Object.hasOwn(snapshot || {}, "memory_intents") && !selectedMemory && !pending?.memory_profile;
      notices.replaceChildren(); if (errorText) notices.append(node("p", "rbs-error", errorText)); if (notice) notices.append(node("p", "rbs-note", notice));
      if (snapshot?.binding?.persisted === true && uuid(snapshot.binding.session_id)) notices.append(node("p", "rbs-note", tr("已保存原生会话：", "Saved native session: ") + snapshot.binding.session_id + " · " + snapshot.binding.cwd));
      if (pending) notices.append(node("p", "rbs-note", pending.status === "accepted" ? tr("Claude已接收原消息，等待原生完成；不会重复发送。", "Claude accepted the original message; waiting for completion without resending.") : posting ? tr("正在提交；原消息和编号已保存。", "Submitting; the original message and ID are saved.") : tr("原消息结果尚未确认；刷新只读核对原编号，不会自动重发。", "The original outcome is unknown. Refresh only checks its original ID without resending.")));
      if (snapshot?.binding?.persisted === false) notices.append(node("p", "rbs-note", tr("原生持久化尚未核实；不据此承诺重启后可恢复。", "Native persistence is not verified; restart recovery is not yet claimed.")));
      if (ready() && snapshot.binding?.persisted === false && snapshot.activity.state === "idle" && !pending && ![snapshot.operation, ...snapshot.operations].some(row => row?.kind === "prompt")) notices.append(node("p", "rbs-note", tr("新空会话尚未落原生文件，断开后可能需另建；尚未发送消息。", "This new empty session has no verified native file yet. Reopening after disconnect may require a new chat; no message has been sent.")));
      if (snapshot?.history?.has_more === true) notices.append(node("p", "rbs-note", tr("这里只展示部分原生历史；仍有历史记录尚未加载。", "Only part of the native history is shown; additional history has not been loaded.")));
      if (Number.isSafeInteger(snapshot?.native_records_omitted) && snapshot.native_records_omitted > 0) notices.append(node("p", "rbs-note", tr(`本页显示容量已省略${snapshot.native_records_omitted}条原生记录；不代表源记录被删除。`, `This page omitted ${snapshot.native_records_omitted} native records to stay within display limits; the source records were not deleted.`)));
      if (snapshot?.native_records.some(row => row.data?.truncated === true || row.data?.display_truncated === true)) notices.append(node("p", "rbs-note", tr("部分原生记录过大，本页只显示摘要与指纹。", "Some native records exceed display limits; this page shows only their summary and fingerprint.")));
      if (snapshot?.connection.problem) notices.append(node("p", "rbs-note", tr("连接问题：", "Connection issue: ") + (typeof snapshot.connection.problem === "string" ? snapshot.connection.problem : encode(snapshot.connection.problem))));
      materialBox.replaceChildren();
      if (draftMaterials.length) {
        materialBox.append(node("p", "rbs-note", tr("随本轮发送完整文本文件；Claude原图入口尚未开放。", "Complete text files accompany this turn. Claude image input is not enabled here.")));
        if (!materialsReady()) materialBox.append(node("p", "rbs-error", tr("文本文件入口暂不可用；原选择保留，尚未发送。", "Text-file input is unavailable; original selections remain unsent.")));
        for (const ref of draftMaterials) if (nativeMaterials) materialBox.append(nativeMaterials.card(ref, { language, verifyOnOpen: true, disabled: writing || !!pending || storageInvalid, remove: () => {
          if (writing || pending || storageInvalid || !enabled || destroyed) return;
          const previous = draftMaterials, revision = draftRevision; draftMaterials = draftMaterials.filter(item => item.id !== ref.id); draftRevision++;
          if (!save()) { draftMaterials = previous; draftRevision = revision; errorText = tr("材料选择暂不能保存，原选择保留。", "The material selection could not be saved; the original selection remains."); } render();
        } }));
      }
      if (memorySourcesError) notices.append(node("p", "rbs-note", memorySourcesError));
      renderRecords(); renderMemorySources(); renderRequests(); rawSummary.textContent = tr("原始Claude记录", "Original Claude records"); rawPre.textContent = JSON.stringify(snapshot?.native_records || [], null, 2);
      filePicker?.update(language);
    }
    input.addEventListener("input", () => { draft = input.value; draftRevision++; if (!save()) errorText = tr("新草稿暂不能保存，请保持页面打开；原回执仍保留。", "The new draft could not be saved. Keep this page open; the original receipt remains."); render(); });
    const visibility = () => { if (document.hidden) { viewVersion++; clearTimeout(timer); timer = null; save(); } else if (visible) refresh(); }; document.addEventListener?.("visibilitychange", visibility);
    async function show() { if (destroyed || !enabled) return; viewVersion++; visible = true; root.hidden = false; syncWorkspace(); render(); await refresh(); await loadMemoryProfiles(); }
    async function showSettings() { if (destroyed || !enabled) return; viewVersion++; visible = true; root.hidden = true; syncWorkspace(); render(); await refresh(); await loadMemoryProfiles(); }
    function hide() { viewVersion++; visible = false; root.hidden = true; clearTimeout(timer); timer = null; save(); }
    function destroy() { hide(); destroyed = true; settingsPanel?.remove(); generation++; readPending = false; document.removeEventListener?.("visibilitychange", visibility); }
    function setLanguage(value) { language = value === "en" ? "en" : "zh-CN"; render(); }
    function setEnabled(value) { enabled = value === true; if (!enabled) hide(); render(); }
    function setMaterialInputs(value) { materialsEnabled = value === true; render(); }
    function getConfigurationContext() {
      if (destroyed || !workspace || workspace !== String(getWorkspace() || "")) return null;
      return { agent_instance_id: "local/claude", profile_id: null, profile_name: null, profile_revision: null, model_id: null, reasoning_effort: null };
    }
    render(); return { show, showSettings, hide, destroy, refresh, setLanguage, setEnabled, setMaterialInputs, getConfigurationContext, putDraft, getTextSnapshot };
  }
  window.RabbitClaudeNative = Object.freeze({ create });
})();
