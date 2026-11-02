// Admin sign-in: state, credentials, prompting and sign-out, and nothing else knows how it works.
// Today's provider is the shared admin token, kept for this browser tab only. Another provider
// (OAuth) changes this file and ui/auth.py: it adds an entry to SHEETS for its kind, and the server
// lists it in /api/session. Views only call state(), signIn(), signOut(), headers() and onChange(),
// and show state().summary rather than knowing what a provider is called; the account chip also
// uses signingIn() and closeSignIn() to open and close the prompt it anchors.
import { html, setHTML } from "./html.js";

const KEY = "gridToken";
const HEADER = "X-Grid-Token";

// The secret lives in tab-scoped storage, which can be blocked (privacy modes) and must never throw.
const tab = {
  get() { try { return window.sessionStorage.getItem(KEY); } catch (e) { return null; } },
  set(value) { try { window.sessionStorage.setItem(KEY, value); return true; } catch (e) { return false; } },
  remove() { try { window.sessionStorage.removeItem(KEY); } catch (e) { /* nothing stored */ } },
};

let session = { signedIn: false, name: null, method: null, adminConfigured: false, providers: [], known: false };
const listeners = new Set();
let pending = null, asking; // the open sign-in, and the reason it was asked for
let dismiss = null; // closes the open prompt as cancelled
let asked = 0; // sign-ins asked for, so one replaced before its prompt showed never shows it

const secret = () => tab.get() || "";

function notify() {
  const s = state();
  listeners.forEach((cb) => { try { cb(s); } catch (e) { console.error(e); } });
}

// "Signed in with admin token · this tab only": what the account menu says, worded per provider kind.
function summary() {
  if (!session.signedIn) return "";
  const p = session.providers.find((x) => x.id === session.method);
  const label = p ? p.label.charAt(0).toLowerCase() + p.label.slice(1) : "admin sign-in";
  return "Signed in with " + label + (!p || p.kind === "secret" ? " · this tab only" : "");
}

export function state() {
  return {
    signedIn: session.signedIn,
    name: session.name,
    provider: session.method,
    adminConfigured: session.adminConfigured,
    providers: session.providers.slice(),
    summary: summary(),
    known: session.known,
  };
}

export function onChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

// Credentials for one request. The header is sent even when empty: the server's same-origin
// guard accepts it as proof the request came from this page.
export function headers() {
  return { [HEADER]: secret() };
}

async function ask(candidate) {
  const r = await fetch("api/session", {
    cache: "no-store", credentials: "same-origin",
    headers: { Accept: "application/json", [HEADER]: candidate },
  });
  if (r.status === 429) {
    let body = {};
    try { body = await r.json(); } catch (e) { body = {}; }
    throw new Error(body.error || "Too many wrong attempts. Try again later.");
  }
  if (!r.ok) throw new Error("The sign-in check failed with HTTP " + r.status + ".");
  return r.json();
}

function adopt(body) {
  session = {
    signedIn: !!body.signedIn, name: body.name || null, method: body.method || null,
    adminConfigured: !!body.adminConfigured, providers: Array.isArray(body.providers) ? body.providers : [], known: true,
  };
}

export async function init() {
  try {
    const body = await ask(secret());
    // A stored secret the server no longer accepts is useless: forget it rather than resend it.
    if (!body.signedIn && secret()) tab.remove();
    adopt(body);
  } catch (e) {
    session.known = false;
  }
  notify();
  return state();
}

// The server refused our credentials: we're signed out, whatever we thought.
export function markSignedOut() {
  tab.remove();
  if (session.signedIn) { session.signedIn = false; session.name = session.method = null; }
  session.adminConfigured = true;
  notify();
}

export function signOut() {
  tab.remove();
  session.signedIn = false;
  session.name = session.method = null;
  notify();
}

const REASONS = {
  rejected: "Your sign-in was refused. Sign in again to continue.",
  expired: "Sign in again to continue.",
};

// One sheet per provider kind. "secret": a password field checked against /api/session.
const SHEETS = { secret: secretSheet };

// Opens sign-in. Resolves true once signed in, false when dismissed. Concurrent callers with the
// same reason share it. Another reason means another action while the prompt left the page usable
// (a scripted click, an assistive technology's default action): that cancels the open prompt, as a
// press elsewhere does, and asks again for the new one, so one sign-in never runs two actions. The
// same goes for a prompt that hasn't shown yet: it never does.
export function signIn(reason) {
  if (pending && reason === asking) return pending;
  if (pending) {
    const prev = pending;
    asked++;
    if (dismiss) dismiss(true);
    return prev.then(() => signIn(reason));
  }
  asking = reason;
  const nth = ++asked;
  pending = (async () => {
    // The page may predate GRID_ADMIN_TOKEN being set: ask the server what it offers now.
    if (!session.providers.length) await init();
    if (nth !== asked) return false;
    const offered = session.providers.filter((p) => SHEETS[p.kind]);
    if (!offered.length) return false;
    return SHEETS[offered[0].kind](offered.filter((p) => p.kind === offered[0].kind), reason);
  })().finally(() => { pending = null; });
  return pending;
}

export const signingIn = () => pending !== null;

// Cancels the open prompt, as its Cancel button does.
export function closeSignIn() {
  if (dismiss) dismiss();
}

function secretSheet(providers, reason) {
  return new Promise((resolve) => {
    const dlg = document.getElementById("signin-dialog");
    const anchor = document.getElementById("account-btn");
    // On a wide screen the prompt hangs under the account chip like a menu and, like a menu, leaves
    // the page usable: a press or focus anywhere else closes it (and still does what it pressed), as
    // does Esc. On a phone it's a bottom sheet, and over another dialog (a form whose request was
    // refused) it must be modal too, or that dialog would leave it out of reach.
    const anchored = !!(anchor && anchor.offsetParent && window.innerWidth >= 760 && !document.querySelector("dialog[open]:modal"));
    const purpose = typeof reason === "string" && !REASONS[reason] ? reason : "";
    dlg.__html = null; // always fresh elements: the listeners below belong to this one prompt
    setHTML(dlg, html`
      <form method="dialog" class="signin" novalidate>
        <header class="sheet-head">
          <h2 id="signin-prompt-title">Sign in</h2>
          <p class="sheet-sub">${purpose ? purpose + "." : REASONS[reason] || "Admin actions need you to sign in."}</p>
        </header>
        ${providers.map((p) => html`
          <div class="field">
            <label for="signin-${p.id}">${p.label}</label>
            <input type="password" id="signin-${p.id}" name="${p.id}" autocomplete="off" spellcheck="false" required
              aria-describedby="signin-hint signin-error">
            <p class="hint" id="signin-hint">Kept for this browser tab only.</p>
          </div>`)}
        <p class="form-error" id="signin-error" role="alert" hidden></p>
        <div class="dialog-actions">
          <button type="button" class="btn ghost" data-dismiss>Cancel</button>
          <button type="submit" class="btn primary" value="ok" id="signin-ok">Sign in</button>
        </div>
      </form>`);
    const form = dlg.querySelector("form"), input = dlg.querySelector("input"), error = dlg.querySelector("#signin-error");
    const listening = new AbortController(), only = { signal: listening.signal };
    const opener = document.activeElement;
    const owner = () => (opener && opener.isConnected && opener !== document.body ? opener : null);
    let done = false;
    // away: closed by a press or focus elsewhere, which keeps focus where it went. Otherwise focus
    // goes back to whatever opened the prompt (the browser does that for a modal one).
    const finish = (ok, away) => {
      if (done) return;
      done = true;
      dismiss = null;
      listening.abort();
      // Focus on the page itself was lost from in here: the Sign in button is disabled while it checks.
      const active = document.activeElement;
      const inside = !active || active === document.body || dlg.contains(active);
      if (inside && away && active) active.blur();
      if (dlg.open) dlg.close();
      dlg.classList.remove("anchored");
      if (anchored) {
        anchor.setAttribute("aria-expanded", "false");
        if (inside && !away && owner()) owner().focus({ preventScroll: true });
      }
      resolve(ok);
    };
    dismiss = (away) => finish(false, away);
    const showError = (text) => {
      error.textContent = text;
      error.hidden = false;
      input.setAttribute("aria-invalid", "true");
      input.select();
      input.focus();
    };
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const value = input.value;
      if (!value) return showError("Enter the " + providers[0].label.toLowerCase() + ".");
      const btn = dlg.querySelector("#signin-ok");
      btn.disabled = true;
      try {
        const body = await ask(value);
        if (done) return; // closed while it checked: closing cancels, whatever the answer
        if (!body.signedIn) {
          adopt(body);
          return showError(body.adminConfigured ? "That " + providers[0].label.toLowerCase() + " was rejected." : "This grid has no admin sign-in configured.");
        }
        tab.set(value);
        adopt(body);
        notify();
        finish(true);
      } catch (err) {
        if (!done) showError(err.message || String(err));
      } finally {
        btn.disabled = false;
      }
    });
    dlg.querySelector("[data-dismiss]").addEventListener("click", () => finish(false));
    // A press that closes one prompt and opens the next one leaves the first one's close event
    // queued: it finds the dialog open again, and isn't about this prompt.
    dlg.addEventListener("close", () => { if (!dlg.open) finish(false); }, only);
    if (anchored) {
      // From the right edge of the page, not of the window: a scrollbar isn't part of it.
      const r = anchor.getBoundingClientRect();
      dlg.classList.add("anchored");
      dlg.style.setProperty("--anchor-top", Math.round(r.bottom + 6) + "px");
      dlg.style.setProperty("--anchor-right", Math.max(8, Math.round(document.documentElement.clientWidth - r.right)) + "px");
      anchor.setAttribute("aria-expanded", "true");
      // The chip is left to its own click, which closes the prompt it opened.
      document.addEventListener("pointerdown", (e) => {
        if (!dlg.contains(e.target) && !anchor.contains(e.target)) finish(false, true);
      }, { capture: true, signal: listening.signal });
      // Focus arriving anywhere else closes it too. Focus leaving the window (a password manager) doesn't.
      document.addEventListener("focusin", (e) => {
        if (!dlg.contains(e.target) && !anchor.contains(e.target)) finish(false, true);
      }, { capture: true, signal: listening.signal });
      // The dialog is at the end of the page, but in the tab order it sits where it shows, just after
      // what opened it: Shift+Tab out of it goes back there, and Tab goes on from there.
      dlg.addEventListener("keydown", (e) => {
        if (e.key !== "Tab" || e.altKey || e.ctrlKey || e.metaKey) return;
        const stops = Array.from(dlg.querySelectorAll("input, button")).filter((el) => !el.disabled);
        const at = stops.indexOf(document.activeElement);
        if (e.shiftKey ? at > 0 : at < stops.length - 1) return;
        const to = owner() || anchor;
        if (e.shiftKey) e.preventDefault(); // Tab isn't: the browser moves on from `to`
        finish(false, true);
        to.focus();
      }, only);
      // Esc closes the prompt and only the prompt; a modal on top (the palette) or an open menu has it first.
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape" || document.querySelector("dialog[open]:modal, .menu:not([hidden])")) return;
        e.preventDefault();
        e.stopPropagation();
        finish(false);
      }, { capture: true, signal: listening.signal });
      dlg.show();
    } else {
      // A press on the backdrop closes the sheet; a drag that starts inside it doesn't.
      const outside = (e) => {
        const r = dlg.getBoundingClientRect();
        return e.target === dlg && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom);
      };
      let downOutside = false;
      dlg.addEventListener("pointerdown", (e) => { downOutside = outside(e); }, only);
      dlg.addEventListener("click", (e) => { if (downOutside && outside(e)) finish(false); }, only);
      dlg.showModal();
    }
    if (input) input.focus();
  });
}
