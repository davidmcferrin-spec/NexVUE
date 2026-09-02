/**
 * nexvue-spectrum.js — Player-only stereo RTA (confidence, not a QC RTA).
 *
 * 64 log-spaced bars 10 Hz–22 kHz per side (L over R), −60…0 dBFS
 * (0 = full scale). Taps NexVueVu.getSpectrumPair() — never opens a
 * second MediaStreamSource. Per-browser pref:
 *   nexvue-spectrum-on   1 | 0   (default off)
 */
(function (global) {
  "use strict";

  const PREF_ON = "nexvue-spectrum-on";
  const BANDS = 64;
  const F_MIN = 10;
  const F_MAX = 22000;
  const DB_MIN = -60;
  const DB_MAX = 0;
  const FREQ_MARKS = [10, 100, 1000, 10000, 22000];
  const DB_MARKS = [0, -30, -60];

  const W = 520;
  const PLOT_L = 28;
  const PLOT_R = 6;
  const PLOT_T = 3;
  const PLOT_B = 14;
  const PLOT_H = 68;
  const ROW_H = PLOT_T + PLOT_H + PLOT_B;
  const H = ROW_H * 2;
  const PLOT_W = W - PLOT_L - PLOT_R;
  const BAR_GAP = 1;

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
  pointer-events: none;
  font: 9px/1.2 ui-monospace, "Cascadia Mono", Consolas, monospace;
  color: #d6dde6;
  user-select: none;
}
.nexvue-spectrum[hidden] { display: none !important; }
.nexvue-spectrum-pane {
  background: rgba(8, 12, 16, .78);
  border: 1px solid rgba(44, 53, 66, .9);
  border-radius: 3px;
  padding: 4px 5px 3px;
}
.nexvue-spectrum-label {
  color: #98a6b5; letter-spacing: .04em; margin-bottom: 2px;
}
.nexvue-spectrum canvas { display: block; width: 100%; height: auto; }
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

    const root = document.createElement("div");
    root.className = "nexvue-spectrum";
    root.hidden = true;
    root.setAttribute("role", "img");
    root.setAttribute("aria-label", "Stereo spectrum analyzer, 10 hertz to 22 kilohertz, minus 60 to 0 dBFS");
    root.innerHTML =
      '<div class="nexvue-spectrum-pane">' +
      '<div class="nexvue-spectrum-label">RTA · L/R · −60…0 dBFS</div>' +
      "</div>";
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    root.querySelector(".nexvue-spectrum-pane").appendChild(canvas);
    container.appendChild(root);

    const ctx2d = canvas.getContext("2d", { alpha: false });
    let visible = opts.visible !== undefined ? !!opts.visible : getOnPref();
    let running = false;
    let handle = 0;

    function barColor(db) {
      if (db >= -6) return "#e5484d";
      if (db >= -12) return "#f5a623";
      return "#4cc38a";
    }

    function drawRow(y0, bands, chLabel) {
      const plotT = y0 + PLOT_T;
      ctx2d.fillStyle = "#0b1014";
      ctx2d.fillRect(0, y0, W, ROW_H);
      ctx2d.strokeStyle = "rgba(214,221,230,.22)";
      ctx2d.fillStyle = "rgba(176,187,200,.7)";
      ctx2d.lineWidth = 1;
      ctx2d.font = "9px ui-monospace, Cascadia Mono, Consolas, monospace";
      ctx2d.textBaseline = "middle";
      DB_MARKS.forEach(function (db) {
        const t = heightFromDb(db) / 100;
        const y = plotT + PLOT_H - t * PLOT_H;
        ctx2d.beginPath();
        ctx2d.moveTo(PLOT_L, y);
        ctx2d.lineTo(W - PLOT_R, y);
        ctx2d.stroke();
        ctx2d.fillText(db === 0 ? "0" : String(db), 2, y);
      });
      ctx2d.fillStyle = "rgba(176,187,200,.85)";
      ctx2d.fillText(chLabel, PLOT_L + 3, plotT + 8);

      const slot = PLOT_W / BANDS;
      const barW = Math.max(1, slot - BAR_GAP);
      for (let i = 0; i < BANDS; i++) {
        const db = bands[i];
        const h = (heightFromDb(db) / 100) * PLOT_H;
        const x = PLOT_L + i * slot;
        const y = plotT + PLOT_H - h;
        ctx2d.fillStyle = barColor(db);
        if (h >= 1) ctx2d.fillRect(x, y, barW, h);
      }

      ctx2d.fillStyle = "rgba(176,187,200,.7)";
      ctx2d.textBaseline = "top";
      FREQ_MARKS.forEach(function (hz) {
        const t = (Math.log(hz) - Math.log(F_MIN)) / (Math.log(F_MAX) - Math.log(F_MIN));
        const x = PLOT_L + t * PLOT_W;
        ctx2d.fillText(freqLabel(hz), x - 6, plotT + PLOT_H + 2);
      });
    }

    function paintEmpty() {
      for (let i = 0; i < BANDS; i++) {
        leftBands[i] = DB_MIN;
        rightBands[i] = DB_MIN;
      }
      drawRow(0, leftBands, "L");
      drawRow(ROW_H, rightBands, "R");
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
      drawRow(ROW_H, rightBands, "R");
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
      if (visible) startLoop();
      else {
        stopLoop();
        paintEmpty();
      }
    }

    paintEmpty();
    applyVisible();

    return {
      setVisible: function (on) {
        visible = !!on;
        applyVisible();
      },
      isVisible: function () { return visible; },
      destroy: function () {
        stopLoop();
        if (root.parentNode) root.parentNode.removeChild(root);
      },
    };
  }

  global.NexVueSpectrum = {
    PREF_ON,
    BANDS,
    F_MIN,
    F_MAX,
    DB_MIN,
    DB_MAX,
    bandEdges,
    fillBands,
    heightFromDb,
    freqLabel,
    getOnPref,
    setOnPref,
    attach,
  };
})(typeof window !== "undefined" ? window : globalThis);
