// Overview: the verdict, what needs you, what just happened, and every slot.
import { app } from "../app.js";
import * as api from "../api.js";
import * as auth from "../auth.js";
import { adminButton, ensureSignedIn, needsSignIn } from "../actions.js";
import { html, icon, patchList, setHTML, setText } from "../html.js";
import { agentCommand, connectUrl, duration, parseTime, plural, slotName } from "../format.js";
import { createTimeline } from "../ui/timeline.js";
import { createMatrix } from "../ui/matrix.js";
import { hubField, wireHubFields } from "../ui/hub.js";
import { refusedPanel } from "../ui/refused.js";
import { toast } from "../ui/toast.js";

const SEVERITY = { failing: 0, unhealthy: 1, stale: 2, offline: 3 };

// One row per actionable problem, most urgent first.
export function attentionItems(data) {
  const items = [];
  if (!data) return items;
  const threshold = data.fail_threshold || 3;
  for (const n of data.nodes || []) {
    if (n.kind === "gateway" && !n.online) {
      items.push({
        key: "node-" + n.host, sev: "offline", tone: "off", subject: n.host, inspect: ["node", n.host],
        cause: html`offline · last heartbeat <span class="mono" data-seen="${n.host}"></span>${n.busy ? html` · ${plural(n.busy, "slot")} still claimed` : ""}`,
        ago: null, action: n.busy ? null : { act: "forget", target: n.host, label: "Forget…", verb: "forget " + n.host, danger: true },
      });
      continue;
    }
    for (const s of n.slots) {
      if (s.state === "failing") {
        items.push({
          key: "slot-" + s.id, sev: "failing", tone: "bad", subject: slotName(n, s), inspect: ["slot", s.id],
          cause: html`${plural(s.fails, "connect failure")} (limit ${threshold}) · ${s.retry_at
            ? html`retry <span data-retry="${s.retry_at}" data-short></span>` : "retry due"}`,
          ago: s.last_run, action: { act: "reset", target: s.id, label: "Reset", verb: "reset failures on " + slotName(n, s) },
        });
      } else if (s.state === "unhealthy") {
        items.push({
          key: "slot-" + s.id, sev: "unhealthy", tone: "warn", subject: slotName(n, s), inspect: ["slot", s.id],
          cause: html`run-server not answering health checks`, ago: null, action: null,
        });
      }
    }
  }
  for (const s of data.sessions || []) {
    if (!s.stale) continue;
    const n = (data.nodes || []).find((x) => x.host === s.host);
    const subject = n ? slotName(n, { port: s.port }) : (s.host || "(blank host)") + ":" + s.port;
    items.push({
      key: "session-" + s.slot_id, sev: "stale", tone: "warn", subject, inspect: ["session", s.slot_id],
      cause: html`held <span class="mono" data-dur="${s.started || ""}"></span> · ${s.via_hub && s.hub_alive === false ? "its hub stopped" : "stale"}`,
      ago: s.started, action: { act: "release", target: s.slot_id, label: "Release…", verb: "release " + subject, danger: true },
    });
  }
  return items.sort((a, b) => (SEVERITY[a.sev] - SEVERITY[b.sev]) || a.subject.localeCompare(b.subject));
}

// What failed, in the words the verdict uses: the database behind a working UI server, or the UI
// server itself (or the network to it). They send whoever is on call to different places.
const OUTAGE = { database: "Can't reach the database", network: "Can't reach the Grid UI server", server: "Can't reach the Grid UI server" };

// The one verdict: the view's h1 and the top bar's chip.
export function verdict(data, error, items, kind) {
  if (!data) {
    return error ? { h1: OUTAGE[kind] || OUTAGE.database, chip: "No data", tone: "bad" } : { h1: "Loading…", chip: "No data", tone: "off" };
  }
  const t = data.totals;
  if (error) return { h1: OUTAGE[kind] || OUTAGE.database, chip: "No data", tone: "bad" };
  if (!data.db.initialised || !t.slots) return { h1: "No nodes yet", chip: "No nodes", tone: "off" };
  if (!t.available && !t.busy) return { h1: "Grid down", chip: "Down", tone: "bad" };
  if (!t.available) return { h1: "No free slots", chip: "Full · 0 free", tone: "warn" };
  if (items.length) return { h1: items.length === 1 ? "1 problem needs you" : items.length + " problems need you", chip: "Degraded · " + items.length, tone: "warn" };
  return { h1: "Healthy", chip: "Healthy", tone: "ok" };
}

function sentence(data, items) {
  const t = data.totals;
  if (!t.slots) return "";
  const parts = [];
  const count = (sev, word, many) => { const n = items.filter((i) => i.sev === sev).length; if (n) parts.push(plural(n, word, many)); };
  count("failing", "slot failing", "slots failing");
  count("unhealthy", "slot unhealthy", "slots unhealthy");
  count("stale", "stale session");
  count("offline", "node offline", "nodes offline");
  const cap = t.available + " of " + plural(t.slots, "slot") + " free · " + t.busy + " running";
  return (parts.length ? parts.join(", ") + ". " : "") + cap + " · " + plural(t.nodes, "node");
}

// The summary above "Needs you": where the slots are, and six counts. Slot problems are counted by
// slot state, as "Needs you" lists them, so the two never disagree.
export function summary(data) {
  const t = data.totals;
  const states = { failing: 0, unhealthy: 0 };
  for (const n of data.nodes || []) for (const s of n.slots) if (s.state in states) states[s.state]++;
  const pct = (n) => (t.slots ? Math.round((100 * n) / t.slots) : 0);
  const problems = states.failing + states.unhealthy;
  const kinds = [states.failing && states.failing + " failing", states.unhealthy && states.unhealthy + " unhealthy"].filter(Boolean);
  return {
    // Idle but not taking runs: on an offline node, not answering health checks, or cooling down.
    busy: t.busy, free: t.available, out: Math.max(0, t.slots - t.busy - t.available), slots: t.slots, pct: pct(t.busy),
    // tone marks the sub line; hot also colours the number, for counts that are themselves the problem.
    tiles: {
      nodes: { value: t.nodes, sub: t.offline ? t.offline + " offline" : "none offline", tone: t.offline ? "warn" : "" },
      slots: { value: t.slots, sub: "across " + plural(t.nodes, "node") },
      sessions: { value: (data.sessions || []).length, sub: pct(t.busy) + "% utilization" },
      free: { value: t.available, sub: pct(t.available) + "% available", tone: t.available ? "" : "warn", hot: !t.available },
      problems: { value: problems, sub: kinds.join(" · ") || "all slots healthy", tone: states.failing ? "bad" : problems ? "warn" : "", hot: problems > 0 },
      stale: { value: t.stale, sub: t.stale ? "held too long or hub gone" : "none abandoned", tone: t.stale ? "warn" : "", hot: t.stale > 0 },
    },
  };
}

const TILES = [
  { key: "nodes", label: "Nodes", icon: "i-server" },
  { key: "slots", label: "Total slots", icon: "i-grid" },
  { key: "sessions", label: "Sessions", icon: "i-sessions", href: "#/sessions" },
  { key: "free", label: "Free slots", icon: "i-check" },
  { key: "problems", label: "Slot problems", icon: "i-alert" },
  { key: "stale", label: "Stale sessions", icon: "i-clock", href: "#/sessions?filter=stale" },
];

function tileShell(t) {
  const inner = html`<span class="stat-label">${t.label}${icon(t.icon)}</span><span class="stat-value" data-v></span><span class="stat-sub" data-s></span>`;
  return t.href ? html`<a class="panel stat" href="${t.href}" data-stat="${t.key}">${inner}</a>`
    : html`<div class="panel stat" data-stat="${t.key}">${inner}</div>`;
}

const graphemes = typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;
const characters = (s) => (graphemes ? Array.from(graphemes.segment(s), (g) => g.segment) : Array.from(s));

// Short of room, a node's name is cut in the middle, keeping the last 6 characters of its first part:
// nodes named alike (k8s-node-eastus-01, -02…) differ there. A domain after it is usually the same on
// every node, so it gives way first. An IP address differs at its end, so it's all one part.
function utilRow(n) {
  const off = n.kind === "gateway" && !n.online;
  const dot = /^[\d.]+$/.test(n.host) ? -1 : n.host.indexOf(".");
  const first = characters(dot > 0 ? n.host.slice(0, dot) : n.host), rest = dot > 0 ? n.host.slice(dot) : "";
  const cut = Math.max(0, first.length - 6);
  return html`<li class="util-row" data-key="${n.host}">
    <a class="mono util-host" dir="auto" href="#/node/${encodeURIComponent(n.host)}" data-inspect="node" data-id="${n.host}" data-fk="util-${n.host}" title="${n.host}" aria-label="${n.host}"><span class="util-head">${first.slice(0, cut).join("")}</span><span class="util-tail">${first.slice(cut).join("")}</span>${rest ? html`<span class="util-rest" data-n="${first.length}">${rest}</span>` : ""}</a>
    <span class="util-bar${off ? " off" : ""}" data-meter="${off || !n.total ? 0 : n.busy / n.total}" aria-hidden="true"><span class="util-fill"></span></span>
    <span class="util-n mono">${off ? "offline" : n.busy + "/" + n.total}</span></li>`;
}

function attentionRow(it) {
  const [type, id] = it.inspect;
  return html`<li class="att-row t-${it.tone}" data-key="${it.key}" data-fk="att-${it.key}" data-jk tabindex="-1" data-inspect="${type}" data-id="${id}">
    <span class="glyph g-${it.tone}" aria-hidden="true"></span>
    <a class="mono att-subject" href="#/${type}/${encodeURIComponent(id)}" data-inspect="${type}" data-id="${id}" tabindex="-1">${it.subject}</a>
    <span class="att-cause">${it.cause}</span>
    <span class="att-age mono">${it.ago ? html`<span data-ago="${it.ago}" data-short></span>` : ""}</span>
    <span class="att-actions">
      <a class="btn ghost small att-inspect" href="#/${type}/${encodeURIComponent(id)}" data-inspect="${type}" data-id="${id}"
        data-fk="att-inspect-${it.key}"><span class="label">Inspect</span>${icon("i-chevron")}</a>
      ${it.action ? adminButton({ label: it.action.label, verb: it.action.verb, attrs: html`data-act="${it.action.act}" data-target="${it.action.target}"`,
        cls: "btn small att-act", danger: it.action.danger, fk: "att-act-" + it.key }) : ""}
    </span></li>`;
}

export function createOverview(section) {
  setHTML(section, html`
    <header class="ov-head">
      <h1 class="verdict" id="ov-h1" tabindex="-1"><span class="glyph" id="verdict-glyph" aria-hidden="true"></span><span id="verdict-word">Loading…</span></h1>
      <p class="verdict-sub" id="verdict-sub"></p>
    </header>
    <div id="ov-error"></div>
    <div id="ov-state"></div>
    <section class="summary" id="summary" aria-labelledby="cap-title" hidden>
      <div class="summary-grid">
        <div class="panel cap-card">
          <h2 class="section-label cap-label" id="cap-title">Grid capacity</h2>
          <div class="donut-wrap">
            <svg class="donut" viewBox="0 0 42 42" aria-hidden="true" focusable="false">
              <circle class="d-free" cx="21" cy="21" r="17"></circle>
              <circle class="d-busy" cx="21" cy="21" r="17" pathLength="100" stroke-dasharray="0 100"></circle>
              <circle class="d-out" cx="21" cy="21" r="17" pathLength="100" stroke-dasharray="0 100"></circle>
            </svg>
            <p class="donut-center"><span class="donut-pct" id="cap-pct"></span><span class="donut-sub" id="cap-sub"></span></p>
          </div>
          <dl class="cap-counts">
            <div class="cap-count"><dt><i class="sw sw-busy" aria-hidden="true"></i>In use</dt><dd id="cap-busy"></dd></div>
            <div class="cap-count"><dt><i class="sw sw-free" aria-hidden="true"></i>Free</dt><dd id="cap-free"></dd></div>
            <div class="cap-count"><dt><i class="sw sw-out" aria-hidden="true"></i>Unavailable</dt><dd id="cap-out"></dd></div>
          </dl>
          <div class="cap-util">
            <h3 class="util-title">Utilization by node</h3>
            <ul class="util-list" id="util-list"></ul>
          </div>
        </div>
        ${TILES.map(tileShell)}
      </div>
    </section>
    <section class="attention" id="attention" tabindex="-1" aria-labelledby="att-title">
      <h2 class="section-label" id="att-title">Needs you <span class="count mono" id="att-count"></span></h2>
      <ul class="att-list" id="att-list"></ul>
      <p class="att-clear" id="att-clear" hidden></p>
    </section>
    <section class="panel runs" id="runs" aria-labelledby="runs-title"></section>
    <section class="matrix-wrap" id="slots" aria-labelledby="mx-title">
      <div class="mx-toolbar">
        <h2 class="section-label" id="mx-title">Slots</h2>
        <ul class="legend" id="mx-legend" aria-label="Slot states">
          <li><i class="cell mini s-free" aria-hidden="true"></i>Free</li>
          <li><i class="cell mini s-busy" aria-hidden="true"></i>In use</li>
          <li><i class="cell mini s-unhealthy" aria-hidden="true"></i>Unhealthy</li>
          <li><i class="cell mini s-cooling legend-sweep" aria-hidden="true"></i>Failing</li>
          <li><i class="cell mini s-offline" aria-hidden="true"></i>Offline</li>
        </ul>
        <details class="legend-more">
          <summary class="btn ghost small">More markers</summary>
          <ul class="legend legend-pop" aria-label="More slot markers">
            <li><i class="cell mini s-cooling legend-sweep" aria-hidden="true"></i>Failing, cooling down: fills up as its retry nears</li>
            <li><i class="cell mini s-failing" aria-hidden="true"></i>Failing, retry due: the next check may try it</li>
            <li><i class="cell mini s-free has-fails" aria-hidden="true"><span class="badge">1</span></i>Connect failures so far (the count)</li>
            <li><i class="cell mini s-free changed-neutral" aria-hidden="true"></i>Claimed in the last 10 s</li>
            <li><i class="cell mini s-free changed-ok" aria-hidden="true"></i>Run finished in the last 10 s</li>
            <li><i class="cell mini s-free changed-bad" aria-hidden="true"></i>Failures went up in the last 10 s</li>
            <li><span class="legend-n mono" aria-hidden="true">3</span>Slot number on gateway nodes, port on static hosts</li>
          </ul>
        </details>
        <a class="btn ghost small mx-connect" href="#/connect" data-inspect="connect" data-id="grid">${icon("i-link")}Connect a check</a>
      </div>
      <div id="matrix-root"></div>
    </section>
    <section id="first-run" hidden></section>`);

  const timeline = createTimeline(section.querySelector("#runs"), { onOpen: (id, el) => app.inspect("slot", id, el) });
  const matrix = createMatrix(section.querySelector("#matrix-root"), {
    onOpen: (type, id, el) => app.inspect(type, id, el),
    onMenu: (host, btn) => app.rowMenu(host, btn),
    hub: () => app.hub(),
  });
  section.querySelectorAll(".legend-sweep").forEach((el) => el.style.setProperty("--sweep", "200deg"));
  const list = section.querySelector("#att-list");
  let firstRun = null; // { token } once the empty state made one

  function renderError(live) {
    const box = section.querySelector("#ov-error");
    if (!live.error) { setHTML(box, html``); return; }
    const age = live.lastOk ? Math.round((Date.now() - live.lastOk) / 1000) : null;
    const db = live.errorKind === "database";
    setHTML(box, html`<div class="panel db-error" role="alert">
      <div class="db-error-head">${icon("i-alert")}<div>
        <p class="db-error-title">${db ? "The Grid UI is up, but it can't read its database." : "This page can't reach the Grid UI server."}</p>
        <p class="db-error-sub">${db ? "Check the database server and the UI's DB settings."
          : html`No usable answer from <span class="mono">${location.host}</span>: check that the Grid UI service is running, and your connection to it (VPN, proxy).`}
          ${age != null ? html`Showing data from <span data-age-ms="${live.lastOk}"></span>.` : "No data yet."} Retrying <span data-next-poll></span>.</p>
      </div><button type="button" class="btn small" data-retry-now>Retry now</button></div>
      <code class="mono db-error-msg">${live.error}</code></div>`);
  }

  function renderFirstRun(data) {
    const box = section.querySelector("#first-run");
    const empty = !data || !data.db.initialised || !data.totals.slots;
    box.hidden = !empty || !data;
    section.querySelector("#summary").hidden = empty;
    section.querySelector("#runs").hidden = empty;
    section.querySelector("#slots").hidden = empty;
    section.querySelector("#attention").hidden = empty || !data;
    if (!empty || !data) return;
    const s = auth.state();
    const token = firstRun && firstRun.token;
    const barred = !needsSignIn() && !auth.can("tokens.agents");
    const agent = firstRunCommand();
    // Typing in the hub field must not rebuild the panel under the cursor.
    if (box.contains(document.activeElement) && document.activeElement.matches("[data-hub-input]")) return;
    setHTML(box, html`<div class="panel empty first-run">
      <h2>${data.db.initialised ? "Add your first node" : "Waiting for the first node"}</h2>
      <p>${data.db.initialised ? "Start a runner or an agent and it registers itself with the hub. It appears here within a few seconds; this page refreshes by itself."
        : "The orchestrator tables haven't been created yet. The gateway hub creates them when it starts, and so does the first check. Nodes show up here as soon as they register."}</p>
      ${hubField()}
      <ol class="steps">
        <li class="step"><div class="step-label">Docker runners, next to the hub</div>
          <div class="snippet"><code class="mono">docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2</code>
          <button type="button" class="icon-btn tiny" data-copy="docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2" data-copy-label="the command" aria-label="Copy the docker command">${icon("i-copy")}</button></div></li>
        <li class="step"><div class="step-label">Any Windows, Linux or macOS host: it dials out, no inbound ports. ${token
          ? html`<strong>Your new agent token is in the command: it's shown once.</strong>`
          : html`Give it an agent token in <code class="mono">GATEWAY_AGENT_TOKEN</code>. The README has the install steps for each platform.`}</div>
          <div class="snippet"><code class="mono" id="first-run-agent">${agent}</code>
          <button type="button" class="icon-btn tiny" data-copy="${agent}" data-copy-label="the command" aria-label="Copy the agent command">${icon("i-copy")}</button></div></li>
        <li class="step"><div class="step-label">Then point every check at the hub, with a client token in <code class="mono">gateway_token</code>. The hub picks a free
          slot that offers the browser the check launches; <a href="#/connect" data-inspect="connect" data-id="grid">Connect a check</a> has snippets.</div>
          <div class="snippet"><code class="mono" id="first-run-connect">${connectUrl(app.hub())}</code>
          <button type="button" class="icon-btn tiny" data-copy="${connectUrl(app.hub())}" data-copy-label="the connect address" aria-label="Copy the connect address">${icon("i-copy")}</button></div></li>
      </ol>
      ${token ? "" : s.adminConfigured
        ? html`<p class="first-run-actions"><button type="button" class="btn primary" data-first-token${barred ? html` disabled title="Your roles don't let you add hosts"` : ""}>${needsSignIn() || barred ? icon("i-lock") : icon("i-plus")}Create agent token</button>
            <span class="dim">Makes a token for this command${needsSignIn() ? " (sign in first)" : barred ? " (it needs “Add hosts”)" : ""}.</span></p>`
        : html`<p class="hint">Set GRID_ADMIN_TOKEN to create agent tokens here; until then hosts use the hub's GATEWAY_AGENT_TOKEN.</p>`}
    </div>`);
    wireHubFields(box);
  }

  section.addEventListener("click", async (e) => {
    if (e.target.closest("[data-retry-now]")) { app.live.refreshNow(); return; }
    if (!e.target.closest("[data-first-token]")) return;
    if (!(await ensureSignedIn("Sign in to create an agent token"))) return;
    try {
      const res = await api.post("api/tokens", { name: "First agent", kind: "agent", nodeName: null, expiresInDays: 365 });
      if (!res) return;
      firstRun = { token: res.token };
      app.unsaved = true;
      renderFirstRun(app.data());
      const code = section.querySelector("#first-run-agent");
      if (code) code.parentElement.querySelector("[data-copy]").focus();
    } catch (err) {
      toast(err.message, true);
    }
  });
  function firstRunCommand() {
    const hub = app.hub(), token = firstRun && firstRun.token;
    return token ? agentCommand("linux", hub, token, "") + " --slots 4" : "node gateway/agent.js --hub " + hub + " --slots 4";
  }
  app.onHub(() => {
    [["#first-run-agent", firstRunCommand()], ["#first-run-connect", connectUrl(app.hub())]].forEach(([sel, text]) => {
      const code = section.querySelector(sel);
      if (!code) return;
      code.textContent = text;
      code.parentElement.querySelector("[data-copy]").dataset.copy = text;
    });
  });

  const utilList = section.querySelector("#util-list");
  function renderSummary(data) {
    if (!data.totals.slots) return;
    const s = summary(data);
    const seg = (cls, start, len) => {
      const el = section.querySelector(".donut ." + cls);
      el.setAttribute("stroke-dasharray", len + " " + (100 - len));
      el.setAttribute("stroke-dashoffset", String(-start));
    };
    seg("d-busy", 0, (100 * s.busy) / s.slots);
    seg("d-out", (100 * s.busy) / s.slots, (100 * s.out) / s.slots);
    setText(section.querySelector("#cap-pct"), s.pct + "%");
    setHTML(section.querySelector("#cap-sub"), html`<span>${s.busy} of ${s.slots}</span> <span>in use</span>`);
    setText(section.querySelector("#cap-busy"), s.busy);
    setText(section.querySelector("#cap-free"), s.free);
    setText(section.querySelector("#cap-out"), s.out);
    patchList(utilList, data.nodes, (n) => n.host, utilRow);
    utilList.querySelectorAll("[data-meter]").forEach((m) => m.style.setProperty("--p", m.dataset.meter));
    utilList.querySelectorAll("[data-n]").forEach((r) => r.style.setProperty("--n", r.dataset.n));
    for (const [key, t] of Object.entries(s.tiles)) {
      const el = section.querySelector('[data-stat="' + key + '"]');
      el.classList.toggle("t-warn", !!t.hot && t.tone === "warn");
      el.classList.toggle("t-bad", !!t.hot && t.tone === "bad");
      setText(el.querySelector("[data-v]"), t.value);
      setHTML(el.querySelector("[data-s]"), html`${t.tone ? html`<span class="glyph g-${t.tone}" aria-hidden="true"></span>` : ""}${t.sub}`);
    }
  }

  function update(msg) {
    const data = msg.data, live = msg.live;
    const items = attentionItems(data);
    // Reading the grid needs a sign-in or a role this page doesn't have: the panel says which.
    const v = live.refused ? { h1: live.refused === "forbidden" ? "No access to the grid" : "Signed out", chip: "No data", tone: "off" }
      : verdict(data, live.error, items, live.errorKind);
    setHTML(section.querySelector("#ov-state"), live.refused ? refusedPanel(live.refused) : html``);
    setText(section.querySelector("#verdict-word"), v.h1);
    section.querySelector("#verdict-glyph").className = "glyph g-" + v.tone;
    section.querySelector("#ov-h1").className = "verdict t-" + v.tone;
    setText(section.querySelector("#verdict-sub"), data && data.totals.slots ? sentence(data, items) : "");
    renderError(live);
    renderFirstRun(data);
    if (!data) return v;
    renderSummary(data);
    setText(section.querySelector("#att-count"), items.length ? String(items.length) : "");
    const clear = section.querySelector("#att-clear");
    clear.hidden = items.length > 0;
    if (!items.length) {
      setHTML(clear, html`${icon("i-check")}<span>Nothing needs you · all ${plural(data.totals.slots, "slot")} healthy · ${data.totals.available} free</span>`);
    }
    patchList(list, items, (it) => it.key, attentionRow);
    timeline.update(data, msg.recorder, app.now());
    matrix.render(data);
    matrix.glow(msg.recorder, msg.events);
    return v;
  }

  return {
    update,
    tick(now) { timeline.tick(now); matrix.sweep(now, app.cfg().cooldown); },
    glow: (rec) => matrix.glow(rec, []),
  };
}
