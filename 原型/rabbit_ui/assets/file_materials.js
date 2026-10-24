(() => {
  "use strict";
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  const limit = 524288;
  function artifactOrigin(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join(",") !== "artifact_id,chat_id,run_id,sha256"
        || typeof value.chat_id !== "string" || !uuid.test(value.chat_id)
        || !uuid.test(value.run_id) || !uuid.test(value.artifact_id) || typeof value.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256)) throw new Error("成果副本来源不完整，原草稿已保留。");
    return { chat_id: value.chat_id, run_id: value.run_id, artifact_id: value.artifact_id, sha256: value.sha256 };
  }
  function markdownOrigin(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join(",") !== "binding_id,head_id,kind,relative_path,sha256"
        || value.kind !== "markdown_preview" || !uuid.test(value.binding_id) || !uuid.test(value.head_id)
        || typeof value.relative_path !== "string" || !value.relative_path || value.relative_path.length > 8192 || value.relative_path.startsWith("/")
        || value.relative_path.split("/").some(part => !part || part === "." || part === "..") || /[\x00-\x1f\x7f\ud800-\udfff]/u.test(value.relative_path)
        || !/\.md$/i.test(value.relative_path) || typeof value.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256)) throw new Error("Markdown资料来源不完整，原文件与草稿保留。");
    return { kind: value.kind, binding_id: value.binding_id, head_id: value.head_id, relative_path: value.relative_path, sha256: value.sha256 };
  }
  function material(value) {
    const checked = window.RabbitFileTasks.artifact(value);
    if (typeof value.available !== "boolean" || typeof value.content_verified !== "boolean") throw new Error("文件输入可读状态不完整，原草稿已保留。");
    if (!["upload", "project_preview"].includes(value.source) || checked.path.includes("/")) throw new Error("文件输入信息不完整，原草稿已保留。");
    const result = { ...checked, source: value.source };
    if (value.provenance?.from_markdown !== undefined) {
      if (value.source !== "upload" || value.provenance.kind !== "upload" || value.provenance.from_artifact !== undefined || Object.keys(value.provenance).sort().join(",") !== "from_markdown,kind") throw new Error("Markdown资料来源不完整，原草稿已保留。");
      const origin = markdownOrigin(value.provenance.from_markdown);
      if (origin.sha256 !== checked.sha256 || origin.relative_path.split("/").at(-1) !== checked.name) throw new Error("Markdown资料版本或名称不一致，原草稿已保留。");
      result.provenance = { kind: "upload", from_markdown: origin };
    }
    if (value.provenance?.from_artifact !== undefined) {
      if (value.source !== "upload" || value.provenance.kind !== "upload") throw new Error("成果副本来源不完整，原草稿已保留。");
      const origin = artifactOrigin(value.provenance.from_artifact);
      if (origin.sha256 !== checked.sha256) throw new Error("成果副本版本不一致，原草稿已保留。");
      result.provenance = { kind: "upload", from_artifact: origin };
    }
    return result;
  }
  function create({ api, root, getWorkspace = () => "", getView = () => "", onSelect = () => {}, onChange = () => {}, onError = () => {} }) {
    let version = 0, focusBefore = null, loadedWorkspace = null, pending = new Map(), recovering = null, notice = "", journalReadable = true;
    const known = new Map(), inFlight = new Set();
    root.hidden = true;
    function key() { const id = getWorkspace(); if (typeof id !== "string" || !/^[a-f0-9]{64}$/.test(id)) throw new Error("先连接这台电脑，再添加文件输入。"); return `rabbit.file-materials.v1:${id}`; }
    function journal(clientId, remove = false) {
      try {
        const storage = window.localStorage, storageKey = `${key()}:${clientId}`;
        if (remove) { storage.removeItem(storageKey); if (storage.getItem(storageKey) !== null) throw Error(); return; }
        const row = pending.get(clientId); if (!row) throw Error();
        const wire = JSON.stringify({ schema_version: 1, client_id: row.client_id, name: row.name, source: row.source, phase: row.phase || "unconfirmed", ...(row.sha256 ? { sha256: row.sha256, size_bytes: row.size_bytes } : {}), ...(row.from_artifact ? { from_artifact: row.from_artifact, size_bytes: row.size_bytes } : {}), ...(row.from_markdown ? { from_markdown: row.from_markdown, size_bytes: row.size_bytes } : {}) });
        storage.setItem(storageKey, wire); if (storage.getItem(storageKey) !== wire) throw Error();
      } catch (_) { throw new Error("浏览器暂时不能保留文件上传回执，请恢复浏览器存储后再添加。原草稿和文件保留。"); }
    }
    function loadJournal() {
      const id = getWorkspace();
      journalReadable = true;
      if (id !== loadedWorkspace) { loadedWorkspace = id; pending = new Map(); known.clear(); notice = ""; }
      if (typeof id !== "string" || !/^[a-f0-9]{64}$/.test(id)) return;
      try {
        const prefix = `${key()}:`, storage = window.localStorage, seen = new Set();
        for (let index = 0; index < storage.length; index++) {
          const storageKey = storage.key(index); if (!storageKey?.startsWith(prefix)) continue;
          const data = JSON.parse(storage.getItem(storageKey));
          if (!data || data.schema_version !== 1 || !uuid.test(data.client_id) || storageKey !== prefix + data.client_id || typeof data.name !== "string" || data.name.length > 240 || !["upload", "project_preview"].includes(data.source) || !["unconfirmed", "publishing"].includes(data.phase)) throw Error();
          if (seen.size >= 100) throw Error(); seen.add(data.client_id);
          const origin = data.from_artifact === undefined ? null : artifactOrigin(data.from_artifact);
          const markdown = data.from_markdown === undefined ? null : markdownOrigin(data.from_markdown);
          const upload = data.sha256 === undefined ? null : data.sha256;
          if (origin && (data.source !== "upload" || !Number.isSafeInteger(data.size_bytes) || data.size_bytes < 0 || data.size_bytes > limit)) throw Error();
          if (markdown && (origin || data.source !== "upload" || markdown.relative_path.split("/").at(-1) !== data.name || !Number.isSafeInteger(data.size_bytes) || data.size_bytes < 0 || data.size_bytes > limit)) throw Error();
          if (data.sha256 !== undefined && (typeof upload !== "string" || !/^[a-f0-9]{64}$/.test(upload) || origin || markdown || data.source !== "upload" || !Number.isSafeInteger(data.size_bytes) || data.size_bytes < 0 || data.size_bytes > limit)) throw Error();
          if (!inFlight.has(data.client_id)) pending.set(data.client_id, { client_id: data.client_id, name: data.name, source: data.source, phase: data.phase, ...(upload === null ? {} : { sha256: upload, size_bytes: data.size_bytes }), ...(origin ? { from_artifact: origin, size_bytes: data.size_bytes } : {}), ...(markdown ? { from_markdown: markdown, size_bytes: data.size_bytes } : {}) });
        }
        for (const clientId of pending.keys()) if (!seen.has(clientId) && !inFlight.has(clientId)) pending.delete(clientId);
      } catch (_) { journalReadable = false; notice = "上次文件上传记录暂时无法读取，未自动上传或发送。"; }
    }
    function accept(raw) { loadJournal(); const item = material(raw); known.set(item.id, item); return item; }
    function pendingCount() { loadJournal(); return pending.size; }
    function state() { loadJournal(); return { pending: [...pending.values()].map(row => ({ ...row })), notice, recovering: !!recovering }; }
    function node(tag, className, text) { const result = document.createElement(tag); if (className) result.className = className; if (text !== undefined) result.textContent = text; return result; }
    function action(label, callback) { const result = node("button", "file-task-action", label); result.type = "button"; result.addEventListener("click", callback); return result; }
    function close() { const wasOpen = !root.hidden; ++version; root.hidden = true; root.replaceChildren(); if (wasOpen) { const shell = document.querySelector(".app-shell"); if (shell) shell.inert = false; focusBefore?.focus?.(); } }
    async function inspect(id) {
      if (typeof id !== "string" || !uuid.test(id)) throw new Error("文件输入标识不完整，原草稿已保留。");
      const data = await api(`/api/file-materials/${id}`); return accept(data.material);
    }
    async function read(expected) {
      expected = material(expected);
      if (!expected.available || !expected.content_verified) throw new Error("文件输入暂不可读取，原草稿与引用仍保留。");
      const data = await api(`/api/file-materials/${expected.id}/content`), actual = material(data.artifact);
      if (JSON.stringify(actual) !== JSON.stringify(expected) || !actual.available || !actual.content_verified || typeof data.text !== "string" || new Blob([data.text]).size !== actual.size_bytes) throw new Error("文件输入内容与保存记录不一致，未使用这份文件。");
      accept(actual); return { artifact: actual, text: data.text };
    }
    async function recoverOne(clientId) {
      loadJournal(); if (!pending.has(clientId)) throw new Error("原上传回执不存在，未再次上传。");
      const workspace = loadedWorkspace, expected = pending.get(clientId);
      const data = await api(`/api/file-material-receipts?${new URLSearchParams({ client_id: clientId })}`);
      if (workspace !== getWorkspace() || workspace !== loadedWorkspace) throw new Error("设备入口已变化，请从原入口核对上传回执；未再次上传。");
      if (data.received !== true) { if (data.received !== false) throw new Error("上传回执不完整，未再次上传。"); notice = "还没有查到上传回执。请稍后再确认；原草稿保留，不会自动重传。"; onChange(); return null; }
      const item = checkedReceipt(data.material, expected); known.set(item.id, item);
      if (!item.available || !item.content_verified || data.state === "publishing") { pending.get(clientId).phase = "publishing"; journal(clientId); notice = "文件输入仍在保存核对中，请稍后查询原回执；未重传。"; onChange(); return null; }
      journal(clientId, true); pending.delete(clientId); notice = "已找到上次上传的文件输入，可从‘已添加的文件’放入当前草稿。尚未发送。"; onChange(); return item;
    }
    async function recover() {
      loadJournal(); if (recovering) return recovering;
      const captured = loadedWorkspace;
      recovering = (async () => { for (const id of [...pending.keys()]) { if (captured !== getWorkspace()) return; if (inFlight.has(id)) continue; try { await recoverOne(id); } catch (_) { notice = "上传结果暂时不能确认。请稍后查询原回执，未重传文件。"; } } onChange(); })();
      onChange();
      try { await recovering; } finally { recovering = null; onChange(); }
    }
    function checkedReceipt(raw, expected) {
      const item = material(raw);
      if (expected.sha256 && (item.source !== expected.source || item.name !== expected.name || item.size_bytes !== expected.size_bytes || item.sha256 !== expected.sha256)) throw new Error("文件上传回执与所选原文件版本不一致；原编号保留，请核对原上传记录，没有再次上传。");
      if (expected.from_artifact && (item.source !== "upload" || item.id === expected.from_artifact.artifact_id || item.name !== expected.name || item.size_bytes !== expected.size_bytes || JSON.stringify(item.provenance?.from_artifact) !== JSON.stringify(expected.from_artifact))) throw new Error("成果副本回执与所选原件不一致。原草稿保留，请查询原回执；未再次导入。");
      if (expected.from_markdown && (item.source !== "upload" || item.name !== expected.name || item.size_bytes !== expected.size_bytes || JSON.stringify(item.provenance?.from_markdown) !== JSON.stringify(expected.from_markdown))) throw new Error("Markdown资料回执与所选原文件版本不一致；原编号保留，没有再次导入。");
      return item;
    }
    async function post(path, body, name, source, fromArtifact = null, sizeBytes = null, fromMarkdown = null, uploadSha = null) {
      loadJournal(); const workspace = loadedWorkspace;
      if (!journalReadable) throw new Error("原上传记录暂不可核对，没有再次添加文件。原草稿和文件保留。");
      if (pending.size >= 100) throw new Error("未确认的文件上传较多，请先确认原回执再添加。原草稿保留。");
      const client_id = crypto.randomUUID(), expected = { client_id, name, source, phase: "unconfirmed", ...(uploadSha ? { sha256: uploadSha, size_bytes: sizeBytes } : {}), ...(fromArtifact ? { from_artifact: artifactOrigin(fromArtifact), size_bytes: sizeBytes } : {}), ...(fromMarkdown ? { from_markdown: markdownOrigin(fromMarkdown), size_bytes: sizeBytes } : {}) }; pending.set(client_id, expected);
      try { journal(client_id); } catch (error) { pending.delete(client_id); throw error; }
      inFlight.add(client_id);
      onChange();
      try {
        const data = await api(path, { method: "POST", body: JSON.stringify({ client_id, ...body }) }, false);
        if (workspace !== getWorkspace()) throw new Error("设备入口已变化，文件仍可通过原入口确认回执。");
        const item = checkedReceipt(data.material, expected); known.set(item.id, item);
        if (!item.available || !item.content_verified || data.state === "publishing") { pending.get(client_id).phase = "publishing"; journal(client_id); throw new Error("文件输入仍在保存核对中，请稍后确认原回执，未重传。"); }
        journal(client_id, true); pending.delete(client_id); notice = "文件材料已保留在本机。"; onChange(); return item;
      } catch (error) {
        if (Number.isInteger(error.status) && error.status >= 400 && error.status < 500 && error.status !== 408) { journal(client_id, true); pending.delete(client_id); }
        else { notice = "上传结果还未确认。只会查询原回执，不会自动重传；原草稿保留。"; }
        onChange(); throw error;
      } finally { inFlight.delete(client_id); }
    }
    async function upload(file) {
      if (!file || typeof file.name !== "string" || !file.name || file.name.length > 240 || /[\\/\x00-\x1f\x7f]/.test(file.name) || file.name.startsWith(".") || !/\.(?:md|txt)$/i.test(file.name)) throw new Error("请选择 Markdown（.md）或文本（.txt）文件。原草稿保留。");
      if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > limit) throw new Error("一份文件最多 512 KiB，整份未添加，原草稿保留。");
      const workspace = getWorkspace(), bytes = new Uint8Array(await file.arrayBuffer()); if (bytes.byteLength !== file.size || bytes.byteLength > limit) throw new Error("文件大小已变化，整份未添加，请重新选择。");
      try { new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch (_) { throw new Error("当前只支持 UTF-8 文本，请先将文件保存为 UTF-8。原草稿保留。"); }
      if (!crypto?.subtle) throw new Error("当前浏览器不能核对完整文件版本，原草稿和文件保留。");
      const sha256 = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(byte => byte.toString(16).padStart(2, "0")).join("");
      if (workspace !== getWorkspace()) throw new Error("设备入口已变化，文件尚未上传；请在当前入口重新选择。");
      let raw = ""; for (let offset = 0; offset < bytes.length; offset += 8192) raw += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      // Encode original bytes: no BOM removal, line-ending conversion or truncation.
      return post("/api/file-materials", { name: file.name, data_base64: btoa(raw) }, file.name, "upload", null, bytes.byteLength, null, sha256);
    }
    async function project(ticket, name) {
      if (typeof ticket !== "string" || !ticket || ticket.length > 4096 || typeof name !== "string" || !/\.(?:md|txt)$/i.test(name)) throw new Error("所见文件的输入凭证暂不可用，请重新预览后再明确选择。原预览仍保留。");
      return post("/api/file-materials/project", { ticket }, name, "project_preview");
    }
    async function markdown(ticket, name, origin, sizeBytes) {
      origin = markdownOrigin(origin);
      if (typeof ticket !== "string" || !/^[a-f0-9]{64}$/.test(ticket) || typeof name !== "string" || name !== origin.relative_path.split("/").at(-1) || !Number.isSafeInteger(sizeBytes) || sizeBytes < 0 || sizeBytes > limit) throw new Error("所见Markdown文件凭证、名称或大小不一致，请重新预览；原文件仍保留。");
      loadJournal();
      if (!journalReadable) throw new Error("原文件导入记录暂时无法核对，未创建新导入。");
      if ([...pending.values()].some(row => JSON.stringify(row.from_markdown) === JSON.stringify(origin))) throw new Error("这份Markdown已有未确认导入，请核对原回执；没有新建编号或再次导入。");
      return post("/api/file-materials/markdown", { ticket }, name, "upload", null, sizeBytes, origin);
    }
    async function recoverMarkdown(origin) {
      origin = markdownOrigin(origin); loadJournal();
      if (!journalReadable) throw new Error("原文件导入记录暂时无法核对，未再次导入。");
      const same = row => JSON.stringify(row.from_markdown || row.provenance?.from_markdown) === JSON.stringify(origin);
      const requests = [...pending.values()].filter(same);
      if (requests.length > 1) throw new Error("这份Markdown有多条未确认记录，请保留原编号逐一核对；没有再次导入。");
      if (requests.length) return inFlight.has(requests[0].client_id) ? null : recoverOne(requests[0].client_id);
      return [...known.values()].find(item => same(item) && item.available && item.content_verified) || null;
    }
    async function fromArtifact(origin, raw) {
      origin = artifactOrigin(origin); const item = window.RabbitFileTasks.artifact(raw);
      if (!item.available || !item.content_verified || item.id !== origin.artifact_id || item.sha256 !== origin.sha256) throw new Error("所选成果版本暂不可读取，原草稿保留。");
      return post("/api/file-materials/artifact", { source_chat_id: origin.chat_id, artifact_id: origin.artifact_id, sha256: origin.sha256 }, item.name, "upload", origin, item.size_bytes);
    }
    async function choose(item, captured = getView()) { const result = await read(item); if (captured !== getView()) return false; return await onSelect(result.artifact, captured); }
    async function preview(expected) {
      const current = ++version, view = getView(); focusBefore = document.activeElement; root.hidden = false; root.replaceChildren(); const shell = document.querySelector(".app-shell"); if (shell) shell.inert = true;
      const panel = node("div", "file-task-preview"), header = node("header", "file-task-preview-header"), title = node("h2", "", expected.name); title.id = "file-material-preview-title";
      const cancel = action("关闭", close), status = node("p", "file-task-note", "正在读取文件输入…"); header.append(title, cancel); panel.append(header, status); root.append(panel); root.setAttribute("aria-labelledby", title.id); cancel.focus();
      try { const result = await read(expected); if (current !== version || root.hidden) return; if (view !== getView()) { close(); return; } status.textContent = `${result.artifact.size_bytes} 字节 · 文件输入的完整原文`; panel.append(node("pre", "file-task-preview-content", result.text));
        const save = action("下载原文", async () => {
          save.disabled = true;
          try { await window.RabbitTextFileDownload.download(result); }
          catch (error) { onError(error); }
          finally { save.disabled = false; }
        }); panel.append(save);
      }
      catch (error) { if (current === version && !root.hidden) { status.textContent = "文件输入暂时没有读出来，原草稿和引用仍保留。"; onError(error); } }
    }
    function card(raw, { removable = false, onRemove = () => {}, disabled = false } = {}) {
      const item = material(raw), result = node("div", "file-task-artifact"); result.append(node("strong", "file-task-name", item.name), node("span", "file-task-note", `${item.size_bytes} 字节 · 文件输入 · ${item.provenance?.from_artifact ? "来自其他对话的成果副本" : item.provenance?.from_markdown ? "来自对话文件夹的固定副本" : item.source === "project_preview" ? "所见项目副本" : "自行添加"}${!item.available || !item.content_verified ? " · 暂不可读取" : ""}`));
      const actions = node("div", "file-task-actions"), open = action("预览输入", () => preview(item)); open.disabled = disabled || !item.available || !item.content_verified; actions.append(open);
      if (removable) { const remove = action("移除", onRemove); remove.disabled = disabled; actions.append(remove); }
      result.append(actions); return result;
    }
    async function list() {
      const current = ++version, view = getView(); focusBefore = document.activeElement; root.hidden = false; root.replaceChildren(); const shell = document.querySelector(".app-shell"); if (shell) shell.inert = true;
      const panel = node("div", "file-task-preview"), header = node("header", "file-task-preview-header"), title = node("h2", "", "已添加的文件"); title.id = "file-material-preview-title";
      const cancel = action("关闭", close), status = node("p", "file-task-note", "正在读取已添加的文件…"); header.append(title, cancel); panel.append(header, status); root.append(panel); root.setAttribute("aria-labelledby", title.id); cancel.focus();
      try { await recover(); const data = await api("/api/file-materials"); if (current !== version || root.hidden || view !== getView()) return;
        if (!Array.isArray(data.materials) || data.materials.length > 100) throw new Error("文件输入列表不完整，请稍后重新打开。");
        const items = data.materials.map(accept); status.textContent = items.length ? "选择你希望 AI 读取的文件。只放入草稿，写下要求后再发送。" : "还没有添加文件输入。可从‘更多 → 添加文件’选择 .md 或 .txt。";
        for (const item of items) { const row = card(item), select = action("放入本轮", async () => { select.disabled = true; try { if (await choose(item, view)) close(); } catch (error) { onError(error); } finally { select.disabled = false; } }); select.disabled = !item.available || !item.content_verified; row.append(select); panel.append(row); }
      } catch (error) { if (current === version && !root.hidden) { status.textContent = "文件输入列表暂时没有读出来，原草稿仍保留。"; onError(error); } }
    }
    root.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); close(); } if (event.key === "Tab") { const buttons = [...root.querySelectorAll("button")].filter(item => !item.disabled), first = buttons[0], last = buttons.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } } });
    return { material, accept, known, inspect, read, upload, project, markdown, recoverMarkdown, fromArtifact, choose, preview, card, list, close, recover, recoverOne, pendingCount, state };
  }
  window.RabbitFileMaterials = { material, artifactOrigin, markdownOrigin, create };
})();
