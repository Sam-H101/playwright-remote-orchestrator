(function () {
  "use strict";

  var REFRESH_MS = 5000;
  var state = { data: null, paused: false, view: "overview", sort: { key: "started", dir: "asc" }, slot: null };
  var tokenRequired = document.body.dataset.tokenRequired === "true";
  var $ = function (id) { return document.getElementById(id); };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function hostLabel(host) { return host || "(unnamed host)"; }

  function duration(seconds) {
    if (seconds == null) return "–";
    var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + s + "s";
    return s + "s";
  }

  function when(iso) {
    if (!iso) return "–";
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  }

  function slotClass(slot, threshold) {
    if (slot.fails >= threshold) return "bad";
    if (slot.busy) return "busy";
    if (slot.fails > 0) return "warn";
    return "";
  }

  // ---- data -------------------------------------------------------------

  function load() {
    return fetch("api/status", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("Status request failed (" + r.status + ")");
        return r.json();
      })
      .then(function (data) {
        state.data = data;
        $("error-banner").hidden = true;
        render();
      })
      .catch(function (err) {
        $("error-banner").textContent = "Could not reach the orchestrator database: " + err.message;
        $("error-banner").hidden = false;
      });
  }

  function token() {
    try { return sessionStorage.getItem("gridToken") || ""; } catch (e) { return ""; }
  }

  function askToken() {
    return new Promise(function (resolve) {
      var dlg = $("token-dialog");
      $("token-input").value = token();
      dlg.addEventListener("close", function onClose() {
        dlg.removeEventListener("close", onClose);
        if (dlg.returnValue === "ok") {
          try { sessionStorage.setItem("gridToken", $("token-input").value); } catch (e) { /* ignore */ }
          resolve(true);
        } else {
          resolve(false);
        }
      });
      dlg.showModal();
    });
  }

  function act(url, body) {
    var ready = tokenRequired && !token() ? askToken() : Promise.resolve(true);
    return ready.then(function (ok) {
      if (!ok) return;
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Grid-Token": token() },
        body: JSON.stringify(body || {})
      }).then(function (r) {
        if (r.status === 403) {
          try { sessionStorage.removeItem("gridToken"); } catch (e) { /* ignore */ }
          throw new Error("The admin token was rejected.");
        }
        if (!r.ok) throw new Error("Action failed (" + r.status + ")");
        return load();
      }).catch(function (err) { alert(err.message); });
    });
  }

  // ---- rendering --------------------------------------------------------

  function render() {
    var d = state.data;
    if (!d) return;
    var t = d.totals;
    $("queue-size").textContent = d.queue_size == null ? "n/a" : d.queue_size;
    $("cap-text").textContent = t.busy + " / " + t.slots + " slots in use";
    $("cap-fill").style.width = (t.slots ? (100 * t.busy / t.slots) : 0) + "%";
    $("session-badge").textContent = d.sessions.length;
    $("threshold").textContent = d.fail_threshold;
    renderTiles(d);
    renderNodes(d);
    renderSessions(d);
  }

  function renderTiles(d) {
    var t = d.totals;
    var tiles = [
      ["Nodes", t.nodes],
      ["Total slots", t.slots],
      ["Sessions", t.busy],
      ["Free slots", t.free],
      ["Failing slots", t.failing],
      ["Stale sessions", t.stale],
      ["Queue", d.queue_size == null ? "n/a" : d.queue_size]
    ];
    $("tiles").innerHTML = tiles.map(function (x) {
      return '<div class="tile"><div class="label">' + esc(x[0]) + '</div><div class="value">' + esc(x[1]) + "</div></div>";
    }).join("");
  }

  function renderNodes(d) {
    var q = $("node-filter").value.trim().toLowerCase();
    var onlyProblems = $("only-problems").checked;
    var nodes = d.nodes.filter(function (n) {
      if (q && hostLabel(n.host).toLowerCase().indexOf(q) === -1) return false;
      if (onlyProblems && !n.failing) return false;
      return true;
    });
    if (!nodes.length) {
      $("nodes").innerHTML = '<p class="empty">' + (d.nodes.length ? "No nodes match the filter." :
        "No nodes registered. Add hosts to src/database.yaml and run main.py with --fr true.") + "</p>";
      return;
    }
    $("nodes").innerHTML = nodes.map(function (n, i) {
      var pct = n.total ? (100 * n.busy / n.total) : 0;
      var ports = n.slots.length ? n.slots[0].port + "–" + n.slots[n.slots.length - 1].port : "none";
      var cells = n.slots.map(function (s) {
        return '<button class="slot ' + slotClass(s, d.fail_threshold) + '" data-node="' + i + '" data-slot="' + s.id +
          '" title="Port ' + s.port + (s.busy ? " · in use" : "") + (s.fails ? " · " + s.fails + " failures" : "") + '"></button>';
      }).join("");
      return '<article class="node">' +
        '<div class="node-head"><div>' +
          '<div class="node-host' + (n.host ? "" : " unnamed") + '">' + esc(hostLabel(n.host)) + "</div>" +
          '<div class="node-sub">Ports ' + esc(ports) + " · last activity " + esc(when(n.last_activity)) + "</div>" +
        '</div><span class="pill ' + n.status + '">' + n.status + "</span></div>" +
        '<div class="node-stats">' +
          "<div>Sessions<strong>" + n.busy + "</strong></div>" +
          "<div>Max concurrency<strong>" + n.total + "</strong></div>" +
          "<div>Free<strong>" + n.free + "</strong></div>" +
          "<div>Failing<strong>" + n.failing + "</strong></div>" +
        "</div>" +
        '<div class="meter"><div class="meter-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="slots">' + cells + "</div>" +
        '<div class="node-foot"><button class="btn" data-reset-node="' + i + '"' + (n.slots.some(function (s) { return s.fails; }) ? "" : " disabled") +
          ">Reset node failures</button></div>" +
        "</article>";
    }).join("");
    state.visibleNodes = nodes;
  }

  function renderSessions(d) {
    var q = $("session-filter").value.trim().toLowerCase();
    var key = state.sort.key, dir = state.sort.dir === "asc" ? 1 : -1;
    var rows = d.sessions.filter(function (s) {
      return !q || [s.host, s.port, s.endpoint].join(" ").toLowerCase().indexOf(q) !== -1;
    }).sort(function (a, b) {
      var x = a[key], y = b[key];
      if (x == null) return 1;
      if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * dir;
    });
    document.querySelectorAll(".sessions th[data-sort]").forEach(function (th) {
      if (th.dataset.sort === key) th.dataset.dir = state.sort.dir; else delete th.dataset.dir;
    });
    $("sessions-empty").hidden = rows.length > 0;
    $("sessions-empty").textContent = d.sessions.length ? "No sessions match the filter." : "No sessions are running right now.";
    $("session-rows").innerHTML = rows.map(function (s) {
      return '<tr' + (s.stale ? ' class="stale" title="Running longer than ' + duration(d.stale_after_s) + '; the check may have died without releasing the port"' : "") + ">" +
        '<td class="num">' + s.slot_id + "</td>" +
        "<td>" + esc(hostLabel(s.host)) + "</td>" +
        '<td class="num">' + s.port + "</td>" +
        "<td><code>" + esc(s.endpoint) + "</code></td>" +
        "<td>" + esc(when(s.started)) + "</td>" +
        '<td class="num">' + duration(s.duration_s) + (s.stale ? ' <span class="tag">stale</span>' : "") + "</td>" +
        '<td class="num">' + s.fails + "</td>" +
        '<td><button class="btn danger" data-release="' + s.slot_id + '">Force release</button></td>' +
        "</tr>";
    }).join("");
  }

  function openSlot(nodeIndex, slotId) {
    var node = state.visibleNodes[nodeIndex];
    var slot = node.slots.find(function (s) { return s.id === slotId; });
    if (!slot) return;
    state.slot = slot;
    var session = state.data.sessions.find(function (s) { return s.slot_id === slotId; });
    $("slot-title").textContent = hostLabel(node.host) + ":" + slot.port;
    var rows = [
      ["Slot id", slot.id],
      ["Endpoint", "ws://" + node.host + ":" + slot.port],
      ["State", slot.busy ? "In use" : "Free"],
      ["Failures", slot.fails + (slot.fails >= state.data.fail_threshold ? " (failing)" : "")]
    ];
    if (session) {
      rows.push(["Started", when(session.started)], ["Duration", duration(session.duration_s)]);
    }
    $("slot-details").innerHTML = rows.map(function (r) {
      return "<dt>" + esc(r[0]) + "</dt><dd>" + esc(r[1]) + "</dd>";
    }).join("");
    document.querySelector('#slot-dialog [data-action="release"]').disabled = !slot.busy;
    document.querySelector('#slot-dialog [data-action="reset"]').disabled = !slot.fails;
    $("slot-dialog").showModal();
  }

  // ---- events -----------------------------------------------------------

  document.querySelectorAll(".tabs button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.view = btn.dataset.view;
      document.querySelectorAll(".tabs button").forEach(function (b) {
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      $("view-overview").hidden = state.view !== "overview";
      $("view-sessions").hidden = state.view !== "sessions";
    });
  });

  $("nodes").addEventListener("click", function (e) {
    var slot = e.target.closest(".slot");
    if (slot) return openSlot(Number(slot.dataset.node), Number(slot.dataset.slot));
    var reset = e.target.closest("[data-reset-node]");
    if (reset) {
      var node = state.visibleNodes[Number(reset.dataset.resetNode)];
      if (confirm("Reset failure counts for every slot on " + hostLabel(node.host) + "?")) {
        act("api/nodes/reset-failures", { host: node.host });
      }
    }
  });

  $("session-rows").addEventListener("click", function (e) {
    var btn = e.target.closest("[data-release]");
    if (btn && confirm("Force release slot " + btn.dataset.release + "? Only do this if the check is no longer running.")) {
      act("api/slots/" + btn.dataset.release + "/release");
    }
  });

  document.querySelector("#slot-dialog").addEventListener("click", function (e) {
    var action = e.target.dataset && e.target.dataset.action;
    if (!action || !state.slot) return;
    var id = state.slot.id;
    $("slot-dialog").close();
    if (action === "release") {
      if (confirm("Force release this slot? Only do this if the check is no longer running.")) act("api/slots/" + id + "/release");
    } else {
      act("api/slots/" + id + "/reset-failures");
    }
  });

  document.querySelectorAll(".sessions th[data-sort]").forEach(function (th) {
    th.addEventListener("click", function () {
      var key = th.dataset.sort;
      state.sort = { key: key, dir: state.sort.key === key && state.sort.dir === "asc" ? "desc" : "asc" };
      if (state.data) renderSessions(state.data);
    });
  });

  ["node-filter", "only-problems"].forEach(function (id) {
    $(id).addEventListener("input", function () { if (state.data) renderNodes(state.data); });
  });
  $("session-filter").addEventListener("input", function () { if (state.data) renderSessions(state.data); });

  $("pause-btn").addEventListener("click", function () {
    state.paused = !state.paused;
    this.setAttribute("aria-pressed", String(state.paused));
    $("pause-label").textContent = state.paused ? "Paused" : "Live";
    if (!state.paused) load();
  });

  setInterval(function () {
    if (!state.paused && !document.hidden && !$("slot-dialog").open) load();
  }, REFRESH_MS);
  load();
})();
