/**
 * nexvue-auth-gate.js — session gate, share-token redeem, WHEP JWT helper.
 *
 * Usage (after nexvue-ui.js):
 *   await NexVueAuth.requirePage({ roles: ['admin','operator'], allowShare: false });
 *   const jwt = await NexVueAuth.whepJwt('ch0');
 *   // WHEP URL: `${whepBase()}/${path}/whep?jwt=${encodeURIComponent(jwt)}`
 */
(function (global) {
  "use strict";

  var AUTH_URL = "nexvue-auth.php";
  var _user = null;

  function api(action, body) {
    var opts = {
      method: body ? "POST" : "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: {},
    };
    var url = AUTH_URL + "?action=" + encodeURIComponent(action);
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
    global.location.href = "/login.html" + q;
  }

  function shareTokenFromLocation() {
    try {
      var u = new URL(global.location.href);
      var t = u.searchParams.get("t");
      if (t) return t;
      // /s/<token> path form
      var m = u.pathname.match(/\/s\/([A-Za-z0-9]+)/);
      return m ? m[1] : null;
    } catch (e) {
      return null;
    }
  }

  function me() {
    return api("me").then(function (data) {
      _user = data.user || null;
      return _user;
    });
  }

  function redeemShare(token) {
    return api("share_redeem", { token: token }).then(function (data) {
      _user = data.user || null;
      return _user;
    });
  }

  /**
   * @param {object} opts
   * @param {string[]} [opts.roles] required roles when auth=user (share never matches unless allowShare)
   * @param {boolean} [opts.allowShare=false]
   * @param {boolean} [opts.allowViewer=true] for player pages
   */
  function requirePage(opts) {
    opts = opts || {};
    var roles = opts.roles || null;
    var allowShare = !!opts.allowShare;
    var next = global.location.pathname + global.location.search;

    var token = shareTokenFromLocation();
    var chain = Promise.resolve();
    if (token) {
      chain = redeemShare(token).catch(function () {
        return null;
      });
    }

    return chain.then(function () {
      return me().catch(function () {
        return null;
      });
    }).then(function (user) {
      if (!user) {
        redirectLogin(next);
        return Promise.reject(new Error("unauthorized"));
      }
      if (user.must_change_password) {
        global.location.href = "/login.html?change=1&next=" + encodeURIComponent(next);
        return Promise.reject(new Error("must_change_password"));
      }
      if (user.auth === "share") {
        if (!allowShare) {
          redirectLogin(next);
          return Promise.reject(new Error("forbidden"));
        }
        applyNav(user);
        return user;
      }
      if (roles && roles.length && roles.indexOf(user.role) < 0) {
        global.location.href = "/index.html";
        return Promise.reject(new Error("forbidden"));
      }
      applyNav(user);
      return user;
    });
  }

  function applyNav(user) {
    var role = user && user.role;
    var isShare = user && user.auth === "share";
    document.querySelectorAll("[data-auth-role]").forEach(function (el) {
      var need = (el.getAttribute("data-auth-role") || "").split(",").map(function (s) {
        return s.trim();
      }).filter(Boolean);
      var ok = !isShare && need.indexOf(role) >= 0;
      el.hidden = !ok;
    });
    var logout = document.getElementById("nav-logout");
    if (logout) {
      logout.hidden = false;
      logout.onclick = function (ev) {
        ev.preventDefault();
        api("logout", {}).finally(function () {
          global.location.href = "/login.html";
        });
      };
    }
    var who = document.getElementById("nav-who");
    if (who) {
      if (isShare) {
        who.textContent = "share:" + (user.name || "");
      } else if (user && user.username) {
        var roleLabel = user.role === "sharer" ? "Viewer+Share" : user.role;
        who.textContent = user.username + " (" + roleLabel + ")";
      }
      who.hidden = false;
    }
  }

  function whepJwt(path) {
    return api("whep_jwt", { path: path }).then(function (data) {
      return data.jwt;
    });
  }

  function channelAllowed(path, user) {
    user = user || _user;
    if (!user) return true;
    var channels = user.channels;
    // Share sessions and local users with an ACL both use channels[].
    // null channels on a user (legacy) → treat as all allowed.
    if (user.auth !== "share" && (channels == null || channels === undefined)) {
      return true;
    }
    if (!Array.isArray(channels) || channels.length === 0) return false;
    var p = String(path || "").toLowerCase();
    var base = /^ch[0-7]lo$/.test(p) ? p.slice(0, -2) : p;
    for (var i = 0; i < channels.length; i++) {
      var c = String(channels[i] || "").toLowerCase();
      if (c === p || c === base) return true;
      if (/^ch[0-7]lo$/.test(c) && c.slice(0, -2) === base) return true;
    }
    return false;
  }

  function canShare(user) {
    user = user || _user;
    if (!user || user.auth === "share") return false;
    return user.role === "admin" || user.role === "sharer";
  }

  function currentUser() {
    return _user;
  }

  global.NexVueAuth = {
    api: api,
    me: me,
    requirePage: requirePage,
    whepJwt: whepJwt,
    channelAllowed: channelAllowed,
    canShare: canShare,
    currentUser: currentUser,
    shareTokenFromLocation: shareTokenFromLocation,
    redeemShare: redeemShare,
  };
})(window);
