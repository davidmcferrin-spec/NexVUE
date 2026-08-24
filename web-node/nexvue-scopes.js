/**
 * nexvue-scopes.js — player-local waveform + vectorscope (confidence, not SDI QC).
 *
 * Samples the decoded <video> via requestVideoFrameCallback, Rec.709 Y′CbCr,
 * IRE = Y′×100 (post-browser YUV→RGB; treat as 0–100 IRE). 75% Rec.709 bar
 * boxes + skin-tone I-line on the vectorscope. Never burned into encode.
 *
 * Click the strip to pop a ~2× panel onto the page (escapes overflow-hidden
 * panes). Esc or click again docks. Popped panel is page-fixed, stacks above
 * the Session metrics drawer, and can be dragged; a short move does not dock.
 * Per-browser prefs:
 *   nexvue-scopes-on   1 | 0   (default off)
 *   nexvue-scopes-pop  1 | 0   (default docked)
 *   nexvue-scopes-pos  {"left":N,"top":N}  (viewport px; only after a drag)
 */
(function (global) {
  "use strict";

  const PREF_ON = "nexvue-scopes-on";
  const PREF_POP = "nexvue-scopes-pop";
  const PREF_POS = "nexvue-scopes-pos";
  const FADE = 0.22;
  const DRAG_THRESHOLD = 6;
  const POS_PAD = 8;

  function layoutFor(pop) {
    const wfmW = pop ? 440 : 220;
    const wfmH = pop ? 280 : 140;
    const plotL = pop ? 32 : 22;
    const plotR = pop ? 8 : 4;
    const plotT = pop ? 12 : 8;
    const plotB = pop ? 18 : 12;
    const plotW = wfmW - plotL - plotR;
    const plotH = wfmH - plotT - plotB;
    return {
      wfmW: wfmW,
      wfmH: wfmH,
      plotL: plotL,
      plotR: plotR,
      plotT: plotT,
      plotB: plotB,
      plotW: plotW,
      plotH: plotH,
      sampleW: plotW,
      sampleH: pop ? 140 : 90,
      vecSize: pop ? 280 : 140,
      font: pop ? 11 : 9,
      box: pop ? 5 : 4,
      vecInset: pop ? 16 : 10,
    };
  }

  const COMPACT = layoutFor(false);
  const SAMPLE_W = COMPACT.sampleW;
  const SAMPLE_H = COMPACT.sampleH;
  const PLOT_W = COMPACT.plotW;
  const PLOT_H = COMPACT.plotH;

  // Rec.709 (display RGB 0–1) → Y′ / Cb / Cr (Cb/Cr centered at 0, ±0.5).
  function rgbToYcbcr(r, g, b) {
    const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    const cb = -0.114572 * r - 0.385428 * g + 0.5 * b;
    const cr = 0.5 * r - 0.454153 * g - 0.045847 * b;
    return { y: y, cb: cb, cr: cr };
  }

  function yToIre(y) {
    return y * 100;
  }

  // 75% Rec.709 color bars (on=0.75, off=0) for vectorscope targets.
  const BAR75 = [
    { name: "R", r: 0.75, g: 0, b: 0, color: "#e5484d" },
    { name: "Mg", r: 0.75, g: 0, b: 0.75, color: "#d46ad6" },
    { name: "B", r: 0, g: 0, b: 0.75, color: "#5aa6ff" },
    { name: "Cy", r: 0, g: 0.75, b: 0.75, color: "#3dd6d0" },
    { name: "G", r: 0, g: 0.75, b: 0, color: "#4cc38a" },
    { name: "Yl", r: 0.75, g: 0.75, b: 0, color: "#e6d04a" },
  ];

  function barTargets() {
    return BAR75.map(function (bar) {
      const ycc = rgbToYcbcr(bar.r, bar.g, bar.b);
      return { name: bar.name, color: bar.color, cb: ycc.cb, cr: ycc.cr };
    });
  }

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

  function parsePos(raw) {
    if (raw == null || raw === "") return null;
    let o = raw;
    if (typeof raw === "string") {
      try { o = JSON.parse(raw); } catch (e) { return null; }
    }
    if (!o || typeof o !== "object") return null;
    const left = Number(o.left);
    const top = Number(o.top);
    if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
    return { left: left, top: top };
  }

  function clampPos(left, top, width, height, vw, vh, pad) {
    pad = pad == null ? POS_PAD : pad;
    width = Math.max(0, Number(width) || 0);
    height = Math.max(0, Number(height) || 0);
    vw = Math.max(0, Number(vw) || 0);
    vh = Math.max(0, Number(vh) || 0);
    const maxL = Math.max(pad, vw - width - pad);
    const maxT = Math.max(pad, vh - height - pad);
    return {
      left: Math.min(maxL, Math.max(pad, left)),
      top: Math.min(maxT, Math.max(pad, top)),
    };
  }

  function getOnPref() { return prefOn(PREF_ON, false); }
  function setOnPref(on) { setPref(PREF_ON, on); }
  function getPopPref() { return prefOn(PREF_POP, false); }
  function setPopPref(on) { setPref(PREF_POP, on); }
  function getPosPref() {
    try {
      return parsePos(localStorage.getItem(PREF_POS));
    } catch (e) {
      return null;
    }
  }
  function setPosPref(pos) {
    const next = parsePos(pos);
    try {
      if (!next) localStorage.removeItem(PREF_POS);
      else localStorage.setItem(PREF_POS, JSON.stringify(next));
    } catch (e) { /* private mode */ }
    return next;
  }

  function ensureStyles() {
    if (document.getElementById("nexvue-scopes-css")) return;
    const s = document.createElement("style");
    s.id = "nexvue-scopes-css";
    s.textContent = `
.nexvue-scopes {
  position: absolute; left: 8px; bottom: 8px; z-index: 3;
  display: flex; flex-direction: row; align-items: flex-end; gap: 6px;
  pointer-events: auto; cursor: pointer;
  font: 9px/1.2 ui-monospace, "Cascadia Mono", Consolas, monospace;
  color: #d6dde6;
  user-select: none;
}
.nexvue-scopes[hidden] { display: none !important; }
.nexvue-scopes.nexvue-scopes-pop {
  position: fixed; left: 12px; bottom: 12px; z-index: 55;
  gap: 8px;
  font-size: 11px;
  cursor: grab;
  touch-action: none;
}
.nexvue-scopes.nexvue-scopes-pop.nexvue-scopes-drag {
  cursor: grabbing;
}
.nexvue-scopes-pane {
  background: rgba(8, 12, 16, .78);
  border: 1px solid rgba(44, 53, 66, .9);
  border-radius: 3px;
  padding: 3px 4px 2px;
}
.nexvue-scopes.nexvue-scopes-pop .nexvue-scopes-pane {
  padding: 5px 6px 4px;
}
.nexvue-scopes-label {
  color: #98a6b5; letter-spacing: .04em; margin-bottom: 2px;
}
.nexvue-scopes canvas { display: block; }
`;
    document.head.appendChild(s);
  }

  function makeCanvas(w, h) {
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    c.style.width = w + "px";
    c.style.height = h + "px";
    return c;
  }

  function resizeCanvas(c, w, h) {
    if (c.width !== w) c.width = w;
    if (c.height !== h) c.height = h;
    c.style.width = w + "px";
    c.style.height = h + "px";
  }

  function drawWfmGraticule(ctx, L) {
    ctx.save();
    ctx.strokeStyle = "rgba(214,221,230,.28)";
    ctx.fillStyle = "rgba(176,187,200,.7)";
    ctx.lineWidth = 1;
    ctx.font = L.font + "px ui-monospace, Cascadia Mono, Consolas, monospace";
    [0, 50, 100].forEach(function (ire) {
      const y = L.plotT + L.plotH - (ire / 100) * L.plotH;
      ctx.beginPath();
      ctx.moveTo(L.plotL, y);
      ctx.lineTo(L.wfmW - L.plotR, y);
      ctx.stroke();
      ctx.fillText(String(ire), 2, y + 3);
    });
    ctx.fillText("IRE", 2, L.font + 2);
    ctx.restore();
  }

  function drawVecGraticule(ctx, targets, L) {
    const cx = L.vecSize / 2;
    const cy = L.vecSize / 2;
    const r = L.vecSize / 2 - L.vecInset;
    ctx.save();
    ctx.strokeStyle = "rgba(214,221,230,.28)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx - r, cy);
    ctx.lineTo(cx + r, cy);
    ctx.moveTo(cx, cy - r);
    ctx.lineTo(cx, cy + r);
    ctx.stroke();
    // Skin-tone I-line (~123° from +Cb toward +Cr, Tek-style).
    const ang = (123 * Math.PI) / 180;
    ctx.strokeStyle = "rgba(232, 168, 110, .55)";
    ctx.beginPath();
    ctx.moveTo(cx - Math.cos(ang) * r, cy + Math.sin(ang) * r);
    ctx.lineTo(cx + Math.cos(ang) * r, cy - Math.sin(ang) * r);
    ctx.stroke();
    const box = L.box;
    targets.forEach(function (t) {
      const x = cx + t.cb * 2 * r;
      const y = cy - t.cr * 2 * r;
      ctx.strokeStyle = t.color;
      ctx.strokeRect(x - box, y - box, box * 2, box * 2);
    });
    ctx.restore();
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

    const targets = barTargets();
    let L = layoutFor(false);
    const root = document.createElement("div");
    root.className = "nexvue-scopes";
    root.hidden = true;
    root.setAttribute("role", "button");
    root.setAttribute("tabindex", "0");
    root.innerHTML =
      '<div class="nexvue-scopes-pane">' +
      '<div class="nexvue-scopes-label">WFM · IRE</div>' +
      "</div>" +
      '<div class="nexvue-scopes-pane">' +
      '<div class="nexvue-scopes-label">VEC · 75%</div>' +
      "</div>";
    const panes = root.querySelectorAll(".nexvue-scopes-pane");
    const wfm = makeCanvas(L.wfmW, L.wfmH);
    const vec = makeCanvas(L.vecSize, L.vecSize);
    panes[0].appendChild(wfm);
    panes[1].appendChild(vec);
    container.appendChild(root);

    const wfmCtx = wfm.getContext("2d", { alpha: false });
    const vecCtx = vec.getContext("2d", { alpha: false });
    const wfmTrace = document.createElement("canvas");
    const wfmTraceCtx = wfmTrace.getContext("2d", { alpha: false });
    const vecTrace = document.createElement("canvas");
    const vecTraceCtx = vecTrace.getContext("2d", { alpha: false });
    const sample = document.createElement("canvas");
    const sampleCtx = sample.getContext("2d", { willReadFrequently: true, alpha: false });

    let visible = opts.visible !== undefined ? !!opts.visible : getOnPref();
    let popped = getPopPref();
    let pos = getPosPref();
    let running = false;
    let handle = 0;
    let usingRaf = false;
    let drag = null;
    let suppressClick = false;

    function dockHost() {
      return document.body || container;
    }

    function applyPosition() {
      if (!(popped && visible) || !pos) {
        root.style.left = "";
        root.style.top = "";
        root.style.bottom = "";
        return;
      }
      const w = root.offsetWidth;
      const h = root.offsetHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const next = clampPos(pos.left, pos.top, w, h, vw, vh, POS_PAD);
      if (w > 0 && h > 0 && (next.left !== pos.left || next.top !== pos.top)) {
        pos = next;
        setPosPref(pos);
      }
      root.style.left = next.left + "px";
      root.style.top = next.top + "px";
      root.style.bottom = "auto";
    }

    function applyHost() {
      const host = popped && visible ? dockHost() : container;
      if (host && root.parentNode !== host) host.appendChild(root);
      root.classList.toggle("nexvue-scopes-pop", !!(popped && visible));
      root.classList.toggle("nexvue-scopes-drag", !!(drag && drag.moved));
      root.title = popped ? "Drag to move · click to dock · Esc" : "Click to enlarge";
      root.setAttribute("aria-pressed", popped ? "true" : "false");
      root.setAttribute("aria-label", popped ? "Dock waveform and vectorscope" : "Enlarge waveform and vectorscope");
      applyPosition();
    }

    function applyLayout() {
      L = layoutFor(popped);
      resizeCanvas(wfm, L.wfmW, L.wfmH);
      resizeCanvas(vec, L.vecSize, L.vecSize);
      resizeCanvas(wfmTrace, L.wfmW, L.wfmH);
      resizeCanvas(vecTrace, L.vecSize, L.vecSize);
      resizeCanvas(sample, L.sampleW, L.sampleH);
      applyHost();
      clearScopes();
    }

    function clearTraces() {
      wfmTraceCtx.fillStyle = "#0b1014";
      wfmTraceCtx.fillRect(0, 0, L.wfmW, L.wfmH);
      vecTraceCtx.fillStyle = "#0b1014";
      vecTraceCtx.fillRect(0, 0, L.vecSize, L.vecSize);
    }

    function paintDisplay() {
      wfmCtx.fillStyle = "#0b1014";
      wfmCtx.fillRect(0, 0, L.wfmW, L.wfmH);
      wfmCtx.drawImage(wfmTrace, 0, 0);
      drawWfmGraticule(wfmCtx, L);
      vecCtx.fillStyle = "#0b1014";
      vecCtx.fillRect(0, 0, L.vecSize, L.vecSize);
      vecCtx.drawImage(vecTrace, 0, 0);
      drawVecGraticule(vecCtx, targets, L);
    }

    function clearScopes() {
      clearTraces();
      paintDisplay();
    }

    function sampleFrame() {
      if (!visible || video.readyState < 2 || !video.videoWidth) return;
      try {
        sampleCtx.drawImage(video, 0, 0, L.sampleW, L.sampleH);
      } catch (e) {
        return;
      }
      let data;
      try {
        data = sampleCtx.getImageData(0, 0, L.sampleW, L.sampleH).data;
      } catch (e) {
        return;
      }

      wfmTraceCtx.fillStyle = "rgba(11,16,20," + FADE + ")";
      wfmTraceCtx.fillRect(0, 0, L.wfmW, L.wfmH);
      vecTraceCtx.fillStyle = "rgba(11,16,20," + FADE + ")";
      vecTraceCtx.fillRect(0, 0, L.vecSize, L.vecSize);

      const vcx = L.vecSize / 2;
      const vcy = L.vecSize / 2;
      const vr = L.vecSize / 2 - L.vecInset;

      const wfmImg = wfmTraceCtx.getImageData(0, 0, L.wfmW, L.wfmH);
      const wfmD = wfmImg.data;
      const vecImg = vecTraceCtx.getImageData(0, 0, L.vecSize, L.vecSize);
      const vecD = vecImg.data;

      for (let i = 0; i < data.length; i += 4) {
        const r = data[i] / 255;
        const g = data[i + 1] / 255;
        const b = data[i + 2] / 255;
        const ycc = rgbToYcbcr(r, g, b);
        const px = (i / 4) % L.sampleW;
        const x = L.plotL + px;
        const ire = Math.max(0, Math.min(100, yToIre(ycc.y)));
        const y = L.plotT + L.plotH - 1 - Math.floor((ire / 100) * (L.plotH - 1));
        if (x >= L.plotL && x < L.plotL + L.plotW && y >= L.plotT && y < L.plotT + L.plotH) {
          const o = (y * L.wfmW + x) * 4;
          wfmD[o] = Math.min(255, wfmD[o] + 50);
          wfmD[o + 1] = Math.min(255, wfmD[o + 1] + 160);
          wfmD[o + 2] = Math.min(255, wfmD[o + 2] + 95);
          wfmD[o + 3] = 255;
        }
        const vx = Math.round(vcx + ycc.cb * 2 * vr);
        const vy = Math.round(vcy - ycc.cr * 2 * vr);
        if (vx >= 0 && vx < L.vecSize && vy >= 0 && vy < L.vecSize) {
          const o = (vy * L.vecSize + vx) * 4;
          vecD[o] = Math.min(255, vecD[o] + data[i] * 0.55 + 40);
          vecD[o + 1] = Math.min(255, vecD[o + 1] + data[i + 1] * 0.55 + 40);
          vecD[o + 2] = Math.min(255, vecD[o + 2] + data[i + 2] * 0.55 + 40);
          vecD[o + 3] = 255;
        }
      }
      wfmTraceCtx.putImageData(wfmImg, 0, 0);
      vecTraceCtx.putImageData(vecImg, 0, 0);
      paintDisplay();
    }

    function stopLoop() {
      running = false;
      if (handle && usingRaf === false && video.cancelVideoFrameCallback) {
        try { video.cancelVideoFrameCallback(handle); } catch (e) { /* ignore */ }
      } else if (handle) {
        cancelAnimationFrame(handle);
      }
      handle = 0;
    }

    function tickRaf() {
      if (!running) return;
      sampleFrame();
      handle = requestAnimationFrame(tickRaf);
    }

    function tickVfc() {
      if (!running) return;
      sampleFrame();
      handle = video.requestVideoFrameCallback(tickVfc);
    }

    function startLoop() {
      if (running || !visible) return;
      running = true;
      clearScopes();
      if (typeof video.requestVideoFrameCallback === "function") {
        usingRaf = false;
        handle = video.requestVideoFrameCallback(tickVfc);
      } else {
        usingRaf = true;
        handle = requestAnimationFrame(tickRaf);
      }
    }

    function applyVisible() {
      root.hidden = !visible;
      applyHost();
      if (visible) startLoop();
      else stopLoop();
    }

    function setPopped(on) {
      on = !!on;
      if (popped === on) {
        applyHost();
        return;
      }
      popped = on;
      setPopPref(popped);
      applyLayout();
    }

    function togglePopped() {
      setPopped(!popped);
    }

    function onPointer(ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }

    function onPointerDown(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (!popped || !visible) return;
      if (ev.button != null && ev.button !== 0) return;
      const rect = root.getBoundingClientRect();
      drag = {
        pointerId: ev.pointerId,
        startX: ev.clientX,
        startY: ev.clientY,
        origL: rect.left,
        origT: rect.top,
        moved: false,
      };
      try { root.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
    }

    function onPointerMove(ev) {
      if (!drag || ev.pointerId !== drag.pointerId) return;
      const dx = ev.clientX - drag.startX;
      const dy = ev.clientY - drag.startY;
      if (!drag.moved && (dx * dx + dy * dy) < DRAG_THRESHOLD * DRAG_THRESHOLD) return;
      ev.preventDefault();
      drag.moved = true;
      pos = clampPos(
        drag.origL + dx,
        drag.origT + dy,
        root.offsetWidth,
        root.offsetHeight,
        window.innerWidth,
        window.innerHeight,
        POS_PAD
      );
      applyHost();
    }

    function endDrag(ev) {
      if (!drag || (ev && ev.pointerId != null && ev.pointerId !== drag.pointerId)) return;
      const moved = drag.moved;
      const pointerId = drag.pointerId;
      drag = null;
      root.classList.remove("nexvue-scopes-drag");
      try { root.releasePointerCapture(pointerId); } catch (e) { /* ignore */ }
      if (moved) {
        pos = setPosPref(pos) || pos;
        suppressClick = true;
      }
    }

    function onClick(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      togglePopped();
    }

    function onResize() {
      if (!popped || !visible || !pos) return;
      applyPosition();
    }

    function onKey(ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        if (ev.target !== root) return;
        ev.preventDefault();
        ev.stopPropagation();
        togglePopped();
        return;
      }
      if (ev.key !== "Escape") return;
      if (!popped || !visible) return;
      if (document.querySelector("dialog[open]")) return;
      ev.preventDefault();
      ev.stopPropagation();
      if (typeof ev.stopImmediatePropagation === "function") ev.stopImmediatePropagation();
      setPopped(false);
    }

    root.addEventListener("pointerdown", onPointerDown);
    root.addEventListener("pointermove", onPointerMove);
    root.addEventListener("pointerup", endDrag);
    root.addEventListener("pointercancel", endDrag);
    root.addEventListener("click", onClick);
    root.addEventListener("dblclick", onPointer);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onResize);

    applyLayout();
    applyVisible();

    return {
      setVisible: function (on) {
        visible = !!on;
        applyVisible();
      },
      isVisible: function () { return visible; },
      setPopped: setPopped,
      isPopped: function () { return popped; },
      destroy: function () {
        stopLoop();
        document.removeEventListener("keydown", onKey, true);
        window.removeEventListener("resize", onResize);
        if (root.parentNode) root.parentNode.removeChild(root);
      },
    };
  }

  global.NexVueScopes = {
    PREF_ON,
    PREF_POP,
    PREF_POS,
    DRAG_THRESHOLD,
    POS_PAD,
    SAMPLE_W,
    SAMPLE_H,
    PLOT_W,
    PLOT_H,
    BAR75,
    layoutFor,
    rgbToYcbcr,
    yToIre,
    barTargets,
    parsePos,
    clampPos,
    getOnPref,
    setOnPref,
    getPopPref,
    setPopPref,
    getPosPref,
    setPosPref,
    attach,
  };
})(typeof window !== "undefined" ? window : globalThis);
