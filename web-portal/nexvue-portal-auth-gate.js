/**
 * nexvue-portal-auth-gate.js — session gate + viewer JWT helper for the
 * NexVUE cloud portal (Phase 4). Simpler than the edge's nexvue-auth-gate.js
 * — no share-link redemption here, just portal_users sessions.
 *
 * Usage (after nexvue-ui.js):
 *   await NexVuePortal.requirePage({ roles: ['org_admin'] });
 *   const { jwt, whep_url } = await NexVuePortal.viewerJwt(stationId, 'ch0');
 */
(function (global) {
  "use strict";

  var API_URL = "/api/portal";
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

  function redirectLogin(next) {
    var q = next ? ("?next=" + encodeURIComponent(next)) : "";
    global.location.href = "/login" + q;
  }

  function me() {
    return api("me").then(function (data) {
      _user = data.user || null;
      return _user;
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
    if (who && user && user.username) {
      who.textContent = user.username + " (" + user.org_name + ")";
      who.hidden = false;
    }
    var logout = document.getElementById("nav-logout");
    if (logout) {
      logout.hidden = false;
      logout.onclick = function (ev) {
        ev.preventDefault();
        api("logout", {}).finally(function () {
          global.location.href = "/login";
        });
      };
    }
  }

  /**
   * @param {object} opts
   * @param {string[]} [opts.roles] required roles (any authenticated portal user if omitted)
   */
  function requirePage(opts) {
    opts = opts || {};
    var roles = opts.roles || null;
    var next = global.location.pathname + global.location.search;

    return me().catch(function () {
      return null;
    }).then(function (user) {
      if (!user) {
        redirectLogin(next);
        return Promise.reject(new Error("unauthorized"));
      }
      if (user.must_change_password) {
        global.location.href = "/login?change=1&next=" + encodeURIComponent(next);
        return Promise.reject(new Error("must_change_password"));
      }
      if (roles && roles.length && roles.indexOf(user.role) < 0) {
        global.location.href = "/catalog";
        return Promise.reject(new Error("forbidden"));
      }
      applyNav(user);
      return user;
    });
  }

  function viewerJwt(stationId, channelBase) {
    return api("viewer_jwt", { station_id: stationId, channel_base: channelBase });
  }

  global.NexVuePortal = {
    api: api,
    me: me,
    requirePage: requirePage,
    viewerJwt: viewerJwt,
  };
})(typeof window !== "undefined" ? window : globalThis);
