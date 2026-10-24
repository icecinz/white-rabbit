(() => {
  "use strict";
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  const states = new Set(["prepared", "publishing", "verified", "conflict", "blocked"]);
  const limit = 524288;
  function relativePath(value) {
    if (typeof value !== "string" || !value || value.length > 240 || value.startsWith("/") || value.includes("\\") || /[\x00-\x1f\x7f]/.test(value) || value.split("/").some(part => !part || part === "." || part === ".." || part.startsWith(".")) || !/\.(md|txt)$/i.test(value)) throw new Error("请填写项目内的 .md 或 .txt 相对路径；已有目录可以使用。");
    return value;
  }
  function identifier(value) { return typeof value === "string" && !!value && value.length <= 128 && !/[\\/\x00-\x1f\x7f]/.test(value); }
  function publication(value) {
    const item = value?.artifact;
    if (!value || !uuid.test(value.id) || typeof value.project_name !== "string" || !value.project_name || value.project_name.length > 240 || !states.has(value.state) || typeof value.current_verified !== "boolean" || typeof value.created_at !== "string" || typeof value.updated_at !== "string" || value.error_code != null && (typeof value.error_code !== "string" || value.error_code.length > 100) || !item || !uuid.test(item.id) || !identifier(item.chat_id) || !identifier(item.run_id) || typeof item.name !== "string" || !item.name || item.name.length > 240 || /[\\/\x00-\x1f\x7f]/.test(item.name) || !/\.(md|txt)$/i.test(item.name) || !/^[a-f0-9]{64}$/.test(item.sha256) || !Number.isSafeInteger(item.size_bytes) || item.size_bytes < 0 || item.size_bytes > limit) throw new Error("另存记录不完整，未再次提交。原成果仍保留。");
    return { id: value.id, project_name: value.project_name, target_path: relativePath(value.target_path), artifact: { id: item.id, chat_id: item.chat_id, run_id: item.run_id, name: item.name, sha256: item.sha256, size_bytes: item.size_bytes }, state: value.state, created_at: value.created_at, updated_at: value.updated_at, current_verified: value.current_verified, error_code: value.error_code || null };
  }
  function create({ api, root, getWorkspace = () => "", getView = () => "", getCapabilities = () => ({}), refreshConnection = async () => {}, onChange = () => {}, onError = () => {} }) {
    let version = 0, focused = null, modal = null, loadedWorkspace = null, pending = new Map(), recovering = null, journalInvalid = false;
    const known = new Map(), inFlight = new Set(), missingReceipts = new Set();
    root.hidden = true;
    function node(tag, className, text) { const value = document.createElement(tag); if (className) value.className = className; if (text !== undefined) value.textContent = text; return value; }
    function action(label, callback) { const value = node("button", "file-task-action", label); value.type = "button"; value.addEventListener("click", callback); return value; }
    function prefix() { const id = getWorkspace(); if (typeof id !== "string" || !/^[a-f0-9]{64}$/.test(id)) throw new Error("先连接这台电脑，再另存成果。"); return `rabbit.project-publication.v1:${id}:`; }
    function loadJournal() {
      const workspace = getWorkspace(); if (workspace !== loadedWorkspace) { loadedWorkspace = workspace; pending = new Map(); known.clear(); missingReceipts.clear(); journalInvalid = false; }
      if (modal && modal.workspace !== workspace) close();
      if (typeof workspace !== "string" || !/^[a-f0-9]{64}$/.test(workspace)) return;
      try {
        const storage = window.localStorage, start = prefix(), seen = new Set();
        for (let index = 0; index < storage.length; index++) {
          const key = storage.key(index); if (!key?.startsWith(start)) continue;
          const row = JSON.parse(storage.getItem(key));
          if (!row || row.schema_version !== 1 || !uuid.test(row.client_id) || key !== start + row.client_id || !uuid.test(row.publication_id) || !uuid.test(row.artifact_id) || !identifier(row.chat_id) || !["unconfirmed", "publishing"].includes(row.phase) || seen.size >= 100) throw Error();
          relativePath(row.target_path); seen.add(row.client_id); if (!inFlight.has(row.client_id)) pending.set(row.client_id, row);
        }
        for (const id of pending.keys()) if (!seen.has(id) && !inFlight.has(id)) pending.delete(id);
        journalInvalid = false;
      } catch (_) { if (!journalInvalid) onError(new Error("上次另存回执暂时不能读取，未自动再次提交。")); journalInvalid = true; }
    }
    function journal(row, remove = false) {
      try {
        const storage = window.localStorage, key = prefix() + row.client_id;
        if (remove) { storage.removeItem(key); if (storage.getItem(key) !== null) throw Error(); return; }
        const wire = JSON.stringify({ schema_version: 1, client_id: row.client_id, publication_id: row.publication_id, artifact_id: row.artifact_id, chat_id: row.chat_id, target_path: row.target_path, phase: row.phase });
        storage.setItem(key, wire); if (storage.getItem(key) !== wire) throw Error();
      } catch (_) { throw new Error("浏览器不能保存另存回执，尚未提交。原成果和草稿保留。"); }
    }
    function capabilities() {
      const value = getCapabilities();
      return value?.enabled === true && typeof value.project_name === "string" && !!value.project_name && typeof value.project_root === "string" && value.project_root.startsWith("/") && value.max_bytes === limit && Array.isArray(value.extensions) && value.extensions.some(ext => ["md", ".md"].includes(ext)) ? value : null;
    }
    function enabled() { return !!capabilities(); }
    function accept(raw) { loadJournal(); const item = publication(raw); known.set(item.id, item); return item; }
    function acceptChat(chatId, rows) {
      loadJournal(); if (!Array.isArray(rows)) return;
      const items = rows.map(publication); if (items.some(item => item.artifact.chat_id !== chatId || item.state === "prepared")) throw new Error("另存记录所属的聊天不一致，原成果保留。");
      for (const [id, item] of known) if (item.artifact.chat_id === chatId && item.state !== "prepared" && !items.some(row => row.id === id)) known.delete(id);
      for (const item of items) known.set(item.id, item);
    }
    function summary(chatId) { loadJournal(); return [enabled(), getCapabilities()?.notice || "", [...known.values()].filter(item => item.artifact.chat_id === chatId && item.state !== "prepared"), [...pending.values()].filter(item => item.chat_id === chatId).map(row => ({ ...row, receipt_missing: missingReceipts.has(row.client_id), in_flight: inFlight.has(row.client_id) }))]; }
    function close() {
      const previous = modal, wasOpen = !root.hidden; ++version; modal = null; root.hidden = true; root.replaceChildren();
      if (wasOpen) { const shell = document.querySelector(".app-shell"); if (shell) shell.inert = false; if (previous?.workspace === getWorkspace() && previous?.view === getView()) focused?.focus?.(); }
    }
    function stillCurrent(current, requestVersion) { return modal === current && !root.hidden && version === requestVersion && current.workspace === getWorkspace() && current.view === getView(); }
    function matches(row, item) { return item.id === row.publication_id && item.artifact.id === row.artifact_id && item.artifact.chat_id === row.chat_id && item.target_path === row.target_path; }
    function resultText(item) {
      if (item.state === "verified") return item.current_verified ? `已另存到 ${item.project_name}：${item.target_path}。上次核验原文一致，刷新可重新核对。` : `曾另存：${item.target_path}。当前文件已变化或无法核对；未恢复旧内容。`;
      if (item.state === "publishing") return `已接收另存请求，正在核对：${item.target_path}。尚未确认保存成功。`;
      if (item.state === "conflict") return `未另存：${item.target_path}。目标已有文件或已变化，请选择其他新文件名。`;
      if (item.state === "blocked") return `未另存：${item.target_path}。目标目录或文件暂不可用，原成果仍保留。`;
      return "目标已检查，尚未提交。";
    }
    function acceptReceipt(row, data) {
      if (data.received !== true) throw new Error("另存结果尚未确认，未再次提交。");
      const item = publication(data.publication); if (!matches(row, item) || data.state !== item.state || item.state === "prepared") throw new Error("另存回执与原请求不一致，未再次提交。");
      accept(item); missingReceipts.delete(row.client_id);
      if (item.state === "publishing") { row.phase = "publishing"; journal(row); }
      else { journal(row, true); pending.delete(row.client_id); }
      if (modal?.rowClient === row.client_id && modal.workspace === getWorkspace() && modal.view === getView()) { modal.lastPublication = item.id; modal.note.textContent = resultText(item); renderRecoveryActions(modal); }
      onChange(); return item;
    }
    async function recoverOne(clientId) {
      loadJournal(); const row = pending.get(clientId); if (!row) return null;
      const workspace = loadedWorkspace, data = await api(`/api/project-publications/receipt?${new URLSearchParams({ client_id: clientId })}`);
      if (workspace !== getWorkspace()) { if (modal?.workspace === workspace) close(); return null; }
      if (data.received === false) { missingReceipts.add(clientId); if (modal?.rowClient === clientId) renderRecoveryActions(modal); onChange(); return null; }
      return acceptReceipt(row, data);
    }
    async function recover() {
      loadJournal(); if (recovering) return recovering; if (!pending.size) return;
      const workspace = loadedWorkspace;
      recovering = (async () => { for (const id of [...pending.keys()]) { if (workspace !== getWorkspace()) return; if (inFlight.has(id)) continue; try { await recoverOne(id); } catch (error) { onError(error); } } })();
      try { await recovering; } finally { recovering = null; onChange(); }
    }
    async function inspect(id) { const workspace = getWorkspace(); const data = await api(`/api/project-publications/${id}`); if (workspace !== getWorkspace()) return null; const item = publication(data.publication); if (item.id !== id) throw new Error("另存记录不一致，未再次提交。"); accept(item); onChange(); return item; }
    function appendStatus(parent, chatId, artifact) {
      loadJournal();
      const capabilityNotice = getCapabilities()?.notice;
      if (typeof capabilityNotice === "string" && capabilityNotice) parent.append(node("p", "file-task-warning", capabilityNotice));
      for (const item of known.values()) if (item.artifact.chat_id === chatId && item.artifact.id === artifact.id && item.state !== "prepared") {
        const row = node("div", "file-task-note"); row.append(node("p", "file-task-note", resultText(item)), action("核对另存状态", async () => { try { await inspect(item.id); } catch (error) { onError(error); } })); parent.append(row);
      }
      for (const row of pending.values()) if (row.chat_id === chatId && row.artifact_id === artifact.id) {
        const note = node("div", "file-task-note"); note.append(node("p", "file-task-note", row.phase === "publishing" ? `另存仍在核对：${row.target_path}。只查询原回执。` : `另存结果尚未确认：${row.target_path}。只查询原回执，未再次提交。`));
        const query = action("查询原另存回执", async () => { query.disabled = true; try { const item = await recoverOne(row.client_id); if (!item) onError(new Error("还未查到原另存回执；可明确重试原请求，不会换目标或请求编号。")); } catch (error) { onError(error); } finally { query.disabled = false; } }); query.disabled = inFlight.has(row.client_id); note.append(query);
        if (row.phase === "unconfirmed" && missingReceipts.has(row.client_id)) { const retry = action("重试原另存请求", () => retryOriginal(row.client_id)); retry.disabled = inFlight.has(row.client_id) || !enabled(); note.append(retry); }
        parent.append(note);
      }
    }
    async function checkTarget(current) {
      if (modal !== current || current.busy || !enabled() || current.view !== getView() || current.workspace !== getWorkspace()) return;
      let target; try { target = relativePath(current.input.value); } catch (error) { current.note.textContent = error.message; return; }
      const requestVersion = ++version; current.busy = true; current.checked = null; current.contents.replaceChildren(); current.confirm.disabled = true; current.check.disabled = true; current.note.textContent = "正在核对成果和项目里的真实目标…";
      try {
        const data = await api(`/api/chats/${encodeURIComponent(current.chatId)}/files/${current.artifact.id}/project-preview`, { method: "POST", body: JSON.stringify({ target_path: target }) }, false);
        if (!stillCurrent(current, requestVersion) || current.input.value !== target) return;
        const item = publication(data.publication), preview = data.preview;
        if (item.artifact.chat_id !== current.chatId || item.artifact.id !== current.artifact.id || item.artifact.sha256 !== current.artifact.sha256 || item.artifact.size_bytes !== current.artifact.size_bytes || item.target_path !== target || item.project_name !== current.project.project_name || item.state !== "prepared" || !preview || typeof preview.source_text !== "string" || new Blob([preview.source_text]).size !== item.artifact.size_bytes || typeof preview.target_exists !== "boolean" || typeof preview.can_confirm !== "boolean" || preview.target_text != null && typeof preview.target_text !== "string" || preview.target_exists && preview.can_confirm) throw new Error("目标检查信息不完整，尚未另存。原成果仍保留。");
        current.checked = item; current.contents.append(node("h3", "", "将另存的完整成果"), node("pre", "file-task-preview-content", preview.source_text));
        current.contents.append(node("h3", "", preview.target_exists ? "目标已有文件，不能覆盖" : "目标当前不存在"));
        if (typeof preview.target_text === "string") current.contents.append(node("pre", "file-task-preview-content", preview.target_text));
        else if (preview.target_exists) current.contents.append(node("p", "file-task-note", "该目标无法作为支持的文本读取，仍不能覆盖。"));
        current.note.textContent = preview.can_confirm ? "检查已完成。确认后将创建这个新文件；已有文件不会被覆盖。" : "这个目标不能另存，请改用其他新文件名，再检查目标。";
        current.confirm.disabled = !preview.can_confirm;
      } catch (error) { if (stillCurrent(current, requestVersion)) { current.note.textContent = error.message || "目标暂时无法检查，尚未提交。"; onError(error); } }
      finally { current.busy = false; if (current.workspace !== getWorkspace()) { if (modal === current) close(); } else if (modal === current) current.check.disabled = false; }
    }
    function renderRecoveryActions(current) {
      if (modal !== current) return;
      if (current.workspace !== getWorkspace()) { close(); return; }
      if (current.view !== getView()) return;
      current.recovery.replaceChildren(); const row = pending.get(current.rowClient);
      if (row) {
        const query = action("查询原另存回执", async () => { query.disabled = true; try { const result = await recoverOne(row.client_id); if (modal === current && current.workspace === getWorkspace() && current.view === getView()) { current.note.textContent = result ? resultText(result) : "尚未查到原回执；可明确重试原请求，或稍后再查询。"; renderRecoveryActions(current); } } catch (error) { if (modal === current && current.workspace === getWorkspace() && current.view === getView()) onError(error); } finally { query.disabled = false; } }); query.disabled = inFlight.has(row.client_id); current.recovery.append(query);
        if (row.phase === "unconfirmed" && missingReceipts.has(row.client_id)) { const retry = action("重试原另存请求", () => retryOriginal(row.client_id, current)); retry.disabled = inFlight.has(row.client_id) || !enabled(); current.recovery.append(retry); }
        current.check.disabled = true;
      } else {
        current.check.disabled = current.busy;
        if (current.lastPublication && known.has(current.lastPublication)) current.recovery.append(action("核对另存状态", async () => { try { const result = await inspect(current.lastPublication); if (modal === current && current.workspace === getWorkspace() && current.view === getView() && result) current.note.textContent = resultText(result); } catch (error) { if (modal === current && current.workspace === getWorkspace() && current.view === getView()) onError(error); } }));
      }
    }
    async function sendOriginal(row, workspace, current) {
      try {
        const data = await api(`/api/project-publications/${row.publication_id}/confirm`, { method: "POST", body: JSON.stringify({ client_id: row.client_id }) }, false);
        if (workspace !== getWorkspace()) { if (modal?.workspace === workspace) close(); return; }
        return acceptReceipt(row, data);
      } catch (error) {
        if (workspace !== getWorkspace()) { if (modal?.workspace === workspace) close(); return; }
        const rejected = error.accepted === false;
        if (rejected) { try { journal(row, true); pending.delete(row.client_id); missingReceipts.delete(row.client_id); } catch (storageError) { error = storageError; } if (current) { current.checked = null; current.confirm.disabled = true; } }
        if (current && modal === current && current.view === getView()) { current.note.textContent = error.accepted === false ? "这次另存没有接收，原成果保留。重新检查目标后再决定。" : "结果尚未确认。请查询原另存回执，未自动再次提交。"; onError(error); }
        else if (!current) onError(error);
      }
    }
    async function retryOriginal(clientId, current = null) {
      loadJournal(); const row = pending.get(clientId), workspace = loadedWorkspace, view = getView();
      if (!row || row.phase !== "unconfirmed" || journalInvalid || inFlight.has(clientId) || !enabled() || current && (modal !== current || current.workspace !== workspace || current.view !== getView())) return;
      inFlight.add(clientId); if (current) { current.busy = true; current.input.disabled = true; current.note.textContent = "正在查询原回执；尚未接收时，只重试原目标和原请求编号…"; renderRecoveryActions(current); } onChange();
      try {
        // This is an explicit human retry, always receipt-first. The original
        // publication and client IDs are reused even if the first POST arrives late.
        const received = await recoverOne(clientId);
        if (received || workspace !== getWorkspace() || view !== getView()) return;
        await refreshConnection();
        if (workspace !== getWorkspace() || view !== getView() || !enabled()) { if (modal?.workspace === workspace && workspace !== getWorkspace()) close(); return; }
        if (current && current.view !== getView()) return;
        await sendOriginal(row, workspace, current);
      } catch (error) { if (workspace === getWorkspace() && (!current || modal === current && current.view === getView())) onError(error); }
      finally { inFlight.delete(clientId); if (current) { current.busy = false; if (workspace !== getWorkspace()) { if (modal === current) close(); } else if (modal === current) { current.input.disabled = false; renderRecoveryActions(current); } } onChange(); }
    }
    async function confirm(current) {
      if (modal !== current || current.busy || current.confirm.disabled || !current.checked || current.input.value !== current.checked.target_path || current.view !== getView() || current.workspace !== getWorkspace() || !enabled()) return;
      loadJournal(); if (journalInvalid) { current.note.textContent = "原另存回执暂时不能读取，尚未再次提交。请先恢复浏览器存储。"; return; }
      if ([...pending.values()].some(row => row.chat_id === current.chatId && row.artifact_id === current.artifact.id)) { current.note.textContent = "先查询原另存回执，未再次提交。"; current.confirm.disabled = true; return; }
      const item = current.checked, row = { client_id: crypto.randomUUID(), publication_id: item.id, artifact_id: item.artifact.id, chat_id: current.chatId, target_path: item.target_path, phase: "unconfirmed" };
      try { journal(row); } catch (error) { current.note.textContent = error.message; onError(error); return; }
      pending.set(row.client_id, row); inFlight.add(row.client_id); current.rowClient = row.client_id; current.busy = true; current.input.disabled = true; current.check.disabled = current.confirm.disabled = true; current.note.textContent = "正在另存到项目，关闭窗口也会保留原回执…"; renderRecoveryActions(current); onChange();
      try {
        await sendOriginal(row, current.workspace, current);
      } finally { inFlight.delete(row.client_id); current.busy = false; if (current.workspace !== getWorkspace()) { if (modal === current) close(); } else if (modal === current) { current.input.disabled = false; renderRecoveryActions(current); } onChange(); }
    }
    function open(chatId, raw) {
      loadJournal(); const project = capabilities(), artifact = window.RabbitFileTasks.artifact(raw);
      if (!project || !identifier(chatId) || !artifact.available || !artifact.content_verified) throw new Error("这份成果暂不能另存到项目，原成果仍保留。");
      close(); focused = document.activeElement; root.hidden = false; const shell = document.querySelector(".app-shell"); if (shell) shell.inert = true;
      const panel = node("div", "file-task-preview"), header = node("header", "file-task-preview-header"), title = node("h2", "", "另存成果到项目"); title.id = "project-publication-title"; const cancel = action("关闭", close); header.append(title, cancel); panel.append(header);
      panel.append(node("p", "file-task-note", `项目：${project.project_name}`), node("p", "file-task-note", `实际保存目录：${project.project_root}`), node("p", "file-task-note", "只创建新 .md 或 .txt 文件。先检查完整内容与目标，再确认另存。"));
      const label = node("label", "file-task-note", "项目内目标路径"), input = node("input", "search-input"); input.type = "text"; input.value = artifact.name; input.setAttribute("aria-label", "项目内目标路径"); input.maxLength = 240; input.style.width = "100%"; label.append(input); panel.append(label);
      const note = node("p", "file-task-note", "尚未检查，也未提交。"), contents = node("div"), recovery = node("div", "file-task-actions"), current = { workspace: loadedWorkspace, view: getView(), project, chatId, artifact, input, note, contents, recovery, checked: null, busy: false, rowClient: null, lastPublication: null };
      current.check = action("检查目标", () => checkTarget(current)); current.confirm = action("确认另存", () => confirm(current)); current.confirm.disabled = true; const actions = node("div", "file-task-actions"); actions.append(current.check, current.confirm); panel.append(actions, note, contents, recovery); root.append(panel); modal = current;
      input.addEventListener("input", () => { ++version; current.checked = null; current.confirm.disabled = true; contents.replaceChildren(); note.textContent = "目标已改变，请重新检查；尚未提交这份目标。"; });
      const previous = [...pending.values()].find(row => row.chat_id === chatId && row.artifact_id === artifact.id);
      if (previous) { current.rowClient = previous.client_id; note.textContent = `上次另存结果尚未确认：${previous.target_path}。先查询原回执。`; renderRecoveryActions(current); }
      cancel.focus(); return current;
    }
    root.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); close(); } if (event.key === "Tab") { const buttons = [...root.querySelectorAll("button")], input = root.querySelector("input"), choices = [buttons[0], input, ...buttons.slice(1)].filter(item => item && !item.disabled), first = choices[0], last = choices.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } } });
    return { enabled, open, close, acceptChat, appendStatus, summary, recover, recoverOne, inspect, publication, pendingCount: () => { loadJournal(); return pending.size; } };
  }
  window.RabbitProjectPublish = { relativePath, publication, create };
})();
