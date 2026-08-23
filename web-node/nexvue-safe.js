/**
 * nexvue-safe.js — title / action safe overlay + center target + 4:3 cut
 * for Player / Multiview. Geometry only (no encode change). HD SMPTE RP 218
 * / EBU R95: action 93%, title 90%. Optional 4:3 center-cut (75% of 16:9).
 *
 * Per-browser prefs (localStorage):
 *   nexvue-safe-on      1 | 0   (default off)
 *   nexvue-safe-target  1 | 0   (default on when overlay is shown)
 *   nexvue-safe-43      1 | 0   (default off)
 */
(function (global) {
  "use strict";

  const PREF_ON = "nexvue-safe-on";
  const PREF_TARGET = "nexvue-safe-target";
  const PREF_CUT43 = "nexvue-safe-43";

  const ACTION_INSET = 3.5;
  const TITLE_INSET = 5;
  const CUT43_WIDTH = 75;

  function prefOn(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      if (v === null || v === "") return fallback;
      return v === "1" || v === "true";
    } catch (e) {
      return fallback;
    }
  }

  function setPref(key, on) {
    try {
      localStorage.setItem(key, on ? "1" : "0");
    } catch (e) { /* private mode */ }
  }

  function getOnPref() { return prefOn(PREF_ON, false); }
  function setOnPref(on) { setPref(PREF_ON, on); }
  function getTargetPref() { return prefOn(PREF_TARGET, true); }
  function setTargetPref(on) { setPref(PREF_TARGET, on); }
  function getCut43Pref() { return prefOn(PREF_CUT43, false); }
  function setCut43Pref(on) { setPref(PREF_CUT43, on); }

  function videoContentRect(video) {
    const cw = video.clientWidth || 0;
    const ch = video.clientHeight || 0;
    const vw = video.videoWidth || 0;
    const vh = video.videoHeight || 0;
    if (!cw || !ch) return { left: 0, top: 0, width: 0, height: 0 };
    if (!vw || !vh) return { left: 0, top: 0, width: cw, height: ch };
    const scale = Math.min(cw / vw, ch / vh);
    const w = vw * scale;
    const h = vh * scale;
    return { left: (cw - w) / 2, top: (ch - h) / 2, width: w, height: h };
  }

  function ensureStyles() {
    if (document.getElementById("nexvue-safe-css")) return;
    const s = document.createElement("style");
    s.id = "nexvue-safe-css";
    s.textContent = `
.nexvue-safe {
  position: absolute; inset: 0; z-index: 1; pointer-events: none;
}
.nexvue-safe[hidden] { display: none !important; }
.nexvue-safe-toolbar {
  position: absolute; top: 8px; left: 8px; z-index: 2;
  display: flex; gap: 3px; pointer-events: auto;
}
.nexvue-safe-toolbar button {
  background: var(--badge-bg, rgba(20,24,29,.85)); color: var(--dim, #98a6b5);
  border: 1px solid var(--edge, #2c3542); border-radius: 3px;
  padding: 2px 6px; font: 10px/1.2 ui-monospace, "Cascadia Mono", Consolas, monospace;
  cursor: pointer;
}
.nexvue-safe-toolbar button:hover { color: var(--text, #d6dde6); border-color: var(--acc, #56c4f5); }
.nexvue-safe-toolbar button.active {
  color: var(--on-acc, #08131a); background: var(--acc, #56c4f5);
  border-color: var(--acc, #56c4f5); font-weight: 600;
}
.nexvue-safe-frame {
  position: absolute; pointer-events: none;
}
.nexvue-safe-frame svg { width: 100%; height: 100%; display: block; overflow: visible; }
.nexvue-safe-action { fill: none; stroke: rgba(86,196,245,.85); stroke-width: 0.35; }
.nexvue-safe-title { fill: none; stroke: rgba(245,166,35,.9); stroke-width: 0.35; }
.nexvue-safe-cut43 { fill: none; stroke: rgba(229,72,77,.75); stroke-width: 0.3; stroke-dasharray: 1.2 0.8; }
.nexvue-safe-target { fill: none; stroke: rgba(255,255,255,.9); stroke-width: 0.28; }
.nexvue-safe-target-dot { fill: rgba(255,255,255,.9); }
.nexvue-safe:not(.show-target) .nexvue-safe-target,
.nexvue-safe:not(.show-target) .nexvue-safe-target-dot { display: none; }
.nexvue-safe:not(.show-43) .nexvue-safe-cut43 { display: none; }
`;
    document.head.appendChild(s);
  }

  /**
   * @param {object} opts
   * @param {HTMLElement} opts.container
   * @param {HTMLVideoElement} opts.video
   */
  function attach(opts) {
    ensureStyles();
    const container = opts && opts.container;
    const video = opts && opts.video;
    if (!container || !video) return null;

    const root = document.createElement("div");
    root.className = "nexvue-safe";
    root.hidden = true;
    root.setAttribute("aria-hidden", "true");
    const a = ACTION_INSET;
    const t = TITLE_INSET;
    const c43x = (100 - CUT43_WIDTH) / 2;
    root.innerHTML =
      '<div class="nexvue-safe-toolbar">' +
      '<button type="button" data-safe="target" title="Center target / crosshair">Target</button>' +
      '<button type="button" data-safe="cut43" title="4:3 center-cut in 16:9">4:3</button>' +
      "</div>" +
      '<div class="nexvue-safe-frame">' +
      '<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">' +
      `<rect class="nexvue-safe-action" x="${a}" y="${a}" width="${100 - 2 * a}" height="${100 - 2 * a}"/>` +
      `<rect class="nexvue-safe-title" x="${t}" y="${t}" width="${100 - 2 * t}" height="${100 - 2 * t}"/>` +
      `<rect class="nexvue-safe-cut43" x="${c43x}" y="0" width="${CUT43_WIDTH}" height="100"/>` +
      '<line class="nexvue-safe-target" x1="50" y1="38" x2="50" y2="62"/>' +
      '<line class="nexvue-safe-target" x1="38" y1="50" x2="62" y2="50"/>' +
      '<circle class="nexvue-safe-target" cx="50" cy="50" r="4.5"/>' +
      '<circle class="nexvue-safe-target-dot" cx="50" cy="50" r="0.7"/>' +
      "</svg></div>";
    container.appendChild(root);

    const frame = root.querySelector(".nexvue-safe-frame");
    const btnTarget = root.querySelector('[data-safe="target"]');
    const btn43 = root.querySelector('[data-safe="cut43"]');

    let visible = opts.visible !== undefined ? !!opts.visible : getOnPref();
    let targetOn = opts.target !== undefined ? !!opts.target : getTargetPref();
    let cut43On = opts.cut43 !== undefined ? !!opts.cut43 : getCut43Pref();
    let ro = null;

    function paintFlags() {
      root.classList.toggle("show-target", targetOn);
      root.classList.toggle("show-43", cut43On);
      btnTarget.classList.toggle("active", targetOn);
      btn43.classList.toggle("active", cut43On);
    }

    function layout() {
      const r = videoContentRect(video);
      frame.style.left = r.left + "px";
      frame.style.top = r.top + "px";
      frame.style.width = r.width + "px";
      frame.style.height = r.height + "px";
    }

    function applyVisible() {
      root.hidden = !visible;
      if (visible) layout();
    }

    btnTarget.addEventListener("click", (e) => {
      e.stopPropagation();
      targetOn = !targetOn;
      setTargetPref(targetOn);
      paintFlags();
    });
    btn43.addEventListener("click", (e) => {
      e.stopPropagation();
      cut43On = !cut43On;
      setCut43Pref(cut43On);
      paintFlags();
    });

    video.addEventListener("resize", layout);
    video.addEventListener("loadedmetadata", layout);
    if (typeof ResizeObserver === "function") {
      ro = new ResizeObserver(layout);
      ro.observe(container);
      ro.observe(video);
    }

    paintFlags();
    applyVisible();

    return {
      setVisible: function (on) {
        visible = !!on;
        applyVisible();
      },
      isVisible: function () { return visible; },
      layout: layout,
      destroy: function () {
        if (ro) {
          try { ro.disconnect(); } catch (e) { /* ignore */ }
          ro = null;
        }
        if (root.parentNode) root.parentNode.removeChild(root);
      },
    };
  }

  global.NexVueSafe = {
    PREF_ON,
    PREF_TARGET,
    PREF_CUT43,
    ACTION_INSET,
    TITLE_INSET,
    CUT43_WIDTH,
    getOnPref,
    setOnPref,
    getTargetPref,
    setTargetPref,
    getCut43Pref,
    setCut43Pref,
    videoContentRect,
    attach,
  };
})(typeof window !== "undefined" ? window : globalThis);
