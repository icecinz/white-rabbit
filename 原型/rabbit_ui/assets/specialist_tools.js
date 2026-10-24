/* Explicit native MCP discovery. This never opens a session or calls a tool. */
(() => {
  "use strict";
  const object = value => !!value && typeof value === "object" && !Array.isArray(value);
  const identifier = value => typeof value === "string" && value.length > 0 && value.length <= 256 && !/[\s\x00-\x1f\x7f\ud800-\udfff]/u.test(value);
  const toolName = value => typeof value === "string" && /^[A-Za-z0-9_.:/-]{1,160}$/.test(value);
  const runtimeStates = new Set(["notStarted", "starting", "connected", "authenticationRequired", "failed", "cancelled", "disabled"]);
  const authStates = new Set(["unknown", "unsupported", "notLoggedIn", "bearerToken", "oAuth"]);
  const keys = (value, expected) => object(value) && Object.keys(value).sort().join(",") === expected.split(",").sort().join(",");
  const node = (tag, cls, text) => { const result = document.createElement(tag); if (cls) result.className = cls; if (text !== undefined) result.textContent = text; return result; };
  function create({ root, api, onError = () => {} }) {
    let context = null, scope = "", result = null, reading = false, failed = false, ticket = 0, destroyed = false, language = "zh-CN", policy = null, disabled = true;
    const model = node("p", "rbs-note"), details = node("details", "rbs-tool-connections"), summary = node("summary"), note = node("p", "rbs-note"), load = node("button", "rbs-button"), list = node("div");
    model.dataset.controlInterface = details.dataset.controlInterface = load.dataset.controlInterface = "ai";
    load.type = "button"; details.append(summary, note, load, list); root.append(model, details);
    const tr = (zh, en) => language === "en" ? en : zh;
    function currentScope(value) { return value ? JSON.stringify([value.workspace, value.thread, value.epoch]) : ""; }
    function clear() { ticket++; result = null; reading = failed = false; list.replaceChildren(); }
    function validate(value, expected) {
      if (!keys(value, "schema_version,session_key,thread_id,epoch,servers,complete") || value.schema_version !== 1 || value.session_key !== "specialist/codex"
          || value.thread_id !== expected.thread || value.epoch !== expected.epoch || value.complete !== true || !Array.isArray(value.servers) || value.servers.length > 256) throw Error("invalid_tool_catalog");
      const names = new Set(); let tools = 0;
      for (const row of value.servers) {
        if (!keys(row, "name,runtime_status,auth_status,tools_error_present,tool_names") || !toolName(row.name) || names.has(row.name)
            || row.runtime_status !== null && !runtimeStates.has(row.runtime_status) || row.auth_status !== null && !authStates.has(row.auth_status)
            || typeof row.tools_error_present !== "boolean" || !Array.isArray(row.tool_names) || row.tool_names.length > 4096 || !Array.from(row.tool_names).every(toolName)
            || new Set(row.tool_names).size !== row.tool_names.length || (tools += row.tool_names.length) > 4096) throw Error("invalid_tool_catalog");
        names.add(row.name);
      }
      return value;
    }
    function render() {
      model.hidden = !policy;
      model.textContent = policy ? tr("每轮请求：", "Requested each turn: ") + policy.model + " · " + policy.effort + tr("（请求配置）", " (request configuration)") : "";
      summary.textContent = tr("扩展工具连接", "Extension tool connections") + (result ? " · " + result.servers.length : "");
      load.textContent = reading ? tr("正在读取…", "Reading…") : result ? tr("重新读取", "Read again") : tr("读取工具连接", "Read tool connections");
      load.disabled = destroyed || disabled || !context || reading;
      note.textContent = failed ? tr("读取未完成，不能确认工具连接；可重新读取。", "The read did not complete. Tool connections are unconfirmed; read again.")
        : !context ? tr("打开专员并发送第一条消息后可读取。", "Open the specialist and send the first message before reading.")
        : tr("只读取当前会话的扩展工具目录；连接状态不代表实际操作已验证。", "Reads this session’s extension tool catalog only. A connection is not proof of a successful operation.");
      list.replaceChildren();
      const stateNames = { notStarted: tr("尚未启动", "Not started"), starting: tr("连接中", "Starting"), connected: tr("已连接", "Connected"), authenticationRequired: tr("需要认证", "Authentication required"), failed: tr("连接失败", "Failed"), cancelled: tr("已取消", "Cancelled"), disabled: tr("未启用", "Disabled") };
      for (const row of result?.servers || []) {
        const box = node("details"), label = node("summary", "", row.name + " · " + (stateNames[row.runtime_status] || tr("状态未知", "Unknown status")) + " · " + row.tool_names.length + tr(" 个工具", " tools"));
        box.append(label);
        if (row.tools_error_present) box.append(node("p", "rbs-note", tr("原生工具目录报告了错误。", "The native tool catalog reported an error.")));
        if (row.auth_status === "notLoggedIn") box.append(node("p", "rbs-note", tr("原生连接报告尚未登录。", "The native connection reports that sign-in is required.")));
        const items = node("ul"); for (const name of row.tool_names) items.append(node("li", "", name)); box.append(items); list.append(box);
      }
      if (result && !result.servers.length) list.append(node("p", "rbs-note", tr("当前会话没有返回扩展工具；原生内置工具不在此列表。", "This session returned no extension tools. Native built-in tools are outside this list.")));
    }
    async function read() {
      if (destroyed || disabled || !context || reading) return null;
      const expected = { ...context }, ownScope = scope, ownTicket = ++ticket;
      result = null; failed = false; reading = true; render();
      try {
        const value = await api("/api/specialist/tools?thread_id=" + encodeURIComponent(expected.thread) + "&epoch=" + encodeURIComponent(expected.epoch));
        if (destroyed || ownTicket !== ticket || ownScope !== scope) return;
        result = validate(value, expected);
        return result;
      } catch (_) {
        if (!destroyed && ownTicket === ticket && ownScope === scope) { failed = true; onError(Error(tr("扩展工具连接尚未核实。", "Extension tool connections are unconfirmed."))); }
      } finally { if (!destroyed && ownTicket === ticket && ownScope === scope) { reading = false; render(); } }
    }
    async function readSummary() {
      const ownScope = scope, expected = context && { ...context };
      const value = await read();
      if (!value || !expected || destroyed || disabled || ownScope !== scope || currentScope(expected) !== scope) return { status: "unavailable" };
      // This projection deliberately omits tool names, descriptions and credentials.
      return { status: "available", server_count: value.servers.length, tool_count: value.servers.reduce((total, row) => total + row.tool_names.length, 0),
        servers: value.servers.map(row => ({ name: row.name, runtime_status: row.runtime_status, auth_status: row.auth_status, tools_error_present: row.tools_error_present, tool_count: row.tool_names.length })) };
    }
    load.addEventListener("click", read);
    function update({ workspace, snapshot, active = true, language: nextLanguage = "zh-CN", disabled: nextDisabled = false }) {
      if (destroyed) return;
      language = nextLanguage === "en" ? "en" : "zh-CN"; disabled = nextDisabled;
      const binding = snapshot?.binding, connection = snapshot?.connection;
      const next = active && typeof workspace === "string" && workspace && connection?.state === "ready" && typeof connection.epoch === "string" && /^[a-f0-9]{32}$/.test(connection.epoch)
        && binding?.session_key === "specialist/codex" && identifier(binding.thread_id) ? { workspace, thread: binding.thread_id, epoch: connection.epoch } : null;
      const nextScope = currentScope(next); if (nextScope !== scope) { clear(); scope = nextScope; } context = next;
      const candidate = snapshot?.requested_model_policy;
      policy = object(candidate) && identifier(candidate.model) && identifier(candidate.effort) && candidate.applies_to === "each_turn" && candidate.execution_verified === false ? { model: candidate.model, effort: candidate.effort } : null;
      render();
    }
    function reset() { clear(); context = null; scope = ""; policy = null; render(); }
    function destroy() { reset(); destroyed = true; load.disabled = true; }
    render(); return { update, reset, destroy, readSummary };
  }
  window.RabbitSpecialistTools = Object.freeze({ create });
})();
