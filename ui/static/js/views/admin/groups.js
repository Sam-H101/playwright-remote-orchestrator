// Admin, Group mappings: which identity provider groups give which roles. A mapping gives its role to everyone
// whose latest sign-in carried the group, and the one for everyone ('*') to everyone who signs in.
import { app } from "../../app.js";
import * as api from "../../api.js";
import * as auth from "../../auth.js";
import { html, icon, patchList, setHTML, setText } from "../../html.js";
import { fullDate, parseTime, plural } from "../../format.js";
import { confirm, dismissible } from "../../ui/dialog.js";
import { toast } from "../../ui/toast.js";
import { NOT_GIVEN, loader, mayGive } from "./common.js";

const EVERYONE = "*";
// The mapping for everyone may not give a role that manages people (accounts.EVERYONE).
const MANAGES = "users.manage";

const groupLabel = (m) => (m.group === EVERYONE ? "Everyone who signs in" : "‘" + m.group + "’");

export function createGroups(el, ctx) {
  setHTML(el, html`
    <p class="view-sub group-intro" id="group-intro"></p>
    <div class="toolbar group-toolbar">
      <p class="toolbar-note" id="group-count" role="status"></p>
      <span class="toolbar-spacer"></span>
      <button type="button" class="btn primary" id="map-group-btn" data-map-group hidden>${icon("i-plus")}Map a group</button>
    </div>
    <div class="panel table-panel" id="group-panel" hidden>
      <table class="table groups">
        <caption class="sr-only">Group mappings</caption>
        <thead><tr>
          <th scope="col">Group</th><th scope="col">Note</th><th scope="col">Role</th><th scope="col" class="num-col">People matched</th>
          <th scope="col">Added</th><th scope="col"><span class="sr-only">Actions</span></th>
        </tr></thead>
        <tbody id="group-rows"></tbody>
      </table>
    </div>
    <div id="group-empty"><div class="empty"><p>Loading…</p></div></div>`);

  const $ = (sel) => el.querySelector(sel);
  const dlg = document.getElementById("group-dialog");
  dismissible(dlg);
  // data: GET /api/admin/groups; roles: GET /api/admin/roles, the roles a group can give.
  const groups = { data: null, roles: null };
  let visible = false;

  const load = loader(async () => {
    const [mappings, roles] = await Promise.all([api.get("api/admin/groups"), api.get("api/admin/roles")]);
    return { mappings, roles };
  }, (answer) => { groups.data = answer.mappings; groups.roles = answer.roles.roles; render(); }, ctx, () => visible);

  const roleOf = (id) => (groups.roles || []).find((r) => r.id === id) || null;

  // ---- rendering ---------------------------------------------------------------
  function mappingRow(m) {
    const role = roleOf(m.roleId), added = parseTime(m.createdAt), barred = role && !mayGive(role) ? NOT_GIVEN : "";
    return html`<tr data-key="${m.id}" data-mapping="${m.id}" data-jk tabindex="-1">
      <td class="cell-group">${m.group === EVERYONE ? html`<strong>Everyone who signs in</strong>` : html`<code class="mono group-name">${m.group}</code>`}</td>
      <td class="cell-note" data-label="Note">${m.note || html`<span class="dim">–</span>`}</td>
      <td class="cell-role" data-label="Role">${m.role}</td>
      <td class="cell-people num-col" data-label="People matched">${m.people}</td>
      <td class="cell-added" data-label="Added" title="${fullDate(added)}${m.createdBy ? " by " + m.createdBy : ""}">${added != null
        ? html`<span data-ago="${m.createdAt}"></span>` : html`<span class="dim">–</span>`}</td>
      <td class="cell-actions">${auth.can("users.manage") ? html`<button type="button" class="btn ghost small destructive" data-unmap="${m.id}" data-fk="unmap-${m.id}"
        aria-label="Remove the mapping of ${groupLabel(m)} to ${m.role}"${barred ? html` disabled title="${barred}"` : ""}>Remove</button>` : ""}</td></tr>`;
  }

  function render() {
    const d = groups.data;
    if (!d) return;
    const claims = d.claims.map((c) => "‘" + c + "’");
    setText($("#group-intro"), "When someone signs in, the grid reads the " + claims.join(" and ") + (claims.length > 1 ? " claims" : " claim") +
      " of their ID token. Each value below gives the role next to it, for as long as the person's latest sign-in carried it. Map group ids " +
      "(an Entra group's object id), not names anyone could create.");
    setText($("#group-count"), d.mappings.length ? plural(d.mappings.length, "mapping") : "");
    $("#map-group-btn").hidden = !(d.ready && d.mappings.length && auth.can("users.manage"));
    $("#group-panel").hidden = !d.mappings.length;
    let empty = html``;
    if (!d.ready) {
      empty = html`<div class="empty">${icon("i-users", "empty-mark")}<h2>Group mappings aren't set up in this database yet</h2>
        <p>It has no groupRoles table. The Grid UI creates it on MySQL when its database user may create tables.</p></div>`;
    } else if (!d.mappings.length) {
      empty = html`<div class="empty">${icon("i-users", "empty-mark")}<h2>No group mappings yet</h2>
        <p>Map a group to give its members a role when they sign in, or give everyone who signs in a role to start with.</p>
        ${auth.can("users.manage") ? html`<button type="button" class="btn primary" data-map-group>${icon("i-plus")}Map a group</button>` : ""}</div>`;
    }
    setHTML($("#group-empty"), empty);
    patchList($("#group-rows"), d.mappings, (m) => m.id, mappingRow);
    if (app.tick) app.tick();
  }

  // ---- mapping a group ----------------------------------------------------------------
  function openMap() {
    const d = groups.data;
    if (dlg.open || !d) return;
    dlg.__html = null; // always fresh elements: the listeners below belong to this one dialog
    setHTML(dlg, html`<form class="group-form" id="group-form" novalidate>
      <h2 id="group-title">Map a group</h2>
      <p class="sheet-sub">Everyone whose ID token carries the group gets the role.</p>
      <div class="field">
        <label for="group-name">Group</label>
        <input type="text" id="group-name" class="mono" list="group-seen" maxlength="255" autocomplete="off" autocapitalize="off" spellcheck="false"
          placeholder="e.g. an Entra group's object id" aria-describedby="group-name-hint">
        <datalist id="group-seen">${d.seen.map((s) => html`<option value="${s.group}">seen for ${plural(s.people, "person", "people")}</option>`)}</datalist>
        <p class="hint" id="group-name-hint">As the ${d.claims.join(" or ")} claim says it, exactly: capitals count.</p>
        <label class="check-line"><input type="checkbox" id="group-everyone">Everyone who signs in</label>
      </div>
      <div class="field">
        <label for="group-role">Role</label>
        <select id="group-role"></select>
      </div>
      <div class="field">
        <label for="group-note">Note <span class="optional">optional</span></label>
        <input type="text" id="group-note" maxlength="128" autocomplete="off" placeholder="e.g. the QA team">
      </div>
      <p class="hint">Takes effect at once for people whose last sign-in carried the group, and for others when they next sign in.</p>
      <p class="form-error" id="group-error" role="alert" hidden></p>
      <div class="dialog-actions">
        <button type="button" class="btn ghost" data-dismiss>Cancel</button>
        <button type="submit" class="btn primary" id="group-submit">Map group</button>
      </div>
    </form>`);
    const field = (id) => dlg.querySelector("#" + id), everyone = field("group-everyone");

    // Roles this person may give; with everyone ticked, none that manages people.
    function syncRoles() {
      const select = field("group-role"), was = select.value;
      setHTML(select, html`${(groups.roles || []).map((r) => {
        const why = !mayGive(r) ? "has permissions you don't have" : everyone.checked && r.effective.includes(MANAGES) ? "manages people" : "";
        return html`<option value="${r.id}" ${why ? "disabled" : ""}>${r.name}${why ? " (" + why + ")" : ""}</option>`;
      })}`);
      select.value = was;
      if (select.selectedIndex === -1 || select.selectedOptions[0].disabled) {
        const first = select.querySelector("option:not(:disabled)");
        select.value = first ? first.value : "";
      }
      field("group-name").disabled = everyone.checked;
      field("group-submit").disabled = !select.querySelector("option:not(:disabled)");
    }

    function error(text, id) {
      setText(field("group-error"), text || "");
      field("group-error").hidden = !text;
      ["group-name", "group-note"].forEach((x) => { if (x === id) field(x).setAttribute("aria-invalid", "true"); else field(x).removeAttribute("aria-invalid"); });
      if (id) field(id).focus();
    }

    everyone.addEventListener("change", syncRoles);
    field("group-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = field("group-submit"), group = everyone.checked ? EVERYONE : field("group-name").value.trim();
      if (!group) return error("Type a group, or choose Everyone who signs in.", "group-name");
      const role = roleOf(Number(field("group-role").value));
      if (!role) return;
      btn.disabled = true;
      error("");
      try {
        const res = await api.post("api/admin/groups", { group, roleId: role.id, note: field("group-note").value.trim() || null });
        if (res === null) return; // sign-in dismissed: the dialog stays as it was
        dlg.close();
        toast(group === EVERYONE ? "Everyone who signs in gets the role " + res.mapping.role + "." : "Mapped ‘" + group + "’ to the role " + res.mapping.role + ".");
        await load(true);
        const row = $("#group-rows [data-mapping='" + res.mapping.id + "']");
        if (row) row.focus();
      } catch (err) {
        error(err.message, /^A group/.test(err.message) ? "group-name" : /^A note/.test(err.message) ? "group-note" : null);
      } finally {
        btn.disabled = false;
        if (dlg.open) syncRoles();
      }
    });

    syncRoles();
    dlg.showModal();
    field("group-name").focus();
  }

  async function unmap(id, origin) {
    const m = ((groups.data && groups.data.mappings) || []).find((x) => String(x.id) === String(id));
    if (!m) return;
    const ok = await confirm({
      title: m.group === EVERYONE ? "Stop giving everyone who signs in the role " + m.role + "?" : "Stop mapping ‘" + m.group + "’ to the role " + m.role + "?",
      text: "The " + plural(m.people, "person", "people") + " it matches lose the role at their next request, unless they have it another way.",
      ok: "Remove mapping",
    });
    if (!ok) return;
    const rows = Array.from(el.querySelectorAll("#group-rows tr")), at = rows.indexOf(origin && origin.closest("tr"));
    try {
      const res = await api.post("api/admin/groups/" + encodeURIComponent(m.id) + "/delete", {});
      if (res === null) return;
      toast("Removed the mapping of " + groupLabel(m) + " to the role " + m.role + ".");
    } catch (err) {
      toast(err.message, true);
    }
    await load(true);
    const after = Array.from(el.querySelectorAll("#group-rows tr")), next = after[Math.min(Math.max(at, 0), after.length - 1)];
    const target = (next && next.querySelector("button:not(:disabled)")) || el.querySelector("[data-map-group]:not([hidden])");
    if (target) target.focus();
  }

  // ---- events -----------------------------------------------------------------------
  el.addEventListener("click", (e) => {
    let b;
    if (e.target.closest("[data-map-group]")) return openMap();
    if ((b = e.target.closest("[data-unmap]"))) return unmap(b.dataset.unmap, b);
  });

  return {
    show() { visible = true; load(false); },
    hide() { visible = false; if (dlg.open) dlg.close(); },
    load(force) { if (force) load.forget(); return load(force); },
    items: () => [],
  };
}
