(() => {
  "use strict";
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
  // Match native_catalogue._title's Python strip, including NEL and excluding BOM.
  // Control whitespace is still rejected from the original input below.
  const cleanTitle = value => value.replace(/^[ \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[ \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu, "");
  const titleValid = value => typeof value === "string" && Array.from(cleanTitle(value)).length >= 1 && Array.from(cleanTitle(value)).length <= 80
    && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value);
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  function create({ root, api, getWorkspace = () => "", onError = () => {}, openDocument = null, onManageProfiles = null, descriptor = null, materialImages = null, materialFiles = null, onTextHandoff = null, onOrganize = null, canOrganize = () => false, settingsRoot = null }) {
    const agent = descriptor === null ? "codex" : descriptor.agent;
    const apiRoot = descriptor === null ? "/api/native-chats" : descriptor.apiRoot;
    const storageNamespace = descriptor === null ? "rabbit.native-chats.page.v1." : descriptor.storageNamespace;
    const createRenderer = descriptor === null ? window.RabbitSpecialist?.create : descriptor.createRenderer;
    const descriptors = { pi: ["/api/pi-chats", "rabbit.pi-native-chats.page.v1."], hermes: ["/api/hermes-chats", "rabbit.hermes-native-chats.page.v1."], claude: ["/api/claude-chats", "rabbit.claude-native-chats.page.v1."], dsh: ["/api/dsh-chats", "rabbit.dsh-native-chats.page.v1."] };
    if (descriptor !== null && (!descriptors[agent] || apiRoot !== descriptors[agent][0] || storageNamespace !== descriptors[agent][1])) throw Error("原生目录描述符无效。");
    const agentLabel = { codex: "Codex", pi: "Pi", hermes: "Hermes", claude: "Claude Code", dsh: "DSH" }[agent];
    const supportsMaterials = ["codex", "pi", "hermes", "claude", "dsh"].includes(agent);
    const rowValid = row => object(row) && uuid(row.id) && titleValid(row.title) && row.agent === agent && row.role === "chat" && typeof row.created_at === "string";
    if (!root || typeof api !== "function" || typeof createRenderer !== "function") throw Error("原生对话页面缺少入口或接口。");
    let workspace = "", storageKey = "", selected = null, pending = null, title = "", rows = [], maxConnections = 2, materialInputs = false;
    let visible = false, settingsVisible = false, enabled = false, destroyed = false, language = "zh-CN", generation = 0, reading = false, creating = false, catalogReadable = false, errorText = "", renderer = null, rendererId = null;
    let catalogueVersion = 0, navigationVersion = 0;
    // Names belong to the app catalogue. None of these operations touch a native renderer.
    const nameDrafts = new Map(), nameSaves = new Map(); let nameOpen = null, nameRead = null, selectionVersion = 0;
    const panel = node("div", "rbn-page"), controls = node("section", "rbn-catalog"), pickerLabel = node("label", "rbs-field"), pickerText = node("span"), picker = node("select", "rbs-control"), toolbar = node("div", "rbs-actions"), refreshButton = node("button", "rbs-button"), createBox = node("div", "rbn-create"), titleInput = node("input", "rbs-control"), createButton = node("button", "rbs-button"), note = node("p", "rbs-note"), errorNode = node("p", "rbs-error"), about = node("details", "rbn-about"), aboutTitle = node("summary"), aboutBody = node("p", "rbs-note"), chatRoot = node("section", "rbn-chat");
    const nameButton = node("button", "rbs-button rbw-button"), nameBox = node("div", "rbn-create"), nameInput = node("input", "rbs-control"), nameSave = node("button", "rbs-button rbw-button"), nameCancel = node("button", "rbs-button"), nameNote = node("p", "rbs-note"), nameError = node("p", "rbs-error");
    const missingNotice = node("p", "rbs-page rbs-note rbn-selection-notice");
    const quoteButton = node("button", "rbs-button"); quoteButton.type = "button";
    const organizeButton = node("button", "rbs-button rbw-button"); organizeButton.type = "button";
    organizeButton.setAttribute("data-control-interface", "ai");
    refreshButton.type = createButton.type = "button"; titleInput.type = "text"; titleInput.maxLength = 160;
    nameButton.type = nameSave.type = nameCancel.type = "button"; nameInput.type = "text"; nameInput.maxLength = 160;
    for (const control of [nameButton, nameSave]) control.setAttribute("data-control-interface", "ai");
    for (const item of [refreshButton, createButton, note, errorNode, nameNote, nameError, aboutBody, missingNotice]) item.setAttribute("data-control-interface", "ai");
    pickerLabel.append(pickerText, picker); toolbar.append(refreshButton, nameButton); createBox.append(titleInput, createButton); nameBox.append(nameInput, nameSave, nameCancel); about.append(aboutTitle, aboutBody); controls.append(pickerLabel, toolbar, nameBox, nameNote, nameError, createBox, note, errorNode, about); panel.append(controls, missingNotice, chatRoot); root.append(panel); root.hidden = true;
    toolbar.append(quoteButton, organizeButton);
    const rendererSettings = settingsRoot ? node("div", "rbn-renderer-settings") : null;
    if (settingsRoot) {
      settingsRoot.append(controls, rendererSettings);
      const chatActions = node("div", "rbs-actions rbn-chat-actions"); chatActions.append(quoteButton, organizeButton);
      panel.append(chatActions, missingNotice, chatRoot);
    }
    const text = (zh, en) => language === "en" ? en : zh;
    function save() {
      if (!storageKey) return false;
      try { window.sessionStorage.setItem(storageKey, JSON.stringify({ version: 1, selected_id: selected, title, pending_creation: pending,
        name_open: nameOpen, name_drafts: [...nameDrafts].map(([id, draft]) => ({ id, expected_title: draft.expected_title, title: draft.title, blocked: draft.blocked })) })); return true; }
      catch (_) { return false; }
    }
    function unmount() { if (renderer) renderer.destroy(); renderer = null; rendererId = null; chatRoot.replaceChildren(); }
    function syncWorkspace() {
      const value = String(getWorkspace() || ""); if (value === workspace) return;
      save(); unmount(); workspace = value; generation++; reading = creating = false;
      storageKey = value ? storageNamespace + encodeURIComponent(value) : "";
      selected = pending = null; title = ""; rows = []; catalogReadable = false; materialInputs = false; errorText = "";
      nameDrafts.clear(); nameSaves.clear(); nameOpen = nameRead = null; selectionVersion++;
      try {
        const saved = storageKey && JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
        if (object(saved) && saved.version === 1) {
          if (uuid(saved.selected_id)) selected = saved.selected_id;
          if (typeof saved.title === "string" && saved.title.length <= 160) title = saved.title;
          if (object(saved.pending_creation) && uuid(saved.pending_creation.client_id) && titleValid(saved.pending_creation.title)) {
            pending = { client_id: saved.pending_creation.client_id, title: saved.pending_creation.title }; title = pending.title;
          }
          if (Array.isArray(saved.name_drafts) && saved.name_drafts.length <= 128) for (const draft of saved.name_drafts) {
            if (object(draft) && uuid(draft.id) && titleValid(draft.expected_title) && typeof draft.title === "string" && draft.title.length <= 160 && typeof draft.blocked === "boolean") {
              nameDrafts.set(draft.id, { expected_title: draft.expected_title, title: draft.title, blocked: true, error: "" });
            }
          }
          if (uuid(saved.name_open) && nameDrafts.has(saved.name_open)) nameOpen = saved.name_open;
        }
      } catch (_) { /* Unreadable browser metadata never creates or opens a native thread. */ }
      titleInput.value = title;
    }
    function render() {
      const selectedRow = rows.find(row => row.id === selected), missing = !!selected && catalogReadable && !selectedRow;
      root.setAttribute("lang", language); pickerText.textContent = text("选择对话", "Choose chat"); picker.setAttribute("aria-label", pickerText.textContent);
      picker.replaceChildren(); const empty = node("option", "", text("选择一段原生对话", "Choose a native chat")); empty.value = ""; picker.append(empty);
      if (missing) { const option = node("option", "", text("原选对话暂不可用", "Previous chat unavailable")); option.value = selected; option.disabled = true; picker.append(option); }
      for (const row of rows) { const option = node("option", "", row.title); option.value = row.id; picker.append(option); }
      picker.value = selected || ""; picker.disabled = !enabled || reading || !catalogReadable;
      refreshButton.textContent = text("更新列表", "Refresh list"); refreshButton.disabled = !enabled || reading || !!nameRead || nameSaves.size > 0;
      quoteButton.textContent = text("交给其他 AI", "Continue with another AI"); quoteButton.hidden = typeof onTextHandoff !== "function";
      quoteButton.disabled = !enabled || !visible || !catalogReadable || reading || creating || !selectedRow;
      organizeButton.textContent = text("整理到文件夹", "Organize in folders");
      organizeButton.hidden = typeof onOrganize !== "function" || !canOrganize();
      organizeButton.disabled = !enabled || !visible || !catalogReadable || reading || creating || !selectedRow;
      const draft = nameDrafts.get(selected), editing = nameOpen === selected && !!draft, checking = nameRead?.id === selected, savingName = nameSaves.has(selected);
      nameButton.textContent = checking ? text("正在核对…", "Checking…") : editing ? text("核对当前名称", "Check current name") : text("改名", "Rename");
      nameButton.disabled = !enabled || !catalogReadable || reading || creating || !selectedRow || checking || savingName;
      for (const control of [nameButton, nameSave]) control.setAttribute("title", text("支持 AI 程序接口；人也可直接操作。", "Supports the AI program interface and direct use."));
      nameBox.hidden = nameNote.hidden = !editing; nameError.hidden = !editing || !draft.error;
      nameInput.placeholder = text("对话名称", "Chat title"); nameInput.setAttribute("aria-label", text("修改原生对话名称", "Rename native chat")); nameInput.value = editing ? draft.title : ""; nameInput.disabled = !enabled || checking || savingName;
      nameSave.textContent = savingName ? text("正在保存…", "Saving…") : text("保存名称", "Save name");
      nameSave.disabled = !enabled || !catalogReadable || reading || !selectedRow || !editing || checking || savingName || draft.blocked || !titleValid(draft.title);
      nameCancel.textContent = text("取消", "Cancel"); nameCancel.disabled = !enabled || checking || savingName;
      nameNote.textContent = editing ? text("当前名称：", "Current name: ") + draft.expected_title + text("。只修改小白兔目录名称，原生会话不变。", ". Only the app catalogue name changes; the native session stays the same.")
        + (draft.blocked ? text(" 草稿保留，请先核对当前名称再保存。", " Draft kept. Check the current name before saving.") : "") : "";
      nameError.textContent = editing ? draft.error || "" : "";
      titleInput.placeholder = text("新对话名称", "New chat title"); titleInput.setAttribute("aria-label", titleInput.placeholder); titleInput.disabled = !enabled || creating || !!pending;
      createButton.textContent = creating ? text("正在创建…", "Creating…") : pending ? text("重试创建", "Retry creation") : text("新建对话", "New chat");
      createButton.disabled = !enabled || reading || creating || !!nameRead || nameSaves.size > 0 || !catalogReadable || !workspace || !pending && (!titleValid(title) || rows.length >= 128);
      note.textContent = pending ? text("上次创建结果尚未确认；重试将使用原请求，不会发送消息。", "Creation is unconfirmed. Retry uses the original request and sends no message.") : ""; note.hidden = !pending;
      missingNotice.textContent = missing ? text("原选对话暂不在目录中。可刷新，或从左侧选择其他对话；没有自动切换。", "The previous chat is absent from the catalogue. Refresh or choose another chat from the sidebar; no other chat was opened.") : ""; missingNotice.hidden = !missing;
      aboutTitle.textContent = text("会话说明", "About native chats");
      aboutBody.textContent = text(agentLabel + " 原生记录；最多同时连接 " + maxConnections + " 段。切换页面不会断开，需在已结束的对话中明确断开。统一列表目前只搜索原生对话名称，不搜索正文。", agentLabel + " native records; up to " + maxConnections + " connections. Navigation does not disconnect. Disconnect an idle chat explicitly. Unified search currently searches native chat titles, not message bodies.");
      errorNode.textContent = errorText; errorNode.hidden = !errorText; chatRoot.hidden = !selectedRow;
    }
    async function mount() {
      if (!selected || !rows.some(row => row.id === selected)) { unmount(); return; }
      if (rendererId !== selected) {
        unmount(); rendererId = selected;
        renderer = createRenderer({ root: chatRoot, api, getWorkspace, onError, openDocument, onManageProfiles, session: { id: selected }, ...(rendererSettings ? { settingsRoot: rendererSettings } : {}), ...(supportsMaterials ? { materialImages, materialFiles, materialInputs } : {}) });
        renderer.setLanguage(language);
      }
      if (supportsMaterials) renderer.setMaterialInputs?.(materialInputs);
      if (visible && enabled) await (settingsVisible && renderer.showSettings ? renderer.showSettings() : renderer.show());
    }
    async function choose(id) {
      syncWorkspace(); if (destroyed || !enabled || reading || !catalogReadable || !rows.some(row => row.id === id)) return;
      navigationVersion++;
      if (selected !== id) selectionVersion++; selected = id; save(); render(); await mount();
    }
    function catalogue(value) {
      if (!object(value) || value.workspace_id !== workspace || !Array.isArray(value.chats) || value.chats.length > 128 || !value.chats.every(rowValid)
          || new Set(value.chats.map(row => row.id)).size !== value.chats.length || !Number.isInteger(value.max_connections) || value.max_connections < 1) throw Error("原生对话目录格式或工作区不匹配。");
      if (value.material_inputs !== undefined && (!object(value.material_inputs) || typeof value.material_inputs.enabled !== "boolean" || value.material_inputs.max_images !== (agent === "dsh" ? 0 : 4) || value.material_inputs.max_files !== 4)) throw Error("原生材料入口信息不完整。");
      return { rows: value.chats.map(row => ({ id: row.id, title: row.title, agent: row.agent, role: row.role, created_at: row.created_at })),
        maxConnections: value.max_connections, materialInputs: supportsMaterials && value.material_inputs?.enabled === true };
    }
    async function refresh(options = {}) {
      syncWorkspace(); if (destroyed || !enabled || !workspace || reading || nameRead || nameSaves.size) return;
      const stamp = generation, request = ++catalogueVersion; navigationVersion++; reading = true; catalogReadable = false; render();
      try {
        const value = await api(apiRoot);
        if (destroyed || stamp !== generation || request !== catalogueVersion || workspace !== String(getWorkspace() || "")) return;
        const data = catalogue(value); ({ rows, maxConnections, materialInputs } = data); catalogReadable = true;
        if (selected === null && !settingsVisible && rows.length) { selected = rows[0].id; selectionVersion++; }
        errorText = ""; save(); render(); if (options.deferMount !== true) await mount(); return true;
      } catch (error) { if (!destroyed && stamp === generation && request === catalogueVersion && workspace === String(getWorkspace() || "")) { catalogReadable = false; unmount(); errorText = message(error); onError(error); } }
      finally { if (stamp === generation && request === catalogueVersion && !destroyed) { reading = false; render(); } }
    }
    async function selectExisting(value, isCurrent = () => true) {
      syncWorkspace();
      if (!object(value) || value.agent !== agent || !uuid(value.chatId) || value.workspaceId !== workspace || !workspace
          || destroyed || !enabled || creating || nameRead || nameSaves.size || typeof isCurrent !== "function") throw Error(text("原生对话入口正在处理请求或所属工作区已变化。", "The native chat entry is busy or its workspace changed."));
      const stamp = generation, request = ++catalogueVersion, navigation = ++navigationVersion, sourceWorkspace = workspace, target = value.chatId;
      const current = () => !destroyed && enabled && stamp === generation && request === catalogueVersion && navigation === navigationVersion
        && sourceWorkspace === workspace && workspace === String(getWorkspace() || "") && isCurrent();
      reading = true; render();
      try {
        const data = catalogue(await api(apiRoot));
        if (!current()) return false;
        if (!data.rows.some(row => row.id === target)) throw Error(text("这段原生对话已不在目录中；没有打开其他对话。", "This native chat is no longer in the catalogue. No other chat was opened."));
        const previous = { rows, maxConnections, materialInputs, selected, catalogReadable };
        ({ rows, maxConnections, materialInputs } = data); catalogReadable = true; selected = target; selectionVersion++;
        if (!save()) { ({ rows, maxConnections, materialInputs, selected, catalogReadable } = previous); throw Error(text("浏览器不能保存原生对话选择；原选择保留。", "The browser could not save the native chat selection. The previous selection remains.")); }
        errorText = ""; render(); await mount();
        return current() && selected === target && rendererId === target;
      } catch (error) { if (!current()) return false; errorText = message(error); render(); throw error; }
      finally { if (stamp === generation && request === catalogueVersion && !destroyed) { reading = false; render(); } }
    }
    async function putDraft(value, isCurrent = () => true) {
      syncWorkspace();
      if (!object(value) || value.workspace_id !== workspace || !workspace || !uuid(value.chat_id)
          || destroyed || !enabled || reading || creating || pending || typeof isCurrent !== "function" || !isCurrent()) throw Error("原生对话目录正在处理请求、来源或工作区已变化；没有放入草稿。");
      if (value.material_refs !== undefined && (!Array.isArray(value.material_refs) || value.material_refs.length && !supportsMaterials)) throw Error("此 Agent 仍仅支持文字交接；材料没有放入草稿。");
      if (agent === "claude" && value.material_refs?.some(ref => !object(ref) || ref.kind !== "text")) throw Error("Claude当前只开放文本文件接力；原图未放入草稿。");
      const hasMemory = Object.hasOwn(value, "memory_profile");
      let memory;
      if (hasMemory) {
        if (value.memory_profile !== null && !window.RabbitNativeMemory) throw Error("共享记忆组件尚未就绪；没有放入草稿。");
        memory = value.memory_profile === null ? null : window.RabbitNativeMemory.choice(value.memory_profile, { readOnly: true });
      }
      const stamp = generation, target = value.chat_id, sourceWorkspace = value.workspace_id, text = value.text;
      if (await refresh({ deferMount: true }) !== true || !isCurrent() || destroyed || !enabled || stamp !== generation
          || workspace !== sourceWorkspace || workspace !== String(getWorkspace() || "") || !catalogReadable
          || !rows.some(row => row.id === target)) throw Error("原生对话目录未能确认这段对话；没有放入草稿。");
      const previous = selected; selected = target; if (previous !== target) selectionVersion++;
      if (!save()) { selected = previous; render(); throw Error("浏览器不能保存原生对话选择；没有放入草稿。"); }
      render(); await mount();
      if (!isCurrent() || destroyed || !enabled || stamp !== generation || workspace !== sourceWorkspace || workspace !== String(getWorkspace() || "")
          || selected !== target || rendererId !== target || !renderer || typeof renderer.putDraft !== "function") throw Error("目标原生对话已变化或没有草稿接口；没有放入草稿。");
      if (value.material_refs?.length && !materialInputs) throw Error("此入口尚未启用原生材料；整份未放入。");
      if (renderer.putDraft({ workspace_id: sourceWorkspace, chat_id: target, text, ...(value.material_refs?.length ? { material_refs: value.material_refs, ...(value.material_preview === undefined ? {} : { material_preview: value.material_preview }) } : {}), ...(hasMemory ? { memory_profile: memory } : {}) }) !== true) throw Error("目标原生对话尚未确认保存草稿；没有发送。");
      return true;
    }
    function message(error) { const value = String(error && error.message || ""); return value && value.length < 300 ? value : text("操作尚未确认，原请求保留。", "The operation is unconfirmed; the original request remains."); }
    function nameScope(token) {
      return !destroyed && enabled && token.generation === generation && token.workspace === workspace && workspace === String(getWorkspace() || "")
        && token.selection === selectionVersion && selected === token.id && nameOpen === token.id && nameDrafts.get(token.id) === token.draft;
    }
    function nameCatalogue(value) {
      const agents = ["codex", "pi", "hermes", "claude", "dsh"], counts = new Map(), identities = new Set();
      if (!object(value) || value.workspace_id !== workspace || value.scope !== "native_chat_names" || value.inference_performed !== false
          || !Array.isArray(value.enabled_agents) || value.enabled_agents.length > 5 || !value.enabled_agents.includes(agent)
          || value.enabled_agents.some((item, index, all) => !agents.includes(item) || index && agents.indexOf(item) <= agents.indexOf(all[index - 1]))
          || !Array.isArray(value.chats) || value.chats.length > 640) throw Error(text("对话名称目录不完整，请重新核对。", "The name catalogue is incomplete. Check it again."));
      for (const row of value.chats) {
        const identity = row && row.agent + ":" + row.id;
        if (!object(row) || !uuid(row.id) || !titleValid(row.title) || !value.enabled_agents.includes(row.agent) || row.role !== "chat" || typeof row.created_at !== "string"
            || identities.has(identity) || (counts.get(row.agent) || 0) >= 128) throw Error(text("对话名称目录不完整，请重新核对。", "The name catalogue is incomplete. Check it again."));
        identities.add(identity); counts.set(row.agent, (counts.get(row.agent) || 0) + 1);
      }
      return value.chats;
    }
    async function checkName() {
      syncWorkspace();
      const row = rows.find(value => value.id === selected);
      if (destroyed || !enabled || !catalogReadable || reading || creating || !row || nameRead?.id === selected || nameSaves.has(selected)) return;
      let draft = nameDrafts.get(selected), fresh = !draft;
      if (!draft) { draft = { expected_title: row.title, title: row.title, blocked: true, error: "" }; nameDrafts.set(selected, draft); }
      nameOpen = selected; draft.blocked = true; draft.error = "";
      const token = { id: selected, draft, workspace, generation, selection: selectionVersion }; nameRead = token; save(); render();
      try {
        const result = await api("/api/management/native-chat-names");
        if (nameRead !== token || !nameScope(token)) return;
        const current = nameCatalogue(result).find(value => value.agent === agent && value.id === token.id);
        if (!current) throw Error(text("这段对话不在当前名称目录中，请更新列表。", "This chat is absent from the name catalogue. Refresh the list."));
        draft.expected_title = current.title; if (fresh) draft.title = current.title; draft.blocked = false; save();
      } catch (error) {
        if (nameRead === token && nameScope(token)) { draft.error = text("名称未能确认，草稿保留；没有修改或自动重试。", "The name is unconfirmed. Draft kept; no change or automatic retry."); save(); onError(error); }
      } finally { if (nameRead === token) nameRead = null; if (!destroyed && token.generation === generation && token.workspace === workspace && workspace === String(getWorkspace() || "")) render(); }
    }
    async function renameChat() {
      syncWorkspace();
      const draft = nameDrafts.get(selected), row = rows.find(value => value.id === selected);
      if (destroyed || !enabled || !catalogReadable || reading || creating || nameRead || !row || nameOpen !== selected || !draft || draft.blocked || !titleValid(draft.title) || nameSaves.has(selected)) return;
      const token = { id: selected, draft, workspace, generation, selection: selectionVersion }, submitted = { agent, chat_id: selected, expected_title: draft.expected_title, title: cleanTitle(draft.title) };
      draft.blocked = true; draft.error = "";
      if (!save()) { draft.error = text("浏览器不能保存改名草稿，尚未提交。", "The browser cannot save this draft. Nothing was submitted."); render(); return; }
      nameSaves.set(token.id, token); render();
      try {
        const result = await api("/api/management/native-chat-names", { method: "POST", body: JSON.stringify(submitted) });
        if (!nameScope(token)) return;
        if (!object(result) || result.workspace_id !== workspace || result.scope !== "native_chat_names" || result.inference_performed !== false
            || !rowValid(result.chat) || result.chat.id !== token.id || result.chat.title !== submitted.title || result.chat.created_at !== row.created_at
            || result.previous_title !== submitted.expected_title || result.changed !== (submitted.expected_title !== submitted.title)) throw Error("改名确认格式不匹配。");
        rows = rows.map(value => value.id === token.id ? { ...value, title: result.chat.title } : value);
        nameDrafts.delete(token.id); nameOpen = null; save(); render();
      } catch (error) {
        if (nameScope(token)) { draft.error = error.status === 409
          ? text("另一入口已修改名称，没有覆盖。草稿保留，请先核对当前名称。", "Another entry changed this name; it was not overwritten. Draft kept. Check the current name first.")
          : text("保存结果尚未确认，草稿保留；请先核对当前名称，没有自动重试。", "The save is unconfirmed. Draft kept. Check the current name; no automatic retry."); save(); onError(error); }
      } finally { if (nameSaves.get(token.id) === token) nameSaves.delete(token.id); if (!destroyed && token.generation === generation) render(); }
    }
    async function createChat({ isCurrent = () => true, throwKnownFailure = false } = {}) {
      syncWorkspace(); if (typeof isCurrent !== "function" || !isCurrent() || destroyed || !enabled || reading || creating || nameRead || nameSaves.size || !catalogReadable || !workspace || !pending && (!titleValid(title) || rows.length >= 128)) return;
      const stamp = generation;
      if (!pending) {
        const client = window.crypto && window.crypto.randomUUID && window.crypto.randomUUID();
        if (!uuid(client)) { errorText = text("当前浏览器不能生成稳定的创建编号，尚未创建。", "This browser cannot generate a stable creation ID. Nothing was created."); render(); return; }
        pending = { client_id: client, title: cleanTitle(title) };
      }
      if (!save()) { errorText = text("浏览器不能保存创建请求，尚未提交；原名称保留。", "The browser cannot save the creation request. Nothing was submitted."); render(); return; }
      const submitted = { ...pending }; creating = true; errorText = ""; render();
      try {
        const row = await api(apiRoot, { method: "POST", body: JSON.stringify(submitted) });
        if (destroyed || stamp !== generation || workspace !== String(getWorkspace() || "")) return;
        if (!rowValid(row) || row.title !== submitted.title) throw Error("创建结果格式不匹配，请保留原请求核对。");
        // Create acknowledgements are immutable snapshots, including on replay.
        // The catalogue owns the current display name after any later rename.
        const current = isCurrent(), previousSelected = selected;
        if (current) { if (selected !== row.id) selectionVersion++; selected = row.id; }
        pending = null; title = ""; titleInput.value = "";
        if (!save()) {
          selected = previousSelected; pending = submitted; title = submitted.title; titleInput.value = title;
          throw createdError(row.id, "对话已创建，但浏览器尚不能保存确认；原创建编号保留，请核对这段对话。");
        }
        render(); if (current) await refresh(); return row.id;
      } catch (error) {
        if (!destroyed && stamp === generation && workspace === String(getWorkspace() || "")) { errorText = message(error); onError(error); }
        if (throwKnownFailure && uuid(error.createdChatId)) throw error;
      }
      finally { if (stamp === generation && !destroyed) { creating = false; render(); } }
    }
    picker.addEventListener("change", () => choose(picker.value)); refreshButton.addEventListener("click", refresh); createButton.addEventListener("click", createChat);
    nameButton.addEventListener("click", checkName); nameSave.addEventListener("click", renameChat);
    nameCancel.addEventListener("click", () => { if (nameRead?.id === selected || nameSaves.has(selected)) return; nameDrafts.delete(selected); nameOpen = null; save(); render(); });
    nameInput.addEventListener("input", () => { syncWorkspace(); const draft = nameDrafts.get(selected); if (nameOpen !== selected || !draft || nameRead?.id === selected || nameSaves.has(selected)) return; draft.title = nameInput.value; save(); render(); });
    titleInput.addEventListener("input", () => { syncWorkspace(); if (pending || creating) { titleInput.value = title; return; } title = titleInput.value; save(); render(); });
    function show(options = {}) { if (destroyed || !enabled) return; visible = true; settingsVisible = false; root.hidden = false; syncWorkspace(); render();
      return options.existingSelection === true && catalogReadable && selected && rendererId === selected ? mount() : refresh(); }
    function showSettings() { if (destroyed || !enabled) return; visible = true; settingsVisible = true; root.hidden = true; syncWorkspace(); render(); return refresh(); }
    function hide() { navigationVersion++; visible = settingsVisible = false; root.hidden = true; if (renderer) renderer.hide(); save(); }
    function setEnabled(value) { const next = value === true; syncWorkspace(); if (enabled !== next) generation++; enabled = next; if (!enabled) { hide(); unmount(); reading = creating = false; } render(); }
    function setLanguage(value) { language = value === "en" ? "en" : "zh-CN"; if (renderer) renderer.setLanguage(language); render(); }
    function destroy() { hide(); destroyed = true; generation++; unmount(); }
    function getQuoteContext() {
      if (destroyed || !visible || settingsVisible || !enabled || !catalogReadable || workspace !== String(getWorkspace() || "") || !selected || selected !== rendererId || !renderer) return null;
      const row = rows.find(item => item.id === selected);
      return row ? { workspace_id: workspace, chat_id: selected, title: row.title, agent, source_key: agent + ":" + selected } : null;
    }
    function getConfigurationContext() {
      const context = getQuoteContext();
      return context ? { ...context, ...renderer.getConfigurationContext?.() } : null;
    }
    quoteButton.addEventListener("click", async () => {
      try {
        const context = getQuoteContext();
        if (!context || typeof renderer.getTextSnapshot !== "function" || typeof onTextHandoff !== "function") throw Error(text("请先连接并读取这段原生对话的文字。", "Connect and read this native chat first."));
        await onTextHandoff({ ...context, ...renderer.getTextSnapshot() });
      } catch (error) { onError(error); }
    });
    organizeButton.addEventListener("click", async () => {
      try {
        const row = rows.find(item => item.id === selected);
        if (!enabled || !visible || !catalogReadable || reading || creating || !workspace || workspace !== String(getWorkspace() || "") || !row || !canOrganize() || typeof onOrganize !== "function") return;
        await onOrganize({ kind: "native_index", agent, id: row.id, title: row.title });
      } catch (error) { onError(error); }
    });
    function createdError(id, note) { const error = Error(note); error.createdChatId = id; return error; }
    function creationDraft(value, saved) {
      if (!object(value) || Object.keys(value).some(key => !["text", "material_refs", "material_preview", "memory_profile"].includes(key))
          || typeof value.text !== "string" || /[\ud800-\udfff]/u.test(value.text) || typeof TextEncoder !== "function"
          || new TextEncoder().encode(value.text).length > 128 * 1024) throw Error("待放入的原生草稿格式或文字容量不完整；尚未创建。");
      const materials = window.RabbitNativeMaterials;
      if (!materials && (value.material_refs !== undefined || value.material_preview !== undefined)) throw Error("原生材料组件尚未加载；尚未创建。");
      const refs = materials ? materials.refs(value.material_refs) : [];
      if (["claude", "dsh"].includes(agent) && refs.some(ref => ref.kind !== "text")) throw Error("此 Agent 当前只支持文本文件；尚未创建。");
      if (refs.length && value.material_preview === undefined || value.material_preview !== undefined && !refs.length) throw Error("所选原生材料缺少完整预览或引用；尚未创建。");
      const preview = value.material_preview === undefined ? undefined : materials.preview(refs, value.material_preview);
      let memory = value.memory_profile;
      if (memory !== undefined && memory !== null) {
        if (!window.RabbitNativeMemory) throw Error("共享记忆组件尚未加载；尚未创建。");
        memory = window.RabbitNativeMemory.choice(memory, { readOnly: true });
      }
      if (!value.text.trim() && (value.text !== "" || !refs.length && !saved && !memory)) throw Error("待放入的草稿没有有效文字、材料或明确配置；尚未创建。");
      return { text: value.text, ...(value.material_refs === undefined ? {} : { material_refs: refs }),
        ...(preview === undefined ? {} : { material_preview: preview }), ...(memory === undefined ? {} : { memory_profile: memory }) };
    }
    async function createNew(initialProfile = null, isCurrent = () => true, initialDraft = null) {
      syncWorkspace();
      if (!isCurrent()) return null;
      const saved = initialProfile === null ? null : window.RabbitProfiles?.profile(initialProfile, false);
      if (initialProfile !== null && (!saved || !["codex", "pi", "hermes"].includes(agent) || saved.agent_instance_id !== "local/" + agent)) throw Error("这份配置没有可用的原生选择接口，尚未创建对话。");
      const draft = initialDraft === null ? null : creationDraft(initialDraft, saved);
      if (pending || creating) throw Error(text("先在设置中核对上次新建结果。", "Check the previous creation in settings first."));
      if (reading) throw Error(text("对话目录正在读取，请稍后再新建。", "The catalogue is loading; try creating afterward."));
      const stamp = generation, sourceWorkspace = workspace;
      const current = () => !destroyed && enabled && stamp === generation && sourceWorkspace === workspace && workspace === String(getWorkspace() || "") && isCurrent();
      if (await refresh({ deferMount: true }) !== true || !current()) return null;
      if (draft?.material_refs?.length && !materialInputs) throw Error("此入口尚未启用原生材料；尚未创建。");
      title = text("新对话", "New chat"); titleInput.value = title;
      const id = await createChat({ isCurrent: current, throwKnownFailure: true });
      if (!id) return null;
      if (!current()) { if (draft) throw createdError(id, "对话已创建，但来源草稿或入口已变化；没有放入，请核对该对话。"); return null; }
      if (draft || saved) {
        let placed = false;
        try {
          if (selected === id && rendererId === id && current()) placed = draft
            ? typeof renderer?.putDraft === "function" && renderer.putDraft({ ...draft, workspace_id: sourceWorkspace, chat_id: id, ...(saved ? { ai_profile: saved } : {}) }) === true
            : typeof renderer?.selectSavedProfile === "function" && renderer.selectSavedProfile({ workspace_id: sourceWorkspace, chat_id: id, profile: saved }) === true;
        } catch (_) { /* The exact created ID remains known even if local staging fails. */ }
        if (!placed) throw createdError(id, draft ? "对话已创建，但草稿尚未保存。请从列表核对该对话；没有连接或发送。" : "对话已创建，但配置尚未保存。请从列表核对该对话；没有连接或发送。");
      }
      return id;
    }

    render(); return { show, showSettings, hide, refresh, setEnabled, setLanguage, destroy, putDraft, getQuoteContext, getConfigurationContext, selectExisting, createNew };
  }
  window.RabbitNativeChats = Object.freeze({ create });
})();
