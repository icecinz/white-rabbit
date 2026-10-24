(() => {
  "use strict";
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  function intent(value) {
    if (value === undefined) return { mode: "chat", artifact_ids: [] };
    if (!value || typeof value !== "object" || Array.isArray(value) || !["chat", "files"].includes(value.mode) || !Array.isArray(value.artifact_ids) || value.artifact_ids.length > 4 || value.artifact_ids.some(id => typeof id !== "string" || !uuid.test(id)) || new Set(value.artifact_ids).size !== value.artifact_ids.length) throw new Error("文件任务选择不完整，原草稿已保留。");
    return { mode: value.mode, artifact_ids: [...value.artifact_ids] };
  }
  function same(a, b) { return JSON.stringify(intent(a)) === JSON.stringify(intent(b)); }
  function body(value) { const checked = intent(value); return checked.mode === "chat" && !checked.artifact_ids.length ? {} : { task_intent: checked }; }
  function artifact(value) {
    if (!value || typeof value !== "object" || typeof value.id !== "string" || !uuid.test(value.id) || typeof value.path !== "string" || !value.path || value.path.length > 240 || value.path.startsWith("/") || value.path.includes("\\") || value.path.split("/").some(part => !part || part === "." || part === ".." || part.startsWith(".")) || /[\x00-\x1f\x7f]/.test(value.path) || !/\.(?:md|txt)$/i.test(value.path) || typeof value.name !== "string" || value.name !== value.path.split("/").at(-1) || !Number.isSafeInteger(value.size_bytes) || value.size_bytes < 0 || value.size_bytes > 524288 || typeof value.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256)) throw new Error("成果信息暂时无法读取，原回答仍保留。");
    if (value.available !== undefined && typeof value.available !== "boolean" || value.content_verified !== undefined && typeof value.content_verified !== "boolean") throw new Error("成果可读状态不完整，请重新打开聊天。");
    return { id: value.id, name: value.name, path: value.path, size_bytes: value.size_bytes, sha256: value.sha256, available: value.available !== false, content_verified: value.content_verified !== false };
  }
  function create({ api, root, onError = () => {}, getView = () => "", onSelect = () => {}, canPublish = () => false, onPublish = () => {}, appendPublicationStatus = () => {} }) {
    let version = 0, focusBefore = null;
    root.hidden = true;
    function node(tag, name, text) { const result = document.createElement(tag); if (name) result.className = name; if (text !== undefined) result.textContent = text; return result; }
    function action(label, callback) { const result = node("button", "file-task-action", label); result.type = "button"; result.addEventListener("click", callback); return result; }
    function close() { const wasOpen = !root.hidden; ++version; root.hidden = true; root.replaceChildren(); if (wasOpen) { const shell = document.querySelector(".app-shell"); if (shell) shell.inert = false; focusBefore?.focus?.(); } }
    async function read(chatId, expected) {
      if (typeof chatId !== "string" || !chatId || chatId.includes("\0")) throw new Error("请在成果所属的聊天里打开。");
      expected = artifact(expected);
      if (!expected.available || !expected.content_verified) throw new Error("文件暂不可读取，保存记录仍保留。");
      const data = await api(`/api/chats/${encodeURIComponent(chatId)}/files/${expected.id}`), actual = artifact(data.artifact);
      if (!actual.available || !actual.content_verified) throw new Error("文件暂不可读取，保存记录仍保留。");
      if (JSON.stringify(actual) !== JSON.stringify(expected) || typeof data.text !== "string" || new Blob([data.text]).size !== actual.size_bytes) throw new Error("成果内容与保存记录不一致，未使用这份文件。");
      return { artifact: actual, text: data.text };
    }
    async function preview(chatId, expected) {
      const current = ++version, view = getView(); focusBefore = document.activeElement;
      root.hidden = false; root.replaceChildren(); const shell = document.querySelector(".app-shell"); if (shell) shell.inert = true;
      const panel = node("div", "file-task-preview"), header = node("header", "file-task-preview-header"), title = node("h2", "", expected.name); title.id = "file-task-preview-title";
      const cancel = action("关闭", close), status = node("p", "file-task-note", "正在读取完整文件…"); header.append(title, cancel); panel.append(header, status); root.append(panel); cancel.focus();
      try { const result = await read(chatId, expected); if (current !== version || root.hidden) return; if (view !== getView()) { close(); return; }
        status.textContent = `${result.artifact.size_bytes} 字节 · 已保存的完整内容`;
        const content = node("pre", "file-task-preview-content", result.text); panel.append(content);
        const save = action("下载文件", async () => {
          save.disabled = true;
          try { await window.RabbitTextFileDownload.download(result); }
          catch (error) { onError(error); }
          finally { save.disabled = false; }
        }); panel.append(save);
      } catch (error) { if (current === version && !root.hidden) { status.textContent = "文件暂时没有读出来，原回答与保存记录仍保留。"; onError(error); } }
    }
    async function select(chatId, expected, button) {
      const view = getView(); button.disabled = true;
      try { const result = await read(chatId, expected); if (view !== getView()) return; await onSelect(chatId, result.artifact, view); }
      catch (error) { onError(error); }
      finally { button.disabled = false; }
    }
    function inputCard(chatId, raw, { removable = false, onRemove = () => {}, disabled = false } = {}) {
      const item = artifact(raw), result = node("div", "file-task-artifact");
      result.append(node("strong", "file-task-name", item.name), node("span", "file-task-note", `${item.size_bytes} 字节 · 文件输入 · 这段聊天的成果 · 固定版本${!item.available || !item.content_verified ? " · 暂不可读取" : ""}`));
      const actions = node("div", "file-task-actions"), open = action("预览输入", () => preview(chatId, item)); open.disabled = disabled || !item.available || !item.content_verified; actions.append(open);
      if (removable) { const remove = action("移除", onRemove); remove.disabled = disabled; actions.append(remove); }
      result.append(actions); return result;
    }
    function card(chatId, raw) {
      const item = artifact(raw), result = node("div", "file-task-artifact");
      result.append(node("strong", "file-task-name", item.name), node("span", "file-task-note", `${item.size_bytes} 字节 · ${item.available && item.content_verified ? "已保存" : "文件暂不可读取"}`));
      const actions = node("div", "file-task-actions"), open = action("预览 / 下载", () => preview(chatId, item));
      const choose = action("用于下一轮", () => select(chatId, item, choose)); open.disabled = choose.disabled = !item.available || !item.content_verified; actions.append(open, choose);
      if (canPublish()) { const publish = action("另存到项目", () => { try { onPublish(chatId, item); } catch (error) { onError(error); } }); publish.disabled = !item.available || !item.content_verified; actions.append(publish); }
      result.append(actions); appendPublicationStatus(result, chatId, item); return result;
    }
    function append(parent, chatId, value) {
      if (!value) return;
      const section = node("section", "file-task-results"); section.setAttribute("aria-label", "本轮文件任务");
      const states = { queued: "文件任务等待执行", running: "文件任务执行中", cancelling: "正在停止文件任务", completed: "文件任务已结束", failed: "文件任务未完成", cancelled: "文件任务已停止", interrupted: "文件任务已中断" };
      section.append(node("p", "file-task-heading", states[value.status] || "文件任务状态待确认"));
      if (Array.isArray(value.artifacts)) for (const raw of value.artifacts.slice(0, 20)) { try { section.append(card(chatId, raw)); } catch (_) { section.append(node("p", "file-task-note", "有一份成果信息暂时无法读取。")); } }
      if (value.artifact_state === "warnings") section.append(node("p", "file-task-warning", "文件保存核对有提示，请查看下面的实际成果和说明。"));
      if (!Array.isArray(value.artifacts) || !value.artifacts.length) section.append(node("p", "file-task-note", ["queued", "running", "cancelling"].includes(value.status) ? "保存成功的文件会显示在这里。" : "本轮没有可读取的已保存文件。"));
      if (Array.isArray(value.warnings)) for (const warning of value.warnings.slice(0, 10)) if (typeof warning === "string") section.append(node("p", "file-task-warning", warning.slice(0, 500)));
      parent.append(section);
    }
    root.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); close(); } if (event.key === "Tab") { const buttons = [...root.querySelectorAll("button")].filter(item => !item.disabled), first = buttons[0], last = buttons.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } } });
    return { read, preview, close, card, inputCard, append };
  }
  window.RabbitFileTasks = { intent, same, body, artifact, create };
})();
