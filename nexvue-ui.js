/**
 * nexvue-ui.js — shared theme + nav logo + version badge for all NexVUE pages.
 *
 * Load synchronously in <head> so the theme is applied before first paint
 * (no light/dark flash). Wires #theme-toggle, .nav-logo, and #nav-version
 * on DOMContentLoaded.
 *
 * localStorage key: nexvue-theme ("dark" | "light"); default dark.
 * Dispatches window event "nexvue-theme-changed" with detail: { theme }.
 * Version comes from nexvue-version.php (VERSION file + optional git stamp).
 */
(function (global) {
  "use strict";

  var STORAGE_KEY = "nexvue-theme";
  var LOGO_SRC = "nexvue-logo.php";
  var VERSION_URL = "nexvue-version.php";
  var ROTATE_KEY = "nexvue-video-rotate";

  function normalizeRotate(deg) {
    var n = Number(deg);
    if (!Number.isFinite(n)) return 0;
    n = ((Math.round(n / 90) * 90) % 360 + 360) % 360;
    return n;
  }

  function getRotatePref() {
    try {
      return normalizeRotate(global.localStorage.getItem(ROTATE_KEY));
    } catch (e) {
      return 0;
    }
  }

  function setRotatePref(deg) {
    deg = normalizeRotate(deg);
    try {
      if (deg === 0) global.localStorage.removeItem(ROTATE_KEY);
      else global.localStorage.setItem(ROTATE_KEY, String(deg));
    } catch (e) {
      /* private mode */
    }
    return deg;
  }

  /**
   * Orient a <video> inside a sized wrap. rotate is CW degrees (0|90|180|270).
   * mirror/flip are optional scaleX/scaleY (Player confidence monitor).
   * For 90/270, swaps layout box so the rotated frame still fits the wrap.
   */
  function applyVideoOrient(video, wrap, opts) {
    if (!video) return;
    opts = opts || {};
    var rot = normalizeRotate(opts.rotate);
    var sx = opts.mirror ? -1 : 1;
    var sy = opts.flip ? -1 : 1;
    var parts = [];
    if (rot) parts.push("rotate(" + rot + "deg)");
    if (sx !== 1 || sy !== 1) parts.push("scale(" + sx + ", " + sy + ")");

    if (rot === 90 || rot === 270) {
      var ww = wrap && wrap.clientWidth ? wrap.clientWidth : 0;
      var wh = wrap && wrap.clientHeight ? wrap.clientHeight : 0;
      if (ww > 0 && wh > 0) {
        video.style.position = "absolute";
        video.style.left = "50%";
        video.style.top = "50%";
        video.style.width = wh + "px";
        video.style.height = ww + "px";
        video.style.maxWidth = "none";
        video.style.maxHeight = "none";
        video.style.objectFit = "contain";
        parts.unshift("translate(-50%, -50%)");
        video.style.transform = parts.join(" ");
        return;
      }
    }

    video.style.position = "";
    video.style.left = "";
    video.style.top = "";
    video.style.width = "";
    video.style.height = "";
    video.style.maxWidth = "";
    video.style.maxHeight = "";
    video.style.objectFit = "";
    video.style.transform = parts.length ? parts.join(" ") : "";
  }

  function ensureVersionCss() {
    if (global.document.getElementById("nexvue-ui-version-css")) return;
    var s = global.document.createElement("style");
    s.id = "nexvue-ui-version-css";
    s.textContent =
      ".topnav .nav-version{" +
      "margin-left:6px;margin-right:0;padding:4px 8px;" +
      "border:1px solid var(--edge,#2c3542);border-radius:4px;" +
      "color:var(--dim,#98a6b5);font-size:11px;letter-spacing:.02em;" +
      "user-select:none;white-space:nowrap;" +
      "}" +
      ".topnav .nav-version[hidden]{display:none!important}" +
      ".topnav .nav-version.update-available{" +
      "color:var(--acc,#56c4f5);border-color:var(--acc,#56c4f5)" +
      "}";
    (global.document.head || global.document.documentElement).appendChild(s);
  }

  function normalizeTheme(value) {
    return value === "light" ? "light" : "dark";
  }

  function readStoredTheme() {
    try {
      return normalizeTheme(global.localStorage.getItem(STORAGE_KEY));
    } catch (e) {
      return "dark";
    }
  }

  function applyTheme(theme) {
    theme = normalizeTheme(theme);
    var root = global.document.documentElement;
    if (root) {
      root.setAttribute("data-theme", theme);
    }
    return theme;
  }

  function setTheme(theme) {
    theme = applyTheme(theme);
    try {
      global.localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      /* ignore quota / private mode */
    }
    try {
      global.dispatchEvent(
        new CustomEvent("nexvue-theme-changed", { detail: { theme: theme } })
      );
    } catch (e) {
      /* ignore */
    }
    syncToggle();
    return theme;
  }

  function getTheme() {
    var root = global.document.documentElement;
    if (root && root.getAttribute("data-theme")) {
      return normalizeTheme(root.getAttribute("data-theme"));
    }
    return readStoredTheme();
  }

  function toggleTheme() {
    return setTheme(getTheme() === "light" ? "dark" : "light");
  }

  function syncToggle() {
    var btn = global.document.getElementById("theme-toggle");
    if (!btn) {
      return;
    }
    var theme = getTheme();
    var isLight = theme === "light";
    btn.setAttribute("aria-pressed", isLight ? "true" : "false");
    btn.setAttribute(
      "title",
      isLight ? "Switch to dark mode" : "Switch to light mode"
    );
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark mode" : "Switch to light mode"
    );
    btn.textContent = isLight ? "Dark" : "Light";
  }

  function wireLogo(img) {
    if (!img) {
      return;
    }
    img.addEventListener("load", function () {
      img.hidden = false;
      img.removeAttribute("hidden");
    });
    img.addEventListener("error", function () {
      img.hidden = true;
      img.setAttribute("hidden", "");
    });
    // Force a fresh check when Settings replaces the logo.
    if (!img.getAttribute("src")) {
      img.setAttribute("src", LOGO_SRC);
    }
  }

  /** Bust cache after upload/delete so all open tabs can refresh the img. */
  function refreshNavLogo() {
    var imgs = global.document.querySelectorAll("img.nav-logo");
    var bust = LOGO_SRC + "?t=" + String(Date.now());
    for (var i = 0; i < imgs.length; i++) {
      imgs[i].hidden = true;
      imgs[i].setAttribute("hidden", "");
      imgs[i].setAttribute("src", bust);
    }
  }

  function onReady(fn) {
    if (global.document.readyState === "loading") {
      global.document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  // Apply before paint (script is in <head>).
  applyTheme(readStoredTheme());

  function ensureVersionEl() {
    var el = global.document.getElementById("nav-version");
    if (el) return el;
    var btn = global.document.getElementById("theme-toggle");
    var nav = global.document.querySelector("nav.topnav");
    if (!nav) return null;
    el = global.document.createElement("span");
    el.id = "nav-version";
    el.className = "nav-version";
    el.hidden = true;
    el.setAttribute("title", "NexVUE version");
    // Sit to the right of the theme toggle (toggle keeps margin-left:auto).
    if (btn && btn.parentNode === nav) {
      if (btn.nextSibling) nav.insertBefore(el, btn.nextSibling);
      else nav.appendChild(el);
    } else {
      nav.appendChild(el);
    }
    return el;
  }

  function paintVersion(data) {
    ensureVersionCss();
    var el = ensureVersionEl();
    if (!el || !data || !data.ok) return;
    var ver = String(data.version || "").trim() || "0.0.0";
    el.textContent = "v" + ver;
    var tip = "NexVUE v" + ver;
    if (data.git_sha) tip += " · " + data.git_sha;
    if (data.git_branch) tip += " (" + data.git_branch + ")";
    el.title = tip;
    el.hidden = false;
  }

  function loadVersion() {
    ensureVersionCss();
    ensureVersionEl();
    if (typeof global.fetch !== "function") return;
    global
      .fetch(VERSION_URL, { cache: "no-store" })
      .then(function (res) {
        return res.json();
      })
      .then(paintVersion)
      .catch(function () {
        /* version badge optional */
      });
  }

  function refreshVersion() {
    loadVersion();
  }

  onReady(function () {
    syncToggle();
    var btn = global.document.getElementById("theme-toggle");
    if (btn) {
      btn.addEventListener("click", function () {
        toggleTheme();
      });
    }
    var logos = global.document.querySelectorAll("img.nav-logo");
    for (var i = 0; i < logos.length; i++) {
      wireLogo(logos[i]);
    }
    loadVersion();
  });

  global.NexVUEUI = {
    getTheme: getTheme,
    setTheme: setTheme,
    toggleTheme: toggleTheme,
    refreshNavLogo: refreshNavLogo,
    refreshVersion: refreshVersion,
    getRotatePref: getRotatePref,
    setRotatePref: setRotatePref,
    applyVideoOrient: applyVideoOrient,
    STORAGE_KEY: STORAGE_KEY,
    ROTATE_KEY: ROTATE_KEY,
    LOGO_SRC: LOGO_SRC,
  };
})(typeof window !== "undefined" ? window : globalThis);
