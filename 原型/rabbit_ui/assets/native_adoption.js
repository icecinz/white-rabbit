(() => {
  "use strict";
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const keys = (value, names) => object(value) && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
  const sha = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const agents = Object.freeze({ codex: { label: "Codex", history: "paginated", id: uuid },
    dsh: { label: "DSH", history: "web_profile", id: value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value) },
    claude: { label: "Claude", history: "sdk_history", id: uuid },
    pi: { label: "Pi", history: "active_branch", id: value => typeof value === "string" && /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,254}[A-Za-z0-9])?$/.test(value) },
    hermes: { label: "Hermes", history: "stored_session", id: value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value) } });
  const text = (value, maximum) => typeof value === "string" && Array.from(value).length <= maximum && !/[\x00-\x1f\x7f\uD800-\uDFFF]/u.test(value);
  const source = value => object(value) && typeof value.agent === "string" && Object.hasOwn(agents, value.agent) && agents[value.agent].id(value.session_id) && sha(value.workspace_id) && typeof value.scope === "string" && value.scope.length > 0;
  const node = (tag, cls, content) => { const value = document.createElement(tag); if (cls) value.className = cls; if (content !== undefined) value.textContent = content; return value; };
  const previewKeys = ["agent", "source_session_id", "title", "cwd", "workspace_id", "revision", "history_mode", "execution_started", "body_copied", "current_executor_ownership"];
  function previewValue(value, captured) {
    if (!keys(value, previewKeys) || value.agent !== captured.agent || value.source_session_id !== captured.session_id
        || !text(value.title, 80) || !value.title.trim() || !text(value.cwd, 4096) || !value.cwd.startsWith("/")
        || value.workspace_id !== captured.workspace_id || !sha(value.revision) || value.history_mode !== agents[captured.agent].history
        || value.execution_started !== false || value.body_copied !== false || value.current_executor_ownership !== "not_checked") throw Error("invalid_preview");
    return { ...value };
  }
  function adoptedValue(value, captured, preview) {
    const chat = value?.chat;
    if (!keys(value, ["chat", "source_session_id", "workspace_id", "adopted", "execution_started", "body_copied"])
        || value.source_session_id !== captured.session_id || value.workspace_id !== preview.workspace_id
        || value.adopted !== true || value.execution_started !== false || value.body_copied !== false
        || !keys(chat, ["id", "title", "agent", "role", "created_at"]) || !uuid(chat.id)
        || !text(chat.title, 80) || !chat.title.trim() || chat.agent !== captured.agent || chat.role !== "chat"
        || !text(chat.created_at, 64) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(chat.created_at) || !Number.isFinite(Date.parse(chat.created_at))) throw Error("invalid_adoption");
    return { ...value, chat: { ...chat } };
  }
  function create({ root, api, getSource, onAdopted, onError = () => {} }) {
    if (!root || [api, getSource, onAdopted, onError].some(value => typeof value !== "function")) throw Error("原会话恢复入口尚未就绪。");
    let generation = 0, captured = null, preview = null, reading = false, writing = false, notice = "";
    let panel, title, directory, status, previewButton, adoptButton, closeButton, returnFocus, shell, wasInert;
    root.hidden = true;
    const button = (label, action) => { const value = node("button", "file-task-action", label); value.type = "button"; value.addEventListener("click", action); return value; };
    function current(stamp = generation) {
      const value = getSource();
      return !root.hidden && captured && stamp === generation && source(value) && value.agent === captured.agent
        && value.session_id === captured.session_id && value.workspace_id === captured.workspace_id && value.scope === captured.scope;
    }
    function report(message) { onError(Error(message)); }
    function dismiss(restoreFocus) {
      const restore = restoreFocus && current() && root.contains?.(document.activeElement);
      generation++; root.hidden = true; root.replaceChildren(); captured = preview = null; reading = false; notice = "";
      panel = title = directory = status = previewButton = adoptButton = closeButton = null;
      if (shell) shell.inert = wasInert;
      shell = null;
      const previous = returnFocus; returnFocus = null;
      if (restore && previous?.isConnected !== false) previous?.focus();
    }
    function close() { dismiss(true); }
    function render() {
      if (root.hidden || !panel) return;
      const same = current();
      title.textContent = preview?.title || `核对原 ${agents[captured.agent].label} 会话`;
      directory.textContent = preview ? preview.cwd : "核对成功后在这里显示原工作目录。";
      previewButton.disabled = reading || writing || !same;
      previewButton.textContent = reading ? "正在核对…" : "重新核对";
      adoptButton.disabled = !preview || reading || writing || !same;
      adoptButton.textContent = writing ? "正在登记…" : "登记原会话";
      status.textContent = !same ? "历史、工作区或草稿已变化。请关闭后重新打开，原记录和草稿保留。"
        : notice || (reading ? "正在读取原会话的身份与工作目录，没有连接或发送。" : "核对目录后可登记并带入本轮草稿；进入原生对话后，第一次发送时自动连接。");
      status.setAttribute("role", notice || !same ? "alert" : "status");
    }
    function makePanel() {
      panel = node("div", "file-task-preview");
      const label = agents[captured.agent].label;
      const header = node("div", "file-task-preview-header"), heading = node("h2", "", `恢复原 ${label} 会话`);
      heading.id = "native-adoption-title"; closeButton = button("关闭", close); header.append(heading, closeButton);
      title = node("p", "file-task-heading"); directory = node("pre", "file-task-preview-content"); directory.setAttribute("aria-label", "原工作目录");
      const boundary = node("p", "file-task-note", "继续同一段原生会话，不复制历史正文。只带入本轮未发送的文字、所选材料和只读记忆；沿用原 Agent 配置。这里不连接、不发送，也不强占其他应用正在使用的会话。");
      const ownership = node("p", "file-task-note", captured.agent === "hermes"
        ? "目前支持工作目录明确的原生 CLI 主会话及无歧义压缩链。恢复前检查中断任务与可观察到的占用；这不是全局排他保证，请先结束其他地方对原会话的使用。原生启动仍沿用原配置。"
        : captured.agent === "pi"
        ? "恢复同一原文件中已落盘的原生当前分支，不恢复其他进程尚未保存的分支选择。请先结束其他地方对原会话的使用，没有全局占用保证。原生启动或扩展可能写入配置条目及会话元数据，仍沿用原配置。"
        : captured.agent === "claude"
        ? "沿原会话 UUID、原工作目录和原 SDK 历史恢复，保留当前 CCSwitch 模型设置。加载 hooks、MCP 或扩展可能有正常副作用，没有全局占用保证；请先结束其他地方对原会话的使用。"
        : captured.agent === "dsh"
        ? "保留原文件、原工作目录和会话预设，使用这台电脑当前的 DSH 配置与原装网页运行入口。历史使用的模型可能与当前配置不同。请先结束其他地方对这段对话的使用；这里不能确认外部占用。未完成任务和其他分支暂不支持。"
        : "目前支持未归档的主对话。原应用专属工具、分支和归档暂不支持，可用“交给原生 Agent”引用前文。原会话是否被占用尚未核实；连接时由原生程序接收或拒绝，不强占已有连接。");
      const actions = node("div", "file-task-actions"); previewButton = button("重新核对", check); adoptButton = button("登记原会话", adopt); actions.append(previewButton, adoptButton);
      previewButton.dataset.controlInterface = adoptButton.dataset.controlInterface = "ai";
      status = node("p", "file-task-note"); status.setAttribute("aria-live", "polite");
      panel.append(header, title, node("p", "file-task-note", "原工作目录"), directory, boundary, ownership, actions, status);
      root.replaceChildren(panel); render();
    }
    async function check() {
      if (!current() || reading || writing) return false;
      const stamp = generation; preview = null; notice = ""; reading = true; render();
      try {
        const value = await api(`/api/native-history/${captured.agent}/preview`, { method: "POST", body: JSON.stringify({ session_id: captured.session_id }) }, false);
        if (!current(stamp)) return false;
        preview = previewValue(value, captured); return true;
      } catch (_) {
        if (current(stamp)) { notice = "暂时无法核对原会话。原记录和草稿保留，可重新核对。"; report(notice); }
        return false;
      } finally { if (stamp === generation) { reading = false; render(); } }
    }
    async function adopt() {
      if (!current() || !preview || reading || writing) return false;
      const stamp = generation, original = preview; writing = true; notice = ""; render();
      let accepted = false;
      try {
        const value = await api(`/api/native-history/${captured.agent}/adopt`, { method: "POST", body: JSON.stringify({ session_id: captured.session_id, revision: original.revision }) }, false);
        if (!current(stamp)) return false;
        const response = adoptedValue(value, captured, original); accepted = true; preview = null;
        notice = "已登记原会话，没有连接或发送。"; render();
        await onAdopted(response, () => current(stamp));
        if (stamp === generation && !root.hidden) dismiss(false);
        return true;
      } catch (error) {
        if (current(stamp)) {
          preview = null;
          notice = accepted ? "原会话已登记，尚未连接或发送。" + (typeof error?.adoptionDraftMessage === "string" ? error.adoptionDraftMessage : "草稿未能带入或入口暂未打开。来源页的文字和材料仍保留，请关闭后核对原生对话。")
            : "登记结果尚未确认，可能已经登记。请重新核对后再决定，或关闭查看原生对话；不会自动重试。";
          report(notice);
        }
        return false;
      } finally { writing = false; if (stamp === generation) render(); }
    }
    async function open() {
      if (writing) return false;
      const value = getSource();
      if (!source(value)) { report("请先打开可恢复的原生历史，原记录和草稿保留。"); return false; }
      if (!root.hidden) dismiss(false);
      captured = { agent: value.agent, session_id: value.session_id, workspace_id: value.workspace_id, scope: value.scope }; generation++;
      returnFocus = document.activeElement; shell = document.querySelector(".app-shell"); wasInert = shell?.inert === true;
      if (shell) shell.inert = true;
      root.hidden = false; makePanel(); closeButton.focus(); return check();
    }
    root.addEventListener("click", event => { if (event.target === root) close(); });
    root.addEventListener("keydown", event => {
      if (root.hidden) return;
      if (event.key === "Escape") { event.preventDefault(); close(); return; }
      if (event.key !== "Tab" || !panel) return;
      const choices = [...panel.querySelectorAll("button")].filter(value => !value.disabled && !value.hidden);
      const first = choices[0], last = choices[choices.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    });
    return { open, close };
  }
  window.RabbitNativeAdoption = Object.freeze({ create });
})();
