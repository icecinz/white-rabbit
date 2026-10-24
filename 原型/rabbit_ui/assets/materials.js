(() => {
  "use strict";
  const pattern = /^img_[a-f0-9]{32}$/;
  const reasons = {
    not_materialized: "原图尚未接入本机图片材料库",
    claude_image_not_integrated: "Claude 原图尚未接入；只保留图片位置",
    pi_image_not_integrated: "Pi 原图尚未接入；只保留图片位置",
    native_unavailable: "原历史图片暂不可用", descriptor_invalid: "图片来源信息不完整",
    source_outside: "图片来源不在已接入的历史范围", source_missing: "原图片记录已找不到",
    source_unavailable: "目前无法读取原图片记录", source_changed: "原图片记录已变化，需要重新读取历史",
    source_read_limit: "原图片记录超过读取范围", source_content_limit: "原消息超过图片检查大小上限",
    source_block_count_limit: "原消息超过图片检查块数上限", attachment_display_limit: "图片超过历史检查数量上限",
    native_id_conflict: "同一条原消息存在不同图片副本，暂不能确定原图",
    external_image_not_loaded: "只有图片链接，没有读取外部图片", unsupported_image_block: "这种原历史图片格式暂不支持",
    unsupported_image_shape: "原图片信息格式暂不支持", unsupported_source_field: "原图片字段格式暂不支持",
    unsupported_image_mime: "目前只支持 PNG 和 JPEG 原图", invalid_image_base64: "原图片编码不完整",
    invalid_image_signature: "原图片类型校验未通过", invalid_image: "原图片内容校验未通过",
    image_size_limit: "原图超过单张图片大小上限", storage_capacity: "本机图片材料库已满",
    storage_unavailable: "本机图片材料库暂不可用", missing: "本机图片材料已找不到",
  };
  function reason(value) { return reasons[value] || "图片暂不可用，原因尚未确认"; }
  function failure(error, fallback) { const detail = error?.message; return reasons[error?.reason] || (typeof detail === "string" && /[\u4e00-\u9fff]/.test(detail) && detail.length < 220 ? detail : fallback); }
  function ids(value, maximum = 4) {
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.length > maximum || value.some(id => typeof id !== "string" || !pattern.test(id)) || new Set(value).size !== value.length) throw new Error("图片材料标识不完整，原草稿已保留。");
    return [...value];
  }
  function material(value, expected) {
    if (!value || !pattern.test(value.id) || expected && value.id !== expected || value.kind !== "image" || !["image/png", "image/jpeg"].includes(value.mime) || value.status !== "ready" || typeof value.name !== "string" || !Number.isSafeInteger(value.size_bytes) || value.size_bytes <= 0 || !Number.isSafeInteger(value.width) || value.width <= 0 || !Number.isSafeInteger(value.height) || value.height <= 0) throw new Error("图片材料暂时没读完整，请重试。");
    return { id: value.id, kind: "image", mime: value.mime, name: value.name, size_bytes: value.size_bytes, width: value.width, height: value.height, status: "ready", source: value.source || "upload" };
  }
  function create({ api, root, onError = () => {} }) {
    root.hidden = true;
    const cache = new Map(), originals = new Map(); let version = 0, focusBefore = null;
    const node = (tag, name, text) => { const result = document.createElement(tag); if (name) result.className = name; if (text !== undefined) result.textContent = text; return result; };
    function action(text, callback, name = "material-button") { const result = node("button", name, text); result.type = "button"; result.addEventListener("click", callback); return result; }
    function close() { ++version; root.hidden = true; root.replaceChildren(); const shell = document.querySelector(".app-shell"); if (shell) shell.inert = false; if (focusBefore?.focus) focusBefore.focus(); }
    async function image(id, original = false) {
      ids([id]); const key = id + (original ? "/content" : "/thumbnail"), selectedCache = original ? originals : cache;
      if (!selectedCache.has(key)) {
        const pending = api(`/api/materials/${key}`).then(data => {
          const meta = material(data.material, id);
          if (typeof data.data_url !== "string" || !/^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(data.data_url)) throw new Error("图片内容暂时无法显示，请重试。");
          return { material: meta, data_url: data.data_url };
        }).catch(error => { selectedCache.delete(key); throw error; });
        if (selectedCache.size >= (original ? 2 : 32)) selectedCache.delete(selectedCache.keys().next().value);
        selectedCache.set(key, pending);
      }
      return await selectedCache.get(key);
    }
    async function open(id) {
      const current = ++version; focusBefore = document.activeElement; root.hidden = false; root.replaceChildren();
      const shell = document.querySelector(".app-shell"); if (shell) shell.inert = true;
      const panel = node("div", "material-viewer-panel"), head = node("header", "material-viewer-header"), title = node("h2", "", "查看图片"); title.id = "material-viewer-title";
      const exit = action("关闭", close); head.append(title, exit); panel.append(head, node("p", "material-loading", "正在读取原图…")); root.append(panel); exit.focus();
      try {
        const data = await image(id, true); if (current !== version || root.hidden) return;
        const img = node("img", "material-original"); img.src = data.data_url; img.alt = data.material.name || "图片材料";
        panel.replaceChildren(head, node("p", "material-meta", `${data.material.name || "图片"} · ${data.material.width}×${data.material.height}`), img);
      } catch (error) { if (current !== version || root.hidden) return; panel.replaceChildren(head, node("p", "material-error", failure(error, "原图暂时无法显示。材料引用仍保留，可重试。")), action("重试", () => open(id))); onError(error); }
    }
    function card(id, { remove = null, move = null, disabled = false } = {}) {
      ids([id]); const result = node("div", "material-card"); result.dataset.materialId = id;
      const view = action("正在读取图片…", () => open(id), "material-thumb-button"); view.setAttribute("aria-label", "查看图片原图");
      const text = node("span", "material-name", "图片材料"); result.append(view, text);
      if (move) { const reorder = action("前移", move, "material-move"); reorder.disabled = disabled; reorder.setAttribute("aria-label", "将这张图片前移"); result.append(reorder); }
      if (remove) { const erase = action("移除", remove, "material-remove"); erase.disabled = disabled; erase.setAttribute("aria-label", "从草稿移除这张图片"); result.append(erase); }
      image(id).then(data => {
        const img = node("img", "material-thumbnail"); img.src = data.data_url; img.alt = data.material.name || "图片材料";
        view.replaceChildren(img); view.setAttribute("aria-label", `查看图片：${data.material.name || "图片材料"}`); text.textContent = data.material.name || "图片材料";
      }).catch(error => { view.textContent = "图片暂时无法显示 · 点击查看原因"; text.textContent = failure(error, "图片内容暂时无法读取，材料引用仍保留"); });
      return result;
    }
    async function upload(file, maximum = 5242880) {
      if (!file || !["image/png", "image/jpeg"].includes(file.type)) throw new Error("目前只支持 PNG 和 JPEG 图片。");
      if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > maximum) throw new Error(`图片最多 ${Math.round(maximum / 1024 / 1024)} MiB；原文字仍保留。`);
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (bytes.length !== file.size || bytes.length > maximum) throw new Error("图片没有读取完整，请重新添加。");
      let binary = ""; for (let start = 0; start < bytes.length; start += 32768) binary += String.fromCharCode(...bytes.subarray(start, start + 32768));
      const data_base64 = btoa(binary);
      const response = await api("/api/materials", { method: "POST", body: JSON.stringify({ name: file.name || "粘贴的图片", data_base64 }) });
      return material(response.material);
    }
    root.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); close(); } if (event.key === "Tab") { const controls = [...root.querySelectorAll("button")].filter(button => !button.disabled); if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); } else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); } } });
    return { upload, image, card, open, close, ids, material };
  }
  window.RabbitMaterials = { create, ids, material, reason };
})();
