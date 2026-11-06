// Admin, Audit log: every recorded change, newest first, a page at a time. The filters (who did it, what
// kind of change, and what it was about) live in the address. Each change reads as a sentence, built as
// text: the names in it come from people, the identity provider and the database.
import { app } from "../../app.js";
import * as api from "../../api.js";
import * as auth from "../../auth.js";
import { html, icon, patchList, setHTML, setText } from "../../html.js";
import { fullDate, parseTime, plural } from "../../format.js";
import { toast } from "../../ui/toast.js";
import { both, loader, permissionGroups, possessive } from "./common.js";

const PAGE = 50;
// The kinds of change the log can be narrowed to (the first part of each action), as the chips say them.
const CATEGORIES = [["auth", "Sign-ins"], ["user", "People"], ["role", "Roles"], ["group", "Group mappings"], ["token", "Tokens"],
  ["mfa", "MFA secrets"], ["signin", "Saved sign-ins"], ["grid", "Grid"], ["access", "Refusals"]];
// How whoever made a change signed in.
const METHOD = { oidc: "single sign-on", token: "admin token", "client-token": "check", "client-bootstrap": "check" };
const TARGET = /^(user|role|group|token|mfa|signin|slot|node):[0-9]{1,10}$/;
const TARGET_WORD = { user: "person", role: "role", group: "group mapping", token: "token", mfa: "MFA secret", signin: "saved sign-in",
  slot: "slot", node: "node" };

const quoted = (s) => "“" + s + "”";
// Groups the identity provider named, as a sign-in's row lists them: ‘qa’, ‘staff’.
const groupList = (groups) => (groups || []).map((g) => "‘" + g + "’").join(", ");
// Who something was given to, as an audit row names them: { id, name }, or null for the admins.
const toWhom = (who) => (who ? who.name || "user " + who.id : "the admins");

// One change as a sentence, without its subject (the Who column says who): "gave Dana Lee the role Operator".
// Text, never markup.
export function sentence(e) {
  const d = e.details || {}, name = e.targetName || (e.targetKind ? (TARGET_WORD[e.targetKind] || e.targetKind) + " " + e.targetId : "");
  const role = d.role ? d.role.name || "role " + d.role.id : "a role";
  const grantee = d.grantee ? d.grantee.name || d.grantee.kind + " " + d.grantee.id : "someone";
  const everyone = d.group === "*";
  switch (e.action) {
    case "auth.signin": return "signed in";
    case "auth.signin_refused":
      return d.reason === "disabled" ? "couldn't sign in: they're disabled on this grid" : "couldn't sign in: the identity provider's answer was refused" +
        (d.reason ? " (" + d.reason + ")" : "");
    case "auth.signout": return d.everywhere ? "signed out everywhere" : "signed out";
    case "auth.breakglass": return "signed in with the admin token";
    // The groups a new person signed in with are on this row: their first sign-in records no change of groups.
    case "user.created":
      return e.actorId != null && e.actorId === e.targetId
        ? "signed in for the first time" + (d.groups && d.groups.length ? ", in " + groupList(d.groups) : "") : "added " + name;
    case "user.groups_changed":
      return "signed in with other groups: " + [d.added && d.added.length && "now in " + groupList(d.added),
        d.removed && d.removed.length && "no longer in " + groupList(d.removed)].filter(Boolean).join("; ");
    case "user.role_added": return "gave " + name + " the role " + role;
    case "user.role_removed": return "took the role " + role + " from " + name;
    case "user.disabled": {
      const revoked = both([d.clientTokens && plural(d.clientTokens, "client token"), d.agentTokens && plural(d.agentTokens, "agent token")]);
      return "disabled " + name + (revoked ? " and revoked " + revoked : "");
    }
    case "user.enabled": return "enabled " + name;
    case "user.sessions_ended": return "ended " + possessive(name) + " " + (d.sessions === 1 ? "session" : plural(d.sessions || 0, "session"));
    case "user.tokens_revoked":
      return "revoked " + possessive(name) + " " + (both([d.client && plural(d.client, "client token"), d.agent && plural(d.agent, "agent token")]) || "tokens");
    case "user.items_moved":
      return "gave " + possessive(name) + " " + (both([d.mfa && plural(d.mfa, "MFA secret"), d.signins && plural(d.signins, "saved sign-in")]) ||
        "MFA secrets and saved sign-ins") + " to " + toWhom(d.to);
    case "role.created": return "made the role " + name;
    case "role.updated": {
      const before = d.before || {}, after = d.after || {};
      const renamed = before.name && after.name && before.name !== after.name;
      const changed = JSON.stringify(before.permissions || []) !== JSON.stringify(after.permissions || []);
      if (renamed) return "renamed the role " + before.name + " to " + after.name + (changed ? " and changed what it lets people do" : "");
      return changed ? "changed what the role " + name + " lets people do" : "changed the role " + name;
    }
    case "role.deleted": return "deleted the role " + name;
    case "group.mapped": return everyone ? "gave everyone who signs in the role " + role : "mapped the group ‘" + (d.group || name) + "’ to the role " + role;
    case "group.unmapped":
      return everyone ? "stopped giving everyone who signs in the role " + role : "stopped mapping the group ‘" + (d.group || name) + "’ to the role " + role;
    case "token.created": return "made the " + (d.kind ? d.kind + " " : "") + "token " + quoted(name);
    case "token.revoked": return "revoked the token " + quoted(name);
    case "token.deleted": return "deleted the token " + quoted(name);
    case "token.owner_set": return "gave the token " + quoted(name) + " to " + toWhom(d.to);
    case "mfa.created": return "added the MFA secret " + quoted(name);
    case "mfa.deleted": return "deleted the MFA secret " + quoted(name);
    case "mfa.code_viewed": return "looked at a code of " + quoted(name);
    case "mfa.code_used": return "took a code of " + quoted(name);
    case "signin.created": return "started the saved sign-in " + quoted(name);
    case "signin.forgotten": return "forgot what was saved for " + quoted(name);
    case "signin.deleted": return "deleted the saved sign-in " + quoted(name);
    case "mfa.shared": case "signin.shared": return "shared " + quoted(name) + " with " + grantee;
    case "mfa.unshared": case "signin.unshared": return "stopped sharing " + quoted(name) + " with " + grantee;
    case "mfa.owner_changed": return "gave the MFA secret " + quoted(name) + " to " + toWhom(d.to);
    case "signin.owner_changed": return "gave the saved sign-in " + quoted(name) + " to " + toWhom(d.to);
    case "grid.slot_released": return "released the slot " + name;
    case "grid.slot_reset": return "reset the failures of the slot " + name;
    case "grid.node_reset": return "reset the failures of " + name;
    case "grid.node_forgotten": return "forgot the node " + name;
    case "access.denied": return "was refused" + (d.route ? " " + d.route : "") + (d.permission ? ", without the permission " + d.permission : "");
  }
  return e.action + (name ? " " + name : "");
}

// What the row expander shows: a role's permissions before and after a change, or the details as recorded.
function detailsMarkup(e, labels) {
  const d = e.details || {};
  if (e.action === "role.updated" && d.before && d.after) {
    const side = (r) => html`<strong>${r.name}</strong>: ${(r.permissions || []).length ? (r.permissions || []).map((p) => labels[p] || p).join(", ")
      : html`<span class="dim">no permissions</span>`}`;
    return html`<dl class="confirm-facts audit-diff"><dt>Before</dt><dd>${side(d.before)}</dd><dt>After</dt><dd>${side(d.after)}</dd></dl>`;
  }
  return html`<pre class="code audit-raw">${JSON.stringify(d, null, 2)}</pre>`;
}

export function createAudit(el, ctx) {
  setHTML(el, html`
    <div class="toolbar audit-toolbar">
      <label class="select-field"><span>Who</span><select id="audit-actor"></select></label>
      <div class="chips" role="group" aria-label="What changed" id="audit-category">
        ${CATEGORIES.map(([id, label]) => html`<button type="button" class="chip" data-category="${id}" aria-pressed="false">${label}</button>`)}
      </div>
      <span id="audit-target"></span>
    </div>
    <div class="panel table-panel" id="audit-panel" hidden>
      <table class="table audit">
        <caption class="sr-only">Recorded changes, newest first</caption>
        <thead><tr><th scope="col">When</th><th scope="col">Who</th><th scope="col">What</th><th scope="col">Address</th></tr></thead>
        <tbody id="audit-rows"></tbody>
      </table>
    </div>
    <div id="audit-empty"><div class="empty"><p>Loading…</p></div></div>
    <div class="audit-foot" id="audit-foot" hidden>
      <button type="button" class="btn small" id="audit-older" hidden>Load older</button>
      <p class="hint" id="audit-kept"></p>
    </div>`);

  const $ = (sel) => el.querySelector(sel);
  // data: the newest page's answer; entries: every row shown, that page's and older ones loaded since; next:
  // where the page after the last one starts; query: the filters they were loaded for; people: whom the Who
  // filter offers; open: the rows expanded; labels: permission ids' labels, for a role's changes.
  const audit = { data: null, entries: [], next: null, query: null, people: [], open: new Set(), labels: null, older: false };
  let visible = false;

  // ---- loading ---------------------------------------------------------------
  function query(p) {
    const q = new URLSearchParams({ limit: String(PAGE) });
    if (p.actor === "token" || p.actor === "checks" || /^[0-9]{1,10}$/.test(p.actor || "")) q.set("actor", p.actor);
    if (CATEGORIES.some(([id]) => id === p.category)) q.set("category", p.category);
    if (TARGET.test(p.target || "")) q.set("target", p.target);
    return q.toString();
  }

  const load = loader(async () => {
    const q = audit.query;
    // Everyone, for the Who filter, takes seeing people; without it, it offers whoever the rows show.
    const [page, people, labels] = await Promise.all([api.get("api/admin/audit?" + q),
      auth.can("users.view") ? api.get("api/admin/users?status=all&sort=name&limit=200") : null,
      audit.labels || api.get("api/permissions").then((b) => {
        const out = {};
        permissionGroups(b.permissions).forEach((g) => g.perms.forEach((p) => { out[p.id] = p.label; }));
        return out;
      })]);
    return { q, page, people: people ? people.users.map((u) => ({ id: u.id, name: u.name })) : null, labels };
  }, (answer) => {
    audit.labels = answer.labels;
    if (answer.q !== audit.query) return; // the filters changed while it was on its way
    audit.data = answer.page;
    audit.entries = answer.page.entries;
    audit.next = answer.page.next;
    if (answer.people) audit.people = answer.people;
    render();
  }, ctx, () => visible);

  async function older() {
    const btn = $("#audit-older"), q = audit.query;
    if (!audit.next || audit.older) return;
    audit.older = true;
    btn.disabled = true;
    const shown = audit.entries.length;
    try {
      const page = await api.get("api/admin/audit?" + q + "&before=" + encodeURIComponent(audit.next));
      if (q !== audit.query) return;
      const have = new Set(audit.entries.map((e) => e.id));
      audit.entries = audit.entries.concat(page.entries.filter((e) => !have.has(e.id)));
      audit.next = page.next;
      render();
      // Focus stays on Load older while there's more; once it goes, on to the first row it brought.
      const row = $("#audit-rows").children[shown];
      if (row && !(audit.next && btn.isConnected)) row.focus();
    } catch (err) {
      toast(err.message, true);
    } finally {
      audit.older = false;
      btn.disabled = false;
    }
  }

  // ---- rendering ---------------------------------------------------------------
  function renderFilters() {
    const p = ctx.params, people = audit.people.slice();
    // Whoever made a change here, which a list of everyone (without "See people and roles") may lack.
    audit.entries.forEach((e) => {
      if (e.actorId != null && e.actorMethod === "oidc" && !people.some((x) => x.id === e.actorId)) people.push({ id: e.actorId, name: e.actorName });
    });
    const actor = /^[0-9]{1,10}$/.test(p.actor || "") ? Number(p.actor) : null;
    if (actor != null && !people.some((x) => x.id === actor)) people.push({ id: actor, name: "user " + actor });
    const select = $("#audit-actor");
    setHTML(select, html`<option value="">Anyone</option><option value="token">Admin token</option><option value="checks">Checks</option>
      ${people.map((x) => html`<option value="${x.id}">${x.name}</option>`)}`);
    select.value = query(p).indexOf("actor=") === -1 ? "" : p.actor;
    el.querySelectorAll("#audit-category [data-category]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.category === p.category)));
    // What a sheet or a link narrowed the log to: one person, role or item.
    const target = TARGET.test(p.target || "") ? p.target : null;
    let label = "";
    if (target) {
      const [kind, id] = target.split(":"), seen = audit.entries.find((e) => e.targetKind === kind && String(e.targetId) === id);
      const named = seen ? seen.targetName : kind === "user" ? (people.find((x) => String(x.id) === id) || {}).name : null;
      label = "About " + (named || TARGET_WORD[kind] + " " + id);
    }
    setHTML($("#audit-target"), target ? html`<span class="chip audit-about"><span>${label}</span>
      <button type="button" class="icon-btn tiny" data-clear-target aria-label="Show changes to anything">${icon("i-close")}</button></span>` : html``);
  }

  function row(e) {
    const open = audit.open.has(e.id), more = Object.keys(e.details || {}).length > 0;
    return html`<tr data-key="${e.id}" data-jk tabindex="-1">
      <td class="cell-when" title="${fullDate(parseTime(e.occurredAt))}"><span data-ago="${e.occurredAt}"></span></td>
      <td class="cell-who" data-label="Who"><span class="audit-who"><span>${e.actorName}</span><small>${METHOD[e.actorMethod] || e.actorMethod}</small></span></td>
      <td class="cell-what what" data-label="What"><div class="audit-line"><span>${sentence(e)}</span>${more ? html`<button type="button" class="icon-btn tiny audit-more"
          data-expand="${e.id}" data-fk="expand-${e.id}" aria-expanded="${String(open)}" aria-controls="audit-more-${e.id}"
          aria-label="Details: ${e.actorName} ${sentence(e)}">${icon("i-chevron")}</button>` : ""}</div>
        ${more ? html`<div class="audit-details" id="audit-more-${e.id}" ${open ? "" : "hidden"}>${detailsMarkup(e, audit.labels || {})}</div>` : ""}</td>
      <td class="cell-address" data-label="Address">${e.address ? html`<code class="mono">${e.address}</code>` : html`<span class="dim">–</span>`}</td></tr>`;
  }

  function emptyMarkup(d) {
    if (!d.ready) {
      return html`<div class="empty">${icon("i-sessions", "empty-mark")}<h2>The audit log isn't set up in this database yet</h2>
        <p>It has no auditLog table. The Grid UI creates it on MySQL when its database user may create tables, and records changes from then on.</p></div>`;
    }
    if (query(ctx.params) !== query({})) {
      return html`<div class="empty compact"><h2>Nothing recorded matches</h2><p>No change in the log has this person, kind and subject.</p>
        <button type="button" class="btn small" data-clear-audit>Clear filters</button></div>`;
    }
    return html`<div class="empty">${icon("i-sessions", "empty-mark")}<h2>Nothing recorded yet</h2>
      <p>Sign-ins, and changes to people, roles, tokens, MFA secrets, saved sign-ins and the grid, show here as they happen.</p></div>`;
  }

  function render() {
    const d = audit.data;
    renderFilters();
    if (!d) return;
    const list = audit.entries.length > 0;
    $("#audit-panel").hidden = !list;
    setHTML($("#audit-empty"), list ? html`` : emptyMarkup(d));
    patchList($("#audit-rows"), audit.entries, (e) => e.id, row);
    $("#audit-foot").hidden = !d.ready;
    $("#audit-older").hidden = !audit.next;
    setText($("#audit-kept"), "Kept for " + plural(d.keptDays, "day") + " (GRID_AUDIT_DAYS).");
    if (app.tick) app.tick();
  }

  // ---- events -----------------------------------------------------------------------
  el.addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-category]"))) return ctx.go({ category: b.dataset.category === ctx.params.category ? null : b.dataset.category });
    if (e.target.closest("[data-clear-target]")) { ctx.go({ target: null }); $("#audit-actor").focus(); return; }
    if (e.target.closest("[data-clear-audit]")) { ctx.go({ actor: null, category: null, target: null }); $("#audit-actor").focus(); return; }
    if (e.target.closest("#audit-older")) return older();
    if ((b = e.target.closest("[data-expand]"))) {
      const id = Number(b.dataset.expand);
      if (audit.open.has(id)) audit.open.delete(id); else audit.open.add(id);
      patchList($("#audit-rows"), audit.entries, (x) => x.id, row);
      if (app.tick) app.tick(); // the row drawn again has its When to fill
    }
  });
  $("#audit-actor").addEventListener("change", (e) => ctx.go({ actor: e.target.value || null }));

  return {
    show() {
      visible = true;
      const q = query(ctx.params);
      if (q !== audit.query) {
        // Other filters: the log starts again from its newest change.
        audit.query = q;
        audit.data = null;
        audit.entries = [];
        audit.next = null;
        audit.open.clear();
        $("#audit-panel").hidden = $("#audit-foot").hidden = true;
        setHTML($("#audit-empty"), html`<div class="empty"><p>Loading…</p></div>`);
        load.forget();
        renderFilters();
        load(true);
      } else {
        renderFilters();
        load(false);
      }
    },
    hide() { visible = false; },
    load(force) { if (force) load.forget(); return load(force); },
    items: () => [],
  };
}
