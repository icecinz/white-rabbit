/* Real-folder organization. No Agent, transcript editing or automatic refresh. */
(() => {
  "use strict";
  const uuid = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
  const sha = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const agents = new Set(["codex", "pi", "hermes", "claude", "dsh"]);
  const native = value => value?.kind === "native_index" && agents.has(value.agent);
  const reference = value => value?.kind === "history_reference" && agents.has(value.agent);
  const indexed = value => native(value) || reference(value);
  const sessionId = value => typeof value === "string" && Array.from(value).length >= 1 && Array.from(value).length <= 256 && !/[\s\p{Cc}\p{Cf}\p{Cs}]/u.test(value);
  const identity = value => JSON.stringify([indexed(value) ? value.kind : "markdown_chat", indexed(value) ? value.agent : "", value?.chat_id || value?.id || value?.rabbit_id]);
  const sameSource = (pub, value) => identity(pub.source) === identity(value) && (!reference(value) || pub.source.session_id === value.session_id);
  const states = new Set(["prepared", "publishing", "verified", "conflict", "blocked"]);
  const labels = { current: "与保存版本一致", db_stale: "聊天或索引已有变化", external_modified: "文件在外部修改过", duplicate: "发现同一身份的多份文件", missing: "文件未找到", unavailable: "目录未完整核对", source_missing: "原聊天或文字索引暂不可用", registry_mismatch: "文件身份不一致" };
  function path(value) {
    if (typeof value !== "string" || !value || value.length > 240 || /[\\:\x00-\x1f\x7f]/.test(value) || value.split("/").some(part => !part || part.startsWith(".")) || !/\.md$/i.test(value)) throw Error("请填写已有文件夹内的 Markdown 相对路径。");
    return value;
  }
  function folderPath(value) {
    if (typeof value !== "string") throw Error("请从已有真实文件夹中选择位置。");
    if (value) path(value + "/对话.md");
    return value;
  }
  function publication(value, workspace) {
    const source = value?.source;
    if (!object(value) || !uuid(value.id) || !states.has(value.state) || typeof value.current_verified !== "boolean" || typeof value.can_confirm !== "boolean" || !object(source) || !(indexed(source) || source.kind === "markdown_chat" && source.agent === undefined) || reference(source) && !sessionId(source.session_id) || source.workspace_id !== workspace || !uuid(source.chat_id) || typeof source.revision !== "string" || !(indexed(source) ? sha(source.revision) : /^(0|[1-9][0-9]{0,18})$/.test(source.revision)) || !sha(source.sha256) || !Number.isSafeInteger(source.size_bytes) || source.size_bytes < 0 || source.size_bytes > 524288) throw Error("整理回执不完整，没有继续写入文件。");
    path(value.target_path); return value;
  }
  function update(value, workspace) {
    publication(value, workspace);
    if (reference(value.source) || reference(value.previous_source)) throw Error("索引引用无需更新快照。");
    if (!uuid(value.binding_id) || !uuid(value.expected_head_id) || typeof value.retained !== "boolean" || !object(value.previous_source)) throw Error("更新回执身份不完整。");
    publication({ ...value, source: value.previous_source }, workspace);
    if (identity(value.previous_source) !== identity(value.source)) throw Error("更新回执改变了原对话身份。");
    return value;
  }
  async function checkedPreview(data, workspace, updating = false) {
    const pub = updating ? update(data?.update, workspace) : publication(data?.publication, workspace), full = data.preview;
    if (data.workspace_id !== workspace || !object(full) || typeof full.source_text !== "string" || typeof full.can_confirm !== "boolean" || full.can_confirm !== pub.can_confirm) throw Error("整理预览没有读完整。");
    const raw = new TextEncoder().encode(full.source_text);
    const digest = [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", raw))].map(n => n.toString(16).padStart(2, "0")).join("");
    if (raw.length !== pub.source.size_bytes || digest !== pub.source.sha256) throw Error("整理全文核对失败，没有提交。");
    return pub;
  }
  async function filePreview(value, workspace, selection) {
    if (!object(value) || value.workspace_id !== workspace || value.binding_id !== selection.binding_id
        || value.head_id !== selection.expected_head_id || !uuid(value.binding_id) || !uuid(value.head_id)
        || !sha(value.snapshot_ticket) || !sha(value.sha256) || typeof value.content !== "string"
        || /[\x00\ud800-\udfff]/u.test(value.content) || !Number.isSafeInteger(value.size_bytes)
        || value.size_bytes < 0 || value.size_bytes > 524288 || typeof value.name !== "string"
        || path(value.path).split("/").at(-1) !== value.name || !object(value.observation)
        || !["current", "external_modified"].includes(value.observation.file_status)
        || value.observation.source_revision_checked !== false) throw Error("当前Markdown文件预览不完整，没有导入资料。");
    publication({ id: value.binding_id, source: value.source, target_path: value.path,
      state: "verified", can_confirm: false, current_verified: false }, workspace);
    const raw = new TextEncoder().encode(value.content);
    const digest = [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", raw))].map(n => n.toString(16).padStart(2, "0")).join("");
    if (raw.length !== value.size_bytes || digest !== value.sha256) throw Error("磁盘文件的完整文字核对失败，没有导入资料。");
    return value;
  }
  function directory(value, workspace) {
    if (value?.workspace_id !== workspace || typeof value.root !== "string" || typeof value.complete !== "boolean" || !Array.isArray(value.chats) || value.chats.length > 1000 || !Array.isArray(value.publications) || value.publications.length > 1000 || !Array.isArray(value.issues) || !Array.isArray(value.unregistered)
        || value.folders !== undefined && (!Array.isArray(value.folders) || value.folders.length > 4096 || value.folders.some(name => typeof name !== "string"))) throw Error("文件夹没有读完整，未推断文件丢失。");
    const seen = new Set(), bindings = new Set();
    for (const row of value.chats) {
      if (!object(row) || !uuid(row.rabbit_id) || !(indexed(row) || row.kind === undefined && row.agent === undefined) || seen.has(identity(row)) || !Array.isArray(row.paths) || !Object.hasOwn(labels, row.status)) throw Error("整理身份或状态无法核对，没有选择副本。");
      seen.add(identity(row));
    }
    if (value.bindings !== undefined) {
      if (!Array.isArray(value.bindings) || value.bindings.length > 1000) throw Error("当前文件绑定没有读完整。");
      for (const binding of value.bindings) {
        if (!object(binding) || !uuid(binding.publication_id) || !uuid(binding.head_id) || bindings.has(identity(binding.source))) throw Error("当前文件版本无法核对。");
        publication({ id: binding.publication_id, state: "verified", can_confirm: false, current_verified: false, source: binding.source, target_path: binding.target_path }, workspace);
        bindings.add(identity(binding.source));
      }
    }
    for (const pub of value.publications) publication(pub, workspace);
    return { ...value, folders: value.folders === undefined ? [] : [...value.folders] };
  }
  function create({ api, root, getWorkspace, getCapabilities, onOpenChat, onOpenNative = null, onOpenHistory = null, onUseMaterial = null, canUseMaterial = () => false, onDirectory = null, onError = () => {} }) {
    let generation = 0, directoryReadVersion = 0, scope = "", source = null, prepared = null, busy = false, pending = [], focused = null, storageBlocked = false, shellWasInert = false;
    let status, tree, rootPath, destination, preview, confirm, check, form, recovery;
    root.hidden = true;
    const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
    const action = (label, fn, ai = true) => { const n = node("button", "file-task-action", label); n.type = "button"; if (ai) n.dataset.controlInterface = "ai"; n.addEventListener("click", fn); return n; };
    const key = () => `rabbit.markdown-chats.v1:${scope}`;
    const current = (version = generation) => !root.hidden && scope === getWorkspace() && version === generation;
    const enabled = () => getCapabilities()?.enabled === true;
    const syncing = value => object(value?.sync);
    const pendingSync = row => row?.operation === "sync";
    const operationId = row => pendingSync(row) ? row.update_id : row.publication_id;
    const operationRoute = row => "/api/markdown-chats/" + (pendingSync(row) ? "updates/" : "") + operationId(row);
    const matchesUpdate = (pub, row) => !pendingSync(row) || pub.binding_id === row.binding_id && pub.expected_head_id === row.expected_head_id;
    const validPending = row => object(row) && uuid(row.client_id) && uuid(row.chat_id) && sha(row.sha256) && (indexed(row) || row.kind === undefined && row.agent === undefined) && (!reference(row) || sessionId(row.session_id))
      && (pendingSync(row) ? uuid(row.update_id) && row.publication_id === undefined && uuid(row.binding_id) && uuid(row.expected_head_id) : row.operation === undefined && uuid(row.publication_id) && row.update_id === undefined);
    function savePending() {
      if (storageBlocked) throw Error("原整理请求无法读取，未覆盖或提交。");
      const wire = JSON.stringify(pending); window.localStorage.setItem(key(), wire);
      if (window.localStorage.getItem(key()) !== wire) throw Error("浏览器不能保存整理请求，尚未提交。");
    }
    function close() {
      if (busy) return;
      generation++; root.hidden = true; root.replaceChildren(); prepared = source = null;
      const shell = document.querySelector(".app-shell"); if (shell) shell.inert = shellWasInert;
      focused?.focus?.();
    }
    function fail(error) {
      if (current()) status.textContent = String(error?.message || "整理暂未完成，原聊天和文件保留。");
      onError(error);
    }
    function controls() {
      if (!current()) return;
      check.disabled = busy || storageBlocked || !source;
      destination.disabled = busy || syncing(source);
      confirm.disabled = busy || storageBlocked || !prepared?.publication.can_confirm || pending.some(row => identity(row) === identity(source));
    }
    function accepted(data, row) {
      if (data?.workspace_id !== scope || data.received !== true) throw Error("原整理回执尚未找到，没有换编号重发。");
      const pub = pendingSync(row) ? update(data.update, scope) : publication(data.publication, scope);
      if (pub.id !== operationId(row) || !matchesUpdate(pub, row) || !sameSource(pub, row) || pub.source.sha256 !== row.sha256 || data.state !== pub.state || pub.state === "prepared") throw Error("回执与原整理请求不一致，没有继续提交。");
      if (pub.state !== "publishing") { pending = pending.filter(item => item.client_id !== row.client_id); savePending(); }
      status.textContent = pub.state === "verified" ? pendingSync(row) ? pub.current_verified ? "已更新同一份文件，更新前的文件已保留。" : "这次更新当时已完成；当前文件或保留版本已有变化，请重新读取核对。" : "已保存整理文件。可在 Finder 改名、移动，再点重新读取。" : pub.state === "publishing" ? "请求已接收，尚未确认文件保存；请继续核对原回执。" : pendingSync(row) ? "更新未完成，已登记的文件和暂存保留。请核对冲突，没有自动换回或重写。" : "这次整理未完成，原文件保留。请查看回执后再处理。";
      prepared = null; preview.textContent = ""; renderRecovery(); return pub;
    }
    function renderRecovery() {
      recovery.replaceChildren();
      for (const row of pending) {
        const wrap = node("div", "markdown-recovery");
        wrap.append(node("p", "file-task-note", "有一笔整理请求尚未确认。先查询原编号，不创建替代文件。"));
        const read = action("核对原整理回执", async () => {
          if (busy || !current() || storageBlocked) return; const version = generation; busy = true; controls(); read.disabled = true;
          try {
            const data = await api(`/api/markdown-chats/${pendingSync(row) ? "updates/" : ""}receipt?${new URLSearchParams({ client_id: row.client_id })}`);
            if (!current(version)) return;
            if (data.workspace_id !== scope) throw Error("工作区已变化，原请求保留。");
            if (data.received === false) {
              const original = await api(operationRoute(row));
              if (!current(version)) return;
              const pub = await checkedPreview(original, scope, pendingSync(row));
              if (!current(version)) return;
              if (original.workspace_id !== scope || pub.id !== operationId(row) || !matchesUpdate(pub, row) || !sameSource(pub, row) || pub.source.sha256 !== row.sha256 || !pub.can_confirm) throw Error("原预览已变化或不能继续，未重新提交。");
              status.textContent = "服务器暂未找到回执；已重新核对原预览。需要继续时，使用同一编号明确登记。";
              const detail = node("details"); detail.append(node("summary", "", "原登记的完整 Markdown 内容"), node("pre", "markdown-chat-preview", original.preview.source_text)); wrap.append(detail);
              const resume = action("继续原登记", () => submit(row)); wrap.append(resume);
            } else { accepted(data, row); await refreshTree(); }
          } catch (error) { fail(error); }
          finally { busy = false; if (current(version)) { controls(); read.disabled = false; } }
        });
        wrap.append(read); recovery.append(wrap);
      }
    }
    async function readDirectory() {
      if (!enabled()) throw Error(getCapabilities()?.notice || "对话文件夹尚未启用。");
      const workspace = getWorkspace(); if (!sha(workspace)) throw Error("工作区身份尚未确认。");
      const version = ++directoryReadVersion;
      const value = await api("/api/markdown-chats");
      if (workspace !== getWorkspace()) throw Error("工作区已变化，没有应用旧文件夹目录。");
      const data = directory(value, workspace);
      if (version === directoryReadVersion && typeof onDirectory === "function") onDirectory(data);
      return data;
    }
    async function refreshTree() {
      if (!current()) return;
      const version = generation, reading = directoryReadVersion + 1;
      const data = await readDirectory(); if (!current(version) || reading !== directoryReadVersion) return;
      rootPath.textContent = data.root; tree.replaceChildren();
      const folders = new Map(), groups = node("div", "markdown-groups");
      const folder = name => {
        if (folders.has(name)) return folders.get(name);
        const parts = name.split("/"), parent = name ? folder(parts.slice(0, -1).join("/")) : groups;
        const details = node("details", "markdown-folder"); details.open = true;
        details.append(node("summary", "", name ? parts.at(-1) : "对话文件夹")); parent.append(details); folders.set(name, details); return details;
      };
      for (const name of data.folders) folder(name);
      const bindings = new Map((data.bindings || []).map(binding => [identity(binding.source), binding]));
      for (const row of data.chats) {
        const name = row.file?.name || row.paths[0] || "未找到的整理文件";
        const target = row.file && typeof row.file.folder === "string" ? folder(row.file.folder) : folder("");
        const binding = bindings.get(identity(row));
        const item = action(name, () => {
          if (busy || !current(version)) return;
          if (reference(row) && !binding) return;
          close();
          if (reference(row)) onOpenHistory?.({ agent: binding.source.agent, sessionId: binding.source.session_id, workspaceId: scope });
          else if (native(row)) onOpenNative?.({ agent: row.agent, chatId: row.rabbit_id, workspaceId: scope });
          else onOpenChat(row.rabbit_id);
        }, false);
        item.disabled = !native(row) && row.status === "source_missing" || native(row) && typeof onOpenNative !== "function"
          || reference(row) && (!binding || typeof onOpenHistory !== "function"); item.classList.add("markdown-chat-entry");
        if (indexed(row)) item.append(node("span", "file-task-note", row.agent + (reference(row) ? " · 原对话" : " · 原生文字索引")));
        item.append(node("span", "file-task-note", labels[row.status])); target.append(item);
        if (!reference(row) && data.complete && row.paths.length === 1 && row.file && binding && binding.source.revision === row.accepted_revision
            && ["current", "db_stale", "external_modified", "source_missing"].includes(row.status)
            && typeof onUseMaterial === "function" && canUseMaterial()) {
          target.append(action("作为资料交给 Agent", () => {
            if (busy || !current(version) || !canUseMaterial()) return;
            const selected = { binding_id: binding.publication_id, expected_head_id: binding.head_id, workspace_id: scope };
            close(); return Promise.resolve(onUseMaterial(selected)).catch(onError);
          }, false));
        }
        if (!reference(row) && data.complete && row.status === "db_stale" && row.paths.length === 1 && row.file && binding && binding.source.revision === row.accepted_revision && getCapabilities()?.sync_enabled === true) {
          const refreshFile = action("更新这份快照", () => {
            if (busy || !current(version)) return;
            const selected = { id: row.rabbit_id, title: row.file.name, revision: binding.source.revision,
              ...(native(row) ? { kind: "native_index", agent: row.agent } : {}),
              sync: { binding_id: binding.publication_id, head_id: binding.head_id, previous_source: binding.source, path: row.file.path } };
            close(); return open(selected).catch(fail);
          });
          target.append(refreshFile);
        }
        if (row.paths.length > 1) target.append(node("p", "file-task-note", row.paths.join(" · ")));
      }
      tree.append(groups);
      if (!data.chats.length) tree.append(node("p", "file-task-note", "还没有已核验的整理文件。在共同聊天或原生对话中选择“整理到文件夹”。"));
      if (!data.complete || data.issues.length) tree.append(node("p", "file-task-error", "目录存在无法完整核对的条目；不会据此删除或重建文件。"));
      if (data.unregistered.length) tree.append(node("p", "file-task-note", `另有 ${data.unregistered.length} 份未登记或其他工作区的文件；未自动导入。`));
      for (const raw of data.publications) {
        const pub = raw;
        if (pub.state !== "verified") tree.append(node("p", "file-task-note", `${pub.target_path} · 整理状态：${pub.state}`));
      }
    }
    async function prepare() {
      if (busy || !current() || storageBlocked || !source) return;
      const version = generation; busy = true; prepared = null; controls(); preview.textContent = "";
      try {
        const target = path(destination.value);
        const route = syncing(source) ? "/api/markdown-chats/updates/preview" : reference(source) ? "/api/markdown-chats/history-reference-preview" : native(source) ? "/api/markdown-chats/native-preview" : `/api/chats/${source.id}/markdown-preview`;
        const body = syncing(source) ? { binding_id: source.sync.binding_id, expected_head_id: source.sync.head_id } : reference(source) ? { agent: source.agent, session_id: source.session_id, target_path: target } : native(source) ? { agent: source.agent, chat_id: source.id, target_path: target } : { source_revision: source.revision, target_path: target };
        const data = await api(route, { method: "POST", body: JSON.stringify(body) }, false);
        if (!current(version)) return;
        const pub = await checkedPreview(data, scope, syncing(source)), full = data.preview;
        if (!current(version)) return;
        if (data.workspace_id !== scope || !sameSource(pub, source) || !syncing(source) && !indexed(source) && pub.source.revision !== source.revision || pub.target_path !== target || !object(full) || typeof full.source_text !== "string" || typeof full.can_confirm !== "boolean" || full.can_confirm !== pub.can_confirm) throw Error("整理预览与所选聊天不一致。");
        if (syncing(source) && (pub.binding_id !== source.sync.binding_id || pub.expected_head_id !== source.sync.head_id || pub.previous_source.sha256 !== source.sync.previous_source.sha256 || pub.previous_source.revision !== source.sync.previous_source.revision)) throw Error("更新前版本已变化，没有继续提交。");
        prepared = { ...data, publication: pub }; preview.textContent = full.source_text;
        status.textContent = pub.can_confirm ? syncing(source) ? "最新全文已核对；确认后更新同一文件，并保留换出的文件。" : "全文已核对；确认后只创建一个新文件。" : "目标已有变化或预览不可提交，文件保留。";
      } catch (error) { fail(error); }
      finally { busy = false; controls(); }
    }
    async function submit(original = null) {
      if (busy || !current() || storageBlocked || !original && !prepared?.publication.can_confirm) return;
      const version = generation;
      const row = original || { client_id: window.crypto.randomUUID(), ...(syncing(source)
        ? { operation: "sync", update_id: prepared.publication.id, binding_id: source.sync.binding_id, expected_head_id: source.sync.head_id }
        : { publication_id: prepared.publication.id }), chat_id: source.id, sha256: prepared.publication.source.sha256, ...(indexed(source) ? { kind: source.kind, agent: source.agent, ...(reference(source) ? { session_id: source.session_id } : {}) } : {}) };
      try {
        if (!original) pending.push(row);
        if (!pending.some(item => item === row)) throw Error("原整理请求未登记在本机，尚未提交。");
        savePending();
        busy = true; controls(); renderRecovery();
        const data = await api(`${operationRoute(row)}/confirm`, { method: "POST", body: JSON.stringify({ client_id: row.client_id }) }, false);
        if (!current(version)) return;
        accepted(data, row); await refreshTree();
      } catch (error) { fail(error); }
      finally { busy = false; if (current(version)) { controls(); renderRecovery(); } }
    }
    async function open(chat = null, options = {}) {
      if (busy) return;
      if (!enabled()) { onError(Error(getCapabilities()?.notice || "对话文件夹尚未启用。")); return; }
      scope = getWorkspace(); if (!sha(scope)) throw Error("工作区身份尚未确认。");
      const selected = chat ? { id: chat.id, title: chat.title, revision: chat.revision, ...(indexed(chat) ? { kind: chat.kind, agent: chat.agent, ...(reference(chat) ? { session_id: chat.session_id } : {}) } : {}), ...(syncing(chat) ? { sync: chat.sync } : {}) } : null;
      if (selected && (!uuid(selected.id) || chat.kind !== undefined && !indexed(chat) || reference(chat) && !sessionId(chat.session_id) || !indexed(selected) && typeof selected.revision !== "string")) throw Error("请重新打开原对话后再整理。");
      if (reference(selected) && syncing(selected)) throw Error("索引引用无需更新快照。");
      if (syncing(selected) && (getCapabilities()?.sync_enabled !== true || !uuid(selected.sync.binding_id) || !uuid(selected.sync.head_id) || !sameSource({ source: selected.sync.previous_source }, selected))) throw Error("这份快照的更新入口或版本暂不可用。");
      const selectedFolder = selected && !syncing(selected) ? folderPath(options?.folder === undefined ? "" : options.folder) : "";
      const filename = selected ? `${selected.title?.replace(/[\\/:\x00-\x1f\x7f]/g, "_").replace(/^\.+/, "_").slice(0, 40) || "对话"}-${selected.id.slice(0, 8)}.md` : "";
      const initialPath = syncing(selected) ? selected.sync.path : selected ? path((selectedFolder ? selectedFolder + "/" : "") + filename) : "";
      generation++; source = selected; prepared = null; storageBlocked = false;
      focused = document.activeElement; root.hidden = false; root.replaceChildren();
      const shell = document.querySelector(".app-shell"); if (shell) { shellWasInert = shell.inert; shell.inert = true; }
      const panel = node("div", "file-task-preview markdown-chats-panel"), head = node("header", "file-task-preview-header");
      const grouping = options?.group === true && selected && !syncing(selected);
      const title = node("h2", "", grouping ? "加入分组" : "对话文件夹"); title.id = "markdown-chats-title";
      head.append(title, action("关闭", close, false)); panel.append(head);
      panel.append(node("p", "file-task-note", grouping ? `分组：${selectedFolder}` : "对话按真实文件夹整理。"));
      rootPath = node("p", "file-task-note markdown-root"); panel.append(rootPath);
      status = node("p", "file-task-note"); status.setAttribute("role", "status");
      form = node("section"); form.hidden = !source;
      destination = node("input", "file-task-path"); destination.setAttribute("aria-label", "整理文件相对路径");
      destination.value = initialPath;
      destination.addEventListener("input", () => { prepared = null; preview.textContent = ""; controls(); });
      check = action(syncing(source) ? "预览最新完整内容" : "检查并预览全文", prepare); confirm = action(syncing(source) ? "确认更新这份文件" : "确认创建整理文件", () => submit());
      if (grouping) { destination.hidden = check.hidden = true; confirm.textContent = "加入分组"; }
      preview = node("pre", "markdown-chat-preview");
      const detail = node("details"); detail.append(node("summary", "", "完整 Markdown 内容"), preview);
      form.append(node("p", "file-task-note", source?.title || ""), destination, check, confirm, detail);
      recovery = node("section"); tree = node("section");
      const refresh = action("重新读取", () => refreshTree().catch(fail));
      const openFolder = action("在访达中打开", async () => {
        if (busy || !current()) return;
        const version = generation; busy = true; controls(); openFolder.disabled = true;
        try {
          const data = await api("/api/markdown-chats/open-folder", { method: "POST", body: "{}" }, false);
          if (!current(version)) return;
          if (data?.workspace_id !== scope || data.opened !== true) throw Error("文件夹打开结果尚未确认，请在访达中核对；没有自动重试。");
          status.textContent = "已在访达中打开对话文件夹。";
        } catch (error) { fail(error); }
        finally { busy = false; if (current(version)) { controls(); openFolder.disabled = false; } }
      }, false);
      const actions = node("div", "file-task-actions"); actions.append(refresh, openFolder);
      panel.append(form, status, recovery, actions, tree); root.append(panel);
      try {
        const loaded = JSON.parse(window.localStorage.getItem(key()) || "[]");
        if (!Array.isArray(loaded) || loaded.length > 100 || !loaded.every(validPending) || new Set(loaded.map(row => row.client_id)).size !== loaded.length) throw Error();
        pending = loaded;
      } catch (_) { pending = []; storageBlocked = true; controls(); status.textContent = "浏览器中的原整理请求无法读取，未提交新请求。"; return; }
      renderRecovery(); controls(); await refreshTree().catch(fail); head.querySelector("button")?.focus();
      if (grouping && current()) await prepare();
    }
    root.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); close(); } });
    return { open, close, enabled, readDirectory };
  }
  window.RabbitMarkdownChats = Object.freeze({ create, path, publication, filePreview });
})();
