(function () {
  "use strict";

  var REFRESH_MS = 5000;
  var state = {
    data: null, sig: null, error: null, lastOk: 0, skew: 0, loading: false, paused: false,
    view: "overview", scope: "all", sort: { key: "started", dir: "asc" }, slotId: null
  };
  var tokenRequired = document.body.dataset.tokenRequired === "true";
  var $ = function (id) { return document.getElementById(id); };

  var STATUS_TONE = { ONLINE: "ok", UP: "ok", DEGRADED: "warn", DOWN: "bad", OFFLINE: "off" };
  var STATE_LABEL = { free: "Free", busy: "In use", unhealthy: "Unhealthy", failing: "Failing", offline: "Node offline" };
  var PLATFORMS = { win32: ["Windows", "i-windows"], linux: ["Linux", "i-linux"], darwin: ["macOS", "i-mac"] };

  // ---- formatting ---------------------------------------------------------

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function icon(id) { return '<svg aria-hidden="true"><use href="#' + id + '"/></svg>'; }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function cap(s) { return s.charAt(0) + s.slice(1).toLowerCase(); }

  // Server timestamps are UTC ("...Z"). Ages are measured against the server's clock, so a
  // skewed browser clock doesn't make things look stale (or seen in the future).
  function serverNow() { return Date.now() + state.skew; }
  function secondsSince(iso) { return iso ? Math.max(0, Math.round((serverNow() - Date.parse(iso)) / 1000)) : null; }

  function duration(s) {
    if (s == null) return "–";
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h" + (m ? " " + m + "m" : "");
    if (m) return m + "m" + (s % 60 ? " " + (s % 60) + "s" : "");
    return s + "s";
  }

  function ago(s) {
    if (s == null) return "never";
    if (s < 5) return "just now";
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  function clock(iso) {
    if (!iso) return { time: "–", day: "" };
    var d = new Date(iso), today = new Date();
    var yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    var day = d.toDateString() === today.toDateString() ? "Today" :
      d.toDateString() === yesterday.toDateString() ? "Yesterday" :
      d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return { time: d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }), day: day };
  }

  function platform(p) {
    var known = PLATFORMS[p];
    return known ? known : [p || "Unknown platform", "i-server"];
  }

  function stateLabel(slot) { return STATE_LABEL[slot.state] || slot.state; }

  // A failing slot is skipped until its cooldown ends; then the checks give it another try.
  function retryIn(iso) {
    var s = Math.round((Date.parse(iso) - serverNow()) / 1000);
    if (!(s > 0)) return "retry due";
    return "retry in " + (s < 60 ? s + "s" : Math.ceil(s / 60) + "m");
  }

  // ---- data -----------------------------------------------------------------

  // Heartbeats change last_seen every few seconds; leave it out so they don't rebuild the page
  // (and drop the user's text selection). refreshSeen() updates it in place instead.
  function signature(data) {
    return JSON.stringify(data, function (k, v) {
      return k === "generated" || k === "last_seen_s" || k === "duration_s" || k === "last_seen" ? undefined : v;
    });
  }

  function refreshSeen(data) {
    var seen = {};
    data.nodes.forEach(function (n) { seen[n.host] = n.last_seen || ""; });
    document.querySelectorAll("[data-seen]").forEach(function (el) {
      var iso = seen[el.dataset.seen];
      if (iso == null || el.dataset.ago === iso) return;
      el.dataset.ago = iso;
      if (el.parentNode) el.parentNode.title = iso ? new Date(iso).toLocaleString() : "";
    });
    tick();
  }

  function load() {
    if (state.loading) return Promise.resolve();
    state.loading = true;
    return fetch("api/status", { cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) throw new Error(body.error || "The status request failed with HTTP " + r.status + ".");
          return body;
        });
      })
      .then(function (data) {
        state.skew = Date.parse(data.generated) - Date.now();
        state.lastOk = Date.now();
        state.error = null;
        var sig = signature(data);
        state.data = data;
        if (sig !== state.sig) {
          state.sig = sig;
          render();
        } else {
          refreshSeen(data);
          renderBanner();
        }
      })
      .catch(function (err) {
        state.error = err.message || String(err);
        render();
      })
      .then(function () { state.loading = false; });
  }

  // ---- admin actions --------------------------------------------------------

  function token() {
    try { return sessionStorage.getItem("gridToken") || ""; } catch (e) { return ""; }
  }

  function setToken(value) {
    try {
      if (value) sessionStorage.setItem("gridToken", value); else sessionStorage.removeItem("gridToken");
    } catch (e) { /* storage blocked: the token lives only as long as this call */ }
  }

  function askToken(rejected) {
    return new Promise(function (resolve) {
      var dlg = $("token-dialog");
      $("token-text").textContent = rejected ? "That token was rejected. Enter the grid's admin token to continue." :
        "This grid needs a token for management actions. It's kept for this browser tab only.";
      $("token-input").value = "";
      dlg.returnValue = "";
      dlg.addEventListener("close", function onClose() {
        dlg.removeEventListener("close", onClose);
        var ok = dlg.returnValue === "ok" && $("token-input").value !== "";
        if (ok) setToken($("token-input").value);
        resolve(ok ? $("token-input").value : null);
      });
      dlg.showModal();
    });
  }

  // opts.items: [{ text, tone }] listed under the text; opts.note: a last line.
  function confirmBox(title, text, okLabel, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dlg = $("confirm-dialog"), list = $("confirm-list"), items = opts.items || [];
      $("confirm-title").textContent = title;
      $("confirm-text").textContent = text;
      $("confirm-ok").textContent = okLabel;
      list.textContent = "";
      items.forEach(function (item) {
        var li = document.createElement("li"), dot = document.createElement("span");
        dot.className = "dot " + (item.tone || "off");
        li.appendChild(dot);
        li.appendChild(document.createTextNode(item.text));
        if (item.label) li.title = item.label;
        list.appendChild(li);
      });
      list.hidden = !items.length;
      $("confirm-note").textContent = opts.note || "";
      $("confirm-note").hidden = !opts.note;
      dlg.returnValue = "";
      dlg.addEventListener("close", function onClose() {
        dlg.removeEventListener("close", onClose);
        resolve(dlg.returnValue === "ok");
      });
      dlg.showModal();
    });
  }

  // A management POST. Resolves with the response body, or null when the user cancels the token
  // prompt; rejects with an Error that carries the response body.
  function post(url, body, rejected) {
    var ready = tokenRequired && (!token() || rejected) ? askToken(rejected) : Promise.resolve(token());
    return ready.then(function (t) {
      if (t === null) return null;
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Grid-Token": t || "" },
        body: JSON.stringify(body || {})
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (res) {
          // Only ask again when the grid says the token is the problem. Any other 403 (an origin
          // check, a proxy) is an error: retrying it without asking would loop forever.
          if (r.status === 403 && res.token_required) {
            setToken("");
            tokenRequired = true; // the page may predate GRID_ADMIN_TOKEN being set
            return post(url, body, !!t);
          }
          if (!r.ok) {
            var err = new Error(res.error || "The action failed with HTTP " + r.status + ".");
            err.body = res;
            throw err;
          }
          return res;
        });
      });
    });
  }

  function act(url, body, done) {
    return post(url, body).then(function (res) {
      if (res === null) return;
      toast(done);
      return load();
    }).catch(function (err) { toast(err.message, true); });
  }

  var toastTimer;
  function toast(text, bad) {
    var el = $("toast");
    el.textContent = text;
    el.className = "toast" + (bad ? " bad" : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, bad ? 6000 : 3000);
  }

  function copy(text, what) {
    var fallback = function () {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { /* unsupported */ }
      ta.remove();
      return ok ? Promise.resolve() : Promise.reject(new Error("Copy isn't available here."));
    };
    var p = navigator.clipboard && window.isSecureContext ? navigator.clipboard.writeText(text).catch(fallback) : fallback();
    p.then(function () { toast("Copied " + (what || text)); }, function (e) { toast(e.message, true); });
  }

  // ---- rendering ------------------------------------------------------------

  function render() {
    // Re-rendering replaces the DOM, so put focus back where it was.
    var active = document.activeElement, key = active && active.dataset && active.dataset.key;
    renderBanner();
    var d = state.data;
    if (d) {
      $("session-count").textContent = d.sessions.length;
      renderStats(d);
      renderNodes(d);
      renderSessions(d);
      renderSlotDialog();
    } else if (state.error) {
      $("node-toolbar").hidden = true;
      $("nodes").innerHTML = emptyState("Can't reach the database",
        "The grid reads the orchestrator's database and keeps retrying every few seconds. The details are above.", "");
    }
    if (key) {
      var again = document.querySelector('[data-key="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
      if (again) again.focus();
    }
    tick();
  }

  function renderBanner() {
    var banner = $("banner"), live = $("live-btn");
    live.classList.toggle("error", !!state.error);
    $("live-label").textContent = state.paused ? "Paused" : state.error ? "Retrying" : "Live";
    if (!state.error) { banner.hidden = true; return; }
    var age = state.lastOk ? " Showing data from " + ago(Math.round((Date.now() - state.lastOk) / 1000)) + "." : "";
    banner.innerHTML = "<div><strong>Can't reach the database.</strong> " + esc(age) +
      " Retrying every " + REFRESH_MS / 1000 + " seconds.<code>" + esc(state.error) + "</code></div>";
    banner.hidden = false;
  }

  function stat(label, value, sub, extra, tone) {
    return '<div class="stat"><div class="stat-label">' + (tone ? '<span class="dot ' + tone + '"></span>' : "") + esc(label) +
      '</div><div class="stat-value">' + value + '</div>' + (extra || "") + '<div class="stat-sub">' + sub + "</div></div>";
  }

  function renderStats(d) {
    var t = d.totals;
    var pct = t.slots ? Math.round(100 * t.busy / t.slots) : 0;
    var attention = t.unhealthy + t.failing;
    var nodes = t.gateway ?
      stat("Nodes online", t.online + '<span class="of">/ ' + t.gateway + "</span>",
        esc(t.offline ? t.offline + " offline" : "All reachable") + (t.static ? " · " + esc(plural(t.static, "static host")) : ""),
        "", t.gateway ? (t.offline ? "off" : "ok") : "") :
      stat("Nodes", String(t.nodes), t.static ? esc(plural(t.static, "static host")) : "None registered yet");
    $("stats").innerHTML = [
      nodes,
      stat("Sessions", t.busy + '<span class="of">/ ' + t.slots + "</span>", pct + "% of slots in use",
        '<div class="meter" role="img" aria-label="' + pct + '% of slots in use"><span style="width:' + pct + '%"></span></div>'),
      stat("Available", String(t.available), t.slots && !t.available ? "No slot can take a check" : "Ready for new checks",
        "", t.slots ? (t.available ? "ok" : "bad") : ""),
      stat("Needs attention", String(attention),
        attention ? esc(t.unhealthy + " unhealthy · " + t.failing + " failing") : "All slots healthy",
        "", t.slots ? (attention ? "warn" : "ok") : ""),
      stat("Stale sessions", String(t.stale), "Running over " + duration(d.stale_after_s), "", t.stale ? "warn" : "")
    ].join("");
  }

  function needsAttention(n) { return n.status === "DEGRADED" || n.status === "DOWN"; }

  function inScope(n) {
    if (state.scope === "attention") return needsAttention(n);
    if (state.scope === "offline") return n.status === "OFFLINE";
    return true;
  }

  function emptyState(title, text, body, mark) {
    return '<div class="empty"><svg class="empty-mark" aria-hidden="true"><use href="#' + (mark || "i-server") + '"/></svg><h2>' + esc(title) +
      "</h2><p>" + esc(text) + "</p>" + body + "</div>";
  }

  // The hub URL as a runner host elsewhere sees it: "hub" is only a name on the compose network.
  function hubForHosts(url) {
    return url.replace(/^wss?:\/\/hub(?=[:/]|$)/, function (m) { return m.replace("hub", "<hub-host>"); });
  }

  function startSteps(d) {
    var hub = hubForHosts(d.gateway_url);
    return '<div class="steps">' +
      '<div class="step"><div class="step-label">Docker runners, next to the hub</div><code>' +
        esc("docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2") + "</code></div>" +
      '<div class="step"><div class="step-label">Any Windows, Linux or macOS host (dials out, no inbound ports). ' +
        "The README has the install steps for each.</div><code>" +
        esc("node gateway/agent.js --hub " + hub + " --token <GATEWAY_AGENT_TOKEN> --slots 4") + "</code></div>" +
      "</div>";
  }

  function renderNodes(d) {
    var counts = { all: d.nodes.length, attention: d.nodes.filter(needsAttention).length,
      offline: d.nodes.filter(function (n) { return n.status === "OFFLINE"; }).length };
    document.querySelectorAll("#node-scope button").forEach(function (b) {
      var n = counts[b.dataset.scope], label = b.dataset.label || (b.dataset.label = b.textContent);
      b.innerHTML = esc(label) + (n ? '<span class="n">' + n + "</span>" : "");
      b.setAttribute("aria-pressed", String(b.dataset.scope === state.scope));
    });

    $("node-toolbar").hidden = !d.nodes.length;
    if (!d.db.initialised) {
      $("nodes").innerHTML = emptyState("Waiting for the first node",
        "The orchestrator tables haven't been created yet. The gateway hub creates them when it starts, " +
        "and so does the first check. Nodes show up here as soon as they register; this page refreshes by itself.", startSteps(d));
      return;
    }
    if (!d.nodes.length) {
      $("nodes").innerHTML = emptyState("No nodes yet",
        "Start a runner or an agent and it registers itself with the hub. It appears here within a few seconds.", startSteps(d));
      return;
    }

    var q = $("node-filter").value.trim().toLowerCase();
    var nodes = d.nodes.filter(function (n) {
      if (!inScope(n)) return false;
      if (!q) return true;
      return [n.host, n.platform, platform(n.platform)[0], n.remote_address, n.kind, (n.browsers || []).join(" ")]
        .join(" ").toLowerCase().indexOf(q) !== -1;
    });
    if (!nodes.length) {
      $("nodes").innerHTML = '<div class="empty"><h2>No nodes match</h2><p>' +
        esc(q ? "Nothing matches “" + q + "”" + (state.scope !== "all" ? " in this view." : ".") : "No nodes are in this view right now.") +
        '</p><button class="btn" type="button" data-clear-filter data-key="clear-filter">Show all nodes</button></div>';
      return;
    }

    var groups = [
      ["Gateway nodes", "Connected through " + d.gateway_url, nodes.filter(function (n) { return n.kind === "gateway"; })],
      ["Static hosts", "From hosts: in database.yaml", nodes.filter(function (n) { return n.kind !== "gateway"; })]
    ].filter(function (g) { return g[2].length; });
    $("nodes").innerHTML = groups.map(function (g) {
      return '<section class="group"><div class="group-head"><h2>' + esc(g[0]) + "</h2><span>" + esc(g[1]) + " · " + g[2].length +
        '</span></div><div class="node-grid">' + g[2].map(function (n) { return nodeCard(n, d); }).join("") + "</div></section>";
    }).join("");
  }

  function nodeCard(n, d) {
    var gateway = n.kind === "gateway", tone = STATUS_TONE[n.status] || "off";
    var p = platform(n.platform);
    var meta = gateway ?
      ['<span>' + icon(p[1]) + esc(p[0]) + "</span>", "<span>Gateway node</span>",
        n.playwright_version ? "<span>Playwright " + esc(n.playwright_version) + "</span>" : ""] :
      ["<span>" + icon("i-server") + "Static host</span>",
        n.slots.length ? "<span>Ports " + esc(n.slots[0].port) + "–" + esc(n.slots[n.slots.length - 1].port) + "</span>" : ""];
    var facts = [
      ["Sessions", n.busy + ' <span class="of">/ ' + n.total + "</span>"],
      gateway ? ["Last seen", '<span data-seen="' + esc(n.host) + '" data-ago="' + esc(n.last_seen || "") + '">' + ago(n.last_seen_s) + "</span>",
        n.last_seen ? new Date(n.last_seen).toLocaleString() : ""] :
        ["Last activity", '<span data-ago="' + esc(n.last_activity || "") + '">' + ago(secondsSince(n.last_activity)) + "</span>",
          n.last_activity ? new Date(n.last_activity).toLocaleString() : ""],
      gateway ? ["Browsers", esc((n.browsers || []).join(", ") || "–"), (n.browsers || []).join(", "), "wrap"] :
        ["Failing", n.failing + ' <span class="of">/ ' + n.total + "</span>"]
    ];
    var chips = n.slots.map(function (s) {
      var cls = "slot " + s.state + (s.fails && s.state !== "failing" ? " has-fails" : "");
      var label = (gateway ? "Slot " : "Port ") + s.port + ": " + stateLabel(s) + (s.fails ? ", " + plural(s.fails, "failure") : "");
      // tick() keeps "retry in ..." current for slots in their failure cooldown.
      var retry = s.retry_at ? ' data-retry="' + esc(s.retry_at) + '" data-label="' + esc(label) + '"' : "";
      if (s.retry_at) label += ", " + retryIn(s.retry_at);
      return '<button type="button" class="' + cls + '" data-slot="' + esc(s.id) + '" data-key="slot-' + esc(s.id) + '"' + retry +
        ' title="' + esc(label) + '" aria-label="' + esc(label) + '">' + esc(s.port) + "</button>";
    }).join("");
    // The hub URL is the same for every gateway node, so the group header shows it once.
    var endpoint = gateway ?
      esc("/connect/" + encodeURIComponent(n.host) + "/") + '<span class="var">&lt;slot&gt;</span>' :
      esc("ws://" + n.host + ":") + '<span class="var">&lt;port&gt;</span>';
    var hasFails = n.slots.some(function (s) { return s.fails; });
    var actions = (hasFails ? '<button type="button" class="btn ghost small" data-reset-node="' + esc(n.host) + '" data-key="reset-' + esc(n.host) + '">Reset failures</button>' : "") +
      (gateway && n.status === "OFFLINE" ? '<button type="button" class="btn ghost small danger" data-forget="' + esc(n.host) + '" data-key="forget-' + esc(n.host) + '">Forget</button>' : "");
    var title = n.host + (n.remote_address ? " (" + n.remote_address + ")" : "");
    var tag = n.ephemeral ? '<span class="tag" title="A short-lived node, such as a docker runner. The hub removes it once it disconnects.">ephemeral</span>' : "";
    var note = n.ephemeral && n.status === "OFFLINE" ?
      '<p class="node-note">Ephemeral node: it and its slots are removed automatically.</p>' : "";

    return '<article class="node" data-status="' + esc(n.status) + '">' +
      '<div class="node-body">' +
        '<div class="node-head"><span class="dot ' + tone + '" aria-hidden="true"></span>' +
          '<div class="node-title"><h3 class="node-name" title="' + esc(title) + '">' + esc(n.host) + "</h3>" +
          '<div class="node-meta">' + meta.join("") + "</div></div>" +
          tag + '<span class="pill ' + tone + '">' + cap(n.status) + "</span></div>" +
        '<dl class="facts-row">' + facts.map(function (f) {
          return "<div><dt>" + f[0] + "</dt><dd" + (f[3] ? ' class="' + f[3] + '"' : "") + (f[2] ? ' title="' + esc(f[2]) + '"' : "") + ">" + f[1] + "</dd></div>";
        }).join("") + "</dl>" +
        (chips ? '<div class="slots" aria-label="Slots">' + chips + "</div>" : '<p class="slots-empty">No slots registered.</p>') +
        note +
      "</div>" +
      '<div class="node-foot"><code class="endpoint" title="Endpoint pattern">' + endpoint + "</code>" + actions + "</div>" +
      "</article>";
  }

  function compare(a, b) {
    var key = state.sort.key, x = a[key], y = b[key];
    if (x == null || y == null) return x == null && y == null ? 0 : x == null ? 1 : -1; // nulls last either way
    var r = typeof x === "string" ? x.localeCompare(y, undefined, { numeric: true }) : x - y;
    return state.sort.dir === "asc" ? r : -r;
  }

  function renderSessions(d) {
    var q = $("session-filter").value.trim().toLowerCase();
    var rows = d.sessions.filter(function (s) {
      return !q || [s.host, s.port, s.endpoint].join(" ").toLowerCase().indexOf(q) !== -1;
    }).sort(function (a, b) { return compare(a, b) || a.slot_id - b.slot_id; });

    document.querySelectorAll(".sessions th").forEach(function (th) {
      var btn = th.querySelector("[data-sort]");
      if (btn && btn.dataset.sort === state.sort.key) th.setAttribute("aria-sort", state.sort.dir === "asc" ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });

    var stale = d.sessions.filter(function (s) { return s.stale; }).length;
    $("session-note").textContent = d.sessions.length ?
      plural(d.sessions.length, "session") + " running" + (stale ? " · " + stale + " stale" : "") : "";

    var empty = $("sessions-empty");
    empty.hidden = rows.length > 0;
    empty.innerHTML = d.sessions.length ? "<h2>No sessions match</h2><p>Nothing matches “" + esc(q) + "”.</p>" :
      "<h2>No sessions running</h2><p>A check claims a slot when it starts and frees it when it finishes. Running checks show up here.</p>";
    $("session-rows").innerHTML = rows.map(function (s) {
      var started = clock(s.started);
      var tags = (s.stale ? '<span class="tag warn" title="Running longer than ' + duration(d.stale_after_s) +
        '. The check may have died without releasing its slot.">stale</span>' : "") +
        (s.kind === "gateway" && !s.node_online ? '<span class="tag">node offline</span>' : "");
      // A "legacy" session is a blank-host row from an old config that is still marked in use.
      var kind = s.kind === "gateway" ? "Gateway node" : s.kind === "legacy" ? "Old config row" : "Static host";
      var endpoint = s.endpoint ?
        '<div class="cell-endpoint"><code title="' + esc(s.endpoint) + '">' + esc(s.endpoint) +
          '</code><button type="button" class="copy" data-copy="' + esc(s.endpoint) + '" data-key="copy-' + esc(s.slot_id) + '" aria-label="Copy endpoint">' + icon("i-copy") + "</button></div>" :
        '<span class="zero" title="Never handed out to checks">–</span>';
      return "<tr>" +
        '<td class="cell-node">' + (s.host ? esc(s.host) : "(blank host)") + "<small>" + kind + "</small></td>" +
        '<td class="num cell-slot">' + esc(s.port) + "</td>" +
        '<td class="cell-endpoint-td" data-label="Endpoint">' + endpoint + "</td>" +
        '<td class="cell-time" data-label="Started" title="' + esc(s.started ? new Date(s.started).toLocaleString() : "") + '">' + esc(started.time) + "<small>" + esc(started.day) + "</small></td>" +
        '<td class="num cell-duration" data-label="Duration"><span data-dur="' + esc(s.started || "") + '">' + duration(s.duration_s) + "</span>" + tags + "</td>" +
        '<td class="num cell-fails' + (s.fails ? "" : " zero") + '">' + esc(s.fails) + "</td>" +
        '<td class="cell-actions"><button type="button" class="btn ghost small danger" data-release="' + esc(s.slot_id) + '" data-key="release-' + esc(s.slot_id) + '">Release</button></td>' +
        "</tr>";
    }).join("");
  }

  function findSlot(id) {
    var nodes = state.data ? state.data.nodes : [];
    for (var i = 0; i < nodes.length; i++) {
      for (var j = 0; j < nodes[i].slots.length; j++) {
        if (nodes[i].slots[j].id === id) return { node: nodes[i], slot: nodes[i].slots[j] };
      }
    }
    return null;
  }

  function renderSlotDialog() {
    var dlg = $("slot-dialog");
    if (state.slotId == null) return;
    var found = findSlot(state.slotId);
    if (!found) {
      if (dlg.open) { dlg.close(); toast("That slot no longer exists."); }
      state.slotId = null;
      return;
    }
    var n = found.node, s = found.slot, gateway = n.kind === "gateway";
    var tone = { free: "ok", busy: "ok", unhealthy: "warn", failing: "bad", offline: "off" }[s.state];
    var session = state.data.sessions.find(function (x) { return x.slot_id === s.id; });
    $("slot-eyebrow").innerHTML = '<span class="dot ' + (STATUS_TONE[n.status] || "off") + '"></span>' +
      esc(n.host) + " · " + (gateway ? "Gateway node" : "Static host");
    $("slot-title").textContent = (gateway ? "Slot " : "Port ") + s.port;
    var rows = [
      ["State", '<span class="pill ' + (s.state === "busy" ? "busy" : tone) + '">' + esc(stateLabel(s)) + "</span>"],
      ["Endpoint", '<div class="endpoint-cell"><code>' + esc(s.endpoint).replace(/\//g, "/<wbr>") + '</code><button type="button" class="copy" data-copy="' +
        esc(s.endpoint) + '" data-key="dialog-copy" aria-label="Copy endpoint">' + icon("i-copy") + "</button></div>"]
    ];
    if (gateway) rows.push(["Run-server", s.healthy ? "Healthy" : "Not responding"]);
    rows.push(["Failures", esc(s.fails) + "<small>failing at " + esc(state.data.fail_threshold) + "</small>"]);
    if (s.retry_at) {
      rows.push(["Next try", '<span data-retry="' + esc(s.retry_at) + '">' + esc(retryIn(s.retry_at)) + "</span>" +
        "<small>Checks skip it for " + esc(duration(state.data.fail_cooldown_s)) + " after a failure</small>"]);
    }
    if (session) {
      var c = clock(session.started);
      rows.push(["Started", esc(c.time) + "<small>" + esc(c.day) + "</small>"],
        ["Duration", '<span data-dur="' + esc(session.started || "") + '">' + duration(session.duration_s) + "</span>" +
          (session.stale ? '<span class="tag warn">stale</span>' : "")]);
    } else {
      rows.push(["Last used", s.last_run ? '<span data-ago="' + esc(s.last_run) + '">' + ago(secondsSince(s.last_run)) + "</span>" : "Never"]);
    }
    $("slot-details").innerHTML = rows.map(function (r) { return "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>"; }).join("");
    dlg.querySelector('[data-action="release"]').disabled = !s.busy;
    dlg.querySelector('[data-action="reset"]').disabled = !s.fails;
  }

  function openSlot(id) {
    state.slotId = id;
    renderSlotDialog();
    if (state.slotId != null) $("slot-dialog").showModal();
    tick();
  }

  // ---- access tokens --------------------------------------------------------

  // Listing and managing tokens needs the admin token. access.state: idle (the tab hasn't been
  // opened), ok, locked (no admin token entered, or it was rejected), unconfigured (the grid has
  // no GRID_ADMIN_TOKEN, so tokens can't be managed here) or error (and nothing to show).
  var access = {
    state: "idle", data: null, sig: null, error: null, rejected: false, loading: false, queued: false,
    kind: "all", status: "all", reveal: null
  };
  var TOKEN_TONE = { active: "ok", expired: "warn", revoked: "off" };
  var TOKEN_RANK = { active: 0, expired: 1, revoked: 2 };
  var KIND_LABEL = { agent: "Agent", client: "Client" };
  var TOKEN_ENV = { agent: "GATEWAY_AGENT_TOKEN", client: "GATEWAY_CLIENT_TOKEN" };
  var NODE_NAME = /^[a-z0-9._-]{1,128}$/;

  // prompt: the user is here (opening the tab, "Enter admin token"), so ask for the admin token
  // when none is stored, and again if it's rejected. The background refresh never asks: a
  // refusal leaves the tab locked until the user acts, so a 403 can't loop.
  function loadTokens(prompt) {
    if (access.loading) { access.queued = access.queued || !prompt; return Promise.resolve(); }
    access.loading = true;
    var before = access.state + "|" + access.sig + "|" + access.error, again = false;
    var ready = prompt && tokenRequired && !token() ? askToken(access.rejected) : Promise.resolve(token());
    return ready.then(function (t) {
      if (t === null) { access.state = "locked"; access.data = access.sig = null; return; }
      return fetch("api/tokens", { cache: "no-store", headers: { Accept: "application/json", "X-Grid-Token": t || "" } })
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (body) {
            if (r.status === 403 && body.admin_token_unset) {
              access.state = "unconfigured";
              access.data = access.sig = access.error = null;
              return;
            }
            if (r.status === 403 && body.token_required) {
              setToken("");
              tokenRequired = true;
              access.rejected = !!t;
              access.state = "locked";
              access.data = access.sig = access.error = null;
              again = prompt;
              return;
            }
            if (!r.ok) throw new Error(body.error || "Loading the access tokens failed with HTTP " + r.status + ".");
            access.state = "ok";
            access.error = null;
            access.rejected = false;
            access.data = body;
            access.sig = JSON.stringify(body, function (k, v) { return k === "generated" ? undefined : v; });
          });
        });
    }).catch(function (err) {
      access.error = err.message || String(err);
      if (!access.data) access.state = "error";
    }).then(function () {
      access.loading = false;
      // Unchanged data: leave the DOM alone (and the user's selection); tick() ages "last used".
      if (access.state + "|" + access.sig + "|" + access.error !== before) renderAccess(); else tick();
      if (again) return loadTokens(true);
      if (access.queued) { access.queued = false; return loadTokens(false); }
    });
  }

  function shortDate(iso) {
    var d = new Date(iso);
    return d.toLocaleDateString(undefined, d.getFullYear() === new Date().getFullYear() ?
      { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
  }

  function fullDate(iso) { return iso ? new Date(iso).toLocaleString() : ""; }

  function inTime(s) {
    if (s < 3600) return "in " + Math.max(1, Math.round(s / 60)) + "m";
    if (s < 86400) return "in " + Math.round(s / 3600) + "h";
    return "in " + Math.round(s / 86400) + "d";
  }

  function expiresCell(t) {
    if (!t.expiresAt) return '<span class="zero">Never</span>';
    var s = Math.round((Date.parse(t.expiresAt) - serverNow()) / 1000);
    if (s <= 0) return "<span>" + esc(ago(-s)) + "</span>";
    if (s > 30 * 86400) return esc(shortDate(t.expiresAt));
    return '<span class="' + (t.status === "active" && s < 7 * 86400 ? "soon" : "") + '">' + esc(inTime(s)) + "</span>";
  }

  function nodeRefs(t) {
    if (t.kind !== "agent") return '<span class="zero" title="Client tokens aren\'t tied to nodes">–</span>';
    if (!t.nodes.length) return '<span class="zero">No nodes</span>';
    var online = t.nodesOnline || [];
    var shown = t.nodes.slice(0, 2).map(function (n) {
      var on = online.indexOf(n) !== -1, label = n + (on ? " (online)" : " (offline)");
      return '<span class="node-ref" title="' + esc(label) + '"><span class="dot ' + (on ? "ok" : "off") + '" aria-hidden="true"></span>' +
        esc(n) + '<span class="sr-only">' + (on ? " (online)" : " (offline)") + "</span></span>";
    }).join("");
    var more = t.nodes.length > 2 ? '<span class="more" title="' + esc(t.nodes.slice(2).join(", ")) + '">+' + (t.nodes.length - 2) + " more</span>" : "";
    return '<div class="node-refs">' + shown + more + "</div>";
  }

  function tokenRow(t) {
    var bound = t.kind !== "agent" ? '<span class="zero">–</span>' :
      t.nodeName ? '<code class="bound">' + esc(t.nodeName) + "</code>" : '<span class="zero">Any node</span>';
    var action = t.status === "active" ?
      '<button type="button" class="btn ghost small danger" data-revoke="' + esc(t.id) + '" data-key="revoke-' + esc(t.id) + '">Revoke</button>' :
      '<button type="button" class="btn ghost small danger" data-delete-token="' + esc(t.id) + '" data-key="delete-' + esc(t.id) + '">Delete</button>';
    return '<tr data-token="' + esc(t.id) + '" data-status="' + esc(t.status) + '">' +
      '<td class="cell-token"><span class="tok-name">' + esc(t.name) + '</span><code class="tok-prefix" title="The token starts with these characters">' +
        esc(t.prefix) + "…</code></td>" +
      '<td class="cell-kind" data-label="Kind"><span class="kind ' + esc(t.kind) + '">' + esc(KIND_LABEL[t.kind] || t.kind) + "</span></td>" +
      // Client tokens aren't tied to nodes; "na" cells are left out of the narrow card layout.
      '<td class="cell-bound' + (t.kind !== "agent" ? " na" : "") + '" data-label="Bound to">' + bound + "</td>" +
      '<td class="cell-used' + (t.kind !== "agent" ? " na" : "") + '" data-label="Used by">' + nodeRefs(t) + "</td>" +
      '<td class="cell-created" data-label="Created" title="' + esc(fullDate(t.createdAt)) + '">' + esc(t.createdAt ? shortDate(t.createdAt) : "–") + "</td>" +
      '<td class="cell-last" data-label="Last used" title="' + esc(fullDate(t.lastUsedAt)) + '">' +
        (t.lastUsedAt ? '<span data-ago="' + esc(t.lastUsedAt) + '">' + ago(secondsSince(t.lastUsedAt)) + "</span>" : '<span class="zero">Never</span>') + "</td>" +
      '<td class="cell-expires" data-label="' + (t.status === "expired" ? "Expired" : "Expires") + '" title="' + esc(fullDate(t.expiresAt)) + '">' + expiresCell(t) + "</td>" +
      '<td class="cell-status"><span class="pill ' + (TOKEN_TONE[t.status] || "off") + '">' + esc(cap(t.status)) + "</span></td>" +
      '<td class="cell-actions">' + action + "</td>" +
      "</tr>";
  }

  function tokenShown(t, q, kind, status) {
    if (kind !== "all" && t.kind !== kind) return false;
    if (status !== "all" && t.status !== status) return false;
    return !q || [t.name, t.prefix, t.nodeName, t.kind].concat(t.nodes).join(" ").toLowerCase().indexOf(q) !== -1;
  }

  function renderTokens(d) {
    var q = $("token-filter").value.trim().toLowerCase();
    // Each filter's counts follow the other filter, so they say what a click would show.
    document.querySelectorAll("#token-kind button").forEach(function (b) {
      var n = d.tokens.filter(function (t) { return tokenShown(t, "", b.dataset.kind, access.status); }).length;
      var label = b.dataset.label || (b.dataset.label = b.textContent);
      b.innerHTML = esc(label) + (n ? '<span class="n">' + n + "</span>" : "");
      b.setAttribute("aria-pressed", String(b.dataset.kind === access.kind));
    });
    document.querySelectorAll("#token-status button").forEach(function (b) {
      var n = d.tokens.filter(function (t) { return tokenShown(t, "", access.kind, b.dataset.status); }).length;
      var label = b.dataset.label || (b.dataset.label = b.textContent);
      b.innerHTML = esc(label) + (n ? '<span class="n">' + n + "</span>" : "");
      b.setAttribute("aria-pressed", String(b.dataset.status === access.status));
    });

    var active = d.tokens.filter(function (t) { return t.status === "active"; }).length;
    var env = ["agent", "client"].filter(function (k) { return d.bootstrap && d.bootstrap[k]; });
    $("token-note").textContent = plural(active, "active token") +
      (env.length ? " · the hub also accepts " + env.map(function (k) { return TOKEN_ENV[k]; }).join(" and ") : "");

    var rows = d.tokens.filter(function (t) { return tokenShown(t, q, access.kind, access.status); }).sort(function (a, b) {
      return (TOKEN_RANK[a.status] - TOKEN_RANK[b.status]) || (Date.parse(b.createdAt) - Date.parse(a.createdAt)) || b.id - a.id;
    });
    var empty = $("tokens-empty");
    empty.hidden = rows.length > 0;
    empty.innerHTML = "<h2>No tokens match</h2><p>" + esc(q ? "Nothing matches “" + q + "” with these filters." : "No tokens have this kind and status.") +
      '</p><button class="btn" type="button" data-clear-token-filter data-key="clear-token-filter">Show all tokens</button>';
    $("token-rows").innerHTML = rows.map(tokenRow).join("");
  }

  function accessState() {
    var d = access.data;
    switch (access.state) {
      case "idle": return "";
      case "unconfigured":
        return emptyState("Set an admin token to manage access tokens",
          "Access tokens are managed with the grid's admin token, and only its holders can see them: names, bindings and usage are sensitive too. " +
          "Until it's set, the hub keeps relying on its env tokens.",
          '<div class="steps">' +
            '<div class="step"><div class="step-label">Set it in .env next to docker-compose.yaml, or in the Grid UI\'s environment</div><code>' +
              esc("GRID_ADMIN_TOKEN=<a long random secret>") + "</code></div>" +
            '<div class="step"><div class="step-label">Then restart the Grid UI and enter the same token here</div><code>' +
              esc("docker compose up -d grid_ui") + "</code></div>" +
          "</div>", "i-lock");
      case "locked":
        return emptyState(access.rejected ? "That admin token was rejected" : "Admin token needed",
          "Access tokens are managed with the grid's admin token. It's kept for this browser tab only.",
          '<button class="btn primary" type="button" data-unlock data-key="unlock">' + icon("i-lock") + "Enter admin token</button>", "i-lock");
      case "error":
        return emptyState("Can't load the access tokens", access.error,
          '<button class="btn" type="button" data-reload-tokens data-key="reload-tokens">Try again</button>', "i-key");
    }
    if (!d.ready) {
      return emptyState("Access tokens aren't set up in this database yet",
        "It has no accessTokens table. The gateway hub creates it when it starts, and so does the Grid UI on MySQL " +
        "when its database user may create tables. Until then the hub relies on its env tokens.", "", "i-key");
    }
    return emptyState("No access tokens yet",
      "Until you create one, the hub relies on its env tokens (GATEWAY_AGENT_TOKEN and GATEWAY_CLIENT_TOKEN). " +
      "Create one per group of hosts, so you can revoke it without touching the others.",
      '<button class="btn primary" type="button" data-new-token data-key="new-token-empty">' + icon("i-plus") + "New token</button>", "i-key");
  }

  function renderAccess() {
    var active = document.activeElement, key = active && active.dataset && active.dataset.key;
    var d = access.state === "ok" ? access.data : null, list = !!(d && d.ready && d.tokens.length);
    $("new-token-btn").hidden = !list;
    $("token-toolbar").hidden = !list;
    $("token-panel").hidden = !list;
    $("access-state").innerHTML = list ? "" : accessState();
    if (list) renderTokens(d);
    var banner = $("access-banner");
    banner.hidden = !(access.error && access.state === "ok");
    if (!banner.hidden) {
      banner.innerHTML = "<div><strong>Can't refresh the access tokens.</strong> Showing the last list. Retrying every " +
        REFRESH_MS / 1000 + " seconds.<code>" + esc(access.error) + "</code></div>";
    }
    if (key) {
      var again = document.querySelector('[data-key="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
      if (again) again.focus();
    }
    tick();
  }

  function renderReveal(fresh) {
    var r = access.reveal, box = $("reveal");
    if (!r) { box.innerHTML = ""; return; }
    var hub = state.data ? state.data.gateway_url : "ws://hub:4500", remote = hubForHosts(hub);
    var uses = r.kind === "agent" ? [
      ["Windows host: the installer in installer\\windows",
        ".\\install-backend.ps1 -HubUrl " + remote + " -AgentToken " + r.token + (r.nodeName ? " -NodeName " + r.nodeName : "")],
      ["Linux or macOS host: the agent, from the repository",
        "GATEWAY_AGENT_TOKEN=" + r.token + " node gateway/agent.js --hub " + remote + (r.nodeName ? " --name " + r.nodeName : "")],
      ["Docker runner: its environment, e.g. runner.env",
        "GATEWAY_AGENT_TOKEN=" + r.token + (r.nodeName ? "\nNODE_NAME=" + r.nodeName : "")]
    ] : [
      ["Check containers: their environment (example_playwright in docker-compose.yaml)", "GATEWAY_CLIENT_TOKEN=" + r.token],
      ["Other Playwright clients: on the endpoint (or as an x-gateway-token header)",
        hub + "/connect/<node>/<slot>?gateway_token=" + r.token]
    ];
    var facts = [KIND_LABEL[r.kind] + " token", r.name, r.nodeName ? "bound to " + r.nodeName : "",
      r.expiresAt ? "expires " + shortDate(r.expiresAt) : "never expires"].filter(Boolean);
    box.innerHTML = '<section class="reveal" aria-labelledby="reveal-title">' +
      '<div class="reveal-head"><span class="reveal-mark">' + icon("i-key") + "</span>" +
        '<div class="reveal-title"><h2 id="reveal-title">Copy your new token now</h2><p>' + esc(facts.join(" · ")) + "</p></div>" +
        '<button type="button" class="btn" data-reveal-done data-key="reveal-done">Done</button></div>' +
      '<div class="secret"><code id="reveal-token">' + esc(r.token) + "</code>" +
        '<button type="button" class="btn primary small" data-copy="' + esc(r.token) + '" data-copy-label="the token" data-key="reveal-copy" aria-describedby="reveal-warn">' +
        icon("i-copy") + "Copy</button></div>" +
      '<p class="reveal-warn" id="reveal-warn">' + icon("i-alert") +
        "<span>It won't be shown again. The grid keeps only a hash of it: if it's lost, revoke it and create another.</span></p>" +
      '<div class="usage"><h3>Use it</h3><div class="usage-grid">' + uses.map(function (u, i) {
        return '<div class="step"><div class="step-label">' + esc(u[0]) + '</div><div class="snippet"><code>' + esc(u[1]) + "</code>" +
          '<button type="button" class="copy" data-copy="' + esc(u[1]) + '" data-copy-label="the command" data-key="reveal-use-' + i +
          '" aria-label="Copy the command for ' + esc(u[0].split(":")[0]) + '">' + icon("i-copy") + "</button></div></div>";
      }).join("") + "</div></div></section>";
    if (fresh) {
      box.querySelector("[data-key=reveal-copy]").focus({ preventScroll: true });
      var top = box.firstChild.getBoundingClientRect().top, bar = document.querySelector(".top").getBoundingClientRect().bottom;
      if (top < bar || top > innerHeight / 2) box.firstChild.scrollIntoView({ block: "start" }); // only when it's out of sight
    }
  }

  function findToken(id) {
    var tokens = access.data ? access.data.tokens : [];
    for (var i = 0; i < tokens.length; i++) if (String(tokens[i].id) === String(id)) return tokens[i];
    return null;
  }

  // The hub keeps asking for a token once a managed one of that kind exists, revoked or not. Revoking the
  // last usable one therefore locks out everything without the env token (the UI only knows about that
  // when it was given the same variable), and deleting the last row lets tokenless ones back in after the
  // next hub restart. `usable` limits the count to tokens that still work.
  function lastOfKind(t, usable) {
    var d = access.data;
    return !(d.bootstrap && d.bootstrap[t.kind]) && !d.tokens.some(function (o) {
      return o.id !== t.id && o.kind === t.kind && (!usable || o.status === "active");
    });
  }

  function tokenAction(url, done) {
    return post(url, {}).then(function (res) {
      if (res === null) return;
      toast(done);
      return loadTokens(false);
    }).catch(function (err) {
      if (err.body && err.body.admin_token_unset) { access.state = "unconfigured"; access.data = access.sig = null; renderAccess(); }
      toast(err.message, true);
      return loadTokens(false);
    });
  }

  function confirmRevoke(id) {
    var t = findToken(id);
    if (!t) return;
    var agent = t.kind === "agent", online = t.nodesOnline || [];
    var text = !agent ? "Checks that use it can't open new browser sessions. Sessions already running carry on." :
      t.nodes.length ? "These nodes last registered with it. The ones connected now are disconnected within about 15 seconds, and none of them can reconnect with it:" :
      "No node has registered with it yet. Nothing can register with it afterwards.";
    var note = "This can't be undone." + (lastOfKind(t, true) ? " It's the last working " + t.kind + " token: the hub keeps asking for one, so " +
      "unless it has " + TOKEN_ENV[t.kind] + " set, no " + (agent ? "agent" : "check") + " gets in until you create a new token." : "");
    return confirmBox("Revoke “" + t.name + "”?", text, "Revoke token", {
      items: agent ? t.nodes.map(function (n) {
        var on = online.indexOf(n) !== -1;
        return { text: n, tone: on ? "ok" : "off", label: on ? "Online: disconnected now" : "Offline" };
      }) : [],
      note: note
    }).then(function (ok) { if (ok) tokenAction("api/tokens/" + encodeURIComponent(id) + "/revoke", "Revoked “" + t.name + "”."); });
  }

  function confirmDeleteToken(id) {
    var t = findToken(id);
    if (!t) return;
    // Any row of a kind, revoked or expired, keeps the hub asking for that kind of token; with the last
    // one gone, a restarted hub stops asking unless its env token is set.
    var note = lastOfKind(t, false) ? "It's the last " + t.kind + " token on record: unless the hub has " +
      TOKEN_ENV[t.kind] + " set, after its next restart it accepts " + (t.kind === "agent" ? "agents" : "checks") +
      " without a token again. Create a new " + t.kind + " token first to keep it closed." : "";
    return confirmBox("Delete “" + t.name + "”?",
      "It's already " + t.status + ", so the hub turns it away. Deleting it removes it from this list" +
      (t.nodes.length ? " and from the " + plural(t.nodes.length, "node") + " that last registered with it." : "."), "Delete token",
      { note: note })
      .then(function (ok) { if (ok) tokenAction("api/tokens/" + encodeURIComponent(id) + "/delete", "Deleted “" + t.name + "”."); });
  }

  function createKind() {
    var checked = document.querySelector('#new-token-form input[name="kind"]:checked');
    return checked ? checked.value : "agent";
  }

  function formError(text) {
    $("nt-error").textContent = text || "";
    $("nt-error").hidden = !text;
  }

  function syncCreate() {
    var kind = createKind(), d = access.data;
    $("nt-node-field").hidden = kind !== "agent";
    $("nt-days-wrap").hidden = $("nt-expiry").value !== "custom";
    // The hub starts asking for a token once one of this kind exists, even without its env token.
    var first = d && d.ready && !(d.bootstrap && d.bootstrap[kind]) &&
      !d.tokens.some(function (t) { return t.kind === kind && t.status !== "revoked"; });
    $("nt-first").hidden = !first;
    $("nt-first").textContent = "This is the first " + kind + " token. From then on the hub turns away " +
      (kind === "agent" ? "agents" : "checks") + " that don't send a valid token, even without " + TOKEN_ENV[kind] + ".";
  }

  function openCreate() {
    $("new-token-form").reset();
    ["nt-name", "nt-node", "nt-days"].forEach(function (id) { $(id).removeAttribute("aria-invalid"); });
    formError("");
    var names = state.data ? state.data.nodes.filter(function (n) { return n.kind === "gateway"; }) : [];
    $("node-names").innerHTML = names.map(function (n) { return '<option value="' + esc(n.host.toLowerCase()) + '"></option>'; }).join("");
    syncCreate();
    $("new-token-dialog").showModal();
    $("nt-name").focus();
  }

  // The request body, or null after pointing at the first problem.
  function createBody() {
    var name = $("nt-name").value.trim(), kind = createKind(), node = $("nt-node").value.trim().toLowerCase();
    var expiry = $("nt-expiry").value;
    var days = expiry === "custom" ? Number($("nt-days").value) : expiry ? Number(expiry) : null;
    var problem = !name ? ["nt-name", "Give the token a name, such as the hosts it's for."] :
      kind === "agent" && node && !NODE_NAME.test(node) ? ["nt-node", "A node name is 1 to 128 characters: letters, digits, '.', '_' and '-'."] :
      days !== null && !(Number.isInteger(days) && days >= 1 && days <= 3650) ? ["nt-days", "Pick a whole number of days from 1 to 3650."] : null;
    ["nt-name", "nt-node", "nt-days"].forEach(function (id) { $(id).removeAttribute("aria-invalid"); });
    if (problem) {
      $(problem[0]).setAttribute("aria-invalid", "true");
      formError(problem[1]);
      $(problem[0]).focus();
      return null;
    }
    formError("");
    return { name: name, kind: kind, nodeName: kind === "agent" && node ? node : null, expiresInDays: days };
  }

  function hideReveal() {
    access.reveal = null;
    renderReveal();
    var btn = $("new-token-btn");
    if (!btn.hidden) btn.focus();
  }

  function tick() {
    document.querySelectorAll("[data-retry]").forEach(function (el) {
      var text = retryIn(el.dataset.retry);
      if (el.dataset.label) {
        el.title = el.dataset.label + ", " + text;
        el.setAttribute("aria-label", el.title);
      } else {
        el.textContent = text;
      }
    });
    document.querySelectorAll("[data-ago]").forEach(function (el) {
      if (el.dataset.ago) el.textContent = ago(secondsSince(el.dataset.ago));
    });
    document.querySelectorAll("[data-dur]").forEach(function (el) {
      if (el.dataset.dur) el.textContent = duration(secondsSince(el.dataset.dur));
    });
  }

  // ---- events ---------------------------------------------------------------

  var VIEWS = ["overview", "sessions", "access"];

  function showView(view, focus) {
    var opening = $("view-" + view).hidden; // not already showing
    state.view = view;
    document.querySelectorAll(".tabs [role=tab]").forEach(function (b) {
      var on = b.dataset.view === view;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    VIEWS.forEach(function (v) { $("view-" + v).hidden = view !== v; });
    if (location.hash !== "#" + view) history.replaceState(null, "", view === "overview" ? location.pathname : "#" + view);
    // Opening the tab asks for the admin token if none is stored (unless the grid has none).
    if (view === "access" && opening && access.state !== "unconfigured") loadTokens(true);
  }

  document.querySelectorAll(".tabs [role=tab]").forEach(function (btn) {
    btn.addEventListener("click", function () { showView(btn.dataset.view); });
    btn.addEventListener("keydown", function (e) {
      var step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (step) showView(VIEWS[(VIEWS.indexOf(btn.dataset.view) + step + VIEWS.length) % VIEWS.length], true);
    });
  });

  $("node-scope").addEventListener("click", function (e) {
    var b = e.target.closest("[data-scope]");
    if (!b) return;
    state.scope = b.dataset.scope;
    if (state.data) renderNodes(state.data);
    tick();
  });

  document.addEventListener("click", function (e) {
    var el;
    if ((el = e.target.closest("[data-copy]"))) {
      if (el.closest("#reveal") && access.reveal) access.reveal.copied = true;
      return copy(el.dataset.copy, el.dataset.copyLabel);
    }
    if (e.target.closest("[data-new-token]")) return openCreate();
    if ((el = e.target.closest("[data-revoke]"))) return confirmRevoke(el.dataset.revoke);
    if ((el = e.target.closest("[data-delete-token]"))) return confirmDeleteToken(el.dataset.deleteToken);
    if (e.target.closest("[data-unlock]")) return loadTokens(true);
    if (e.target.closest("[data-reload-tokens]")) return loadTokens(true);
    if (e.target.closest("[data-clear-token-filter]")) {
      $("token-filter").value = "";
      access.kind = access.status = "all";
      renderAccess();
      return;
    }
    if (e.target.closest("[data-reveal-done]")) {
      if (access.reveal && access.reveal.copied) return hideReveal();
      return confirmBox("Hide the token without copying it?",
        "It can't be shown again. If you need it later, revoke it and create another.", "Hide token")
        .then(function (ok) { if (ok) hideReveal(); });
    }
    if ((el = e.target.closest("[data-clear-filter]"))) {
      $("node-filter").value = "";
      state.scope = "all";
      renderNodes(state.data);
      tick();
      return;
    }
    if ((el = e.target.closest(".slot[data-slot]"))) return openSlot(Number(el.dataset.slot));
    if ((el = e.target.closest("[data-reset-node]"))) {
      var host = el.dataset.resetNode;
      return confirmBox("Reset failures on " + host + "?", "Every slot on this node goes back to zero failures.", "Reset failures")
        .then(function (ok) { if (ok) act("api/nodes/reset-failures", { host: host }, "Failures reset on " + host + "."); });
    }
    if ((el = e.target.closest("[data-forget]"))) {
      var name = el.dataset.forget;
      return confirmBox("Forget " + name + "?",
        "This removes the node and its slots from the grid. If its agent starts again, it registers itself again.", "Forget node")
        .then(function (ok) { if (ok) act("api/nodes/forget", { host: name }, name + " was removed."); });
    }
    if ((el = e.target.closest("[data-release]"))) {
      var id = el.dataset.release;
      return confirmRelease(id);
    }
  });

  function confirmRelease(id) {
    return confirmBox("Force release this slot?",
      "Only do this if the check that holds it is no longer running. Another check can claim it straight away.", "Force release")
      .then(function (ok) { if (ok) act("api/slots/" + id + "/release", null, "Slot released."); });
  }

  // The dialog has no padding of its own, so a click whose target is the dialog can still be
  // inside it (its content's margins, a text selection dragged out of the endpoint). Only a
  // press and a release that are both outside its box count as a backdrop click.
  function outside(dlg, e) {
    var r = dlg.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  }
  var pressedBackdrop = false;
  $("slot-dialog").addEventListener("pointerdown", function (e) { pressedBackdrop = e.target === this && outside(this, e); });
  $("slot-dialog").addEventListener("click", function (e) {
    var dlg = this, backdrop = pressedBackdrop && e.target === dlg && outside(dlg, e);
    pressedBackdrop = false;
    if (backdrop || e.target.closest("[data-close]")) return dlg.close();
    var btn = e.target.closest("[data-action]");
    if (!btn || state.slotId == null) return;
    var id = state.slotId;
    dlg.close();
    if (btn.dataset.action === "release") confirmRelease(id);
    else act("api/slots/" + id + "/reset-failures", null, "Failures reset.");
  });
  $("slot-dialog").addEventListener("close", function () { state.slotId = null; });

  // Cancel isn't a submit button, so Enter in the token field submits Continue, not Cancel.
  document.querySelectorAll("dialog [data-dismiss]").forEach(function (btn) {
    btn.addEventListener("click", function () { btn.closest("dialog").close("cancel"); });
  });

  document.querySelectorAll(".sessions [data-sort]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = btn.dataset.sort;
      state.sort = { key: key, dir: state.sort.key === key && state.sort.dir === "asc" ? "desc" : "asc" };
      if (state.data) { renderSessions(state.data); tick(); }
    });
  });

  $("node-filter").addEventListener("input", function () { if (state.data) { renderNodes(state.data); tick(); } });
  $("token-filter").addEventListener("input", function () { if (access.data) renderAccess(); });
  $("token-kind").addEventListener("click", function (e) {
    var b = e.target.closest("[data-kind]");
    if (!b) return;
    access.kind = b.dataset.kind;
    renderAccess();
  });
  $("token-status").addEventListener("click", function (e) {
    var b = e.target.closest("[data-status]");
    if (!b) return;
    access.status = b.dataset.status;
    renderAccess();
  });

  $("new-token-form").addEventListener("change", syncCreate);
  $("new-token-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var body = createBody(), btn = $("nt-submit");
    if (!body || btn.disabled) return;
    btn.disabled = true;
    post("api/tokens", body).then(function (res) {
      if (res === null) return; // the admin token prompt was cancelled; the form stays as it was
      $("new-token-dialog").close();
      access.reveal = res;
      renderReveal(true);
      return loadTokens(false);
    }).catch(function (err) {
      if (err.body && err.body.admin_token_unset) {
        $("new-token-dialog").close();
        access.state = "unconfigured";
        access.data = access.sig = null;
        renderAccess();
        return toast(err.message, true);
      }
      formError(err.message);
    }).then(function () { btn.disabled = false; });
  });

  // The new token is only in this page: warn before it's gone.
  window.addEventListener("beforeunload", function (e) {
    if (access.reveal) { e.preventDefault(); e.returnValue = ""; }
  });
  $("session-filter").addEventListener("input", function () { if (state.data) { renderSessions(state.data); tick(); } });

  $("live-btn").addEventListener("click", function () {
    state.paused = !state.paused;
    this.setAttribute("aria-pressed", String(state.paused));
    this.title = state.paused ? "Resume auto-refresh" : "Pause auto-refresh";
    renderBanner();
    if (!state.paused) refresh();
  });

  // ---- theme ----------------------------------------------------------------

  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function currentTheme() {
    return document.documentElement.dataset.theme || (darkQuery && darkQuery.matches ? "dark" : "light");
  }
  function syncThemeButton() {
    var next = currentTheme() === "dark" ? "light" : "dark";
    var btn = $("theme-btn");
    btn.setAttribute("aria-label", "Switch to " + next + " theme");
    btn.querySelector("use").setAttribute("href", next === "dark" ? "#i-moon" : "#i-sun");
  }
  $("theme-btn").addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("gridTheme", next); } catch (e) { /* not remembered, still applied */ }
    syncThemeButton();
  });
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener("change", syncThemeButton);
  syncThemeButton();

  // ---- start ----------------------------------------------------------------

  function refresh() {
    load();
    if (state.view === "access" && access.state === "ok") loadTokens(false);
  }

  if (location.hash === "#sessions" || location.hash === "#access") showView(location.hash.slice(1));
  document.addEventListener("visibilitychange", function () { if (!document.hidden && !state.paused) refresh(); });
  setInterval(function () { if (!state.paused && !document.hidden) refresh(); }, REFRESH_MS);
  setInterval(function () { tick(); if (state.error) renderBanner(); }, 1000);
  load();
})();
