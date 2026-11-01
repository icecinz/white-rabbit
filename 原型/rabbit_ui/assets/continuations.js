/* A local continuation draft. Creation and native execution stay with the host. */
(() => {
  "use strict";
  const clone = value => JSON.parse(JSON.stringify(value));
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const canonical = value => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  const aiFields = ["id", "revision", "name", "agent_instance_id", "device_id", "model_id", "reasoning_effort"];
  const memoryFields = ["id", "revision", "name", "description", "habits", "program", "connection_id", "mode", "bank_ref"];
  const pick = (value, fields) => Object.fromEntries(fields.map(key => [key, value[key]]));
  const node = (tag, className, text) => { const item = document.createElement(tag); if (className) item.className = className; if (text !== undefined) item.textContent = text; return item; };
  const sameSource = (left, right) => left && right && left.workspace_id === right.workspace_id && left.agent === right.agent && left.chat_id === right.chat_id;
  function create({ root, getWorkspace, getState, saveState, getProfiles, onChooseConfiguration, onOpenSource, onOpenTarget = null, onReadCreation = null, onSubmit, getSendShortcut = () => "enter", onError = () => {} }) {
    if (!root || [getWorkspace, getState, saveState, getProfiles, onSubmit].some(fn => typeof fn !== "function") || !window.RabbitComposerShell || !window.RabbitBrowserState?.continuation) throw Error("接续草稿入口未就绪。");
    let visible = false, generation = 0, busy = null, notice = "", unsavedTask = null, renderedSource = null;
    const page = node("section", "rbs-page rbs-conversation"), content = node("div", "rbs-conversation-body conversation-scroll"), heading = node("h2"), actions = node("div", "file-task-actions");
    const more = node("details", "rbh-details"), original = node("div"), materialList = node("div", "native-handoff-materials"), message = node("p", "rbs-note");
    more.append(node("summary", "", "更多"), original, materialList, actions, message); content.append(heading, more);
    const region = node("div", "rbs-conversation-composer composer-region"), composer = node("div"), plate = node("p", "composer-status"), choices = node("div"), input = node("textarea"), support = node("div"), hint = node("span");
    input.rows = 3; input.placeholder = "接下来要做什么…"; input.setAttribute("aria-label", "接续任务");
    const button = (label, fn, ai = false) => { const item = node("button", "file-task-action", label); item.type = "button"; if (ai) item.setAttribute("data-control-interface", "ai"); item.addEventListener("click", fn); return item; };
    const aiButton = button("AI 配置", () => onChooseConfiguration?.("ai"), true), memoryButton = button("记忆配置", () => onChooseConfiguration?.("memory"), true);
    aiButton.className = memoryButton.className = "profile-trigger";
    const method = node("label", "interface-choice"), methodSelect = node("select"), program = node("option", "", "纯程序接口"); program.value = "program"; methodSelect.append(program); methodSelect.value = "program"; methodSelect.disabled = true; methodSelect.setAttribute("aria-label", "接口方式"); method.append(node("span", "", "接口方式"), methodSelect); choices.append(aiButton, memoryButton, method);
    const send = button("发送 ↑", submit, true); send.className += " send-button";
    const back = button("返回来源", () => { const record = current(); if (record) onOpenSource?.(record.source_view); });
    const cancel = button("取消接续", cancelCurrent), openTarget = button("打开已创建对话", () => { const record = current(); if (record?.creation?.created_chat_id) onOpenTarget?.({ workspace_id: record.source_view.workspace_id, agent: record.target.agent, chat_id: record.creation.created_chat_id }); });
    const checkCreation = button("核对原创建记录", readCreation); actions.append(back, openTarget, checkCreation, cancel);
    window.RabbitComposerShell.mount({ root: composer, status: plate, choices, input, support, send, hint }); region.append(composer); page.append(content, region); root.replaceChildren(page); root.hidden = true;
    const checked = value => window.RabbitBrowserState.continuation(value, String(getWorkspace() || ""));
    function state() {
      const value = getState();
      if (!object(value) || !Array.isArray(value.continuations)) throw Error("接续草稿尚未读取。");
      return { continuations: value.continuations.map(checked), currentContinuation: value.currentContinuation };
    }
    function current() { const value = state(); return value.continuations.find(row => row.id === value.currentContinuation) || null; }
    function findBySource(source) { return state().continuations.find(row => sameSource(row.source_view, source)) || null; }
    function fail(error) { notice = typeof error?.message === "string" && error.message !== "invalid-browser-snapshot" ? error.message : "接续草稿无法完整保存或核对；原内容保留。"; onError(error); render(); }
    function persist(next) {
      const checkedRows = next.continuations.map(checked);
      if (saveState({ continuations: checkedRows, currentContinuation: next.currentContinuation }) !== true) throw Error("浏览器不能保存接续草稿；尚未提交。");
      return true;
    }
    function replace(record) { const value = state(); if (!value.continuations.some(row => row.id === record.id)) return false; return persist({ ...value, continuations: value.continuations.map(row => row.id === record.id ? checked(record) : row) }); }
    function aiReady(profile) {
      const row = (getProfiles()?.ai_profiles || []).find(item => item.id === profile?.id);
      return !!row && row.native_available === true && canonical(pick(row, aiFields)) === canonical(profile);
    }
    function memoryChoice(row) { return window.RabbitNativeMemory.choice({ ...pick(row, memoryFields), locked_mode: row.locked_mode === true, source_kind: row.source_kind || "owned" }, { readOnly: true }); }
    function memoryReady(profile) {
      if (!profile) return true;
      const row = (getProfiles()?.memory_profiles || []).find(item => item.id === profile.id);
      try { return !!row && row.available === true && canonical(memoryChoice(row)) === canonical(profile); } catch (_) { return false; }
    }
    function render() {
      if (!visible) return;
      let record; try { record = current(); } catch (_) { root.hidden = true; return; }
      if (!record) { root.hidden = true; return; }
      root.hidden = false; heading.textContent = `接着《${record.source_view.title}》`;
      input.value = unsavedTask?.id === record.id ? unsavedTask.value : record.task;
      const profile = record.target.ai_profile, live = (getProfiles()?.ai_profiles || []).find(item => item.id === profile?.id);
      window.RabbitProfiles.renderStatus(plate, { agent: record.target.agent === "codex" ? "Codex" : "Hermes", model: profile?.model_id || live?.effective_model || null, effort: profile?.reasoning_effort || live?.effective_effort || null, memory: record.target.memory_profile?.name || "不额外带入记忆", status: record.creation?.handoff_started ? "已交给原对话" : "尚未发送" });
      for (const [control, label, value] of [[aiButton, "AI 配置", profile?.name || "选择 AI 配置"], [memoryButton, "记忆配置", record.target.memory_profile?.name || "不额外带入记忆"]]) control.replaceChildren(node("span", "", label), node("strong", "", value), node("span", "", "⌄"));
      aiButton.disabled = memoryButton.disabled = !!busy || !!record.creation || typeof onChooseConfiguration !== "function";
      send.disabled = !!busy || record.creation?.handoff_started === true || unsavedTask?.id === record.id || !record.task.trim() || !aiReady(profile) || !memoryReady(record.target.memory_profile);
      send.textContent = busy ? busy.reading ? "正在核对…" : "正在准备…" : "发送 ↑";
      hint.textContent = getSendShortcut() === "ctrl-enter" ? "⌘/Ctrl+Enter 发送 · Enter 换行" : "Enter 发送 · Shift+Enter 换行";
      openTarget.hidden = !record.creation?.created_chat_id || typeof onOpenTarget !== "function";
      checkCreation.hidden = !record.creation || !!record.creation.created_chat_id || typeof onReadCreation !== "function"; checkCreation.disabled = !!busy;
      cancel.disabled = !!busy || !!record.creation && !record.creation.created_chat_id;
      message.textContent = record.creation?.handoff_started ? "已交给原对话，打开查看或继续；此处不会再次发送。" : notice || (record.creation ? "已有创建记录。继续只核对原编号，不另建对话。" : ""); message.hidden = !message.textContent;
      if (renderedSource !== record.id) {
        original.replaceChildren(...record.snapshot.records.map(row => { const details = node("details", "rbh-details"); details.append(node("summary", "", row.role === "user" ? "所选用户原文" : "所选 AI 原文"), node("pre", "file-task-preview-content", row.content)); return details; }));
        materialList.replaceChildren(...record.material_refs.map(ref => { const candidate = record.snapshot.material_candidates?.find(item => item.kind === ref.kind && item.id === ref.id); return node("p", "file-task-note", (ref.kind === "image" ? "原图：" : "文件：") + (candidate?.name || ref.id) + " · " + ref.sha256.slice(0, 8)); }));
        renderedSource = record.id;
      }
    }
    function show(id = getState()?.currentContinuation) {
      try {
        const value = state(); if (!value.continuations.some(row => row.id === id)) return false;
        if (value.currentContinuation !== id) persist({ ...value, currentContinuation: id });
        if (!visible || root.hidden) generation++;
        visible = true; notice = ""; render(); return true;
      } catch (error) { fail(error); return false; }
    }
    function hide() { generation++; visible = false; root.hidden = true; }
    function restore() { return show(); }
    function prepare(value, chosenProfile = null) {
      try {
        if (value?.workspace_id !== String(getWorkspace() || "") || !["codex", "hermes"].includes(value.target_agent) || !object(value.frozen?.snapshot) || !object(value.frozen.body)) throw Error("接续来源已变化；没有创建目标。");
        const snapshot = clone(value.frozen.snapshot), body = value.frozen.body, source = snapshot.source;
        const source_view = { workspace_id: value.workspace_id, agent: source.agent, chat_id: source.chat_id, title: source.title };
        const old = findBySource(source_view); if (old) return show(old.id);
        if (body.source_revision !== source.revision || !Array.isArray(body.message_ids) || new Set(body.message_ids).size !== body.message_ids.length) throw Error("所选文字版本不一致。");
        snapshot.records = snapshot.records.filter(row => body.message_ids.includes(row.message_id));
        if (canonical(snapshot.records.map(row => row.message_id)) !== canonical(body.message_ids)) throw Error("所选文字不完整，原来源保留。");
        const refs = window.RabbitNativeMaterials.refs(body.registered_material_refs || []);
        if (snapshot.material_candidates) snapshot.material_candidates = snapshot.material_candidates.filter(row => refs.some(ref => ref.kind === row.kind && ref.id === row.id));
        let profile = chosenProfile;
        if (!profile) profile = (getProfiles()?.ai_profiles || []).find(row => row.agent_instance_id === "local/" + value.target_agent && row.native_available === true) || null;
        if (profile && (profile.native_available !== true || profile.agent_instance_id !== "local/" + value.target_agent)) throw Error("所选配置尚不能用于这位 Agent。");
        const record = checked({ id: window.crypto.randomUUID(), revision: 1, source_view, snapshot, material_refs: refs, task: body.task, target: { agent: value.target_agent, ai_profile: profile ? pick(profile, aiFields) : null, memory_profile: null }, creation: null });
        const saved = state(); persist({ continuations: [...saved.continuations, record], currentContinuation: record.id });
        return show(record.id);
      } catch (error) { fail(error); return false; }
    }
    function getComposerView() {
      if (!visible || root.hidden) return null;
      const record = current(); if (!record) return null;
      return { ai_profile_id: record.target.ai_profile?.id || null, memory_profile_id: record.target.memory_profile?.id || "none", existing: false, native_history: true, native_agents: ["codex", "hermes"], continuation: true,
        view: canonical([getWorkspace(), record.id, record.revision, generation]), busy: !!busy || !!record.creation || unsavedTask?.id === record.id, notice };
    }
    function chooseConfiguration(kind, row, expectedView) {
      const view = getComposerView(); if (!view || view.busy || view.view !== expectedView || !["ai", "memory"].includes(kind)) return false;
      try {
        const record = current(), target = clone(record.target);
        if (kind === "ai") {
          const agent = row?.agent_instance_id?.replace(/^local\//, "");
          if (!["codex", "hermes"].includes(agent) || row.native_available !== true) throw Error("此处只接续到可用的 Codex 或 Hermes 配置。");
          target.agent = agent; target.ai_profile = pick(row, aiFields);
        } else target.memory_profile = row === "none" || row?.id === "none" ? null : memoryChoice(row);
        if (canonical(target) === canonical(record.target)) return true;
        replace({ ...record, revision: record.revision + 1, target }); notice = ""; render(); return true;
      } catch (error) { fail(error); return false; }
    }
    function creationProgress(record, workspace) {
      return patch => {
        if (!object(patch) || Object.keys(patch).length !== 1 || String(getWorkspace()) !== workspace) return false;
        const handingOff = Object.hasOwn(patch, "handoff_started");
        if (handingOff ? patch.handoff_started !== true : !Object.hasOwn(patch, "created_chat_id") || typeof patch.created_chat_id !== "string" || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(patch.created_chat_id)) return false;
        const saved = state().continuations.find(row => row.id === record.id);
        if (!saved || saved.creation?.creation_id !== record.creation.creation_id || (handingOff ? !saved.creation.created_chat_id || saved.revision !== record.revision || unsavedTask?.id === record.id : saved.creation.created_chat_id && saved.creation.created_chat_id !== patch.created_chat_id)) return false;
        try { return replace({ ...saved, creation: { ...saved.creation, ...patch } }); } catch (error) { fail(error); return false; }
      };
    }
    async function readCreation() {
      const record = current(); if (!record?.creation || record.creation.created_chat_id || busy || typeof onReadCreation !== "function") return false;
      const workspace = String(getWorkspace()); busy = { id: record.id, reading: true }; notice = ""; render();
      try {
        await onReadCreation(clone(record), creationProgress(record, workspace));
        if (String(getWorkspace()) === workspace) notice = state().continuations.find(row => row.id === record.id)?.creation?.created_chat_id ? "已找到原对话，可以打开或取消这份接续。" : "尚未找到原创建结果；记录保留，没有重新创建。";
        return true;
      } catch (error) { fail(error); return false; }
      finally { busy = null; render(); }
    }
    async function submit() {
      if (!visible || root.hidden || busy) return false;
      const original = current(); if (!original || original.creation?.handoff_started === true || unsavedTask?.id === original.id || !original.task.trim() || !aiReady(original.target.ai_profile) || !memoryReady(original.target.memory_profile)) return false;
      const stamp = generation, workspace = String(getWorkspace()), revision = original.revision;
      const originalCurrent = () => visible && !root.hidden && generation === stamp && String(getWorkspace()) === workspace && current()?.id === original.id && current()?.revision === revision && unsavedTask?.id !== original.id;
      busy = { id: original.id }; notice = ""; render();
      try {
        const { creation: _creation, ...intent } = original;
        const digest = [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(intent))))].map(byte => byte.toString(16).padStart(2, "0")).join("");
        if (!originalCurrent() || current()?.creation?.handoff_started === true) return false;
        if (original.creation && original.creation.source_hash !== digest) throw Error("任务或配置已变化。原创建记录保留，请先打开已创建对话处理。");
        const record = { ...original, creation: original.creation || { creation_id: window.crypto.randomUUID(), created_chat_id: null, source_hash: digest, handoff_started: false } };
        if (!original.creation) replace(record);
        const isCurrent = () => originalCurrent() && current()?.creation?.creation_id === record.creation.creation_id;
        const result = await onSubmit(clone(record), isCurrent, creationProgress(record, workspace));
        if (String(getWorkspace()) !== workspace) return false;
        const saved = state(), remaining = saved.continuations.find(row => row.id === record.id);
        if (result?.accepted === true && remaining?.revision === revision && remaining.creation?.creation_id === record.creation.creation_id && remaining.creation.handoff_started === true && unsavedTask?.id !== record.id) {
          persist({ continuations: saved.continuations.filter(row => row.id !== record.id), currentContinuation: saved.currentContinuation === record.id ? null : saved.currentContinuation });
          return true;
        }
        if (remaining) notice = "发送结果待核对；原创建编号和接续草稿保留。";
        return false;
      } catch (error) { fail(error); return false; }
      finally { busy = null; render(); }
    }
    function cancelCurrent() {
      const record = current(); if (!record || busy || record.creation && !record.creation.created_chat_id) return false;
      try { const value = state(); persist({ continuations: value.continuations.filter(row => row.id !== record.id), currentContinuation: null }); unsavedTask = null; hide(); onOpenSource?.(record.source_view); return true; } catch (error) { fail(error); return false; }
    }
    input.addEventListener("input", () => {
      const record = current(); if (!record) return;
      unsavedTask = { id: record.id, value: input.value };
      try { replace({ ...record, revision: record.revision + 1, task: input.value }); unsavedTask = null; notice = ""; render(); } catch (error) { fail(error); }
    });
    input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229 && !event.shiftKey && (getSendShortcut() === "ctrl-enter" ? event.ctrlKey || event.metaKey : !event.ctrlKey && !event.metaKey)) { event.preventDefault(); if (!busy) return submit(); }
    });
    return { prepare, show, hide, restore, current, findBySource, getComposerView, chooseConfiguration };
  }
  window.RabbitContinuations = Object.freeze({ create });
})();
