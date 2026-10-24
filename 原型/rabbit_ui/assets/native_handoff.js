(() => {
  "use strict";
  const routes = Object.freeze({ codex: "/api/native-chats", pi: "/api/pi-chats", hermes: "/api/hermes-chats", claude: "/api/claude-chats", dsh: "/api/dsh-chats" });
  const materialAgent = value => ["codex", "pi", "hermes", "claude", "dsh"].includes(value);
  const materialKind = (agent, kind) => materialAgent(agent) && (kind === "text" || kind === "image" && !["claude", "dsh"].includes(agent));
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const count = value => Array.from(value).length;
  const bytes = value => new TextEncoder().encode(value).byteLength;
  const id = value => typeof value === "string" && value.length > 0 && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value);
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
  const cleanTitle = value => value.replace(/^[ \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[ \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu, "");
  const titleValid = value => typeof value === "string" && count(cleanTitle(value)) >= 1 && count(cleanTitle(value)) <= 80 && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value);
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const keys = (value, names) => object(value) && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
  const memoryText = (value, maximum, multiline = false) => typeof value === "string" && count(value) <= maximum && !/[\uD800-\uDFFF]/u.test(value) && !(multiline ? /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/ : /[\x00-\x1f\x7f]/).test(value);
  function memoryCandidate(value) {
    if (!keys(value, ["profile_id", "profile_revision", "profile_name", "retrieved_at", "references", "habits"]) || !uuid(value.profile_id) || !integer(value.profile_revision) || !value.profile_revision || !memoryText(value.profile_name, 40) || !value.profile_name.trim() || !memoryText(value.retrieved_at, 64) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value.retrieved_at) || !Number.isFinite(Date.parse(value.retrieved_at)) || !memoryText(value.habits, 4000, true) || !Array.isArray(value.references) || value.references.length > 16) throw Error("历史记忆候选暂时无法完整核验，原草稿保留。");
    let characters = 0; const seen = new Set();
    for (const ref of value.references) {
      if (!keys(ref, ["reference_id", "memory_profile_id", "memory_profile_revision", "type", "text", "retrieved_at", "document_ref"]) || typeof ref.reference_id !== "string" || !/^memref_[a-f0-9]{32}$/.test(ref.reference_id) || seen.has(ref.reference_id) || ref.memory_profile_id !== value.profile_id || ref.memory_profile_revision !== value.profile_revision || ref.retrieved_at !== value.retrieved_at || !["world", "experience", "observation"].includes(ref.type) || !memoryText(ref.text, 8192, true) || !ref.text.trim() || !(ref.document_ref === null || typeof ref.document_ref === "string" && /^memdoc_[a-f0-9]{32}$/.test(ref.document_ref))) throw Error("历史记忆条目或来源暂时无法核验，原草稿保留。");
      seen.add(ref.reference_id); characters += count(ref.text);
    }
    if (characters > 8192) throw Error("历史记忆候选超出容量，原草稿保留。");
    return { ...value, references: value.references.map(ref => ({ ...ref })) };
  }
  function sameValue(left, right) {
    if (left === right) return true;
    if (Array.isArray(left)) return Array.isArray(right) && left.length === right.length && left.every((value, index) => sameValue(value, right[index]));
    if (!object(left) || !object(right) || Object.keys(left).length !== Object.keys(right).length) return false;
    return Object.keys(left).every(key => Object.hasOwn(right, key) && sameValue(left[key], right[key]));
  }
  const node = (tag, cls, text) => { const item = document.createElement(tag); if (cls) item.className = cls; if (text !== undefined) item.textContent = String(text); return item; };
  function create({ root, api, getSource, getTargets, placeDraft, materialImages = null, onError = () => {}, sourceAdapter = null }) {
    if (!root || [api, getSource, getTargets, placeDraft].some(value => typeof value !== "function")) throw Error("原生接力缺少入口或接口。");
    const nativeSource = sourceAdapter !== null;
    const fileSource = sourceAdapter?.kind === "file_material";
    if (nativeSource && (!object(sourceAdapter) || typeof sourceAdapter.read !== "function" || typeof sourceAdapter.prepare !== "function")) throw Error("原生文字接力接口未就绪。");
    if (nativeSource && sourceAdapter.kind !== undefined && !fileSource) throw Error("接力资料来源类型未接入。");
    let sourceIncomplete = false;
    let generation = 0, catalogueGeneration = 0, selectionVersion = 0, captured = null, source = null, records = [], targets = [], chats = [], selected = new Set();
    let preview = null, fileSnapshot = null, reading = false, readingChats = false, preparing = false, placing = false, notice = "", catalogueError = "", returnFocus = null;
    let materialSelections = new Map(), materialInputsEnabled = false, lastAgent = "";
    let memorySelections = new Map(), habitsMessageId = null;
    // Only creation metadata is journaled. Keep it separate from each native
    // page's selection/name drafts, and retain it across modal/source changes.
    let creationKey = "", creationRaw = null, creationPending = null, creationBlocked = false, creatingTarget = false, creationGeneration = 0, creationTargetVersion = 0, creationError = "";
    let creationDetails, creationName, creationButton, creationNote;
    let panel, heading, sourceNote, status, collection, task, agent, chat, previewButton, insertButton, fullText, previewDetails, previewCounts, previewMaterials, selectAll, selectLast, clear, closeButton;
    root.hidden = true;
    const button = (text, callback) => { const item = node("button", "file-task-action", text); item.type = "button"; item.addEventListener("click", callback); return item; };
    const indexedSource = () => nativeSource && (source?.origin_kind || captured?.origin_kind) === "history_index";
    function sameScope(stamp = generation) {
      const current = getSource();
      return !root.hidden && captured && stamp === generation && current?.workspace_id === captured.workspace_id
        && (fileSource ? current?.origin_kind === "file_material" && current.material_id === captured.material_id && current.source_key === captured.source_key
          : current?.chat_id === captured.chat_id && (!nativeSource || current.source_key === captured.source_key));
    }
    function targetAvailable() { return targets.some(item => item.agent === agent?.value) && getTargets().some(item => item.agent === agent.value && item.apiRoot === routes[item.agent]); }
    function ids() { return records.filter(record => selected.has(record.message_id)).map(record => record.message_id); }
    function errorMessage(error) { const value = String(error?.message || ""); return value && value.length <= 400 ? value : "接力资料暂时无法完整核验，请重新打开；原草稿保留。"; }
    function fail(error) { notice = errorMessage(error); onError(error); render(); }
    function close() {
      if (placing) return;
      generation++; catalogueGeneration++; root.hidden = true; captured = source = preview = fileSnapshot = null; records = targets = chats = []; selected.clear(); materialSelections.clear();
      creationGeneration++; creatingTarget = false; creationKey = ""; creationRaw = creationPending = null; creationBlocked = false; creationError = "";
      memorySelections.clear(); habitsMessageId = null;
      reading = readingChats = preparing = placing = false; root.replaceChildren();
      // Do not retain detached DOM containing complete quoted conversations.
      panel = heading = sourceNote = status = collection = task = agent = chat = previewButton = insertButton = fullText = previewDetails = previewCounts = previewMaterials = selectAll = selectLast = clear = closeButton = null;
      creationDetails = creationName = creationButton = creationNote = null;
      if (nativeSource && typeof sourceAdapter.release === "function") sourceAdapter.release();
      const shell = document.querySelector(".app-shell"); if (shell) shell.inert = false;
      if (returnFocus && returnFocus.isConnected !== false) returnFocus.focus();
    }
    function invalidate() {
      for (const [key, value] of materialSelections) if (!selected.has(value.message_id)) materialSelections.delete(key);
      for (const [key, value] of memorySelections) if (!selected.has(value.message_id)) memorySelections.delete(key);
      if (!selected.has(habitsMessageId)) habitsMessageId = null;
      selectionVersion++; preview = null; preparing = false; notice = "";
      if (previewDetails) { previewDetails.hidden = true; fullText.textContent = ""; previewMaterials.replaceChildren(); }
      render();
    }
    function render() {
      if (root.hidden || !captured || !panel) return;
      const current = sameScope(), selection = ids(), validTarget = targetAvailable();
      sourceNote.textContent = fileSource ? source ? `完整文件：${source.title} · ${fileSnapshot.material_preview.files[0].size_bytes} 字节 · 固定版本 ${source.revision.slice(0, 12)}` : "正在核对完整Markdown文件资料…" : source ? `来源：${source.title} · ${records.length} 条可选文字${nativeSource ? "片段" : "记录"}。已选 ${selection.length}/50 条。` + (indexedSource() ? " 已读历史索引文字快照。" + (sourceIncomplete ? "仍有未加载、完整性未核实或不可引用的内容。" : "只包括当前已读索引文字。") : nativeSource ? sourceIncomplete ? " 这是已读取部分，仍有未加载、未完成或不可引用的内容。" : " 仅交接本页已读取的文字快照。" : "") : indexedSource() ? "正在核对已读历史索引文字…" : nativeSource ? "正在核对已读取的原生文字…" : "正在读取已保存的共同聊天…";
      closeButton.disabled = placing;
      task.disabled = reading || placing; agent.disabled = reading || placing || !targets.length; chat.disabled = reading || readingChats || placing || !validTarget || !chats.length;
      selectAll.disabled = reading || placing || !records.length || records.length > 50; selectLast.disabled = clear.disabled = reading || placing || !records.length;
      for (const checkbox of collection.querySelectorAll("input")) {
        if (checkbox.dataset.memoryReferenceId) {
          checkbox.checked = memorySelections.has(checkbox.dataset.messageId + ":" + checkbox.dataset.memoryReferenceId);
          checkbox.disabled = reading || preparing || placing || !selected.has(checkbox.dataset.messageId) || !checkbox.checked && memorySelections.size >= 16;
        } else if (checkbox.dataset.memoryHabitsId) {
          checkbox.checked = habitsMessageId === checkbox.dataset.messageId;
          checkbox.disabled = reading || preparing || placing || !selected.has(checkbox.dataset.messageId);
        } else if (checkbox.dataset.materialKey) {
          const item = materialSelections.get(checkbox.dataset.materialKey);
          checkbox.checked = item?.message_id === checkbox.dataset.messageId;
          const kind = checkbox.dataset.materialKind, total = [...materialSelections.values()].filter(value => value.kind === kind).length;
          checkbox.disabled = reading || readingChats || preparing || placing || !materialKind(agent.value, kind) || !materialInputsEnabled || !selected.has(checkbox.dataset.messageId) || !checkbox.checked && (materialSelections.size >= 8 || total >= 4);
        } else { checkbox.checked = selected.has(checkbox.dataset.messageId); checkbox.disabled = reading || placing || !checkbox.checked && selection.length >= 50; }
      }
      for (const option of agent.children) option.disabled = [...materialSelections.values()].some(item => !materialKind(option.value, item.kind));
      previewButton.disabled = !current || reading || preparing || placing || !source || (fileSource ? !fileSnapshot || !materialInputsEnabled : !selection.length) || !task.value.trim() || !validTarget;
      previewButton.textContent = preparing ? "正在准备…" : fileSource ? "预览任务与完整文件" : "预览接力文字";
      insertButton.disabled = !current || !preview || preparing || placing || readingChats || !validTarget || fileSource && !materialInputsEnabled || !chats.some(item => item.id === chat.value);
      insertButton.textContent = placing ? "正在放入…" : "放入原生草稿";
      creationName.disabled = !current || reading || placing || creatingTarget || creationBlocked || !!creationPending;
      creationButton.disabled = !current || reading || readingChats || placing || creatingTarget || creationBlocked || !validTarget || fileSource && !materialInputsEnabled || !!catalogueError || !creationKey || !creationPending && (!titleValid(creationName.value) || chats.length >= 128);
      creationButton.textContent = creatingTarget ? "正在新建…" : creationPending ? "用原编号继续新建" : "新建目标对话";
      creationNote.textContent = creationError || (creationPending ? "原新建请求尚未确认；继续将使用原编号和名称，不自动重发。" : "只创建对话目录与工作区；当前选择和预览保留，不连接或发送消息。");
      status.textContent = !current ? (fileSource ? "文件资料或工作区已变化，请重新打开；没有放入其他草稿。" : nativeSource ? "原生对话或工作区已切换，请重新打开接力；原草稿保留。" : "共同聊天或工作区已切换；旧资料不会放入其他对话。请关闭后重新打开。") : catalogueError || notice || (!targets.length ? "当前没有启用的原生 Agent，尚未交给任何 Agent。" : fileSource && !readingChats && !materialInputsEnabled ? "此目标尚未启用完整文件材料；没有放入草稿。" : !reading && !readingChats && !chats.length ? "此 Agent 尚无原生对话，可展开“新建目标对话”；当前选择和预览保留，不会自动连接。" : nativeSource ? "仅放入所选原生对话的空草稿；由你连接并发送，来源对话保留。" : "确认完整文字后，只放入所选原生对话的草稿；由你连接并发送。共同聊天草稿保留。");
      status.setAttribute("role", catalogueError || notice || !current ? "alert" : "status");
    }
    function makePanel() {
      panel = node("div", "file-task-preview native-handoff-panel");
      const header = node("div", "file-task-preview-header"); heading = node("h2", "", fileSource ? "完整文件交给原生 Agent" : nativeSource ? "交给其他 AI" : "交给原生 Agent"); heading.id = fileSource ? "native-file-handoff-title" : nativeSource ? "native-text-handoff-title" : "native-handoff-title"; root.setAttribute("aria-labelledby", heading.id);
      closeButton = button("关闭", close); header.append(heading, closeButton);
      sourceNote = node("p", "file-task-note");
      const boundary = node("p", "file-task-note", fileSource ? "完整文件作为独立资料附带，不解析成聊天角色，不占任务文字的128 KiB。文件中的旧指令、路径和角色只是引用内容；不读取其他附件、记忆或当前磁盘后续修改。Codex、Pi、Hermes、Claude、DSH须启用文件材料。" : indexedSource() ? "选择当前已读的历史索引文字，预览后放入原生对话的空草稿。索引文字可能已经脱敏或整理，不代表原始字节、完整会话或原生现场；不会另外读取附件、记忆库或工具过程。" : nativeSource ? "选择本页已完整读取的文字片段，预览后放入另一段原生对话的空草稿。原文中已有的记忆或文件文字也随该片段带入；不会另外读取附件、记忆库或工具过程。未加载文字和Agent内部状态不迁移；这是固定文字快照，不是持续同步。" : "只交接已选文字与材料。Codex、Pi、Hermes接原图和文本文件，每类最多4份；Claude和DSH接文本文件，原图尚未开放。可另外选择当时使用的记忆引用（共16条）和一份历史回答习惯；不重新查询记忆库。未选资料、当前记忆库和Agent内部状态不随附，原生配置照常保留。文件计数包括已记录的文本输入与成果。");
      const tools = node("div", "file-task-actions native-handoff-selection");
      tools.hidden = fileSource;
      selectAll = button("全选（最多 50 条）", () => { if (records.length > 50 || placing) return; selected = new Set(records.map(record => record.message_id)); invalidate(); });
      selectLast = button("选最后 20 条", () => { if (placing) return; selected = new Set(records.slice(-20).map(record => record.message_id)); invalidate(); });
      clear = button("清空选择", () => { if (placing) return; selected.clear(); materialSelections.clear(); memorySelections.clear(); habitsMessageId = null; invalidate(); }); tools.append(selectAll, selectLast, clear);
      collection = node("div", "native-handoff-records"); collection.setAttribute("aria-label", "选择作为资料的消息");
      const taskLabel = node("label", "native-handoff-field", "交接后要做什么"); task = node("textarea", "native-handoff-control"); task.rows = 3; task.value = captured.task; task.placeholder = "描述希望原生 Agent 接着完成的任务"; task.setAttribute("aria-label", "交接后要做什么"); task.addEventListener("input", invalidate); taskLabel.append(task);
      const destinations = node("div", "native-handoff-destinations"), agentLabel = node("label", "native-handoff-field", "原生 Agent"), chatLabel = node("label", "native-handoff-field", "放入哪段原生对话");
      agent = node("select", "native-handoff-control"); agent.setAttribute("aria-label", "原生 Agent");
      for (const target of targets) { const option = node("option", "", target.label); option.value = target.agent; agent.append(option); }
      chat = node("select", "native-handoff-control"); chat.setAttribute("aria-label", "放入哪段原生对话");
      agent.addEventListener("change", () => { if ([...materialSelections.values()].some(item => !materialKind(agent.value, item.kind))) { agent.value = targets.find(item => item.agent === lastAgent && [...materialSelections.values()].every(material => materialKind(item.agent, material.kind)))?.agent || targets.find(item => [...materialSelections.values()].every(material => materialKind(item.agent, material.kind)))?.agent || ""; notice = "此 Agent 尚不支持全部所选材料；请先取消相应材料，全部选择仍保留。"; render(); return; } invalidate(); return loadChats(); }); chat.addEventListener("change", () => { creationTargetVersion++; render(); });
      agentLabel.append(agent); chatLabel.append(chat); destinations.append(agentLabel, chatLabel);
      creationDetails = node("details", "rbh-details"); creationDetails.append(node("summary", "", "新建目标对话"));
      const nameLabel = node("label", "native-handoff-field", "新对话名称"); creationName = node("input", "native-handoff-control"); creationName.type = "text"; creationName.maxLength = 160; creationName.setAttribute("aria-label", "新建目标对话名称");
      creationName.addEventListener("input", () => { if (creationPending || creationBlocked || creatingTarget) creationName.value = creationPending?.title || ""; render(); }); nameLabel.append(creationName);
      creationButton = button("新建目标对话", createTarget); creationNote = node("p", "file-task-note"); creationNote.setAttribute("aria-live", "polite"); creationDetails.append(nameLabel, creationButton, creationNote);
      const actions = node("div", "file-task-actions"); previewButton = button("预览接力文字", prepare); insertButton = button("放入原生草稿", insert); actions.append(previewButton, insertButton);
      previewDetails = node("details", "native-handoff-preview"); previewDetails.hidden = true; previewDetails.open = true;
      previewDetails.append(node("summary", "", "将交给 Agent 的完整文字")); previewCounts = node("p", "file-task-note"); fullText = node("pre", "file-task-preview-content"); previewMaterials = node("div", "native-handoff-materials"); previewDetails.append(previewCounts, fullText, previewMaterials);
      status = node("p", "file-task-note native-handoff-status"); status.setAttribute("aria-live", "polite");
      panel.append(header, sourceNote, boundary, tools, collection, taskLabel, destinations, creationDetails, actions, status, previewDetails); root.replaceChildren(panel); render();
    }
    function loadCreation() {
      creationGeneration++; creatingTarget = false; creationPending = null; creationRaw = null; creationBlocked = false; creationError = "";
      creationKey = captured && routes[agent.value] ? "rabbit.native-handoff.creation.v1." + encodeURIComponent(captured.workspace_id) + "." + agent.value : "";
      creationName.value = ""; creationDetails.open = false;
      if (!creationKey) return;
      try {
        creationRaw = window.sessionStorage.getItem(creationKey);
        if (creationRaw !== null) {
          if (typeof creationRaw !== "string" || creationRaw.length > 1024) throw Error();
          const saved = JSON.parse(creationRaw);
          if (!keys(saved, ["version", "client_id", "title"]) || saved.version !== 1 || !uuid(saved.client_id) || !titleValid(saved.title) || cleanTitle(saved.title) !== saved.title) throw Error();
          creationPending = { client_id: saved.client_id, title: saved.title }; creationName.value = saved.title; creationDetails.open = true;
        }
      } catch (_) { creationBlocked = true; creationDetails.open = true; creationError = "浏览器中的原创建记录无法读取或核对；没有覆盖记录，也不会新建。现有目标仍可选择。"; }
    }
    async function createTarget() {
      render(); if (!creationButton || creationButton.disabled) return false;
      const key = creationKey, targetAgent = agent.value, stamp = generation, version = selectionVersion, targetVersion = creationTargetVersion, previousChat = chat.value, sourceTask = getSource()?.task;
      const ticket = ++creationGeneration;
      const current = () => ticket === creationGeneration && sameScope(stamp) && version === selectionVersion && targetVersion === creationTargetVersion && getSource()?.task === sourceTask && agent.value === targetAgent && chat.value === previousChat && creationKey === key && targetAvailable();
      let submitted, raw;
      try {
        if (window.sessionStorage.getItem(key) !== creationRaw) throw Error();
        submitted = creationPending || { client_id: window.crypto?.randomUUID?.(), title: cleanTitle(creationName.value) };
        if (!uuid(submitted.client_id) || !titleValid(submitted.title)) throw Error();
        raw = JSON.stringify({ version: 1, ...submitted });
        window.sessionStorage.setItem(key, raw);
        if (window.sessionStorage.getItem(key) !== raw) throw Error();
        creationRaw = raw; creationPending = { ...submitted };
      } catch (_) { creationBlocked = true; creationDetails.open = true; creationError = "浏览器不能保存或核对原创建编号，尚未提交；记录没有被替换。"; render(); return false; }
      creatingTarget = true; creationError = ""; render();
      try {
        const row = await api(routes[targetAgent], { method: "POST", body: JSON.stringify(submitted) }, false);
        if (!current()) return false;
        if (!keys(row, ["id", "title", "agent", "role", "created_at"]) || !uuid(row.id) || row.agent !== targetAgent || row.role !== "chat" || row.title !== submitted.title || !id(row.created_at) || row.created_at.length > 80) throw Error("新建回执无法核对，原编号保留；没有放入草稿。");
        if (window.sessionStorage.getItem(key) !== raw) { creationBlocked = true; throw Error("原创建记录已有变化，没有覆盖或清除；请保留原编号核对。"); }
        window.sessionStorage.removeItem(key);
        if (window.sessionStorage.getItem(key) !== null) { creationBlocked = true; throw Error("新建已收到回执，但浏览器不能清除原记录；请用原编号核对。"); }
        creationPending = null; creationRaw = null; creationName.value = ""; creationDetails.open = false;
        if (!chats.some(item => item.id === row.id)) { chats.push({ id: row.id, title: row.title }); const option = node("option", "", row.title); option.value = row.id; chat.append(option); }
        chat.value = row.id; notice = "目标对话已新建，当前选择和完整预览保留；核对后点“放入原生草稿”，尚未连接或发送。";
        return true;
      } catch (error) { if (current()) { creationError = errorMessage(error); creationDetails.open = true; onError(error); } return false; }
      finally { if (ticket === creationGeneration && stamp === generation) { creatingTarget = false; render(); } }
    }
    function renderRecords() {
      collection.replaceChildren();
      for (const record of records) {
        const entry = node("article", "native-handoff-record"), label = node("label", "native-handoff-pick"), checkbox = node("input");
        checkbox.type = "checkbox"; checkbox.dataset.messageId = record.message_id; checkbox.checked = selected.has(record.message_id);
        checkbox.addEventListener("change", () => { if (placing) return; if (checkbox.checked && selected.size < 50) selected.add(record.message_id); else { selected.delete(record.message_id); for (const [key, value] of materialSelections) if (value.message_id === record.message_id) materialSelections.delete(key); } invalidate(); });
        const who = (indexedSource() ? "索引角色：" : "") + (nativeSource ? (record.role === "user" ? "用户" : "助手") : record.source_kind || (record.role === "user" ? "用户" : "助手"));
        const owner = record.source_ai ? ` · ${record.source_ai}` : "";
        const imageCount = record.image_count_unknown ? `图片至少 ${record.excluded_images} 项，部分数量未明` : `${record.excluded_images} 张图片`;
        label.append(checkbox, node("span", "", `${who}${owner} · ${count(record.content)} 字符${!nativeSource && (record.excluded_images || record.excluded_files || record.image_count_unknown) ? ` · 未附 ${imageCount}、${record.excluded_files} 份文本文件` : ""}`));
        const details = node("details"), summary = node("summary", "", indexedSource() ? "查看索引全文" : "查看完整原文"), original = node("pre", "file-task-preview-content");
        original.textContent = record.content || "（这条记录没有文字，所含材料未附）"; details.append(summary, original);
        const excerpt = Array.from(record.content.replace(/\s+/g, " ")).slice(0, 110).join("");
        entry.append(label, node("p", "native-handoff-excerpt", excerpt + (count(record.content.replace(/\s+/g, " ")) > 110 ? "…" : "")), details);
        for (const candidate of record.material_candidates || []) {
          const key = candidate.kind + ":" + candidate.id, pick = node("input"), wrap = node("label", "native-handoff-pick"); pick.type = "checkbox";
          pick.dataset.messageId = record.message_id; pick.dataset.materialKey = key; pick.dataset.materialKind = candidate.kind;
          wrap.append(pick, node("span", "", (candidate.kind === "image" ? "原图：" : record.role === "assistant" ? "成果文件：" : "文本文件：") + candidate.name + (candidate.kind === "image" ? " · Codex / Pi / Hermes" : " · Codex / Pi / Hermes / Claude / DSH")));
          pick.addEventListener("change", () => {
            if (placing || preparing || !materialKind(agent.value, candidate.kind) || !materialInputsEnabled || !selected.has(record.message_id)) { render(); return; }
            if (pick.checked) { const total = [...materialSelections.values()].filter(value => value.kind === candidate.kind).length; if (!materialSelections.has(key) && (materialSelections.size >= 8 || total >= 4)) { render(); return; } materialSelections.set(key, { message_id: record.message_id, ...candidate }); }
            else materialSelections.delete(key); invalidate();
          }); entry.append(wrap);
        }
        if (record.memory_unavailable) entry.append(node("p", "file-task-note", "这条回答的历史记忆来源暂时无法核验；文字仍可选择，记忆不会补造。"));
        const memory = record.memory_candidates;
        if (memory) {
          const group = node("details", "rbh-details"); group.append(node("summary", "", `当时使用的记忆：${memory.profile_name} · 第 ${memory.profile_revision} 版 · ${memory.references.length} 条引用`));
          for (const [index, ref] of memory.references.entries()) {
            const pick = node("input"), wrap = node("label", "native-handoff-pick"), key = record.message_id + ":" + ref.reference_id;
            pick.type = "checkbox"; pick.dataset.messageId = record.message_id; pick.dataset.memoryReferenceId = ref.reference_id;
            wrap.append(pick, node("span", "", `带入历史记忆引用 ${index + 1}`));
            pick.addEventListener("change", () => {
              if (placing || preparing || !selected.has(record.message_id)) { render(); return; }
              if (pick.checked && (memorySelections.has(key) || memorySelections.size < 16)) memorySelections.set(key, { message_id: record.message_id, reference_id: ref.reference_id });
              else memorySelections.delete(key); invalidate();
            });
            group.append(wrap, node("pre", "file-task-preview-content", ref.text), node("p", "file-task-note", `${ref.reference_id} · ${ref.retrieved_at}`));
          }
          if (memory.habits) {
            const pick = node("input"), wrap = node("label", "native-handoff-pick"); pick.type = "checkbox"; pick.dataset.messageId = record.message_id; pick.dataset.memoryHabitsId = record.message_id;
            wrap.append(pick, node("span", "", "带入当时的身份与回答习惯（一次选一份）"));
            pick.addEventListener("change", () => { if (!placing && !preparing && selected.has(record.message_id)) { habitsMessageId = pick.checked ? record.message_id : null; invalidate(); } else render(); });
            group.append(wrap, node("pre", "file-task-preview-content", memory.habits));
          }
          entry.append(group);
        }
        collection.append(entry);
      }
      if (!records.length) collection.append(node("p", "file-task-note", "暂无可交接的已完成文字记录。"));
      render();
    }
    async function loadChats() {
      const target = targets.find(item => item.agent === agent.value), stamp = generation, ticket = ++catalogueGeneration;
      loadCreation();
      chats = []; materialInputsEnabled = false; catalogueError = ""; chat.replaceChildren(); const empty = node("option", "", "请选择原生对话"); empty.value = ""; chat.append(empty);
      if (!target || !sameScope(stamp)) { readingChats = false; render(); return; }
      readingChats = true; render();
      try {
        lastAgent = target.agent;
        const value = await api(target.apiRoot);
        if (!sameScope(stamp) || ticket !== catalogueGeneration || agent.value !== target.agent) return;
        if (!object(value) || value.workspace_id !== captured.workspace_id || !Array.isArray(value.chats) || value.chats.length > 128 || !value.chats.every(item => object(item) && uuid(item.id) && item.agent === target.agent && item.role === "chat" && typeof item.title === "string") || new Set(value.chats.map(item => item.id)).size !== value.chats.length) throw Error("原生对话目录格式或工作区不匹配，尚未选择目标。");
        if (value.material_inputs !== undefined && (!object(value.material_inputs) || typeof value.material_inputs.enabled !== "boolean" || value.material_inputs.max_images !== (target.agent === "dsh" ? 0 : 4) || value.material_inputs.max_files !== 4)) throw Error("材料入口信息不完整，尚未放入草稿。");
        materialInputsEnabled = materialAgent(target.agent) && value.material_inputs?.enabled === true;
        chats = value.chats.map(item => ({ id: item.id, title: item.title }));
        for (const item of chats) { const option = node("option", "", item.title); option.value = item.id; chat.append(option); }
        chat.value = "";
      } catch (error) { if (sameScope(stamp) && ticket === catalogueGeneration) { catalogueError = errorMessage(error); onError(error); render(); } }
      finally { if (stamp === generation && ticket === catalogueGeneration) { readingChats = false; render(); } }
    }
    function chosenMemory() {
      const references = [], requests = []; let habits = null, available = 0;
      for (const record of records) {
        const memory = record.memory_candidates;
        if (!selected.has(record.message_id) || !memory) continue;
        available += memory.references.length;
        for (const ref of memory.references) if (memorySelections.has(record.message_id + ":" + ref.reference_id)) {
          requests.push({ message_id: record.message_id, reference_id: ref.reference_id }); references.push({ message_id: record.message_id, ...ref });
        }
        if (habitsMessageId === record.message_id) habits = { message_id: record.message_id, profile_id: memory.profile_id, profile_revision: memory.profile_revision, profile_name: memory.profile_name, habits: memory.habits };
      }
      return { requests, preview: references.length || habits ? { references, habits, excluded_references: available - references.length } : null };
    }
    async function prepare() {
      render(); if (previewButton.disabled) return false;
      const memory = chosenMemory(), stamp = generation, version = selectionVersion, targetAgent = agent.value, selection = ids(), materials = [...materialSelections.values()].filter(value => selected.has(value.message_id)), body = fileSource ? { task: task.value } : { source_revision: source.revision, message_ids: selection, task: task.value,
        ...(memory.requests.length ? { memory_selections: memory.requests } : {}), ...(memory.preview?.habits ? { habits_message_id: habitsMessageId } : {}),
        ...(["hermes", "claude"].includes(targetAgent) ? { target_agent: targetAgent } : {}),
        ...(materials.length ? { material_selections: materials.map(({ message_id, kind, id }) => ({ message_id, kind, id })) } : {}) };
      preparing = true; notice = ""; preview = null; previewDetails.hidden = true; render();
      try {
        const value = nativeSource ? await sourceAdapter.prepare({ captured, source, records, body, targetAgent, ...(fileSource ? { snapshot: fileSnapshot } : {}) }) : await api(`/api/chats/${encodeURIComponent(captured.chat_id)}/native-handoff`, { method: "POST", body: JSON.stringify(body) });
        if (!sameScope(stamp) || version !== selectionVersion) return false;
        if (fileSource) {
          if (!keys(value, ["snapshot_kind", "workspace_id", "source", "prompt", "sha256", "size_bytes", "characters", "material_refs", "material_preview"])
              || value.snapshot_kind !== "file_material" || value.workspace_id !== captured.workspace_id || !sameValue(value.source, source)
              || typeof value.prompt !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256) || !integer(value.size_bytes) || value.size_bytes !== bytes(value.prompt)
              || value.size_bytes > 131072 || value.characters !== count(value.prompt)) throw Error("完整文件任务的回执不匹配，原草稿保留。");
        } else if (!object(value) || value.schema_version !== (memory.preview ? 3 : materials.length ? 2 : 1) || value.workspace_id !== captured.workspace_id || !object(value.source) || value.source.chat_id !== source.chat_id || value.source.revision !== source.revision || value.source.title !== source.title || JSON.stringify(value.selected_message_ids) !== JSON.stringify(selection) || typeof value.prompt !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256) || !integer(value.size_bytes) || value.size_bytes !== bytes(value.prompt) || value.size_bytes > 131072 || value.characters !== count(value.prompt) || value.record_count !== selection.length || !object(value.excluded) || !integer(value.excluded.images) || !integer(value.excluded.files) || typeof value.excluded.image_count_unknown !== "boolean") throw Error("完整接力文字的回执不匹配；整份未放入，没有截断。");
        if (indexedSource() && (value.source.origin_kind !== "history_index" || value.source.history_session_id !== source.history_session_id || value.source.agent !== source.agent)) throw Error("索引文字来源与预览不一致；整份未放入。");
        if (memory.preview ? !sameValue(memory.preview, value.memory_preview) : value.memory_preview !== undefined) throw Error("记忆引用或历史习惯与明确选择不一致；整份未放入。");
        if (!fileSource && (["hermes", "claude"].includes(targetAgent) ? value.target_agent !== targetAgent : value.target_agent !== undefined)) throw Error("接力文字的目标编码不匹配；整份未放入。");
        let refs = [], materialPreview = null;
        if (fileSource) {
          if (!materialAgent(targetAgent) || !materialInputsEnabled || !sameValue(value.material_refs, fileSnapshot.material_refs) || !sameValue(value.material_preview, fileSnapshot.material_preview)) throw Error("完整文件版本或目标材料能力已变化；整份未放入。");
          refs = window.RabbitNativeMaterials.refs(value.material_refs); materialPreview = window.RabbitNativeMaterials.preview(refs, value.material_preview);
        } else if (materials.length) {
          if (!materialAgent(agent.value) || materials.some(item => !materialKind(agent.value, item.kind)) || !materialInputsEnabled || !window.RabbitNativeMaterials) throw Error("此 Agent 尚未接入全部所选材料；整份未放入。");
          refs = window.RabbitNativeMaterials.refs(value.material_refs);
          if (refs.length !== materials.length || materials.some(item => !refs.some(ref => ref.kind === item.kind && ref.id === item.id && (item.sha256 === undefined || ref.sha256 === item.sha256) && ref.source_chat_id === item.source_chat_id))) throw Error("材料回执与选择不一致；整份未放入。");
          materialPreview = window.RabbitNativeMaterials.preview(refs, value.material_preview);
        } else if (value.material_refs !== undefined && (!Array.isArray(value.material_refs) || value.material_refs.length)) throw Error("没有选择的材料不会放入草稿。");
        if (nativeSource || materials.length || memory.preview) {
          if (!window.crypto?.subtle) throw Error("当前浏览器不能核对交接文字；整份未放入。");
          const digest = [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value.prompt)))].map(byte => byte.toString(16).padStart(2, "0")).join("");
          if (!sameScope(stamp) || version !== selectionVersion) return false;
          if (digest !== value.sha256) throw Error("交接文字与预览指纹不一致；整份未放入。");
        }
        preview = { prompt: value.prompt, sha256: value.sha256, material_refs: refs, material_preview: materialPreview, targetAgent }; fullText.textContent = value.prompt; previewDetails.hidden = false;
        previewMaterials.replaceChildren();
        // File bodies stay outside the bounded handoff text and are shown in full.
        if (materialPreview) {
          const extra = node("div", "native-handoff-materials");
          for (const image of materialPreview.images) { const detail = node("details", "rbh-details"); detail.append(node("summary", "", "原图：" + image.name + " · " + image.width + "×" + image.height)); const body = node("div"); detail.append(body); detail.addEventListener("toggle", async () => { if (!detail.open || body.children.length || !materialImages?.image) return; try { const data = await materialImages.image(image.id, true); if (!sameScope(stamp) || version !== selectionVersion) return; const img = node("img", "material-original"); img.src = data.data_url; img.alt = image.name; body.append(img); } catch (_) { if (sameScope(stamp)) body.textContent = "原图暂时无法显示，材料引用仍保留；不会用缩略图代替原图。"; } }); extra.append(detail); }
          for (const file of materialPreview.files) { const detail = node("details", "rbh-details"); detail.append(node("summary", "", "完整文件：" + file.name + " · " + file.size_bytes + " 字节"), node("pre", "file-task-preview-content", file.text)); extra.append(detail); }
          previewMaterials.append(extra);
        }
        const imageCount = fileSource ? "" : value.excluded.image_count_unknown ? `图片至少 ${value.excluded.images} 项，部分数量未明` : `${value.excluded.images} 张图片`;
        previewCounts.textContent = fileSource ? `${value.size_bytes}/131072 字节任务文字，另附 ${materialPreview.files[0].size_bytes} 字节完整文件。文件不解析为对话角色，不带入其他附件或记忆；固定副本不随磁盘后续编辑变化。` : indexedSource() ? `${value.record_count} 条已读历史索引文字 · ${value.characters} 字符 · ${value.size_bytes}/131072 字节。所选索引文字完整保留，可能已脱敏；不代表原始字节、完整会话或原生现场，不另外读取附件或记忆库。` : nativeSource ? `${value.record_count} 条文字片段 · ${value.characters} 字符 · ${value.size_bytes}/131072 字节。只含所选原文和来源，其中已有的材料文字保持原样；不另外读取附件、记忆库或工具过程，不迁移Agent内部状态。` : `${value.record_count} 条文字 · ${value.characters} 字符 · ${value.size_bytes}/131072 字节。${refs.length ? `随附 ${materialPreview.images.length} 张原图、${materialPreview.files.length} 份完整文本文件。` : ""}${memory.preview ? `带入 ${memory.preview.references.length} 条历史记忆引用${memory.preview.habits ? "及一份历史回答习惯" : ""}，另有 ${memory.preview.excluded_references} 条未选引用。` : "未带入历史记忆与回答习惯。"}未附 ${imageCount}、${value.excluded.files} 份文本文件；未选资料、当前记忆库和 Agent 内部状态均不随附。`;
        notice = "完整接力文字已准备。核对后选择一段原生对话，再放入草稿。"; return true;
      } catch (error) { if (sameScope(stamp) && version === selectionVersion) fail(error); return false; }
      finally { if (stamp === generation && version === selectionVersion) { preparing = false; render(); } }
    }
    async function insert() {
      render(); if (insertButton.disabled) return false;
      const stamp = generation, version = selectionVersion, value = { agent: agent.value, chatId: chat.value, workspaceId: captured.workspace_id, text: preview.prompt,
        ...(preview.material_refs.length ? { material_refs: preview.material_refs, material_preview: preview.material_preview } : {}) };
      placing = true; notice = ""; render();
      try {
        if (!sameScope(stamp) || !targetAvailable() || preview.targetAgent !== agent.value) return false;
        const placed = indexedSource() || fileSource
          ? await placeDraft(value, () => sameScope(stamp) && version === selectionVersion && targetAvailable() && agent.value === value.agent && chat.value === value.chatId)
          : await placeDraft(value);
        if (stamp !== generation || root.hidden) return placed === true;
        if (placed === true) { placing = false; close(); return true; }
        throw Error("目标草稿未接收这份资料。原草稿保留，请核对目标后再试。");
      } catch (error) { if (sameScope(stamp)) fail(error); return false; }
      finally { if (stamp === generation) { placing = false; render(); } }
    }
    async function open() {
      if (placing) return false;
      if (!root.hidden) close();
      const current = getSource();
      if (!object(current) || !id(current.workspace_id) || (fileSource ? current.origin_kind !== "file_material" || !uuid(current.material_id) || !id(current.source_key) : !id(current.chat_id))) { onError(Error(fileSource ? "请先核对并冻结这份完整文件资料。" : nativeSource ? "请先打开一段原生对话，连接并读取文字。" : "请先保存并打开一段共同聊天。")); return false; }
      const available = getTargets();
      if (!Array.isArray(available) || available.some(item => !object(item) || !routes[item.agent] || item.apiRoot !== routes[item.agent] || typeof item.label !== "string") || new Set(available.map(item => item.agent)).size !== available.length) { onError(Error("原生 Agent 入口格式不匹配。")); return false; }
      const stamp = ++generation; captured = { workspace_id: current.workspace_id, task: typeof current.task === "string" ? current.task : "",
        ...(fileSource ? { origin_kind: "file_material", material_id: current.material_id, source_key: current.source_key } : { chat_id: current.chat_id, ...(nativeSource ? { source_key: current.source_key, ...(current.origin_kind === "history_index" ? { origin_kind: "history_index" } : {}) } : {}) }) };
      source = preview = fileSnapshot = null; records = chats = []; selected = new Set(); materialSelections = new Map(); memorySelections = new Map(); habitsMessageId = null; targets = available.filter(item => !fileSource || materialAgent(item.agent)).map(item => ({ agent: item.agent, label: item.label, apiRoot: item.apiRoot }));
      reading = true; preparing = placing = false; notice = catalogueError = ""; returnFocus = document.activeElement; root.hidden = false;
      const shell = document.querySelector(".app-shell"); if (shell) shell.inert = true; makePanel(); closeButton.focus();
      const loadingChats = loadChats();
      try {
        const value = nativeSource ? await sourceAdapter.read(captured) : await api(`/api/chats/${encodeURIComponent(captured.chat_id)}/native-handoff`);
        if (!sameScope(stamp)) return false;
        if (fileSource) {
          if (!window.RabbitNativeFileQuote?.check) throw Error("完整文件资料接口尚未加载，未放入草稿。");
          const checked = await window.RabbitNativeFileQuote.check(value);
          if (!sameScope(stamp)) return false;
          if (checked.workspace_id !== captured.workspace_id || checked.source.material_id !== captured.material_id) throw Error("文件资料来源或工作区与当前预览不一致，原草稿保留。");
          fileSnapshot = checked; source = { ...checked.source }; collection.append(node("pre", "file-task-preview-content", checked.material_preview.files[0].text)); render(); return true;
        }
        if (nativeSource && (captured.origin_kind === "history_index") !== (value?.source?.origin_kind === "history_index")) throw Error("文字来源类型与当前页面不一致，原草稿保留。");
        if (!object(value) || value.workspace_id !== captured.workspace_id || !object(value.source) || value.source.chat_id !== captured.chat_id || typeof value.source.title !== "string" || typeof value.source.revision !== "string" || !value.source.revision || value.max_records !== 50 || value.max_bytes !== 131072 || !Array.isArray(value.records) || !value.records.every(record => object(record) && id(record.message_id) && ["user", "assistant"].includes(record.role) && typeof record.content === "string" && (record.source_ai === null || record.source_ai === undefined || typeof record.source_ai === "string") && (record.source_kind === undefined || typeof record.source_kind === "string") && integer(record.excluded_images) && integer(record.excluded_files) && typeof record.image_count_unknown === "boolean") || new Set(value.records.map(record => record.message_id)).size !== value.records.length) throw Error("共同聊天资料格式或版本不匹配；没有选取或裁剪任何消息。");
        sourceIncomplete = value.incomplete === true; source = { ...value.source }; records = value.records.map(record => {
          const memory = record.memory_candidates === undefined ? {} : { memory_candidates: memoryCandidate(record.memory_candidates) };
          if (record.memory_unavailable !== undefined && record.memory_unavailable !== true || record.memory_unavailable && memory.memory_candidates) throw Error("历史记忆状态与候选不一致，原草稿保留。");
          if (record.material_candidates === undefined) return { ...record, ...memory, material_candidates: [] };
          if (!Array.isArray(record.material_candidates) || record.material_candidates.length > 512 || !window.RabbitNativeMaterials) throw Error("材料候选列表不完整，原草稿保留。");
          const candidates = record.material_candidates.map(window.RabbitNativeMaterials.candidate);
          if (new Set(candidates.map(item => item.kind + ":" + item.id)).size !== candidates.length) throw Error("材料候选重复，原草稿保留。");
          return { ...record, ...memory, material_candidates: candidates };
        }); selected = new Set(nativeSource ? [] : records.slice(-20).map(record => record.message_id)); renderRecords();
        return true;
      } catch (error) { if (sameScope(stamp)) fail(error); return false; }
      finally { if (stamp === generation) { reading = false; render(); } await loadingChats; }
    }
    root.addEventListener("click", event => { if (event.target === root) close(); });
    root.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      if (event.key !== "Tab" || !panel) return;
      const focusable = [...panel.querySelectorAll("button, input, select, textarea, summary")].filter(item => !item.disabled && !item.hidden && !item.closest("[hidden]"));
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    return { open, close };
  }
  window.RabbitNativeHandoff = Object.freeze({ create });
})();
