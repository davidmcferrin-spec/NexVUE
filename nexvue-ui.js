/**
 * nexvue-ui.js — shared theme + nav logo helpers for all NexVUE pages.
 *
 * Load synchronously in <head> so the theme is applied before first paint
 * (no light/dark flash). Wires #theme-toggle and .nav-logo on DOMContentLoaded.
 *
 * localStorage key: nexvue-theme ("dark" | "light"); default dark.
 * Dispatches window event "nexvue-theme-changed" with detail: { theme }.
 */
(function (global) {
  "use strict";

  var STORAGE_KEY = "nexvue-theme";
  var LOGO_SRC = "nexvue-logo.php";

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
  });

  global.NexVUEUI = {
    getTheme: getTheme,
    setTheme: setTheme,
    toggleTheme: toggleTheme,
    refreshNavLogo: refreshNavLogo,
    STORAGE_KEY: STORAGE_KEY,
    LOGO_SRC: LOGO_SRC,
  };
})(typeof window !== "undefined" ? window : globalThis);
