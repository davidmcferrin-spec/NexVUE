/**
 * nexvue-vu.js — shared Web-Audio VU + program/playout for Player / Multiview.
 *
 * Transport is always 8ch discrete Opus (SDI embeds 1–8 → indices 0–7):
 *   L R C LFE Ls Rs SAPL SAPR
 * AUDIO_LAYOUT is a role preset for Main/SAP/5.1 routing only
 * (stereo hides 5.1 playout; no-SAP hides SAP).
 * AUDIO_EMBEDS (Settings checkboxes) gates which meters/listen channels
 * the UI shows — encode still publishes all eight.
 *
 * Per-browser prefs (localStorage) never change encode or other viewers:
 *   nexvue-vu-on          1 | 0               (show/hide meter overlay)
 *   nexvue-vu-scale       1 | 0               (show dBFS scale beside meters)
 *   nexvue-audio-muted    1 | 0               (Web Audio listen off)
 *   nexvue-audio-volume   0..1               (Web Audio master gain)
 *   nexvue-audio-program  main | sap
 *   nexvue-audio-playout  stereo | surround   (5.1 → stereo mixdown vs discrete)
 *   nexvue-vu-solo        -1 | channel index  (engineering solo)
 */
(function (global) {
  "use strict";

  const PREF_VISIBLE = "nexvue-vu-on";
  const PREF_SCALE = "nexvue-vu-scale";
  const PREF_MUTED = "nexvue-audio-muted";
  const PREF_VOLUME = "nexvue-audio-volume";
  const PREF_SOLO = "nexvue-vu-solo";
  const PREF_PROGRAM = "nexvue-audio-program";
  const PREF_PLAYOUT = "nexvue-audio-playout";
  const MAX_CH = 8;
  // dBFS marks for the optional scale (matches heightFromDb −60…0 → 0…100%).
  const SCALE_MARKS_DB = [0, -6, -12, -20, -30, -40, -60];

  const FFT = 2048;
  const SMOOTH = 0.3;
  const ATTACK = 0.35;
  const RELEASE = 0.06;
  // ITU-ish fold for 5.1 → stereo (C/Ls/Rs −3 dB, LFE −6 dB).
  const MIX_C = 0.707;
  const MIX_S = 0.707;
  const MIX_LFE = 0.5;

  // Fixed transport labels — encode always publishes 8ch in this order.
  const TRANSPORT_LABELS = ["L", "R", "C", "LFE", "Ls", "Rs", "SAPL", "SAPR"];

  // Role presets: how Main/SAP/5.1 buttons map onto the fixed 8ch transport.
  // channels is always 8 for metering; main/sap index into transport 0–7.
  const LAYOUTS = {
    stereo: {
      id: "stereo", channels: 8, has51: false, hasSap: false,
      labels: TRANSPORT_LABELS,
      main: [0, 1], sap: null,
    },
    "51": {
      id: "51", channels: 8, has51: true, hasSap: false,
      labels: TRANSPORT_LABELS,
      main: [0, 1, 2, 3, 4, 5], sap: null,
    },
    stereo_sap: {
      id: "stereo_sap", channels: 8, has51: false, hasSap: true,
      labels: TRANSPORT_LABELS,
      // Transport is always embeds 1–8; SAP rides on 7–8 (indices 6–7),
      // not a packed 4ch remix like the old encode path.
      main: [0, 1], sap: [6, 7],
    },
    "51_sap": {
      id: "51_sap", channels: 8, has51: true, hasSap: true,
      labels: TRANSPORT_LABELS,
      main: [0, 1, 2, 3, 4, 5], sap: [6, 7],
    },
  };

  function normalizeEmbeds(raw) {
    if (raw == null || raw === "") return [0, 1, 2, 3, 4, 5, 6, 7];
    if (Array.isArray(raw)) {
      const out = [];
      const seen = new Set();
      for (const v of raw) {
        let n = Number(v);
        if (!Number.isFinite(n)) continue;
        // Accept 1-based embeds or 0-based indices.
        if (n >= 1 && n <= 8) n = n - 1;
        if (n < 0 || n > 7 || seen.has(n)) continue;
        seen.add(n);
        out.push(n);
      }
      return out.length ? out.sort((a, b) => a - b) : [0, 1, 2, 3, 4, 5, 6, 7];
    }
    const s = String(raw).trim().toLowerCase();
    if (!s || s === "1-8" || s === "all" || s === "*") {
      return [0, 1, 2, 3, 4, 5, 6, 7];
    }
    return normalizeEmbeds(s.split(","));
  }

  let sharedCtx = null;

  function normalizeLayout(raw) {
    const s = String(raw || "stereo").toLowerCase().replace(/-/g, "_");
    if (s === "5.1" || s === "surround") return "51";
    if (s === "5.1_sap" || s === "surround_sap") return "51_sap";
    if (s === "sap") return "stereo_sap";
    if (LAYOUTS[s]) return s;
    // Legacy numeric AUDIO_CHANNELS
    const n = parseInt(s, 10);
    if (n === 2) return "stereo";
    if (n === 4) return "stereo_sap";
    if (n === 6 || n === 3 || n === 5) return "51";
    if (n === 8 || n === 16) return "51_sap";
    return "stereo";
  }

  function layoutInfo(raw) {
    return LAYOUTS[normalizeLayout(raw)] || LAYOUTS.stereo;
  }

  /** Default on — hide only when user explicitly turns VU off. */
  function getVisiblePref() {
    try {
      return localStorage.getItem(PREF_VISIBLE) !== "0";
    } catch {
      return true;
    }
  }

  function setVisiblePref(on) {
    try {
      localStorage.setItem(PREF_VISIBLE, on ? "1" : "0");
    } catch { /* private mode */ }
    return !!on;
  }

  /** Default off — scale is optional clutter for confidence monitors. */
  function getScalePref() {
    try {
      return localStorage.getItem(PREF_SCALE) === "1";
    } catch {
      return false;
    }
  }

  function setScalePref(on) {
    try {
      localStorage.setItem(PREF_SCALE, on ? "1" : "0");
    } catch { /* private mode */ }
    return !!on;
  }

  /** User mute — element stays muted; Web Audio master gain is the real mute. */
  function getMutedPref() {
    try {
      return localStorage.getItem(PREF_MUTED) === "1";
    } catch {
      return false;
    }
  }

  function setMutedPref(on) {
    try {
      localStorage.setItem(PREF_MUTED, on ? "1" : "0");
    } catch { /* private mode */ }
    return !!on;
  }

  /** Linear gain 0–1 (default 1). */
  function getVolumePref() {
    try {
      const raw = localStorage.getItem(PREF_VOLUME);
      if (raw === null || raw === "") return 1;
      const n = parseFloat(raw);
      if (!Number.isFinite(n)) return 1;
      return Math.max(0, Math.min(1, n));
    } catch {
      return 1;
    }
  }

  function setVolumePref(v) {
    const n = Math.max(0, Math.min(1, Number(v)));
    const out = Number.isFinite(n) ? n : 1;
    try {
      localStorage.setItem(PREF_VOLUME, String(out));
    } catch { /* private mode */ }
    return out;
  }

  function getProgramPref() {
    try {
      return localStorage.getItem(PREF_PROGRAM) === "sap" ? "sap" : "main";
    } catch {
      return "main";
    }
  }

  function setProgramPref(prog) {
    const v = prog === "sap" ? "sap" : "main";
    try { localStorage.setItem(PREF_PROGRAM, v); } catch { /* private mode */ }
    return v;
  }

  function getPlayoutPref() {
    try {
      return localStorage.getItem(PREF_PLAYOUT) === "surround" ? "surround" : "stereo";
    } catch {
      return "stereo";
    }
  }

  function setPlayoutPref(mode) {
    const v = mode === "surround" ? "surround" : "stereo";
    try { localStorage.setItem(PREF_PLAYOUT, v); } catch { /* private mode */ }
    return v;
  }

  function getSoloPref() {
    try {
      const raw = localStorage.getItem(PREF_SOLO);
      if (raw === null || raw === "" || raw === "-1") return -1;
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n) || n < 0) return -1;
      return Math.min(MAX_CH - 1, n);
    } catch {
      return -1;
    }
  }

  function setSoloPref(ch) {
    const v = ch === null || ch === undefined || ch < 0 ? -1 : Math.min(MAX_CH - 1, ch | 0);
    try { localStorage.setItem(PREF_SOLO, String(v)); } catch { /* private mode */ }
    return v;
  }

  function ensureCtx() {
    if (sharedCtx) return sharedCtx;
    const AC = global.AudioContext || global.webkitAudioContext;
    if (!AC) return null;
    sharedCtx = new AC();
    return sharedCtx;
  }

  async function resume() {
    const ctx = ensureCtx();
    if (ctx && ctx.state === "suspended") {
      try { await ctx.resume(); } catch { /* autoplay policy */ }
    }
    return ctx;
  }

  function ensureStyles() {
    if (document.getElementById("nexvue-vu-css")) return;
    const s = document.createElement("style");
    s.id = "nexvue-vu-css";
    s.textContent = `
.nexvue-vu {
  position: absolute; top: 8px; bottom: 8px; right: 8px; z-index: 3;
  display: flex; flex-direction: column; align-items: stretch; gap: 4px;
  width: auto; max-width: 48%; pointer-events: auto;
  font: 10px/1.2 ui-monospace, "Cascadia Mono", Consolas, monospace;
  color: var(--text, #d6dde6);
}
.nexvue-vu[hidden] { display: none !important; }
.nexvue-vu-toolbar {
  display: flex; gap: 3px; justify-content: flex-end; flex-wrap: wrap;
}
.nexvue-vu-toolbar button {
  background: var(--badge-bg, rgba(20,24,29,.85)); color: var(--dim, #98a6b5);
  border: 1px solid var(--edge, #2c3542); border-radius: 3px;
  padding: 2px 6px; font: inherit; cursor: pointer; line-height: 1.2;
}
.nexvue-vu-toolbar button:hover { color: var(--text, #d6dde6); border-color: var(--acc, #56c4f5); }
.nexvue-vu-toolbar button.active {
  color: var(--on-acc, #08131a); background: var(--acc, #56c4f5);
  border-color: var(--acc, #56c4f5); font-weight: 600;
}
.nexvue-vu-toolbar button:disabled { opacity: .35; cursor: not-allowed; }
.nexvue-vu-toolbar button[hidden],
.nexvue-vu-ch[hidden] { display: none !important; }
.nexvue-vu-meter-row {
  flex: 1; min-height: 0; display: flex; flex-direction: row;
  align-items: stretch; gap: 3px; justify-content: flex-end;
  width: max-content; max-width: 100%; margin-left: auto;
}
.nexvue-vu-scale {
  position: relative; width: 28px; flex: 0 0 28px;
  /* Sit beside the meter tracks (labels are ~12px under the tracks). */
  margin-bottom: 12px; pointer-events: none;
  color: var(--dim, #98a6b5); font-size: 8px; line-height: 1;
}
.nexvue-vu-scale[hidden] { display: none !important; }
.nexvue-vu-scale-mark {
  position: absolute; left: 0; right: 2px; text-align: right;
  transform: translateY(50%);
  white-space: nowrap;
}
.nexvue-vu-scale-unit {
  position: absolute; left: 0; right: 2px; top: -10px;
  text-align: right; font-size: 7px; letter-spacing: .04em;
  color: var(--muted, #b0bbc8);
}
.nexvue-vu-bars {
  flex: 0 0 auto; min-height: 0; display: flex; flex-direction: row;
  align-items: stretch; gap: 3px; justify-content: flex-end;
}
.nexvue-vu.show-scale .nexvue-vu-track {
  background-image: linear-gradient(to top,
    transparent 0%, transparent calc(33.333% - 0.5px),
    rgba(255,255,255,.12) calc(33.333% - 0.5px), rgba(255,255,255,.12) calc(33.333% + 0.5px),
    transparent calc(33.333% + 0.5px), transparent calc(66.666% - 0.5px),
    rgba(255,255,255,.12) calc(66.666% - 0.5px), rgba(255,255,255,.12) calc(66.666% + 0.5px),
    transparent calc(66.666% + 0.5px), transparent 100%);
}
.nexvue-vu-ch {
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  min-width: 14px; flex: 0 0 auto; cursor: pointer;
  background: transparent; border: none; padding: 0; color: inherit; font: inherit;
}
.nexvue-vu-ch:focus-visible { outline: 2px solid var(--acc, #56c4f5); outline-offset: 1px; }
.nexvue-vu-track {
  flex: 1; width: 10px; min-height: 48px; position: relative;
  background: rgba(0,0,0,.55); border: 1px solid var(--edge, #2c3542);
  border-radius: 2px; overflow: hidden;
}
.nexvue-vu-ch.solo .nexvue-vu-track {
  border-color: var(--acc, #56c4f5); box-shadow: 0 0 0 1px var(--acc, #56c4f5);
}
.nexvue-vu-ch.dimmed { opacity: .45; }
.nexvue-vu-fill {
  position: absolute; left: 0; right: 0; bottom: 0; height: 0%;
  background: linear-gradient(to top,
    var(--ok, #4cc38a) 0%,
    var(--ok, #4cc38a) 55%,
    var(--warn, #f5a623) 75%,
    var(--bad, #e5484d) 92%);
  transition: height 50ms linear;
}
.nexvue-vu-peak {
  position: absolute; left: 0; right: 0; height: 2px;
  background: #fff; opacity: .9; pointer-events: none;
}
.nexvue-vu-label { font-size: 9px; color: var(--dim, #98a6b5); }
.nexvue-vu-ch.solo .nexvue-vu-label { color: var(--acc, #56c4f5); font-weight: 600; }
`;
    document.head.appendChild(s);
  }

  function dbFromPeak(peak) {
    if (peak <= 0.00001) return -60;
    return 20 * Math.log10(peak);
  }

  function heightFromDb(db) {
    const n = (db + 60) / 60;
    return Math.max(0, Math.min(100, n * 100));
  }

  /**
   * @param {object} opts
   * @param {HTMLElement} opts.container
   * @param {HTMLVideoElement} opts.video
   * @param {MediaStream|null} [opts.stream]
   * @param {boolean} [opts.listen=false]
   * @param {string} [opts.layout="stereo"]
   */
  function attach(opts) {
    ensureStyles();
    const container = opts && opts.container;
    const video = opts && opts.video;
    if (!container || !video) return null;
    const onListenChange = opts && typeof opts.onListenChange === "function"
      ? opts.onListenChange
      : null;

    const root = document.createElement("div");
    root.className = "nexvue-vu";
    root.hidden = true;
    root.innerHTML =
      '<div class="nexvue-vu-toolbar">' +
      '<button type="button" data-vu="main" title="Main program (this browser only)">Main</button>' +
      '<button type="button" data-vu="sap" title="SAP / embeds 7+8 (this browser only)">SAP</button>' +
      '<button type="button" data-vu="stereo" title="Play as stereo (5.1 folded down)">St</button>' +
      '<button type="button" data-vu="surround" title="Play discrete 5.1 to computer speakers">5.1</button>' +
      '<button type="button" data-vu="scale" title="Show dBFS scale (this browser only)">dB</button>' +
      '<button type="button" data-vu="all" title="Clear engineering solo">ALL</button>' +
      "</div>" +
      '<div class="nexvue-vu-meter-row">' +
      '<div class="nexvue-vu-scale" hidden aria-hidden="true"></div>' +
      '<div class="nexvue-vu-bars" role="group" aria-label="Audio level meters"></div>' +
      "</div>";
    container.appendChild(root);

    const barsEl = root.querySelector(".nexvue-vu-bars");
    const scaleEl = root.querySelector(".nexvue-vu-scale");
    const btnMain = root.querySelector('[data-vu="main"]');
    const btnSap = root.querySelector('[data-vu="sap"]');
    const btnStereo = root.querySelector('[data-vu="stereo"]');
    const btnSurround = root.querySelector('[data-vu="surround"]');
    const btnScale = root.querySelector('[data-vu="scale"]');
    const allBtn = root.querySelector('[data-vu="all"]');

    let layout = layoutInfo(opts.layout || "51_sap");
    let channelCount = 8;
    let embedsOn = new Set(normalizeEmbeds(opts.embeds));
    let listen = !!opts.listen;
    let visible = opts.visible !== undefined ? !!opts.visible : getVisiblePref();
    let scaleOn = opts.scale !== undefined ? !!opts.scale : getScalePref();
    let volume = opts.volume !== undefined ? Math.max(0, Math.min(1, Number(opts.volume))) : getVolumePref();
    if (!Number.isFinite(volume)) volume = 1;
    let program = getProgramPref();
    let playout = getPlayoutPref();
    let solo = getSoloPref();
    let hasAudio = false;

    let ctx = null;
    let source = null;
    let splitter = null;
    let masterGain = null;
    let analysers = [];
    let chGains = []; // per transport channel → feed mix or surround
    let outNodes = []; // nodes to disconnect on teardown (mergers, gains)
    let fills = [];
    let peaks = [];
    let chBtns = [];
    let raf = 0;
    let levels = [];
    let peakHold = [];
    let connectedStreamId = null;
    let timeData = null;

    function effectiveProgram() {
      if (program === "sap" && layout.hasSap && layout.sap &&
          layout.sap.every((i) => embedsOn.has(i))) {
        return "sap";
      }
      return "main";
    }

    function effectivePlayout() {
      if (playout === "surround" && layout.has51 && effectiveProgram() === "main") {
        return "surround";
      }
      return "stereo";
    }

    function updateRootVisibility() {
      root.hidden = !(visible && hasAudio);
      if (visible && hasAudio && analysers.length && !raf) {
        raf = requestAnimationFrame(tick);
      } else if (!visible && raf) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
    }

    function setVisible(on) {
      visible = setVisiblePref(!!on);
      updateRootVisibility();
      return visible;
    }

    function paintScale() {
      root.classList.toggle("show-scale", scaleOn);
      btnScale.classList.toggle("active", scaleOn);
      btnScale.setAttribute("aria-pressed", scaleOn ? "true" : "false");
      if (!scaleOn) {
        scaleEl.hidden = true;
        scaleEl.setAttribute("aria-hidden", "true");
        scaleEl.innerHTML = "";
        return;
      }
      scaleEl.hidden = false;
      scaleEl.setAttribute("aria-hidden", "false");
      let html = '<span class="nexvue-vu-scale-unit">dBFS</span>';
      for (const db of SCALE_MARKS_DB) {
        const bottom = heightFromDb(db);
        const label = db === 0 ? "0" : String(db);
        html += '<span class="nexvue-vu-scale-mark" style="bottom:' +
          bottom.toFixed(1) + '%">' + label + "</span>";
      }
      scaleEl.innerHTML = html;
    }

    function setScale(on) {
      scaleOn = setScalePref(!!on);
      paintScale();
      return scaleOn;
    }

    function paintToolbar() {
      const prog = effectiveProgram();
      const play = effectivePlayout();
      const sapEnabled = layout.hasSap && layout.sap &&
        layout.sap.every((i) => embedsOn.has(i));
      // Role buttons: hide what Settings layout does not offer (not merely disable).
      btnMain.hidden = false;
      btnMain.classList.toggle("active", prog === "main");
      btnSap.hidden = !layout.hasSap;
      btnSap.classList.toggle("active", prog === "sap");
      btnSap.disabled = !sapEnabled;
      btnStereo.hidden = !layout.has51;
      btnSurround.hidden = !layout.has51;
      btnStereo.classList.toggle("active", layout.has51 && play === "stereo");
      btnSurround.classList.toggle("active", layout.has51 && play === "surround");
      btnStereo.disabled = !layout.has51;
      btnSurround.disabled = !layout.has51;
      btnScale.classList.toggle("active", scaleOn);
      allBtn.classList.toggle("active", solo < 0);
      chBtns.forEach((btn, i) => {
        const embOn = embedsOn.has(i);
        const isSolo = solo === i;
        // Settings AUDIO_EMBEDS: only show offered embeds in the meter strip.
        btn.hidden = !embOn;
        btn.classList.toggle("solo", isSolo);
        btn.classList.toggle("dimmed", solo >= 0 && !isSolo);
        btn.disabled = !embOn;
        btn.setAttribute("aria-pressed", isSolo ? "true" : "false");
        btn.title = embOn
          ? ("Solo " + (TRANSPORT_LABELS[i] || (i + 1)) + " (this browser only)")
          : ("Embed " + (i + 1) + " disabled in Settings (AUDIO_EMBEDS)");
      });
    }

    function effectiveMasterGain() {
      return listen ? volume : 0;
    }

    function applyRouting() {
      // Settings-disabled embeds + solo mute at chGains (meters still live).
      chGains.forEach((g, i) => {
        if (!g) return;
        const embOn = embedsOn.has(i);
        g.gain.value = (embOn && (solo < 0 || solo === i)) ? 1 : 0;
      });
      if (masterGain) masterGain.gain.value = effectiveMasterGain();
      paintToolbar();
    }

    function setVolume(v, volOpts) {
      const persist = !(volOpts && volOpts.persist === false);
      volume = Math.max(0, Math.min(1, Number(v)));
      if (!Number.isFinite(volume)) volume = 1;
      if (persist) setVolumePref(volume);
      if (masterGain) masterGain.gain.value = effectiveMasterGain();
      return volume;
    }

    function setSolo(ch) {
      if (ch === solo && ch >= 0) solo = setSoloPref(-1);
      else if (ch === null || ch < 0) solo = setSoloPref(-1);
      else solo = setSoloPref(Math.min(channelCount - 1, ch | 0));
      applyRouting();
    }

    function setProgram(prog) {
      program = setProgramPref(prog);
      if (connectedStreamId && video.srcObject) buildGraph(video.srcObject);
      else paintToolbar();
    }

    function setPlayout(mode) {
      playout = setPlayoutPref(mode);
      if (connectedStreamId && video.srcObject) buildGraph(video.srcObject);
      else paintToolbar();
    }

    function rebuildMeterDom() {
      barsEl.innerHTML = "";
      fills = [];
      peaks = [];
      chBtns = [];
      const labels = layout.labels;
      for (let i = 0; i < channelCount; i++) {
        const lab = labels[i] || String(i + 1);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "nexvue-vu-ch";
        btn.title = "Solo " + lab + " (this browser only)";
        btn.setAttribute("aria-label", "Solo audio " + lab);
        btn.setAttribute("aria-pressed", "false");
        btn.innerHTML =
          '<span class="nexvue-vu-track">' +
          '<span class="nexvue-vu-fill"></span>' +
          '<span class="nexvue-vu-peak" style="bottom:0%"></span>' +
          "</span>" +
          '<span class="nexvue-vu-label">' + lab + "</span>";
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
          resume();
          setSolo(i);
        });
        barsEl.appendChild(btn);
        chBtns.push(btn);
        fills.push(btn.querySelector(".nexvue-vu-fill"));
        peaks.push(btn.querySelector(".nexvue-vu-peak"));
      }
      levels = new Array(channelCount).fill(0);
      peakHold = new Array(channelCount).fill(0);
      if (solo >= channelCount) solo = setSoloPref(-1);
      paintToolbar();
    }

    function teardownGraph() {
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      const nodes = [source, splitter, masterGain].concat(analysers, chGains, outNodes);
      nodes.forEach((n) => { try { if (n) n.disconnect(); } catch { /* ignore */ } });
      source = null;
      splitter = null;
      masterGain = null;
      analysers = [];
      chGains = [];
      outNodes = [];
      connectedStreamId = null;
    }

    function tick() {
      raf = 0;
      if (!analysers.length) return;
      for (let i = 0; i < analysers.length; i++) {
        const a = analysers[i];
        if (!timeData || timeData.length !== a.fftSize) {
          timeData = new Float32Array(a.fftSize);
        }
        a.getFloatTimeDomainData(timeData);
        let peak = 0;
        for (let j = 0; j < timeData.length; j++) {
          const v = Math.abs(timeData[j]);
          if (v > peak) peak = v;
        }
        const prev = levels[i] || 0;
        const next = peak > prev
          ? prev + (peak - prev) * ATTACK
          : prev + (peak - prev) * RELEASE;
        levels[i] = next;
        peakHold[i] = Math.max(peakHold[i] * 0.985, next);
        if (fills[i]) fills[i].style.height = heightFromDb(dbFromPeak(next)).toFixed(1) + "%";
        if (peaks[i]) peaks[i].style.bottom = heightFromDb(dbFromPeak(peakHold[i])).toFixed(1) + "%";
      }
      raf = requestAnimationFrame(tick);
    }

    function buildStereoPair(leftIdx, rightIdx) {
      const merger = ctx.createChannelMerger(2);
      outNodes.push(merger);
      analysers = [];
      chGains = [];
      for (let i = 0; i < channelCount; i++) {
        const analyser = ctx.createAnalyser();
        analyser.fftSize = FFT;
        analyser.smoothingTimeConstant = SMOOTH;
        const gain = ctx.createGain();
        gain.gain.value = 1;
        splitter.connect(analyser, i);
        splitter.connect(gain, i);
        if (i === leftIdx) gain.connect(merger, 0, 0);
        else if (i === rightIdx) gain.connect(merger, 0, 1);
        analysers.push(analyser);
        chGains.push(gain);
      }
      return merger;
    }

    function build51Mixdown() {
      // L' = L + 0.707*C + 0.707*Ls + 0.5*LFE
      // R' = R + 0.707*C + 0.707*Rs + 0.5*LFE
      const merger = ctx.createChannelMerger(2);
      outNodes.push(merger);
      analysers = [];
      chGains = [];
      const coeffsL = [1, 0, MIX_C, MIX_LFE, MIX_S, 0];
      const coeffsR = [0, 1, MIX_C, MIX_LFE, 0, MIX_S];
      for (let i = 0; i < channelCount; i++) {
        const analyser = ctx.createAnalyser();
        analyser.fftSize = FFT;
        analyser.smoothingTimeConstant = SMOOTH;
        const gain = ctx.createGain();
        gain.gain.value = 1;
        splitter.connect(analyser, i);
        splitter.connect(gain, i);
        analysers.push(analyser);
        chGains.push(gain);
        if (i < 6) {
          if (coeffsL[i]) {
            const gL = ctx.createGain();
            gL.gain.value = coeffsL[i];
            gain.connect(gL);
            gL.connect(merger, 0, 0);
            outNodes.push(gL);
          }
          if (coeffsR[i]) {
            const gR = ctx.createGain();
            gR.gain.value = coeffsR[i];
            gain.connect(gR);
            gR.connect(merger, 0, 1);
            outNodes.push(gR);
          }
        }
        // SAP channels (6,7) meter only when in main+stereo mixdown mode.
      }
      return merger;
    }

    function build51Discrete() {
      const outCh = Math.min(6, channelCount);
      const merger = ctx.createChannelMerger(outCh);
      outNodes.push(merger);
      analysers = [];
      chGains = [];
      for (let i = 0; i < channelCount; i++) {
        const analyser = ctx.createAnalyser();
        analyser.fftSize = FFT;
        analyser.smoothingTimeConstant = SMOOTH;
        const gain = ctx.createGain();
        gain.gain.value = 1;
        splitter.connect(analyser, i);
        splitter.connect(gain, i);
        analysers.push(analyser);
        chGains.push(gain);
        if (i < outCh) gain.connect(merger, 0, i);
      }
      return merger;
    }

    function configureDestination(outChannels) {
      try {
        const dest = ctx.destination;
        const max = dest.maxChannelCount || 2;
        const n = Math.min(outChannels, max);
        dest.channelCount = Math.max(2, n);
        dest.channelCountMode = "explicit";
        dest.channelInterpretation = outChannels > 2 ? "discrete" : "speakers";
      } catch { /* some browsers reject */ }
    }

    function buildGraph(stream) {
      teardownGraph();
      hasAudio = false;
      ctx = ensureCtx();
      if (!ctx || !stream) {
        updateRootVisibility();
        return;
      }
      const audioTracks = stream.getAudioTracks();
      if (!audioTracks.length) {
        updateRootVisibility();
        return;
      }

      let detected = 0;
      try {
        const st = audioTracks[0].getSettings && audioTracks[0].getSettings();
        if (st && st.channelCount) detected = st.channelCount | 0;
      } catch { /* ignore */ }
      // Encode always publishes 8ch; meter the full transport even if the
      // browser reports a lower channelCount while decoding.
      channelCount = 8;
      rebuildMeterDom();

      try {
        const audioStream = new MediaStream(audioTracks);
        source = ctx.createMediaStreamSource(audioStream);
        const splitN = Math.max(8, detected || 8);
        splitter = ctx.createChannelSplitter(Math.min(MAX_CH, Math.max(splitN, 8)));
        masterGain = ctx.createGain();
        masterGain.gain.value = effectiveMasterGain();

        source.connect(splitter);

        const prog = effectiveProgram();
        const play = effectivePlayout();
        let bus;
        if (prog === "sap" && layout.sap) {
          bus = buildStereoPair(layout.sap[0], layout.sap[1]);
          configureDestination(2);
        } else if (layout.has51 && play === "surround") {
          bus = build51Discrete();
          configureDestination(6);
        } else if (layout.has51 && play === "stereo") {
          bus = build51Mixdown();
          configureDestination(2);
        } else {
          // Main stereo (or stereo half of stereo_sap)
          const pair = layout.main;
          bus = buildStereoPair(pair[0], pair[1]);
          configureDestination(2);
        }

        bus.connect(masterGain);
        masterGain.connect(ctx.destination);
        connectedStreamId = stream.id;
        hasAudio = true;
        applyRouting();
        video.muted = true;
        updateRootVisibility();
      } catch (err) {
        console.warn("nexvue-vu: graph failed", err);
        teardownGraph();
        hasAudio = false;
        updateRootVisibility();
      }
    }

    btnMain.addEventListener("click", (ev) => {
      ev.stopPropagation();
      resume();
      setProgram("main");
    });
    btnSap.addEventListener("click", (ev) => {
      ev.stopPropagation();
      resume();
      if (layout.hasSap) setProgram("sap");
    });
    btnStereo.addEventListener("click", (ev) => {
      ev.stopPropagation();
      resume();
      setPlayout("stereo");
    });
    btnSurround.addEventListener("click", (ev) => {
      ev.stopPropagation();
      resume();
      if (layout.has51) setPlayout("surround");
    });
    btnScale.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setScale(!scaleOn);
    });
    allBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      resume();
      setSolo(-1);
    });

    // Element must stay muted — playout is Web Audio only (no native controls).
    function keepElementMuted() {
      if (!video.muted) video.muted = true;
    }
    video.addEventListener("volumechange", keepElementMuted);

    rebuildMeterDom();
    paintScale();
    if (opts.stream) buildGraph(opts.stream);

    return {
      root,
      setStream(stream, layoutOrChannels, embeds) {
        let layoutChanged = false;
        if (layoutOrChannels !== undefined && layoutOrChannels !== null) {
          const next = layoutInfo(layoutOrChannels);
          layoutChanged = next.id !== layout.id;
          layout = next;
        }
        if (embeds !== undefined) {
          embedsOn = new Set(normalizeEmbeds(embeds));
          if (solo >= 0 && !embedsOn.has(solo)) solo = setSoloPref(-1);
        }
        channelCount = 8;
        if (!stream) {
          teardownGraph();
          hasAudio = false;
          updateRootVisibility();
          return;
        }
        // Same MediaStream can arrive again after Settings change — rebuild
        // when the role changed; embeds-only updates just re-paint/route.
        if (stream.id === connectedStreamId && analysers.length) {
          if (layoutChanged) {
            buildGraph(stream);
            return;
          }
          hasAudio = true;
          applyRouting();
          updateRootVisibility();
          return;
        }
        buildGraph(stream);
      },
      setEmbeds(raw) {
        embedsOn = new Set(normalizeEmbeds(raw));
        if (solo >= 0 && !embedsOn.has(solo)) solo = setSoloPref(-1);
        applyRouting();
      },
      setListen(on, listenOpts) {
        listen = !!on;
        const persist = !(listenOpts && listenOpts.persist === false);
        if (persist) setMutedPref(!listen);
        if (masterGain) masterGain.gain.value = effectiveMasterGain();
        // Element must stay muted — audio is via Web Audio only.
        video.muted = true;
        if (listen) resume();
        if (onListenChange) {
          try { onListenChange(listen); } catch { /* ignore */ }
        }
      },
      isListening: () => listen,
      setVolume,
      getVolume: () => volume,
      setLayout(raw) {
        const next = layoutInfo(raw);
        if (next.id === layout.id) {
          paintToolbar();
          return;
        }
        layout = next;
        channelCount = 8;
        if (connectedStreamId && video.srcObject) buildGraph(video.srcObject);
        else rebuildMeterDom();
      },
      setVisible,
      getVisible: () => visible,
      setScale,
      getScale: () => scaleOn,
      setProgram,
      setPlayout,
      setSolo,
      getSolo: () => solo,
      getLayout: () => layout.id,
      getProgram: () => effectiveProgram(),
      getPlayout: () => effectivePlayout(),
      // Back-compat for older callers.
      setChannels(n) {
        if (n === 2) this.setLayout("stereo");
        else if (n === 4) this.setLayout("stereo_sap");
        else if (n === 6) this.setLayout("51");
        else if (n >= 8) this.setLayout("51_sap");
      },
      getChannels: () => channelCount,
      resume,
      detach() {
        teardownGraph();
        video.removeEventListener("volumechange", keepElementMuted);
        if (root.parentNode) root.parentNode.removeChild(root);
      },
    };
  }

  // ---- WHEP multiopus offer munge ---------------------------------------------
  // Chrome/Edge will not advertise multichannel Opus in createOffer(), but they
  // will ACCEPT a munged offer that adds multiopus payload types — MediaMTX's
  // own reader.js does exactly this. Without it, a path publishing >2ch Opus
  // makes MediaMTX CreateAnswer fail with ErrSenderWithNoCodecs → WHEP 400
  // "codecs not supported by client", even though the RTSP path is online.
  // Fmtp strings MUST match mediamtx internal/protocols/webrtc/from_stream.go
  // multichannelOpusSDP (pion matches MimeType + Channels + fmtp).
  const MULTICHANNEL_OPUS_FMTP = {
    3: "channel_mapping=0,2,1;num_streams=2;coupled_streams=1",
    4: "channel_mapping=0,1,2,3;num_streams=2;coupled_streams=2",
    5: "channel_mapping=0,4,1,2,3;num_streams=3;coupled_streams=2",
    6: "channel_mapping=0,4,1,2,3,5;num_streams=4;coupled_streams=2",
    7: "channel_mapping=0,4,1,2,3,5,6;num_streams=4;coupled_streams=4",
    8: "channel_mapping=0,6,1,4,5,2,3,7;num_streams=5;coupled_streams=4",
  };

  function reservePayloadType(used) {
    // Valid dynamic PTs: 30–63 and 96–127 (Chrome payload_type.h).
    for (let i = 30; i <= 127; i++) {
      if ((i <= 63 || i >= 96) && !used.has(String(i))) {
        used.add(String(i));
        return String(i);
      }
    }
    throw new Error("unable to find a free RTP payload type");
  }

  function collectPayloadTypes(sdp) {
    const used = new Set();
    for (const section of sdp.split("m=").slice(1)) {
      const header = section.split("\r\n")[0] || "";
      for (const tok of header.split(" ").slice(3)) {
        if (tok) used.add(tok);
      }
    }
    return used;
  }

  /**
   * Inject multiopus 3–8 recv codecs into a WHEP offer SDP (MediaMTX reader.js
   * algorithm). Leaves stereo opus in place. No-op if multiopus already present.
   */
  function mungeWhepOfferSdp(sdp) {
    if (typeof sdp !== "string" || !sdp) return sdp;
    if (/multiopus\/48000\//i.test(sdp)) return sdp;
    const sections = sdp.split("m=");
    if (sections.length < 2) return sdp;
    const used = collectPayloadTypes(sdp);
    let edited = false;
    for (let i = 1; i < sections.length; i++) {
      if (!sections[i].startsWith("audio")) continue;
      const lines = sections[i].split("\r\n");
      // Insert before the trailing empty line that split leaves after \r\n.
      let insertAt = lines.length;
      while (insertAt > 0 && lines[insertAt - 1] === "") insertAt--;
      for (let ch = 3; ch <= 8; ch++) {
        const pt = reservePayloadType(used);
        lines[0] += ` ${pt}`;
        lines.splice(insertAt, 0, `a=rtpmap:${pt} multiopus/48000/${ch}`);
        insertAt++;
        lines.splice(insertAt, 0, `a=fmtp:${pt} ${MULTICHANNEL_OPUS_FMTP[ch]}`);
        insertAt++;
        lines.splice(insertAt, 0, `a=rtcp-fb:${pt} transport-cc`);
        insertAt++;
      }
      sections[i] = lines.join("\r\n");
      edited = true;
      break;
    }
    return edited ? sections.join("m=") : sdp;
  }

  /**
   * Probe whether this browser accepts a non-advertised multiopus offer
   * (Chrome/Edge yes; Firefox/Safari no). Cached for the page lifetime.
   */
  let multiopusProbe = null;
  function supportsMultiopus() {
    if (multiopusProbe) return multiopusProbe;
    multiopusProbe = new Promise((resolve) => {
      if (typeof RTCPeerConnection === "undefined") {
        resolve(false);
        return;
      }
      const pc = new RTCPeerConnection({ iceServers: [] });
      let pt = "";
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.createOffer()
        .then((offer) => {
          if (/multiopus\/48000\//i.test(offer.sdp || "")) {
            throw new Error("already present");
          }
          const used = collectPayloadTypes(offer.sdp);
          pt = reservePayloadType(used);
          const sections = offer.sdp.split("m=");
          for (let i = 1; i < sections.length; i++) {
            if (!sections[i].startsWith("audio")) continue;
            const lines = sections[i].split("\r\n");
            let insertAt = lines.length;
            while (insertAt > 0 && lines[insertAt - 1] === "") insertAt--;
            lines[0] += ` ${pt}`;
            lines.splice(insertAt, 0, `a=rtpmap:${pt} multiopus/48000/6`);
            lines.splice(
              insertAt + 1,
              0,
              `a=fmtp:${pt} ${MULTICHANNEL_OPUS_FMTP[6]}`
            );
            sections[i] = lines.join("\r\n");
            break;
          }
          offer.sdp = sections.join("m=");
          return pc.setLocalDescription(offer);
        })
        .then(() =>
          pc.setRemoteDescription({
            type: "answer",
            sdp:
              "v=0\r\n" +
              "o=- 0 0 IN IP4 0.0.0.0\r\n" +
              "s=-\r\n" +
              "t=0 0\r\n" +
              "a=fingerprint:sha-256 " +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:" +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n" +
              `m=audio 9 UDP/TLS/RTP/SAVPF ${pt}\r\n` +
              "c=IN IP4 0.0.0.0\r\n" +
              "a=ice-ufrag:nexvue\r\n" +
              "a=ice-pwd:nexvuemultiopusprobe000000\r\n" +
              "a=fingerprint:sha-256 " +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:" +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n" +
              "a=setup:active\r\n" +
              "a=sendonly\r\n" +
              "a=rtcp-mux\r\n" +
              `a=rtpmap:${pt} multiopus/48000/6\r\n` +
              `a=fmtp:${pt} ${MULTICHANNEL_OPUS_FMTP[6]}\r\n`,
          })
        )
        .then(() => resolve(true))
        .catch(() => resolve(false))
        .finally(() => {
          try {
            pc.close();
          } catch (e) {
            /* ignore */
          }
        });
    });
    return multiopusProbe;
  }

  /**
   * Prepare a WHEP offer: add multiopus when the browser supports it.
   * Returns { sdp, multiopus }.
   */
  async function prepareWhepOffer(offer) {
    const base = offer && offer.sdp ? offer.sdp : "";
    const ok = await supportsMultiopus();
    if (!ok) {
      return { sdp: base, multiopus: false };
    }
    const munged = mungeWhepOfferSdp(base);
    return { sdp: munged, multiopus: munged !== base };
  }

  global.NexVueVu = {
    MAX_CH,
    LAYOUTS,
    TRANSPORT_LABELS,
    PREF_VISIBLE,
    PREF_SCALE,
    PREF_MUTED,
    PREF_VOLUME,
    MULTICHANNEL_OPUS_FMTP,
    normalizeLayout,
    normalizeEmbeds,
    layoutInfo,
    mungeWhepOfferSdp,
    supportsMultiopus,
    prepareWhepOffer,
    getVisiblePref,
    setVisiblePref,
    getScalePref,
    setScalePref,
    getMutedPref,
    setMutedPref,
    getVolumePref,
    setVolumePref,
    getProgramPref,
    setProgramPref,
    getPlayoutPref,
    setPlayoutPref,
    getSoloPref,
    setSoloPref,
    resume,
    attach,
  };
})(typeof window !== "undefined" ? window : globalThis);
