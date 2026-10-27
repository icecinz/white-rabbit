/* Custom groups are real folders. Membership belongs to Markdown bindings. */
(() => {
  "use strict";
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  function create({ api, root, filters, directory, getWorkspace, onSelect, onAssign, onError }) {
    let data = null, selected = null, enabled = false, scope = "", version = 0, busy = false, focus, wasInert = false;
    const button = (text, fn, ai = false) => {
      const n = node("button", "file-task-action", text); n.type = "button";
      if (ai) n.dataset.controlInterface = "ai"; n.addEventListener("click", fn); return n;
    };
    const add = button("＋", () => open(), true); add.className = "source-filter group-add";
    add.setAttribute("aria-label", "新建对话分组"); add.title = "新建分组"; add.hidden = true; filters.append(add);
    function render() {
      for (const n of filters.querySelectorAll(".group-filter")) n.remove();
      for (const path of data?.folders || []) {
        const n = button(path, () => select(path)); n.className = "source-filter group-filter";
        n.dataset.folder = path; n.title = path; n.classList.toggle("active", path === selected);
        n.setAttribute("aria-pressed", String(path === selected)); filters.insertBefore(n, add);
      }
      add.hidden = !enabled;
    }
    function accept(value) {
      if (!enabled || value.workspace_id !== getWorkspace()) return;
      data = value; render();
    }
    function select(path, notify = true) { selected = path; render(); if (notify) onSelect(path); }
    function clear() { select(null, false); }
    function close() {
      if (busy) return;
      version++; root.hidden = true; root.replaceChildren();
      const shell = document.querySelector(".app-shell"); if (shell) shell.inert = wasInert;
      focus?.focus?.();
    }
    async function open(item = null) {
      if (!enabled || busy) return;
      if (!root.hidden) close();
      const stamp = ++version, workspace = getWorkspace();
      focus = document.activeElement; const shell = document.querySelector(".app-shell");
      if (shell) { wasInert = shell.inert; shell.inert = true; }
      root.hidden = false; root.replaceChildren();
      const current = () => !root.hidden && version === stamp && getWorkspace() === workspace;
      const panel = node("div", "file-task-preview conversation-group-panel"), head = node("header", "file-task-preview-header");
      const heading = node("h2", "", item ? "加入分组" : "新建分组"); heading.id = "conversation-group-title";
      const dismiss = button("关闭", close); head.append(heading, dismiss); panel.append(head);
      const status = node("p", "file-task-note"); status.setAttribute("role", "status");
      const choices = node("div", "conversation-group-choices");
      let readRequired = false;
      const fail = error => {
        if (!current()) { onError(error); return; }
        status.textContent = error.message || "暂未加入，原对话保留。";
        if (error.groupObservationRequired) {
          readRequired = true;
          for (const control of choices.querySelectorAll("button")) control.disabled = true;
        }
      };
      const label = node("label", "conversation-group-name", "分组名称"), input = node("input", "file-task-path"); input.setAttribute("aria-label", "分组名称"); input.maxLength = 80;
      label.append(input);
      const controls = node("div", "file-task-actions");
      const make = button("新建分组", async () => {
        if (busy || readRequired || !current()) return;
        const name = input.value.trim();
        if (!name || /[\\/:\x00-\x1f\x7f]/.test(name) || name.startsWith(".")) { status.textContent = "请填写普通的文件夹名称。"; input.focus(); return; }
        busy = true; make.disabled = dismiss.disabled = true;
        try {
          const value = await api("/api/markdown-chats/folders", { method: "POST", body: JSON.stringify({ path: name }) }, false);
          if (!current()) return;
          if (value?.workspace_id !== workspace || value.folder?.path !== name || typeof value.folder.created !== "boolean") throw Error("分组结果暂未确认，请重新读取文件夹。");
          await directory.readDirectory();
          if (!current()) return;
          const target = item ? await onAssign(item, name, data) : null;
          if (!current()) return;
          busy = false; close(); select(name);
          if (target) await directory.open(target, { folder: name, group: true });
        } catch (error) { fail(error); }
        finally { busy = false; if (!root.hidden && version === stamp) { dismiss.disabled = false; make.disabled = readRequired || !current(); } }
      }, true);
      controls.append(make); panel.append(choices, label, controls, status); root.append(panel);
      input.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); make.click(); } });
      try {
        const value = await directory.readDirectory(); if (!current()) return;
        if (item) for (const path of value.folders) {
          const choose = button(path, async () => {
            if (busy || readRequired || !current()) return;
            busy = true; dismiss.disabled = make.disabled = true; choose.disabled = true;
            try {
              const target = await onAssign(item, path, value);
              if (!current()) return;
              busy = false; close(); select(path);
              if (target) await directory.open(target, { folder: path, group: true });
            }
            catch (error) { fail(error); }
            finally { busy = false; if (!root.hidden && version === stamp) { dismiss.disabled = false; make.disabled = choose.disabled = readRequired || !current(); } }
          }, true); choices.append(choose);
        }
        if (item && !value.folders.length) choices.append(node("p", "file-task-note", "先创建一个分组。"));
      } catch (error) { if (current()) status.textContent = error.message || "文件夹暂未读出。"; }
      if (current()) input.focus();
    }
    function setEnabled(value) {
      const workspace = getWorkspace(), changed = enabled !== value || scope !== workspace;
      enabled = value; scope = workspace; if (changed) { data = null; selected = null; render(); if (enabled) directory.readDirectory().catch(onError); }
    }
    root.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); close(); } });
    return { open, accept, clear, select, setEnabled, selected: () => selected };
  }
  window.RabbitConversationGroups = Object.freeze({ create });
})();
