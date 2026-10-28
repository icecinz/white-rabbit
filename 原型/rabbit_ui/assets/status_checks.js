/* One explicit status check, stored facts and AI comments. No Agent loop. */
(() => {
  "use strict";
  const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
  const HASH = /^[a-f0-9]{64}$/;
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const text = (value, limit = 1000) => typeof value === "string" && value.length <= limit && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value);
  const stages = new Set(["collecting", "facts_ready", "analyzing", "completed", "partial", "analysis_failed", "unconfirmed"]);
  const factIds = new Set(["configuration", "catalogue", "tools", "memory"]);
  const factStates = new Set(["observed", "unverified", "skipped"]);
  const judgments = new Set(["consistent", "review_needed", "unverified", "not_applicable"]);
  const active = value => ["collecting", "facts_ready", "analyzing"].includes(value?.stage);
  const fields = ["agent", "chat_id", "profile_id", "profile_revision", "memory_profile_id", "memory_profile_revision"];
  const canonical = value => JSON.stringify(value, (_key, entry) => object(entry) ? Object.fromEntries(Object.keys(entry).sort().map(key => [key, entry[key]])) : entry);
  function target(value) {
    if (!object(value)) throw Error("检查对象不完整，尚未发送。");
    const result = {};
    for (const key of fields) {
      const item = value[key]; if (item == null) continue;
      const valid = key === "agent" ? ["codex", "pi", "hermes", "claude", "dsh"].includes(item)
        : key.endsWith("revision") ? Number.isSafeInteger(item) && item > 0
        : typeof item === "string" && UUID.test(item);
      if (!valid) throw Error("检查对象不完整，尚未发送。");
      result[key] = item;
    }
    return result;
  }
  function report(value, workspace) {
    if (value === null) return null;
    if (!object(value) || value.schema_version !== 1 || value.workspace_id !== workspace || !UUID.test(value.check_id)
        || !stages.has(value.stage) || !Array.isArray(value.facts) || value.facts.length > 4 || !text(value.created_at, 100)
        || value.published_at !== null && !text(value.published_at, 100) || value.error_code !== null && !text(value.error_code, 160)) throw Error("检查报告身份不完整，请重新读取。");
    const normalized = target(value.target), ids = new Map();
    if (canonical(normalized) !== canonical(value.target) || JSON.stringify(value).length > 262144) throw Error("检查报告范围不完整，请重新读取。");
    for (const fact of value.facts) {
      if (!object(fact) || !factIds.has(fact.id) || ids.has(fact.id) || !text(fact.label, 200) || !factStates.has(fact.state)
          || !text(fact.source, 200) || fact.observed_at !== null && !text(fact.observed_at, 100)
          || !text(fact.read_at, 100) || !object(fact.identity) || !object(fact.data) || !HASH.test(fact.evidence_sha256)) throw Error("程序事实没有读完整，请重新读取。");
      ids.set(fact.id, fact.evidence_sha256);
    }
    if (value.analysis !== null) {
      const analysis = value.analysis, source = analysis?.source;
      if (!object(analysis) || analysis.check_id !== value.check_id || !object(source) || source.agent !== "claude" || source.mode !== "tool_free_text" || !text(source.model, 220)
          || !["requested_route", "service_reported"].includes(source.model_source) || !text(analysis.summary, 1600) || !Array.isArray(analysis.items) || analysis.items.length > 4) throw Error("专员说明没有读完整，请重新读取。");
      const seen = new Set();
      for (const item of analysis.items) {
        if (!object(item) || !ids.has(item.evidence_id) || seen.has(item.evidence_id) || item.evidence_sha256 !== ids.get(item.evidence_id)
            || !judgments.has(item.judgment) || !text(item.note, 1600) || typeof item.applicable !== "boolean") throw Error("专员说明与程序事实不匹配，请重新读取。");
        seen.add(item.evidence_id);
      }
    }
    if (["completed", "partial"].includes(value.stage) && (ids.size !== 4 || !value.analysis || value.analysis.items.length !== 4)) throw Error("检查结果尚未完整发布，请重新读取。");
    return value;
  }
  function create({ root, api, getWorkspace = () => "", getTarget = () => ({}), onPublished = () => {}, onError = () => {} }) {
    let workspace = "", journal = null, value = null, visible = false, destroyed = false, busy = false, version = 0, timer = null, message = "", storageInvalid = false;
    const published = new Set();
    const node = (tag, cls, content) => { const result = document.createElement(tag); if (cls) result.className = cls; if (content !== undefined) result.textContent = content; return result; };
    const heading = node("p", "rbs-note"), summary = node("p", "rbs-note"), details = node("details", "rbs-status-check-details"), detailsTitle = node("summary", "", "检查记录"), body = node("div");
    details.append(detailsTitle, body); root.append(heading, summary, details); root.setAttribute("data-control-interface", "ai"); root.setAttribute("aria-live", "polite");
    const key = () => "rabbit.status-check.v1." + encodeURIComponent(workspace);
    function save(record) {
      try { window.localStorage.setItem(key(), JSON.stringify(record)); journal = record; return true; }
      catch (_) { message = "浏览器无法保存检查编号，尚未发送。"; return false; }
    }
    function sync() {
      const scope = String(getWorkspace() || ""); if (workspace === scope) return;
      version++; clearTimeout(timer); timer = null; workspace = scope; journal = value = null; storageInvalid = false; message = "";
      if (!scope) return;
      try {
        const raw = window.localStorage.getItem(key()); if (raw === null) return;
        const stored = JSON.parse(raw), parsed = JSON.parse(stored.body_json);
        if (!object(stored) || stored.version !== 1 || !UUID.test(stored.check_id) || typeof stored.pending !== "boolean"
            || !object(parsed) || parsed.check_id !== stored.check_id || Object.keys(parsed).sort().join(",") !== "check_id,target"
            || canonical(target(parsed.target)) !== canonical(parsed.target)) throw Error();
        journal = stored;
      } catch (_) { storageInvalid = true; message = "本浏览器的检查编号待核对；没有发送新检查。"; }
    }
    function render() {
      root.hidden = !visible || !workspace;
      const names = { collecting: "正在读取程序事实", facts_ready: "程序事实已读取，等待专员判断", analyzing: "专员正在判断", completed: "状态已更新", partial: "状态已部分更新", analysis_failed: "程序事实已读取，专员说明未完成", unconfirmed: "本次结果待核实" };
      heading.textContent = value ? names[value.stage] : journal?.pending ? "本次结果待核实" : "尚未手动更新状态";
      const stale = value?.analysis?.items.some(item => !item.applicable);
      summary.textContent = message || (stale ? "部分依据已变化，说明待更新。" : value?.analysis?.summary || "");
      summary.hidden = !summary.textContent; details.hidden = !value && !journal?.pending && !storageInvalid;
      body.replaceChildren();
      if (value) {
        body.append(node("p", "rbs-note", "检查时间：" + value.created_at));
        if (value.published_at) body.append(node("p", "rbs-note", "保存时间：" + value.published_at));
        if (value.analysis) {
          const source = value.analysis.source;
          const modelLabel = source.model_source === "requested_route" ? "请求型号" : source.model_source === "service_reported" ? "连接报告型号" : "记录型号（来源未核实）";
          body.append(node("p", "rbs-note", `专员说明：Claude · ${modelLabel}：${source.model} · 无工具文字分析`));
        }
        for (const fact of value.facts) {
          const item = node("section", "rbs-status-check-fact"), comment = value.analysis?.items.find(row => row.evidence_id === fact.id);
          const state = { observed: "已读取", unverified: "未核实", skipped: "未纳入" }[fact.state] || "未核实";
          const judgment = { consistent: "依据一致", review_needed: "需处理", unverified: "未核实", not_applicable: "不适用" }[comment?.judgment] || "待核对";
          item.append(node("h4", "", fact.label), node("p", "rbs-note", "程序记录：" + state), node("p", "rbs-note", "来源：" + fact.source),
            node("p", "rbs-note", "观察时间：" + (fact.observed_at || "未报告")));
          if (Object.keys(fact.data).length) item.append(node("pre", "rbs-native", JSON.stringify(fact.data, null, 2)));
          if (comment) item.append(node("p", "rbs-note", (comment.applicable ? "专员判断：" : "上次判断，依据已变化：") + judgment), node("p", "rbs-note", comment.note));
          body.append(item);
        }
      }
      if (journal?.pending || value?.stage === "unconfirmed") {
        const read = node("button", "rbs-button", "重新读取检查结果"); read.type = "button"; read.disabled = busy;
        read.addEventListener("click", () => refresh({ recover: true })); body.append(read);
      }
    }
    function schedule() {
      clearTimeout(timer); timer = null;
      if (!destroyed && visible && !document.hidden && active(value)) timer = setTimeout(() => refresh(), 2000);
    }
    function accept(envelope, expectedId, expectedTarget, canonicalPost = false) {
      if (!object(envelope) || envelope.workspace_id !== workspace || !Object.hasOwn(envelope, "report")) throw Error("检查结果不属于当前工作区。");
      const received = report(envelope.report, workspace);
      if (!received) return null;
      if (canonicalPost ? canonical(received.target) !== canonical(expectedTarget) : expectedId && received.check_id !== expectedId) throw Error("检查结果与原编号或对象不匹配。");
      value = received; message = "";
      const adopted = { version: 1, check_id: received.check_id, body_json: JSON.stringify({ check_id: received.check_id, target: received.target }), pending: false };
      if (!save(adopted)) message = "结果已读到，但浏览器未保存检查编号；请保留当前页面。";
      if (["completed", "partial"].includes(received.stage) && received.analysis && received.published_at && !published.has(received.check_id)) {
        const ownScope = workspace, ownId = received.check_id;
        published.add(ownId); Promise.resolve().then(() => onPublished(received)).catch(() => { if (!destroyed && workspace === ownScope && value?.check_id === ownId) { message = "报告已保存，界面资料尚未读回。"; render(); } });
      }
      return received;
    }
    async function refresh(options = {}) {
      sync(); if (!workspace || destroyed || busy || storageInvalid) return value;
      const scope = workspace, stamp = version, expected = journal?.check_id || null;
      busy = true; clearTimeout(timer); timer = null; render();
      try {
        const envelope = await api("/api/status-checks" + (expected ? "?check_id=" + encodeURIComponent(expected) : ""));
        if (destroyed || scope !== String(getWorkspace() || "") || stamp !== version) return null;
        const received = accept(envelope, expected, null);
        if (!received && journal?.pending && options.recover === true) {
          const savedTarget = JSON.parse(journal.body_json).target, latest = await api("/api/status-checks");
          if (destroyed || scope !== String(getWorkspace() || "") || stamp !== version) return null;
          if (latest?.report && canonical(latest.report.target) === canonical(savedTarget)) accept(latest, null, savedTarget, true);
          else message = "原请求结果尚未核实；没有重新发送。";
        } else if (!received && journal?.pending) message = "原请求结果尚未核实；可展开检查记录重新读取。";
        return value;
      } catch (_) { if (!destroyed && scope === String(getWorkspace() || "") && stamp === version) message = "状态报告暂未完整读回；没有重复发送。"; return null; }
      finally { busy = false; if (!destroyed) { sync(); render(); schedule(); } }
    }
    async function start() {
      sync(); if (!workspace || destroyed || busy || storageInvalid) return null;
      if (journal?.pending || active(value)) return refresh({ recover: true });
      let body;
      try {
        const id = window.crypto?.randomUUID?.(); if (!UUID.test(id || "")) throw Error("当前浏览器不能生成检查编号，尚未发送。");
        body = { check_id: id, target: target(getTarget() || {}) };
      } catch (error) { onError(error); return null; }
      if (!save({ version: 1, check_id: body.check_id, body_json: JSON.stringify(body), pending: true })) { render(); return null; }
      const scope = workspace, stamp = version; value = null; message = "正在读取程序事实…"; busy = true; render();
      try {
        const envelope = await api("/api/status-checks", { method: "POST", body: JSON.stringify(body) });
        if (destroyed || scope !== String(getWorkspace() || "") || stamp !== version) return null;
        return accept(envelope, body.check_id, body.target, true);
      } catch (error) {
        if (!destroyed && scope === String(getWorkspace() || "") && stamp === version) {
          if (error?.code === "status_check_rejected" && error.accepted === false && journal?.check_id === body.check_id && journal.pending) {
            try {
              const stored = JSON.parse(window.localStorage.getItem(key()) || "null");
              if (stored?.check_id !== body.check_id || stored.body_json !== journal.body_json) throw Error();
              window.localStorage.removeItem(key()); journal = null;
              message = "这次检查未创建；可以调整后重新点击。";
            } catch (_) { message = "这次检查未创建，但浏览器记录尚未清除；请保留并核对原编号。"; }
          } else message = "检查请求结果待核实；没有重复发送。";
        }
        return null;
      }
      finally { busy = false; if (!destroyed) { sync(); render(); schedule(); } }
    }
    function show() { visible = true; sync(); render(); if (!busy) return refresh(); }
    function hide() { visible = false; clearTimeout(timer); timer = null; render(); }
    const visibility = () => { if (document.hidden) { clearTimeout(timer); timer = null; } else if (visible) refresh(); };
    document.addEventListener?.("visibilitychange", visibility);
    function destroy() { destroyed = true; hide(); version++; document.removeEventListener?.("visibilitychange", visibility); }
    render(); return { start, show, hide, refresh, destroy };
  }
  window.RabbitStatusChecks = Object.freeze({ create, normalize: report });
})();
