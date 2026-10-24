/* Common Markdown + Codex memory citations, constructed as DOM, never HTML. */
(() => {
  "use strict";
  const md = markdownit({ html: false, linkify: false, typographer: false, breaks: true });
  md.block.ruler.before("paragraph", "memory_citation", (state, start, end, silent) => {
    const first = state.src.slice(state.bMarks[start] + state.tShift[start], state.eMarks[start]);
    if (!first.startsWith("<oai-mem-citation>")) return false;
    let last = start;
    while (last < end && !state.src.slice(state.bMarks[last], state.eMarks[last]).includes("</oai-mem-citation>")) last++;
    if (last === end) return false; // A partial streaming answer remains visible.
    const raw = state.src.slice(state.bMarks[start] + state.tShift[start], state.eMarks[last]);
    if (!/^<oai-mem-citation>[\s\S]*<\/oai-mem-citation>\s*$/.test(raw)) return false;
    if (silent) return true;
    const token = state.push("memory_citation", "", 0);
    token.content = raw; token.map = [start, last + 1];
    state.line = last + 1;
    return true;
  });

  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = String(text);
    if (className) result.className = className;
    return result;
  }
  function text(parent, value, query) {
    if (!query) { parent.append(document.createTextNode(value)); return; }
    const low = value.toLocaleLowerCase(), needle = query.toLocaleLowerCase();
    let at = 0, found;
    while (needle && (found = low.indexOf(needle, at)) >= 0) {
      parent.append(document.createTextNode(value.slice(at, found)), node("mark", value.slice(found, found + query.length)));
      at = found + query.length;
    }
    parent.append(document.createTextNode(value.slice(at)));
  }
  function destination(href, basePath) {
    let value;
    try { value = decodeURIComponent(String(href)); } catch (_) { return { kind: "blocked" }; }
    if (/^(?:javascript|data|vbscript|file):/i.test(value) || value.startsWith("//") || value.includes("\u0000")) return { kind: "blocked" };
    if (/^https?:\/\//i.test(value) || /^mailto:/i.test(value)) return { kind: "web", value: href };
    // Absolute Mac text files, and relative text links inside a previewed file.
    const match = value.match(/^(.+?\.(?:md|txt))(?::([1-9]\d*))?(?:#.*)?$/i);
    if (!match || (!match[1].startsWith("/") && !basePath)) return { kind: "blocked" };
    const path = match[1].startsWith("/") ? match[1] : basePath.slice(0, basePath.lastIndexOf("/") + 1) + match[1];
    return { kind: "local", value: path, line: match[2] ? Number(match[2]) : null };
  }
  function citation(parent, raw, options) {
    const details = node("details", undefined, "memory-citation");
    const entries = (raw.match(/<citation_entries>([\s\S]*?)<\/citation_entries>/) || [])[1];
    const records = entries ? entries.trim().split(/\r?\n/).filter(Boolean) : [];
    details.append(node("summary", `记忆引用${records.length ? " · " + records.length + " 条来源" : ""}`));
    if (records.length) {
      const list = node("ul");
      for (const entry of records) {
        const item = node("li");
        const match = entry.match(/^(.+?):(\d+)-(\d+)\|note=\[([^\r\n]*)\]$/);
        text(item, match ? `${match[1]} · 第 ${match[2]}–${match[3]} 行：${match[4]}` : entry, options.query);
        list.append(item);
      }
      details.append(list);
    }
    details.append(node("p", "这是原消息附带的来源说明，尚未读取或核验来源文件。", "citation-note"));
    const original = node("details", undefined, "citation-raw");
    original.append(node("summary", "查看引用原文"));
    const pre = node("pre"); text(pre, raw, options.query); original.append(pre); details.append(original);
    if (options.query && raw.toLocaleLowerCase().includes(options.query.toLocaleLowerCase())) { details.open = true; original.open = true; }
    parent.append(details);
  }
  function appendTokens(parent, tokens, options) {
    const stack = [parent];
    for (const token of tokens) {
      const target = stack[stack.length - 1];
      if (token.type === "inline") { appendTokens(target, token.children || [], options); continue; }
      if (token.type === "memory_citation") { citation(target, token.content, options); continue; }
      if (token.type === "text" || token.type === "html_inline" || token.type === "html_block") { text(target, token.content, options.query); continue; }
      if (["code_inline", "code_block", "fence"].includes(token.type)) {
        const code = node("code"); text(code, token.content, options.query);
        if (token.type === "code_inline") target.append(code);
        else { const pre = node("pre"); pre.append(code); target.append(pre); }
        continue;
      }
      if (token.type === "image") { text(target, "[图片：" + (token.content || "无描述") + "]", options.query); continue; }
      if (["softbreak", "hardbreak"].includes(token.type)) { target.append(node("br")); continue; }
      if (token.type === "hr") { target.append(node("hr")); continue; }
      if (token.nesting === -1) { if (stack.length > 1) stack.pop(); continue; }
      if (token.nesting === 1) {
        let element;
        if (token.type === "link_open") {
          const href = token.attrGet("href") || "", link = destination(href, options.basePath);
          element = node(link.kind === "web" ? "a" : link.kind === "local" ? "button" : "span");
          if (link.kind === "web") {
            element.setAttribute("href", link.value); element.setAttribute("target", "_blank"); element.setAttribute("rel", "noopener noreferrer");
          } else if (link.kind === "local") {
            element.type = "button"; element.className = "document-link"; element.title = "预览本项目文字文件：" + link.value;
            element.addEventListener("click", () => options.openDocument && options.openDocument(link.value, link.line));
          }
        } else {
          const allowed = new Set(["p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "strong", "em", "s", "table", "thead", "tbody", "tr", "th", "td"]);
          element = node(allowed.has(token.tag) ? token.tag : "span");
          if (token.tag === "ol" && token.attrGet("start")) element.setAttribute("start", token.attrGet("start"));
          if (token.hidden) element.className = "markdown-tight";
        }
        target.append(element); stack.push(element);
      }
    }
  }
  function render(parent, source, options = {}) {
    parent.classList.add("rich-text");
    try { appendTokens(parent, md.parse(String(source), {}), options); }
    catch (_) { parent.replaceChildren(); text(parent, String(source), options.query); }
  }
  window.OneAIRichText = { render, destination };
})();
