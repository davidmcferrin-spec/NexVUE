/**
 * nexvue-client-report.js — opt-in Player / Multiview session reporting.
 *
 * Nothing is uploaded until Report session is clicked. While armed, posts
 * snapshots (~20s) and events (channel / rendition / errors / hide / close)
 * to same-origin /api/client-events. Join key is the MediaMTX WHEP ID
 * (or a client-generated c… key when Stream/SFU has no UUID).
 */
(function (global) {
  "use strict";

  var ENDPOINT = "/api/client-events";
  var SNAP_MS = 20000;
  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  var CLIENT_RE = /^c[0-9a-f]{32}$/i;

  function sessionIdOk(s) {
    return typeof s === "string" && (UUID_RE.test(s) || CLIENT_RE.test(s));
  }

  function newClientKey() {
    var a = new Uint8Array(16);
    (global.crypto || window.crypto).getRandomValues(a);
    var hex = "";
    for (var i = 0; i < a.length; i++) {
      hex += ("0" + a[i].toString(16)).slice(-2);
    }
    return "c" + hex;
  }

  function clientHints() {
    var out = { ua: String(navigator.userAgent || "").slice(0, 240) };
    try {
      var ua = navigator.userAgentData;
      if (ua) {
        var brands = (ua.brands || []).map(function (b) {
          return (b.brand || "") + "/" + (b.version || "");
        }).join(", ");
        out.ua_brands = brands.slice(0, 120);
        out.ua_mobile = !!ua.mobile;
        out.ua_platform = String(ua.platform || "").slice(0, 40);
      }
    } catch (e) { /* ignore */ }
    if (navigator.hardwareConcurrency) out.hw_concurrency = navigator.hardwareConcurrency;
    try {
      var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
      if (c) {
        out.net_type = String(c.effectiveType || c.type || "").slice(0, 24);
      }
    } catch (e) { /* ignore */ }
    return out;
  }

  function clientLabel(h) {
    if (h.ua_brands) {
      var plat = h.ua_platform ? " · " + h.ua_platform : "";
      return (h.ua_brands + plat).slice(0, 160);
    }
    return (h.ua || "").slice(0, 160);
  }

  function postJson(body, beacon) {
    var payload = JSON.stringify(body);
    if (beacon && navigator.sendBeacon) {
      try {
        var blob = new Blob([payload], { type: "application/json" });
        return Promise.resolve(navigator.sendBeacon(ENDPOINT, blob));
      } catch (e) { /* fall through */ }
    }
    return fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      credentials: "same-origin",
      keepalive: !!beacon,
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok || data.ok === false) {
          throw new Error(data.error || ("HTTP " + res.status));
        }
        return data;
      });
    });
  }

  /**
   * @param {object} opts
   * @param {HTMLButtonElement} opts.button
   * @param {HTMLElement} [opts.statusEl]
   * @param {function(): object} opts.getContext
   *   { sessionId, channel, rendition, page, stats, connected }
   */
  function attach(opts) {
    var button = opts.button;
    var statusEl = opts.statusEl || null;
    var getContext = opts.getContext;
    var armed = false;
    var timer = null;
    var clientFallback = null;
    var lastSid = null;
    var lastChannel = null;

    function setStatus(text, cls) {
      if (!statusEl) return;
      statusEl.textContent = text || "";
      statusEl.className = "report-state" + (cls ? " " + cls : "");
    }

    function paint() {
      if (!button) return;
      button.classList.toggle("active", armed);
      button.textContent = armed ? "Stop reporting" : "Report session";
      button.setAttribute("aria-pressed", armed ? "true" : "false");
    }

    function resolveSid(ctx) {
      var sid = ctx && ctx.sessionId;
      if (sessionIdOk(sid)) return sid;
      if (ctx && ctx.connected) {
        if (!clientFallback) clientFallback = newClientKey();
        return clientFallback;
      }
      return null;
    }

    function buildSnapshot(ctx) {
      var h = clientHints();
      var s = (ctx && ctx.stats) || {};
      return {
        inbound_bps: s.inbound_bps != null ? s.inbound_bps : null,
        loss_pct: s.loss_pct != null ? s.loss_pct : null,
        rtt_ms: s.rtt_ms != null ? s.rtt_ms : null,
        jb_ms: s.jb_ms != null ? s.jb_ms : null,
        width: s.width != null ? s.width : null,
        height: s.height != null ? s.height : null,
        fps: s.fps != null ? s.fps : null,
        frames_dropped: s.frames_dropped != null ? s.frames_dropped : null,
        rendition: ctx && ctx.rendition ? String(ctx.rendition).slice(0, 8) : null,
        channel: ctx && ctx.channel ? String(ctx.channel).slice(0, 16) : null,
        hidden: !!(document.hidden),
        hw_concurrency: h.hw_concurrency || null,
        net_type: h.net_type || null,
        ua: h.ua,
      };
    }

    function envelope(ctx, extra) {
      var h = clientHints();
      var sid = resolveSid(ctx);
      var body = {
        session_id: sid,
        page: ctx && ctx.page ? ctx.page : "player",
        channel: ctx && ctx.channel ? String(ctx.channel).slice(0, 16) : "",
        client_label: clientLabel(h),
      };
      for (var k in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, k)) body[k] = extra[k];
      }
      return body;
    }

    function send(extra, beacon) {
      if (!armed) return Promise.resolve(null);
      var ctx = getContext() || {};
      var sid = resolveSid(ctx);
      if (!sid) return Promise.resolve(null);
      lastSid = sid;
      if (ctx.channel) lastChannel = ctx.channel;
      return postJson(envelope(ctx, extra), beacon).catch(function (err) {
        setStatus("report failed: " + (err.message || err), "bad");
        return null;
      });
    }

    function sendSnapshot(beacon) {
      var ctx = getContext() || {};
      return send({ kind: "snapshot", snapshot: buildSnapshot(ctx) }, beacon);
    }

    function sendEvent(kind, detail, beacon) {
      var extra = { kind: "event", event: kind };
      if (detail) extra.detail = String(detail).slice(0, 400);
      return send(extra, beacon);
    }

    function startTimer() {
      if (timer) clearInterval(timer);
      timer = setInterval(function () {
        if (!armed || document.hidden) return;
        sendSnapshot(false);
      }, SNAP_MS);
    }

    function stopTimer() {
      if (timer) { clearInterval(timer); timer = null; }
    }

    function start() {
      var ctx = getContext() || {};
      var sid = resolveSid(ctx);
      if (!sid) {
        setStatus("start a channel first", "bad");
        return;
      }
      armed = true;
      lastSid = sid;
      lastChannel = ctx.channel || lastChannel;
      paint();
      setStatus("reporting this session…", "ok");
      sendEvent("start", ctx.channel || "").then(function () {
        return sendSnapshot(false);
      });
      startTimer();
    }

    function stop(reason) {
      if (!armed) return;
      sendEvent("stop", reason || "button", reason === "pagehide");
      armed = false;
      stopTimer();
      paint();
      setStatus("report stopped", "");
    }

    function toggle() {
      if (armed) stop("button");
      else start();
    }

    /**
     * Channel / session identity changed while playing.
     * If armed, keep reporting (new WHEP UUID is a new report row + reconnect).
     */
    function noteSession(sessionId, channel) {
      var ctx = getContext() || {};
      var sid = sessionIdOk(sessionId) ? sessionId : resolveSid(ctx);
      if (channel && lastChannel && channel !== lastChannel && armed) {
        sendEvent("channel", lastChannel + " → " + channel, false);
      }
      if (sid && lastSid && sid !== lastSid && armed) {
        sendEvent("reconnect", lastSid + " → " + sid, false);
        lastSid = sid;
        sendEvent("start", channel || ctx.channel || "", false);
        sendSnapshot(false);
      } else if (sid) {
        lastSid = sid;
      }
      if (channel) lastChannel = channel;
      if (button) {
        button.disabled = !(armed || sid || (ctx && ctx.connected));
      }
    }

    function notify(kind, detail) {
      if (!armed) return;
      sendEvent(kind, detail, kind === "pagehide");
    }

    if (button) {
      button.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        toggle();
      });
      button.disabled = true;
    }
    paint();

    document.addEventListener("visibilitychange", function () {
      if (!armed) return;
      notify(document.hidden ? "hidden" : "visible", "");
    });
    window.addEventListener("pagehide", function () {
      if (!armed) return;
      sendEvent("pagehide", lastChannel || "", true);
    });

    return {
      notify: notify,
      noteSession: noteSession,
      isArmed: function () { return armed; },
      stop: stop,
    };
  }

  global.NexVueClientReport = { attach: attach };
})(window);
