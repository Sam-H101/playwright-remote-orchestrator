// Admin, Users: the people who signed in, their roles and sessions, and the sheet that manages each one.
// The server finds, filters and pages them; the filters live in the address (?q=, ?status=, ?role=, ?sort=,
// ?page=), and ?user=<id> opens that person's sheet. Each change in the sheet is made at once, and the sheet
// then shows the person as the server says they are now.
import { app } from "../../app.js";
import * as api from "../../api.js";
import * as auth from "../../auth.js";
import * as router from "../../router.js";
import { html, icon, patchList, setHTML, setText } from "../../html.js";
import { fullDate, parseTime, plural } from "../../format.js";
import { confirm, dismissible } from "../../ui/dialog.js";
import { createPicker, directory } from "../../ui/picker.js";
import { toast } from "../../ui/toast.js";
import { sentence } from "./audit.js";
import { ADMIN_ROLE, MANAGE, NOT_GIVEN, both, heldRoles, loader, mayGive, permissionGroups, possessive, roleSource, roleSourceShort } from "./common.js";

const PAGE = 50;
const POLL_MS = 10000;
const SEARCH_MS = 250;
// The status filter: the address's word (left out: the active ones), the API's, and the button's label. The
// address can't say "all": router.build() drops it.
const STATUSES = [["", "active", "Active"], ["disabled", "disabled", "Disabled"], ["any", "all", "All"]];
const SHEET_TABS = [["access", "Access"], ["sessions", "Sessions"], ["owns", "Owns"], ["history", "History"]];
const UA_LENGTH = 60;
const GROUPS_SHOWN = 30;
const RESTART = "docker compose up -d grid_ui";

const statusOf = (p) => STATUSES.find(([word]) => word === (p.status || "")) || STATUSES[0];
const pageOf = (p) => Math.max(1, Math.min(100000, parseInt(p.page, 10) || 1));
const activeTokens = (d, kind) => d.owns.tokens.filter((t) => t.kind === kind && t.status === "active");

function statusPill(u) {
  return u.disabled ? html`<span class="pill"><span class="glyph g-off" aria-hidden="true"></span>Disabled</span>`
    : html`<span class="pill p-ok"><span class="glyph g-ok" aria-hidden="true"></span>Active</span>`;
}

// One pill per role, however many ways they have it; one they have only through a group, the mapping for
// everyone or GRID_OIDC_ADMINS says so. The title says every way.
function rolePills(u) {
  const held = heldRoles(u);
  if (!held.length) return html`<span class="dim">No roles</span>`;
  return html`<span class="role-pills">${held.map((r) => {
    const from = r.manual ? "" : roleSourceShort(r.sources[0]);
    return html`<span class="pill${r.id === ADMIN_ROLE ? " p-accent" : ""}" title="${r.sources.map(roleSource).join("; ")}">${r.name}${from
      ? html`<span class="pill-from">· ${from}</span>` : ""}</span>`;
  })}</span>`;
}

function ownsText(o) {
  return [o.mfa && plural(o.mfa, "secret"), o.signins && plural(o.signins, "sign-in"), o.tokens && plural(o.tokens, "token")].filter(Boolean).join(" · ");
}

export function createUsers(el, ctx) {
  setHTML(el, html`
    <div class="toolbar user-toolbar" id="user-toolbar" hidden>
      <label class="search">${icon("i-search")}<span class="sr-only">Find people by name, email or subject</span>
        <input type="search" id="user-filter" placeholder="Find people" autocomplete="off"></label>
      <div class="segmented" id="user-status" role="group" aria-label="Status">
        ${STATUSES.map(([word, , label]) => html`<button type="button" data-status="${word}" data-label="${label}" aria-pressed="false">${label}</button>`)}
      </div>
      <label class="select-field"><span>Role</span><select id="user-role"></select></label>
      <label class="select-field"><span>Sort</span><select id="user-sort"><option value="">Recent sign-in</option><option value="name">Name</option></select></label>
      <p class="toolbar-note" id="user-note" role="status"></p>
    </div>
    <div class="panel table-panel" id="user-panel" hidden>
      <table class="table users">
        <caption class="sr-only">People who signed in</caption>
        <thead><tr>
          <th scope="col">User</th><th scope="col">Roles</th><th scope="col">Last sign-in</th><th scope="col" class="num-col">Sessions</th>
          <th scope="col">Owns</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th>
        </tr></thead>
        <tbody id="user-rows"></tbody>
      </table>
    </div>
    <nav class="pager" id="user-pager" aria-label="Pages of people" hidden>
      <button type="button" class="btn small" data-page="-1">Previous</button>
      <span class="pager-at" id="user-page-at"></span>
      <button type="button" class="btn small" data-page="1">Next</button>
    </nav>
    <div id="user-empty"><div class="empty"><p>Loading…</p></div></div>`);

  const $ = (sel) => el.querySelector(sel);
  const dlg = document.getElementById("user-dialog");
  dismissible(dlg);
  // data: the answer for the page the address asks for (query); roles: every role and the permission catalogue
  // (GET /api/admin/roles), for the role filter and the sheet.
  const users = { data: null, roles: null, query: null };
  let visible = false, poll = null, typed = null;

  // ---- loading ---------------------------------------------------------------
  function query(p) {
    const q = new URLSearchParams({ status: statusOf(p)[1], sort: p.sort === "name" ? "name" : "recent",
      offset: String((pageOf(p) - 1) * PAGE), limit: String(PAGE) });
    if (p.q) q.set("q", p.q.slice(0, 255));
    if (/^(none|-?[0-9]{1,10})$/.test(p.role || "")) q.set("role", p.role);
    return q.toString();
  }

  const load = loader(async () => {
    const q = users.query;
    const [list, roles] = await Promise.all([api.get("api/admin/users?" + q), api.get("api/admin/roles")]);
    return { q, list, roles };
  }, (answer) => {
    users.roles = answer.roles;
    if (sheet && sheet.detail) renderSheet(); // its roles, if it opened first
    if (answer.q !== users.query) return; // the filters changed while it was on its way
    users.data = answer.list;
    render();
  }, ctx, () => visible);

  // Every 10 s while the tab is on screen and the browser tab isn't hidden.
  function schedule() {
    clearTimeout(poll);
    if (visible) poll = setTimeout(() => { if (!document.hidden) load(false); schedule(); }, POLL_MS);
  }

  // ---- rendering ---------------------------------------------------------------
  function userRow(u) {
    const signed = parseTime(u.lastSignInAt), owns = ownsText(u.owns);
    return html`<tr data-key="${u.id}" data-user-row="${u.id}" data-jk tabindex="-1">
      <td class="cell-user"><span class="user-name">${u.name}</span><small>${u.email || "No email"}${u.email && !u.emailVerified
        ? html` <span class="tag">email not verified</span>` : ""}</small></td>
      <td class="cell-roles" data-label="Roles">${rolePills(u)}</td>
      <td class="cell-last" data-label="Last sign-in" title="${fullDate(signed)}">${signed != null ? html`<span data-ago="${u.lastSignInAt}"></span>`
        : html`<span class="dim">Never</span>`}</td>
      <td class="cell-sessions num-col" data-label="Sessions">${u.sessions}</td>
      <td class="cell-owns" data-label="Owns">${owns || html`<span class="dim">—</span>`}</td>
      <td class="cell-status" data-label="Status">${statusPill(u)}</td>
      <td class="cell-actions"><button type="button" class="btn small" data-user="${u.id}" data-fk="manage-${u.id}" aria-label="Manage ${u.name}">Manage</button></td></tr>`;
  }

  // What to do where nobody has signed in yet and single sign-on isn't set up. The steps are filled in as text
  // (fillSetup): the page's own address stands in for GRID_UI_PUBLIC_URL, being where people open the Grid UI.
  function setupMarkup() {
    const step = (label, what, kind, value) => html`<li class="step"><div class="step-label">${label}</div>
      <div class="snippet"><code class="mono"${kind ? html` data-setup="${kind}"` : ""}>${value || ""}</code>
        <button type="button" class="icon-btn tiny" data-copy="${value || ""}"${kind ? html` data-setup="${kind}"` : ""} data-copy-label="${what}"
          aria-label="Copy ${what}">${icon("i-copy")}</button></div></li>`;
    return html`<div class="empty">${icon("i-users", "empty-mark")}
      <h2>Set up single sign-on to see people here</h2>
      <p>People appear here after they first sign in with your identity provider (Entra ID, Google, Okta, Keycloak...). Register the
        Grid UI there as a web application, with a client secret, or without one where the platform gives the Grid UI a token (see the README), then give it these
        settings.</p>
      <ol class="steps">
        ${step("Register this redirect URI with your identity provider", "the redirect URI", "redirect")}
        ${step("Add these to .env next to docker-compose.yaml (or to the Grid UI's environment), with your provider's values", "the settings", "env")}
        ${step("Then restart the Grid UI and sign in with single sign-on", "the command", "", RESTART)}
      </ol></div>`;
  }
  function fillSetup() {
    const base = new URL(".", location.href).href;
    const values = { redirect: base + "auth/callback", env: ["GRID_OIDC_ISSUER=", "GRID_OIDC_CLIENT_ID=", "GRID_OIDC_CLIENT_SECRET=",
      "GRID_UI_PUBLIC_URL=" + base, "GRID_OIDC_ADMINS="].join("\n") };
    el.querySelectorAll("code[data-setup]").forEach((c) => setText(c, values[c.dataset.setup]));
    el.querySelectorAll("button[data-setup]").forEach((b) => { b.dataset.copy = values[b.dataset.setup]; });
  }

  function emptyMarkup(d, nobody) {
    if (!d.ready) {
      return html`<div class="empty">${icon("i-users", "empty-mark")}<h2>People aren't set up in this database yet</h2>
        <p>${d.problem || "It has no users table. The Grid UI creates it on MySQL when its database user may create tables."}</p></div>`;
    }
    if (nobody && !d.ssoConfigured) return setupMarkup();
    if (nobody) {
      return html`<div class="empty">${icon("i-users", "empty-mark")}<h2>Nobody has signed in yet</h2>
        <p>People appear here after their first sign-in.</p></div>`;
    }
    const q = ctx.params.q;
    return html`<div class="empty compact"><h2>No one matches</h2><p>${q ? "Nothing matches “" + q + "” with these filters." : "No one has this role and status."}</p>
      <button type="button" class="btn small" data-clear-users>Clear filters</button></div>`;
  }

  function renderToolbar(d) {
    const p = ctx.params, input = $("#user-filter");
    if (document.activeElement !== input && input.value !== (p.q || "")) input.value = p.q || "";
    const counts = d.counts || { active: 0, disabled: 0 };
    const n = { "": counts.active, disabled: counts.disabled, any: counts.active + counts.disabled };
    el.querySelectorAll("#user-status [data-status]").forEach((b) => {
      setHTML(b, html`${b.dataset.label}${n[b.dataset.status] ? html`<span class="n">${n[b.dataset.status]}</span>` : ""}`);
      b.setAttribute("aria-pressed", String(b.dataset.status === statusOf(p)[0]));
    });
    const roles = (users.roles && users.roles.roles) || [], select = $("#user-role");
    setHTML(select, html`<option value="">Any role</option>${roles.map((r) => html`<option value="${r.id}">${r.name}</option>`)}<option value="none">No role</option>`);
    select.value = p.role || "";
    if (select.selectedIndex === -1) select.value = "";
    $("#user-sort").value = p.sort === "name" ? "name" : "";
    setText($("#user-note"), d.total ? plural(d.total, "person", "people") : "");
  }

  function renderPager(d) {
    const page = pageOf(ctx.params), from = (page - 1) * PAGE;
    $("#user-pager").hidden = !d.ready || (d.total <= PAGE && page === 1);
    setText($("#user-page-at"), d.users.length ? (from + 1) + "–" + (from + d.users.length) + " of " + d.total : "");
    $("#user-pager [data-page='-1']").disabled = page <= 1;
    $("#user-pager [data-page='1']").disabled = from + PAGE >= d.total;
  }

  function render() {
    const d = users.data, p = ctx.params;
    if (!d) return;
    // Past the last page (people left meanwhile): the last page there is.
    if (d.ready && !d.users.length && d.total && pageOf(p) > 1) {
      const last = Math.ceil(d.total / PAGE);
      ctx.go({ page: last > 1 ? String(last) : null });
      return;
    }
    const nobody = d.ready && !(d.counts.active + d.counts.disabled) && !p.q && !p.role;
    const list = d.ready && d.users.length > 0;
    $("#user-toolbar").hidden = !d.ready || nobody;
    $("#user-panel").hidden = !list;
    renderToolbar(d);
    renderPager(d);
    setHTML($("#user-empty"), list ? html`` : emptyMarkup(d, nobody));
    if (!list && nobody && !d.ssoConfigured) fillSetup();
    patchList($("#user-rows"), list ? d.users : [], (u) => u.id, userRow);
    if (app.tick) app.tick();
  }

  // ---- the person's sheet -------------------------------------------------------------------
  // sheet: whose it is (id), what the server says of them (detail, or failed: why it couldn't say), the tab on
  // show, the role checkboxes being posted (busy), the error to show, how many client tokens of theirs still
  // work after the last role taken (tokens), whether the owner picker is out (moving), and whether closing it
  // leaves the address alone (quiet: it closed because the page moved on).
  let sheet = null;

  const findRole = (id) => ((users.roles && users.roles.roles) || []).find((r) => r.id === id) || { id, name: "role " + id };

  function factsMarkup(d) {
    const u = d.user;
    const when = (iso) => (iso ? html`<span title="${fullDate(parseTime(iso))}" data-ago="${iso}"></span>` : html`<span class="dim">Never</span>`);
    // Each group the identity provider sent, and the roles it maps to.
    const mapped = (g) => u.roles.filter((r) => r.source === "group" && r.group === g).map((r) => r.name);
    const more = u.groups.length - GROUPS_SHOWN;
    return html`<dl class="facts user-facts">
      <dt>Subject</dt><dd><span class="copyable"><code class="mono">${u.subject}</code><button type="button" class="icon-btn tiny" data-copy="${u.subject}"
        data-copy-label="the subject" data-fk="copy-subject" aria-label="Copy the subject">${icon("i-copy")}</button></span></dd>
      <dt>Issuer</dt><dd><code class="mono">${u.issuer}</code></dd>
      <dt>First sign-in</dt><dd>${when(u.createdAt)}</dd>
      <dt>Last sign-in</dt><dd>${when(u.lastSignInAt)}</dd>
      <dt>Last seen</dt><dd>${when(u.lastSeenAt)}</dd>
      <dt>Groups at last sign-in</dt><dd>${u.groups.length ? html`<ul class="plain-list user-groups">${u.groups.slice(0, GROUPS_SHOWN).map((g) => {
        const roles = mapped(g);
        return html`<li><code class="mono">${g}</code>${roles.length ? html` <span class="dim">→ ${roles.join(", ")}</span>` : ""}</li>`;
      })}${more > 0 ? html`<li class="dim">and ${more} more</li>` : ""}</ul>` : html`<span class="dim">None</span>`}</dd>
    </dl>
    ${u.groupsOverage ? html`<p class="hint notice">The identity provider sent too many groups to list (over 200), so none count. Map app roles instead, or limit
      the claim to the groups assigned to the application.</p>` : ""}`;
  }

  // The roles, one checkbox each, for roles given by hand. One they have another way is ticked and fixed, and
  // says where it comes from; one with permissions this person lacks can't be given or taken.
  // A box being posted (sheet.busy: role id -> ticked) shows what was asked for, and takes no other change
  // meanwhile; it isn't disabled, which would take focus out of the sheet.
  function rolesMarkup(d, locked) {
    const u = d.user, held = heldRoles(u);
    return html`<fieldset class="perm-list user-roles"><legend>Roles</legend>${((users.roles && users.roles.roles) || []).map((r) => {
      const has = held.find((h) => h.id === r.id), other = has ? has.sources.filter((x) => x.source !== "manual") : [];
      const giving = mayGive(r), fixed = !!has && !has.manual;
      const why = other.length ? (has.manual ? "Also: " : "") + other.map(roleSource).join("; ") : !giving && !locked ? NOT_GIVEN : "";
      const off = !!locked || fixed || !giving, busy = sheet.busy.has(r.id), ticked = busy ? sheet.busy.get(r.id) : !!has;
      return html`<label class="perm-item"${!giving && !locked && !fixed ? html` title="${NOT_GIVEN}"` : ""}>
        <input type="checkbox" data-role-check="${r.id}" data-fk="role-${r.id}" ${ticked ? "checked" : ""} ${off ? "disabled" : ""}${busy ? html` aria-disabled="true"` : ""}>
        <span><strong>${r.name}</strong>${why ? html`<small class="${other.length ? "" : "hint"}">${why}</small>` : ""}</span></label>`;
    })}</fieldset>`;
  }

  // The client tokens someone made still open sessions at the hub after they lose “Make client tokens”.
  const stillOpen = (u, left) => u.name + " can't make client tokens any more, but their " +
    (left === 1 ? "active one still opens" : left + " active ones still open") + " sessions at the hub.";

  // What they may do now, in the role editor's groups, each with the roles that give it.
  function canDoMarkup(d) {
    const names = {}, via = {};
    d.user.roles.forEach((r) => { names[r.id] = r.name; });
    d.permissions.forEach((p) => { via[p.id] = p.via; });
    const groups = permissionGroups(users.roles && users.roles.permissions)
      .map((g) => ({ name: g.name, perms: g.perms.filter((p) => via[p.id]) })).filter((g) => g.perms.length);
    if (!groups.length) return html`<p class="hint">${d.user.disabled ? "Nothing while they're disabled." : "Nothing yet: they have no roles."}</p>`;
    return html`<div class="perm-groups">${groups.map((g) => html`<div class="perm-group"><h4>${g.name}</h4><ul class="plain-list">
      ${g.perms.map((p) => html`<li><span class="glyph g-ok" aria-hidden="true"></span><span>${p.label}<small class="perm-via">via ${
        Array.from(new Set(via[p.id].map((id) => names[id] || "role " + id))).join(", ")}</small></span></li>`)}</ul></div>`)}</div>`;
  }

  function accessPart(d, mine) {
    const u = d.user, locked = mine ? "You can't change your own access." : !d.canManage ? d.cannotManage || MANAGE : "";
    const left = sheet.tokens;
    return html`${rolesMarkup(d, locked)}
      ${locked ? html`<p class="hint">${locked}</p>` : ""}
      ${left ? html`<div class="hint notice user-left">${stillOpen(u, left)}
        <button type="button" class="btn small" data-revoke-tokens="client" data-fk="revoke-left">${left === 1 ? "Revoke it" : "Revoke them"}</button></div>` : ""}
      <h3 class="section-label">What they can do</h3>
      ${canDoMarkup(d)}`;
  }

  function sessionsPart(d, mine) {
    const may = auth.can("users.manage") && (d.canManage || mine);
    if (!d.sessions.length) return html`<p class="hint">${d.user.disabled ? "None: they're disabled." : "None: they aren't signed in anywhere."}</p>`;
    return html`<ul class="plain-list user-sessions">${d.sessions.map((x) => {
      const ua = x.userAgent || "", cut = ua.length > UA_LENGTH ? ua.slice(0, UA_LENGTH - 1) + "…" : ua;
      return html`<li class="user-session">
        <div class="user-session-what"><span class="ua"${ua ? html` title="${ua}"` : ""}>${cut || "Unknown browser"}</span>${x.current
          ? html` <span class="tag">This browser</span>` : ""}
          <small>Started <span data-ago="${x.createdAt}"></span> · last seen <span data-ago="${x.lastSeenAt}"></span> · ${x.address || "address unknown"}
            · ends <span data-until="${x.expiresAt}"></span></small></div>
        ${may ? html`<button type="button" class="btn ghost small" data-end-session="${x.id}" data-fk="end-${x.id}"
          aria-label="End the session of ${x.current ? "this browser" : (cut || "an unknown browser") + " at " + (x.address || "an unknown address")}">End</button>` : ""}</li>`;
    })}</ul>
    ${may && d.sessions.length > 1 ? html`<button type="button" class="btn small user-end-all" data-end-session="all" data-fk="end-all">End all sessions</button>` : ""}`;
  }

  function ownsPart(d, mine) {
    const o = d.owns, revoke = auth.can("users.manage") && d.canManage;
    const moving = auth.can("users.manage") && auth.can("mfa.all") && auth.can("signins.all") && (d.canManage || mine) && (o.mfa.length || o.signins.length);
    const part = (title, items, href, extra) => html`<section class="user-owns"><h3 class="section-label">${title}<span class="count">${items.length}</span></h3>
      ${items.length ? html`<ul class="plain-list">${items.map((x) => html`<li><a href="${href}">${x.name}</a>${extra ? extra(x) : ""}</li>`)}</ul>`
        : html`<p class="hint">None.</p>`}</section>`;
    const client = activeTokens(d, "client").length, agent = activeTokens(d, "agent").length;
    return html`${part("MFA secrets", o.mfa, "#/mfa")}${part("Saved sign-ins", o.signins, "#/mfa")}
      ${part("Tokens", o.tokens, "#/access", (t) => html` <span class="dim">· ${t.kind} · ${t.status}</span>`)}
      ${(revoke && (client || agent)) || moving ? html`<div class="user-owns-actions">
        ${revoke && client ? html`<button type="button" class="btn small destructive" data-revoke-tokens="client" data-fk="revoke-client">Revoke their client tokens</button>` : ""}
        ${revoke && agent ? html`<button type="button" class="btn small destructive" data-revoke-tokens="agent" data-fk="revoke-agent">Revoke their agent tokens</button>` : ""}
        ${moving && !sheet.moving ? html`<button type="button" class="btn small" data-move-items data-fk="move-items">Move all to…</button>` : ""}
      </div>` : ""}
      ${moving && sheet.moving ? html`<div class="field user-move">
        <label for="user-move-input">Give their MFA secrets and saved sign-ins to</label>
        <div id="user-move-pick"></div>
        <p class="hint" id="user-move-hint">Their tokens stay theirs.</p>
        <button type="button" class="btn ghost small" data-keep-items data-fk="keep-items">Cancel</button>
      </div>` : ""}`;
  }

  function historyPart(d) {
    return html`${d.history.length ? html`<ul class="plain-list user-history">${d.history.map((e) => html`<li>
        <span class="dim user-history-when" title="${fullDate(parseTime(e.occurredAt))}" data-ago="${e.occurredAt}"></span>
        <span><strong>${e.actorName}</strong> ${sentence(e)}</span></li>`)}</ul>` : html`<p class="hint">Nothing recorded yet.</p>`}
      <a class="btn small user-audit-link" href="${router.build("admin", { tab: "audit", actor: d.user.id })}">Open in audit log</a>`;
  }

  const PARTS = { access: accessPart, sessions: sessionsPart, owns: ownsPart, history: historyPart };

  function sheetMarkup(s) {
    const d = s.detail, close = html`<button type="button" class="icon-btn" data-dismiss data-fk="user-close" aria-label="Close">${icon("i-close")}</button>`;
    if (!d) {
      return html`<div class="sheet-head"><h2 id="user-title">${s.failed ? "Can't show this person" : "Loading…"}</h2>${close}</div>
        ${s.failed ? html`<p class="form-error">${s.failed}</p>` : ""}`;
    }
    const u = d.user, me = auth.state().user, mine = !!(me && me.id === u.id);
    const tabs = SHEET_TABS.filter(([id]) => id !== "history" || d.history);
    const tab = tabs.some(([id]) => id === s.tab) ? s.tab : "access";
    const manage = auth.can("users.manage") && d.canManage;
    return html`<div class="sheet-head user-head">
        <div class="user-who"><div class="user-title-line"><h2 id="user-title">${u.name}</h2>${statusPill(u)}</div>
          <p class="sheet-sub">${[u.email, "Signed in with " + (auth.state().ssoLabel || "single sign-on")].filter(Boolean).join(" · ")}</p></div>
        ${close}
      </div>
      ${factsMarkup(d)}
      <div class="tabs-row user-tabs"><div class="tabs" role="tablist" aria-label="${u.name}">
        ${tabs.map(([id, label]) => html`<button type="button" role="tab" id="user-tab-${id}" data-user-tab="${id}" data-fk="user-tab-${id}"
          aria-controls="user-part-${id}" aria-selected="${String(id === tab)}" tabindex="${id === tab ? 0 : -1}">${label}</button>`)}
      </div></div>
      ${tabs.map(([id]) => html`<div class="user-part" role="tabpanel" id="user-part-${id}" aria-labelledby="user-tab-${id}" ${id === tab ? "" : "hidden"}>
        ${PARTS[id](d, mine)}</div>`)}
      <p class="form-error" id="user-error" role="alert" ${s.error ? "" : "hidden"}>${s.error}</p>
      ${manage ? html`<div class="dialog-actions">${u.disabled ? html`<button type="button" class="btn" data-enable-user data-fk="enable">Enable</button>`
        : html`<button type="button" class="btn destructive" data-disable-user data-fk="disable">Disable</button>`}</div>` : ""}`;
  }

  // Draws the sheet again; whatever had focus in it keeps it, or its stand-in does (data-fk), else the tab.
  function renderSheet() {
    const s = sheet;
    if (!s) return;
    const active = document.activeElement, inside = !!(active && dlg.contains(active)), fk = inside && active.dataset ? active.dataset.fk : null;
    setHTML(dlg.querySelector("#user-body"), sheetMarkup(s));
    if (inside && !active.isConnected) {
      const again = (fk && dlg.querySelector('[data-fk="' + CSS.escape(fk) + '"]')) || dlg.querySelector("[role=tab][aria-selected=true]") ||
        dlg.querySelector("[data-dismiss]");
      if (again) again.focus({ preventScroll: true });
    }
    if (s.moving && !dlg.querySelector("#user-move-input")) mountMover();
    if (app.tick) app.tick();
  }

  async function refreshSheet() {
    const s = sheet;
    if (!s) return;
    try {
      const detail = await api.get("api/admin/users/" + encodeURIComponent(s.id));
      if (sheet !== s) return;
      s.detail = detail;
      s.failed = "";
    } catch (err) {
      if (sheet !== s) return;
      s.detail = null;
      s.failed = err.message;
    }
    renderSheet();
  }

  function openSheet(id) {
    sheet = { id, detail: null, failed: "", tab: "access", busy: new Map(), error: "", tokens: 0, moving: false, quiet: false };
    dlg.__html = null; // always fresh elements
    // The status line stays put while the rest is drawn again, so screen readers announce what goes in it.
    setHTML(dlg, html`<div id="user-body"></div><p class="sr-only" id="user-said" role="status"></p>`);
    renderSheet();
    if (!dlg.open) dlg.showModal();
    dlg.querySelector("[data-dismiss]").focus();
    refreshSheet().then(() => {
      // Once it's there, on to its first tab, unless focus moved on meanwhile.
      const tab = dlg.querySelector("[role=tab][aria-selected=true]");
      if (tab && document.activeElement === dlg.querySelector("[data-dismiss]")) tab.focus();
    });
  }

  function closeSheet(quiet) {
    if (!sheet) return;
    sheet.quiet = quiet;
    if (dlg.open) dlg.close();
  }

  // ?user=<id> opens that person's sheet, and its going closes it.
  function syncSheet() {
    const id = /^[0-9]{1,10}$/.test(ctx.params.user || "") ? Number(ctx.params.user) : null;
    if (id == null) { if (sheet) closeSheet(true); return; }
    if (sheet && sheet.id === id && dlg.open) { if (sheet.detail) renderSheet(); return; }
    openSheet(id);
  }

  dlg.addEventListener("close", () => {
    if (dlg.open || !sheet) return; // an earlier close's event, after it opened again
    const s = sheet;
    sheet = null;
    if (s.quiet) return;
    ctx.go({ user: null });
    const btn = el.querySelector('[data-fk="manage-' + s.id + '"]');
    if (btn) btn.focus();
  });

  // ---- changes --------------------------------------------------------------------
  // After each: the sheet and the list as the server has them now.
  async function settled() {
    await refreshSheet();
    load(true);
  }

  // What a change did, in the sheet's status line while it's open: a toast would sit under it, out of reach.
  function say(text) {
    if (dlg.open) setText(dlg.querySelector("#user-said"), text);
    else toast(text);
  }

  async function toggleRole(box) {
    const s = sheet, u = s.detail.user, roleId = Number(box.dataset.roleCheck), add = box.checked, role = findRole(roleId);
    s.busy.set(roleId, add);
    s.error = "";
    box.setAttribute("aria-disabled", "true"); // as rolesMarkup draws it, should the sheet be drawn meanwhile
    let res;
    try {
      res = await api.post("api/admin/users/" + encodeURIComponent(s.id) + "/roles/" + (add ? "add" : "remove"), { role: roleId });
      if (res) {
        // Without “Make client tokens” now, the client tokens they made still work at the hub until revoked.
        if (!add) s.tokens = res.activeClientTokens || 0;
        say((add ? "Gave " + u.name + " the role " + role.name + "." : "Took the role " + role.name + " from " + u.name + ".") +
          (!add && s.tokens ? " " + stillOpen(u, s.tokens) : ""));
      }
    } catch (err) {
      s.error = err.message;
    } finally {
      s.busy.delete(roleId);
    }
    // Back as it was when nothing changed. The sheet drawn below may keep this very box: the same refusal
    // again draws nothing new.
    if (box.isConnected) {
      box.removeAttribute("aria-disabled");
      if (!res) box.checked = !add;
    }
    if (res === null) return renderSheet(); // sign-in dismissed
    await settled();
  }

  async function revokeTokens(kind) {
    const s = sheet, d = s.detail, u = d.user, tokens = activeTokens(d, kind), n = Math.max(tokens.length, kind === "client" ? s.tokens : 0);
    const ok = await confirm({
      title: "Revoke " + possessive(u.name) + " " + plural(n, kind + " token") + "?",
      text: kind === "client" ? "Checks using " + (n === 1 ? "it" : "them") + " stop connecting to the hub within about 10 s."
        : "The hosts using " + (n === 1 ? "it" : "them") + " disconnect, and can't connect with " + (n === 1 ? "it" : "them") + " again.",
      items: tokens.map((t) => ({ text: t.name, tone: "ok", label: "Active" })),
      note: "This can't be undone.", ok: n === 1 ? "Revoke token" : "Revoke tokens",
    });
    if (!ok) return;
    s.error = "";
    try {
      const res = await api.post("api/admin/users/" + encodeURIComponent(s.id) + "/tokens/revoke", { [kind]: true });
      if (res === null) return;
      say("Revoked " + possessive(u.name) + " " + plural(res.revoked[kind], kind + " token") + ".");
      if (kind === "client") s.tokens = 0;
    } catch (err) {
      s.error = err.message;
    }
    await settled();
  }

  async function endSession(which) {
    const s = sheet, d = s.detail, u = d.user, all = which === "all";
    const x = all ? null : d.sessions.find((y) => String(y.id) === which);
    if (!all && !x) return;
    const ok = await confirm({
      title: all ? "End all of " + possessive(u.name) + " sessions?" : x.current ? "End this browser's session?" : "End this session?",
      text: all ? "Every browser they're signed in with is signed out at its next request."
        : (x.current ? "This browser" : "That browser") + " is signed out at its next request.",
      ok: all ? "End sessions" : "End session", danger: false,
    });
    if (!ok) return;
    s.error = "";
    try {
      const res = await api.post("api/admin/users/" + encodeURIComponent(s.id) + "/sessions/revoke", all ? {} : { sessionId: x.id });
      if (res === null) return;
      say(res.ended === 1 ? "Ended the session." : res.ended ? "Ended " + res.ended + " sessions." : "It had ended already.");
    } catch (err) {
      s.error = err.message;
    }
    await settled();
  }

  async function disable() {
    const s = sheet, d = s.detail, u = d.user, n = d.sessions.length;
    const client = activeTokens(d, "client").length, agent = activeTokens(d, "agent").length;
    const checks = [];
    if (client) {
      checks.push({ id: "client", checked: true,
        label: "Also revoke their " + plural(client, "client token") + " (checks using them stop connecting to the hub within about 10 s)" });
    }
    if (agent) checks.push({ id: "agent", checked: false, label: "Also revoke their " + plural(agent, "agent token") + " (those hosts disconnect)" });
    const answer = await confirm({
      title: "Disable " + u.name + "?",
      text: "They can't sign in until an admin enables them. " + (n ? (n === 1 ? "Their session ends" : "Their " + n + " sessions end") + " now, and their"
        : "Their") + " checks' client tokens stop getting MFA codes and saved sign-ins.",
      facts: [["Sessions", String(n)], ["Client tokens", client + " active"], ["Agent tokens", agent + " active"],
        ["Owns", plural(d.owns.mfa.length, "secret") + ", " + plural(d.owns.signins.length, "sign-in") + " (they stay theirs)"]],
      checks, note: checks.length ? "Revoking can't be undone." : "", ok: "Disable", danger: false,
    });
    if (!answer.ok) return;
    s.error = "";
    try {
      const res = await api.post("api/admin/users/" + encodeURIComponent(s.id) + "/disable",
        { revokeClientTokens: !!answer.checked.client, revokeAgentTokens: !!answer.checked.agent });
      if (res === null) return;
      const revoked = both([res.tokensRevoked.client && plural(res.tokensRevoked.client, "client token"),
        res.tokensRevoked.agent && plural(res.tokensRevoked.agent, "agent token")]);
      say("Disabled " + u.name + (revoked ? " and revoked " + revoked : "") + ".");
    } catch (err) {
      s.error = err.message;
    }
    await settled();
  }

  async function enable() {
    const s = sheet, u = s.detail.user;
    s.error = "";
    try {
      const res = await api.post("api/admin/users/" + encodeURIComponent(s.id) + "/enable", {});
      if (res === null) return;
      say("Enabled " + u.name + ": they can sign in again.");
    } catch (err) {
      s.error = err.message;
    }
    await settled();
  }

  // Giving everything they own of MFA secrets and saved sign-ins to someone else, or to the admins.
  function mountMover() {
    const host = dlg.querySelector("#user-move-pick"), s = sheet;
    if (!host || !s) return;
    createPicker(host, {
      id: "user-move", placeholder: "Find a person, or Admins", describedBy: "user-move-hint",
      async options(q) {
        const found = await directory(q), text = q.toLowerCase();
        const admins = "admins".indexOf(text) !== -1 ? [{ key: "admins", label: "Admins", userId: null }] : [];
        return [{ label: "", options: admins }, { label: "People", options: found.users.filter((x) => x.id !== s.id)
          .map((x) => ({ key: "user:" + x.id, label: x.name, meta: x.email || "", userId: x.id })) }];
      },
      empty: (q) => (q.length < 2 ? "Type 2 letters or more to find people." : "No one matches “" + q + "”."),
      choose: moveItems,
    }).focus();
  }

  async function moveItems(o) {
    const s = sheet, d = s.detail, u = d.user, whose = o.userId == null ? "the admins'" : possessive(o.label);
    const ok = await confirm({
      title: "Give what " + u.name + " owns to " + (o.userId == null ? "the admins" : o.label) + "?",
      text: "Their MFA secrets and saved sign-ins become " + whose + ". Their tokens stay theirs.",
      facts: [["MFA secrets", String(d.owns.mfa.length)], ["Saved sign-ins", String(d.owns.signins.length)]],
      note: "Saved sign-ins lose their shares, what's saved for them and any sign-in under way; MFA secrets keep their shares.",
      ok: "Give them", danger: false,
    });
    if (!ok) return false;
    s.error = "";
    try {
      const res = await api.post("api/admin/users/" + encodeURIComponent(s.id) + "/move-items", { toUserId: o.userId });
      if (res === null) return false;
      say("Gave " + (both([res.moved.mfa && plural(res.moved.mfa, "MFA secret"), res.moved.signins && plural(res.moved.signins, "saved sign-in")]) ||
        "nothing") + " to " + (o.userId == null ? "the admins" : o.label) + ".");
      s.moving = false;
    } catch (err) {
      s.error = err.message;
    }
    await settled();
    return true;
  }

  // ---- events -----------------------------------------------------------------------
  el.addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-user]"))) return ctx.go({ user: b.dataset.user });
    if ((b = e.target.closest("#user-status [data-status]"))) return ctx.go({ status: b.dataset.status || null, page: null });
    if ((b = e.target.closest("[data-page]"))) {
      const page = pageOf(ctx.params) + Number(b.dataset.page);
      ctx.go({ page: page > 1 ? String(page) : null });
      return;
    }
    if (e.target.closest("[data-clear-users]")) {
      $("#user-filter").value = "";
      ctx.go({ q: null, status: null, role: null, page: null });
      $("#user-filter").focus();
    }
  });
  $("#user-filter").addEventListener("input", (e) => {
    clearTimeout(typed);
    typed = setTimeout(() => ctx.go({ q: e.target.value.trim() || null, page: null }), SEARCH_MS);
  });
  $("#user-role").addEventListener("change", (e) => ctx.go({ role: e.target.value || null, page: null }));
  $("#user-sort").addEventListener("change", (e) => ctx.go({ sort: e.target.value || null, page: null }));
  // Enter on a row opens the person, as Manage does.
  $("#user-rows").addEventListener("keydown", (e) => {
    const row = e.target.closest && e.target.closest("[data-user-row]");
    if (e.key === "Enter" && row && row === e.target) {
      e.preventDefault();
      ctx.go({ user: row.dataset.userRow });
    }
  });

  dlg.addEventListener("click", (e) => {
    if (!sheet || !sheet.detail) return;
    let b;
    // A role on its way to the server takes no other change meanwhile.
    if (e.target.closest("[data-role-check][aria-disabled=true]")) { e.preventDefault(); return; }
    if ((b = e.target.closest("[data-user-tab]"))) { sheet.tab = b.dataset.userTab; renderSheet(); return; }
    if ((b = e.target.closest("[data-end-session]"))) return endSession(b.dataset.endSession);
    if ((b = e.target.closest("[data-revoke-tokens]"))) return revokeTokens(b.dataset.revokeTokens);
    if (e.target.closest("[data-move-items]")) { sheet.moving = true; sheet.error = ""; renderSheet(); return; }
    if (e.target.closest("[data-keep-items]")) {
      sheet.moving = false;
      renderSheet();
      const btn = dlg.querySelector("[data-move-items]");
      if (btn) btn.focus();
      return;
    }
    if (e.target.closest("[data-disable-user]")) return disable();
    if (e.target.closest("[data-enable-user]")) return enable();
  });
  dlg.addEventListener("change", (e) => {
    const box = e.target.closest("[data-role-check]:not([aria-disabled=true])");
    if (box && sheet && sheet.detail) toggleRole(box);
  });
  // The sheet's tablist: arrows move along its tabs, Home and End go to the ends.
  dlg.addEventListener("keydown", (e) => {
    const tab = e.target.closest && e.target.closest("[data-user-tab]");
    if (!tab || !["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const ids = Array.from(dlg.querySelectorAll("[data-user-tab]")).map((x) => x.dataset.userTab), i = ids.indexOf(tab.dataset.userTab);
    const j = e.key === "Home" ? 0 : e.key === "End" ? ids.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + ids.length) % ids.length;
    sheet.tab = ids[j];
    renderSheet();
    dlg.querySelector("#user-tab-" + ids[j]).focus();
  });

  return {
    show() {
      visible = true;
      const q = query(ctx.params);
      if (q !== users.query) {
        users.query = q;
        if (users.data) renderToolbar(users.data);
        load.forget();
        load(true);
      } else {
        load(false);
      }
      schedule();
      syncSheet();
    },
    hide() {
      visible = false;
      clearTimeout(poll);
      closeSheet(true);
    },
    load(force) { if (force) load.forget(); return load(force); },
    items: () => (users.data && users.data.users) || [],
  };
}
