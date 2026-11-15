/**
 * nexvue-ui.js — shared theme + nav logo + version badge for all NexVUE pages.
 *
 * Load synchronously in <head> so the theme is applied before first paint
 * (no light/dark flash). Wires #theme-toggle, .nav-logo, and #nav-version
 * on DOMContentLoaded.
 *
 * localStorage key: nexvue-theme ("dark" | "light"); default dark.
 * Dispatches window event "nexvue-theme-changed" with detail: { theme }.
 * Theater (fill-window) lives on pages marked data-theater-page (Player /
 * Multiview). Key: nexvue-theater ("1" | unset); applied before first paint
 * so wall monitors do not flash chrome. Dispatches "nexvue-theater-changed".
 * Version comes from nexvue-version.php (VERSION file + optional git stamp).
 */
(function (global) {
  "use strict";

  var STORAGE_KEY = "nexvue-theme";
  var LOGO_SRC = "/api/logo";
  var VERSION_URL = "/api/version";
  var ROTATE_KEY = "nexvue-video-rotate";
  var THEATER_KEY = "nexvue-theater";
  var TOOLS_KEY = "nexvue-player-tools";
  // Phones, plus short landscape (a landscape phone is wide but not tall).
  // 759px leaves a 768px tablet on the desktop bars.
  var COMPACT_MQ = "(max-width: 759px), (max-height: 520px) and (max-width: 1024px)";
  var _maxChannels = 8;

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

  function pageWantsTheater() {
    var root = global.document && global.document.documentElement;
    return !!(root && root.hasAttribute("data-theater-page"));
  }

  function pageIsPlayer() {
    var root = global.document && global.document.documentElement;
    return !!(root && root.hasAttribute("data-player-page"));
  }

  function readPlayerToolsOpen() {
    try {
      return global.localStorage.getItem(TOOLS_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function setPlayerToolsOpen(on) {
    on = !!on;
    var root = global.document && global.document.documentElement;
    if (root) root.classList.toggle("player-tools-open", on);
    try {
      global.localStorage.setItem(TOOLS_KEY, on ? "1" : "0");
    } catch (e) {
      /* private mode */
    }
    return on;
  }

  function getTheaterPref() {
    try {
      return global.localStorage.getItem(THEATER_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function setTheaterPref(on) {
    on = !!on;
    try {
      if (on) global.localStorage.setItem(THEATER_KEY, "1");
      else global.localStorage.removeItem(THEATER_KEY);
    } catch (e) {
      /* private mode */
    }
    return on;
  }

  function applyTheater(on) {
    on = !!on;
    var root = global.document && global.document.documentElement;
    if (root) {
      if (on) root.classList.add("theater");
      else root.classList.remove("theater");
    }
    return on;
  }

  function isTheater() {
    var root = global.document && global.document.documentElement;
    return !!(root && root.classList.contains("theater"));
  }

  function syncTheaterButton() {
    var btn = global.document && global.document.getElementById("theater");
    if (!btn) return;
    var on = isTheater();
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function setTheater(on) {
    on = applyTheater(!!on);
    setTheaterPref(on);
    syncTheaterButton();
    try {
      global.dispatchEvent(
        new CustomEvent("nexvue-theater-changed", { detail: { theater: on } })
      );
    } catch (e) {
      /* ignore */
    }
    return on;
  }

  function toggleTheater() {
    return setTheater(!isTheater());
  }

  function fullscreenElement() {
    var doc = global.document;
    if (!doc) return null;
    return doc.fullscreenElement || doc.webkitFullscreenElement || null;
  }

  function wireTheaterControls() {
    if (!pageWantsTheater()) return;
    applyTheater(getTheaterPref());
    syncTheaterButton();
    var btn = global.document.getElementById("theater");
    if (btn) {
      btn.addEventListener("click", function () {
        toggleTheater();
      });
    }
    var exitBtn = global.document.getElementById("theater-exit");
    if (exitBtn) {
      exitBtn.addEventListener("click", function () {
        setTheater(false);
      });
    }
    global.document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      if (fullscreenElement()) return;
      if (!isTheater()) return;
      ev.preventDefault();
      setTheater(false);
    });
  }

  function pipSupported() {
    try {
      return !!(global.document && global.document.pictureInPictureEnabled);
    } catch (e) {
      return false;
    }
  }

  function isPipActive(video) {
    return !!(
      global.document &&
      video &&
      global.document.pictureInPictureElement === video
    );
  }

  function requestPip(video) {
    if (!video || !pipSupported() || !video.srcObject) {
      return Promise.resolve(false);
    }
    if (typeof video.requestPictureInPicture !== "function") {
      return Promise.resolve(false);
    }
    return video
      .requestPictureInPicture()
      .then(function () {
        return true;
      })
      .catch(function () {
        return false;
      });
  }

  function exitPip() {
    if (!global.document || !global.document.pictureInPictureElement) {
      return Promise.resolve(false);
    }
    if (typeof global.document.exitPictureInPicture !== "function") {
      return Promise.resolve(false);
    }
    return global.document
      .exitPictureInPicture()
      .then(function () {
        return false;
      })
      .catch(function () {
        return false;
      });
  }

  function togglePip(video) {
    if (isPipActive(video)) return exitPip();
    return requestPip(video);
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
    // Probe before setting <img src> so a missing logo (PHP 404 by design)
    // does not spam the console. No logo → stay hidden.
    img.removeAttribute("src");
    img.hidden = true;
    img.setAttribute("hidden", "");
    var show = function () {
      img.setAttribute("src", LOGO_SRC + "?t=" + String(Date.now()));
    };
    if (typeof global.fetch !== "function") {
      show();
      return;
    }
    global
      .fetch(LOGO_SRC, {
        method: "HEAD",
        credentials: "same-origin",
        cache: "no-store",
      })
      .then(function (res) {
        if (res.ok) {
          show();
          return;
        }
        // Some Apache configs reject HEAD on PHP — fall back once.
        if (res.status === 405 || res.status === 501) {
          show();
        }
      })
      .catch(function () {
        /* stay hidden */
      });
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
  if (pageWantsTheater()) {
    applyTheater(getTheaterPref());
  }
  // Player options default closed on a phone. Remembered per browser.
  if (pageIsPlayer() && readPlayerToolsOpen()) {
    global.document.documentElement.classList.add("player-tools-open");
  }

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

  // CHANNEL_ALIAS display — path (chN / chNlo) stays identity for ACL, WHEP,
  // MediaMTX, and systemd. Empty alias falls back to the path. LO renditions
  // append " LO" to the base-channel label.
  var _aliasCache = {};
  var _aliasPromise = null;

  function channelBase(path) {
    var s = String(path == null ? "" : path).trim().toLowerCase();
    var m = s.match(/^ch([0-7])(?:lo)?$/);
    if (m) return "ch" + m[1];
    m = s.match(/^([0-7])$/);
    if (m) return "ch" + m[1];
    return s;
  }

  function channelLabel(path, aliases) {
    var raw = String(path == null ? "" : path).trim();
    if (!raw) return "";
    var base = channelBase(raw);
    var map = aliases || _aliasCache;
    var alias = "";
    if (map && typeof map === "object") {
      alias = map[base] || map[String(base).replace(/^ch/, "")] || "";
    }
    var label = alias || base || raw;
    if (/^ch[0-7]lo$/i.test(raw.replace(/\s+/g, ""))) {
      return label + " LO";
    }
    return label;
  }

  function channelTitle(path, aliases) {
    var raw = String(path == null ? "" : path).trim();
    if (!raw) return "";
    var label = channelLabel(raw, aliases);
    if (label === raw) return raw;
    return label + " (" + raw + ")";
  }

  function channelLabels(list, aliases) {
    if (list == null) return "all";
    if (!Array.isArray(list) || !list.length) return "—";
    return list
      .map(function (c) {
        return channelLabel(c, aliases);
      })
      .join(", ");
  }

  function unitLabel(unit, aliases) {
    var u = String(unit == null ? "" : unit);
    var m = u.match(/^nexvue-encode@([0-7])$/);
    if (!m) return u;
    var path = "ch" + m[1];
    var lab = channelLabel(path, aliases);
    if (!lab || lab === path) return u;
    return u + " (" + lab + ")";
  }

  function getChannelAliases() {
    return _aliasCache;
  }

  function setChannelAliases(map) {
    if (map && typeof map === "object") {
      _aliasCache = map;
    }
    return _aliasCache;
  }

  function clampSlotCount(n) {
    n = Number(n);
    if (!Number.isFinite(n)) return 8;
    n = Math.floor(n);
    if (n < 1) return 1;
    if (n > 8) return 8;
    return n;
  }

  function setMaxChannels(n) {
    _maxChannels = clampSlotCount(n);
    return _maxChannels;
  }

  function maxChannels() {
    return _maxChannels;
  }

  function applyAliasPayload(data) {
    if (data && data.ok && data.aliases && typeof data.aliases === "object") {
      _aliasCache = data.aliases;
    }
    if (data && data.ok && data.max_channels != null) {
      setMaxChannels(data.max_channels);
    }
    return _aliasCache;
  }

  function loadChannelAliases(force) {
    if (force) _aliasPromise = null;
    if (_aliasPromise) return _aliasPromise;
    if (typeof global.fetch !== "function") {
      return Promise.resolve(_aliasCache);
    }
    _aliasPromise = global
      .fetch("/api/ops?action=aliases", {
        cache: "no-store",
        credentials: "same-origin",
      })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        applyAliasPayload(data);
        return _aliasCache;
      })
      .catch(function () {
        return _aliasCache;
      });
    return _aliasPromise;
  }

  function ensureCompactNavCss() {
    if (global.document.getElementById("nexvue-ui-compact-css")) return;
    var s = global.document.createElement("style");
    s.id = "nexvue-ui-compact-css";
    s.textContent = [
      ".nav-menu-btn{display:none}",
      ".nav-menu{display:none}",
      "@media " + COMPACT_MQ + "{",
      ".topnav{position:relative;z-index:80;flex-wrap:nowrap;gap:6px}",
      ".topnav .brand{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".topnav .nav-logo{height:22px;max-width:64px;margin-right:4px}",
      ".nav-menu-btn{display:inline-flex;align-items:center;justify-content:center;",
      "flex:0 0 auto;min-width:44px;min-height:44px;padding:8px}",
      ".topnav .theme-toggle,.topnav .nav-logout,.topnav .nav-version{flex-shrink:0}",
      ".topnav .theme-toggle,.topnav .nav-logout,.topnav .brand{min-height:44px}",
      ".nav-menu-bars{display:block;width:18px;height:2px;background:currentColor;position:relative}",
      ".nav-menu-bars::before,.nav-menu-bars::after{content:\"\";position:absolute;left:0;",
      "width:18px;height:2px;background:currentColor}",
      ".nav-menu-bars::before{top:-6px}",
      ".nav-menu-bars::after{top:6px}",
      ".topnav.menu-open .nav-menu-bars{background:transparent}",
      ".topnav.menu-open .nav-menu-bars::before{top:0;transform:rotate(45deg)}",
      ".topnav.menu-open .nav-menu-bars::after{top:0;transform:rotate(-45deg)}",
      ".nav-menu{position:absolute;top:100%;left:0;right:0;z-index:80;",
      "flex-direction:column;align-items:stretch;gap:4px;padding:8px;",
      "background:var(--panel,#1d232b);border-bottom:1px solid var(--edge,#2c3542);",
      "box-shadow:var(--shadow,0 4px 16px rgba(0,0,0,.45))}",
      ".topnav.menu-open .nav-menu{display:flex}",
      ".topnav .nav-menu a,.topnav .nav-menu .nav-share{min-height:44px;display:flex;align-items:center}",
      ".topnav .nav-menu [hidden]{display:none!important}",
      ".topnav .nav-menu .nav-who{display:block;max-width:none;margin:6px 8px 2px;",
      "white-space:normal;overflow:visible;text-overflow:clip}",
      "}"
    ].join("");
    (global.document.head || global.document.documentElement).appendChild(s);
  }

  // Page links collapse into one menu on a phone. Theme and Logout stay in the bar
  // because those are the two controls you still need with the menu closed.
  // Share and the signed-in name move into the menu — they are what wrapped the
  // bar onto extra rows. Version stays beside the theme toggle.
  function wireCompactNav() {
    var nav = global.document.querySelector("nav.topnav");
    if (!nav || nav.getAttribute("data-compact-nav") === "1") return;
    nav.setAttribute("data-compact-nav", "1");
    ensureCompactNavCss();

    var links = Array.prototype.slice.call(nav.querySelectorAll(":scope > a"));
    var menuBtn = global.document.createElement("button");
    menuBtn.type = "button";
    menuBtn.className = "nav-menu-btn";
    menuBtn.setAttribute("aria-label", "Menu");
    menuBtn.setAttribute("aria-expanded", "false");
    menuBtn.setAttribute("aria-controls", "nav-menu");
    menuBtn.innerHTML = '<span class="nav-menu-bars" aria-hidden="true"></span>';

    var panel = global.document.createElement("div");
    panel.id = "nav-menu";
    panel.className = "nav-menu";

    var logo = nav.querySelector(".nav-logo");
    var brand = nav.querySelector(".brand");
    var after = logo || brand;
    if (after && after.nextSibling) nav.insertBefore(menuBtn, after.nextSibling);
    else nav.insertBefore(menuBtn, nav.firstChild);
    nav.appendChild(panel);

    var mq = global.matchMedia(COMPACT_MQ);
    var layingOut = false;

    function closeMenu() {
      nav.classList.remove("menu-open");
      menuBtn.setAttribute("aria-expanded", "false");
    }

    function openMenu() {
      if (!mq.matches) return;
      nav.classList.add("menu-open");
      menuBtn.setAttribute("aria-expanded", "true");
    }

    function shareEl() { return global.document.getElementById("nav-share"); }
    function whoEl() { return global.document.getElementById("nav-who"); }

    function compactReady() {
      if (!mq.matches) return false;
      var i;
      for (i = 0; i < links.length; i++) {
        if (links[i].parentNode !== panel) return false;
      }
      var want = [];
      if (shareEl()) want.push(shareEl());
      if (whoEl()) want.push(whoEl());
      var seen = [];
      var n;
      for (n = panel.firstChild; n; n = n.nextSibling) {
        if (n.id === "nav-share" || n.id === "nav-who") seen.push(n);
      }
      if (seen.length !== want.length) return false;
      for (i = 0; i < want.length; i++) {
        if (seen[i] !== want[i]) return false;
      }
      return true;
    }

    function desktopReady() {
      if (mq.matches) return false;
      var theme = global.document.getElementById("theme-toggle");
      var share = shareEl();
      var who = whoEl();
      var logout = global.document.getElementById("nav-logout");
      var version = global.document.getElementById("nav-version");
      var i;
      for (i = 0; i < links.length; i++) {
        if (links[i].parentNode !== nav) return false;
      }
      var beforeExtras = share && share.parentNode === nav ? share : theme;
      if (links.length && beforeExtras && links[links.length - 1].nextSibling !== beforeExtras) {
        return false;
      }
      if (share && theme && share.nextSibling !== theme) return false;
      if (version && theme && theme.nextSibling !== version) return false;
      if (who && who.parentNode !== nav) return false;
      if (who && logout && who.nextSibling !== logout) return false;
      return true;
    }

    function layoutNav() {
      if (layingOut) return;
      if (compactReady() || desktopReady()) return;
      layingOut = true;
      try {
        var theme = global.document.getElementById("theme-toggle");
        var share = shareEl();
        var who = whoEl();
        var logout = global.document.getElementById("nav-logout");
        var version = global.document.getElementById("nav-version");
        if (mq.matches) {
          links.forEach(function (a) { panel.appendChild(a); });
          if (share) panel.appendChild(share);
          if (who) panel.appendChild(who);
        } else {
          closeMenu();
          var anchor = share && share.parentNode === nav ? share : theme;
          links.forEach(function (a) {
            if (anchor) nav.insertBefore(a, anchor);
            else nav.appendChild(a);
          });
          if (share && theme) nav.insertBefore(share, theme);
          if (version && theme) {
            if (theme.nextSibling) nav.insertBefore(version, theme.nextSibling);
            else nav.appendChild(version);
          }
          if (who && logout) nav.insertBefore(who, logout);
          else if (who) nav.appendChild(who);
        }
      } finally {
        layingOut = false;
      }
    }

    menuBtn.addEventListener("click", function () {
      if (nav.classList.contains("menu-open")) closeMenu();
      else openMenu();
    });
    panel.addEventListener("click", function (ev) {
      var t = ev.target;
      if (t && t.closest && t.closest("a, button")) closeMenu();
    });
    global.document.addEventListener("click", function (ev) {
      if (!nav.classList.contains("menu-open")) return;
      if (nav.contains(ev.target)) return;
      closeMenu();
    });
    // Capture so Escape closes the menu before theater treats it as "exit fill".
    global.document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      if (!nav.classList.contains("menu-open")) return;
      ev.preventDefault();
      ev.stopPropagation();
      closeMenu();
      menuBtn.focus();
    }, true);
    if (mq.addEventListener) mq.addEventListener("change", layoutNav);
    else if (mq.addListener) mq.addListener(layoutNav);

    layoutNav();
    var obs = new MutationObserver(function () { layoutNav(); });
    obs.observe(nav, { childList: true });
  }

  function wirePlayerTools() {
    var btn = global.document.getElementById("tools-toggle");
    if (!btn) return;
    function sync() {
      var on = global.document.documentElement.classList.contains("player-tools-open");
      btn.setAttribute("aria-expanded", on ? "true" : "false");
    }
    sync();
    btn.addEventListener("click", function () {
      var on = !global.document.documentElement.classList.contains("player-tools-open");
      setPlayerToolsOpen(on);
      sync();
    });
  }

  onReady(function () {
    syncToggle();
    wireCompactNav();
    wirePlayerTools();
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
    wireTheaterControls();
  });

  // Canonical name matches NexVueAuth / NexVueVu / NexVueCaptions. Keep
  // NexVUEUI as a compat alias (older pages / cached HTML).
  var api = {
    getTheme: getTheme,
    setTheme: setTheme,
    toggleTheme: toggleTheme,
    refreshNavLogo: refreshNavLogo,
    refreshVersion: refreshVersion,
    getRotatePref: getRotatePref,
    setRotatePref: setRotatePref,
    applyVideoOrient: applyVideoOrient,
    getTheaterPref: getTheaterPref,
    setTheaterPref: setTheaterPref,
    applyTheater: applyTheater,
    isTheater: isTheater,
    setTheater: setTheater,
    toggleTheater: toggleTheater,
    pipSupported: pipSupported,
    isPipActive: isPipActive,
    requestPip: requestPip,
    exitPip: exitPip,
    togglePip: togglePip,
    channelBase: channelBase,
    channelLabel: channelLabel,
    channelTitle: channelTitle,
    channelLabels: channelLabels,
    unitLabel: unitLabel,
    getChannelAliases: getChannelAliases,
    setChannelAliases: setChannelAliases,
    applyAliasPayload: applyAliasPayload,
    maxChannels: maxChannels,
    setMaxChannels: setMaxChannels,
    loadChannelAliases: loadChannelAliases,
    STORAGE_KEY: STORAGE_KEY,
    ROTATE_KEY: ROTATE_KEY,
    THEATER_KEY: THEATER_KEY,
    LOGO_SRC: LOGO_SRC,
  };
  global.NexVueUI = api;
  global.NexVUEUI = api;
})(typeof window !== "undefined" ? window : globalThis);
