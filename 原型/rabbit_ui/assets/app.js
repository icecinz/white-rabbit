(() => {
  "use strict";

  const agents = window.RabbitAgents;
  const $ = (id) => document.getElementById(id);
  let bundledHelp, myAI = null;
  function readBundledHelp() {
    if (bundledHelp !== undefined) return bundledHelp;
    try {
      const source = $("rabbit-help-data"), value = JSON.parse(source?.content?.textContent || source?.textContent || "");
      bundledHelp = value && typeof value.manual === "string" && typeof value.guide === "string" ? value : null;
    } catch (_) { bundledHelp = null; }
    return bundledHelp;
  }
  const ui = {
    sidebar: $("sidebar"), sidebarContent: $("sidebar-content"), backdrop: $("sidebar-backdrop"), list: $("session-list"),
    search: $("search-input"), title: $("conversation-title"), eyebrow: $("heading-eyebrow"),
    meta: $("conversation-meta"), actions: $("header-actions"), messages: $("messages"),
    scroll: $("conversation-scroll"), composer: $("composer-region"), input: $("message-input"),
    send: $("send-button"), sendLabel: $("send-label"), notice: $("run-notice"),
    hint: $("composer-hint"), footnote: $("composer-footnote"),
    contextPanel: $("context-preview"), contextButton: $("preview-context"),
  };
  const settingsMounts = { specialist: $("specialist-settings"), native: $("native-chat-settings") };
  const sidebarScroll = { conversations: 0, management: 0, settings: 0 };
  let sidebarView = "conversations";
  const state = {
    workspaceSections: { management: null, settings: "general" }, managementTarget: null,
    csrf: "", status: null, chats: [], listView: "library", page: "conversations", historyAgent: "all", includeInternal: false,
    queries: { chats: "", history: "" }, historyItems: [], historyTotal: 0, historyNext: null,
    historyLoading: false, hiddenInternal: 0, listVersion: 0,
    libraryItems: [], libraryWorkspace: "", libraryTotal: 0, libraryNext: null, libraryQuery: "", libraryError: "",
    preferences: { default_backend: "codex", send_shortcut: "enter", markdown: true, show_internal: false, chat_backends: {}, chat_models: {}, default_models: agents.emptyModels(), revision: "0" },
    management: null, backendChoosing: false, backendChoiceVersion: 0, preferenceNotice: "",
    selected: { type: "new", id: null }, chat: null, historySession: null, historyMessages: [], historyReady: false, historyReadOnly: false,
    historyMessageTotal: 0, historyMessageNext: null, historyMessagePrev: null, historyMessageStart: 0,
    historyMatch: null, historyPageLoading: false, run: null, chatMessages: [], exporting: false, rawMessages: false,
    backend: "codex", model_id: "", homeNativeChoice: null, homeConfiguration: null, homeNativeHandoff: null, homeConfigurationReceipt: null, homeTextCompatibility: false, nativeHomeSending: false, sending: false, creating: false, cancelling: false,
    continuations: [], currentContinuation: null,
    openVersion: 0, pollTimer: null, searchTimer: null, toastTimer: null,
    drafts: new Map(), draftAttachments: new Map(), taskIntents: new Map(), task_intent: { mode: "chat", artifact_ids: [] }, fileMaterialChecks: new Map(), fileMaterialJobs: new Map(), fileMaterialError: "", attachment_ids: [], materialUploads: new Map(), materialError: "", messageSignature: "", transientError: "",
    pendingSubmission: null, pendingSubmissions: new Map(), newCreationId: null, historyCreationIds: new Map(), historyBackendChoices: new Map(), historyModelChoices: new Map(), historyContinuationIds: new Map(), historyChoices: new Map(),
    contextVisible: false, contextData: null, contextState: "empty", contextError: "", contextVersion: 0,
    contextPinSaving: null, contextPinNotice: "", contextPinError: false, contextNavigationVersion: 0, chatContextSelection: null,
    libraryRevision: null, libraryWatchRevision: null, selectedRevision: null, remoteRunning: false, chatLoading: null, nativeWorkStatus: null,
    syncInFlight: false, syncRequested: false, syncAbort: null,
    browserNotice: "", browserInputTouched: false, browserTouchedDrafts: new Set(), browserPageTouched: false, browserQueryTouched: false, browserAgentFilterTouched: false, browserSaveTimer: null, browserRestoring: false, conversationScrollTop: 0,
    sourceUpdateVisible: false, sourceUpdateChatId: null, sourceUpdateState: "empty", sourceUpdateData: null, sourceUpdateNotice: "", sourceUpdateVersion: 0, sourceUpdateInputVersion: 0, sourceUpdateSaving: null,
    sourceImages: { visible: false, chatId: null, state: "empty", data: null, notice: "", version: 0, saving: null }, sourceImagePending: new Map(),
    profile_snapshot: null, profileChoices: new Map(), profileSelection: null, profileSelectionSync: null, profileSelectionIntent: false, profilesInventory: null, profilesRevision: null, profileNotice: "", profileLegacy: false, memoryChatCreation: null, memoryRecallError: false,
  };
  ui.input.placeholder = "在此输入";
  window.RabbitComposerShell?.mount({ root: $("composer-form"), status: $("conversation-config-status"),
    choices: document.querySelector(".profile-choice-row"), input: ui.input,
    label: document.querySelector('label[for="message-input"]'),
    materials: [$("image-input"), $("composer-materials"), $("text-file-input"), $("composer-files"), $("file-material-status"), $("material-upload-status"), $("image-model-status")],
    support: $("composer-more"), send: ui.send, hint: ui.hint });
  for (const control of [ui.send, $("new-chat"), $("refresh-history")]) control.dataset.controlInterface = "ai";
  let browserStore = null, browserRestoreSnapshot = null, profileCatalogReadback = 0;
  $("brand-agents").textContent = agents.ids.map(id => agents.labels[id]).join(" · ");
  const sidebarAgentLabel = id => id === "dsh" ? "DSH" : agents.labels[id] || id;
  for (const id of agents.ids) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "source-filter";
    item.dataset.agent = id;
    item.textContent = sidebarAgentLabel(id);
    item.setAttribute("aria-pressed", "false");
    $("source-filters").append(item);
  }
  const sourceFilters = () => [...document.querySelectorAll(".source-filter")].filter(n => n.dataset.agent === "all" || agents.has(n.dataset.agent));
  const contextScrollVersions = new WeakMap();
  let conversationGroups = null;
  const materials = window.RabbitMaterials ? window.RabbitMaterials.create({ api, root: $("material-viewer"), onError: error => toast(userError(error, "图片暂时无法显示，材料引用仍保留。")) }) : null;
  const projectPublish = window.RabbitProjectPublish ? window.RabbitProjectPublish.create({ api, root: $("project-publication-modal"), getWorkspace: () => state.status?.workspace_id || "", getView: fileMaterialView, getCapabilities: () => state.status?.project_publication || {}, refreshConnection: refreshStatus, onChange: renderProjectPublicationStatus, onError: error => toast(userError(error, "另存结果尚未确认，原成果和草稿保留。")) }) : null;
  const markdownChats = window.RabbitMarkdownChats ? window.RabbitMarkdownChats.create({ api, root: $("markdown-chats-modal"), getWorkspace: () => state.status?.workspace_id || "", getCapabilities: () => state.status?.markdown_chats || {}, onOpenChat: id => openChat(id).catch(error => toast(userError(error, "原聊天暂时无法打开，整理文件仍保留。"))), onOpenNative: value => nativeChats?.selectExisting(value).catch(error => toast(userError(error, "原生入口暂不可用，整理文件和原草稿仍保留。"))), onOpenHistory: value => { if (value.workspaceId === state.status?.workspace_id) return openHistory(value.agent, value.sessionId).catch(error => toast(userError(error, "原索引暂不可用，整理文档仍保留。"))); }, onUseMaterial: openMarkdownMaterial, canUseMaterial: () => !!nativeFileHandoff && nativeHandoffTargets().some(item => ["codex", "pi", "hermes", "claude", "dsh"].includes(item.agent)), onDirectory: data => { conversationGroups?.accept(data); if (conversationGroups?.selected()) loadHistoryList(true); else renderList(); }, onError: error => toast(userError(error, "文件夹尚未核对，原聊天和文件保留。")) }) : null;
  const fileTasks = window.RabbitFileTasks ? window.RabbitFileTasks.create({ api, root: $("file-task-modal"), getView: () => quoteDraft().stamp + ":" + ui.input.value + ":" + JSON.stringify(state.attachment_ids), onSelect: selectFileArtifact, canPublish: () => !!projectPublish?.enabled(), onPublish: openProjectPublication, appendPublicationStatus: (parent, chatId, item) => projectPublish?.appendStatus(parent, chatId, item), onError: error => toast(userError(error, "文件暂时无法读取，原回答仍保留。")) }) : null;
  const fileMaterials = window.RabbitFileMaterials ? window.RabbitFileMaterials.create({ api, root: $("file-material-modal"), getWorkspace: () => state.status?.workspace_id || "", getView: fileMaterialView, onSelect: selectFileMaterial, onChange: () => { renderFileMaterialStatus(); updateComposer(); }, onError: error => toast(userError(error, "文件输入暂时无法读取，原草稿仍保留。")) }) : null;
  const historyQuotes = window.RabbitHistoryQuotes ? window.RabbitHistoryQuotes.create({
    api, root: $("history-quotes-modal"), getDraft: quoteDraft, putDraft: putQuoteDraft,
    getImage: (id, original = false) => materials ? materials.image(id, original) : Promise.reject(Error("图片查看组件尚未就绪。")),
    getFile: file => file.origin ? fileTasks.read(file.origin.chat_id, file.item) : fileMaterials.read(file.item),
    importFile: (origin, item) => fileMaterials.fromArtifact(origin, item),
    includeInternal: () => state.includeInternal,
  }) : null;
  const profiles = window.RabbitProfiles ? window.RabbitProfiles.create({ api, root: $("profile-picker"), getWorkspace: () => state.status?.workspace_id || "", getSelection: profileView, onChoose: chooseProfile,
    getNativeAgents: () => nativeHandoffTargets().filter(item => item.enabled).map(item => ({ agent: item.agent, label: item.label })),
    onChooseNative: chooseHomeConfiguration,
    onLocalConfiguration: (parameters, view, retry) => state.page === "native-chats" ? nativeChats?.saveLocalConfiguration?.(parameters, view, retry) ?? false : saveHomeLocalConfiguration(parameters, view),
    onManage: (kind, named) => {
      showPage("management", kind === "memory" ? "memory-profiles" : "ai-profiles");
      if (named) try { profiles.editNamed(kind, named.id, named.revision); } catch (error) { toast(userError(error, "配置尚未打开，原编辑保留。")); }
    }, onAskSpecialist: askSpecialistForConfiguration, canAskSpecialist: specialistEnabled, onMemoryManual: openManual, onCatalog: acceptProfileCatalog, getInventory: () => state.management, getCapabilities: () => state.status?.profiles || {} }) : null;
  const activeRunStates = new Set(["queued", "running", "cancelling"]);
  const backendLabels = agents.labels;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function plain(value) {
    if (value == null) return "";
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }
  function button(text, className, callback) {
    const node = el("button", className, text);
    node.type = "button";
    node.addEventListener("click", callback);
    return node;
  }
  function dateValue(value) {
    if (value == null || value === "") return null;
    const numeric = typeof value === "number" || /^\d+(\.\d+)?$/.test(value);
    const date = new Date(numeric ? (Number(value) < 1e12 ? Number(value) * 1000 : Number(value)) : value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  function fullDate(value) {
    const date = dateValue(value);
    return date ? date.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "日期未记录";
  }
  function shortDate(value) {
    const date = dateValue(value);
    if (!date) return "";
    const now = new Date();
    const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const time = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    if (day === today) return `今天 ${time}`;
    if (today - day === 86400000) return `昨天 ${time}`;
    return date.toLocaleDateString("zh-CN", { year: date.getFullYear() === now.getFullYear() ? undefined : "numeric", month: "numeric", day: "numeric" });
  }
  function userError(error, fallback = "暂时没有完成，请稍后再试。") {
    const message = error && plain(error.message || error.error);
    if (error?.status === 400 && error.context_info?.can_send === false) {
      const copy = contextStatusCopy(error.context_info);
      if (copy.isFileBlock) return copy.warning;
    }
    if (error && error.status === 409) return message && /[\u4e00-\u9fff]/.test(message) && message.length < 220 ? message : "上一轮还在回答，请等它完成或先停止。";
    if (error && error.status === 413) return "这条消息太长，请分成几段发送。";
    if (error && error.status === 403) return "页面连接已更新，请刷新页面后再试。";
    if (error && error.status === 404) return "这段记录暂时找不到了，请重新打开会话列表。";
    if (message && /[\u4e00-\u9fff]/.test(message) && message.length < 220) return message;
    return fallback;
  }
  async function api(path, options = {}, canRetry = true) {
    const method = options.method || "GET";
    if (method !== "GET" && !state.csrf) await refreshStatus();
    const headers = { Accept: "application/json", ...options.headers };
    const requestOptions = { ...options };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-OneAI-CSRF"] = state.csrf;
    }
    const response = await fetch(path, { ...requestOptions, method, headers, credentials: "same-origin", cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 403 && method !== "GET" && canRetry) {
        await refreshStatus();
        return api(path, options, false);
      }
      const error = new Error(plain(data.error && (data.error.message || data.error) || data.message || "请求未完成"));
      error.status = response.status;
      if (data.draft) error.draft = data.draft;
      if (data.profile_selection) error.profile_selection = data.profile_selection;
      if (data.ai_profile) error.ai_profile = data.ai_profile;
      if (data.profile) error.profile = data.profile;
      if (data.memory_profile) error.memory_profile = data.memory_profile;
      if (data.context_info && typeof data.context_info === "object" && !Array.isArray(data.context_info)) error.context_info = data.context_info;
      if (typeof data.code === "string") error.code = data.code;
      if (path === "/api/management/settings" && response.status === 409 && data.code === "settings_version_conflict") error.current_settings = data.current_settings;
      if (data.accepted === false) error.accepted = false;
      throw error;
    }
    return data;
  }
  function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3800);
  }
  function toggleSidebar(open, moveFocus = true) {
    const mobile = window.matchMedia("(max-width: 720px)").matches;
    const focusInside = ui.sidebar.contains(document.activeElement);
    ui.sidebar.classList.toggle("is-open", open);
    ui.sidebar.inert = mobile && !open;
    ui.sidebar.setAttribute("aria-hidden", String(mobile && !open));
    document.querySelector(".main-panel").inert = mobile && open;
    ui.backdrop.hidden = !open || !mobile;
    $("open-sidebar").setAttribute("aria-expanded", String(open));
    if (moveFocus && open && mobile) $("close-sidebar").focus();
    else if (moveFocus && !open && mobile && focusInside) $("open-sidebar").focus();
  }
  function draftKey(selection = state.selected) {
    return selection.type === "history" ? `history:${selection.id}` : selection.type === "chat" ? selection.id : "__new__";
  }
  function profilesEnabled() { return state.status?.profiles?.enabled === true; }
  function profileDirectoryNeeded() {
    return state.page === "conversations" && !(state.selected.type === "history" && state.historyReadOnly)
      || state.page === "native-chats" && !nativeChats?.hasReadOnlySelection()
      || state.page === "management" && ["ai-profiles", "memory-profiles"].includes(state.workspaceSections.management);
  }
  function sourceCanContinue() { return state.selected.type !== "history" || state.historySession?.coverage?.continuable !== false; }
  function sourceCoverageNotice() { return state.historySession?.coverage?.notice || "这段来源的当前对话链不完整，可以阅读、搜索和引用；不能直接带入接续。"; }
  function historyChoice() {
    if (state.selected.type !== "history") return null;
    const remembered = state.historyChoices.get(state.selected.id);
    if (remembered) return remembered;
    const key = draftKey(), legacyDraft = draftHasContent(state.drafts.get(key) || "", state.draftAttachments.get(key) || [], state.taskIntents.get(key));
    const text = state.historyContinuationIds.has(state.selected.id) || state.historyCreationIds.has(state.selected.id)
      || state.historyBackendChoices.has(state.selected.id) || legacyDraft && !!state.profileChoices.get(key);
    return { mode: !text && window.RabbitNativeAdoption && nativeAdoptionEnabled(state.selected.agent) ? "native" : "text", memory_explicit: false };
  }
  function historyUsesNative() { return historyChoice()?.mode === "native"; }
  function homeNativeChoice() { return state.status?.demo_mode === true ? null : state.selected.type === "new" ? state.homeNativeChoice : null; }
  function nextHomeConfiguration(fresh = false) {
    const previous = fresh ? null : state.homeConfiguration;
    if (previous) {
      if (!Number.isSafeInteger(previous.revision + 1)) throw Error("这份首页配置的版本已不能继续增加，请保留草稿。");
      return { draft_id: previous.draft_id, revision: previous.revision + 1 };
    }
    const id = crypto.randomUUID?.();
    if (!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(id || "")) throw Error("浏览器不能保存稳定的草稿编号，原稿保留。");
    return { draft_id: id, revision: 1 };
  }
  function homeLocalSupported(parameters) {
    if (!parameters || Object.keys(parameters).length !== 2 || !Object.hasOwn(parameters, "model_id") || !Object.hasOwn(parameters, "reasoning_effort")
        || !agents.validModelId(parameters.model_id, "codex")) return false;
    const catalog = profiles?.catalog(), source = catalog?.workspace_id === state.status?.workspace_id && catalog?.native_profile_capabilities?.codex;
    if (!source?.enabled || !source.catalogue_available) return false;
    if (!parameters.model_id) return parameters.reasoning_effort === null;
    const model = source.models.find(row => row.id === parameters.model_id);
    return !!model && (parameters.reasoning_effort === null || model.explicit_effort_supported === true && model.supported_efforts.includes(parameters.reasoning_effort));
  }
  function homeLocalView() {
    const choice = homeNativeChoice();
    if (choice?.agent !== "codex" || !choice.profile) return null;
    const base = choice.local_base || choice.profile, latest = profiles?.get(base.id), identity = state.homeConfiguration;
    const catalog = profiles?.catalog(), source = catalog?.workspace_id === state.status?.workspace_id && catalog?.native_profile_capabilities?.codex;
    const available = !!identity && latest?.native_available === true && sameProfile(toProfileSnapshot(latest, { id: "none" }), toProfileSnapshot(base, { id: "none" }))
      && !!source?.enabled && !!source.catalogue_available;
    return { scope: "home", workspace_id: state.status?.workspace_id || "", draft_id: identity?.draft_id || null, chat_id: null, profile: choice.profile,
      available, pending: null, reason: available ? "" : "先选择一份已保存配置，并核对 Codex 模型目录" };
  }
  function saveHomeLocalConfiguration(parameters, view) {
    const choice = homeNativeChoice(), current = profileView();
    if (state.page !== "conversations" || !choice || current.view !== view || current.busy || !homeLocalView()?.available || !homeLocalSupported(parameters)) return false;
    if (!writeHomeConfiguration(parameters)) throw Error("浏览器不能保存首页配置，原参数与草稿保留。");
    return true;
  }

  function homeChoice(profile, memory, agent = agents.backendOfInstance(profile.agent_instance_id)) {
    return { agent, profile: profile && window.RabbitProfiles.profile(profile, false), memory: window.RabbitProfiles.memoryProfile(memory, false) };
  }
  async function chooseHomeConfiguration(agent, view, isCurrent, savedProfile = null, compatibility = false) {
    if (view !== profileView().view || state.page !== "conversations" || state.selected.type !== "new" || state.nativeHomeSending || !isCurrent()) return false;
    if (!compatibility && !nativeHandoffTargets().some(item => item.agent === agent && item.enabled)) return false;
    const profile = savedProfile && window.RabbitProfiles.profile(savedProfile, false);
    if (profile && profile.agent_instance_id !== "local/" + agent) return false;
    const previous = { identity: state.homeConfiguration, choice: state.homeNativeChoice, backend: state.backend, model: state.model_id, compatibility: state.homeTextCompatibility, snapshot: state.profile_snapshot, remembered: state.profileChoices.get("__new__"), hasRemembered: state.profileChoices.has("__new__") };
    const memory = draftMemoryProfile(), identity = nextHomeConfiguration();
    state.homeNativeChoice = compatibility ? null : homeChoice(profile, memory, agent); state.homeTextCompatibility = compatibility;
    state.homeConfiguration = identity;
    state.profile_snapshot = profile ? toProfileSnapshot(profile, memory) : null;
    state.backend = agent; state.model_id = profile?.model_id || ""; state.backendChoiceVersion++;
    if (saveDraft() !== true) {
      state.homeConfiguration = previous.identity; state.homeNativeChoice = previous.choice; state.backend = previous.backend; state.model_id = previous.model; state.homeTextCompatibility = previous.compatibility; state.profile_snapshot = previous.snapshot;
      if (previous.hasRemembered) state.profileChoices.set("__new__", previous.remembered); else state.profileChoices.delete("__new__");
      updateComposer(); throw Error("浏览器不能保存所选配置，原选择和草稿保留。");
    }
    invalidateContextPreview(); state.profileNotice = ""; updateComposer(); return true;
  }
  function draftMemoryProfile() {
    if (homeNativeChoice()) return homeNativeChoice().memory;
    const choice = historyChoice();
    return choice?.memory_explicit && Object.hasOwn(choice, "memory_profile") ? choice.memory_profile : state.profile_snapshot?.memory_profile || { id: "none" };
  }
  function profileSnapshot(value) { if (value == null) return null; if (!window.RabbitProfiles) throw new Error("配置快照暂时无法读取，请刷新页面。原草稿保留。"); return window.RabbitProfiles.snapshot(value); }
  function sameProfile(a, b) { if (a == null || b == null) return a == null && b == null; return window.RabbitProfiles?.sameSnapshot(a, b) === true; }
  function toProfileSnapshot(row, memory = state.profile_snapshot?.memory_profile || { id: "none" }) { return profileSnapshot({ schema_version: 2, ai_profile: row, memory_profile: memory, used_memory_references: [] }); }
  function profileBody(value = state.profile_snapshot) { const snap = profileSnapshot(value); return snap ? { ai_profile_id: snap.ai_profile.id, ai_profile_revision: snap.ai_profile.revision, memory_profile_id: snap.memory_profile.id, ...(snap.memory_profile.id === "none" ? {} : { memory_profile_revision: snap.memory_profile.revision }) } : {}; }
  function profileView() {
    if (state.page === "continuation") return continuations?.getComposerView?.() || { view: "continuation-unavailable", busy: true };
    const native = state.page === "native-chats" && nativeChats?.getComposerView?.();
    if (native) return native;
    const home = homeNativeChoice(), local = homeLocalView();
    return { ...(local ? { native_agent: "codex", local_configuration: local } : {}), ai_profile_id: home ? home.profile?.id || null : state.profile_snapshot?.ai_profile.id || null, memory_profile_id: draftMemoryProfile().id, existing: state.selected.type !== "new" || !!state.chatMessages.length, native_history: historyUsesNative() || !!home, view: `${state.page}:${state.openVersion}:${state.contextNavigationVersion}:${draftKey()}:${state.selected.type === "new" ? state.homeConfiguration?.revision || 0 : ""}`, busy: state.sending || state.creating || state.backendChoosing || state.nativeHomeSending, notice: state.profileNotice };
  }
  function migratedProfile(backend = state.backend, modelId = state.model_id) { return state.profilesInventory?.ai_profiles.find(row => row.agent_instance_id === agents.instanceId(backend) && row.model_id === modelId && row.reasoning_effort === null) || null; }
  function adoptProfile(value, { frozen = false, remember = false } = {}) {
    let snap = profileSnapshot(value);
    if (snap && !frozen && state.selected.type !== "new") {
      const latest = profiles?.get(snap.ai_profile.id), choice = historyChoice();
      const memory = choice?.memory_explicit && Object.hasOwn(choice, "memory_profile") ? choice.memory_profile
        : snap.memory_profile.id === "none" ? { id: "none" } : profiles?.getMemory(snap.memory_profile.id) || snap.memory_profile;
      snap = toProfileSnapshot(latest || snap.ai_profile, memory);
    }
    state.profile_snapshot = snap; state.profileLegacy = !snap;
    if (snap) { state.backend = agents.backendOfInstance(snap.ai_profile.agent_instance_id); state.model_id = snap.ai_profile.model_id; }
    if (snap && state.selected.type === "new" && !state.homeTextCompatibility) {
      const next = homeChoice(snap.ai_profile, snap.memory_profile);
      if (!state.homeConfiguration || JSON.stringify(state.homeNativeChoice) !== JSON.stringify(next)) state.homeConfiguration = nextHomeConfiguration();
      state.homeNativeChoice = next;
    }
    if (remember) state.profileChoices.set(draftKey(), snap);
  }
  const sameProfileSelection = (a, b) => !!a && !!b && a.revision === b.revision && a.ai_profile_id === b.ai_profile_id && a.memory_profile_id === b.memory_profile_id;
  function observeProfileSelection(value) {
    let next;
    try { next = window.RabbitProfiles.selection(value); }
    catch (_) { state.profileSelectionSync = "conflict"; state.profileSelectionIntent = false; return false; }
    const previous = state.profileSelection;
    if (previous && (next.revision < previous.revision || next.revision === previous.revision && !sameProfileSelection(previous, next))) {
      state.profileSelectionSync = "conflict"; state.profileSelectionIntent = false; return false;
    }
    if (!sameProfileSelection(previous, next)) {
      state.profileSelection = next; state.profileSelectionIntent = false;
      // Only an untouched legacy selection may use its old route as a fallback.
      state.profileSelectionSync = !previous && next.revision === 0 && next.ai_profile_id === null && next.memory_profile_id === "none" ? null : "pending";
    }
    return state.profileSelectionSync !== "conflict";
  }
  function commonProfileBusy() {
    return state.sending || state.creating || state.backendChoosing || state.cancelling || isRunning() || currentUploadCount() > 0 || state.contextState === "loading" || contextPinSavingCurrent()
      || state.pendingSubmissions.has(state.selected.id) || state.pendingSubmission?.chatId === state.selected.id;
  }
  function applyObservedProfileSelection() {
    if (state.selected.type !== "chat" || state.profileSelectionSync !== "pending" || commonProfileBusy()) return;
    if (!state.profilesInventory || state.profilesInventory.workspace_id !== state.status?.workspace_id) return;
    const choice = state.profileSelection, row = choice?.ai_profile_id && profiles?.get(choice.ai_profile_id);
    const memory = choice?.memory_profile_id === "none" ? { id: "none" } : profiles?.getMemory(choice?.memory_profile_id);
    if (!row || !memory) { state.profile_snapshot = null; state.profileLegacy = false; state.profileChoices.delete(draftKey()); return; }
    const previous = state.profile_snapshot;
    state.profileSelectionSync = null; state.profileSelectionIntent = false;
    adoptProfile(toProfileSnapshot(row, memory), { frozen: true, remember: true }); state.profileNotice = "";
    if (!sameProfile(previous, state.profile_snapshot)) invalidateContextPreview("另一入口已更换配置，请重新查看本轮上下文。草稿和材料保留。");
    checkpointBrowserState();
  }
  function acceptProfileCatalog(data, changed = null, kind = "ai") {
    if (!data) return;
    const previousIntent = state.profile_snapshot;
    state.profilesInventory = data; state.profilesRevision = data.revision;
    if (profileCatalogReadback && !changed) { profiles?.selectChanged(); return; }
    if (!homeNativeChoice() && !(state.selected.type === "chat" && (state.profileSelectionSync || commonProfileBusy()))) {
      if (state.profile_snapshot) adoptProfile(state.profile_snapshot);
      else if (!state.profileLegacy && !browserRestoreSnapshot) { const row = state.selected.type === "new" ? profiles?.get(data.default_ai_profile_id) : migratedProfile(); if (row) adoptProfile(toProfileSnapshot(row, { id: "none" })); }
    }
    if (!homeNativeChoice() && !(state.selected.type === "chat" && (state.profileSelectionSync || commonProfileBusy())) && changed && (kind === "ai" ? state.profile_snapshot?.ai_profile.id : state.profile_snapshot?.memory_profile.id) === changed.id) { adoptProfile(kind === "ai" ? toProfileSnapshot(changed) : toProfileSnapshot(state.profile_snapshot.ai_profile, changed), { remember: true }); invalidateContextPreview("配置已保存新版本，请重新查看本轮上下文。"); }
    if (!sameProfile(previousIntent, state.profile_snapshot)) invalidateContextPreview("所选配置已更新，之前的上下文预览已清除，请重新查看。草稿和图片保留。");
    if (homeNativeChoice()) { state.backend = homeNativeChoice().agent; state.model_id = homeNativeChoice().profile?.model_id || ""; }
    updateComposer(); profiles?.selectChanged();
  }
  function restoreProfileChoice() {
    const native = homeNativeChoice();
    if (native) { state.backend = native.agent; state.model_id = native.profile?.model_id || ""; state.profile_snapshot = native.profile ? toProfileSnapshot(native.profile, native.memory) : null; return; }
    if (!profilesEnabled()) return;
    if (state.selected.type === "chat" && state.profileSelectionSync) { applyObservedProfileSelection(); return; }
    const key = draftKey();
    if (state.profileChoices.get(key)) { adoptProfile(state.profileChoices.get(key)); return; }
    const chosen = state.profileSelection?.ai_profile_id ? profiles?.get(state.profileSelection.ai_profile_id) : null;
    const row = chosen || (state.selected.type === "new" ? profiles?.get(state.profilesInventory?.default_ai_profile_id) : migratedProfile());
    const memoryId = state.profileSelection?.memory_profile_id || "none", memory = memoryId === "none" ? { id: "none" } : profiles?.getMemory(memoryId);
    if (row && memory) adoptProfile(toProfileSnapshot(row, memory));
    else { state.profile_snapshot = null; state.profileLegacy = false; }
  }
  async function chooseProfile(kind, row, view, { newChat: openNew = false, compatibility = false } = {}) {
    if (state.page === "continuation") return continuations?.chooseConfiguration?.(kind, row, view) ?? false;
    if (state.page === "native-chats") {
      const current = nativeChats?.getComposerView?.(), target = agents.backendOfInstance(row?.agent_instance_id || "");
      if (kind === "ai" && ["codex", "hermes"].includes(current?.native_agent) && ["codex", "hermes"].includes(target) && target !== current.native_agent) {
        if (current.busy || current.view !== view || row?.native_available !== true) return false;
        if (resumeNativeContinuation(nativeChats.getQuoteContext(), row)) return true;
        return openNativeTextHandoff(await nativeChats.readTextSnapshot(), row);
      }
      return nativeChats?.chooseConfiguration?.(kind, row, view) ?? false;
    }
    if (view !== profileView().view || state.sending || state.creating || state.backendChoosing || state.nativeHomeSending) return false;
    const selected = kind === "memory" ? window.RabbitProfiles.memoryProfile(row === "none" ? { id: "none" } : row) : window.RabbitProfiles.profile(row);
    if (kind === "ai" && state.selected.type === "new") {
      return chooseHomeConfiguration(agents.backendOfInstance(selected.agent_instance_id), view, () => view === profileView().view, selected, compatibility);
    }
    if (kind === "memory" && homeNativeChoice()) {
      const memory = window.RabbitProfiles.memoryProfile(selected, false);
      if (memory.id !== "none") window.RabbitNativeMemory.choice({ ...memory, locked_mode: row.locked_mode === true, source_kind: row.source_kind || "owned" }, { readOnly: true });
      const previous = state.homeNativeChoice, previousIdentity = state.homeConfiguration;
      state.homeConfiguration = nextHomeConfiguration();
      state.homeNativeChoice = { ...previous, memory, ...(previous.local_base ? { profile: { ...previous.profile, revision: state.homeConfiguration.revision } } : {}) }; state.backendChoiceVersion++;
      state.profile_snapshot = state.homeNativeChoice.profile ? toProfileSnapshot(state.homeNativeChoice.profile, memory) : null;
      if (saveDraft() !== true) { state.homeConfiguration = previousIdentity; state.homeNativeChoice = previous; state.profile_snapshot = previous.profile ? toProfileSnapshot(previous.profile, previous.memory) : null; state.profileChoices.set("__new__", state.profile_snapshot); updateComposer(); throw Error("浏览器不能保存记忆选择，原选择和草稿保留。"); }
      invalidateContextPreview(); updateComposer(); return true;
    }
    if (kind === "memory" && historyUsesNative()) {
      if (openNew) throw Error("原生历史请在当前会话选择只读记忆；原稿保留。");
      const memory = window.RabbitProfiles.memoryProfile(selected, false);
      if (memory.id !== "none") {
        if (!window.RabbitNativeMemory) throw Error("共享记忆组件尚未就绪，原选择和草稿保留。");
        window.RabbitNativeMemory.choice({ ...memory, locked_mode: row.locked_mode === true, source_kind: row.source_kind || "owned" }, { readOnly: true });
      }
      const id = state.selected.id, previous = historyChoice();
      state.historyChoices.set(id, { ...previous, memory_explicit: true, memory_profile: memory }); state.backendChoiceVersion++;
      if (saveDraft() !== true) { state.historyChoices.set(id, previous); updateComposer(); throw Error("浏览器不能保存记忆选择；原选择和草稿保留。"); }
      invalidateContextPreview("记忆选择已更换，原生发送前会核对最新来源。"); updateComposer(); return true;
    }
    if (kind === "memory" && !state.profile_snapshot) throw new Error("先明确选择 AI 配置，再选择记忆。原草稿保留。");
    const desired = kind === "memory" ? toProfileSnapshot(state.profile_snapshot.ai_profile, selected) : toProfileSnapshot(selected, draftMemoryProfile());
    if (openNew && kind === "memory") return createMemoryChat(desired, view);
    const version = state.openVersion, key = draftKey(), choiceVersion = ++state.backendChoiceVersion;
    state.backendChoosing = true; updateComposer();
    try {
      if (state.selected.type === "chat" && !isRunning()) {
        const chatId = state.selected.id;
        const current = state.profileSelection || window.RabbitProfiles.selection((await api(`/api/chats/${encodeURIComponent(chatId)}/profile-selection`)).profile_selection);
        const data = await api(`/api/chats/${encodeURIComponent(chatId)}/profile-selection`, { method: "PUT", body: JSON.stringify({ revision: current.revision, [kind === "memory" ? "memory_profile_id" : "ai_profile_id"]: selected.id }) }, false);
        if (version !== state.openVersion || key !== draftKey() || choiceVersion !== state.backendChoiceVersion) return false;
        if (!observeProfileSelection(data.profile_selection)) throw Error("聊天选择的版本待核对，原草稿保留。请重新读取对话后再选择。");
        if (state.profileSelection.ai_profile_id !== desired.ai_profile.id || state.profileSelection.memory_profile_id !== desired.memory_profile.id) throw Error("聊天选择已在另一入口更新，原草稿保留。");
      }
      if (version !== state.openVersion || key !== draftKey() || choiceVersion !== state.backendChoiceVersion) return false;
      state.profileSelectionSync = null; adoptProfile(desired, { frozen: true, remember: true }); state.profileNotice = ""; state.memoryRecallError = false;
      if (state.selected.type === "history") {
        const previous = historyChoice(), explicit = kind === "memory" || previous.memory_explicit;
        state.historyChoices.set(state.selected.id, { ...previous, mode: kind === "ai" ? "text" : previous.mode, memory_explicit: explicit, ...(explicit ? { memory_profile: desired.memory_profile } : {}) });
      }
      state.profileSelectionIntent = true;
      invalidateContextPreview("配置已更换，请重新查看本轮上下文。"); invalidateSourceUpdate("配置已更换，请重新查看来源更新。"); invalidateSourceImages("配置已更换，请重新查看来源图片。"); saveDraft(); renderRunNotice(); return true;
    } catch (error) {
      if (version === state.openVersion && key === draftKey()) { if (error.profile_selection) observeProfileSelection(error.profile_selection); state.profileNotice = userError(error, "配置暂时没有完成选择，原配置和草稿保留。"); }
      throw error;
    } finally { state.backendChoosing = false; updateComposer(); }
  }
  async function prepareProfileSelection(chatId, snap, expected = state.profileSelection) {
    if (!snap) return null;
    const data = await api(`/api/chats/${encodeURIComponent(chatId)}/profile-selection`), current = window.RabbitProfiles.selection(data.profile_selection);
    const sameChoice = (a, b) => a.ai_profile_id === b.ai_profile_id && a.memory_profile_id === b.memory_profile_id;
    const desired = { ai_profile_id: snap.ai_profile.id, memory_profile_id: snap.memory_profile.id };
    if (current.revision === 0 && current.ai_profile_id === null && current.memory_profile_id === "none" || sameChoice(current, desired) && (!expected || expected.revision === current.revision && sameChoice(expected, current))) { if (state.selected.type === "chat" && state.selected.id === chatId && !observeProfileSelection(current)) throw Error("聊天选择的版本待核对，原草稿保留。"); return current.revision; }
    if (!expected || !sameChoice(expected, current) || expected.revision !== current.revision) {
      if (state.selected.type === "chat" && state.selected.id === chatId) observeProfileSelection(current);
      throw new Error("共同聊天已有另一入口选择的 AI 或记忆配置。请明确重新选择；原草稿和图片保留，没有覆盖另一份选择。");
    }
    const changed = await api(`/api/chats/${encodeURIComponent(chatId)}/profile-selection`, { method: "PUT", body: JSON.stringify({ revision: current.revision, ...desired }) }, false);
    const received = window.RabbitProfiles.selection(changed.profile_selection);
    if (received.revision !== current.revision + 1 || !sameChoice(received, desired)) { if (state.selected.type === "chat" && state.selected.id === chatId) { state.profileSelectionSync = "conflict"; state.profileSelectionIntent = false; } throw Error("聊天选择回执不一致，原草稿保留。请重新读取对话。"); }
    if (state.selected.type === "chat" && state.selected.id === chatId && !observeProfileSelection(received)) throw Error("聊天选择的版本待核对，原草稿保留。");
    return received.revision;
  }
  function profileReady() {
    const native = homeNativeChoice();
    if (native) {
      const base = native.local_base || native.profile, row = base && profiles?.get(base.id), memory = native.memory.id === "none" ? { available: true } : profiles?.getMemory(native.memory.id);
      return nativeHandoffTargets().some(item => item.agent === native.agent && item.enabled)
        && (!base || row?.native_available === true && row.revision === base.revision && sameProfile(toProfileSnapshot(row, { id: "none" }), toProfileSnapshot(base, { id: "none" })))
        && (!native.local_base || native.agent === "codex" && native.profile.id === state.homeConfiguration?.draft_id && native.profile.revision === state.homeConfiguration.revision
          && homeLocalSupported({ model_id: native.profile.model_id, reasoning_effort: native.profile.reasoning_effort }))
        && !!memory?.available && (native.memory.id === "none" || memory.revision === native.memory.revision);
    }
    if (!profilesEnabled()) return state.profile_snapshot == null;
    if (state.selected.type === "chat" && state.profileSelectionSync) return false;
    if (!state.profile_snapshot) return state.profileLegacy;
    const selected = profiles?.get(state.profile_snapshot.ai_profile.id);
    const memory = state.profile_snapshot.memory_profile.id === "none" ? { available: true } : profiles?.getMemory(state.profile_snapshot.memory_profile.id);
    return !!(selected?.available && selected.revision === state.profile_snapshot.ai_profile.revision && memory?.available && (state.profile_snapshot.memory_profile.id === "none" || memory.revision === state.profile_snapshot.memory_profile.revision));
  }
  function memoryRecallRejected(error) { return error?.code === "memory_recall_failed" && error.accepted === false; }
  async function recoverMemoryChatCreation() {
    const pending = state.memoryChatCreation; if (!pending || pending.chatId) return pending;
    const receipt = await requestReceipt("creation", pending.clientId);
    if (state.memoryChatCreation !== pending) return null;
    if (receipt?.received && typeof receipt.chat_id === "string" && receipt.chat_id && Array.from(receipt.chat_id).length <= 256) { pending.chatId = receipt.chat_id; checkpointBrowserState(); }
    return pending;
  }
  async function createMemoryChat(desired, view) {
    if (view !== profileView().view || state.sending || state.creating || state.backendChoosing) return false;
    const oldPending = state.memoryChatCreation;
    if (oldPending && !sameProfile(oldPending.profile_snapshot, desired)) throw new Error("另一份新对话创建还待确认。原草稿仍保留，请先检查那次创建结果；没有重复创建。");
    saveDraft(); state.creating = true; updateComposer();
    const pending = oldPending || { clientId: requestId(), chatId: null, profile_snapshot: profileSnapshot(desired) }; state.memoryChatCreation = pending; checkpointBrowserState();
    try {
      if (!oldPending) {
        try { const created = await api("/api/chats", { method: "POST", body: JSON.stringify({ client_id: pending.clientId }) }, false); if (typeof created.chat?.id !== "string" || !created.chat.id) throw new Error("新对话创建结果尚未确认。"); pending.chatId = created.chat.id; checkpointBrowserState(); }
        catch (error) { if (error.status && error.status < 500) { if (state.memoryChatCreation === pending) state.memoryChatCreation = null; throw error; } await recoverMemoryChatCreation(); }
      } else await recoverMemoryChatCreation();
      if (!pending.chatId) throw new Error("新对话创建结果尚未确认。旧草稿原位保留，再次检查只查询原创建回执，没有重复创建。");
      const path = `/api/chats/${encodeURIComponent(pending.chatId)}/profile-selection`, read = await api(path), current = window.RabbitProfiles.selection(read.profile_selection), wanted = { ai_profile_id: pending.profile_snapshot.ai_profile.id, memory_profile_id: pending.profile_snapshot.memory_profile.id };
      if (current.ai_profile_id !== wanted.ai_profile_id || current.memory_profile_id !== wanted.memory_profile_id) {
        if (current.revision !== 0 || current.ai_profile_id !== null || current.memory_profile_id !== "none") throw new Error("新对话的配置已在别处更改。旧稿和创建标识保留，没有覆盖另一份选择。");
        await api(path, { method: "PUT", body: JSON.stringify({ revision: current.revision, ...wanted }) }, false);
      }
      if (state.memoryChatCreation === pending) state.memoryChatCreation = null; checkpointBrowserState(); refreshChats().catch(() => {});
      if (view !== profileView().view) { toast("新空对话已建立。旧草稿原位保留；你当前打开的页面没有改变。"); return false; }
      state.profileChoices.set(pending.chatId, pending.profile_snapshot);
      state.creating = false; profiles?.close(false);
      const openingChoiceVersion = state.backendChoiceVersion;
      await openChat(pending.chatId, true);
      if (state.selected.type === "chat" && state.selected.id === pending.chatId && state.page === "conversations") { if (openingChoiceVersion === state.backendChoiceVersion) { adoptProfile(pending.profile_snapshot, { frozen: true, remember: true }); state.profileSelectionIntent = true; state.memoryRecallError = false; } saveDraft(); updateComposer(); ui.input.focus(); toast("已新开空白对话。原话题的草稿、图片和引用仍留在原处。"); }
      return true;
    } catch (error) { checkpointBrowserState(); throw error; }
    finally { state.creating = false; updateComposer(); renderRunNotice(); }
  }
  function taskIntent(value) { return window.RabbitFileTasks.intent(value); }
  function sameTaskIntent(a, b) { return window.RabbitFileTasks.same(a, b); }
  function taskBody(value) { return window.RabbitFileTasks.body(value); }
  function setTaskIntent(value, edited = true) {
    state.task_intent = taskIntent(value);
    if (edited) { ++state.sourceUpdateInputVersion; state.browserInputTouched = true; state.browserTouchedDrafts.add(draftKey()); invalidateContextPreview("文件材料或生成选择已修改，请重新查看本轮上下文。"); saveDraft(); }
    renderComposerFiles(); updateComposer();
  }
  function availableFileArtifacts() {
    const result = new Map();
    if (state.selected.type !== "chat") return result;
    for (const message of state.chatMessages) for (const raw of message.file_task?.artifacts || []) { try { const item = window.RabbitFileTasks.artifact(raw); result.set(item.id, item); } catch (_) {} }
    for (const message of state.chatMessages) for (const raw of message.file_inputs || []) if (raw?.source === "generated") { try { const item = window.RabbitFileTasks.artifact(raw); if (!result.has(item.id)) result.set(item.id, item); } catch (_) {} }
    return result;
  }
  async function selectFileArtifact(chatId, item) {
    if (state.selected.type !== "chat" || state.selected.id !== chatId || state.page !== "conversations" || state.sending || state.creating || currentUploadCount()) throw new Error("请在成果所属的聊天里选择，原草稿已保留。");
    if (!fileInputsSupported()) throw new Error("当前入口暂不能添加文件材料，原草稿与文件选择保留。");
    if (state.task_intent.mode === "files" && state.attachment_ids.length) throw new Error("当前文件任务只支持文字。图片仍保留，请先关闭生成或移除图片再选择文件成果。");
    item = window.RabbitFileTasks.artifact(item);
    const known = availableFileArtifacts().get(item.id);
    if (!known?.available || !known.content_verified || item.available === false || item.content_verified === false) throw new Error("文件暂不可读取，保存记录与草稿仍保留。");
    if (!known || JSON.stringify(known) !== JSON.stringify(item)) throw new Error("这份成果不在当前聊天的保存记录里，请重新打开聊天。");
    const ids = taskIntent(state.task_intent).artifact_ids;
    if (ids.includes(item.id)) { toast("这份成果已选入本轮草稿。"); return; }
    if (ids.length >= 4) throw new Error("一轮最多选择四份成果，已有选择仍保留。");
    setTaskIntent({ mode: state.task_intent.mode, artifact_ids: [...ids, item.id] }); ui.input.focus(); toast("成果已放入本轮草稿。写下要求后再发送。");
  }
  function fileMaterialView() { const current = quoteDraft(); return current.stamp + ":" + current.text + ":" + JSON.stringify(current.attachment_ids); }
  function openProjectPublication(chatId, item) { if (state.page !== "conversations" || state.selected.type !== "chat" || state.selected.id !== chatId) throw new Error("请在成果所属的聊天里明确另存。原成果仍保留。"); return projectPublish?.open(chatId, item); }
  function renderProjectPublicationStatus() { if (state.page === "conversations" && state.selected.type === "chat" && state.chat?.id === state.selected.id) renderMessages(state.chatMessages, { appendNotice: sourceNotice(state.chat), preserveScroll: true }); }
  function currentFileMaterialJobs() { return (fileMaterials?.state().recovering ? 1 : 0) + [...state.fileMaterialJobs.values()].filter(item => item.version === state.openVersion && item.key === draftKey()).length; }
  function sourceFile(id) { return fileMaterials?.known.get(id) || null; }
  function allWorkspaceSourceFiles(task = state.task_intent) { return taskIntent(task).artifact_ids.every(id => { const item = sourceFile(id); return item && ["upload", "project_preview"].includes(item.source) && item.available && item.content_verified; }); }
  function fileInputsUnready() {
    const generated = availableFileArtifacts();
    return state.task_intent.artifact_ids.some(id => { const item = generated.get(id) || sourceFile(id); return !item?.available || !item.content_verified; });
  }
  function hydrateFileSources() {
    if (!fileMaterials || !state.status?.workspace_id) return;
    const generated = availableFileArtifacts();
    for (const id of state.task_intent.artifact_ids) {
      if (generated.has(id) || sourceFile(id) || state.fileMaterialChecks.has(id)) continue;
      state.fileMaterialChecks.set(id, "loading");
      fileMaterials.inspect(id).then(() => { state.fileMaterialChecks.set(id, "ready"); }).catch(() => { state.fileMaterialChecks.set(id, "unavailable"); }).finally(() => { renderComposerFiles(); updateComposer(); });
    }
  }
  async function selectFileMaterial(raw, view = fileMaterialView()) {
    if (!fileMaterials || !fileInputsSupported() || view !== fileMaterialView() || !quoteDraft().active || quoteDraft().busy || state.task_intent.mode === "files" && state.attachment_ids.length) throw new Error("话题、连接、草稿或材料已变化，文件输入已保留。请从‘已添加的文件’明确放入；未发送。");
    const item = fileMaterials.accept(raw);
    if (!item.available || !item.content_verified) throw new Error("文件输入暂不可读取，原草稿与引用仍保留。");
    const ids = taskIntent(state.task_intent).artifact_ids;
    if (ids.includes(item.id)) { toast("这份文件输入已在本轮草稿中。尚未发送。"); return true; }
    if (ids.length >= 4) throw new Error("一轮最多选择四份文件输入或成果，已有选择仍保留。");
    setTaskIntent({ mode: state.task_intent.mode, artifact_ids: [...ids, item.id] }); toast("文件输入已放入本轮草稿。写下要求后再发送。"); return true;
  }
  function renderFileMaterialStatus() {
    const root = $("file-material-status"), jobs = currentFileMaterialJobs(), data = fileMaterials?.state() || { pending: [], notice: "" };
    root.hidden = !jobs && !data.pending.length && !data.notice && !state.fileMaterialError; root.replaceChildren();
    if (root.hidden) return;
    root.append(el("span", "", jobs ? "正在保留文本文件输入，完成后再发送…" : state.fileMaterialError || data.notice || "上次文件上传结果待确认，原草稿保留。"));
    if (data.pending.length && !jobs) { const check = button("确认原上传回执", "file-task-action", async () => { check.disabled = true; try { await fileMaterials.recover(); } finally { check.disabled = false; renderFileMaterialStatus(); } }); root.append(check); }
    if (!jobs) root.append(button("已添加的文件", "file-task-action", () => fileMaterials?.list()));
  }
  async function uploadTextFile(files) {
    const chosen = Array.from(files || []); if (!chosen.length) return;
    if (chosen.length !== 1) { toast("一次请选择一份文本文件。原草稿保留。"); return; }
    if (!fileMaterials || !fileInputsSupported() || quoteDraft().busy || !quoteDraft().active) return;
    if (state.task_intent.mode === "files" && state.attachment_ids.length) { toast("文件任务当前只支持文字。图片仍保留，请先关闭生成或移除图片再添加文件。"); return; }
    if (state.task_intent.artifact_ids.length >= 4) { toast("一轮最多选择四份文件输入或成果，已有选择仍保留。"); return; }
    const target = () => JSON.stringify([state.page, state.status?.workspace_id, state.selected.type, state.selected.id,
      state.selected.agent, state.selected.sessionId, state.openVersion, draftKey(), state.contextNavigationVersion]);
    const captured = target(), token = requestId(), job = { version: state.openVersion, key: draftKey() };
    state.fileMaterialJobs.set(token, job); state.fileMaterialError = ""; renderFileMaterialStatus(); updateComposer();
    try {
      const item = await fileMaterials.upload(chosen[0]); state.fileMaterialJobs.delete(token);
      if (captured === target()) await selectFileMaterial(item, fileMaterialView());
      else toast("文件输入已保留。你已离开原话题，可从‘已添加的文件’找回；未发送。");
    } catch (error) { if (captured === target()) state.fileMaterialError = userError(error, "上传结果尚未确认，请查询原回执。原草稿保留，未重传。"); }
    finally { state.fileMaterialJobs.delete(token); renderFileMaterialStatus(); updateComposer(); }
  }
  function appendFileInputs(parent, message) {
    if (!fileMaterials || !Array.isArray(message.file_inputs) || !message.file_inputs.length) return;
    const section = el("section", "file-task-results"); section.setAttribute("aria-label", "这条消息的文件输入"); section.append(el("p", "file-task-heading", "文件输入"));
    for (const raw of message.file_inputs.slice(0, 4)) { try {
      if (raw?.source === "generated" && fileTasks && state.selected.type === "chat") section.append(fileTasks.inputCard(state.selected.id, raw));
      else section.append(fileMaterials.card(fileMaterials.accept(raw)));
    } catch (_) { section.append(el("p", "file-task-note", "有一份文件输入信息暂时无法读取，原消息保留。")); } }
    parent.append(section);
  }
  function renderComposerFiles() {
    const root = $("composer-files"), current = taskIntent(state.task_intent), busy = state.sending || state.creating || currentFileMaterialJobs() > 0;
    root.hidden = current.mode !== "files" && !current.artifact_ids.length; root.setAttribute("aria-label", "本轮文件材料"); root.replaceChildren();
    if (root.hidden) return;
    hydrateFileSources();
    root.append(el("p", current.mode === "files" && !fileTaskSupported() ? "file-task-warning" : "", current.mode === "files" ? fileTaskSupported() ? "本轮生成文件：AI 会读取你选择的文本文件，并在这台电脑的独立成果区保存新文件。" : "所选连接不能执行文件任务。文件选择和草稿保留，可换支持文件的 AI 配置，或关闭生成后读取材料。" : "本轮文件材料：AI 将使用这些固定版本的文字，文件原件保留。"));
    const selected = el("div", "composer-file-picks"), available = availableFileArtifacts();
    for (const id of current.artifact_ids) {
      const source = sourceFile(id), generated = available.get(id), known = generated || source;
      if (source && !generated && fileMaterials) { selected.append(fileMaterials.card(source, { removable: true, disabled: busy, onRemove: () => setTaskIntent({ mode: current.mode, artifact_ids: current.artifact_ids.filter(value => value !== id) }) })); continue; }
      if (generated && fileTasks && state.selected.type === "chat") { selected.append(fileTasks.inputCard(state.selected.id, generated, { removable: true, disabled: busy, onRemove: () => setTaskIntent({ mode: current.mode, artifact_ids: current.artifact_ids.filter(value => value !== id) }) })); continue; }
      const item = el("span", "composer-file-pick"); item.append(el("span", "", generated?.name || (state.fileMaterialChecks.get(id) === "loading" ? "正在核对文件输入…" : `文件选择 ${id.slice(0, 8)} · 暂不可读取`)));
      if (generated && (!known.available || !known.content_verified)) item.append(el("span", "file-task-warning", "暂不可读取"));
      const remove = button("×", "", () => setTaskIntent({ mode: current.mode, artifact_ids: current.artifact_ids.filter(value => value !== id) })); remove.disabled = busy; remove.setAttribute("aria-label", `移除 ${known?.name || "这份文件选择"}`); item.append(remove); selected.append(item);
    }
    root.append(selected);
    if (fileInputsUnready()) root.append(el("p", "file-task-warning", "所选文件正在核对或暂不可读取；原选择与草稿保留，可重新选择或明确移除后发送。"));
    if (current.mode === "files" && state.attachment_ids.length) root.append(el("p", "file-task-warning", "当前文件任务不支持图片；图片与草稿已保留，请关闭生成或移除图片。"));
  }
  function attachmentIds(value) { return window.RabbitMaterials ? window.RabbitMaterials.ids(value) : value === undefined ? [] : [...value]; }
  function readAttachmentIds(value) { return window.RabbitMaterials ? window.RabbitMaterials.ids(value, 512) : value === undefined ? [] : [...value]; }
  function imageReason(value) { return window.RabbitMaterials?.reason(value) || "图片暂不可用，原因尚未确认"; }
  function imageSummary(message) {
    const number = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
    const provided = message.attachment_summary;
    let ready = 0; try { ready = readAttachmentIds(message.attachment_ids).length; } catch (_) {}
    if (provided && typeof provided === "object") return { total: number(provided.total), ready: number(provided.ready), unavailable: number(provided.unavailable), omitted: number(provided.omitted), unknown_omitted: provided.unknown_omitted === true, reasons: provided.reasons };
    const native = Array.isArray(message.native_attachments) ? message.native_attachments.slice(0, 512) : [];
    const actual = native.filter(item => item && item.kind !== "inspection_summary");
    const unavailable = actual.filter(item => item.material_state !== "ready").length;
    const summaries = native.filter(item => item?.kind === "inspection_summary");
    const omitted = summaries.reduce((count, item) => count + number(item.omitted_count), 0);
    return { total: native.length ? actual.length + omitted : ready, ready: native.length ? actual.length - unavailable : ready, unavailable, omitted, unknown_omitted: summaries.some(item => item.omitted_count === -1), reasons: null };
  }
  function imageSummaryText(message) {
    const summary = imageSummary(message);
    return `图片 ${summary.total} 张 · 可查看 ${summary.ready} 张 · 暂不可用 ${summary.unavailable} 张${summary.omitted ? ` · 未检查 ${summary.omitted} 张` : ""}${summary.unknown_omitted ? " · 另有未检查图片，数量未知" : ""}`;
  }
  function hasImageRecords(message) { return !!(message.attachment_ids?.length || message.native_attachments?.length || message.attachment_summary?.total || message.attachment_summary?.omitted || message.attachment_summary?.unknown_omitted); }
  function appendMessageMaterials(parent, message) {
    if (!hasImageRecords(message)) return;
    const section = el("section", "message-image-section"); section.setAttribute("aria-label", "这条消息的图片");
    if (message.attachment_summary || message.native_attachments?.length) section.append(el("p", "message-image-summary", imageSummaryText(message)));
    const images = el("div", "message-materials");
    try { if (materials) readAttachmentIds(message.attachment_ids).forEach(id => images.append(materials.card(id))); }
    catch (_) { images.append(el("p", "material-error", "这条消息的图片标识暂时无法读取，原文字保留。")); }
    if (images.childNodes.length) section.append(images);
    const native = Array.isArray(message.native_attachments) ? message.native_attachments.slice(0, 512) : [];
    for (const record of native) {
      if (!record || record.material_state === "ready") continue;
      const reason = record.material_reason && record.material_reason !== "native_unavailable" ? record.material_reason : record.reason;
      const position = Number.isSafeInteger(record.attachment_ordinal) && record.attachment_ordinal >= 0 ? `第 ${record.attachment_ordinal + 1} 项：` : "";
      const item = el("p", "native-image-unavailable", `${position}${imageReason(reason)}${record.kind === "inspection_summary" && record.omitted_count >= 0 ? `（${record.omitted_count} 张未检查）` : ""}`);
      section.append(item);
    }
    // Summaries may describe failures outside the returned preview descriptors.
    const rawReasons = message.attachment_summary?.reasons;
    const reasons = Array.isArray(rawReasons) ? rawReasons : rawReasons && typeof rawReasons === "object" ? Object.entries(rawReasons).map(([reason, count]) => ({ reason, count })) : [];
    if (!native.length) for (const value of reasons.slice(0, 64)) {
      const code = typeof value === "string" ? value : value?.reason;
      section.append(el("p", "native-image-unavailable", `${imageReason(code)}${Number.isSafeInteger(value?.count) && value.count > 0 ? `（${value.count} 项）` : ""}`));
    }
    parent.append(section);
  }
  function sameAttachments(a, b) { return JSON.stringify(a || []) === JSON.stringify(b || []); }
  function draftHasContent(text = ui.input.value, ids = state.attachment_ids, task) { const files = task === undefined ? arguments.length === 0 ? state.task_intent.artifact_ids.length : 0 : taskIntent(task).artifact_ids.length; return !!(text.trim() || ids.length || files); }
  function removeDraft(key) { state.drafts.delete(key); state.draftAttachments.delete(key); state.taskIntents.delete(key); state.profileChoices.delete(key); }
  function moveDraft(from, to) { const task = taskIntent(state.taskIntents.get(from)); if (task.artifact_ids.length && from !== to && !allWorkspaceSourceFiles(task)) throw new Error("文件成果只能在原聊天使用，原草稿已保留。"); state.taskIntents.set(to, task); state.drafts.set(to, state.drafts.get(from) || ""); state.draftAttachments.set(to, [...(state.draftAttachments.get(from) || [])]); if (state.profileChoices.has(from)) state.profileChoices.set(to, state.profileChoices.get(from)); removeDraft(from); }
  function currentUploadCount() { return currentFileMaterialJobs() + [...state.materialUploads.values()].filter(item => item.version === state.openVersion && item.key === draftKey()).reduce((count, item) => count + item.remaining, 0); }
  function setAttachments(value, edited = true) {
    state.attachment_ids = attachmentIds(value);
    if (edited) {
      ++state.sourceUpdateInputVersion; state.browserInputTouched = true; state.browserTouchedDrafts.add(draftKey());
      invalidateContextPreview("图片材料已修改，请重新查看本轮上下文。"); invalidateSourceUpdate("材料已修改，请重新查看来源更新。"); invalidateSourceImages("草稿图片已修改，请重新查看来源图片。");
      state.materialError = ""; saveDraft();
    }
    renderComposerMaterials(); updateComposer();
  }
  function renderComposerMaterials() {
    const root = $("composer-materials"), busy = state.sending || state.creating;
    root.hidden = !state.attachment_ids.length;
    const stamp = JSON.stringify([state.attachment_ids, busy]);
    if (root.materialStamp !== stamp) { root.materialStamp = stamp; root.replaceChildren();
    if (materials) state.attachment_ids.forEach((id, index) => root.append(materials.card(id, { disabled: busy,
      remove: () => { if (!busy) setAttachments(state.attachment_ids.filter(value => value !== id)); },
      move: index > 0 ? () => { if (!busy) { const next = [...state.attachment_ids]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; setAttachments(next); } } : null })));
    }
    const count = [...state.materialUploads.values()].filter(item => item.version === state.openVersion && item.key === draftKey()).reduce((total, item) => total + item.remaining, 0), status = $("material-upload-status");
    status.hidden = !count && !state.materialError; status.textContent = count ? `正在上传 ${count} 张图片，完成后再发送…` : state.materialError;
  }
  async function uploadImages(files) {
    if (state.task_intent.mode === "files") { toast("当前文件任务只支持文字。可切回普通聊天后再添加图片。"); return; }
    if (!materials || !state.status?.materials?.enabled || state.sending || state.creating || currentUploadCount()) return;
    const chosen = Array.from(files || []); if (!chosen.length) return;
    const config = state.status.materials, maximum = Math.min(4, config.max_message_images || 4);
    if (chosen.length + state.attachment_ids.length + currentUploadCount() > maximum) { state.materialError = `一条消息最多 ${maximum} 张图片；已选图片和文字仍保留。`; renderComposerMaterials(); return; }
    const job = { version: state.openVersion, key: draftKey(), remaining: chosen.length }, token = requestId(), errors = []; state.materialUploads.set(token, job); state.materialError = ""; renderComposerMaterials(); updateComposer();
    try {
      for (const file of chosen) {
        try {
          const result = await materials.upload(file, config.max_file_bytes || 5242880);
          if (job.version === state.openVersion && job.key === draftKey()) setAttachments([...state.attachment_ids, result.id]);
        } catch (error) { if (job.version === state.openVersion && job.key === draftKey()) errors.push(userError(error, "图片上传失败，原文字和已加入的图片仍保留。请重新添加。")); }
        finally { --job.remaining; }
      }
    } finally { state.materialUploads.delete(token); if (job.version === state.openVersion && job.key === draftKey() && errors.length) state.materialError = errors.join(" "); renderComposerMaterials(); updateComposer(); }
  }
  function saveDraft(deferred = false) {
    if (state.browserRestoring) return;
    const choice = historyChoice();
    // A cached draft exists before the empty startup composer has been filled.
    // Visibility/pagehide events must not count that placeholder as a user edit.
    const unfilled = browserRestoreSnapshot && state.openVersion === 0 && !state.browserTouchedDrafts.has(draftKey()) && ui.input.value === "" && !state.attachment_ids.length;
    if (!unfilled) { state.drafts.set(draftKey(), ui.input.value); state.draftAttachments.set(draftKey(), [...state.attachment_ids]); state.taskIntents.set(draftKey(), taskIntent(state.task_intent)); if (profilesEnabled()) state.profileChoices.set(draftKey(), homeNativeChoice()?.profile ? toProfileSnapshot(state.homeNativeChoice.profile, state.homeNativeChoice.memory) : state.profile_snapshot); }
    if (!unfilled && choice && draftHasContent()) state.historyChoices.set(state.selected.id, choice);
    return checkpointBrowserState(deferred);
  }
  function browserSnapshot() {
    if (state.page === "conversations") state.conversationScrollTop = Math.min(1e9, Math.max(0, Number(ui.scroll.scrollTop) || 0));
    const snapshot = {
      selection: { ...state.selected }, page: state.page === "continuation" && !state.currentContinuation ? "conversations" : state.page, backend: state.backend, model_id: state.model_id, homeNativeChoice: state.homeNativeChoice, homeConfiguration: state.homeConfiguration, homeNativeHandoff: state.homeNativeHandoff, homeConfigurationReceipt: state.homeConfigurationReceipt, homeTextCompatibility: state.homeTextCompatibility,
      workspaceSections: { ...state.workspaceSections }, managementTarget: state.managementTarget,
      dashboardView: myAI?.getView?.() || null,
      historyReadOnly: state.historyReadOnly,
      continuations: state.continuations, currentContinuation: state.currentContinuation,
      query: state.libraryQuery, historyAgent: state.historyAgent,
      historyOffset: state.historyMessageStart, historyMatch: state.historyMatch?.titleOnly ? null : state.historyMatch,
      scrollTop: state.conversationScrollTop,
      drafts: [...state.drafts].filter(([, text]) => text.length > 0),
      draftAttachments: [...state.draftAttachments].filter(([, ids]) => ids.length > 0),
      taskIntents: [...state.taskIntents].filter(([, task]) => task.mode !== "chat" || task.artifact_ids.length),
      newCreationId: state.newCreationId,
      historyCreationIds: [...state.historyCreationIds], historyBackendChoices: [...state.historyBackendChoices], historyModelChoices: [...state.historyModelChoices],
      historyChoices: { version: 1, entries: [...state.historyChoices].filter(([id, choice]) => choice.mode === "text" || choice.memory_explicit
        || state.historyContinuationIds.has(id) || state.historyBackendChoices.has(id) || state.historyCreationIds.has(id)
        || draftHasContent(state.drafts.get("history:" + id) || "", state.draftAttachments.get("history:" + id) || [], state.taskIntents.get("history:" + id))) },
      historyContinuationIds: [...state.historyContinuationIds],
      pendingSubmissions: [...state.pendingSubmissions.values()].map(({ chatId, text, backend, model_id = "", attachment_ids = [], task_intent, clientId, profile_snapshot = null, profile_selection_revision = null }) => ({ chatId, text, backend, model_id, attachment_ids: [...attachment_ids], task_intent: taskIntent(task_intent), clientId, profile_snapshot, profile_selection_revision })),
      profileChoices: { version: 1, entries: [...state.profileChoices] },
      sourceImagePending: { version: 1, entries: [...state.sourceImagePending.values()].map(({ chatId, plan_version, chat_revision }) => ({ chatId, plan_version, chat_revision })) },
      memoryChatCreation: { version: 1, pending: state.memoryChatCreation },
    };
    if (browserRestoreSnapshot && state.openVersion === 0 && !state.browserInputTouched) {
      // While startup reads or a receipt are pending, keep the previous topic.
      // Current journals/maps still win; a real topic choice increments openVersion.
      const previous = browserRestoreSnapshot;
      snapshot.selection = { ...previous.selection };
      snapshot.historyOffset = previous.historyOffset; snapshot.historyMatch = previous.historyMatch;
      snapshot.scrollTop = previous.scrollTop;
      if (!state.browserPageTouched) snapshot.page = previous.page;
      if (!state.browserQueryTouched) snapshot.query = previous.query;
      if (!state.browserAgentFilterTouched) snapshot.historyAgent = previous.historyAgent;
      if (!state.backendChoiceVersion) { snapshot.backend = previous.backend; snapshot.model_id = previous.model_id || ""; }
    }
    return snapshot;
  }
  function checkpointBrowserState(deferred = false) {
    if (!browserStore || state.browserRestoring) return false;
    clearTimeout(state.browserSaveTimer); state.browserSaveTimer = null;
    if (deferred) { state.browserSaveTimer = setTimeout(() => checkpointBrowserState(), 250); return true; }
    const saved = browserStore.save(browserSnapshot());
    browserStorageNotice(browserStore.status().notice);
    return saved;
  }
  function browserStorageNotice(message) {
    state.browserNotice = message || "";
    const notice = $("browser-state-notice");
    notice.textContent = state.browserNotice; notice.hidden = !state.browserNotice;
  }
  function beginBrowserRestore() {
    if (browserStore) return browserRestoreSnapshot;
    if (!window.RabbitBrowserState || !state.status || !state.status.workspace_id) return null;
    const storage = (kind) => { try { return window[kind]; } catch (_) { return null; } };
    browserStore = window.RabbitBrowserState.create({ workspaceId: state.status.workspace_id, location: window.location,
      localStorage: storage("localStorage"), sessionStorage: storage("sessionStorage"), onError: browserStorageNotice });
    const snapshot = browserStore.load();
    browserRestoreSnapshot = snapshot;
    if (!snapshot) return null;
    if (snapshot.workspaceSections) state.workspaceSections = { ...snapshot.workspaceSections };
    state.managementTarget = snapshot.managementTarget || null;
    state.historyReadOnly = snapshot.historyReadOnly === true;
    state.continuations = snapshot.continuations || [];
    state.currentContinuation = snapshot.currentContinuation || null;
    state.drafts = new Map([...snapshot.drafts, ...[...state.drafts].filter(([key, text]) => text.length > 0 || state.browserTouchedDrafts.has(key))]);
    state.draftAttachments = new Map([...(snapshot.draftAttachments || []), ...[...state.draftAttachments].filter(([key, ids]) => ids.length > 0 || state.browserTouchedDrafts.has(key))]);
    state.taskIntents = new Map([...(snapshot.taskIntents || []), ...[...state.taskIntents].filter(([key]) => state.browserTouchedDrafts.has(key))]);
    state.pendingSubmissions = new Map([...snapshot.pendingSubmissions.map((submission) => [submission.chatId, { ...submission, uncertain: true }]), ...state.pendingSubmissions]);
    state.sourceImagePending = new Map([...(snapshot.sourceImagePending?.entries || []).map(entry => [entry.chatId, entry]), ...state.sourceImagePending]);
    state.profileChoices = new Map([...(snapshot.profileChoices?.entries || []), ...state.profileChoices]);
    if (!state.backendChoiceVersion) {
      state.homeNativeChoice = snapshot.homeNativeChoice || null;
      state.homeConfiguration = snapshot.homeConfiguration || (state.homeNativeChoice ? nextHomeConfiguration() : null);
      // Older nonempty text drafts retain their route. Fresh defaults and new
      // snapshots use the same native choice as an explicit selection.
      const oldTextDraft = !Object.hasOwn(snapshot, "homeTextCompatibility") && !snapshot.homeNativeChoice
        && (snapshot.newCreationId || snapshot.memoryChatCreation?.pending || snapshot.profileChoices?.entries.some(([key, value]) => key === "__new__" && value)
          && draftHasContent(new Map(snapshot.drafts).get("__new__") || "", new Map(snapshot.draftAttachments).get("__new__") || [], new Map(snapshot.taskIntents).get("__new__")));
      state.homeTextCompatibility = snapshot.homeTextCompatibility === true || !!oldTextDraft;
    }
    state.homeNativeHandoff = state.homeNativeHandoff || snapshot.homeNativeHandoff || null;
    state.homeConfigurationReceipt = state.homeConfigurationReceipt || snapshot.homeConfigurationReceipt || null;
    state.memoryChatCreation = state.memoryChatCreation || snapshot.memoryChatCreation?.pending || null;
    if (state.openVersion === 0 && state.selected.type === "new" && snapshot.selection.type === "new" && !state.newCreationId) state.newCreationId = snapshot.newCreationId;
    state.historyCreationIds = new Map([...snapshot.historyCreationIds, ...state.historyCreationIds]);
    state.historyBackendChoices = new Map([...snapshot.historyBackendChoices, ...state.historyBackendChoices]);
    state.historyModelChoices = new Map([...(snapshot.historyModelChoices || []), ...state.historyModelChoices]);
    state.historyContinuationIds = new Map([...snapshot.historyContinuationIds, ...state.historyContinuationIds]);
    state.historyChoices = new Map([...(snapshot.historyChoices?.entries || []), ...state.historyChoices]);
    return snapshot;
  }
  function canRestoreBrowserView() {
    return state.openVersion === 0 && ["my-ai", "conversations"].includes(state.page) && !state.browserInputTouched && !state.browserPageTouched && !state.browserQueryTouched && !state.browserAgentFilterTouched && !ui.input.value && !state.libraryQuery && state.historyAgent === "all" && state.backendChoiceVersion === 0 && !state.sending && !state.creating;
  }
  function restoreHomeSnapshot(snapshot) {
    if (!canRestoreBrowserView()) return false;
    // A fresh visit opens Home. Journals and every chat's local draft were
    // loaded above; a reload still restores the current conversation below.
    restoreDraft();
    showPage("my-ai"); updateComposer();
    return true;
  }
  async function restoreBrowserSnapshot(snapshot) {
    if (!snapshot || !canRestoreBrowserView()) return false;
    myAI?.restoreView?.(snapshot.dashboardView);
    if (["my-ai", "management", "settings"].includes(snapshot.page)) {
      state.selected = { ...snapshot.selection }; state.backend = snapshot.backend; state.model_id = snapshot.model_id || "";
      restoreDraft();
      showPage(snapshot.page, state.workspaceSections[snapshot.page]);
      if (snapshot.page === "management" && snapshot.managementTarget) await workspace?.openObject?.(snapshot.managementTarget);
      checkpointBrowserState(); return true;
    }
    if (snapshot.page === "continuation") {
      state.selected = { type: "new", id: null };
      restoreDraft(); showPage("continuation"); checkpointBrowserState(); return true;
    }
    if (snapshot.page === "native-chats" && nativeChats?.hasReadOnlySelection()) {
      // The saved native reading identity is separate from the last editable
      // composer. Restoring it must not first open that unrelated conversation.
      state.libraryQuery = snapshot.query; ui.search.value = snapshot.query; state.historyAgent = snapshot.historyAgent;
      sourceFilters().forEach(node => { const active = node.dataset.agent === state.historyAgent; node.classList.toggle("active", active); node.setAttribute("aria-pressed", String(active)); });
      if (snapshot.query || snapshot.historyAgent !== "all") loadLibrary(true).catch(() => {});
      restoreDraft(); showPage("native-chats"); checkpointBrowserState(); return true;
    }
    let selection = { ...snapshot.selection };
    const creationId = selection.type === "new" ? snapshot.newCreationId : selection.type === "history" ? state.historyCreationIds.get(selection.id) : null;
    if (creationId) {
      try {
        const receipt = await requestReceipt("creation", creationId);
        if (!canRestoreBrowserView()) return false;
        if (receipt && receipt.received) {
          const key = draftKey(selection), targetDraft = state.drafts.get(receipt.chat_id);
          const targetTask = taskIntent(state.taskIntents.get(receipt.chat_id)), originalTask = taskIntent(state.taskIntents.get(key));
          if (!draftHasContent(targetDraft || "", state.draftAttachments.get(receipt.chat_id) || [], targetTask)) {
            for (const id of originalTask.artifact_ids) if (!sourceFile(id) && fileMaterials) await fileMaterials.inspect(id);
            if (!canRestoreBrowserView()) return false;
            moveDraft(key, receipt.chat_id);
          } else if (!sameTaskIntent(targetTask, originalTask) || (targetDraft || "") !== (state.drafts.get(key) || "") || !sameAttachments(state.draftAttachments.get(receipt.chat_id), state.draftAttachments.get(key))) toast("已创建的聊天有另一份草稿，原文件选择和文字也仍留在原话题，未覆盖。");
          if (selection.type === "history") { state.historyContinuationIds.set(selection.id, receipt.chat_id); state.historyCreationIds.delete(selection.id); }
          selection = { type: "chat", id: receipt.chat_id };
        } else if (selection.type === "new") state.newCreationId = creationId;
      } catch (_) {
        if (!canRestoreBrowserView()) return false;
        if (selection.type === "new") state.newCreationId = creationId;
        toast("上次创建的回执暂时没查到；草稿和原请求已保留，未自动发送。");
      }
    }
    if (!canRestoreBrowserView()) return false;
    state.libraryQuery = snapshot.query; ui.search.value = snapshot.query; state.historyAgent = snapshot.historyAgent;
    sourceFilters().forEach((node) => { const active = node.dataset.agent === state.historyAgent; node.classList.toggle("active", active); node.setAttribute("aria-pressed", String(active)); });
    if (snapshot.query || snapshot.historyAgent !== "all") loadLibrary(true).catch(() => {});
    const choiceVersion = state.backendChoiceVersion;
    // Only the synchronous opening phase suppresses saving the initial empty composer.
    // User edits and navigation while the GET is pending must remain saveable.
    state.browserRestoring = true;
    let opening;
    try {
      if (selection.type === "chat") opening = openChat(selection.id, true, snapshot.historyMatch);
      else if (selection.type === "history") opening = openHistory(selection.agent, selection.sessionId, snapshot.historyMatch, snapshot.historyOffset, snapshot.historyReadOnly === true);
      else { if (!creationId) state.newCreationId = snapshot.newCreationId; newChat(); opening = Promise.resolve(); }
    } finally { state.browserRestoring = false; }
    const version = state.openVersion;
    await opening;
    if (version !== state.openVersion || state.page !== "conversations") { checkpointBrowserState(); return true; }
    if (choiceVersion === state.backendChoiceVersion) { state.backend = snapshot.backend; state.model_id = snapshot.model_id || ""; restoreProfileChoice(); updateComposer(); }
    if (snapshot.page !== "conversations") showPage(snapshot.page);
    state.conversationScrollTop = snapshot.scrollTop;
    if (state.page === "conversations") ui.scroll.scrollTop = snapshot.scrollTop;
    requestAnimationFrame(() => { if (version === state.openVersion && state.page === "conversations" && state.page === snapshot.page) { ui.scroll.scrollTop = snapshot.scrollTop; checkpointBrowserState(); } });
    checkpointBrowserState();
    if (selection.type === "chat") await reconcileRecoveredSubmission(selection.id, version);
    if (selection.type === "chat") await reconcileSourceImagePending(selection.id, version);
    if (state.memoryChatCreation) { try { await recoverMemoryChatCreation(); } catch (_) { /* Restoration only reads the original creation receipt. */ } if (version === state.openVersion) renderRunNotice(); }
    return true;
  }
  async function reconcileRecoveredSubmission(chatId, version) {
    const submission = state.pendingSubmissions.get(chatId);
    if (!submission) return;
    try {
      const receipt = await requestReceipt("message", submission.clientId, chatId);
      if (!receipt || !receipt.received) {
        if (version === state.openVersion && state.selected.id === chatId) { state.pendingSubmission = submission; state.transientError = "上次发送尚未确认，文字已保留。点发送会先查回执；不会自动重发。"; renderRunNotice(); }
        return;
      }
      if (state.pendingSubmissions.get(chatId) !== submission) return;
      state.pendingSubmissions.delete(chatId);
      const changedRouteHere = version === state.openVersion && state.selected.id === chatId && (state.backend !== submission.backend || state.model_id !== (submission.model_id || "") || !sameProfile(state.profile_snapshot, submission.profile_snapshot) || !sameTaskIntent(state.task_intent, submission.task_intent));
      if (!changedRouteHere && (state.drafts.get(chatId) || "").trim() === submission.text && sameProfile(state.profileChoices.get(chatId), submission.profile_snapshot) && sameAttachments(state.draftAttachments.get(chatId), submission.attachment_ids) && sameTaskIntent(state.taskIntents.get(chatId), submission.task_intent)) removeDraft(chatId);
      if (state.pendingSubmission === submission) state.pendingSubmission = null;
      if (version === state.openVersion && state.selected.id === chatId) {
        if (!changedRouteHere && draftKey() === submission.chatId && ui.input.value.trim() === submission.text && sameAttachments(state.attachment_ids, submission.attachment_ids)) { ui.input.value = ""; state.attachment_ids = []; state.task_intent = taskIntent(undefined); resizeInput(); }
        state.transientError = ""; renderRunNotice(); toast("已确认上次消息发送成功；没有重复发送。");
      }
    } catch (_) {
      if (version === state.openVersion && state.selected.id === chatId) { state.pendingSubmission = submission; state.transientError = "暂时查不到上次发送的回执。草稿已保留，未自动发送。"; renderRunNotice(); }
    } finally { checkpointBrowserState(); }
  }
  function restoreDraft() {
    const continuation = state.selected.type === "history" && state.historyContinuationIds.get(state.selected.id);
    const key = draftHasContent(state.drafts.get(draftKey()) || "", state.draftAttachments.get(draftKey()) || [], state.taskIntents.get(draftKey())) || state.taskIntents.get(draftKey())?.mode === "files" ? draftKey() : continuation || draftKey();
    ui.input.value = state.drafts.get(key) || "";
    state.attachment_ids = [...(state.draftAttachments.get(key) || [])]; state.task_intent = taskIntent(state.taskIntents.get(key)); state.materialError = ""; restoreProfileChoice(); renderComposerMaterials(); renderComposerFiles(); resizeInput();
  }
  function resizeInput() {
    ui.input.style.height = "auto";
    ui.input.style.height = `${Math.min(170, Math.max(53, ui.input.scrollHeight))}px`;
    updateComposer();
  }
  function isRunning() { return !!(state.run && activeRunStates.has(state.run.status)); }
  function backendInfo(id) {
    return state.status && Array.isArray(state.status.backends) ? state.status.backends.find((item) => item.id === id) : null;
  }
  function validModelId(value, backend) { return agents.validModelId(value, backend); }
  function defaultModelId(backend) { const value = state.preferences.default_models?.[backend] || ""; return validModelId(value, backend) ? value : ""; }
  function modelInfo(modelId = state.model_id, backend = state.backend) { return modelId ? state.management?.models?.find(model => model.id === modelId && model.agent_id === backend) || null : backendInfo(backend); }
  function modelReady(modelId = state.model_id, backend = state.backend) { if (homeNativeChoice() && modelId === state.model_id && backend === state.backend) return profileReady(); return !!(backendInfo(backend)?.available && modelInfo(modelId, backend)?.available && (modelId !== state.model_id || backend !== state.backend || profileReady())); }
  function modelCapabilityInfo(modelId = state.model_id, backend = state.backend) {
    const current = modelInfo(modelId, backend);
    return modelId ? current : state.management?.models?.find(model => model.agent_id === backend && model.model === backendInfo(backend)?.model) || state.management?.models?.find(model => model.agent_id === backend && model.is_default) || current;
  }
  function fileInputsSupported() { return !!(fileMaterials && state.status?.files?.upload_enabled === true); }
  function fileTaskSupported(backend = state.backend, modelId = state.model_id) {
    return !homeNativeChoice() && !!(state.status?.files?.enabled && state.status.files.backends?.includes(backend) && modelCapabilityInfo(modelId, backend)?.file_output !== "unsupported");
  }
  function modelUnavailableReason() {
    const snap = state.profile_snapshot, memory = snap?.memory_profile.id !== "none" && profiles?.getMemory(snap?.memory_profile.id);
    if (snap?.memory_profile.id !== "none" && snap?.memory_profile.id) {
      if (!memory) return "所选记忆配置目前无法读取。原选择和草稿保留，可明确选择不使用长期记忆。";
      if (memory.revision !== snap.memory_profile.revision) return "草稿使用旧记忆配置。可继续编辑保存；发送前请明确重新选择最新版本。";
      if (!memory.available) return plain(memory.reason) || "所选记忆配置当前不可用。原草稿保留，可明确选择不使用长期记忆。";
    }
    return state.model_id ? modelInfo() ? plain(modelInfo().detail || modelInfo().reason) || "所选模型暂时不可用，可以重新检测或明确选择另一模型。" : "所选模型目前不在目录中，请在管理里重新检测或明确选择；原选择和草稿保留。" : backendUnavailableReason(backendInfo(state.backend));
  }
  function backendUnavailableReason(info) {
    const reason = plain(info && info.reason);
    if (/登录|login|auth/i.test(reason)) return "请先完成登录，再使用这位 AI。";
    if (reason && /[\u4e00-\u9fff]/.test(reason) && reason.length < 100) return reason;
    return "这位 AI 暂时不能回答，仍可以阅读已有记录。";
  }
  let composerStopTarget = null;
  let homeReleasing = false;
  const homeReleaseBox = el("div", "home-handoff-actions"), homeRelease = button("解除这次交接", "text-button", releaseHomeHandoff), homeReleaseNote = el("p", "muted");
  homeRelease.setAttribute("data-control-interface", "human");
  homeReleaseBox.append(homeRelease, homeReleaseNote); $("composer-more").append(homeReleaseBox);
  async function readHomeCreation(record, workspace) {
    if (state.homeNativeHandoff !== record || workspace !== state.status?.workspace_id) return null;
    const found = await nativeChats?.readCreation?.(record.creation_id);
    if (state.homeNativeHandoff !== record || workspace !== state.status?.workspace_id) return null;
    if (!found || found.workspace_id !== workspace || found.agent !== "codex" || found.creation_id !== record.creation_id) throw Error("原创建结果尚未核实，交接记录保留。");
    if (found.chat === null) return null;
    if (!homePreparation(record, workspace).onProgress({ created_chat_id: found.chat.id })) throw Error("原对话已找到，但浏览器不能保存确认，交接记录保留。");
    return found.chat.id;
  }
  async function releaseHomeHandoff() {
    const record = state.homeNativeHandoff, choice = homeNativeChoice(), identity = state.homeConfiguration, workspace = state.status?.workspace_id;
    if (!record || !choice || homeReleasing || state.nativeHomeSending || state.page !== "conversations" || saveDraft() !== true) return;
    homeReleasing = true; updateComposer();
    try {
      if (!record.created_chat_id) {
        if (!await readHomeCreation(record, workspace)) toast("原创建结果尚未确认，原编号和草稿保留；没有新建。");
        return;
      }
      const data = await api("/api/native-chats");
      if (state.homeNativeHandoff !== record || state.homeNativeChoice !== choice || state.homeConfiguration !== identity || state.status?.workspace_id !== workspace) return;
      if (data?.workspace_id !== workspace || !Array.isArray(data.chats) || !data.chats.some(row => row.id === record.created_chat_id && row.agent === "codex" && row.role === "chat")) throw Error("原对话尚未核实，没有解除交接。");
      const previousSnapshot = state.profile_snapshot;
      state.homeConfiguration = nextHomeConfiguration(true);
      if (choice.local_base) state.homeNativeChoice = { ...choice, profile: { ...choice.profile, id: state.homeConfiguration.draft_id, revision: state.homeConfiguration.revision } };
      state.profile_snapshot = state.homeNativeChoice.profile ? toProfileSnapshot(state.homeNativeChoice.profile, choice.memory) : null;
      state.profileChoices.set("__new__", state.profile_snapshot);
      if (!checkpointHomeHandoff(record, null)) {
        state.homeConfiguration = identity; state.homeNativeChoice = choice; state.profile_snapshot = previousSnapshot; state.profileChoices.set("__new__", previousSnapshot);
        throw Error("浏览器不能保存解除结果，原交接与草稿保留。");
      }
    } catch (error) { toast(userError(error, "原对话尚未核实，没有解除交接。")); }
    finally { homeReleasing = false; updateComposer(); }
  }
  function updateComposer() {
    homeReleaseBox.hidden = state.page !== "conversations" || state.selected.type !== "new" || !state.homeNativeHandoff;
    homeRelease.disabled = homeReleasing || state.nativeHomeSending;
    homeRelease.textContent = state.homeNativeHandoff?.created_chat_id ? "解除这次交接" : "核对原创建";
    homeReleaseNote.textContent = state.homeNativeHandoff?.created_chat_id ? "原对话和当前草稿都会保留。" : "只核对上次创建结果，不创建或发送。";
    applyObservedProfileSelection();
    renderComposerFiles(); const fileMode = state.task_intent.mode === "files"; const fileBlocked = (fileMode || state.task_intent.artifact_ids.length > 0) && (!ui.input.value.trim() || fileInputsUnready() || !fileMode && !fileInputsSupported() || fileMode && (state.attachment_ids.length > 0 || !fileTaskSupported()));
    $("toggle-file-task").textContent = fileMode ? "取消生成文件" : "生成文件"; $("toggle-file-task").setAttribute("aria-pressed", String(fileMode)); $("toggle-file-task").disabled = !fileTasks || !fileMode && !fileTaskSupported() || state.sending || state.creating || currentUploadCount() > 0;
    $("add-text-file").disabled = $("pick-text-file").disabled = !fileInputsSupported() || state.sending || state.creating || state.backendChoosing || currentUploadCount() > 0 || fileMode && state.attachment_ids.length > 0 || state.task_intent.artifact_ids.length >= 4;
    const selectedInfo = backendInfo(state.backend);
    const available = modelReady(), nativeHistory = historyUsesNative(), nativeHome = homeNativeChoice();
    $("toggle-file-task").hidden = !!(nativeHistory || nativeHome) && !fileMode;
    const historyLoading = state.selected.type === "history" && !state.historyReady;
    const ordinaryPending = state.selected.type === "chat" && (state.pendingSubmissions.get(state.selected.id) || state.pendingSubmission);
    const ordinaryReceiptOnly = !!(ordinaryPending?.uncertain && (!modelReady() || profilesEnabled() && ordinaryPending.text !== ui.input.value.trim() || ordinaryPending.backend !== state.backend || (ordinaryPending.model_id || "") !== state.model_id || !sameProfile(ordinaryPending.profile_snapshot, state.profile_snapshot) || !sameAttachments(ordinaryPending.attachment_ids, state.attachment_ids) || !sameTaskIntent(ordinaryPending.task_intent, state.task_intent)));
    ui.send.disabled = state.sending || state.creating || state.backendChoosing || state.page !== "conversations" || state.selected.type === "history" && state.historyReadOnly || !ordinaryReceiptOnly && (!draftHasContent() || currentUploadCount() || fileBlocked || !available || state.attachment_ids.length && modelCapabilityInfo()?.image_input === "unsupported" || !sourceCanContinue() || isRunning() || historyLoading || contextCannotSend() || contextPinSavingCurrent());
    ui.sendLabel.textContent = state.sending ? "发送中" : ordinaryReceiptOnly ? "确认上次发送" : "发送";
    if (nativeHistory) {
      ui.send.disabled = !draftHasContent() || !nativeAdoptionSource() || !nativeAdoption || fileMode || fileInputsUnready() || state.sending || state.creating || state.backendChoosing || currentUploadCount() > 0;
      ui.sendLabel.textContent = draftHasContent() ? "继续原对话" : "发送";
    }
    const stopping = !nativeHistory && state.page === "conversations" && state.selected.type === "chat" && typeof state.run?.id === "string" && !!state.run.id && isRunning() && !ordinaryPending?.uncertain;
    composerStopTarget = stopping ? { runId: state.run.id, chatId: state.selected.id, version: state.openVersion, workspace: state.status?.workspace_id || null } : null;
    ui.send.dataset.composerAction = stopping ? "stop" : "send";
    if (stopping) {
      const stopPending = state.cancelling || state.run.cancel_requested === true || state.run.status === "cancelling";
      ui.sendLabel.textContent = stopPending ? "停止中" : "停止";
      ui.send.disabled = state.sending || state.creating || state.backendChoosing || stopPending;
    }
    if (nativeHome && !ui.input.value.trim()) ui.send.disabled = true;
    ui.send.setAttribute("aria-label", stopping ? "停止当前回答" : ui.sendLabel.textContent);
    ui.send.setAttribute("title", stopping ? "停止当前回答；保留已收到的内容" : ui.sendLabel.textContent);
    const submitIcon = ui.send.querySelector("svg path");
    if (submitIcon) submitIcon.setAttribute("d", stopping ? "M7 7h10v10H7z" : "M12 19V5m-6 6 6-6 6 6");
    ui.input.disabled = state.sending || state.creating;
    ui.contextButton.disabled = nativeHistory || state.sending || state.creating || currentUploadCount() > 0 || historyLoading || state.contextState === "loading";
    ui.contextButton.hidden = nativeHistory || !!nativeHome;
    if (nativeHistory || nativeHome) { state.contextVisible = false; ui.contextPanel.hidden = true; ui.contextButton.setAttribute("aria-expanded", "false"); }
    if (!state.status) {
      ui.hint.textContent = "正在连接这台电脑…";
      ui.footnote.textContent = "连接完成后就可以发送。";
    } else if (!profileReady()) {
      ui.hint.textContent = state.profileSelectionSync ? "聊天配置待核对" : state.profile_snapshot ? "这份 AI 配置需要确认" : "请选择一份 AI 配置";
      const row = state.profile_snapshot && profiles?.get(state.profile_snapshot.ai_profile.id), memory = state.profile_snapshot?.memory_profile.id !== "none" && profiles?.getMemory(state.profile_snapshot?.memory_profile.id);
      const expired = row && row.revision !== state.profile_snapshot.ai_profile.revision || memory && memory.revision !== state.profile_snapshot.memory_profile.revision;
      ui.footnote.textContent = state.profileSelectionSync ? "另一入口的配置尚未采用；草稿和材料保留，发送前需核对选择及配置目录。" : expired ? "当前草稿使用旧配置；发送前请明确选择最新配置。" : plain(memory && !memory.available ? memory.reason : row?.reason) || (state.profile_snapshot ? "所选配置暂时不可用，草稿保留；请在管理检查或重新选择配置。" : "请选择一份已接入的 AI 配置；历史和草稿会保留。");
    } else if (!available) {
      ui.hint.textContent = state.model_id ? "所选模型暂时不能回答" : `${backendLabels[state.backend]} 暂时不能回答`;
      ui.footnote.textContent = modelUnavailableReason();
    } else if (isRunning()) {
      ui.hint.textContent = "可以先写下一条，等回答完成再发送";
      ui.footnote.textContent = "需要修改问题时，可以先停止这一轮。";
    } else {
      ui.hint.textContent = state.preferences.send_shortcut === "ctrl-enter" ? "⌘/Ctrl+Enter 发送 · Enter 换行" : "Enter 发送 · Shift+Enter 换行";
      ui.footnote.textContent = state.selected.type === "history" ? nativeAdoptionEnabled(state.selected.agent)
        ? "继续原生会话可点上方“继续原对话”；直接发送会用文字副本接续，可换用其他 AI。"
        : state.historyContinuationIds.has(state.selected.id) ? "将使用已有共同聊天的前文；原来源后来新增的文字需明确补入。" : "发送时会带入这段历史，在小白兔接着聊；原记录保留。" : "";
    }
    if (contextCannotSend()) {
      const blocked = contextBlockCopy();
      ui.hint.textContent = blocked.hint; ui.footnote.textContent = blocked.footnote;
    } else if (contextPinSavingCurrent()) ui.hint.textContent = "正在保存必带前文，文字仍保留";
    if (!sourceCanContinue()) { ui.hint.textContent = "这段来源只能阅读和引用"; ui.footnote.textContent = sourceCoverageNotice(); }
    if (nativeHistory) {
      ui.hint.textContent = fileMode ? "先取消“生成文件”，再继续原对话" : currentUploadCount() ? "正在添加材料…" : "进入原生对话后发送";
      ui.footnote.textContent = "沿用原 Agent 的配置与上下文；可先写问题、添加材料，再继续原对话。";
    }
    if (nativeHome && available) { ui.hint.textContent = state.preferences.send_shortcut === "ctrl-enter" ? "⌘/Ctrl+Enter 发送 · Enter 换行" : "Enter 发送 · Shift+Enter 换行"; ui.footnote.textContent = ""; }
    const imageInfo = nativeHistory || nativeHome ? { image_input: ["claude", "dsh"].includes(nativeHome?.agent || state.selected.agent) ? "unsupported" : "unknown" } : modelCapabilityInfo(), hasImages = state.attachment_ids.length || state.chatMessages.some(message => message.attachment_ids?.length);
    $("image-model-status").hidden = !hasImages;
    $("image-model-status").textContent = !hasImages ? "" : imageInfo?.image_input === "supported" ? "所选连接支持图像输入；本轮带图数量可在上下文中检查。" : imageInfo?.image_input === "unsupported" ? "所选连接暂不支持图像输入。图片和草稿保留，可选择支持图片的模型。" : "所选连接的图像能力尚未确认。图片保留，预览或发送时会检查，不会自动换模型。";
    ui.footnote.hidden = !ui.footnote.textContent;
    $("add-image").disabled = fileMode || imageInfo?.image_input === "unsupported" || !materials || !state.status?.materials?.enabled || state.sending || state.creating || currentUploadCount() > 0 || state.attachment_ids.length >= Math.min(4, state.status?.materials?.max_message_images || 4);
    const profileUnresolved = state.selected.type === "chat" && !!state.profileSelectionSync;
    $("ai-profile-label").textContent = profileUnresolved ? "配置待核对" : nativeHistory ? `${backendLabels[state.selected.agent] || state.selected.agent} · 原会话` : nativeHome ? nativeHome.profile?.name || `${backendLabels[nativeHome.agent]} · 原生` : state.profile_snapshot?.ai_profile.name || (profilesEnabled() ? "选择配置" : `${backendLabels[state.backend]} · 原有配置`);
    if (state.selected.type === "new" && state.homeTextCompatibility) $("ai-profile-label").textContent += " · 文字兼容";
    const memory = draftMemoryProfile();
    const requested = nativeHistory || profileUnresolved ? null : nativeHome ? nativeHome.profile : state.profile_snapshot?.ai_profile;
    const currentProfile = requested && profiles?.get(requested.id);
    const effective = currentProfile?.revision === requested?.revision ? currentProfile : null;
    window.RabbitProfiles?.renderStatus($("conversation-config-status"), {
      agent: backendLabels[nativeHistory ? state.selected.agent : state.backend],
      device: state.management?.device?.name,
      model: nativeHome ? requested?.model_id || null : effective?.effective_model || requested?.model_id || (nativeHistory || requested || profileUnresolved ? null : modelInfo()?.model || null),
      effort: nativeHome ? requested?.reasoning_effort : effective?.effective_effort || requested?.reasoning_effort,
      memory: !profileUnresolved && memory.id !== "none" ? [memory.name, memory.mode === "read_only" ? "只读" : "读取并记住"].filter(Boolean).join(" · ") : null,
      status: !state.status ? "正在连接" : state.sending || isRunning() ? "正在回复" : state.pendingSubmission?.uncertain ? "发送待核对" : !profileReady() || !available ? "配置待核对" : "空闲",
      language: window.RabbitWorkspace?.language?.() || "zh-CN",
    });
    $("memory-profile-label").textContent = profileUnresolved ? "配置待核对" : memory.id !== "none" && memory.name || (nativeHistory || nativeHome ? "不额外带入记忆" : "不使用长期记忆");
    $("choose-ai-profile").disabled = !profilesEnabled() || !profiles || !state.profilesInventory || state.sending || state.creating || state.backendChoosing;
    $("choose-memory-profile").disabled = !profilesEnabled() || !profiles || !state.profilesInventory || state.sending || state.creating || state.backendChoosing;
    if (state.nativeHomeSending) {
      ui.send.disabled = true; ui.sendLabel.textContent = "发送中";
      for (const id of ["choose-ai-profile", "choose-memory-profile", "add-text-file", "pick-text-file", "add-image", "toggle-file-task"]) $(id).disabled = true;
    }
    if (state.selected.type === "new" && !nativeHome && !state.homeTextCompatibility && state.status && state.status.demo_mode !== true && state.profilesInventory
        && ![state.status.native_chats, state.status.hermes_native_chats, state.status.pi_native_chats, state.status.claude_native_chats, state.status.dsh_native_chats].some(entry => entry?.enabled === true)) {
      ui.send.disabled = true;
      const connect = el("button", "context-preview-button", "先接入一个 Agent"); connect.type = "button";
      connect.addEventListener("click", () => openManual("connect-agent"));
      ui.hint.replaceChildren(connect); ui.footnote.hidden = true;
    }
    renderComposerMaterials(); renderFileMaterialStatus();
    updateRoute();
    updateDocumentDraftAction();
    if (historyQuotes) historyQuotes.refresh();
    $("quote-history").disabled = !historyQuotes || state.page !== "conversations" || state.sending || state.creating || currentUploadCount() > 0 || ui.input.disabled;
    if ($("native-handoff")) {
      const commonSource = state.selected.type === "chat" && state.chat?.id === state.selected.id;
      $("native-handoff").hidden = !nativeChatsEnabled() || !commonSource && !historyTextSource();
      $("native-handoff").disabled = state.page !== "conversations" || !commonSource && !historyTextSource()
        || state.sending || state.creating || currentUploadCount() > 0;
    }
    if ($("native-adopt")) {
      const supported = nativeAdoptionEnabled(state.selected.agent);
      $("native-adopt").hidden = state.page !== "conversations" || state.selected.type !== "history" || !supported || nativeHistory && draftHasContent();
      $("native-adopt").textContent = "继续原对话";
      $("native-adopt").disabled = !nativeAdoptionSource() || !nativeAdoption;
    }
    if (state.page === "conversations" && state.selected.type === "history" && state.historyReadOnly) { ui.composer.hidden = true; $("native-adopt").hidden = true; }
  }
  async function refreshStatus() {
    const data = await api("/api/status?cached=1");
    const previousScope = state.status?.workspace_id;
    state.status = data;
    if (previousScope && previousScope !== data.workspace_id) {
      historySourceSerial++; historySourcePending = false; historySourceCheckedAt = 0; historySourceState = null;
      checkHistorySources();
    }
    $("conversation-folder-tools").hidden = !markdownChats?.enabled();
    conversationGroups?.setEnabled(markdownChats?.enabled() === true);
    const demoNotice = $("demo-mode-banner");
    if (demoNotice) demoNotice.hidden = data.demo_mode !== true;
    state.csrf = data.csrf_token || state.csrf;
    updateSpecialistAvailability();
    updateNativeChatsAvailability();
    renderList();
    if (!browserStore) beginBrowserRestore();
    if (profilesEnabled() && profiles && profileDirectoryNeeded()) await profiles.load();
    $("connection-state").textContent = "";
    updateComposer();
    updateHistorySummary();
    return data;
  }
  function updateHistorySummary() {
    const control = $("refresh-history");
    if (control.disabled) return;
    const history = state.status?.history;
    const changed = historySourceState === "changed";
    const summary = changed ? "原对话可能有更新"
      : historySourceState === "unknown" && history?.built_at ? "原对话变化待核对"
      : history?.built_at ? `最近读取 ${shortDate(history.built_at)}`
      : "原来的对话，保留原文";
    const details = [summary];
    if (state.hiddenInternal) details.push(`还有 ${state.hiddenInternal} 条内部记录`);
    if (history?.notice) details.push(history.notice);
    control.textContent = changed ? "可能有更新 ↻" : "更新";
    control.setAttribute("aria-label", `更新原对话。${summary}`);
    control.title = details.join("\n");
  }
  // Compare source metadata only when entering or returning to the app; never
  // rebuild the index in the background or infer that an unknown is current.
  let historySourceState = null, historySourceSerial = 0, historySourceCheckedAt = 0, historySourcePending = false;
  async function checkHistorySources(force = false) {
    const scope = state.status?.workspace_id;
    if (!scope || document.hidden || !force && (historySourcePending || Date.now() - historySourceCheckedAt < 10000)) return;
    const serial = ++historySourceSerial; historySourcePending = true; historySourceCheckedAt = Date.now();
    try {
      const result = await api("/api/history/source-changes", {}, false);
      if (serial !== historySourceSerial || scope !== state.status?.workspace_id) return;
      historySourceState = result && Object.keys(result).sort().join(",") === "state,workspace_id" && result.workspace_id === scope
        && ["changed", "unchanged", "unknown"].includes(result.state) ? result.state : "unknown";
    } catch (_) { if (serial === historySourceSerial && scope === state.status?.workspace_id) historySourceState = "unknown"; }
    finally { if (serial === historySourceSerial) { historySourcePending = false; updateHistorySummary(); } }
  }
  async function refreshChats(shouldApply = null, options = {}) {
    return loadLibrary(true, shouldApply, options, true);
  }
  // One projection of owned connection facts. Reuse the existing page refresh;
  // missing or stale observations never mean an Agent is idle.
  let nativeWorkRead = 0, nativeWorkFlight = null;
  function acceptNativeWorkStatus(observation) {
    const scope = state.status?.workspace_id, age = Date.now() - observation?.received_at;
    const next = observation && !document.hidden && observation.workspace_id === scope && observation.scope === "rabbit_owned_cache"
      && Number.isFinite(age) && age >= 0 && age < 6000 ? observation : null;
    const badges = new Map();
    for (const row of next?.works || []) {
      if (row.role !== "chat" || !row.complete || row.reason !== null || row.connection_state !== "ready") continue;
      if (row.status === "needs_user" || row.status === "running") badges.set(`${row.agent}/${row.chat_id}`, row.status);
    }
    const signature = JSON.stringify([...badges].sort());
    const previous = state.nativeWorkStatus;
    state.nativeWorkStatus = next ? { workspace: scope, receivedAt: next.received_at, badges, signature } : null;
    if ((previous?.signature || "[]") !== signature || previous && previous.workspace !== scope) renderList();
  }
  function nativeWorkBadge(item) {
    const observed = state.nativeWorkStatus, age = Date.now() - observed?.receivedAt;
    if (item.kind !== "native" || !observed || observed.workspace !== state.status?.workspace_id
        || state.libraryWorkspace !== observed.workspace || document.hidden || age < 0 || age >= 6000) return "";
    return observed.badges.get(`${item.agent}/${item.id}`) || "";
  }
  function readNativeWorkStatus() {
    if (document.hidden || !state.status?.workspace_id || typeof window.RabbitWorkStatus?.snapshot !== "function") return;
    if (state.page === "management" && state.workspaceSections.management === "agents") return;
    if (Date.now() - state.nativeWorkStatus?.receivedAt >= 6000) acceptNativeWorkStatus(null);
    if (nativeWorkFlight) return;
    const scope = state.status.workspace_id, request = ++nativeWorkRead;
    const controller = new AbortController(); let expired = false;
    const valid = () => !expired && !controller.signal.aborted && request === nativeWorkRead && scope === state.status?.workspace_id && !document.hidden
      && !(state.page === "management" && state.workspaceSections.management === "agents");
    const timeout = setTimeout(() => { const current = valid(); expired = true; controller.abort(); if (current) acceptNativeWorkStatus(null); }, 2500);
    nativeWorkFlight = Promise.resolve().then(() => valid() ? api("/api/management/work-status", { signal: controller.signal }, false) : null)
      .then(value => { if (valid() && value) acceptNativeWorkStatus({ ...window.RabbitWorkStatus.snapshot(value, scope), workspace_id: scope, scope: "rabbit_owned_cache", received_at: Date.now() }); })
      .catch(() => { if (valid()) acceptNativeWorkStatus(null); })
      .finally(() => { clearTimeout(timeout); nativeWorkFlight = null; });
    return nativeWorkFlight;
  }
  function switchListView() {
    if (state.selected.type === "chat" && !state.chat && !state.chatLoading) return openChat(state.selected.id);
    if (state.selected.type === "history" && !state.historySession) return openHistory(state.selected.agent, state.selected.sessionId);
    showPage("conversations");
  }
  let listSearchMatch = null, listSearchNote = null;
  function appendListSearchMatch(nativeMount = null) {
    listSearchNote?.remove(); listSearchNote = null;
    const item = listSearchMatch;
    if (!item || item.workspace_id !== state.status?.workspace_id) return;
    const native = item.kind === "native" && state.page === "native-chats" ? nativeChats?.getViewContext() : null;
    const current = item.kind === "native" ? native?.agent === item.agent && native?.chat_id === item.id && native?.workspace_id === item.workspace_id
      : state.page === "conversations" && state.selected.type === "chat" && state.selected.id === item.id;
    if (!current || item.kind === "native" && !nativeMount) return;
    const english = window.RabbitWorkspace?.language?.() === "en";
    const note = el("p", "detail-notice list-search-match"); note.dataset.controlInterface = "ai";
    note.append(el("strong", "", english ? "Search excerpt" : "搜索命中"), el("span", "", " · " + item.excerpt),
      el("span", "", english ? " — Matched source record; not located in this conversation yet." : "——命中来源记录，尚未定位到本对话正文。"));
    const mount = nativeMount || ui.actions.querySelector(".conversation-more-actions");
    if (mount) mount.append(note); else ui.actions.append(headerMore([note]));
    listSearchNote = note;
  }
  function sessionListItem(item, index) {
    const isHistory = item.kind === "history", isNative = item.kind === "native", english = window.RabbitWorkspace?.language?.() === "en";
    const match = item.match;
    const target = match && match.message_id != null && Number.isInteger(match.index) ? { messageId: String(match.message_id), index: match.index, query: match.query || state.libraryQuery.trim() } : null;
    const sourceMatch = !isHistory && !isNative && match?.kind === "history";
    const indexed = isNative && item.indexed_match;
    const excerpt = indexed?.excerpt ?? match?.excerpt;
    const node = button("", "session-item", () => {
      listSearchMatch = (sourceMatch || indexed) && typeof excerpt === "string" && excerpt ? { workspace_id: state.status?.workspace_id, kind: item.kind, agent: item.agent || null, id: item.id, excerpt } : null;
      if (isNative) {
        state.browserPageTouched = true;
        if (!nativeChats?.openEntry) return;
        return nativeChats.openEntry({ agent: item.agent, chatId: item.id, workspaceId: state.status?.workspace_id || "" }, { readOnly: item.available !== true, title: item.title, match: indexed, excerpt })
          .catch(error => toast(userError(error, english ? "The native chat could not be opened. Drafts remain." : "原生对话暂时没打开，原草稿保留。")));
      }
      return isHistory ? openHistory(item.agent, item.session_id, target) : openChat(item.id, false, sourceMatch ? null : target);
    });
    const nativeSelection = isNative ? nativeChats?.getViewContext() : null;
    const active = isNative ? state.page === "native-chats" && nativeSelection?.agent === item.agent && nativeSelection?.chat_id === item.id
      : isHistory ? state.page === "conversations" && state.selected.type === "history" && state.selected.id === `${item.agent}:${item.session_id}` : state.page === "conversations" && state.selected.type === "chat" && state.selected.id === item.id;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "true");
    if (isNative) {
      node.disabled = !state.status?.workspace_id;
      if (item.available !== true) node.title = english ? "Read existing bound history; the Agent will not be connected." : "查看已绑定的历史，不连接 Agent。";
    }
    node.append(el("div", "session-title", item.title || "未命名的对话"));
    const meta = el("div", "session-meta");
    (item.source_agents || (item.agent ? [item.agent] : [])).forEach((agent) => meta.append(el("span", `source-tag ${agent}`, sidebarAgentLabel(agent))));
    const date = shortDate(isNative && item.timestamp_kind !== "confirmed_activity" ? item.created_at : item.timestamp);
    if (isNative) { meta.style.flexWrap = "wrap"; meta.append(el("span", "", english ? "Native" : "原生")); }
    if (date) {
      const when = el("span", "", isNative ? (item.timestamp_kind === "confirmed_activity" ? english ? "Recent activity " : "最近活动 " : item.timestamp_kind === "catalogued" ? english ? "Added " : "加入于 " : english ? "Created " : "创建于 ") + date : date);
      if (isNative && item.timestamp_kind === "confirmed_activity") when.title = english ? "The latest native request receipt confirmed by Rabbit." : "来自小白兔最近确认的原生请求回执。";
      meta.append(when);
    }
    if (typeof item.message_count === "number") meta.append(el("span", "", `${item.message_count} 条`));
    const workState = isNative ? nativeWorkBadge(item) : item.running ? "running" : "";
    if (workState) meta.append(el("span", "session-running", workState === "needs_user" ? english ? "Needs attention" : "需处理" : english ? "Replying" : "回答中"));
    if (!isHistory && !isNative && item.source_update && item.source_update.has_changes) meta.append(el("span", "session-source-change", "来源有变化"));
    if (item.archived) meta.append(el("span", "archived-mark", "已归档"));
    node.append(meta);
    if (isNative) node.append(el("div", "session-excerpt", (item.search_scope === "indexed_text" ? english ? "Includes indexed history" : "可搜历史索引" : english ? "Title search only" : "仅名称搜索") + (item.available === true ? "" : english ? " · Unavailable" : " · 暂不可用")));
    if (excerpt != null && !(isNative && match?.title_only && !indexed)) node.append(el("div", "session-excerpt", excerpt));
    if (isNative && item.available !== true) node.append(el("div", "session-excerpt", english ? "Read only" : "仅可阅读"));
    for (const text of node.children) text.dataset.controlInterface = "ai";
    node.dataset.listIndex = String(index);
    return groupedListItem(node, item);
  }
  function groupedListItem(content, item) {
    if (!conversationGroups || !markdownChats?.enabled()) return content;
    const row = el("div", "session-group-entry"), choose = button("⋯", "session-group-menu", () => conversationGroups.open(item));
    choose.setAttribute("aria-label", `将“${item.title || "未命名对话"}”加入对话夹`); choose.title = "加入对话夹";
    row.append(content, choose); return row;
  }
  function conversationSidebarTop() {
    return sidebarView === "conversations" ? ui.sidebarContent.scrollTop : sidebarScroll.conversations;
  }
  function setConversationSidebarTop(top) {
    sidebarScroll.conversations = top;
    if (sidebarView === "conversations") ui.sidebarContent.scrollTop = top;
  }
  function renderList() {
    const nodes = [];
    if (state.libraryItems.length) {
      nodes.push(el("p", "list-caption", `${state.libraryQuery.trim() ? "找到" : "共"} ${state.libraryTotal} 段对话`));
      state.libraryItems.forEach((item, index) => nodes.push(sessionListItem(item, index)));
    } else if (!state.historyLoading) nodes.push(el("div", "empty-list", state.libraryError || (state.libraryQuery.trim() ? "没有找到这些文字。" : conversationGroups?.selected() ? "这个对话夹还没有对话。" : "还没有可读取的对话。写下第一条消息，或点更新读取原有记录。")));
    if (state.historyLoading) nodes.push(el("p", "list-caption", "正在读取对话…"));
    else if (state.libraryNext != null) nodes.push(button("继续加载", "load-more", () => loadHistoryList(false)));
    if (state.libraryError && state.libraryItems.length) nodes.push(el("p", "list-caption", state.libraryError));
    const previousTop = conversationSidebarTop();
    ui.list.replaceChildren(...nodes);
    setConversationSidebarTop(previousTop);
  }
  async function loadLibrary(reset, shouldApply = null, options = {}, preserve = false, retry = 0) {
    if (!reset && state.historyLoading) return null;
    const version = ++state.listVersion;
    const baseRevision = state.libraryRevision;
    const query = state.libraryQuery.trim();
    const agent = state.historyAgent;
    const folder = conversationGroups?.selected() || null;
    const previousTop = conversationSidebarTop();
    const wanted = reset && preserve ? Math.max(50, state.libraryItems.length) : 50;
    if (reset && !preserve) { state.libraryItems = []; state.libraryNext = null; setConversationSidebarTop(0); }
    state.historyLoading = true; state.libraryError = ""; renderList();
    const current = () => version === state.listVersion && query === state.libraryQuery.trim() && agent === state.historyAgent && folder === (conversationGroups?.selected() || null) && (!shouldApply || shouldApply());
    let discarded = false;
    try {
      let offset = reset ? 0 : state.libraryNext || 0, data, items = [], pageRevision = null;
      do {
        const params = new URLSearchParams({ query, agent, offset: String(offset), limit: String(Math.min(100, wanted - items.length)), include_internal: state.includeInternal ? "1" : "0" });
        if (folder) params.set("folder", folder);
        data = await api(`/api/library?${params}`, options);
        if (!current()) { discarded = true; return null; }
        if (data.revision && ((pageRevision && data.revision !== pageRevision) || (!reset && baseRevision && data.revision !== baseRevision))) {
          if (retry >= 2) throw new Error("对话正在更新，请稍后再加载。");
          state.historyLoading = false;
          return await loadLibrary(true, shouldApply, options, true, retry + 1);
        }
        pageRevision = data.revision || pageRevision;
        items.push(...(Array.isArray(data.items) ? data.items : []));
        offset = data.next_offset;
      } while (reset && preserve && offset != null && items.length < wanted && data.items && data.items.length);
      state.libraryItems = reset ? items : [...state.libraryItems, ...items];
      state.libraryWorkspace = typeof data.workspace_id === "string" && data.workspace_id === state.status?.workspace_id ? data.workspace_id : "";
      state.libraryTotal = Number(data.total) || 0;
      state.libraryNext = data.next_offset == null ? null : data.next_offset;
      state.libraryRevision = data.revision ?? state.libraryRevision;
      state.libraryWatchRevision = data.base_revision ?? data.revision ?? state.libraryWatchRevision;
      state.chats = state.libraryItems.filter((item) => item.kind === "chat");
      return state.chats;
    } catch (error) {
      if (!current()) { discarded = true; return null; }
      state.libraryError = userError(error, "对话暂时没有读出来，请点更新再试。");
      throw error;
    } finally {
      if (version === state.listVersion) {
        state.historyLoading = false;
        if (!discarded) { renderList(); if (preserve) setConversationSidebarTop(previousTop); }
      }
    }
  }
  async function loadHistoryList(reset) {
    try { return await loadLibrary(reset); }
    catch (error) { toast(userError(error, "对话暂时没有读出来，请稍后再试。")); }
  }
  let specialistGuideTarget = null;
  let specialistLocationRevision = 0, specialistBinding = null;
  let specialistGuideGeneration = 0, specialistGuideRead = 0, specialistGuideSelection = null, specialistGuideVerification = "";
  let statusChecks = null, specialistGuideHistory = null, specialistHistoryRead = 0, specialistDshGuide = null, specialistDshRead = 0;
  async function readSpecialistGuideHistory(scope, isCurrent = () => true) {
    const serial = ++specialistHistoryRead;
    try {
      const value = await api("/api/management/model-guide-history", {}, false);
      if (scope !== state.status?.workspace_id || serial !== specialistHistoryRead || !isCurrent()) return;
      if (!value || Object.keys(value).sort().join(",") !== "catalogues,workspace_id" || value.workspace_id !== scope) throw Error();
      if (!value.catalogues || typeof value.catalogues !== "object" || Array.isArray(value.catalogues)) throw Error();
      const { dsh, ...native } = value.catalogues;
      const catalogues = window.RabbitProfiles.nativeCapabilities(native);
      if (!catalogues) throw Error();
      if (dsh !== undefined) try { catalogues.dsh = window.RabbitSpecialist.dshCatalogue(dsh); } catch (_) { /* Ignore malformed DSH history without hiding other agents. */ }
      specialistGuideHistory = { workspace_id: scope, catalogues };
      specialist?.updateGuideContext?.();
      return specialistGuideHistory;
    } catch (_) {
      if (scope === state.status?.workspace_id && serial === specialistHistoryRead && isCurrent()) specialist?.updateGuideContext?.();
    }
  }
  async function readSpecialistDshGuide(scope, isCurrent = () => true) {
    const serial = ++specialistDshRead;
    try {
      const value = await api("/api/management/dsh-model-guide", {}, false);
      if (scope !== state.status?.workspace_id || serial !== specialistDshRead || !isCurrent()) return;
      if (!value || Object.keys(value).sort().join(",") !== "catalogue,workspace_id" || value.workspace_id !== scope) throw Error();
      const catalogue = value.catalogue === null ? null : window.RabbitSpecialist.dshCatalogue(value.catalogue);
      specialistDshGuide = { workspace_id: scope, catalogue };
      specialist?.updateGuideContext?.(); return specialistDshGuide;
    } catch (_) {
      if (scope === state.status?.workspace_id && serial === specialistDshRead && isCurrent()) specialist?.updateGuideContext?.();
    }
  }
  const specialist = window.RabbitSpecialist ? window.RabbitSpecialist.create({
    root: $("specialist-panel"),
    composerRoot: $("specialist-composer-mount"), composerLabel: $("specialist-composer-label"), settingsRoot: settingsMounts.specialist,
    guideRoot: $("specialist-guide-panel"),
    guideText: readBundledHelp()?.guide || "", onOpenManual: openManual,
    getGuideContext: () => ({ catalog: profiles?.catalog(), history: specialistGuideHistory?.workspace_id === state.status?.workspace_id ? specialistGuideHistory : null, dsh: specialistDshGuide?.workspace_id === state.status?.workspace_id ? specialistDshGuide : null, target: specialistGuideTarget?.workspace_id === state.status?.workspace_id ? specialistGuideTarget : null, verification: specialistGuideVerification }),
    getCommandContext: captureSpecialistCommandContext,
    onDraftBindingChange: acceptSpecialistBinding, onCommandSettled: readSpecialistCommandResult,
    onPrepareCommand: prepareHomeCommand, onCommandObserved: event => homeCommandTarget(event) ? readHomeCommandResult(event) : true,
    onTurnSettled: refreshSpecialistGuide, onGuideCheckContext: specialistCheckContext,
    onStatusCheck: window.RabbitStatusChecks ? () => statusChecks?.start() : null,
    onModelCheck: checkSpecialistModels,
    // Native submissions and answers are never replayed by the common 403
    // retry path. Only an explicit check can reconcile an unknown receipt.
    api: (path, options = {}) => api(path, options, false),
    getWorkspace: () => state.status?.workspace_id || "", openDocument,
    onError: error => toast(userError(error, "专员请求尚未确认，草稿与原请求保留。")),
  }) : null;
  const statusReportMount = specialist?.getStatusReportMount?.();
  if (statusReportMount && window.RabbitStatusChecks) statusChecks = window.RabbitStatusChecks.create({
    root: statusReportMount, api: (path, options = {}) => api(path, options, false),
    getWorkspace: () => state.status?.workspace_id || "",
    getTarget: () => {
      const source = specialistGuideTarget?.workspace_id === state.status?.workspace_id ? specialistGuideTarget : null;
      if (!source) return {};
      return Object.fromEntries(["agent", "chat_id", "profile_id", "profile_revision", "memory_profile_id", "memory_profile_revision"]
        .filter(key => source[key] != null).map(key => [key, source[key]]));
    },
    onPublished: async report => {
      if (report.workspace_id !== state.status?.workspace_id) return;
      await profiles?.loadFresh(); specialist?.updateGuideContext?.();
    },
    onError: error => toast(userError(error, "状态报告尚未完整读回。")),
  });
  // An explicit guide action refreshes only existing native connections. Each
  // result is tied to the workspace and the connection identity just observed.
  async function checkSpecialistModels({ workspace_id: scope, isCurrent }) {
    const names = ["codex", "pi", "hermes", "claude", "dsh"], expected = new Map();
    const current = () => !!scope && scope === state.status?.workspace_id && typeof isCurrent === "function" && isCurrent();
    if (!current() || !profiles) return null;
    try {
      const result = await api("/api/management/specialist-model-catalog", {}, false);
      if (!current()) return null;
      if (result && typeof result === "object" && !Array.isArray(result)
          && Object.keys(result).sort().join(",") === "agent,native_catalog_ready,source,workspace_id"
          && result.workspace_id === scope && result.agent === "codex" && result.source === "specialist" && result.native_catalog_ready === true)
        expected.set("codex", null);
    } catch (_) { if (!current()) return null; }
    let rows = [];
    try {
      const list = await api("/api/management/native-model-connections", {}, false);
      if (!current()) return null;
      const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
      const counts = { codex: 0, pi: 0, hermes: 0, claude: 0, dsh: 0 }, seen = new Set();
      if (!list || typeof list !== "object" || Object.keys(list).sort().join(",") !== "agents,connections,workspace_id"
          || list.workspace_id !== scope || !Array.isArray(list.connections) || list.connections.length > 6
          || !list.agents || typeof list.agents !== "object" || Array.isArray(list.agents)
          || Object.keys(list.agents).sort().join(",") !== [...names].sort().join(",")
          || names.some(agent => !["observed", "unverified", "disabled"].includes(list.agents[agent]))
          || list.connections.some(row => {
            if (!row || typeof row !== "object" || Array.isArray(row) || Object.keys(row).sort().join(",") !== "agent,chat_id,epoch,title"
                || !names.includes(row.agent) || list.agents[row.agent] !== "observed" || typeof row.chat_id !== "string" || !uuid.test(row.chat_id)
                || typeof row.epoch !== "string" || !/^[0-9a-f]{32}$/.test(row.epoch)
                || typeof row.title !== "string" || !row.title.trim() || row.title.length > 160
                || /[\x00-\x1f\x7f\ud800-\udfff]/u.test(row.title)
                || seen.has(row.agent + ":" + row.chat_id) || ++counts[row.agent] > (row.agent === "codex" ? 2 : 1)) return true;
            seen.add(row.agent + ":" + row.chat_id); return false;
          })) throw Error("invalid connections");
      rows = list.connections;
    } catch (_) { if (!current()) return null; }
    for (const agent of names) {
      if (!current()) return null;
      const row = rows.find(item => item.agent === agent);
      if (!row) continue;
      try {
        const query = new URLSearchParams({ agent, chat_id: row.chat_id, epoch: row.epoch });
        const result = await api("/api/management/native-model-catalog?" + query, {}, false);
        if (!current()) return null;
        if (result && typeof result === "object" && !Array.isArray(result)
            && Object.keys(result).sort().join(",") === "agent,chat_id,epoch,native_catalog_ready,workspace_id"
            && result.workspace_id === scope && result.agent === agent && result.chat_id === row.chat_id
            && result.epoch === row.epoch && result.native_catalog_ready === true) expected.set(agent, row.chat_id);
      } catch (_) { if (!current()) return null; }
    }
    let catalog;
    try { catalog = await profiles.loadFresh(); } catch (_) { /* No readback means no verified update. */ }
    if (!current()) return null;
    const dshRead = expected.has("dsh") ? await readSpecialistDshGuide(scope, current) : null;
    if (!current()) return null;
    const updated = names.filter(agent => {
      const item = agent === "dsh" ? dshRead?.catalogue : catalog?.workspace_id === scope && catalog.native_profile_capabilities?.[agent];
      return expected.has(agent) && (agent === "dsh" ? !!item : item?.catalogue_available === true) && item.source_chat_id === expected.get(agent);
    });
    let saveFailed = false;
    if (updated.length) {
      const observations = updated.map(agent => { const item = agent === "dsh" ? dshRead.catalogue : catalog.native_profile_capabilities[agent]; return { agent, observed_at: item.observed_at, source_chat_id: item.source_chat_id }; });
      try {
        const saved = await api("/api/management/model-guide-history", { method: "POST", body: JSON.stringify({ workspace_id: scope, observations }) }, false);
        if (!current()) return null;
        const results = [...(Array.isArray(saved?.saved) ? saved.saved : []), ...(Array.isArray(saved?.unchanged) ? saved.unchanged : [])];
        if (!saved || Object.keys(saved).sort().join(",") !== "saved,unchanged,workspace_id" || saved.workspace_id !== scope
            || !Array.isArray(saved.saved) || !Array.isArray(saved.unchanged) || results.length !== updated.length
            || new Set(results).size !== results.length || results.some(agent => !updated.includes(agent))) throw Error();
        const history = await readSpecialistGuideHistory(scope, current);
        if (!current()) return null;
        saveFailed = observations.some(item => {
          const previous = history?.catalogues?.[item.agent], savedTime = Date.parse(previous?.observed_at), readTime = Date.parse(item.observed_at);
          return !Number.isFinite(savedTime) || !Number.isFinite(readTime) || savedTime < readTime
            || savedTime === readTime && (previous.source_chat_id !== item.source_chat_id
              || JSON.stringify(previous.models) !== JSON.stringify((item.agent === "dsh" ? dshRead.catalogue : catalog.native_profile_capabilities[item.agent]).models)
              || item.agent === "dsh" && previous.partial !== dshRead.catalogue.partial);
        });
      } catch (_) { if (!current()) return null; saveFailed = true; }
    }
    if (!current()) return null;
    specialist?.updateGuideContext?.();
    return { updated, unavailable: names.filter(agent => !updated.includes(agent)), unsupported: [], ...(dshRead?.catalogue?.partial && updated.includes("dsh") ? { partial: ["dsh"] } : {}), ...(saveFailed ? { saveFailed: true } : {}) };
  }
  // The current work identity is separate from the guide's descriptive snapshot.
  // It never reads conversation contents, resolves a name or opens an Agent.
  function specialistObserved(ai, memory, aiRevision = null, memoryRevision = null) {
    const pair = row => row?.id && row.id !== "none" && Number.isSafeInteger(row.revision) && row.revision > 0 ? [row.id, row.revision] : [null, null];
    const [aiId, aiVersion] = pair(ai), [memoryId, memoryVersion] = pair(memory);
    const revision = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
    return { ai_profile_id: aiId, ai_profile_revision: aiVersion, ai_selection_revision: revision(aiRevision),
      memory_profile_id: memoryId, memory_profile_revision: memoryVersion, memory_selection_revision: revision(memoryRevision) };
  }
  function currentSpecialistWorkContext() {
    const scope = state.status?.workspace_id;
    if (!scope) return null;
    const codec = window.RabbitSpecialistContext;
    if (!codec?.normalize) throw Error("当前操作对象组件尚未就绪；原指令保留。");
    if (state.page === "specialist") return specialistBinding?.workspace_id === scope ? specialistBinding.context : null;
    let target = { kind: "page", page: state.page === "continuation" ? "conversations" : state.page, section: ["management", "settings"].includes(state.page) ? state.workspaceSections[state.page] || null : null }, observed = null;
    if (state.page === "native-chats") {
      const native = nativeChats?.getConfigurationContext();
      if (native?.workspace_id === scope && agents.has(native.agent) && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(native.chat_id || "")) {
        target = { kind: "native", agent: native.agent, chat_id: native.chat_id };
        observed = specialistObserved({ id: native.profile_id, revision: native.profile_revision }, { id: native.memory_profile_id, revision: native.memory_profile_revision }, native.ai_selection_revision, native.memory_selection_revision);
      }
    } else if (state.page === "management") {
      const object = workspace?.getObject?.();
      if (object?.kind === "agent" && agents.has(object.id)) target = { kind: "agent_instance", agent_instance_id: agents.instanceId(object.id) };
      else if (object?.kind === "model" && object.id && agents.validModelId(object.id)) target = { kind: "model", model_id: object.id };
      else if (object?.kind === "memory") {
        const memory = profiles?.getMemory(object.id);
        if (memory && memory.id !== "none") { target = { kind: "memory_profile", profile_id: memory.id, profile_revision: memory.revision }; observed = specialistObserved(null, memory); }
      }
    } else if (state.page === "conversations") {
      if (state.selected.type === "new") target = state.homeConfiguration ? { kind: "new_chat", draft_id: state.homeConfiguration.draft_id, configuration_revision: state.homeConfiguration.revision } : { kind: "new_chat" };
      else if (state.selected.type === "chat" && state.chat?.id === state.selected.id && !state.chatLoading) target = { kind: "chat", chat_id: state.chat.id };
      else if (state.selected.type === "history" && state.historyReady && agents.has(state.selected.agent) && state.historySession?.session_id)
        target = { kind: "history", agent: state.selected.agent, session_id: state.historySession.session_id };
      if (target.kind !== "page") {
        const row = historyUsesNative() ? null : homeNativeChoice()?.profile || state.profile_snapshot?.ai_profile;
        const memory = historyUsesNative() ? null : draftMemoryProfile();
        const revision = target.kind === "chat" ? state.profileSelection?.revision : null;
        observed = specialistObserved(row, memory, revision, revision);
      }
    }
    return codec.normalize({ schema_version: target.kind === "new_chat" && target.draft_id ? 3 : 2, workspace_id: scope, target, observed });
  }
  function workTargetEqual(a, b) {
    return !!a && !!b && a.workspace_id === b.workspace_id && JSON.stringify(a.target) === JSON.stringify(b.target);
  }
  function workBinding(context, guide = "model", label, returnTo = context) {
    const codec = window.RabbitSpecialistContext, source = context?.target;
    const title = source?.kind === "chat" ? state.chat?.title : source?.kind === "history" ? state.historySession?.title
      : source?.kind === "native" ? nativeChats?.getConfigurationContext()?.title : ["agent_instance", "model", "memory_profile"].includes(source?.kind) ? workspace?.getObjectTitle?.() : null;
    const name = Array.from(String(label || title || codec.label(context)).replace(/[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]/gu, " ").trim()).slice(0, 160).join("") || "未指定对象";
    return codec.normalizeBinding({ workspace_id: state.status.workspace_id, context, label: name, guide, return_to: returnTo });
  }
  function captureSpecialistCommandContext() {
    const context = currentSpecialistWorkContext(), scope = state.status?.workspace_id;
    const target = state.page === "specialist" && specialistBinding?.workspace_id === scope ? specialistBinding
      : scope ? workBinding(context, state.page === "management" && state.workspaceSections.management !== "models" ? "agent" : "model") : null;
    return { context, target, isCurrent: () => !!scope && scope === state.status?.workspace_id };
  }
  function acceptSpecialistBinding(value, command = null) {
    if (value === null) value = specialist?.getGuideBinding?.() || null;
    if (value === null) return; // Keep the last explicit target for the guide and return link.
    const binding = value === null ? null : window.RabbitSpecialistContext.normalizeBinding(value);
    if (binding && binding.workspace_id !== state.status?.workspace_id) return;
    if (command) void restoreHomeCommandDisplay(command).catch(() => {});
    if (JSON.stringify(binding) === JSON.stringify(specialistBinding)) return;
    specialistBinding = binding; specialistGuideGeneration++; specialistGuideRead++; specialistGuideSelection = null; specialistGuideVerification = "";
    const context = binding?.context, target = context?.target, observed = context?.observed;
    const home = target?.kind === "new_chat" && state.homeConfiguration?.draft_id === target.draft_id && state.homeConfiguration.revision === target.configuration_revision ? state.homeNativeChoice?.profile : null;
    const exactHome = home?.id === observed?.ai_profile_id && home?.revision === observed?.ai_profile_revision ? home : null;
    const row = target?.kind === "ai_profile" ? profiles?.get(target.profile_id) : profiles?.get(observed?.ai_profile_id) || exactHome;
    const exactRow = row?.revision === (target?.kind === "ai_profile" ? target.profile_revision : observed?.ai_profile_revision) ? row : null;
    const native = target?.kind === "native" && state.page === "native-chats" ? nativeChats?.getConfigurationContext() : null;
    const exactNative = native?.workspace_id === binding?.workspace_id && native?.agent === target?.agent && native?.chat_id === target?.chat_id
      && observed?.ai_profile_id && native?.profile_id === observed.ai_profile_id && native?.profile_revision === observed.ai_profile_revision ? native : null;
    specialistGuideTarget = binding ? { workspace_id: binding.workspace_id, title: binding.label,
      ...(target?.kind === "native" ? { agent: target.agent, chat_id: target.chat_id, agent_instance_id: "local/" + target.agent } : {}),
      ...(target?.kind === "chat" ? { chat_id: target.chat_id } : {}),
      ...(exactRow ? { agent_instance_id: exactRow.agent_instance_id, profile_id: exactRow.id, profile_name: exactRow.name, profile_revision: exactRow.revision, model_id: exactRow.model_id, reasoning_effort: exactRow.reasoning_effort }
        : exactNative ? Object.fromEntries(["agent_instance_id", "profile_id", "profile_name", "profile_revision", "model_id", "reasoning_effort"].map(key => [key, exactNative[key]])) : {}),
      memory_profile_id: target?.kind === "memory_profile" ? target.profile_id : observed?.memory_profile_id || null,
      memory_profile_revision: target?.kind === "memory_profile" ? target.profile_revision : observed?.memory_profile_revision || null } : null;
    specialist?.updateGuideContext?.();
    if (state.page === "specialist") { if (binding) specialist?.openGuide?.(binding.guide); workspaceHeader(); }
  }
  function openSpecialistGuide(binding) {
    if (!specialistEnabled()) { toast("专员尚未配置，可以在管理里手动调整命名配置。"); return false; }
    const value = window.RabbitSpecialistContext.normalizeBinding(binding);
    if (value.workspace_id !== state.status?.workspace_id) return false;
    const accepted = specialist.bindTarget?.(value) === true;
    const actual = specialist.getDraftBinding?.();
    if (actual !== undefined) acceptSpecialistBinding(actual);
    showPage("specialist", actual?.guide || specialistBinding?.guide || value.guide);
    return accepted;
  }
  function askSpecialistForConfiguration({ kind, profile: named } = {}) {
    try {
      const returnTo = currentSpecialistWorkContext();
      if (!named) return openSpecialistGuide(workBinding(returnTo, kind === "memory" ? "agent" : "model"));
      const row = kind === "memory" ? profiles?.getMemory(named.id) : profiles?.get(named.id);
      if (!row || row.id === "none" || row.revision !== named.revision) throw Error("配置已变化，请刷新后重新选择。");
      const context = window.RabbitSpecialistContext.normalize({ schema_version: 2, workspace_id: state.status.workspace_id,
        target: { kind: kind === "memory" ? "memory_profile" : "ai_profile", profile_id: row.id, profile_revision: row.revision },
        observed: specialistObserved(kind === "memory" ? null : row, kind === "memory" ? row : null) });
      return openSpecialistGuide(workBinding(context, kind === "memory" ? "agent" : "model", (kind === "memory" ? "记忆配置 · " : "AI 配置 · ") + row.name, returnTo));
    } catch (error) { toast(userError(error, "调整对象尚未确认，原草稿保留。")); return false; }
  }
  async function returnFromSpecialist() {
    const context = specialistBinding?.return_to;
    if (!context || context.workspace_id !== state.status?.workspace_id) return;
    const target = context.target;
    try {
      if (target.kind === "page") showPage(target.page, target.section);
      else if (target.kind === "native") await nativeChats?.selectExisting({ workspaceId: context.workspace_id, agent: target.agent, chatId: target.chat_id });
      else if (target.kind === "chat") { if (state.selected.type === "chat" && state.selected.id === target.chat_id) showPage("conversations"); else await openChat(target.chat_id); }
      else if (target.kind === "history") { if (state.selected.type === "history" && state.selected.agent === target.agent && state.historySession?.session_id === target.session_id) showPage("conversations"); else await openHistory(target.agent, target.session_id); }
      else if (target.kind === "new_chat") { if (state.selected.type === "new") showPage("conversations"); else newChat(); }
      else if (target.kind === "agent_instance" || target.kind === "model") await openMyAIAction({ kind: "manage", target: target.kind === "model" ? { kind: "model", id: target.model_id } : { kind: "agent", id: agents.backendOfInstance(target.agent_instance_id) } });
      else if (target.kind === "memory_profile") await openMyAIAction({ kind: "manage", target: { kind: "memory", id: target.profile_id } });
      if (target.kind === "chat") await readSpecialistConfiguration({ ...specialistBinding, context }, () => context.workspace_id === state.status?.workspace_id && state.page === "conversations" && workTargetEqual(context, currentSpecialistWorkContext()));
    } catch (error) { toast(userError(error, "原页面暂时无法打开，专员草稿保留。")); }
  }
  const homeCommandOwners = new Map();
  function homeCommandTarget(event) {
    const context = event?.target?.context;
    return context?.schema_version === 3 && context.target?.kind === "new_chat" ? context.target : null;
  }
  function homeCommandIdentity(event) {
    const target = homeCommandTarget(event);
    if (!target || event.workspace_id !== state.status?.workspace_id || event.target.workspace_id !== event.workspace_id
        || !/^specialist\/ui\/[A-Za-z0-9_.-]{1,100}$/.test(event.client_id || "")) throw Error("首页设置请求对象已变化。");
    return { client_id: event.client_id, draft_id: target.draft_id, configuration_revision: target.configuration_revision };
  }
  function homeCommandOwner(event, create = false) {
    const identity = homeCommandIdentity(event), key = "rabbit.home-configuration-owner.v1." + event.workspace_id;
    if (!homeCommandOwners.has(key)) {
      let value = null;
      try {
        const navigation = window.performance?.getEntriesByType?.("navigation")?.[0]?.type;
        if (!window.opener && ["reload", "back_forward"].includes(navigation)) value = JSON.parse(window.sessionStorage.getItem(key) || "null");
        else window.sessionStorage.removeItem(key);
      } catch (_) { /* Unknown ownership remains pending, never taken from local recovery. */ }
      homeCommandOwners.set(key, value);
    }
    const stored = homeCommandOwners.get(key);
    if (!create) return stored && Object.keys(stored).length === 4 && Object.entries(identity).every(([key, value]) => stored[key] === value)
      && /^[a-f0-9]{64}$/.test(stored.owner_secret || "") ? stored.owner_secret : null;
    const bytes = new Uint8Array(32); crypto.getRandomValues(bytes);
    const value = { ...identity, owner_secret: Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("") };
    const encoded = JSON.stringify(value); window.sessionStorage.setItem(key, encoded);
    if (window.sessionStorage.getItem(key) !== encoded) throw Error("浏览器不能保存这次设置请求，尚未发送。");
    homeCommandOwners.set(key, value); return value.owner_secret;
  }
  async function homeProposalRequest(event, operation, extra = {}) {
    const identity = homeCommandIdentity(event);
    const value = await api("/api/management/draft-configuration", { method: "POST", body: JSON.stringify({ operation, ...identity, ...extra }) }, false);
    const exact = (item, keys) => item && typeof item === "object" && !Array.isArray(item) && Object.keys(item).length === keys.length && keys.every(key => Object.hasOwn(item, key));
    const parameters = row => exact(row, ["model_id", "reasoning_effort"]) && agents.validModelId(row.model_id, "codex")
      && (row.reasoning_effort === null || ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"].includes(row.reasoning_effort));
    if (!exact(value, ["workspace_id", "scope", "client_id", "draft_id", "configuration_revision", "base_profile", "parameters", "proposal", "state", "application", "native_turn"])
        || value.workspace_id !== event.workspace_id || value.scope !== "home_configuration_proposal" || Object.entries(identity).some(([key, item]) => value[key] !== item)
        || !parameters(value.parameters) || !["registered", "pending", "applied", "rejected"].includes(value.state)) throw Error("首页设置回执无法核对。");
    const base = window.RabbitProfiles.profile(value.base_profile, false);
    if (!exact(value.base_profile, ["id", "revision", "name", "agent_instance_id", "device_id", "model_id", "reasoning_effort"]) || base.agent_instance_id !== "local/codex" || base.id === identity.draft_id) throw Error("首页设置基值无法核对。");
    if (value.proposal !== null && (!exact(value.proposal, ["parameters", "sha256"]) || !parameters(value.proposal.parameters)
        || !/^[a-f0-9]{64}$/.test(value.proposal.sha256))) throw Error("首页设置建议无法核对。");
    if (value.native_turn !== null && (!exact(value.native_turn, ["thread_id", "turn_id"]) || ![value.native_turn.thread_id, value.native_turn.turn_id].every(item => typeof item === "string" && !!item && item.length <= 256))) throw Error("原生请求关联无法核对。");
    if (value.state === "registered" ? value.proposal !== null || value.application !== null || value.native_turn !== null
        : value.proposal === null || value.native_turn === null || value.state === "pending" && value.application !== null) throw Error("首页设置回执状态不完整。");
    if (["applied", "rejected"].includes(value.state)) {
      if (!exact(value.application, ["result", "applied_revision", "reason"]) || value.application.result !== value.state) throw Error("首页设置结果不完整。");
      window.RabbitBrowserState.homeConfigurationReceipt({ ...identity, proposal_sha256: value.proposal.sha256, ...value.application });
    }
    if (value.native_turn && (event.thread_id && event.thread_id !== value.native_turn.thread_id || event.turn_id && event.turn_id !== value.native_turn.turn_id)) throw Error("设置建议不属于原来的专员请求。");
    value.base_profile = base; value.parameters = { model_id: value.parameters.model_id, reasoning_effort: value.parameters.reasoning_effort };
    if (value.application) value.application = { result: value.application.result, applied_revision: value.application.applied_revision, reason: value.application.reason };
    if (value.proposal) {
      value.proposal = { parameters: { model_id: value.proposal.parameters.model_id, reasoning_effort: value.proposal.parameters.reasoning_effort }, sha256: value.proposal.sha256 };
      const json = JSON.stringify({ model_id: value.proposal.parameters.model_id, reasoning_effort: value.proposal.parameters.reasoning_effort });
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(json))), byte => byte.toString(16).padStart(2, "0")).join("");
      if (digest !== value.proposal.sha256) throw Error("首页设置建议指纹不一致。");
    }
    return value;
  }
  function homeProposalCurrent(event) { return event.workspace_id === state.status?.workspace_id && (typeof event.isCurrent !== "function" || event.isCurrent()); }
  async function prepareHomeCommand(event) {
    if (!homeCommandTarget(event)) return true;
    const identity = homeCommandIdentity(event), choice = state.homeNativeChoice, base = choice?.local_base || choice?.profile;
    if (choice?.agent !== "codex" || !base || base.id === state.homeConfiguration?.draft_id || !profiles?.get(base.id)) return true;
    if (state.homeConfiguration?.draft_id !== identity.draft_id || state.homeConfiguration.revision !== identity.configuration_revision || state.homeNativeHandoff || state.nativeHomeSending)
      throw Error("首页配置或发送状态已变化，请把这条指令改为当前对象后再发送。");
    const latest = window.RabbitProfiles.profile(profiles.get(base.id), false);
    if (JSON.stringify(latest) !== JSON.stringify(base)) throw Error("命名配置已变化，请重新选择后发送这条指令。");
    const owner = homeCommandOwner(event) || homeCommandOwner(event, true), parameters = { model_id: choice.profile.model_id, reasoning_effort: choice.profile.reasoning_effort };
    const value = await homeProposalRequest(event, "register", { owner_secret: owner, base_profile_id: base.id, base_profile_revision: base.revision, parameters });
    if (!homeProposalCurrent(event) || JSON.stringify(value.base_profile) !== JSON.stringify(base) || JSON.stringify(value.parameters) !== JSON.stringify(parameters)) throw Error("首页设置登记未核实，原指令保留。");
    return true;
  }
  function writeHomeConfiguration(parameters, receipt = null) {
    // Only Home's independent choice changes. Another page keeps its own composer.
    const previous = { identity: state.homeConfiguration, choice: state.homeNativeChoice, receipt: state.homeConfigurationReceipt,
      snapshot: state.profile_snapshot, model: state.model_id, backend: state.backend, remembered: state.profileChoices.get("__new__"), hasRemembered: state.profileChoices.has("__new__") };
    const identity = nextHomeConfiguration(), choice = state.homeNativeChoice, base = choice.local_base || choice.profile;
    const profile = window.RabbitProfiles.profile({ ...base, id: identity.draft_id, revision: identity.revision, name: "本草稿设置", ...parameters }, false);
    state.homeConfiguration = identity; state.homeNativeChoice = { ...choice, profile, local_base: base };
    if (receipt) state.homeConfigurationReceipt = window.RabbitBrowserState.homeConfigurationReceipt(receipt);
    const snapshot = toProfileSnapshot(profile, choice.memory); state.profileChoices.set("__new__", snapshot);
    const visibleHome = state.page === "conversations" && state.selected.type === "new";
    if (visibleHome) { state.profile_snapshot = snapshot; state.model_id = profile.model_id; state.backend = choice.agent; }
    if (checkpointBrowserState() !== true) {
      state.homeConfiguration = previous.identity; state.homeNativeChoice = previous.choice; state.homeConfigurationReceipt = previous.receipt;
      state.profile_snapshot = previous.snapshot; state.model_id = previous.model; state.backend = previous.backend;
      if (previous.hasRemembered) state.profileChoices.set("__new__", previous.remembered); else state.profileChoices.delete("__new__");
      return false;
    }
    if (visibleHome) { state.backendChoiceVersion++; invalidateContextPreview(); updateComposer(); }
    return true;
  }
  async function restoreHomeCommandDisplay(event) {
    const target = homeCommandTarget(event), receipt = state.homeConfigurationReceipt, identity = state.homeConfiguration, choice = state.homeNativeChoice;
    if (!target || receipt?.result !== "applied" || receipt.client_id !== event.client_id || receipt.draft_id !== target.draft_id
        || receipt.configuration_revision !== target.configuration_revision || identity?.draft_id !== receipt.draft_id || identity.revision !== receipt.applied_revision
        || !choice?.profile || specialist?.getDraftBinding?.()) return;
    const json = JSON.stringify({ model_id: choice.profile.model_id, reasoning_effort: choice.profile.reasoning_effort });
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(json))), byte => byte.toString(16).padStart(2, "0")).join("");
    if (state.homeConfigurationReceipt !== receipt || state.homeConfiguration !== identity || state.homeNativeChoice !== choice
        || digest !== receipt.proposal_sha256 || event.workspace_id !== state.status?.workspace_id) return;
    updateHomeCommandDisplay(event, { state: "applied", application: receipt });
  }
  function updateHomeCommandDisplay(event, value) {
    const target = homeCommandTarget(event), identity = state.homeConfiguration, choice = state.homeNativeChoice;
    if (value.state !== "applied" || !target || identity?.draft_id !== target.draft_id || identity.revision !== value.application?.applied_revision
        || !choice?.profile || specialist?.getDraftBinding?.() || !workTargetEqual(event.target.context, specialistBinding?.context)) return;
    const context = { ...event.target.context, target: { ...target, configuration_revision: identity.revision }, observed: specialistObserved(choice.profile, choice.memory) };
    const returnTo = workTargetEqual(specialistBinding.return_to, event.target.context) ? context : specialistBinding.return_to;
    acceptSpecialistBinding({ ...specialistBinding, context, return_to: returnTo });
  }
  async function readHomeCommandResult(event) {
    const identity = homeCommandIdentity(event);
    if (!homeProposalCurrent(event)) return false;
    let owner = homeCommandOwner(event), value;
    try { value = await homeProposalRequest(event, owner ? "read" : "inspect", owner ? { owner_secret: owner } : {}); }
    catch (error) {
      if (error.status === 404 && error.code === "draft_configuration_not_found") return true;
      if (event.manual !== true || error.code !== "draft_configuration_owner_mismatch") throw error;
      owner = null; value = await homeProposalRequest(event, "inspect");
    }
    if (!homeProposalCurrent(event)) return false;
    if (value.state === "registered" || ["applied", "rejected"].includes(value.state)) { updateHomeCommandDisplay(event, value); return true; }
    if (!owner) {
      if (event.manual !== true) return false;
      owner = homeCommandOwner(event, true);
      value = await homeProposalRequest(event, "resume", { owner_secret: owner, proposal_sha256: value.proposal.sha256 });
      if (!homeProposalCurrent(event)) return false;
    }
    if (value.state !== "pending") return ["applied", "rejected"].includes(value.state);
    let receipt = state.homeConfigurationReceipt;
    const original = item => item && Object.entries(identity).every(([key, value]) => item[key] === value) && item.proposal_sha256 === value.proposal.sha256;
    if (!original(receipt)) {
      profileCatalogReadback++;
      try { await profiles.loadFresh(); } finally { profileCatalogReadback--; }
      if (!homeProposalCurrent(event)) return false;
      const choice = state.homeNativeChoice, base = choice?.local_base || choice?.profile;
      let reason = state.homeConfiguration?.draft_id !== identity.draft_id ? "target_changed"
        : state.homeConfiguration.revision !== identity.configuration_revision ? "configuration_changed"
        : state.homeNativeHandoff || state.nativeHomeSending ? "handoff_started"
        : ["failed", "interrupted", "rejected"].includes(event.status) ? "cancelled"
        : choice?.agent !== "codex" || JSON.stringify(base) !== JSON.stringify(value.base_profile)
          || !profiles.get(base?.id) || JSON.stringify(window.RabbitProfiles.profile(profiles.get(base.id), false)) !== JSON.stringify(base)
          || !homeLocalSupported(value.proposal.parameters) ? "unsupported" : null;
      receipt = { ...identity, proposal_sha256: value.proposal.sha256, result: reason ? "rejected" : "applied", applied_revision: reason ? null : identity.configuration_revision + 1, reason };
      if (!reason && !writeHomeConfiguration(value.proposal.parameters, receipt)) { reason = "storage_unavailable"; receipt = { ...receipt, result: "rejected", applied_revision: null, reason }; }
      if (reason) {
        const previous = state.homeConfigurationReceipt; state.homeConfigurationReceipt = window.RabbitBrowserState.homeConfigurationReceipt(receipt);
        if (checkpointBrowserState() !== true) { state.homeConfigurationReceipt = previous; return false; }
      }
    }
    if (!homeProposalCurrent(event)) return false;
    const settled = await homeProposalRequest(event, "settle", { owner_secret: owner, proposal_sha256: receipt.proposal_sha256,
      result: receipt.result, applied_revision: receipt.applied_revision, reason: receipt.reason });
    updateHomeCommandDisplay(event, settled);
    return settled.state === receipt.result && JSON.stringify(settled.application) === JSON.stringify({ result: receipt.result, applied_revision: receipt.applied_revision, reason: receipt.reason });
  }

  async function readSpecialistCommandResult(event) {
    if (homeCommandTarget(event)) return readHomeCommandResult(event);
    let binding;
    try { binding = window.RabbitSpecialistContext.normalizeBinding(event?.target); } catch (_) { return false; }
    const scope = binding.workspace_id;
    return readSpecialistConfiguration(binding, () => event.workspace_id === scope && scope === state.status?.workspace_id && (typeof event.isCurrent !== "function" || event.isCurrent()));
  }
  async function readSpecialistConfiguration(binding, current) {
    const context = binding.context, scope = binding.workspace_id, target = context?.target;
    if (!current()) return;
    if (context === null) return true;
    if (!target || !profiles) return false;
    const requests = [];
    if (target.kind === "chat") requests.push(api(`/api/chats/${encodeURIComponent(target.chat_id)}/profile-selection`, {}, false));
    if (target.kind === "native") {
      const query = new URLSearchParams({ agent: target.agent, chat_id: target.chat_id });
      requests.push(["codex", "pi", "hermes", "claude"].includes(target.agent) ? api("/api/management/native-chat-configuration?" + query, {}, false) : Promise.resolve(null));
      requests.push(api("/api/management/native-chat-memory-configuration?" + query, {}, false));
    }
    // This catalogue read must not adopt a different page's selection.
    profileCatalogReadback++;
    let results;
    try { results = await Promise.allSettled([profiles.loadFresh(), ...requests]); }
    finally { profileCatalogReadback--; }
    if (!current()) return;
    if (results.some(item => item.status !== "fulfilled") || results[0].value?.workspace_id !== scope) return false;
    try {
      let observed = context.observed, refreshedTarget = target, verified = true;
      if (target.kind === "chat") {
        const selection = window.RabbitProfiles.selection(results[1].value.profile_selection);
        const expectedRevision = context.observed?.ai_selection_revision;
        if (expectedRevision != null && selection.revision < expectedRevision) throw Error();
        const ai = profiles.get(selection.ai_profile_id), memory = profiles.getMemory(selection.memory_profile_id);
        observed = specialistObserved(ai, memory, selection.revision, selection.revision);
        verified = (!selection.ai_profile_id || !!ai) && (selection.memory_profile_id === "none" || !!memory);
        if (workTargetEqual(context, currentSpecialistWorkContext()) && state.page === "conversations") {
          if (observeProfileSelection(selection)) { if (!state.profileSelectionSync && selection.ai_profile_id) state.profileSelectionSync = "pending"; applyObservedProfileSelection(); updateComposer(); }
          else verified = false;
        }
      } else if (target.kind === "native") {
        const readbacks = {};
        for (const [index, field, expectedScope, factory] of [[1, "configuration_selection", "native_next_turn_selection", () => window.RabbitProfiles.createNativeSelection({ agent: target.agent, parseProfile: row => window.RabbitProfiles.profile(row, false) })], [2, "memory_selection", "native_next_turn_memory_selection", () => window.RabbitProfiles.createNativeMemorySelection({ agent: target.agent })]]) {
          const value = results[index].value; if (value === null) continue;
          if (value?.scope !== expectedScope || value.inference_performed !== false || value.connection_checked !== false) throw Error();
          const dto = Object.fromEntries(["workspace_id", "agent", "chat_id", "selection", "profile", "profile_current"].map(key => [key, value[key]]));
          const expectedRevision = context.observed?.[field === "memory_selection" ? "memory_selection_revision" : "ai_selection_revision"];
          if (expectedRevision != null && dto.selection?.revision < expectedRevision) throw Error();
          const machine = factory();
          if (dto.selection?.revision === 0) { machine.reset(undefined, { workspace: scope, chatId: target.chat_id }); machine.observe({ [field]: dto }); if (!machine.ready()) throw Error(); }
          else machine.reset(dto, { workspace: scope, chatId: target.chat_id });
          readbacks[field] = dto;
        }
        const ai = readbacks.configuration_selection, memory = readbacks.memory_selection;
        observed = specialistObserved(ai?.profile, memory?.profile, ai?.selection.revision, memory?.selection.revision);
        verified = Object.values(readbacks).every(dto => dto.profile_current);
        if (state.page === "native-chats" && workTargetEqual(context, currentSpecialistWorkContext()) && nativeChats?.applyConfigurationReadback?.(context, { configuration_selection: ai || null, memory_selection: memory || null }) !== true) return false;
      } else if (["ai_profile", "memory_profile"].includes(target.kind)) {
        const row = target.kind === "ai_profile" ? profiles.get(target.profile_id) : profiles.getMemory(target.profile_id);
        if (!row || row.revision < target.profile_revision) return false;
        refreshedTarget = { ...target, profile_revision: row.revision };
        observed = specialistObserved(target.kind === "ai_profile" ? row : null, target.kind === "memory_profile" ? row : null);
      }
      // A completed command refreshes its display target only. A newer draft's
      // identity and expected versions remain owned by the composer.
      if (workTargetEqual(context, specialistBinding?.context)) {
        if (!specialist?.getDraftBinding?.() && context.schema_version === 2)
          acceptSpecialistBinding({ ...specialistBinding, context: { ...context, target: refreshedTarget, observed } });
        else specialist?.updateGuideContext?.();
      }
      return verified;
    } catch (_) { return false; }
  }
  async function specialistCheckContext(event) {
    const scope = state.status?.workspace_id, target = specialistGuideTarget, stamp = specialistGuideGeneration;
    const current = () => specialistEnabled() && event?.workspace_id === scope && scope === state.status?.workspace_id
      && stamp === specialistGuideGeneration && target === specialistGuideTarget && event.isCurrent?.() === true;
    if (!scope || !current()) throw Error("核对对象已变化，请重新点击核对。");
    let value = null;
    try { value = await api("/api/status", {}, false); } catch (_) { /* Report unavailable metadata, never assume health. */ }
    if (!current()) throw Error("核对对象已变化，没有发送核对消息。");
    const nativeKeys = { codex: "native_chats", pi: "pi_native_chats", hermes: "hermes_native_chats", claude: "claude_native_chats", dsh: "dsh_native_chats" };
    const statusRead = value?.workspace_id === scope;
    const knownBoolean = value => typeof value === "boolean" ? value : null;
    const fields = ["workspace_id", "agent", "chat_id", "title", "agent_instance_id", "profile_id", "profile_name", "profile_revision", "model_id", "reasoning_effort", "memory_profile_id", "memory_profile_revision"];
    return { workspace_id: scope, target: target?.workspace_id === scope
      ? Object.fromEntries(fields.filter(key => Object.hasOwn(target, key)).map(key => [key, target[key]])) : null,
      status_read: statusRead, agent_entries: statusRead ? agents.ids.map(agent => ({ agent,
        native_entry_enabled: knownBoolean(value[nativeKeys[agent]]?.enabled),
        text_connection_available: knownBoolean(value.backends?.find?.(row => row.id === agent)?.available) })) : [] };
  }
  async function refreshSpecialistGuide(event) {
    const scope = state.status?.workspace_id, target = specialistGuideTarget, generation = specialistGuideGeneration, request = ++specialistGuideRead;
    const current = () => specialistEnabled() && scope === state.status?.workspace_id
      && event?.manual_check === true && event.workspace_id === scope && (typeof event.isCurrent !== "function" || event.isCurrent())
      && generation === specialistGuideGeneration && request === specialistGuideRead && target === specialistGuideTarget;
    if (!scope || !profiles || !current()) return;
    const nativeTarget = target?.workspace_id === scope && ["codex", "pi", "hermes", "claude"].includes(target.agent)
      && target.agent_instance_id === "local/" + target.agent && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(target.chat_id || "");
    const results = await Promise.allSettled([profiles.loadFresh(), nativeTarget
      ? api("/api/management/native-chat-configuration?" + new URLSearchParams({ agent: target.agent, chat_id: target.chat_id }), {}, false)
      : Promise.resolve(null)]);
    if (!current()) return;
    try {
      if (results.some(row => row.status !== "fulfilled") || results[0].value?.workspace_id !== scope) throw Error();
      if (nativeTarget) {
        const value = results[1].value, keys = ["workspace_id", "agent", "chat_id", "selection", "profile", "profile_current", "scope", "inference_performed", "connection_checked"];
        if (!value || Object.keys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key))
            || value.scope !== "native_next_turn_selection" || value.inference_performed !== false || value.connection_checked !== false) throw Error();
        const dto = Object.fromEntries(keys.slice(0, 6).map(key => [key, value[key]]));
        const machine = window.RabbitProfiles.createNativeSelection({ agent: target.agent, parseProfile: row => window.RabbitProfiles.profile(row, false) });
        if (dto.selection?.revision === 0) {
          machine.reset(undefined, { workspace: scope, chatId: target.chat_id }); machine.observe({ configuration_selection: dto });
          if (!machine.ready() || machine.managed()) throw Error();
        } else machine.reset(dto, { workspace: scope, chatId: target.chat_id });
        const previous = specialistGuideSelection;
        if (previous && (dto.selection.revision < previous.selection.revision || dto.selection.revision === previous.selection.revision
            && (dto.selection.ai_profile_id !== previous.selection.ai_profile_id || dto.selection.ai_profile_revision !== previous.selection.ai_profile_revision
              || dto.profile_current && previous.profile_current && JSON.stringify(dto.profile && window.RabbitProfiles.profile(dto.profile, false)) !== JSON.stringify(previous.profile && window.RabbitProfiles.profile(previous.profile, false))))) throw Error();
        specialistGuideSelection = dto;
        if (!dto.profile_current) specialistGuideVerification = "unverified";
        else {
          const row = dto.profile;
          specialistGuideTarget = { ...target, profile_id: row?.id || null, profile_name: row?.name || null,
            profile_revision: row?.revision || null, model_id: row?.model_id || null, reasoning_effort: row?.reasoning_effort ?? null };
          specialistGuideVerification = row ? "" : "inherited";
        }
      } else specialistGuideVerification = "";
    } catch (_) { specialistGuideVerification = "failed"; }
    specialist?.updateGuideContext?.();
  }
  function specialistEnabled() { return !!specialist && state.status?.specialist?.enabled === true; }
  function updateSpecialistAvailability() {
    const enabled = specialistEnabled();
    $("specialist-composer-card").hidden = !enabled;
    specialist?.setEnabled(enabled, { passive: state.page === "my-ai" });
    if (!enabled && state.page === "specialist") showPage("conversations");
  }
  const nativeChats = (() => {
    if (!window.RabbitNativeChats) return null;
    const root = $("native-chats-panel"), choiceLabel = el("label", "rbs-field rbn-agent-picker"), choiceText = el("span", "", "使用的 Agent"), choice = el("select", "rbs-control");
    choice.setAttribute("aria-label", "原生 Agent"); choiceLabel.append(choiceText, choice);
    const panes = { codex: el("div"), pi: el("div"), hermes: el("div"), claude: el("div"), dsh: el("div") };
    const settingsPanes = Object.fromEntries(Object.keys(panes).map(id => [id, el("div")]));
    settingsMounts.native.append(choiceLabel, ...Object.values(settingsPanes));
    root.append(...Object.values(panes));
    const options = { api: (path, settings = {}) => api(path, settings, false), getWorkspace: () => state.status?.workspace_id || "", openDocument, materialFiles: fileMaterials,
      getSendShortcut: () => state.preferences.send_shortcut,
      onChooseConfiguration: kind => profiles?.open(kind),
      getModelLabel: value => {
        const source = profiles?.catalog()?.native_profile_capabilities?.[agents.backendOfInstance(value?.agent_instance_id)];
        return source?.enabled && source.catalogue_available ? source.models.find(row => row.id === value?.model_id)?.model || null : null;
      },
      onTextHandoff: value => openNativeTextHandoff(value),
      onResumeHandoff: value => resumeNativeContinuation(value),
      onQuoteHistory: async () => {
        const captured = quoteDraft();
        if (!historyQuotes || !captured.active || captured.busy) return;
        // Load existing file provenance before the picker deduplicates copies.
        // An unavailable old file stays in the draft; only new file selection is disabled.
        if (fileInputsSupported()) await Promise.allSettled(captured.native_quote.material_refs.filter(ref => ref.kind === "text").map(async ref => {
          fileMaterials.known.delete(ref.id);
          return fileMaterials.inspect(ref.id);
        }));
        const current = quoteDraft();
        if (current.active && !current.busy && current.stamp === captured.stamp && current.text === captured.text) historyQuotes.open();
      },
      onOrganize: value => markdownChats?.open(value), canOrganize: () => markdownChats?.enabled() === true,
      onError: error => toast(userError(error, "原生对话操作尚未确认，草稿与原请求保留。")) };
    const controllers = { codex: window.RabbitNativeChats.create({ ...options, root: panes.codex, settingsRoot: settingsPanes.codex,
      materialImages: materials,
      onManageProfiles: () => { state.browserPageTouched = true; showPage("management", "ai-profiles"); } }) };
    if (window.RabbitPiNative) controllers.pi = window.RabbitNativeChats.create({ ...options, root: panes.pi, settingsRoot: settingsPanes.pi,
      materialImages: materials,
      onManageProfiles: () => { state.browserPageTouched = true; showPage("management", "ai-profiles"); },
      descriptor: { agent: "pi", apiRoot: "/api/pi-chats", storageNamespace: "rabbit.pi-native-chats.page.v1.", createRenderer: window.RabbitPiNative.create } });
    if (window.RabbitHermesNative) controllers.hermes = window.RabbitNativeChats.create({ ...options, root: panes.hermes, settingsRoot: settingsPanes.hermes,
      materialImages: materials,
      onManageProfiles: () => { state.browserPageTouched = true; showPage("management", "ai-profiles"); },
      descriptor: { agent: "hermes", apiRoot: "/api/hermes-chats", storageNamespace: "rabbit.hermes-native-chats.page.v1.", createRenderer: window.RabbitHermesNative.create } });
    if (window.RabbitClaudeNative) controllers.claude = window.RabbitNativeChats.create({ ...options, root: panes.claude, settingsRoot: settingsPanes.claude,
      descriptor: { agent: "claude", apiRoot: "/api/claude-chats", storageNamespace: "rabbit.claude-native-chats.page.v1.", createRenderer: window.RabbitClaudeNative.create } });
    if (window.RabbitDshNative) controllers.dsh = window.RabbitNativeChats.create({ ...options, root: panes.dsh, settingsRoot: settingsPanes.dsh,
      materialImages: materials,
      descriptor: { agent: "dsh", apiRoot: "/api/dsh-chats", storageNamespace: "rabbit.dsh-native-chats.page.v1.", createRenderer: window.RabbitDshNative.create } });
    let selected = "codex", available = {}, visible = false, settingsVisible = false, enabled = false, scope = "", language = "zh-CN", navigationVersion = 0, showExistingSelection = false, showing = Promise.resolve();
    const readingRoot = el("div"); root.append(readingRoot);
    const indexReader = window.RabbitNativeIndexText?.create({ root: readingRoot, scrollRoot: root, api: options.api, getWorkspace: options.getWorkspace,
      renderMessage: (row, agent, match) => {
        if (isThinkingRecord(row)) return null;
        const item = messageNode(row, true, agent, match);
        if (isOperationRecord(row)) item.dataset.operationRecord = "true";
        return item;
      },
      onViewChange: source => {
        if (visible && state.page === "native-chats" && source?.workspace_id === scope) {
          nativeHeader(source); renderList(); ui.actions.replaceChildren();
          const read = indexReader.getHistorySourceContext();
          if (read) void loadHistoryOrigins(read.agent, read.source_session_id, state.openVersion, read);
          else { historyOriginTargets = null; historyOriginRead++; }
        }
      } });
    function nativeHeader(source) {
      const row = state.libraryItems.find(item => item.kind === "native" && item.agent === source.agent && item.id === source.chat_id);
      ui.title.textContent = source.title || row?.title || "原生对话"; ui.eyebrow.textContent = ui.meta.textContent = "";
    }
    const readingSelected = () => !!indexReader?.selected();
    function remember() { try { if (scope) window.sessionStorage.setItem("rabbit.native-agent.v1." + encodeURIComponent(scope), selected); } catch (_) { /* Selection is optional; submission journals remain in the renderer. */ } }
    function renderChoice() {
      for (const [id, pane] of Object.entries(settingsPanes)) pane.hidden = id !== selected;
      choiceText.textContent = language === "en" ? "Choose Agent" : "使用的 Agent";
      choice.replaceChildren();
      for (const id of Object.keys(controllers)) { const option = el("option", "", { codex: "Codex", pi: "Pi", hermes: "Hermes", claude: "Claude Code", dsh: "DSH" }[id]); option.value = id; option.disabled = !available[id]; choice.append(option); }
      choice.value = selected; choice.disabled = !enabled; choice.setAttribute("aria-label", language === "en" ? "Native Agent" : "原生 Agent");
    }
    choice.addEventListener("change", () => {
      if (!enabled || !available[choice.value] || !controllers[choice.value]) { renderChoice(); return; }
      specialistLocationRevision++;
      navigationVersion++;
      for (const controller of Object.values(controllers)) controller.hide();
      indexReader?.clear();
      selected = choice.value; remember(); renderChoice();
      if (settingsVisible) controllers[selected].showSettings(); else if (visible) controllers[selected].show();
    });
    return {
      getComposerView: () => {
        const view = !readingSelected() && visible && !settingsVisible && scope === String(state.status?.workspace_id || "") ? controllers[selected]?.getComposerView?.() : null;
        return view && ["codex", "hermes"].includes(selected) ? { ...view, native_agents: ["codex", "hermes"] } : view;
      },
      saveLocalConfiguration: (parameters, view, retry) => !readingSelected() && visible && !settingsVisible && selected === "codex" && scope === String(state.status?.workspace_id || "") ? controllers.codex?.saveLocalConfiguration?.(parameters, view, retry) ?? false : false,
      chooseConfiguration: (kind, row, view) => !readingSelected() && visible && !settingsVisible && scope === String(state.status?.workspace_id || "") ? controllers[selected]?.chooseConfiguration?.(kind, row, view) ?? false : false,
      applyConfigurationReadback(context, value) {
        const current = this.getConfigurationContext();
        if (!visible || settingsVisible || context?.workspace_id !== scope || context?.target?.kind !== "native" || context.target.agent !== selected || current?.chat_id !== context.target.chat_id) return false;
        return controllers[selected]?.applyConfigurationReadback?.(value) === true;
      },
      getConfigurationContext: () => !readingSelected() && visible && !settingsVisible && scope === String(state.status?.workspace_id || "") ? controllers[selected]?.getConfigurationContext?.() || null : null,
      captureQuoteDraft: () => !readingSelected() && enabled && visible && !settingsVisible && ["codex", "pi", "hermes", "claude", "dsh"].includes(selected) && scope === String(state.status?.workspace_id || "") ? controllers[selected]?.captureQuoteDraft?.() || null : null,
      applyQuoteDraft: (expected, value, refs, preview) => !readingSelected() && enabled && visible && !settingsVisible && ["codex", "pi", "hermes", "claude", "dsh"].includes(selected) && expected?.agent === selected && scope === String(state.status?.workspace_id || "") && controllers[selected]?.applyQuoteDraft?.(expected, value, refs, preview) === true,
      setAvailability(status) {
        const next = String(status?.workspace_id || "");
        if (next !== scope) { navigationVersion++; scope = next; try { const value = window.sessionStorage.getItem("rabbit.native-agent.v1." + encodeURIComponent(scope)); if (Object.hasOwn(controllers, value)) selected = value; } catch (_) {} }
        indexReader?.restore();
        available = { codex: status?.native_chats?.enabled === true, pi: !!controllers.pi && status?.pi_native_chats?.enabled === true,
          hermes: !!controllers.hermes && status?.hermes_native_chats?.enabled === true,
          claude: !!controllers.claude && status?.claude_native_chats?.enabled === true,
          dsh: !!controllers.dsh && status?.dsh_native_chats?.enabled === true };
        if (readingSelected()) selected = indexReader.selected().agent;
        else if (!available[selected]) selected = Object.keys(controllers).find(id => available[id]) || "codex";
        renderChoice();
      },
      setEnabled(value) { const next = value === true; if (enabled !== next) navigationVersion++; enabled = next; for (const [id, controller] of Object.entries(controllers)) controller.setEnabled(enabled && available[id]); renderChoice(); if (!enabled && !readingSelected()) this.hide(); },
      setLanguage(value) { language = value === "en" ? "en" : "zh-CN"; for (const controller of Object.values(controllers)) controller.setLanguage(language); indexReader?.setLanguage(language); renderChoice(); },
      hasReadOnlySelection: readingSelected,
      getViewContext() { return visible && !settingsVisible ? indexReader?.getViewContext() || (!readingSelected() ? controllers[selected]?.getQuoteContext() : null) : null; },
      getHistorySourceContext() { return visible && !settingsVisible && scope === String(state.status?.workspace_id || "") ? indexReader?.getHistorySourceContext() || null : null; },
      async openEntry(value, details = {}) {
        if (details.readOnly !== true && available[value?.agent]) return this.selectExisting(value);
        if (!indexReader) throw Error("只读历史组件尚未加载，请刷新页面。");
        specialistLocationRevision++; navigationVersion++;
        indexReader.select(value, details);
        for (const controller of Object.values(controllers)) controller.hide();
        selected = value.agent; remember(); renderChoice(); state.browserPageTouched = true;
        showPage("native-chats"); renderList(); return showing;
      },
      show() {
        visible = true; settingsVisible = false; root.hidden = false;
        const existingSelection = showExistingSelection; showExistingSelection = false;
        if (readingSelected()) {
          for (const controller of Object.values(controllers)) controller.hide();
          nativeHeader(indexReader.selected());
          return showing = Promise.resolve(indexReader.show());
        }
        indexReader?.hide();
        for (const [id, controller] of Object.entries(controllers)) if (id !== selected) controller.hide();
        return showing = Promise.resolve(controllers[selected]?.show({ existingSelection })).then(async () => {
          const source = controllers[selected]?.getQuoteContext();
          if (visible && state.page === "native-chats" && source) {
            ui.title.textContent = source.title; ui.eyebrow.textContent = ui.meta.textContent = "";
            appendListSearchMatch(panes[selected].querySelector(".rbs-chat-more"));
            await renderNativeOrigin(source, () => visible && state.page === "native-chats" && scope === source.workspace_id
              && selected === source.agent && controllers[selected]?.getQuoteContext()?.chat_id === source.chat_id);
          }
        });
      },
      showSettings() { visible = false; settingsVisible = true; root.hidden = true; indexReader?.hide(); for (const [id, controller] of Object.entries(controllers)) if (id !== selected) controller.hide(); return controllers[selected]?.showSettings(); },
      hide() { navigationVersion++; showExistingSelection = false; visible = settingsVisible = false; root.hidden = true; indexReader?.hide(); for (const controller of Object.values(controllers)) controller.hide(); },
      readCreation(creationId, agent = "codex") {
        if (!["codex", "hermes"].includes(agent) || !enabled || !available[agent] || scope !== String(state.status?.workspace_id || "")) throw Error("原创建入口暂不可用，原记录保留。");
        return controllers[agent].readCreation(creationId);
      },
      async createNew(agent, isCurrent = () => true, savedProfile = null, initialDraft = null, send = false, preparation = null, captureNextDraft = null) {
        specialistLocationRevision++;
        const request = ++navigationVersion, originalWorkspace = scope;
        if (!isCurrent() || !enabled || !available[agent] || !controllers[agent]) throw Error("这个 Agent 的原生入口暂不可用。");
        const current = () => isCurrent() && request === navigationVersion && scope === originalWorkspace && enabled && available[agent];
        const id = await controllers[agent].createNew(savedProfile, current, initialDraft, preparation);
        if (!id || !isCurrent() || request !== navigationVersion || scope !== originalWorkspace) return false;
        const draftToken = send ? controllers[agent].captureDraftRevision?.(id) || null : null;
        const nextDraft = send && typeof captureNextDraft === "function" ? captureNextDraft() : null;
        indexReader?.clear(); selected = agent; remember(); renderChoice(); showExistingSelection = true; state.browserPageTouched = true; showPage("native-chats"); refreshChats().catch(() => {});
        const location = state.contextNavigationVersion;
        if (send) {
          await showing;
          if (request !== navigationVersion || location !== state.contextNavigationVersion || state.page !== "native-chats" || selected !== agent || scope !== originalWorkspace) return false;
          if (draftToken && controllers[agent].captureDraftRevision?.(id) !== draftToken) { toast("目标对话已有新草稿，没有覆盖或发送；首页草稿仍保留。"); return false; }
          const sending = controllers[agent].sendDraft(id);
          // sendDraft freezes A synchronously. B is a local draft, never a
          // second submission, and a newer target edit C invalidates its token.
          if (nextDraft) {
            if (draftToken && controllers[agent].adoptTextDraft?.(id, draftToken, nextDraft.text) === true) {
              if (nextDraft.consume() !== true) toast("新草稿已放到当前对话；首页仍保留一份，请核对后再发送。");
            } else toast("新输入仍保留在首页；目标草稿没有覆盖。");
          }
          await sending;
        }
        return true;
      },
      async createContinuation(record, draft, origin, isCurrent, onProgress) {
        const agent = record.target.agent, originalWorkspace = scope, request = ++navigationVersion;
        const current = () => isCurrent() && request === navigationVersion && enabled && scope === originalWorkspace && scope === String(state.status?.workspace_id || "") && available[agent];
        if (!["codex", "hermes"].includes(agent) || !controllers[agent] || !current()) throw Error("接续目标暂不可用，原准备保留。");
        const id = await controllers[agent].createNew(record.target.ai_profile, current, draft, null, {
          creation_id: record.creation.creation_id, created_chat_id: record.creation.created_chat_id,
          onProgress,
          beforePlace: async id => {
            if (await saveContinuationOrigin({ ...origin, target_chat_id: id }, current) !== true) return false;
            return current() && onProgress({ handoff_started: true }) === true;
          },
        });
        if (!id || !current()) return { accepted: false };
        indexReader?.clear(); selected = agent; remember(); renderChoice(); showExistingSelection = true; state.browserPageTouched = true;
        showPage("native-chats"); const location = state.contextNavigationVersion;
        refreshChats().catch(() => {}); await showing;
        // This explicit navigation replaces the preparation view. From here the
        // existing native renderer owns the draft and original submission journal.
        if (request !== navigationVersion || location !== state.contextNavigationVersion || state.page !== "native-chats" || selected !== agent
            || scope !== originalWorkspace || controllers[agent]?.getQuoteContext()?.chat_id !== id) return { accepted: false };
        const value = await controllers[agent].sendDraft(id), receipt = value?.receipt || value?.operation;
        return { accepted: !!receipt && ["accepted", "completed", "failed", "interrupted"].includes(receipt.status)
          && (agent === "hermes" ? receipt.chat_id === id : receipt.session_key === "chat/codex/" + id) };
      },
      getQuoteContext() { return !readingSelected() && enabled && visible && available[selected] ? controllers[selected]?.getQuoteContext() : null; },
      getTextSnapshot() { return !readingSelected() && enabled && visible && available[selected] ? controllers[selected]?.getTextSnapshot() : null; },
      async readTextSnapshot() { return !readingSelected() && enabled && visible && available[selected] ? controllers[selected]?.readTextSnapshot() : null; },
      async selectExisting(value, isCurrent = () => true) {
        specialistLocationRevision++;
        const request = ++navigationVersion, sourceWorkspace = scope, target = value?.agent;
        const current = () => typeof isCurrent === "function" && isCurrent() && request === navigationVersion && enabled && scope === sourceWorkspace && scope === String(state.status?.workspace_id || "")
          && value?.workspaceId === scope && available[target] && typeof controllers[target]?.selectExisting === "function";
        if (!value || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value.chatId) || !current()) throw Error(language === "en" ? "The native chat entry or workspace is unavailable." : "原生对话入口或所属工作区暂不可用。");
        if (await controllers[target].selectExisting(value, current) !== true || !current()) return false;
        for (const controller of Object.values(controllers)) controller.hide();
        indexReader?.clear(); selected = target; remember(); renderChoice(); showExistingSelection = true; state.browserPageTouched = true; showPage("native-chats"); renderList();
        return true;
      },
      async putDraft(value, isCurrent = () => true) {
        const { agent, chatId, workspaceId, text, material_refs, material_preview } = value;
        if (typeof isCurrent !== "function" || !isCurrent() || !enabled || scope !== workspaceId || scope !== String(state.status?.workspace_id || "") || !available[agent] || !controllers[agent]) throw Error("目标原生 Agent、来源或工作区已变化，请重新选择。");
        if (material_refs !== undefined && (!Array.isArray(material_refs) || material_refs.length && (!["codex", "pi", "hermes", "claude", "dsh"].includes(agent) || ["claude", "dsh"].includes(agent) && material_refs.some(ref => ref?.kind !== "text")))) throw Error("此 Agent 尚未接入所选材料，原草稿保留。");
        const request = ++navigationVersion, page = state.page, opening = state.openVersion, location = state.contextNavigationVersion;
        const replacesVisibleSource = page === "native-chats" && selected === agent;
        const positionCurrent = () => request === navigationVersion && page === state.page && opening === state.openVersion
          && location === state.contextNavigationVersion && enabled && scope === workspaceId && scope === String(state.status?.workspace_id || "");
        // Selecting another chat in this controller replaces its source renderer.
        // After that explicit selection, protect the host position and controller
        // generations rather than asking the replaced renderer to stay current.
        const afterSelection = () => positionCurrent() && (replacesVisibleSource || isCurrent());
        const placed = await controllers[agent].putDraft({ workspace_id: workspaceId, chat_id: chatId, text, ...(material_refs?.length ? { material_refs, ...(material_preview === undefined ? {} : { material_preview }) } : {}), ...(Object.hasOwn(value, "memory_profile") ? { memory_profile: value.memory_profile } : {}) }, () => positionCurrent() && isCurrent(), afterSelection);
        if (placed !== true) throw Error("未能放入原生草稿；来源页的文字和材料仍保留。");
        if (scope !== workspaceId || scope !== String(state.status?.workspace_id || "")) throw Error("工作区已变化，请在原工作区核对刚才的草稿。");
        if (!afterSelection()) return true; // The synchronous draft commit already succeeded; do not redirect a later view.
        for (const controller of Object.values(controllers)) controller.hide();
        indexReader?.clear(); selected = agent; remember(); renderChoice(); showExistingSelection = true; state.browserPageTouched = true; showPage("native-chats");
        return true;
      },
    };
  })();
  function nativeChatsEnabled() { return !!nativeChats && (state.status?.native_chats?.enabled === true || !!window.RabbitPiNative && state.status?.pi_native_chats?.enabled === true || !!window.RabbitHermesNative && state.status?.hermes_native_chats?.enabled === true || !!window.RabbitClaudeNative && state.status?.claude_native_chats?.enabled === true || !!window.RabbitDshNative && state.status?.dsh_native_chats?.enabled === true); }
  function updateNativeChatsAvailability() {
    nativeChats?.setAvailability(state.status);
    const enabled = nativeChatsEnabled(); nativeChats?.setEnabled(enabled);
    if (!enabled && state.page === "native-chats" && !nativeChats?.hasReadOnlySelection()) showPage("conversations");
  }
  function nativeHandoffTargets() { return [
      { agent: "codex", label: "Codex", apiRoot: "/api/native-chats", enabled: state.status?.native_chats?.enabled },
      { agent: "pi", label: "Pi", apiRoot: "/api/pi-chats", enabled: state.status?.pi_native_chats?.enabled },
      { agent: "hermes", label: "Hermes", apiRoot: "/api/hermes-chats", enabled: state.status?.hermes_native_chats?.enabled },
      { agent: "claude", label: "Claude Code", apiRoot: "/api/claude-chats", enabled: state.status?.claude_native_chats?.enabled },
      { agent: "dsh", label: "DSH", apiRoot: "/api/dsh-chats", enabled: !!window.RabbitDshNative && state.status?.dsh_native_chats?.enabled },
    ].filter(row => row.enabled === true); }
  const continuationJSON = value => JSON.stringify(value, (_key, item) => item && typeof item === "object" && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  async function continuationDigest(value) {
    if (!window.crypto?.subtle) throw Error("浏览器不能核对接续版本，原准备保留。");
    return [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(continuationJSON(value))))].map(v => v.toString(16).padStart(2, "0")).join("");
  }
  const continuationKeys = (value, names) => value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
  const continuationUUID = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
  function continuationSourceView(source, scope) {
    if (source?.workspace_id !== scope || typeof source.title !== "string" || !source.title.length || source.title.length > 16384 || /[\u0000\ud800-\udfff]/u.test(source.title)) return false;
    if (source.kind === "history_reference") return continuationKeys(source, ["kind", "workspace_id", "id", "agent", "session_id", "title"])
      && continuationUUID(source.id) && ["codex", "pi", "hermes", "claude", "dsh"].includes(source.agent)
      && typeof source.session_id === "string" && source.session_id.length > 0 && source.session_id.length <= 256 && !/[\s\p{Cc}\p{Cf}\p{Cs}]/u.test(source.session_id);
    return continuationKeys(source, ["workspace_id", "agent", "chat_id", "title"])
      && ["codex", "hermes"].includes(source.agent) && continuationUUID(source.chat_id);
  }
  function continuationOrigin(value, scope, agent, chatId) {
    return continuationKeys(value, ["target_agent", "target_chat_id", "creation_id", "source_view", "source_revision", "selected_message_ids", "material_refs", "snapshot_sha256"])
      && ["codex", "hermes"].includes(agent) && continuationUUID(chatId) && value.target_agent === agent && value.target_chat_id === chatId && continuationUUID(value.creation_id)
      && continuationSourceView(value.source_view, scope) && /^[a-f0-9]{64}$/.test(value.source_revision) && /^[a-f0-9]{64}$/.test(value.snapshot_sha256)
      && Array.isArray(value.selected_message_ids) && value.selected_message_ids.length <= 50 && value.selected_message_ids.every(id => typeof id === "string" && id.length > 0 && id.length <= 16384 && !/[\u0000\ud800-\udfff]/u.test(id))
      && new Set(value.selected_message_ids).size === value.selected_message_ids.length && Array.isArray(value.material_refs);
  }
  async function saveContinuationOrigin(origin, isCurrent = () => true) {
    const query = new URLSearchParams({ agent: origin.target_agent, chat_id: origin.target_chat_id });
    const scope = origin.source_view?.workspace_id;
    if (scope !== state.status?.workspace_id || !continuationOrigin(origin, scope, origin.target_agent, origin.target_chat_id) || !isCurrent()) return false;
    const key = "rabbit.native-origin.pending.v1." + encodeURIComponent(scope) + "." + origin.target_agent + "." + origin.target_chat_id;
    let pending;
    try {
      pending = window.sessionStorage.getItem(key);
      if (pending !== null && continuationJSON(JSON.parse(pending)) !== continuationJSON(origin)) throw Error();
    } catch (_) { throw Error("原目标的来源保存记录无法核对，没有替换或放入草稿。"); }
    const valid = value => value?.workspace_id === scope && value.scope === "native_continuation_source"
      && continuationKeys(value, ["origin", "scope", "workspace_id"])
      && (value.origin === null || continuationOrigin(value.origin, scope, origin.target_agent, origin.target_chat_id));
    const observed = await api("/api/management/native-chat-origin?" + query, {}, false);
    if (scope !== state.status?.workspace_id || !isCurrent()) return false;
    if (!valid(observed)) throw Error("接续来源的读取身份待核对。");
    if (observed.origin !== null) {
      if (continuationJSON(observed.origin) !== continuationJSON(origin)) throw Error("原目标已有不同来源，没有覆盖。");
      if (pending !== null) {
        if (window.sessionStorage.getItem(key) !== pending) throw Error("原来源保存记录已有变化，没有放入草稿。");
        window.sessionStorage.removeItem(key);
        if (window.sessionStorage.getItem(key) !== null) throw Error("原来源保存记录尚未清除；此次没有放入草稿。");
      }
      return true;
    }
    if (pending !== null) throw Error("原目标的来源保存尚未查到确认；此次只读核对，没有再次保存、重建或放入草稿。");
    const raw = JSON.stringify(origin);
    try {
      if (window.sessionStorage.getItem(key) !== null) throw Error();
      window.sessionStorage.setItem(key, raw);
      if (window.sessionStorage.getItem(key) !== raw) throw Error();
    } catch (_) { throw Error("浏览器不能保留原来源保存记录；尚未保存来源或放入草稿。"); }
    try {
      const saved = await api("/api/management/native-chat-origin", { method: "POST", body: JSON.stringify(origin) }, false);
      if (!valid(saved) || continuationJSON(saved.origin) !== continuationJSON(origin)) throw Error();
      if (window.sessionStorage.getItem(key) !== raw) throw Error();
      window.sessionStorage.removeItem(key);
      if (window.sessionStorage.getItem(key) !== null) throw Error();
    } catch (_) {
      let confirmed = false;
      if (scope === state.status?.workspace_id) try {
        const readback = await api("/api/management/native-chat-origin?" + query, {}, false);
        confirmed = valid(readback) && continuationJSON(readback.origin) === continuationJSON(origin);
      } catch (_) { /* Keep the exact original target and creation receipt. */ }
      throw Error(confirmed ? "来源已按原目标读回确认；此次未放入草稿，请核对后再次明确放入。" : "来源保存结果待核对；此次未放入草稿，原目标和创建编号保留，没有重建。");
    }
    return scope === state.status?.workspace_id && isCurrent();
  }
  let nativeOriginView = null;
  function appendNativeOrigin() {
    const current = nativeChats?.getQuoteContext(), cached = nativeOriginView, previous = cached?.origin?.source_view;
    if (state.page !== "native-chats" || !cached || !previous || !continuationSourceView(previous, current?.workspace_id) || current?.workspace_id !== cached.workspace_id || current.agent !== cached.origin.target_agent || current.chat_id !== cached.origin.target_chat_id) return;
    const location = state.contextNavigationVersion;
    ui.actions.querySelector('[data-continuation-source]')?.remove();
    const back = el("button", "text-button", "返回原对话"); back.type = "button"; back.setAttribute("data-continuation-source", "");
    back.title = previous.title; back.addEventListener("click", () => {
      const view = nativeChats?.getQuoteContext();
      if (cached === nativeOriginView && location === state.contextNavigationVersion && state.page === "native-chats" && state.status?.workspace_id === current.workspace_id
          && view?.workspace_id === current.workspace_id && view.agent === current.agent && view.chat_id === current.chat_id) void openContinuationSource(previous);
    }); ui.actions.append(back);
  }
  async function renderNativeOrigin(source, isCurrent) {
    if (!["codex", "hermes"].includes(source.agent)) return;
    try {
      const value = await api("/api/management/native-chat-origin?" + new URLSearchParams({ agent: source.agent, chat_id: source.chat_id }), {}, false);
      if (!isCurrent() || !continuationKeys(value, ["workspace_id", "scope", "origin"]) || value.workspace_id !== source.workspace_id || value.scope !== "native_continuation_source" || !value.origin) return;
      const origin = value.origin, previous = origin.source_view;
      if (!continuationOrigin(origin, source.workspace_id, source.agent, source.chat_id)) return;
      nativeOriginView = { workspace_id: source.workspace_id, origin }; appendNativeOrigin();
    } catch (_) { /* An unavailable relation never changes or replaces a native chat. */ }
  }
  async function openContinuationSource(source) {
    if (!continuationSourceView(source, state.status?.workspace_id)) return false;
    try {
      if (source.kind === "history_reference") return await openHistory(source.agent, source.session_id, null, 0, true);
      return await nativeChats.selectExisting({ workspaceId: source.workspace_id, agent: source.agent, chatId: source.chat_id });
    } catch (error) { toast(userError(error, "原对话暂不可用，接续准备仍保留。")); return false; }
  }
  async function openContinuationTarget(target) {
    if (!continuationKeys(target, ["workspace_id", "agent", "chat_id"]) || target.workspace_id !== state.status?.workspace_id
        || !["codex", "hermes"].includes(target.agent) || !continuationUUID(target.chat_id)) return false;
    try { return await nativeChats.selectExisting({ workspaceId: target.workspace_id, agent: target.agent, chatId: target.chat_id }); }
    catch (error) { toast(userError(error, "接续对话暂不可用，原准备仍保留。")); return false; }
  }
  let historyOriginTargets = null, historyOriginRead = 0;
  function sameNativeHistorySource(left, right) {
    return !!left && !!right && ["workspace_id", "agent", "chat_id", "source_session_id", "generation"].every(key => left[key] === right[key]);
  }
  async function loadHistoryOrigins(agent, sessionId, version = state.openVersion, readerContext = null) {
    const request = ++historyOriginRead, scope = state.status?.workspace_id;
    const reader = readerContext === null ? null : { ...readerContext }, location = state.contextNavigationVersion;
    historyOriginTargets = null;
    if (!scope) return;
    if (reader && (!continuationKeys(reader, ["workspace_id", "agent", "chat_id", "source_session_id", "generation"])
        || reader.workspace_id !== scope || reader.agent !== agent || reader.source_session_id !== sessionId || !continuationUUID(reader.chat_id) || !Number.isSafeInteger(reader.generation))) return;
    const current = () => request === historyOriginRead && scope === state.status?.workspace_id && version === state.openVersion
      && location === state.contextNavigationVersion && (reader ? state.page === "native-chats" && sameNativeHistorySource(reader, nativeChats?.getHistorySourceContext())
        : state.page === "conversations" && state.selected.type === "history" && state.selected.agent === agent && state.selected.sessionId === sessionId);
    if (!current()) return;
    try {
      const value = await api("/api/management/native-chat-origin?" + new URLSearchParams({ source_kind: "history_reference", source_agent: agent, source_session_id: sessionId }), {}, false);
      if (!current() || !continuationKeys(value, ["workspace_id", "scope", "source_view", "targets"]) || value.workspace_id !== scope || value.scope !== "native_continuation_targets"
          || !continuationSourceView(value.source_view, scope) || value.source_view.kind !== "history_reference" || value.source_view.agent !== agent || value.source_view.session_id !== sessionId
          || !Array.isArray(value.targets) || value.targets.length > 512 || value.targets.some(row => !continuationKeys(row, ["agent", "chat_id", "title", "available"])
            || !["codex", "hermes"].includes(row.agent) || !continuationUUID(row.chat_id) || typeof row.available !== "boolean"
            || (row.available ? typeof row.title !== "string" || /[\u0000\ud800-\udfff]/u.test(row.title) || row.title.length > 16384 : row.title !== null))
          || new Set(value.targets.map(row => row.agent + ":" + row.chat_id)).size !== value.targets.length) return;
      historyOriginTargets = { ...value, version, reader }; historyHeader(); updateComposer();
    } catch (_) { /* A missing relation never creates a target or rewrites history. */ }
  }
  function appendHistoryOriginTargets() {
    const value = historyOriginTargets, version = state.openVersion, location = state.contextNavigationVersion;
    const current = () => value === historyOriginTargets && value?.workspace_id === state.status?.workspace_id && value.version === state.openVersion && version === state.openVersion
      && location === state.contextNavigationVersion && (value.reader ? state.page === "native-chats" && sameNativeHistorySource(value.reader, nativeChats?.getHistorySourceContext())
        : state.page === "conversations" && state.selected.type === "history" && state.selected.agent === value.source_view.agent && state.selected.sessionId === value.source_view.session_id);
    if (!value?.targets.length || !current()) return;
    const group = el("details", "conversation-more"), actions = el("div", "conversation-more-actions");
    group.append(el("summary", "", "接续对话"));
    for (const target of value.targets) {
      if (!target.available) { actions.append(el("p", "detail-notice", (backendLabels[target.agent] || target.agent) + " 接续入口暂不可用；原关系保留。")); continue; }
      const open = button(target.title || "未命名接续对话", "text-button", () => {
        if (current()) void nativeChats.selectExisting({ workspaceId: value.workspace_id, agent: target.agent, chatId: target.chat_id }, current)
          .catch(error => toast(userError(error, "这段接续对话暂时无法打开，原关系保留。")));
      });
      open.title = (backendLabels[target.agent] || target.agent) + " · 打开已有对话，不会发送"; actions.append(open);
    }
    group.append(actions); ui.actions.append(group);
  }
  const continuations = window.RabbitContinuations ? window.RabbitContinuations.create({
    root: $("continuation-panel"), getWorkspace: () => state.status?.workspace_id || "",
    getState: () => ({ continuations: state.continuations, currentContinuation: state.currentContinuation }),
    saveState: next => {
      const previous = { continuations: state.continuations, currentContinuation: state.currentContinuation };
      state.continuations = next.continuations; state.currentContinuation = next.currentContinuation;
      if (checkpointBrowserState()) return true;
      Object.assign(state, previous); return false;
    },
    getProfiles: () => profiles?.catalog(), onChooseConfiguration: kind => profiles?.open(kind),
    getSendShortcut: () => state.preferences.send_shortcut, onOpenSource: openContinuationSource,
    onOpenTarget: openContinuationTarget,
    onReadCreation: async (record, onProgress) => {
      const found = await nativeChats.readCreation(record.creation.creation_id, record.target.agent);
      return !!found.chat && onProgress({ created_chat_id: found.chat.id }) === true;
    },
    onSubmit: async (record, isCurrent, onProgress) => {
      record = window.RabbitBrowserState.continuation(record, state.status?.workspace_id);
      const preview = record.material_refs.length ? await api("/api/native-materials/preview", { method: "POST", body: JSON.stringify({ material_refs: record.material_refs }) }, false) : undefined;
      if (!isCurrent()) return { accepted: false };
      const body = { source_revision: record.snapshot.source.revision, message_ids: record.snapshot.records.map(row => row.message_id), task: record.task,
        ...(record.material_refs.length ? { registered_material_refs: record.material_refs } : {}) };
      const prepared = await window.RabbitNativeTextQuote.prepare(record.snapshot, body, record.target.agent, preview);
      const snapshotSha = await continuationDigest({ source_view: record.source_view, snapshot: record.snapshot, material_refs: record.material_refs });
      if (!isCurrent()) return { accepted: false };
      const origin = { target_agent: record.target.agent, target_chat_id: record.creation.created_chat_id,
        creation_id: record.creation.creation_id, source_view: record.source_view, source_revision: record.snapshot.source.revision,
        selected_message_ids: body.message_ids, material_refs: record.material_refs, snapshot_sha256: snapshotSha };
      return nativeChats.createContinuation(record, { text: prepared.prompt, material_refs: record.material_refs,
        ...(prepared.material_preview ? { material_preview: prepared.material_preview } : {}), memory_profile: record.target.memory_profile }, origin, isCurrent, onProgress);
    },
    onError: error => toast(userError(error, "接续尚未完成，原文与创建编号保留。")),
  }) : null;
  const nativeHandoff = window.RabbitNativeHandoff ? window.RabbitNativeHandoff.create({
    root: $("native-handoff-modal"), api, materialImages: materials,
    getSource: () => state.page === "conversations" && state.selected.type === "chat" && state.chat?.id === state.selected.id
      ? { chat_id: state.selected.id, workspace_id: state.status?.workspace_id || "", task: ui.input.value,
        source_key: JSON.stringify([state.openVersion, state.contextNavigationVersion]) } : null,
    getTargets: nativeHandoffTargets,
    placeDraft: (value, isCurrent) => nativeChats.putDraft(value, isCurrent),
    onError: error => toast(userError(error, "交接尚未完成，原草稿仍保留。")),
  }) : null;
  $("native-handoff")?.addEventListener("click", () => state.selected.type === "history" ? openHistoryTextHandoff() : nativeHandoff?.open());
  let nativeQuoteInput = null, nativeQuoteProfile = null;
  let historyTextReadVersion = 0;
  function historyTextSource() {
    const selected = state.selected, indexed = state.historySession, workspaceId = state.status?.workspace_id;
    if (state.page !== "conversations" || selected.type !== "history" || !state.historyReady || state.historyPageLoading
        || state.sending || state.creating || state.backendChoosing || currentUploadCount() > 0
        || typeof workspaceId !== "string" || !workspaceId || typeof selected.sessionId !== "string" || !selected.sessionId
        || selected.id !== `${selected.agent}:${selected.sessionId}` || indexed?.agent !== selected.agent || indexed.session_id !== selected.sessionId) return null;
    return { origin_kind: "history_index", workspace_id: workspaceId, chat_id: "indexed-history:" + state.openVersion,
      source_key: JSON.stringify([workspaceId, selected.agent, selected.sessionId, state.openVersion, state.contextNavigationVersion,
        historyTextReadVersion, state.historyMessageStart, state.historyMessagePrev, state.historyMessageNext, state.historyMessageTotal,
        state.historyMessages.map(row => [row?.message_id, row?.role, row?.phase]), state.sourceUpdateInputVersion, ui.input.value]) };
  }
  function nativeAdoptionEnabled(agent) {
    if (!["codex", "hermes", "pi", "claude", "dsh"].includes(agent)) return false;
    const entry = { codex: state.status?.native_chats, hermes: state.status?.hermes_native_chats,
      pi: state.status?.pi_native_chats, claude: state.status?.claude_native_chats,
      dsh: state.status?.dsh_native_chats }[agent];
    if (entry?.enabled !== true) return false;
    const advertised = state.status?.native_history?.enabled_agents;
    return Array.isArray(advertised) ? advertised.includes(agent) : agent === "codex";
  }
  function nativeAdoptionSource() {
    const source = historyTextSource();
    if (!source || !nativeAdoptionEnabled(state.selected.agent)) return null;
    const draft = quoteDraft();
    return { agent: state.selected.agent, session_id: state.selected.sessionId, workspace_id: source.workspace_id,
      scope: JSON.stringify([source.source_key, draft.stamp, draft.attachment_ids, historyChoice()]) };
  }
  async function placeHistoryNativeDraft(value, isCurrent) {
    if (!isCurrent()) return false;
    const target = { agent: value.chat.agent, chatId: value.chat.id, workspaceId: value.workspace_id };
    // With no new task, opening preserves the destination's draft and settings.
    // This also applies when a filtered list required idempotent registration.
    if (!draftHasContent()) return nativeChats.selectExisting(target, isCurrent);
    const source = captureNativeDraft("history"), current = () => isCurrent() && sameNativeDraft(source);
    const prepared = await prepareNativeDraft(value.chat.agent, source, current);
    if (!current()) return false;
    if (saveDraft() !== true) throw Error("浏览器不能保存当前草稿；文字和材料保留。");
    await refreshChats(current);
    if (!current()) return false;
    const opened = prepared ? await nativeChats.putDraft({ ...target, ...prepared }, current) : await nativeChats.selectExisting(target, current);
    if (!opened) throw Error("原生入口暂未打开，草稿尚未带入；原稿保留，请核对原生对话。");
    if (prepared) clearNativeDraft(source);
    return true;
  }
  function chooseHistoryMode(mode) {
    if (!["native", "text"].includes(mode) || !historyTextSource()) return false;
    const wasReadOnly = state.historyReadOnly;
    state.historyReadOnly = false;
    const id = state.selected.id, previous = historyChoice();
    state.historyChoices.set(id, { ...previous, mode }); state.backendChoiceVersion++;
    invalidateContextPreview("接续方式已更换，请重新查看本轮内容。");
    if (saveDraft() !== true) { state.historyReadOnly = wasReadOnly; state.historyChoices.set(id, previous); updateComposer(); toast("浏览器不能保存接续选择；原方式与草稿保留。"); return false; }
    historyHeader(); updateComposer(); return true;
  }
  async function continueNativeHistory() {
    if (!nativeAdoptionSource() || !nativeAdoption || !chooseHistoryMode("native")) return false;
    const source = nativeAdoptionSource(), current = () => JSON.stringify(nativeAdoptionSource()) === JSON.stringify(source);
    const matches = state.libraryItems.filter(row => row.kind === "native" && row.agent === source.agent && row.available === true
      && (Array.isArray(row.index_sources) ? row.index_sources : [row.index_source]).some(value => value?.agent === source.agent && value.session_id === source.session_id));
    if (matches.length > 1) { toast("这段历史关联了多个原生入口，请从对话列表明确选择；草稿保留。"); return false; }
    if (!matches.length) return nativeAdoption.open(); // A filtered or paginated list does not prove absence.
    try {
      return await placeHistoryNativeDraft({ chat: matches[0], workspace_id: source.workspace_id }, current);
    }
    catch (error) { if (current()) toast(userError(error, "原生对话暂时无法打开，草稿保留。")); return false; }
  }
  const nativeAdoption = window.RabbitNativeAdoption ? window.RabbitNativeAdoption.create({
    root: $("native-adoption-modal"), api, getSource: nativeAdoptionSource,
    onAdopted: async (value, isCurrent) => {
      try {
        await placeHistoryNativeDraft(value, isCurrent);
      } catch (error) {
        const failure = Error("native_draft_not_placed");
        failure.adoptionDraftMessage = userError(error, "草稿未能带入；来源页的文字和材料仍保留。请核对原生对话。");
        throw failure;
      }
    },
    onError: () => {},
  }) : null;
  $("native-adopt")?.addEventListener("click", continueNativeHistory);
  if (window.RabbitConversationGroups && markdownChats && $("conversation-group-modal")) {
    conversationGroups = window.RabbitConversationGroups.create({ api, root: $("conversation-group-modal"),
      filters: $("conversation-folder-filters"), heading: $("conversation-folder-heading"),
      directory: markdownChats, getWorkspace: () => state.status?.workspace_id || "",
      onSelect: folder => {
        state.historyAgent = "all"; state.browserAgentFilterTouched = true;
        sourceFilters().forEach(n => { const active = !folder && n.dataset.agent === "all"; n.classList.toggle("active", active); n.setAttribute("aria-pressed", String(active)); });
        loadHistoryList(true); checkpointBrowserState();
      },
      onAssign: prepareGroupSource, onError: error => toast(userError(error, "对话夹暂时未读出，请重新读取。")) });
  }
  async function prepareGroupSource(item, folder, directory) {
    const workspace = state.status?.workspace_id;
    if (!workspace || directory.workspace_id !== workspace || !directory.complete) throw Error("请重新读取完整的对话文件夹。");
    let source;
    if (item.kind === "chat") {
      const value = await api(`/api/chats/${encodeURIComponent(item.id)}`);
      if (workspace !== state.status?.workspace_id || value?.chat?.id !== item.id) throw Error("对话已变化，请重新选择。");
      source = value.chat;
    } else {
      let target = item;
      if (item.kind === "history") {
        const value = await api(`/api/markdown-chats/history-reference?${new URLSearchParams({ agent: item.agent, session_id: item.session_id })}`);
        target = value?.reference;
        if (value?.workspace_id !== workspace || target?.kind !== "history_reference" || target.agent !== item.agent || target.session_id !== item.session_id) throw Error("索引来源已变化，请重新选择。");
      }
      if (workspace !== state.status?.workspace_id || !target.id || !agents.has(target.agent)) throw Error("原生来源暂不可核对。");
      source = { id: target.id, title: target.title, agent: target.agent, kind: item.kind === "history" ? "history_reference" : "native_index", ...(item.kind === "history" ? { session_id: target.session_id } : {}) };
    }
    const saved = directory.chats.find(row => row.rabbit_id === source.id && (row.kind === "native_index" || row.kind === "history_reference" ? source.kind === row.kind && source.agent === row.agent : source.kind === undefined));
    if (saved) {
      if (await window.RabbitMarkdownChats.moveFile({ api, getWorkspace: () => state.status?.workspace_id,
          directory, source, folder })) {
        try { await markdownChats.readDirectory(); }
        catch (_) {
          const error = Error("文件已移动，目录暂未更新。请关闭后重新读取。");
          error.groupObservationRequired = true;
          throw error;
        }
      }
      return null;
    }
    return source;
  }
  function openHistoryTextHandoff() {
    const context = historyTextSource();
    if (!context || !nativeTextHandoff || typeof window.RabbitNativeTextQuote?.readIndexed !== "function") { toast("请先打开并读取一段历史索引对话，原草稿保留。"); return false; }
    let incomplete = state.historyMessageStart > 0 || state.historyMessagePrev !== null || state.historyMessageNext !== null
      || state.historyMessageTotal > state.historyMessages.length || state.historySession.coverage?.continuable === false;
    const coverage = state.historySession.coverage;
    if (coverage && Object.entries(coverage).some(([key, value]) => /^(?:omitted_|missing_|invalid_|unsupported_|unparsed_|ambiguous_|attachment_only_)/.test(key) && Number(value) > 0)) incomplete = true;
    const counts = new Map(); for (const row of state.historyMessages) counts.set(row?.message_id, (counts.get(row?.message_id) || 0) + 1);
    if ([...counts].some(([id, count]) => typeof id === "string" && id && count > 1)) { toast("历史索引出现重复消息编号，请重新读取历史；整份未交接。"); return false; }
    const records = [];
    for (const row of state.historyMessages) {
      if (row && (hasImageRecords(row) || row.attachment_summary)) incomplete = true;
      if (!row || !["user", "assistant"].includes(row.role) || typeof row.message_id !== "string" || !row.message_id
          || ![undefined, "", "commentary", "final_answer", "final"].includes(row.phase)
          || row.message_kind !== undefined && !["user", "assistant"].includes(row.message_kind)
          || typeof row.content !== "string" || /[\ud800-\udfff]/u.test(row.content) || row.truncated === true) { incomplete = true; continue; }
      if (row.content.length) records.push({ message_id: row.message_id, role: row.role, content: row.content, ...(row.phase === undefined ? {} : { phase: row.phase }) });
      else incomplete = true;
    }
    nativeHandoff?.close(); nativeTextHandoff.close();
    nativeQuoteInput = { ...context, agent: state.selected.agent, session_id: state.selected.sessionId,
      title: state.historySession.title || "历史索引对话", records, incomplete };
    return nativeTextHandoff.open();
  }
  const nativeTextHandoff = window.RabbitNativeHandoff && window.RabbitNativeTextQuote ? window.RabbitNativeHandoff.create({
    root: $("native-text-handoff-modal"), api: (path, options = {}) => api(path, options, false),
    getSource: () => nativeQuoteInput?.origin_kind === "history_index" ? historyTextSource() : nativeChats?.getQuoteContext(),
    getTargets: nativeHandoffTargets,
    sourceAdapter: {
      release: () => { nativeQuoteInput = nativeQuoteProfile = null; },
      read: async captured => {
        if (!nativeQuoteInput || captured.source_key !== nativeQuoteInput.source_key || captured.workspace_id !== nativeQuoteInput.workspace_id) throw Error("原生文字来源已切换，请重新打开。");
        const input = nativeQuoteInput;
        if (input.origin_kind === "history_index") return window.RabbitNativeTextQuote.readIndexed(input);
        let inventory;
        try { inventory = await api("/api/management/chat-materials?" + new URLSearchParams({ kind: "native", agent: input.agent, chat_id: input.chat_id }), {}, false); }
        catch (_) { toast("登记资料暂时未读到；这里只显示已读文字。"); }
        return window.RabbitNativeTextQuote.read(input, inventory);
      },
      prepare: async ({ captured, source, records, snapshot, body, targetAgent }) => {
        const preview = body.registered_material_refs?.length ? await api("/api/native-materials/preview", { method: "POST", body: JSON.stringify({ material_refs: body.registered_material_refs }) }, false) : undefined;
        return window.RabbitNativeTextQuote.prepare(snapshot || { snapshot_kind: "native_text", workspace_id: captured.workspace_id, source, records }, body, targetAgent, preview);
      },
    },
    onPrepareOrigin: async (value, isCurrent) => {
      const scope = value.workspace_id, snapshot = value.snapshot, source = snapshot?.source;
      if (!isCurrent() || scope !== state.status?.workspace_id || source?.origin_kind !== "history_index") return false;
      const response = await api("/api/markdown-chats/history-reference?" + new URLSearchParams({ agent: source.agent, session_id: source.history_session_id }), {}, false);
      if (!isCurrent()) return false;
      const reference = response?.reference;
      if (!continuationKeys(response, ["workspace_id", "reference"]) || response.workspace_id !== scope
          || !continuationKeys(reference, ["kind", "id", "agent", "session_id", "title"]) || reference.kind !== "history_reference"
          || reference.agent !== source.agent || reference.session_id !== source.history_session_id || !continuationUUID(reference.id)) throw Error("原索引引用身份待核对；原目标和草稿保留。");
      const sourceView = { kind: "history_reference", workspace_id: scope, id: reference.id, agent: source.agent, session_id: source.history_session_id, title: source.title };
      const snapshotSha = await continuationDigest({ source_view: sourceView, snapshot, material_refs: value.material_refs });
      if (!isCurrent()) return false;
      return saveContinuationOrigin({ target_agent: value.target_agent, target_chat_id: value.target_chat_id, creation_id: value.creation_id,
        source_view: sourceView, source_revision: source.revision, selected_message_ids: value.selected_message_ids,
        material_refs: value.material_refs, snapshot_sha256: snapshotSha }, isCurrent);
    },
    onPrepareContinuation: async (value, isCurrent) => {
      const profile = nativeQuoteProfile?.agent_instance_id === "local/" + value.target_agent ? nativeQuoteProfile : null;
      if (!continuations || !isCurrent() || await continuations.prepare(value, profile) !== true) return false;
      if (!isCurrent()) return false;
      state.browserPageTouched = true; showPage("continuation"); return true;
    },
    placeDraft: (value, isCurrent) => nativeChats.putDraft(value, isCurrent),
    onError: error => toast(userError(error, "原生文字接力尚未完成，原草稿保留。")),
  }) : null;
  function resumeNativeContinuation(value, chosenProfile = null) {
    if (continuations && ["codex", "hermes"].includes(value?.agent)) {
      const saved = continuations.findBySource({ workspace_id: value.workspace_id, agent: value.agent, chat_id: value.chat_id });
      if (saved) {
        if (continuations.show(saved.id) !== true) return false;
        state.browserPageTouched = true; showPage("continuation");
        if (chosenProfile) continuations.chooseConfiguration("ai", chosenProfile, continuations.getComposerView()?.view);
        return true;
      }
    }
    return false;
  }
  function openNativeTextHandoff(value, chosenProfile = null) {
    if (!nativeTextHandoff) throw Error("原生文字接力组件尚未加载。");
    if (resumeNativeContinuation(value, chosenProfile)) return true;
    nativeHandoff?.close(); nativeTextHandoff.close(); nativeQuoteInput = value; nativeQuoteProfile = chosenProfile;
    return nativeTextHandoff.open(chosenProfile ? agents.backendOfInstance(chosenProfile.agent_instance_id) : null);
  }
  let nativeFileInput = null;
  const nativeFileHandoff = window.RabbitNativeHandoff && window.RabbitNativeFileQuote ? window.RabbitNativeHandoff.create({
    root: $("native-file-handoff-modal"), api: (path, options = {}) => api(path, options, false),
    getSource: () => nativeFileInput?.workspace_id === state.status?.workspace_id ? {
      origin_kind: "file_material", workspace_id: nativeFileInput.workspace_id,
      material_id: nativeFileInput.artifact.id, source_key: nativeFileInput.source_key,
    } : null,
    getTargets: () => nativeHandoffTargets().filter(item => ["codex", "pi", "hermes", "claude", "dsh"].includes(item.agent)),
    sourceAdapter: {
      kind: "file_material",
      release: () => { nativeFileInput = null; },
      read: captured => {
        if (!nativeFileInput || captured.source_key !== nativeFileInput.source_key
            || captured.material_id !== nativeFileInput.artifact.id || captured.workspace_id !== nativeFileInput.workspace_id) throw Error("文件资料来源已变化，请重新选择。");
        return window.RabbitNativeFileQuote.read({ workspace_id: nativeFileInput.workspace_id,
          artifact: nativeFileInput.artifact, text: nativeFileInput.text });
      },
      prepare: ({ snapshot, body, targetAgent }) => window.RabbitNativeFileQuote.prepare(snapshot, { task: body.task }, targetAgent),
    },
    placeDraft: (value, isCurrent) => nativeChats.putDraft(value, isCurrent),
    onError: error => toast(userError(error, "文件资料接力尚未完成，原草稿保留。")),
  }) : null;
  let workspaceReady = false;
  const workStatus = window.RabbitWorkStatus ? window.RabbitWorkStatus.create({
    api: (path, options) => api(path, options, false),
    getWorkspace: () => state.status?.workspace_id || "", translate: uiText,
    onSnapshot: observation => {
      if (observation || state.page === "management" && state.workspaceSections.management === "agents") {
        nativeWorkRead++; acceptNativeWorkStatus(observation);
      }
    },
    getLabel: row => state.libraryWorkspace === state.status?.workspace_id
      ? state.libraryItems.find(item => item.kind === "native" && item.agent === row.agent && item.id === row.chat_id)?.title || "" : "",
    onOpen: async (row, isCurrent) => {
      const current = () => isCurrent() && row.workspace_id === state.status?.workspace_id
        && state.page === "management" && state.workspaceSections.management === "agents";
      if (!current()) return false;
      try {
        if (row.role === "specialist") {
          if (!specialistEnabled()) throw Error("小白兔专员入口暂不可用。");
          state.browserPageTouched = true; showPage("specialist"); return true;
        }
        if (!nativeChats?.selectExisting) throw Error("原生对话入口暂不可用。");
        return await nativeChats.selectExisting({ agent: row.agent, chatId: row.chat_id, workspaceId: row.workspace_id }, current);
      } catch (error) {
        if (current()) toast(userError(error, "原工作暂时没打开，原对话和草稿仍保留。"));
        return false;
      }
    },
  }) : null;
  const workspace = window.RabbitWorkspace ? window.RabbitWorkspace.create({
    root: $("workspace-panel"), api, getWorkspace: () => state.status?.workspace_id || "",
    workStatus,
    getOverview: () => myAI?.snapshot(),
    onBackToMyAI: () => showPage("my-ai"),
    onOpenWork: (action, isCurrent = () => true) => isCurrent() ? openMyAIAction(action) : false,
    getSelection: () => ({ backend: state.backend, model_id: state.model_id, chatId: state.selected.type === "chat" ? state.selected.id : null, chatTitle: state.chat && state.chat.title || state.selected.type === "history" && state.historySession && state.historySession.title }),
    selectBackend, selectModel, onPreferences: applyPreferences, profiles, profilesEnabled, onNavigate: showPage,
    renderSpecialistSettings: container => container.append(settingsMounts.specialist),
    renderNativeSettings: container => container.append(settingsMounts.native),
    onSectionChange: ({ page, tab }) => { state.workspaceSections[page] = tab; if (workspaceReady) { state.managementTarget = workspace?.getObject?.() || null; if (state.page === page) workspaceHeader(); renderSidebar(); checkpointBrowserState(); } },
    onLanguageChange: updateBasicLanguage, onAddResource: openConnectionGuide, onManual: openManual,
    onManagement: (data) => {
      state.management = data;
      if (state.status) state.status = { ...state.status, backends: data.agents };
      profiles?.setInventory();
      updateComposer();
    },
  }) : null;
  workspaceReady = true;
  const dashboard = window.RabbitDashboardSections?.create({ api, getWorkspace: () => state.status?.workspace_id || "",
    onConnection: id => openMyAIAction({ kind: "manage", target: { kind: "agent", id, section: "connection", fromMyAI: true, backTo: "usage" } }),
  });
  myAI = window.RabbitMyAIPage?.create({ root: $("my-ai-panel"), api, getWorkspace: () => state.status?.workspace_id || "",
    sections: dashboard?.sections,
    onViewChange: () => { state.browserPageTouched = true; checkpointBrowserState(); },
    onRead: value => { state.management = value.management; profiles?.setInventory(); updateComposer(); },
    onAction: openMyAIAction,
  });
  async function openMyAIAction(action) {
      state.browserPageTouched = true;
      if (action.kind === "conversations") { await switchListView(); toggleSidebar(true); return; }
      const target = action.target;
      if (action.kind === "manage") {
        const tab = target.kind === "agent" ? "agents" : target.kind === "model" ? "models" : "memory-profiles";
        state.managementTarget = target.id ? { kind: target.kind, id: target.id } : null;
        showPage("management", tab); if (target.id) await workspace?.openObject?.({ ...target, fromMyAI: target.kind === "agent" }); checkpointBrowserState(); return;
      }
      if (target?.kind === "native") return nativeChats?.openEntry({ agent: target.agent, chatId: target.id, workspaceId: state.status.workspace_id }, { readOnly: action.kind === "read" || target.read_only });
      if (target?.kind === "history") return openHistory(target.agent, target.session_id, null, 0, true);
      if (target?.kind === "chat") return openChat(target.id);
      throw Error("这项工作没有可核对的原始编号。原稿保留。");
  }
  function uiText(text) { return window.RabbitWorkspace?.translate?.(text) || text; }
  function updateBasicLanguage() {
    for (const [id, text] of [["my-ai-nav", "我的 AI"], ["conversations-nav", "对话"], ["management-nav", "管理"], ["settings-nav", "设置"]]) if ($(id)) $(id).textContent = uiText(text);
    specialist?.setLanguage(window.RabbitWorkspace?.language?.() || "zh-CN");
    nativeChats?.setLanguage(window.RabbitWorkspace?.language?.() || "zh-CN");
    $("new-chat").textContent = "＋ " + uiText("新对话");
    for (const [id, text] of [["choose-ai-profile", "AI 配置"], ["choose-memory-profile", "记忆配置"]]) {
      const label = $(id).querySelector("span"); if (label) label.textContent = uiText(text);
    }
    // create() calls this synchronously: do not read the workspace binding here.
    if (state.page !== "conversations") workspaceHeader();
    renderList();
    if (workspaceReady) renderSidebar();
  }
  function openConnectionGuide(kind) {
    if (!["agent", "model"].includes(kind)) return;
    return openManual(kind === "agent" ? "connect-agent" : "connect-model");
  }
  function openManual(topic = "intro") {
    const version = ++documentVersion;
    documentDraftPreview = null;
    if ($("document-viewer").hidden) documentReturnFocus = document.activeElement;
    $("document-viewer").hidden = false;
    $("document-title").textContent = "小白兔使用手册";
    $("document-meta").replaceChildren();
    const content = $("document-content"), text = readBundledHelp()?.manual;
    content.replaceChildren(); content.classList.add("builtin-manual");
    $("close-document").focus();
    if (!text) { content.append(el("p", "", "使用手册未随界面载入，请重新打开小白兔。")); return false; }
    const body = el("div", "manual-body"), index = el("nav", "manual-index");
    index.setAttribute("aria-label", "手册目录");
    if (window.OneAIRichText?.render) window.OneAIRichText.render(body, text);
    else { const plainText = el("pre", "document-lines", text); body.append(plainText); }
    const topics = { intro: "小白兔是什么", ai: "AI 配置", memory: "记忆配置", native: "Agent 会话", specialist: "小白兔专员", settings: "应用设置", "connect-agent": "接入 Agent", "connect-model": "接入大模型", problems: "遇到问题" };
    const headings = [...body.querySelectorAll("h2,h3")], sections = new Map();
    for (const [key, label] of Object.entries(topics)) {
      const heading = headings.find(node => node.textContent.trim() === label);
      if (!heading) continue;
      sections.set(key, heading);
      index.append(button(label, "document-link", () => heading.scrollIntoView({ block: "start" })));
    }
    content.append(index, body); content.scrollTop = 0;
    const section = sections.get(topic) || headings.find(node => node.textContent.trim() === topic);
    if (section) requestAnimationFrame(() => { if (version === documentVersion && !$("document-viewer").hidden && content.classList.contains("builtin-manual")) section.scrollIntoView({ block: "start" }); });
    return true;
  }
  let routeSignature = "";
  function updateRoute() {
    const info = backendInfo(state.backend), model = modelInfo();
    const signature = JSON.stringify([state.backend, state.model_id, model && model.available, info && info.model, state.management && state.management.device, state.selected.type, state.selected.id, state.chat && state.chat.title]);
    if (workspace && signature !== routeSignature) workspace.selectChanged();
    routeSignature = signature;
  }
  function applyPreferences(prefs) {
    const previous = state.preferences;
    state.preferences = prefs;
    state.includeInternal = !!prefs.show_internal;
    const choice = state.selected.type === "chat" && prefs.chat_backends && prefs.chat_backends[state.selected.id];
    if (!profilesEnabled() && !state.sending && !state.backendChoosing && backendLabels[choice]) { state.backend = choice; const savedModel = Object.hasOwn(prefs.chat_models || {}, state.selected.id) ? prefs.chat_models[state.selected.id] : validModelId(state.model_id, choice) ? state.model_id : ""; state.model_id = validModelId(savedModel, choice) ? savedModel : ""; }
    else if (!profilesEnabled() && state.selected.type === "new" && !state.backendChoosing && (previous.default_backend !== prefs.default_backend || !state.backendChoiceVersion && JSON.stringify(previous.default_models) !== JSON.stringify(prefs.default_models))) { state.backend = prefs.default_backend; state.model_id = defaultModelId(state.backend); }
    if (previous.show_internal !== prefs.show_internal) loadHistoryList(true);
    if (previous.markdown !== prefs.markdown) {
      state.messageSignature = "";
      if (state.selected.type === "history") renderMessages(state.historyMessages, { history: true });
      else if (state.chat) renderMessages(state.chatMessages, { appendNotice: sourceNotice(state.chat) });
    }
    updateComposer();
  }
  async function rememberReplyRoute(chatId, backend, modelId) {
    const prefs = await api(`/api/chats/${encodeURIComponent(chatId)}/backend`, { method: "PUT", body: JSON.stringify({ backend, model_id: modelId }) });
    state.preferences = prefs; if (workspace) workspace.applyPreferences(prefs);
    return prefs;
  }
  async function selectBackend(id) { return selectReplyRoute(id, defaultModelId(id)); }
  async function selectModel(modelId, backend) {
    const row = modelId && state.management?.models?.find(model => model.id === modelId);
    return selectReplyRoute(backend || row?.agent_id || state.backend, modelId);
  }
  async function selectReplyRoute(id, modelId = "") {
    if (!agents.has(id) || !validModelId(modelId, id) || state.sending || state.creating || state.backendChoosing) return false;
    if (profilesEnabled() && profiles) { showPage("management", "ai-profiles"); profiles.newFor({ agent_instance_id: agents.instanceId(id), model_id: modelId }); return true; }
    const info = backendInfo(id), chosenModel = modelInfo(modelId, id);
    if (!info || !info.available || !chosenModel || !chosenModel.available) { toast(modelId ? chosenModel?.detail || chosenModel?.reason || "这个模型目前不可选，请重新检测。" : backendUnavailableReason(info)); return false; }
    const chatId = state.selected.type === "chat" ? state.selected.id : null;
    const version = state.openVersion, choiceVersion = ++state.backendChoiceVersion;
    invalidateSourceUpdate("已更换回答组合，请重新查看来源更新。");
    invalidateSourceImages("已更换回答组合，请重新查看来源图片。");
    state.backendChoosing = true; updateComposer();
    try {
      if (chatId) {
        await rememberReplyRoute(chatId, id, modelId);
      }
      if (choiceVersion === state.backendChoiceVersion && version === state.openVersion) {
        state.backend = id; state.model_id = modelId;
        if (state.selected.type === "history") { state.historyBackendChoices.set(state.selected.id, id); state.historyModelChoices.set(state.selected.id, modelId); state.historyChoices.set(state.selected.id, { ...historyChoice(), mode: "text" }); }
        if (!state.pendingSubmission || !state.pendingSubmission.uncertain) state.transientError = "";
        renderRunNotice(); updateComposer(); checkpointBrowserState();
      }
      return true;
    } catch (error) { toast(userError(error, "选择没有保存，请再试一次。")); return false; }
    finally { state.backendChoosing = false; updateComposer(); }
  }
  function workspaceHeader() {
    if (state.page === "my-ai") {
      ui.title.textContent = "我的 AI"; ui.eyebrow.textContent = "";
      ui.meta.textContent = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "long" }).format(new Date());
      ui.actions.replaceChildren(); ui.composer.hidden = true; return;
    }
    if (state.page === "continuation") {
      ui.title.textContent = "接着《" + (continuations?.current()?.source_view.title || "原对话") + "》";
      ui.eyebrow.textContent = ui.meta.textContent = ""; ui.actions.replaceChildren(); ui.composer.hidden = true; return;
    }
    if (state.page === "native-chats") {
      const view = nativeChats?.getViewContext(), row = view && state.libraryItems.find(item => item.kind === "native" && item.agent === view.agent && item.id === view.chat_id);
      ui.title.textContent = view?.title || row?.title || (window.RabbitWorkspace?.language?.() === "en" ? "Native chats" : "原生对话");
      ui.eyebrow.textContent = ui.meta.textContent = "";
      ui.actions.replaceChildren(); appendNativeOrigin(); appendHistoryOriginTargets(); ui.composer.hidden = true;
      return;
    }
    if (state.page === "specialist") {
      ui.title.textContent = window.RabbitWorkspace?.language?.() === "en" ? "Rabbit specialist conversation" : "小白兔专员对话框";
      ui.eyebrow.textContent = "";
      ui.meta.textContent = "";
      ui.actions.replaceChildren();
      if (specialistBinding?.return_to && specialistBinding.workspace_id === state.status?.workspace_id) ui.actions.append(button("返回原页面", "text-button", returnFromSpecialist));
      ui.composer.hidden = true;
      return;
    }
    const section = state.workspaceSections[state.page];
    ui.title.textContent = state.page === "management" && workspace?.getObjectTitle?.() || workspace?.getNavigation(state.page).find(value => value.id === section)?.label || uiText(state.page === "management" ? "管理" : "设置");
    ui.eyebrow.textContent = "";
    ui.meta.textContent = "";
    ui.actions.replaceChildren(); ui.composer.hidden = true;
  }
  function headerMore(items) {
    const details = el("details", "conversation-more"), actions = el("div", "conversation-more-actions");
    details.append(el("summary", "", "更多"));
    actions.append(...items); details.append(actions);
    return details;
  }
  function originalAppEntry(agent, threadId) {
    const href = agents.originalAppUrl?.(agent, threadId);
    if (!href) return null;
    const link = el("a", "text-button original-app-link original-app-entry", "在 Codex 打开原对话");
    link.href = href; link.setAttribute("data-control-interface", "human");
    return link;
  }
  function historyHeader() {
    if (state.page !== "conversations") { workspaceHeader(); return; }
    const history = state.historySession || {};
    ui.eyebrow.textContent = `${backendLabels[history.agent] || "AI"} · 原有记录`;
    ui.title.textContent = history.title || "正在打开对话…";
    ui.meta.textContent = `${fullDate(history.updated_at)} · ${state.historyMessageTotal} 条消息${history.archived ? " · 已归档" : ""}`;
    const continuation = state.historyContinuationIds.get(state.selected.id);
    ui.actions.replaceChildren();
    const extra = [];
    const original = state.historyReady && state.selected.type === "history" && history.agent === state.selected.agent
      && history.session_id === state.selected.sessionId && state.selected.id === `${history.agent}:${history.session_id}`
      ? originalAppEntry(history.agent, history.session_id) : null;
    if (original) extra.push(original);
    if (nativeAdoptionEnabled(state.selected.agent)) extra.push(button("用文字副本接续", "text-button", () => { if (chooseHistoryMode("text")) profiles?.open("ai"); }));
    if (continuation) {
      const open = button("打开接续对话", "text-button", continueHistory); open.disabled = state.creating || state.sending;
      extra.push(open, button("查看来源更新", "text-button source-update-entry", () => openSourceUpdate(continuation)), button(state.sourceImagePending.has(continuation) ? "检查上次图片补入" : "补入来源图片", "text-button source-images-entry", () => openSourceImages(continuation)));
    }
    if (extra.length) ui.actions.append(headerMore(extra));
    appendHistoryOriginTargets();
    ui.composer.hidden = false;
  }
  function renderSidebar() {
    const categories = state.page === "management" || state.page === "settings";
    const nextView = categories ? state.page : "conversations";
    const changed = sidebarView !== nextView;
    if (changed) {
      sidebarScroll[sidebarView] = ui.sidebarContent.scrollTop;
      sidebarView = nextView;
    }
    $("sidebar-chat-content").hidden = categories;
    const list = $("sidebar-section-list"); list.hidden = !categories;
    $("specialist-composer-card").dataset.active = String(state.page === "specialist");
    if (categories && workspaceReady) {
      const current = state.workspaceSections[state.page];
      list.replaceChildren(...(workspace?.getNavigation(state.page) || []).map(item => {
        const control = button(item.label, "sidebar-section", () => { state.browserPageTouched = true; showPage(state.page, item.id); });
        control.classList.toggle("active", current === item.id);
        if (current === item.id) control.setAttribute("aria-current", "page");
        return control;
      }));
    }
    if (changed) ui.sidebarContent.scrollTop = sidebarScroll[sidebarView];
  }
  function showPage(page, tab, { keepSidebar = false } = {}) {
    fileTasks?.close(); fileMaterials?.close(); projectPublish?.close();
    if (!['my-ai', 'conversations', 'management', 'settings', 'specialist', 'native-chats', 'continuation'].includes(page)) return;
    if (page === "specialist" && !specialistEnabled()) return;
    if (page === "native-chats" && !nativeChatsEnabled() && !nativeChats?.hasReadOnlySelection()) return;
    specialistLocationRevision++;
    saveDraft();
    profiles?.close(false);
    const previousPage = state.page;
    const guide = tab || specialistBinding?.guide || "model";
    if (previousPage !== page) { closeSourceUpdate(false); closeSourceImages(false); }
    if (previousPage !== page) ++state.contextNavigationVersion;
    if (previousPage === "conversations" && page !== "conversations") invalidateContextPreview("已离开对话页，请返回后重新查看本轮上下文。");
    if (previousPage === "conversations") state.conversationScrollTop = Math.max(0, Number(ui.scroll.scrollTop) || 0);
    state.page = page;
    specialist?.setPassive?.(page === "my-ai");
    if (page !== "my-ai") myAI?.hide();
    if (statusReportMount && page === "specialist") $("specialist-guide-panel")?.querySelector(".rbs-guide")?.append(statusReportMount);
    $("specialist-layout").hidden = page !== "specialist";
    if (page !== "specialist") specialist?.hide();
    if (page !== "specialist") statusChecks?.hide();
    if (page !== "native-chats") nativeChats?.hide();
    if (page !== "continuation") continuations?.hide();
    document.querySelectorAll(".page-nav").forEach((node) => {
      const active = node.dataset.page === (["specialist", "native-chats", "continuation"].includes(page) ? "conversations" : page);
      node.classList.toggle("active", active);
      if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    });
    ui.scroll.hidden = page !== "conversations";
    if (page === "my-ai") {
      workspace?.hide(); workspaceHeader(); myAI?.show();
    } else if (page === "conversations") {
      if (previousPage !== "conversations") ui.scroll.scrollTop = state.conversationScrollTop;
      if (state.selected.type === "new" && state.homeNativeChoice?.profile) { state.profile_snapshot = toProfileSnapshot(state.homeNativeChoice.profile, state.homeNativeChoice.memory); state.backend = state.homeNativeChoice.agent; state.model_id = state.homeNativeChoice.profile.model_id; }
      if (workspace) workspace.hide();
      if (state.selected.type === "history") historyHeader(); else headerForChat(state.chat);
    } else if (page === "native-chats") {
      workspaceHeader(); workspace?.hide(); nativeChats.show();
    } else if (page === "continuation") {
      workspace?.hide(); workspaceHeader();
      continuations?.show();
    } else if (page === "specialist") {
      workspaceHeader(); workspace?.hide(); statusChecks?.hide();
      const location = specialistLocationRevision, scope = state.status?.workspace_id;
      if (scope) { const current = () => state.page === "specialist" && location === specialistLocationRevision; void readSpecialistGuideHistory(scope, current); void readSpecialistDshGuide(scope, current); }
      // Initial native-history restoration uses the same holder as tool facts.
      // Read the report after it releases that holder, and only for this visit.
      Promise.resolve(specialist.show({ restoreHistory: true })).then(() => {
        if (state.page === "specialist" && location === specialistLocationRevision && scope === state.status?.workspace_id && specialistEnabled()) return statusChecks?.show();
      }).catch(error => {
        if (state.page === "specialist" && location === specialistLocationRevision && scope === state.status?.workspace_id) toast(userError(error, "专员记录尚未读完，状态报告暂未读取。"));
      });
    } else {
      workspaceHeader(); if (workspace) workspace.show(page, tab || state.workspaceSections[page]);
      if (page === "settings" && state.workspaceSections.settings === "specialist") specialist?.showSettings();
      if (page === "settings" && state.workspaceSections.settings === "native-chats") nativeChats?.showSettings();
    }
    updateComposer();
    renderSidebar();
    if (page === "specialist") {
      if (previousPage !== "specialist" || tab) specialist.openGuide?.(guide);
      toggleSidebar(true, false); specialist.focusComposer?.();
    } else if (!keepSidebar) toggleSidebar(false);
    checkpointBrowserState();
  }
  function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; }
  function resetMessageView() {
    fileTasks?.close(); fileMaterials?.close(); projectPublish?.close();
    state.messageSignature = "";
    state.rawMessages = false;
    ui.messages.replaceChildren();
    ui.scroll.scrollTop = 0; state.conversationScrollTop = 0;
  }
  function showWelcome() {
    const welcome = el("div", "welcome");
    const hero = el("div", "welcome-hero"), copy = el("div", "welcome-copy"), heading = el("h3");
    for (const line of ["新的一段，", "从这里开始"]) heading.append(el("span", "", line));
    copy.append(heading, el("p", "", "选择已有的 AI 配置，写下这次想做的事。"));
    const icon = el("img", "welcome-icon"); icon.src = "/rabbit-app-1024.png"; icon.alt = ""; icon.width = icon.height = 200;
    hero.append(copy, icon);
    const actions = el("div", "welcome-actions");
    actions.append(button("开始新对话　→", "", () => ui.input.focus()), button("接着已有对话　→", "", () => { showPage("conversations"); toggleSidebar(true); ui.search.focus(); }));
    welcome.append(hero, actions);
    ui.messages.replaceChildren(welcome);
  }
  function headerForChat(chat) {
    if (state.page !== "conversations") { workspaceHeader(); return; }
    ui.eyebrow.textContent = "对话";
    ui.title.textContent = chat && chat.title || "新对话";
    ui.meta.textContent = chat && chat.updated_at ? `最近更新 ${fullDate(chat.updated_at)} · 每轮可以换 AI` : "每一轮，都可以选择回答你的 AI";
    ui.actions.replaceChildren();
    if (chat && chat.id && state.selected.type === "chat") {
      const secondary = [];
      if (chat.source && chat.source.agent && chat.source.session_id) {
        const original = chat.id === state.selected.id ? originalAppEntry(chat.source.agent, chat.source.session_id) : null;
        if (original) secondary.push(original);
        secondary.push(button("显示原对话", "text-button source-view-entry", () => viewChatSource(chat)), button("查看来源更新", "text-button source-update-entry", () => openSourceUpdate(chat.id)), button(state.sourceImagePending.has(chat.id) ? "检查上次图片补入" : "补入来源图片", "text-button source-images-entry", () => openSourceImages(chat.id)));
      }
      const exportButton = button(state.exporting ? "导出中…" : "导出文字", "text-button export-button", exportChat);
      exportButton.disabled = state.exporting;
      exportButton.title = "将全部文字与图片数量、状态下载为 Markdown 文件；未打包图片文件";
      secondary.push(exportButton);
      if (markdownChats?.enabled()) {
        const organize = button("整理到对话夹", "text-button", () => markdownChats.open(chat).catch(error => toast(userError(error, "整理面板尚未打开。"))));
        organize.dataset.controlInterface = "ai";
        secondary.push(organize);
      }
      ui.actions.append(headerMore(secondary));
    }
    ui.composer.hidden = false;
    appendListSearchMatch();
    ui.messages.setAttribute("aria-live", "polite");
  }
  function exportTime(value) {
    if (value == null || value === "") return "未记录";
    if (typeof value === "string" && !/^\d+(\.\d+)?$/.test(value)) return value.replace(/[\r\n]+/g, " ");
    const date = dateValue(value);
    return date ? date.toISOString() : plain(value).replace(/[\r\n]+/g, " ");
  }
  function modelSourceLabel(value) { return value === "requested_route" ? "请求参数回显（不能证明厂商内部实际执行型号）" : value === "adapter_reported" ? "接入器报告（不能证明厂商内部实际执行型号）" : "原记录未提供来源，实际执行型号未知"; }
  function profileExportLines(value) {
    let snap = null; try { snap = profileSnapshot(value); } catch (_) { return ["- AI 配置快照：原记录暂时无法读取"]; }
    if (!snap) return [];
    const row = snap.ai_profile;
    const memory = snap.memory_profile;
    return [`- 本次 AI 配置：${markdownLabel(row.name)} · 第 ${row.revision} 版`, `- 配置标识：${row.id}`, `- 请求 Agent：${row.agent_instance_id}`, `- 执行设备：本机`, `- 请求模型：${markdownLabel(row.model_id || "跟随 Agent 默认")}`, `- 请求强度：${row.reasoning_effort || "跟随 Agent 默认"}`, ...(memory.id === "none" ? ["- 长期记忆：不使用；当前聊天上文照常保留"] : [`- 本次记忆配置：${markdownLabel(memory.name)} · 第 ${memory.revision} 版`, `- 记忆程序：${window.RabbitProfiles.memoryProgramLabel(memory.program)} · ${memory.mode === "read_write" ? "读取并记住" : "只读取"}`, `- 记忆范围引用：${memory.bank_ref}`, ...(memory.habits ? [`- 本轮身份与习惯：${markdownLabel(memory.habits)}`] : [])])];
  }
  function safeMemoryInfo(value) { try { return window.RabbitProfiles?.memoryInfo(value) || null; } catch (_) { return null; } }
  function memoryStatusText(value) {
    const info = safeMemoryInfo(value); if (!info) return value == null ? "" : "本轮记忆状态暂时无法读取，已有回答保留。";
    const read = info.recall.state === "skipped" ? "本轮未查询长期记忆" : info.recall.state === "failed" ? "长期记忆检索失败" : info.recall.included_facts ? `本轮带入 ${info.recall.included_facts} 条长期记忆${info.recall.omitted_facts ? `，另有 ${info.recall.omitted_facts} 条未带入` : ""}` : "长期记忆检索完成，本轮未带入事实";
    const write = { disabled: "未保存本轮新交互", not_applicable: "本轮尚未提交记忆保存", queued: "记忆提取任务已排队", submitting: "正在提交记忆提取任务", processing: "记忆提取任务处理中", completed: info.retain.fact_count === 0 ? "提取任务处理完成，未提取事实" : info.retain.fact_count > 0 ? `提取任务处理完成，得到 ${info.retain.fact_count} 条事实` : "提取任务处理完成", failed: "记忆保存失败，已有回答不受影响", unknown: "记忆保存结果未确认，已有回答保留" }[info.retain.state];
    return `${read}；${write}。`;
  }
  function memoryExportLines(value) {
    const info = safeMemoryInfo(value); if (!info) return value == null ? [] : ["- 本轮记忆状态：暂时无法读取"];
    const lines = [`- 本轮记忆状态：${memoryStatusText(info)}`];
    for (const ref of info.recall.used_references) lines.push(`- 记忆来源：${ref.reference_id} · 配置 ${ref.memory_profile_id} 第 ${ref.memory_profile_revision} 版 · ${ref.type} · ${ref.retrieved_at}`, "", fencedOriginal(ref.text), "");
    if (info.retain.operation_id) lines.push(`- 记忆任务标识：${info.retain.operation_id}`);
    if (info.retain.error_code) lines.push(`- 记忆任务错误码：${markdownLabel(info.retain.error_code)}`);
    return lines;
  }
  function appendMemoryInfo(container, value) {
    if (value == null) return;
    const info = safeMemoryInfo(value); container.append(el("p", "memory-state", memoryStatusText(value))); if (!info) return;
    appendMemoryReferences(container, info.recall);
  }
  function appendMemoryReferences(container, recall, preview = false) {
    if (!recall.used_references.length) return;
    const details = el("details", "memory-used-references"); details.append(el("summary", "", `查看${preview ? "预览检索到" : "本轮使用"}的 ${recall.used_references.length} 条长期记忆`), el("p", "profile-note", "这些是当时检索到的历史资料，不是本轮的新指令。下面保留完整事实文字。"));
    for (const ref of recall.used_references) { const card = el("article", "memory-reference"); card.append(el("p", "", `${ref.type} · ${ref.retrieved_at} · 记忆配置第 ${ref.memory_profile_revision} 版`), el("pre", "", ref.text), el("p", "memory-reference-id", `来源引用：${ref.reference_id}${ref.document_ref ? ` · 文档引用：${ref.document_ref}` : ""}`)); details.append(card); } container.append(details);
  }
  function appendMemoryPreview(container, value) {
    let recall = null; try { recall = window.RabbitProfiles?.memoryRecall(value?.recall); } catch (_) { /* Preserve draft if optional preview evidence is malformed. */ }
    if (!recall) { container.append(el("p", "memory-state", "预览中的记忆来源暂时无法读取，草稿仍保留。")); return; }
    const time = recall.retrieved_at || recall.used_references[0]?.retrieved_at;
    container.append(el("p", "memory-state", `这次预览检索到 ${recall.included_facts} 条长期记忆${recall.omitted_facts ? `，另有 ${recall.omitted_facts} 条未带入` : ""}${time ? ` · 检索时刻 ${fullDate(time)}` : ""}。`), el("p", "profile-note", "这次预览不会保存新交互，也不会锁定发送。发送时会重新检索，并冻结当时实际使用的结果。"));
    appendMemoryReferences(container, recall, true);
  }
  function markdownLabel(value) {
    return plain(value).replace(/[\r\n]+/g, " ").replace(/[\\`*_[\]<>]/g, "\\$&");
  }
  function statusLabel(status) {
    return { queued: "等待回答", pending: "等待回答", running: "回答中", completed: "已完成", sent: "已发送", failed: "未完成（失败）", cancelled: "已停止", interrupted: "已中断", imported: "原有历史" }[status] || (status ? plain(status) : "未记录");
  }
  function fencedOriginal(content) {
    const runs = content.match(/`+/g) || [];
    const fence = "`".repeat(runs.reduce((longest, run) => Math.max(longest, run.length + 1), 3));
    return `${fence}text\n${content}\n${fence}`;
  }
  function sourceBackgroundLabel(record, agent) {
    return window.RabbitHistoryQuotes?.backgroundLabel?.(record, agent) || record.source_kind || "";
  }
  function fileSourceLabel(source) { return { upload: "自行添加的输入", project_preview: "所见项目副本", generated: "这段聊天的成果" }[source] || "来源待确认"; }
  function textFileMetadata(raw) {
    if (!raw || !["upload", "project_preview", "generated"].includes(raw.source)) throw new Error("文件资料来源暂时无法读取。");
    return { ...window.RabbitFileTasks.artifact({ ...raw, path: raw.path || raw.name }), source: raw.source };
  }
  function fileInputExportLines(values) {
    if (!Array.isArray(values) || !values.length) return [];
    const lines = ["", "### 文件输入（固定版本）", "", "以下保留输入的身份和状态；文件正文未打包，请从小白兔中的输入预览另取。", ""];
    for (const raw of values.slice(0, 4)) { try { const item = textFileMetadata(raw);
      lines.push(`- 文件：${markdownLabel(item.name)}`, `  - ID：${item.id}`, `  - SHA256：${item.sha256}`, `  - 大小：${item.size_bytes} 字节`, `  - 来源：${fileSourceLabel(item.source)}`, `  - 状态：${item.available && item.content_verified ? "固定版本可读取" : "暂不可读取"}`);
    } catch (_) { lines.push("- 有一份输入元数据暂不可读取；原消息保留。"); } }
    return lines;
  }
  function appendTextFileMaterials(parent, record) {
    const values = Array.isArray(record.text_file_materials) ? record.text_file_materials : [], missing = Array.isArray(record.missing_text_file_materials) ? record.missing_text_file_materials : [];
    for (const raw of values.slice(0, 4)) { try {
      const item = textFileMetadata(raw), complete = typeof raw.text === "string" && new Blob([raw.text]).size === item.size_bytes;
      const details = el("details", "context-draft-full"), status = record.included ? "将带入" : "已省略";
      details.append(el("summary", "", `${status}文件资料 · ${item.name} · ${item.size_bytes} 字节 · ${fileSourceLabel(item.source)}`));
      details.append(el("p", "file-task-note", `ID：${item.id} · SHA256：${item.sha256}`));
      details.append(complete ? el("pre", "context-draft-text", raw.text) : el("p", "file-task-warning", "文件原文暂时没有读完整，不能据此确认已经带入。")); parent.append(details);
    } catch (_) { parent.append(el("p", "file-task-warning", "有一份文件资料信息暂时无法读取。")); } }
    for (const raw of missing.slice(0, 4)) {
      const name = typeof raw?.name === "string" && raw.name.length <= 240 ? raw.name : "原文件", id = typeof raw?.id === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(raw.id) ? raw.id : "标识待确认", hash = typeof raw?.sha256 === "string" && /^[a-f0-9]{64}$/.test(raw.sha256) ? raw.sha256 : "版本待确认";
      const reason = { not_found: "固定版本未找到", changed: "保存内容与固定版本不一致", unverified: "固定版本尚未核验", preparation_capacity: "文件准备超过本轮容量" }[raw?.reason] || "固定版本暂不可读取";
      parent.append(el("p", "file-task-warning", `${record.included ? "文件资料暂不可用" : "该条文件资料已省略"} · ${name} · ${reason} · ID：${id} · SHA256：${hash}`));
    }
  }
  function chatMarkdown(data) {
    const chat = data.chat || {};
    const messages = Array.isArray(data.messages) ? data.messages : [];
    const source = chat.source;
    const lines = [`# ${markdownLabel(chat.title || "共同聊天")}`, "", `- 导出时间：${new Date().toISOString()}`, `- 创建时间：${markdownLabel(exportTime(chat.created_at))}`, `- 最近更新：${markdownLabel(exportTime(chat.updated_at))}`, `- 来源：${source ? `${backendLabels[source.agent] || markdownLabel(source.agent || "未知")} 原有历史${source.title ? ` · ${markdownLabel(source.title)}` : ""}` : "新建共同聊天"}`, `- 消息数：${messages.length}`, "", "本文件包含全部对话正文及图片、文件输入的身份和状态；未打包图片文件或文件输入正文，不是完整资料备份。", ""];
    if (data.run) {
      const run = data.run;
      const reply = messages.find((message) => message.id === run.message_id);
      const model = run.model || reply && reply.model;
      const provider = run.provider || reply && reply.provider;
      lines.push("## 当前轮次", "", `- AI：${backendLabels[run.backend] || markdownLabel(run.backend || "未知")}`, `- 模型：${markdownLabel(model || "未知（原记录未提供）")}`, `- 状态：${statusLabel(run.status)}${run.cancel_requested && activeRunStates.has(run.status) ? "，正在停止" : ""}`);
      if (provider) lines.push(`- 提供方：${markdownLabel(provider)}`);
      lines.push(`- 型号信息来源：${modelSourceLabel(run.model_source)}`); lines.push(...profileExportLines(run.profile_snapshot));
      lines.push(...memoryExportLines(run.memory_info));
      if (run.created_at) lines.push(`- 开始时间：${markdownLabel(exportTime(run.created_at))}`);
      if (run.updated_at) lines.push(`- 更新时间：${markdownLabel(exportTime(run.updated_at))}`);
      if (run.context_info) lines.push(`- 已带入对话：${Number(run.context_info.included_messages) || 0} / ${Number(run.context_info.total_messages) || 0} 条`);
      if (run.error) lines.push("", "本轮错误：", "", plain(run.error));
      if (activeRunStates.has(run.status)) lines.push("", "这一轮尚未完成，下面保留导出时已收到的全部正文。");
      lines.push("");
    }
    messages.forEach((message, index) => {
      const role = message.role || "unknown";
      const roleName = sourceBackgroundLabel(message) || (message.phase === "incomplete_reply" ? "未完成的助手回复，未带入接续" : { user: "你", assistant: "助手", system: "背景说明", tool: "工具记录" }[role] || markdownLabel(role));
      const agent = message.backend || message.provenance && message.provenance.agent;
      const ai = role === "assistant" ? backendLabels[agent] || agent || "未知（原记录未提供）" : "不适用";
      const model = role === "assistant" ? message.model || "未知（原记录未提供）" : "不适用";
      const sourceTimestamp = message.status === "imported" && message.provenance && message.provenance.timestamp;
      lines.push("---", "", `## ${index + 1}. ${roleName}${role === "assistant" ? ` · ${markdownLabel(ai)}` : ""}`, "", `- 角色：${roleName}`, `- AI：${markdownLabel(ai)}`, `- 模型：${markdownLabel(model)}`, `- ${sourceTimestamp ? "原来源时间" : "时间"}：${markdownLabel(exportTime(sourceTimestamp || message.timestamp || message.created_at))}`, `- 状态：${statusLabel(message.status)}`);
      if (sourceTimestamp && message.created_at && sourceTimestamp !== message.created_at) lines.push(`- 录入共同聊天时间：${markdownLabel(exportTime(message.created_at))}`);
      if (message.provider) lines.push(`- 提供方：${markdownLabel(message.provider)}`);
      if (role === "assistant") { lines.push(`- 型号信息来源：${modelSourceLabel(message.model_source)}`); lines.push(...profileExportLines(message.profile_snapshot), ...memoryExportLines(message.memory_info)); }
      if (message.phase) lines.push(`- 阶段：${{ commentary: "过程", final_answer: "回答" }[message.phase] || markdownLabel(message.phase)}`);
      if (message.provenance && message.provenance.agent) lines.push(`- 原历史来源：${backendLabels[message.provenance.agent] || markdownLabel(message.provenance.agent)}`);
      if (hasImageRecords(message)) {
        lines.push(`- ${imageSummaryText(message)}（图片文件未打包）`);
        const reasons = Array.isArray(message.native_attachments) ? message.native_attachments.filter(record => record && record.material_state !== "ready").slice(0, 512) : [];
        for (const record of reasons) lines.push(`- 图片状态：${imageReason(record.material_reason && record.material_reason !== "native_unavailable" ? record.material_reason : record.reason)}`);
      }
      lines.push(...fileInputExportLines(message.file_inputs));
      const original = plain(message.content);
      const display = typeof message.display_content === "string" ? message.display_content : original;
      lines.push("", "### 正文", "", display, "");
      if (message.has_wrapper || original !== display) lines.push("### 原始记录（全文）", "", fencedOriginal(original), "");
    });
    return lines.join("\n");
  }
  async function exportChat() {
    if (state.selected.type !== "chat" || state.exporting) return;
    const id = state.selected.id;
    state.exporting = true; headerForChat(state.chat || state.chats.find((chat) => chat.id === id));
    try {
      const data = await api(`/api/chats/${encodeURIComponent(id)}`);
      const blob = new Blob([chatMarkdown(data)], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const filenameTitle = [...plain(data.chat && data.chat.title || "共同聊天").replace(/[\x00-\x1f\x7f\\/:*?"<>|]/g, "_")].slice(0, 80).join("").replace(/^\.+|\.+$/g, "").trim() || "共同聊天";
      const date = new Date().toISOString().slice(0, 10);
      const link = el("a"); link.href = url; link.download = `${filenameTitle}_${date}.md`; link.hidden = true;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      toast("已开始下载全部文字与图片状态；图片文件未打包。文件保存在浏览器的下载位置。");
    } catch (error) { toast(userError(error, "文字暂时没有导出成功，请再试一次。")); }
    finally { state.exporting = false; if (state.selected.type === "chat") headerForChat(state.chat || state.chats.find((chat) => chat.id === state.selected.id)); }
  }
  function invalidateContextPreview(reason) {
    const hadResult = state.contextData || state.contextState === "loading" || state.contextState === "error";
    if (!hadResult) return;
    ++state.contextVersion; state.contextData = null; state.contextState = "stale"; state.contextError = reason;
    state.contextPinNotice = ""; state.contextPinError = false;
    renderContextPreview(); updateComposer();
  }
  function contextCannotSend() { return state.contextState === "ready" && state.contextData?.context_info?.can_send === false; }
  function contextStatusCopy(info = {}, records = [], fallback = "") {
    const positive = value => Number.isSafeInteger(value) && value > 0;
    const fileRecords = (Array.isArray(records) ? records : []).filter(record => record && typeof record === "object");
    const unavailable = fileRecords.flatMap(record => Array.isArray(record.missing_text_file_materials) ? record.missing_text_file_materials : []);
    const recordHasFiles = record => [record.text_file_materials, record.missing_text_file_materials].some(value => Array.isArray(value) && value.length > 0);
    const omittedFileRecords = fileRecords.filter(record => record.included === false &&
      recordHasFiles(record));
    // Missing/zero counters are not evidence of a file problem. The existing
    // preview descriptors supplement older receipts without inventing counts.
    const hasFiles = [info.total_text_file_materials, info.included_text_file_materials,
      info.omitted_text_file_materials, info.missing_text_file_materials,
      info.preparation_capacity_text_file_materials].some(positive) ||
      (Array.isArray(info.included_text_file_inputs) ? info.included_text_file_inputs.length > 0 : positive(info.included_text_file_inputs)) ||
      fileRecords.some(recordHasFiles);
    const preparationCount = positive(info.preparation_capacity_text_file_materials) ? info.preparation_capacity_text_file_materials :
      unavailable.filter(item => item?.reason === "preparation_capacity").length;
    const preparation = positive(info.preparation_capacity_text_file_materials) ||
      unavailable.some(item => item?.reason === "preparation_capacity") ||
      omittedFileRecords.some(record => (record.omission_reason || record.omit_reason) === "text_file_preparation_capacity");
    const missingFiles = positive(info.missing_text_file_materials) &&
      info.missing_text_file_materials > preparationCount ||
      unavailable.some(item => ["not_found", "changed", "unverified"].includes(item?.reason)) ||
      omittedFileRecords.some(record => (record.omission_reason || record.omit_reason) === "missing_text_files");
    const missingImages = positive(info.missing_images), omittedImages = positive(info.omitted_images);
    const omittedFiles = positive(info.omitted_text_file_materials) || omittedFileRecords.length > 0;
    const message = typeof info.send_block_message === "string" && info.send_block_message || fallback;
    let block;
    switch (info.policy) {
      case "required_context_overflow": block = hasFiles ? {
        hint: "本轮文字与文件资料超出容量",
        footnote: `减少文件资料${positive(info.pinned_messages) ? "或取消部分必带前文" : ""}，也可缩短问题，再重新查看；原草稿与文件选择已保留。`
      } : { hint: "本轮必带前文与问题超出文字容量", footnote: message || "缩短问题，或在预览里取消部分必带前文，再重新查看。" }; break;
      case "required_text_files_preparation_capacity": block = { hint: "本轮文件资料超出准备容量", footnote: "减少本轮文件资料或取消部分必带文件前文，再重新查看；原草稿与文件选择已保留。" }; break;
      case "required_text_files_missing": block = { hint: "本轮有文件资料暂时无法核验", footnote: "移除或重新添加缺失文件，或取消相应必带前文，再重新查看；原草稿与文件选择已保留。" }; break;
      case "required_images_missing": block = { hint: "本轮有图片材料暂时无法读取", footnote: message || "查看上下文中缺失的图片，确认材料恢复后再试；原草稿已保留。" }; break;
      case "required_images_overflow": block = { hint: "本轮必带图片超出图像容量", footnote: message || "减少本轮图片，或在预览里调整必带前文，再重新查看；原草稿已保留。" }; break;
      case "image_input_unavailable": block = { hint: "所选模型暂不能处理本轮图片", footnote: message || "请选择支持图像输入的模型后重新查看；原文字和图片已保留。" }; break;
      case "file_output_unavailable": block = { hint: "所选连接暂不能执行本轮文件任务", footnote: message || "文件材料与草稿保留，可切回普通聊天或选择支持文件的 AI 配置。" }; break;
      default: block = { hint: "本轮暂不能发送，请查看上下文中的原因", footnote: message || "原文字和图片已保留；处理原因后重新查看。" };
    }
    const omissions = [];
    if (preparation) omissions.push("部分文件资料超出准备容量，已连同所在记录省略");
    if (missingFiles) omissions.push("部分前文文件资料暂不可用，已连同所在记录省略");
    if (missingImages) omissions.push("部分前文图片不可用，已省略相应记录");
    else if (omittedImages) omissions.push("部分图片前文未带入");
    if (omittedFiles && !preparation && !missingFiles) omissions.push("部分文件资料未带入");
    const materialOmission = omissions.length > 0 || hasFiles;
    if (!omissions.length) omissions.push(hasFiles && ["recent_and_earlier_records", "required_context_overflow"].includes(info.policy) ? "本轮文字与文件资料容量有限，部分前文未带入" : ["recent_and_earlier_records", "required_context_overflow"].includes(info.policy) ? "对话较长" : "部分前文未带入");
    const fileBlock = ["required_text_files_preparation_capacity", "required_text_files_missing"].includes(info.policy) ||
      info.policy === "required_context_overflow" && hasFiles;
    return { ...block, isFileBlock: fileBlock, warning: fileBlock ? `${block.hint}。${block.footnote}` : block.footnote,
      omission: omissions.join("；"), materialOmission };
  }
  function contextBlockCopy() {
    return contextStatusCopy(state.contextData?.context_info || {}, state.contextData?.records || []);
  }
  function contextPinSavingCurrent() { return !!(state.contextPinSaving && sameContextPinView(state.contextPinSaving)); }
  function sameContextPinView(captured) {
    return state.contextVisible && state.page === "conversations" && captured.navigationVersion === state.contextNavigationVersion && captured.openVersion === state.openVersion && captured.contextVersion === state.contextVersion && captured.key === draftKey() && captured.text === ui.input.value && captured.backend === state.backend && captured.backendChoiceVersion === state.backendChoiceVersion;
  }
  function validContextSelection(selection) {
    return !!(selection && Number.isSafeInteger(selection.revision) && selection.revision >= 0 && Array.isArray(selection.pinned_ids) && selection.pinned_ids.length <= 20 && selection.pinned_ids.every(id => typeof id === "string" && id.length > 0) && new Set(selection.pinned_ids).size === selection.pinned_ids.length);
  }
  async function toggleContextPin(record, pinned) {
    const data = state.contextData, selection = data && data.context_selection;
    if (!data || state.contextState !== "ready" || state.page !== "conversations" || !state.contextVisible || state.sending || state.creating || contextPinSavingCurrent() || typeof data.pin_chat_id !== "string" || !data.pin_chat_id || !validContextSelection(selection) || !record || record.is_draft || typeof record.message_id !== "string" || !record.message_id || !['user', 'assistant'].includes(record.role) || (record.status != null && !['completed', 'imported'].includes(record.status)) || !Array.isArray(data.records) || !data.records.includes(record)) return;
    const captured = { openVersion: state.openVersion, contextVersion: state.contextVersion, navigationVersion: state.contextNavigationVersion, key: draftKey(), text: ui.input.value, backend: state.backend, backendChoiceVersion: state.backendChoiceVersion };
    const previousRecords = ui.contextPanel.querySelector(".context-records");
    const previousScrollTop = previousRecords && Number(previousRecords.scrollTop);
    const previousScrollVersion = previousRecords && contextScrollVersions.get(previousRecords);
    state.contextPinSaving = captured; state.contextPinNotice = ""; state.contextPinError = false;
    for (const node of ui.contextPanel.querySelectorAll(".context-pin-button")) node.disabled = true;
    const saving = el("p", "context-pin-status", "正在保存必带前文，没有发送消息…"); saving.setAttribute("role", "status"); ui.contextPanel.append(saving);
    updateComposer();
    try {
      const result = await api(`/api/chats/${encodeURIComponent(data.pin_chat_id)}/context-pins`, { method: "POST", body: JSON.stringify({ message_id: record.message_id, pinned, revision: selection.revision }) });
      if (!sameContextPinView(captured)) return;
      if (!validContextSelection(result.context_selection)) throw new Error("保存结果暂时无法确认，请重新查看必带前文。");
      // A detached scroll container can report zero and emit a scroll event.
      // Decide while the old list still belongs to this live preview, before
      // previewContext replaces it with the loading state.
      const restoreScrollTop = previousRecords && Number.isFinite(previousScrollTop) && previousScrollTop >= 0 && previousRecords.scrollTop === previousScrollTop && contextScrollVersions.get(previousRecords) === previousScrollVersion ? previousScrollTop : null;
      // Read the new server selection; never infer success by editing old cards.
      const refreshVersion = state.contextVersion + 1;
      await previewContext();
      if (state.contextVersion === refreshVersion && captured.openVersion === state.openVersion && captured.navigationVersion === state.contextNavigationVersion && captured.key === draftKey() && captured.text === ui.input.value && captured.backend === state.backend && captured.backendChoiceVersion === state.backendChoiceVersion && state.page === "conversations" && state.contextVisible && state.contextState === "ready") {
        state.contextPinNotice = pinned ? "已设为必带前文；草稿和下一位 AI 保持原样。" : "已取消必带；历史原文仍保留。";
        state.contextPinError = false;
        const notice = el("p", "context-pin-status", state.contextPinNotice); notice.setAttribute("role", "status"); ui.contextPanel.append(notice);
        const currentRecords = ui.contextPanel.querySelector(".context-records");
        // Follow only this explicit save. A scroll made while saving, a newer
        // preview, or any later view/text/Agent operation wins over this offset.
        if (restoreScrollTop !== null && currentRecords && !currentRecords.scrollTop && contextScrollVersions.get(currentRecords) === 0) currentRecords.scrollTop = restoreScrollTop;
      }
    } catch (error) {
      if (!sameContextPinView(captured)) return;
      if (error.status === 409 || !error.status || error.status >= 500) {
        ++state.contextVersion; state.contextData = null; state.contextState = "stale";
        state.contextError = error.status === 409 ? "另一入口已修改必带前文。请重新查看，再决定是否保存；没有覆盖另一份选择。" : "暂时无法确认必带前文是否保存。请重新查看；没有自动重试或发送消息。";
      } else { state.contextPinNotice = userError(error, "必带前文没有保存，请重新查看后再试。"); state.contextPinError = true; }
      renderContextPreview();
    } finally {
      saving.remove();
      if (state.contextPinSaving === captured) {
        state.contextPinSaving = null; updateComposer();
        for (const node of ui.contextPanel.querySelectorAll(".context-pin-button")) node.disabled = state.sending || state.creating || contextPinSavingCurrent();
      }
    }
  }
  function closeContextPreview() {
    state.contextVisible = false;
    if (state.contextState === "loading") { ++state.contextVersion; state.contextState = "empty"; state.contextData = null; }
    renderContextPreview(); updateComposer(); ui.contextButton.focus();
  }
  function renderContextPreview() {
    ui.contextPanel.hidden = !state.contextVisible;
    ui.contextButton.setAttribute("aria-expanded", String(state.contextVisible));
    if (!state.contextVisible) return;
    const header = el("div", "context-preview-header");
    header.append(el("h3", "", "本轮上下文"), el("span", "context-private-label", "尚未提交回答请求"), button("收起", "", closeContextPreview));
    const nodes = [header];
    if (state.contextState === "loading") {
      const loading = el("p", "context-preview-status", "正在本机查看会带入哪些前文…"); loading.setAttribute("role", "status"); nodes.push(loading);
    } else if (state.contextState === "error" || state.contextState === "stale") {
      const notice = el("p", "context-preview-status", state.contextError || "内容已变化，请重新查看。"); notice.setAttribute("role", "status");
      nodes.push(notice, button(state.contextState === "error" ? "再试一次" : "重新查看", "context-retry", previewContext));
    } else if (state.contextData) {
      const data = state.contextData; const info = data.context_info || {};
      const summary = el("p", "context-counts", info.can_send === false ? info.policy === "required_context_overflow" ? `这一轮暂不能发送 · 必带 ${Number(info.pinned_messages) || 0} 条前文` : "这一轮暂不能发送 · 原草稿已保留" : `将带入 ${Number(info.included_messages) || 0}/${Number(info.total_messages) || 0} 条 · 省略 ${Number(info.omitted_messages) || 0} 条`);
      summary.setAttribute("role", "status"); nodes.push(summary);
      if (data.memory_info) { const memoryBox = el("div", "context-memory-info"); appendMemoryPreview(memoryBox, data.memory_info); nodes.push(memoryBox); }
      else if (state.profile_snapshot?.memory_profile.id !== "none" && state.profile_snapshot?.memory_profile.name) nodes.push(el("p", "context-memory-plan", `记忆配置：${state.profile_snapshot.memory_profile.name}。本轮预览尚未展示长期记忆检索结果，不能据此认为已读取记忆；发送时会检查该配置。`));
      if (Array.isArray(info.file_input_refs) && info.file_input_refs.length && info.file_input_refs.length <= 4) {
        const inputs = el("div", "file-task-results"); inputs.append(el("p", "file-task-heading", "本轮选择的文件"));
        for (const ref of info.file_input_refs) if (ref && typeof ref.name === "string" && ref.name.length <= 240 && ["upload", "project_preview", "generated"].includes(ref.source) && Number.isSafeInteger(ref.size_bytes) && ref.size_bytes >= 0 && ref.size_bytes <= 524288) inputs.append(el("p", "file-task-note", `${ref.name} · ${ref.size_bytes} 字节 · ${ref.source === "upload" ? "自行添加的输入" : ref.source === "project_preview" ? "所见项目副本" : "这段聊天的成果"}`));
        inputs.append(el("p", "file-task-note", state.task_intent.mode === "files" ? "这里展示材料选择，实际读取和保存结果以本轮回执为准。" : "本轮使用固定版本的文件原文。具体带入和省略情况见下方资料；这是本机预览，尚未发送给模型。")); nodes.push(inputs);
      }
      if (Array.isArray(info.included_text_file_inputs) || Number.isSafeInteger(info.included_text_file_inputs)) nodes.push(el("p", "context-counts", `文件资料：将带入 ${Array.isArray(info.included_text_file_inputs) ? info.included_text_file_inputs.length : info.included_text_file_inputs} 份 · 省略 ${Number(info.omitted_text_file_materials) || 0} 份 · 暂不可用 ${Number(info.missing_text_file_materials) || 0} 份`));
      if (Number.isSafeInteger(info.omitted_previous_image_positions) && info.omitted_previous_image_positions >= 0) nodes.push(el("p", "context-image-counts", `本轮文件任务只使用文字前文，${info.omitted_previous_image_positions} 项旧图片未提供。`));
      if (Number.isSafeInteger(info.total_images)) nodes.push(el("p", "context-image-counts", `图片：将带入 ${info.included_images || 0}/${info.total_images} 张 · 本轮新增 ${info.draft_images || 0} 张 · 前文候选 ${info.previous_images || 0} 张（含可能省略的图片） · 省略 ${info.omitted_images || 0} 张 · 缺失 ${info.missing_images || 0} 张`));
      if (typeof data.draft_text === "string" && characterCount(data.draft_text) <= draftCharacterLimit) {
        const fullDraft = el("details", "context-draft-full");
        fullDraft.append(el("summary", "", `查看本轮草稿全文（${characterCount(data.draft_text)}字符）`), el("pre", "context-draft-text", data.draft_text));
        nodes.push(fullDraft);
      }
      const canPin = typeof data.pin_chat_id === "string" && !!data.pin_chat_id && validContextSelection(data.context_selection);
      if (canPin) nodes.push(el("p", "context-pin-explanation", "必带保留这条历史原文，不会把旧话升级成当前指令。最多 20 条，换 AI 后也保留。"));
      else if (state.selected.type === "history" && data.pin_chat_id === null) nodes.push(el("p", "context-pin-explanation", "发送后会自动建立接续对话，之后可设置必带前文。"));
      const copy = contextStatusCopy(info, data.records || [], data.notice || "");
      if (info.can_send === false) { const warning = el("p", "context-overflow", copy.warning); warning.setAttribute("role", "alert"); nodes.push(warning); }
      else if (Number.isSafeInteger(info.omitted_messages) && info.omitted_messages > 0 && copy.materialOmission) nodes.push(el("p", "context-preview-status", `${copy.omission}；原记录仍保留。`));
      if (state.contextPinNotice) { const notice = el("p", `context-pin-status${state.contextPinError ? " error" : ""}`, state.contextPinNotice); notice.setAttribute("role", state.contextPinError ? "alert" : "status"); nodes.push(notice); }
      const records = el("div", "context-records"); records.dataset.controlInterface = "ai"; records.tabIndex = 0; records.setAttribute("aria-label", "前文带入与省略列表");
      contextScrollVersions.set(records, 0);
      records.addEventListener("scroll", () => contextScrollVersions.set(records, contextScrollVersions.get(records) + 1), { passive: true });
      for (const record of Array.isArray(data.records) ? data.records : []) {
        const card = el("div", `context-record ${record.included ? "included" : "omitted"}${record.pinned ? " pinned" : ""}`);
        const speaker = sourceBackgroundLabel(record) || (record.role === "user" ? "你" : backendLabels[record.source_ai] || record.source_ai || (record.role === "assistant" ? "助手" : "记录"));
        const line = el("div", "context-record-heading");
        line.append(el("span", "", `第 ${record.position} 条 · ${speaker}`), el("span", "context-inclusion", record.included ? "将带入" : info.can_send === false ? "暂未带入" : "已省略"));
        if (record.pinned) line.append(el("span", "context-pinned", "必带前文"));
        if (record.is_draft) line.append(el("span", "context-draft", "当前草稿"));
        if (typeof record.characters === "number") line.append(el("span", "context-characters", `${record.characters} 字`));
        if (canPin && !record.is_draft && typeof record.message_id === "string" && record.message_id && ['user', 'assistant'].includes(record.role) && (record.status == null || ['completed', 'imported'].includes(record.status))) {
          const pin = button(record.pinned ? "取消必带" : "设为必带前文", "context-pin-button", () => toggleContextPin(record, !record.pinned));
          pin.dataset.messageId = record.message_id; pin.setAttribute("aria-pressed", String(!!record.pinned));
          pin.disabled = state.sending || state.creating || contextPinSavingCurrent(); line.append(pin);
        }
        card.append(line, el("p", "context-record-text", plain(record.preview)));
        appendMessageMaterials(card, record); appendTextFileMaterials(card, record);
        records.append(card);
      }
      nodes.push(records, el("p", "context-preview-note", info.can_send === false && copy.isFileBlock ? "这是本机预览，尚未发送给模型。" : data.notice || "这是本机预览，未发送给模型。发送时会使用最新存档和当前草稿，预览不会锁定这次发送。"));
    }
    ui.contextPanel.replaceChildren(...nodes);
  }
  async function previewContext() {
    if (historyUsesNative()) return; // Native context belongs to the original Agent, not the text-copy preview.
    if ((!['new', 'chat', 'history'].includes(state.selected.type) || state.selected.type !== 'new' && !state.selected.id)) return;
    if (state.selected.type === "history" && (!state.historyReady || !sourceCanContinue())) return;
    const draft = ui.input.value;
    if (!draftHasContent(draft)) { toast("先写下这一轮想说的内容，再查看会带入哪些前文。"); ui.input.focus(); return; }
    const selection = { ...state.selected }; const key = draftKey(selection); const version = ++state.contextVersion;
    const continuation = selection.type === "history" && state.historyContinuationIds.get(selection.id);
    const path = selection.type === "new" ? "/api/context-preview" : selection.type === "history" && !continuation ? "/api/history/context-preview" : `/api/chats/${encodeURIComponent(continuation || selection.id)}/context-preview`;
    const body = selection.type === "history" && !continuation ? { source: { agent: selection.agent, session_id: selection.sessionId }, text: draft.trim(), reuse_source: true, client_id: state.historyCreationIds.get(selection.id) } : { text: draft.trim() };
    body.attachment_ids = [...state.attachment_ids]; body.backend = state.backend; body.model_id = state.model_id; Object.assign(body, profileBody());
    state.contextVisible = true; state.contextData = null; state.contextState = "loading"; state.contextError = "";
    state.contextPinNotice = ""; state.contextPinError = false;
    renderContextPreview(); updateComposer();
    Object.assign(body, taskBody(state.task_intent));
    try {
      const data = await api(path, { method: "POST", body: JSON.stringify(body) });
      if (version !== state.contextVersion || draftKey() !== key || ui.input.value !== draft) return;
      state.contextData = data; state.contextState = "ready"; renderContextPreview();
      if (state.selected.type === "chat" && data.pin_chat_id === state.selected.id && validContextSelection(data.context_selection)) state.chatContextSelection = data.context_selection;
    } catch (error) {
      if (version !== state.contextVersion) return;
      state.contextData = null; state.contextState = "error"; state.contextError = userError(error, "上下文暂时没有读出来，可以再试一次。"); renderContextPreview();
    } finally { if (version === state.contextVersion) updateComposer(); }
  }
  function newChat() {
    state.historyReadOnly = false;
    showPage("conversations");
    closeSourceUpdate(false);
    closeSourceImages(false);
    invalidateContextPreview("已开始新的聊天，请重新查看本轮上下文。");
    saveDraft(); stopPolling(); ++state.openVersion;
    state.selected = { type: "new", id: null };
    state.profile_snapshot = null; state.profileSelection = null; state.profileSelectionSync = null; state.profileSelectionIntent = false; state.profileLegacy = false; state.profileNotice = ""; profiles?.close(false);
    state.memoryRecallError = false;
    state.backend = state.preferences.default_backend || "codex"; state.model_id = defaultModelId(state.backend);
    state.chat = null; state.chatMessages = []; state.run = null; state.historySession = null; state.historyMatch = null; state.transientError = "";
    state.pendingSubmission = null; if (!state.browserRestoring) { state.newCreationId = null; }
    state.selectedRevision = null; state.remoteRunning = false; state.chatLoading = null;
    resetMessageView(); headerForChat(null); restoreDraft(); showWelcome(); renderRunNotice(); renderList(); toggleSidebar(false);
    ui.input.focus();
    checkpointBrowserState();
    schedulePoll(true);
    if (profilesEnabled() && profiles && !state.profilesInventory) void profiles.load().catch(error => toast(userError(error, "配置目录暂未读到，草稿保留。")));
  }
  function showViewError(message, retry) {
    const box = el("div", "view-error");
    box.append(el("h3", "", "这段对话暂时没打开"), el("p", "", message), button("再试一次", "text-button", retry));
    ui.messages.replaceChildren(box);
  }
  async function openChat(id, keepSidebar = false, target = null) {
    state.historyReadOnly = false;
    showPage("conversations");
    closeSourceUpdate(false);
    closeSourceImages(false);
    invalidateContextPreview("已换到另一段聊天，请重新查看本轮上下文。");
    saveDraft(); stopPolling();
    const version = ++state.openVersion;
    state.chatLoading = { id, version }; state.selectedRevision = null; state.remoteRunning = false;
    state.selected = { type: "chat", id }; state.chat = null; state.chatMessages = []; state.run = null; state.transientError = "";
    state.profile_snapshot = null; state.profileSelection = null; state.profileSelectionSync = null; state.profileSelectionIntent = false; state.profileLegacy = false; state.profileNotice = ""; profiles?.close(false);
    state.memoryRecallError = false;
    state.pendingSubmission = null;
    state.historySession = null; state.historyMatch = target; resetMessageView(); headerForChat(state.chats.find((chat) => chat.id === id)); restoreDraft(); renderRunNotice(); renderList();
    if (!keepSidebar) toggleSidebar(false);
    ui.messages.append(el("p", "history-loading", "正在打开对话…"));
    try {
      const data = await api(`/api/chats/${encodeURIComponent(id)}`);
      if (version !== state.openVersion) return;
      renderChat(data, !target);
      if (target) requestAnimationFrame(() => { if (version === state.openVersion) scrollToHistoryMatch(); });
    } catch (error) {
      if (version === state.openVersion) showViewError(userError(error, "暂时连不上这台电脑，请确认助手仍在运行。"), () => openChat(id));
    } finally {
      if (state.chatLoading && state.chatLoading.version === version) state.chatLoading = null;
      checkpointBrowserState();
      schedulePoll(true);
    }
  }
  function appendHighlightedText(node, content, query) {
    if (!query) { node.textContent = content; return; }
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const expression = new RegExp(escaped, "giu");
    let cursor = 0;
    for (const match of content.matchAll(expression)) {
      node.append(document.createTextNode(content.slice(cursor, match.index)), el("mark", "search-word", match[0]));
      cursor = match.index + match[0].length;
    }
    node.append(document.createTextNode(content.slice(cursor)));
  }
  let documentVersion = 0, documentReturnFocus = null, documentDraftPreview = null;
  const draftCharacterLimit = 30000;
  function characterCount(value) { return Array.from(value).length; }
  function quoteDraft() {
    if (state.page === "native-chats") {
      const native = nativeChats?.captureQuoteDraft?.();
      if (!native) return { stamp: "native-unavailable", text: "", title: "原生对话", active: false, attachment_ids: [], task_intent: { mode: "chat", artifact_ids: [] }, file_inputs: [], allow_quote_files: false, allow_quote_images: false, busy: true };
      const refs = native.material_refs, images = refs.filter(item => item.kind === "image").map(item => item.id), fileRefs = refs.filter(item => item.kind === "text"), files = fileRefs.map(item => item.id);
      const fileInputs = fileRefs.map(ref => { const item = sourceFile(ref.id); return item && item.sha256 === ref.sha256 ? item : { id: ref.id, available: false, content_verified: false }; });
      return { stamp: JSON.stringify([state.page, state.contextNavigationVersion, native.workspace_id, native.agent, native.chat_id, native.stamp]), text: native.text, title: native.title || "原生对话", active: true,
        attachment_ids: images, task_intent: { mode: "chat", artifact_ids: files }, file_inputs: fileInputs, allow_quote_files: fileInputsSupported() && fileInputs.every(item => item.available && item.content_verified), allow_quote_images: !["claude", "dsh"].includes(native.agent), busy: false, native_quote: native };
    }
    const generated = availableFileArtifacts(), fileInputs = state.task_intent.artifact_ids.map(id => sourceFile(id) || generated.get(id) || { id, available: false, content_verified: false });
    return { stamp: `${state.page}:${state.openVersion}:${state.contextNavigationVersion}:${draftKey()}:${state.sourceUpdateInputVersion}:${state.backendChoiceVersion}:${state.backend}:${state.model_id}:${JSON.stringify(state.profile_snapshot)}:${JSON.stringify(state.task_intent)}:${JSON.stringify(fileInputs)}`,
      text: ui.input.value, title: ui.title.textContent || "新对话", active: state.page === "conversations",
      attachment_ids: [...state.attachment_ids], task_intent: taskIntent(state.task_intent),
      file_inputs: fileInputs,
      busy: state.sending || state.creating || state.backendChoosing || currentUploadCount() > 0 || ui.input.disabled };
  }
  function nativeDraftMemory() {
    const frozen = draftMemoryProfile();
    if (!frozen || frozen.id === "none") return null;
    const row = profiles?.getMemory(frozen.id), checked = row && window.RabbitProfiles.memoryProfile(row, false);
    if (!row?.available || JSON.stringify(checked) !== JSON.stringify(frozen)) throw Error("所选记忆已变化或暂不可用，请重新选择；原稿保留。");
    if (!window.RabbitNativeMemory) throw Error("共享记忆组件尚未就绪；原稿保留。");
    return window.RabbitNativeMemory.choice({ ...checked, locked_mode: row.locked_mode === true, source_kind: row.source_kind || "owned" }, { readOnly: true });
  }
  function captureNativeDraft(type) {
    if (!["new", "history"].includes(type) || state.page !== "conversations" || state.selected.type !== type || state.sending || state.creating || state.backendChoosing || currentUploadCount()
        || type === "history" && !historyTextSource()) throw Error("草稿或上传仍在处理，请完成后再选择；原稿保留。");
    if (state.pendingSubmission || state.newCreationId || state.memoryChatCreation || type === "history" && state.historyCreationIds.has(state.selected.id)) throw Error("请先核对上次创建或发送结果；原稿保留，没有新建另一段对话。");
    const captured = quoteDraft();
    if (captured.task_intent.mode !== "chat") throw Error("请先完成或取消“生成文件”任务，再切换原生 Agent；原稿和文件保留。");
    const memory = nativeDraftMemory();
    return { ...captured, type, workspaceId: state.status?.workspace_id || "", key: draftKey(), openVersion: state.openVersion,
      editVersion: state.sourceUpdateInputVersion, navigationVersion: state.contextNavigationVersion, configurationVersion: state.backendChoiceVersion,
      configuration: JSON.stringify([state.backend, state.model_id, state.profile_snapshot]), memory, memorySpecified: type !== "history" || memory !== null || historyChoice()?.memory_explicit === true };
  }
  function sameNativeDraft(source) {
    try {
      const current = captureNativeDraft(source.type);
      return source.workspaceId === current.workspaceId && source.key === current.key && source.openVersion === current.openVersion
        && source.editVersion === current.editVersion && source.stamp === current.stamp && source.text === current.text
        && JSON.stringify(source.attachment_ids) === JSON.stringify(current.attachment_ids) && JSON.stringify(source.memory) === JSON.stringify(current.memory)
        && source.memorySpecified === current.memorySpecified;
    } catch (_) { return false; }
  }
  function sameNativeSendTarget(source) {
    // A click freezes its text. Later typing is another local draft, while the
    // original target, chosen configuration and materials must remain unchanged.
    try {
      const current = captureNativeDraft(source.type);
      return source.workspaceId === current.workspaceId && source.key === current.key && source.openVersion === current.openVersion
        && source.navigationVersion === current.navigationVersion && source.configurationVersion === current.configurationVersion
        && source.configuration === current.configuration && JSON.stringify(source.attachment_ids) === JSON.stringify(current.attachment_ids)
        && JSON.stringify(source.file_inputs) === JSON.stringify(current.file_inputs) && sameTaskIntent(source.task_intent, current.task_intent)
        && JSON.stringify(source.memory) === JSON.stringify(current.memory) && source.memorySpecified === current.memorySpecified;
    } catch (_) { return false; }
  }
  async function prepareNativeDraft(agent, source, isCurrent) {
    if (!source.text && !source.attachment_ids.length && !source.file_inputs.length && source.memory === null && !(source.type === "history" && source.memorySpecified)) return null;
    if (!window.RabbitNativeMaterials) throw Error("材料组件尚未就绪；原稿保留，没有放入原生草稿。");
    if (source.attachment_ids.length && ["claude", "dsh"].includes(agent)) throw Error("此 Agent 尚未接入原图；请选择其他 Agent 或移除图片，原稿保留。");
    const refs = source.file_inputs.map(item => {
      if (!item?.available || !item.content_verified) throw Error("所选文件尚未核对完整；原稿保留，没有放入原生草稿。");
      return { kind: "text", id: item.id, sha256: item.sha256 };
    });
    const images = await Promise.allSettled(source.attachment_ids.map(async id => {
      if (!materials || !window.crypto?.subtle) throw Error("原图或版本暂不可核对；原稿保留。");
      const original = await materials.image(id, true), prefix = `data:${original.material.mime};base64,`;
      if (!original.data_url.startsWith(prefix)) throw Error("原图类型与内容不一致；原稿保留。");
      const bytes = Uint8Array.from(atob(original.data_url.slice(prefix.length)), char => char.charCodeAt(0));
      if (bytes.length !== original.material.size_bytes) throw Error("原图尚未读完整；原稿保留。");
      const digest = [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", bytes))].map(byte => byte.toString(16).padStart(2, "0")).join("");
      return { kind: "image", id, sha256: digest };
    }));
    for (const result of images) { if (result.status === "rejected") throw result.reason; refs.push(result.value); }
    const checkedRefs = window.RabbitNativeMaterials.refs(refs);
    let materialPreview;
    if (checkedRefs.length) {
      const reader = window.RabbitNativeMaterials.create({ api, getWorkspace: () => state.status?.workspace_id || "" });
      reader.setScope(source.stamp);
      const results = await Promise.allSettled(checkedRefs.map(ref => reader.read(ref, { refresh: true })));
      const values = results.map(result => { if (result.status === "rejected") throw result.reason; return result.value; });
      materialPreview = window.RabbitNativeMaterials.preview(checkedRefs, { material_refs: checkedRefs,
        images: values.filter((_value, index) => checkedRefs[index].kind === "image"), files: values.filter((_value, index) => checkedRefs[index].kind === "text") });
    }
    if (!isCurrent()) throw Error("文字、材料或所选配置已变化；原稿保留，没有放入原生草稿。");
    return { text: source.text, material_refs: checkedRefs, ...(materialPreview ? { material_preview: materialPreview } : {}), ...(source.memorySpecified ? { memory_profile: source.memory } : {}) };
  }
  function clearNativeDraft(source) {
    // The destination has already saved. Keep a source copy if clearing this
    // browser's Home state fails; these two journals are not one transaction.
    if (state.selected.type !== source.type || source.key !== draftKey() || source.openVersion !== state.openVersion || source.editVersion !== state.sourceUpdateInputVersion
        || source.workspaceId !== state.status?.workspace_id || ui.input.value !== source.text
        || JSON.stringify(state.attachment_ids) !== JSON.stringify(source.attachment_ids) || !sameTaskIntent(state.task_intent, source.task_intent)) return false;
    const previous = { text: ui.input.value, images: state.attachment_ids, task: state.task_intent };
    const empty = { mode: "chat", artifact_ids: [] };
    ui.input.value = ""; state.attachment_ids = []; state.task_intent = empty;
    state.drafts.set(source.key, ""); state.draftAttachments.set(source.key, []); state.taskIntents.set(source.key, empty);
    if (!checkpointBrowserState()) {
      ui.input.value = previous.text; state.attachment_ids = previous.images; state.task_intent = previous.task;
      state.drafts.set(source.key, previous.text); state.draftAttachments.set(source.key, previous.images); state.taskIntents.set(source.key, previous.task);
      toast("已带入原生草稿；来源页的副本仍保留，请在目标对话核对发送结果。");
      return false;
    }
    return true;
  }
  function putQuoteDraft(text, captured, imageIds = captured.attachment_ids || [], fileIds = captured.task_intent?.artifact_ids || [], isCurrent = () => true, fileVersions = []) {
    if (captured.native_quote) return putNativeQuoteDraft(text, captured, imageIds, fileIds, isCurrent, fileVersions);
    const current = quoteDraft();
    let checked, checkedIntent;
    try { checked = attachmentIds(imageIds); checkedIntent = taskIntent({ mode: current.task_intent.mode, artifact_ids: fileIds }); } catch (_) { return false; }
    if (state.task_intent.mode === "files" && checked.length) return false;
    if (typeof text !== "string" || !current.active || current.busy || current.stamp !== captured.stamp
        || current.text !== captured.text || !sameAttachments(current.attachment_ids, captured.attachment_ids)
        || !sameTaskIntent(current.task_intent, captured.task_intent)
        || JSON.stringify(current.file_inputs) !== JSON.stringify(captured.file_inputs || [])
        || !sameAttachments(checked.slice(0, current.attachment_ids.length), current.attachment_ids)
        || JSON.stringify(checkedIntent.artifact_ids.slice(0, current.task_intent.artifact_ids.length)) !== JSON.stringify(current.task_intent.artifact_ids)
        || checkedIntent.artifact_ids.length > current.task_intent.artifact_ids.length && !fileInputsSupported()
        || characterCount(text) > draftCharacterLimit) return false;
    // Text, selected immutable image IDs and file IDs enter one local intent. The
    // material bytes were freshly checked by the picker; sending rechecks them.
    ui.input.value = text;
    state.task_intent = checkedIntent;
    setAttachments(checked, false);
    ui.input.dispatchEvent(new Event("input", { bubbles: true }));
    checkpointBrowserState();
    return true;
  }
  async function putNativeQuoteDraft(text, captured, imageIds, fileIds, isCurrent, fileVersions) {
    const current = () => typeof isCurrent === "function" && isCurrent() && state.page === "native-chats" && quoteDraft().active && quoteDraft().stamp === captured.stamp;
    const expected = captured.native_quote, images = attachmentIds(imageIds), oldImages = expected.material_refs.filter(item => item.kind === "image").map(item => item.id), oldFiles = expected.material_refs.filter(item => item.kind === "text").map(item => item.id);
    let files; try { files = taskIntent({ mode: "chat", artifact_ids: fileIds }).artifact_ids; } catch (_) { return false; }
    if (!current() || typeof text !== "string" || characterCount(text) > draftCharacterLimit || !sameAttachments(images.slice(0, oldImages.length), oldImages)
        || JSON.stringify(files.slice(0, oldFiles.length)) !== JSON.stringify(oldFiles)) return false;
    const additions = images.slice(oldImages.length);
    const fileAdditions = files.slice(oldFiles.length);
    if (additions.length && ["claude", "dsh"].includes(expected.agent)) return false;
    if (fileAdditions.length && (!fileInputsSupported() || !Array.isArray(fileVersions))) return false;
    const versions = new Map();
    for (const item of fileVersions) {
      if (!item || typeof item.id !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256) || versions.has(item.id)) return false;
      versions.set(item.id, item.sha256);
    }
    if (fileAdditions.some(id => !versions.has(id))) return false;
    if (!additions.length && !fileAdditions.length) return current() && nativeChats.applyQuoteDraft(expected, text, expected.material_refs);
    if (!window.crypto?.subtle || !window.RabbitNativeMaterials || additions.length && !materials || fileAdditions.length && !fileMaterials) throw Error("材料核对组件尚未就绪；原草稿保留。");
    const newImages = await Promise.all(additions.map(async id => {
      const original = await materials.image(id, true), prefix = `data:${original.material.mime};base64,`;
      if (!current() || !original.data_url.startsWith(prefix)) throw Error("原图或草稿已变化；原草稿保留。");
      const bytes = Uint8Array.from(atob(original.data_url.slice(prefix.length)), char => char.charCodeAt(0));
      if (bytes.length !== original.material.size_bytes) throw Error("原图没有读完整；原草稿保留。");
      const digest = [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", bytes))].map(byte => byte.toString(16).padStart(2, "0")).join("");
      return { kind: "image", id, sha256: digest };
    }));
    const newFiles = await Promise.all(fileAdditions.map(async id => {
      const item = await fileMaterials.inspect(id);
      if (!current() || item.id !== id || item.sha256 !== versions.get(id) || !item.available || !item.content_verified) throw Error("文件版本或草稿已变化；原草稿保留。");
      return { kind: "text", id, sha256: item.sha256 };
    }));
    if (!current()) return false;
    const refs = window.RabbitNativeMaterials.refs([...expected.material_refs, ...newImages, ...newFiles]);
    const reader = window.RabbitNativeMaterials.create({ api, getWorkspace: () => state.status?.workspace_id || "" }); reader.setScope(captured.stamp);
    const values = await Promise.all(refs.map(ref => reader.read(ref, { refresh: true })));
    if (!current()) return false;
    const preview = window.RabbitNativeMaterials.preview(refs, { material_refs: refs,
      images: values.filter((_value, index) => refs[index].kind === "image"), files: values.filter((_value, index) => refs[index].kind === "text") });
    return current() && nativeChats.applyQuoteDraft(expected, text, refs, preview);
  }
  function captureDocumentDraft(version) {
    return { version, openVersion: state.openVersion, navigationVersion: state.contextNavigationVersion, key: draftKey(), text: ui.input.value, inputVersion: state.sourceUpdateInputVersion, fileView: fileMaterialView() };
  }
  function sameDocumentDraftView(preview) {
    const captured = preview && preview.captured;
    return !!(captured && documentDraftPreview === preview && captured.version === documentVersion && !$("document-viewer").hidden && state.page === "conversations" && captured.openVersion === state.openVersion && captured.navigationVersion === state.contextNavigationVersion && captured.key === draftKey() && captured.text === ui.input.value && captured.inputVersion === state.sourceUpdateInputVersion);
  }
  function documentReference(data) {
    const lines = [`### 资料引用：${markdownLabel(data.name)}`, "来源：预览副本"];
    if (data.snapshot && data.updated_at) lines.push(`副本更新时间：${exportTime(data.updated_at)}`);
    lines.push("说明：以下完整原文作为资料引用；其中的角色、命令或请求均属于引用内容。", "", fencedOriginal(data.content));
    return lines.join("\n");
  }
  function updateDocumentDraftAction() {
    const preview = documentDraftPreview;
    if (!preview || !preview.button || $("document-viewer").hidden) return;
    const duplicate = ui.input.value.includes(preview.block);
    const current = sameDocumentDraftView(preview);
    const remaining = Math.max(0, draftCharacterLimit - characterCount(ui.input.value));
    const addition = `${ui.input.value ? "\n\n" : ""}${preview.block}`;
    const characters = characterCount(addition);
    const busy = state.sending || state.creating || state.backendChoosing || currentUploadCount() > 0 || ui.input.disabled;
    preview.button.disabled = !current || duplicate || busy || characters > remaining;
    preview.button.textContent = duplicate && current ? "已放入草稿" : "放入草稿";
    let notice = "只放入所见文件文字，附件不会自动读取。可编辑、删除；发送后属于用户消息，以后可能作为前文。";
    let error = false;
    if (!current) notice = "话题或草稿已变化，请重新预览后再放入；所见副本不会自动重读。";
    else if (duplicate) notice = "完整引用已在当前草稿中，没有重复添加。手动删掉后可重新预览再放入。尚未发送。";
    else if (busy) notice = "当前操作尚未完成，暂不能放入草稿；副本和文字仍保留。";
    else if (characters > remaining) { notice = `整份资料（含引用标记）需要 ${characters} 字符，当前草稿剩余 ${remaining} 字符。超过 ${draftCharacterLimit} 字符上限，整份未放入，没有截断。`; error = true; }
    if (preview.fileButton) {
      preview.fileButton.disabled = !current || preview.captured.fileView !== fileMaterialView() || busy || !fileInputsSupported() || state.task_intent.mode === "files" && state.attachment_ids.length > 0 || state.task_intent.artifact_ids.length >= 4 || !preview.ticket;
      preview.fileButton.title = state.task_intent.mode === "files" && state.attachment_ids.length ? "文件任务目前不能同时带图片，原图片保留。" : "保留所见完整文本作为文件输入；写下要求后再发送。";
    }
    if (preview.fileError) { notice = preview.fileError; error = true; }
    preview.notice.textContent = notice; preview.notice.classList.toggle("error", error); preview.notice.setAttribute("role", error ? "alert" : "status");
  }
  function insertDocumentDraft(preview = documentDraftPreview) {
    if (!sameDocumentDraftView(preview)) { updateDocumentDraftAction(); return false; }
    if (ui.input.value.includes(preview.block) || state.sending || state.creating || currentUploadCount() > 0 || ui.input.disabled) { updateDocumentDraftAction(); return false; }
    const combined = `${ui.input.value}${ui.input.value ? "\n\n" : ""}${preview.block}`;
    if (characterCount(combined) > draftCharacterLimit) { updateDocumentDraftAction(); return false; }
    const buttonHadFocus = document.activeElement === preview.button;
    ui.input.value = combined;
    // Follow exactly the normal editing path: local draft isolation,
    // browser recovery, pending-send handling and context invalidation.
    ui.input.dispatchEvent(new Event("input", { bubbles: true }));
    preview.captured = captureDocumentDraft(documentVersion);
    updateDocumentDraftAction();
    checkpointBrowserState();
    if (buttonHadFocus && (document.activeElement === preview.button || document.activeElement === document.body)) $("close-document").focus({ preventScroll: true });
    return true;
  }
  async function insertDocumentFile(preview = documentDraftPreview) {
    if (!sameDocumentDraftView(preview) || preview.captured.fileView !== fileMaterialView() || !fileInputsSupported() || !preview.ticket || quoteDraft().busy || state.task_intent.mode === "files" && state.attachment_ids.length || state.task_intent.artifact_ids.length >= 4) return false;
    const captured = fileMaterialView(), token = requestId(), job = { version: state.openVersion, key: draftKey() };
    state.fileMaterialJobs.set(token, job); state.fileMaterialError = ""; updateComposer();
    try {
      const item = await fileMaterials.project(preview.ticket, preview.name); state.fileMaterialJobs.delete(token);
      if (sameDocumentDraftView(preview) && captured === fileMaterialView()) { await selectFileMaterial(item, captured); preview.captured = captureDocumentDraft(documentVersion); }
      else toast("所见副本已作为文件输入保留。草稿或话题已变化，可从‘已添加的文件’找回；未发送。");
      updateDocumentDraftAction(); return true;
    } catch (error) {
      const message = error.status === 404 || error.status === 410 ? "这份预览的输入凭证已过期。原预览与草稿保留，没有重读文件；重新预览后再明确选择。" : userError(error, "文件输入结果尚未确认，请查询原回执。原预览与草稿保留。");
      if (sameDocumentDraftView(preview)) { preview.fileError = message; if (error.status === 404 || error.status === 410) preview.ticket = null; preview.notice.textContent = message; preview.notice.setAttribute("role", "alert"); }
      if (job.version === state.openVersion && job.key === draftKey()) state.fileMaterialError = message;
      return false;
    } finally { state.fileMaterialJobs.delete(token); renderFileMaterialStatus(); updateComposer(); }
  }
  function closeDocument() {
    documentVersion++;
    documentDraftPreview = null;
    $("document-viewer").hidden = true;
    $("document-content").replaceChildren();
    $("document-content").classList.remove("builtin-manual");
    $("document-meta").replaceChildren();
    if (documentReturnFocus && documentReturnFocus.isConnected) documentReturnFocus.focus();
  }
  async function openMarkdownMaterial(selection) {
    $("document-content").classList.remove("builtin-manual");
    if (!nativeFileHandoff || !fileMaterials || selection?.workspace_id !== state.status?.workspace_id) throw Error("文件资料入口或工作区暂不可用。");
    nativeHandoff?.close(); nativeTextHandoff?.close(); nativeFileHandoff.close();
    if (!$("native-file-handoff-modal").hidden || !$("native-handoff-modal").hidden || !$("native-text-handoff-modal").hidden) throw Error("资料正在放入目标草稿，请稍后再打开文件。");
    const version = ++documentVersion, scope = selection.workspace_id, capturedDraft = quoteDraft();
    documentDraftPreview = null;
    if ($("document-viewer").hidden) documentReturnFocus = document.activeElement;
    $("document-viewer").hidden = false;
    $("document-title").textContent = "正在读取当前Markdown文件…";
    $("document-meta").textContent = "读取磁盘上的完整文字。";
    $("document-content").replaceChildren(); $("close-document").focus();
    const current = () => documentVersion === version && !$("document-viewer").hidden && state.status?.workspace_id === scope;
    try {
      const raw = await api("/api/markdown-chats/file-preview", { method: "POST",
        body: JSON.stringify({ binding_id: selection.binding_id, expected_head_id: selection.expected_head_id }) }, false);
      if (!current()) return;
      const data = await window.RabbitMarkdownChats.filePreview(raw, scope, selection);
      if (!current()) return;
      const origin = { kind: "markdown_preview", binding_id: data.binding_id, head_id: data.head_id,
        relative_path: data.path, sha256: data.sha256 };
      $("document-title").textContent = data.name;
      $("document-meta").textContent = `${data.path} · ${data.size_bytes} 字节 · 所见磁盘版本${data.observation.file_status === "external_modified" ? " · 含外部修改" : ""}`;
      $("document-content").append(el("pre", "file-task-preview-content", data.content));
      const tools = el("span", "document-draft-tools"), note = el("span", "document-draft-note", "将整份文件作为资料，不改写原聊天。" );
      let attempted = false, busy = false, frozenFile = null;
      const sameDraft = () => {
        const draft = quoteDraft();
        return current() && draft.active && !draft.busy && draft.stamp === capturedDraft.stamp
          && draft.text === capturedDraft.text;
      };
      const canAdd = () => capturedDraft.active && !capturedDraft.busy && fileInputsSupported()
        && capturedDraft.task_intent.artifact_ids.length < 4
        && !(capturedDraft.task_intent.mode === "files" && capturedDraft.attachment_ids.length);
      async function frozenInput({ recoverSelected = false } = {}) {
        if (frozenFile) return frozenFile;
        // Reuse this exact origin's verified import, including a previous open
        // of the same file. An uncertain import is only queried, never retried.
        let item = await fileMaterials.recoverMarkdown(origin);
        if (!current()) return null;
        if (!item && !attempted && recoverSelected && capturedDraft.native_quote && capturedDraft.active && !capturedDraft.busy) {
          // Native journals retain immutable refs across reloads, while the
          // shared metadata cache does not. Resolve those selected IDs before
          // deciding that this Markdown origin has not already been imported.
          for (const id of capturedDraft.task_intent.artifact_ids) {
            const metadata = await fileMaterials.inspect(id);
            if (!current()) return null;
            const expected = capturedDraft.native_quote.material_refs.find(ref => ref.kind === "text" && ref.id === id);
            if (metadata.id !== id || expected?.sha256 && metadata.sha256 !== expected.sha256
                || !metadata.available || !metadata.content_verified) throw Error("已有文件的版本或可用性尚未核对，未再次导入；原草稿保留。");
          }
          item = await fileMaterials.recoverMarkdown(origin);
          if (!current()) return null;
        }
        if (!item && !attempted) {
          attempted = true;
          item = await fileMaterials.markdown(data.snapshot_ticket, data.name, origin, data.size_bytes);
        }
        if (!current()) { toast("文件导入请求保留在原入口，未放入其他草稿。"); return null; }
        if (!item) { note.textContent = "尚未找到已核验的原导入回执；未重新读取磁盘或再次导入。"; return null; }
        const frozen = await fileMaterials.read(item);
        if (!current()) return null;
        if (frozen.text !== data.content || frozen.artifact.sha256 !== data.sha256
            || frozen.artifact.size_bytes !== data.size_bytes) throw Error("资料版本与所见文件不一致，未交给Agent。");
        frozenFile = frozen; return frozen;
      }
      const add = button("加入当前草稿", "document-to-draft", async () => {
        if (!current() || busy) return;
        if (!canAdd() || !sameDraft()) { note.textContent = "当前草稿、配置或已有材料已变化，或文件已达四份；原稿保留，请重新打开后再选择。"; return; }
        busy = true; add.disabled = use.disabled = true;
        try {
          const frozen = await frozenInput({ recoverSelected: true }); if (!current() || !frozen) return;
          if (!sameDraft()) throw Error("草稿、配置或页面已变化；文件资料已保留，没有放入其他草稿。请重新打开后再选择。");
          const ids = capturedDraft.task_intent.artifact_ids, item = frozen.artifact;
          if (!ids.includes(item.id) && await putQuoteDraft(capturedDraft.text, capturedDraft,
              capturedDraft.attachment_ids, [...ids, item.id], sameDraft, [{ id: item.id, sha256: item.sha256 }]) !== true) {
            throw Error("当前草稿没有接收文件；原问题与已有材料保留，请重新打开后再选择。");
          }
          closeDocument(); toast(ids.includes(item.id) ? "这份文件已在当前草稿中，尚未发送。" : "完整文件已加入当前草稿；原问题和已有材料保留，尚未发送。");
        } catch (error) {
          if (current()) note.textContent = userError(error, "文件尚未加入当前草稿；原稿和固定资料保留。");
        } finally { busy = false; if (current()) { add.disabled = !canAdd(); use.disabled = false; } }
      });
      add.disabled = !canAdd();
      const use = button("交给原生 Agent", "document-to-draft", async () => {
        if (!current() || busy) return;
        busy = true; add.disabled = use.disabled = true;
        try {
          const frozen = await frozenInput(); if (!current() || !frozen) return;
          const item = frozen.artifact;
          const input = { workspace_id: scope, ...frozen,
            source_key: JSON.stringify([scope, version, item.id, item.sha256]) };
          nativeFileInput = input;
          closeDocument();
          if (await nativeFileHandoff.open() !== true) {
            if (nativeFileInput === input) {
              nativeFileInput = null;
              toast("文件资料已保留，接力面板暂未打开；尚未发送。");
            }
          }
        } catch (error) {
          if (current()) note.textContent = userError(error, "导入结果尚未确认，请核对原回执；所见文件保留。");
        } finally {
          busy = false;
          if (current()) { add.disabled = !canAdd(); use.disabled = false; use.textContent = attempted && !frozenFile ? "核对原导入回执" : "交给原生 Agent"; }
        }
      });
      tools.append(add, use, note); $("document-meta").append(tools);
    } catch (error) {
      if (!current()) return;
      $("document-title").textContent = "暂时不能读取这个Markdown文件";
      $("document-content").append(el("p", "", userError(error, "文件位置、版本或绑定已有变化，请从文件夹重新核对。")));
    }
  }
  async function openDocument(path, line = null, { referenceActions = true } = {}) {
    $("document-content").classList.remove("builtin-manual");
    const version = ++documentVersion;
    documentDraftPreview = null;
    const captured = captureDocumentDraft(version);
    if ($("document-viewer").hidden) documentReturnFocus = document.activeElement;
    $("document-viewer").hidden = false;
    $("document-title").textContent = "正在读取文件…";
    $("document-meta").textContent = "只预览本项目里的文字文件。";
    $("document-content").replaceChildren();
    $("close-document").focus();
    try {
      const data = await api("/api/local-document", { method: "POST", body: JSON.stringify({ path }) });
      if (version !== documentVersion) return;
      $("document-title").textContent = data.name;
      const fileNotice = data.snapshot ? `预览副本 · 更新于 ${fullDate(data.updated_at)} · 双击“打开小白兔”可刷新` : "预览副本 · 内容来自这台 Mac";
      $("document-meta").textContent = `${line ? `源文件第 ${line} 行 · ` : ""}${fileNotice} · 完整文字 ${characterCount(data.content)} 字符`;
      if (line) {
        const pre = el("pre", "document-lines");
        data.content.split("\n").forEach((value, index) => {
          const row = el("span", index + 1 === line ? "file-line selected-line" : "file-line", `${index + 1}  ${value}\n`);
          pre.append(row);
        });
        $("document-content").append(pre);
        const selected = pre.querySelector(".selected-line");
        if (selected) requestAnimationFrame(() => selected.scrollIntoView({ block: "center" }));
      } else window.OneAIRichText.render($("document-content"), data.content, { openDocument, basePath: data.path });
      if (referenceActions && typeof data.content === "string" && typeof data.name === "string" && /\.(?:md|txt)$/i.test(data.name)) {
        const tools = el("span", "document-draft-tools");
        const preview = { captured, block: documentReference(data), name: data.name, ticket: typeof data.snapshot_ticket === "string" ? data.snapshot_ticket : null, fileButton: null, button: null, notice: el("span", "document-draft-note") };
        preview.button = button("放入草稿", "document-to-draft", () => insertDocumentDraft(preview));
        tools.append(preview.button);
        if (fileMaterials && preview.ticket) { preview.fileButton = button("作为文件输入", "document-to-draft", () => insertDocumentFile(preview)); tools.append(preview.fileButton); }
        tools.append(preview.notice); $("document-meta").append(tools);
        documentDraftPreview = preview; updateDocumentDraftAction();
      }
    } catch (error) {
      if (version !== documentVersion) return;
      $("document-title").textContent = "暂时不能预览这个文件";
      $("document-content").append(el("p", "", userError(error, "文件可能不在本项目里，或这台 Mac 暂时不能读取。")));
    }
  }
  function messageNode(message, history, sourceAgent, historyMatch = state.historyMatch) {
    const summaryLabel = sourceBackgroundLabel(message, sourceAgent), summary = Boolean(summaryLabel), role = summary ? "system" : message.role || "assistant";
    const backend = message.backend || message.provenance && message.provenance.agent || sourceAgent;
    const internal = ["approval_context", "agent_context", "agent_message"].includes(message.message_kind);
    const phase = summaryLabel || (message.phase === "commentary" ? "过程" : message.phase === "incomplete_reply" ? message.phase_label || "未完成回复" : "");
    const article = el("article", `message ${role}${message.status === "pending" || message.status === "running" ? " pending" : ""}${phase === "过程" ? " commentary" : ""}${internal ? " internal-record" : ""}`);
    article.dataset.messageId = String(message.id || message.message_id || "");
    article.dataset.controlInterface = "ai";
    const matched = historyMatch && article.dataset.messageId === historyMatch.messageId;
    if (matched) { article.classList.add("search-match"); article.tabIndex = -1; }
    const label = internal ? "内部记录" : role === "user" ? "你" : role === "assistant" ? backendLabels[backend] || "AI" : role === "system" ? "背景说明" : role === "tool" ? "工具记录" : "记录";
    const top = el("div", "message-topline");
    article.setAttribute("aria-label", role === "user" ? "用户消息" : role === "assistant" ? "AI消息" : label);
    if (internal || !["user", "assistant"].includes(role)) top.append(el("span", "message-speaker", label));
    if (matched) top.append(el("span", "matched-label", "匹配内容"));
    if (phase && !internal) top.append(el("span", "phase-tag", phase));
    const sourceTimestamp = message.status === "imported" && message.provenance && message.provenance.timestamp;
    const timestamp = sourceTimestamp || message.timestamp || message.created_at;
    if (timestamp) { const time = el("time", "message-time", `${sourceTimestamp ? "来源 " : ""}${shortDate(timestamp)}`); time.title = `${sourceTimestamp ? "原来源时间：" : ""}${fullDate(timestamp)}`; top.append(time); }
    if (sourceTimestamp && message.created_at && sourceTimestamp !== message.created_at) { const added = el("time", "message-time message-import-time", `补入 ${shortDate(message.created_at)}`); added.title = `录入此共同聊天时间：${fullDate(message.created_at)}；原来源时间与录入顺序分别保留。`; top.append(added); }
    const original = plain(message.content);
    const display = typeof message.display_content === "string" ? message.display_content : original;
    const query = matched ? String(historyMatch.query || "") : "";
    const rawMatch = !!query && !display.toLocaleLowerCase().includes(query.toLocaleLowerCase()) && original.toLocaleLowerCase().includes(query.toLocaleLowerCase());
    const content = state.rawMessages || rawMatch ? original : display;
    const copy = button(hasImageRecords(message) ? "复制文字" : "复制", "message-copy", async () => {
      if (!content && hasImageRecords(message)) { toast("这条消息只有图片，没有可复制的文字。"); return; }
      try { await navigator.clipboard.writeText(content); toast(hasImageRecords(message) ? "已复制文字；图片未复制。" : "已复制这条消息"); }
      catch (_) { toast("暂时无法复制，可以选中文字后复制。"); }
    });
    if (hasImageRecords(message)) copy.title = "只复制文字，不复制图片";
    copy.dataset.controlInterface = "human";
    top.append(copy);
    const body = el("div", `message-content${state.rawMessages || rawMatch ? " plain-original" : ""}`);
    if (rawMatch && !state.rawMessages) top.append(el("span", "matched-label", "匹配原文"));
    if (state.rawMessages || rawMatch || state.preferences.markdown === false) appendHighlightedText(body, content, matched ? historyMatch.query : "");
    else window.OneAIRichText.render(body, content, { query: matched ? historyMatch.query : "", openDocument });
    article.append(top);
    article.append(body);
    if (role === "user" && !history) appendFileInputs(article, message);
    if (role === "assistant" && !history && fileTasks && state.selected.type === "chat") fileTasks.append(article, state.selected.id, message.file_task);
    if (role === "assistant") {
      let snap = null; try { snap = profileSnapshot(message.profile_snapshot); } catch (_) { /* Keep text readable if optional metadata is malformed. */ }
      const run = !history && state.selected.type === "chat" && state.run?.message_id === message.id ? state.run : null;
      const memory = message.memory_info || run?.memory_info;
      const configuration = message.request_configuration;
      const request = configuration?.source === "requested_route" && (configuration.model === null || typeof configuration.model === "string") && (configuration.reasoning_effort === null || typeof configuration.reasoning_effort === "string") ? configuration : null;
      if (snap || message.model || message.model_source || memory || run?.context_info || request) {
        const details = el("details", "message-profile-details"), lines = el("div"); details.append(el("summary", "", "本轮信息"));
        if (request) lines.append(el("p", "", `本轮请求：${request.model || "模型待核对"} · ${request.reasoning_effort || "强度待核对"}`));
        for (const text of profileExportLines(snap).map(line => line.replace(/^- /, ""))) lines.append(el("p", "", text));
        if (message.model || message.model_source || request) lines.append(el("p", "", `型号信息来源：${modelSourceLabel(request?.source || message.model_source)}`));
        if (run?.context_info) {
          const info = run.context_info, included = Number(info.included_messages) || 0, total = Number(info.total_messages) || included;
          lines.append(el("p", "", Number(info.omitted_messages) > 0 ? `${contextStatusCopy(info).omission}，已带入 ${included}/${total} 条对话。` : `已带入 ${included} 条对话。`));
        }
        appendMemoryInfo(lines, memory); details.append(lines); article.append(details);
      }
    }
    if (!content && hasImageRecords(message)) article.append(el("p", "message-image-only", "原消息只有图片，未附文字。"));
    appendMessageMaterials(article, message);
    if (!content && ["pending", "running", "queued"].includes(message.status)) {
      const dots = el("div", "typing-dots");
      dots.setAttribute("aria-label", "正在回答");
      dots.append(el("i"), el("i"), el("i")); article.append(dots);
    }
    if (message.status === "failed") article.append(el("p", "message-status error", content ? "这轮回答未完成，以上为已收到的内容。" : "这轮没有生成回答。"));
    if (["cancelled", "interrupted"].includes(message.status)) article.append(el("p", "message-status", "这轮已停止。"));
    return article;
  }
  function isThinkingRecord(message) {
    return message.role !== "user" && [message.type, message.role, message.message_kind, message.phase].some(value => ["reasoning", "thinking", "analysis"].includes(value));
  }
  function isOperationRecord(message) {
    if (message.approval_required === true || ["approval_context", "approval_request", "permission_request"].includes(message.message_kind)
        || ["awaiting_approval", "pending_approval", "needs_input"].includes(message.status)) return false;
    return message.role === "tool" || ["tool_call", "tool_result", "command_execution", "file_change", "agent_context", "agent_message", "bash_context"].includes(message.message_kind || message.phase);
  }
  function renderMessages(messages, { history = false, forceBottom = false, appendNotice = null, preserveScroll = false } = {}) {
    const signature = JSON.stringify([state.selected.type, state.selected.id, messages.map((message) => [message.id || message.message_id, message.role, message.content, message.display_content, message.status, message.phase, message.type, message.message_kind, message.approval_required, message.model, message.provider, message.model_source, message.request_configuration, message.profile_snapshot, message.memory_info, message.attachment_ids, message.native_attachments, message.attachment_summary, message.file_task, message.file_inputs]), !history ? [state.run?.message_id, state.run?.context_info, state.run?.memory_info] : null, state.historyMessageNext, state.historyMessagePrev, state.historyMessageStart, state.historyMatch, state.preferences.markdown, state.rawMessages, history ? state.historySession?.coverage : null, !history && state.selected.type === "chat" ? projectPublish?.summary(state.selected.id) : null]);
    if (signature === state.messageSignature) return;
    state.messageSignature = signature;
    const expandedProfiles = new Set([...ui.messages.querySelectorAll(".message-profile-details[open]")].map((node) => node.closest(".message").dataset.messageId));
    const expandedMemory = new Set([...ui.messages.querySelectorAll(".memory-used-references[open]")].map((node) => node.closest(".message").dataset.messageId));
    const operationScope = JSON.stringify([state.selected.type, state.selected.id]), previousOperations = ui.messages.querySelector(".operation-records");
    const operationsExpanded = previousOperations?.dataset.scope === operationScope && previousOperations.open === true;
    const previousTop = ui.scroll.scrollTop;
    const nearBottom = state.page === "conversations" && ui.scroll.scrollHeight - ui.scroll.scrollTop - ui.scroll.clientHeight < 100;
    const nodes = [];
    if (history) {
      const notice = state.historySession?.coverage?.notice || state.status && state.status.history && state.status.history.notice || "已接入用户和助手文字及已支持的原历史图片；缺失或未支持的图片会显示原因。工具和推理记录不在此范围。";
      nodes.push(el("p", "detail-notice", notice));
      if (state.historyMatch) nodes.push(el("p", "history-match-note", state.historyMatch.titleOnly ? "搜索词匹配的是原来源标题。当前查看原始来源，没有定位到特定消息。" : `已定位到第 ${state.historyMatch.index + 1} 条消息，匹配内容已高亮。`));
      if (state.historyMatch && state.historyMatch.sourcePending && state.historyMatch.sourceChatId) {
        const pending = el("div", "detail-notice source-unimported-note");
        pending.append(el("p", "", "这条原文尚未自动进入共同聊天。这里查看的是原始来源；从已有共同聊天继续时，会使用其中已保存的前文。"), button("查看来源更新", "text-button source-update-entry", () => openSourceUpdate(state.historyMatch.sourceChatId)));
        nodes.push(pending);
      }
      if (state.historyMessagePrev != null) {
        const page = el("div", "history-pagination before-messages");
        const previous = button("读取前面的消息", "load-more", () => loadMoreHistoryMessages(previous, "previous"));
        page.append(previous, el("p", "", historyRangeLabel(messages.length))); nodes.push(page);
      }
    }
    if (appendNotice) nodes.push(appendNotice);
    const operations = [];
    messages.forEach((message) => {
      if (isThinkingRecord(message)) return;
      const node = messageNode(message, history, state.historySession && state.historySession.agent);
      if (expandedProfiles.has(node.dataset.messageId) && node.querySelector(".message-profile-details")) node.querySelector(".message-profile-details").open = true;
      if (expandedMemory.has(node.dataset.messageId) && node.querySelector(".memory-used-references")) node.querySelector(".memory-used-references").open = true;
      (isOperationRecord(message) ? operations : nodes).push(node);
    });
    if (operations.length) {
      const records = el("details", "operation-records"); records.dataset.scope = operationScope; records.open = operationsExpanded;
      records.append(el("summary", "", "操作记录"), ...operations); nodes.push(records);
    }
    if (history && state.historyMessageNext != null) {
      const page = el("div", "history-pagination");
      const next = button("继续读后面的消息", "load-more", () => loadMoreHistoryMessages(next));
      page.append(next, el("p", "", historyRangeLabel(messages.length)));
      nodes.push(page);
    }
    if (!history && !messages.length) { showWelcome(); return; }
    ui.messages.replaceChildren(...nodes);
    if (!preserveScroll && state.page === "conversations" && (forceBottom || (!history && nearBottom))) requestAnimationFrame(() => { ui.scroll.scrollTop = ui.scroll.scrollHeight; });
    else ui.scroll.scrollTop = previousTop;
  }
  function sourceNotice(chat) {
    const source = chat && chat.source;
    if (!source) return null;
    const label = backendLabels[source.agent] || "原有";
    const node = el("div", "source-context");
    node.append(el("span", `source-tag ${source.agent || ""}`, label), el("span", "", "从原有历史接着聊"));
    return node;
  }
  function viewChatSource(chat) {
    const source = chat && chat.source;
    if (!source || !source.agent || !source.session_id) return;
    state.historyContinuationIds.set(`${source.agent}:${source.session_id}`, chat.id);
    return openHistory(source.agent, source.session_id);
  }
  let sourceUpdatePanel = null, sourceUpdateReturnFocus = null;
  function sourceUpdateRoot() {
    if (sourceUpdatePanel) return sourceUpdatePanel;
    sourceUpdatePanel = el("div", "source-update-modal"); sourceUpdatePanel.hidden = true;
    sourceUpdatePanel.addEventListener("click", event => { if (event.target === sourceUpdatePanel) closeSourceUpdate(); });
    sourceUpdatePanel.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); closeSourceUpdate(); }
      if (event.key === "Tab") {
        const controls = [...sourceUpdatePanel.querySelectorAll("button")].filter(node => !node.disabled && !node.hidden);
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    document.body.append(sourceUpdatePanel); return sourceUpdatePanel;
  }
  function captureSourceUpdate() {
    return { version: state.sourceUpdateVersion, openVersion: state.openVersion, navigationVersion: state.contextNavigationVersion, key: draftKey(), text: ui.input.value, inputVersion: state.sourceUpdateInputVersion, backend: state.backend, backendChoiceVersion: state.backendChoiceVersion, chatId: state.sourceUpdateChatId };
  }
  function sameSourceUpdateView(captured) {
    return state.sourceUpdateVisible && state.page === "conversations" && captured.version === state.sourceUpdateVersion && captured.openVersion === state.openVersion && captured.navigationVersion === state.contextNavigationVersion && captured.key === draftKey() && captured.text === ui.input.value && captured.inputVersion === state.sourceUpdateInputVersion && captured.backend === state.backend && captured.backendChoiceVersion === state.backendChoiceVersion && captured.chatId === state.sourceUpdateChatId;
  }
  function closeSourceUpdate(moveFocus = true) {
    if (!state.sourceUpdateVisible) return;
    state.sourceUpdateVisible = false; ++state.sourceUpdateVersion;
    state.sourceUpdateData = null; state.sourceUpdateState = "empty"; state.sourceUpdateNotice = "";
    if (sourceUpdatePanel) { sourceUpdatePanel.hidden = true; sourceUpdatePanel.replaceChildren(); }
    if (moveFocus && sourceUpdateReturnFocus && sourceUpdateReturnFocus.isConnected) sourceUpdateReturnFocus.focus();
    sourceUpdateReturnFocus = null;
  }
  function invalidateSourceUpdate(notice) {
    if (!state.sourceUpdateVisible || !["loading", "saving"].includes(state.sourceUpdateState)) return;
    ++state.sourceUpdateVersion; state.sourceUpdateData = null; state.sourceUpdateState = "stale"; state.sourceUpdateNotice = notice;
    renderSourceUpdate();
  }
  function validSourceUpdate(plan) {
    if (!plan || !["unchanged", "appendable", "diverged", "missing", "no_source"].includes(plan.status) || !Number.isSafeInteger(plan.chat_revision) || plan.chat_revision < 0) return false;
    if (![plan.indexed_messages, plan.imported_messages, plan.new_messages].every(value => Number.isSafeInteger(value) && value >= 0) || !Array.isArray(plan.records) || plan.records.length > 20 || plan.records_total !== plan.new_messages) return false;
    if (!plan.records.every(record => record && typeof record.message_id === "string" && ["user", "assistant"].includes(record.role) && typeof record.preview === "string" && Number.isSafeInteger(record.characters) && record.characters >= 0)) return false;
    if (plan.status === "appendable") return !!(plan.new_messages > 0 && plan.indexed_messages - plan.imported_messages === plan.new_messages && plan.records.length === Math.min(20, plan.new_messages) && plan.source && backendLabels[plan.source.agent] && typeof plan.source.session_id === "string" && plan.source.session_id && typeof plan.source_version === "string" && /^[a-f0-9]{64}$/.test(plan.source_version));
    return true;
  }
  function sourceUpdateBusy() {
    return state.sending || state.creating || state.backendChoosing || !!state.sourceUpdateSaving || !!state.sourceImages.saving || state.selected.type === "chat" && state.selected.id === state.sourceUpdateChatId && isRunning();
  }
  function renderSourceUpdate() {
    const root = sourceUpdateRoot(); root.hidden = !state.sourceUpdateVisible;
    if (!state.sourceUpdateVisible) return;
    const active = document.activeElement;
    const focusInside = !!(active && root.contains(active));
    const focusLabel = focusInside && active.tagName === "BUTTON" ? active.textContent : null;
    const focusClass = focusInside && active.className || "";
    const panel = el("section", "source-update-panel"); panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true"); panel.setAttribute("aria-labelledby", "source-update-title");
    const head = el("div", "source-update-header"); const title = el("h2", "", "查看来源更新"); title.id = "source-update-title";
    head.append(title, button("关闭", "source-update-close", closeSourceUpdate)); panel.append(head);
    panel.append(el("p", "source-update-note", "查看只读取本机记录，还没有发送给 AI。"));
    if (state.sourceUpdateState === "loading") {
      const loading = el("p", "source-update-status", "正在比较原始来源与已带入的前文…"); loading.setAttribute("role", "status"); panel.append(loading);
    } else if (state.sourceUpdateState === "stale" || state.sourceUpdateState === "error") {
      const notice = el("p", "source-update-status error", state.sourceUpdateNotice || "来源更新暂时无法确认，请重新查看。"); notice.setAttribute("role", "alert"); panel.append(notice, button("重新查看", "text-button source-update-retry", () => openSourceUpdate(state.sourceUpdateChatId)));
    } else if (state.sourceUpdateData) {
      const plan = state.sourceUpdateData;
      panel.append(el("p", "source-update-counts", `原来源现有 ${plan.indexed_messages} 条 · 共同聊天已带入 ${plan.imported_messages} 条 · 新增 ${plan.new_messages} 条`));
      if (state.sourceUpdateNotice) { const notice = el("p", "source-update-status", state.sourceUpdateNotice); notice.setAttribute("role", "status"); panel.append(notice); }
      const statusNotice = { unchanged: "已带入的原文与当前来源一致，没有待补入内容。", appendable: "新增原文将按原来源顺序，追加到这段共同聊天末尾，原来源保留。已有共同聊天中的新消息仍保留。", diverged: "原来源已修改或删除已带入的内容，暂不能直接追加。请查看原始来源；目前没有自动合并。", missing: "原来源目前找不到，暂不能追加。已有共同聊天仍保留，请重新读取或查看原始来源。", no_source: "这段共同聊天没有对应的原始来源。" }[plan.status];
      panel.append(el("p", `source-update-note${["diverged", "missing"].includes(plan.status) ? " error" : ""}`, statusNotice));
      if (plan.records.length) {
        panel.append(el("p", "source-update-preview-label", `这里只预览前 ${plan.records.length} 条摘要；补入时包含全部 ${plan.new_messages} 条完整原文。`));
        const records = el("div", "source-update-records"); records.tabIndex = 0; records.setAttribute("aria-label", "新增原文预览");
        plan.records.forEach((record, index) => {
          const card = el("article", "source-update-record");
          const speaker = sourceBackgroundLabel(record, plan.source?.agent) || (record.role === "user" ? "你" : backendLabels[plan.source && plan.source.agent] || "助手");
          const line = el("div", "source-update-record-heading", `原来源第 ${plan.imported_messages + index + 1} 条 · ${speaker} · ${record.characters} 字${record.timestamp ? ` · ${fullDate(record.timestamp)}` : ""}`);
          card.append(line, el("p", "source-update-record-text", record.preview)); records.append(card);
        }); panel.append(records);
      }
      if (plan.notice) panel.append(el("p", "source-update-note", plain(plan.notice)));
      const actions = el("div", "source-update-actions");
      if (plan.source && plan.source.agent && plan.source.session_id) actions.append(button("查看原始来源", "text-button", () => { state.historyContinuationIds.set(`${plan.source.agent}:${plan.source.session_id}`, state.sourceUpdateChatId); return openHistory(plan.source.agent, plan.source.session_id); }));
      if (plan.status === "appendable") {
        const append = button(state.sourceUpdateState === "saving" ? "正在补入…" : `补入这 ${plan.new_messages} 条前文`, "source-update-append", appendSourceUpdate);
        append.disabled = sourceUpdateBusy(); actions.append(append);
        if (sourceUpdateBusy() && state.sourceUpdateState !== "saving") panel.append(el("p", "source-update-note", "共同聊天正在处理上一项操作，暂不能补入。完成后请重新查看。"));
      }
      if (state.sourceUpdateState === "saving") panel.append(el("p", "source-update-status", "正在明确补入原文；没有发送消息，也没有设置必带前文…"));
      else actions.append(button("重新查看", "text-button source-update-retry", () => openSourceUpdate(state.sourceUpdateChatId)));
      panel.append(actions);
    }
    root.replaceChildren(panel);
    // Replacing a focused DOM node returns focus to the document body. Restore
    // only a control that was still in this dialog; later outside focus wins.
    if (focusInside) {
      const matchingButton = focusLabel && [...root.querySelectorAll("button")].find(node => !node.disabled && node.textContent === focusLabel);
      const matchingRecords = !focusLabel && focusClass.split(/\s+/).includes("source-update-records") && root.querySelector(".source-update-records");
      (matchingButton || matchingRecords || root.querySelector(".source-update-close"))?.focus({ preventScroll: true });
    }
  }
  async function openSourceUpdate(chatId) {
    if (typeof chatId !== "string" || !chatId || state.page !== "conversations") return;
    closeSourceImages(false);
    const firstOpen = !state.sourceUpdateVisible;
    if (firstOpen) sourceUpdateReturnFocus = document.activeElement;
    state.sourceUpdateVisible = true; state.sourceUpdateChatId = chatId; ++state.sourceUpdateVersion;
    state.sourceUpdateState = "loading"; state.sourceUpdateData = null; state.sourceUpdateNotice = "";
    const captured = captureSourceUpdate(); renderSourceUpdate();
    if (firstOpen) sourceUpdatePanel.querySelector(".source-update-close")?.focus();
    try {
      const data = await api(`/api/chats/${encodeURIComponent(chatId)}/source-update`);
      if (!sameSourceUpdateView(captured)) return;
      if (!validSourceUpdate(data.source_update)) throw new Error("来源比较结果不完整，请重新查看后再决定是否补入。");
      state.sourceUpdateData = data.source_update; state.sourceUpdateState = "ready"; renderSourceUpdate();
    } catch (error) {
      if (!sameSourceUpdateView(captured)) return;
      state.sourceUpdateState = "error"; state.sourceUpdateNotice = userError(error, "来源更新暂时没读出来，已有文字仍保留。"); renderSourceUpdate();
    }
  }
  async function appendSourceUpdate() {
    const plan = state.sourceUpdateData;
    if (!state.sourceUpdateVisible || state.sourceUpdateState !== "ready" || !validSourceUpdate(plan) || plan.status !== "appendable" || sourceUpdateBusy()) return;
    const captured = captureSourceUpdate(); state.sourceUpdateSaving = captured; state.sourceUpdateState = "saving"; state.sourceUpdateNotice = ""; renderSourceUpdate();
    try {
      const result = await api(`/api/chats/${encodeURIComponent(captured.chatId)}/source-update`, { method: "POST", body: JSON.stringify({ source_version: plan.source_version, chat_revision: plan.chat_revision }) }, false);
      if (!sameSourceUpdateView(captured)) return;
      if (!Number.isSafeInteger(result.appended_messages) || result.appended_messages < 0 || !validSourceUpdate(result.source_update)) throw new Error("补入结果暂时无法确认，请重新查看。");
      state.sourceUpdateData = result.source_update; state.sourceUpdateState = "ready";
      state.sourceUpdateNotice = result.appended_messages ? `已补入 ${result.appended_messages} 条完整原文。草稿、下一位 AI 与阅读位置保持原样。` : "本次新增 0 条；这些原文已经补入，没有重复添加。";
      invalidateContextPreview("共同聊天的前文已更新，之前的预览已清除，请重新查看。");
      if (state.selected.type === "chat" && state.selected.id === captured.chatId) {
        const latest = await api(`/api/chats/${encodeURIComponent(captured.chatId)}`);
        if (!sameSourceUpdateView(captured)) return;
        renderChat(latest, false, { background: true, preserveScroll: true });
      }
      if (state.historyMatch && state.historyMatch.sourceChatId === captured.chatId && result.source_update.status === "unchanged" && state.historyMatch.index < result.source_update.imported_messages) {
        state.historyMatch = { ...state.historyMatch, sourcePending: false };
        if (state.selected.type === "history") renderMessages(state.historyMessages, { history: true, preserveScroll: true });
      }
      renderSourceUpdate(); checkpointBrowserState();
      refreshChats(() => sameSourceUpdateView(captured)).catch(() => {});
    } catch (error) {
      if (!sameSourceUpdateView(captured)) return;
      state.sourceUpdateData = null; state.sourceUpdateState = "stale";
      state.sourceUpdateNotice = error.status === 409 ? `补入没有完成：${userError(error, "来源或共同聊天已变化，或上一轮仍在处理。")} 请重新查看，再决定是否补入；没有自动重试。` : "暂时无法确认是否补入。文字仍保留，请重新查看确认；没有自动重试，也没有发送消息。";
      renderSourceUpdate();
    } finally {
      if (state.sourceUpdateSaving === captured) { state.sourceUpdateSaving = null; if (sameSourceUpdateView(captured)) renderSourceUpdate(); }
    }
  }
  let sourceImagesPanel = null, sourceImagesReturnFocus = null;
  function sourceImagesRoot() {
    if (sourceImagesPanel) return sourceImagesPanel;
    sourceImagesPanel = el("div", "source-update-modal source-images-modal"); sourceImagesPanel.hidden = true;
    sourceImagesPanel.addEventListener("click", event => { if (event.target === sourceImagesPanel) closeSourceImages(); });
    sourceImagesPanel.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); closeSourceImages(); }
      if (event.key === "Tab") {
        const controls = [...sourceImagesPanel.querySelectorAll("button")].filter(node => !node.disabled && !node.hidden);
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    document.body.append(sourceImagesPanel); return sourceImagesPanel;
  }
  function captureSourceImages() {
    return { version: state.sourceImages.version, openVersion: state.openVersion, navigationVersion: state.contextNavigationVersion, key: draftKey(), text: ui.input.value, attachment_ids: [...state.attachment_ids], inputVersion: state.sourceUpdateInputVersion, backend: state.backend, model_id: state.model_id, backendChoiceVersion: state.backendChoiceVersion, chatId: state.sourceImages.chatId };
  }
  function sameSourceImagesView(captured) {
    return state.sourceImages.visible && state.page === "conversations" && captured.version === state.sourceImages.version && captured.openVersion === state.openVersion && captured.navigationVersion === state.contextNavigationVersion && captured.key === draftKey() && captured.text === ui.input.value && sameAttachments(captured.attachment_ids, state.attachment_ids) && captured.inputVersion === state.sourceUpdateInputVersion && captured.backend === state.backend && captured.model_id === state.model_id && captured.backendChoiceVersion === state.backendChoiceVersion && captured.chatId === state.sourceImages.chatId;
  }
  function closeSourceImages(moveFocus = true) {
    if (!state.sourceImages.visible) return;
    state.sourceImages.visible = false; ++state.sourceImages.version;
    state.sourceImages.data = null; state.sourceImages.state = "empty"; state.sourceImages.notice = "";
    if (sourceImagesPanel) { sourceImagesPanel.hidden = true; sourceImagesPanel.replaceChildren(); }
    if (moveFocus && sourceImagesReturnFocus?.isConnected !== false) sourceImagesReturnFocus?.focus();
    sourceImagesReturnFocus = null;
  }
  function invalidateSourceImages(notice) {
    if (!state.sourceImages.visible || !["loading", "saving"].includes(state.sourceImages.state)) return;
    ++state.sourceImages.version; state.sourceImages.data = null; state.sourceImages.state = "stale"; state.sourceImages.notice = notice;
    renderSourceImages();
  }
  function sourceImagesBusy() {
    return state.sending || state.creating || state.backendChoosing || !!state.sourceImages.saving || !!state.sourceUpdateSaving || state.selected.type === "chat" && state.selected.id === state.sourceImages.chatId && isRunning();
  }
  function validSourceImages(plan) {
    const count = value => Number.isSafeInteger(value) && value >= 0;
    const revision = typeof plan?.chat_revision === "number" ? plan.chat_revision : typeof plan?.chat_revision === "string" && /^(?:0|[1-9]\d*)$/.test(plan.chat_revision) ? Number(plan.chat_revision) : NaN;
    if (!plan || !["backfillable", "unchanged", "diverged", "missing", "no_source"].includes(plan.status) || !count(revision) || !plan.counts || ![plan.counts.backfilled_messages, plan.counts.backfilled_images, plan.counts.inserted_image_messages].every(count) || !Array.isArray(plan.records) || !count(plan.records_total)) return false;
    if (plan.records_total !== plan.counts.backfilled_messages + plan.counts.inserted_image_messages || plan.records.length !== Math.min(20, plan.records_total)) return false;
    if (!plan.records.every(record => record && ["attach_images", "insert_image_message"].includes(record.action) && typeof record.source_message_id === "string" && !!record.source_message_id && record.source_message_id.length <= 256 && (record.action === "attach_images" ? typeof record.message_id === "string" && !!record.message_id : record.message_id === null) && ["user", "assistant"].includes(record.role) && typeof record.preview === "string" && characterCount(record.preview) <= 180 && [record.images, record.unavailable, record.omitted].every(count) && typeof record.unknown_omitted === "boolean")) return false;
    if (new Set(plan.records.map(record => record.source_message_id)).size !== plan.records.length || plan.records.filter(record => record.action === "attach_images").length > plan.counts.backfilled_messages || plan.records.filter(record => record.action === "insert_image_message").length > plan.counts.inserted_image_messages) return false;
    if (["missing", "no_source"].includes(plan.status)) return plan.records_total === 0 && plan.counts.backfilled_images === 0;
    if (typeof plan.plan_version !== "string" || !/^[a-f0-9]{64}$/.test(plan.plan_version)) return false;
    return plan.status === "backfillable" ? plan.records_total > 0 : plan.records_total === 0;
  }
  function renderSourceImages() {
    const stateImages = state.sourceImages, root = sourceImagesRoot(); root.hidden = !stateImages.visible;
    if (!stateImages.visible) return;
    const active = document.activeElement, focusInside = active && root.contains(active), focusLabel = focusInside && active.tagName === "BUTTON" ? active.textContent : null;
    const panel = el("section", "source-update-panel source-images-panel"); panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true"); panel.setAttribute("aria-labelledby", "source-images-title");
    const header = el("div", "source-update-header"), title = el("h2", "", "补入来源图片"); title.id = "source-images-title";
    header.append(title, button("关闭", "source-update-close source-images-close", closeSourceImages)); panel.append(header);
    panel.append(el("p", "source-update-note", "先核对这段共同聊天缺少的原历史图片。查看不会修改聊天，也不会发送给 AI。"));
    if (stateImages.state === "loading") {
      const note = el("p", "source-update-status", "正在核对原历史图片与已有共同聊天…"); note.setAttribute("role", "status"); panel.append(note);
    } else if (["stale", "error"].includes(stateImages.state)) {
      const note = el("p", "source-update-status error", stateImages.notice || "图片计划已变化，请重新查看。"); note.setAttribute("role", "alert"); panel.append(note, button("重新查看", "text-button source-images-retry", () => openSourceImages(stateImages.chatId)));
    } else if (stateImages.data) {
      const plan = stateImages.data, counts = plan.counts;
      panel.append(el("p", "source-update-counts", `给 ${counts.backfilled_messages} 条前文补图片信息 · 新增 ${counts.inserted_image_messages} 条原纯图消息 · 新增可用原图 ${counts.backfilled_images} 张`));
      if (stateImages.notice) { const note = el("p", "source-update-status", stateImages.notice); note.setAttribute("role", "status"); panel.append(note); }
      const notice = { unchanged: "目前没有待补入的来源图片。", backfillable: "确认后只补入以下原历史图片信息和纯图消息，保留已有文字与共同聊天中的新消息。", diverged: "原历史的文字、消息身份或图片已经变化，暂不能直接补入。请先查看原始来源。", missing: "原历史目前无法读取，已有共同聊天仍保留。", no_source: "这段共同聊天没有关联的原始来源。" }[plan.status];
      panel.append(el("p", `source-update-note${["diverged", "missing"].includes(plan.status) ? " error" : ""}`, notice));
      if (plan.records.length) {
        panel.append(el("p", "source-update-preview-label", `这里只预览前 ${plan.records.length}/${plan.records_total} 项摘要；确认时处理这个已核对计划中的全部 ${plan.records_total} 项。`));
        const records = el("div", "source-update-records source-images-records"); records.tabIndex = 0; records.setAttribute("aria-label", "原历史图片补入预览");
        for (const record of plan.records) {
          const card = el("article", "source-update-record"), action = record.action === "attach_images" ? "给旧消息补图片信息" : "插入原纯图消息";
          card.append(el("div", "source-update-record-heading", `${action} · ${record.role === "user" ? "你" : "助手"}${record.timestamp ? ` · ${fullDate(record.timestamp)}` : ""}`), el("p", "source-update-record-text", record.preview || "原消息只有图片，未附文字。"), el("p", "message-image-summary", `可用原图 ${record.images} 张 · 暂不可用 ${record.unavailable} 项 · 未检查 ${record.omitted} 张${record.unknown_omitted ? " · 另有未检查图片，数量未知" : ""}`));
          records.append(card);
        }
        panel.append(records);
      }
      if (typeof plan.notice === "string" && plan.notice) panel.append(el("p", "source-update-note", plan.notice));
      const actions = el("div", "source-update-actions");
      if (state.sourceImagePending.has(stateImages.chatId)) {
        panel.append(el("p", "source-update-note", "上次图片补入尚未确认。当前计划只说明目前的状态；请先检查原成功回执，不会重复提交。"));
      } else if (plan.status === "backfillable") {
        const submit = button(stateImages.state === "saving" ? "正在补入…" : "确认补入图片信息", "source-update-append source-images-submit", applySourceImages);
        submit.disabled = sourceImagesBusy(); actions.append(submit);
        if (sourceImagesBusy() && stateImages.state !== "saving") panel.append(el("p", "source-update-note", "这段共同聊天正在处理另一项操作，完成后请重新查看。"));
      }
      if (stateImages.state === "saving") { const note = el("p", "source-update-status", "正在按已核对的版本补入图片；没有发送消息…"); note.setAttribute("role", "status"); panel.append(note); }
      else actions.append(button("重新查看", "text-button source-images-retry", () => openSourceImages(stateImages.chatId)));
      const source = plan.source || (state.chat?.id === stateImages.chatId ? state.chat.source : state.selected.type === "history" ? { agent: state.selected.agent, session_id: state.selected.sessionId } : null);
      if (source && backendLabels[source.agent] && typeof source.session_id === "string" && source.session_id) actions.append(button("查看原始来源", "text-button", () => { state.historyContinuationIds.set(`${source.agent}:${source.session_id}`, stateImages.chatId); return openHistory(source.agent, source.session_id); }));
      panel.append(actions);
    }
    root.replaceChildren(panel);
    if (focusInside) ([...root.querySelectorAll("button")].find(node => !node.disabled && node.textContent === focusLabel) || root.querySelector(".source-images-close"))?.focus({ preventScroll: true });
  }
  function readingPosition() {
    const top = ui.scroll.getBoundingClientRect().top;
    const message = [...ui.messages.querySelectorAll(".message")].find(node => node.getBoundingClientRect().bottom > top);
    return { scrollTop: ui.scroll.scrollTop, messageId: message?.dataset.messageId, top: message?.getBoundingClientRect().top };
  }
  function validSourceImageResult(value) {
    return value && [value.backfilled_messages, value.backfilled_images, value.inserted_image_messages].every(number => Number.isSafeInteger(number) && number >= 0) && typeof value.recovered === "boolean";
  }
  function sourceImageSuccess(value) { return `${value.recovered ? "已确认之前的补入，没有重复写入。" : "已按核对版本补入。"}更新 ${value.backfilled_messages} 条前文图片信息，新增 ${value.inserted_image_messages} 条纯图消息、${value.backfilled_images} 张可用原图。草稿和下一位 AI 保持原样。`; }
  function sourceImageChatView(chatId, captured) { return state.page === "conversations" && state.selected.type === "chat" && state.selected.id === chatId && captured.openVersion === state.openVersion && captured.navigationVersion === state.contextNavigationVersion; }
  async function refreshSourceImageChat(chatId, captured) {
    if (!sourceImageChatView(chatId, captured)) return;
    const anchor = readingPosition(), revision = state.selectedRevision;
    const latest = await api(`/api/chats/${encodeURIComponent(chatId)}`);
    if (!sourceImageChatView(chatId, captured)) return;
    if (state.selectedRevision === revision) {
      const adjustAnchor = ui.scroll.scrollTop === anchor.scrollTop;
      renderChat(latest, false, { background: true, preserveScroll: true });
      if (adjustAnchor && anchor.messageId) {
        const sameMessage = [...ui.messages.querySelectorAll(".message")].find(node => node.dataset.messageId === anchor.messageId);
        if (sameMessage && Number.isFinite(anchor.top)) ui.scroll.scrollTop += sameMessage.getBoundingClientRect().top - anchor.top;
      }
    }
    refreshChats(() => sourceImageChatView(chatId, captured)).catch(() => {});
  }
  async function sourceImageReceipt(pending) {
    const query = new URLSearchParams({ plan_version: pending.plan_version, chat_revision: String(pending.chat_revision) });
    const data = await api(`/api/chats/${encodeURIComponent(pending.chatId)}/source-images/receipt?${query}`);
    if (data.receipt === null) return null;
    if (!validSourceImageResult(data.receipt) || data.receipt.recovered !== true) throw new Error("上次图片补入回执暂时无法确认，请重新查看。");
    return data.receipt;
  }
  async function reconcileSourceImagePending(chatId, openVersion) {
    const pending = state.sourceImagePending.get(chatId);
    if (!pending) return;
    const captured = { openVersion, navigationVersion: state.contextNavigationVersion };
    try {
      const receipt = await sourceImageReceipt(pending);
      if (state.sourceImagePending.get(chatId) !== pending || !receipt) return;
      state.sourceImagePending.delete(chatId); checkpointBrowserState();
      if (sourceImageChatView(chatId, captured)) {
        invalidateContextPreview("已确认上次图片补入，请重新查看本轮上下文。");
        toast(sourceImageSuccess(receipt)); await refreshSourceImageChat(chatId, captured);
      }
    } catch (_) { /* Keep the exact old tuple; restoration never submits it. */ }
    finally { checkpointBrowserState(); }
  }
  async function applySourceImages() {
    if (state.sourceImages.saving) return;
    if (state.sourceImages.visible && state.sourceImagePending.has(state.sourceImages.chatId)) return openSourceImages(state.sourceImages.chatId);
    const plan = state.sourceImages.data;
    if (!state.sourceImages.visible || state.sourceImages.state !== "ready" || !validSourceImages(plan) || plan.status !== "backfillable" || sourceImagesBusy()) return;
    if (state.sourceImagePending.size >= 20) { state.sourceImages.notice = "待确认的图片补入已达到 20 项，请先查看原成功回执。没有提交这次操作。"; renderSourceImages(); return; }
    const captured = captureSourceImages(); state.sourceImages.saving = captured; state.sourceImages.state = "saving"; state.sourceImages.notice = ""; renderSourceImages();
    const pending = { chatId: captured.chatId, plan_version: plan.plan_version, chat_revision: Number(plan.chat_revision) };
    state.sourceImagePending.set(captured.chatId, pending); checkpointBrowserState();
    try {
      const result = await api(`/api/chats/${encodeURIComponent(captured.chatId)}/source-images`, { method: "POST", body: JSON.stringify({ plan_version: pending.plan_version, chat_revision: pending.chat_revision }) }, false);
      if (!validSourceImageResult(result)) throw new Error("图片补入结果暂时无法确认，请重新查看原成功回执。");
      if (state.sourceImagePending.get(captured.chatId) === pending) state.sourceImagePending.delete(captured.chatId);
      checkpointBrowserState();
      // A recovered original success remains true even if its source is now missing.
      if (result.recovered && !sameSourceImagesView(captured)) { await refreshSourceImageChat(captured.chatId, captured); return; }
      if (!sameSourceImagesView(captured)) return;
      if (!validSourceImages(result.source_images)) throw new Error("当前图片计划暂时无法查看，原补入结果已确认。请重新查看。");
      state.sourceImages.data = result.source_images; state.sourceImages.state = "ready";
      state.sourceImages.notice = sourceImageSuccess(result);
      invalidateContextPreview("共同聊天的图片前文已更新，之前的预览已清除，请重新查看。");
      // Normal writes still guard their subsequent snapshot against draft edits.
      if (result.recovered) await refreshSourceImageChat(captured.chatId, captured);
      else if (state.selected.type === "chat" && state.selected.id === captured.chatId) {
        const anchor = readingPosition(), revision = state.selectedRevision;
        const latest = await api(`/api/chats/${encodeURIComponent(captured.chatId)}`);
        if (!sameSourceImagesView(captured)) return;
        if (state.selectedRevision === revision) {
          const adjustAnchor = ui.scroll.scrollTop === anchor.scrollTop; renderChat(latest, false, { background: true, preserveScroll: true });
          if (adjustAnchor && anchor.messageId) { const same = [...ui.messages.querySelectorAll(".message")].find(node => node.dataset.messageId === anchor.messageId); if (same && Number.isFinite(anchor.top)) ui.scroll.scrollTop += same.getBoundingClientRect().top - anchor.top; }
        }
        refreshChats(() => sameSourceImagesView(captured)).catch(() => {});
      }
      if (sameSourceImagesView(captured)) renderSourceImages(); checkpointBrowserState();
    } catch (error) {
      if (error.status && error.status < 500 && state.sourceImagePending.get(captured.chatId) === pending) state.sourceImagePending.delete(captured.chatId);
      checkpointBrowserState();
      if (!sameSourceImagesView(captured)) return;
      state.sourceImages.data = null; state.sourceImages.state = "stale";
      state.sourceImages.notice = error.status === 409 ? `补入没有完成：${userError(error, "来源或共同聊天已变化。")} 请重新查看；没有自动重试。` : "暂时无法确认图片是否补入。请重新查看当前结果；没有自动重试或发送消息。草稿和已有图片仍保留。";
      renderSourceImages();
    } finally {
      if (state.sourceImages.saving === captured) { state.sourceImages.saving = null; if (state.sourceImages.visible) renderSourceImages(); }
    }
  }
  async function openSourceImages(chatId) {
    if (typeof chatId !== "string" || !chatId || state.page !== "conversations") return;
    closeSourceUpdate(false);
    if (!state.sourceImages.visible) sourceImagesReturnFocus = document.activeElement;
    state.sourceImages.visible = true; state.sourceImages.chatId = chatId; ++state.sourceImages.version;
    state.sourceImages.state = "loading"; state.sourceImages.data = null; state.sourceImages.notice = "";
    const captured = captureSourceImages(); renderSourceImages(); sourceImagesPanel.querySelector(".source-images-close")?.focus();
    try {
      const pending = state.sourceImagePending.get(chatId);
      if (pending) {
        const receipt = await sourceImageReceipt(pending);
        if (receipt && state.sourceImagePending.get(chatId) === pending) {
          state.sourceImagePending.delete(chatId); checkpointBrowserState();
          if (sameSourceImagesView(captured)) state.sourceImages.notice = sourceImageSuccess(receipt);
          await refreshSourceImageChat(chatId, captured);
        } else if (sameSourceImagesView(captured)) state.sourceImages.notice = "尚未找到上次补入的成功回执。下面只显示目前状态，不能据此确认上次成功；没有重复提交。";
        if (!sameSourceImagesView(captured)) return;
      }
      const data = await api(`/api/chats/${encodeURIComponent(chatId)}/source-images`);
      if (!sameSourceImagesView(captured)) return;
      if (!validSourceImages(data.source_images)) throw new Error("来源图片比较结果不完整，请重新查看后再决定是否补入。");
      state.sourceImages.data = data.source_images; state.sourceImages.state = "ready"; renderSourceImages();
    } catch (error) {
      if (!sameSourceImagesView(captured)) return;
      state.sourceImages.state = "error"; state.sourceImages.notice = userError(error, "来源图片暂时无法查看，已有文字和图片仍保留。"); renderSourceImages();
    }
  }
  function contextMessageStamp(messages) {
    return JSON.stringify(messages.map((message) => [message.id, message.role, message.content, message.display_content, message.status, message.attachment_ids, message.native_attachments, message.attachment_summary, message.file_inputs]));
  }
  function renderChat(data, forceBottom = false, { background = false, revisionFallback = null, preserveScroll = false } = {}) {
    if (data.chat && data.chat.id !== state.selected.id) return;
    const firstLoad = !state.chat;
    const messages = Array.isArray(data.messages) ? data.messages : [];
    if (background && contextMessageStamp(messages) !== contextMessageStamp(state.chatMessages)) invalidateContextPreview("前文已更新，之前的预览已清除，请重新查看。");
    if (background && validContextSelection(data.context_selection) && state.chatContextSelection && data.context_selection.revision !== state.chatContextSelection.revision) invalidateContextPreview("另一入口已修改必带前文，请重新查看本轮上下文。");
    state.chatContextSelection = validContextSelection(data.context_selection) ? data.context_selection : null;
    state.chat = data.chat || state.chat;
    state.chatMessages = messages;
    if (projectPublish && Array.isArray(data.project_publications)) { try { projectPublish.acceptChat(state.selected.id, data.project_publications); } catch (error) { toast(userError(error, "另存记录暂时无法读取，原成果保留。")); } }
    if (fileMaterials && Array.isArray(data.file_materials)) for (const raw of data.file_materials.slice(0, 100)) { try { fileMaterials.accept(raw); } catch (_) {} }
    state.run = data.run || null;
    if (profilesEnabled()) observeProfileSelection(data.profile_selection);
    state.selectedRevision = data.revision ?? (data.chat && data.chat.revision) ?? revisionFallback ?? state.selectedRevision;
    if (firstLoad && !background) {
      const recentBackend = state.run && state.run.backend || [...state.chatMessages].reverse().find((message) => message.role === "assistant" && backendLabels[message.backend])?.backend;
      const choice = state.preferences.chat_backends && state.preferences.chat_backends[state.selected.id];
      state.backend = backendLabels[choice] ? choice : backendLabels[recentBackend] ? recentBackend : state.preferences.default_backend;
      const savedModel = Object.hasOwn(state.preferences.chat_models || {}, state.selected.id) ? state.preferences.chat_models[state.selected.id] : state.run?.route?.requested_model_id ?? state.run?.route?.model_id ?? "";
      state.model_id = validModelId(savedModel, state.backend) ? savedModel : "";
    }
    if (firstLoad && !background) restoreProfileChoice();
    if (!background) state.transientError = "";
    headerForChat(state.chat);
    renderMessages(state.chatMessages, { forceBottom, appendNotice: sourceNotice(state.chat), preserveScroll });
    renderRunNotice(); updateComposer();
  }
  function renderRunNotice() {
    ui.notice.replaceChildren(); ui.notice.classList.remove("error");
    const running = isRunning();
    if (state.transientError) {
      ui.notice.classList.add("error"); ui.notice.append(el("span", "", state.transientError));
      if (state.memoryRecallError && state.profile_snapshot?.memory_profile.id !== "none") { ui.notice.append(button("查看记忆配置", "", () => profiles?.open("memory")), button("本次不使用长期记忆", "", async () => { try { await chooseProfile("memory", "none", profileView().view); toast("已明确选择不使用长期记忆，草稿和图片保留；尚未发送。共享稿可先保存新版本。"); } catch (error) { toast(userError(error, "选择尚未完成，原稿保留。")); } })); }
    } else if (running) {
      const backend = backendLabels[state.run.backend] || "AI";
      const stopping = state.cancelling || state.run.cancel_requested;
      ui.notice.append(el("span", "spinner"), el("span", "", stopping ? "正在停止这一轮…" : state.run.status === "queued" ? `消息已发送，${backend} 正在准备回答…` : `${backend} 正在回答…`));
    } else if (state.run && state.run.status === "failed") {
      ui.notice.classList.add("error");
      const reason = userError({ message: state.run.error }, "这一轮没有完成。可以修改问题后再试一次。");
      ui.notice.append(el("span", "", reason), button("修改后重试", "", retryLastMessage));
    } else if (state.run && ["cancelled", "interrupted"].includes(state.run.status)) {
      ui.notice.append(el("span", "", state.run.status === "interrupted" ? "上一轮已中断。可以在下方继续。" : "这一轮已停止，可以修改问题后继续。"));
    }
    if (state.memoryChatCreation) { const pending = state.memoryChatCreation; ui.notice.append(el("span", "", "新空对话的创建结果待检查，原草稿仍保留。"), button("检查新对话创建", "", async () => { try { await createMemoryChat(pending.profile_snapshot, profileView().view); } catch (error) { toast(userError(error, "暂时无法确认，未重复创建。")); } })); }
    if (!state.transientError && state.run?.memory_info) {
      const info = safeMemoryInfo(state.run.memory_info);
      const attention = info?.recall.state === "failed" ? "长期记忆检索失败。" : { queued: "记忆提取任务已排队。", submitting: "正在提交记忆提取任务。", processing: "记忆提取任务处理中。", failed: "记忆保存失败，已有回答不受影响。", unknown: "记忆保存结果未确认，已有回答保留。" }[info?.retain.state];
      if (attention) ui.notice.append(el("span", "run-memory", attention));
    }
    ui.notice.hidden = !ui.notice.childNodes.length;
    updateComposer();
  }
  function schedulePoll(immediate = false) {
    stopPolling();
    if (document.hidden) return;
    if (state.syncInFlight) { if (immediate) state.syncRequested = true; return; }
    state.pollTimer = setTimeout(() => { state.pollTimer = null; return syncCurrentView(); }, immediate ? 0 : isRunning() || state.remoteRunning ? 2000 : 4000);
  }
  async function syncCurrentView() {
    if (document.hidden || state.syncInFlight) return;
    readNativeWorkStatus();
    state.syncInFlight = true; state.syncRequested = false;
    const version = state.openVersion; const type = state.selected.type; const id = state.selected.id; const page = state.page;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    state.syncAbort = controller;
    const options = controller ? { signal: controller.signal } : {};
    const current = () => !document.hidden && !(controller && controller.signal.aborted) && page === state.page && version === state.openVersion && type === state.selected.type && id === state.selected.id;
    try {
      const params = type === "chat" && id ? `?${new URLSearchParams({ chat_id: id })}` : "";
      const changes = await api(`/api/changes${params}`, options);
      if (!current()) return;
      const libraryRevision = changes.library_revision ?? changes.revision;
      if (profileDirectoryNeeded() && profilesEnabled() && profiles && changes.profiles_revision && changes.profiles_revision !== state.profilesRevision) { await profiles.load(true); if (!current()) return; }
      if (libraryRevision !== (state.libraryWatchRevision ?? state.libraryRevision)) {
        const chats = await refreshChats(current, options);
        if (!current() || chats === null) return;
      }
      if (workspace && changes.preferences_revision != null && changes.preferences_revision !== state.preferences.revision) {
        await workspace.loadPreferences();
        if (!current()) return;
      }
      if (page === "conversations" && type === "chat" && changes.selected && changes.selected.id === id) {
        state.remoteRunning = !!changes.selected.running;
        const loading = state.chatLoading && state.chatLoading.id === id && state.chatLoading.version === version;
        if (changes.selected.revision !== state.selectedRevision && !loading && !state.sending) {
          const data = await api(`/api/chats/${encodeURIComponent(id)}`, options);
          if (!current() || state.sending) return;
          renderChat(data, false, { background: true, revisionFallback: changes.selected.revision });
        }
      } else state.remoteRunning = false;
      if (current()) {
        if (page === "my-ai") await myAI?.refresh({ quiet: true });
        if (!current()) return;
        updateComposer();
        const notice = plain(changes.preferences_notice);
        if (notice && notice !== state.preferenceNotice) toast(notice);
        state.preferenceNotice = notice;
        $("connection-state").textContent = notice ? "设置暂时未读取" : "";
      }
    } catch (error) {
      if (current() && error.name !== "AbortError") $("connection-state").textContent = "同步暂时断开";
    } finally {
      if (state.syncAbort === controller) state.syncAbort = null;
      state.syncInFlight = false;
      const immediate = state.syncRequested; state.syncRequested = false;
      schedulePoll(immediate);
    }
  }
  function historyRangeLabel(length) {
    return `当前第 ${state.historyMessageStart + 1}–${state.historyMessageStart + length} 条 / 共 ${state.historyMessageTotal} 条 · 消息全文保留`;
  }
  function scrollToHistoryMatch() {
    if (!state.historyMatch || state.historyMatch.titleOnly || state.page !== "conversations") return;
    const target = [...ui.messages.querySelectorAll(".message")].find((node) => node.dataset.messageId === state.historyMatch.messageId);
    if (!target) { toast("匹配消息暂时没有定位成功，可以继续读取原文。"); return; }
    target.focus({ preventScroll: true });
    ui.scroll.scrollTop += target.getBoundingClientRect().top - ui.scroll.getBoundingClientRect().top - 18;
  }
  async function openHistory(agent, sessionId, target = null, resumeOffset = 0, readOnly = false) {
    state.historyReadOnly = readOnly === true;
    showPage("conversations");
    closeSourceUpdate(false);
    closeSourceImages(false);
    invalidateContextPreview("已切换会话，请重新查看本轮上下文。");
    saveDraft(); stopPolling();
    const version = ++state.openVersion;
    state.selected = { type: "history", id: `${agent}:${sessionId}`, agent, sessionId };
    state.profile_snapshot = null; state.profileSelection = null; state.profileSelectionSync = null; state.profileSelectionIntent = false; state.profileLegacy = false; state.profileNotice = ""; profiles?.close(false);
    state.memoryRecallError = false;
    state.backend = state.historyBackendChoices.get(state.selected.id) || agent; state.model_id = state.historyModelChoices.get(state.selected.id) ?? defaultModelId(state.backend);
    state.historyReady = false;
    state.pendingSubmission = null;
    state.selectedRevision = null; state.remoteRunning = false; state.chatLoading = null;
    const start = target ? Math.floor(target.index / 100) * 100 : Math.floor(resumeOffset / 100) * 100;
    state.historySession = { agent, session_id: sessionId }; state.historyMessages = []; state.historyMessageNext = null;
    state.historyMessageStart = start; state.historyMessagePrev = start > 0 ? Math.max(0, start - 100) : null;
    state.historyMatch = target; state.historyPageLoading = false;
    state.chat = null; state.chatMessages = []; state.run = null; state.transientError = "";
    resetMessageView(); ui.composer.hidden = false; ui.actions.replaceChildren(); restoreDraft(); renderRunNotice();
    ui.eyebrow.textContent = `${backendLabels[agent] || agent} · 原有历史`;
    ui.title.textContent = "正在打开历史…"; ui.meta.textContent = "保留原来的文字与顺序";
    ui.messages.setAttribute("aria-live", "off"); renderList(); toggleSidebar(false);
    ui.messages.append(el("p", "history-loading", "正在读取原文…"));
    try {
      const params = new URLSearchParams({ agent, session_id: sessionId, offset: String(start), limit: "100" });
      const data = await api(`/api/history/session?${params}`);
      if (version !== state.openVersion) return;
      state.historySession = { ...data.session, agent, session_id: sessionId };
      state.historyMessages = Array.isArray(data.messages) ? data.messages : [];
      state.historyMessageTotal = Number(data.total) || state.historyMessages.length;
      state.historyMessageNext = data.next_offset == null ? null : data.next_offset;
      state.historyReady = true;
      historyHeader();
      void loadHistoryOrigins(agent, sessionId, version);
      renderMessages(state.historyMessages, { history: true });
      if (target) requestAnimationFrame(() => { if (version === state.openVersion) scrollToHistoryMatch(); });
      if (!state.historyMessages.length) ui.messages.append(el("div", "empty-list", "这段历史暂时没有可读取的文字或图片记录。"));
    } catch (error) {
      if (version === state.openVersion) { historyHeader(); showViewError(userError(error, "这段原文暂时没有读出来，请稍后再试。"), () => openHistory(agent, sessionId, target, start, readOnly)); }
    } finally { updateComposer(); checkpointBrowserState(); schedulePoll(true); }
  }
  async function loadMoreHistoryMessages(node, direction = "next") {
    const offset = direction === "previous" ? state.historyMessagePrev : state.historyMessageNext;
    if (offset == null || state.selected.type !== "history" || state.historyPageLoading) return;
    const { agent, sessionId } = state.selected;
    const version = state.openVersion;
    const oldFirstId = state.historyMessages[0] && String(state.historyMessages[0].message_id || state.historyMessages[0].id);
    const oldFirst = [...ui.messages.querySelectorAll(".message")].find((item) => item.dataset.messageId === oldFirstId);
    const oldTop = oldFirst && oldFirst.getBoundingClientRect().top;
    state.historyPageLoading = true; historyTextReadVersion++;
    node.disabled = true; node.textContent = "正在读取…";
    try {
      const params = new URLSearchParams({ agent, session_id: sessionId, offset: String(offset), limit: "100" });
      const data = await api(`/api/history/session?${params}`);
      if (version !== state.openVersion) return;
      const messages = Array.isArray(data.messages) ? data.messages : [];
      if (data.session?.coverage) state.historySession = { ...state.historySession, coverage: data.session.coverage };
      historyHeader(); updateComposer();
      if (direction === "previous") {
        state.historyMessages.unshift(...messages); state.historyMessageStart = offset;
        state.historyMessagePrev = offset > 0 ? Math.max(0, offset - 100) : null;
      } else {
        state.historyMessages.push(...messages);
        state.historyMessageNext = data.next_offset == null ? null : data.next_offset;
      }
      state.historyMessageTotal = Number(data.total) || state.historyMessages.length;
      renderMessages(state.historyMessages, { history: true });
      if (direction === "previous" && oldTop != null) {
        const sameFirst = [...ui.messages.querySelectorAll(".message")].find((item) => item.dataset.messageId === oldFirstId);
        if (sameFirst) ui.scroll.scrollTop += sameFirst.getBoundingClientRect().top - oldTop;
      }
    } catch (error) { toast(userError(error, "这些消息暂时没有读出来，请再试一次。")); node.disabled = false; node.textContent = direction === "previous" ? "读取前面的消息" : "继续读后面的消息"; }
    finally { if (version === state.openVersion) { state.historyPageLoading = false; checkpointBrowserState(); } }
  }
  async function ensureContinuation(selection) {
    const source = { agent: selection.agent, session_id: selection.sessionId };
    const sourceKey = selection.id;
    const known = state.historyContinuationIds.get(sourceKey);
    if (known) return { chat: { id: known } };
    const creationId = state.historyCreationIds.get(sourceKey) || requestId();
    state.historyCreationIds.set(sourceKey, creationId);
    checkpointBrowserState();
    const data = await api("/api/chats", { method: "POST", body: JSON.stringify({ source, client_id: creationId, reuse_source: true }) });
    state.historyContinuationIds.set(sourceKey, data.chat.id);
    state.historyCreationIds.delete(sourceKey);
    checkpointBrowserState();
    return data;
  }
  async function continueHistory() {
    // Navigation to an existing continuation never creates an import copy.
    if (state.creating || state.sending || state.selected.type !== "history") return;
    const known = state.historyContinuationIds.get(state.selected.id);
    if (!known) return;
    saveDraft();
    try { await openChat(known); }
    catch (error) { toast(userError(error, "接续对话暂时无法打开，原历史和草稿仍保留。")); }
  }
  function requestId() {
    return typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `web-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
  function submissionWasReceived(data, submission) {
    return !!(data.run && data.run.client_id === submission.clientId || Array.isArray(data.messages) && data.messages.some((message) => message.client_id === submission.clientId || message.id === submission.clientId));
  }
  async function requestReceipt(kind, clientId, chatId = null) {
    const params = new URLSearchParams({ kind, client_id: clientId });
    if (chatId) params.set("chat_id", chatId);
    try { return await api(`/api/receipts?${params}`); }
    catch (error) { if (error.status === 404) return null; throw error; }
  }
  function confirmSubmission(data, submission, version) {
    if (state.pendingSubmission === submission) state.pendingSubmission = null;
    if (state.pendingSubmissions.get(submission.chatId) === submission) state.pendingSubmissions.delete(submission.chatId);
    const changedRouteHere = version === state.openVersion && state.selected.id === submission.chatId && (state.backend !== submission.backend || state.model_id !== (submission.model_id || "") || !sameProfile(state.profile_snapshot, submission.profile_snapshot) || !sameTaskIntent(state.task_intent, submission.task_intent));
    if (!changedRouteHere && (state.drafts.get(submission.chatId) || "").trim() === submission.text && sameProfile(state.profileChoices.get(submission.chatId), submission.profile_snapshot) && sameAttachments(state.draftAttachments.get(submission.chatId), submission.attachment_ids) && sameTaskIntent(state.taskIntents.get(submission.chatId), submission.task_intent)) removeDraft(submission.chatId);
    if (version === state.openVersion && state.selected.id === submission.chatId) {
      invalidateContextPreview("消息已发送，前文已更新。下次发送前可以重新查看。");
      if (!changedRouteHere && draftKey() === submission.chatId && ui.input.value.trim() === submission.text && sameAttachments(state.attachment_ids, submission.attachment_ids)) { ui.input.value = ""; state.attachment_ids = []; state.task_intent = taskIntent(undefined); }
      resizeInput(); renderChat(data, true); schedulePoll(true);
    }
    checkpointBrowserState();
    refreshChats().catch(() => {});
  }
  function submitComposer(event) {
    event?.preventDefault();
    if (composerStopTarget) return cancelRun(composerStopTarget);
    return sendMessage();
  }
  async function homeSourceHash(draft, base, parameters) {
    const value = { text: draft.text, material_refs: draft.material_refs || [], memory: draft.memory_profile ?? null, base_profile: base, parameters };
    const canonical = JSON.stringify(value, (_key, item) => item && typeof item === "object" && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
    return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical)))].map(value => value.toString(16).padStart(2, "0")).join("");
  }
  function checkpointHomeHandoff(previous, next) {
    if (state.homeNativeHandoff !== previous) return false;
    state.homeNativeHandoff = next;
    if (checkpointBrowserState()) return true;
    state.homeNativeHandoff = previous; return false;
  }
  function homePreparation(record, workspace) {
    let active = record;
    return { creation_id: record.creation_id, created_chat_id: record.created_chat_id, base_profile: record.base_profile, parameters: record.parameters,
      base_selection: record.base_selection, local_request: record.local_request, local_result: record.local_result,
      onProgress(patch) {
        if (workspace !== state.status?.workspace_id || state.homeNativeHandoff !== active || !patch || typeof patch !== "object" || Array.isArray(patch)
            || Object.keys(patch).some(key => !["created_chat_id", "base_selection", "local_request", "local_result"].includes(key))) return false;
        try {
          const next = window.RabbitBrowserState.homeHandoff({ ...active, ...patch });
          if (next.base_selection && next.base_selection.workspace_id !== workspace || next.local_result && next.local_result.workspace_id !== workspace
              || active.created_chat_id && next.created_chat_id !== active.created_chat_id || active.base_selection && (!next.base_selection || next.base_selection.selection.revision < active.base_selection.selection.revision)
              || active.local_request && JSON.stringify(next.local_request) !== JSON.stringify(active.local_request)
              || active.local_result && JSON.stringify(next.local_result) !== JSON.stringify(active.local_result)) return false;
          if (!checkpointHomeHandoff(active, next)) return false;
          active = next; return true;
        } catch (_) { return false; }
      } };
  }
  function finishHomeHandoff(record, choice) {
    const active = state.homeNativeHandoff;
    if (!active || active.creation_id !== record.creation_id || state.homeNativeChoice !== choice || state.homeConfiguration?.draft_id !== record.draft_id
        || state.homeConfiguration.revision !== record.configuration_revision) return false;
    const previousIdentity = state.homeConfiguration, previousSnapshot = state.profile_snapshot, previousModel = state.model_id;
    state.homeConfiguration = nextHomeConfiguration(true);
    state.homeNativeChoice = homeChoice(record.base_profile, choice.memory, "codex");
    state.profile_snapshot = toProfileSnapshot(record.base_profile, choice.memory); state.model_id = record.base_profile.model_id;
    state.profileChoices.set("__new__", state.profile_snapshot);
    if (checkpointHomeHandoff(active, null)) return true;
    state.homeConfiguration = previousIdentity; state.homeNativeChoice = choice; state.profile_snapshot = previousSnapshot; state.model_id = previousModel;
    state.profileChoices.set("__new__", previousSnapshot); return false;
  }
  async function sendHomeNative() {
    const choice = homeNativeChoice();
    if (!choice || state.nativeHomeSending || !ui.input.value.trim() || !draftHasContent() || !profileReady()) return;
    let source, nextConsumed = false, nextConsumedRevision = null;
    state.nativeHomeSending = true; updateComposer();
    const current = () => state.homeNativeChoice === choice && sameNativeSendTarget(source);
    try {
      source = captureNativeDraft("new");
      const draft = await prepareNativeDraft(choice.agent, source, current);
      if (!current()) return;
      if (saveDraft() !== true) throw Error("浏览器不能保存当前草稿，文字和材料保留。");
      let preparation = null, record = state.homeNativeHandoff;
      if (choice.local_base || record) {
        const identity = state.homeConfiguration, base = choice.local_base, parameters = { model_id: choice.profile?.model_id, reasoning_effort: choice.profile?.reasoning_effort };
        const hash = base ? await homeSourceHash(draft, base, parameters) : null;
        if (!current()) return;
        if (record && (!base || record.draft_id !== identity?.draft_id || record.configuration_revision !== identity.revision || record.source_hash !== hash)) {
          const target = record.created_chat_id || await readHomeCreation(record, source.workspaceId);
          if (target && current()) await nativeChats?.selectExisting({ agent: "codex", chatId: target, workspaceId: source.workspaceId }, current);
          toast("这份新草稿与上次交接不同。原目标保留，请先核对上次交接；新稿没有发送。"); return;
        }
        if (!record) {
          record = window.RabbitBrowserState.homeHandoff({ draft_id: identity.draft_id, configuration_revision: identity.revision, creation_id: crypto.randomUUID(), created_chat_id: null,
            base_profile: base, parameters, base_selection: null, local_request: null, local_result: null, source_hash: hash });
          if (!checkpointHomeHandoff(null, record)) throw Error("浏览器不能保存创建编号，原稿保留，尚未创建。");
        }
        preparation = homePreparation(record, source.workspaceId);
      }
      // Creation and send keep their existing request journals; this submission
      // only hands the saved draft to that renderer's ordinary Send action.
      if (await nativeChats?.createNew(choice.agent, current, choice.local_base || choice.profile, draft, true, preparation, () => {
        if (!current() || state.sourceUpdateInputVersion === source.editVersion) return null;
        const following = captureNativeDraft("new");
        return { text: following.text, consume: () => { nextConsumed = clearNativeDraft(following); if (nextConsumed) nextConsumedRevision = following.editVersion; return nextConsumed; } };
      })) {
        const consumed = nextConsumed ? state.sourceUpdateInputVersion === nextConsumedRevision : clearNativeDraft(source);
        if (record && consumed && !finishHomeHandoff(record, choice)) toast("原目标已保存草稿；首页交接记录仍需核对。");
      }
      else if (current()) toast("创建结果尚未确认，请在设置的“连接与恢复”中核对；原稿保留，没有自动重发。");
    } catch (error) {
      // A known created target is the recovery destination; do not offer a
      // fresh Home creation as if the original attempt had never happened.
      if (error.createdChatId && current()) {
        try { await nativeChats?.selectExisting({ agent: choice.agent, chatId: error.createdChatId, workspaceId: source.workspaceId }, current); } catch (_) { /* The exact target remains in the original creation journal. */ }
      }
      toast(userError(error, "发送尚未完成，请核对原生对话；原草稿保留，没有自动重发。"));
    }
    finally { state.nativeHomeSending = false; updateComposer(); }
  }
  async function sendMessage(event) {
    if (event) event.preventDefault();
    if (homeNativeChoice()) return sendHomeNative();
    if (historyUsesNative()) {
      if (!draftHasContent() || !nativeAdoptionSource() || currentUploadCount() || fileInputsUnready() || state.task_intent.mode !== "chat") return;
      return continueNativeHistory();
    }
    const text = ui.input.value.trim(), attachment_ids = [...state.attachment_ids], capturedTask = taskIntent(state.task_intent);
    const oldPending = state.selected.type === "chat" && (state.pendingSubmissions.get(state.selected.id) || state.pendingSubmission);
    if (oldPending?.uncertain && !state.sending && !state.creating && !state.backendChoosing && state.page === "conversations" && (!modelReady() || profilesEnabled() && oldPending.text !== text || oldPending.backend !== state.backend || (oldPending.model_id || "") !== state.model_id || !sameProfile(oldPending.profile_snapshot, state.profile_snapshot) || !sameAttachments(oldPending.attachment_ids, attachment_ids) || !sameTaskIntent(oldPending.task_intent, capturedTask))) {
      const version = state.openVersion;
      try { const receipt = await requestReceipt("message", oldPending.clientId, oldPending.chatId); const existing = await api(`/api/chats/${encodeURIComponent(oldPending.chatId)}`);
        if (receipt?.received || submissionWasReceived(existing, oldPending)) { confirmSubmission(existing, oldPending, version); toast("已确认原组合的上次发送；当前选择和文字仍保留，没有重新发送。"); return; }
        if (!sameTaskIntent(oldPending.task_intent, capturedTask)) toast("上次发送还未确认。原文件任务和当前草稿都保留，这次只查询了回执，没有重发。");
        else if (profilesEnabled()) toast("尚未找到上次发送的回执。原配置和请求保留；这次只查询，没有把新配置写进旧请求。");
        else if (modelReady()) { oldPending.uncertain = false; oldPending.backend = state.backend; oldPending.model_id = state.model_id; checkpointBrowserState(); toast("目前没有收到上次提交的回执。选择和文字已保留；再点发送会用当前组合重试同一请求。"); }
        else toast("上次提交尚未收到。原请求已保留；再次发送前请明确选择可用组合。");
      } catch (_) { toast("上次发送回执暂时读不到，原请求、选择和文字仍保留，未重发。"); }
      return;
    }
    if (!sourceCanContinue()) { toast(sourceCoverageNotice()); return; }
    if (attachment_ids.length && modelCapabilityInfo()?.image_input === "unsupported") { toast("所选连接不支持图像输入，图片和草稿保留，请明确选择支持图像的 AI 配置。"); return; }
    if ((capturedTask.mode === "files" || capturedTask.artifact_ids.length) && !text) { toast("先写下希望 AI 对所选文件做什么。原文件选择和草稿保留。"); return; }
    if (capturedTask.mode === "files" && attachment_ids.length) { toast("当前文件任务只支持文字；图片和草稿保留，可切回普通聊天或移除图片。"); return; }
    if (state.selected.type !== "chat" && capturedTask.artifact_ids.length && !allWorkspaceSourceFiles(capturedTask)) { toast("文件成果只能在原聊天使用，原草稿已保留。"); return; }
    if (!draftHasContent(text, attachment_ids, capturedTask) || fileInputsUnready() || currentUploadCount() || state.sending || state.creating || state.backendChoosing || isRunning() || contextCannotSend() || contextPinSavingCurrent() || (state.selected.type === "history" && !state.historyReady) || state.page !== "conversations") return;
    if (capturedTask.artifact_ids.length && !fileInputsSupported()) { toast("当前入口暂不能读取文件材料，原草稿与选择保留。"); return; }
    if (capturedTask.mode === "files" && !fileTaskSupported()) { toast("所选连接当前不能执行文件任务；草稿与文件选择保留，可切回普通聊天。"); return; }
    const info = backendInfo(state.backend);
    if (!info || !info.available || !modelReady()) { toast(modelUnavailableReason()); return; }
    const backend = state.backend, model_id = state.model_id, capturedProfile = profileSnapshot(state.profile_snapshot), expectedChoice = state.profileSelectionIntent || state.profileSelection?.ai_profile_id === state.profile_snapshot?.ai_profile.id && state.profileSelection?.memory_profile_id === state.profile_snapshot?.memory_profile.id ? state.profileSelection : null;
    const previousSelection = { ...state.selected };
    const previousDraftKey = draftKey(previousSelection);
    const initialVersion = state.openVersion;
    invalidateSourceUpdate("已开始发送这一轮，请重新查看来源更新。");
    invalidateSourceImages("已开始发送这一轮，请重新查看来源图片。");
    saveDraft(); state.sending = true; state.transientError = ""; updateComposer(); renderRunNotice();
    let id = previousSelection.type === "chat" ? previousSelection.id : null;
    let submission = null, submitted = false;
    try {
      if (previousSelection.type === "new" || previousSelection.type === "history") {
        const creationId = state.newCreationId || requestId();
        if (previousSelection.type === "new") state.newCreationId = creationId;
        checkpointBrowserState();
        const created = previousSelection.type === "history" ? await ensureContinuation(previousSelection) : await api("/api/chats", { method: "POST", body: JSON.stringify({ client_id: creationId }) });
        id = created.chat.id;
        if (previousSelection.type === "new" && state.newCreationId === creationId) state.newCreationId = null;
        const targetDraft = state.drafts.get(id);
        if (previousSelection.type === "history" && draftHasContent(targetDraft || "", state.draftAttachments.get(id) || [], state.taskIntents.get(id)) && (targetDraft?.trim() !== text || !sameAttachments(state.draftAttachments.get(id), attachment_ids) || !sameTaskIntent(state.taskIntents.get(id), capturedTask))) {
          throw new Error("已有接续对话中有另一份草稿，可在“更多 → 打开接续对话”中查看；这里的文字仍保留。");
        }
        removeDraft(previousDraftKey); state.drafts.set(id, text); state.draftAttachments.set(id, [...attachment_ids]); state.taskIntents.set(id, capturedTask); if (capturedProfile) state.profileChoices.set(id, capturedProfile);
        refreshChats().catch(() => {});
        if (initialVersion === state.openVersion) {
          invalidateContextPreview("已接上这段对话，请重新查看本轮上下文。");
          state.selected = { type: "chat", id }; state.chat = created.chat;
          state.historySession = null; state.historyMessages = []; state.historyMatch = null; state.historyReady = false;
          state.chatMessages = []; state.run = null; state.selectedRevision = null; state.profileSelection = null; state.profileSelectionSync = null; adoptProfile(capturedProfile, { frozen: true });
          headerForChat(created.chat); state.messageSignature = "";
        }
        if (previousSelection.type === "history") {
          const data = await api(`/api/chats/${encodeURIComponent(id)}`);
          if (initialVersion === state.openVersion) { renderChat(data, false); state.backend = backend; state.model_id = model_id; adoptProfile(capturedProfile, { frozen: true }); updateComposer(); }
        }
        checkpointBrowserState();
      }
      const pending = state.pendingSubmissions.get(id) || state.pendingSubmission;
      submission = pending && pending.chatId === id && pending.text === text && sameProfile(pending.profile_snapshot, capturedProfile) && sameAttachments(pending.attachment_ids, attachment_ids) && sameTaskIntent(pending.task_intent, capturedTask) ? pending : { chatId: id, text, backend, model_id, profile_snapshot: capturedProfile, profile_selection_revision: null, attachment_ids, task_intent: capturedTask, clientId: requestId(), uncertain: false };
      state.pendingSubmissions.set(id, submission);
      if (initialVersion === state.openVersion) state.pendingSubmission = submission;
      checkpointBrowserState();
      if (submission.uncertain) {
        const receipt = await requestReceipt("message", submission.clientId, id);
        const existing = await api(`/api/chats/${encodeURIComponent(id)}`);
        if (receipt && receipt.received || submissionWasReceived(existing, submission)) { confirmSubmission(existing, submission, initialVersion); toast("已确认上一条消息发送成功。"); return; }
        submission.uncertain = false;
        if (!submission.profile_snapshot) { submission.backend = backend; submission.model_id = model_id; }
      }
      if (submission.profile_snapshot && submission.profile_selection_revision === null) { submission.profile_selection_revision = await prepareProfileSelection(id, submission.profile_snapshot, previousSelection.type === "chat" ? expectedChoice : null); checkpointBrowserState(); }
      const submitBody = { text, backend: submission.backend, model_id: submission.model_id || "", attachment_ids: [...(submission.attachment_ids || [])], client_id: submission.clientId, ...taskBody(submission.task_intent), ...profileBody(submission.profile_snapshot) };
      if (submission.profile_snapshot) submitBody.profile_selection_revision = submission.profile_selection_revision;
      submitted = true;
      const response = await api(`/api/chats/${encodeURIComponent(id)}/messages`, { method: "POST", body: JSON.stringify(submitBody) });
      if (state.pendingSubmission === submission) state.pendingSubmission = null;
      if (state.pendingSubmissions.get(id) === submission) state.pendingSubmissions.delete(id);
      const sameIntent = sameTaskIntent(state.task_intent, submission.task_intent) && sameProfile(state.profile_snapshot, submission.profile_snapshot) && state.backend === submission.backend && state.model_id === (submission.model_id || "");
      if ((initialVersion !== state.openVersion || sameIntent) && (state.drafts.get(id) || "").trim() === submission.text && sameAttachments(state.draftAttachments.get(id), submission.attachment_ids) && sameTaskIntent(state.taskIntents.get(id), submission.task_intent) && sameProfile(state.profileChoices.get(id), submission.profile_snapshot)) removeDraft(id);
      if (initialVersion === state.openVersion) {
        invalidateContextPreview("消息已发送，前文已更新。下次发送前可以重新查看。");
        if (sameIntent && ui.input.value.trim() === submission.text && sameAttachments(state.attachment_ids, submission.attachment_ids)) { ui.input.value = ""; state.attachment_ids = []; state.task_intent = taskIntent(undefined); } resizeInput(); state.run = response.run;
        state.chatMessages.push({ id: submission.clientId, role: "user", content: text, attachment_ids: [...attachment_ids], created_at: new Date().toISOString() });
        renderMessages(state.chatMessages, { forceBottom: true, appendNotice: sourceNotice(state.chat) }); renderRunNotice();
        try {
          const data = await api(`/api/chats/${encodeURIComponent(id)}`);
          if (initialVersion === state.openVersion) { renderChat(data, true); schedulePoll(); }
        } catch (_) {
          if (initialVersion === state.openVersion) {
            state.transientError = "消息已发送，正在重新连接回答…";
            renderRunNotice(); schedulePoll();
          }
        }
      }
      refreshChats().catch(() => {});
    } catch (error) {
      if (submission && memoryRecallRejected(error)) { if (state.pendingSubmissions.get(id) === submission) state.pendingSubmissions.delete(id); if (state.pendingSubmission === submission) state.pendingSubmission = null; submission.uncertain = false; if (initialVersion === state.openVersion) state.memoryRecallError = true; }
      else if (submission && submitted && (!error.status || error.status >= 500 || error.status === 409)) {
        submission.uncertain = true;
        try {
          const receipt = await requestReceipt("message", submission.clientId, id);
          const existing = await api(`/api/chats/${encodeURIComponent(id)}`);
          if (receipt && receipt.received || submissionWasReceived(existing, submission)) { confirmSubmission(existing, submission, initialVersion); return; }
          if (error.status === 409 && submission.profile_snapshot) { if (state.pendingSubmissions.get(id) === submission) state.pendingSubmissions.delete(id); if (state.pendingSubmission === submission) state.pendingSubmission = null; submission.uncertain = false; }
          if (error.status === 409 && initialVersion === state.openVersion) { renderChat(existing); schedulePoll(); }
        } catch (_) { /* Preserve the request ID until receipt can be checked. */ }
      }
      else if (submission && (!submitted || error.status >= 400 && error.status < 500)) { if (state.pendingSubmissions.get(id) === submission) state.pendingSubmissions.delete(id); if (state.pendingSubmission === submission) state.pendingSubmission = null; }
      if (initialVersion === state.openVersion) {
        state.transientError = memoryRecallRejected(error) ? `${userError(error, "长期记忆暂时无法检索。")} 这条消息尚未接收，原记忆选择和草稿保留。可重试或明确选择本次不使用长期记忆。` : submission && submission.uncertain ? "暂时无法确认是否已收到。文字已保留，再点发送会先检查状态。" : userError(error, "这条消息暂时没有发送成功，文字已保留，可以再试一次。");
        if (state.status && (state.status.synthetic_fixture || state.status.backends.every((item) => item.provider === "synthetic"))) {
          state.transientError = `虚构验收错误：${error.name || "Error"} ${plain(error.message).slice(0, 180)} · HTTP ${error.status || "未收到响应"}`;
        }
        if (!ui.input.value) ui.input.value = text;
        state.drafts.set(draftKey(), ui.input.value); state.draftAttachments.set(draftKey(), [...state.attachment_ids]); state.taskIntents.set(draftKey(), taskIntent(state.task_intent)); renderRunNotice(); resizeInput();
      } else { state.drafts.set(id || previousDraftKey, text); state.draftAttachments.set(id || previousDraftKey, [...attachment_ids]); state.taskIntents.set(id || previousDraftKey, capturedTask); toast(userError(error, "消息暂时没有发送成功，文字已保留在原来的聊天。")); }
    } finally { state.sending = false; updateComposer(); checkpointBrowserState(); schedulePoll(true); }
  }
  async function cancelRun(target) {
    if (!target || state.page !== "conversations" || state.selected.type !== "chat" || target.chatId !== state.selected.id
        || target.version !== state.openVersion || target.runId !== state.run?.id || typeof target.runId !== "string" || !target.runId
        || target.workspace !== (state.status?.workspace_id || null)
        || !isRunning() || state.cancelling || state.run.cancel_requested || state.run.status === "cancelling") return;
    const runId = target.runId, version = target.version, chatId = target.chatId;
    const current = () => state.page === "conversations" && state.selected.type === "chat"
      && version === state.openVersion && state.selected.id === chatId && state.run?.id === runId
      && target.workspace === (state.status?.workspace_id || null);
    state.cancelling = true; renderRunNotice();
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: "{}" });
      if (!current()) return;
      const data = await api(`/api/chats/${encodeURIComponent(chatId)}`);
      if (current()) { renderChat(data); schedulePoll(); }
      refreshChats().catch(() => {});
    } catch (error) { toast(userError(error, "停止结果暂未确认，等待自动同步后再核对。")); if (current()) schedulePoll(true); }
    finally { state.cancelling = false; if (current()) renderRunNotice(); else updateComposer(); }
  }
  function retryLastMessage() {
    const last = [...state.chatMessages].reverse().find((message) => message.role === "user");
    if (last) { invalidateContextPreview("草稿已修改，之前的预览已清除，请重新查看。"); ui.input.value = plain(last.content); state.attachment_ids = attachmentIds(last.attachment_ids); saveDraft(); resizeInput(); ui.input.focus(); }
  }
  async function updateHistory() {
    const node = $("refresh-history"); node.disabled = true; node.textContent = "更新中…";
    node.setAttribute("aria-label", "正在更新原对话");
    historySourceSerial++; historySourcePending = false;
    const scope = state.status?.workspace_id || null;
    const current = () => !scope || state.status?.workspace_id === scope;
    try {
      const result = await api("/api/history/refresh", { method: "POST", body: "{}" }, false);
      if (!result || result.ok !== true || !result.stats || typeof result.stats !== "object" || Array.isArray(result.stats)
          || Object.hasOwn(result.stats, "source_pending") && typeof result.stats.source_pending !== "boolean") throw Error("历史更新结果尚未确认。");
      if (!current()) return;
      checkHistorySources(true);
      let statusRead = true;
      try { await refreshStatus(); } catch (_) { statusRead = false; }
      if (!current()) return;
      try {
        // Keep the current filter, loaded rows, scroll position and draft while
        // the unified list is re-read. A newer view discards this response.
        const refreshed = await refreshChats(current);
        if (!current() || refreshed === null) return;
        toast(result?.stats?.source_pending === true ? "已更新读取时的记录；写入中的内容稍后可再更新。" : statusRead ? "历史记录已更新。" : "历史已更新；读取时间暂时没有刷新。");
      } catch (_) {
        if (current()) toast("历史已更新，但列表暂时没有读回来，请稍后重试。");
      }
    } catch (error) {
      if (current()) {
        const rejected = [400, 403, 409, 413].includes(error?.status);
        toast(rejected ? `历史更新未完成。${userError(error, "请稍后再试。")}` : "历史更新结果尚未确认；请稍后查看对话列表，未自动重发。");
      }
    }
    finally { node.disabled = false; updateHistorySummary(); }
  }

  $("new-chat").addEventListener("click", newChat);
  $("home-logo")?.addEventListener("click", () => { state.browserPageTouched = true; myAI?.resetView?.(); showPage("my-ai"); });
  document.addEventListener("keydown", event => { if (event.key === "Escape") closeNewChatMenu(); });
  $("close-document").addEventListener("click", closeDocument);
  document.addEventListener("keydown", (event) => {
    if ($("document-viewer").hidden) return;
    if (event.key === "Escape") { event.preventDefault(); closeDocument(); }
    if (event.key === "Tab") {
      const controls = [...$("document-viewer").querySelectorAll("button, a, [tabindex='0']")].filter((item) => !item.disabled);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  $("open-sidebar").addEventListener("click", () => toggleSidebar(true));
  $("open-markdown-chats").addEventListener("click", () => markdownChats?.open().catch(error => toast(userError(error, "文件夹暂时没有打开。"))));
  $("close-sidebar").addEventListener("click", () => toggleSidebar(false));
  ui.backdrop.addEventListener("click", () => toggleSidebar(false));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") toggleSidebar(false); });
  document.querySelectorAll(".page-nav").forEach((node) => node.addEventListener("click", () => { state.browserPageTouched = true; if (node.dataset.page === "conversations") void switchListView(); else { if (node.dataset.page === "my-ai") myAI?.resetView?.(); showPage(node.dataset.page); } }));
  sourceFilters().forEach((node) => node.addEventListener("click", () => {
    conversationGroups?.clear();
    state.browserAgentFilterTouched = true;
    state.historyAgent = node.dataset.agent;
    sourceFilters().forEach((item) => { const selected = item === node; item.classList.toggle("active", selected); item.setAttribute("aria-pressed", String(selected)); });
    loadHistoryList(true);
    checkpointBrowserState();
  }));
  ui.search.addEventListener("input", () => {
    state.browserQueryTouched = true;
    state.libraryQuery = ui.search.value;
    ++state.listVersion;
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => loadHistoryList(true), 300);
    checkpointBrowserState(true);
  });
  $("specialist-composer-label").addEventListener("click", () => showPage("specialist"));
  $("choose-ai-profile").addEventListener("click", () => profiles?.open("ai"));
  $("choose-memory-profile").addEventListener("click", () => profiles?.open("memory"));
  ui.input.addEventListener("input", () => {
    ++state.sourceUpdateInputVersion;
    invalidateSourceUpdate("草稿已修改。请重新查看来源更新；旧请求的结果不会覆盖这份文字。");
    invalidateSourceImages("草稿已修改。请重新查看来源图片；旧请求不会覆盖这份草稿。");
    state.browserInputTouched = true;
    state.browserTouchedDrafts.add(draftKey());
    invalidateContextPreview("草稿已修改，之前的预览已清除，请重新查看。");
    if (state.pendingSubmission && ui.input.value.trim() !== state.pendingSubmission.text) state.pendingSubmission = null;
    saveDraft(true); resizeInput();
  });
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229 && !event.shiftKey && (state.preferences.send_shortcut === "ctrl-enter" ? event.ctrlKey || event.metaKey : !event.ctrlKey && !event.metaKey)) {
      event.preventDefault(); sendMessage();
    }
  });
  $("composer-form").addEventListener("submit", submitComposer);
  $("toggle-file-task").addEventListener("click", () => { if (state.sending || state.creating || state.task_intent.mode !== "files" && !fileTaskSupported()) return; setTaskIntent({ mode: state.task_intent.mode === "files" ? "chat" : "files", artifact_ids: [...state.task_intent.artifact_ids] }); });
  $("add-text-file").addEventListener("click", () => { if (!quoteDraft().busy && (state.task_intent.mode !== "files" || !state.attachment_ids.length) && fileInputsSupported()) $("text-file-input").click(); });
  $("pick-text-file").addEventListener("click", () => { if (!quoteDraft().busy && (state.task_intent.mode !== "files" || !state.attachment_ids.length) && fileInputsSupported()) fileMaterials?.list(); });
  $("text-file-input").addEventListener("change", event => { uploadTextFile(event.target.files); event.target.value = ""; });
  $("add-image").addEventListener("click", () => $("image-input").click());
  $("image-input").addEventListener("change", event => { uploadImages(event.target.files); event.target.value = ""; });
  ui.input.addEventListener("paste", event => {
    const images = [...(event.clipboardData?.items || [])].filter(item => item.kind === "file" && /^image\//.test(item.type)).map(item => item.getAsFile()).filter(Boolean);
    if (images.length) { event.preventDefault(); const text = event.clipboardData?.getData("text/plain"); if (text) { const start = ui.input.selectionStart ?? ui.input.value.length, end = ui.input.selectionEnd ?? start; ui.input.value = ui.input.value.slice(0, start) + text + ui.input.value.slice(end); ui.input.dispatchEvent(new Event("input")); } uploadImages(images); }
  });
  $("quote-history").addEventListener("click", () => {
    if (!historyQuotes || !quoteDraft().active || quoteDraft().busy) return;
    closeDocument(); closeSourceUpdate(false); closeSourceImages(false); closeContextPreview();
    toast("选择原文、图片或文件，放入草稿后再发送。");
    historyQuotes.open();
  });
  $("refresh-history").addEventListener("click", updateHistory);
  ui.contextButton.addEventListener("click", previewContext);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { nativeWorkRead++; acceptNativeWorkStatus(null); saveDraft(); stopPolling(); state.syncRequested = false; if (state.syncAbort) state.syncAbort.abort(); }
    else { schedulePoll(true); checkHistorySources(); }
  });
  window.addEventListener("focus", () => checkHistorySources());
  window.addEventListener("pagehide", () => saveDraft());
  ui.scroll.addEventListener("scroll", () => checkpointBrowserState(true), { passive: true });
  window.addEventListener("resize", () => toggleSidebar(window.innerWidth <= 720 && ui.sidebar.classList.contains("is-open"), false));

  async function init() {
    toggleSidebar(false); showPage("my-ai"); updateComposer(); renderList();
    schedulePoll(true);
    const results = await Promise.allSettled([refreshStatus(), refreshChats(), workspace ? workspace.loadPreferences() : Promise.resolve()]);
    if (results[0].status === "rejected") {
      $("connection-state").textContent = "连接暂时断开";
      state.transientError = "暂时连不上这台电脑。确认助手仍在运行后，刷新页面再试。"; renderRunNotice();
    }
    if (results[0].status === "fulfilled") checkHistorySources();
    if (results[2].status === "rejected") toast("设置暂时没读出来，使用默认设置。可到设置页重试。");
    const snapshot = beginBrowserRestore();
    const freshEntry = window.performance?.getEntriesByType?.("navigation")?.[0]?.type === "navigate";
    const restored = freshEntry && !snapshot?.newCreationId && !state.memoryChatCreation
      ? restoreHomeSnapshot(snapshot) : await restoreBrowserSnapshot(snapshot);
    if (fileMaterials && state.status?.workspace_id) fileMaterials.recover().catch(() => {});
    if (projectPublish && state.status?.workspace_id) projectPublish.recover().catch(() => {});
    browserRestoreSnapshot = null;
    if (state.page === "my-ai") await myAI?.refresh();
    if (!restored && !state.browserTouchedDrafts.has(draftKey()) && ui.input.value === "") restoreDraft();
    saveDraft();
  }
  init();
})();
