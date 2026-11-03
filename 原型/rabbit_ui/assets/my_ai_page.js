/* The overview reads saved facts. Actions remain in the existing work pages. */
(() => {
  "use strict";
  function create({ root, api, getWorkspace, onAction, onNewChat, onCheck, onRead = () => {}, headerActions = null }) {
    if (!root || [api, getWorkspace, onAction, onNewChat, onCheck].some(fn => typeof fn !== "function")) throw Error("我的 AI 页面入口不完整。");
    let visible = false, generation = 0, actionGeneration = 0, loading = false, checking = false, data = null, error = "", flight = null, expiry = null;
    const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
    const button = (label, callback, cls = "my-ai-link") => { const n = node("button", cls, label); n.type = "button"; n.addEventListener("click", callback); return n; };
    const report = node("div", "my-ai-report");
    let currentActions = null;
    const current = (snapshot, stamp, scope) => visible && stamp === actionGeneration && scope === getWorkspace() && snapshot === data && (!snapshot || snapshot.workspace_id === scope);
    function actionButton(label, action) {
      const snapshot = data, stamp = actionGeneration, scope = getWorkspace();
      return button(label, () => { if (current(snapshot, stamp, scope)) Promise.resolve().then(() => current(snapshot, stamp, scope) && onAction(action)).catch(e => { if (current(snapshot, stamp, scope)) { error = e?.message || "这项工作暂时没有打开，原稿保留。"; render(); } }); });
    }
    function resource(title, rows, kind, empty) {
      const section = node("section", "my-ai-resource");
      const head = node("div", "my-ai-section-heading"); head.append(node("h3", "", title), actionButton("查看全部", { kind: "manage", target: { kind, id: null } })); section.append(head);
      const list = node("ul", "my-ai-resources");
      const summary = kind === "agent" ? [...rows].sort((a, b) => Number(["running", "needs_user"].includes(b.status)) - Number(["running", "needs_user"].includes(a.status))) : rows;
      for (const row of summary.slice(0, 3)) {
        const item = node("li", "my-ai-resource-row"), control = actionButton(row.name, { kind: "manage", target: row.target });
        control.dataset.objectId = row.id; control.dataset.controlInterface = "ai";
        const status = node("span", "my-ai-status", row.status_label); status.dataset.state = row.status;
        item.append(control, status, node("p", "my-ai-resource-note", kind === "model" ? row.agent_name : kind === "memory" ? row.program : row.model_count ? `${row.model_count} 项模型目录` : "查看接入与工作"));
        list.append(item);
      }
      if (!rows.length) list.append(node("li", "my-ai-empty-note", empty));
      section.append(list);
      return section;
    }
    function render() {
      actionGeneration++;
      if (data && data.workspace_id !== getWorkspace()) data = null;
      root.hidden = !visible;
      const body = node("div", "my-ai-body"), hero = node("section", "my-ai-hero");
      const actions = node("div", "my-ai-actions");
      const primary = data?.empty ? actionButton("接入第一个 Agent", { kind: "manage", target: { kind: "agent", id: null } }) : button("新对话", onNewChat);
      primary.className = "my-ai-primary";
      const check = button(checking ? "正在核对…" : "核对并更新", checkNow, "my-ai-secondary"); check.disabled = checking || loading; actions.append(check, primary);
      currentActions = actions;
      if (visible && headerActions) headerActions.replaceChildren(actions);
      if (!headerActions) { hero.append(actions); body.append(hero); }
      if (error) { const note = node("p", "my-ai-notice", error); note.setAttribute("role", "status"); note.append(button("重新读取", refresh)); body.append(note); }
      if (!data) body.append(node("p", "my-ai-empty-note", loading ? "正在读取已有目录…" : "目录尚未读到。"));
      else {
        const resources = node("div", "my-ai-resource-grid");
        resources.append(resource("Agent", data.agents, "agent", "还没有已接入的 Agent。"), resource("大模型", data.models, "model", "目录尚未报告模型。"), resource("记忆", data.memories, "memory", "尚未保存记忆配置。"));
        body.append(resources);
        const recent = node("section", "my-ai-recent"), head = node("div", "my-ai-section-heading");
        head.append(node("h3", "", "最近工作"), actionButton("全部对话 →", { kind: "conversations" })); recent.append(head);
        const list = node("ul", "my-ai-work-list");
        for (const row of data.works) {
          const item = node("li", "my-ai-work"), description = node("div", "my-ai-work-copy"); item.dataset.workKey = row.key;
          description.append(node("strong", "", row.name), node("p", "my-ai-resource-note", [row.agent_name, row.status_label, row.detail].filter(Boolean).join(" · ")));
          const controls = node("div", "my-ai-work-actions");
          for (const action of row.actions) {
            if (action.kind === "original") { const link = node("a", "my-ai-link", action.label); link.href = action.url; link.dataset.controlInterface = "human";
              const snapshot = data, stamp = actionGeneration, scope = getWorkspace(); link.addEventListener("click", event => { if (!current(snapshot, stamp, scope)) event.preventDefault(); }); controls.append(link); }
            else controls.append(actionButton(action.label, action));
          }
          item.append(description, controls); list.append(item);
        }
        if (!data.works.length) list.append(node("li", "my-ai-empty-note", "在这里开始的工作，以及已索引的历史，会出现在这里。"));
        recent.append(list); body.append(recent);
        if (data.issues.length) body.append(node("p", "my-ai-notice", "部分目录暂未读完整；已有条目仍保留。"));
        body.append(node("p", "my-ai-facts-note", "首页展示已有目录和可观察的工作。模型标为“已发现”；记忆只显示配置，不读取正文。"));
      }
      body.append(report); root.replaceChildren(body);
    }
    function expireExecutionFacts() {
      if (!data || !visible) return;
      let changed = false;
      for (const row of [...data.agents, ...data.works]) if (["running", "needs_user"].includes(row.status)) {
        row.status = "unknown"; row.status_label = "状态待核对"; changed = true;
      }
      if (changed) render();
    }
    const signature = value => JSON.stringify(value && [value.workspace_id, value.agents, value.models, value.memories, value.works, value.empty, value.issues]);
    async function refresh({ quiet = false } = {}) {
      const scope = getWorkspace();
      if (flight?.scope === scope && flight.serial === generation) return flight.promise;
      const serial = ++generation;
      if (!scope) { render(); return false; }
      loading = true; const hadError = !!error; error = ""; if (!quiet || !data) render();
      const request = { scope, serial, promise: null }; flight = request;
      request.promise = (async () => { try {
        const value = await api(window.RabbitMyAI.PATH, { method: "GET" }, false);
        if (serial !== generation || scope !== getWorkspace()) return false;
        const next = window.RabbitMyAI.project(value, scope), changed = signature(data) !== signature(next);
        if (changed) data = next;
        clearTimeout(expiry); expiry = setTimeout(expireExecutionFacts, 6000);
        onRead(value); if (quiet && (changed || hadError)) render(); return true;
      } catch (e) { if (serial === generation) { error = e?.message || "已有目录暂时无法读取。"; expireExecutionFacts(); if (quiet) render(); } return false; }
      finally { if (flight === request) flight = null; if (serial === generation) { loading = false; if (!quiet || !data) render(); } }
      })(); return request.promise;
    }
    async function checkNow() {
      if (checking || loading) return;
      checking = true; error = ""; render();
      try { await onCheck(); await refresh(); }
      catch (e) { error = e?.message || "核对尚未完成，已有事实保留。"; }
      finally { checking = false; render(); }
    }
    return { show() { visible = true; render(); return refresh(); }, hide() { visible = false; generation++; loading = false; clearTimeout(expiry); render(); }, refresh, snapshot: () => data && data.workspace_id === getWorkspace() ? data : null, actions: () => currentActions, reportMount: () => report };
  }
  window.RabbitMyAIPage = Object.freeze({ create });
})();
