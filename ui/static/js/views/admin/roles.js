// Admin, Roles: the built-in roles and custom ones, and the role editor. Built-in roles never change: they
// open read only, and Duplicate makes a custom role from one. Nobody gives a role, or a permission in one,
// that they don't hold themselves, so the editor offers only those.
import { app } from "../../app.js";
import * as api from "../../api.js";
import * as auth from "../../auth.js";
import { html, icon, patchList, setHTML, setText } from "../../html.js";
import { plural } from "../../format.js";
import { confirm, dismissible } from "../../ui/dialog.js";
import { toast } from "../../ui/toast.js";
import { MANAGE, NOT_GIVEN, loader, permissionGroups } from "./common.js";

const NAME_LENGTH = 64;
const BUILT_IN = "Built-in roles can't be changed. Duplicate one to make your own.";
const NOT_HELD = "You can't give a permission you don't have.";
// The catalogue's groups, as the table's Permissions column says them.
const SHORT = { Grid: "Grid", "Access tokens": "Tokens", "MFA secrets": "MFA", "Saved sign-ins": "Sign-ins", People: "People" };

const members = (r) => r.members.manual + r.members.groups;

export function createRoles(el, ctx) {
  setHTML(el, html`
    <div class="toolbar role-toolbar">
      <p class="toolbar-note role-note" id="role-note" role="status"></p>
      <span class="toolbar-spacer"></span>
      <button type="button" class="btn primary" id="new-role-btn" data-new-role hidden>${icon("i-plus")}New role</button>
    </div>
    <div class="banner" id="role-banner" role="status" hidden></div>
    <div class="panel table-panel" id="role-panel" hidden>
      <table class="table roles">
        <caption class="sr-only">Roles</caption>
        <thead><tr>
          <th scope="col">Role</th><th scope="col">Kind</th><th scope="col">Permissions</th><th scope="col">Members</th>
          <th scope="col"><span class="sr-only">Actions</span></th>
        </tr></thead>
        <tbody id="role-rows"></tbody>
      </table>
    </div>
    <div id="role-empty"><div class="empty"><p>Loading…</p></div></div>`);

  const $ = (sel) => el.querySelector(sel);
  const dlg = document.getElementById("role-dialog");
  dismissible(dlg);
  // data: GET /api/admin/roles ({ ready, permissions: the catalogue, roles }).
  const roles = { data: null };
  let visible = false;

  const load = loader(() => api.get("api/admin/roles"), (body) => { roles.data = body; render(); }, ctx, () => visible);

  const findRole = (id) => ((roles.data && roles.data.roles) || []).find((r) => String(r.id) === String(id)) || null;
  const catalogue = () => (roles.data && roles.data.permissions) || [];

  // ---- rendering ---------------------------------------------------------------
  // Why this person can't change a custom role, or "" when they can.
  const barred = (r) => (r.canEdit ? "" : !auth.can("users.manage") ? MANAGE : NOT_GIVEN);

  function roleRow(r) {
    const labels = catalogue().filter((p) => r.effective.includes(p.id)), groups = permissionGroups(labels).map((g) => SHORT[g.name] || g.name);
    const parts = [r.members.manual && r.members.manual + " by hand", r.members.groups && r.members.groups + " via groups"].filter(Boolean);
    // A list in a cell, as one element (a phone's card lays out each child of a cell) with a span for each part:
    // a narrow column wraps between parts, never inside one (at the hyphen of "Sign-ins").
    const listed = (lead, list, sep) => html`<span class="parts">${lead}${list.map((x, i) => html`${i ? sep : ""}<span>${x}</span>`)}</span>`;
    const why = barred(r), copy = auth.can("users.manage") ? "" : MANAGE;
    const action = (attr, label, verb, reason, cls = "") => html`<button type="button" class="btn ghost small ${cls}" data-${attr}="${r.id}" data-fk="${attr}-${r.id}"
      aria-label="${verb} ${r.name}"${reason ? html` disabled title="${reason}"` : ""}>${label}</button>`;
    return html`<tr data-key="${r.id}" data-role="${r.id}" data-jk tabindex="-1">
      <td class="cell-role"><span class="role-name">${r.name}</span>${r.description ? html`<small>${r.description}</small>` : ""}</td>
      <td class="cell-kind" data-label="Kind"><span class="tag">${r.builtIn ? "Built-in" : "Custom"}</span></td>
      <td class="cell-perms" data-label="Permissions" title="${labels.map((p) => p.label).join(", ")}">${listed(r.effective.length + (groups.length ? " · " : ""), groups, ", ")}</td>
      <td class="cell-members" data-label="Members">${parts.length ? listed("", parts, " · ") : html`<span class="dim">Nobody</span>`}</td>
      <td class="cell-actions">${r.builtIn ? html`${action("view-role", "View", "View", "")}${action("copy-role", "Duplicate", "Duplicate", copy)}`
        : html`${action("edit-role", "Edit", "Edit", why)}${action("copy-role", "Duplicate", "Duplicate", copy)}${action("delete-role", "Delete", "Delete", why, "destructive")}`}</td></tr>`;
  }

  function render() {
    const d = roles.data;
    if (!d) return;
    const custom = d.roles.filter((r) => !r.builtIn).length;
    setText($("#role-note"), plural(d.roles.length - custom, "built-in role") + " · " + plural(custom, "custom role"));
    $("#new-role-btn").hidden = !(d.ready && auth.can("users.manage"));
    const banner = $("#role-banner");
    banner.hidden = d.ready;
    setText(banner, d.ready ? "" : "Custom roles aren't set up in this database yet: it has no roles table. The Grid UI creates it on MySQL when its database user may create tables.");
    $("#role-panel").hidden = !d.roles.length;
    setHTML($("#role-empty"), html``);
    patchList($("#role-rows"), d.roles, (r) => r.id, roleRow);
    if (app.tick) app.tick();
  }

  // ---- the role editor ------------------------------------------------------------
  // mode: "new", "edit", "copy" (a new role from another) or "view" (a built-in role, read only).
  function openEditor(mode, role) {
    if (dlg.open || !roles.data) return;
    const view = mode === "view", edit = mode === "edit";
    // A copy keeps only what this person may give.
    const selected = new Set(role ? role.permissions.filter((p) => mode !== "copy" || auth.can(p)) : []);
    const name = mode === "copy" ? ("Copy of " + role.name).slice(0, NAME_LENGTH) : role ? role.name : "";
    const title = view ? "The role “" + role.name + "”" : edit ? "Edit the role “" + role.name + "”" : "New role";
    const people = edit ? members(role) : 0;
    // Said once, above them all, and read out with each one held back: a disabled box can't show its title.
    const holding = !view && catalogue().some((p) => !auth.can(p.id));
    dlg.__html = null; // always fresh elements: the listeners below belong to this one editor
    setHTML(dlg, html`<form class="role-form" id="role-form" novalidate>
      <h2 id="role-title">${title}</h2>
      <p class="sheet-sub">${view ? BUILT_IN : "What people with this role may do. Each permission brings those it includes."}</p>
      <div class="field">
        <label for="role-name">Name</label>
        <input type="text" id="role-name" maxlength="${NAME_LENGTH}" required autocomplete="off" value="${name}" ${view ? "disabled" : ""}>
      </div>
      <div class="field">
        <label for="role-description">Description <span class="optional">optional</span></label>
        <input type="text" id="role-description" maxlength="255" autocomplete="off" value="${role && role.description ? role.description : ""}" ${view ? "disabled" : ""}>
      </div>
      <p class="form-error" id="role-field-error" role="alert" hidden></p>
      ${holding ? html`<p class="hint" id="role-perms-note">Permissions you don't have are greyed out: you can't give them.</p>` : ""}
      <div class="role-perms" id="role-perms"></div>
      ${edit ? html`<p class="hint">${people
        ? "Changes apply at once to the " + plural(people, "person", "people") + " with this role, including anyone signed in now."
        : "Nobody has this role yet."}</p>` : ""}
      <p class="form-error" id="role-error" role="alert" hidden></p>
      <div class="dialog-actions">
        <button type="button" class="btn ghost" data-dismiss>${view ? "Close" : "Cancel"}</button>
        ${view ? (auth.can("users.manage") ? html`<button type="button" class="btn" data-copy-role="${role.id}">Duplicate</button>` : "")
          : html`<button type="submit" class="btn primary" id="role-submit">${edit ? "Save role" : "Make role"}</button>`}
      </div>
    </form>`);
    const form = dlg.querySelector("form"), field = (id) => dlg.querySelector("#" + id);

    // What the ticked permissions bring with them, each with the first ticked one that brings it.
    function implied() {
      const by = {};
      catalogue().forEach((p) => {
        if (selected.has(p.id)) p.implies.forEach((q) => { if (!(q in by)) by[q] = p.label; });
      });
      return by;
    }

    function permItem(p, by) {
      const included = p.id in by, held = auth.can(p.id);
      const off = view || included || !held, back = !view && !held && !included;
      return html`<label class="perm-item"${back ? html` title="${NOT_HELD}"` : ""}>
        <input type="checkbox" data-perm="${p.id}" data-fk="perm-${p.id}" ${selected.has(p.id) || included ? "checked" : ""} ${off ? "disabled" : ""}${back
          ? html` aria-describedby="role-perms-note"` : ""}>
        <span><strong>${p.label}</strong><small>${p.description}</small>${included ? html`<small class="perm-by">Included by “${by[p.id]}”</small>` : ""}</span></label>`;
    }

    function renderPerms() {
      const by = implied();
      patchList(field("role-perms"), permissionGroups(catalogue()), (g) => g.name, (g) => html`<fieldset class="perm-list" data-key="${g.name}">
        <legend>${g.name}</legend>${g.perms.map((p) => permItem(p, by))}</fieldset>`);
    }

    // An error about the name or the description shows under them, where focus takes the eye; any other
    // above the buttons, where the eye already is. The form is long.
    function error(text, id) {
      const at = id ? "role-field-error" : "role-error";
      ["role-field-error", "role-error"].forEach((x) => {
        setText(field(x), x === at ? text || "" : "");
        field(x).hidden = !(x === at && text);
      });
      ["role-name", "role-description"].forEach((x) => {
        if (x === id) {
          field(x).setAttribute("aria-invalid", "true");
          field(x).setAttribute("aria-describedby", "role-field-error");
        } else {
          field(x).removeAttribute("aria-invalid");
          field(x).removeAttribute("aria-describedby");
        }
      });
      if (id) field(id).focus();
    }

    form.addEventListener("change", (e) => {
      const box = e.target.closest("[data-perm]");
      if (!box) return;
      if (box.checked) selected.add(box.dataset.perm); else selected.delete(box.dataset.perm);
      renderPerms();
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (view) return;
      const btn = field("role-submit"), text = field("role-name").value.trim();
      if (!text) return error("Give the role a name.", "role-name");
      // Only what's ticked by hand: what it includes comes with it.
      const by = implied(), perms = catalogue().map((p) => p.id).filter((p) => selected.has(p) && !(p in by));
      const body = { name: text, description: field("role-description").value.trim() || null, permissions: perms };
      btn.disabled = true;
      error("");
      try {
        const res = await api.post(edit ? "api/admin/roles/" + encodeURIComponent(role.id) : "api/admin/roles", body);
        if (res === null) return; // sign-in dismissed: the editor stays as it was
        dlg.close();
        toast((edit ? "Saved the role “" : "Made the role “") + res.role.name + "”.");
        await load(true);
        const row = $("#role-rows [data-role='" + res.role.id + "']");
        if (row) (row.querySelector("[data-edit-role]:not(:disabled)") || row).focus();
      } catch (err) {
        // By how accounts.py words them: a refusal about permissions names the role, which may say anything.
        error(err.message, /^A role's description/.test(err.message) ? "role-description"
          : /^(A role's name|There is already a role called)|is reserved: choose another name\.$/.test(err.message) ? "role-name" : null);
      } finally {
        btn.disabled = false;
      }
    });
    // Duplicate, from a built-in role on show: the editor for the copy takes its place.
    form.addEventListener("click", (e) => {
      const b = e.target.closest("[data-copy-role]");
      if (!b) return;
      dlg.close();
      openEditor("copy", role);
    });

    renderPerms();
    dlg.showModal();
    // Read only, focus goes to Close at the foot of a long form: the form still opens at its top, title first.
    if (view) {
      dlg.querySelector("[data-dismiss]").focus({ preventScroll: true });
      dlg.scrollTop = 0;
    } else field("role-name").focus();
    if (mode === "copy") field("role-name").select();
  }

  async function deleteRole(id, origin) {
    const r = findRole(id);
    if (!r) return;
    const ok = await confirm({
      title: "Delete the role “" + r.name + "”?",
      facts: [["People with it", String(members(r))], ["Group mappings", String(r.mappings.length)], ["Shared with it", plural(r.shares, "item")]],
      note: "They lose what this role gave them. Its mappings and shares are removed too. This can't be undone.", ok: "Delete role",
    });
    if (!ok) return;
    const rows = Array.from(el.querySelectorAll("#role-rows tr")), at = rows.indexOf(origin && origin.closest("tr"));
    try {
      const res = await api.post("api/admin/roles/" + encodeURIComponent(r.id) + "/delete", {});
      if (res === null) return;
      toast("Deleted the role “" + r.name + "”.");
    } catch (err) {
      toast(err.message, true);
    }
    await load(true);
    const after = Array.from(el.querySelectorAll("#role-rows tr")), next = after[Math.min(Math.max(at, 0), after.length - 1)];
    (next && next.querySelector("button:not(:disabled)") || $("#new-role-btn")).focus();
  }

  // ---- events -----------------------------------------------------------------------
  el.addEventListener("click", (e) => {
    let b;
    if (e.target.closest("[data-new-role]")) return openEditor("new", null);
    if ((b = e.target.closest("[data-view-role]"))) return openEditor("view", findRole(b.dataset.viewRole));
    if ((b = e.target.closest("[data-edit-role]"))) return openEditor("edit", findRole(b.dataset.editRole));
    if ((b = e.target.closest("[data-copy-role]"))) return openEditor("copy", findRole(b.dataset.copyRole));
    if ((b = e.target.closest("[data-delete-role]"))) return deleteRole(b.dataset.deleteRole, b);
  });

  return {
    show() { visible = true; load(false); },
    hide() { visible = false; if (dlg.open) dlg.close(); },
    load(force) { if (force) load.forget(); return load(force); },
    items: () => (roles.data && roles.data.roles) || [],
  };
}
