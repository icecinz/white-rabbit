/* Home reads saved cards. Refreshes, launches and setup require explicit actions. */
(() => {
  "use strict";
  const USAGE = "/api/management/usage-sources", BRIEF = "/api/management/briefing";
  const LAUNCH = "/api/management/agent-launch", MINIMAX = "minimax-token-plan";
  const localAgents = new Set(["codex", "hermes", "pi", "dsh"]);
  const dateTime = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const day = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" });
  const tokenCount = new Intl.NumberFormat("zh-CN");
  const isDetailMetric = field => field.detail === true || ["detail", "details"].includes(field.group);
  const node = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; };
  function button(label, key, click, cls = "dashboard-link") {
    const el = node("button", cls, label); el.type = "button"; el.dataset.dashboardFocus = key;
    el.addEventListener("click", () => click(el)); return el;
  }
  function stamp(value, prefix = "", suffix = "") {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.valueOf())) return null;
    const el = node("time", "dashboard-muted", prefix + dateTime.format(date) + suffix); el.dateTime = value; return el;
  }
  function heading(title, ...controls) {
    const el = node("header", "dashboard-section-heading");
    if (title) el.append(node("h2", "", title));
    const actions = node("div", "dashboard-section-actions"); actions.append(...controls); el.append(actions); return el;
  }
  function create({ api, getWorkspace, onConnection }) {
    let usage = null, brief = null, savedBrief = null, openedBrief = null;
    const refreshing = new Map();
    const launching = new Set();
    const live = record => !!(record && record.ctx.workspace === getWorkspace() && record.ctx.isCurrent());
    const activeUsage = record => record === usage && live(record);
    function canRefresh(card) {
      return card.can_refresh === true || (card.can_refresh === undefined && ["ready", "unread", "failed", "needs_login"].includes(card.status));
    }
    function external(entry) {
      if (!entry || typeof entry.url !== "string" || typeof entry.label !== "string") return null;
      try {
        const url = new URL(entry.url);
        if (!["https:", "codex:"].includes(url.protocol) || url.username || url.password) return null;
        const link = node("a", "dashboard-external", entry.label + " ↗"); link.href = entry.url;
        if (url.protocol === "https:") link.target = "_blank";
        link.rel = "noopener noreferrer"; return link;
      } catch (_) { return null; }
    }
    function launchDestination(agent, value) {
      const url = new URL(value);
      if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.username || url.password || url.hash) throw Error();
      if (agent === "hermes" && url.port === "9119" && url.pathname === "/chat") {
        const params = [...url.searchParams];
        if (!params.length || (params.length === 1 && params[0][0] === "profile" && params[0][1])) return url.href;
      }
      if (agent === "dsh" && url.port === "3080" && url.pathname === "/" && !url.search) return url.href;
      throw Error();
    }
    async function launchAgent(record, agent) {
      if (!activeUsage(record) || launching.has(agent)) return;
      // Reserve the tab while the click is still a browser user gesture.
      let tab = null;
      if (agent === "hermes" || agent === "dsh") {
        tab = window.open("about:blank", "_blank");
        if (tab) tab.opener = null;
      }
      launching.add(agent); record.launchMessages.delete(agent); paintUsage(record);
      try {
        const result = await api(LAUNCH, { method: "POST", body: JSON.stringify({ agent }) }, false);
        if (!activeUsage(record)) return;
        if (result.workspace_id !== record.ctx.workspace || result.opened !== true) throw Error();
        if (result.web_url == null) {
          record.launchMessages.set(agent, { text: "已打开。" });
          return;
        }
        const destination = launchDestination(agent, result.web_url);
        if (tab && !tab.closed) {
          tab.location.replace(destination);
          tab = null;
        } else {
          record.launchMessages.set(agent, { text: "网页已就绪。", url: destination });
        }
      } catch (error) {
        record.launchMessages.set(agent, { text: error.message || "暂时无法打开，请稍后再试。" });
      } finally {
        if (tab && !tab.closed) tab.close();
        launching.delete(agent);
        if (activeUsage(usage)) paintUsage(usage);
      }
    }
    function minimaxSetup(card, record) {
      if (!card.configuration) return null;
      if (record.minimaxSetup) return record.minimaxSetup;
      const config = card.configuration, details = node("details", "dashboard-breakdown dashboard-minimax-setup");
      details.append(node("summary", "dashboard-muted", "接入 MiniMax"));
      const form = node("form", "dashboard-minimax-form");
      form.autocomplete = "off";
      const region = node("select", "");
      region.setAttribute("aria-label", "账户站点");
      for (const [value, text] of [["cn", "中国站"], ["global", "国际站"]]) {
        const option = node("option", "", text); option.value = value; region.append(option);
      }
      region.value = config.region;
      function keyInput(label, prefix, configured) {
        const input = node("input", ""); input.type = "password"; input.maxLength = 519;
        input.autocomplete = "new-password"; input.placeholder = configured ? "已接入，留空保留" : prefix + "…";
        const field = node("label", "dashboard-muted", label); field.append(input);
        return { field, input };
      }
      const station = node("label", "dashboard-muted", "账户站点"); station.append(region);
      const account = keyInput("按量 API Key", "sk-api-", config.account_configured);
      const plan = keyInput("套餐密钥", "sk-cp-", config.subscription_configured);
      const save = node("button", "dashboard-update", "保存接入"); save.type = "submit";
      const status = node("p", "dashboard-muted", record.minimaxMessage || ""); status.setAttribute("role", "status");
      form.append(station, account.field, plan.field, node("p", "dashboard-muted", "按需填一项或两项。切换站点时，请重新填写要使用的密钥。"), save, status);
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (!activeUsage(record) || save.disabled) return;
        const account_key = account.input.value.trim(), subscription_key = plan.input.value.trim();
        if (!account_key && !subscription_key) { status.textContent = "请填写至少一种密钥。"; return; }
        save.disabled = true; status.textContent = "正在保存…";
        try {
          const result = await api(USAGE + "/minimax-configuration", { method: "POST",
            body: JSON.stringify({ region: region.value, account_key, subscription_key }) }, false);
          if (!activeUsage(record)) return;
          if (result.workspace_id !== record.ctx.workspace || !result.configuration) throw Error();
          account.input.value = plan.input.value = "";
          card.configuration = result.configuration;
          record.minimaxMessage = "已保存。点「更新」读取余额或套餐用量。";
          record.minimaxSetup = null;
          await readUsage(record);
        } catch (error) { status.textContent = error.message || "接入设置暂时无法保存。"; }
        finally { save.disabled = false; }
      });
      details.open = Boolean(record.minimaxMessage);
      details.append(form); record.minimaxSetup = details;
      return details;
    }
    function metric(row) {
      const cell = node("div", "dashboard-metric"); cell.append(node("dt", "", row.label));
      const unit = row.unit || "";
      const raw = String(row.value);
      const text = unit === "Token" && /^[0-9]{1,19}$/.test(raw) ? tokenCount.format(BigInt(raw)) : raw;
      const value = node("dd", "dashboard-value", text + (unit === "%" ? "%" : ""));
      if (unit && unit !== "%") value.append(node("span", "dashboard-unit", unit));
      const readAt = stamp(row.read_at, "该项上次读取：");
      if (readAt) { readAt.classList.add("dashboard-metric-read-at"); value.append(readAt); }
      cell.append(value);
      const window = Number.isFinite(row.window_minutes) && row.window_minutes > 0
        ? (row.window_minutes % 60 === 0 ? `${row.window_minutes / 60} 小时窗口` : `${row.window_minutes} 分钟窗口`) : "";
      const timing = stamp(row.resets_at, "", " 恢复");
      if (window || timing) {
        const details = node("dd", "dashboard-muted dashboard-metric-note");
        if (window) details.append(node("span", "", window)); if (timing) details.append(timing); cell.append(details);
      }
      return cell;
    }
    function sourceCard(card, record, { waiting = false, sharedRead = false } = {}) {
      const { ctx } = record, item = node("article", "dashboard-source-card");
      item.dataset.dashboardSource = card.id; item.tabIndex = -1;
      if (ctx.full && ctx.focusId === card.id) item.classList.add("dashboard-source-located");
      const head = node("header", "dashboard-source-head"), title = node("h3", "");
      if (ctx.full) title.textContent = card.title;
      else title.append(button(card.title + " ›", "usage-" + card.id, trigger => { if (activeUsage(record)) ctx.open(card.id, trigger); }));
      head.append(title);
      const agent = localAgents.has(card.entry?.action) ? card.entry.action : null;
      const entry = agent
        ? button(launching.has(agent) ? "正在打开…" : card.entry.label + " ↗", "usage-entry-" + card.id,
          () => void launchAgent(record, agent), "dashboard-external")
        : external(card.entry);
      if (entry) {
        entry.dataset.dashboardFocus = "usage-entry-" + card.id;
        if (agent) {
          if (launching.has(agent)) entry.setAttribute("aria-disabled", "true");
        } else entry.addEventListener("click", event => { if (!activeUsage(record)) event.preventDefault(); });
      }
      const otherEntries = (Array.isArray(card.additional_entries) ? card.additional_entries : []).map(external).filter(Boolean);
      if (otherEntries.length) {
        const actions = node("div", "dashboard-source-actions");
        if (entry) actions.append(entry);
        otherEntries.forEach((link, index) => {
          link.dataset.dashboardFocus = `usage-entry-${card.id}-${index + 1}`;
          link.addEventListener("click", event => { if (!activeUsage(record)) event.preventDefault(); });
          actions.append(link);
        });
        head.append(actions);
      } else if (entry) head.append(entry);
      const scope = node("p", "dashboard-muted dashboard-source-scope");
      const scopeLabel = {account: "账户", session: "会话"}[card.scope_kind];
      if (scopeLabel) scope.append(node("span", "dashboard-scope-label", scopeLabel));
      scope.append(card.scope || "");
      head.append(scope);
      item.append(head);
      const launchMessage = agent && record.launchMessages.get(agent);
      if (launchMessage) {
        const status = node("p", "dashboard-source-message", launchMessage.text);
        if (launchMessage.url) {
          const link = node("a", "dashboard-link", "在新标签页打开 ↗");
          link.href = launchMessage.url; link.target = "_blank"; link.rel = "noopener noreferrer";
          status.append(link);
        }
        status.setAttribute("role", "status"); item.append(status);
      }
      if (card.last_success_at) {
        const fields = Array.isArray(card.metrics) ? card.metrics : [];
        const groups = new Map();
        for (const field of fields.filter(field => !isDetailMetric(field))) {
          const group = field.group || ""; if (!groups.has(group)) groups.set(group, []); groups.get(group).push(field);
        }
        for (const [group, rows] of groups) {
          if (group && card.id === "codex-quota") item.append(node("p", "dashboard-muted dashboard-group", group));
          const list = node("dl", "dashboard-metrics"); list.append(...rows.map(metric)); item.append(list);
        }
        if (ctx.full) appendBreakdowns(item, card, fields.filter(isDetailMetric));
      }
      if (!ctx.full) {
        if (!card.last_success_at) {
          let hint = "尚未读取用量。";
          if (card.id === MINIMAX) hint = card.can_refresh ? "已接入，待更新。" : "在详情中接入余额与用量。";
          item.append(node("p", "dashboard-muted", hint));
        }
        return item;
      }
      const details = (Array.isArray(card.detail_entries) ? card.detail_entries : []).map(external).filter(Boolean);
      if (details.length) {
        const links = node("div", "dashboard-source-links"); links.append(...details); item.append(links);
      }
      if (card.id === MINIMAX) {
        const setup = minimaxSetup(card, record);
        if (setup) item.append(setup);
      }
      if (waiting || card.message || card.last_attempt_status === "failed" || card.status === "failed") {
        const reason = node("p", "dashboard-source-message", card.message || (waiting ? "尚未读取。" : "更新失败，保留上次读数。"));
        item.append(reason);
        if (["needs_login", "needs_configuration", "unavailable"].includes(card.status) && card.connection_agent) {
          item.append(button("查看接入说明 →", "connection-" + card.id, () => { if (activeUsage(record)) onConnection(card.connection_agent); }));
        }
      }
      const success = !sharedRead && stamp(card.last_success_at, "上次读取："), attempt = card.last_attempt_status === "failed" && stamp(card.last_attempt_at, "本次尝试：");
      if (success || attempt) { const foot = node("footer", "dashboard-source-foot"); if (success) foot.append(success); if (attempt) foot.append(attempt); item.append(foot); }
      return item;
    }
    function appendBreakdowns(item, card, fields) {
      const breakdowns = new Map();
      for (const field of fields) {
        const group = card.id === "deepseek-balance" ? "余额明细"
          : !field.group || ["detail", "details"].includes(field.group)
            ? (card.scope_kind === "session" ? "会话明细" : "来源明细") : field.group;
        if (!breakdowns.has(group)) breakdowns.set(group, []);
        breakdowns.get(group).push(field);
      }
      for (const [group, rows] of breakdowns) {
        const daily = group === "按日 Token";
        const collapsible = daily || group === "账户活动";
        const breakdown = node(collapsible ? "details" : "div", "dashboard-breakdown");
        const list = node("dl", daily ? "dashboard-daily-list" : "");
        list.append(...rows.map(metric));
        const heading = daily ? `${group} · ${rows.length} 个日期` : group;
        breakdown.append(node(collapsible ? "summary" : "h4", "dashboard-muted", heading));
        if (daily) {
          breakdown.append(node("p", "dashboard-muted dashboard-daily-note", "按 Codex 返回的日期显示，最多 366 条。"));
          list.tabIndex = 0;
        }
        breakdown.append(list);
        item.append(breakdown);
      }
    }
    function paintUsage(record) {
      if (!activeUsage(record)) return;
      const { mount, ctx } = record, cards = record.cards || [];
      const focused = mount.contains(document.activeElement) ? document.activeElement : null;
      const focusedKey = focused?.dataset.dashboardFocus, focusedSource = focused?.dataset.dashboardSource;
      const ready = cards.filter(card => card.last_success_at), waiting = cards.filter(card => !card.last_success_at);
      const eligible = cards.filter(canRefresh), pending = refreshing.has(ctx.workspace);
      // Combine timestamps only when the metrics visible here share a minute.
      const minute = Math.floor(Date.parse(ready[0]?.last_success_at) / 60000);
      const sameRead = ready.length > 1 && Number.isFinite(minute) && ready.every(card =>
        card.status === "ready" && card.last_attempt_status !== "failed"
        && !(Array.isArray(card.metrics) && card.metrics.some(field => field.read_at && (ctx.full || !isDetailMetric(field))))
        && Math.floor(Date.parse(card.last_success_at) / 60000) === minute);
      const shared = sameRead ? stamp(ready[0].last_success_at, "上次读取：") : null;
      const update = button(pending ? "正在更新…" : "更新", "usage-update", () => updateUsage(record), "dashboard-update");
      update.disabled = !eligible.length; if (pending) update.setAttribute("aria-disabled", "true");
      const refreshControls = node("div", "dashboard-refresh-controls");
      if (shared) refreshControls.append(shared);
      refreshControls.append(update);
      const details = button("详情 →", "usage-all", trigger => { if (activeUsage(record)) ctx.open(null, trigger); });
      mount.replaceChildren(heading(ctx.full ? null : "额度与用量", refreshControls, ...(!ctx.full ? [details] : [])));
      if (record.error) {
        const note = node("p", "dashboard-empty", record.error); note.setAttribute("role", "status");
        note.append(button("重新读取", "usage-reload", () => { if (activeUsage(record)) void readUsage(record); })); mount.append(note);
      }
      const cardsToShow = ctx.full ? ready : cards.filter(card => card.last_success_at || card.entry || card.configuration);
      if (cardsToShow.length) {
        const grid = node("div", `dashboard-source-grid${ctx.full || cardsToShow.length > 3 ? " dashboard-source-full" : ""}`);
        grid.append(...cardsToShow.map(card => sourceCard(card, record, { sharedRead: Boolean(shared) }))); mount.append(grid);
        if (!ctx.full && !record.error && ready.some(card => card.last_attempt_status === "failed" || card.status === "failed")) {
          const notice = node("p", "dashboard-source-message", "部分来源更新未完成，卡片保留上次读数。");
          notice.setAttribute("role", "status"); mount.append(notice);
        }
      } else if (!record.error) mount.append(node("p", "dashboard-empty", record.cards ? "还没有读到额度或用量。" : "正在读取已保存的来源…"));
      if (ctx.full && waiting.length) {
        const list = node("div", "dashboard-source-grid dashboard-source-full dashboard-source-waiting");
        list.append(...waiting.map(card => sourceCard(card, record, { waiting: true }))); mount.append(node("h3", "dashboard-waiting-title", "尚未读到"), list);
      }
      if (focusedKey) [...mount.querySelectorAll("[data-dashboard-focus]")].find(el => el.dataset.dashboardFocus === focusedKey)?.focus({ preventScroll: true });
      if (focusedSource) [...mount.querySelectorAll("[data-dashboard-source]")].find(el => el.dataset.dashboardSource === focusedSource)?.focus({ preventScroll: true });
      if (ctx.full && ctx.focusId && !record.located && record.cards) {
        const target = [...mount.querySelectorAll("[data-dashboard-source]")].find(el => el.dataset.dashboardSource === ctx.focusId);
        if (target) { record.located = true; target.classList.add("dashboard-source-located"); target.focus({ preventScroll: true }); target.scrollIntoView({ block: "nearest" }); }
      }
      if (record.cards || record.error) ctx.onReady?.();
    }
    async function readUsage(record) {
      const serial = ++record.serial;
      record.error = ""; paintUsage(record);
      try {
        const value = await api(USAGE, { method: "GET" }, false);
        if (!activeUsage(record) || serial !== record.serial) return;
        if (value.workspace_id !== record.ctx.workspace || !Array.isArray(value.sources)) throw Error();
        record.cards = value.sources;
      } catch (_) { if (activeUsage(record) && serial === record.serial) record.error = "来源暂时未能读取。"; }
      paintUsage(record);
    }
    async function updateUsage(record) {
      if (!activeUsage(record)) return;
      const scope = record.ctx.workspace, ids = (record.cards || []).filter(canRefresh).map(card => card.id);
      if (!ids.length || refreshing.has(scope)) return;
      const requests = Promise.allSettled(ids.map(source_id => api(USAGE + "/refresh", { method: "POST", body: JSON.stringify({ source_id }) }, false)));
      refreshing.set(scope, requests); paintUsage(record);
      const results = await requests;
      if (refreshing.get(scope) === requests) refreshing.delete(scope);
      if (usage?.ctx.workspace !== scope || !activeUsage(usage)) return;
      const current = usage;
      await readUsage(current);
      if (activeUsage(current) && results.some(result => result.status === "rejected")) {
        current.error ||= "有来源未完成更新，已保存的读数仍保留。"; paintUsage(current);
      }
    }
    function renderUsage(mount, ctx) {
      if (!usage || usage.ctx.workspace !== ctx.workspace || usage.ctx.visit !== ctx.visit) {
        usage = { mount, ctx, cards: null, error: "", located: false, serial: 0, launchMessages: new Map() };
        if (ctx.workspace) void readUsage(usage); else paintUsage(usage);
      } else { usage.mount = mount; usage.ctx = ctx; paintUsage(usage); }
    }
    function paintBrief(record) {
      if (record !== brief || !live(record)) return;
      const { mount, ctx, value, status } = record;
      mount.replaceChildren(); if (!ctx.full) mount.append(heading("简报"));
      if (!value) {
        mount.append(node("p", "dashboard-empty", status === "changed" ? "本地简报已变化。返回首页可查看当前简报。" : status === "empty" ? "暂时没有简报。" : status === "unavailable" ? "简报暂时无法读取。" : "正在读取简报…"));
        if (status !== "loading") ctx.onReady?.(); return;
      }
      const card = node("article", `dashboard-brief${ctx.full ? " dashboard-brief-full" : ""}`), date = node("time", "dashboard-muted", day.format(new Date(value.date + "T00:00:00Z")));
      date.dateTime = value.date; card.append(date);
      if (value.snapshot_at) {
        const mirror = stamp(value.snapshot_at, "本机副本 · ", " 更新");
        if (mirror) { mirror.classList.add("dashboard-brief-mirror"); card.append(mirror); }
      }
      const title = node(ctx.full ? "h2" : "h3", "");
      const open = trigger => {
        if (record !== brief || !live(record)) return;
        openedBrief = { scope: ctx.workspace, value }; ctx.open(value.revision, trigger);
      };
      if (ctx.full) title.textContent = value.title; else title.append(button(value.title, "brief-title", open, "dashboard-brief-title"));
      card.append(title, node("p", "dashboard-brief-lead", value.summary));
      if (ctx.full) {
        const body = node("div", "dashboard-brief-text");
        if (window.OneAIRichText?.render) window.OneAIRichText.render(body, value.body);
        else body.textContent = value.body;
        card.append(body);
      } else card.append(button("读简报 →", "brief-read", open));
      mount.append(card);
      ctx.onReady?.();
    }
    async function readBrief(record) {
      try {
        const value = await api(BRIEF, { method: "GET" }, false);
        if (record !== brief || !live(record)) return;
        if (value.workspace_id !== record.ctx.workspace) throw Error();
        record.status = value.status;
        record.value = value.status === "ready" ? value.briefing : null;
        if (record.ctx.full && record.value?.revision !== record.ctx.focusId) {
          record.value = null;
          if (record.status !== "unavailable") record.status = "changed";
        }
        if (!record.ctx.full) savedBrief = { scope: record.ctx.workspace, value: record.value, status: record.status };
      } catch (_) { if (record === brief && live(record)) { record.status = "unavailable"; record.value = null; } }
      paintBrief(record);
    }
    function renderBrief(mount, ctx) {
      if (!brief || brief.ctx.workspace !== ctx.workspace || brief.ctx.visit !== ctx.visit) {
        const pinned = ctx.full && openedBrief?.scope === ctx.workspace && openedBrief.value.revision === ctx.focusId ? openedBrief.value : null;
        const cached = !ctx.full && savedBrief?.scope === ctx.workspace ? savedBrief : null;
        brief = { mount, ctx, value: pinned || cached?.value || null, status: pinned ? "ready" : cached?.status || "loading" };
        paintBrief(brief); if (ctx.workspace && !pinned) void readBrief(brief);
      } else { brief.mount = mount; brief.ctx = ctx; paintBrief(brief); }
    }
    return { sections: { usage: { render: renderUsage }, brief: { render: renderBrief } } };
  }
  window.RabbitDashboardSections = Object.freeze({ create });
})();
