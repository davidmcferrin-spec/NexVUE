/**
 * nexvue-vu.js — shared Web-Audio VU meters + per-channel solo for Player / Multiview.
 *
 * Meters and solo are entirely client-side (this browser only). Solo never
 * changes the encode or other viewers. Prefs live in localStorage.
 *
 * Audio is taken from the WebRTC MediaStream (not the <video> element) so
 * muted panes still meter. When listening, the <video> stays muted and
 * playback goes through ChannelSplitter → per-channel Gain → Merger → dest.
 */
(function (global) {
  "use strict";

  const PREF_SOLO = "nexvue-vu-solo"; // "-1" = mix all, "0".."5" = solo that index
  const MIN_CH = 2;
  const MAX_CH = 6;
  const FFT = 2048;
  const SMOOTH = 0.3;
  // Peak ballistics (approx broadcast VU / PPM feel).
  const ATTACK = 0.35;
  const RELEASE = 0.06;

  let sharedCtx = null;

  function clampChannels(n) {
    const v = n | 0;
    if (v < 1) return MIN_CH;
    return Math.min(MAX_CH, Math.max(1, v));
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
    try {
      localStorage.setItem(PREF_SOLO, String(v));
    } catch { /* private mode */ }
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
  width: auto; max-width: 42%; pointer-events: auto;
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
.nexvue-vu-bars {
  flex: 1; min-height: 0; display: flex; flex-direction: row;
  align-items: stretch; gap: 3px; justify-content: flex-end;
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
    // Map -60..0 dBFS → 0..100%
    const n = (db + 60) / 60;
    return Math.max(0, Math.min(100, n * 100));
  }

  /**
   * Attach meters + optional listen graph to a video pane.
   * @param {object} opts
   * @param {HTMLElement} opts.container  .pane-video or .videowrap
   * @param {HTMLVideoElement} opts.video
   * @param {MediaStream|null} opts.stream
   * @param {boolean} [opts.listen=false]  route audio to speakers
   * @param {number} [opts.channels]       expected channel count (2–6); refined from track
   * @returns {object|null} controller
   */
  function attach(opts) {
    ensureStyles();
    const container = opts && opts.container;
    const video = opts && opts.video;
    if (!container || !video) return null;

    const root = document.createElement("div");
    root.className = "nexvue-vu";
    root.hidden = true;
    root.innerHTML =
      '<div class="nexvue-vu-toolbar">' +
      '<button type="button" data-vu="all" title="Listen to all channels (mix)">ALL</button>' +
      "</div>" +
      '<div class="nexvue-vu-bars" role="group" aria-label="Audio level meters"></div>';
    container.appendChild(root);

    const barsEl = root.querySelector(".nexvue-vu-bars");
    const allBtn = root.querySelector('[data-vu="all"]');

    let ctx = null;
    let source = null;
    let splitter = null;
    let merger = null;
    let masterGain = null;
    let analysers = [];
    let gains = [];
    let fills = [];
    let peaks = [];
    let labels = [];
    let chBtns = [];
    let channelCount = clampChannels(opts.channels || MIN_CH);
    let listen = !!opts.listen;
    let solo = getSoloPref();
    let raf = 0;
    let levels = [];
    let peakHold = [];
    let connectedStreamId = null;
    let timeData = null;

    function paintSoloUi() {
      allBtn.classList.toggle("active", solo < 0);
      chBtns.forEach((btn, i) => {
        const isSolo = solo === i;
        btn.classList.toggle("solo", isSolo);
        btn.classList.toggle("dimmed", solo >= 0 && !isSolo);
        btn.setAttribute("aria-pressed", isSolo ? "true" : "false");
      });
    }

    function applyGains() {
      gains.forEach((g, i) => {
        if (!g) return;
        const on = solo < 0 || solo === i;
        g.gain.value = on ? 1 : 0;
      });
      if (masterGain) masterGain.gain.value = listen ? 1 : 0;
      paintSoloUi();
    }

    function setSolo(ch) {
      if (ch === solo && ch >= 0) {
        solo = setSoloPref(-1);
      } else if (ch === null || ch < 0) {
        solo = setSoloPref(-1);
      } else {
        solo = setSoloPref(Math.min(channelCount - 1, ch | 0));
      }
      applyGains();
    }

    function rebuildMeterDom() {
      barsEl.innerHTML = "";
      fills = [];
      peaks = [];
      labels = [];
      chBtns = [];
      for (let i = 0; i < channelCount; i++) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "nexvue-vu-ch";
        btn.title = "Solo channel " + (i + 1) + " (this browser only)";
        btn.setAttribute("aria-label", "Solo audio channel " + (i + 1));
        btn.setAttribute("aria-pressed", "false");
        btn.innerHTML =
          '<span class="nexvue-vu-track">' +
          '<span class="nexvue-vu-fill"></span>' +
          '<span class="nexvue-vu-peak" style="bottom:0%"></span>' +
          "</span>" +
          '<span class="nexvue-vu-label">' + (i + 1) + "</span>";
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
        labels.push(btn.querySelector(".nexvue-vu-label"));
      }
      levels = new Array(channelCount).fill(0);
      peakHold = new Array(channelCount).fill(0);
      if (solo >= channelCount) solo = setSoloPref(-1);
      paintSoloUi();
    }

    function teardownGraph() {
      if (raf) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
      try { if (source) source.disconnect(); } catch { /* ignore */ }
      try { if (splitter) splitter.disconnect(); } catch { /* ignore */ }
      analysers.forEach((a) => { try { a.disconnect(); } catch { /* ignore */ } });
      gains.forEach((g) => { try { g.disconnect(); } catch { /* ignore */ } });
      try { if (merger) merger.disconnect(); } catch { /* ignore */ }
      try { if (masterGain) masterGain.disconnect(); } catch { /* ignore */ }
      source = null;
      splitter = null;
      merger = null;
      masterGain = null;
      analysers = [];
      gains = [];
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
        const h = heightFromDb(dbFromPeak(next));
        const ph = heightFromDb(dbFromPeak(peakHold[i]));
        if (fills[i]) fills[i].style.height = h.toFixed(1) + "%";
        if (peaks[i]) peaks[i].style.bottom = ph.toFixed(1) + "%";
      }
      raf = requestAnimationFrame(tick);
    }

    function buildGraph(stream) {
      teardownGraph();
      ctx = ensureCtx();
      if (!ctx || !stream) {
        root.hidden = true;
        return;
      }
      const audioTracks = stream.getAudioTracks();
      if (!audioTracks.length) {
        root.hidden = true;
        return;
      }

# Prefer configured AUDIO_CHANNELS for bar count when the track has not
      // reported yet; once getSettings().channelCount is known, prefer that
      // so solo matches what the browser actually decoded.
      let detected = 0;
      try {
        const st = audioTracks[0].getSettings && audioTracks[0].getSettings();
        if (st && st.channelCount) detected = st.channelCount | 0;
      } catch { /* ignore */ }
      if (detected > 0) {
        channelCount = clampChannels(detected);
      } else {
        channelCount = clampChannels(channelCount || opts.channels || MIN_CH);
      }
      rebuildMeterDom();

      try {
        // Dedicated stream with audio only avoids pulling video into the graph.
        const audioStream = new MediaStream(audioTracks);
        source = ctx.createMediaStreamSource(audioStream);
        splitter = ctx.createChannelSplitter(channelCount);
        merger = ctx.createChannelMerger(channelCount);
        masterGain = ctx.createGain();
        masterGain.gain.value = listen ? 1 : 0;

        source.connect(splitter);
        analysers = [];
        gains = [];
        for (let i = 0; i < channelCount; i++) {
          const analyser = ctx.createAnalyser();
          analyser.fftSize = FFT;
          analyser.smoothingTimeConstant = SMOOTH;
          const gain = ctx.createGain();
          gain.gain.value = solo < 0 || solo === i ? 1 : 0;
          splitter.connect(analyser, i);
          // Tap meter before solo mute so dimmed channels still show level.
          splitter.connect(gain, i);
          gain.connect(merger, 0, i);
          analysers.push(analyser);
          gains.push(gain);
        }
        merger.connect(masterGain);
        masterGain.connect(ctx.destination);
        connectedStreamId = stream.id;
        root.hidden = false;
        applyGains();
        // Element must stay muted — audio is via Web Audio only.
        video.muted = true;
        if (!raf) raf = requestAnimationFrame(tick);
      } catch (err) {
        console.warn("nexvue-vu: graph failed", err);
        teardownGraph();
        root.hidden = true;
      }
    }

    allBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      resume();
      setSolo(-1);
    });

    // Video element audio must stay muted — Web Audio owns playback. Re-assert
    // if the user toggles the native controls unmute control.
    function keepElementMuted() {
      if (!video.muted) video.muted = true;
    }
    video.addEventListener("volumechange", keepElementMuted);

    rebuildMeterDom();

    if (opts.stream) buildGraph(opts.stream);

    return {
      root,
      setStream(stream, channelsHint) {
        if (channelsHint) channelCount = clampChannels(channelsHint);
        if (!stream) {
          teardownGraph();
          root.hidden = true;
          return;
        }
        if (stream.id === connectedStreamId && analysers.length) {
          // Same stream; maybe update listen/solo only.
          applyGains();
          root.hidden = false;
          return;
        }
        buildGraph(stream);
      },
      setListen(on) {
        listen = !!on;
        if (masterGain) masterGain.gain.value = listen ? 1 : 0;
        if (listen) {
          video.muted = true;
          resume();
        }
      },
      setChannels(n) {
        const next = clampChannels(n);
        if (next === channelCount) return;
        channelCount = next;
        if (connectedStreamId && video.srcObject) {
          buildGraph(video.srcObject);
        } else {
          rebuildMeterDom();
        }
      },
      setSolo,
      getSolo: () => solo,
      getChannels: () => channelCount,
      resume,
      detach() {
        teardownGraph();
        video.removeEventListener("volumechange", keepElementMuted);
        if (root.parentNode) root.parentNode.removeChild(root);
      },
    };
  }

  global.NexVueVu = {
    MIN_CH,
    MAX_CH,
    getSoloPref,
    setSoloPref,
    resume,
    attach,
    clampChannels,
  };
})(typeof window !== "undefined" ? window : globalThis);
