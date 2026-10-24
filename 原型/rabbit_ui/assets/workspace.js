(() => {
  "use strict";

  const agents = window.RabbitAgents;
  const defaults = Object.freeze({ theme: "system", density: "comfortable", send_shortcut: "enter", default_backend: "codex", default_models: agents.emptyModels(), show_internal: false, markdown: true, favorites: [] });
  const managementTabs = [["devices", "设备"], ["agents", "Agent"], ["models", "大模型"]];
  const settingsTabs = [["general", "通用设置"], ["native-chats", "Agent 会话"], ["specialist", "小白兔专员"], ["ai-settings", "交给 AI 的设置"]];
  const enums = { theme: ["system", "light", "dark"], density: ["comfortable", "compact"], send_shortcut: ["enter", "ctrl-enter"], default_backend: agents.ids };
  const settingValues = Object.freeze({ theme: enums.theme, density: enums.density, send_shortcut: enums.send_shortcut, markdown: [true, false], show_internal: [true, false] });
  const sharedSettings = new Set(["theme", "density", "send_shortcut"]);
  // A small, browser-local vocabulary. User names, records and service details
  // never pass through this table; untranslated modules keep their own text.
  const languageKey = "rabbit.ui.language.v1";
  const english = Object.freeze({
    "对话": "Chats", "新对话": "New chat", "小白兔专员": "Rabbit specialist", "管理": "Manage", "设置": "Settings", "通用设置": "General", "Agent 会话": "Agent session", "先从对话列表选择一段 Agent 对话": "Select an Agent chat from the chat list first", "专员设置尚未接入。": "Specialist settings are not connected yet.", "聊天": "Chat", "外观": "Appearance", "数据": "Data", "设备": "Devices", "大模型": "Models", "AI 配置": "AI profiles", "记忆配置": "Memory profiles", "管理分类": "Management tabs", "设置分类": "Settings tabs", "管理和设置": "Management and settings", "AI 配置 · 记忆配置 · 接入连接": "AI profiles · Memory profiles · Connections", "让小白兔更适合你": "Make Rabbit work for you",
    "更多": "More", "本机 Agent": "Local Agent", "检测说明": "Status notes", "连接与手机入口": "Connection and phone access", "刷新检测": "Refresh status", "正在检测…": "Checking…", "关闭详情": "Close details", "查看设备详情": "Device details", "查看接入详情": "Connection details", "重试读取设置": "Retry settings",
    "当前可用": "Available", "当前不可用": "Unavailable", "请求验证通过": "Request verified", "可选择 · 发送时验证": "Selectable · checked on send", "本机服务在线": "Local service online", "状态未知": "Status unknown",
    "用于下一条回复": "Use for next reply", "下一条回复已选": "Selected for next reply", "用它新建 AI 配置": "Create AI profile", "固定使用此模型": "Use this model", "跟随当前 Agent 默认模型": "Follow Agent default", "已跟随当前 Agent 默认模型": "Following Agent default", "新建跟随默认的配置": "Create default-follow profile", "☆ 收藏": "☆ Favorite", "★ 已收藏": "★ Favorited", "全部": "All", "收藏": "Favorites",
    "搜索模型、连接或 Agent": "Search models, connections or Agents", "搜索大模型": "Search models", "跟随 Agent": "Follow Agent", "手机入口": "Phone access", "打开 IM.codes": "Open IM.codes", "如何打开正式聊天": "How to open your chats",
    "新对话默认 AI 配置": "Default AI profile", "新对话默认 Agent": "Default Agent", "管理 AI 配置": "Manage AI profiles", "发送快捷键": "Send shortcut", "Enter 发送": "Enter to send", "Ctrl / ⌘ + Enter 发送": "Ctrl / ⌘ + Enter to send",
    "主题": "Theme", "跟随系统": "System", "浅色": "Light", "深色": "Dark", "布局密度": "Layout density", "舒适": "Comfortable", "紧凑": "Compact", "统一 Markdown 排版": "Render Markdown", "显示内部记录": "Show internal records", "导出当前对话": "Export current chat", "保存位置": "Storage location",
    "界面语言": "Interface language", "仅基础导航、管理和设置标签；其他界面、原文和配置名保持原样。语言只保存在当前浏览器。": "Basic navigation, management and settings labels only. Other views, original text and profile names stay unchanged. Saved in this browser only.", "当前页面已切换；浏览器未能保存，刷新后可能恢复中文。": "Changed for this page. Browser storage is unavailable; refreshing may restore Chinese.",
    "紫色条目代表 AI 可直接操作": "Purple entries can be operated directly by AI", "AI 程序接口也可修改；人可直接操作。": "Also editable through the AI program interface; you can use this control directly.", "交给 AI 的设置": "Settings for AI", "这些设置只由专员或受支持的程序接口修改；这里仅展示，不提供逐项开关。": "These settings are changed through the specialist or a supported program interface. This section is read-only.", "读取最新设置目录": "Read latest settings catalog", "正在读取 AI 设置目录…": "Reading AI settings catalog…", "AI 设置目录暂时读不到；没有增加可用接口的标记。": "The AI settings catalog is unavailable. No interface capability has been marked.", "目录尚未读取。": "The catalog has not been read yet.", "主题保留在通用设置，也可请 AI 修改。界面语言由你在当前浏览器切换。": "Theme stays in General settings and can also be changed by AI. Change the interface language yourself in this browser.",
    "当前：": "Current: ", "可以这样说：": "You can say: ", "Markdown 排版": "Markdown formatting", "原始文字": "Plain text", "显示": "Shown", "隐藏": "Hidden", "把回答显示为 Markdown 排版": "Show replies with Markdown formatting", "把回答改成原始文本显示": "Show replies as plain text", "显示已索引的协作和审批记录": "Show indexed collaboration and approval records", "隐藏已索引的协作和审批记录": "Hide indexed collaboration and approval records",
    "Markdown排版": "Markdown formatting", "是否将回答显示为Markdown；只影响显示，不修改原文。": "Show replies as Markdown. This changes the display without changing the original text.", "只显示已接入文字索引的内部记录，不扩展读取原生工具或推理正文。": "Show internal records already included in the text index. This does not add native tool or reasoning content.", "偏好保存在这台电脑；界面语言仅保存在当前浏览器。": "Preferences are saved on this computer; the interface language is saved only in this browser.",
    "Enter发送，或Ctrl/⌘+Enter发送；输入法确认不发送。": "Use Enter or Ctrl / ⌘ + Enter to send. Confirming input-method suggestions does not send a message.", "用 Enter 发送消息": "Send messages with Enter", "用 Ctrl / ⌘ + Enter 发送消息": "Send messages with Ctrl / ⌘ + Enter", "把布局改为舒适留白": "Use the comfortable layout with more space", "把布局改为紧凑排列": "Use the compact layout",
    "在 AI 配置卡片上设为默认；不会改变已经打开的对话。": "Set the default on an AI profile card. Existing chats keep their selection.", "沿用 Agent 默认模型和参数。": "Use the Agent's default model and parameters.", "输入中文时，候选词确认不会发送。": "Confirming input-method suggestions will not send a message.", "跟随系统会随着系统明暗模式切换。": "System theme follows your device's light or dark mode.", "舒适留白或紧凑排列。": "Choose more space or a compact layout.", "关闭后按原始文字显示，不修改保存的内容。": "Turn off to show plain text. Saved content stays unchanged.", "只包括已接入文字索引的子代理协作、自动审批记录；不含系统、工具和推理正文。关闭只是隐藏，不会删除。": "Includes indexed subagent and approval records, without system, tool or reasoning bodies. Turning off hides records without deleting them.", "回到“对话”，打开一段共同对话，使用右上方“导出”下载 Markdown。": "Open a shared chat in Chats and use Export to download Markdown.", "共同对话保存在本机数据库；原有记录只读接入。": "Shared chats are stored in a local database; native records are read-only.", "这些偏好会保存到本机小白兔，刷新后仍保留。": "These preferences are saved on this computer and survive refresh.",
    "＋ 添加 Agent": "＋ Add Agent", "＋ 添加模型": "＋ Add model", "尚未配置；请按手动指南完成原生安装与连接检测。": "Not configured. Follow the manual guide to install the native Agent and check its connection.", "打开手动接入指南；原生安装、凭据和检测完成后才可接入。": "Open the manual setup guide. Native installation, credentials and connection checks are required.", "配置指导暂时打不开；现有连接保持原样。": "Setup guidance could not be opened. Existing connections stay unchanged.", "目录名称匹配，不代表已验证实际模型相同；每条连接沿用各自的状态。": "Catalog names match; this does not verify that the actual models are identical. Each connection keeps its own status.", "本机发现的模型。展开只查看，能否使用在发送时确认。": "Models found on this computer. Expand to view; availability is checked when you send.", "目录暂未报告模型。": "No models reported by the catalog yet."
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

  function create({ root, api, getWorkspace = () => "", getSelection, selectBackend, selectModel, profiles = null, profilesEnabled = () => false, onNavigate = null, onSectionChange = () => {}, renderSpecialistSettings = null, renderNativeSettings = null, onPreferences = () => {}, onManagement = () => {}, onLanguageChange = () => {}, onAddResource = null }) {
    if (!root || typeof api !== "function") throw new Error("小白兔管理界面缺少入口或接口。");
    let page = "management", tab = "devices", visible = false;
    let prefs = normalized({}), confirmed = normalized({}), prefsReady = false, prefsLoading = false, prefsLoadVersion = 0, mutationVersion = 0;
    let saving = false, saveError = "", preferenceMessage = "", pending = new Map(), keyVersions = new Map(), metadataVersion = 0;
    let management = null, managementLoading = false, managementError = "", managementVersion = 0, managementPromise = null;
    let modelFilter = "all", modelQuery = "", selectedBusy = false, selectionMessage = "", detail = null;
    let statusNode = null, cardsNode = null;
    let languageSaved = true, resourceMessage = "", resourceBusy = false;
    let settings = null, settingsLoading = false, settingsError = "", settingsVersion = 0, settingsDetailsOpen = true;
    const settingsFieldVersions = new Map();
    const pageTabs = () => profilesEnabled() && profiles ? [["ai-profiles", "AI 配置"], ["memory-profiles", "记忆配置"], ["agents", "Agent"], ["models", "大模型"], ["devices", "设备"]] : managementTabs;
    function getNavigation(nextPage = page) { return (nextPage === "settings" ? settingsTabs : pageTabs()).map(([id, label]) => ({ id, label: translate(label) })); }
    function getSection() { return { page, tab }; }
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
          preferenceMessage = pending.size ? "正在保存下一项…" : "已保存到这台电脑。";
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
      const messages = [preferenceNotice, selectionMessage, managementError, page === "management" ? resourceMessage : ""].filter(Boolean);
      statusNode.textContent = messages.map(translate).join(" ");
      statusNode.classList.toggle("rbw-error", Boolean(saveError || managementError || page === "management" && resourceMessage));
    }
    async function loadManagement() {
      const version = ++managementVersion;
      managementLoading = true;
      managementError = "";
      if (visible) render();
      const operation = (async () => {
        try {
          const response = await api("/api/management?refresh=1");
          if (version !== managementVersion) return;
          management = { ...response, agents: Array.isArray(response.agents) ? response.agents : [], models: Array.isArray(response.models) ? response.models : [] };
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
    async function chooseAgent(id, label) {
      if (selectedBusy) return;
      selectedBusy = true;
      selectionMessage = "正在切换下一条回复…";
      render();
      try {
        const selected = await selectBackend(id);
        selectionMessage = selected === false ? "没有完成切换，当前组合保留。" : `下一条回复使用 ${label || id}，模型跟随这位 Agent。`;
      } catch (error) { selectionMessage = readableError(error, "没有完成切换，当前组合和草稿保留。"); }
      finally { selectedBusy = false; if (visible) render(); }
    }
    async function chooseModel(model) {
      if (selectedBusy || typeof selectModel !== "function") return;
      selectedBusy = true; selectionMessage = "正在选择下一轮模型…"; render();
      try { const changed = await selectModel(model.id, model.agent_id); selectionMessage = changed === false ? "没有完成选择，原组合和草稿保留。" : `下一条回复使用 ${model.name || model.model || model.id} · ${agents.labels[model.agent_id] || model.agent_id}。`; }
      catch (error) { selectionMessage = readableError(error, "没有完成选择，原组合和草稿保留。"); }
      finally { selectedBusy = false; if (visible) render(); }
    }
    function status(label, available) {
      const item = node("span", `rbw-state ${available ? "rbw-online" : "rbw-unavailable"}`);
      item.append(node("span", "rbw-dot"), node("span", "", translate(label)));
      return item;
    }
    function card(title, subtitle, available, label, lines) {
      const article = node("article", "rbw-card");
      article.dataset.controlInterface = "ai";
      article.append(node("h3", "", title), node("p", "rbw-muted rbw-small", subtitle), status(label, available));
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
      const chosenModel = management && management.models.find(model => model.id === current.model_id && model.agent_id === current.backend);
      return { ...current, agent, chosenModel };
    }
    function routeSummary() {
      const current = selection();
      const box = node("div", "rbw-route");
      box.append(node("span", "rbw-muted rbw-small", current.chatId || current.chatTitle ? `本对话 · ${current.chatTitle || "当前对话"} · 下一条回复` : "新对话 · 下一条回复"));
      const device = management && management.device;
      box.append(node("strong", "", `${device && device.name || "这台电脑"} / ${current.agent && current.agent.name || (agents.labels[current.backend] || current.backend)} / ${current.model_id ? current.chosenModel && (current.chosenModel.model || current.chosenModel.name) || current.model_id.slice((agents.instanceId(current.backend) + "/").length) : `跟随默认 · ${current.agent && current.agent.model || "Agent 配置"}`}`));
      return box;
    }
    function detailPanel(heading, entries) {
      const panel = node("section", "rbw-detail");
      panel.dataset.controlInterface = "ai";
      const head = node("div", "rbw-title-row");
      head.append(node("h3", "", heading), action("关闭详情", () => { detail = null; render(); }, "rbw-button rbw-subtle"));
      panel.append(head);
      const list = node("dl", "rbw-facts");
      for (const [label, value] of entries) list.append(node("dt", "", translate(label)), node("dd", "", value || "未报告"));
      panel.append(list);
      return panel;
    }
    function catalogModels() {
      return [...new Map((management && management.models || []).filter(model => model && typeof model.id === "string").map(model => [model.id, model])).values()];
    }
    function modelConnections(model) {
      const directory = catalogModels();
      if (typeof model.provider !== "string" || !model.provider || typeof model.model !== "string" || !model.model) return directory.filter(candidate => candidate.id === model.id);
      return directory.filter(candidate => candidate.id === model.id || candidate.provider === model.provider && candidate.model === model.model);
    }
    function catalogDetails(kind, rows, { collapsed = true } = {}) {
      const box = node(collapsed ? "details" : "section", "rbw-catalog");
      box.dataset.controlInterface = "ai";
      const title = kind === "models" ? language() === "en" ? `Catalog models (${rows.length})` : `目录模型（${rows.length}）` : language() === "en" ? `Catalog Agent connections (${rows.length})` : `目录 Agent 连接（${rows.length}）`;
      box.append(node(collapsed ? "summary" : "h4", "", title), node("p", "rbw-muted rbw-small", translate(kind === "models" ? "本机发现的模型。展开只查看，能否使用在发送时确认。" : "目录名称匹配，不代表已验证实际模型相同；每条连接沿用各自的状态。")));
      const list = node("ul", "rbw-catalog-list");
      for (const model of rows) {
        const item = node("li", "rbw-catalog-item");
        const agent = management.agents.find(candidate => candidate.id === model.agent_id);
        const label = kind === "models" ? model.name || model.model || model.id : agent && agent.name || agents.labels[model.agent_id] || model.agent_id;
        item.append(node("strong", "", label));
        if (kind === "connections") item.append(node("span", "rbw-muted rbw-small", `${model.model || model.name || model.id} · ${model.provider || model.connection_label || "Agent"}`));
        item.append(status(model.status === "verified" ? "请求验证通过" : model.available === true ? "可选择 · 发送时验证" : "当前不可用", model.available === true));
        list.append(item);
      }
      if (rows.length) box.append(list);
      else box.append(node("p", "rbw-muted rbw-small", translate("目录暂未报告模型。")));
      return box;
    }
    async function addResource(kind) {
      if (resourceBusy) return;
      if (typeof onAddResource !== "function") { resourceMessage = "尚未配置；请按手动指南完成原生安装与连接检测。"; updateStatus(); return; }
      resourceBusy = true; resourceMessage = ""; render();
      try { if (await onAddResource(kind) === false) resourceMessage = "配置指导暂时打不开；现有连接保持原样。"; }
      catch (_) { resourceMessage = "配置指导暂时打不开；现有连接保持原样。"; }
      finally { resourceBusy = false; if (visible) render(); }
    }
    function addResourceButton(kind) {
      const button = action(kind === "agent" ? "＋ 添加 Agent" : "＋ 添加模型", () => addResource(kind), "rbw-button rbw-subtle");
      button.setAttribute("title", translate("打开手动接入指南；原生安装、凭据和检测完成后才可接入。"));
      button.disabled = resourceBusy;
      return button;
    }
    function devicesPage(container) {
      container.append(node("p", "rbw-description", "AI 在这台电脑上运行。手机和其他浏览器可打开同一个入口，其他电脑尚未接入。"));
      const device = management && management.device;
      if (!device) return;
      const grid = node("div", "rbw-grid");
      const configured = management.agents.filter((agent) => agent.available).map((agent) => agent.name).join("、");
      const item = card(device.name || "这台电脑", `${device.platform === "Darwin" ? "macOS" : device.platform || "系统未报告"} · 执行设备`, device.online === true, device.online === true ? "本机服务在线" : "状态未知", [configured ? `当前接入：${configured}` : "当前没有报告可用 Agent"]);
      item.append(foldedDetails("连接与手机入口", [action("查看设备详情", () => { detail = detail === "device" ? null : "device"; render(); }, "rbw-button rbw-subtle"), phoneEntry()]));
      grid.append(item); container.append(grid);
      if (detail === "device") container.append(detailPanel("本机设备详情", [["设备", device.name], ["系统", device.platform === "Darwin" ? "macOS" : device.platform], ["用途", "执行设备"], ["连接", device.online === true ? "当前请求已到达本机服务" : "未确认"], ["Agent", configured || "没有报告可用 Agent"], ["远程切换与唤醒", "后续接入；当前没有验证其他电脑的连接"]]));
    }
    function agentsPage(container) {
      container.append(node("p", "rbw-description", profilesEnabled() ? "查看这台电脑上已连接的 Agent，新建 AI 配置时从中选择。" : "切换只影响本对话下一轮回复。每位 Agent 沿用现有登录和模型配置。"));
      const toolbar = node("div", "rbw-toolbar"); toolbar.append(addResourceButton("agent")); container.append(toolbar);
      const grid = node("div", "rbw-grid"), current = selection();
      for (const agent of management.agents) {
        const item = card(agent.name || agent.id, translate("本机 Agent"), agent.available === true, agent.available ? "当前可用" : "当前不可用", [`默认模型：${agent.model || "未报告"}`, !agent.available ? agent.reason || "接入条件尚未满足" : ""]);
        item.dataset.selected = String(current.backend === agent.id);
        const controls = node("div", "rbw-card-actions");
        const choose = action(profilesEnabled() ? "用它新建 AI 配置" : current.backend === agent.id ? "下一条回复已选" : "用于下一条回复", () => chooseAgent(agent.id, agent.name), "rbw-button rbw-primary");
        choose.disabled = !agent.available || selectedBusy || !profilesEnabled() && current.backend === agent.id && current.model_id === (prefs.default_models[agent.id] || "");
        controls.append(choose);
        item.append(controls, foldedDetails("更多", [
          ...(agent.provider ? [node("p", "rbw-card-meta", `连接：${agent.provider}`)] : []),
          action("查看接入详情", () => { detail = detail === agent.id ? null : agent.id; render(); }, "rbw-button rbw-subtle"),
          catalogDetails("models", catalogModels().filter(model => model.agent_id === agent.id), { collapsed: false })
        ])); grid.append(item);
      }
      container.append(grid);
      const found = management.agents.find((agent) => agent.id === detail);
      if (found) container.append(detailPanel(`${found.name || found.id} 接入详情`, [["执行设备", management.device && management.device.name], ["对话接入", "小白兔统一文字接入器"], ["当前检测", found.available ? "接入器报告当前可用；真正发送时再次验证" : found.reason || "当前不可用"], ["模型", found.model], ["提供方", found.provider], ["部署范围", "只报告当前电脑，不推断其他电脑的配置状态"]]));
    }
    function modelMatches(model) {
      const query = modelQuery.trim().toLocaleLowerCase();
      const searchable = [model.name, model.connection, model.provider, model.agent_id].filter(Boolean).join(" ").toLocaleLowerCase();
      return (!query || searchable.includes(query)) && (modelFilter !== "favorites" || prefs.favorites.includes(model.id)) && (modelFilter !== "available" || model.available === true);
    }
    function renderModels() {
      if (!cardsNode) return;
      cardsNode.replaceChildren();
      const current = selection();
      const available = management.models.filter(modelMatches);
      if (!available.length) cardsNode.append(node("p", "rbw-empty", modelFilter === "favorites" ? "还没有收藏与当前搜索匹配的模型。" : "没有与当前筛选匹配的模型。"));
      for (const model of available) {
        const agent = management.agents.find((value) => value.id === model.agent_id);
        const label = model.status === "verified" ? "请求验证通过" : model.available ? "可选择 · 发送时验证" : "当前不可用";
        const item = card(model.name || model.model || "Agent 默认模型", model.connection_label || model.provider || "Agent 现有连接", model.available === true, label, [`Agent：${agent && agent.name || model.agent_id}`, model.is_default ? "Agent 当前默认模型" : "", model.image_input === "supported" ? "支持图像输入" : model.image_input === "unsupported" ? "不支持图像输入" : "图像能力尚未确认", !model.available ? model.reason || "" : ""]);
        const selected = current.backend === model.agent_id && current.model_id === model.id;
        item.dataset.selected = String(selected || !current.model_id && current.backend === model.agent_id && model.is_default === true); item.dataset.modelId = model.id;
        const controls = node("div", "rbw-card-actions");
        const choose = action(profilesEnabled() ? "用它新建 AI 配置" : selected ? "下一条回复已选" : !current.model_id && current.backend === model.agent_id && model.is_default === true ? "固定使用此模型" : "用于下一条回复", () => chooseModel(model), "rbw-button rbw-primary");
        choose.disabled = !model.available || !agent || !agent.available || selectedBusy || !profilesEnabled() && selected || typeof selectModel !== "function";
        const favorite = action(prefs.favorites.includes(model.id) ? "★ 已收藏" : "☆ 收藏", () => { const ids = prefs.favorites.includes(model.id) ? prefs.favorites.filter((id) => id !== model.id) : [...prefs.favorites, model.id]; setPreference("favorites", ids); renderModels(); }, "rbw-button rbw-subtle");
        aiControl(favorite);
        favorite.disabled = !prefsReady;
        favorite.setAttribute("aria-pressed", String(prefs.favorites.includes(model.id)));
        controls.append(choose, favorite);
        const metadata = [model.model ? `请求模型：${model.model}` : "", model.image_input_source || "", model.detail || ""].filter(Boolean).map(text => node("p", "rbw-card-meta", text));
        item.append(controls, foldedDetails("更多", [...metadata, catalogDetails("connections", modelConnections(model), { collapsed: false })])); cardsNode.append(item);
      }
    }
    function modelsPage(container) {
      container.append(node("p", "rbw-description", profilesEnabled() ? "从这里选择模型，新建 AI 配置。能否使用在发送时确认。" : "选择模型和支持它的 Agent，用于下一条回复。原有配置保留，能否使用在发送时确认。"));
      const toolbar = node("div", "rbw-toolbar"), search = node("input", "rbw-search");
      search.type = "search"; search.placeholder = translate("搜索模型、连接或 Agent"); search.value = modelQuery; search.setAttribute("aria-label", translate("搜索大模型"));
      search.addEventListener("input", () => { modelQuery = search.value; renderModels(); });
      const filters = node("div", "rbw-filters");
      for (const [id, label] of [["all", "全部"], ["favorites", "收藏"], ["available", "当前可用"]]) {
        const control = action(label, () => { modelFilter = id; for (const child of filters.children) child.setAttribute("aria-pressed", String(child.dataset.filter === id)); renderModels(); }, "rbw-button rbw-subtle");
        control.dataset.filter = id; control.setAttribute("aria-pressed", String(modelFilter === id)); filters.append(control);
      }
      toolbar.append(search, filters, addResourceButton("model")); container.append(toolbar);
      const follow = node("div", "rbw-follow");
      const current = selection();
      const followButton = action(current.model_id ? "跟随当前 Agent 默认模型" : "已跟随当前 Agent 默认模型", () => chooseModel({ id: "", agent_id: current.backend, name: "Agent 默认模型" }), "rbw-button rbw-primary");
      followButton.textContent = profilesEnabled() ? translate("新建跟随默认的配置") : followButton.textContent;
      followButton.disabled = selectedBusy || !current.agent || !current.agent.available || !profilesEnabled() && !current.model_id || typeof selectModel !== "function";
      follow.append(node("strong", "", translate("跟随 Agent")), node("span", "rbw-muted rbw-small", "使用这位 Agent 已配置的默认模型。候选模型不会因浏览或选择而自动请求。"), followButton);
      container.append(follow);
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
      const details = node("details", "rbw-ai-settings"); details.open = settingsDetailsOpen; details.dataset.controlInterface = "ai";
      details.addEventListener("toggle", () => { settingsDetailsOpen = details.open; });
      details.append(node("summary", "", translate("交给 AI 的设置")), node("p", "rbw-muted rbw-small", translate("这些设置只由专员或受支持的程序接口修改；这里仅展示，不提供逐项开关。")));
      if (settings) {
        const list = node("div", "rbw-ai-settings-list");
        const entries = [
          ["send_shortcut", { enter: "Enter 发送", "ctrl-enter": "Ctrl / ⌘ + Enter 发送" }, { enter: "用 Enter 发送消息", "ctrl-enter": "用 Ctrl / ⌘ + Enter 发送消息" }],
          ["density", { comfortable: "舒适", compact: "紧凑" }, { comfortable: "把布局改为舒适留白", compact: "把布局改为紧凑排列" }],
          ["markdown", { false: "原始文字", true: "Markdown 排版" }, { false: "把回答改成原始文本显示", true: "把回答显示为 Markdown 排版" }],
          ["show_internal", { false: "隐藏", true: "显示" }, { false: "隐藏已索引的协作和审批记录", true: "显示已索引的协作和审批记录" }],
        ];
        for (const [key, labels, phrases] of entries) {
          const field = settings.fields.find(value => value.key === key);
          const item = node("section", "rbw-ai-setting"); item.dataset.settingKey = field.key; item.dataset.controlInterface = "ai";
          const current = prefsReady ? prefs[field.key] : settings.settings[field.key], example = field.example.patch[field.key];
          item.append(node("h3", "", translate(field.label)), node("p", "rbw-muted rbw-small", translate(field.description)), node("p", "rbw-small", translate("当前：") + translate(labels[current])), node("p", "rbw-muted rbw-small", translate("可以这样说：") + "“" + translate(phrases[example]) + "”")); list.append(item);
        }
        details.append(list, node("p", "rbw-muted rbw-small", translate("主题保留在通用设置，也可请 AI 修改。界面语言由你在当前浏览器切换。")));
      } else details.append(node("p", "rbw-muted rbw-small", translate(settingsLoading ? "正在读取 AI 设置目录…" : settingsError || "目录尚未读取。")));
      const refresh = action(settingsLoading ? "正在读取 AI 设置目录…" : "读取最新设置目录", () => loadSettings({ apply: true }), "rbw-button rbw-subtle"); refresh.disabled = settingsLoading; refresh.dataset.controlInterface = "ai"; details.append(refresh);
      return details;
    }
    function phoneEntry() {
      const section = node("section", "rbw-phone-entry");
      section.dataset.controlInterface = "human";
      section.setAttribute("aria-label", translate("手机入口"));
      section.append(node("h3", "", translate("手机入口")), node("p", "rbw-muted rbw-small", "沿用现有 IM.codes 账号和已绑定的在线 Mac。iPhone 正式文字聊天收发和刷新保留已验证。"));
      const link = node("a", "rbw-button rbw-subtle rbw-phone-link", translate("打开 IM.codes"));
      link.setAttribute("href", "https://app.im.codes/");
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      link.setAttribute("referrerpolicy", "no-referrer");
      section.append(link);
      const details = node("details", "rbw-phone-steps");
      details.append(node("summary", "", translate("如何打开正式聊天")));
      const steps = node("ol", "");
      for (const instruction of [
        "在 Mac 打开小白兔，保持电脑和本机服务在线。",
        "在手机登录现有 IM.codes 账号，选择已绑定的在线 Mac → 本地网页预览，端口填 17860、路径填 /，打开。手机应用没有此入口时，可在 Safari 登录官方网页版查看。",
        "可在这里继续正式文字聊天；收到回答后刷新，同一段聊天仍会保留。",
      ]) steps.append(node("li", "", instruction));
      details.append(steps, node("p", "rbw-muted rbw-small", "需要重开时，固定 IM.codes 的网页面板；不要把带临时凭证的预览地址当长期书签。"));
      section.append(details, node("p", "rbw-muted rbw-small", "正式入口 17860 包含真实聊天，Mac 需保持在线。"));
      return section;
    }
    function settingsPage(container) {
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
        settingRow("主题", "跟随系统会随着系统明暗模式切换。", selectSetting("theme", [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]])),
        settingRow("界面语言", languageSaved ? "仅基础导航、管理和设置标签；其他界面、原文和配置名保持原样。语言只保存在当前浏览器。" : "当前页面已切换；浏览器未能保存，刷新后可能恢复中文。", languageSetting())
      );
      container.append(box, node("p", "rbw-hint", translate("偏好保存在这台电脑；界面语言仅保存在当前浏览器。")));
    }
    function render() {
      if (!visible) return;
      cardsNode = null;
      root.setAttribute("lang", language());
      const wrapper = node("div", "rbw-page");
      const heading = node("div", "rbw-title-row");
      heading.append(node("h2", "", translate(page === "management" ? "管理" : "设置")));
      if (page === "management") { const refresh = action(managementLoading ? "正在检测…" : "刷新检测", () => loadManagement(), "rbw-button rbw-subtle"); refresh.dataset.controlInterface = "ai"; refresh.disabled = managementLoading; heading.append(foldedDetails("更多", [refresh], "rbw-more rbw-page-more")); }
      wrapper.append(heading); if (page === "management") wrapper.append(routeSummary());
      wrapper.append(node("p", "rbw-control-legend", translate("紫色条目代表 AI 可直接操作")));
      statusNode = node("p", "rbw-feedback"); if (page === "settings") statusNode.dataset.controlInterface = "ai"; statusNode.setAttribute("role", "status"); statusNode.setAttribute("aria-live", "polite"); wrapper.append(statusNode); updateStatus();
      if (!prefsReady && !prefsLoading && saveError) wrapper.append(action("重试读取设置", () => loadPreferences(), "rbw-button rbw-subtle"));
      const body = node("div", "rbw-body");
      if (page === "management") {
        if (profilesEnabled() && profiles && ["ai-profiles", "memory-profiles"].includes(tab)) profiles.renderManagement(body, tab === "memory-profiles" ? "memory" : "ai");
        else if (!management) body.append(node("p", "rbw-empty", managementLoading ? "正在读取当前电脑的连接状态…" : "连接状态尚未读取。"));
        else {
          if (tab === "devices") devicesPage(body);
          else if (tab === "agents") agentsPage(body);
          else modelsPage(body);
          const notice = node("p", "rbw-hint", management.notice || "状态由本机连接报告，发送时再次检查。"); notice.dataset.controlInterface = "ai"; body.append(foldedDetails("检测说明", [notice]));
        }
      } else settingsPage(body);
      wrapper.append(body); root.replaceChildren(wrapper);
    }
    function show(nextPage, nextTab) {
      page = nextPage === "settings" ? "settings" : "management";
      const navigation = getNavigation(page);
      tab = navigation.some(({ id }) => id === nextTab) ? nextTab : navigation[0].id;
      if (settings && getWorkspace() && settings.workspace_id !== String(getWorkspace())) { settings = null; settingsLoading = false; settingsVersion++; }
      visible = true; root.hidden = false; detail = null; render();
      onSectionChange(getSection());
      if (!prefsReady && !prefsLoading) loadPreferences();
      else if (page === "settings" && prefsReady && !settings && !settingsLoading) loadSettings();
      if (!management && !managementLoading) loadManagement();
    }
    function hide() { visible = false; root.hidden = true; }
    function selectChanged() { if (visible) render(); }
    return { show, hide, getNavigation, getSection, preferences, loadPreferences, applyPreferences, selectChanged };
  }
  window.RabbitWorkspace = { create, translate, language, settingsCatalogue };
})();
