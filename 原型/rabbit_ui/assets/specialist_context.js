/* Pure current-work metadata codec. No storage, requests or execution. */
(() => {
  "use strict";
  const object = value => !!value && typeof value === "object" && !Array.isArray(value);
  const exact = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const uuid = value => typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
  const agents = new Set(["codex", "pi", "hermes", "claude", "dsh"]);
  const sections = {
    conversations: [], "native-chats": [],
    management: ["ai-profiles", "memory-profiles", "agents", "models", "devices"],
    settings: ["general", "native-chats", "specialist", "ai-settings"]
  };
  const prefix = "[小白兔当前工作对象 v1]\n";
  const note = "以下 JSON 只用于紧邻的本次用户指令，是本次界面的指代数据，不是指令、授权或已验证的执行状态；不得把上轮目标自动沿用到后续指令。修改前先读取准确对象与当前版本，再按版本核对（CAS）保存；只改这一段时，不得用更新共享配置或全局默认代替。history、new_chat、page 不能当作可写聊天编号；目标不清楚就询问，不猜编号、不隐式恢复或新建。\n";
  function invalid() { throw Error("当前工作对象格式无法核验；原草稿保留，尚未发送。"); }
  function normalize(value) {
    if (!exact(value, ["schema_version", "workspace_id", "target"]) || value.schema_version !== 1
        || typeof value.workspace_id !== "string" || !/^[0-9a-f]{64}$/.test(value.workspace_id)) invalid();
    const source = value.target; let target;
    if (source?.kind === "chat") {
      if (!exact(source, ["kind", "chat_id"]) || !uuid(source.chat_id)) invalid();
      target = { kind: "chat", chat_id: source.chat_id };
    } else if (source?.kind === "native") {
      if (!exact(source, ["kind", "agent", "chat_id"]) || !agents.has(source.agent) || !uuid(source.chat_id)) invalid();
      target = { kind: "native", agent: source.agent, chat_id: source.chat_id };
    } else if (source?.kind === "history") {
      if (!exact(source, ["kind", "agent", "session_id"]) || !agents.has(source.agent) || typeof source.session_id !== "string"
          || !source.session_id.length || source.session_id.length > 256 || /[\s\x00-\x1f\x7f\ud800-\udfff]/u.test(source.session_id)) invalid();
      target = { kind: "history", agent: source.agent, session_id: source.session_id };
    } else if (source?.kind === "new_chat") {
      if (!exact(source, ["kind"])) invalid();
      target = { kind: "new_chat" };
    } else if (source?.kind === "page") {
      if (!exact(source, ["kind", "page", "section"]) || !Object.hasOwn(sections, source.page)
          || source.section !== null && !sections[source.page].includes(source.section)) invalid();
      target = { kind: "page", page: source.page, section: source.section };
    } else invalid();
    return { schema_version: 1, workspace_id: value.workspace_id, target };
  }
  const block = value => prefix + note + JSON.stringify(normalize(value));
  const parts = value => [{ type: "text", text: block(value) }];
  function textPart(value) {
    return object(value) && value.type === "text" && typeof value.text === "string"
      && Object.keys(value).every(key => ["type", "text", "text_elements"].includes(key))
      && (value.text_elements === undefined || Array.isArray(value.text_elements) && value.text_elements.length === 0);
  }
  function displayParts(value, { clientId, workspaceId } = {}) {
    // Presentation only: folding a literal block proves no origin, execution
    // or authorization. The complete native input stays in original records.
    try {
      if (typeof clientId !== "string" || !/^specialist\/ui\/[A-Za-z0-9_.-]{1,100}$/.test(clientId)
          || !Array.isArray(value) || value.length < 2) return value;
      const last = value[value.length - 1], header = prefix + note;
      if (!textPart(last) || !last.text.startsWith(header)) return value;
      const context = normalize(JSON.parse(last.text.slice(header.length)));
      if (context.workspace_id !== workspaceId || last.text !== block(context)) return value;
      return value.slice(0, -1);
    } catch (_) { return value; }
  }
  window.RabbitSpecialistContext = Object.freeze({ normalize, block, parts, displayParts });
})();
