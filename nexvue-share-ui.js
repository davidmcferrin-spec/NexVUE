/**
 * nexvue-share-ui.js — Share button + create/list dialogs for Player/Multiview.
 *
 * Requires nexvue-auth-gate.js (NexVueAuth). Bootstrap share icon (MIT):
 * https://icons.getbootstrap.com/icons/share/
 *
 *   NexVueShareUI.mount({
 *     page: "player" | "multiview",
 *     getDefaultChannels: function () { return ["ch0"]; },
 *     getAllowedChannels: function () { return user.channels || []; },
 *   });
 */
(function (global) {
  "use strict";

  var SHARE_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="currentColor" viewBox="0 0 16 16" aria-hidden="true">' +
    '<path d="M13.5 1a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3M11 2.5a2.5 2.5 0 1 1 .603 1.628l-6.718 3.12a2.5 2.5 0 0 1 0 1.504l6.718 3.12a2.5 2.5 0 1 1-.488.876l-6.718-3.12a2.5 2.5 0 1 1 0-3.256l6.718-3.12A2.5 2.5 0 0 1 11 2.5m-8.5 4a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3m11 5.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3"/>' +
    "</svg>";

  function injectCss() {
    if (document.getElementById("nexvue-share-ui-css")) return;
    var s = document.createElement("style");
    s.id = "nexvue-share-ui-css";
    s.textContent =
      ".topnav .nav-share{display:inline-flex;align-items:center;gap:6px;" +
      "background:var(--bg);color:var(--text);border:1px solid var(--edge);" +
      "border-radius:4px;padding:6px 10px;font:inherit;font-size:12px;cursor:pointer;}" +
      ".topnav .nav-share:hover{border-color:var(--acc);color:var(--acc);}" +
      ".topnav .nav-share[hidden]{display:none;}" +
      "dialog.nv-share-dlg{border:1px solid var(--edge);border-radius:8px;" +
      "background:var(--panel);color:var(--text);padding:0;max-width:520px;" +
      "width:calc(100% - 24px);}" +
      "dialog.nv-share-dlg::backdrop{background:rgba(0,0,0,.55);}" +
      ".nv-share-inner{padding:16px;}" +
      ".nv-share-inner h3{margin:0 0 12px;font-size:15px;}" +
      ".nv-share-inner label{display:block;font-size:12px;color:var(--muted);margin:8px 0 4px;}" +
      ".nv-share-inner input,.nv-share-inner select{width:100%;background:var(--bg);color:var(--text);" +
      "border:1px solid var(--edge);border-radius:4px;padding:7px 9px;font:inherit;box-sizing:border-box;}" +
      ".nv-share-inner input[type=checkbox]{width:auto;margin:0;padding:0;accent-color:var(--acc);}" +
      ".nv-share-ch{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;font-size:12px;}" +
      ".nv-share-ch label{display:flex;gap:4px;align-items:center;margin:0;color:var(--text);}" +
      ".nv-share-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px;}" +
      ".nv-share-actions button{background:var(--bg);color:var(--text);border:1px solid var(--edge);" +
      "border-radius:4px;padding:6px 10px;font:inherit;font-size:12px;cursor:pointer;}" +
      ".nv-share-actions button.primary{background:var(--acc);color:var(--on-acc);border-color:var(--acc);font-weight:600;}" +
      ".nv-share-actions button.danger{color:var(--bad);border-color:var(--bad);}" +
      ".nv-share-err{color:var(--bad);font-size:12px;margin-top:8px;}" +
      ".nv-share-once{background:var(--bg);border:1px dashed var(--acc);padding:8px;margin-top:8px;" +
      "font-size:11px;word-break:break-all;}" +
      ".nv-share-list{margin-top:14px;border-top:1px solid var(--edge);padding-top:10px;}" +
      ".nv-share-list h4{margin:0 0 8px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}" +
      ".nv-share-list table{width:100%;border-collapse:collapse;font-size:11px;}" +
      ".nv-share-list th,.nv-share-list td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--edge);vertical-align:top;}" +
      ".nv-share-list th{color:var(--muted);}";
    document.head.appendChild(s);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function basePath(path) {
    var p = String(path || "").toLowerCase();
    if (/^ch[0-7]lo$/.test(p)) return p.slice(0, -2);
    return p;
  }

  function mount(opts) {
    opts = opts || {};
    if (!global.NexVueAuth || !NexVueAuth.canShare || !NexVueAuth.canShare()) {
      return null;
    }
    injectCss();

    var page = opts.page === "multiview" ? "multiview" : "player";
    var getDefaultChannels =
      typeof opts.getDefaultChannels === "function"
        ? opts.getDefaultChannels
        : function () {
            return [];
          };
    var getAllowedChannels =
      typeof opts.getAllowedChannels === "function"
        ? opts.getAllowedChannels
        : function () {
            var u = NexVueAuth.currentUser();
            return (u && u.channels) || [];
          };

    var themeBtn = document.getElementById("theme-toggle");
    var nav = document.querySelector(".topnav");
    if (!nav || !themeBtn) return null;

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nav-share";
    btn.id = "nav-share";
    btn.title = "Create a share link for selected channels";
    btn.innerHTML = SHARE_SVG + " Share";
    nav.insertBefore(btn, themeBtn);

    var dlg = document.createElement("dialog");
    dlg.className = "nv-share-dlg";
    dlg.id = "nv-share-dlg";
    dlg.innerHTML =
      '<div class="nv-share-inner">' +
      "<h3>Share link</h3>" +
      '<label for="nv-share-name">Name</label>' +
      '<input id="nv-share-name" maxlength="128" required>' +
      "<label>Channels (one or more)</label>" +
      '<div class="nv-share-ch" id="nv-share-channels"></div>' +
      '<label for="nv-share-mode">Expiry</label>' +
      '<select id="nv-share-mode">' +
      '<option value="duration">Duration from now</option>' +
      '<option value="absolute">Absolute date/time</option>' +
      "</select>" +
      '<div id="nv-share-duration-wrap">' +
      '<label for="nv-share-duration">Duration</label>' +
      '<select id="nv-share-duration">' +
      '<option value="3600">1 hour</option>' +
      '<option value="21600">6 hours</option>' +
      '<option value="86400" selected>24 hours</option>' +
      '<option value="604800">7 days</option>' +
      '<option value="custom">Custom hours…</option>' +
      "</select>" +
      '<label for="nv-share-hours" id="nv-share-hours-lab" hidden>Custom hours</label>' +
      '<input id="nv-share-hours" type="number" min="1" step="1" hidden>' +
      "</div>" +
      '<div id="nv-share-abs-wrap" hidden>' +
      '<label for="nv-share-expires">Expires at (local)</label>' +
      '<input id="nv-share-expires" type="datetime-local">' +
      "</div>" +
      '<div class="nv-share-err" id="nv-share-err"></div>' +
      '<div class="nv-share-once" id="nv-share-once" hidden></div>' +
      '<div class="nv-share-actions">' +
      '<button type="button" id="nv-share-cancel">Close</button>' +
      '<button type="button" class="primary" id="nv-share-save">Create</button>' +
      "</div>" +
      '<div class="nv-share-list">' +
      "<h4>My shares</h4>" +
      "<table><thead><tr><th>Name</th><th>Channels</th><th>Expires</th><th>Status</th><th></th></tr></thead>" +
      '<tbody id="nv-share-body"></tbody></table>' +
      "</div>" +
      "</div>";
    document.body.appendChild(dlg);

    var chBox = dlg.querySelector("#nv-share-channels");
    var allowed = getAllowedChannels();
    if (!Array.isArray(allowed) || allowed.length === 0) {
      allowed = [];
      for (var i = 0; i < 8; i++) allowed.push("ch" + i);
    }
    allowed = allowed
      .map(basePath)
      .filter(function (c, idx, arr) {
        return /^ch[0-7]$/.test(c) && arr.indexOf(c) === idx;
      })
      .sort();
    allowed.forEach(function (c) {
      var lab = document.createElement("label");
      lab.innerHTML = '<input type="checkbox" value="' + esc(c) + '"> ' + esc(c);
      chBox.appendChild(lab);
    });

    function setDefaults() {
      var defaults = (getDefaultChannels() || []).map(basePath);
      var set = {};
      defaults.forEach(function (c) {
        set[c] = true;
      });
      chBox.querySelectorAll("input").forEach(function (inp) {
        inp.checked = !!set[inp.value];
      });
      if (!chBox.querySelector("input:checked") && chBox.querySelector("input")) {
        chBox.querySelector("input").checked = true;
      }
    }

    function loadList() {
      var tb = dlg.querySelector("#nv-share-body");
      tb.innerHTML = "<tr><td colspan='5'>Loading…</td></tr>";
      return NexVueAuth.api("shares_list")
        .then(function (data) {
          tb.innerHTML = "";
          var shares = data.shares || [];
          if (!shares.length) {
            tb.innerHTML = "<tr><td colspan='5'>No shares yet.</td></tr>";
            return;
          }
          shares.forEach(function (s) {
            var tr = document.createElement("tr");
            tr.innerHTML =
              "<td>" +
              esc(s.name) +
              "</td><td>" +
              esc((s.channels || []).join(", ")) +
              "</td><td>" +
              esc(s.expires_at) +
              "</td><td>" +
              esc(s.status) +
              "</td><td></td>";
            var td = tr.lastElementChild;
            if (s.status === "active") {
              var rev = document.createElement("button");
              rev.type = "button";
              rev.className = "danger";
              rev.textContent = "Revoke";
              rev.onclick = function () {
                if (!confirm('Revoke share "' + s.name + '"?')) return;
                NexVueAuth.api("share_revoke", { id: s.id })
                  .then(loadList)
                  .catch(function (e) {
                    alert(e.message || e);
                  });
              };
              td.appendChild(rev);
            }
            tb.appendChild(tr);
          });
        })
        .catch(function (e) {
          tb.innerHTML =
            "<tr><td colspan='5'>" + esc(e.message || "failed") + "</td></tr>";
        });
    }

    dlg.querySelector("#nv-share-mode").onchange = function () {
      var abs = this.value === "absolute";
      dlg.querySelector("#nv-share-duration-wrap").hidden = abs;
      dlg.querySelector("#nv-share-abs-wrap").hidden = !abs;
    };
    dlg.querySelector("#nv-share-duration").onchange = function () {
      var custom = this.value === "custom";
      dlg.querySelector("#nv-share-hours").hidden = !custom;
      dlg.querySelector("#nv-share-hours-lab").hidden = !custom;
    };
    dlg.querySelector("#nv-share-cancel").onclick = function () {
      dlg.close();
    };

    dlg.querySelector("#nv-share-save").onclick = function () {
      var err = dlg.querySelector("#nv-share-err");
      var once = dlg.querySelector("#nv-share-once");
      err.textContent = "";
      once.hidden = true;
      var channels = Array.from(chBox.querySelectorAll("input:checked")).map(
        function (c) {
          return c.value;
        }
      );
      if (!channels.length) {
        err.textContent = "select at least one channel";
        return;
      }
      var body = {
        name: dlg.querySelector("#nv-share-name").value,
        channels: channels,
        page: page === "multiview" ? "multiview" : "player",
      };
      if (dlg.querySelector("#nv-share-mode").value === "absolute") {
        var local = dlg.querySelector("#nv-share-expires").value;
        if (!local) {
          err.textContent = "expiry required";
          return;
        }
        body.expires_at = new Date(local).toISOString();
      } else {
        var dur = dlg.querySelector("#nv-share-duration").value;
        if (dur === "custom") {
          var h = parseInt(dlg.querySelector("#nv-share-hours").value, 10);
          if (!h || h < 1) {
            err.textContent = "custom hours required";
            return;
          }
          body.duration_s = h * 3600;
        } else {
          body.duration_s = parseInt(dur, 10);
        }
      }
      NexVueAuth.api("share_create", body)
        .then(function (data) {
          var url = data.share && data.share.url ? data.share.url : "";
          once.hidden = false;
          once.textContent = "Copy now (token shown once): " + url;
          if (url && navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).catch(function () {});
          }
          loadList();
        })
        .catch(function (e) {
          err.textContent = e.message || "create failed";
        });
    };

    btn.onclick = function () {
      dlg.querySelector("#nv-share-name").value = "";
      dlg.querySelector("#nv-share-err").textContent = "";
      dlg.querySelector("#nv-share-once").hidden = true;
      setDefaults();
      dlg.showModal();
      loadList();
    };

    return { button: btn, dialog: dlg, reload: loadList };
  }

  global.NexVueShareUI = { mount: mount };
})(window);
