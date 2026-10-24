/* DSH event history and stable rpcId interactions retain their native protocol. */
(() => {
  "use strict";
  const object = value => !!value && typeof value === "object" && !Array.isArray(value);
  const uuid = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
  const text = value => typeof value === "string" && !/[\ud800-\udfff]/u.test(value);
  const sha = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const opaque = value => text(value) && value.length <= 4096;
  const nativeId = value => opaque(value) && value.length > 0;
  const countValid = value => value === undefined || Number.isSafeInteger(value) && value >= 0;
  const epochValid = value => typeof value === "string" && value.length > 0 && value.length <= 128 || Number.isSafeInteger(value) && value >= 0;
  const terminal = new Set(["completed", "command_completed", "failed", "interrupted", "rejected"]);
  const statuses = new Set(["submitted_unknown", "accepted", ...terminal]);
  const requestStates = new Set(["pending", "unknown", "answered", "expired"]);
  const encode = value => JSON.stringify(value);
  const canonical = value => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
  const memoryCodec = () => { if (!window.RabbitNativeMemory) throw Error("共享记忆组件尚未加载；原选择与草稿保留。"); return window.RabbitNativeMemory; };
  const memoryChoice = value => memoryCodec().choice(value, { readOnly: true });
  const sendBody = (client_id, message, memory = null) => ({ client_id, message, ...(memory ? { memory_profile_id: memory.id, memory_profile_revision: memory.revision } : {}) });
  const node = (tag, cls, value) => { const result = document.createElement(tag); if (cls) result.className = cls; if (value !== undefined) result.textContent = String(value); if (["button", "input", "select", "textarea", "summary"].includes(tag)) result.setAttribute("data-control-interface", "human"); return result; };
  function create({ root, api, getWorkspace = () => "", onError = () => {}, openDocument = null, session, settingsRoot = null, materialImages = null, materialFiles = null, materialInputs = false }) {
    const chatId = session?.id;
    if (!root || typeof api !== "function" || !uuid(chatId)) throw Error("DSH原生页面入口无效。");
    const apiRoot = "/api/dsh-chats/" + chatId;
    if (settingsRoot !== null && typeof settingsRoot.append !== "function") throw Error("原生对话设置入口无效。");
    const settingsPanel = settingsRoot ? node("section", "rbs-settings") : null;
    if (settingsPanel) { settingsPanel.setAttribute("data-control-interface", "ai"); settingsRoot.append(settingsPanel); }
    const materialDraft = window.RabbitDshMaterialDraft;
    const materialRefs = value => value === undefined || Array.isArray(value) && value.length === 0 ? [] : materialDraft ? materialDraft.refs(value) : (() => { throw Error("DSH文本材料组件尚未加载；整份草稿保留。"); })();
    let materialsEnabled = materialInputs === true, draftMaterials = [], draftRevision = 0, materialError = "";
    const materialTexts = new Map();
    let workspace = "", storageKey = "", draft = "", pending = null, answerJournal = Object.create(null), storageInvalid = false;
    const nativeMaterials = window.RabbitNativeMaterials?.create({ api, getWorkspace, images: materialImages, files: materialFiles, onError });
    let enabled = true, visible = false, destroyed = false, language = "zh-CN", generation = 0, writeVersion = 0, reading = false, writing = false, posting = false, readPending = false, timer = null;
    let snapshot = null, readable = false, errorText = "", notice = "", displayScope = null;
    let openingForSend = false, autoOpenBlocked = false, viewVersion = 0, filePicker = null;
    let selectedMemory = null, memoryRows = [], memoryReadable = false, memoryReading = false, memoryRequest = 0, memoryVersion = 0, memoryTextVersion = 0, memoryError = "", memorySourcesError = "";
    const memoryEvidence = new Map(), memoryCards = new Map(), memoryTexts = new Map();
    const requestNodes = new Map(), recordNodes = new Map(), burned = new Set(); let digests = new Map();
    const handoffView = window.RabbitHandoffView?.create();
    const tr = (zh, en) => language === "en" ? en : zh;
    function button(zh, en, callback) { const result = node("button", "rbs-button", tr(zh, en)); result.type = "button"; result.setAttribute("data-dsh-zh", zh); result.setAttribute("data-dsh-en", en); result.addEventListener("click", callback); return result; }
    const statusCard = node("section", "profile-status"), fallbackSettings = settingsPanel ? null : node("details", "rbs-setting-details"), fallbackTitle = fallbackSettings ? node("summary") : null;
    if (fallbackSettings) fallbackSettings.append(fallbackTitle);
    const page = node("div", "rbs-page"), head = node("header", "rbs-header"), connection = node("p", "rbs-status"), notices = node("div", "rbs-notices"), messages = node("div", "rbs-feed"), requests = node("section", "rbs-requests"), rawRecords = node("details", "rbs-item"), rawSummary = node("summary"), rawPre = node("pre", "rbs-native"), composer = node("div", "rbs-composer"), input = node("textarea", "rbs-input"), composerActions = node("div", "rbs-actions");
    const openButton = button("连接 DSH", "Connect DSH", () => mutate("open", {})), disconnectButton = button("断开连接", "Disconnect", disconnect), sendButton = button("发送", "Send", submit);
    const memorySources = node("section", "rbs-feed rbs-memory-sources"), memoryLabel = node("label", "rbs-field"), memoryTitle = node("span"), memorySelect = node("select", "rbs-control"), memoryInfo = node("p", "rbs-note"), memoryRefresh = button("更新记忆配置", "Refresh memory configurations", loadMemoryProfiles), materialBox = node("section", "rbs-materials");
    const recovery = settingsPanel ? node("details", "rbs-setting-details") : null, recoveryLabel = recovery ? node("summary") : null;
    if (recovery) { const controls = node("div", "rbs-actions"); controls.append(openButton, disconnectButton); recovery.append(recoveryLabel, controls); }
    materialBox.setAttribute("data-control-interface", "ai");
    page.setAttribute("data-control-interface", "ai"); composer.setAttribute("data-control-interface", "human");
    memoryLabel.setAttribute("data-control-interface", "ai"); memoryInfo.setAttribute("data-control-interface", "ai");
    for (const control of [openButton, disconnectButton, sendButton, memoryRefresh]) control.setAttribute("data-control-interface", "ai");
    memoryLabel.append(memoryTitle, memorySelect);
    const operationRecords = node("details", "rbs-operation-records"), operationSummary = node("summary"), historyTools = node("div", "rbs-feed");
    const moreMenu = node("details", "rbs-chat-more"), moreSummary = node("summary");
    operationRecords.append(operationSummary, historyTools); moreMenu.append(moreSummary, connection);
    sendButton.className += " rbs-primary rbs-submit";
    input.rows = 3; input.setAttribute("aria-label", "DSH原生独立草稿"); if (!settingsPanel) moreMenu.append(openButton, disconnectButton); head.append(statusCard, moreMenu); rawRecords.className += " rbs-originals"; rawRecords.append(rawSummary, rawPre); moreMenu.append(rawRecords); composerActions.append(sendButton); if (settingsPanel) { settingsPanel.append(memoryLabel, memoryRefresh, memoryInfo, recovery); composer.append(input, materialBox, composerActions); } else { fallbackSettings.append(memoryLabel, memoryRefresh, memoryInfo); composer.append(fallbackSettings, input, materialBox, composerActions); } page.append(head, notices, messages, operationRecords, memorySources, requests, composer); root.append(page); root.hidden = true;
    if (materialFiles && materialDraft && nativeMaterials?.textInput) {
      filePicker = nativeMaterials.textInput({ state: fileInputState, append: ref => {
        if (!fileInputState() || ref?.kind !== "text") throw Error("当前DSH草稿不可添加文件；原材料保留。");
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
      return { key: canonical([workspace, chatId, generation, viewVersion, draftRevision, draft, draftMaterials, selectedMemory, memoryVersion, snapshot?.epoch, snapshot?.binding?.sessionId]), refs: materialRefs(draftMaterials) };
    }
    function save() { if (!storageKey || storageInvalid) return false; try { window.sessionStorage.setItem(storageKey, encode(materialDraft ? { version: 3, draft, pending, answer_journal: answerJournal, selected_memory: selectedMemory, material_refs: draftMaterials, draft_revision: draftRevision } : selectedMemory || pending?.memory_profile ? { version: 2, draft, pending, answer_journal: answerJournal, selected_memory: selectedMemory } : { version: 1, draft, pending, answer_journal: answerJournal })); return true; } catch (_) { return false; } }
    function materialsReady() { return !draftMaterials.length || !!materialDraft && !!nativeMaterials && materialsEnabled; }
    function memoryReady() { return selectedMemory === null || !!window.RabbitNativeMemory && Object.hasOwn(snapshot || {}, "memory_intents") && memoryReadable && memoryRows.some(row => row.mode === "read_only" && canonical(row) === canonical(selectedMemory)); }
    memorySelect.addEventListener("change", () => {
      if (!enabled || destroyed || writing || pending || storageInvalid || unsettled() || pendingRequest() || snapshot?.activity.state !== "idle") { render(); return; }
      const row = memoryRows.find(value => value.id === memorySelect.value && value.mode === "read_only");
      if (memorySelect.value && (!memoryReadable || !row)) { render(); return; }
      const previous = selectedMemory; selectedMemory = row ? memoryChoice(row) : null; memoryVersion++;
      if (!save()) { selectedMemory = previous; memoryError = "浏览器不能保存共享记忆选择；原选择与草稿保留。"; } render();
    });
    async function loadMemoryProfiles() {
      syncWorkspace(); if (destroyed || !enabled || !workspace || writing || !Object.hasOwn(snapshot || {}, "memory_intents")) return;
      const stamp = generation, request = ++memoryRequest, writes = writeVersion, version = memoryVersion, identity = encode([snapshot?.epoch, snapshot?.binding?.sessionId]);
      const current = () => sameScope(stamp) && request === memoryRequest && writes === writeVersion && version === memoryVersion && identity === encode([snapshot?.epoch, snapshot?.binding?.sessionId]);
      memoryReading = true; render();
      try {
        const value = await api("/api/management/memory-profiles"); if (!current()) return;
        if (!exact(value, ["workspace_id", "scope", "connection_checked", "inference_performed", "memory_profiles"]) || value.workspace_id !== workspace || value.scope !== "saved_memory_parameters" || value.connection_checked !== false || value.inference_performed !== false || !Array.isArray(value.memory_profiles) || value.memory_profiles.length > 128) throw Error();
        const rows = value.memory_profiles.map(row => memoryCodec().choice(row)); if (new Set(rows.map(row => row.id)).size !== rows.length) throw Error();
        memoryRows = rows; memoryReadable = true; memoryError = "";
      } catch (_) { if (current()) { memoryReadable = false; memoryError = "共享记忆目录暂不可读；原选择与草稿保留。"; } }
      finally { if (sameScope(stamp) && request === memoryRequest) { memoryReading = false; render(); } }
    }
    function syncWorkspace() {
      const value = String(getWorkspace() || ""); if (value === workspace) return;
      save(); workspace = value; generation++; writeVersion++; openingForSend = autoOpenBlocked = false; viewVersion++;
      storageKey = value ? "rabbit.dsh-native.page.v1." + encodeURIComponent(value) + "." + chatId : "";
      draft = ""; pending = null; answerJournal = Object.create(null); storageInvalid = false; snapshot = null; readable = false; errorText = notice = ""; reading = writing = posting = readPending = false; digests.clear(); requestNodes.clear(); recordNodes.clear(); burned.clear();
      draftMaterials = []; draftRevision = 0; materialError = ""; materialTexts.clear(); nativeMaterials?.setScope(value);
      selectedMemory = null; memoryRows = []; memoryReadable = memoryReading = false; memoryError = memorySourcesError = ""; memoryRequest++; memoryVersion++; memoryTextVersion++; memoryEvidence.clear(); memoryCards.clear(); memoryTexts.clear();
      try {
        const stored = storageKey && JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
        if (stored) {
          if (!object(stored) || ![1, 2, 3].includes(stored.version) || stored.version === 3 && !materialDraft || !text(stored.draft) || stored.draft.length > 200000 || !object(stored.answer_journal) || Object.keys(stored.answer_journal).length > 128) throw Error();
          draft = stored.draft;
          if (stored.version >= 2) selectedMemory = stored.selected_memory === null ? null : memoryChoice(stored.selected_memory);
          if (stored.version === 3) { draftMaterials = materialRefs(stored.material_refs); if (!Number.isSafeInteger(stored.draft_revision) || stored.draft_revision < 0) throw Error(); draftRevision = stored.draft_revision; }
          if (stored.pending !== null) {
            const item = stored.pending; if (!object(item) || !uuid(item.client_id) || !text(item.message) || !item.message.trim() || item.message.length > 200000 || !["submitted_unknown", "accepted"].includes(item.status)) throw Error();
            if (item.material_refs !== undefined) {
              if (stored.version !== 3) throw Error(); pending = materialDraft.restore(item, workspace, chatId);
            } else if (item.memory_profile !== undefined) {
              if (stored.version < 2 || !text(item.body_json) || !sha(item.intent_sha256) || !sha(item.journal_sha256) || !(item.request_sha256 === null && item.derived_sha256 === undefined || sha(item.request_sha256) && sha(item.derived_sha256))) throw Error();
              pending = { client_id: item.client_id, message: item.message, request_sha256: item.request_sha256, status: item.status, memory_profile: memoryChoice(item.memory_profile), body_json: item.body_json, intent_sha256: item.intent_sha256, journal_sha256: item.journal_sha256, ...(item.derived_sha256 === undefined ? {} : { derived_sha256: item.derived_sha256 }) };
            } else { if (!sha(item.request_sha256)) throw Error(); pending = { client_id: item.client_id, message: item.message, request_sha256: item.request_sha256, status: item.status }; }
          }
          for (const [digest, status] of Object.entries(stored.answer_journal)) { if (!sha(digest) || status !== "submitted_unknown") throw Error(); answerJournal[digest] = status; }
        }
      } catch (_) { storageInvalid = true; errorText = "浏览器保存的DSH回执无法读取；原记录保留，尚未提交。"; }
      input.value = draft;
    }
    const sameScope = stamp => !destroyed && stamp === generation && workspace === String(getWorkspace() || "");
    async function digest(value) { if (!window.crypto?.subtle || typeof TextEncoder !== "function") throw Error("当前浏览器不能保存DSH请求指纹，尚未提交。"); const bytes = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)); return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join(""); }
    const promptHash = message => digest(encode({ message, type: "prompt" }));
    const journalHash = (body, memory) => digest(canonical({ body, memory_snapshot: memory }));
    const derivedHash = (item, hash) => digest(canonical({ client_id: item.client_id, intent_sha256: item.intent_sha256, request_sha256: hash }));
    async function verifyPending() {
      if (!pending) return;
      const original = pending;
      if (original.material_refs) { await materialDraft.verify(original, workspace, chatId); return; }
      if (!original.memory_profile) { if (await promptHash(original.message) !== original.request_sha256) throw Error("DSH原消息指纹无法核对；原记录保留，不会重新提交。"); return; }
      const body = sendBody(original.client_id, original.message, memoryChoice(original.memory_profile));
      if (original.body_json !== encode(body) || await digest(canonical(body)) !== original.intent_sha256 || await journalHash(body, original.memory_profile) !== original.journal_sha256
          || original.request_sha256 !== null && await derivedHash(original, original.request_sha256) !== original.derived_sha256) throw Error("DSH保存的共享记忆或请求指纹不一致；原记录保留，没有再次发送。");
    }
    function operation(value) { return object(value) && uuid(value.client_id) && value.kind === "prompt" && statuses.has(value.status) && sha(value.request_sha256) && nativeId(value.session_id) && (value.chat_id === undefined || value.chat_id === chatId) && (value.status !== "command_completed" || sha(value.native_command_sha256)); }
    function promptOperations(value, client) { return [value.operation, ...(value.operations || [])].filter(row => row?.kind === "prompt" && row.client_id === client); }
    function memoryRecord(value, source, expected = null) {
      const intent = memoryCodec().intent(source, { workspaceId: workspace, chatId, clientKind: "uuid" });
      if (expected && (intent.client_id !== expected.client_id || intent.request_sha256 !== (expected.raw_request_sha256 || expected.intent_sha256) || canonical(intent.memory_profile) !== canonical(memoryCodec().snapshot(expected.memory_profile)))) throw Error("共享记忆来源与冻结的DSH原请求不一致；原草稿和回执保留。");
      const rows = [...new Map(promptOperations(value, intent.client_id).map(row => [canonical(row), row])).values()];
      if (rows.length > 1 || rows.some(row => !operation(row) || row.session_id !== (value.session_id ?? value.binding?.sessionId))) throw Error("共享记忆原生回执身份不一致。");
      const found = rows[0] || null;
      if (intent.state === "failed" && (found || expected && (expected.request_sha256 !== null || expected.status === "accepted") || promptOperations(snapshot || {}, intent.client_id).length)) throw Error("共享记忆失败记录与原生回执矛盾；原记录保留。");
      if (found && (intent.state !== "ready" || intent.derived_inputs_sha256 !== found.request_sha256 || !sha(found.message_sha256))) throw Error("共享记忆派生指纹与DSH原生回执不符；原请求保留。");
      return { intent, operation: found, attached: !!found && !["submitted_unknown", "rejected"].includes(found.status) };
    }
    async function bindMemory(value, stamp, writes) {
      if (!pending?.memory_profile || pending.material_refs) return;
      const original = pending, current = () => sameScope(stamp) && writes === writeVersion && pending === original;
      const source = value.memory_intent ?? value.memory_intents?.find(row => row?.client_id === original.client_id);
      if (!source) { if (promptOperations(value, original.client_id).length) throw Error("DSH共享记忆回执缺少原选择；原请求保留。"); return; }
      const record = memoryRecord(value, source, original), found = record.operation;
      if (found && await digest(original.message) !== found.message_sha256) throw Error("DSH原文字与回执指纹不一致；原记录保留。");
      if (!current()) return;
      if (record.intent.state === "failed" && found === null) {
        pending = null; if (!save()) { pending = original; throw Error("共享记忆准备失败且尚未发送，但浏览器无法保存核对结果；原草稿与回执保留。"); }
        notice = tr("共享记忆准备失败，尚未发送；草稿保留，可修复后手动发送。", "Shared memory preparation failed before sending. Draft kept; fix it and send manually."); return;
      }
      if (!found) return;
      if (original.request_sha256 !== null && original.request_sha256 !== found.request_sha256) throw Error("DSH派生回执改变；原编号保留。");
      if (original.request_sha256 === null) {
        const hash = await derivedHash(original, found.request_sha256); if (!current()) return;
        original.request_sha256 = found.request_sha256; original.derived_sha256 = hash;
        if (!save()) { original.request_sha256 = null; delete original.derived_sha256; throw Error("浏览器不能保存DSH派生回执；原记录保留。"); }
      }
    }
    async function updateMemoryEvidence(value, current) {
      const evidence = new Map(), texts = new Map(); let problem = "";
      try {
        if (value.memory_intents !== undefined) {
          if (!Array.isArray(value.memory_intents) || value.memory_intents.length > 128 || new TextEncoder().encode(encode(value.memory_intents)).length > 2 * 1024 * 1024) throw Error();
          for (const source of value.memory_intents) {
            const expected = pending?.client_id === source?.client_id ? pending : null;
            if (expected && !expected.memory_profile) throw Error();
            const record = memoryRecord(value, source, expected); if (evidence.has(record.intent.client_id)) throw Error(); evidence.set(record.intent.client_id, record);
          }
          const annexes = [...evidence.values()].filter(row => row.attached).map(row => ({ ...row, raw: memoryCodec().piSuffix(row.intent) }));
          const events = [...value.history.events.map(row => row.event), ...value.frames.filter(row => row.stream === "mux" && row.request.payload.type === "session/event" && row.request.payload.sessionId === value.binding?.sessionId).map(row => row.request.payload.event)];
          for (const event of events) {
            const message = event?.data;
            if (event.type !== "user/message" || event.surfaceOp !== "append" || message?.source?.kind !== "user" || message.role !== "user" || message.truncated === true || message.display_truncated === true || message.display_placeholder === true || message.content?.length !== 1) continue;
            const part = message.content[0]; if (part?.type !== "text" || !text(part.text) || part.truncated === true || part.display_truncated === true) continue;
            for (const { intent, operation: found, raw } of annexes) {
              if (!part.text.endsWith(raw) || part.text.length <= raw.length) continue;
              const original = part.text.slice(0, -raw.length);
              if (await promptHash(part.text) === found.request_sha256 && await digest(original) === found.message_sha256) texts.set(part.text, { text: original, raw, version: canonical(intent) });
              if (!current()) return;
            }
          }
        }
      } catch (_) { evidence.clear(); texts.clear(); problem = "共享记忆来源记录暂时无法核验；DSH原生消息与草稿保留。"; }
      if (!current()) return;
      memoryEvidence.clear(); for (const [key, record] of evidence) memoryEvidence.set(key, record);
      if (canonical([...memoryTexts]) !== canonical([...texts])) memoryTextVersion++;
      memoryTexts.clear(); for (const [key, record] of texts) memoryTexts.set(key, record);
      memorySourcesError = problem; for (const client of memoryCards.keys()) if (!memoryEvidence.has(client)) memoryCards.delete(client);
    }
    function renderMemorySources() {
      const nodes = [];
      for (const { intent, attached } of memoryEvidence.values()) {
        const profile = intent.memory_profile, key = canonical([intent, attached, language]); let stored = memoryCards.get(intent.client_id);
        if (!stored || stored.key !== key) {
          const card = node("details", "rbs-parameters rbs-memory-record"); card.open = stored?.node.open === true;
          card.append(node("summary", "", attached ? tr("本轮附加的共享记忆", "Shared memory added for this send") : tr("共享记忆准备记录", "Shared memory preparation record")), node("p", "rbs-note", `${profile.name} · ${tr("第 ", "v")}${profile.revision} · ${intent.created_at}`), node("p", "rbs-note", tr("本应用附加的来源资料，原生消息完整保留；不单独证明模型实际读取。", "Reference data added by this app; native messages remain intact. This alone does not prove model use.")));
          if (intent.state === "ready") {
            card.append(node("p", "rbs-note", `${intent.recall_info.retrieved_at} · ${tr("完整条目 ", "Whole facts ")}${intent.recall_info.included_facts} · ${tr("未附 ", "Omitted ")}${intent.recall_info.omitted_facts}`));
            for (const ref of intent.recall_info.used_references) card.append(node("p", "rbs-note", ref.type + " · " + ref.retrieved_at), node("pre", "rbs-native", ref.text), node("p", "rbs-note", ref.reference_id + (ref.document_ref ? " · " + ref.document_ref : "")));
            if (profile.habits) card.append(node("p", "rbs-note", tr("本轮选定的身份与回答习惯", "Identity and answer habits selected for this send")), node("pre", "rbs-native", profile.habits));
          } else card.append(node("p", "rbs-note", intent.state === "failed" ? tr("准备失败；没有可展示的检索资料。", "Preparation failed; no retrieved context is available.") : tr("正在准备；尚未核实原生接收。", "Preparing; native acceptance is unverified.")));
          stored = { key, node: card }; memoryCards.set(intent.client_id, stored);
        }
        nodes.push(stored.node);
      }
      memorySources.replaceChildren(...nodes);
    }
    async function lookupMemoryPending(stamp, writes) {
      if (!pending?.memory_profile || pending.material_refs || storageInvalid) return;
      const original = pending, current = () => sameScope(stamp) && writes === writeVersion && pending === original;
      await verifyPending(); if (!current()) return;
      const value = await api(apiRoot + "/receipt?client_id=" + original.client_id); if (!current()) return;
      if (!exact(value, ["workspace_id", "chat_id", "session_id", "operation", ...(Object.hasOwn(value || {}, "memory_intent") ? ["memory_intent"] : []), ...(Object.hasOwn(value || {}, "material_intent") ? ["material_intent"] : [])]) || value.material_intent != null || value.workspace_id !== workspace || value.chat_id !== chatId || value.session_id !== snapshot?.binding?.sessionId || !(value.operation === null || operation(value.operation) && value.operation.client_id === original.client_id) || !value.memory_intent) throw Error("DSH精确回执身份或格式不完整；原请求保留。");
      await bindMemory(value, stamp, writes); if (!sameScope(stamp) || writes !== writeVersion) return;
      if (pending === original && value.operation) settle(value);
    }
    async function confirmMaterials(value, stamp, writes, exactReceipt = false) {
      const original = pending;
      if (!original?.material_refs) return;
      const proof = await materialDraft.confirm(original, value, workspace, chatId, exactReceipt);
      if (!sameScope(stamp) || writes !== writeVersion || pending !== original || !proof) return;
      pending = proof.pending;
      if (!save()) { pending = original; throw Error("浏览器不能保存DSH材料回执；原编号与草稿保留。"); }
      if (proof.failed) { const checked = pending; pending = null; if (!save()) pending = checked; notice = tr("准备失败，尚未发送；完整草稿保留。", "Preparation failed before sending. The complete draft is kept."); return; }
      settle({ operation: proof.operation, operations: [] }, true);
    }
    async function lookupMaterialPending(stamp, writes) {
      if (!pending?.material_refs || storageInvalid) return;
      const original = pending;
      const value = await api(apiRoot + "/receipt?client_id=" + original.client_id);
      if (sameScope(stamp) && writes === writeVersion && pending === original) await confirmMaterials(value, stamp, writes, true);
    }
    async function updateMaterialEvidence(value, current) {
      const texts = new Map(); let problem = "";
      try {
        if (value.material_intents !== undefined) {
          if (!materialDraft || !Array.isArray(value.material_intents) || value.material_intents.length > 128 || new TextEncoder().encode(encode(value.material_intents)).length > 1024 * 1024) throw Error();
          const seen = new Set();
          const byClient = new Map();
          const events = [...value.history.events.map(row => row.event), ...value.frames.filter(row => row.stream === "mux" && row.request.payload.type === "session/event" && row.request.payload.sessionId === value.binding?.sessionId).map(row => row.request.payload.event)];
          for (const event of events) {
            const message = event?.data;
            if (event.type !== "user/message" || event.surfaceOp !== "append" || message?.source?.kind !== "user" || !uuid(message.source.rpcId) || message.role !== "user" || message.truncated === true || message.display_truncated === true || message.display_placeholder === true || message.content?.length !== 1 || message.content[0]?.type !== "text" || !text(message.content[0].text) || message.content[0].truncated === true || message.content[0].display_truncated === true || message.content[0].display_placeholder === true) continue;
            const list = byClient.get(message.source.rpcId) || new Map(); list.set(event.seq, message.content[0].text); byClient.set(message.source.rpcId, list);
          }
          for (const intent of value.material_intents) {
            if (!object(intent) || intent.workspace_id !== workspace || intent.chat_id !== chatId || !uuid(intent.client_id) || seen.has(intent.client_id)) throw Error();
            seen.add(intent.client_id);
            const operations = [...new Map(promptOperations(value, intent.client_id).map(row => [canonical(row), row])).values()];
            if (operations.length > 1) throw Error();
            const found = operations[0]; if (!found || !operation(found) || ["submitted_unknown", "rejected"].includes(found.status)) continue;
            const memoryIntent = value.memory_intents?.find(row => row?.client_id === intent.client_id) || null;
            const evidenceKey = canonical([intent, found, memoryIntent, value.binding?.sessionId]);
            for (const [seq, original] of byClient.get(intent.client_id)?.entries() || []) {
              const key = String(seq), cached = materialTexts.get(key);
              if (cached?.evidence_key === evidenceKey && cached.native_text === original) { texts.set(key, cached); continue; }
              const projected = await materialDraft.project(original, intent, found, { workspaceId: workspace, chatId, sessionId: value.binding?.sessionId, memoryIntent });
              if (!current()) return;
              if (projected) texts.set(key, { ...projected, native_text: original, evidence_key: evidenceKey });
            }
          }
        }
      } catch (_) { texts.clear(); problem = "文本材料来源暂不能核验，完整原生消息保持原样。"; }
      if (!current()) return;
      if (materialTexts.size !== texts.size || [...texts].some(([key, record]) => materialTexts.get(key) !== record)) memoryTextVersion++;
      materialTexts.clear(); for (const [key, record] of texts) materialTexts.set(key, record);
      materialError = problem;
    }
    function settle(value, materialProof = false) {
      if (!pending) return;
      if (pending.material_refs && !materialProof) return;
      const matches = promptOperations(value, pending.client_id);
      if (matches.some(row => !operation(row) || row.request_sha256 !== pending.request_sha256)) throw Error("DSH原生回执与原消息不匹配；原草稿和编号保留。");
      const item = matches[0]; if (!item || item.status === "submitted_unknown") return;
      if (item.status === "accepted") { pending.status = "accepted"; save(); return; }
      if (terminal.has(item.status)) { const old = pending, previousDraft = draft, previousMaterials = draftMaterials;
        if ((pending.material_refs ? item.status === "completed" && pending.draft_revision === draftRevision && canonical(draftMaterials) === canonical(pending.material_refs) : item.status !== "rejected") && draft === pending.message) { draft = ""; draftMaterials = []; input.value = ""; }
        pending = null; if (!save()) { pending = old; draft = previousDraft; draftMaterials = previousMaterials; input.value = draft; errorText = "DSH结果已核对，但本地回执暂不能清除；不会重复发送。"; } if (item.status === "command_completed") notice = tr("原生命令已执行。", "Native command executed."); }
    }
    function ready() { return enabled && !destroyed && readable && snapshot?.connection.state === "ready"; }
    function unsettled() { return !![snapshot?.operation, ...(snapshot?.operations || [])].find(row => row?.kind === "prompt" && ["submitted_unknown", "accepted"].includes(row.status)); }
    function pendingRequest() { return snapshot?.pending.some(row => ["pending", "unknown"].includes(row.state)) === true; }
    function idle() { return ready() && nativeId(snapshot?.binding?.sessionId) && snapshot?.activity.state === "idle" && !unsettled() && !pendingRequest(); }
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
        errorText = "消息超过DSH单次发送容量；原稿保留，请缩短后发送。"; render(); return false;
      }
      if (!save()) { errorText = "浏览器不能保存DSH草稿，尚未连接或发送；请保持页面打开。"; render(); return false; }
      openingForSend = autoOpenBlocked = true; render();
      try {
        const opened = await mutate("open", {});
        if (!sameScope(stamp)) return false;
        const currentView = view === viewVersion && visible && !root.hidden && !document.hidden;
        const proof = opened?.view || opened;
        if (!proof || proof.schema_version !== 1 || proof.agent !== "dsh" || proof.workspace_id !== workspace || proof.chat_id !== chatId || proof.connection?.state !== "ready") { if (currentView) notice = tr("连接结果待核对；原稿保留，请在设置中核对连接，没有自动发送或重试。", "Connection outcome needs verification. The draft remains; check the connection in Settings. Nothing was sent or retried."); return false; }
        autoOpenBlocked = false;
        return currentView && !filePicker?.busy() && idle() && !pending && !storageInvalid && draft === message && draftRevision === revision
          && memoryVersion === version && canonical(selectedMemory) === memory && canonical(draftMaterials) === refs;
      } finally { if (sameScope(stamp)) { openingForSend = false; render(); } }
    }
    function activeClient() { return uuid(snapshot?.activity?.client_id) ? snapshot.activity.client_id : pending?.client_id || [snapshot?.operation, ...(snapshot?.operations || [])].find(row => row?.kind === "prompt" && ["submitted_unknown", "accepted"].includes(row.status))?.client_id; }
    function requestIdentity(value, record) { return encode([workspace, chatId, value.epoch, value.binding?.sessionId, record.epoch, record.token, record.request]); }
    function requestFingerprint(record) { return requestIdentity(snapshot, record); }
    function rpcRequest(value) { return object(value) && opaque(value.rpcId) && object(value.payload); }
    function sessionEvent(value) { return object(value) && nativeId(value.type) && Number.isSafeInteger(value.seq) && value.seq >= 0 && Number.isSafeInteger(value.time) && value.time >= 0 && Object.hasOwn(value, "data"); }
    async function consume(value, stamp, writes = writeVersion) {
      const current = () => sameScope(stamp) && writes === writeVersion;
      if (!current()) return;
      if (!object(value) || value.schema_version !== 1 || value.agent !== "dsh" || value.workspace_id !== workspace || value.chat_id !== chatId || !epochValid(value.epoch)
          || !object(value.connection) || !["not_open", "opening", "ready", "failed", "closed"].includes(value.connection.state)
          || !(value.binding === null || object(value.binding) && nativeId(value.binding.sessionId) && nativeId(value.binding.cwd) && (value.binding.agentPreset === null || value.binding.agentPreset === undefined || opaque(value.binding.agentPreset)) && typeof value.binding.persisted === "boolean")
          || !object(value.activity) || !["idle", "running", "unknown"].includes(value.activity.state) || !(value.activity.client_id === null || uuid(value.activity.client_id))
          || !object(value.history) || !Array.isArray(value.history.events) || value.history.events.length > 2000 || typeof value.history.hasMore !== "boolean" || !value.history.events.every(row => object(row) && sessionEvent(row.event))
          || !countValid(value.history.omitted_stream_chunks) || !countValid(value.history.omitted_history_events) || value.history.omitted_history_events > 0 && value.history.hasMore !== true || !countValid(value.stream_chunks_omitted) || !countValid(value.frames_omitted)
          || !Array.isArray(value.frames) || value.frames.length > 2000 || !value.frames.every(row => object(row) && ["mux", "host"].includes(row.stream) && rpcRequest(row.request))
          || value.native_responses !== undefined && (!Array.isArray(value.native_responses) || value.native_responses.length > 2000 || !value.native_responses.every(row => object(row) && opaque(row.rpcId) && object(row.result)))
          || !Array.isArray(value.pending) || value.pending.length > 128 || !value.pending.every(row => object(row) && nativeId(row.token) && requestStates.has(row.state) && typeof row.answerable === "boolean" && epochValid(row.epoch) && rpcRequest(row.request) && row.request.payload.sessionId === value.binding?.sessionId)
          || !Array.isArray(value.operations) || value.operations.length > 128 || !value.operations.every(operation) || !(value.operation === null || operation(value.operation))) throw Error("DSH原生视图身份或格式不完整。");
      if (new TextEncoder().encode(encode(value.history)).length > 1024 * 1024 || new TextEncoder().encode(encode([value.history, value.frames, value.native_responses || []])).length > 4 * 1024 * 1024 || new TextEncoder().encode(encode(value.pending)).length > 512 * 1024) throw Error("DSH原生记录超过本页读取容量；原稿保留，没有静默截断。");
      const eventIdentities = new Map();
      for (const entry of [...value.history.events, ...value.frames.filter(row => row.stream === "mux" && row.request.payload.type === "session/event" && row.request.payload.sessionId === value.binding?.sessionId).map(row => ({ event: row.request.payload.event }))]) { if (!sessionEvent(entry.event) || eventIdentities.has(entry.event.seq) && eventIdentities.get(entry.event.seq) !== encode(entry.event)) throw Error("DSH原生事件序号与原内容不一致，尚未提交新请求。"); eventIdentities.set(entry.event.seq, encode(entry.event)); }
      if (new Set(value.pending.map(row => row.token)).size !== value.pending.length || new Set(value.pending.map(row => row.request.rpcId)).size !== value.pending.length) throw Error("DSH原生询问编号重复，尚未答复。");
      const lookup = value.operation_lookup;
      if (lookup !== undefined && (!object(lookup) || Object.keys(lookup).sort().join(",") !== "checked,client_id,found" || !uuid(lookup.client_id) || lookup.checked !== true || typeof lookup.found !== "boolean")) throw Error("DSH回执查询没有精确核对。");
      if ([value.operation, ...value.operations].some(row => row && row.session_id !== value.binding?.sessionId)) throw Error("DSH回执不属于这段原生会话。");
      const next = new Map();
      for (const record of value.pending) { try { next.set(record.token, await digest(requestIdentity(value, record))); } catch (_) { /* Original request stays visible but cannot be answered. */ } if (!current()) return; }
      try { await verifyPending(); } catch (error) { if (current()) storageInvalid = true; throw error; }
      if (!current()) return;
      if (pending?.material_refs && pending.session_id !== value.binding?.sessionId) throw Error("DSH材料请求属于另一段原生会话；原编号与草稿保留。");
      await bindMemory(value, stamp, writes); if (!current()) return;
      await updateMemoryEvidence(value, current); if (!current()) return;
      await updateMaterialEvidence(value, current); if (!current()) return;
      if (snapshot && (snapshot.epoch !== value.epoch || snapshot.binding?.sessionId !== value.binding?.sessionId)) { requestNodes.clear(); burned.clear(); }
      settle(value); if (!storageInvalid) errorText = ""; snapshot = value; digests = next; readable = true;
    }
    function fail(error, fallback) { const message = String(error?.message || ""); errorText = message && message.length < 300 ? message : fallback; onError(error); }
    function schedule() { clearTimeout(timer); timer = null; if (visible && enabled && !destroyed && !document.hidden) timer = setTimeout(refresh, 1000); }
    async function refresh() {
      syncWorkspace(); if (destroyed || !enabled || !workspace) return;
      if (reading || posting) { readPending = true; return; }
      const stamp = generation, writes = writeVersion; reading = true;
      try { const value = await api(apiRoot + (pending && !pending.memory_profile ? "?client_id=" + pending.client_id : "")); if (sameScope(stamp) && writes === writeVersion) { await consume(value, stamp, writes); if (sameScope(stamp) && writes === writeVersion) { await lookupMaterialPending(stamp, writes); await lookupMemoryPending(stamp, writes); } } }
      catch (error) { if (sameScope(stamp) && writes === writeVersion) { readable = false; fail(error, "DSH原生状态暂不可读取；原请求保留。"); } }
      finally { if (sameScope(stamp)) { reading = false; render(); const again = readPending; readPending = false; if (again) await refresh(); else schedule(); } }
    }
    async function mutate(action, body) {
      syncWorkspace(); if (destroyed || !enabled || !readable || writing || !workspace) return null;
      const stamp = generation, writes = ++writeVersion; writing = posting = true; errorText = ""; render();
      try {
        const value = await api(apiRoot + "/" + action, { method: "POST", body: encode(body) }); if (!sameScope(stamp)) return null;
        if (action === "send") {
          if (pending?.material_refs) await confirmMaterials({ ...value, session_id: snapshot?.binding?.sessionId }, stamp, writes);
          else { await bindMemory({ ...value, session_id: snapshot?.binding?.sessionId }, stamp, writes); if (!sameScope(stamp)) return null; if (!operation(value.operation) || value.operation.client_id !== body.client_id || value.operation.request_sha256 !== pending?.request_sha256 || value.operation.session_id !== snapshot?.binding?.sessionId) throw Error("DSH发送确认与原消息不匹配，原编号保留。"); settle({ operation: value.operation, operations: [] }); }
        }
        if (value.view) await consume(value.view, stamp, writes); else if (value.schema_version === 1) await consume(value, stamp, writes);
        if (action === "open") { notice = ""; if (ready()) autoOpenBlocked = false; }
        if (action === "disconnect" && value.disconnected === true) { autoOpenBlocked = false; notice = tr("已断开；原生会话仍保留，后台工具退出不作额外推断。", "Disconnected; the native session remains. Background-tool exit is not inferred."); }
        return value;
      } catch (error) {
        if (sameScope(stamp)) { if (action === "send" && error.accepted === false && pending?.client_id === body.client_id && pending.status === "submitted_unknown" && (!pending.memory_profile || pending.request_sha256 === null) && (!pending.material_refs || pending.request_sha256 === null) && !promptOperations(snapshot || {}, body.client_id).length) { const old = pending; pending = null; if (!save()) pending = old; } fail(error, "结果仍待核对，没有自动重复提交。"); }
        return null;
      } finally { if (sameScope(stamp)) { writing = posting = false; render(); await refresh(); } }
    }
    async function send() {
      syncWorkspace(); if (filePicker?.busy() || openingForSend || !(idle() || canOpenForSend()) || writing || pending || storageInvalid || !materialsReady() || !memoryReady() || !text(draft) || !draft.trim()) return;
      if (!idle()) { if (await connectForSend()) await send(); return; }
      const view = viewVersion;
      const stamp = generation, message = draft, refs = materialRefs(draftMaterials), revision = draftRevision, memory = selectedMemory ? memoryChoice(selectedMemory) : null, version = memoryVersion, identity = encode([snapshot.epoch, snapshot.binding?.sessionId]), client = window.crypto?.randomUUID?.(); if (!uuid(client)) { errorText = "无法生成稳定DSH请求编号，尚未发送。"; render(); return; }
      let body = sendBody(client, message, memory);
      writing = true; render();
      try {
        if (typeof TextEncoder !== "function" || new TextEncoder().encode(message).length > 128 * 1024) throw Error("消息超过DSH单次发送容量；原稿保留，请缩短后发送。");
        const prepared = refs.length ? await materialDraft.prepare(client, message, refs, workspace, chatId, snapshot.binding.sessionId, revision, memory) : { client_id: client, message, request_sha256: memory ? null : await promptHash(message), status: "submitted_unknown", ...(memory ? { memory_profile: memory, body_json: encode(body), intent_sha256: await digest(canonical(body)), journal_sha256: await journalHash(body, memory) } : {}) };
        if (!sameScope(stamp) || filePicker?.busy() || view !== viewVersion || !visible || root.hidden || document.hidden || !idle() || pending || !materialsReady() || !memoryReady() || version !== memoryVersion || canonical(memory) !== canonical(selectedMemory) || draft !== message || draftRevision !== revision || canonical(refs) !== canonical(draftMaterials) || identity !== encode([snapshot.epoch, snapshot.binding?.sessionId])) return;
        pending = prepared; if (refs.length) body = JSON.parse(prepared.body_json);
        if (!save()) { pending = null; throw Error("浏览器不能保存DSH回执，尚未发送；原稿保留。"); }
      } catch (error) { if (sameScope(stamp)) fail(error, "原消息无法保存，尚未提交。"); }
      finally { if (sameScope(stamp)) { writing = false; render(); } }
      if (sameScope(stamp) && pending?.client_id === client) await mutate("send", body);
    }
    function submit() {
      syncWorkspace();
      if (!enabled || destroyed || !visible || root.hidden || document.hidden) return;
      return !draft.trim() && (snapshot?.activity.state === "running") ? interrupt() : send();
    }
    async function interrupt() { const client = activeClient(); if (!ready() || snapshot.activity.state !== "running" || writing || !uuid(client)) return; const value = await mutate("interrupt", { client_id: client }); if (value) notice = tr("已提交停止请求，等待DSH原生结果；不能据此推断后台工具已退出。", "Interrupt requested; waiting for the native outcome. Background-tool exit is not inferred."); render(); }
    async function disconnect() { if (!readable || writing || pending || unsettled() || pendingRequest() || !["idle"].includes(snapshot?.activity.state)) return; await mutate("disconnect", {}); }
    function putDraft(value) {
      syncWorkspace();
      if (!object(value) || value.workspace_id !== workspace || !workspace || value.chat_id !== chatId || !enabled || destroyed || filePicker?.busy() || writing || pending || storageInvalid || unsettled() || pendingRequest() || snapshot?.activity.state === "running" || snapshot?.activity.state === "unknown" || draft !== "" || input.value !== "" || draftMaterials.length) throw Error("DSH原生草稿已有内容、请求尚未结束或所属对话已变化；没有覆盖。");
      if (Object.hasOwn(value, "ai_profile")) throw Error("DSH原生对话沿用本机配置；目标模型配置没有放入草稿。");
      const hasMemory = Object.hasOwn(value, "memory_profile"), memory = hasMemory ? value.memory_profile === null ? null : memoryChoice(value.memory_profile) : selectedMemory;
      if (hasMemory && selectedMemory !== null && canonical(memory) !== canonical(selectedMemory)) throw Error("DSH原生对话已有不同的共享记忆选择；没有覆盖。");
      const refs = materialRefs(value.material_refs);
      if (!text(value.text) || !(value.text.trim() || value.text === "" && (refs.length || hasMemory)) || typeof TextEncoder !== "function" || new TextEncoder().encode(value.text).length > 128 * 1024) throw Error("交接文字无效或超过DSH发送容量。");
      if (refs.length && (!materialsEnabled || !nativeMaterials || !value.material_preview)) throw Error("DSH文本材料入口尚未启用或缺少完整预览；整份未放入。");
      if (value.material_preview !== undefined) { if (!refs.length || !nativeMaterials) throw Error("DSH材料预览缺少所选版本；整份未放入。"); nativeMaterials.seed(refs, value.material_preview); }
      const previous = { draft, input: input.value, refs: draftMaterials, memory: selectedMemory, revision: draftRevision };
      draft = value.text; draftMaterials = refs; selectedMemory = memory; draftRevision++; input.value = draft;
      if (!save()) { draft = previous.draft; input.value = previous.input; draftMaterials = previous.refs; selectedMemory = previous.memory; draftRevision = previous.revision; throw Error("浏览器不能保存DSH原生草稿；原内容保留，尚未发送。"); }
      if (hasMemory) memoryVersion++; render(); return true;
    }
    function answerable(record) {
      const payload = record?.request?.payload;
      return ready() && record?.state === "pending" && record.answerable === true && record.epoch === snapshot.epoch && payload?.sessionId === snapshot.binding?.sessionId && ["approval/requested", "question/requested"].includes(payload?.type);
    }
    async function respond(record, result, fingerprint) {
      const current = snapshot?.pending.find(row => row.token === record.token);
      if (!answerable(current) || writing || storageInvalid || requestFingerprint(current) !== fingerprint || burned.has(record.token)) return;
      const stamp = generation; writing = true; render();
      try {
        const hash = await digest(fingerprint), latest = snapshot?.pending.find(row => row.token === record.token);
        if (!sameScope(stamp) || !answerable(latest) || requestFingerprint(latest) !== fingerprint || Object.hasOwn(answerJournal, hash)) return;
        if (Object.keys(answerJournal).length >= 128) throw Error("DSH尚未核对的答复记录过多，尚未答复。");
        const response = { type: "client-response", rpcId: latest.request.rpcId, result };
        if (new TextEncoder().encode(encode(response)).length > 128 * 1024) throw Error("DSH答复超过本页容量；原问题保留，尚未答复。");
        answerJournal[hash] = "submitted_unknown"; if (!save()) { delete answerJournal[hash]; throw Error("浏览器不能保存DSH答复回执，尚未提交。"); }
        burned.add(record.token); const writes = ++writeVersion; posting = true;
        const value = await api(apiRoot + "/respond", { method: "POST", body: encode({ token: latest.token, epoch: latest.epoch, response }) });
        if (sameScope(stamp) && (value.view || value.schema_version === 1)) await consume(value.view || value, stamp, writes);
      } catch (error) { if (sameScope(stamp)) fail(error, "DSH答复结果尚未确认，不会重复答复。"); }
      finally { if (sameScope(stamp)) { writing = posting = false; render(); await refresh(); } }
    }
    function raw(parent, value, label) { const details = node("details", "rbs-parameters"); details.append(node("summary", "", label), node("pre", "rbs-native", JSON.stringify(value, null, 2))); parent.append(details); }
    function rich(parent, value) { if (window.OneAIRichText?.render) window.OneAIRichText.render(parent, value, { openDocument }); else parent.textContent = value; }
    function content(parent, value, role, key, depth = 0, operationMount = parent) {
      if (!Array.isArray(value) || depth > 8) { raw(operationMount, value, tr("原始内容", "Original content")); return; }
      const candidate = role === "user" && value.length === 1 && value[0]?.type === "text" ? materialTexts.get(key) : null;
      const material = candidate?.native_text === value[0]?.text ? candidate : null;
      if (material) {
        content(parent, [{ type: "text", text: material.message }], role, key, depth + 1, operationMount);
        const details = node("details", "rbs-parameters"); details.append(node("summary", "", tr("本轮完整文本资料 · 不可信资料", "Complete text materials · untrusted reference data")));
        for (const file of material.material_preview.files) details.append(node("p", "rbs-note", file.name + " · " + file.size_bytes + tr(" 字节", " bytes")), node("pre", "file-task-preview-content", file.text));
        parent.append(details);
        if (material.memory_suffix) { const memoryDetails = node("details", "rbs-parameters"); memoryDetails.append(node("summary", "", tr("附加的共享记忆原文", "Original added shared memory")), node("pre", "rbs-native", material.memory_suffix)); parent.append(memoryDetails); }
        return;
      }
      const memory = role === "user" && value.length === 1 && value[0]?.type === "text" ? memoryTexts.get(value[0].text) : null;
      if (memory) { content(parent, [{ type: "text", text: memory.text }], role, key, depth + 1, operationMount); const details = node("details", "rbs-parameters"); details.append(node("summary", "", tr("附加的共享记忆原文", "Original added shared memory")), node("pre", "rbs-native", memory.raw)); parent.append(details); return; }
      if (role === "user" && value.length === 1 && value[0]?.type === "text" && text(value[0].text) && handoffView?.render(parent, { message: value[0].text, role, messageKey: key, language })) return;
      for (const block of value) {
        if (block?.type === "text" && text(block.text)) { const paragraph = node("div", "rbs-item-body"); rich(paragraph, block.text); parent.append(paragraph); }
        else if (block?.type === "reasoning") continue;
        else if (block?.type === "tool-call") raw(operationMount, block, `DSH · ${tr("工具调用", "Tool call")} · ${text(block.name) ? block.name : ""}`);
        else if (block?.type === "tool-result") { const details = node("details", "rbs-item"); details.append(node("summary", "", tr(block.isError === true ? "工具失败" : "工具结果", block.isError === true ? "Tool failed" : "Tool result"))); content(details, block.content, "tool", key, depth + 1); raw(details, block, tr("原始工具结果", "Original tool result")); operationMount.append(details); }
        else raw(operationMount, block, tr("未识别的原生内容", "Unrecognized native content"));
      }
    }
    function eventEntries() {
      const entries = new Map();
      for (const entry of snapshot?.history.events || []) entries.set(entry.event.seq, entry);
      for (const frame of snapshot?.frames || []) {
        const payload = frame.request.payload;
        if (frame.stream === "mux" && payload.type === "session/event" && payload.sessionId === snapshot.binding?.sessionId && sessionEvent(payload.event)) entries.set(payload.event.seq, { event: payload.event, ...(payload.view === undefined ? {} : { view: payload.view }) });
      }
      return [...entries.values()].sort((a, b) => a.event.seq - b.event.seq);
    }
    function getTextSnapshot() {
      if (!ready() || !workspace || workspace !== String(getWorkspace() || "") || snapshot.workspace_id !== workspace
          || snapshot.chat_id !== chatId || !nativeId(snapshot.binding?.sessionId) || snapshot.binding.persisted !== true) throw Error("DSH 原生身份或已保存视图尚未核对，不能读取文字快照。");
      const entries = snapshot.history.events, completed = new Set(), records = [], seen = new Set();
      let incomplete = snapshot.history.hasMore === true;
      for (const { event } of entries) if (event.type === "turn/end" && event.data?.reason?.kind === "completed" && Number.isSafeInteger(event.data.turn)) completed.add(event.data.turn);
      // Only durable history append records qualify. Mux frames, replacements,
      // reasoning, tool results and token chunks are not copied into text.
      for (const { event } of entries) {
        if (event.surfaceOp !== "append" || !["user/message", "assistant/message"].includes(event.type)) continue;
        const human = event.type === "user/message", data = event.data, message = human ? data : data?.message;
        if (!object(message) || message.role !== (human ? "user" : "assistant") || message.source?.kind !== (human ? "user" : "model")) continue;
        if (!nativeId(message.id) || message.truncated === true || message.display_truncated === true || message.display_placeholder === true
            || !human && !completed.has(data.turn) || human && snapshot.activity.state !== "idle" && message.source.rpcId === activeClient()) { incomplete = true; continue; }
        if (!Array.isArray(message.content)) { incomplete = true; continue; }
        for (const [index, part] of message.content.entries()) {
          if (!object(part) || part.type !== "text") continue;
          if (!text(part.text) || part.truncated === true || part.display_truncated === true) { incomplete = true; continue; }
          const message_id = encode([event.seq, message.id]) + "/text/" + index;
          if (seen.has(message_id)) { incomplete = true; continue; }
          seen.add(message_id);
          if (part.text.length) records.push({ message_id, role: human ? "user" : "assistant", content: part.text });
        }
      }
      return { native_identity: snapshot.binding.sessionId, records, incomplete };
    }
    function detailsIn(parent) {
      const result = parent.tagName === "DETAILS" ? [parent] : [];
      for (const child of parent.children) result.push(...detailsIn(child));
      return result;
    }
    function recordNode(key, value, build) {
      const identity = encode(value?.event || value), fingerprint = encode([language, value, memoryTextVersion]), previous = recordNodes.get(key);
      if (previous?.fingerprint === fingerprint) return previous;
      const allDetails = record => [record?.node, record?.operations].filter(Boolean).flatMap(detailsIn);
      const opened = previous?.identity === identity ? allDetails(previous).map(detail => detail.open === true) : [];
      const built = build(), record = built.node !== undefined ? built : { node: built, operations: null };
      allDetails(record).forEach((detail, index) => { detail.open = opened[index] === true; });
      const result = { ...record, identity, fingerprint }; recordNodes.set(key, result); return result;
    }
    function toolIdentity(turn, step, id, name, args) {
      return Number.isSafeInteger(turn) && turn >= 0 && Number.isSafeInteger(step) && step >= 0 && nativeId(id) && nativeId(name) && text(args) ? encode([turn, step, id, name, args]) : null;
    }
    function replacement(event) { return event.surfaceOp === "replace" || object(event.surfaceOp) && event.surfaceOp.op === "replace"; }
    function renderRecords(entries) {
      const nodes = [], operations = [], keys = new Set(), representedTools = new Set();
      const keep = (key, value, build, operationOnly = false) => { keys.add(key); const record = recordNode(key, value, build); if (record.node) (operationOnly ? operations : nodes).push(record.node); if (record.operations) operations.push(record.operations); };
      for (const { event } of entries) {
        const data = event.data;
        if (event.type !== "assistant/message" || event.surfaceOp !== "append" || !Array.isArray(data?.message?.content)) continue;
        for (const block of data.message.content) if (block?.type === "tool-call") {
          const identity = toolIdentity(data.turn, data.step, block.id, block.name, block.arguments); if (identity) representedTools.add(identity);
        }
      }
      for (const entry of entries) {
        const event = entry.event, key = String(event.seq), data = event.data;
        const append = event.surfaceOp === "append", message = event.type === "user/message" ? data : ["assistant/message", "tool/result"].includes(event.type) ? data?.message : null;
        const human = event.type === "user/message" && message?.source?.kind === "user", tool = event.type === "tool/result";
        if (append && object(message) && Array.isArray(message.content) && (human || tool || event.type === "assistant/message")) {
          keep("event:" + key, entry, () => {
            const role = human ? "user" : tool ? "tool" : "assistant";
            const box = node("article", "rbs-item" + (human ? " rbs-user" : "")), body = node("div", "rbs-item-body");
            if (!tool) { box.className += " rbs-message-" + role; box.setAttribute("data-message-role", role); box.setAttribute("aria-label", human ? tr("用户消息", "User message") : tr("AI消息", "AI message")); }
            else box.append(node("p", "rbs-item-role", tr("工具结果", "Tool results")));
            const operationBox = node("article", "rbs-item");
            content(body, message.content, role, key, 0, operationBox);
            if (body.children.length) box.append(body); box.setAttribute("data-dsh-record-key", key);
            if (tool) { if (body.children.length) operationBox.append(box); return { node: null, operations: operationBox.children.length ? operationBox : null }; }
            return { node: body.children.length ? box : null, operations: operationBox.children.length ? operationBox : null };
          });
        } else if (event.type === "tool/call" && !replacement(event) && !representedTools.has(toolIdentity(data?.turn, data?.step, data?.callId, data?.name, data?.arguments))) {
          keep("event:" + key, entry, () => { const box = node("details", "rbs-item"); box.append(node("summary", "", "DSH · " + tr("工具调用", "Tool call") + " · " + (text(data?.name) ? data.name : "")), node("pre", "rbs-native", JSON.stringify(entry, null, 2))); return box; }, true);
        } else if (event.type === "turn/end") {
          keep("event:" + key, entry, () => node("p", data?.reason?.kind === "completed" ? "rbs-note" : "rbs-error", tr("DSH原生轮次结束", "DSH native turn ended") + " · " + (text(data?.reason?.kind) ? data.reason.kind : tr("未知", "unknown"))), data?.reason?.kind === "completed");
        }
        // Runtime context, lifecycle, replacements, chunks and unknown native
        // events remain intact in the single original-records section below.
      }
      for (const response of snapshot?.native_responses || []) {
        const command = response.result?.ok === true && response.result.value?.accepted === true && response.result.value.command?.kind === "success" ? response.result.value.command : null;
        if (command) keep("response:" + response.rpcId, response, () => { const box = node("details", "rbs-item"); box.append(node("summary", "", tr("DSH原生命令结果", "DSH native command result"))); if (text(command.text)) { const body = node("div", "rbs-item-body"); rich(body, command.text); box.append(body); } raw(box, response, tr("原始DSH响应", "Original DSH response")); return box; }, true);
        else if (response.result?.ok === false) keep("response:" + response.rpcId, response, () => { const error = response.result.error, label = text(error?.message) && error.message.length <= 300 ? error.message : text(error?.code) ? error.code : tr("查看下方原生记录", "See the native records below"); return node("p", "rbs-error", tr("DSH请求失败：", "DSH request failed: ") + label); });
      }
      for (const key of recordNodes.keys()) if (!keys.has(key)) recordNodes.delete(key);
      messages.replaceChildren(...nodes); historyTools.replaceChildren(...operations); operationRecords.hidden = operations.length === 0;
    }
    function questionsValid(questions) {
      return Array.isArray(questions) && questions.length > 0 && questions.length <= 32 && questions.every(question => object(question) && opaque(question.id) && text(question.question) && question.question.length <= 16000 && (question.header === undefined || text(question.header)) && (question.detail === undefined || text(question.detail)) && (question.multiSelect === undefined || typeof question.multiSelect === "boolean") && (question.options === undefined || Array.isArray(question.options) && question.options.length <= 128 && question.options.every(option => object(option) && text(option.label) && (option.description === undefined || text(option.description)))));
    }
    function questionForm(card, record, fingerprint) {
      const payload = record.request.payload, questions = payload.questions;
      if (!questionsValid(questions)) return false;
      card.fields = []; const drafts = questions.map(() => ({ selected: [], custom: "", skipped: false })), groups = [];
      const synchronize = index => {
        const draft = drafts[index], group = groups[index];
        for (const option of group.options) option.control.checked = draft.selected.includes(option.label);
        group.custom.value = draft.custom; group.status.textContent = draft.skipped ? tr("此题已跳过", "This question is skipped") : "";
        card.note.textContent = ""; card.note.hidden = true;
      };
      questions.forEach((question, index) => {
        const group = node("fieldset", "rbs-item rbs-field rbs-question"), heading = node("legend", "", question.header || question.question); group.append(heading);
        if (question.header) group.append(node("p", "", question.question));
        if (question.detail !== undefined) { const detail = node("div", "rbs-question-detail"); rich(detail, question.detail); group.append(detail); }
        const options = [];
        for (const [optionIndex, option] of (question.options || []).entries()) {
          const label = node("label", "rbs-question-option"), control = node("input", "rbs-question-choice"); control.type = question.multiSelect === true ? "checkbox" : "radio"; control.name = "dsh-question-" + record.token + "-" + index; control.value = option.label; control.setAttribute("data-question-index", index); control.setAttribute("data-option-index", optionIndex); label.append(control, node("span", "", option.label)); if (option.description) label.append(node("small", "", option.description)); group.append(label); options.push({ control, label: option.label }); card.fields.push(control);
          control.addEventListener("change", () => { if (control.disabled) return; const draft = drafts[index]; draft.skipped = false; draft.selected = question.multiSelect === true ? control.checked ? [...new Set([...draft.selected, option.label])] : draft.selected.filter(value => value !== option.label) : [option.label]; if (question.multiSelect !== true) draft.custom = ""; synchronize(index); });
        }
        const custom = node("textarea", "rbs-control rbs-question-custom"); custom.rows = 2; custom.setAttribute("aria-label", tr("其他回答：", "Other answer: ") + question.question); group.append(custom); card.fields.push(custom);
        custom.addEventListener("input", () => { if (custom.disabled) return; const draft = drafts[index]; draft.custom = custom.value; draft.skipped = false; if (question.multiSelect !== true && custom.value.trim()) draft.selected = []; synchronize(index); });
        const status = node("p", "rbs-note"), skip = button("跳过此题", "Skip question", () => { if (skip.disabled) return; drafts[index] = { selected: [], custom: "", skipped: true }; synchronize(index); }); group.append(skip, status); groups.push({ options, custom, status }); card.node.append(group);
      });
      const controls = node("div", "rbs-actions");
      controls.append(button("取消这组问题", "Cancel questions", () => respond(record, { ok: false, error: { code: "cancelled", message: "the user closed this question request", details: {} } }, fingerprint)), button("提交整组回答", "Submit all answers", () => {
        if (drafts.some(draft => !draft.skipped && draft.selected.length === 0 && !draft.custom.trim())) { card.note.textContent = tr("请回答或明确跳过每道问题。", "Answer or explicitly skip every question."); card.note.hidden = false; return; }
        const answers = questions.map((question, index) => { const draft = drafts[index], custom = draft.custom.trim(); return { id: question.id, selected: draft.skipped ? [] : custom && question.multiSelect !== true ? [] : draft.selected.slice(), ...(draft.skipped || !custom ? {} : { custom }) }; });
        return respond(record, { ok: true, value: { sessionId: payload.sessionId, answer: { answers } } }, fingerprint);
      })); card.node.append(controls); return true;
    }
    function renderRequests() {
      const nodes = [], keys = new Set();
      for (const record of snapshot?.pending || []) {
        if (!["pending", "unknown"].includes(record.state)) continue;
        const fingerprint = requestFingerprint(record), key = record.token, payload = record.request.payload; keys.add(key); let card = requestNodes.get(key);
        if (!card || card.fingerprint !== fingerprint) {
          const box = node("article", "rbs-request"), note = node("p", "rbs-note"); box.append(node("h3", "", payload.type === "approval/requested" ? "DSH · " + tr("工具审批", "Tool approval") + " · " + String(payload.toolName || "") : payload.type === "question/requested" ? tr("DSH原生问题", "DSH native questions") : tr("DSH原生询问", "DSH native request"))); raw(box, record.request, tr("原始DSH询问", "Original DSH request")); box.append(note); card = { node: box, fingerprint, note, fields: [], supported: false };
          if (payload.type === "approval/requested" && opaque(payload.approvalId) && text(payload.toolName)) { if (text(payload.reason)) box.append(node("p", "rbs-note", payload.reason)); const controls = node("div", "rbs-actions"); for (const [zh, en, outcome] of [["拒绝", "Reject", "rejected"], ["仅这次允许", "Allow once", "allowed-once"]]) controls.append(button(zh, en, () => respond(record, { ok: true, value: { sessionId: payload.sessionId, approvalId: payload.approvalId, outcome } }, fingerprint))); box.append(controls); card.supported = true; }
          if (payload.type === "question/requested") card.supported = questionForm(card, record, fingerprint);
          requestNodes.set(key, card);
        }
        const hash = digests.get(key), blocked = !card.supported || !answerable(record) || writing || storageInvalid || !hash || burned.has(key) || Object.hasOwn(answerJournal, hash);
        for (const control of [...card.node.querySelectorAll("button"), ...card.fields]) control.disabled = blocked;
        const pendingUnknown = burned.has(key) || hash && Object.hasOwn(answerJournal, hash);
        const status = !card.supported ? tr("此原生询问形状暂不支持，只保留原始内容，没有自动答复。", "This native request shape is unsupported. Its original content is retained without an automatic answer.") : record.state !== "pending" ? tr("该询问已结束或仍待核对，旧答复入口不会重新启用。", "This request ended or remains unconfirmed. Old controls are not re-enabled.") : pendingUnknown ? tr("答复已提交或结果未知；等待原生resolved确认，不会重复提交。", "The response was submitted or remains unknown. Waiting for native resolution without resubmitting.") : !record.answerable || record.epoch !== snapshot.epoch ? tr("询问身份尚未确认，不能答复。", "The request identity is unconfirmed and cannot be answered.") : "";
        if (status || card.status) { card.note.textContent = status; card.note.hidden = !status; } card.status = status; nodes.push(card.node);
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
      renderStatus(statusCard, { agent: "DeepSeek Harness", model: inherited, effort: null,
        memory: selectedMemory ? `${selectedMemory.name} · ${tr("只读", "Read only")}${memoryReady() ? "" : tr("（待核对）", " (needs verification)")}` : tr("不附加共享记忆", "No added shared memory"),
        status, observed: "", language });
    }
    function render() {
      renderProfileStatus();
      moreSummary.textContent = tr("更多", "More"); operationSummary.textContent = tr("操作记录", "Operation records");
      const scope = encode([workspace, snapshot?.epoch ?? null, snapshot?.binding?.sessionId ?? null]); if (displayScope !== scope) { displayScope = scope; messages.replaceChildren(); historyTools.replaceChildren(); rawRecords.open = false; moreMenu.open = operationRecords.open = false; requestNodes.clear(); recordNodes.clear(); }
      handoffView?.setScope(encode([workspace, chatId, scope])); root.setAttribute("lang", language); settingsPanel?.setAttribute("lang", language);
      if (recoveryLabel) recoveryLabel.textContent = tr("连接与恢复", "Connection and recovery");
      for (const control of [...page.querySelectorAll("button"), ...(settingsPanel?.querySelectorAll("button") || [])]) { const value = control.getAttribute(language === "en" ? "data-dsh-en" : "data-dsh-zh"); if (value) control.textContent = value; }
      input.placeholder = tr("给 DSH 发消息…", "Message DSH…"); connection.textContent = readable ? `DSH · ${snapshot.connection.state} · ${snapshot.activity.state}` : tr("DSH状态待核对", "DSH status unconfirmed");
      memoryTitle.textContent = tr("共享记忆", "Shared memory"); memorySelect.setAttribute("aria-label", tr("原生 DSH 共享记忆", "Native DSH shared memory"));
      memorySelect.replaceChildren(); const none = node("option", "", tr("不附加小白兔共享记忆", "Do not add Rabbit shared memory")); none.value = ""; memorySelect.append(none);
      const stale = selectedMemory !== null && !memoryReady(), saved = selectedMemory && "saved:" + selectedMemory.id + ":" + selectedMemory.revision;
      if (stale) { const option = node("option", "", selectedMemory.name + tr(" · 原选第 ", " · saved v") + selectedMemory.revision + tr(" 版（待重新选择）", " (review required)")); option.value = saved; option.disabled = true; memorySelect.append(option); }
      for (const row of memoryRows) { const option = node("option", "", row.name + tr(" · 第 ", " · v") + row.revision + tr(" 版", "") + (row.mode === "read_only" ? tr(" · 只读", " · read only") : tr(" · 本入口不支持读写", " · read-write unavailable here"))); option.value = row.id; option.disabled = !memoryReadable || row.mode !== "read_only"; memorySelect.append(option); }
      memorySelect.value = stale ? saved : selectedMemory?.id || ""; memorySelect.disabled = !enabled || destroyed || writing || !!pending || storageInvalid || unsettled() || pendingRequest() || snapshot?.activity.state !== "idle";
      memoryRefresh.disabled = !enabled || destroyed || writing || memoryReading;
      memoryInfo.textContent = [tr("DSH 原生上文、插件与设置照常；这里只为下一次发送附加只读资料，不保存本轮交互。不附加不会清除原生上文。", "DSH native context, plugins and settings stay unchanged. Selected read-only context is added for the next send without saving this interaction. Choosing none does not erase earlier context."), stale ? tr("原选版本已变化或尚未核实，请重新选择；原稿保留。", "The saved version changed or is unverified. Choose explicitly; the draft remains.") : "", memoryReading ? tr("正在读取保存的参数…", "Reading saved parameters…") : "", memoryError].filter(Boolean).join(" ");
      memoryLabel.hidden = memoryRefresh.hidden = memoryInfo.hidden = !Object.hasOwn(snapshot || {}, "memory_intents") && !selectedMemory && !pending?.memory_profile;
      openButton.hidden = ready(); openButton.disabled = !enabled || destroyed || !readable || openingForSend || writing || !!pending || unsettled() || pendingRequest();
      if (openingForSend) connection.textContent = tr("正在连接 DSH…", "Connecting DSH…");
      sendButton.disabled = !!filePicker?.busy() || openingForSend || !(idle() || canOpenForSend()) || writing || !!pending || storageInvalid || !materialsReady() || !memoryReady() || !draft.trim(); const stopping = !draft.trim() && snapshot?.activity.state === "running";
      sendButton.textContent = stopping ? tr("停止", "Stop") : tr("发送", "Send");
      sendButton.setAttribute("aria-label", stopping ? tr("停止本轮", "Stop current turn") : tr("发送", "Send"));
      sendButton.setAttribute("data-composer-action", stopping ? "stop" : "send");
      if (stopping) sendButton.disabled = !ready() || !uuid(activeClient()) || writing || openingForSend;
      disconnectButton.hidden = !snapshot || !["ready", "failed", "closed"].includes(snapshot.connection.state); disconnectButton.disabled = !enabled || destroyed || !readable || writing || !!pending || unsettled() || pendingRequest() || snapshot?.activity.state !== "idle";
      notices.replaceChildren(); if (errorText) notices.append(node("p", "rbs-error", errorText)); if (notice) notices.append(node("p", "rbs-note", notice));
      if (memorySourcesError) notices.append(node("p", "rbs-note", memorySourcesError));
      if (materialError) notices.append(node("p", "rbs-note", materialError));
      if (draft.startsWith("/")) notices.append(node("p", "rbs-note", tr("此入口尚未接入DSH原生命令；以 / 开头的内容会作为普通消息发送给模型。", "DSH native commands are not connected to this entry yet; content starting with / is sent to the model as an ordinary message.")));
      if (pending) notices.append(node("p", "rbs-note", pending.status === "accepted" ? tr("DSH已接收原消息，等待原生完成；不会重复发送。", "DSH accepted the original message; waiting for completion without resending.") : posting ? tr("正在提交；原消息和编号已保存。", "Submitting; the original message and ID are saved.") : tr("原消息结果尚未确认；刷新只读核对原编号，不会自动重发。", "The original outcome is unknown. Refresh only checks its original ID without resending.")));
      if (snapshot?.binding?.persisted === false) notices.append(node("p", "rbs-note", tr("原生持久化尚未核实；不据此承诺重启后可恢复。", "Native persistence is not verified; restart recovery is not yet claimed.")));
      if (snapshot?.history.hasMore === true) notices.append(node("p", "rbs-note", tr("这里只展示部分原生历史；仍有历史记录尚未加载。", "Only part of the native history is shown; additional history has not been loaded.")));
      if (snapshot?.history.omitted_stream_chunks > 0 || snapshot?.stream_chunks_omitted > 0) notices.append(node("p", "rbs-note", tr("此页省略逐字流增量，完整回答保留在原生消息中。", "This page omits token stream increments; complete answers remain in native messages.")));
      if (snapshot?.frames_omitted > 0) notices.append(node("p", "rbs-note", tr("部分即时事件已移出显示缓存，原始记录仍在DSH。", "Some live events have left the display cache; their original records remain in DSH.")));
      const entries = eventEntries(); if (entries.some(({ event }) => replacement(event))) notices.append(node("p", "rbs-note", tr("DSH有模型上下文替换；原始记录可在下方查看。", "DSH replaced model context; its original records remain available below.")));
      if (snapshot?.connection.problem) notices.append(node("p", "rbs-note", tr("连接问题：", "Connection issue: ") + (typeof snapshot.connection.problem === "string" ? snapshot.connection.problem : encode(snapshot.connection.problem))));
      materialBox.hidden = !draftMaterials.length; materialBox.replaceChildren();
      if (draftMaterials.length) {
        materialBox.append(node("p", "rbs-note", tr("随本轮发送完整文本文件；DSH原图入口尚未开放。", "Complete text files accompany this turn. DSH image input is not enabled here.")));
        if (!materialsReady()) materialBox.append(node("p", "rbs-error", tr("文本文件入口暂不可用；原选择保留，尚未发送。", "Text-file input is unavailable; original selections remain unsent.")));
        for (const ref of draftMaterials) if (nativeMaterials) materialBox.append(nativeMaterials.card(ref, { language, verifyOnOpen: true, disabled: writing || !!pending || storageInvalid, remove: () => {
          if (writing || pending || storageInvalid) return;
          const previous = draftMaterials, revision = draftRevision; draftMaterials = draftMaterials.filter(item => canonical(item) !== canonical(ref)); draftRevision++;
          if (!save()) { draftMaterials = previous; draftRevision = revision; errorText = tr("材料选择暂不能保存，原选择保留。", "The material selection could not be saved; the original selection remains."); } render();
        } }));
      }
      renderRecords(entries); renderMemorySources(); renderRequests(); rawSummary.textContent = tr("DSH原生记录", "DSH native records"); rawPre.textContent = JSON.stringify(snapshot ? { history: snapshot.history, frames: snapshot.frames, native_responses: snapshot.native_responses || [], pending: snapshot.pending } : {}, null, 2);
      filePicker?.update(language);
    }
    input.addEventListener("input", () => { draft = input.value; draftRevision++; save(); render(); });
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
      return { agent_instance_id: "local/dsh", profile_id: null, profile_name: null, profile_revision: null, model_id: null, reasoning_effort: null };
    }
    render(); return { show, showSettings, hide, destroy, refresh, setLanguage, setEnabled, setMaterialInputs, getConfigurationContext, putDraft, getTextSnapshot };
  }
  window.RabbitDshNative = Object.freeze({ create });
})();
