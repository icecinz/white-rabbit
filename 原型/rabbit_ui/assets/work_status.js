(() => {
  "use strict";
  const PATH = "/api/management/work-status", INTERVAL = 3000, TIMEOUT = 2500, MAX_AGE = 6000;
  const EXTERNAL_PATH = "/api/management/external-work", EXTERNAL_TIMEOUT = 5500;
  const agents = Object.freeze({ codex: "Codex", pi: "Pi", hermes: "Hermes", claude: "Claude Code", dsh: "DeepSeek Harness" });
  const object = value => !!value && typeof value === "object" && !Array.isArray(value);
  const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
  const code = value => value === null || typeof value === "string" && /^[a-z][a-z_]{0,47}$/.test(value);
  const epoch = value => value === null || typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
  const session = value => value === null || typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(value);
  const text = value => typeof value === "string" && value.length > 0 && value.length <= 160 && !/[\x00-\x1f\x7f]/.test(value);
  const timestamp = value => typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value));
  const origin = value => typeof value === "string" && /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(value) && Number(value.split(":").at(-1)) <= 65535;
  const nullableText = (value, limit) => value === null || typeof value === "string" && Array.from(value).length <= limit && !/[\x00-\x1f\x7f\ud800-\udfff]/u.test(value);
  const encodeSession = value => encodeURIComponent(value).replace(/[!'()*]/g, char => "%" + char.charCodeAt(0).toString(16).toUpperCase());
  function externalSnapshot(value, workspace) {
    const fail = () => { throw Error("invalid_external_work"); };
    const reasons = ["auth_required", "connection_unavailable", "http_error", "redirect_refused", "invalid_response", "response_limit", "observation_unavailable"];
    if (!exact(value, ["schema_version", "workspace_id", "provider", "scope", "revision", "origin", "state", "reason", "report"])
        || value.schema_version !== 1 || value.workspace_id !== workspace || value.provider !== "hapi" || value.scope !== "external_hub"
        || !Number.isSafeInteger(value.revision) || value.revision < 0 || !["unconfigured", "unverified", "observed", "unavailable"].includes(value.state)
        || (value.state === "unavailable" ? !reasons.includes(value.reason) : value.reason !== null)
        || (value.state === "unconfigured" ? value.origin !== null : !origin(value.origin))) fail();
    if (value.state !== "observed") { if (value.report !== null) fail(); return value; }
    const report = value.report;
    if (!exact(report, ["schema_version", "provider", "scope", "origin", "snapshot_at", "native_event_freshness", "native_executor_verified", "capabilities", "complete", "reason", "works"])
        || report.schema_version !== 1 || report.provider !== "hapi" || report.scope !== "hapi_hub_summary" || report.origin !== value.origin
        || !timestamp(report.snapshot_at) || report.native_event_freshness !== "unknown" || report.native_executor_verified !== false
        || !exact(report.capabilities, ["send", "approve", "stop"]) || Object.values(report.capabilities).some(flag => flag !== false)
        || report.complete !== true || report.reason !== null || !Array.isArray(report.works) || report.works.length > 500) fail();
    const seen = new Set();
    for (const row of report.works) {
      if (!exact(row, ["id", "metadata", "active", "thinking", "activeAt", "updatedAt", "pendingRequestsCount", "backgroundTaskCount", "model", "modelReasoningEffort", "effort", "reported_status", "url"])
          || !nullableText(row.id, 256) || !row.id || [".", ".."].includes(row.id) || seen.has(row.id)
          || typeof row.active !== "boolean" || typeof row.thinking !== "boolean"
          || [row.activeAt, row.updatedAt].some(number => typeof number !== "number" || !Number.isFinite(number) || number < 0 || number > Number.MAX_SAFE_INTEGER)
          || [row.pendingRequestsCount, row.backgroundTaskCount].some(number => !Number.isInteger(number) || number < 0 || number > 1000000)
          || [row.model, row.modelReasoningEffort, row.effort].some(value => !nullableText(value, 256))
          || row.url !== value.origin + "/sessions/" + encodeSession(row.id)
          || row.reported_status !== (!row.active ? "inactive" : row.pendingRequestsCount ? "needs_user" : row.thinking ? "thinking" : "active")) fail();
      if (row.metadata !== null && (!exact(row.metadata, ["name", "path", "flavor", "agentSessionId"])
          || !nullableText(row.metadata.name, 512) || !nullableText(row.metadata.path, 4096)
          || !nullableText(row.metadata.flavor, 64) || !nullableText(row.metadata.agentSessionId, 256))) fail();
      seen.add(row.id);
    }
    return value;
  }
  function snapshot(value, workspace) {
    const fail = () => { throw Error("invalid_work_status"); };
    if (!exact(value, ["schema_version", "workspace_id", "scope", "snapshot_at", "external_work", "complete", "agents", "works"])
        || value.schema_version !== 1 || value.workspace_id !== workspace || value.scope !== "rabbit_owned_cache"
        || value.external_work !== "not_observed" || typeof value.complete !== "boolean"
        || typeof value.snapshot_at !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value.snapshot_at)
        || !Number.isFinite(Date.parse(value.snapshot_at)) || !Array.isArray(value.agents) || value.agents.length !== 5
        || !Array.isArray(value.works) || value.works.length > 7) fail();
    const byAgent = new Map(), seen = new Set(), counts = {};
    for (const row of value.agents) {
      if (!exact(row, ["agent", "enabled", "complete", "reason"]) || typeof row.agent !== "string" || !Object.hasOwn(agents, row.agent)
          || byAgent.has(row.agent) || typeof row.enabled !== "boolean" || typeof row.complete !== "boolean" || !code(row.reason)) fail();
      byAgent.set(row.agent, { ...row });
    }
    const works = value.works.map(row => {
      if (!exact(row, ["agent", "role", "chat_id", "owner", "complete", "reason", "status", "connection_state", "epoch", "native_session_id", "pending_requests"])
          || !byAgent.get(row.agent)?.enabled || !["chat", "specialist"].includes(row.role) || row.owner !== "rabbit"
          || typeof row.complete !== "boolean" || !code(row.reason) || !epoch(row.epoch) || !session(row.native_session_id)
          || !["running", "needs_user", "idle", "unknown"].includes(row.status)
          || !["not_open", "opening", "ready", "failed", "closed", "unknown"].includes(row.connection_state)
          || row.pending_requests !== null && (!Number.isInteger(row.pending_requests) || row.pending_requests < 0 || row.pending_requests > 129)) fail();
      if (row.role === "specialist") { if (row.agent !== "codex" || row.chat_id !== null) fail(); }
      else if (!uuid(row.chat_id) || (counts[row.agent] = (counts[row.agent] || 0) + 1) > (row.agent === "codex" ? 2 : 1)) fail();
      const key = `${row.agent}/${row.role}/${row.chat_id}`;
      if (seen.has(key) || !row.complete && (row.status !== "unknown" || byAgent.get(row.agent).complete)
          || row.status === "idle" && (row.connection_state !== "ready" || row.epoch === null || row.native_session_id === null || row.pending_requests !== 0)
          || row.status === "needs_user" && !(row.pending_requests > 0)) fail();
      seen.add(key); return { ...row };
    });
    if (value.complete !== [...byAgent.values()].every(row => row.complete)) fail();
    return { complete: value.complete, agents: byAgent, works };
  }
  const sameSnapshot = (left, right) => !!left && left.complete === right.complete && left.works.length === right.works.length
    && Object.keys(agents).every(agent => ["enabled", "complete", "reason"].every(key => left.agents.get(agent)[key] === right.agents.get(agent)[key]))
    && right.works.every((row, index) => Object.keys(row).every(key => row[key] === left.works[index][key]));
  function create({ api, getWorkspace, onOpen, translate = value => value, getLabel = () => "" }) {
    if ([api, getWorkspace, onOpen, translate, getLabel].some(value => typeof value !== "function")) throw Error("work_status_entry_required");
    const node = (tag, cls, content) => { const result = document.createElement(tag); result.className = cls; if (content !== undefined) result.textContent = content; return result; };
    const root = node("section", "rabbit-work-status"), heading = node("h3", "", translate("当前工作"));
    const list = node("ul", "rabbit-work-list"), notice = node("p", "rabbit-work-notice");
    const external = node("section", "rabbit-external-work"), externalHeading = node("h3", ""), externalTime = node("p", "rabbit-work-notice"), externalList = node("ul", "rabbit-work-list"), externalNotice = node("p", "rabbit-work-notice");
    external.append(externalHeading, externalTime, externalList, externalNotice); external.hidden = true;
    externalNotice.setAttribute("role", "status"); externalNotice.setAttribute("aria-live", "polite");
    root.setAttribute("data-control-interface", "ai"); root.title = translate("仅观察小白兔已连接的工作；不包括原应用中的其他工作。");
    notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite"); root.append(heading, list, notice, external); root.hidden = true;
    let visible = false, suspended = false, workspace = "", generation = 0, timer = null, flight = null, value = null, receivedAt = null, rendered = null;
    let externalValue = null, externalKnown = false, externalSeen = false, externalFlight = null, externalGeneration = 0, externalRendered = null, externalFocused = true;
    const currentWorkspace = () => { try { const id = getWorkspace(); return typeof id === "string" && id.length > 0 && id.length <= 128 ? id : ""; } catch (_) { return ""; } };
    const active = () => !!(visible && !suspended && !root.hidden && !document.hidden && root.isConnected !== false && workspace && workspace === currentWorkspace());
    function render() {
      const setText = (element, content) => { if (element.textContent !== content) element.textContent = content; };
      setText(heading, translate("当前工作"));
      if (!rendered || rendered.value !== value || rendered.scope !== workspace || rendered.stamp !== generation) {
        list.replaceChildren();
        const items = (value?.works || []).map(row => {
          const captured = Object.freeze({ ...row, workspace_id: workspace }), scope = workspace, stamp = generation, observed = value;
          const isCurrent = () => reconcile() && stamp === generation && scope === currentWorkspace() && value === observed;
          const item = node("li", "rabbit-work-row"), button = node("button", "rabbit-work-open"), status = node("span", "rabbit-work-state");
          button.type = "button"; button.setAttribute("data-control-interface", "human");
          button.addEventListener("click", () => {
            if (!isCurrent()) return;
            try { const result = onOpen(captured, isCurrent); if (result?.catch) result.catch(() => {}); } catch (_) { /* Navigation owns its error display. */ }
          });
          item.append(button, status); list.append(item); return { button, status };
        });
        rendered = { value, scope: workspace, stamp: generation, items };
      }
      if (!value) { setText(notice, translate("状态待核对")); renderExternal(); return; }
      const states = { running: "处理中", needs_user: "等待确认", idle: "本连接空闲", unknown: "状态待核对" };
      const connections = { not_open: "尚未连接", opening: "正在连接", failed: "连接异常", closed: "连接已关闭", unknown: "状态待核对" };
      value.works.forEach((row, index) => {
        const { button, status } = rendered.items[index];
        let label; try { label = getLabel(Object.freeze({ ...row })); } catch (_) { /* Use the metadata-only default below. */ }
        setText(button, text(label) ? label : row.role === "specialist" ? translate("小白兔专员") : `${agents[row.agent]} · ${translate("连接中的对话")} · ${row.chat_id.slice(-6)}`);
        const trusted = row.complete && value.agents.get(row.agent).complete;
        setText(status, translate(trusted ? connections[row.connection_state] || states[row.status] : states.unknown));
      });
      setText(notice, !value.complete ? translate("部分工作状态未知") : !value.works.length ? translate("暂无已连接的工作") : "");
      renderExternal();
    }
    const externalActive = () => active() && externalFocused;
    function renderExternal() {
      external.hidden = !externalKnown || externalValue?.state === "unconfigured";
      externalHeading.textContent = translate("HAPI报告的工作");
      external.title = translate("只显示 Hub 报告；不证明原生执行者实时状态，也不能在此发送或审批。");
      const report = externalValue?.report;
      externalTime.textContent = report ? `${translate("Hub 查询时间")}：${report.snapshot_at} · ${translate("非原生实时状态")}` : "";
      if (externalRendered?.value !== externalValue || externalRendered?.scope !== workspace || externalRendered?.stamp !== externalGeneration) {
        externalList.replaceChildren();
        const rows = (report?.works || []).map(row => {
          const item = node("li", "rabbit-work-row"), link = node("a", "rabbit-work-open"), status = node("span", "rabbit-work-state");
          const observed = externalValue, scope = workspace, stamp = externalGeneration;
          link.setAttribute("href", row.url); link.setAttribute("target", "_blank"); link.setAttribute("rel", "noopener noreferrer"); link.setAttribute("referrerpolicy", "no-referrer"); link.setAttribute("data-control-interface", "human");
          link.addEventListener("click", event => { if (!reconcile() || !externalActive() || stamp !== externalGeneration || scope !== currentWorkspace() || observed !== externalValue) event.preventDefault(); });
          item.append(link, status); externalList.append(item); return { link, status };
        });
        externalRendered = { value: externalValue, scope: workspace, stamp: externalGeneration, rows };
      }
      const states = { active: "Hub 活跃", thinking: "Hub 思考中", needs_user: "Hub 等待确认", inactive: "Hub 非活跃" };
      (report?.works || []).forEach((row, index) => {
        const { link, status } = externalRendered.rows[index];
        const label = [row.metadata?.name || translate("未命名工作") + " · " + Array.from(row.id).slice(-6).join(""), row.metadata?.flavor || translate("Agent 未报告")].join(" · ");
        if (link.textContent !== label) link.textContent = label;
        const state = translate(states[row.reported_status]); if (status.textContent !== state) status.textContent = state;
      });
      externalNotice.textContent = externalValue?.state === "unverified" ? translate("HAPI尚未核对，点“刷新检测”读取。")
        : report ? report.works.length ? "" : translate("Hub 未列出工作") : translate("HAPI状态待核对");
    }
    function invalidateExternal() {
      externalGeneration++; externalValue = null; externalKnown = false; externalSeen = false;
      if (externalFlight) { externalFlight.invalid = true; externalFlight.controller.abort(); clearTimeout(externalFlight.timeout); }
      renderExternal();
    }
    function readExternal(refresh = false) {
      if (!reconcile() || !externalActive()) return Promise.resolve(false);
      if (externalFlight) return externalFlight.promise;
      externalSeen = true;
      const request = { scope: workspace, stamp: externalGeneration, controller: new AbortController(), invalid: false, timeout: null, refresh, expected: null };
      externalFlight = request;
      const valid = () => externalFlight === request && !request.invalid && request.stamp === externalGeneration && externalActive() && request.scope === currentWorkspace();
      request.timeout = setTimeout(() => { if (valid()) { request.invalid = true; request.controller.abort(); externalValue = null; externalKnown = true; renderExternal(); } }, EXTERNAL_TIMEOUT);
      request.promise = Promise.resolve().then(() => valid() ? api(EXTERNAL_PATH, { method: "GET", signal: request.controller.signal }, false) : null).then(received => {
        if (!reconcile() || !valid()) return null;
        const cached = externalSnapshot(received, request.scope);
        if (!refresh || cached.state === "unconfigured") return cached;
        request.expected = cached;
        return api(EXTERNAL_PATH, { method: "POST", body: JSON.stringify({ operation: "refresh", expected_revision: cached.revision }), signal: request.controller.signal }, false);
      }).then(received => {
        if (!reconcile() || !valid() || received === null) return false;
        const next = externalSnapshot(received, request.scope);
        if (request.expected && (next.origin !== request.expected.origin || next.revision !== request.expected.revision || !["observed", "unavailable"].includes(next.state))) throw Error("external_binding_changed");
        externalValue = next; externalKnown = true; renderExternal(); return true;
      }).catch(() => { if (valid()) { externalValue = null; externalKnown = true; renderExternal(); } return false; }).finally(() => {
        clearTimeout(request.timeout); if (externalFlight !== request) return; externalFlight = null;
        if (reconcile() && externalActive() && !externalSeen) readExternal();
      });
      return request.promise;
    }
    function refreshExternal() {
      if (!reconcile() || !externalActive()) return Promise.resolve(false);
      if (externalFlight?.refresh) return externalFlight.promise;
      if (externalFlight) {
        const stamp = externalGeneration, scope = workspace;
        return externalFlight.promise.then(() => stamp === externalGeneration && scope === currentWorkspace() && externalActive() ? readExternal(true) : false);
      }
      return readExternal(true);
    }
    function invalidate() {
      generation++; if (timer !== null) clearTimeout(timer); timer = null;
      if (flight) { flight.invalid = true; flight.controller.abort(); clearTimeout(flight.timeout); }
      value = null; receivedAt = null; invalidateExternal(); render();
    }
    function reconcile() {
      const next = currentWorkspace();
      if (next !== workspace) { invalidate(); workspace = next; }
      const age = Date.now() - receivedAt;
      if (value && (!Number.isFinite(age) || age < 0 || age >= MAX_AGE)) { value = null; receivedAt = null; render(); }
      if (visible && (document.hidden || root.isConnected === false || !workspace)) { invalidate(); return false; }
      return active();
    }
    function schedule() {
      if (!reconcile() || flight || timer !== null) return;
      timer = setTimeout(() => { timer = null; read(); }, INTERVAL);
    }
    function read() {
      if (!reconcile() || flight) return;
      const request = { scope: workspace, stamp: generation, invalid: false, controller: new AbortController(), timeout: null };
      flight = request;
      const valid = () => flight === request && !request.invalid && request.stamp === generation && active() && request.scope === currentWorkspace();
      request.timeout = setTimeout(() => {
        if (!reconcile() || !valid()) return;
        request.invalid = true; request.controller.abort(); value = null; receivedAt = null; render();
      }, TIMEOUT);
      Promise.resolve().then(() => valid() ? api(PATH, { method: "GET", signal: request.controller.signal }, false) : null).then(received => {
        if (!reconcile() || !valid()) return;
        const next = snapshot(received, request.scope);
        if (!sameSnapshot(value, next)) value = next;
        receivedAt = Date.now(); render();
      }).catch(() => { if (valid()) { value = null; receivedAt = null; render(); } }).finally(() => {
        clearTimeout(request.timeout); if (flight !== request) return; flight = null;
        if (!reconcile()) return;
        if (request.stamp !== generation) read(); else schedule();
      });
    }
    function show() {
      const wasVisible = visible; visible = true; root.hidden = false;
      if (!wasVisible) invalidate();
      if (!reconcile() || document.hidden) { invalidate(); return; }
      render(); if (!flight && timer === null) read();
      if (!externalSeen) readExternal();
    }
    function hide() { visible = false; root.hidden = true; invalidate(); }
    document.addEventListener("visibilitychange", () => { if (!visible) return; if (document.hidden) invalidate(); else show(); });
    window.addEventListener?.("pagehide", () => { suspended = true; invalidate(); });
    window.addEventListener?.("pageshow", () => { suspended = false; if (visible) show(); });
    window.addEventListener?.("blur", () => { externalFocused = false; invalidateExternal(); });
    window.addEventListener?.("focus", () => { externalFocused = true; if (visible && !externalSeen) readExternal(); });
    return Object.freeze({ root, show, hide, refreshExternal });
  }
  window.RabbitWorkStatus = Object.freeze({ create });
})();
