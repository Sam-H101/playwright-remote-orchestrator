// Admin sign-in: state, credentials, prompting and sign-out, and nothing else knows how it works.
// Today's provider is the shared admin token, kept for this browser tab only. Another provider
// (OAuth) changes this file and ui/auth.py: it adds an entry to SHEETS for its kind, and the server
// lists it in /api/session. Views only call state(), signIn(), signOut(), headers() and onChange(),
// and show state().summary rather than knowing what a provider is called.
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
let pending = null;

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

// Opens sign-in. Resolves true once signed in, false when dismissed. Concurrent callers share it.
export function signIn(reason) {
  if (pending) return pending;
  pending = (async () => {
    // The page may predate GRID_ADMIN_TOKEN being set: ask the server what it offers now.
    if (!session.providers.length) await init();
    const offered = session.providers.filter((p) => SHEETS[p.kind]);
    if (!offered.length) return false;
    return SHEETS[offered[0].kind](offered.filter((p) => p.kind === offered[0].kind), reason);
  })().finally(() => { pending = null; });
  return pending;
}

function secretSheet(providers, reason) {
  return new Promise((resolve) => {
    const dlg = document.getElementById("signin-dialog");
    const anchor = document.getElementById("account-btn");
    const purpose = typeof reason === "string" && !REASONS[reason] ? reason : "";
    dlg.__html = null; // always fresh elements: the listeners below belong to this one prompt
    setHTML(dlg, html`
      <form method="dialog" class="signin" novalidate>
        <header class="sheet-head">
          <h2 id="signin-title">Sign in</h2>
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
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      if (dlg.open) dlg.close();
      dlg.classList.remove("anchored");
      resolve(ok);
    };
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
        if (!body.signedIn) {
          adopt(body);
          return showError(body.adminConfigured ? "That " + providers[0].label.toLowerCase() + " was rejected." : "This grid has no admin sign-in configured.");
        }
        tab.set(value);
        adopt(body);
        notify();
        finish(true);
      } catch (err) {
        showError(err.message || String(err));
      } finally {
        btn.disabled = false;
      }
    });
    dlg.querySelector("[data-dismiss]").addEventListener("click", () => finish(false));
    dlg.addEventListener("close", function onClose() { dlg.removeEventListener("close", onClose); finish(false); });
    // On a wide screen the sheet hangs under the account chip; on a phone it's a bottom sheet.
    if (anchor && anchor.offsetParent && window.innerWidth >= 760) {
      const r = anchor.getBoundingClientRect();
      dlg.classList.add("anchored");
      dlg.style.setProperty("--anchor-top", Math.round(r.bottom + 6) + "px");
      dlg.style.setProperty("--anchor-right", Math.max(8, Math.round(window.innerWidth - r.right)) + "px");
    }
    dlg.showModal();
    if (input) input.focus();
  });
}
