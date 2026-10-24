/* Claude derived-input browser journal. Pure validation/hashes; never sends. */
(() => {
  "use strict";
  const object = v => !!v && typeof v === "object" && !Array.isArray(v);
  const uuid = v => typeof v === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
  const sha = v => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
  const text = v => typeof v === "string" && !/[\ud800-\udfff]/u.test(v);
  const canonical = value => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  const same = (a, b) => canonical(a) === canonical(b);
  const fail = () => { throw Error("Claude材料原请求或精确回执未能核对；整份草稿与编号保留，不会重发。"); };
  function refs(value = []) {
    if (!window.RabbitNativeMaterials) { if (Array.isArray(value) && !value.length) return []; fail(); }
    const result = window.RabbitNativeMaterials.refs(value);
    if (result.length > 4 || result.some(row => row.kind !== "text")) fail();
    return result;
  }
  async function hash(value) {
    if (!window.crypto?.subtle || typeof TextEncoder !== "function") fail();
    const bytes = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join("");
  }
  const memory = value => { if (!window.RabbitNativeMemory) fail(); return window.RabbitNativeMemory.choice(value, { readOnly: true }); };
  const body = pending => ({ client_id: pending.client_id, message: pending.message, ...(pending.material_refs.length ? { material_refs: refs(pending.material_refs) } : {}), ...(pending.memory_profile ? { memory_profile_id: pending.memory_profile.id, memory_profile_revision: pending.memory_profile.revision } : {}) });
  const binding = pending => ({ workspace_id: pending.workspace_id, chat_id: pending.chat_id, client_id: pending.client_id,
    session_id: pending.session_id, raw_request_sha256: pending.raw_request_sha256, material_refs: pending.material_refs, request_sha256: pending.request_sha256, ...(pending.memory_profile ? { memory_profile: pending.memory_profile } : {}) });
  function restore(value, workspace, chatId) {
    if (!object(value) || value.workspace_id !== workspace || value.chat_id !== chatId || !uuid(value.client_id) || !uuid(value.session_id)
        || !text(value.message) || !value.message.trim() || new TextEncoder().encode(value.message).length > 131072
        || !Number.isSafeInteger(value.draft_revision) || value.draft_revision < 0
        || !["submitted_unknown", "accepted"].includes(value.status) || !sha(value.raw_request_sha256)
        || !(value.request_sha256 === null || sha(value.request_sha256)) || !(value.binding_sha256 === null || sha(value.binding_sha256))
        || (value.request_sha256 === null) !== (value.binding_sha256 === null) || !text(value.body_json)) fail();
    const material_refs = refs(value.material_refs);
    const memory_profile = value.memory_profile === undefined ? null : memory(value.memory_profile);
    if (!material_refs.length && !memory_profile || value.body_json !== JSON.stringify(body({ ...value, material_refs, memory_profile }))) fail();
    const result = { client_id: value.client_id, message: value.message, material_refs, workspace_id: workspace, chat_id: chatId,
      session_id: value.session_id, draft_revision: value.draft_revision, body_json: value.body_json, raw_request_sha256: value.raw_request_sha256,
      request_sha256: value.request_sha256, binding_sha256: value.binding_sha256, status: value.status, ...(memory_profile ? { memory_profile } : {}) };
    if (!same(value, result)) fail();
    return result;
  }
  async function verify(value, workspace, chatId) {
    const result = restore(value, workspace, chatId);
    if (await hash(canonical(body(result))) !== result.raw_request_sha256
        || result.request_sha256 !== null && await hash(canonical(binding(result))) !== result.binding_sha256) fail();
    return result;
  }
  async function prepare(client, message, material_refs, workspace, chatId, sessionId, revision, selectedMemory = null) {
    const result = { client_id: client, message, material_refs: refs(material_refs), workspace_id: workspace, chat_id: chatId,
      session_id: sessionId, draft_revision: revision, body_json: "", raw_request_sha256: "", request_sha256: null, binding_sha256: null, status: "submitted_unknown", ...(selectedMemory ? { memory_profile: memory(selectedMemory) } : {}) };
    result.body_json = JSON.stringify(body(result)); result.raw_request_sha256 = await hash(canonical(body(result)));
    return restore(result, workspace, chatId);
  }
  async function confirm(pending, value, workspace, chatId, exact = false) {
    const checked = await verify(pending, workspace, chatId);
    if (!object(value) || exact && (value.workspace_id !== workspace || value.chat_id !== chatId || value.session_id !== checked.session_id)) fail();
    const intent = value.material_intent, operation = value.operation;
    let derived = null, memory_intent = null;
    if (checked.memory_profile) {
      if (!window.RabbitNativeMemory) fail();
      memory_intent = window.RabbitNativeMemory.intent(value.memory_intent, { workspaceId: workspace, chatId, clientKind: "uuid" });
      if (memory_intent.client_id !== checked.client_id || memory_intent.request_sha256 !== checked.raw_request_sha256 || !same(memory_intent.memory_profile, window.RabbitNativeMemory.snapshot(checked.memory_profile))) fail();
      if (memory_intent.state === "failed") {
        if (operation !== null || intent != null || checked.status !== "submitted_unknown" || checked.request_sha256 !== null || checked.binding_sha256 !== null) fail();
        return exact ? { pending: checked, operation: null, memory_intent, failed: true } : null;
      }
      derived = memory_intent.derived_inputs_sha256;
    }
    if (checked.material_refs.length) {
      if (intent === null && operation === null && !checked.memory_profile) return null;
      if (!object(intent) || intent.schema_version !== 1 || intent.workspace_id !== workspace || intent.chat_id !== chatId || intent.client_id !== checked.client_id
          || intent.raw_request_sha256 !== checked.raw_request_sha256 || !same(refs(intent.material_refs), checked.material_refs)
          || !(intent.request_sha256 === null || sha(intent.request_sha256))
          || Object.keys(intent).sort().join(",") !== "chat_id,client_id,created_at,material_refs,raw_request_sha256,request_sha256,schema_version,updated_at,workspace_id"
          || !text(intent.created_at) || !intent.created_at || intent.created_at.length > 64 || !text(intent.updated_at) || !intent.updated_at || intent.updated_at.length > 64) fail();
      // A crash between the two input bindings is still unknown; never dispatch again.
      if (checked.memory_profile && intent.request_sha256 !== derived && operation !== null) fail();
      if (!checked.memory_profile) derived = intent.request_sha256;
    } else if (intent != null) fail();
    if (checked.request_sha256 !== null && derived !== checked.request_sha256) fail();
    if (operation === null) return null;
    if (!object(operation) || operation.kind !== "prompt" || operation.chat_id !== chatId || operation.client_id !== checked.client_id || operation.session_id !== checked.session_id
        || !["submitted_unknown", "accepted", "completed", "failed", "interrupted", "rejected"].includes(operation.status)
        || !sha(derived) || operation.request_sha256 !== derived || checked.memory_profile && memory_intent.state !== "ready") fail();
    const result = { ...checked, request_sha256: derived };
    result.binding_sha256 = await hash(canonical(binding(result)));
    return { pending: result, operation, ...(memory_intent ? { memory_intent } : {}) };
  }
  function context(value) {
    return window.RabbitNativeMaterials?.context(value) ?? null;
  }
  window.RabbitClaudeMaterialDraft = Object.freeze({ refs, prepare, restore, verify, confirm, context });
})();
