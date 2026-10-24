(() => {
  "use strict";
  const input = window.RabbitAgentDefinitions;
  if (!Array.isArray(input) || !input.length || input.length > 32) throw new Error("助手身份定义缺失或不完整，请重新加载入口。");
  const entries = input.map(row => {
    if (!row || typeof row !== "object" || Array.isArray(row) || Object.keys(row).sort().join(",") !== "id,initial,label" || typeof row.id !== "string" || !/^[a-z][a-z0-9_-]{0,31}$(?![\s\S])/.test(row.id) || ["constructor", "prototype", "__proto__"].includes(row.id) || typeof row.label !== "string" || !row.label.replace(/\p{White_Space}/gu, "") || Array.from(row.label).length > 60 || /[\x00-\x1f\x7f\uD800-\uDFFF]/u.test(row.label) || typeof row.initial !== "string" || !/^[A-Za-z0-9]{1,3}$(?![\s\S])/.test(row.initial)) throw new Error("助手身份定义不完整，请重新加载入口。");
    return Object.freeze({ id: row.id, label: row.label, initial: row.initial });
  });
  const ids = Object.freeze(entries.map(row => row.id));
  if (new Set(ids).size !== ids.length) throw new Error("助手身份定义重复，请重新加载入口。");
  const labels = Object.freeze(Object.assign(Object.create(null), Object.fromEntries(entries.map(row => [row.id, row.label]))));
  const initials = Object.freeze(Object.assign(Object.create(null), Object.fromEntries(entries.map(row => [row.id, row.initial]))));
  const has = id => typeof id === "string" && ids.includes(id);
  const instanceId = id => has(id) ? `local/${id}` : null;
  const backendOfInstance = value => typeof value === "string" && value.startsWith("local/") && has(value.slice(6)) ? value.slice(6) : null;
  const isInstance = value => backendOfInstance(value) !== null;
  function validModelId(value, backend) {
    if (typeof value !== "string" || Array.from(value).length > 220 || backend !== undefined && !has(backend)) return false;
    if (!value) return true;
    const match = /^local\/([a-z][a-z0-9_-]{0,31})\/([^\x00-\x20\x7f]{1,192})$(?![\s\S])/u.exec(value);
    return !!match && has(match[1]) && (backend === undefined || backend === match[1]);
  }
  const emptyModels = () => Object.fromEntries(ids.map(id => [id, ""]));
  window.RabbitAgents = Object.freeze({ ids, labels, initials, has, instanceId, isInstance, backendOfInstance, validModelId, emptyModels });
})();
