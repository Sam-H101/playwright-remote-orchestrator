// Sharing an MFA secret or a saved sign-in: who has it, adding people and roles, stopping, and for whoever
// may see every one, giving it to someone else. Built afresh each time it opens. Every change is made at
// once, and answered with the item as its listing shows it, which the dialog then shows.
import * as api from "../api.js";
import * as auth from "../auth.js";
import { html, icon, patchList, setHTML, setText } from "../html.js";
import { createPicker, directory } from "./picker.js";

const ADMIN_ROLE = -1;
const KINDS = {
  mfa: {
    path: "api/mfa/", all: "mfa.all", use: "Use MFA secrets", admins: "see everything",
    sub: "People and roles you add get its codes here and through their checks' client tokens. Nobody ever sees the seed.",
  },
  signin: {
    path: "api/signins/", all: "signins.all", use: "Use saved sign-ins", admins: "see it here",
    sub: "People and roles you add have their checks reuse this sign-in, but it reaches only those who may do nothing its owner may not, " +
      "unless someone with “Every saved sign-in” shares it. Only its owner's checks sign in fresh for it or save it again, and nobody " +
      "ever sees its cookies.",
    // What it loses with its owner (accounts.set_item_owner): what was saved is an account the old owner's checks signed in as.
    moving: "Its shares are removed, what's saved for it is dropped and any sign-in under way is cancelled: the new owner decides who " +
      "it's shared with, and their checks sign in again.",
  },
};

// kind: "mfa" or "signin"; item: its row as the listing has it; onDone(): the dialog closed, changed or not.
export function openShare({ kind, item, onDone }) {
  const dlg = document.getElementById("share-dialog"), k = KINDS[kind];
  if (dlg.open) return;
  // current: the item as the last answer had it; moving: the owner line is a picker; warning: the last
  // owner change's (their roles don't let them use it).
  let current = item, moving = false, warning = "";
  dlg.__html = null; // always fresh elements: the listeners below belong to this one dialog
  setHTML(dlg, html`<div class="share">
    <h2 id="share-title">Share “${item.name}”</h2>
    <p class="sheet-sub">${k.sub}</p>
    <div id="share-owner"></div>
    <div class="field">
      <label for="share-add-input">Add people or roles</label>
      <div id="share-add"></div>
      <p class="hint" id="share-add-hint">Type 2 letters or more to find people by name or email.</p>
    </div>
    <p class="form-error" id="share-error" role="alert" hidden></p>
    <p class="sr-only" id="share-status" role="status"></p>
    <h3 class="section-label" id="share-list-title">Who has access</h3>
    <ul class="share-list" id="share-list" aria-labelledby="share-list-title"></ul>
    <div class="dialog-actions"><button type="button" class="btn primary" data-dismiss>Done</button></div>
  </div>`);
  const $ = (sel) => dlg.querySelector(sel);
  const me = () => auth.state().user;

  function error(text) {
    setText($("#share-error"), text || "");
    $("#share-error").hidden = !text;
  }

  // ---- whose it is ----------------------------------------------------------------
  function renderOwner() {
    const o = current.owner, mine = o && me() && me().id === o.id;
    // What a sign-in loses goes above the field, where the list of people opening under it can't cover it.
    setHTML($("#share-owner"), moving ? html`<div class="field share-move">
        <label for="share-owner-input">Give it to</label>
        ${k.moving ? html`<p class="hint notice" id="share-move-note">${k.moving}</p>` : ""}
        <div id="share-owner-pick"></div>
        <button type="button" class="btn ghost small" data-keep-owner>Keep the owner</button>
      </div>`
      : html`<dl class="confirm-facts share-owner"><dt>Owner</dt>
        <dd><span>${o ? o.name + (mine ? " (you)" : "") : "Admins"}</span>${o && o.disabled ? html` <span class="tag">disabled</span>` : ""}
          ${auth.can(k.all) ? html`<button type="button" class="btn ghost small" data-change-owner>Change owner…</button>` : ""}</dd></dl>
        ${warning ? html`<p class="hint notice share-warning">${warning}</p>` : ""}`);
    if (!moving) return;
    createPicker($("#share-owner-pick"), {
      id: "share-owner", placeholder: "Find a person, or Admins", describedBy: k.moving ? "share-move-note" : "",
      async options(q) {
        const found = await directory(q), text = q.toLowerCase();
        const admins = current.owner && "admins".indexOf(text) !== -1 ? [{ key: "admins", label: "Admins", userId: null }] : [];
        const people = found.users.filter((u) => !current.owner || u.id !== current.owner.id)
          .map((u) => ({ key: "user:" + u.id, label: u.name, meta: u.email || "", userId: u.id }));
        return [{ label: "", options: admins }, { label: "People", options: people }];
      },
      empty: (q) => (q.length < 2 ? "Type 2 letters or more to find people." : "No one matches “" + q + "”."),
      async choose(o) {
        const res = await change("/owner", { userId: o.userId }, (o.userId == null ? "Gave it to the admins." : "Gave it to " + o.label + "."));
        if (!res) return false;
        moving = false;
        warning = res.warning || "";
        renderOwner();
        const btn = $("[data-change-owner]");
        if (btn) btn.focus();
        return true;
      },
    }).focus();
  }

  // ---- who has it -------------------------------------------------------------------
  function entries() {
    const o = current.owner, out = [{ key: "owner", icon: o ? "i-user" : "i-users", name: o ? o.name : "Admins", owner: true }];
    // Everyone whose roles include every one of them sees it anyway: there's nobody to take it from.
    if (o) out.push({ key: "admins", icon: "i-lock", name: "Admins", sub: k.admins });
    (current.sharedWith || []).forEach((g) => out.push({
      key: g.kind + ":" + g.id, icon: g.kind === "role" ? "i-users" : "i-user", name: g.name, sub: g.kind === "role" ? "role" : g.email || "", grant: g,
    }));
    return out;
  }

  function entryRow(e) {
    const g = e.grant;
    return html`<li data-key="${e.key}" class="share-row">${icon(e.icon)}
      <span class="share-who"><span class="share-name">${e.name}</span>${e.sub ? html`<small>${e.sub}</small>` : ""}</span>
      ${g ? html`<button type="button" class="icon-btn small" data-unshare="${e.key}" data-fk="unshare-${e.key}" aria-label="Stop sharing with ${e.name}"
        title="Stop sharing with ${e.name}">${icon("i-close")}</button>` : e.owner ? html`<span class="tag">Owner</span>` : ""}
      ${g && !g.canUse ? html`<p class="hint notice">${cannotUse(g)}</p>` : ""}</li>`;
  }

  // Why someone it's shared with can't use it (accounts.annotate): their roles, or the role, lack the permission
  // to; or, for a saved sign-in, they may do things its owner may not, and only a share from someone who may
  // see every sign-in reaches them (their checks would run as whatever account the owner's checks saved), while
  // its owner may still do all they could when it was made (vouchLapsed: they may do less now).
  function cannotUse(g) {
    if (!g.outranksOwner) {
      return g.kind === "role" ? g.name + " can't use it: the role doesn't include “" + k.use + "”."
        : g.name + " can't use it: their roles don't include “" + k.use + "”.";
    }
    // Nobody shares with themselves: whoever may see every sign-in reaches their own checks through a role.
    const you = g.kind === "user" && me() && me().id === g.id;
    const [who, them] = g.kind === "role" ? ["Members of " + g.name + " can't use it: the role has", "them"]
      : you ? ["You can't use it: you have", "you"] : [g.name + " can't use it: they have", "them"];
    const why = who + " permissions its owner doesn't, " + (g.vouchLapsed
      ? "and its owner may now do less than when it was shared, so the share no longer reaches " + them
      : "so only a share from someone with “Every saved sign-in” reaches " + them);
    if (!auth.can(k.all)) {
      return why + (g.vouchLapsed ? ". Someone with “Every saved sign-in” can stop sharing and share it again, which records what " +
        "its owner may do now." : ".");
    }
    if (you) {
      return why + ". Share it with " + (auth.state().admin ? "Admin" : "one of your roles") + ", or ask someone else with “Every " +
        "saved sign-in” to share it with you.";
    }
    return why + (g.vouchLapsed ? ". Stop sharing and share it again to record what its owner may do now."
      : ". Stop sharing and share it again to make it one.");
  }

  function renderList() {
    patchList($("#share-list"), entries(), (e) => e.key, entryRow);
  }

  // ---- changes --------------------------------------------------------------------
  // POSTs one change; the answer is the item as it's listed now. Returns it, or null when the sign-in it
  // asked for was dismissed (the dialog stays as it was) or it failed (and says why).
  async function change(path, body, done) {
    error("");
    try {
      const res = await api.post(k.path + encodeURIComponent(current.id) + path, body);
      if (res === null) return null;
      current = res;
      renderList();
      setText($("#share-status"), done);
      return res;
    } catch (err) {
      error(err.message);
      return null;
    }
  }

  // Whom it may be shared with: not yourself, its owner or whoever has it already, nor for an MFA secret the
  // Admin role, whose members see every one. A saved sign-in goes only to roles with no permission the sharer
  // lacks, unless they may see every sign-in (the server says the same of people, who only it can tell).
  async function candidates(q) {
    const found = await directory(q), you = me(), owner = current.owner ? current.owner.id : null;
    const taken = new Set((current.sharedWith || []).map((g) => g.kind + ":" + g.id));
    const within = (r) => kind !== "signin" || auth.can("signins.all") || r.permissions.every((p) => auth.can(p));
    const roles = found.roles.filter((r) => !taken.has("role:" + r.id) && !(kind === "mfa" && r.id === ADMIN_ROLE) && within(r));
    const users = found.users.filter((u) => u.id !== owner && !(you && u.id === you.id) && !taken.has("user:" + u.id));
    return [
      { label: "Roles", options: roles.map((r) => ({ key: "role:" + r.id, label: r.name, meta: "role", body: { role: r.id },
        note: r.id === ADMIN_ROLE ? "Admins' checks can then reuse it" : "" })) },
      { label: "People", options: users.map((u) => ({ key: "user:" + u.id, label: u.name, meta: u.email || "", body: { user: u.id } })) },
    ];
  }

  const adder = createPicker($("#share-add"), {
    id: "share-add", placeholder: "Find a person or a role", describedBy: "share-add-hint",
    options: candidates,
    empty: (q) => (q.length < 2 ? "No role matches. Type 2 letters or more to find people." : "No one matches “" + q + "”."),
    async choose(o) {
      return !!(await change("/shares/add", o.body, "Shared with " + o.label + "."));
    },
  });

  async function unshare(btn) {
    const key = btn.dataset.unshare, g = (current.sharedWith || []).find((x) => x.kind + ":" + x.id === key);
    if (!g) return;
    const keys = Array.from($("#share-list").children).map((li) => li.dataset.key), at = keys.indexOf(key);
    btn.disabled = true;
    const res = await change("/shares/remove", { [g.kind]: g.id }, "Stopped sharing with " + g.name + ".");
    if (!res) { if (btn.isConnected) { btn.disabled = false; btn.focus(); } return; }
    // Focus goes to the next one down (or up), else back to the field.
    const left = Array.from($("#share-list").querySelectorAll("[data-unshare]"));
    const next = left.find((b) => keys.indexOf(b.dataset.unshare) > at) || left[left.length - 1];
    (next || adder.input).focus();
  }

  dlg.firstElementChild.addEventListener("click", (e) => {
    let el;
    if (e.target.closest("[data-dismiss]")) return dlg.close();
    if ((el = e.target.closest("[data-unshare]"))) return unshare(el);
    if (e.target.closest("[data-change-owner]")) {
      moving = true;
      error("");
      renderOwner();
      return;
    }
    if (e.target.closest("[data-keep-owner]")) {
      moving = false;
      renderOwner();
      $("[data-change-owner]").focus();
    }
  });
  dlg.addEventListener("close", () => { if (onDone) onDone(); }, { once: true });

  renderOwner();
  renderList();
  dlg.showModal();
  adder.focus();
}
