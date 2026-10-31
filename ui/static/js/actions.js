// Grid admin actions: sign in first (if needed), then a confirm that names the target, then the
// request. Never a confirm followed by a surprise sign-in prompt.
import * as api from "./api.js";
import * as auth from "./auth.js";
import { app } from "./app.js";
import { clock, duration, plural, slotName } from "./format.js";
import { html, icon } from "./html.js";
import { confirm } from "./ui/dialog.js";
import { toast } from "./ui/toast.js";

export function needsSignIn() {
  const s = auth.state();
  return s.adminConfigured && !s.signedIn;
}

export async function ensureSignedIn(reason) {
  return needsSignIn() ? auth.signIn(reason) : true;
}

// A control for an admin action. Signed out, it says so with a lock and an accessible name like
// "Sign in to release"; it stays visible so people can see what is possible.
export function adminButton({ label, verb, attrs, cls = "btn ghost small", danger = false, fk }) {
  const locked = needsSignIn();
  return html`<button type="button" class="${cls} ${danger ? "destructive" : ""} ${locked ? "locked" : ""}" ${attrs}
    ${fk ? html`data-fk="${fk}"` : ""} ${locked ? html`aria-label="Sign in to ${verb}" title="Sign in to ${verb}"` : ""}>${locked ? icon("i-lock") : ""}${label}</button>`;
}

async function run(url, body, done, expect) {
  if (expect) app.live.expect(expect[0], expect[1]);
  try {
    const res = await api.post(url, body);
    if (res === null) return null; // sign-in dismissed: nothing happened
    toast(done);
    // The server names the events this action caused: the activity log shows them as "you", once.
    if (Array.isArray(res.events)) app.live.mine(res.events);
    return res;
  } catch (err) {
    toast(err.message, true);
    return null;
  } finally {
    app.live.refreshNow();
  }
}

export async function releaseSlot(id) {
  const f = app.findSlot(id);
  if (!f) return false;
  const name = slotName(f.node, f.slot);
  if (!(await ensureSignedIn("Sign in to release " + name))) return false;
  const s = f.session, started = s && app.ms(s.started);
  // The claim this confirm describes. If that check ends and another claims the slot while the
  // dialog is open, the server refuses (409) instead of cutting off the new one.
  const claimedAt = (s && s.started) || f.slot.last_run;
  const held = started != null ? "Claimed " + clock(started, false) + ", running " + duration((app.now() - started) / 1000) + (s.stale ? " (stale)" : "") + ". " : "";
  const ok = await confirm({
    title: "Release " + name + "?",
    text: held + "If that check is still running, the next check can claim this slot and both will fail.",
    ok: "Release slot",
  });
  if (!ok) return false;
  const target = { slot: f.slot.id, node: f.node.host, subject: name };
  const res = await run("api/slots/" + encodeURIComponent(f.slot.id) + "/release", claimedAt ? { claimed_at: claimedAt } : {},
    name + " released.", ["release", target]);
  return !!res;
}

export async function resetSlot(id) {
  const f = app.findSlot(id);
  if (!f) return false;
  const name = slotName(f.node, f.slot);
  if (!(await ensureSignedIn("Sign in to reset failures on " + name))) return false;
  const target = { slot: f.slot.id, node: f.node.host, subject: name };
  const res = await run("api/slots/" + encodeURIComponent(f.slot.id) + "/reset-failures", {}, "Failures reset on " + name + ".", ["reset", target]);
  return !!res;
}

export async function resetNode(host) {
  const n = app.findNode(host);
  if (!n) return false;
  if (!(await ensureSignedIn("Sign in to reset failures on " + host))) return false;
  const failing = n.slots.filter((s) => s.fails).length;
  const ok = await confirm({
    title: "Reset failures on " + host + "?",
    text: "Every slot on this node goes back to zero failures" + (failing ? " (" + plural(failing, "slot") + " " + (failing === 1 ? "has" : "have") + " some now)" : "") +
      ". Checks pick failing slots again straight away.",
    ok: "Reset failures", danger: false,
  });
  if (!ok) return false;
  const res = await run("api/nodes/reset-failures", { host }, "Failures reset on " + host + ".", ["reset", { node: host }]);
  return !!res;
}

export async function forgetNode(host) {
  const n = app.findNode(host);
  if (!n) return false;
  if (!(await ensureSignedIn("Sign in to forget " + host))) return false;
  const ok = await confirm({
    title: "Forget " + host + "?",
    text: "Offline for " + duration(n.last_seen_s) + ". This removes the node and its " + plural(n.total, "slot") +
      " from the grid. If its agent starts again, it registers itself again.",
    ok: "Forget node",
  });
  if (!ok) return false;
  const res = await run("api/nodes/forget", { host }, host + " was removed.", ["forget", { node: host }]);
  return !!res;
}

// Delegated handling for every admin control on the page: data-act="release|reset|reset-node|forget".
export function bindActions(root, after) {
  root.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-act]");
    if (!el || !root.contains(el)) return;
    e.preventDefault();
    const act = el.dataset.act, target = el.dataset.target;
    const fn = { release: releaseSlot, reset: resetSlot, "reset-node": resetNode, forget: forgetNode }[act];
    if (!fn) return;
    const done = await fn(act === "release" || act === "reset" ? Number(target) : target);
    if (after) after(el, done, act);
  });
}
