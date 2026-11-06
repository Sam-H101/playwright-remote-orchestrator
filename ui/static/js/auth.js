// Sign-in: state, credentials, prompting and sign-out, and nothing else knows how it works. The
// server (ui/auth.py) offers two providers. The shared admin token is kept for this browser tab only.
// With single sign-on, the identity provider signs people in and the server sets a session cookie
// this page never sees. /api/session lists what is offered, who is signed in and what they may do.
// Views only call state(), can(), signIn(), signOut(), headers() and onChange(), and show
// state().summary and state().ssoLabel rather than knowing what a provider is called; the account
// chip also uses signingIn() and closeSignIn() to open and close the prompt it anchors.
import { html, icon, setHTML } from "./html.js";

const KEY = "gridToken";
const HEADER = "X-Grid-Token";
const REFUSED = "X-Grid-Token-Refused"; // on the answer to a request whose secret was wrong (app.py)
// What anyone may do on a grid with no sign-in configured (OPEN_GRID in ui/permissions.py).
const OPEN_GRID = ["grid.view", "grid.operate"];

// The secret lives in tab-scoped storage, which can be blocked (privacy modes) and must never throw.
const tab = {
  get() { try { return window.sessionStorage.getItem(KEY); } catch (e) { return null; } },
  set(value) { try { window.sessionStorage.setItem(KEY, value); return true; } catch (e) { return false; } },
  remove() { try { window.sessionStorage.removeItem(KEY); } catch (e) { /* nothing stored */ } },
};

// Who is signed in and what they may do; signedOut() is the same for nobody.
const signedOut = () => ({ signedIn: false, name: null, method: null, user: null, permissions: [], roles: [], admin: false });
let session = Object.assign(signedOut(), { adminConfigured: false, providers: [], readsOpen: true, unavailable: false, known: false });
const listeners = new Set();
let pending = null, asking; // the open sign-in, and the reason it was asked for
let dismiss = null; // closes the open prompt as cancelled
let asked = 0; // sign-ins asked for, so one replaced before its prompt showed never shows it
let lastEnded = null; // why the server last stopped taking the session cookie (a key of ENDED)
let refreshing = null, lookedAt = 0; // the refresh() under way, and when the last one started

const secret = () => tab.get() || "";

function notify() {
  const s = state();
  listeners.forEach((cb) => { try { cb(s); } catch (e) { console.error(e); } });
}

// "Signed in with admin token · this tab only": what the account menu says, worded per provider kind.
// An identity provider's label is a name ("Microsoft") and keeps its capital.
function summary() {
  if (!session.signedIn) return "";
  const p = session.providers.find((x) => x.id === session.method);
  if (p && p.kind === "redirect") return "Signed in with " + p.label + (session.user && session.user.email ? " as " + session.user.email : "");
  const label = p ? p.label.charAt(0).toLowerCase() + p.label.slice(1) : "admin sign-in";
  return "Signed in with " + label + (!p || p.kind === "secret" ? " · this tab only" : "");
}

export function state() {
  const sso = session.providers.find((p) => p.kind === "redirect");
  return {
    signedIn: session.signedIn,
    name: session.name,
    provider: session.method,
    adminConfigured: session.adminConfigured,
    providers: session.providers.slice(),
    summary: summary(),
    known: session.known,
    user: session.user,
    permissions: session.permissions.slice(),
    roles: session.roles.slice(),
    admin: session.admin,
    readsOpen: session.readsOpen,
    unavailable: session.unavailable,
    // Single sign-on: whether it's offered, its name for people ("Microsoft"), and why it can't work as
    // set up, when it can't.
    sso: !!sso,
    ssoLabel: sso ? sso.label : null,
    ssoProblem: (sso && sso.problem) || null,
  };
}

// Whether this page may do perm (an id from /api/permissions) now. With no sign-in configured anyone
// may run the grid, and nothing else is offered.
export function can(perm) {
  return !session.adminConfigured ? OPEN_GRID.includes(perm) : session.permissions.includes(perm);
}

export function onChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

// Credentials for one request. The header is sent even when empty: the server's same-origin
// guard accepts it as proof the request came from this page. A session cookie goes by itself.
export function headers() {
  return { [HEADER]: secret() };
}

// Looks at the answer r to a request that sent these headers (from headers()). When it says the secret
// in them was wrong, though something else let the request through (a session cookie, or reading
// that's open to anyone), the secret is no use here (GRID_ADMIN_TOKEN changed since it was typed), and
// every request that sends it counts towards the guessing limit: it's forgotten, unless another has
// taken its place meanwhile.
export function answered(r, sent) {
  if (!r.ok || !r.headers.get(REFUSED) || !sent[HEADER] || sent[HEADER] !== secret()) return;
  tab.remove();
  // A refresh under way asked with the secret, and its answer is about that: ask again once it's in.
  (refreshing || Promise.resolve()).then(() => refresh());
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

// Whether the secret sent is what vouched for the request, rather than a session cookie beside it.
const bySecret = (body) => (Array.isArray(body.providers) ? body.providers : []).some((p) => p.kind === "secret" && p.id === body.method);

function adopt(body) {
  const next = {
    signedIn: !!body.signedIn, name: body.name || null, method: body.method || null, user: body.user || null,
    permissions: Array.isArray(body.permissions) ? body.permissions : [], roles: Array.isArray(body.roles) ? body.roles : [], admin: !!body.admin,
    adminConfigured: !!body.adminConfigured, providers: Array.isArray(body.providers) ? body.providers : [],
    readsOpen: body.readsOpen !== false, unavailable: !!body.unavailable, known: true,
  };
  // The database is down, so the server couldn't look at the session cookie: as far as the page can
  // tell, whoever was signed in still is, until it can ask again.
  if (next.unavailable && session.known && !next.signedIn) {
    ["signedIn", "name", "method", "user", "permissions", "roles", "admin"].forEach((k) => { next[k] = session[k]; });
  }
  session = next;
}

// Asks the server who the stored secret (or the session cookie) signs in, and takes its answer, unless
// the secret changed while it asked: a sign-in or sign-out since then is newer. Throws when it can't ask.
async function check() {
  const sent = secret();
  const body = await ask(sent);
  if (secret() !== sent) return;
  // A stored secret the server no longer accepts is useless, and each wrong one counts towards the
  // guessing limit: forget it rather than resend it. A session cookie vouching beside it doesn't count.
  if (sent && !bySecret(body)) tab.remove();
  adopt(body);
}

export async function init() {
  // ?signin=<code>: back from the identity provider without a session, and why (app.py). It leaves the
  // address at once, so a reload or a bookmark doesn't say it again.
  const code = new URLSearchParams(location.search).get("signin");
  if (code !== null) history.replaceState(history.state, "", location.pathname + location.hash);
  try {
    await check();
  } catch (e) {
    session.known = false;
  }
  notify();
  if (code !== null && session.known && !session.signedIn) signIn(REASONS["signin:" + code] ? "signin:" + code : "signin:failed");
  return state();
}

// What decides what the page shows: a change to any of it is worth telling the views.
const decides = () => JSON.stringify([session.known, session.signedIn, session.method, session.permissions, session.roles, session.adminConfigured, session.readsOpen]);

// Asks the server again, as init() does, and tells the page only when that changed what it may show:
// a role given or taken while the page is open, a session ended elsewhere. After a refusal for lack of
// permission, after a token sign-out (a single sign-on session may still be there), and when the tab is
// shown again, at most once every atMostEveryMs.
export function refresh(atMostEveryMs = 0) {
  if (refreshing) return refreshing;
  if (atMostEveryMs && Date.now() - lookedAt < atMostEveryMs) return Promise.resolve(state());
  lookedAt = Date.now();
  const before = decides();
  refreshing = check().catch(() => { /* no answer: the page keeps what it knew */ }).then(() => {
    if (decides() !== before) notify();
    return state();
  }).finally(() => { refreshing = null; });
  return refreshing;
}

// The server refused our credentials: we're signed out, whatever we thought. reason: why it stopped
// taking the session cookie, which the prompt that follows a refused action then says.
export function markSignedOut(reason) {
  tab.remove();
  lastEnded = ENDED[reason] ? reason : null;
  const changed = session.signedIn || !session.adminConfigured;
  Object.assign(session, signedOut(), { adminConfigured: true });
  if (changed) notify();
}

// Signing out of single sign-on ends the session at the server ({everywhere}: every session of the
// person's), and with GRID_OIDC_END_SESSION=1 goes on to sign out of the identity provider. Signing
// out of the admin token only forgets it here.
export async function signOut({ everywhere = false } = {}) {
  const p = session.providers.find((x) => x.id === session.method);
  if (!p || p.kind !== "redirect") {
    tab.remove();
    Object.assign(session, signedOut());
    notify();
    refresh(); // a single sign-on session may still be there
    return;
  }
  // Not through api.js, which imports this file, and whose refusals would open a prompt to sign in.
  let r;
  try {
    r = await fetch("api/session/signout", {
      method: "POST", cache: "no-store", credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json", [HEADER]: secret() },
      body: JSON.stringify({ everywhere }),
    });
  } catch (e) {
    throw new Error("No answer from the Grid UI server at " + location.host + ".");
  }
  let body = {};
  try { body = await r.json(); } catch (e) { body = {}; }
  if (!r.ok) {
    refresh();
    throw new Error(body.error || "Signing out failed with HTTP " + r.status + ".");
  }
  Object.assign(session, signedOut());
  notify();
  if (typeof body.endSessionUrl === "string" && /^https?:\/\//i.test(body.endSessionUrl)) location.assign(body.endSessionUrl);
}

const REASONS = {
  rejected: "Your sign-in was refused. Sign in again to continue.",
  expired: "Sign in again to continue.",
  // Back from the identity provider without a session (?signin=<code>): shown as a notice in the prompt.
  "signin:expired": "That sign-in took too long, was already used, or was started in another browser or tab. Sign in again.",
  "signin:denied": "The identity provider didn't sign you in.",
  "signin:failed": "The Grid UI couldn't confirm who you are with the identity provider. Its log says why.",
  "signin:disabled": "Your account on this grid is disabled. Ask an admin to enable it.",
  "signin:unavailable": "Signing in needs the identity provider and the grid's database, and one of them can't be reached right now. Try again in a minute.",
  "signin:misconfigured": "Single sign-on isn't set up correctly. The Grid UI's log says what's wrong; an admin can sign in with the admin token meanwhile.",
  "signin:busy": "Too many sign-ins are under way. Try again in a minute.",
};

// Why the server stopped taking a session cookie (the reason of a refusal), as the prompt after it says.
const ENDED = {
  expired: "Your sign-in has expired. Sign in again.",
  idle: "You were signed out after a while without using the grid. Sign in again.",
  revoked: "Your session was ended, by signing out elsewhere or by an admin. Sign in again.",
  disabled: "Your account on this grid is disabled. Ask an admin to enable it.",
};

// Opens sign-in. Resolves true once signed in, false when dismissed. Concurrent callers with the
// same reason share it. Another reason means another action while the prompt left the page usable
// (a scripted click, an assistive technology's default action): that cancels the open prompt, as a
// press elsewhere does, and asks again for the new one, so one sign-in never runs two actions. The
// same goes for a prompt that hasn't shown yet: it never does. Single sign-on leaves the page for
// the identity provider, so it never resolves true: the page loads again once it's done.
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
    const offered = session.providers.filter((p) => p.kind === "secret" || p.kind === "redirect");
    if (!offered.length) return false;
    return signInSheet(offered, reason);
  })().finally(() => { pending = null; });
  return pending;
}

export const signingIn = () => pending !== null;

// Cancels the open prompt, as its Cancel button does.
export function closeSignIn() {
  if (dismiss) dismiss();
}

// Single sign-on, with the admin token tucked under it for when single sign-on is down. The way in is
// a link, outside any form: the page's form-action 'self' would stop the browser following a submitted
// form's redirect on to the identity provider.
function redirectSheet(p, secretProvider, reason, sub) {
  // misconfigured says what the problem below says already.
  const failed = /^signin:/.test(reason) && !(p.problem && reason === "signin:misconfigured") ? REASONS[reason] : "";
  return html`
      <div class="signin signin-redirect">
        <header class="sheet-head">
          <h2 id="signin-prompt-title">Sign in</h2>
          <p class="sheet-sub">${sub}</p>
        </header>
        <p class="form-error notice" id="signin-error" role="alert" ${failed ? "" : "hidden"}>${failed}</p>
        ${p.problem ? html`<button type="button" class="btn primary signin-sso" id="signin-sso" disabled>${icon("i-login")}Sign in with ${p.label}</button>
          <p class="form-error notice signin-problem" id="signin-problem">${p.problem}</p>`
          : html`<a class="btn primary signin-sso" id="signin-sso" href="auth/login?return=${encodeURIComponent(location.hash || "#/overview")}">${icon("i-login")}Sign in with ${p.label}</a>
          <p class="hint signin-back">You'll come back to this page.${reason === "rejected" ? " Then try it again." : ""}</p>`}
        ${secretProvider ? html`<details class="advanced signin-other" id="signin-other" ${p.problem ? "open" : ""}>
          <summary>${icon("i-chevron")}Use the admin token instead</summary>
          <form method="dialog" novalidate>
            <div class="field">
              <label for="signin-${secretProvider.id}">${secretProvider.label}</label>
              <div class="signin-secret">
                <input type="password" id="signin-${secretProvider.id}" name="${secretProvider.id}" autocomplete="off" spellcheck="false" required
                  aria-describedby="signin-hint signin-error">
                <button type="submit" class="btn" value="ok" id="signin-ok">Sign in</button>
              </div>
              <p class="hint" id="signin-hint">For when single sign-on is down. Kept for this browser tab only.</p>
            </div>
          </form>
        </details>` : ""}
        <div class="dialog-actions">
          <button type="button" class="btn ghost" data-dismiss>Cancel</button>
        </div>
      </div>`;
}

function signInSheet(providers, reason) {
  return new Promise((resolve) => {
    const dlg = document.getElementById("signin-dialog");
    const anchor = document.getElementById("account-btn");
    // On a wide screen the prompt hangs under the account chip like a menu and, like a menu, leaves
    // the page usable: a press or focus anywhere else closes it (and still does what it pressed), as
    // does Esc. On a phone it's a bottom sheet, and over another dialog (a form whose request was
    // refused) it must be modal too, or that dialog would leave it out of reach.
    const anchored = !!(anchor && anchor.offsetParent && window.innerWidth >= 760 && !document.querySelector("dialog[open]:modal"));
    const purpose = typeof reason === "string" && !REASONS[reason] ? reason : "";
    const redirect = providers.find((p) => p.kind === "redirect"), secretProvider = providers.find((p) => p.kind === "secret");
    // After a refused action, why the session ended, when the server said. A failure coming back from the
    // identity provider is a notice of its own (redirectSheet).
    const why = reason === "rejected" && ENDED[lastEnded] ? ENDED[lastEnded] : /^signin:/.test(reason) ? "" : REASONS[reason];
    dlg.__html = null; // always fresh elements: the listeners below belong to this one prompt
    setHTML(dlg, redirect ? redirectSheet(redirect, secretProvider, reason, purpose ? purpose + "." : why || "Sign in to use this grid.") : html`
      <form method="dialog" class="signin" novalidate>
        <header class="sheet-head">
          <h2 id="signin-prompt-title">Sign in</h2>
          <p class="sheet-sub">${purpose ? purpose + "." : why || "Admin actions need you to sign in."}</p>
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
    const other = dlg.querySelector("#signin-other"), sso = dlg.querySelector("a#signin-sso");
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
      error.classList.remove("notice");
      input.setAttribute("aria-invalid", "true");
      input.select();
      input.focus();
    };
    if (form) form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const value = input.value;
      if (!value) return showError("Enter the " + secretProvider.label.toLowerCase() + ".");
      const btn = dlg.querySelector("#signin-ok");
      btn.disabled = true;
      try {
        const body = await ask(value);
        if (done) return; // closed while it checked: closing cancels, whatever the answer
        // Only the token itself signs in here, not a session cookie that happened to ride along.
        if (!body.signedIn || body.method !== secretProvider.id) {
          if (!body.signedIn) adopt(body);
          return showError(body.adminConfigured ? "That " + secretProvider.label.toLowerCase() + " was rejected." : "This grid has no admin sign-in configured.");
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
    if (other) other.addEventListener("toggle", () => { if (other.open) input.focus(); });
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
        const stops = Array.from(dlg.querySelectorAll("a[href], summary, input, button")).filter((el) => !el.disabled && el.getClientRects().length);
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
    // The way in that's open: the token where it's the only one (or single sign-on can't work), else the link.
    (input && (!redirect || (other && other.open)) ? input : sso || dlg.querySelector("[data-dismiss]")).focus();
  });
}
