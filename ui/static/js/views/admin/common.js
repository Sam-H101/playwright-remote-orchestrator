// What the Admin page's tabs share: how each loads, the words for where a role came from and for
// permissions grouped as the role editor groups them, and why a change isn't this person's to make.
import * as auth from "../../auth.js";

export const ADMIN_ROLE = -1;
export const NOT_GIVEN = "It has permissions you don't have.";
export const MANAGE = "Your roles don't include “Manage people and roles”.";

export const possessive = (name) => name + "'s";
// "2 client tokens and 1 agent token": the parts there are, joined.
export const both = (parts) => parts.filter(Boolean).join(" and ");

// Each tab's load(force). ask() fetches what the tab shows; took(answer) keeps and renders it, and is called
// only when the answer changed (its signature leaves out "generated"). At most one request at a time: a forced
// load asked for meanwhile runs once more when it's in, as its answer may predate what the caller changed.
// Unforced, at most every 4 s. A failure goes to ctx.failed, which shows the matching panel instead, and the
// next answer that comes (a tab that polls) to ctx.recovered, which shows the tab again.
export function loader(ask, took, ctx, shown) {
  let busy = null, again = false, at = 0, sig = null, failed = false;
  const load = (force) => {
    if (!shown()) return Promise.resolve();
    if (busy) {
      if (force) again = true;
      return busy;
    }
    if (!force && Date.now() - at < 4000) return Promise.resolve();
    busy = (async () => {
      do {
        again = false;
        try {
          const answer = await ask();
          const next = JSON.stringify(answer, (k, v) => (k === "generated" ? undefined : v));
          if (next !== sig && shown()) { sig = next; took(answer); }
          if (failed && shown()) ctx.recovered();
          failed = false;
        } catch (err) {
          sig = null;
          failed = true;
          if (shown()) ctx.failed(err);
        }
        at = Date.now();
      } while (again && shown());
    })().finally(() => { busy = null; });
    return busy;
  };
  // The next answer renders even if it's the same: what the tab shows depends on more than the answer.
  load.forget = () => { sig = null; };
  return load;
}

// Where someone's role comes from, in a sentence and in the few words of a pill.
const SOURCE = { manual: "Given by an admin", everyone: "Everyone who signs in", env: "From GRID_OIDC_ADMINS" };
const SOURCE_SHORT = { group: "group", everyone: "everyone", env: "GRID_OIDC_ADMINS" };
export const roleSource = (r) => (r.source === "group" ? "From the group ‘" + r.group + "’" : SOURCE[r.source] || r.source);
export const roleSourceShort = (r) => SOURCE_SHORT[r.source] || "";

// A person's roles, one entry per role however many ways they have it: { id, name, sources: [their roles
// entries], manual: given by hand }.
export function heldRoles(user) {
  const out = [];
  (user.roles || []).forEach((r) => {
    let held = out.find((x) => x.id === r.id);
    if (!held) out.push((held = { id: r.id, name: r.name, sources: [], manual: false }));
    held.sources.push(r);
    if (r.source === "manual") held.manual = true;
  });
  return out;
}

// The permission catalogue (GET /api/permissions, or the roles' answer) in its groups, in order: [{ name, perms }].
export function permissionGroups(catalogue) {
  const groups = [];
  (catalogue || []).forEach((p) => {
    let g = groups.find((x) => x.name === p.group);
    if (!g) groups.push((g = { name: p.group, perms: [] }));
    g.perms.push(p);
  });
  return groups;
}

// Whether this person may hand out a role (a role of GET /api/admin/roles), map a group to it, or change it:
// only with everything it brings.
export const mayGive = (role) => role.effective.every((p) => auth.can(p));
