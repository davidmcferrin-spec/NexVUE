/**
 * nexvue-spectrum.js — Player-only stereo RTA (confidence, not a QC RTA).
 *
 * 64 log-spaced bars 10 Hz–22 kHz per side (L over R), −60…0 dBFS
 * (0 = full scale). Taps NexVueVu.getSpectrumPair() — never opens a
 * second MediaStreamSource. Click the strip to pop a ~2× page-level
 * panel (escapes the videowrap). Esc or click again docks. Popped panel
 * is page-fixed, stacks above the Session metrics drawer, and can be
 * dragged; a short move does not dock. Per-browser prefs:
 *   nexvue-spectrum-on    1 | 0   (default off)
 *   nexvue-spectrum-pop   1 | 0   (default docked)
 *   nexvue-spectrum-pos   {"left":N,"top":N}  (viewport px; only after a drag)
 */
(function (global) {
  "use strict";

  const PREF_ON = "nexvue-spectrum-on";
  const PREF_POP = "nexvue-spectrum-pop";
  const PREF_POS = "nexvue-spectrum-pos";
  const BANDS = 64;
  const F_MIN = 10;
  const F_MAX = 22000;
  const DB_MIN = -60;
  const DB_MAX = 0;
  const FREQ_MARKS = [10, 100, 1000, 10000, 22000];
  const DB_MARKS = [0, -6, -12, -20, -30, -40, -60];
  const DRAG_THRESHOLD = 6;
  const POS_PAD = 8;

  function layoutFor(pop) {
    const w = pop ? 880 : 520;
    const plotL = pop ? 38 : 30;
    const plotR = pop ? 10 : 6;
    const plotT = pop ? 6 : 3;
    const plotB = pop ? 18 : 14;
    const plotH = pop ? 136 : 68;
    const font = pop ? 11 : 9;
    const plotW = w - plotL - plotR;
    const rowH = plotT + plotH + plotB;
    return {
      w: w,
      plotL: plotL,
      plotR: plotR,
      plotT: plotT,
      plotB: plotB,
      plotH: plotH,
      plotW: plotW,
      rowH: rowH,
      h: rowH * 2,
      font: font,
      barGap: pop ? 2 : 1,
    };
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

  function bandEdges(n, fMin, fMax) {
    const count = n == null ? BANDS : n;
    const lo = fMin == null ? F_MIN : fMin;
    const hi = fMax == null ? F_MAX : fMax;
    const edges = new Array(count + 1);
    const logMin = Math.log(lo);
    const logMax = Math.log(hi);
    for (let i = 0; i <= count; i++) {
      edges[i] = Math.exp(logMin + (i / count) * (logMax - logMin));
    }
    return edges;
  }

  function heightFromDb(db) {
    if (!Number.isFinite(db)) return 0;
    const n = (db - DB_MIN) / (DB_MAX - DB_MIN);
    return Math.max(0, Math.min(100, n * 100));
  }

  function dbScaleLabel(db) {
    return db === 0 ? "FS" : String(db);
  }

  function fillBands(freqDb, sampleRate, fftSize, out, edges) {
    const n = out.length;
    const nBins = freqDb && freqDb.length ? freqDb.length : 0;
    const hzPerBin = (sampleRate || 0) / (fftSize || 1);
    if (!nBins || !hzPerBin || !edges || edges.length < n + 1) {
      for (let b = 0; b < n; b++) out[b] = DB_MIN;
      return out;
    }
    for (let b = 0; b < n; b++) {
      let i0 = Math.max(1, Math.floor(edges[b] / hzPerBin));
      let i1 = Math.min(nBins - 1, Math.ceil(edges[b + 1] / hzPerBin) - 1);
      if (i1 < i0) i1 = i0;
      let peak = -Infinity;
      for (let i = i0; i <= i1; i++) {
        const v = freqDb[i];
        if (v > peak) peak = v;
      }
      out[b] = peak === -Infinity ? DB_MIN : peak;
    }
    return out;
  }

  function freqLabel(hz) {
    if (hz >= 1000) {
      const k = hz / 1000;
      return (Number.isInteger(k) ? String(k) : k.toFixed(1)) + "k";
    }
    return String(hz);
  }

  function ensureStyles() {
    if (document.getElementById("nexvue-spectrum-css")) return;
    const s = document.createElement("style");
    s.id = "nexvue-spectrum-css";
    s.textContent = `
.nexvue-spectrum {
  position: absolute; left: 50%; bottom: 8px; z-index: 3;
  transform: translateX(-50%);
  width: min(520px, calc(100% - 16px));
  pointer-events: auto; cursor: pointer;
  font: 9px/1.2 ui-monospace, "Cascadia Mono", Consolas, monospace;
  color: #d6dde6;
  user-select: none;
}
.nexvue-spectrum[hidden] { display: none !important; }
.nexvue-spectrum.nexvue-spectrum-pop {
  position: fixed; left: 12px; bottom: 12px; z-index: 55;
  transform: none; width: auto;
  cursor: grab;
  touch-action: none;
}
.nexvue-spectrum.nexvue-spectrum-pop.nexvue-spectrum-drag {
  cursor: grabbing;
}
.nexvue-spectrum-pane {
  background: rgba(8, 12, 16, .78);
  border: 1px solid rgba(44, 53, 66, .9);
  border-radius: 3px;
  padding: 4px 5px 3px;
}
.nexvue-spectrum.nexvue-spectrum-pop .nexvue-spectrum-pane {
  padding: 5px 6px 4px;
}
.nexvue-spectrum-label {
  color: #98a6b5; letter-spacing: .04em; margin-bottom: 2px;
}
.nexvue-spectrum canvas { display: block; }
.nexvue-spectrum:not(.nexvue-spectrum-pop) canvas { width: 100%; height: auto; }
`;
    document.head.appendChild(s);
  }

  /**
   * @param {object} opts
   * @param {HTMLElement} opts.container
   * @param {object|null} opts.vu  NexVueVu.attach() handle
   */
  function attach(opts) {
    ensureStyles();
    const container = opts && opts.container;
    const vu = opts && opts.vu;
    if (!container) return null;

    const edges = bandEdges(BANDS, F_MIN, F_MAX);
    const leftBands = new Float32Array(BANDS);
    const rightBands = new Float32Array(BANDS);
    let freqL = null;
    let freqR = null;
    let L = layoutFor(false);

    const root = document.createElement("div");
    root.className = "nexvue-spectrum";
    root.hidden = true;
    root.setAttribute("role", "button");
    root.setAttribute("tabindex", "0");
    root.innerHTML =
      '<div class="nexvue-spectrum-pane">' +
      '<div class="nexvue-spectrum-label">RTA · L/R · −60…0 dBFS</div>' +
      "</div>";
    const canvas = document.createElement("canvas");
    canvas.width = L.w;
    canvas.height = L.h;
    canvas.style.width = L.w + "px";
    canvas.style.height = L.h + "px";
    root.querySelector(".nexvue-spectrum-pane").appendChild(canvas);
    container.appendChild(root);

    const ctx2d = canvas.getContext("2d", { alpha: false });
    let visible = opts.visible !== undefined ? !!opts.visible : getOnPref();
    let popped = getPopPref();
    let pos = getPosPref();
    let running = false;
    let handle = 0;
    let drag = null;
    let suppressClick = false;

    function barColor(db) {
      if (db >= -6) return "#e5484d";
      if (db >= -12) return "#f5a623";
      return "#4cc38a";
    }

    function dbMarkStyle(db) {
      if (db === 0) {
        return { stroke: "rgba(229,72,77,.85)", fill: "rgba(229,72,77,.95)", width: 1.5 };
      }
      if (db === -6 || db === -12 || db === -20) {
        return { stroke: "rgba(214,221,230,.4)", fill: "rgba(176,187,200,.85)", width: 1 };
      }
      return { stroke: "rgba(214,221,230,.22)", fill: "rgba(176,187,200,.65)", width: 1 };
    }

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
      const next = clampPos(pos.left, pos.top, w, h, window.innerWidth, window.innerHeight, POS_PAD);
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
      root.classList.toggle("nexvue-spectrum-pop", !!(popped && visible));
      root.classList.toggle("nexvue-spectrum-drag", !!(drag && drag.moved));
      root.title = popped ? "Drag to move · click to dock · Esc" : "Click to enlarge";
      root.setAttribute("aria-pressed", popped ? "true" : "false");
      root.setAttribute("aria-label", popped
        ? "Dock spectrum analyzer"
        : "Enlarge spectrum analyzer");
      applyPosition();
    }

    function applyLayout() {
      L = layoutFor(popped);
      if (canvas.width !== L.w) canvas.width = L.w;
      if (canvas.height !== L.h) canvas.height = L.h;
      canvas.style.width = L.w + "px";
      canvas.style.height = L.h + "px";
      applyHost();
    }

    function drawRow(y0, bands, chLabel) {
      const plotT = y0 + L.plotT;
      ctx2d.fillStyle = "#0b1014";
      ctx2d.fillRect(0, y0, L.w, L.rowH);
      ctx2d.font = L.font + "px ui-monospace, Cascadia Mono, Consolas, monospace";
      ctx2d.textBaseline = "middle";
      DB_MARKS.forEach(function (db) {
        const t = heightFromDb(db) / 100;
        const y = plotT + L.plotH - t * L.plotH;
        const st = dbMarkStyle(db);
        ctx2d.strokeStyle = st.stroke;
        ctx2d.lineWidth = st.width;
        ctx2d.beginPath();
        ctx2d.moveTo(L.plotL, y);
        ctx2d.lineTo(L.w - L.plotR, y);
        ctx2d.stroke();
        ctx2d.fillStyle = st.fill;
        ctx2d.fillText(dbScaleLabel(db), 2, y);
      });
      ctx2d.fillStyle = "rgba(176,187,200,.85)";
      ctx2d.fillText(chLabel, L.plotL + 3, plotT + L.font + 2);

      const slot = L.plotW / BANDS;
      const barW = Math.max(1, slot - L.barGap);
      for (let i = 0; i < BANDS; i++) {
        const db = bands[i];
        const h = (heightFromDb(db) / 100) * L.plotH;
        const x = L.plotL + i * slot;
        const y = plotT + L.plotH - h;
        ctx2d.fillStyle = barColor(db);
        if (h >= 1) ctx2d.fillRect(x, y, barW, h);
      }

      ctx2d.fillStyle = "rgba(176,187,200,.7)";
      ctx2d.textBaseline = "top";
      FREQ_MARKS.forEach(function (hz) {
        const t = (Math.log(hz) - Math.log(F_MIN)) / (Math.log(F_MAX) - Math.log(F_MIN));
        const x = L.plotL + t * L.plotW;
        ctx2d.fillText(freqLabel(hz), x - 6, plotT + L.plotH + 2);
      });
    }

    function paintEmpty() {
      for (let i = 0; i < BANDS; i++) {
        leftBands[i] = DB_MIN;
        rightBands[i] = DB_MIN;
      }
      drawRow(0, leftBands, "L");
      drawRow(L.rowH, rightBands, "R");
    }

    function sample() {
      const pair = vu && typeof vu.getSpectrumPair === "function"
        ? vu.getSpectrumPair()
        : null;
      if (!pair || !pair.left || !pair.right) {
        paintEmpty();
        return;
      }
      const n = pair.left.frequencyBinCount;
      if (!freqL || freqL.length !== n) freqL = new Float32Array(n);
      if (!freqR || freqR.length !== n) freqR = new Float32Array(n);
      pair.left.getFloatFrequencyData(freqL);
      pair.right.getFloatFrequencyData(freqR);
      fillBands(freqL, pair.sampleRate, pair.fftSize, leftBands, edges);
      fillBands(freqR, pair.sampleRate, pair.fftSize, rightBands, edges);
      drawRow(0, leftBands, "L");
      drawRow(L.rowH, rightBands, "R");
    }

    function tick() {
      if (!running) return;
      sample();
      handle = requestAnimationFrame(tick);
    }

    function stopLoop() {
      running = false;
      if (handle) cancelAnimationFrame(handle);
      handle = 0;
    }

    function startLoop() {
      if (running || !visible) return;
      running = true;
      handle = requestAnimationFrame(tick);
    }

    function applyVisible() {
      root.hidden = !visible;
      applyHost();
      if (visible) startLoop();
      else {
        stopLoop();
        paintEmpty();
      }
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
      root.classList.remove("nexvue-spectrum-drag");
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

  global.NexVueSpectrum = {
    PREF_ON,
    PREF_POP,
    PREF_POS,
    BANDS,
    F_MIN,
    F_MAX,
    DB_MIN,
    DB_MAX,
    DB_MARKS,
    DRAG_THRESHOLD,
    POS_PAD,
    layoutFor,
    bandEdges,
    fillBands,
    heightFromDb,
    dbScaleLabel,
    freqLabel,
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
