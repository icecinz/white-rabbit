(() => {
  "use strict";
  const limit = 524288;
  function utf8(value) {
    if (typeof value !== "string" || value.includes("\0")) throw new Error("文件原文未通过完整核对，没有下载；原预览和记录保留。");
    // Reject malformed UTF-16 rather than letting TextEncoder replace it.
    // Valid surrogate pairs, BOM and line endings retain their exact bytes.
    for (let index = 0; index < value.length; index++) {
      const unit = value.charCodeAt(index);
      if (unit >= 0xd800 && unit <= 0xdbff) {
        const next = value.charCodeAt(++index);
        if (!(next >= 0xdc00 && next <= 0xdfff)) throw new Error("文件原文不是完整 UTF-8 文本，没有下载；原预览和记录保留。");
      } else if (unit >= 0xdc00 && unit <= 0xdfff) throw new Error("文件原文不是完整 UTF-8 文本，没有下载；原预览和记录保留。");
    }
    return new TextEncoder().encode(value);
  }
  async function download(result) {
    if (window.location.hostname === "app.im.codes" && /^\/api\/server\/[^/]+\/local-web\/[^/]+(?:\/|$)/.test(window.location.pathname) && window.self !== window.top) throw new Error("请先点击 IM.codes 的“在新标签页打开”，再下载文件；当前仍可预览。");
    const item = result?.artifact;
    if (!item || typeof item !== "object" || typeof item.name !== "string" || !item.name || item.name.length > 240 || item.name.startsWith(".") || /[\\/\x00-\x1f\x7f]/.test(item.name) || !/\.(?:md|txt)$/i.test(item.name) || !Number.isSafeInteger(item.size_bytes) || item.size_bytes < 0 || item.size_bytes > limit || typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256) || item.available !== true || item.content_verified !== true) throw new Error("文件版本尚未完整核验，没有下载；原预览和记录保留。");
    // Snapshot metadata before awaiting: a later UI change cannot rename or
    // replace the body that was selected by this explicit download action.
    const name = item.name, expected = item.sha256, size = item.size_bytes;
    const bytes = utf8(result.text);
    if (bytes.byteLength !== size || bytes.byteLength > limit) throw new Error("文件原文与保存大小不同，没有下载；原预览和记录保留。");
    if (!globalThis.crypto?.subtle || typeof globalThis.crypto.subtle.digest !== "function") throw new Error("当前浏览器暂不能核对完整文件，原预览和记录保留。请刷新或换浏览器后再下载。");
    let digest;
    try { digest = await globalThis.crypto.subtle.digest("SHA-256", bytes); }
    catch (_) { throw new Error("完整文件核对未完成，没有下载；原预览和记录保留。"); }
    const actual = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
    if (actual !== expected) throw new Error("文件原文与固定版本不同，没有下载；原预览和记录保留。");
    const url = URL.createObjectURL(new Blob([bytes], { type: "text/plain;charset=utf-8" }));
    let link;
    try {
      link = document.createElement("a"); link.href = url; link.download = name;
      document.body.append(link); link.click();
    } finally {
      link?.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }
  window.RabbitTextFileDownload = Object.freeze({ download });
})();
