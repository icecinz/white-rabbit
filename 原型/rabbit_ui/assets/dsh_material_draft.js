/* DSH text-material journal. Pure validation and hashes; no storage or sending. */
(() => {
  "use strict";
  const object = value => !!value && typeof value === "object" && !Array.isArray(value);
  const uuid = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
  const sha = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const text = value => typeof value === "string" && !/[\ud800-\udfff]/u.test(value);
  const nativeId = value => text(value) && value.length > 0 && value.length <= 4096 && !/[\x00-\x1f\x7f]/u.test(value);
  const canonical = value => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  const same = (left, right) => canonical(left) === canonical(right);
  const timestamp = value => text(value) && value.length <= 64 && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
  const fail = () => { throw Error("DSH材料原请求或精确回执未能核对；整份草稿与编号保留，不会重发。"); };
  function refs(value) {
    if (!window.RabbitNativeMaterials) fail();
    const checked = window.RabbitNativeMaterials.refs(value);
    if (!checked.length || checked.length > 4 || checked.some(row => row.kind !== "text")) fail();
    return checked;
  }
  async function hash(value) {
    if (!window.crypto?.subtle || typeof TextEncoder !== "function") fail();
    const bytes = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join("");
  }
  const memory = value => { if (!window.RabbitNativeMemory) fail(); return window.RabbitNativeMemory.choice(value, { readOnly: true }); };
  const body = pending => ({ client_id: pending.client_id, message: pending.message, material_refs: refs(pending.material_refs),
    ...(pending.memory_profile ? { memory_profile_id: pending.memory_profile.id, memory_profile_revision: pending.memory_profile.revision } : {}) });
  const binding = pending => ({ workspace_id: pending.workspace_id, chat_id: pending.chat_id, client_id: pending.client_id,
    session_id: pending.session_id, raw_request_sha256: pending.raw_request_sha256, material_refs: pending.material_refs,
    request_sha256: pending.request_sha256, ...(pending.memory_profile ? { memory_profile: pending.memory_profile } : {}) });
  function restore(value, workspace, chatId) {
    if (!sha(workspace) || !uuid(chatId) || !object(value) || value.workspace_id !== workspace || value.chat_id !== chatId
        || !uuid(value.client_id) || !nativeId(value.session_id) || !text(value.message) || !value.message.trim()
        || new TextEncoder().encode(value.message).length > 131072 || !Number.isSafeInteger(value.draft_revision) || value.draft_revision < 0
        || !["submitted_unknown", "accepted"].includes(value.status) || !sha(value.raw_request_sha256)
        || !(value.request_sha256 === null || sha(value.request_sha256)) || !(value.binding_sha256 === null || sha(value.binding_sha256))
        || (value.request_sha256 === null) !== (value.binding_sha256 === null) || !text(value.body_json)) fail();
    const material_refs = refs(value.material_refs), memory_profile = value.memory_profile === undefined ? null : memory(value.memory_profile);
    if (value.body_json !== JSON.stringify(body({ ...value, material_refs, memory_profile }))) fail();
    const result = { workspace_id: workspace, chat_id: chatId, session_id: value.session_id, client_id: value.client_id,
      message: value.message, material_refs, body_json: value.body_json, raw_request_sha256: value.raw_request_sha256,
      request_sha256: value.request_sha256, binding_sha256: value.binding_sha256, draft_revision: value.draft_revision,
      status: value.status, ...(memory_profile ? { memory_profile } : {}) };
    if (!same(value, result)) fail();
    return result;
  }
  async function verify(value, workspace, chatId) {
    const checked = restore(value, workspace, chatId);
    if (await hash(canonical(body(checked))) !== checked.raw_request_sha256
        || checked.request_sha256 !== null && await hash(canonical(binding(checked))) !== checked.binding_sha256) fail();
    return checked;
  }
  async function prepare(client, message, material_refs, workspace, chatId, sessionId, revision, selectedMemory = null) {
    const result = { workspace_id: workspace, chat_id: chatId, session_id: sessionId, client_id: client, message,
      material_refs: refs(material_refs), body_json: "", raw_request_sha256: "", request_sha256: null,
      binding_sha256: null, draft_revision: revision, status: "submitted_unknown",
      ...(selectedMemory ? { memory_profile: memory(selectedMemory) } : {}) };
    result.body_json = JSON.stringify(body(result)); result.raw_request_sha256 = await hash(canonical(body(result)));
    return restore(result, workspace, chatId);
  }
  function materialIntent(value, checked) {
    if (!object(value) || Object.keys(value).sort().join(",") !== "chat_id,client_id,created_at,material_refs,raw_request_sha256,request_sha256,schema_version,updated_at,workspace_id"
        || value.schema_version !== 1 || value.workspace_id !== checked.workspace_id || value.chat_id !== checked.chat_id
        || value.client_id !== checked.client_id || value.raw_request_sha256 !== checked.raw_request_sha256
        || !same(refs(value.material_refs), checked.material_refs) || !(value.request_sha256 === null || sha(value.request_sha256))
        || !timestamp(value.created_at) || !timestamp(value.updated_at) || Date.parse(value.updated_at) < Date.parse(value.created_at)
        || value.request_sha256 === null && value.updated_at !== value.created_at) fail();
    return JSON.parse(JSON.stringify(value));
  }
  function selectedIntent(value, checked) {
    let intent = value.material_intent ?? null;
    if (value.material_intents !== undefined) {
      if (!Array.isArray(value.material_intents) || value.material_intents.length > 128) fail();
      const selected = value.material_intents.filter(row => row?.client_id === checked.client_id);
      if (selected.length > 1 || selected.length && intent !== null && !same(selected[0], intent)) fail();
      if (selected.length) intent = selected[0];
    }
    return intent === null ? null : materialIntent(intent, checked);
  }
  async function confirm(pending, value, workspace, chatId, exact = false) {
    const checked = await verify(pending, workspace, chatId);
    if (!object(value) || typeof exact !== "boolean"
        || exact && (value.workspace_id !== workspace || value.chat_id !== chatId || value.session_id !== checked.session_id)
        || value.workspace_id !== undefined && value.workspace_id !== workspace || value.chat_id !== undefined && value.chat_id !== chatId
        || value.session_id !== undefined && value.session_id !== checked.session_id) fail();
    const operation = value.operation, intent = selectedIntent(value, checked);
    if (operation !== null && !object(operation)) fail();
    let memory_intent = null, derived = intent?.request_sha256 ?? null;
    if (checked.memory_profile) {
      if (!window.RabbitNativeMemory) fail();
      memory_intent = window.RabbitNativeMemory.intent(value.memory_intent, { workspaceId: workspace, chatId, clientKind: "uuid" });
      if (memory_intent.client_id !== checked.client_id || memory_intent.request_sha256 !== checked.raw_request_sha256
          || !same(memory_intent.memory_profile, window.RabbitNativeMemory.snapshot(checked.memory_profile))) fail();
      if (memory_intent.state === "failed") {
        if (operation !== null || intent !== null || checked.status !== "submitted_unknown" || checked.request_sha256 !== null || checked.binding_sha256 !== null) fail();
        return exact ? { pending: checked, operation: null, memory_intent, failed: true } : null;
      }
      if (derived !== null && memory_intent.derived_inputs_sha256 !== null && derived !== memory_intent.derived_inputs_sha256) fail();
      if (intent === null || derived === null || memory_intent.derived_inputs_sha256 === null) derived = null;
    } else if (value.memory_intent != null) fail();
    if (checked.request_sha256 !== null && derived !== checked.request_sha256) fail();
    // Missing or half-bound evidence is unknown, never permission to send again.
    if (operation === null) return null;
    if (!intent || !sha(derived) || operation.kind !== "prompt" || operation.chat_id !== chatId
        || operation.client_id !== checked.client_id || operation.session_id !== checked.session_id
        || !["submitted_unknown", "accepted", "completed", "failed", "interrupted", "rejected", "command_completed"].includes(operation.status)
        || operation.request_sha256 !== derived || operation.message_sha256 !== await hash(checked.message)
        || operation.status === "command_completed" && !sha(operation.native_command_sha256)
        || memory_intent && memory_intent.state !== "ready") fail();
    const result = { ...checked, request_sha256: derived };
    result.binding_sha256 = await hash(canonical(binding(result)));
    return { pending: result, operation, ...(memory_intent ? { memory_intent } : {}) };
  }
  const context = value => window.RabbitNativeMaterials?.context(value) ?? null;
  async function project(value, intent, operation, { workspaceId, chatId, sessionId, memoryIntent = null } = {}) {
    try {
      if (!text(value) || !sha(workspaceId) || !uuid(chatId) || !nativeId(sessionId) || !object(intent) || !object(operation)
          || !uuid(operation.client_id) || operation.kind !== "prompt" || operation.chat_id !== chatId || operation.session_id !== sessionId
          || !["submitted_unknown", "accepted", "completed", "failed", "interrupted", "rejected", "command_completed"].includes(operation.status)
          || !sha(operation.request_sha256) || !sha(operation.message_sha256)
          || operation.status === "command_completed" && !sha(operation.native_command_sha256)
          || new TextEncoder().encode(canonical({ type: "prompt", message: value })).length > 900 * 1024
          || await hash(canonical({ type: "prompt", message: value })) !== operation.request_sha256) return null;
      const material_refs = refs(intent.material_refs);
      const checked = materialIntent(intent, { workspace_id: workspaceId, chat_id: chatId, client_id: operation.client_id,
        raw_request_sha256: intent.raw_request_sha256, material_refs });
      if (!sha(checked.raw_request_sha256) || checked.request_sha256 !== operation.request_sha256) return null;
      let source = value, memory_suffix = null, selectedMemory = null;
      if (memoryIntent !== null) {
        const codec = window.RabbitNativeMemory;
        if (!codec) return null;
        const memory = codec.intent(memoryIntent, { workspaceId, chatId, clientKind: "uuid" });
        if (memory.state !== "ready" || memory.client_id !== operation.client_id || memory.request_sha256 !== checked.raw_request_sha256
            || memory.derived_inputs_sha256 !== checked.request_sha256) return null;
        memory_suffix = codec.piSuffix(memory);
        if (!source.endsWith(memory_suffix)) return null;
        source = source.slice(0, -memory_suffix.length); selectedMemory = memory.memory_profile;
      }
      const prefix = window.RabbitNativeMaterials?.contextPrefix;
      if (typeof prefix !== "string" || !prefix) return null;
      // JSON string newlines are escaped. The last literal delimiter is the
      // host annex; an identical marker inside a selected file is not a split.
      const at = source.lastIndexOf("\n\n" + prefix);
      if (at < 0) return null;
      const message = source.slice(0, at), material_preview = context(source.slice(at + 2));
      if (!message.trim() || new TextEncoder().encode(message).length > 131072 || !material_preview
          || !same(material_preview.material_refs, material_refs) || await hash(message) !== operation.message_sha256) return null;
      const raw = { client_id: operation.client_id, message, material_refs,
        ...(selectedMemory ? { memory_profile_id: selectedMemory.id, memory_profile_revision: selectedMemory.revision } : {}) };
      if (await hash(canonical(raw)) !== checked.raw_request_sha256) return null;
      for (const file of material_preview.files) if (await hash(file.text) !== file.sha256) return null;
      return { message, material_preview, ...(memory_suffix === null ? {} : { memory_suffix }) };
    } catch (_) { return null; }
  }
  window.RabbitDshMaterialDraft = Object.freeze({ refs, prepare, restore, verify, confirm, context, project });
})();
