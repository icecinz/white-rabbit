(() => {
  "use strict";

  function mount({ root, status, choices, input, materials = [], support, send, hint, label = null }) {
    if (!Array.isArray(materials)) throw new Error("对话输入框的材料节点无效。");
    const nodes = [status, choices, input, ...materials, support, send, hint, ...(label ? [label] : [])];
    if (!root?.replaceChildren || nodes.some(node => typeof node?.append !== "function")) {
      throw new Error("对话输入框的布局节点不完整。");
    }
    const doc = root.ownerDocument || document;
    const bottomline = doc.createElement("div");
    const submit = doc.createElement("div");
    bottomline.className = "composer-bottomline";
    submit.className = "composer-submit";
    root.classList.add("composer", "composer-shell");
    input.classList.add("conversation-input");
    choices.classList.add("profile-choice-row");
    hint.classList.add("composer-hint");
    submit.append(send, hint);
    bottomline.append(support, submit);
    // Move the caller's original nodes; its listeners, values and ownership stay intact.
    root.replaceChildren(status, choices, ...(label ? [label] : []), input, ...materials, bottomline);
    return root;
  }

  window.RabbitComposerShell = Object.freeze({ mount });
})();
