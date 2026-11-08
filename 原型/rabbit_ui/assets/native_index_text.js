/* An unavailable native entry keeps its identity; this reader only reads its verified index. */
(() => {
  "use strict";
  const agents = new Set(["codex", "hermes", "pi", "claude", "dsh"]);
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
  const revision = value => typeof value === "string" && /^[0-9a-f]{16}:[0-9a-f]{24}$/.test(value);
  const sid = value => typeof value === "string" && value.length > 0 && value.length <= 256;
  const messageId = value => typeof value === "string" && value.length > 0 && value.length <= 512;
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  function position(value) {
    if (!object(value) || Object.keys(value).sort().join() !== "agent,chat_id,offset,revision,source_session_id"
        || !agents.has(value.agent) || !uuid(value.chat_id) || !integer(value.offset)
        || !(value.source_session_id === null || sid(value.source_session_id)) || !(value.revision === null || revision(value.revision))
        || value.offset > 0 && (!value.source_session_id || !value.revision)) throw Error("阅读位置无效。");
    return { agent: value.agent, chat_id: value.chat_id, source_session_id: value.source_session_id, offset: value.offset, revision: value.revision };
  }
  function create({ root, scrollRoot = root, api, getWorkspace, renderMessage, onViewChange = () => {} }) {
    const node = (tag, cls, text) => { const value = document.createElement(tag); value.className = cls; if (text !== undefined) value.textContent = text; return value; };
    const button = (label, callback) => { const value = node("button", "rbs-button", label); value.type = "button"; value.addEventListener("click", callback); return value; };
    const page = node("section", "rbs-page native-index-text"), more = node("details", "rbs-chat-more"), summary = node("summary", "", "更多");
    page.dataset.controlInterface = "ai";
    const notice = node("p", "rbs-note"), errorNode = node("p", "rbs-error"), excerptNode = node("p", "list-search-match"), messages = node("div", "messages"), operations = node("div", "operation-records");
    const range = node("p", "rbs-note"), paging = node("div", "history-pagination"), previous = button("前一页", () => load({ offset: Math.max(0, data.offset - 100) })), next = button("后一页", () => load({ offset: data.next_offset }));
    const retry = button("重新读取", () => load({ offset: 0, revision: null })), sources = node("select", "rbs-control"), sourceLabel = node("label", "rbs-field"), storageNote = node("p", "rbs-note");
    sourceLabel.append(node("span", "", "历史分段"), sources); sources.setAttribute("aria-label", "历史分段");
    more.append(summary, retry, sourceLabel, storageNote, operations); paging.append(previous, range, next); page.append(more, notice, errorNode, excerptNode, messages, paging); root.append(page); root.hidden = true;
    let workspace = "", selected = null, visible = false, generation = 0, reading = false, data = null, dataGeneration = null, match = null, excerpt = "", title = "", errorText = "", storageFailed = false, language = "zh-CN";
    const tr = (zh, en) => language === "en" ? en : zh;
    const key = () => "rabbit.native-reading.v1." + encodeURIComponent(workspace);
    function save() {
      try { if (selected) window.sessionStorage.setItem(key(), JSON.stringify(selected)); else window.sessionStorage.removeItem(key()); storageFailed = false; }
      catch (_) { storageFailed = true; }
    }
    function restore() {
      const current = String(getWorkspace() || ""); if (current === workspace) return;
      generation++; workspace = current; selected = data = match = null; title = excerpt = errorText = ""; reading = visible = false; root.hidden = true;
      if (!workspace) return;
      try { const raw = window.sessionStorage.getItem(key()); if (raw) selected = position(JSON.parse(raw)); }
      catch (_) { /* A malformed location never selects a different chat. */ }
      render();
    }
    function context() { return selected && workspace === String(getWorkspace() || "") ? { workspace_id: workspace, agent: selected.agent, chat_id: selected.chat_id, title } : null; }
    function getHistorySourceContext() {
      if (!visible || reading || errorText || dataGeneration !== generation || !data || !context()
          || data.workspace_id !== workspace || data.agent !== selected.agent || data.chat_id !== selected.chat_id
          || data.source_session_id !== selected.source_session_id || data.revision !== selected.revision
          || !sid(data.source_session_id) || /[\s\p{Cc}\p{Cf}\p{Cs}]/u.test(data.source_session_id)) return null;
      // Only a current validate() result establishes the source; restored
      // positions and catalogue/index_sources metadata never do.
      return { workspace_id: workspace, agent: data.agent, chat_id: data.chat_id,
        source_session_id: data.source_session_id, generation };
    }
    function select(value, details = {}) {
      restore();
      if (value?.workspaceId !== workspace || !workspace || !agents.has(value?.agent) || !uuid(value?.chatId)) throw Error("原对话或工作区暂不可读取。");
      generation++; reading = false; data = null; errorText = ""; title = typeof details.title === "string" ? details.title : "";
      excerpt = typeof details.excerpt === "string" ? details.excerpt.slice(0, 2000) : "";
      const candidate = details.match;
      match = object(candidate) && candidate.agent === value.agent && sid(candidate.session_id) && messageId(candidate.message_id) && integer(candidate.index)
        ? { session_id: candidate.session_id, messageId: candidate.message_id, index: candidate.index, query: typeof candidate.query === "string" ? candidate.query : "" } : null;
      selected = position({ agent: value.agent, chat_id: value.chatId, source_session_id: match?.session_id || null, offset: 0, revision: null });
      save(); render(); return true;
    }
    function validate(value, requested) {
      if (!object(value) || value.workspace_id !== workspace || value.scope !== "native_index_text" || value.read_only !== true
          || value.agent !== requested.agent || value.chat_id !== requested.chat_id || !revision(value.revision)
          || typeof value.title !== "string" || !value.title.trim() || Array.from(value.title).length > 80
          || !Array.isArray(value.sources) || !value.sources.length || value.sources.length > 128
          || !value.sources.every(row => object(row) && row.agent === value.agent && sid(row.session_id))
          || new Set(value.sources.map(row => row.session_id)).size !== value.sources.length
          || !value.sources.some(row => row.session_id === value.source_session_id)
          || requested.source_session_id && value.source_session_id !== requested.source_session_id
          || requested.revision && value.revision !== requested.revision
          || !object(value.session) || value.session.agent !== value.agent || value.session.session_id !== value.source_session_id
          || !integer(value.offset) || !integer(value.total) || value.offset > value.total
          || !(value.next_offset === null || integer(value.next_offset) && value.next_offset > value.offset && value.next_offset < value.total)
          || !Array.isArray(value.messages) || value.messages.length > 100
          || !value.messages.every((row, index) => object(row) && typeof row.message_id === "string" && row.message_id.length > 0 && typeof row.content === "string" && typeof row.role === "string" && row.message_index === value.offset + index)
          || new Set(value.messages.map(row => row.message_id)).size !== value.messages.length
          || !(value.located_message_id === null && value.located_message_index === null || typeof value.located_message_id === "string" && integer(value.located_message_index)
            && value.messages.some(row => row.message_id === value.located_message_id && row.message_index === value.located_message_index))) throw Error("读取结果与原对话不一致，未显示另一段记录。");
      if (requested.message_id && value.located_message_id !== requested.message_id) throw Error("未能在原记录中定位这条消息。");
      return value;
    }
    async function load(patch = {}) {
      if (!visible || !context() || reading) return false;
      const request = ++generation, scope = workspace, requested = { ...selected, ...patch };
      if (match && !data && patch.offset === undefined) requested.message_id = match.messageId;
      const query = new URLSearchParams({ agent: requested.agent, chat_id: requested.chat_id, limit: "100" });
      if (requested.source_session_id) query.set("source_session_id", requested.source_session_id);
      if (requested.message_id) query.set("message_id", requested.message_id); else query.set("offset", String(requested.offset));
      if (requested.revision) query.set("revision", requested.revision);
      const current = () => visible && request === generation && scope === workspace && scope === String(getWorkspace() || "");
      reading = true; errorText = ""; render(); onViewChange(context());
      try {
        const value = await api("/api/library/native-text?" + query);
        if (!current()) return false;
        data = validate(value, requested); dataGeneration = request; title = data.title; selected = position({ agent: data.agent, chat_id: data.chat_id, source_session_id: data.source_session_id, offset: data.offset, revision: data.revision });
        if (data.located_message_id) match = { session_id: data.source_session_id, messageId: data.located_message_id, index: data.located_message_index, query: match?.query || "" };
        save(); render();
        const target = match && Array.from(messages.children).find(item => item.dataset.messageId === match.messageId);
        if (target) { target.focus?.({ preventScroll: true }); target.scrollIntoView?.({ block: "center" }); }
        else scrollRoot.scrollTop = 0;
        return true;
      } catch (error) {
        if (!current()) return false;
        if (error.status === 409) { data = null; errorText = error.code === "native_text_changed" ? error.message : tr("来源已变化，请重新读取。", "The source changed. Read it again."); }
        else if (error.status === 404) { data = null; errorText = error.code === "native_text_unavailable" ? error.message : tr("这段对话的已绑定记录暂不可读取。", "The bound record is unavailable."); }
        else errorText = error.status ? tr("已有记录暂时无法读取。", "The existing record could not be read.") : error.message || tr("已有记录暂时无法读取。", "The existing record could not be read.");
        render(); return false;
      } finally { if (current()) { reading = false; renderControls(); onViewChange(context()); } }
    }
    function renderControls() {
      retry.disabled = reading; previous.disabled = reading || !data || data.offset === 0; next.disabled = reading || !data || data.next_offset === null; sources.disabled = reading;
      paging.hidden = !data || !data.total; sourceLabel.hidden = !data || data.sources.length < 2;
      storageNote.hidden = !storageFailed; storageNote.textContent = tr("阅读位置未能保存。", "The reading position could not be saved.");
    }
    function render() {
      summary.textContent = tr("更多", "More"); retry.textContent = tr("重新读取", "Read again"); previous.textContent = tr("前一页", "Previous"); next.textContent = tr("后一页", "Next");
      notice.textContent = tr("已保存的文字索引 · 仅阅读", "Saved text index · Read only");
      errorNode.textContent = errorText; errorNode.hidden = !errorText;
      excerptNode.textContent = excerpt ? tr("搜索命中：", "Search excerpt: ") + excerpt : ""; excerptNode.hidden = !excerpt || !!data?.located_message_id;
      messages.replaceChildren(); operations.replaceChildren();
      if (data) {
        const exactMatch = match?.session_id === data.source_session_id ? match : null;
        for (const row of data.messages) { const item = renderMessage(row, data.agent, exactMatch); if (item) (item.dataset.operationRecord === "true" ? operations : messages).append(item); }
        if (!data.messages.length) messages.append(node("p", "rbs-note", tr("这份索引没有可读正文。", "This index has no readable messages.")));
        range.textContent = data.total ? `${data.offset + 1}–${data.offset + data.messages.length} / ${data.total}` : "";
        sources.replaceChildren(...data.sources.map((source, index) => { const option = node("option", "", `${index + 1} · ${source.session_id}`); option.value = source.session_id; return option; })); sources.value = data.source_session_id;
      } else if (reading && !errorText) messages.append(node("p", "rbs-note", tr("正在读取…", "Reading…")));
      renderControls();
    }
    sources.addEventListener("change", () => { if (data?.sources.some(row => row.session_id === sources.value)) { match = null; return load({ source_session_id: sources.value, offset: 0 }); } });
    return {
      restore, select,
      selected: () => context(), getViewContext: () => visible ? context() : null, getHistorySourceContext,
      show() { restore(); if (!selected) return false; visible = true; root.hidden = false; return load(); },
      hide() { generation++; visible = reading = false; root.hidden = true; },
      clear() { generation++; selected = data = match = null; title = excerpt = errorText = ""; visible = reading = false; root.hidden = true; if (workspace) save(); },
      setLanguage(value) { language = value === "en" ? "en" : "zh-CN"; render(); },
    };
  }
  window.RabbitNativeIndexText = { create };
})();
