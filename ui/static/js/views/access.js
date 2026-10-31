// Access: the hub's managed agent and client tokens (a gateway feature, separate from admin
// sign-in), plus "Add a host", which makes a bound agent token and its one install command.
import { app } from "../app.js";
import * as api from "../api.js";
import * as auth from "../auth.js";
import { html, icon, patchList, setHTML, setText } from "../html.js";
import { agentCommand, ago, connectUrl, fullDate, inTime, parseTime, plural, shortDate } from "../format.js";
import { confirm, dismissible } from "../ui/dialog.js";
import { copy, toast } from "../ui/toast.js";
import { hubField, wireHubFields } from "../ui/hub.js";

const TOKEN_TONE = { active: "ok", expired: "warn", revoked: "off" };
const TOKEN_RANK = { active: 0, expired: 1, revoked: 2 };
const KIND_LABEL = { agent: "Agent", client: "Client" };
const TOKEN_ENV = { agent: "GATEWAY_AGENT_TOKEN", client: "GATEWAY_CLIENT_TOKEN" };
const NODE_NAME = /^[a-z0-9._-]{1,128}$/;
const PLATFORMS = [["windows", "Windows", "i-windows"], ["linux", "Linux", "i-linux"], ["macos", "macOS", "i-mac"], ["docker", "Docker", "i-docker"]];
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

export function createAccess(section) {
  setHTML(section, html`
    <header class="view-head access-head">
      <div>
        <h1 id="access-h1" tabindex="-1">Access tokens</h1>
        <p class="view-sub">Agents send an <strong>agent token</strong> to register a node with the hub; checks send a <strong>client token</strong> to open a browser session.
          Give each group of hosts its own token, so you can revoke one without touching the rest.</p>
      </div>
      <div class="head-actions">
        <button type="button" class="btn" id="new-token-btn" data-new-token hidden>${icon("i-plus")}New token</button>
        <button type="button" class="btn primary" id="add-host-btn" aria-expanded="false" aria-controls="add-host" hidden>${icon("i-server")}Add a host</button>
      </div>
    </header>
    <div class="banner" id="access-banner" role="status" hidden></div>
    <div id="reveal"></div>
    <section class="panel add-host" id="add-host" aria-labelledby="ah-title" hidden></section>
    <div class="toolbar" id="token-toolbar" hidden>
      <label class="search">${icon("i-search")}<span class="sr-only">Filter tokens by name, prefix or node</span>
        <input type="search" id="token-filter" placeholder="Filter tokens" autocomplete="off"></label>
      <div class="segmented" id="token-kind" role="group" aria-label="Kind">
        <button type="button" data-kind="all" aria-pressed="true">All kinds</button>
        <button type="button" data-kind="agent" aria-pressed="false">Agent</button>
        <button type="button" data-kind="client" aria-pressed="false">Client</button>
      </div>
      <div class="segmented" id="token-status" role="group" aria-label="Status">
        <button type="button" data-status="all" aria-pressed="true">Any status</button>
        <button type="button" data-status="active" aria-pressed="false">Active</button>
        <button type="button" data-status="expired" aria-pressed="false">Expired</button>
        <button type="button" data-status="revoked" aria-pressed="false">Revoked</button>
      </div>
      <p class="toolbar-note" id="token-note" role="status"></p>
    </div>
    <div class="panel table-panel" id="token-panel" hidden>
      <table class="table tokens">
        <caption class="sr-only">Managed access tokens</caption>
        <thead><tr>
          <th scope="col">Token</th><th scope="col">Kind</th><th scope="col">Bound to</th><th scope="col">Used by</th>
          <th scope="col">Created</th><th scope="col">Last used</th><th scope="col">Expires</th><th scope="col">Status</th>
          <th scope="col"><span class="sr-only">Actions</span></th>
        </tr></thead>
        <tbody id="token-rows"></tbody>
      </table>
      <div class="empty compact" id="tokens-empty" hidden></div>
    </div>
    <div id="access-state"></div>`);

  const $ = (sel) => section.querySelector(sel);
  const dlg = document.getElementById("new-token-dialog");
  dismissible(dlg);
  // ok, signedout, unconfigured, error, loading
  const access = { state: "loading", data: null, sig: null, error: null, kind: "all", status: "all", reveal: null, loading: false, loadedAt: 0, note: "", refused: false };
  const host = { platform: "windows", pending: null, done: null, open: false, confirmDup: null };
  let visible = false;

  // ---- loading ---------------------------------------------------------------
  async function load(force) {
    const s = auth.state();
    if (!s.known) { access.state = "loading"; return render(); }
    if (!s.adminConfigured) { access.state = "unconfigured"; access.data = access.sig = null; return render(); }
    if (!s.signedIn) { access.state = "signedout"; access.data = access.sig = null; return render(); }
    if (access.loading || (!force && Date.now() - access.loadedAt < 4000)) return;
    access.loading = true;
    const before = access.state + "|" + access.sig + "|" + access.error;
    try {
      const body = await api.get("api/tokens");
      access.state = "ok";
      access.error = null;
      access.refused = false;
      access.data = body;
      access.sig = JSON.stringify(body, (k, v) => (k === "generated" ? undefined : v));
    } catch (err) {
      if (err.body && err.body.admin_token_unset) { access.state = "unconfigured"; access.data = access.sig = null; }
      else if (err.body && err.body.token_required) { access.state = "signedout"; access.refused = true; access.data = access.sig = null; }
      else { access.error = err.message; if (!access.data) access.state = "error"; }
    } finally {
      access.loading = false;
      access.loadedAt = Date.now();
    }
    if (access.state + "|" + access.sig + "|" + access.error !== before) render();
  }

  // ---- rendering ---------------------------------------------------------------
  function stateMarkup() {
    switch (access.state) {
      case "loading": return html`<div class="empty"><p>Loading…</p></div>`;
      case "unconfigured": return html`<div class="empty">${icon("i-lock", "empty-mark")}
        <h2>Set an admin token to manage access tokens</h2>
        <p>Access tokens are managed by an admin, and only admins can see them: names, bindings and usage are sensitive too.
          Until the grid has an admin token, the hub keeps relying on its env tokens.</p>
        <ol class="steps">
          <li class="step"><div class="step-label">Add one to .env next to docker-compose.yaml (or to the Grid UI's environment)</div>
            <div class="snippet"><code class="mono">echo "GRID_ADMIN_TOKEN=$(openssl rand -hex 32)" >> .env</code>
            <button type="button" class="icon-btn tiny" data-copy='echo "GRID_ADMIN_TOKEN=$(openssl rand -hex 32)" >> .env' data-copy-label="the command" aria-label="Copy the command">${icon("i-copy")}</button></div></li>
          <li class="step"><div class="step-label">Then restart the Grid UI and sign in with the same value</div>
            <div class="snippet"><code class="mono">docker compose up -d grid_ui</code>
            <button type="button" class="icon-btn tiny" data-copy="docker compose up -d grid_ui" data-copy-label="the command" aria-label="Copy the command">${icon("i-copy")}</button></div></li>
        </ol></div>`;
      case "signedout": return html`<div class="empty calm">${icon("i-lock", "empty-mark")}
        <h2>${access.refused ? "Sign in again to manage access tokens" : "Sign in to manage access tokens"}</h2>
        <p>Only admins can see and change the hub's agent and client tokens. Everything else on this page works without signing in.</p>
        <button type="button" class="btn primary" data-signin>Sign in</button></div>`;
      case "error": return html`<div class="empty">${icon("i-alert", "empty-mark")}<h2>Can't load the access tokens</h2><p>${access.error}</p>
        <button type="button" class="btn" data-reload-tokens>Try again</button></div>`;
    }
    const d = access.data;
    if (!d.ready) {
      return html`<div class="empty">${icon("i-key", "empty-mark")}<h2>Access tokens aren't set up in this database yet</h2>
        <p>It has no accessTokens table. The gateway hub creates it when it starts, and so does the Grid UI on MySQL when its database
          user may create tables. Until then the hub relies on its env tokens.</p></div>`;
    }
    return html`<div class="empty">${icon("i-key", "empty-mark")}<h2>No access tokens yet</h2>
      <p>Until you create one, the hub relies on its env tokens (GATEWAY_AGENT_TOKEN and GATEWAY_CLIENT_TOKEN).
        Create one per group of hosts, so you can revoke it without touching the others.</p>
      <button type="button" class="btn primary" data-new-token>${icon("i-plus")}New token</button></div>`;
  }

  function expiresCell(t, now) {
    const exp = parseTime(t.expiresAt);
    if (exp == null) return html`<span class="dim">Never</span>`;
    const s = Math.round((exp - now) / 1000);
    if (s <= 0) return html`<span>${ago(-s)}</span>`;
    if (s > 30 * 86400) return html`${shortDate(exp, now)}`;
    return html`<span class="${t.status === "active" && s < 7 * 86400 ? "soon" : ""}">${inTime(s)}</span>`;
  }

  function nodeRefs(t) {
    if (t.kind !== "agent") return html`<span class="dim" title="Client tokens aren't tied to nodes">–</span>`;
    if (!t.nodes.length) return html`<span class="dim">No nodes</span>`;
    const online = t.nodesOnline || [];
    return html`<div class="node-refs">${t.nodes.slice(0, 2).map((n) => {
      const on = online.indexOf(n) !== -1;
      return html`<span class="node-ref mono" title="${n} (${on ? "online" : "offline"})"><span class="dot ${on ? "ok" : "off"}" aria-hidden="true"></span>${n}<span class="sr-only"> (${on ? "online" : "offline"})</span></span>`;
    })}${t.nodes.length > 2 ? html`<span class="more" title="${t.nodes.slice(2).join(", ")}">+${t.nodes.length - 2} more</span>` : ""}</div>`;
  }

  function tokenRow(t, now) {
    const created = parseTime(t.createdAt), used = parseTime(t.lastUsedAt);
    const action = t.status === "active"
      ? html`<button type="button" class="btn ghost small destructive" data-revoke="${t.id}" data-fk="revoke-${t.id}">Revoke</button>`
      : html`<button type="button" class="btn ghost small destructive" data-delete-token="${t.id}" data-fk="delete-${t.id}">Delete</button>`;
    return html`<tr data-key="${t.id}" data-token="${t.id}" data-status="${t.status}" data-jk tabindex="-1">
      <td class="cell-token"><span class="tok-name">${t.name}</span><code class="tok-prefix mono" title="The token starts with these characters">${t.prefix}…</code></td>
      <td class="cell-kind" data-label="Kind"><span class="kind k-${t.kind}">${KIND_LABEL[t.kind] || t.kind}</span></td>
      <td class="cell-bound${t.kind !== "agent" ? " na" : ""}" data-label="Bound to">${t.kind !== "agent" ? html`<span class="dim">–</span>`
        : t.nodeName ? html`<code class="mono bound">${t.nodeName}</code>` : html`<span class="dim">Any node</span>`}</td>
      <td class="cell-used${t.kind !== "agent" ? " na" : ""}" data-label="Used by">${nodeRefs(t)}</td>
      <td class="cell-created" data-label="Created" title="${fullDate(created)}">${created != null ? shortDate(created, now) : "–"}</td>
      <td class="cell-last" data-label="Last used" title="${fullDate(used)}">${used != null ? html`<span data-ago="${t.lastUsedAt}"></span>` : html`<span class="dim">Never</span>`}</td>
      <td class="cell-expires" data-label="${t.status === "expired" ? "Expired" : "Expires"}" title="${fullDate(parseTime(t.expiresAt))}">${expiresCell(t, now)}</td>
      <td class="cell-status"><span class="pill p-${TOKEN_TONE[t.status] || "off"}"><span class="glyph g-${TOKEN_TONE[t.status] || "off"}" aria-hidden="true"></span>${cap(t.status)}</span></td>
      <td class="cell-actions">${action}</td></tr>`;
  }

  const shownBy = (t, q, kind, status) => (kind === "all" || t.kind === kind) && (status === "all" || t.status === status) &&
    (!q || [t.name, t.prefix, t.nodeName, t.kind].concat(t.nodes).join(" ").toLowerCase().indexOf(q) !== -1);

  function renderTokens(d) {
    const q = $("#token-filter").value.trim().toLowerCase(), now = app.now();
    section.querySelectorAll("#token-kind button").forEach((b) => {
      const n = d.tokens.filter((t) => shownBy(t, "", b.dataset.kind, access.status)).length;
      const label = b.dataset.label || (b.dataset.label = b.textContent);
      setHTML(b, html`${label}${n ? html`<span class="n">${n}</span>` : ""}`);
      b.setAttribute("aria-pressed", String(b.dataset.kind === access.kind));
    });
    section.querySelectorAll("#token-status button").forEach((b) => {
      const n = d.tokens.filter((t) => shownBy(t, "", access.kind, b.dataset.status)).length;
      const label = b.dataset.label || (b.dataset.label = b.textContent);
      setHTML(b, html`${label}${n ? html`<span class="n">${n}</span>` : ""}`);
      b.setAttribute("aria-pressed", String(b.dataset.status === access.status));
    });
    const active = d.tokens.filter((t) => t.status === "active").length;
    const env = ["agent", "client"].filter((k) => d.bootstrap && d.bootstrap[k]);
    setText($("#token-note"), access.note || (plural(active, "active token") +
      (env.length ? " · the hub also accepts " + env.map((k) => TOKEN_ENV[k]).join(" and ") : "")));
    const rows = d.tokens.filter((t) => shownBy(t, q, access.kind, access.status)).sort((a, b) =>
      (TOKEN_RANK[a.status] - TOKEN_RANK[b.status]) || (Date.parse(b.createdAt) - Date.parse(a.createdAt)) || b.id - a.id);
    const empty = $("#tokens-empty");
    empty.hidden = rows.length > 0;
    setHTML(empty, html`<h2>No tokens match</h2><p>${q ? "Nothing matches “" + q + "” with these filters." : "No tokens have this kind and status."}</p>
      <button class="btn small" type="button" data-clear-token-filter>Show all tokens</button>`);
    patchList($("#token-rows"), rows, (t) => t.id, (t) => tokenRow(t, now));
  }

  function render() {
    const d = access.state === "ok" ? access.data : null, list = !!(d && d.ready && d.tokens.length);
    $("#new-token-btn").hidden = !list;
    $("#add-host-btn").hidden = !(d && d.ready);
    $("#add-host-btn").setAttribute("aria-expanded", String(host.open));
    $("#token-toolbar").hidden = !list;
    $("#token-panel").hidden = !list;
    $("#add-host").hidden = !(d && d.ready && host.open);
    setHTML($("#access-state"), list ? html`` : stateMarkup());
    if (list) renderTokens(d);
    if (d && d.ready) renderAddHost();
    const banner = $("#access-banner");
    banner.hidden = !(access.error && access.state === "ok");
    if (!banner.hidden) {
      setHTML(banner, html`<div><strong>Can't refresh the access tokens.</strong> Showing the last list. Retrying.<code class="mono">${access.error}</code></div>`);
    }
    if (app.tick) app.tick();
  }

  // ---- reveal -----------------------------------------------------------------
  function usages(r) {
    const hub = app.hub();
    if (r.platform) {
      const label = PLATFORMS.find((p) => p[0] === r.platform)[1];
      return [[{ windows: "Windows: in installer\\windows, from an elevated PowerShell", linux: "Linux: from a checkout of this repository",
        macos: "macOS: from a checkout of this repository", docker: "Docker: on the host, with the runner image built" }[r.platform] || label,
        agentCommand(r.platform, hub, r.token, r.nodeName)]];
    }
    if (r.kind === "agent") {
      return [
        ["Windows host: the installer in installer\\windows", agentCommand("windows", hub, r.token, r.nodeName)],
        ["Linux or macOS host: the agent, from the repository", agentCommand("linux", hub, r.token, r.nodeName)],
        ["Docker runner: its environment, e.g. runner.env", "GATEWAY_AGENT_TOKEN=" + r.token + (r.nodeName ? "\nNODE_NAME=" + r.nodeName : "")],
      ];
    }
    const d = app.data(), node = d && d.nodes.find((n) => n.kind === "gateway" && n.slots.length);
    const out = [["Check containers: their environment (example_playwright in docker-compose.yaml)", "GATEWAY_CLIENT_TOKEN=" + r.token]];
    if (node) out.push(["Other Playwright clients: on the endpoint (or as an x-gateway-token header), e.g. " + node.host + " slot " + node.slots[0].port,
      connectUrl(hub, node, node.slots[0]) + "?gateway_token=" + r.token]);
    return out;
  }

  function waitLine() {
    const r = access.reveal, el = $("#wait-line");
    if (!r || !el) return;
    const waiting = !(host.done && host.done === r.waitFor);
    setHTML(el, waiting
      ? html`<span class="spinner" aria-hidden="true"></span><span>Waiting for <span class="mono">${r.waitFor}</span> to register…</span>`
      : html`${icon("i-check")}<span><span class="mono">${host.done}</span> registered. <a href="#/node/${encodeURIComponent(host.doneHost || host.done)}" data-inspect="node" data-id="${host.doneHost || host.done}">Inspect the node</a></span>`);
    el.classList.toggle("done", !waiting);
  }

  // The hub address changed: rewrite the commands in place (the field being typed in stays put).
  function refreshUsages() {
    const r = access.reveal;
    if (!r) return;
    const list = usages(r);
    $("#reveal").querySelectorAll("[data-usage]").forEach((code) => {
      const u = list[Number(code.dataset.usage)];
      if (!u) return;
      code.textContent = u[1];
      const btn = code.parentElement.querySelector("[data-copy]");
      if (btn) btn.dataset.copy = u[1];
    });
  }

  function renderReveal(fresh) {
    const r = access.reveal, box = $("#reveal");
    app.unsaved = !!r;
    if (!r) { setHTML(box, html``); return; }
    const facts = [KIND_LABEL[r.kind] + " token", r.name, r.nodeName ? "bound to " + r.nodeName : "",
      r.expiresAt ? "expires " + shortDate(parseTime(r.expiresAt), app.now()) : "never expires"].filter(Boolean);
    box.__html = null;
    setHTML(box, html`<section class="reveal panel" aria-labelledby="reveal-title">
      <div class="reveal-head"><span class="reveal-mark">${icon("i-key")}</span>
        <div class="reveal-title"><h2 id="reveal-title">${r.platform ? "Run this on the new host" : "Copy your new token now"}</h2><p>${facts.join(" · ")}</p></div>
        <button type="button" class="btn" data-reveal-done>Done</button></div>
      <div class="secret"><code id="reveal-token" class="mono">${r.token}</code>
        <button type="button" class="btn ${r.platform ? "" : "primary"} small" data-copy="${r.token}" data-copy-label="the token" data-fk="reveal-copy" aria-describedby="reveal-warn">${icon("i-copy")}Copy</button></div>
      <p class="reveal-warn" id="reveal-warn">${icon("i-alert")}<span>It won't be shown again. The grid keeps only a hash of it: if it's lost, revoke it and create another.</span></p>
      <div class="usage"><h3>${r.platform ? "The command" : "Use it"}</h3>
        ${r.kind === "agent" || node0() ? hubField() : ""}
        <div class="usage-grid">${usages(r).map((u, i) => html`<div class="step"><div class="step-label">${u[0]}</div>
          <div class="snippet"><code class="mono" data-usage="${i}">${u[1]}</code><button type="button" class="${r.platform ? "btn primary small" : "icon-btn tiny"}" data-copy="${u[1]}" data-copy-label="the command"
            data-fk="reveal-use-${i}" aria-label="Copy the command for ${u[0].split(":")[0]}">${icon("i-copy")}${r.platform ? "Copy command" : ""}</button></div></div>`)}</div>
        ${r.platform && r.platform !== "docker" ? html`<p class="hint">The token is part of this command, so it also stays in that shell's history on the host.</p>` : ""}
      </div>
      ${r.waitFor ? html`<p class="wait-line" id="wait-line" role="status"></p>` : ""}
    </section>`);
    wireHubFields(box);
    waitLine();
    if (fresh) {
      const btn = box.querySelector(r.platform ? "[data-fk=reveal-use-0]" : "[data-fk=reveal-copy]");
      btn.focus({ preventScroll: true });
      const top = box.firstElementChild.getBoundingClientRect().top, bar = document.querySelector(".topbar").getBoundingClientRect().bottom;
      if (top < bar || top > innerHeight / 2) box.firstElementChild.scrollIntoView({ block: "start" });
    }
  }
  const node0 = () => { const d = app.data(); return d && d.nodes.some((n) => n.kind === "gateway" && n.slots.length); };
  app.onHub(refreshUsages);

  function hideReveal() {
    access.reveal = null;
    host.pending = null;
    renderReveal();
    const btn = $("#new-token-btn");
    if (!btn.hidden) btn.focus(); else $("#access-h1").focus();
  }

  // ---- add a host ----------------------------------------------------------------
  function renderAddHost() {
    const box = $("#add-host");
    if (box.__built) return;
    box.__built = true;
    setHTML(box, html`
      <header class="ah-head"><div><h2 id="ah-title">Add a host</h2>
        <p>Makes an agent token bound to one node name, shows the one command that installs it, and waits for the host to register.</p></div>
        <button type="button" class="btn ghost small" data-ah-close>Cancel</button></header>
      <form class="ah-steps" id="ah-form" novalidate>
        <fieldset class="ah-step"><legend><span class="step-n">1</span>Platform</legend>
          <div class="ah-platforms">${PLATFORMS.map(([id, label, ic]) => html`<label class="choice-card">
            <input type="radio" name="ah-platform" value="${id}" ${id === host.platform ? "checked" : ""}>${icon(ic)}<span>${label}</span></label>`)}</div>
        </fieldset>
        <div class="ah-step"><label class="step-legend" for="ah-node"><span class="step-n">2</span>Bind to node name</label>
          <input id="ah-node" class="mono" type="text" list="node-names" maxlength="128" autocomplete="off" autocapitalize="off" spellcheck="false"
            placeholder="e.g. win-build-02" aria-describedby="ah-hint ah-error">
          <p class="hint" id="ah-hint">The host registers under this name, and only it can use the token.</p>
          <p class="form-error" id="ah-error" role="alert" hidden></p>
        </div>
        <div class="ah-step"><span class="step-legend"><span class="step-n">3</span>Create the token</span>
          <button type="submit" class="btn primary" id="ah-create">${icon("i-plus")}Create token and command</button>
          <p class="hint">An agent token named “<span id="ah-name">node agent</span>”, valid for a year.</p>
        </div>
      </form>`);
    const input = box.querySelector("#ah-node");
    const HINT = "The host registers under this name, and only it can use the token.";
    const createLabel = (text) => setHTML(box.querySelector("#ah-create"), html`${icon("i-plus")}${text}`);
    input.addEventListener("input", () => {
      const typed = input.value.trim(), name = typed.toLowerCase();
      box.querySelector("#ah-name").textContent = (name || "node") + " agent";
      // Node names are lower case: say what it will really be called.
      box.querySelector("#ah-hint").textContent = typed && typed !== name ? "Registers as " + name + ": node names are lower case." : HINT;
      if (host.confirmDup) {
        host.confirmDup = null;
        createLabel("Create token and command");
        box.querySelector("#ah-error").hidden = true;
      }
    });
    box.querySelector("#ah-form").addEventListener("change", (e) => {
      if (e.target.name === "ah-platform") host.platform = e.target.value;
    });
    box.querySelector("#ah-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = input.value.trim().toLowerCase(), err = box.querySelector("#ah-error");
      if (!NODE_NAME.test(name)) {
        err.textContent = "A node name is 1 to 128 characters: letters, digits, '.', '_' and '-'.";
        err.hidden = false;
        input.setAttribute("aria-invalid", "true");
        input.focus();
        return;
      }
      err.hidden = true;
      err.classList.remove("notice");
      input.removeAttribute("aria-invalid");
      const btn = box.querySelector("#ah-create");
      // A second token for the same host usually means the first was lost: say so before piling up.
      const dup = ((access.data && access.data.tokens) || []).find((t) => t.kind === "agent" && t.status === "active" && t.nodeName === name);
      if (dup && host.confirmDup !== name) {
        host.confirmDup = name;
        err.textContent = name + " already has an active agent token, “" + dup.name + "”, created " + shortDate(parseTime(dup.createdAt), app.now()) +
          ". Creating another leaves both working: revoke the old one in the list below once the host uses the new one.";
        err.classList.add("notice");
        err.hidden = false;
        createLabel("Create another token");
        btn.focus();
        return;
      }
      btn.disabled = true;
      try {
        const res = await api.post("api/tokens", { name: name + " agent", kind: "agent", nodeName: name, expiresInDays: 365 });
        if (!res) return;
        const known = app.data() && app.data().nodes.find((n) => n.host.toLowerCase() === name);
        host.pending = known ? null : name;
        host.done = known ? name : null;
        host.doneHost = known ? known.host : null;
        access.reveal = Object.assign({}, res, { platform: host.platform, waitFor: name });
        renderReveal(true);
        input.value = "";
        host.confirmDup = null;
        createLabel("Create token and command");
        load(true);
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ---- token actions ------------------------------------------------------------
  const findToken = (id) => ((access.data && access.data.tokens) || []).find((t) => String(t.id) === String(id)) || null;

  // The hub keeps asking for a token once a managed one of that kind exists, revoked or not. Revoking the
  // last usable one therefore locks out everything without the env token (the UI only knows about that
  // when it was given the same variable), and deleting the last row lets tokenless ones back in after the
  // next hub restart. `usable` limits the count to tokens that still work.
  function lastOfKind(t, usable) {
    const d = access.data;
    return !(d.bootstrap && d.bootstrap[t.kind]) && !d.tokens.some((o) => o.id !== t.id && o.kind === t.kind && (!usable || o.status === "active"));
  }

  async function tokenAction(url, done, origin) {
    const rows = Array.from(section.querySelectorAll("#token-rows tr"));
    const at = rows.indexOf(origin && origin.closest("tr"));
    try {
      const res = await api.post(url, {});
      if (res === null) return;
      toast(done);
    } catch (err) {
      if (err.body && err.body.admin_token_unset) { access.state = "unconfigured"; access.data = access.sig = null; render(); }
      toast(err.message, true);
    }
    await load(true);
    // A filter the action emptied goes back to All, and says so.
    const d = access.data;
    if (d && d.tokens.length && !d.tokens.some((t) => shownBy(t, $("#token-filter").value.trim().toLowerCase(), access.kind, access.status))) {
      access.kind = access.status = "all";
      $("#token-filter").value = "";
      access.note = "Nothing left in that filter: showing all tokens.";
      render();
      setTimeout(() => { access.note = ""; if (access.data) renderTokens(access.data); }, 6000);
    }
    const after = Array.from(section.querySelectorAll("#token-rows tr"));
    const next = after[Math.min(Math.max(at, 0), after.length - 1)];
    const target = next && next.querySelector("button") || $("#access-h1");
    target.focus();
  }

  function confirmRevoke(id, origin) {
    const t = findToken(id);
    if (!t) return;
    const agent = t.kind === "agent", online = t.nodesOnline || [];
    const text = !agent ? "Checks that use it can't open new browser sessions. Sessions already running carry on." :
      t.nodes.length ? "These nodes last registered with it. The ones connected now are disconnected within about 15 seconds, and none of them can reconnect with it:" :
        "No node has registered with it yet. Nothing can register with it afterwards.";
    const note = "This can't be undone." + (lastOfKind(t, true) ? " It's the last working " + t.kind + " token: the hub keeps asking for one, so " +
      "unless it has " + TOKEN_ENV[t.kind] + " set, no " + (agent ? "agent" : "check") + " gets in until you create a new token." : "");
    return confirm({
      title: "Revoke “" + t.name + "”?", text, ok: "Revoke token", note,
      items: agent ? t.nodes.map((n) => {
        const on = online.indexOf(n) !== -1;
        return { text: n, tone: on ? "ok" : "off", label: on ? "Online now: disconnected within about 15 s" : "Offline" };
      }) : [],
    }).then((ok) => { if (ok) return tokenAction("api/tokens/" + encodeURIComponent(id) + "/revoke", "Revoked “" + t.name + "”.", origin); });
  }

  function confirmDelete(id, origin) {
    const t = findToken(id);
    if (!t) return;
    // Any row of a kind, revoked or expired, keeps the hub asking for that kind of token; with the last
    // one gone, a restarted hub stops asking unless its env token is set.
    const note = lastOfKind(t, false) ? "It's the last " + t.kind + " token on record: unless the hub has " +
      TOKEN_ENV[t.kind] + " set, after its next restart it accepts " + (t.kind === "agent" ? "agents" : "checks") +
      " without a token again. Create a new " + t.kind + " token first to keep it closed." : "";
    return confirm({
      title: "Delete “" + t.name + "”?",
      text: "It's already " + t.status + ", so the hub turns it away. Deleting it removes it from this list" +
        (t.nodes.length ? " and from the " + plural(t.nodes.length, "node") + " that last registered with it." : "."),
      ok: "Delete token", note,
    }).then((ok) => { if (ok) return tokenAction("api/tokens/" + encodeURIComponent(id) + "/delete", "Deleted “" + t.name + "”.", origin); });
  }

  // ---- create dialog --------------------------------------------------------------
  const kindOf = () => { const c = dlg.querySelector('input[name="kind"]:checked'); return c ? c.value : "agent"; };
  function formError(text) {
    const el = document.getElementById("nt-error");
    el.textContent = text || "";
    el.hidden = !text;
  }
  function syncCreate() {
    const kind = kindOf(), d = access.data;
    document.getElementById("nt-node-field").hidden = kind !== "agent";
    document.getElementById("nt-days-wrap").hidden = document.getElementById("nt-expiry").value !== "custom";
    // The hub starts asking for a token once one of this kind exists, even without its env token.
    const first = d && d.ready && !(d.bootstrap && d.bootstrap[kind]) && !d.tokens.some((t) => t.kind === kind && t.status !== "revoked");
    const el = document.getElementById("nt-first");
    el.hidden = !first;
    el.textContent = "This is the first " + kind + " token. From then on the hub turns away " +
      (kind === "agent" ? "agents" : "checks") + " that don't send a valid token, even without " + TOKEN_ENV[kind] + ".";
  }
  function openCreate() {
    document.getElementById("new-token-form").reset();
    ["nt-name", "nt-node", "nt-days"].forEach((id) => document.getElementById(id).removeAttribute("aria-invalid"));
    formError("");
    syncCreate();
    dlg.showModal();
    document.getElementById("nt-name").focus();
  }
  function createBody() {
    const name = document.getElementById("nt-name").value.trim(), kind = kindOf();
    const node = document.getElementById("nt-node").value.trim().toLowerCase();
    const expiry = document.getElementById("nt-expiry").value;
    const days = expiry === "custom" ? Number(document.getElementById("nt-days").value) : expiry ? Number(expiry) : null;
    const problem = !name ? ["nt-name", "Give the token a name, such as the hosts it's for."] :
      kind === "agent" && node && !NODE_NAME.test(node) ? ["nt-node", "A node name is 1 to 128 characters: letters, digits, '.', '_' and '-'."] :
        days !== null && !(Number.isInteger(days) && days >= 1 && days <= 3650) ? ["nt-days", "Pick a whole number of days from 1 to 3650."] : null;
    ["nt-name", "nt-node", "nt-days"].forEach((id) => document.getElementById(id).removeAttribute("aria-invalid"));
    if (problem) {
      document.getElementById(problem[0]).setAttribute("aria-invalid", "true");
      formError(problem[1]);
      document.getElementById(problem[0]).focus();
      return null;
    }
    formError("");
    return { name, kind, nodeName: kind === "agent" && node ? node : null, expiresInDays: days };
  }
  document.getElementById("new-token-form").addEventListener("change", syncCreate);
  document.getElementById("new-token-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = createBody(), btn = document.getElementById("nt-submit");
    if (!body || btn.disabled) return;
    btn.disabled = true;
    try {
      const res = await api.post("api/tokens", body);
      if (res === null) return; // sign-in dismissed: the form stays as it was
      dlg.close();
      access.reveal = res;
      renderReveal(true);
      load(true);
    } catch (err) {
      if (err.body && err.body.admin_token_unset) {
        dlg.close();
        access.state = "unconfigured";
        access.data = access.sig = null;
        render();
        toast(err.message, true);
        return;
      }
      formError(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  function toggleAddHost(open) {
    host.open = open;
    render();
    if (open) {
      const first = section.querySelector('#add-host input[name="ah-platform"]:checked');
      if (first) first.focus();
      $("#add-host").scrollIntoView({ block: "nearest" });
    } else {
      $("#add-host-btn").focus();
    }
  }

  // ---- events -----------------------------------------------------------------------
  section.addEventListener("click", async (e) => {
    let el;
    if (e.target.closest("#add-host-btn")) return toggleAddHost(!host.open);
    if (e.target.closest("[data-ah-close]")) return toggleAddHost(false);
    if ((el = e.target.closest("[data-copy]")) && el.closest("#reveal") && access.reveal) access.reveal.copied = true;
    if (e.target.closest("[data-new-token]")) return openCreate();
    if ((el = e.target.closest("[data-revoke]"))) return confirmRevoke(el.dataset.revoke, el);
    if ((el = e.target.closest("[data-delete-token]"))) return confirmDelete(el.dataset.deleteToken, el);
    if (e.target.closest("[data-signin]")) {
      if (await auth.signIn("Sign in to manage access tokens")) load(true);
      return;
    }
    if (e.target.closest("[data-reload-tokens]")) return load(true);
    if (e.target.closest("[data-clear-token-filter]")) {
      $("#token-filter").value = "";
      access.kind = access.status = "all";
      render();
      return;
    }
    if (e.target.closest("[data-reveal-done]")) {
      if (access.reveal && access.reveal.copied) return hideReveal();
      const ok = await confirm({ title: "Hide the token without copying it?", text: "It can't be shown again. If you need it later, revoke it and create another.", ok: "Hide token" });
      if (ok) hideReveal();
    }
  });
  $("#token-filter").addEventListener("input", () => { if (access.data) render(); });
  $("#token-kind").addEventListener("click", (e) => { const b = e.target.closest("[data-kind]"); if (b) { access.kind = b.dataset.kind; render(); } });
  $("#token-status").addEventListener("click", (e) => { const b = e.target.closest("[data-status]"); if (b) { access.status = b.dataset.status; render(); } });

  auth.onChange(() => { if (visible) load(true); });

  return {
    show() { visible = true; load(true); },
    hide() { visible = false; },
    update(data) {
      if (host.pending && data) {
        const n = data.nodes.find((x) => x.host.toLowerCase() === host.pending);
        if (n) { host.done = host.pending; host.doneHost = n.host; host.pending = null; waitLine(); }
      }
      if (visible) load(false);
    },
    tokens: () => (access.state === "ok" && access.data ? access.data.tokens : []),
  };
}
