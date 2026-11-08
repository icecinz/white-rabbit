/* The dashboard mounts cached facts and keeps each visit separate from account refresh. */
(() => {
  "use strict";
  const TITLES = Object.freeze({ usage: "额度与用量", brief: "简报" });
  const THREAD_URL = /^codex:\/\/threads\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
  const execution = status => status === "running" || status === "needs_user";

  function node(tag, cls, text) {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = text;
    return value;
  }

  function button(label, callback, cls = "my-ai-link") {
    const value = node("button", cls, label);
    value.type = "button";
    value.addEventListener("click", callback);
    return value;
  }

  function create({ root, api, getWorkspace, onAction, onRead = () => {}, onViewChange = () => {}, sections = {} }) {
    if (!root || [api, getWorkspace, onAction].some(fn => typeof fn !== "function")) throw Error("我的 AI 页面入口不完整。");
    const body = node("div", "my-ai-body"), home = node("div", "my-ai-home");
    const agentMount = node("section", "my-ai-agents");
    const mounts = { usage: node("section", "my-ai-extra my-ai-usage"), brief: node("section", "my-ai-extra my-ai-brief") };
    const full = node("div", "my-ai-subpage");
    const privacy = node("footer", "my-ai-privacy");
    const dot = node("span", "privacy-dot"); dot.setAttribute("aria-hidden", "true");
    privacy.append(dot, node("span", "", "记录保存在这台电脑"));
    home.append(agentMount, mounts.usage, mounts.brief, privacy);
    root.replaceChildren(body);
    let visible = false, workspace = "", active = null, expanded = false, visit = 0;
    let scrollTop = 0, homeReturn = null, detailReturn = null, viewReturn = null;
    let generation = 0, controlVersion = 0, loading = false, data = null, error = "", flight = null, expiry = null;

    function focusOf(trigger) {
      return { trigger, key: trigger?.dataset.dashboardFocus, top: root.scrollTop || 0 };
    }

    function restoreFocus(location) {
      if (!location) return false;
      const trigger = location.trigger;
      const replacement = location.key && [...root.querySelectorAll("[data-dashboard-focus]")]
        .find(control => control.dataset.dashboardFocus === location.key);
      const control = trigger && root.contains(trigger) ? trigger : replacement;
      control?.focus({ preventScroll: true });
      root.scrollTop = location.top;
      return Boolean(control);
    }

    function syncWorkspace() {
      const next = getWorkspace();
      if (next === workspace) return false;
      workspace = next;
      data = null;
      loading = false;
      error = "";
      active = null;
      expanded = false;
      scrollTop = 0;
      homeReturn = detailReturn = viewReturn = null;
      generation++;
      clearTimeout(expiry);
      return true;
    }

    function currentControl(stamp, scope) {
      return visible && !active && stamp === controlVersion && workspace === scope && scope === getWorkspace();
    }

    function agentAction(label, target, focusKey) {
      const stamp = controlVersion, scope = workspace;
      const control = button(label, () => {
        if (!currentControl(stamp, scope)) return;
        detailReturn = focusOf(control);
        Promise.resolve().then(() => currentControl(stamp, scope) && onAction({ kind: "manage", target }))
          .catch(failure => {
            if (!currentControl(stamp, scope)) return;
            error = failure?.message || "暂时无法打开这位 Agent 的详情。";
            renderAgents();
          });
      });
      control.dataset.dashboardFocus = focusKey;
      return control;
    }

    function agentStatus(row) {
      const status = node("span", "my-ai-status", row.status_label);
      status.dataset.state = row.status;
      return status;
    }

    function modelLabel(row) {
      if (row.recorded_model) return "记录中的模型 " + row.recorded_model;
      return row.model_count ? `已发现 ${row.model_count} 项模型` : "模型待核对";
    }

    function renderAgents() {
      const wasFocused = document.activeElement;
      const focus = wasFocused && agentMount.contains(wasFocused) ? focusOf(wasFocused) : null;
      controlVersion++;
      const stamp = controlVersion, scope = workspace;
      const rows = data?.agents || [];
      const head = node("div", "my-ai-section-heading");
      const title = node("h3", "", "常用 Agent");
      title.id = "my-ai-agents-heading";
      agentMount.setAttribute("aria-labelledby", title.id);
      head.append(title);
      const content = [head];
      const toggle = control => {
        if (!currentControl(stamp, scope)) return;
        expanded = !expanded;
        renderAgents();
        if (control.dataset.dashboardFocus === "agent-summary") {
          agentMount.querySelector('[data-dashboard-focus="agent-expand"]')?.focus({ preventScroll: true });
        }
        onViewChange();
      };
      if (rows.length) {
        const expand = button(expanded ? "收起 ↑" : "展开 ↓", () => toggle(expand));
        expand.dataset.dashboardFocus = "agent-expand";
        expand.setAttribute("aria-expanded", String(expanded));
        expand.setAttribute("aria-controls", "my-ai-agent-rows");
        head.append(expand);
        const box = node("div", "my-ai-agent-box");
        if (!expanded) {
          const summary = button("", () => toggle(summary), "my-ai-agent-summary-toggle");
          summary.dataset.dashboardFocus = "agent-summary";
          summary.setAttribute("aria-expanded", "false");
          summary.setAttribute("aria-controls", "my-ai-agent-rows");
          summary.setAttribute("aria-label", "展开常用 Agent");
          const strip = node("span", "my-ai-agent-strip");
          const featured = [...rows].sort((a, b) => Number(execution(b.status)) - Number(execution(a.status)));
          for (const row of featured.slice(0, 3)) {
            const item = node("span", "my-ai-agent-summary");
            const name = node("strong", "my-ai-agent-name", row.name);
            name.dataset.state = row.status;
            item.append(name, node("small", "my-ai-agent-model", modelLabel(row)), agentStatus(row));
            strip.append(item);
          }
          summary.append(strip);
          box.append(summary);
        }
        const list = node("ul", "my-ai-agent-rows");
        list.id = "my-ai-agent-rows";
        list.hidden = !expanded;
        if (expanded) for (const row of rows) {
          const item = node("li", "my-ai-agent-row"), identity = node("div", "my-ai-agent-identity");
          item.dataset.agentId = row.id;
          const target = { ...row.target, fromMyAI: true };
          const details = agentAction(row.name, target, "agent-" + row.id);
          details.dataset.objectId = row.id;
          identity.append(details, node("small", "my-ai-agent-model", modelLabel(row)));
          item.append(identity, agentStatus(row));
          if (THREAD_URL.test(row.original_url || "")) {
            const link = node("a", "my-ai-external", "在 Codex 打开工作 ↗");
            link.href = row.original_url;
            link.addEventListener("click", event => { if (!currentControl(stamp, scope)) event.preventDefault(); });
            item.append(link);
          } else if (["needs_attention", "unverified"].includes(row.status)) {
            item.append(agentAction("接入说明 →", { ...target, section: "connection" }, "connection-" + row.id));
          }
          list.append(item);
        }
        box.append(list);
        content.push(box);
      } else {
        const empty = node("div", "my-ai-agent-box my-ai-empty-note", data ? "还没有可展示的 Agent。" : loading ? "正在读取 Agent 目录…" : "Agent 目录尚未读到。");
        if (data) empty.append(agentAction("查看 Agent 接入 →", { kind: "agent", id: null }, "agent-connect"));
        content.push(empty);
      }
      if (error || data?.issues.length) {
        const notice = node("p", "my-ai-notice", error || "部分目录暂未读完整。");
        notice.setAttribute("role", "status");
        if (error) notice.append(button("重新读取", () => currentControl(stamp, scope) && refresh()));
        content.push(notice);
      }
      agentMount.replaceChildren(...content);
      restoreFocus(focus);
    }

    function renderView(location = null) {
      const currentVisit = ++visit, scope = workspace, view = active;
      root.dataset.dashboardView = view?.kind || "home";
      root.setAttribute("aria-label", view ? TITLES[view.kind] : "我的 AI");
      const isCurrent = () => visible && currentVisit === visit && scope === getWorkspace();
      const previousFocus = document.activeElement;
      let pending = location, initialTop = null;
      const back = () => {
        if (!isCurrent() || !active) return;
        active = null;
        renderAgents();
        renderView(homeReturn);
        homeReturn = null;
        onViewChange();
      };
      let backButton;
      function mount(kind, target) {
        target.replaceChildren();
        sections[kind]?.render(target, {
          full: Boolean(view), focusId: view?.id || null, workspace: scope, visit: currentVisit, isCurrent, back,
          onReady() {
            if (!pending || !isCurrent() || pending.section && pending.section !== kind) return;
            const focus = document.activeElement;
            if ((!focus || focus === previousFocus || focus === document.body) && (initialTop === null || root.scrollTop === initialTop)) restoreFocus(pending);
            pending = null;
          },
          open(id = null, trigger = document.activeElement) {
            if (!isCurrent() || view?.kind === kind && view.id === id) return;
            if (!view) homeReturn = { ...focusOf(trigger), section: kind };
            active = { kind, id };
            const nextBack = renderView();
            root.scrollTop = 0;
            nextBack?.focus({ preventScroll: true });
            onViewChange();
          }
        });
      }
      if (view) {
        const heading = node("header", "my-ai-subpage-heading");
        backButton = button("← 我的 AI", back);
        backButton.dataset.dashboardFocus = "dashboard-back";
        heading.append(backButton, node("h2", "", TITLES[view.kind]));
        const target = node("section", "my-ai-extra my-ai-" + view.kind);
        full.replaceChildren(heading, target);
        body.replaceChildren(full);
        mount(view.kind, target);
      } else {
        body.replaceChildren(home);
        for (const kind of Object.keys(TITLES)) mount(kind, mounts[kind]);
      }
      if (pending && isCurrent()) {
        if (restoreFocus(pending)) pending = null;
        initialTop = root.scrollTop;
      }
      return backButton;
    }

    function expireExecutionFacts() {
      if (!data) return;
      let changed = false;
      for (const row of data.agents) {
        if (execution(row.status)) {
          row.status = "unknown";
          row.status_label = "状态待核对";
          changed = true;
        }
        if (row.original_url) {
          row.original_url = null;
          changed = true;
        }
      }
      if (changed && visible && !active) renderAgents();
    }

    const signature = value => JSON.stringify(value && [value.workspace_id, value.agents, value.models, value.issues]);
    function refresh({ quiet = false } = {}) {
      if (!visible) return Promise.resolve(false);
      if (syncWorkspace()) {
        renderAgents();
        renderView();
        root.scrollTop = 0;
      }
      const scope = workspace;
      if (!scope) return Promise.resolve(false);
      if (flight?.scope === scope && flight.serial === generation) return flight.promise;
      const serial = ++generation;
      loading = true;
      const hadError = Boolean(error);
      error = "";
      if ((!quiet || !data) && !active) renderAgents();
      const request = { scope, serial, promise: null };
      flight = request;
      request.promise = (async () => {
        const isCurrent = () => visible && serial === generation && scope === getWorkspace();
        try {
          const value = await api(window.RabbitMyAI.DASHBOARD_PATH, { method: "GET" }, false);
          if (!isCurrent()) return false;
          const next = window.RabbitMyAI.project(value, scope);
          const changed = signature(data) !== signature(next);
          if (changed) data = next;
          clearTimeout(expiry);
          expiry = setTimeout(expireExecutionFacts, 6000);
          onRead(value);
          if (!active && (changed || hadError)) renderAgents();
          return true;
        } catch (failure) {
          if (isCurrent()) {
            error = failure?.message || "已有目录暂时无法读取。";
            expireExecutionFacts();
          }
          return false;
        } finally {
          if (flight === request) flight = null;
          if (isCurrent()) {
            loading = false;
            if (!active && (error || !quiet || !data)) renderAgents();
          }
        }
      })();
      return request.promise;
    }

    return {
      show() {
        visible = true;
        root.hidden = false;
        syncWorkspace();
        renderAgents();
        renderView(active ? viewReturn || { top: scrollTop, section: active.kind } : null);
        viewReturn = null;
        if (!active) root.scrollTop = scrollTop;
        if (!active && restoreFocus(detailReturn)) detailReturn = null;
        const showVisit = visit;
        return refresh({ quiet: true }).then(result => {
          if (visible && showVisit === visit && !active && detailReturn) {
            restoreFocus(detailReturn);
            detailReturn = null;
          }
          return result;
        });
      },
      hide() {
        if (!visible) return;
        scrollTop = root.scrollTop || 0;
        if (active) {
          const trigger = document.activeElement;
          viewReturn = { ...focusOf(trigger && root.contains(trigger) ? trigger : null), section: active.kind };
        }
        visible = false;
        root.hidden = true;
        generation++;
        visit++;
        loading = false;
        clearTimeout(expiry);
        expireExecutionFacts();
      },
      resetView() {
        active = null;
        homeReturn = detailReturn = viewReturn = null;
        scrollTop = 0;
      },
      getView() {
        if (!workspace || workspace !== getWorkspace()) return null;
        const top = value => Math.min(1e9, Math.max(0, Math.floor(Number(value) || 0)));
        return { kind: active?.kind || "home", focusId: active?.id || null, expanded,
          scrollTop: top(visible ? root.scrollTop : scrollTop), homeScrollTop: top(homeReturn?.top),
          homeFocus: homeReturn?.key || detailReturn?.key || null };
      },
      restoreView(view) {
        if (!view || !["home", ...Object.keys(TITLES)].includes(view.kind)) return;
        syncWorkspace();
        active = view.kind === "home" ? null : { kind: view.kind, id: view.focusId };
        expanded = view.expanded === true;
        scrollTop = view.scrollTop;
        homeReturn = active ? { key: view.homeFocus, top: view.homeScrollTop, section: active.kind } : null;
        detailReturn = !active && view.homeFocus ? { key: view.homeFocus, top: scrollTop } : null;
      },
      refresh,
      snapshot: () => data && data.workspace_id === getWorkspace() ? data : null,
    };
  }

  window.RabbitMyAIPage = Object.freeze({ create });
})();
