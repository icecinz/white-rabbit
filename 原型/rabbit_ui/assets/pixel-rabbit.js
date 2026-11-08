(() => {
  "use strict";

  const slot = document.getElementById("home-pixel-rabbit");
  if (!slot || typeof IntersectionObserver !== "function") return;
  const sidebar = document.getElementById("sidebar");
  const frames = slot.querySelectorAll("img");
  const cycle = [[0, 3010], [1, 60], [2, 90], [1, 60]];
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let visible = false;
  let phase = 0;
  let timer = null;

  function showFrame(index) {
    frames.forEach((image, frame) => { image.hidden = frame !== index; });
  }

  function canBlink() {
    return visible && !document.hidden
      && sidebar.getAttribute("aria-hidden") !== "true" && !reducedMotion.matches;
  }

  function synchronize() {
    if (!canBlink()) {
      clearTimeout(timer);
      timer = null;
      phase = 0;
      showFrame(0);
      return;
    }
    if (timer !== null) return;
    timer = setTimeout(() => {
      timer = null;
      if (canBlink()) {
        phase = (phase + 1) % cycle.length;
        showFrame(cycle[phase][0]);
      }
      synchronize();
    }, cycle[phase][1]);
  }

  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    synchronize();
  }).observe(slot);
  const visibility = new MutationObserver(synchronize);
  visibility.observe(sidebar, { attributeFilter: ["aria-hidden"] });
  document.addEventListener("visibilitychange", synchronize);
  reducedMotion.addEventListener("change", synchronize);
})();
