/**
 * nexvue-portal-auth-gate.js — NexAPP session gate + viewer JWT helper.
 *
 * Usage (after nexvue-ui.js):
 *   await NexVuePortal.requirePage({ roles: ['org_admin'] });
 *   const { jwt, whep_url } = await NexVuePortal.viewerJwt(stationId, 'ch0');
 */
(function (global) {
  "use strict";

  var BASE = "/nexvue";
  var API_URL = BASE + "/api/portal";
  var _user = null;

  function api(action, body) {
    var opts = {
      method: body ? "POST" : "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: {},
    };
    var url = API_URL + "?action=" + encodeURIComponent(action);
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(Object.assign({ action: action }, body));
    }
    return fetch(url, opts).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok || !data || data.ok === false) {
          var err = new Error((data && data.error) || ("HTTP " + res.status));
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  /** Hub GET /logout.php is 405. POST csrf from the hub session, else scrape the hub shell. */
  function hubLogoutAction(data) {
    var action = (data && data.logout_url) || "/logout.php";
    try {
      var url = new URL(action, global.location.href);
      if (url.origin === global.location.origin) {
        return url.pathname + url.search;
      }
    } catch (e) {}
    return action;
  }

  function csrfFromHubHtml(html) {
    var meta = /<meta\s+name="csrf-token"\s+content="([^"]+)"/i.exec(html || "");
    if (meta && meta[1]) {
      return meta[1];
    }
    var input = /<input\s+[^>]*name="csrf"\s+[^>]*value="([^"]+)"/i.exec(html || "");
    return (input && input[1]) || "";
  }

  function submitHubLogout(action, csrf, ret) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = action;
    var csrfInput = document.createElement("input");
    csrfInput.type = "hidden";
    csrfInput.name = "csrf";
    csrfInput.value = csrf;
    form.appendChild(csrfInput);
    var retInput = document.createElement("input");
    retInput.type = "hidden";
    retInput.name = "return";
    retInput.value = ret || "/login.php";
    form.appendChild(retInput);
    (document.body || document.documentElement).appendChild(form);
    form.submit();
  }

  function finishHubSignOut(data) {
    data = data || {};
    var action = hubLogoutAction(data);
    var ret = data.return_to || "/login.php";
    if (data.csrf) {
      submitHubLogout(action, data.csrf, ret);
      return;
    }
    return fetch("/portal.php", {
      credentials: "same-origin",
      cache: "no-store",
      redirect: "follow",
    }).then(function (res) {
      return res.text();
    }).then(function (html) {
      var scraped = csrfFromHubHtml(html);
      if (!scraped) {
        global.location.href = "/login.php";
        return;
      }
      submitHubLogout(action, scraped, ret);
    });
  }

  function applyNav(user) {
    var role = user && user.role;
    document.querySelectorAll("[data-auth-role]").forEach(function (el) {
      var need = (el.getAttribute("data-auth-role") || "").split(",").map(function (s) {
        return s.trim();
      }).filter(Boolean);
      el.hidden = need.indexOf(role) < 0;
    });
    var who = document.getElementById("nav-who");
    if (who && user) {
      who.textContent = (user.email || user.username || "") +
        (user.catalog_role === "admin" ? " · admin" : "");
      who.hidden = false;
    }
    var logout = document.getElementById("nav-logout");
    if (logout) {
      logout.hidden = false;
      logout.onclick = function (ev) {
        ev.preventDefault();
        api("logout", {}).then(function (data) {
          return finishHubSignOut(data);
        }).catch(function () {
          global.location.href = "/login.php";
        });
      };
    }
    var themeBtn = document.getElementById("theme-toggle");
    if (themeBtn && global.NexAppTheme && typeof global.NexAppTheme.cycle === "function") {
      themeBtn.onclick = function () { global.NexAppTheme.cycle(); };
    }
  }

  /**
   * @param {object} opts
   * @param {string[]} [opts.roles] required portal roles
   */
  function requirePage(opts) {
    opts = opts || {};
    var roles = opts.roles || null;
    return api("me").then(function (data) {
      _user = data.user || null;
      if (!_user) {
        throw Object.assign(new Error("unauthorized"), { status: 401 });
      }
      if (roles && roles.length && roles.indexOf(_user.role) < 0) {
        global.location.href = BASE + "/catalog";
        return Promise.reject(new Error("forbidden"));
      }
      applyNav(_user);
      return _user;
    }).catch(function (err) {
      if (err && err.status === 403) {
        global.location.href = BASE + "/access";
        return Promise.reject(err);
      }
      global.location.href = BASE + "/login";
      return Promise.reject(err);
    });
  }

  function viewerJwt(stationId, channelBase) {
    return api("viewer_jwt", { station_id: stationId, channel_base: channelBase });
  }

  function stationSso(stationId) {
    return api("station_sso", { station_id: stationId });
  }

  global.NexVuePortal = {
    BASE: BASE,
    api: api,
    requirePage: requirePage,
    viewerJwt: viewerJwt,
    stationSso: stationSso,
  };
})(typeof window !== "undefined" ? window : globalThis);
