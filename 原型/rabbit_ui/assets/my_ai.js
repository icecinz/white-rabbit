(() => {
  "use strict";
  const PATH = "/api/management/my-ai";
  const LABELS = Object.freeze({ codex: "Codex", pi: "Pi", hermes: "Hermes", claude: "Claude Code", dsh: "DeepSeek Harness" });
  const PROGRAMS = Object.freeze({ hindsight: "Hindsight", hermes_files: "Hermes 文档记忆", mem0: "Mem0" });
  const object = value => !!value && typeof value === "object" && !Array.isArray(value);
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
  const text = (value, maximum = 512) => typeof value === "string" && value.length <= maximum && !/[\x00-\x1f\x7f]/.test(value);
  const agent = value => typeof value === "string" && Object.hasOwn(LABELS, value);
  const stamp = value => typeof value === "string" && Number.isFinite(Date.parse(value)) ? value : null;
  const session = value => text(value, 256) && value.length > 0 && !/\s/.test(value);
  const names = (value, fallback) => text(value) && value.trim() ? value : fallback;
  function failure() { throw Error("我的 AI 目录未读完整，请重试。"); }
  function target(row) {
    if (!object(row)) return null;
    if (row.kind === "chat" && uuid(row.id) && row.key === "chat:" + row.id)
      return { kind: "chat", key: row.key, id: row.id, read_only: row.archived === true };
    if (row.kind === "native" && agent(row.agent) && uuid(row.id) && row.key === `native:${row.agent}:${row.id}`)
      return { kind: "native", key: row.key, id: row.id, agent: row.agent, read_only: row.available !== true };
    if (row.kind === "history" && agent(row.agent) && session(row.session_id) && row.key === `history:${row.agent}:${row.session_id}`)
      return { kind: "history", key: row.key, agent: row.agent, session_id: row.session_id, read_only: true };
    return null;
  }
  function originalSession(row, observations) {
    if (row.kind === "history") return row.agent === "codex" && uuid(row.session_id) ? row.session_id : null;
    if (row.kind !== "native" || row.agent !== "codex") return null;
    const ids = new Set();
    const indexed = Array.isArray(row.index_sources) ? row.index_sources : row.index_source ? [row.index_source] : [];
    for (const source of indexed) {
      if (!object(source) || source.agent !== "codex" || !uuid(source.session_id)) return null;
      ids.add(source.session_id);
    }
    for (const work of observations) if (work.role === "chat" && work.agent === "codex" && work.chat_id === row.id && uuid(work.native_session_id)) ids.add(work.native_session_id);
    return ids.size === 1 ? [...ids][0] : null;
  }
  function project(value, workspace) {
    if (!object(value) || value.schema_version !== 1 || !text(workspace, 128) || !workspace
        || value.workspace_id !== workspace || value.scope !== "saved_and_cached_overview"
        || value.connection_checked !== false || value.memory_read !== false || value.inference_performed !== false
        || !object(value.management) || value.management.workspace_id !== workspace
        || !Array.isArray(value.management.agents) || !Array.isArray(value.management.models)
        || !Array.isArray(value.ai_profiles) || !Array.isArray(value.memory_profiles)
        || !object(value.recent) || !Array.isArray(value.recent.items) || value.recent.items.length > 12
        || !Array.isArray(value.issues)) failure();
    const workSource = value.work_status;
    let observations = [];
    if (object(workSource) && workSource.workspace_id === workspace && workSource.scope === "rabbit_owned_cache"
        && Array.isArray(workSource.works)) observations = workSource.works.filter(row => object(row) && agent(row.agent));
    const modelMap = new Map(), agentMap = new Map();
    for (const row of value.management.agents) {
      if (!object(row) || !agent(row.id) || agentMap.has(row.id)) failure();
      const observed = observations.filter(work => work.agent === row.id && work.complete === true && work.reason === null && work.connection_state === "ready");
      const status = observed.some(work => work.status === "needs_user") ? "needs_user"
        : observed.some(work => work.status === "running") ? "running"
        : row.checked === true && row.available !== true ? "needs_attention"
        : row.checked === true && row.available === true ? "checked" : row.has_saved_configuration === true || row.native_enabled === true ? "configured" : "unverified";
      agentMap.set(row.id, { id: row.id, name: names(row.name, LABELS[row.id]), status,
        status_label: ({ needs_user: "等待确认", running: "进行中", configured: "有连接设置 · 待核对", needs_attention: "连接待处理", checked: "接入检查通过", unverified: "待核对" })[status],
        detail: names(row.reason, "尚未手动核对本机连接。"), native_enabled: row.native_enabled === true, has_saved_configuration: row.has_saved_configuration === true,
        model_count: 0, latest_activity: null, target: { kind: "agent", id: row.id } });
    }
    const addModel = (row, source, detail) => {
      if (!object(row) || !agent(row.agent_id) || !text(row.id, 220) || !row.id.startsWith(`local/${row.agent_id}/`)
          || !row.id.slice(`local/${row.agent_id}/`.length) || modelMap.has(row.id)) return;
      modelMap.set(row.id, { id: row.id, name: names(row.name, names(row.model, "已发现的模型")),
        model: names(row.model, ""), agent_id: row.agent_id, agent_name: LABELS[row.agent_id],
        status: "discovered", status_label: "已发现", detail: names(detail, "目录候选；不代表本次模型调用成功。"),
        source: names(source, "已有模型目录"), target: { kind: "model", id: row.id, agent_id: row.agent_id } });
    };
    for (const row of value.management.models) addModel(row, row?.source, row?.detail);
    const capabilities = value.management.native_profile_capabilities;
    if (object(capabilities)) for (const [id, catalogue] of Object.entries(capabilities)) {
      if (!agent(id) || !object(catalogue) || catalogue.catalogue_available !== true || !Array.isArray(catalogue.models)) continue;
      for (const row of catalogue.models) addModel(row, catalogue.source,
        "已有原生目录；核对时间：" + (stamp(catalogue.observed_at) || "未报告") + "。目录发现不等于模型调用成功。");
    }
    const models = [...modelMap.values()];
    for (const row of models) { const owner = agentMap.get(row.agent_id); if (owner) owner.model_count++; }
    const memories = [], memoryIds = new Set();
    for (const row of value.memory_profiles) {
      if (!object(row) || !uuid(row.id) || !Object.hasOwn(PROGRAMS, row.program) || memoryIds.has(row.id)) failure();
      memoryIds.add(row.id);
      memories.push({ id: row.id, name: names(row.name, "记忆配置"), program: row.program,
        connection_id: names(row.connection_id, ""), status: "unverified", status_label: "连接待核对",
        detail: PROGRAMS[row.program] + " · 已保存配置；本页未读取记忆内容或检查连接。", target: { kind: "memory", id: row.id } });
    }
    const works = [], keys = new Set();
    for (const row of value.recent.items) {
      const location = target(row);
      if (!location || keys.has(location.key)) failure();
      keys.add(location.key);
      const observed = location.kind === "native" ? observations.find(work => work.role === "chat" && work.agent === row.agent && work.chat_id === row.id) : null;
      const status = observed?.complete === true && observed.reason === null && observed.connection_state === "ready" && ["running", "needs_user"].includes(observed.status) ? observed.status
        : row.kind === "chat" && row.running === true ? "running" : location.read_only ? "read_only" : "continuable";
      const actions = [{ kind: "read", label: "阅读", target: { ...location, read_only: true } }];
      if (!location.read_only) actions.push({ kind: "continue", label: "继续", target: { ...location } });
      const original = originalSession(row, observations);
      const url = original && typeof window.RabbitAgents?.originalAppUrl === "function" ? window.RabbitAgents.originalAppUrl("codex", original) : null;
      if (url) actions.push({ kind: "original", label: "在原应用打开", target: { ...location }, url });
      const sources = Array.isArray(row.source_agents) ? row.source_agents.filter(agent) : [];
      const owner = agent(row.agent) ? row.agent : sources.length === 1 ? sources[0] : null;
      const timestamp = stamp(row.timestamp) || stamp(row.created_at);
      works.push({ key: row.key, name: names(row.title, "未命名工作"), agent: owner,
        agent_name: owner ? LABELS[owner] : "共同对话", timestamp, timestamp_kind: row.timestamp_kind || (row.kind === "history" ? "indexed_activity" : "chat_activity"), status,
        status_label: ({ running: "进行中", needs_user: "等待确认", read_only: "仅可阅读", continuable: "可继续" })[status],
        detail: row.kind === "history" ? "已有文字索引" : row.kind === "native" ? "小白兔管理的原生对话" : "本机共同对话",
        target: location, actions });
      if (owner && timestamp && agentMap.has(owner) && (row.kind !== "native" || row.timestamp_kind === "confirmed_activity")) {
        const summary = agentMap.get(owner);
        if (!summary.latest_activity || Date.parse(timestamp) > Date.parse(summary.latest_activity)) summary.latest_activity = timestamp;
      }
    }
    const agents = [...agentMap.values()];
    return { workspace_id: workspace, snapshot_at: stamp(value.snapshot_at), agents, models, memories, works,
      empty: !value.management.agents.some(row => row.has_saved_configuration === true || row.native_enabled === true) && value.ai_profiles.length === 0,
      issues: value.issues.filter(code => text(code, 100)), cache: object(value.cache) ? { ...value.cache } : {},
      actions: { connect: { kind: "management", section: "agents" }, new_chat: { kind: "new_chat" }, check: { kind: "manual_check" } } };
  }
  async function load(api, workspace) {
    if (typeof api !== "function") failure();
    return project(await api(PATH, { method: "GET" }, false), workspace);
  }
  window.RabbitMyAI = Object.freeze({ PATH, project, load, target });
})();
