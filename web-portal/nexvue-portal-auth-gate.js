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
          global.location.href = (data && data.redirect) || "/logout.php";
        }).catch(function () {
          global.location.href = "/logout.php";
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
