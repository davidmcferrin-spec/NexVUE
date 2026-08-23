/**
 * nexvue-scopes.js — player-local waveform + vectorscope (confidence, not SDI QC).
 *
 * Samples the decoded <video> via requestVideoFrameCallback, Rec.709 Y′CbCr,
 * IRE = Y′×100 (post-browser YUV→RGB; treat as 0–100 IRE). 75% Rec.709 bar
 * boxes + skin-tone I-line on the vectorscope. Never burned into encode.
 *
 * Per-browser prefs:
 *   nexvue-scopes-on  1 | 0   (default off)
 */
(function (global) {
  "use strict";

  const PREF_ON = "nexvue-scopes-on";
  const WFM_W = 220;
  const WFM_H = 140;
  const PLOT_L = 22;
  const PLOT_R = 4;
  const PLOT_T = 8;
  const PLOT_B = 12;
  const PLOT_W = WFM_W - PLOT_L - PLOT_R;
  const PLOT_H = WFM_H - PLOT_T - PLOT_B;
  // 1:1 with the WFM plot so every X column gets samples (no barcode gaps).
  const SAMPLE_W = PLOT_W;
  const SAMPLE_H = 90;
  const VEC_SIZE = 140;
  const FADE = 0.22;

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

  function getOnPref() { return prefOn(PREF_ON, false); }
  function setOnPref(on) { setPref(PREF_ON, on); }

  function ensureStyles() {
    if (document.getElementById("nexvue-scopes-css")) return;
    const s = document.createElement("style");
    s.id = "nexvue-scopes-css";
    s.textContent = `
.nexvue-scopes {
  position: absolute; left: 8px; bottom: 8px; z-index: 3;
  display: flex; flex-direction: row; align-items: flex-end; gap: 6px;
  pointer-events: none;
  font: 9px/1.2 ui-monospace, "Cascadia Mono", Consolas, monospace;
  color: #d6dde6;
}
.nexvue-scopes[hidden] { display: none !important; }
.nexvue-scopes-pane {
  background: rgba(8, 12, 16, .78);
  border: 1px solid rgba(44, 53, 66, .9);
  border-radius: 3px;
  padding: 3px 4px 2px;
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

  function drawWfmGraticule(ctx) {
    ctx.save();
    ctx.strokeStyle = "rgba(214,221,230,.28)";
    ctx.fillStyle = "rgba(176,187,200,.7)";
    ctx.lineWidth = 1;
    ctx.font = "9px ui-monospace, Cascadia Mono, Consolas, monospace";
    [0, 50, 100].forEach(function (ire) {
      const y = PLOT_T + PLOT_H - (ire / 100) * PLOT_H;
      ctx.beginPath();
      ctx.moveTo(PLOT_L, y);
      ctx.lineTo(WFM_W - PLOT_R, y);
      ctx.stroke();
      ctx.fillText(String(ire), 2, y + 3);
    });
    ctx.fillText("IRE", 2, 10);
    ctx.restore();
  }

  function drawVecGraticule(ctx, targets) {
    const cx = VEC_SIZE / 2;
    const cy = VEC_SIZE / 2;
    const r = VEC_SIZE / 2 - 10;
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
    targets.forEach(function (t) {
      const x = cx + t.cb * 2 * r;
      const y = cy - t.cr * 2 * r;
      ctx.strokeStyle = t.color;
      ctx.strokeRect(x - 4, y - 4, 8, 8);
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
    const root = document.createElement("div");
    root.className = "nexvue-scopes";
    root.hidden = true;
    root.innerHTML =
      '<div class="nexvue-scopes-pane">' +
      '<div class="nexvue-scopes-label">WFM · IRE</div>' +
      "</div>" +
      '<div class="nexvue-scopes-pane">' +
      '<div class="nexvue-scopes-label">VEC · 75%</div>' +
      "</div>";
    const panes = root.querySelectorAll(".nexvue-scopes-pane");
    const wfm = makeCanvas(WFM_W, WFM_H);
    const vec = makeCanvas(VEC_SIZE, VEC_SIZE);
    panes[0].appendChild(wfm);
    panes[1].appendChild(vec);
    container.appendChild(root);

    const wfmCtx = wfm.getContext("2d", { alpha: false });
    const vecCtx = vec.getContext("2d", { alpha: false });
    const wfmTrace = document.createElement("canvas");
    wfmTrace.width = WFM_W;
    wfmTrace.height = WFM_H;
    const wfmTraceCtx = wfmTrace.getContext("2d", { alpha: false });
    const vecTrace = document.createElement("canvas");
    vecTrace.width = VEC_SIZE;
    vecTrace.height = VEC_SIZE;
    const vecTraceCtx = vecTrace.getContext("2d", { alpha: false });
    const sample = document.createElement("canvas");
    sample.width = SAMPLE_W;
    sample.height = SAMPLE_H;
    const sampleCtx = sample.getContext("2d", { willReadFrequently: true, alpha: false });

    let visible = opts.visible !== undefined ? !!opts.visible : getOnPref();
    let running = false;
    let handle = 0;
    let usingRaf = false;

    function clearTraces() {
      wfmTraceCtx.fillStyle = "#0b1014";
      wfmTraceCtx.fillRect(0, 0, WFM_W, WFM_H);
      vecTraceCtx.fillStyle = "#0b1014";
      vecTraceCtx.fillRect(0, 0, VEC_SIZE, VEC_SIZE);
    }

    function paintDisplay() {
      wfmCtx.fillStyle = "#0b1014";
      wfmCtx.fillRect(0, 0, WFM_W, WFM_H);
      wfmCtx.drawImage(wfmTrace, 0, 0);
      drawWfmGraticule(wfmCtx);
      vecCtx.fillStyle = "#0b1014";
      vecCtx.fillRect(0, 0, VEC_SIZE, VEC_SIZE);
      vecCtx.drawImage(vecTrace, 0, 0);
      drawVecGraticule(vecCtx, targets);
    }

    function clearScopes() {
      clearTraces();
      paintDisplay();
    }

    function sampleFrame() {
      if (!visible || video.readyState < 2 || !video.videoWidth) return;
      try {
        sampleCtx.drawImage(video, 0, 0, SAMPLE_W, SAMPLE_H);
      } catch (e) {
        return;
      }
      let data;
      try {
        data = sampleCtx.getImageData(0, 0, SAMPLE_W, SAMPLE_H).data;
      } catch (e) {
        return;
      }

      wfmTraceCtx.fillStyle = "rgba(11,16,20," + FADE + ")";
      wfmTraceCtx.fillRect(0, 0, WFM_W, WFM_H);
      vecTraceCtx.fillStyle = "rgba(11,16,20," + FADE + ")";
      vecTraceCtx.fillRect(0, 0, VEC_SIZE, VEC_SIZE);

      const vcx = VEC_SIZE / 2;
      const vcy = VEC_SIZE / 2;
      const vr = VEC_SIZE / 2 - 10;

      const wfmImg = wfmTraceCtx.getImageData(0, 0, WFM_W, WFM_H);
      const wfmD = wfmImg.data;
      const vecImg = vecTraceCtx.getImageData(0, 0, VEC_SIZE, VEC_SIZE);
      const vecD = vecImg.data;

      for (let i = 0; i < data.length; i += 4) {
        const r = data[i] / 255;
        const g = data[i + 1] / 255;
        const b = data[i + 2] / 255;
        const ycc = rgbToYcbcr(r, g, b);
        const px = (i / 4) % SAMPLE_W;
        const x = PLOT_L + px;
        const ire = Math.max(0, Math.min(100, yToIre(ycc.y)));
        const y = PLOT_T + PLOT_H - 1 - Math.floor((ire / 100) * (PLOT_H - 1));
        if (x >= PLOT_L && x < PLOT_L + PLOT_W && y >= PLOT_T && y < PLOT_T + PLOT_H) {
          const o = (y * WFM_W + x) * 4;
          wfmD[o] = Math.min(255, wfmD[o] + 50);
          wfmD[o + 1] = Math.min(255, wfmD[o + 1] + 160);
          wfmD[o + 2] = Math.min(255, wfmD[o + 2] + 95);
          wfmD[o + 3] = 255;
        }
        const vx = Math.round(vcx + ycc.cb * 2 * vr);
        const vy = Math.round(vcy - ycc.cr * 2 * vr);
        if (vx >= 0 && vx < VEC_SIZE && vy >= 0 && vy < VEC_SIZE) {
          const o = (vy * VEC_SIZE + vx) * 4;
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
      if (visible) startLoop();
      else stopLoop();
    }

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

  global.NexVueScopes = {
    PREF_ON,
    SAMPLE_W,
    SAMPLE_H,
    PLOT_W,
    PLOT_H,
    BAR75,
    rgbToYcbcr,
    yToIre,
    barTargets,
    getOnPref,
    setOnPref,
    attach,
  };
})(typeof window !== "undefined" ? window : globalThis);
