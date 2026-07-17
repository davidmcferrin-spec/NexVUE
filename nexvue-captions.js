/**
 * nexvue-captions.js — shared CC preference + SSE client for Player / Multiview / Cast.
 * Captions arrive via same-origin nexvue-captions.php (not MediaMTX / WHEP).
 */
(function (global) {
  "use strict";

  const PREF_KEY = "nexvue-captions-on";

  function getPref() {
    try {
      return localStorage.getItem(PREF_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setPref(on) {
    try {
      localStorage.setItem(PREF_KEY, on ? "1" : "0");
    } catch { /* private mode */ }
  }

  /**
   * Subscribe to caption cues for a base channel (ch0, not ch0lo).
   * onCue({ text, clear, seq, service })
   * Returns { close(), setChannel(base|null) }.
   */
  function connect(onCue) {
    let es = null;
    let channel = null;

    function close() {
      if (es) {
        try { es.close(); } catch { /* ignore */ }
        es = null;
      }
    }

    function setChannel(base) {
      const next = base && String(base).replace(/lo$/, "");
      if (next === channel && es) return;
      close();
      channel = next || null;
      if (!channel) {
        onCue({ text: "", clear: true, seq: 0, service: "CC1" });
        return;
      }
      const url = "nexvue-captions.php?channel=" + encodeURIComponent(channel);
      es = new EventSource(url);
      es.onmessage = (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch {
          return;
        }
        onCue({
          text: typeof data.text === "string" ? data.text : "",
          clear: !!data.clear || !data.text,
          seq: data.seq | 0,
          service: data.service || "CC1",
        });
      };
      es.onerror = () => {
        // EventSource reconnects automatically; leave open.
      };
    }

    return { close, setChannel, getChannel: () => channel };
  }

  /** Render cue text into an overlay element (uses textContent). */
  function renderOverlay(el, cue, enabled) {
    if (!el) return;
    if (!enabled || !cue || cue.clear || !cue.text) {
      el.textContent = "";
      el.hidden = true;
      return;
    }
    el.textContent = cue.text;
    el.hidden = false;
  }

  global.NexVueCaptions = {
    PREF_KEY,
    getPref,
    setPref,
    connect,
    renderOverlay,
  };
})(typeof window !== "undefined" ? window : globalThis);
