// Admin: who can sign in to this grid, what each person may do, and every change to it. This file is
// the shell: the tabs (one module each, in views/admin/), their place in the address (#/admin?tab=),
// and the panels shown instead of them. What the session lets the page show is decided before
// anything is fetched, so a page in a normal state is never refused.
//
// Each tab module exports create<Tab>(el, ctx) and returns { show, hide, load(force), items }:
//   show()       the tab is on screen, or the hash's params changed while it is: read ctx.params,
//                render, and load what it needs (load throttles itself)
//   hide()       it left the screen: stop polling
//   load(force)  fetch (at most every 4 s unless force) and render when the answer changed
//   items()      what it loaded, for the palette (people, roles)
// ctx.params is the hash's params; ctx.go(patch, { push }) changes them (replacing the history entry
// unless push); ctx.failed(err) hands over a failure the tab can't show in its own place: a refusal
// or an unset sign-in becomes the matching panel, anything else the error panel, which
// ctx.recovered() takes away again once a load works.
import { prefs } from "../app.js";
import * as auth from "../auth.js";
import * as router from "../router.js";
import { html, icon, setHTML, setText } from "../html.js";
import { createUsers } from "./admin/users.js";
import { createRoles } from "./admin/roles.js";
import { createGroups } from "./admin/groups.js";
import { createAudit } from "./admin/audit.js";
import { createSignin } from "./admin/signin.js";

// id (the hash's ?tab=), label, and the permission that shows it.
export const TABS = [
  ["users", "Users", "users.view"],
  ["roles", "Roles", "users.view"],
  ["groups", "Group mappings", "users.view"],
  ["audit", "Audit log", "audit.view"],
  ["sso", "Single sign-on", "users.view"],
];
const MODULES = { users: createUsers, roles: createRoles, groups: createGroups, audit: createAudit, sso: createSignin };
const TAB_KEY = "gridAdminTab";
const NOT_SET_UP = "Single sign-on isn't set up: people appear here once GRID_OIDC_ISSUER is set and they sign in.";

export function createAdmin(section) {
  setHTML(section, html`
    <header class="view-head access-head">
      <div>
        <h1 id="admin-h1" tabindex="-1">Admin</h1>
        <p class="view-sub">Who can sign in to this grid, what each person may do, and every change to it.</p>
      </div>
    </header>
    <div class="banner" id="admin-banner" role="status" hidden></div>
    <div class="tabs-row admin-tabs" id="admin-tabs" hidden>
      <div class="tabs" role="tablist" aria-label="Admin">
        ${TABS.map(([id, label]) => html`<button type="button" role="tab" id="admin-tab-${id}" data-admin-tab="${id}" aria-controls="admin-panel-${id}"
          aria-selected="false" tabindex="-1">${label}</button>`)}
      </div>
    </div>
    ${TABS.map(([id]) => html`<div class="admin-panel" role="tabpanel" id="admin-panel-${id}" aria-labelledby="admin-tab-${id}" hidden></div>`)}
    <div id="admin-state"></div>`);

  const $ = (sel) => section.querySelector(sel);
  // loading, unconfigured, signedout, forbidden, error, ok
  const admin = { state: "loading", tab: null, params: {}, error: null };
  let visible = false;

  const ctx = {
    get params() { return admin.params; },
    go(patch, { push = false } = {}) {
      router.go(router.build("admin", Object.assign({}, admin.params, patch)), { replace: !push });
    },
    failed(err) {
      const body = (err && err.body) || {};
      if (body.admin_token_unset) admin.state = "unconfigured";
      else if (body.token_required) admin.state = "signedout";
      else { admin.state = "error"; admin.error = (err && err.message) || String(err); }
      render();
    },
    // A tab that failed loaded again (it polls): it comes back in place of the error.
    recovered() {
      if (admin.state !== "error") return;
      admin.state = decide();
      render();
    },
  };
  const tabs = {};
  TABS.forEach(([id]) => { tabs[id] = MODULES[id]($("#admin-panel-" + id), ctx); });

  // The tabs this session may see.
  const allowedTabs = () => TABS.filter(([, , perm]) => auth.can(perm)).map(([id]) => id);

  // What the session lets the page show, before anything is fetched.
  function decide() {
    const s = auth.state();
    if (!s.known) return "loading";
    if (!s.adminConfigured) return "unconfigured";
    if (!s.signedIn) return "signedout";
    return allowedTabs().length ? "ok" : "forbidden";
  }

  // The tab the hash asks for, else the one open last time, else the first this session may see.
  function pick(params) {
    const ok = allowedTabs();
    if (ok.includes(params.tab)) return params.tab;
    const kept = prefs.get(TAB_KEY, "");
    return ok.includes(kept) ? kept : ok[0] || null;
  }

  function stateMarkup() {
    switch (admin.state) {
      case "loading": return html`<div class="empty"><p>Loading…</p></div>`;
      case "unconfigured": return html`<div class="empty">${icon("i-users", "empty-mark")}
        <h2>Set up sign-in to manage people</h2>
        <p>Anyone who can open this page can manage the grid, so there's nobody to manage. Set GRID_OIDC_ISSUER to have people sign in
          with your identity provider and give them roles, or GRID_ADMIN_TOKEN for one shared admin sign-in, then restart the Grid UI.</p></div>`;
      case "signedout": return html`<div class="empty calm">${icon("i-lock", "empty-mark")}
        <h2>Sign in to see people and roles</h2>
        <p>Only people whose roles include “See people and roles” or “Read the audit log” can see this page.</p>
        <button type="button" class="btn primary" data-signin>Sign in</button></div>`;
      case "forbidden": return html`<div class="empty calm">${icon("i-lock", "empty-mark")}
        <h2>Your roles don't include people or the audit log</h2>
        <p>Ask an admin for a role with “See people and roles” or “Read the audit log”.</p>
        <button type="button" class="btn" data-your-access>Your access</button></div>`;
      default: return html`<div class="empty">${icon("i-alert", "empty-mark")}<h2>Can't load this part of the Admin page</h2><p>${admin.error}</p>
        <button type="button" class="btn" data-reload-admin>Try again</button></div>`;
    }
  }

  function render() {
    const s = auth.state(), ok = admin.state === "ok", shown = allowedTabs();
    const open = ok || admin.state === "error";
    section.querySelectorAll("[data-admin-tab]").forEach((b) => {
      const on = b.dataset.adminTab === admin.tab;
      b.hidden = !shown.includes(b.dataset.adminTab);
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
    });
    $("#admin-tabs").hidden = !open;
    TABS.forEach(([id]) => { $("#admin-panel-" + id).hidden = !(ok && id === admin.tab); });
    setHTML($("#admin-state"), ok ? html`` : stateMarkup());
    // Single sign-on that isn't set up, or can't work as set up: people can't sign in to be managed.
    const note = !open ? "" : !s.sso ? NOT_SET_UP : s.ssoProblem || "";
    $("#admin-banner").hidden = !note;
    setText($("#admin-banner"), note);
  }

  // The hash or the session changed: decide again, and show the tab that's open now.
  function apply(params) {
    admin.params = params || {};
    admin.state = decide();
    const tab = admin.state === "ok" ? pick(admin.params) : null;
    if (tab !== admin.tab && admin.tab) tabs[admin.tab].hide();
    admin.tab = tab;
    if (tab) prefs.set(TAB_KEY, tab);
    render();
    if (visible && tab) tabs[tab].show();
  }

  // Another tab's filters, and the person it had open, don't carry over.
  function select(id, focus) {
    if (id !== admin.tab) router.go(router.build("admin", { tab: id }), { replace: true });
    if (focus) $("#admin-tab-" + id).focus();
  }

  section.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-admin-tab]");
    if (tab) return select(tab.dataset.adminTab, false);
    if (e.target.closest("[data-signin]")) {
      auth.signIn("Sign in to see people and roles");
      return;
    }
    if (e.target.closest("[data-reload-admin]") && admin.tab) {
      admin.state = decide();
      render();
      tabs[admin.tab].load(true);
    }
  });
  // The tablist pattern: arrows move along the tabs this session may see, Home and End go to the ends.
  $("[role=tablist]").addEventListener("keydown", (e) => {
    const tab = e.target.closest("[role=tab]");
    if (!tab || !["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const ids = allowedTabs(), i = ids.indexOf(tab.dataset.adminTab);
    const j = e.key === "Home" ? 0 : e.key === "End" ? ids.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + ids.length) % ids.length;
    select(ids[j], true);
  });

  auth.onChange(() => {
    if (!visible) return;
    apply(admin.params);
    if (admin.state === "ok") tabs[admin.tab].load(true);
  });

  // What the tabs loaded stays with them, but the palette offers it only while the session may see it: not
  // after signing out, nor to whoever signs in next without the role.
  const seen = () => auth.state().signedIn && auth.can("users.view");

  return {
    show(params) { visible = true; apply(params); },
    hide() { visible = false; if (admin.tab) tabs[admin.tab].hide(); },
    users: () => (seen() ? tabs.users.items() : []),
    roles: () => (seen() ? tabs.roles.items() : []),
  };
}
