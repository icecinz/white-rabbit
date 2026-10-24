/* Pi native messages/events remain Pi data; no Codex item/turn conversion. */
(() => {
  "use strict";
  const object = v => !!v && typeof v === "object" && !Array.isArray(v);
  const uuid = v => typeof v === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
  const terminal = new Set(["completed", "failed", "interrupted", "rejected"]);
  const statuses = new Set(["submitted_unknown", "accepted", ...terminal]);
  const dialogs = new Set(["select", "confirm", "input", "editor"]);
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = String(text); if (["button", "input", "select", "textarea", "summary"].includes(tag)) n.setAttribute("data-control-interface", "human"); return n; };
  const validText = text => typeof text === "string" && !/[\ud800-\udfff]/u.test(text);
  const encode = value => JSON.stringify(value);
  const canonical = value => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  const profileFields = ["id", "revision", "name", "agent_instance_id", "device_id", "model_id", "reasoning_effort"];
  function preference(value) {
    if (!object(value) || Object.keys(value).sort().join(",") !== "id,revision" || !uuid(value.id) || !Number.isSafeInteger(value.revision) || value.revision < 1) throw Error("Pi配置选择无效。");
    return { id: value.id, revision: value.revision };
  }
  function profile(value) {
    if (!object(value) || !uuid(value.id) || !Number.isSafeInteger(value.revision) || value.revision < 1 || !validText(value.name) || !value.name.trim() || Array.from(value.name).length > 40 || /[\x00-\x1f\x7f]/.test(value.name)
        || value.agent_instance_id !== "local/pi" || value.device_id !== "local" || !validText(value.model_id) || value.model_id !== "" && !/^local\/pi\/[^\x00-\x20\x7f]{1,192}$/u.test(value.model_id)
        || value.reasoning_effort !== null && !["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"].includes(value.reasoning_effort)) throw Error("Pi原生配置没有读完整。");
    return Object.fromEntries(profileFields.map(key => [key, value[key]]));
  }
  const isSHA = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
  const memoryCodec = () => { if (!window.RabbitNativeMemory) throw Error("共享记忆组件尚未加载；原选择与草稿保留。"); return window.RabbitNativeMemory; };
  const selectedMemoryChoice = value => memoryCodec().choice(value, { readOnly: true });
  const materialRefs = value => { if (window.RabbitNativeMaterials) return window.RabbitNativeMaterials.refs(value); if (value === undefined || Array.isArray(value) && !value.length) return []; throw Error("材料组件未就绪；原引用保留。"); };
  const sendBody = (id, message, selected = null, refs = [], memory = null) => ({ client_id: id, message, ...(selected ? { ai_profile_id: selected.id, ai_profile_revision: selected.revision } : {}), ...(refs.length ? { material_refs: refs } : {}), ...(memory ? { memory_profile_id: memory.id, memory_profile_revision: memory.revision } : {}) });
  const journalBody = (body, selected, memory) => ({ body, profile_snapshot: selected, ...(memory ? { memory_snapshot: memory } : {}) });
  function create({ root, api, getWorkspace = () => "", onError = () => {}, openDocument = null, session, onManageProfiles = null, materialImages = null, materialFiles = null, materialInputs = false, settingsRoot = null }) {
    const chatId = session && session.id;
    if (!root || typeof api !== "function" || !uuid(chatId)) throw Error("Pi原生页面入口无效。");
    const apiRoot = "/api/pi-chats/" + chatId;
    if (settingsRoot !== null && typeof settingsRoot.append !== "function") throw Error("原生对话设置入口无效。");
    const settingsPanel = settingsRoot ? node("section", "rbs-settings") : null;
    if (settingsPanel) { settingsPanel.setAttribute("data-control-interface", "ai"); settingsRoot.append(settingsPanel); }
    let workspace = "", storageKey = "", draft = "", draftMaterials = [], pending = null, journal = Object.create(null), storageInvalid = false, materialsEnabled = materialInputs === true;
    let openingForSend = false, openSendUnconfirmed = false, composerVersion = 0;
    let visible = false, destroyed = false, enabled = true, language = "zh-CN", generation = 0, timer = null, reading = false, writing = false, posting = false, writeVersion = 0, readPending = false, snapshot = null, readable = false, errorText = "", notice = "";
    let selectedProfile = null, profileRows = [], profilesReadable = false, profilesReading = false, profileRequest = 0, profilesError = "", receiptError = "", currentImageInput = "unknown";
    const configurationSelection = window.RabbitProfiles?.createNativeSelection?.({ agent: "pi", parseProfile: profile });
    let selectionUnsupported = false;
    let selectedMemory = null, memoryRows = [], memoryReadable = false, memoryReading = false, memoryRequest = 0, memoryVersion = 0, memoryError = "", memorySourcesError = "";
    const memoryEvidence = new Map(), memoryCards = new Map(), memoryTextVersions = new Map();
    const requestNodes = new Map(), toolNodes = new Map(), burned = new Set(); let digests = new Map(), displayScope = null, freshSend = null;
    const handoffView = window.RabbitHandoffView?.create();
    const nativeMaterials = window.RabbitNativeMaterials?.create({ api, images: materialImages, files: materialFiles, getWorkspace, onError });
    const memorySources = node("section", "rbs-feed rbs-memory-sources");
    const statusCard = node("section", "profile-status"), fallbackSettings = settingsPanel ? null : node("details", "rbs-setting-details"), fallbackTitle = fallbackSettings ? node("summary") : null;
    if (fallbackSettings) fallbackSettings.append(fallbackTitle);
    const page = node("div", "rbs-page"), head = node("header", "rbs-header"), connection = node("p", "rbs-status"), notices = node("div", "rbs-notices"), messages = node("div", "rbs-feed"), tools = node("div", "rbs-feed"), requests = node("section", "rbs-requests"), rawEvents = node("details", "rbs-item"), eventSummary = node("summary"), eventPre = node("pre", "rbs-native"), composer = node("div", "rbs-composer"), input = node("textarea", "rbs-input"), materialBox = node("section", "rbn-materials"), composerActions = node("div", "rbs-actions");
    page.setAttribute("data-control-interface", "ai"); composer.setAttribute("data-control-interface", "human"); materialBox.setAttribute("data-control-interface", "ai");
    const tr = (zh, en) => language === "en" ? en : zh;
    let historyBefore = null, historyRequest = 0;
    const operationRecords = node("details", "rbs-operation-records"), operationSummary = node("summary"), historyTools = node("div", "rbs-feed"), originalsRoot = node("div", "rbs-original-records");
    const historyScope = () => encode([workspace, chatId, generation, snapshot?.epoch ?? null, snapshot?.binding?.session_id ?? null]);
    const historyView = window.RabbitPiNativeHistory?.create({ root: messages, operationsRoot: historyTools, originalsRoot, api, getState: () => snapshot, getScope: historyScope,
      onPage: before => { historyBefore = before; historyRequest++; return refresh(); }, rich, handoffView, splitMemory: text => splitMemoryText(text), onError });
    function button(zh, en, callback) { const b = node("button", "rbs-button", tr(zh, en)); b.type = "button"; b.setAttribute("data-pi-zh", zh); b.setAttribute("data-pi-en", en); b.addEventListener("click", callback); return b; }
    const openButton = button("连接 Pi", "Connect Pi", () => { if (!openingForSend) return mutate("open", {}); }), disconnectButton = button("断开连接", "Disconnect", disconnect), sendButton = button("发送", "Send", submit);
    const moreMenu = node("details", "rbs-chat-more"), moreSummary = node("summary");
    operationRecords.append(operationSummary, historyTools, tools); moreMenu.append(moreSummary, connection);
    sendButton.className += " rbs-primary rbs-submit";
    input.rows = 3; input.setAttribute("aria-label", "Pi原生独立草稿"); if (settingsPanel) { const recovery = node("details", "rbs-setting-details"), summary = node("summary", "", "连接与恢复"); summary.setAttribute("data-control-interface", "human"); recovery.append(summary, openButton, disconnectButton); settingsPanel.append(recovery);  } else moreMenu.append(openButton, disconnectButton); head.append(statusCard, moreMenu); rawEvents.className += " rbs-originals"; rawEvents.append(eventSummary, eventPre); moreMenu.append(rawEvents, originalsRoot); composerActions.append(sendButton); composer.append(input, composerActions); page.append(head, notices, messages, operationRecords, memorySources, requests, composer); root.append(page); root.hidden = true;
    const profileBox = node("div", "rbs-field"), profileLabel = node("label", "rbs-field"), profileTitle = node("span"), profileSelect = node("select", "rbs-control"), profileControls = node("div", "rbs-actions"), profileInfo = node("p", "rbs-note"), currentInfo = node("p", "rbs-note");
    profileBox.setAttribute("data-control-interface", "ai");
    const profilesRefresh = button("更新配置", "Refresh configurations", loadProfiles);
    profileLabel.append(profileTitle, profileSelect); profileControls.append(profilesRefresh);
    const profilesManage = typeof onManageProfiles === "function" ? button("管理配置", "Manage configurations", () => onManageProfiles()) : null;
    if (profilesManage) profileControls.append(profilesManage);
    profileBox.append(profileLabel, profileControls, profileInfo, currentInfo); if (settingsPanel) { settingsPanel.append(profileBox); composer.replaceChildren(input, materialBox, composerActions); } else { fallbackSettings.append(profileBox); composer.replaceChildren(fallbackSettings, input, materialBox, composerActions); }
    const memoryLabel = node("label", "rbs-field"), memoryTitle = node("span"), memorySelect = node("select", "rbs-control"), memoryInfo = node("p", "rbs-note");
    const memoryRefresh = button("更新记忆配置", "Refresh memory configurations", loadMemoryProfiles);
    for (const control of [openButton, disconnectButton, sendButton, profilesRefresh, memoryRefresh]) control.setAttribute("data-control-interface", "ai");
    memoryLabel.append(memoryTitle, memorySelect); profileBox.append(memoryLabel, memoryRefresh, memoryInfo);
    function textInputState() {
      if (!materialFiles || !materialsEnabled || !nativeMaterials || !enabled || destroyed || !visible || root.hidden || document.hidden || !readable || !workspace || workspace !== String(getWorkspace() || "")
          || writing || openingForSend || openSendUnconfirmed || pending || storageInvalid || unsettled() || active() || snapshot?.disconnect_unconfirmed === true
          || !["not_open", "closed", "ready"].includes(snapshot?.connection.state) || snapshot?.extension_requests?.some(row => ["pending", "unknown"].includes(row.state))) return null;
      return { key: encode([workspace, chatId, generation, composerVersion, memoryVersion, draftMaterials, selectedProfile, selectedMemory, [snapshot?.epoch, snapshot?.binding?.session_id]]), refs: materialRefs(draftMaterials) };
    }
    const textPicker = typeof materialFiles?.upload === "function" && nativeMaterials?.textInput ? nativeMaterials.textInput({
      state: textInputState,
      append: ref => {
        if (!textInputState() || ref?.kind !== "text") throw Error("Pi草稿或材料入口已变化，文件已保存在本机，没有放入其他草稿。");
        const previous = draftMaterials; draftMaterials = materialRefs([...draftMaterials, ref]);
        if (!save()) { draftMaterials = previous; throw Error("浏览器不能保存Pi文件选择；原草稿与材料保留。"); }
        composerVersion++; render(); return true;
      }, changed: render
    }) : null;
    if (textPicker) composerActions.replaceChildren(textPicker.root, sendButton);
    function memoryBlocked() { return !enabled || destroyed || writing || openingForSend || !!pending || storageInvalid || unsettled() || active(); }
    function memoryReady() { return selectedMemory === null || !!window.RabbitNativeMemory && Object.hasOwn(snapshot || {}, "memory_intents") && memoryReadable && memoryRows.some(row => row.mode === "read_only" && canonical(row) === canonical(selectedMemory)); }
    memorySelect.addEventListener("change", () => {
      if (memoryBlocked()) { render(); return; }
      const row = memoryRows.find(value => value.id === memorySelect.value && value.mode === "read_only");
      if (memorySelect.value && (!memoryReadable || !row)) { render(); return; }
      const previous = selectedMemory; selectedMemory = row ? selectedMemoryChoice(row) : null; memoryVersion++;
      if (!save()) { selectedMemory = previous; memoryError = "浏览器不能保存共享记忆选择；原选择与草稿保留。"; }
      render();
    });
    async function loadMemoryProfiles() {
      syncWorkspace(); if (destroyed || !enabled || !workspace || writing || !Object.hasOwn(snapshot || {}, "memory_intents")) return;
      const stamp = generation, request = ++memoryRequest, writes = writeVersion, version = memoryVersion, identity = encode([snapshot?.epoch, snapshot?.binding?.session_id]);
      const current = () => sameScope(stamp) && request === memoryRequest && writes === writeVersion && version === memoryVersion && identity === encode([snapshot?.epoch, snapshot?.binding?.session_id]);
      memoryReading = true; render();
      try {
        const value = await api("/api/management/memory-profiles"); if (!current()) return;
        if (!exact(value, ["workspace_id", "scope", "connection_checked", "inference_performed", "memory_profiles"]) || value.workspace_id !== workspace || value.scope !== "saved_memory_parameters" || value.connection_checked !== false || value.inference_performed !== false || !Array.isArray(value.memory_profiles) || value.memory_profiles.length > 128) throw Error("共享记忆目录身份或格式不完整。");
        const rows = value.memory_profiles.map(value => memoryCodec().choice(value)); if (new Set(rows.map(row => row.id)).size !== rows.length) throw Error("共享记忆配置编号重复。");
        memoryRows = rows; memoryReadable = true; memoryError = "";
      } catch (_) { if (current()) { memoryReadable = false; memoryError = "共享记忆目录暂不可读；原选择与草稿保留。"; } }
      finally { if (sameScope(stamp) && request === memoryRequest) { memoryReading = false; render(); } }
    }
    function selectedRow() { return selectedProfile && profileRows.find(row => row.id === selectedProfile.id && row.revision === selectedProfile.revision) || null; }
    function selectionReady() { return !selectionUnsupported && (!configurationSelection || configurationSelection.ready()); }
    function selectionControlled() { return selectionUnsupported || configurationSelection?.controlled() === true; }
    function syncConfigurationSelection() {
      configurationSelection?.synchronize({ blocked: !enabled || destroyed || !readable || writing || posting || textPicker?.busy() || openingForSend || openSendUnconfirmed || !!pending || storageInvalid || !!unsettled() || active() || snapshot?.disconnect_unconfirmed === true || snapshot?.extension_requests?.some(row => ["pending", "unknown"].includes(row.state)), save,
        apply: value => { const previous = selectedProfile; selectedProfile = value === null ? null : preference({ id: value.id, revision: value.revision }); if (!save()) { selectedProfile = previous; return false; } composerVersion++; return true; } });
      if (configurationSelection?.takeCatalogRefresh(ready() && !writing && !posting && !openingForSend && !profilesReading)) { profilesReadable = false; void loadProfiles(); }
    }
    function profileReady() { return selectionReady() && (selectedProfile === null ? !configurationSelection || configurationSelection.matchesProfile(null) : profilesReadable && selectedRow()?.available === true && (!configurationSelection || configurationSelection.matchesProfile(selectedRow()))); }
    function materialsReady() {
      if (!draftMaterials.length) return true;
      if (!materialsEnabled || !nativeMaterials) return false;
      if (!draftMaterials.some(ref => ref.kind === "image")) return true;
      const selected = selectedRow();
      return selectedProfile === null || selected?.model_id === "" ? currentImageInput === "supported" : selected?.image_input === "supported";
    }
    profileSelect.addEventListener("change", () => {
      if (selectionControlled() || writing || openingForSend || pending || storageInvalid || !enabled || destroyed) { render(); return; }
      const row = profilesReadable && profileRows.find(row => row.id === profileSelect.value && row.available === true);
      if (profileSelect.value && !row) { render(); return; }
      const previous = selectedProfile; composerVersion++; selectedProfile = row ? preference({ id: row.id, revision: row.revision }) : null;
      if (!save()) { selectedProfile = previous; profilesError = "浏览器不能保存Pi配置选择；原选择与草稿保留。"; }
      render();
    });
    async function loadInitialProfiles() { if (!configurationSelection?.managed() || configurationSelection.takeCatalogRefresh(ready())) await loadProfiles(); }
    async function loadProfiles() {
      syncWorkspace(); if (!ready() || writing || profilesReading || !workspace) return;
      configurationSelection?.catalogStarted();
      const stamp = generation, version = ++profileRequest, writes = writeVersion; profilesReading = true; render();
      try {
        const value = await api(apiRoot + "/profiles");
        if (!sameScope(stamp) || version !== profileRequest || writes !== writeVersion) return;
        if (!object(value) || value.workspace_id !== workspace || value.chat_id !== chatId || !Array.isArray(value.profiles) || value.profiles.length > 512 || typeof value.native_catalog_ready !== "boolean") throw Error("Pi配置目录身份或格式不完整。");
        if (value.current_image_input !== undefined && !["supported", "unsupported", "unknown"].includes(value.current_image_input)) throw Error("Pi当前图片能力没有读完整。");
        const rows = value.profiles.map(row => {
          const selected = profile(row);
          if (typeof row.available !== "boolean" || row.unavailable_reason !== null && (typeof row.unavailable_reason !== "string" || row.unavailable_reason.length > 1000)) throw Error("Pi配置兼容状态不完整。");
          if (row.image_input !== undefined && !["supported", "unsupported", "unknown"].includes(row.image_input)) throw Error("Pi图片能力没有读完整。");
          return { ...selected, available: row.available, unavailable_reason: row.unavailable_reason, image_input: row.image_input || "unknown" };
        });
        if (new Set(rows.map(row => row.id)).size !== rows.length) throw Error("Pi配置目录标识重复。");
        profileRows = rows; currentImageInput = value.current_image_input || "unknown"; profilesReadable = true; profilesError = "";
      } catch (error) { if (sameScope(stamp) && version === profileRequest && writes === writeVersion) { profilesReadable = false; currentImageInput = "unknown"; profilesError = String(error?.message || "Pi配置目录暂时无法读取。").slice(0, 300); } }
      finally { if (sameScope(stamp) && version === profileRequest) { profilesReading = false; render(); } }
    }
    function save() {
      if (!storageKey || storageInvalid) return false;
      const version = selectedMemory || pending?.memory_profile ? 4 : pending && !pending.body_json ? 1 : pending?.material_refs !== undefined || draftMaterials.length ? 3 : 2;
      try { window.sessionStorage.setItem(storageKey, encode({ version, draft, pending, answer_journal: journal, ...(version >= 2 ? { selected_profile: selectedProfile } : {}), ...(version >= 3 ? { material_refs: draftMaterials } : {}), ...(version === 4 ? { selected_memory: selectedMemory } : {}), ...(configurationSelection?.managed() ? { configuration_selection: configurationSelection.checkpoint() } : {}) })); return true; } catch (_) { return false; }
    }
    function syncWorkspace() {
      const id = String(getWorkspace() || ""); if (id === workspace) return;
      save(); openingForSend = openSendUnconfirmed = false; composerVersion++; workspace = id; generation++; storageKey = id ? "rabbit.pi-native.page.v1." + encodeURIComponent(id) + "." + chatId : "";
      draft = ""; draftMaterials = []; pending = null; journal = Object.create(null); storageInvalid = false; snapshot = null; readable = false; errorText = notice = ""; digests.clear(); requestNodes.clear(); toolNodes.clear(); burned.clear(); reading = writing = false; readPending = false;
      selectedProfile = null; profileRows = []; profilesReadable = profilesReading = posting = false; profileRequest++; writeVersion++; profilesError = receiptError = ""; freshSend = null; currentImageInput = "unknown"; historyBefore = null; historyRequest++;
      selectedMemory = null; memoryRows = []; memoryReadable = memoryReading = false; memoryError = memorySourcesError = ""; memoryRequest++; memoryVersion++; memoryEvidence.clear(); memoryCards.clear(); memoryTextVersions.clear();
      selectionUnsupported = false; configurationSelection?.reset(undefined, { workspace, chatId });
      try {
        const saved = storageKey && JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
        if (saved) {
          if (!object(saved) || ![1, 2, 3, 4].includes(saved.version) || !validText(saved.draft) || saved.draft.length > 200000) throw Error(); draft = saved.draft;
          if (Object.hasOwn(saved, "configuration_selection")) { if (!configurationSelection) throw Error(); configurationSelection.reset(saved.configuration_selection, { workspace, chatId }); }
          if (saved.version >= 2) selectedProfile = saved.selected_profile === null ? null : preference(saved.selected_profile);
          if (saved.version >= 3) draftMaterials = materialRefs(saved.material_refs);
          if (saved.version === 4) selectedMemory = saved.selected_memory === null ? null : selectedMemoryChoice(saved.selected_memory);
          if (saved.pending !== null) {
            const p = saved.pending; if (!object(p) || !uuid(p.client_id) || !validText(p.message) || !p.message.trim() || p.message.length > 200000 || !(saved.version >= 3 && p.request_sha256 === null || /^[a-f0-9]{64}$/.test(p.request_sha256)) || !["submitted_unknown", "accepted"].includes(p.status)) throw Error();
            pending = { client_id: p.client_id, message: p.message, request_sha256: p.request_sha256, status: p.status };
            if (saved.version >= 2) {
              const selected = p.profile_snapshot === null ? null : profile(p.profile_snapshot);
              const refs = saved.version >= 3 ? materialRefs(p.material_refs) : [], memory = saved.version === 4 && p.memory_profile !== undefined ? selectedMemoryChoice(p.memory_profile) : null;
              if (memory && canonical(memory) !== canonical(selectedMemory)) throw Error();
              if (p.body_json !== encode(sendBody(p.client_id, p.message, selected, refs, memory)) || !/^[a-f0-9]{64}$/.test(p.intent_sha256) || !/^[a-f0-9]{64}$/.test(p.journal_sha256)) throw Error();
              Object.assign(pending, { body_json: p.body_json, intent_sha256: p.intent_sha256, journal_sha256: p.journal_sha256, profile_snapshot: selected });
              if (memory) pending.memory_profile = memory;
              if (saved.version === 3 || saved.version === 4 && (memory || refs.length)) {
                if (!refs.length && !memory || p.request_sha256 === null && p.derived_sha256 !== undefined || p.request_sha256 !== null && !/^[a-f0-9]{64}$/.test(p.derived_sha256)) throw Error();
                pending.material_refs = refs; if (p.derived_sha256 !== undefined) pending.derived_sha256 = p.derived_sha256;
              }
              if (p.startup_overrides !== undefined || p.startup_sha256 !== undefined) {
                if (!/^[a-f0-9]{64}$/.test(p.startup_sha256)) throw Error();
                Object.assign(pending, { startup_overrides: startup(p.startup_overrides), startup_sha256: p.startup_sha256 });
              }
            }
          }
          if (!object(saved.answer_journal) || Object.keys(saved.answer_journal).length > 128) throw Error();
          for (const [digest, state] of Object.entries(saved.answer_journal)) { if (!/^[a-f0-9]{64}$/.test(digest) || state !== "submitted_unknown") throw Error(); journal[digest] = state; }
        }
      } catch (_) { storageInvalid = true; errorText = "浏览器保存的Pi回执无法读取；原记录保留，尚未提交。"; }
      nativeMaterials?.setScope(encode([workspace, chatId]));
      input.value = draft;
    }
    function initialProfileChoice(value) {
      if (selectionControlled() || !workspace || !enabled || destroyed
          || writing || textPicker?.busy() || openingForSend || openSendUnconfirmed || pending || storageInvalid || unsettled() || active()
          || !readable && (snapshot !== null || visible || errorText !== "") || snapshot?.disconnect_unconfirmed === true || snapshot?.extension_requests?.some(row => ["pending", "unknown"].includes(row.state))
          || draft !== "" || input.value !== "" || draftMaterials.length || selectedProfile !== null || selectedMemory !== null || Object.keys(journal).length) throw Error("Pi原生配置入口已有选择、草稿或待核对请求；没有覆盖。");
      return profile(value);
    }
    function selectSavedProfile(value) {
      syncWorkspace();
      if (!exact(value, ["workspace_id", "chat_id", "profile"]) || value.workspace_id !== workspace || value.chat_id !== chatId) throw Error("Pi原生配置所属对话已变化；没有覆盖。");
      const chosen = initialProfileChoice(value.profile), previous = selectedProfile;
      selectedProfile = preference({ id: chosen.id, revision: chosen.revision });
      if (!save()) { selectedProfile = previous; throw Error("浏览器不能保存Pi配置选择；原选择与草稿保留。"); }
      composerVersion++; render(); return true;
    }
    function putDraft(value) {
      syncWorkspace();
      if (!object(value) || value.workspace_id !== workspace || !workspace || value.chat_id !== chatId || !enabled || destroyed
          || writing || textPicker?.busy() || pending || storageInvalid || unsettled() || active() || draft !== "" || input.value !== "" || draftMaterials.length) throw Error("Pi原生草稿已有内容、正在处理请求或所属对话已变化；没有覆盖。");
      const hasAI = Object.hasOwn(value, "ai_profile"), hasMemory = Object.hasOwn(value, "memory_profile");
      let choice = selectedProfile;
      if (hasAI) {
        if (!exact(value.ai_profile, profileFields) || snapshot !== null && (!["not_open", "closed"].includes(snapshot.connection?.state) || snapshot.binding != null || snapshot.native_state != null || snapshot.operations?.length || snapshot.operation)) throw Error("Pi目标配置只能放入全新原生对话；原草稿保留。");
        const chosen = initialProfileChoice(value.ai_profile); choice = preference({ id: chosen.id, revision: chosen.revision });
      }
      const memory = hasMemory ? value.memory_profile === null ? null : selectedMemoryChoice(value.memory_profile) : selectedMemory;
      if (hasMemory && selectedMemory !== null && canonical(memory) !== canonical(selectedMemory)) throw Error("Pi原生对话已有不同的共享记忆选择；没有覆盖。");
      const text = value.text, refs = materialRefs(value.material_refs);
      if (refs.length && (!materialsEnabled || !nativeMaterials)) throw Error("Pi材料交接尚未启用；整份未放入草稿。");
      if (!validText(text) || !(text.trim() || text === "" && (refs.length || hasAI || hasMemory)) || typeof TextEncoder !== "function" || new TextEncoder().encode(text).length > 128 * 1024
          || new TextEncoder().encode(encode(sendBody("00000000-0000-4000-8000-000000000000", text, choice, refs, memory))).length > 256 * 1024) throw Error("交接内容无效或超过原生发送容量。");
      if (value.material_preview !== undefined) { if (!nativeMaterials || !refs.length) throw Error("材料预览缺少所选版本；整份未放入。"); nativeMaterials.seed(refs, value.material_preview); }
      const previous = { draft, input: input.value, refs: draftMaterials, profile: selectedProfile, memory: selectedMemory };
      draft = text; input.value = text; draftMaterials = refs; selectedProfile = choice; selectedMemory = memory;
      if (!save()) { draft = previous.draft; input.value = previous.input; draftMaterials = previous.refs; selectedProfile = previous.profile; selectedMemory = previous.memory; throw Error("浏览器不能保存Pi原生草稿；原内容保留，尚未发送。"); }
      composerVersion++; if (hasMemory) memoryVersion++; render(); return true;
    }
    const sameScope = stamp => !destroyed && stamp === generation && workspace === String(getWorkspace() || "");
    async function digest(value) {
      if (!window.crypto?.subtle || typeof TextEncoder !== "function") throw Error("当前浏览器不能安全保存Pi回执，尚未提交。");
      const bytes = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)); return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, "0")).join("");
    }
    const promptHash = message => digest(encode({ message, type: "prompt" }));
    const nativeText = (value, limit) => validText(value) && Array.from(value).length <= limit && value.length > 0 && value.trim() === value && !value.startsWith("-") && !/[\p{Cc}\p{Cf}\p{Cs}]/u.test(value);
    function startup(value) {
      if (!object(value) || Object.keys(value).sort().join(",") !== "model,provider,thinking" || !nativeText(value.provider, 256) || !nativeText(value.model, 1024)
          || !["off", "minimal", "low", "medium", "high", "xhigh"].includes(value.thinking)) throw Error("Pi冻结启动设置没有读完整；原请求保留。");
      return { provider: value.provider, model: value.model, thinking: value.thinking };
    }
    const startupHash = (p, overrides) => digest(canonical({ client_id: p.client_id, intent_sha256: p.intent_sha256, startup_overrides: overrides }));
    const derivedHash = (p, hash) => digest(canonical({ client_id: p.client_id, intent_sha256: p.intent_sha256, request_sha256: hash }));
    async function verifyPending() {
      if (!pending) return;
      if (pending.material_refs === undefined && await promptHash(pending.message) !== pending.request_sha256) throw Error("Pi原请求指纹无法核对，尚未提交；原记录保留。");
      if (pending.material_refs !== undefined && pending.request_sha256 !== null && await derivedHash(pending, pending.request_sha256) !== pending.derived_sha256) throw Error("Pi派生原生回执指纹无法核对；原记录保留。");
      if (pending.body_json) {
        const body = sendBody(pending.client_id, pending.message, pending.profile_snapshot, materialRefs(pending.material_refs), pending.memory_profile || null);
        if (pending.body_json !== encode(body) || await digest(canonical(body)) !== pending.intent_sha256
            || await digest(canonical(journalBody(body, pending.profile_snapshot, pending.memory_profile || null))) !== pending.journal_sha256) throw Error("Pi保存的配置或原请求指纹不一致；原记录保留，没有再次发送。");
        if (pending.startup_overrides !== undefined && await startupHash(pending, startup(pending.startup_overrides)) !== pending.startup_sha256) throw Error("Pi保存的启动设置指纹不一致；原记录保留，没有再次发送。");
      }
    }
    async function checkIntent(value) {
      if (!pending?.body_json) return null;
      const candidates = [value.profile_intent, ...(value.profile_intents || [])].filter(row => row && row.client_id === pending.client_id);
      if (candidates.length !== 1 || candidates.some(row => Object.keys(row).sort().join(",") !== "ai_profile,chat_id,client_id,created_at,request_sha256,startup_overrides,workspace_id"
          || row.workspace_id !== workspace || row.chat_id !== chatId || row.request_sha256 !== pending.intent_sha256 || typeof row.created_at !== "string" || !row.created_at || row.created_at.length > 64 || /[\x00-\x1f\x7f]/.test(row.created_at)
          || canonical(row.ai_profile) !== canonical(pending.profile_snapshot))) throw Error("Pi配置回执与冻结的版本不匹配；原消息和回执保留。");
      const overrides = startup(candidates[0].startup_overrides), selected = pending.profile_snapshot;
      if (pending.startup_overrides !== undefined && canonical(overrides) !== canonical(pending.startup_overrides)) throw Error("Pi启动设置与已冻结回执不一致；原消息和回执保留。");
      if (selected?.reasoning_effort !== null && selected?.reasoning_effort !== undefined && overrides.thinking !== (selected.reasoning_effort === "none" ? "off" : selected.reasoning_effort)) throw Error("Pi原生思考设置与冻结配置不一致；原请求保留。");
      if (selected?.model_id && selected.model_id !== "local/pi/native/" + await digest(canonical({ provider: overrides.provider, model: overrides.model }))) throw Error("Pi原生模型与冻结配置不一致；原请求保留。");
      return overrides;
    }
    function receipt(value) { return object(value) && uuid(value.client_id) && value.chat_id === chatId && typeof value.request_sha256 === "string" && /^[a-f0-9]{64}$/.test(value.request_sha256) && statuses.has(value.status) && [value.created_at, value.updated_at].every(stamp => typeof stamp === "string" && stamp.length > 0 && stamp.length <= 64 && !/[\x00-\x1f\x7f]/.test(stamp)); }
    function memoryRecord(value, source, expected = null) {
      const intent = memoryCodec().intent(source, { workspaceId: workspace, chatId, clientKind: "uuid" });
      const unique = rows => [...new Map(rows.map(row => [canonical(row), row])).values()];
      const intents = unique([value.profile_intent, ...(Array.isArray(value.profile_intents) ? value.profile_intents : [])].filter(row => row?.client_id === intent.client_id));
      const rawVerified = intents.length === 1 && intents[0].workspace_id === workspace && intents[0].chat_id === chatId && intents[0].request_sha256 === intent.request_sha256
        && (intents[0].ai_profile === null || canonical(profile(intents[0].ai_profile)) === canonical(intents[0].ai_profile));
      if (expected && (intent.client_id !== expected.client_id || intent.request_sha256 !== expected.intent_sha256
          || canonical(intent.memory_profile) !== canonical(memoryCodec().snapshot(expected.memory_profile))
          || !(intent.state === "failed" && value.profile_intent === null && !intents.length) && (!rawVerified
            || canonical(intents[0]?.ai_profile) !== canonical(expected.profile_snapshot)))) throw Error("共享记忆来源与冻结的Pi原请求不一致；原草稿和回执保留。");
      const receipts = unique([value.receipt, value.operation, ...(Array.isArray(value.operations) ? value.operations : [])].filter(row => row?.client_id === intent.client_id));
      const found = receipts.length === 1 && receipt(receipts[0]) ? receipts[0] : null;
      if (intent.state === "failed" && (found || expected && (expected.request_sha256 !== null || expected.status === "accepted" || expected.startup_overrides !== undefined) || [snapshot?.operation, ...(snapshot?.operations || [])].some(row => row?.client_id === intent.client_id))) throw Error("共享记忆失败记录与原生回执矛盾；原记录保留。");
      const attached = rawVerified && intent.state === "ready" && isSHA(intent.derived_inputs_sha256) && found
        && ["accepted", "completed", "failed", "interrupted"].includes(found.status) && found.request_sha256 === intent.derived_inputs_sha256;
      return { intent, rawVerified, attached: !!attached };
    }
    function updateMemoryEvidence(value) {
      memoryEvidence.clear(); memorySourcesError = "";
      try {
        if (value.memory_intents === undefined) { memoryTextVersions.clear(); memoryCards.clear(); return; }
        if (!Array.isArray(value.memory_intents) || value.memory_intents.length > 128) throw Error();
        for (const source of value.memory_intents) {
          const expected = pending?.client_id === source?.client_id ? pending : null;
          if (expected && !expected.memory_profile) throw Error();
          const record = memoryRecord(value, source, expected);
          if (memoryEvidence.has(record.intent.client_id)) throw Error(); memoryEvidence.set(record.intent.client_id, record);
        }
      } catch (_) { memoryEvidence.clear(); memorySourcesError = "共享记忆来源记录暂时无法核验；Pi原生消息与草稿保留。"; }
      for (const client of memoryCards.keys()) if (!memoryEvidence.has(client)) memoryCards.delete(client);
      memoryTextVersions.clear();
      for (const record of memoryEvidence.values()) if (record.attached) memoryTextVersions.set(canonical(record.intent), record.intent);
    }
    function splitMemoryText(text) {
      for (const [version, intent] of memoryTextVersions) {
        const raw = memoryCodec().piSuffix(intent);
        if (text.endsWith(raw) && text.length > raw.length) return { text: text.slice(0, -raw.length), raw, version };
      }
      return null;
    }
    function renderMemorySources() {
      const nodes = [];
      for (const record of memoryEvidence.values()) {
        const { intent, attached, rawVerified } = record, profile = intent.memory_profile, key = canonical([intent, attached, rawVerified, language]);
        let stored = memoryCards.get(intent.client_id);
        if (!stored || stored.key !== key) {
          const card = node("details", "rbs-parameters rbs-memory-record"); card.open = stored?.node.open === true;
          card.append(node("summary", "", attached ? tr("本轮附加的共享记忆", "Shared memory added for this send") : tr("共享记忆准备记录", "Shared memory preparation record")),
            node("p", "rbs-note", `${profile.name} · ${tr("第 ", "v")}${profile.revision} · ${intent.created_at}`),
            node("p", "rbs-note", tr("本应用附加的来源资料，原生消息完整保留；不单独证明模型实际读取。", "Reference data added by this app; native messages remain intact. This alone does not prove model use.")));
          if (!rawVerified) card.append(node("p", "rbs-note", tr("原始发送意图尚未配对核验。", "The original send intent is not verified.")));
          if (intent.state === "ready") {
            card.append(node("p", "rbs-note", `${intent.recall_info.retrieved_at} · ${tr("完整条目 ", "Whole facts ")}${intent.recall_info.included_facts} · ${tr("未附 ", "Omitted ")}${intent.recall_info.omitted_facts}`));
            for (const ref of intent.recall_info.used_references) card.append(node("p", "rbs-note", ref.type + " · " + ref.retrieved_at), node("pre", "rbs-native", ref.text), node("p", "rbs-note", ref.reference_id + (ref.document_ref ? " · " + ref.document_ref : "")));
            if (profile.habits) card.append(node("p", "rbs-note", tr("本轮选定的回答习惯", "Selected answering preferences")), node("pre", "rbs-native", profile.habits));
          } else card.append(node("p", "rbs-note", intent.state === "failed" ? tr("资料准备失败，尚未发送；不会自动重试。", "Preparation failed before sending; no automatic retry.") : tr("资料正在准备，尚不能据此认定发送成功。", "Preparation is in progress; it does not confirm sending.")));
          stored = { key, node: card }; memoryCards.set(intent.client_id, stored);
        }
        nodes.push(stored.node);
      }
      memorySources.replaceChildren(...nodes);
    }
    function settlePending(found) {
      if (!pending || found.status === "submitted_unknown") return;
      if (found.status === "accepted") { pending.status = "accepted"; save(); return; }
      if (terminal.has(found.status)) {
        const old = pending, previousDraft = draft, previousInput = input.value, previousMaterials = draftMaterials;
        if (found.status !== "rejected" && draft === old.message && encode(draftMaterials) === encode(old.material_refs || [])) { draft = ""; input.value = ""; draftMaterials = []; }
        pending = null;
        if (!save()) { pending = old; draft = previousDraft; input.value = previousInput; draftMaterials = previousMaterials; errorText = "原请求结果已确认，但本地回执暂不能清除；不会重复发送。"; }
      }
    }
    async function lookupPending(stamp, writes) {
      if (!pending || storageInvalid) return;
      const original = pending, current = () => sameScope(stamp) && writes === writeVersion && pending === original;
      await verifyPending(); if (!current()) return;
      const value = await api(apiRoot + "/receipt?client_id=" + original.client_id);
      if (!current()) return;
      if (!object(value) || Object.keys(value).filter(key => key !== "memory_intent").sort().join(",") !== "chat_id,profile_intent,receipt,workspace_id" || value.workspace_id !== workspace || value.chat_id !== chatId) throw Error("Pi精确回执身份或格式不完整；原请求保留。");
      const memory = value.memory_intent == null ? null : memoryRecord(value, value.memory_intent, original.memory_profile ? original : null);
      if (original.memory_profile && !memory || !original.memory_profile && memory) throw Error("Pi共享记忆回执没有匹配原选择；原请求保留。");
      if (memory && value.receipt !== null && (memory.intent.state !== "ready" || memory.intent.derived_inputs_sha256 !== value.receipt.request_sha256)) throw Error("共享记忆派生指纹与Pi原生回执不符；原请求保留。");
      if (memory?.intent.state === "failed" && value.receipt === null) {
        pending = null;
        if (!save()) { pending = original; throw Error("共享记忆准备失败且尚未发送，但浏览器无法保存核对结果；原草稿与回执保留。"); }
        notice = tr("共享记忆准备失败，尚未发送；草稿保留，可修复后手动发送。", "Shared memory preparation failed before sending. Draft kept; fix it and send manually.");
        return;
      }
      if (value.receipt === null) {
        // A concurrent preparation may not have begun the native prompt yet.
        // Absence is never proof that the original request was not submitted.
        if (value.profile_intent !== null) await checkIntent(value);
        return;
      }
      const found = value.receipt;
      if (!receipt(found) || found.client_id !== original.client_id || original.request_sha256 !== null && found.request_sha256 !== original.request_sha256) throw Error("Pi回执与原请求不匹配，原草稿和回执保留。");
      const overrides = await checkIntent(value);
      if (!current()) return;
      if (original.material_refs !== undefined && original.request_sha256 === null) {
        if (overrides === null) throw Error("Pi材料请求缺少原始配置意图；原请求保留。");
        const hash = await derivedHash(original, found.request_sha256); if (!current()) return;
        original.request_sha256 = found.request_sha256; original.derived_sha256 = hash;
        if (!save()) { original.request_sha256 = null; delete original.derived_sha256; throw Error("浏览器不能保存Pi派生回执；原请求保留。"); }
      }
      if (overrides !== null && original.startup_overrides === undefined) {
        const hash = await startupHash(original, overrides); if (!current()) return;
        original.startup_overrides = overrides; original.startup_sha256 = hash;
        if (!save()) { delete original.startup_overrides; delete original.startup_sha256; throw Error("浏览器不能保存已核对的Pi启动设置；原请求保留。"); }
      }
      settlePending(found);
    }
    function ready() { return enabled && !destroyed && readable && snapshot?.connection.state === "ready"; }
    function unsettled() { return (snapshot?.operations || []).some(r => ["accepted", "submitted_unknown"].includes(r.status)) || snapshot?.operation && ["accepted", "submitted_unknown"].includes(snapshot.operation.status); }
    function active() { const state = snapshot?.native_state; return snapshot?.native_activity?.continuing === true || !!(state && (state.isStreaming === true || state.isCompacting === true || Number.isInteger(state.pendingMessageCount) && state.pendingMessageCount > 0)); }
    function idle() { const s = snapshot?.native_state; return ready() && object(s) && s.isStreaming === false && s.isCompacting === false && s.pendingMessageCount === 0 && !active() && !unsettled(); }
    function canOpenForSend() { return selectionReady() && !textPicker?.busy() && !openSendUnconfirmed && enabled && !destroyed && visible && !root.hidden && !document.hidden && readable && ["not_open", "closed"].includes(snapshot?.connection.state) && snapshot.disconnect_unconfirmed !== true && !snapshot.connection.problem && !pending && !storageInvalid && !unsettled() && !active() && !snapshot?.extension_requests.some(row => ["pending", "unknown"].includes(row.state)); }
    function sendStateReady() { return !textPicker?.busy() && !openSendUnconfirmed && snapshot?.disconnect_unconfirmed !== true && !snapshot?.extension_requests.some(row => ["pending", "unknown"].includes(row.state)) && idle() && profileReady() && materialsReady() && memoryReady(); }
    async function consume(value, stamp, writes = writeVersion) {
      const current = () => sameScope(stamp) && writes === writeVersion;
      if (!current()) return;
      if (!object(value) || value.agent !== undefined && value.agent !== "pi" || value.schema_version !== undefined && value.schema_version !== 1 || value.workspace_id !== workspace || value.chat_id !== chatId || !object(value.connection) || !["not_open", "opening", "ready", "failed", "closed"].includes(value.connection.state) || value.disconnect_unconfirmed !== undefined && typeof value.disconnect_unconfirmed !== "boolean"
          || !(typeof value.epoch === "string" && value.epoch || Number.isSafeInteger(value.epoch) && value.epoch >= 0) || !(value.binding === null || object(value.binding) && typeof value.binding.persisted === "boolean" && (
            value.binding.session_id === null && value.binding.session_file === null && !value.binding.persisted
            || typeof value.binding.session_id === "string" && value.binding.session_id && (value.binding.session_file === null || typeof value.binding.session_file === "string" && value.binding.session_file)
              && (!value.binding.persisted || typeof value.binding.session_file === "string" && value.binding.session_file)))
          || !(value.native_state === null || object(value.native_state)) || value.native_activity !== undefined && (!object(value.native_activity) || typeof value.native_activity.continuing !== "boolean") || !Array.isArray(value.native_messages) || !value.native_messages.every(object) || !Array.isArray(value.events) || !value.events.every(object)
          || !Array.isArray(value.extension_requests) || !value.extension_requests.every(r => object(r) && object(r.request)) || !Array.isArray(value.operations) || !value.operations.every(receipt) || !(value.operation === null || receipt(value.operation))) throw Error("Pi原生视图身份或格式不完整。");
      if (value.native_messages.length > 2000 || new TextEncoder().encode(encode(value.native_messages)).length > 2 * 1024 * 1024) throw Error("Pi原生消息超过本页读取容量，原草稿保留；没有静默截断。");
      if (value.native_message_refs !== undefined || value.native_messages_info !== undefined) {
        if (!window.RabbitPiNativeHistory) throw Error("Pi历史显示组件未就绪；未把显示投影当成完整原文。");
        window.RabbitPiNativeHistory.validate(value);
      }
      const nextDigests = new Map();
      for (const record of value.extension_requests) if (typeof record.token === "string" && record.token) {
        try { nextDigests.set(record.token, await digest(encode([value.epoch, value.binding?.session_id || null, record.token, record.request]))); } catch (_) { /* Readable original form remains disabled without durable answer hashing. */ }
        if (!current()) return;
      }
      if (!current()) return;
      if (snapshot && (snapshot.epoch !== value.epoch || snapshot.binding?.session_id !== value.binding?.session_id)) { requestNodes.clear(); toolNodes.clear(); burned.clear(); }
      try { await verifyPending(); } catch (error) { if (current()) storageInvalid = true; throw error; }
      if (!current()) return; snapshot = value; digests = nextDigests; readable = true; if (value.connection.state === "ready" && !value.connection.problem && value.disconnect_unconfirmed !== true) openSendUnconfirmed = false; updateMemoryEvidence(value);
    }
    function schedule() { clearTimeout(timer); timer = null; if (visible && enabled && !destroyed && !document.hidden) timer = setTimeout(() => refresh(), 1000); }
    async function refresh() {
      syncWorkspace(); if (destroyed || !enabled || !workspace) return;
      if (posting) { readPending = true; return; }
      if (reading) { readPending = true; return; }
      const stamp = generation, writes = writeVersion, historyTicket = historyRequest; reading = true;
      try {
        if (pending) {
          try { await lookupPending(stamp, writes); if (sameScope(stamp) && writes === writeVersion) receiptError = ""; }
          catch (error) { if (sameScope(stamp) && writes === writeVersion) { const text = String(error?.message || ""); receiptError = text && text.length < 300 ? text : "Pi精确回执暂不可读取；原请求保留。"; onError(error); } }
        }
        if (!sameScope(stamp) || writes !== writeVersion) return;
        const query = [...(pending ? ["client_id=" + pending.client_id] : []), ...(historyBefore === null ? [] : ["before=" + historyBefore])].join("&");
        const value = await api(apiRoot + (query ? "?" + query : "")); if (!sameScope(stamp) || writes !== writeVersion || historyTicket !== historyRequest) return; await consume(value, stamp, writes);
        if (sameScope(stamp) && writes === writeVersion) { if (configurationSelection) configurationSelection.observe(value); else if (Object.hasOwn(value, "configuration_selection") || Object.hasOwn(value, "configuration_selection_error")) selectionUnsupported = true; }
      }
      catch (error) { if (sameScope(stamp) && writes === writeVersion) { readable = false; fail(error, "Pi原生状态暂不可读取；原草稿和回执保留。"); } }
      finally { if (sameScope(stamp)) { reading = false; render(); const again = readPending; readPending = false; if (again) await refresh(); else schedule(); } }
    }
    function fail(error, fallback) { const text = String(error && error.message || ""); errorText = text && text.length < 300 ? text : fallback; onError(error); }
    async function mutate(kind, body) {
      syncWorkspace(); if (destroyed || !enabled || !readable || writing || !workspace) return null;
      const stamp = generation, writes = ++writeVersion; writing = posting = true; errorText = ""; render();
      try {
        const value = await api(apiRoot + "/" + kind, { method: "POST", body: encode(body) }); if (!sameScope(stamp)) return null;
        if (kind === "send" && (!receipt(value.receipt) || value.receipt.client_id !== body.client_id || pending?.request_sha256 !== null && value.receipt.request_sha256 !== pending?.request_sha256)) throw Error("Pi发送确认与原请求不匹配；原回执保留。");
        if (kind !== "disconnect" && (kind !== "send" || value.view !== undefined)) await consume(value.view || value, stamp, writes);
        if (kind === "open" && sameScope(stamp) && snapshot?.connection.state === "ready") notice = "";
        return sameScope(stamp) ? value : null;
      } catch (error) {
        if (sameScope(stamp)) {
          if (kind === "send" && error.accepted === false && freshSend?.client_id === body.client_id && freshSend.generation === stamp && pending?.client_id === body.client_id && pending.status === "submitted_unknown" && (!pending.memory_profile || pending.request_sha256 === null && pending.derived_sha256 === undefined)) {
            const old = pending; pending = null; if (!save()) pending = old;
          }
          fail(error, "结果仍待核对，没有自动重复提交。");
        }
        return null;
      }
      finally { if (kind === "send" && freshSend?.client_id === body.client_id && freshSend.generation === stamp) freshSend = null; if (sameScope(stamp)) { writing = posting = false; render(); await refresh(); if (kind === "open" && ready()) { await loadInitialProfiles(); await loadMemoryProfiles(); } } }
    }
    async function send() {
      syncWorkspace(); if (openingForSend || writing || pending || storageInvalid || !visible || root.hidden || document.hidden || !validText(draft) || !draft.trim() || draft.length > 200000 || !(sendStateReady() || canOpenForSend())) return;
      const sendGeneration = generation, sendVersion = composerVersion, sendDraft = draft, sendChoice = canonical(selectedProfile), sendMemory = canonical(selectedMemory), sendMaterials = encode(draftMaterials);
      const sameSendView = () => sameScope(sendGeneration) && visible && !root.hidden && !document.hidden && !textPicker?.busy() && composerVersion === sendVersion && draft === sendDraft && canonical(selectedProfile) === sendChoice && canonical(selectedMemory) === sendMemory && encode(draftMaterials) === sendMaterials;
      if (!sendStateReady()) {
        openingForSend = true; render();
        try {
          const opened = await mutate("open", {});
          if (!opened && sameScope(sendGeneration)) openSendUnconfirmed = true;
          if (!opened || !sameSendView() || !sendStateReady()) return;
        } finally { if (sameScope(sendGeneration)) { openingForSend = false; render(); } }
      }
      if (!sameSendView() || !sendStateReady()) return;
      const stamp = generation, message = draft, refs = materialRefs(draftMaterials), selected = selectedProfile === null ? null : profile(selectedRow()), memory = selectedMemory === null ? null : selectedMemoryChoice(selectedMemory), identity = encode([snapshot.epoch, snapshot.binding?.session_id]), clientId = window.crypto?.randomUUID?.(); if (!uuid(clientId)) { errorText = "无法生成稳定Pi请求编号，尚未发送。"; render(); return; }
      writing = true; render();
      try {
        const body = sendBody(clientId, message, selected, refs, memory), bodyJson = encode(body);
        if (typeof TextEncoder !== "function" || new TextEncoder().encode(message).length > 128 * 1024 || new TextEncoder().encode(bodyJson).length > 256 * 1024) throw Error("消息超过单次发送容量，原稿保留，请缩短后发送。");
        const hash = refs.length || memory ? null : await promptHash(message), intentHash = await digest(canonical(body)), journalHash = await digest(canonical(journalBody(body, selected, memory))); if (!sameSendView() || !sameScope(stamp) || !sendStateReady() || pending || encode(refs) !== encode(draftMaterials) || canonical(memory) !== canonical(selectedMemory) || memory && (draft !== message || identity !== encode([snapshot.epoch, snapshot.binding?.session_id]))) return;
        pending = { client_id: clientId, message, request_sha256: hash, status: "submitted_unknown", body_json: bodyJson, intent_sha256: intentHash, journal_sha256: journalHash, profile_snapshot: selected };
        if (refs.length || memory) pending.material_refs = refs;
        if (memory) pending.memory_profile = memory;
        if (!save()) { pending = null; throw Error("浏览器不能保存Pi发送回执，尚未发送；原稿保留。"); }
        freshSend = { client_id: clientId, generation: stamp };
      } catch (error) { if (sameScope(stamp)) fail(error, "无法保存原请求，尚未发送。"); }
      finally { if (sameScope(stamp)) { writing = false; render(); } }
      if (!sameSendView() || !sameScope(stamp) || !pending || pending.client_id !== clientId) return;
      await mutate("send", JSON.parse(pending.body_json));
    }
    function activeClient() { return pending?.client_id || ([snapshot?.operation, ...(snapshot?.operations || [])].find(r => r && ["accepted", "submitted_unknown"].includes(r.status)) || {}).client_id; }
    function submit() {
      syncWorkspace();
      if (!enabled || destroyed || !visible || root.hidden || document.hidden) return;
      return !draft.trim() && (active()) ? interrupt() : send();
    }
    async function interrupt() { const id = activeClient(); if (!ready() || !active() || writing || !uuid(id)) return; const value = await mutate("interrupt", { client_id: id }); if (value) notice = tr("已提交中断，等待原生结束；不能据此推断后台工具已退出。", "Interrupt requested; waiting for native completion. Background-tool exit is not inferred."); render(); }
    async function disconnect() { if (!enabled || destroyed || !readable || writing || openingForSend || pending || unsettled() || snapshot.extension_requests.length || ready() && active()) return; const value = await mutate("disconnect", {}); if (value?.disconnected === true) notice = tr("已断开；原生会话仍保留，后台工具退出不作额外推断。", "Disconnected; native session remains. Background-tool exit is not inferred."); render(); }
    function raw(parent, value, label) { const details = node("details", "rbs-parameters"); details.append(node("summary", "", label), node("pre", "rbs-native", JSON.stringify(value, null, 2))); parent.append(details); }
    function rich(parent, text) { if (window.OneAIRichText?.render) window.OneAIRichText.render(parent, text, { openDocument }); else parent.textContent = text; }
    function textParts(content) { return typeof content === "string" ? content : Array.isArray(content) ? content.filter(p => object(p) && p.type === "text" && typeof p.text === "string").map(p => p.text).join("\n") : ""; }
    function renderMessages() {
      if (historyView && snapshot) { historyView.render(snapshot, { language, before: historyBefore }); return; }
      // Preserve only the current DOM's expansion booleans, never transcript copies.
      const expanded = new Map(), nodes = [], operations = [];
      for (const box of [...messages.children, ...historyTools.children]) {
        const key = box.getAttribute("data-pi-message-key"), last = box.children[box.children.length - 1];
        if (key) expanded.set(key, { outer: box.open === true, raw: last?.open === true });
      }
      function append(box, key) {
        box.setAttribute("data-pi-message-key", key); const previous = expanded.get(key), last = box.children[box.children.length - 1];
        if (box.tagName === "DETAILS") box.open = previous?.outer === true;
        if (last?.tagName === "DETAILS") last.open = previous?.raw === true;
        (box.getAttribute("data-message-role") ? nodes : operations).push(box);
      }
      for (const [index, message] of (snapshot?.native_messages || []).entries()) {
        const tool = message.role === "toolResult", box = node(tool ? "details" : "article", "rbs-item" + (message.role === "user" ? " rbs-user" : ""));
        if (["user", "assistant"].includes(message.role)) { box.className += " rbs-message-" + message.role; box.setAttribute("data-message-role", message.role); box.setAttribute("aria-label", message.role === "user" ? tr("用户消息", "User message") : tr("AI消息", "AI message")); }
        else box.append(node(tool ? "summary" : "p", "rbs-item-role", tool ? `${message.toolName || "Pi"} · ${message.isError === true ? tr("工具失败", "Tool failed") : tr("工具结果", "Tool result")}` : String(message.role || "Pi")));
        const body = node("div", "rbs-item-body"), text = textParts(message.content), key = encode(["message", index, message.role, message.timestamp ?? null, message.toolCallId ?? null]);
        // textParts filters out images/custom parts. Eligibility must inspect
        // the complete original content so those parts cannot be hidden by a card.
        const onlyText = typeof message.content === "string" || Array.isArray(message.content) && message.content.length === 1 && message.content[0]?.type === "text";
        const complete = typeof message.content === "string" || onlyText && !["truncated", "display_truncated", "display_omitted"].some(key => message.content[0]?.[key] === true);
        const memory = message.role === "user" && onlyText && complete && message.truncated !== true && message.display_truncated !== true ? splitMemoryText(text) : null, display = memory?.text ?? text;
        if (tool) body.append(node("pre", "rbs-native", display)); else if (!(onlyText && handoffView?.render(body, { message: display, role: message.role, messageKey: key, language }))) rich(body, display);
        if (memory) { const detail = node("details", "rbs-parameters"); detail.append(node("summary", "", tr("附加的共享记忆原文", "Original added shared memory")), node("pre", "rbs-native", memory.raw)); body.append(detail); }
        box.append(body); if (!["user", "assistant"].includes(message.role)) raw(box, message, tr("原始消息", "Original message")); append(box, key);
      }
      const preview = [...(snapshot?.events || [])].reverse().find(e => ["message_update", "message_start"].includes(e.type) && object(e.message));
      if (active() && preview && preview.message.role === "assistant") { const text = textParts(preview.message.content); if (text && !(snapshot.native_messages || []).some(m => m.role === "assistant" && textParts(m.content) === text)) { const box = node("article", "rbs-item rbs-message-assistant"); box.setAttribute("data-message-role", "assistant"); box.setAttribute("data-message-state", "streaming"); box.setAttribute("aria-label", tr("AI消息（正在回复）", "AI message (replying)")); const body = node("div", "rbs-item-body"); rich(body, text); box.append(body); append(box, encode(["live", preview.message.role, preview.message.timestamp ?? null])); } }
      messages.replaceChildren(...nodes); historyTools.replaceChildren(...operations);
    }
    function renderTools() {
      const current = new Map();
      for (const event of snapshot?.events || []) if (["tool_execution_start", "tool_execution_update", "tool_execution_end"].includes(event.type) && typeof event.toolCallId === "string") {
        const pair = current.get(event.toolCallId) || {}; pair[event.type] = event; current.set(event.toolCallId, pair);
      }
      const nodes = [];
      for (const [key, value] of current) {
        let card = toolNodes.get(key); if (!card) { card = node("details", "rbs-item"); card.append(node("summary"), node("pre", "rbs-native")); toolNodes.set(key, card); }
        const end = value.tool_execution_end, update = value.tool_execution_update, start = value.tool_execution_start, latest = end || update || start;
        card.children[0].textContent = `${latest.toolName || "Pi"} · ${end ? end.isError === true ? tr("工具失败", "Tool failed") : tr("工具完成", "Tool completed") : tr("工具执行中", "Tool running")}`;
        card.children[1].textContent = JSON.stringify(value, null, 2); nodes.push(card);
      }
      for (const key of toolNodes.keys()) if (!current.has(key)) toolNodes.delete(key); tools.replaceChildren(...nodes);
    }
    function requestFingerprint(record) { return encode([snapshot.epoch, snapshot.binding?.session_id || null, record.token, record.request]); }
    async function answer(record, result, fingerprint) {
      const current = snapshot?.extension_requests.find(r => r.token === record.token); if (!ready() || writing || !current || current.state !== "pending" || requestFingerprint(current) !== fingerprint || burned.has(record.token)) return;
      const stamp = generation; writing = true; render();
      try {
        const hash = await digest(encode([snapshot.epoch, snapshot.binding?.session_id || null, record.token, record.request]));
        const latest = snapshot?.extension_requests.find(r => r.token === record.token);
        if (!sameScope(stamp) || !ready() || !latest || latest.state !== "pending" || requestFingerprint(latest) !== fingerprint || Object.hasOwn(journal, hash)) return;
        if (Object.keys(journal).length >= 128) throw Error("尚未核对的Pi答复回执过多，尚未提交。");
        journal[hash] = "submitted_unknown"; if (!save()) { delete journal[hash]; throw Error("浏览器不能保存Pi答复回执，尚未提交。"); }
        burned.add(record.token); const body = { token: record.token, epoch: snapshot.epoch, result }, writes = ++writeVersion; posting = true;
        const value = await api(apiRoot + "/answer", { method: "POST", body: encode(body) }); if (sameScope(stamp)) await consume(value, stamp, writes);
      } catch (error) { if (sameScope(stamp)) fail(error, "Pi答复结果尚未确认，不会重复答复。"); }
      finally { if (sameScope(stamp)) { writing = posting = false; render(); await refresh(); } }
    }
    function requestCard(record) {
      const request = record.request, box = node("article", "rbs-request"), method = request.method;
      box.append(node("h3", "", typeof request.title === "string" ? request.title : tr("Pi扩展询问", "Pi extension request"))); if (typeof request.message === "string") box.append(node("p", "", request.message)); raw(box, request, tr("原始询问", "Original request"));
      const fingerprint = requestFingerprint(record), supported = dialogs.has(method) && typeof request.id === "string" && request.type === "extension_ui_request"
        && (method !== "select" || Array.isArray(request.options) && request.options.length > 0 && request.options.length <= 128 && request.options.every(o => typeof o === "string"));
      if (!supported) { box.append(node("p", "rbs-note", method === "notify" ? String(request.message || "") : tr("此Pi交互暂未支持，仅保留原始内容；没有自动答复。", "This Pi interaction is not supported. Original data is retained; no reply was sent."))); return box; }
      const actions = node("div", "rbs-actions");
      if (method === "confirm") actions.append(button("确认", "Confirm", () => answer(record, { confirmed: true }, fingerprint)), button("拒绝", "Decline", () => answer(record, { confirmed: false }, fingerprint)));
      else {
        const field = node(method === "select" ? "select" : method === "editor" ? "textarea" : "input", "rbs-control"); field.setAttribute("aria-label", request.title || "Pi");
        if (method === "select") { const empty = node("option", "", tr("请选择", "Choose")); let marker = ""; while (request.options.includes(marker)) marker += "_"; empty.value = marker; empty.disabled = true; field.append(empty); for (const value of request.options) { const option = node("option", "", value); option.value = value; field.append(option); } field.value = marker; }
        else { field.value = method === "editor" && typeof request.prefill === "string" ? request.prefill : ""; if (typeof request.placeholder === "string") field.placeholder = request.placeholder; }
        box.append(field); actions.append(button("提交回答", "Submit answer", () => { if (method === "select" && !request.options.includes(field.value)) return; if (!validText(field.value) || new TextEncoder().encode(field.value).length > 64 * 1024) return; return answer(record, { value: field.value }, fingerprint); }));
      }
      actions.append(button("取消询问", "Cancel request", () => answer(record, { cancelled: true }, fingerprint))); box.append(actions); return box;
    }
    function renderRequests() {
      const nodes = [], keys = new Set();
      for (const record of snapshot?.extension_requests || []) {
        const key = typeof record.token === "string" && record.token ? record.token : encode(record.request.id), fingerprint = requestFingerprint(record); keys.add(key);
        let card = requestNodes.get(key); if (!card || card.fingerprint !== fingerprint) { card = { fingerprint, node: requestCard(record), note: node("p", "rbs-note") }; card.node.append(card.note); requestNodes.set(key, card); }
        const hash = digests.get(record.token), blocked = !ready() || writing || storageInvalid || record.state !== "pending" || !hash || burned.has(record.token) || Object.hasOwn(journal, hash);
        for (const b of card.node.querySelectorAll("button")) b.disabled = blocked;
        card.note.textContent = record.state !== "pending" ? tr("该Pi询问已结束或状态待核对，仅保留原内容。", "This Pi request has ended or is unconfirmed. Original content remains.") : blocked && (burned.has(record.token) || hash && Object.hasOwn(journal, hash)) ? tr("答复结果尚未确认，不会重复答复。", "The answer is unconfirmed and will not be repeated.") : ""; card.note.hidden = !card.note.textContent; nodes.push(card.node);
      }
      for (const key of requestNodes.keys()) if (!keys.has(key)) requestNodes.delete(key);
      const notifications = (snapshot?.events || []).filter(e => e.type === "extension_ui_request" && !dialogs.has(e.method));
      for (const value of notifications) { const box = node("details", "rbs-item"); box.append(node("summary", "", `${value.method || "Pi"} · ${typeof value.message === "string" ? value.message : ""}`), node("pre", "rbs-native", JSON.stringify(value, null, 2))); nodes.push(box); }
      requests.replaceChildren(...nodes);
    }
    function renderProfileStatus() {
      const renderStatus = window.RabbitProfiles?.renderStatus;
      statusCard.hidden = typeof renderStatus !== "function"; connection.hidden = !statusCard.hidden;
      if (fallbackTitle) fallbackTitle.textContent = tr("备用选择", "Fallback choices");
      if (statusCard.hidden) return;
      const status = !selectionReady() || selectedProfile !== null && !profileReady() || selectedMemory !== null && !memoryReady() || !readable || storageInvalid || openSendUnconfirmed || snapshot?.disconnect_unconfirmed === true ? tr("待核对", "Needs verification")
        : openingForSend || snapshot?.connection.state === "opening" ? tr("正在连接", "Connecting")
        : snapshot?.connection.state === "failed" ? tr("连接失败", "Connection failed")
        : snapshot?.extension_requests?.some(row => ["pending", "unknown"].includes(row.state)) ? tr("等待确认", "Waiting for confirmation")
        : active() ? tr("正在回复", "Replying")
        : pending || unsettled() ? tr("待核对", "Needs verification")
        : idle() ? tr("空闲", "Idle")
        : ["not_open", "closed"].includes(snapshot?.connection.state) ? tr("未连接", "Not connected") : tr("待核对", "Needs verification");
      const chosen = profileReady() ? selectedRow() : null, inherited = tr("沿用原生配置", "Use native settings"), unverified = tr("原选配置待核对", "Saved configuration needs verification");
      renderStatus(statusCard, { agent: "Pi", model: selectedProfile === null ? inherited : !chosen ? unverified : !chosen.model_id ? inherited : chosen.model_id.includes("/native/") ? tr("已选原生模型 · ", "Selected native model · ") + chosen.name : chosen.model_id.slice("local/pi/".length), effort: selectedProfile === null || !chosen ? null : chosen.reasoning_effort === "none" ? tr("off（不指定推理等级）", "off (no reasoning level requested)") : chosen.reasoning_effort ?? null,
        memory: selectedMemory ? `${selectedMemory.name} · ${tr("只读", "Read only")}${memoryReady() ? "" : tr("（待核对）", " (needs verification)")}` : tr("不附加共享记忆", "No added shared memory"),
        status, observed: readable ? snapshot?.native_state ? `${snapshot.native_state.model?.provider || tr("来源未返回", "Provider unavailable")} / ${snapshot.native_state.model?.id || tr("模型未返回", "Model unavailable")} · ${snapshot.native_state.thinkingLevel || tr("等级未返回", "Thinking level unavailable")}${snapshot.native_state.thinkingLevel === "off" ? tr("（不指定推理等级）", " (no reasoning level requested)") : ""}` : "" : "", language });
    }
    function render() {
      syncConfigurationSelection();
      renderProfileStatus();
      moreSummary.textContent = tr("更多", "More"); operationSummary.textContent = tr("操作记录", "Operation records");
      const scope = encode([workspace, snapshot?.epoch ?? null, snapshot?.binding?.session_id ?? null]);
      if (scope !== displayScope) { displayScope = scope; messages.replaceChildren(); historyTools.replaceChildren(); originalsRoot.replaceChildren(); toolNodes.clear(); rawEvents.open = false; moreMenu.open = operationRecords.open = false; }
      handoffView?.setScope(encode([workspace, chatId, scope]));
      nativeMaterials?.setScope(encode([workspace, chatId]));
      root.setAttribute("lang", language); settingsPanel?.setAttribute("lang", language); for (const b of [...page.querySelectorAll("button"), ...(settingsPanel?.querySelectorAll("button") || [])]) { const text = b.getAttribute(language === "en" ? "data-pi-en" : "data-pi-zh"); if (text) b.textContent = text; }
      input.placeholder = tr("给 Pi 发消息…", "Message Pi…"); connection.textContent = readable ? `Pi · ${snapshot.connection.state}` : tr("Pi状态待核对", "Pi status unconfirmed");
      profileTitle.textContent = tr("AI 配置", "AI configuration"); profileSelect.setAttribute("aria-label", tr("原生 Pi AI 配置", "Native Pi AI configuration"));
      profileSelect.replaceChildren(); const inherit = node("option", "", tr("沿用原生当前设置", "Use current native settings")); inherit.value = ""; profileSelect.append(inherit);
      const chosen = selectedRow(), stale = selectedProfile !== null && !profileReady(), staleValue = selectedProfile && "saved:" + selectedProfile.id + ":" + selectedProfile.revision;
      if (stale) { const saved = node("option", "", tr("原选配置", "Saved configuration") + " · " + tr("第 ", "v") + selectedProfile.revision + tr(" 版（待重新选择）", " (review required)")); saved.value = staleValue; saved.disabled = true; profileSelect.append(saved); }
      for (const row of profileRows) { const option = node("option", "", `${row.name} · ${tr("第 ", "v")}${row.revision}${tr(" 版", "")}${row.available ? "" : " · " + (row.unavailable_reason || tr("不可用", "Unavailable"))}`); option.value = row.id; option.disabled = !profilesReadable || !row.available; profileSelect.append(option); }
      profileLabel.hidden = selectionControlled();
      profileSelect.value = stale ? staleValue : selectedProfile?.id || ""; profileSelect.disabled = selectionControlled() || !enabled || destroyed || writing || openingForSend || !!pending || storageInvalid;
      profilesRefresh.disabled = !ready() || writing || profilesReading; if (profilesManage) profilesManage.disabled = writing;
      const effort = chosen?.reasoning_effort, selectedInfo = selectedProfile === null ? tr("未指定的模型和思考等级沿用原生当前设置。", "Unspecified model and thinking level inherit current native settings.") : chosen ? `${chosen.name} · ${tr("第 ", "v")}${chosen.revision}${tr(" 版", "")} · ${tr("思考等级：", "Thinking: ")}${effort === "none" ? tr("Pi off（不指定推理等级）", "Pi off (no reasoning level requested)") : effort ?? tr("沿用当前", "keep current")}` : "";
      profileInfo.textContent = [configurationSelection?.message(language), selectionUnsupported ? tr("配置状态组件未就绪；请刷新页面，原草稿保留。", "Configuration state is unavailable. Refresh the page; the draft remains.") : "", selectedInfo, stale ? tr("原选配置已变化、不可用或尚未核实；原稿保留。", "The saved configuration changed, is unavailable, or is unverified; the draft remains.") : "", profilesError, profilesReading ? tr("正在读取配置…", "Reading configurations…") : ""].filter(Boolean).join(" ");
      memoryTitle.textContent = tr("共享记忆", "Shared memory"); memorySelect.setAttribute("aria-label", tr("原生 Pi 共享记忆", "Native Pi shared memory"));
      memorySelect.replaceChildren(); const none = node("option", "", tr("不附加小白兔共享记忆", "Do not add Rabbit shared memory")); none.value = ""; memorySelect.append(none);
      const memoryStale = selectedMemory !== null && !memoryReady(), savedMemory = selectedMemory && "saved:" + selectedMemory.id + ":" + selectedMemory.revision;
      if (memoryStale) { const option = node("option", "", selectedMemory.name + tr(" · 原选第 ", " · saved v") + selectedMemory.revision + tr(" 版（待重新选择）", " (review required)")); option.value = savedMemory; option.disabled = true; memorySelect.append(option); }
      for (const row of memoryRows) { const option = node("option", "", row.name + tr(" · 第 ", " · v") + row.revision + tr(" 版", "") + (row.mode === "read_only" ? tr(" · 只读", " · read only") : tr(" · 本入口不支持读写", " · read-write unavailable here"))); option.value = row.id; option.disabled = !memoryReadable || row.mode !== "read_only"; memorySelect.append(option); }
      memorySelect.value = memoryStale ? savedMemory : selectedMemory?.id || ""; memorySelect.disabled = memoryBlocked();
      memoryRefresh.disabled = !enabled || destroyed || writing || memoryReading;
      memoryInfo.textContent = [tr("Pi 原生上文与扩展照常；这里只为下一次发送附加只读资料，不保存本轮交互。不附加不会清除原生上文。", "Pi native context and extensions stay unchanged. Selected read-only context is added for the next send without saving this interaction. Choosing none does not erase earlier context."), memoryStale ? tr("原选版本已变化或尚未核实，请重新选择；原稿保留。", "The saved version changed or is unverified. Choose explicitly; the draft remains.") : "", memoryReading ? tr("正在读取保存的参数…", "Reading saved parameters…") : "", memoryError].filter(Boolean).join(" ");
      memoryLabel.hidden = memoryRefresh.hidden = memoryInfo.hidden = !Object.hasOwn(snapshot || {}, "memory_intents") && !selectedMemory && !pending?.memory_profile;
      const state = snapshot?.native_state, nativeModel = state?.model;
      currentInfo.textContent = state ? `${tr("Pi 当前：", "Current Pi: ")}${nativeModel?.provider || tr("来源未返回", "provider unavailable")} / ${nativeModel?.id || tr("模型未返回", "model unavailable")} · ${tr("原生思考等级：", "Native thinking: ")}${state.thinkingLevel || tr("未返回", "unavailable")}${state.thinkingLevel === "off" ? tr("（不指定推理等级，不代表厂商禁用思考）", " (no reasoning level requested; provider thinking is not guaranteed disabled)") : ""}` : "";
      openButton.hidden = ready(); openButton.disabled = !enabled || destroyed || !readable || writing || openingForSend; 
      sendButton.disabled = openingForSend || writing || !!pending || storageInvalid || !(sendStateReady() || canOpenForSend()) || !draft.trim() || draft.length > 200000;
      materialBox.hidden = !draftMaterials.length; materialBox.replaceChildren();
      if (draftMaterials.length) {
        materialBox.append(node("p", "rbs-note", tr("所选材料 · 发送前核对原始版本", "Selected materials · original versions checked before sending")));
        const blocked = writing || !!pending || storageInvalid || !enabled || destroyed, materialStamp = generation;
        for (const ref of draftMaterials) if (nativeMaterials) materialBox.append(nativeMaterials.card(ref, { language, disabled: blocked, remove: () => {
          if (materialStamp !== generation || writing || pending || storageInvalid || !enabled || destroyed) return;
          const previous = draftMaterials; draftMaterials = draftMaterials.filter(item => item.kind !== ref.kind || item.id !== ref.id);
          if (!save()) { draftMaterials = previous; errorText = tr("材料移除未保存；原引用保留。", "Removal was not saved; the original reference remains."); } render();
        } }));
        if (!materialsReady()) materialBox.append(node("p", "rbs-note", tr("图片需要当前原生模型明确声明支持图片，或选择已核实支持图片的配置；材料和草稿保留。", "Images require declared support from the current native model or a verified saved configuration. Materials and draft remain.")));
      }
      const stopping = !draft.trim() && active();
      sendButton.textContent = stopping ? tr("停止", "Stop") : tr("发送", "Send");
      sendButton.setAttribute("aria-label", stopping ? tr("停止本轮", "Stop current turn") : tr("发送", "Send"));
      sendButton.setAttribute("data-composer-action", stopping ? "stop" : "send");
      if (stopping) sendButton.disabled = !ready() || !uuid(activeClient()) || writing || openingForSend; disconnectButton.hidden = !snapshot || !["ready", "failed", "closed"].includes(snapshot.connection.state); disconnectButton.disabled = !enabled || destroyed || !readable || writing || openingForSend || !!pending || !!unsettled() || !!snapshot?.extension_requests.length || ready() && active();
      notices.replaceChildren(); if (openSendUnconfirmed) notices.append(node("p", "rbs-note", tr("接通结果待核对；请刷新状态或明确连接，原消息尚未发送。", "Connection is unconfirmed. Refresh or connect explicitly; the original message has not been sent."))); if (errorText) notices.append(node("p", "rbs-error", errorText)); if (receiptError) notices.append(node("p", "rbs-error", receiptError)); if (notice) notices.append(node("p", "rbs-note", notice)); if (pending) notices.append(node("p", "rbs-note", pending.status === "accepted" ? tr("原消息已接收，等待原生完成；不会重复发送。", "Accepted; waiting for native completion. No resend.") : posting ? tr("正在发送；原消息与配置已保存。", "Sending; original message and configuration saved.") : tr("上次发送结果未知；保留原编号，刷新只读核对，不会自动重发。", "Last send is unknown. Refresh checks the original ID without resending.")));
      if (snapshot?.events.some(event => event.display_truncated === true)) notices.append(node("p", "rbs-note", tr("部分Pi实时事件超出显示容量；原字节数与指纹见事件，不能视为完整过程。", "Some Pi live events exceed display limits. Byte counts and hashes remain; this is not the complete process.")));
      if (memorySourcesError) notices.append(node("p", "rbs-note", memorySourcesError));
      renderMessages(); renderMemorySources(); renderTools(); renderRequests(); operationRecords.hidden = historyTools.children.length === 0 && tools.children.length === 0; eventSummary.textContent = tr("Pi原始文字与事件", "Pi original text and events"); eventPre.textContent = JSON.stringify({ native_messages: snapshot?.native_messages || [], events: snapshot?.events || [] }, null, 2);
      textPicker?.update(language);
    }
    input.addEventListener("input", () => { composerVersion++; draft = input.value; save(); render(); });
    const visibility = () => { if (document.hidden) { composerVersion++; clearTimeout(timer); timer = null; save(); } else if (visible) refresh(); }; document.addEventListener?.("visibilitychange", visibility);
    function getTextSnapshot() {
      if (!ready() || !workspace || workspace !== String(getWorkspace() || "") || snapshot.workspace_id !== workspace || snapshot.chat_id !== chatId) throw Error("Pi原生文字快照尚未就绪或所属工作区已变化。");
      const native = snapshot?.binding?.session_id;
      if (typeof native !== "string" || !native || /[\x00-\x1f\x7f\ud800-\udfff]/u.test(native)) throw Error("Pi原生会话身份尚未核实。");
      const native_identity = encode(["pi", workspace, chatId, native]);
      if (!historyView?.getTextRecords) throw Error("原生历史文字组件尚未就绪，请重新打开对话。");
      const value = historyView.getTextRecords(snapshot);
      return { native_identity, records: value.records, incomplete: value.incomplete || active() };
    }
    async function show() { if (destroyed) return; visible = true; root.hidden = false; syncWorkspace(); render(); await refresh(); if (ready()) await loadInitialProfiles(); await loadMemoryProfiles(); }
    async function showSettings() { if (destroyed) return; composerVersion++; visible = true; root.hidden = true; syncWorkspace(); render(); await refresh(); if (ready()) await loadInitialProfiles(); await loadMemoryProfiles(); }
    function hide() { composerVersion++; visible = false; root.hidden = true; clearTimeout(timer); timer = null; save(); }
    function destroy() { hide(); destroyed = true; settingsPanel?.remove(); generation++; readPending = false; freshSend = null; historyView?.destroy(); document.removeEventListener?.("visibilitychange", visibility); }
    function setLanguage(value) { language = value === "en" ? "en" : "zh-CN"; render(); }
    function setEnabled(value) { enabled = value === true; if (!enabled) { freshSend = null; hide(); } render(); }
    function setMaterialInputs(value) { const next = value === true; if (next !== materialsEnabled) composerVersion++; materialsEnabled = next; render(); }
    function getConfigurationContext() {
      if (destroyed || !workspace || workspace !== String(getWorkspace() || "")) return null;
      const chosen = selectedRow();
      return { agent_instance_id: "local/pi", profile_id: selectedProfile?.id ?? null, profile_name: chosen?.name ?? null,
        profile_revision: selectedProfile?.revision ?? null, model_id: chosen?.model_id ?? null, reasoning_effort: chosen?.reasoning_effort ?? null };
    }
    render(); return { show, showSettings, hide, destroy, refresh, setLanguage, setEnabled, setMaterialInputs, getConfigurationContext, selectSavedProfile, putDraft, getTextSnapshot };
  }
  window.RabbitPiNative = Object.freeze({ create });
})();
