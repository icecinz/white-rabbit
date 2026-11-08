(() => {
  "use strict";

  const agents = window.RabbitAgents;
  const defaults = Object.freeze({ theme: "system", density: "comfortable", send_shortcut: "enter", default_backend: "codex", default_models: agents.emptyModels(), show_internal: false, markdown: true, favorites: [] });
  const managementTabs = [["agents", "Agent"], ["models", "大模型"]];
  const settingsTabs = [["general", "通用设置"], ["native-chats", "连接与恢复"], ["specialist", "小白兔专员"], ["ai-settings", "交给 AI 的设置"], ["about", "关于"]];
  const enums = { theme: ["system", "light", "dark"], density: ["comfortable", "compact"], send_shortcut: ["enter", "ctrl-enter"], default_backend: agents.ids };
  const settingValues = Object.freeze({ theme: enums.theme, density: enums.density, send_shortcut: enums.send_shortcut, markdown: [true, false], show_internal: [true, false] });
  const sharedSettings = new Set(["theme", "density", "send_shortcut"]);
  // A small, browser-local vocabulary. User names, records and service details
  // never pass through this table; untranslated modules keep their own text.
  const languageKey = "rabbit.ui.language.v1";
  const english = Object.freeze({
    "关于": "About", "一封旧信": "An old letter", "核心能力": "Core ability", "返回": "Back", "会劈叉": "Can do the splits", "小白兔正在劈叉": "Rabbit doing the splits",
    "对话": "Chats", "新对话": "New chat", "小白兔专员": "Rabbit specialist", "管理": "Manage", "设置": "Settings", "通用设置": "General", "Agent 会话": "Agent session", "先从对话列表选择一段 Agent 对话": "Select an Agent chat from the chat list first", "专员设置尚未接入。": "Specialist settings are not connected yet.", "聊天": "Chat", "外观": "Appearance", "数据": "Data", "大模型": "Models", "AI 配置": "AI profiles", "记忆配置": "Memory profiles", "管理分类": "Management tabs", "设置分类": "Settings tabs", "管理和设置": "Management and settings", "AI 配置 · 记忆配置 · 接入连接": "AI profiles · Memory profiles · Connections", "让小白兔更适合你": "Make Rabbit work for you",
    "更多": "More", "本机 Agent": "Local Agent", "检测说明": "Status notes", "连接与恢复": "Connection and recovery", "刷新检测": "Refresh status", "正在检测…": "Checking…", "关闭详情": "Close details", "查看接入详情": "Connection details", "重试读取设置": "Retry settings",
    "接入检查通过": "Setup check passed", "接入待核对": "Setup unverified", "有通过的通路": "With a passed route", "当前可用": "Available", "当前不可用": "Unavailable", "可用": "Available", "不可用": "Unavailable", "备注": "Notes", "使用手册": "Manual", "请求验证通过": "Request verified", "可选择 · 发送时验证": "Selectable · checked on send", "本机服务可达": "Local service reachable", "状态未知": "Status unknown",
    "固定使用此模型": "Use this model", "跟随当前 Agent 默认模型": "Follow Agent default", "已跟随当前 Agent 默认模型": "Following Agent default", "新建跟随默认的配置": "Create default-follow profile", "☆ 收藏": "☆ Favorite", "★ 已收藏": "★ Favorited", "全部": "All", "收藏": "Favorites",
    "搜索模型、连接或 Agent": "Search models, connections or Agents", "搜索大模型": "Search models", "跟随 Agent": "Follow Agent",
    "新对话默认 AI 配置": "Default AI profile", "新对话默认 Agent": "Default Agent", "管理 AI 配置": "Manage AI profiles", "发送快捷键": "Send shortcut", "Enter 发送": "Enter to send", "Ctrl / ⌘ + Enter 发送": "Ctrl / ⌘ + Enter to send",
    "主题": "Theme", "跟随系统": "System", "浅色": "Light", "深色": "Dark", "布局密度": "Layout density", "舒适": "Comfortable", "紧凑": "Compact", "统一 Markdown 排版": "Render Markdown", "显示内部记录": "Show internal records", "导出当前对话": "Export current chat", "保存位置": "Storage location",
    "界面语言": "Interface language", "仅基础导航、管理和设置标签；其他界面、原文和配置名保持原样。语言只保存在当前浏览器。": "Basic navigation, management and settings labels only. Other views, original text and profile names stay unchanged. Saved in this browser only.", "当前页面已切换；浏览器未能保存，刷新后可能恢复中文。": "Changed for this page. Browser storage is unavailable; refreshing may restore Chinese.",
    "紫色条目代表 AI 可直接操作": "Purple entries can be operated directly by AI", "AI 程序接口也可修改；人可直接操作。": "Also editable through the AI program interface; you can use this control directly.", "交给 AI 的设置": "Settings for AI", "这些设置只由专员或受支持的程序接口修改；这里仅展示，不提供逐项开关。": "These settings are changed through the specialist or a supported program interface. This section is read-only.", "读取最新设置目录": "Read latest settings catalog", "正在读取 AI 设置目录…": "Reading AI settings catalog…", "AI 设置目录暂时读不到；没有增加可用接口的标记。": "The AI settings catalog is unavailable. No interface capability has been marked.", "目录尚未读取。": "The catalog has not been read yet.", "主题保留在通用设置，也可请 AI 修改。界面语言由你在当前浏览器切换。": "Theme stays in General settings and can also be changed by AI. Change the interface language yourself in this browser.",
    "当前：": "Current: ", "可以这样说：": "You can say: ", "Markdown 排版": "Markdown formatting", "原始文字": "Plain text", "显示": "Shown", "隐藏": "Hidden", "把回答显示为 Markdown 排版": "Show replies with Markdown formatting", "把回答改成原始文本显示": "Show replies as plain text", "显示已索引的协作和审批记录": "Show indexed collaboration and approval records", "隐藏已索引的协作和审批记录": "Hide indexed collaboration and approval records",
    "Markdown排版": "Markdown formatting", "是否将回答显示为Markdown；只影响显示，不修改原文。": "Show replies as Markdown. This changes the display without changing the original text.", "只显示已接入文字索引的内部记录，不扩展读取原生工具或推理正文。": "Show internal records already included in the text index. This does not add native tool or reasoning content.", "偏好保存在这台电脑；界面语言仅保存在当前浏览器。": "Preferences are saved on this computer; the interface language is saved only in this browser.",
    "Enter发送，或Ctrl/⌘+Enter发送；输入法确认不发送。": "Use Enter or Ctrl / ⌘ + Enter to send. Confirming input-method suggestions does not send a message.", "用 Enter 发送消息": "Send messages with Enter", "用 Ctrl / ⌘ + Enter 发送消息": "Send messages with Ctrl / ⌘ + Enter", "把布局改为舒适留白": "Use the comfortable layout with more space", "把布局改为紧凑排列": "Use the compact layout",
    "在 AI 配置卡片上设为默认；不会改变已经打开的对话。": "Set the default on an AI profile card. Existing chats keep their selection.", "沿用 Agent 默认模型和参数。": "Use the Agent's default model and parameters.", "输入中文时，候选词确认不会发送。": "Confirming input-method suggestions will not send a message.", "跟随系统会随着系统明暗模式切换。": "System theme follows your device's light or dark mode.", "舒适留白或紧凑排列。": "Choose more space or a compact layout.", "关闭后按原始文字显示，不修改保存的内容。": "Turn off to show plain text. Saved content stays unchanged.", "只包括已接入文字索引的子代理协作、自动审批记录；不含系统、工具和推理正文。关闭只是隐藏，不会删除。": "Includes indexed subagent and approval records, without system, tool or reasoning bodies. Turning off hides records without deleting them.", "回到“对话”，打开一段共同对话，使用右上方“导出”下载 Markdown。": "Open a shared chat in Chats and use Export to download Markdown.", "共同对话保存在本机数据库；原有记录只读接入。": "Shared chats are stored in a local database; native records are read-only.", "这些偏好会保存到本机小白兔，刷新后仍保留。": "These preferences are saved on this computer and survive refresh.",
    "小白兔已连接的工作": "Rabbit-connected work", "仅观察小白兔已连接的工作；不包括原应用中的其他工作。": "Rabbit-owned connections only; other work in the original apps is not observed.",
    "处理中": "Working", "等待确认": "Needs attention", "本连接空闲": "Connection idle", "状态待核对": "Status unknown", "尚未连接": "Not connected", "正在连接": "Connecting", "连接异常": "Connection error", "连接已关闭": "Connection closed", "连接中的对话": "Connected chat", "部分工作状态未知": "Some work states are unknown", "暂无已连接的工作": "No connected work",
    "HAPI报告的工作": "HAPI-reported work", "Hub 查询时间": "Hub query time", "非原生实时状态": "Not native real-time status", "Hub 活跃": "Hub active", "Hub 思考中": "Hub thinking", "Hub 等待确认": "Hub needs attention", "Hub 非活跃": "Hub inactive", "未命名工作": "Unnamed work", "Agent 未报告": "Agent unreported", "Hub 未列出工作": "No work reported by Hub", "HAPI状态待核对": "HAPI status unknown", "HAPI尚未核对，点“刷新检测”读取。": "HAPI is unverified. Use Refresh status to query it.", "只显示 Hub 报告；不证明原生执行者实时状态，也不能在此发送或审批。": "Hub reports only; native executor state is unverified. No sending or approval here.",
    "接入说明": "Connection guide", "尚未配置；请按手动指南完成原生安装与连接检测。": "Not configured. Follow the manual guide to install the native Agent and check its connection.", "打开手动接入指南；原生安装、凭据和检测完成后才可接入。": "Open the manual setup guide. Native installation, credentials and connection checks are required.", "配置指导暂时打不开；现有连接保持原样。": "Setup guidance could not be opened. Existing connections stay unchanged.", "目录名称匹配，不代表已验证实际模型相同；每条连接沿用各自的状态。": "Catalog names match; this does not verify that the actual models are identical. Each connection keeps its own status.", "本机发现的模型。展开只查看，能否使用在发送时确认。": "Models found on this computer. Expand to view; availability is checked when you send.", "目录暂未报告模型。": "No models reported by the catalog yet."
  });
  let interfaceLanguage = "zh-CN";
  try { if (window.localStorage && window.localStorage.getItem(languageKey) === "en") interfaceLanguage = "en"; } catch (_) { /* Browser storage can be unavailable inside a preview. */ }
  const language = () => interfaceLanguage;
  const translate = text => interfaceLanguage === "en" && Object.hasOwn(english, text) ? english[text] : text;
  function validModelId(id, backend) { return agents.validModelId(id, backend); }
  function normalized(input, base = defaults) {
    const value = { ...base, favorites: [...base.favorites], revision: base.revision || "0", chat_backends: { ...base.chat_backends }, chat_models: { ...base.chat_models }, default_models: { ...agents.emptyModels(), ...base.default_models } };
    for (const [key, allowed] of Object.entries(enums)) if (allowed.includes(input && input[key])) value[key] = input[key];
    for (const key of ["show_internal", "markdown"]) if (input && typeof input[key] === "boolean") value[key] = input[key];
    if (input && Array.isArray(input.favorites)) value.favorites = [...new Set(input.favorites.filter((id) => !!id && agents.validModelId(id)))].slice(0, 64);
    if (input && typeof input.revision === "string" && /^(?:0|[a-f0-9]{32})$/.test(input.revision)) value.revision = input.revision;
    if (input && input.chat_backends && typeof input.chat_backends === "object" && !Array.isArray(input.chat_backends)) value.chat_backends = Object.fromEntries(Object.entries(input.chat_backends).filter(([id, backend]) => /^[A-Za-z0-9_.-]{1,128}$/.test(id) && agents.has(backend)).slice(0, 512));
    if (input && input.default_models && typeof input.default_models === "object" && !Array.isArray(input.default_models)) for (const backend of agents.ids) if (validModelId(input.default_models[backend], backend)) value.default_models[backend] = input.default_models[backend];
    if (input && input.chat_models && typeof input.chat_models === "object" && !Array.isArray(input.chat_models)) value.chat_models = Object.fromEntries(Object.entries(input.chat_models).filter(([id, model]) => /^[A-Za-z0-9_.-]{1,128}$/.test(id) && validModelId(model, value.chat_backends[id])).slice(0, 512));
    return value;
  }
  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }
  function action(label, callback, className = "rbw-button") {
    const button = node("button", className, translate(label));
    button.type = "button";
    button.addEventListener("click", callback);
    return button;
  }
  function readableError(error, fallback) {
    const text = String(error && error.message || "");
    return /[\u4e00-\u9fff]/.test(text) && text.length < 200 ? text : fallback;
  }
  function settingsCatalogue(value, workspace = "") {
    const record = object => object && typeof object === "object" && !Array.isArray(object);
    const text = (input, limit) => typeof input === "string" && input.length <= limit && !/[\x00-\x1f\x7f\ud800-\udfff]/u.test(input);
    const keys = Object.keys(settingValues), directory = value?.catalogue;
    if (!record(value) || !text(value.workspace_id, 128) || !value.workspace_id || workspace && value.workspace_id !== workspace || value.scope !== "application_preferences" || value.inference_performed !== false || typeof value.revision !== "string" || !/^(?:0|[a-f0-9]{32})$/.test(value.revision) || !record(value.settings) || Object.keys(value.settings).length !== keys.length || !record(directory) || directory.schema_version !== 1 || directory.read_tool !== "rabbit_settings_read" || directory.update_tool !== "rabbit_settings_update" || !Array.isArray(directory.fields) || directory.fields.length !== keys.length) throw Error("AI 设置目录没有读完整，请重新读取。");
    const seen = new Set();
    const fields = directory.fields.map(field => {
      const key = field?.key, values = settingValues[key];
      if (!values || seen.has(key) || !text(field.label, 80) || !field.label || !text(field.description, 600) || field.type !== (typeof values[0] === "boolean" ? "boolean" : "string") || field.surface !== (sharedSettings.has(key) ? "human_and_ai" : "ai_only") || !Array.isArray(field.allowed_values) || field.allowed_values.length !== values.length || new Set(field.allowed_values).size !== values.length || field.allowed_values.some(item => !values.includes(item)) || !values.includes(field.default) || !Object.hasOwn(value.settings, key) || !values.includes(value.settings[key]) || !record(field.example) || field.example.expected_revision !== "0" || !record(field.example.patch) || Object.keys(field.example.patch).length !== 1 || !values.includes(field.example.patch[key])) throw Error("AI 设置目录字段没有读完整，请重新读取。");
      seen.add(key);
      return { key, label: field.label, description: field.description, allowed_values: [...field.allowed_values], surface: field.surface, example: { expected_revision: "0", patch: { [key]: field.example.patch[key] } } };
    });
    return { workspace_id: value.workspace_id, revision: value.revision, settings: Object.fromEntries(keys.map(key => [key, value.settings[key]])), fields, read_tool: directory.read_tool, update_tool: directory.update_tool };
  }
  function aiControl(control) {
    control.dataset.controlInterface = "ai";
    control.setAttribute("title", translate("AI 程序接口也可修改；人可直接操作。"));
    return control;
  }

  function create({ root, api, getWorkspace = () => "", getSelection, getOverview = () => null, onOpenWork = null, profiles = null, profilesEnabled = () => false, workStatus = null, onNavigate = null, onSectionChange = () => {}, renderSpecialistSettings = null, renderNativeSettings = null, onPreferences = () => {}, onManagement = () => {}, onLanguageChange = () => {}, onAddResource = null, onManual = null, onBackToMyAI = null }) {
    if (!root || typeof api !== "function") throw new Error("小白兔管理界面缺少入口或接口。");
    let page = "management", tab = "agents", visible = false;
    let prefs = normalized({}), confirmed = normalized({}), prefsReady = false, prefsLoading = false, prefsLoadVersion = 0, mutationVersion = 0;
    let saving = false, saveError = "", preferenceMessage = "", pending = new Map(), keyVersions = new Map(), metadataVersion = 0;
    let management = null, managementLoading = false, managementError = "", managementVersion = 0, managementPromise = null, managementWorkspace = "", managementEvidence = null, managementOverview = null;
    let modelFilter = "all", modelQuery = "", focusedObject = null;
    let statusNode = null, cardsNode = null;
    let languageSaved = true, resourceMessage = "", resourceBusy = false;
    let settings = null, settingsLoading = false, settingsError = "", settingsVersion = 0;
    const settingsFieldVersions = new Map();
    const pageTabs = () => profilesEnabled() && profiles ? [["ai-profiles", "AI 配置"], ["memory-profiles", "记忆配置"], ["agents", "Agent"], ["models", "大模型"]] : managementTabs;
    function getNavigation(nextPage = page) { return (nextPage === "settings" ? settingsTabs : pageTabs()).map(([id, label]) => ({ id, label: translate(label) })); }
    function getSection() { return { page, tab }; }
    // Manual navigation uses a stable topic, never a project-document path.
    function manualTopic() { return page === "settings" ? tab === "native-chats" ? "native" : tab === "specialist" ? "specialist" : "settings" : "ai"; }
    const media = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;

    function applyAppearance() {
      const html = document.documentElement;
      html.dataset.theme = prefs.theme === "system" ? (media && media.matches ? "dark" : "light") : prefs.theme;
      html.dataset.themePreference = prefs.theme;
      html.dataset.density = prefs.density;
    }
    function emitPreferences() { applyAppearance(); onPreferences(preferences()); }
    function preferences() { return { ...prefs, favorites: [...prefs.favorites], chat_backends: { ...prefs.chat_backends }, chat_models: { ...prefs.chat_models }, default_models: { ...prefs.default_models } }; }
    function observeSettings(value) {
      const keys = Object.keys(settingValues).filter(key => value && Object.hasOwn(value, key));
      const changed = keys.some(key => settingValues[key].includes(value[key]) && value[key] !== confirmed[key])
        || typeof value?.revision === "string" && value.revision !== confirmed.revision && /^(?:0|[a-f0-9]{32})$/.test(value.revision);
      for (const key of keys) settingsFieldVersions.set(key, (settingsFieldVersions.get(key) || 0)+1);
      const cancelled = changed ? [...pending.keys()].filter(key => Object.hasOwn(settingValues, key)) : [];
      for (const key of cancelled) pending.delete(key);
      if (cancelled.length) saveError = "设置版本已更新，尚未发送的选择已取消；请重新选择，没有覆盖新值。";
      return cancelled;
    }
    function applyPreferences(value) {
      mutationVersion++;
      metadataVersion++;
      const cancelled = observeSettings(value);
      confirmed = normalized(value, confirmed);
      prefs = normalized(value, prefs);
      for (const key of cancelled) prefs[key] = confirmed[key];
      for (const [key, item] of pending) prefs[key] = item.value;
      prefsReady = true;
      emitPreferences();
      if (visible) render();
      return preferences();
    }
    async function loadSettings({ apply = false } = {}) {
      const version = ++settingsVersion, scope = String(getWorkspace() || ""), metadata = metadataVersion;
      settingsLoading = true; settingsError = ""; if (visible) render();
      try {
        const received = settingsCatalogue(await api("/api/management/settings"), scope);
        if (version !== settingsVersion || scope !== String(getWorkspace() || "")) return;
        settings = received;
        if (apply && prefsReady && metadata === metadataVersion && !saving && !pending.size) {
          saveError = "";
          applyPreferences({ ...received.settings, revision: received.revision });
        }
      } catch (error) {
        if (version === settingsVersion && scope === String(getWorkspace() || "")) { settings = null; settingsError = readableError(error, "AI 设置目录暂时读不到；没有增加可用接口的标记。"); }
      } finally { if (version === settingsVersion) { settingsLoading = false; if (visible) render(); } }
    }
    if (media && typeof media.addEventListener === "function") media.addEventListener("change", () => { if (prefs.theme === "system") applyAppearance(); });
    else if (media && typeof media.addListener === "function") media.addListener(() => { if (prefs.theme === "system") applyAppearance(); });
    applyAppearance();
    onLanguageChange(language());

    async function loadPreferences() {
      const version = ++prefsLoadVersion, mutation = mutationVersion;
      prefsLoading = true;
      preferenceMessage = "正在读取设置…";
      if (visible) render();
      try {
        const response = await api("/api/preferences");
        if (version !== prefsLoadVersion || mutation !== mutationVersion) return preferences();
        saveError = "";
        observeSettings(response);
        confirmed = normalized(response);
        prefs = normalized(response);
        for (const [key, item] of pending) prefs[key] = item.value;
        metadataVersion++;
        prefsReady = true;
        preferenceMessage = "设置已从这台电脑读取。";
        emitPreferences();
      } catch (error) {
        if (version === prefsLoadVersion && mutation === mutationVersion) {
          saveError = readableError(error, "设置暂时读不到，请重试。现有聊天不会改变。");
          preferenceMessage = "";
        }
      } finally {
        if (version === prefsLoadVersion) {
          prefsLoading = false; if (visible) render();
          if (visible && page === "settings" && !settings && !settingsLoading) loadSettings();
        }
      }
      return preferences();
    }
    function setPreference(key, value) {
      if (!prefsReady || !Object.hasOwn(defaults, key)) return;
      if (Object.hasOwn(settingValues, key) && (!settings || !sharedSettings.has(key))) return;
      const checked = normalized({ [key]: value }, prefs)[key];
      const version = ++mutationVersion;
      keyVersions.set(key, version);
      pending.set(key, { value: checked, version });
      prefs = { ...prefs, [key]: checked };
      saveError = "";
      preferenceMessage = "正在保存…";
      emitPreferences();
      updateStatus();
      flushPreferences();
    }
    async function flushPreferences() {
      if (saving) return;
      saving = true;
      while (pending.size) {
        const managed = Object.hasOwn(settingValues, pending.keys().next().value);
        const batch = new Map([...pending].filter(([key]) => Object.hasOwn(settingValues, key) === managed));
        const metadata = metadataVersion;
        const externalVersions = new Map(settingsFieldVersions);
        for (const key of batch.keys()) pending.delete(key);
        const patch = Object.fromEntries([...batch].map(([key, item]) => [key, item.value]));
        try {
          const response = await api(managed ? "/api/management/settings" : "/api/preferences", { method: "POST", body: JSON.stringify(managed ? { expected_revision: confirmed.revision, patch } : { patch }) }, !managed);
          const projection = managed ? settingsCatalogue(response, String(getWorkspace() || "")) : null;
          const received = normalized(managed ? { ...projection.settings, revision: projection.revision } : response, confirmed);
          if (projection) settings = projection;
          if (metadata !== metadataVersion) { received.revision = confirmed.revision; received.chat_backends = { ...confirmed.chat_backends }; received.chat_models = { ...confirmed.chat_models }; if (!batch.has("default_models")) received.default_models = { ...confirmed.default_models }; if (managed) for (const key of Object.keys(settingValues)) if (settingsFieldVersions.get(key) !== externalVersions.get(key)) received[key] = confirmed[key]; }
          confirmed = received;
          const next = normalized(confirmed);
          for (const [key, version] of keyVersions) {
            const saved = batch.get(key);
            if (!saved || saved.version !== version) next[key] = prefs[key];
          }
          prefs = next;
          preferenceMessage = pending.size ? "正在保存下一项…" : "";
        } catch (error) {
          if (managed) {
            for (const key of Object.keys(settingValues)) pending.delete(key);
            const current = error.status === 409 && error.code === "settings_version_conflict" && error.current_settings;
            let recovered = null;
            try { if (current) recovered = settingsCatalogue(current, String(getWorkspace() || "")); } catch (_) { /* Keep unknown metadata blocked. */ }
            if (recovered) {
              settings = recovered;
              if (metadata === metadataVersion) {
                confirmed = normalized({ ...recovered.settings, revision: recovered.revision }, confirmed);
                prefs = normalized({ ...recovered.settings, revision: recovered.revision }, prefs);
              }
              saveError = "设置版本有冲突，请重新选择；没有覆盖或重试。";
            } else {
              for (const key of Object.keys(settingValues)) prefs[key] = confirmed[key];
              settings = null;
              saveError = "这次设置是否保存尚未确认，请重新读取设置目录后再选择；没有自动重复保存。";
              settingsError = saveError;
            }
          }
          for (const [key, item] of batch) if (keyVersions.get(key) === item.version) prefs[key] = confirmed[key];
          if (!managed) saveError = readableError(error, "这次设置没有保存成功，已恢复原值；可以重新选择。");
          preferenceMessage = "";
        }
        emitPreferences();
        if (visible) render();
      }
      saving = false;
      updateStatus();
    }
    function updateStatus() {
      if (!statusNode) return;
      const showPreferenceNotice = (page === "settings" || /保存/.test(preferenceMessage)) && preferenceMessage !== "设置已从这台电脑读取。";
      const preferenceNotice = saveError || (showPreferenceNotice ? preferenceMessage : "");
      const messages = [preferenceNotice, managementError, page === "management" ? resourceMessage : ""].filter(Boolean);
      statusNode.textContent = messages.map(translate).join(" ");
      statusNode.hidden = !messages.length;
      statusNode.classList.toggle("rbw-error", Boolean(saveError || managementError || page === "management" && resourceMessage));
    }
    function syncManagementWorkspace() {
      const scope = String(getWorkspace() || "");
      if (managementWorkspace !== scope) {
        managementWorkspace = scope; managementVersion++; management = null; managementEvidence = null; managementOverview = null;
        managementLoading = false; managementPromise = null; managementError = ""; resourceMessage = ""; focusedObject = null;
        workStatus?.setAgentFilter?.(null);
      }
      return scope;
    }
    async function loadManagement({ refresh = false } = {}) {
      const scope = syncManagementWorkspace();
      const version = ++managementVersion;
      managementLoading = true;
      managementError = "";
      if (visible) render();
      const operation = (async () => {
        try {
          const value = await api(refresh ? "/api/management?refresh=1" : "/api/management/my-ai");
          if (version !== managementVersion || scope !== String(getWorkspace() || "")) return;
          if (!refresh && (!value || value.workspace_id !== scope || value.scope !== "saved_and_cached_overview" || value.connection_checked !== false || value.memory_read !== false || value.inference_performed !== false)) throw Error("已有目录不属于当前工作区，请重新读取。");
          const response = refresh ? value : value.management;
          if (!refresh) managementOverview = window.RabbitMyAI?.project(value, scope) || null;
          if (!response || !Array.isArray(response.agents) || !Array.isArray(response.models) || response.workspace_id && response.workspace_id !== scope) throw Error("已有目录没有读完整。");
          management = { ...response, agents: Array.isArray(response.agents) ? response.agents : [], models: Array.isArray(response.models) ? response.models : [] };
          managementEvidence = refresh ? { source: "本次主动检测", completed_at: new Date().toISOString() } : { source: "已有目录缓存", age: value.cache?.agent_checked_age_seconds };
          onManagement(management);
        } catch (error) {
          if (version === managementVersion) managementError = readableError(error, "连接状态暂时读不到，请重新检测。已有聊天和草稿仍保留。");
        } finally {
          if (version === managementVersion) { managementLoading = false; managementPromise = null; if (visible) render(); }
        }
      })();
      managementPromise = operation;
      return operation;
    }
    function refreshDetection() {
      const external = visible && page === "management" && tab === "agents" ? workStatus?.refreshExternal?.() : null;
      return Promise.allSettled([loadManagement({ refresh: true }), external]);
    }
    function status(label, available) {
      const item = node("span", available === true ? "rbw-state rbw-online" : "rbw-state");
      item.append(node("span", "rbw-dot"), node("span", "", translate(label)));
      return item;
    }
    function card(title, subtitle, available, label, lines) {
      const article = node("article", "rbw-card");
      article.dataset.controlInterface = "ai";
      article.append(node("h3", "", title));
      if (subtitle) article.append(node("p", "rbw-muted rbw-small", subtitle));
      article.append(status(label, available));
      for (const line of lines.filter(Boolean)) article.append(node("p", "rbw-card-meta", line));
      return article;
    }
    function foldedDetails(label, contents, className = "rbw-more") {
      const box = node("details", className);
      box.dataset.controlInterface = "ai";
      box.append(node("summary", "", translate(label)));
      const body = node("div", "rbw-more-body");
      body.append(...contents); box.append(body);
      return box;
    }
    function selection() {
      const current = typeof getSelection === "function" ? getSelection() || {} : {};
      const agent = management && management.agents.find((value) => value.id === current.backend);
      return { ...current, agent };
    }
    function catalogModels() {
      const native = Object.values(management?.native_profile_capabilities || {}).flatMap(row => row?.catalogue_available === true && Array.isArray(row.models) ? row.models : []);
      return [...new Map([...native, ...(management?.models || [])].filter(model => model && typeof model.id === "string").map(model => [model.id, model])).values()];
    }
    function modelConnections(model) {
      // A shared display name/provider is not a verified common model identity.
      return catalogModels().filter(candidate => candidate.id === model.id);
    }
    function catalogDetails(rows, { collapsed = true } = {}) {
      const box = node(collapsed ? "details" : "section", "rbw-catalog");
      box.dataset.controlInterface = "ai";
      const title = language() === "en" ? `Models (${rows.length})` : `模型（${rows.length}）`;
      box.append(node(collapsed ? "summary" : "h4", "", title));
      const list = node("ul", "rbw-catalog-list");
      for (const model of rows) {
        const item = node("li", "rbw-catalog-item");
        const label = model.name || model.model || "模型";
        item.append(node("strong", "", label));
        item.append(status(model.available === true ? "接入检查通过" : "接入待核对", model.available === true));
        list.append(item);
      }
      if (rows.length) box.append(list);
      else box.append(node("p", "rbw-muted rbw-small", translate("目录暂未报告模型。")));
      return box;
    }
    async function addResource(kind, target = null) {
      if (resourceBusy) return;
      if (typeof onAddResource !== "function") { resourceMessage = "尚未配置；请按手动指南完成原生安装与连接检测。"; updateStatus(); return; }
      resourceBusy = true; resourceMessage = ""; render();
      try { if (await onAddResource(kind, target) === false) resourceMessage = "配置指导暂时打不开；现有连接保持原样。"; }
      catch (_) { resourceMessage = "配置指导暂时打不开；现有连接保持原样。"; }
      finally { resourceBusy = false; if (visible) render(); }
    }
    function addResourceButton(kind) {
      const captured = focusedObject, scope = String(getWorkspace() || "");
      const button = action("接入说明", () => {
        if (!visible || page !== "management" || scope !== String(getWorkspace() || "") || focusedObject !== captured) return;
        if (kind === "agent" && captured?.kind === "agent") {
          focusedObject = { ...captured, section: "connection" };
          onSectionChange(getSection());
          render();
          return;
        }
        return addResource(kind, captured);
      }, "rbw-button rbw-subtle");
      button.setAttribute("title", translate("打开手动接入指南；原生安装、凭据和检测完成后才可接入。"));
      button.disabled = resourceBusy;
      return button;
    }
    function agentsPage(container) {
      if (focusedObject?.kind === "agent") { agentDetail(container); return; }
      const grid = node("div", "rbw-grid"), current = selection();
      for (const agent of management.agents) {
        if (focusedObject?.kind === "agent" && focusedObject.id !== agent.id) continue;
        const item = card(agent.name || agent.id, translate("本机 Agent"), agent.available === true, agent.available === true ? "接入检查通过" : "接入待核对", [`默认模型：${agent.model || "未报告"}`, agent.available !== true ? agent.reason || "接入条件尚未满足" : ""]);
        item.dataset.agentId = agent.id;
        item.dataset.selected = String(current.backend === agent.id);
        item.append(foldedDetails("备注", [
          ...(agent.provider ? [node("p", "rbw-card-meta", `连接：${agent.provider}`)] : []),
          catalogDetails(catalogModels().filter(model => model.agent_id === agent.id), { collapsed: false })
        ])); grid.append(item);
      }
      container.append(grid);
    }
    function overview() {
      try { const value = getOverview() || managementOverview; return value?.workspace_id === String(getWorkspace() || "") ? value : null; }
      catch (_) { return null; }
    }
    function getObjectTitle() {
      if (managementWorkspace !== String(getWorkspace() || "") || !focusedObject) return "";
      if (focusedObject.kind === "agent" && focusedObject.section === "connection") return "接入说明";
      const row = focusedObject.kind === "agent" ? management?.agents.find(row => row.id === focusedObject.id)
        : focusedObject.kind === "model" ? catalogModels().find(row => row.id === focusedObject.id) : profiles?.getMemory?.(focusedObject.id);
      return row?.name || row?.model || "";
    }
    function agentDetail(container) {
      const target = focusedObject, identity = target.id, scope = managementWorkspace;
      const source = management?.agents.find(row => row.id === identity);
      const isCurrent = () => visible && page === "management" && tab === "agents" && scope === String(getWorkspace() || "") && focusedObject === target;
      if (target.section === "connection") {
        const name = source?.name || agents.labels[identity];
        const detail = node("section", "rbw-agent-detail");
        detail.dataset.agentId = identity;
        const card = node("div", "rbw-card rbw-agent-connection");
        card.append(node("h3", "", `${name} 接入`));
        if (identity === "codex") {
          card.append(node("p", "", "在 Codex 原应用确认本人账号已登录，再返回小白兔。"));
          if (target.backTo === "usage") card.append(node("p", "", "回到额度与用量后，点击更新读取账户额度。"));
        } else if (identity === "claude") {
          card.append(node("p", "", "在 Claude Code 核对登录与当前提供商配置。使用 CC Switch 时沿用当前选定的提供商。"));
        } else {
          card.append(node("p", "", `在 ${name} 的原入口核对当前安装、账号和提供商设置。`));
        }
        const controls = node("div", "rbw-card-actions");
        if (source) controls.append(action(`← ${name} 详情`, () => {
          if (!isCurrent()) return;
          focusedObject = { ...target, section: undefined };
          onSectionChange(getSection());
          render();
        }, "rbw-button rbw-subtle"));
        if (typeof onManual === "function") controls.append(action("接入手册", () => {
          if (isCurrent()) onManual("connect-agent");
        }, "rbw-button rbw-subtle"));
        card.append(controls);
        detail.append(card);
        container.append(detail);
        return;
      }
      if (!source) { container.append(node("p", "rbw-empty", "这项 Agent 已不在当前目录，请返回所有 Agent。")); return; }
      const cached = overview(), summary = cached?.agents.find(row => row.id === identity);
      const recorded = managementOverview?.agents.find(row => row.id === identity)?.recorded_model || source.model || summary?.recorded_model;
      const recent = managementOverview || cached;
      const detail = node("section", "rbw-agent-detail"), hero = node("div", "rbw-agent-hero"), copy = node("div", "rbw-agent-copy");
      detail.dataset.agentId = identity;
      hero.append(node("span", "rbw-agent-initial", agents.initials[identity] || source.name?.slice(0, 1) || "AI"));
      copy.append(node("h3", "", source.name || identity));
      const label = summary?.status_label || ((source.checked === true || managementEvidence?.completed_at) && !source.available ? "连接待处理"
        : source.available === true && (source.checked === true || managementEvidence?.completed_at) ? "接入检查通过"
        : source.native_enabled === true || source.has_saved_configuration === true ? "有连接设置 · 待核对" : "待核对");
      const badge = status(label, summary?.status === "running"); badge.dataset.state = summary?.status || "unverified"; copy.append(badge);
      copy.append(node("p", "rbw-card-meta", "查看已发现的模型、准确关联的工作和接入说明。")); hero.append(copy); detail.append(hero);
      const models = node("section", "rbw-agent-section");
      const recordedModel = node("div", "rbw-card rbw-agent-recorded-model");
      recordedModel.append(node("h3", "", "记录中的模型"), node("p", recorded ? "" : "rbw-muted rbw-small", recorded || "尚未记录模型。"));
      models.append(recordedModel);
      const modelList = node("div", "rbw-agent-models");
      for (const model of catalogModels().filter(row => row.agent_id === identity)) {
        const id = model.id, control = action(model.name || model.model || "模型", async () => {
          if (!isCurrent()) return;
          try { await openObject({ kind: "model", id }); }
          catch (error) { if (scope === String(getWorkspace() || "")) { resourceMessage = readableError(error, "模型详情暂未打开，原工作保留。"); render(); } }
        }, "rbw-button rbw-agent-model");
        control.dataset.modelId = id; control.dataset.controlInterface = "ai";
        control.append(node("span", "rbw-card-meta", "已发现 · 通过 " + (source.name || identity))); modelList.append(control);
      }
      if (modelList.children.length) models.append(foldedDetails("已发现的模型", [modelList]));
      detail.append(models);
      const work = node("section", "rbw-agent-section"), workHeading = node("div", "rbw-agent-section-heading");
      workHeading.append(node("h3", "", "工作"), node("p", "rbw-muted rbw-small", "只列能准确对应的记录")); work.append(workHeading);
      if (workStatus) work.append(workStatus.root);
      const rows = node("ul", "rbw-agent-work-list");
      for (const item of recent?.works || []) {
        if (item.agent !== identity) continue;
        const row = node("li", "rbw-agent-work"), description = node("div", "rbw-agent-work-copy"); row.dataset.workKey = item.key;
        description.append(node("strong", "", item.name), node("p", "rbw-card-meta", [item.status_label, item.detail].filter(Boolean).join(" · ")));
        const controls = node("div", "rbw-agent-work-actions");
        for (const value of item.actions || []) {
          const captured = JSON.parse(JSON.stringify(value));
          if (captured.kind === "original" && /^codex:\/\/threads\/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(captured.url || "")) {
            const link = node("a", "rbw-agent-work-link", captured.label); link.href = captured.url; link.dataset.controlInterface = "human";
            link.addEventListener("click", event => { if (!isCurrent()) event.preventDefault(); }); controls.append(link);
          } else if (["read", "continue"].includes(captured.kind) && typeof onOpenWork === "function") {
            const control = action(captured.label, async () => {
              if (!isCurrent()) return;
              try { await onOpenWork(captured, isCurrent); }
              catch (error) { if (isCurrent()) { resourceMessage = readableError(error, "原工作暂未打开，原稿保留。"); render(); } }
            }, "rbw-button rbw-subtle"); controls.append(control);
          }
        }
        row.append(description, controls); rows.append(row);
      }
      if (rows.children.length) work.append(foldedDetails("相关工作记录", [rows]));
      else work.append(node("p", "rbw-muted rbw-small", "已有索引暂未列出这位 Agent 的工作。"));
      detail.append(work);
      const evidence = [node("p", "rbw-card-meta", "来源：本机已保存连接、已有模型目录与对话索引。"), node("p", "rbw-card-meta", source.check_scope || "目录名称和接入设置不证明模型调用成功。")];
      if (managementEvidence?.completed_at) evidence.push(node("p", "rbw-card-meta", "本次主动检测读回：" + managementEvidence.completed_at));
      else evidence.push(node("p", "rbw-card-meta", Number.isFinite(managementEvidence?.age) ? `最近连接检查：距缓存读取约 ${Math.floor(managementEvidence.age)} 秒。` : "最近连接核对时间：未记录；本次打开没有发起核对。"));
      if (source.reason) evidence.push(node("p", "rbw-card-meta", source.reason));
      const nativeCatalogue = management.native_profile_capabilities?.[identity];
      if (nativeCatalogue?.observed_at) evidence.push(node("p", "rbw-card-meta", "原生模型目录核对：" + nativeCatalogue.observed_at));
      detail.append(foldedDetails("来源与核对记录", evidence, "rbw-more rbw-agent-evidence")); container.append(detail);
    }
    function modelMatches(model) {
      const query = modelQuery.trim().toLocaleLowerCase();
      const searchable = [model.name, model.model, model.connection, model.provider, model.agent_id].filter(Boolean).join(" ").toLocaleLowerCase();
      return !query || searchable.includes(query);
    }
    function renderModels() {
      if (!cardsNode) return;
      cardsNode.replaceChildren();
      const seen = new Set();
      for (const model of catalogModels()) {
        if (focusedObject?.kind === "model" && focusedObject.id !== model.id) continue;
        if (seen.has(model.id)) continue;
        const connections = modelConnections(model);
        for (const connection of connections) seen.add(connection.id);
        const available = connections.some(connection => connection.available === true);
        if (!connections.some(modelMatches) || modelFilter === "available" && !available) continue;
        const passed = connections.filter(connection => connection.available === true).length;
        const label = language() === "en" ? `${passed}/${connections.length} routes passed setup checks` : `${passed}/${connections.length} 条通路接入检查通过`;
        const item = card(model.name || model.model || "模型", "", passed === connections.length, label, []);
        item.dataset.modelId = model.id;
        const list = node("ul", "rbw-model-agents"), agentRoutes = new Map();
        for (const connection of connections) {
          const agent = management.agents.find(value => value.id === connection.agent_id);
          const name = agent?.name || agents.labels[connection.agent_id] || connection.agent_id;
          const counts = agentRoutes.get(connection.agent_id) || { name, passed: 0, total: 0 };
          counts.total++; if (connection.available === true) counts.passed++;
          agentRoutes.set(connection.agent_id, counts);
        }
        for (const { name, passed, total } of agentRoutes.values()) {
          const label = total === 1 ? translate(passed ? "接入检查通过" : "接入待核对") : language() === "en" ? `${passed}/${total} routes passed setup checks` : `${passed}/${total} 条通路接入检查通过`;
          list.append(node("li", "", `${name} · ${label}`));
        }
        item.append(list);
        const notes = [...new Set(connections.filter(connection => connection.available !== true && typeof connection.reason === "string" && connection.reason.trim()).map(connection => `${management.agents.find(agent => agent.id === connection.agent_id)?.name || agents.labels[connection.agent_id] || connection.agent_id}：${Array.from(connection.reason.trim()).slice(0, 160).join("")}`))];
        if (notes.length) item.append(foldedDetails("备注", notes.map(note => node("p", "rbw-card-meta", note))));
        cardsNode.append(item);
      }
      if (!cardsNode.children.length) cardsNode.append(node("p", "rbw-empty", "没有与当前筛选匹配的模型。"));
    }
    function modelsPage(container) {
      const toolbar = node("div", "rbw-toolbar"), search = node("input", "rbw-search");
      search.type = "search"; search.placeholder = translate("搜索模型、连接或 Agent"); search.value = modelQuery; search.setAttribute("aria-label", translate("搜索大模型"));
      search.addEventListener("input", () => { modelQuery = search.value; renderModels(); });
      const filters = node("div", "rbw-filters");
      for (const [id, label] of [["all", "全部"], ["available", "有通过的通路"]]) {
        const control = action(label, () => { modelFilter = id; for (const child of filters.children) child.setAttribute("aria-pressed", String(child.dataset.filter === id)); renderModels(); }, "rbw-button rbw-subtle");
        control.dataset.filter = id; control.setAttribute("aria-pressed", String(modelFilter === id)); filters.append(control);
      }
      const actions = node("div", "rbw-action-group"); actions.append(filters, addResourceButton("model"));
      toolbar.append(search, actions); container.append(toolbar);
      cardsNode = node("div", "rbw-grid"); container.append(cardsNode); renderModels();
    }
    function settingRow(title, description, control) {
      const supported = control?.dataset.controlInterface === "ai";
      const row = node("div", supported ? "rbw-setting rbw-ai-setting" : "rbw-setting");
      if (supported) row.dataset.controlInterface = "ai";
      const copy = node("div", "rbw-setting-copy"); copy.append(node("label", "", translate(title)));
      if (description) copy.append(node("p", "rbw-muted rbw-small", translate(description)));
      if (control && control.id) copy.children[0].setAttribute("for", control.id);
      row.append(copy); if (control) row.append(control);
      return row;
    }
    function selectSetting(key, values) {
      const control = node("select", "rbw-select"); control.id = `rbw-${key}`;
      for (const [value, label] of values) { const option = node("option", "", translate(label)); option.value = value; control.append(option); }
      const managed = sharedSettings.has(key);
      control.value = prefs[key]; control.disabled = !prefsReady || managed && !settings;
      if (managed && settings) aiControl(control);
      control.addEventListener("change", () => setPreference(key, control.value));
      return control;
    }
    function languageSetting() {
      const control = node("select", "rbw-select"); control.id = "rbw-language";
      for (const [value, label] of [["zh-CN", "中文"], ["en", "English"]]) { const option = node("option", "", label); option.value = value; control.append(option); }
      control.value = language();
      control.addEventListener("change", () => {
        if (!["zh-CN", "en"].includes(control.value)) return;
        interfaceLanguage = control.value;
        languageSaved = true;
        try {
          if (!window.localStorage) throw new Error("Browser storage unavailable");
          window.localStorage.setItem(languageKey, interfaceLanguage);
        } catch (_) { languageSaved = false; }
        onLanguageChange(language());
        if (visible) render();
      });
      return control;
    }
    function aiSettingsDirectory() {
      const details = node("section", "rbw-ai-settings"); details.dataset.controlInterface = "ai";
      if (settings) {
        const list = node("div", "rbw-ai-settings-list");
        const entries = [
          ["density", { comfortable: "舒适", compact: "紧凑" }],
          ["markdown", { false: "原始文字", true: "Markdown 排版" }],
          ["show_internal", { false: "隐藏", true: "显示" }],
        ];
        for (const [key, labels] of entries) {
          const field = settings.fields.find(value => value.key === key);
          const item = node("section", "rbw-ai-setting"); item.dataset.settingKey = field.key; item.dataset.controlInterface = "ai";
          const current = prefsReady ? prefs[field.key] : settings.settings[field.key];
          item.append(node("h3", "", translate(field.label)), node("p", "rbw-small", translate("当前：") + translate(labels[current]))); list.append(item);
        }
        details.append(list);
      } else details.append(node("p", "rbw-muted rbw-small", translate(settingsLoading ? "正在读取 AI 设置目录…" : settingsError || "目录尚未读取。")));
      const refresh = action(settingsLoading ? "正在读取 AI 设置目录…" : "读取最新设置目录", () => loadSettings({ apply: true }), "rbw-button rbw-subtle"); refresh.disabled = settingsLoading; refresh.dataset.controlInterface = "ai"; details.append(refresh);
      return details;
    }
    let oldLetter, aboutAbilityButton, aboutCapability = false;
    function aboutPage(container) {
      const card = node("div", "rbw-about-card");
      const profile = node("div", "rbw-about-profile");
      const icon = node("img", "rbw-about-icon");
      icon.src = "/rabbit-app-1024.png";
      icon.alt = "";
      icon.width = icon.height = 96;
      const copy = node("div", "rbw-about-copy");
      const birthYear = 2026, birthMonth = 10, birthDay = 7;
      const today = new Date();
      const currentDay = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
      const birth = Date.UTC(birthYear, birthMonth - 1, birthDay);
      const age = (Math.max(0, currentDay - birth) / (365.2425 * 86_400_000)).toFixed(2);
      const bio = node("p", "rbw-about-bio", `${birthYear} 年 ${birthMonth} 月 ${birthDay} 日生兔 · 现 `);
      bio.append(node("strong", "rbw-about-age", `${age} 岁`));
      copy.append(node("h3", "", "小白兔"), bio);
      profile.append(icon, copy);
      const ability = action("核心能力", () => {
        if (!visible || page !== "settings" || tab !== "about" || aboutAbilityButton !== ability) return;
        aboutCapability = true;
        render();
      }, "rbw-button rbw-about-ability");
      aboutAbilityButton = ability;
      card.append(profile, ability);
      container.append(card);
      if (!oldLetter) {
        oldLetter = node("details", "rbw-more rbw-letter");
        const body = node("article", "rbw-letter-body");
        oldLetter.append(node("summary", "", translate("一封旧信")), body);
        let reading = false, loaded = false;
        oldLetter.addEventListener("toggle", async () => {
          if (!oldLetter.open || reading || loaded) return;
          reading = true;
          try {
            const response = await fetch("/old-letter.md", { mode: "same-origin" });
            if (!response.ok) throw Error();
            const text = (await response.text()).replace(/^# 一封旧信\r?\n\s*/, "");
            if (!text.trim()) throw Error();
            body.replaceChildren();
            if (window.OneAIRichText?.render) window.OneAIRichText.render(body, text);
            else body.append(node("pre", "document-lines", text));
            loaded = true;
          } catch (_) {
            body.textContent = "旧信暂时无法读取。收起后重新展开即可重试。";
          } finally { reading = false; }
        });
      }
      oldLetter.children[0].textContent = translate("一封旧信");
      container.append(oldLetter);
    }
    function renderAboutCapability() {
      if (root.dataset.aboutView === "ability") return;
      root.dataset.aboutView = "ability";
      statusNode = null;
      const view = node("div", "rbw-page rbw-settings-page rbw-about-capability");
      const back = action("返回", () => {
        aboutCapability = false;
        render();
        aboutAbilityButton?.focus();
      }, "rbw-button rbw-about-back");
      const figure = node("figure", "rbw-about-figure");
      const image = node("img", "rbw-about-split");
      image.src = "/rabbit-split.png";
      image.alt = translate("小白兔正在劈叉");
      image.width = image.height = 1254;
      figure.append(image, node("figcaption", "", translate("会劈叉")));
      view.append(back, figure);
      root.replaceChildren(view);
      root.scrollTop = 0;
      back.focus();
    }
    function settingsPage(container) {
      if (tab === "about") { aboutPage(container); return; }
      if (tab === "ai-settings") { container.append(aiSettingsDirectory()); return; }
      if (tab === "native-chats") {
        if (typeof renderNativeSettings === "function") renderNativeSettings(container);
        else container.append(node("p", "rbw-empty", translate("先从对话列表选择一段 Agent 对话")));
        return;
      }
      if (tab === "specialist") {
        if (typeof renderSpecialistSettings === "function") renderSpecialistSettings(container);
        else container.append(node("p", "rbw-empty", translate("专员设置尚未接入。")));
        return;
      }
      const box = node("div", "rbw-settings-box");
      box.append(
        settingRow("主题", "", selectSetting("theme", [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]])),
        settingRow("发送快捷键", "", selectSetting("send_shortcut", [["enter", "Enter 发送"], ["ctrl-enter", "Ctrl / ⌘ + Enter 发送"]])),
        settingRow("界面语言", languageSaved ? "" : "当前页面已切换；浏览器未能保存，刷新后可能恢复中文。", languageSetting())
      );
      container.append(box);
    }
    function render() {
      if (!visible) return;
      syncManagementWorkspace();
      cardsNode = null;
      root.setAttribute("lang", language());
      if (aboutCapability) { renderAboutCapability(); return; }
      delete root.dataset.aboutView;
      const restoreAboutFocus = aboutAbilityButton && document.activeElement === aboutAbilityButton;
      aboutAbilityButton = null;
      const wrapper = node("div", page === "settings" ? "rbw-page rbw-settings-page" : "rbw-page");
      const heading = node("div", "rbw-toolbar rbw-action-group");
      const target = focusedObject, connection = target?.kind === "agent" && target.section === "connection";
      if (page === "management" && target?.fromMyAI && typeof onBackToMyAI === "function") {
        heading.append(action(target.backTo === "usage" ? "← 额度与用量" : "← 我的 AI", () => {
          if (visible && focusedObject === target && managementWorkspace === String(getWorkspace() || "")) onBackToMyAI();
        }, "rbw-button rbw-subtle"));
      }
      if (page === "management" && focusedObject) heading.append(action(focusedObject.kind === "agent" ? "所有 Agent" : "查看完整目录", () => { focusedObject = null; workStatus?.setAgentFilter?.(null); onSectionChange(getSection()); render(); }, "rbw-button rbw-subtle"));
      const profilePage = page === "management" && profilesEnabled() && profiles && ["ai-profiles", "memory-profiles"].includes(tab);
      if (page === "management" && !profilePage && !connection) { const refresh = action(managementLoading ? "正在检测…" : "刷新检测", refreshDetection, "rbw-button rbw-subtle"); refresh.dataset.controlInterface = "ai"; refresh.disabled = managementLoading; heading.append(refresh); }
      if (!profilePage && !connection && typeof onManual === "function") heading.append(aiControl(action("使用手册", () => onManual(manualTopic()), "rbw-button rbw-subtle")));
      if (page === "management" && tab === "agents" && !connection) heading.append(addResourceButton("agent"));
      if (heading.children.length) wrapper.append(heading);
      statusNode = node("p", "rbw-feedback"); if (page === "settings") statusNode.dataset.controlInterface = "ai"; statusNode.setAttribute("role", "status"); statusNode.setAttribute("aria-live", "polite"); wrapper.append(statusNode); updateStatus();
      if (!prefsReady && !prefsLoading && saveError) wrapper.append(action("重试读取设置", () => loadPreferences(), "rbw-button rbw-subtle"));
      const body = node("div", "rbw-body");
      if (page === "management" && tab === "agents" && focusedObject?.kind !== "agent" && workStatus) {
        body.append(workStatus.root);
      } else if (!(page === "management" && tab === "agents") || connection) workStatus?.hide();
      if (page === "management") {
        if (profilesEnabled() && profiles && ["ai-profiles", "memory-profiles"].includes(tab)) profiles.renderManagement(body, tab === "memory-profiles" ? "memory" : "ai");
        else if (connection) agentDetail(body);
        else if (!management) body.append(node("p", "rbw-empty", managementLoading ? "正在读取当前电脑的连接状态…" : "连接状态尚未读取。"));
        else {
          if (tab === "agents") agentsPage(body);
          else modelsPage(body);
        }
      } else settingsPage(body);
      wrapper.append(body); root.replaceChildren(wrapper);
      if (restoreAboutFocus) aboutAbilityButton?.focus();
      if (page === "management" && tab === "agents" && !connection) workStatus?.show();
    }
    function show(nextPage, nextTab) {
      syncManagementWorkspace();
      page = nextPage === "settings" ? "settings" : "management";
      const navigation = getNavigation(page);
      const section = navigation.some(({ id }) => id === nextTab) ? nextTab : navigation[0].id;
      if (tab !== section) focusedObject = null;
      tab = section;
      if (page !== "settings" || tab !== "about") aboutCapability = false;
      workStatus?.setAgentFilter?.(focusedObject?.kind === "agent" ? focusedObject.id : null);
      if (settings && getWorkspace() && settings.workspace_id !== String(getWorkspace())) { settings = null; settingsLoading = false; settingsVersion++; }
      visible = true; root.hidden = false; render();
      onSectionChange(getSection());
      if (!prefsReady && !prefsLoading) loadPreferences();
      else if (page === "settings" && prefsReady && !settings && !settingsLoading) loadSettings();
      if (page === "management" && !["ai-profiles", "memory-profiles"].includes(tab) && !management && !managementLoading) loadManagement();
    }
    function hide() {
      visible = false;
      root.hidden = true;
      aboutCapability = false;
      delete root.dataset.aboutView;
      workStatus?.hide();
    }
    function selectChanged() { if (visible) render(); }
    async function openObject(target) {
      if (!target || !["agent", "model", "memory"].includes(target.kind) || typeof target.id !== "string" || !target.id || target.id.length > 512) throw Error("管理对象编号无效。");
      const { kind, id, section: agentSection, fromMyAI, backTo } = target;
      target = { kind, id };
      if (kind === "agent" && agents.has(id)) {
        if (agentSection === "connection") target.section = "connection";
        if (fromMyAI === true) {
          target.fromMyAI = true;
          if (backTo === "usage") target.backTo = "usage";
        }
      }
      const scope = syncManagementWorkspace(), section = target.kind === "agent" ? "agents" : target.kind === "model" ? "models" : "memory-profiles";
      show("management", section); focusedObject = target; workStatus?.setAgentFilter?.(target.kind === "agent" ? target.id : null); render();
      if (target.kind === "memory") {
        if (!profilesEnabled() || !profiles) throw Error("记忆配置管理暂不可用。");
        await profiles.load();
        if (!visible || page !== "management" || tab !== section || String(getWorkspace()) !== scope || focusedObject !== target) return false;
        const profile = profiles.getMemory(target.id); if (!profile || profile.id === "none") throw Error("这项记忆配置已变化，请重新读取目录。");
        profiles.editNamed("memory", profile.id, profile.revision);
      } else {
        if (managementLoading) await managementPromise;
        else if (!management || target.kind === "agent" && target.fromMyAI && !target.section) await loadManagement();
        if (!visible || page !== "management" || tab !== section || String(getWorkspace()) !== scope || focusedObject !== target) return false;
        const rows = target.kind === "agent" ? management?.agents : catalogModels();
        if (target.section !== "connection" && !rows?.some(row => row.id === target.id)) throw Error("这项管理对象已不在当前目录；没有按名称猜选。");
        workStatus?.setAgentFilter?.(target.kind === "agent" ? target.id : null); render();
      }
      onSectionChange(getSection()); return true;
    }
    return { show, hide, getNavigation, getSection, getObject: () => managementWorkspace === String(getWorkspace() || "") && focusedObject ? { ...focusedObject } : null, getObjectTitle, openObject, preferences, loadPreferences, applyPreferences, selectChanged };
  }
  window.RabbitWorkspace = { create, translate, language, settingsCatalogue };
})();
