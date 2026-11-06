// MFA & sign-ins: what checks need to get through two-step sign-ins.
// MFA secrets are authenticator (TOTP) seeds the Grid UI keeps for checks that sign in with two-step
// verification. Seeds go in through the add dialog and never come back out: the page shows codes,
// counted down by the server's clock, and who was handed which window's code.
// Saved sign-ins are the browser states checks save after signing in and reuse on later runs. Checks add
// them; the page shows what is saved, how it is used and who is signing in now, never a cookie.
// With single sign-on, each secret and sign-in has an owner, who may share it with people and roles; each
// part shows what this person's roles let them see, and says so when they let them see none.
import { app } from "../app.js";
import * as api from "../api.js";
import * as auth from "../auth.js";
import { html, icon, patchList, setHTML, setText } from "../html.js";
import { duration, fullDate, parseTime, plural } from "../format.js";
import { confirm, dismissible } from "../ui/dialog.js";
import { openShare } from "../ui/share.js";
import { copy, toast } from "../ui/toast.js";

const MFA_NAME = /^[A-Za-z0-9._-]{1,64}$/;
// A code looked at hides itself after this long, and whenever the view or the tab is hidden. One taken
// with Use code is the admin's alone: it stays until its window closes, tab hidden or not.
const AUTO_HIDE_MS = 60000;
const POLL_MS = 5000;
// Below this, a check asking now would get the next window's code (the server's MFA_MIN_VALID_S).
const LOW_S = 5;
const KEY_COMMAND = 'echo "GRID_SECRETS_KEY=$(openssl rand -hex 32)" >> .env';
const ADMIN_COMMAND = 'echo "GRID_ADMIN_TOKEN=$(openssl rand -hex 32)" >> .env';
const RESTART = "docker compose up -d grid_ui";
const REBUILD = "docker compose build grid_ui && docker compose up -d grid_ui";
const isUri = (v) => v.trim().toLowerCase().indexOf("otpauth:") === 0;

// "482193" -> two halves, so the eye can hold it; copying the text gives the digits alone.
function codeMarkup(code) {
  const cut = Math.ceil(code.length / 2);
  return html`<span class="half">${code.slice(0, cut)}</span><span class="half">${code.slice(cut)}</span>`;
}

const settings = (s) => s.digits + " digits · " + s.period + " s · " + s.algorithm;
const who = (s) => [s.issuer, s.account].filter(Boolean).join(" · ");
// Whose rows the page lists: the person's own, those shared with them, or (with mfa.all or signins.all)
// everyone's. Each row's access says why they see it.
const SCOPES = { mine: (s) => s.access === "owner", shared: (s) => s.access === "shared", all: () => true };
// Why the checks of whoever looks can't use a sign-in they see (store._rights): it's shared with them, but they
// may do more than the owner and no share from someone who may see every sign-in reaches them (store._reaches),
// or they see it only because they may see every one. Admins' checks use what is shared with the Admin role;
// anyone else's, what is shared with a role of theirs.
function unusable(s) {
  if (s.access === "shared") {
    return "It's shared with you, but you may do things its owner may not, so it only reaches your checks when someone with “Every saved sign-in” shares it. " +
      "Such a share stops reaching you once its owner may do less than when it was made.";
  }
  return auth.state().admin
    ? "It isn't shared with you or with Admin. Share it with Admin, or give it to Admins, to let your checks use it."
    : "It isn't shared with you or a role you have. Share it with one of your roles, or give it to Admins, to let your checks use it.";
}

function snippet(name) {
  return "from src import mfa\n\n" +
    'page.get_by_label("Code").fill(mfa.code("' + name + '", label="nightly sign-in",\n' +
    "    sleep=lambda s: page.wait_for_timeout(s * 1000)))";
}

// The whole of a check that reuses its sign-in: name is the saved sign-in's, secret the MFA secret's.
function reuseSnippet(name, secret) {
  return "from playwright.sync_api import expect\nfrom src import mfa, signin\n\n" +
    'APP = "https://intranet.example.com"\n\n' +
    "def sign_in(page):      # the whole sign-in: password, then the MFA code\n" +
    "    page.goto(APP)\n" +
    "    ...\n" +
    '    page.get_by_label("Code").fill(mfa.code("' + secret + '"))\n\n' +
    "def signed_in(page):    # opens the app: signed in, or asked to sign in again?\n" +
    "    page.goto(APP)\n" +
    '    home = page.get_by_role("heading", name="Home")\n' +
    '    asked = page.get_by_role("heading", name="Sign in")\n' +
    "    expect(home.or_(asked)).to_be_visible()\n" +
    "    return home.is_visible()\n\n" +
    'with signin.reuse(browser, "' + name + '",\n' +
    "                  sign_in=sign_in, signed_in=signed_in) as s:\n" +
    '    s.page.get_by_role("link", name="Reports").click()';
}

function command(text) {
  return html`<div class="snippet"><code class="mono">${text}</code>
    <button type="button" class="icon-btn tiny" data-copy="${text}" data-copy-label="the command" aria-label="Copy the command">${icon("i-copy")}</button></div>`;
}

export function createMfa(section) {
  setHTML(section, html`
    <header class="view-head">
      <h1 id="mfa-h1" tabindex="-1">MFA &amp; sign-ins</h1>
    </header>
    <div class="banner" id="mfa-banner" role="status" hidden></div>
    <div class="toolbar" id="mfa-toolbar" hidden>
      <div class="segmented" id="mfa-scope" role="group" aria-label="Whose">
        <button type="button" data-scope="mine" aria-pressed="false">Mine</button>
        <button type="button" data-scope="shared" aria-pressed="false">Shared with me</button>
        <button type="button" data-scope="all" aria-pressed="false">All</button>
      </div>
    </div>
    <div id="mfa-state"></div>
    <div id="mfa-key"></div>
    <section class="mfa-part" id="mfa-secrets" aria-labelledby="mfa-secrets-title" hidden>
      <header class="part-head access-head">
        <div>
          <h2 id="mfa-secrets-title">MFA secrets</h2>
          <p class="view-sub">Authenticator-app seeds for the sites your checks sign in to with two-step verification. A check asks the Grid UI
            for a code when the site's prompt is on screen: the seed stays here, encrypted, and each window's code is handed out once.</p>
        </div>
        <div class="head-actions">
          <button type="button" class="btn primary" id="add-mfa-btn" data-add-mfa hidden>${icon("i-plus")}Add secret</button>
        </div>
      </header>
      <div id="mfa-live"></div>
      <div class="panel table-panel" id="mfa-panel" hidden>
        <table class="table mfa-secrets">
          <caption class="sr-only">MFA secrets</caption>
          <thead><tr>
            <th scope="col">Secret</th><th scope="col">Settings</th><th scope="col">Last handed out</th>
            <th scope="col" class="num-col">Handed out</th><th scope="col"><span class="sr-only">Actions</span></th>
          </tr></thead>
          <tbody id="mfa-rows"></tbody>
        </table>
      </div>
      <div class="mfa-lower" id="mfa-lower" hidden>
        <section class="panel mfa-log" aria-labelledby="mfa-log-title">
          <header class="panel-head"><h3 id="mfa-log-title">Recent hand-outs</h3><span class="panel-sub" id="mfa-log-sub"></span></header>
          <div class="mfa-log-scroll">
            <table class="table mfa-issues">
              <caption class="sr-only">Codes handed out in the last 7 days, newest first</caption>
              <thead><tr><th scope="col">When</th><th scope="col">Secret</th><th scope="col">Handed to</th><th scope="col">Label</th></tr></thead>
              <tbody id="mfa-issue-rows"></tbody>
            </table>
            <div class="empty compact" id="mfa-issues-empty" hidden><p>No codes handed out in the last 7 days.</p></div>
          </div>
        </section>
        <section class="panel mfa-use" aria-labelledby="mfa-use-title">
          <h3 id="mfa-use-title">Use it from a check</h3>
          <p class="hint">Where the site asks for the code. <span class="mono">mfa.code()</span> waits when the code comes from the next window;
            the sleep keeps the page served meanwhile.</p>
          <div class="snippet"><code class="mono" id="mfa-snippet"></code>
            <button type="button" class="icon-btn tiny" id="mfa-snippet-copy" data-copy="" data-copy-label="the snippet" aria-label="Copy the snippet">${icon("i-copy")}</button></div>
          <dl class="env-list">
            <dt><code>GRID_UI_URL</code></dt><dd>The Grid UI as checks reach it. Default <code>http://grid_ui:4444</code>.</dd>
            <dt><code>GRID_MFA_TOKEN</code></dt><dd>A client token from Access. Default <code>GATEWAY_CLIENT_TOKEN</code>, which checks already have.</dd>
          </dl>
          <p class="hint">Codes come from this server's clock: keep it in sync with the site's (NTP).</p>
        </section>
      </div>
      <div id="mfa-empty"></div>
    </section>
    <section class="mfa-part signin-part" id="signins" aria-labelledby="signin-title" hidden>
      <header class="part-head">
        <div class="part-title"><h2 id="signin-title" tabindex="-1">Saved sign-ins</h2><span class="panel-sub" id="signin-sub"></span></div>
        <p class="view-sub">The cookies and local storage a check saves once it has signed in, which later runs reuse until the site asks to
          sign in again. Most runs skip the password and the MFA prompt, and the site sees one sign-in rather than one per run: many sign-ins
          from server addresses can get an account challenged or blocked. Encrypted like the seeds; only checks get them back.</p>
      </header>
      <div class="panel table-panel" id="signin-panel" hidden>
        <table class="table signins">
          <caption class="sr-only">Saved sign-ins</caption>
          <thead><tr>
            <th scope="col">Sign-in</th><th scope="col">Saved</th><th scope="col">Expires</th><th scope="col">Runs</th>
            <th scope="col">Asked again</th><th scope="col"><span class="sr-only">Actions</span></th>
          </tr></thead>
          <tbody id="signin-rows"></tbody>
        </table>
      </div>
      <div id="signin-state"></div>
      <details class="panel signin-how" id="signin-how" hidden>
        <summary>${icon("i-chevron")}Use it from a check</summary>
        <div class="signin-how-body" id="signin-how-body"></div>
      </details>
    </section>`);

  const $ = (sel) => section.querySelector(sel);
  const dlg = document.getElementById("add-mfa-dialog");
  const form = document.getElementById("add-mfa-form");
  const field = (id) => document.getElementById(id);
  dismissible(dlg);
  // ok, signedout, unconfigured, error, loading
  // loading: the load in flight (a promise); again: a forced load asked for meanwhile. forbidden: this
  // person's roles don't let them see the secrets (decided before asking), so that part says so.
  const mfa = { state: "loading", data: null, sig: null, error: null, loading: null, again: false, loadedAt: 0, refused: false, forbidden: false };
  // The saved sign-ins, loaded with the secrets: data is null until the page state is "ok".
  const signins = { data: null, sig: null, error: null, forbidden: false };
  // Whose rows are listed (SCOPES), once a person signed in with single sign-on; null until then.
  let scope = null;
  // The code on screen: { id, name, mode: "peek" | "used", code, step, issued, period, endsAt, opensAt, shownAt }.
  let shown = null, seq = 0, pending = 0, codeTimer = null, visible = false;
  // Live sign-in leases: sign-in id -> when the lease runs out, on this tab's monotonic clock.
  let leases = new Map(), leaseTimer = null, wanted;

  // ---- loading ---------------------------------------------------------------
  async function load(force) {
    const s = auth.state();
    if (!s.known) { mfa.state = "loading"; return render(); }
    if (!s.adminConfigured) { mfa.state = "unconfigured"; mfa.data = mfa.sig = null; dropSignins(); hideCode(); return render(); }
    if (!s.signedIn) { mfa.state = "signedout"; mfa.data = mfa.sig = null; dropSignins(); hideCode(); return render(); }
    if (mfa.loading) {
      // The answer on its way may predate what the caller just changed: ask once more when it's in.
      if (force) mfa.again = true;
      return mfa.loading;
    }
    if (!force && Date.now() - mfa.loadedAt < POLL_MS - 1000) return;
    mfa.loading = (async () => {
      do {
        mfa.again = false;
        await fetchList();
      } while (mfa.again && auth.state().signedIn);
    })().finally(() => { mfa.loading = null; });
    return mfa.loading;
  }

  const summary = () => [mfa.state, mfa.sig, mfa.error, mfa.forbidden, signins.sig, signins.error, signins.forbidden].join("|");

  async function fetchList() {
    const before = summary();
    // Both lists in one poll, so the page changes once; only those this person's roles let them see, as
    // asking for another would only be refused.
    const ask = { secrets: auth.can("mfa.use"), signins: auth.can("signins.use") };
    const [secrets, saved] = await Promise.allSettled([ask.secrets && api.get("api/mfa"), ask.signins && api.get("api/signins")]);
    mfa.loadedAt = Date.now();
    mfa.forbidden = !ask.secrets;
    signins.forbidden = !ask.signins;
    // The page's own state comes from the secrets, or from the sign-ins for someone who may see only those.
    const lead = ask.secrets ? secrets : ask.signins ? saved : null, err = lead && lead.status === "rejected" ? lead.reason : null;
    if (err && err.body && err.body.admin_token_unset) mfa.state = "unconfigured";
    else if (err && err.body && err.body.token_required) { mfa.state = "signedout"; mfa.refused = true; }
    else if (err && !(ask.secrets ? mfa.data : signins.data)) { mfa.state = "error"; mfa.error = err.message; }
    else { mfa.state = "ok"; mfa.refused = false; mfa.error = ask.secrets && err ? err.message : null; }
    if (!ask.secrets) mfa.data = mfa.sig = null;
    else if (secrets.status === "fulfilled") {
      mfa.data = secrets.value;
      mfa.sig = JSON.stringify(secrets.value, (k, v) => (k === "generated" || k === "serverTime" ? undefined : v));
    }
    // Signed out while the lists were on their way: they don't come back (nor into the palette).
    if (mfa.state === "ok" && !auth.state().signedIn) mfa.state = "signedout";
    if (mfa.state !== "ok") { mfa.data = mfa.sig = null; dropSignins(); }
    else if (!ask.signins) dropSignins();
    else if (saved.status === "fulfilled") takeSignins(saved.value);
    else signins.error = saved.reason.message;
    if (!mfa.data) hideCode();
    if (summary() !== before) render();
  }

  function takeSignins(body) {
    signins.data = body;
    signins.error = null;
    // The lease countdowns run on this tab's clock between polls: leaseForS stays out of the signature.
    signins.sig = JSON.stringify(body, (k, v) => (k === "generated" || k === "serverTime" || k === "leaseForS" ? undefined : v));
    const now = performance.now(), next = new Map();
    for (const s of body.signins || []) {
      if (s.leaseForS == null) continue;
      const was = leases.get(s.id), endsAt = now + s.leaseForS * 1000;
      // The server counts whole seconds: a countdown already running keeps going unless it's out by more.
      next.set(s.id, was != null && Math.abs(was - endsAt) < 1500 ? was : endsAt);
    }
    leases = next;
  }

  function dropSignins() {
    signins.data = signins.sig = signins.error = null;
    leases = new Map();
  }

  // ---- rendering ---------------------------------------------------------------
  // The page's own states, for both parts: nothing to show until an admin is signed in.
  function stateMarkup() {
    switch (mfa.state) {
      case "loading": return html`<div class="empty"><p>Loading…</p></div>`;
      case "unconfigured": return html`<div class="empty">${icon("i-lock", "empty-mark")}
        <h2>Set an admin token to manage MFA secrets and sign-ins</h2>
        <p>MFA secrets and saved sign-ins are managed by an admin, and only admins can see them, their codes and who used them.
          Until the grid has an admin token, this page stays locked and checks get no codes. Saved sign-ins keep working, out of sight.</p>
        <ol class="steps">
          <li class="step"><div class="step-label">Add one to .env next to docker-compose.yaml (or to the Grid UI's environment)</div>${command(ADMIN_COMMAND)}</li>
          <li class="step"><div class="step-label">Then restart the Grid UI and sign in with the same value</div>${command(RESTART)}</li>
        </ol></div>`;
      case "signedout": return html`<div class="empty calm">${icon("i-lock", "empty-mark")}
        <h2>${mfa.refused ? "Sign in again to manage MFA secrets and sign-ins" : "Sign in to manage MFA secrets and sign-ins"}</h2>
        <p>${auth.state().sso ? "Only people whose roles include “Use MFA secrets” can see the MFA secrets, their codes and who was handed them, " +
          "and only those whose roles include “Use saved sign-ins” the sign-ins checks saved." : "Only admins can see the MFA secrets, their codes and " +
          "who was handed them, and the sign-ins checks saved."} Checks keep getting codes and their saved sign-ins with their client tokens meanwhile.</p>
        <button type="button" class="btn primary" data-signin>Sign in</button></div>`;
      case "error": return html`<div class="empty">${icon("i-alert", "empty-mark")}<h2>Can't load the ${mfa.forbidden ? "saved sign-ins" : "MFA secrets"}</h2><p>${mfa.error}</p>
        <button type="button" class="btn" data-reload-mfa>Try again</button></div>`;
    }
    return html``;
  }

  // A part this person's roles don't let them see, in place of its list.
  function forbiddenMarkup(permission) {
    return html`<div class="empty calm">${icon("i-lock", "empty-mark")}<h3>Your roles don't include “${permission}”</h3>
      <p>Ask an admin for a role that does.</p>
      <button type="button" class="btn" data-your-access>Your access</button></div>`;
  }

  function emptyMarkup(d) {
    if (!d.ready) {
      return html`<div class="empty">${icon("i-shield", "empty-mark")}<h3>MFA secrets aren't set up in this database yet</h3>
        <p>It has no mfaSecrets table. The Grid UI creates it on MySQL when its database user may create tables, and so does the gateway hub when it starts.</p></div>`;
    }
    if (d.keyProblem) return html`<div class="empty compact"><p>No MFA secrets yet. Set the secrets key first, as above.</p></div>`;
    // Someone who may use secrets but not add them sees those shared with them, and none are yet.
    if (!auth.can("mfa.add")) {
      return html`<div class="empty">${icon("i-shield", "empty-mark")}<h3>No MFA secrets are shared with you yet</h3>
        <p>Whoever adds one can share it with you, or with a role of yours. Its codes then show here, and your checks get them.</p></div>`;
    }
    return html`<div class="empty">${icon("i-shield", "empty-mark")}<h3>No MFA secrets yet</h3>
      <p>Add the setup key a site shows when you turn on an authenticator app for the account your checks sign in with.
        From then on, checks ask the Grid UI for its codes by name.</p>
      <button type="button" class="btn primary" data-add-mfa>${icon("i-plus")}Add secret</button></div>`;
  }

  // ---- whose rows ---------------------------------------------------------------------
  // The scope choice is for a person signed in with single sign-on; All only for whoever may see every
  // secret or sign-in, and then it's where the page starts.
  function scopes() {
    const s = auth.state();
    if (!s.sso || !s.user) return [];
    return auth.can("mfa.all") || auth.can("signins.all") ? ["mine", "shared", "all"] : ["mine", "shared"];
  }
  const inScope = (rows) => (scope ? rows.filter(SCOPES[scope]) : rows);

  function renderScope() {
    const offered = scopes();
    if (!offered.includes(scope)) scope = offered.length ? offered[offered.length === 3 ? 2 : 0] : null;
    const rows = [].concat((mfa.data && mfa.data.secrets) || [], (signins.data && signins.data.signins) || []);
    $("#mfa-toolbar").hidden = !(mfa.state === "ok" && scope && !(mfa.forbidden && signins.forbidden));
    section.querySelectorAll("#mfa-scope [data-scope]").forEach((b) => {
      const label = b.dataset.label || (b.dataset.label = b.textContent), n = rows.filter(SCOPES[b.dataset.scope]).length;
      b.hidden = !offered.includes(b.dataset.scope);
      setHTML(b, html`${label}${n ? html`<span class="n">${n}</span>` : ""}`);
      b.setAttribute("aria-pressed", String(b.dataset.scope === scope));
    });
  }

  // A scope that leaves out every row of a part: says so, and offers the rows there are.
  function scopeEmpty(what) {
    const to = scopes().includes("all") ? "all" : scope === "mine" ? "shared" : "mine";
    return html`<div class="empty compact"><p>None of the ${what} are ${scope === "mine" ? "yours" : "shared with you"}.</p>
      <button type="button" class="btn small" data-scope-to="${to}">${{ all: "Show all", shared: "Show those shared with you", mine: "Show yours" }[to]}</button></div>`;
  }

  // Whose it is, and who else has it, said once in each row's meta.
  function ownerLine(s, kind) {
    if (!auth.state().sso) return "";
    const o = s.owner, shared = s.sharedWith || [];
    const whose = s.access === "owner" ? "Yours" : !o ? "Admins'" : o.name + "'s" + (o.disabled ? " (disabled)" : "");
    return html`<small class="owner-line">${whose}${s.canManage && shared.length
      ? html` · <span title="${shared.map((g) => g.name).join(", ")}">shared with ${shared.length}</span>` : s.access === "shared" ? " · shared with you" : ""}${
      kind === "signin" && s.usable === false ? html` · <span title="${unusable(s)}">your checks can't use it</span>` : ""}</small>`;
  }

  function shareButton(s, kind) {
    if (!(s.canManage && auth.state().sso)) return "";
    return html`<button type="button" class="btn ghost small" data-share-${kind}="${s.id}" data-fk="share-${s.id}" aria-label="Share ${s.name}">${icon("i-share")}Share</button> `;
  }

  // GRID_SECRETS_KEY unset or too short (or the image lacks the cryptography package): say what to do. The
  // one key encrypts seeds and saved sign-ins alike.
  function keyMarkup(problem, secrets, saved) {
    const pkg = /cryptography/.test(problem);
    const until = secrets && saved ? " Until it's set, the secrets below give no codes and checks can't use the saved sign-ins."
      : secrets ? " Until it's set, the secrets below give no codes." : saved ? " Until it's set, checks can't use the saved sign-ins below." : "";
    return html`<div class="empty">${icon("i-lock", "empty-mark")}
      <h2>${pkg ? "The Grid UI can't encrypt MFA secrets or saved sign-ins yet" : "Set a secrets key to store MFA secrets and saved sign-ins"}</h2>
      <p>${/^Set /.test(problem) ? "" : problem + " "}The Grid UI encrypts every seed and saved sign-in with a key derived from
        <code class="mono">GRID_SECRETS_KEY</code>, so the database alone never holds a usable seed or cookie.${until}</p>
      <ol class="steps">${pkg
        ? html`<li class="step"><div class="step-label">Rebuild the Grid UI image (ui/requirements.txt lists cryptography) and restart it</div>${command(REBUILD)}</li>`
        : html`<li class="step"><div class="step-label">Add one to .env next to docker-compose.yaml (or to the Grid UI's environment), at least 16 characters</div>${command(KEY_COMMAND)}</li>
          <li class="step"><div class="step-label">Then restart the Grid UI</div>${command(RESTART)}</li>`}
      </ol>
      ${pkg ? "" : html`<p class="hint">Keep the key with your other secrets: seeds and sign-ins stored under one key don't decrypt under another.</p>`}</div>`;
  }

  function lastCell(s) {
    if (!s.lastIssuedAt) return html`<span class="dim">Never</span>`;
    return html`<span><span data-ago="${s.lastIssuedAt}"></span> <span class="dim">to</span> <span class="issued-to">${s.lastIssuedTo || "–"}</span></span>`;
  }

  function secretRow(s) {
    const on = !!(shown && shown.id === s.id), meta = who(s), locked = !!(mfa.data && mfa.data.keyProblem);
    return html`<tr data-key="${s.id}" data-mfa="${s.id}" data-jk tabindex="-1" class="${on ? "showing" : ""}">
      <td class="cell-secret"><code class="mfa-name mono">${s.name}</code>${meta ? html`<small>${meta}</small>` : html`<small class="dim">No issuer or account</small>`}${ownerLine(s, "mfa")}</td>
      <td class="cell-settings" data-label="Settings">${settings(s)}</td>
      <td class="cell-last" data-label="Last handed out" title="${fullDate(parseTime(s.lastIssuedAt))}">${lastCell(s)}</td>
      <td class="cell-count num-col" data-label="Handed out">${s.issuedCount}</td>
      <td class="cell-actions">
        <button type="button" class="btn small" data-show-code="${s.id}" data-fk="show-${s.id}" aria-pressed="${String(on)}" aria-controls="mfa-live"${locked ? html` disabled title="Set GRID_SECRETS_KEY to see codes"` : ""}>${icon(on ? "i-eye-off" : "i-eye")}${on ? "Hide code" : "Show code"}</button>
        ${shareButton(s, "mfa")}${s.canManage !== false ? html`<button type="button" class="btn ghost small destructive" data-delete-mfa="${s.id}" data-fk="delete-${s.id}" aria-label="Delete ${s.name}">Delete</button>` : ""}</td></tr>`;
  }

  function issueRow(i) {
    return html`<tr data-key="${i.id}">
      <td class="cell-when" title="${fullDate(parseTime(i.issuedAt))}"><span data-ago="${i.issuedAt}"></span></td>
      <td class="cell-secret" data-label="Secret"><code class="mono">${i.name}</code></td>
      <td class="cell-to" data-label="Handed to">${i.issuedTo}</td>
      <td class="cell-label" data-label="Label">${i.label ? i.label : html`<span class="dim">–</span>`}</td></tr>`;
  }

  function renderSnippet(d) {
    const pick = (shown && d.secrets.find((s) => s.id === shown.id)) || inScope(d.secrets)[0] || d.secrets[0];
    const text = snippet(pick ? pick.name : "intranet");
    setText($("#mfa-snippet"), text);
    $("#mfa-snippet-copy").dataset.copy = text;
  }

  function render() {
    const ok = mfa.state === "ok", d = ok ? mfa.data : null, sd = ok ? signins.data : null;
    const ready = !!(d && d.ready), list = !!(ready && d.secrets.length);
    renderScope();
    const rows = list ? inScope(d.secrets) : [];
    $("#mfa-secrets").hidden = !ok;
    $("#add-mfa-btn").hidden = !(list && !d.keyProblem && auth.can("mfa.add"));
    $("#mfa-panel").hidden = !rows.length;
    $("#mfa-lower").hidden = !list;
    setHTML($("#mfa-state"), stateMarkup());
    const problem = (ready && d.keyProblem) || (sd && sd.ready && sd.keyProblem) || null;
    setHTML($("#mfa-key"), problem ? keyMarkup(problem, list, !!(sd && sd.signins.length)) : html``);
    setHTML($("#mfa-empty"), !ok ? html`` : mfa.forbidden ? forbiddenMarkup("Use MFA secrets") : !list ? emptyMarkup(d)
      : !rows.length ? scopeEmpty("MFA secrets") : html``);
    // Rows of a list that's gone go too, or focus after the last delete would land on a hidden one.
    patchList($("#mfa-rows"), rows, (s) => s.id, secretRow);
    if (list) {
      $("#mfa-issues-empty").hidden = d.issues.length > 0;
      setText($("#mfa-log-sub"), d.issues.length ? plural(d.issues.length, "code") + (d.issues.length >= 100 ? ", the newest" : "") + " · last 7 days" : "");
      patchList($("#mfa-issue-rows"), d.issues, (i) => i.id, issueRow);
      renderSnippet(d);
    }
    if (shown) {
      const s = d && d.secrets.find((x) => x.id === shown.id);
      if (!s) hideCode();
      // A check took this window's code since we looked: a site would refuse it now.
      else if (shown.mode === "peek" && !shown.issued && s.lastStep != null && s.lastStep >= shown.step) { shown.issued = true; renderCode(); }
    }
    renderSignins();
    const banner = $("#mfa-banner");
    const stale = [mfa.error && "MFA secrets", signins.error && signins.data && "saved sign-ins"].filter(Boolean);
    banner.hidden = !(ok && stale.length);
    if (!banner.hidden) {
      setHTML(banner, html`<div><strong>Can't refresh the ${stale.join(" and ")}.</strong> Showing the last ${stale.length > 1 ? "lists" : "list"}. Retrying.<code class="mono">${mfa.error || signins.error}</code></div>`);
    }
    if (app.tick) app.tick();
  }

  // ---- saved sign-ins ----------------------------------------------------------------
  const findSignin = (id) => ((signins.data && signins.data.signins) || []).find((s) => String(s.id) === String(id)) || null;
  const leased = (s) => s.leaseForS != null;

  // The snippet's names: for reuse(), a sign-in's, else an MFA secret's (a check often names both alike); for
  // mfa.code(), a secret's, the one named like the sign-in if there is one. With no secret, the sign-in's name
  // stands in, the name to give the secret.
  function exampleNames() {
    const saved = (signins.data && signins.data.signins) || [], secrets = (mfa.data && mfa.data.secrets) || [];
    const name = (saved[0] || secrets[0] || { name: "intranet" }).name;
    return [name, (secrets.find((s) => s.name === name) || secrets[0] || { name }).name];
  }

  function howMarkup() {
    const text = reuseSnippet(...exampleNames());
    return html`<div class="signin-how-grid">
      <ol class="signin-steps">
        <li><span><strong>The first run signs in,</strong> password and MFA code, and saves the browser's cookies and local storage here, encrypted.</span></li>
        <li><span><strong>Later runs reuse them</strong> and open the app already signed in. Each run saves them again as the site rolls its
          cookies on, and a state left unused for 30 days expires.</span></li>
        <li><span><strong>When the site asks again,</strong> one run signs in and saves the new state. Checks that start meanwhile wait for it
          rather than sign in too.</span></li>
      </ol>
      <div class="signin-code">
        <div class="snippet"><code class="mono">${text}</code>
          <button type="button" class="icon-btn tiny" data-copy="${text}" data-copy-label="the snippet" aria-label="Copy the snippet">${icon("i-copy")}</button></div>
        <p class="hint">Settings as for <span class="mono">mfa.code()</span>: <code class="mono">GRID_UI_URL</code> and <code class="mono">GRID_MFA_TOKEN</code>.
          A sign-in's first checkout adds its name here.</p>
      </div></div>`;
  }

  // Why a state that was saved isn't any more: the site refused it, it expired, or an admin forgot it.
  function dropped(s) {
    const saved = parseTime(s.savedAt), rejected = parseTime(s.lastRejectedAt), expires = parseTime(s.expiresAt);
    const now = signins.data && signins.data.serverTime ? signins.data.serverTime * 1000 : Date.now();
    if (rejected != null && (saved == null || rejected >= saved)) return "The site asked to sign in again";
    if (expires != null && expires <= now) return "Expired";
    return "Forgotten";
  }

  function domainsMarkup(s) {
    if (!s.domains.length) return html`<small class="dim">${s.version ? "No cookies" : "No domains yet"}</small>`;
    const more = s.domains.length - 3;
    return html`<small class="signin-domains" title="${s.domains.join(", ")}">${s.domains.slice(0, 3).join(", ")}${more > 0 ? html` <span class="more">+${more}</span>` : ""}</small>`;
  }

  function savedCell(s) {
    if (leased(s)) {
      return html`<div class="stack"><span class="tag live"><span class="spinner" aria-hidden="true"></span>Signing in now</span>
        <small><span class="who">${s.leaseHolder || "A check"}</span> · <span data-lease-left="${s.id}" role="timer"></span></small></div>`;
    }
    const by = html`<span class="dim">by</span> <span class="who">${s.savedBy || "–"}</span>`;
    if (s.saved && s.expired) return html`<div class="stack"><span class="tag note">Expired</span><small>Saved <span data-ago="${s.savedAt}"></span> ${by}</small></div>`;
    if (s.saved) {
      return html`<div class="stack"><span><span data-ago="${s.savedAt}"></span> ${by}</span>
        <small>${plural(s.cookieCount, "cookie")} · ${plural(s.originCount, "origin")}</small></div>`;
    }
    if (!s.version) return html`<div class="stack"><span class="dim">Not saved yet</span><small>The next run signs in</small></div>`;
    return html`<div class="stack"><span class="dim">Not saved</span><small>${dropped(s)}</small></div>`;
  }

  function signinRow(s) {
    const live = leased(s), forgettable = s.saved || live, manage = s.canManage !== false;
    return html`<tr data-key="${s.id}" data-signin-row="${s.id}" data-jk tabindex="-1">
      <td class="cell-signin"><code class="signin-name mono">${s.name}</code>${domainsMarkup(s)}${ownerLine(s, "signin")}</td>
      <td class="cell-saved" data-label="Saved" title="${s.saved ? "Saved " + fullDate(parseTime(s.savedAt)) : ""}">${savedCell(s)}</td>
      <td class="cell-expires" data-label="Expires" title="${s.saved ? fullDate(parseTime(s.expiresAt)) : ""}">${s.saved && s.expiresAt
        ? html`<span data-until="${s.expiresAt}"></span>` : html`<span class="dim">–</span>`}</td>
      <td class="cell-runs" data-label="Runs" title="${s.lastUsedAt ? "Last reused " + fullDate(parseTime(s.lastUsedAt)) : ""}"><div class="stack">
        <span class="count">Reused ${plural(s.useCount, "time")}</span>
        ${s.lastUsedAt ? html`<small>Last <span data-ago="${s.lastUsedAt}"></span> by <span class="who">${s.lastUsedBy || "–"}</span></small>` : ""}
        <small>Signed in ${plural(s.signInCount, "time")} · refreshed ${s.refreshCount}</small></div></td>
      <td class="cell-asked" data-label="Asked again" title="${fullDate(parseTime(s.lastRejectedAt))}">${s.rejectedCount
        ? html`<div class="stack"><span class="count">${plural(s.rejectedCount, "time")}</span><small>Last <span data-ago="${s.lastRejectedAt}"></span></small></div>`
        : html`<span class="dim">Never</span>`}</td>
      <td class="cell-actions">${manage ? html`
        <button type="button" class="btn small" data-forget-signin="${s.id}" data-fk="forget-${s.id}" aria-label="Forget ${s.name}"${forgettable ? "" : html` disabled title="Nothing saved to forget"`}>Forget</button>
        ${shareButton(s, "signin")}<button type="button" class="btn ghost small destructive" data-delete-signin="${s.id}" data-fk="delete-${s.id}" aria-label="Delete ${s.name}">Delete</button>` : ""}</td></tr>`;
  }

  function signinStateMarkup(d) {
    if (!d) {
      return signins.error ? html`<div class="empty">${icon("i-alert", "empty-mark")}<h3>Can't load the saved sign-ins</h3><p>${signins.error}</p>
        <button type="button" class="btn" data-reload-mfa>Try again</button></div>` : html``;
    }
    if (!d.ready) {
      return html`<div class="empty">${icon("i-login", "empty-mark")}<h3>Saved sign-ins aren't set up in this database yet</h3>
        <p>It has no savedSignins table. The Grid UI creates it on MySQL when its database user may create tables, and so does the gateway hub when it starts.</p></div>`;
    }
    if (d.signins.length) return inScope(d.signins).length ? html`` : scopeEmpty("saved sign-ins");
    return html`<div class="panel signin-intro">
      <div class="signin-intro-head"><span class="reveal-mark">${icon("i-login")}</span>
        <div><h3>No saved sign-ins yet</h3><p>A check adds its sign-in the first time it signs in through <span class="mono">signin.reuse()</span>.</p></div></div>
      ${howMarkup()}</div>`;
  }

  function renderSignins() {
    const d = mfa.state === "ok" ? signins.data : null, list = !!(d && d.ready && d.signins.length), rows = list ? inScope(d.signins) : [];
    $("#signins").hidden = mfa.state !== "ok";
    $("#signin-panel").hidden = !rows.length;
    $("#signin-how").hidden = !list;
    setHTML($("#signin-state"), mfa.state !== "ok" ? html`` : signins.forbidden ? forbiddenMarkup("Use saved sign-ins") : signinStateMarkup(d));
    patchList($("#signin-rows"), rows, (s) => s.id, signinRow);
    if (list) {
      const usable = d.signins.filter((s) => s.saved && !s.expired).length, live = d.signins.filter(leased).length;
      setText($("#signin-sub"), plural(d.signins.length, "sign-in") + " · " + usable + " saved" + (live ? " · " + live + " signing in now" : ""));
      setHTML($("#signin-how-body"), howMarkup());
    } else {
      setText($("#signin-sub"), "");
      setHTML($("#signin-how-body"), html``);
    }
    paintLeases();
    syncLeaseTimer();
  }

  // Once a second: each lease's countdown, by the server's numbers and this tab's monotonic clock.
  function paintLeases() {
    const now = performance.now();
    let over = false;
    section.querySelectorAll("[data-lease-left]").forEach((el) => {
      const endsAt = leases.get(Number(el.dataset.leaseLeft));
      if (endsAt == null) return;
      const left = Math.ceil((endsAt - now) / 1000);
      setText(el, left > 0 ? duration(left) + " left" : "running out");
      if (left <= 0) over = true;
    });
    // Saved or run out by now: look rather than wait for the poll (paused, the poll brings it on resuming).
    if (over && visible && !document.hidden && !(app.live && app.live.paused) && Date.now() - mfa.loadedAt > 2000) load(true);
  }

  function syncLeaseTimer() {
    const want = visible && leases.size > 0;
    if (want && !leaseTimer) leaseTimer = setInterval(paintLeases, 1000);
    if (!want && leaseTimer) { clearInterval(leaseTimer); leaseTimer = null; }
  }

  function signinFacts(s) {
    const facts = [["Domains", s.domains.join(", ") || "–"],
      ["Runs", "Reused " + plural(s.useCount, "time") + " · signed in " + plural(s.signInCount, "time")]];
    if (leased(s)) facts.push(["Signing in", s.leaseHolder || "A check"]);
    return facts;
  }

  // After a forget or a delete, focus goes back to the row's Forget (the row itself, with nothing left to
  // forget), or to the row that took its place.
  function focusSigninRow(id, at) {
    const rows = Array.from(section.querySelectorAll("#signin-rows tr"));
    const row = rows.find((r) => r.dataset.signinRow === String(id)) || rows[Math.min(Math.max(at, 0), rows.length - 1)];
    if (!row) return $("#signin-title").focus();
    const forget = row.querySelector("[data-forget-signin]");
    (forget && !forget.disabled ? forget : row).focus();
  }

  // After the share dialog closes, focus goes back to the row's Share (the row itself, if it can't be
  // shared any more), or to the part's title when the row has left the list.
  function focusShared(kind, id) {
    const row = kind === "mfa" ? section.querySelector('#mfa-rows [data-mfa="' + id + '"]') : section.querySelector('#signin-rows [data-signin-row="' + id + '"]');
    const btn = row && row.querySelector("[data-share-" + kind + "]");
    (btn || row || $(kind === "mfa" ? "#mfa-h1" : "#signin-title")).focus();
  }

  function share(kind, id) {
    const item = kind === "mfa" ? findSecret(id) : findSignin(id);
    if (!item) return;
    openShare({ kind, item, onDone: async () => { await load(true); focusShared(kind, item.id); } });
  }

  // The sign-in (or, for null, the part) the palette asked for: shown once the view is open and its list in.
  function reveal() {
    if (wanted === undefined || !visible) return;
    // A sign-in the scope leaves out comes into it.
    const s = wanted != null && findSignin(wanted);
    if (s && scope && !SCOPES[scope](s)) {
      scope = scopes().includes("all") ? "all" : s.access === "owner" ? "mine" : "shared";
      render();
    }
    const row = wanted != null && section.querySelector('#signin-rows [data-signin-row="' + wanted + '"]');
    const target = row || $("#signin-title");
    wanted = undefined;
    if (!target.offsetParent) return;
    target.scrollIntoView({ block: row ? "center" : "start" });
    target.focus({ preventScroll: true });
  }

  async function confirmForget(id, origin) {
    const s = findSignin(id);
    if (!s) return;
    const ok = await confirm({
      title: "Forget “" + s.name + "”?",
      text: (leased(s) ? "A check is signing in to it now: its sign-in won't be saved, and it fails when it tries. " : "Its saved cookies are dropped. ") +
        "The next run signs in again, password and MFA code included, and saves a new state. Checks already running carry on.",
      facts: signinFacts(s),
      ok: "Forget sign-in",
      danger: false,
    });
    if (!ok) { if (origin.isConnected) origin.focus(); return; }
    const at = Array.from(section.querySelectorAll("#signin-rows tr")).indexOf(origin.closest("tr"));
    try {
      const res = await api.post("api/signins/" + encodeURIComponent(s.id) + "/forget", {});
      if (res === null) return;
      toast("Forgot “" + s.name + "”. The next run signs in again.");
    } catch (err) {
      toast(err.message, true);
    }
    await load(true);
    focusSigninRow(s.id, at);
  }

  async function confirmDeleteSignin(id, origin) {
    const s = findSignin(id);
    if (!s) return;
    const ok = await confirm({
      title: "Delete “" + s.name + "”?",
      text: "Its saved state and its counts go with it. A check that uses the name again adds it back and signs in, MFA code included." +
        (leased(s) ? " The check signing in to it now fails when it tries to save." : ""),
      facts: signinFacts(s),
      ok: "Delete sign-in",
    });
    if (!ok) { if (origin.isConnected) origin.focus(); return; }
    const at = Array.from(section.querySelectorAll("#signin-rows tr")).indexOf(origin.closest("tr"));
    try {
      const res = await api.post("api/signins/" + encodeURIComponent(s.id) + "/delete", {});
      if (res === null) return;
      toast("Deleted “" + s.name + "”.");
    } catch (err) {
      toast(err.message, true);
    }
    await load(true);
    focusSigninRow(s.id, at);
  }

  // ---- the code on screen -----------------------------------------------------------
  const findSecret = (id) => ((mfa.data && mfa.data.secrets) || []).find((s) => String(s.id) === String(id)) || null;

  function renderCode() {
    const box = $("#mfa-live");
    if (!shown) { setHTML(box, html``); return; }
    const s = findSecret(shown.id) || { name: shown.name };
    const used = shown.mode === "used", meta = who(s);
    const note = used
      ? html`${icon("i-check")}<span>Handed out to you${shown.copied ? " and copied" : ""}. No check gets this code: ${shown.opensAt > performance.now()
        ? "it's the next window's, so type it once the countdown reaches it." : "type it before the countdown runs out."}</span>`
      : shown.issued
        ? html`${icon("i-alert")}<span>This window's code was already handed out: a site refuses it on a second sign-in. <strong>Use code</strong> gives you the next one.</span>`
        : html`<span>Looking doesn't use it up. <strong>Use code</strong> hands it out to you and copies it, so no check gets the same one.</span>`;
    setHTML(box, html`<section class="panel reveal mfa-live${used ? " used" : ""}" aria-labelledby="mfa-live-title">
      <div class="reveal-head"><span class="reveal-mark">${icon("i-shield")}</span>
        <div class="reveal-title"><h3 id="mfa-live-title">${used ? "Your code for " : "Current code for "}<span class="mono">${s.name}</span></h3>
          <p>${meta ? meta + " · " : ""}${s.digits ? s.digits + " digits every " + s.period + " s" : ""}</p></div>
        <button type="button" class="btn ghost small" data-hide-code>Hide</button></div>
      <div class="mfa-code-row">
        <div class="mfa-code mono${shown.issued && !used ? " issued" : ""}" id="mfa-code" aria-label="Code ${shown.code.split("").join(" ")}">${codeMarkup(shown.code)}</div>
        ${shown.issued && !used ? html`<span class="tag note">Already handed out</span>` : ""}
        <div class="mfa-timer"><span class="mfa-left" id="mfa-left" role="timer"></span><span class="mfa-bar" id="mfa-bar" aria-hidden="true"><span class="mfa-bar-fill"></span></span></div>
        <div class="mfa-actions">${used
          ? html`<button type="button" class="btn small" data-copy-code data-fk="mfa-copy">${icon("i-copy")}Copy again</button>`
          : html`<button type="button" class="btn primary" data-use-code data-fk="mfa-use">${icon("i-copy")}Use code</button>`}</div>
      </div>
      <p class="mfa-note${shown.issued && !used ? " warn" : ""}" id="mfa-note">${note}</p>
    </section>`);
    paintCode();
  }

  // Once a second (four times, for a smooth bar): the countdown, by the server's numbers and this tab's
  // monotonic clock, never by comparing wall clocks.
  function paintCode() {
    const left = $("#mfa-left"), bar = $("#mfa-bar");
    if (!shown || !left) return;
    const now = performance.now();
    const waiting = shown.mode === "used" && shown.opensAt > now;
    const secs = Math.max(0, Math.ceil(((waiting ? shown.opensAt : shown.endsAt) - now) / 1000));
    setText(left, waiting ? "Works in " + secs + "s" : secs + "s left");
    const p = waiting ? 0 : Math.max(0, Math.min(1, (shown.endsAt - now) / 1000 / shown.period));
    bar.style.setProperty("--p", p.toFixed(3));
    bar.classList.toggle("low", !waiting && secs <= LOW_S);
    bar.classList.toggle("waiting", waiting);
    const opened = shown.wasWaiting && !waiting;
    shown.wasWaiting = waiting;
    if (opened) renderCode(); // the next window opened: the note changes
  }

  function codeTick() {
    if (!shown) return;
    const now = performance.now(), old = now - shown.shownAt > AUTO_HIDE_MS;
    // A used code may only open after a minute (two windows ahead): it stays until it stops working.
    if (old && shown.mode !== "used") return hideCode();
    if (now >= shown.endsAt) {
      // The window closed: look again (a used code goes back to the current one), unless nobody is looking.
      if (old || document.hidden) return hideCode();
      if (!pending) peek(shown.id, { keepTimer: true });
      return;
    }
    paintCode();
  }

  // A later request (or hiding the code) makes an answer still on its way stale.
  const cancel = () => { seq++; pending = 0; };

  async function peek(id, { keepTimer = false } = {}) {
    const ticket = pending = ++seq;
    try {
      const res = await api.get("api/mfa/" + encodeURIComponent(id) + "/code");
      if (ticket !== seq || !visible || document.hidden) return;
      const now = performance.now();
      const shownAt = keepTimer && shown && shown.id === res.id ? shown.shownAt : now;
      shown = { id: res.id, name: res.name, mode: "peek", code: res.code, step: res.step, issued: !!res.issued, period: res.period,
        endsAt: now + res.secondsLeft * 1000, opensAt: now, shownAt };
      startTimer();
      renderCode();
      renderRowsAndSnippet();
    } catch (err) {
      if (ticket !== seq) return;
      hideCode();
      codeError(err);
    } finally {
      if (pending === ticket) pending = 0;
    }
  }

  async function useCode(btn) {
    if (!shown || btn.disabled) return;
    const id = shown.id;
    btn.disabled = true;
    try {
      const res = await api.post("api/mfa/" + encodeURIComponent(id) + "/use", {});
      // The server has handed it out, whatever happened meanwhile (Hide, a sign-in, another code shown):
      // show it while the view is open, or nobody ever gets it.
      if (res === null || !visible) return;
      const now = performance.now();
      cancel();
      shown = { id: res.id, name: res.name, mode: "used", code: res.code, step: res.step, issued: true, period: res.period,
        opensAt: now + res.waitS * 1000, endsAt: now + res.validForS * 1000, shownAt: now, wasWaiting: res.waitS > 0 };
      shown.copied = await copy(res.code, res.waitS > 0
        ? "the code. It's the next window's: type it in " + Math.ceil(res.waitS) + "s"
        : "the code. Type it within " + Math.floor(res.validForS) + "s");
      renderCode();
      const again = $("#mfa-live [data-copy-code]");
      if (again) again.focus({ preventScroll: true });
      load(true);
    } catch (err) {
      codeError(err);
    } finally {
      btn.disabled = false;
    }
  }

  function codeError(err) {
    if (err.body && err.body.secrets_key_unset) load(true);
    if (err.body && err.body.token_required) load(true);
    toast(err.message, true);
  }

  function startTimer() {
    if (!codeTimer) codeTimer = setInterval(codeTick, 250);
  }

  // Codes leave the page as soon as nobody is looking at them.
  function hideCode() {
    cancel(); // a first look still on its way, too
    if (!shown) return;
    const id = shown.id, focused = $("#mfa-live").contains(document.activeElement);
    shown = null;
    clearInterval(codeTimer);
    codeTimer = null;
    renderCode();
    renderRowsAndSnippet();
    const btn = focused && section.querySelector('[data-show-code="' + id + '"]');
    if (btn) btn.focus({ preventScroll: true });
  }

  function renderRowsAndSnippet() {
    if (mfa.state !== "ok" || !mfa.data || !mfa.data.ready) return;
    patchList($("#mfa-rows"), inScope(mfa.data.secrets), (s) => s.id, secretRow);
    renderSnippet(mfa.data);
    if (app.tick) app.tick();
  }

  function toggleCode(id) {
    if (shown && String(shown.id) === String(id)) return hideCode();
    const s = findSecret(id);
    if (!s) return;
    peek(s.id).then(() => {
      const box = $("#mfa-live").firstElementChild;
      if (!box) return;
      const top = box.getBoundingClientRect().top, bar = document.querySelector(".topbar").getBoundingClientRect().bottom;
      if (top < bar || top > innerHeight - 120) box.scrollIntoView({ block: "nearest" });
    });
  }

  // ---- delete ------------------------------------------------------------------------
  async function confirmDelete(id, origin) {
    const s = findSecret(id);
    if (!s) return;
    const ok = await confirm({
      title: "Delete “" + s.name + "”?",
      text: "Checks that ask for its codes are refused from then on, and its hand-out log goes with it. The seed can't be recovered: " +
        "to use the account again, add its setup key again.",
      facts: [["Account", who(s) || "–"], ["Handed out", plural(s.issuedCount, "code")]],
      ok: "Delete secret",
    });
    if (!ok) { if (origin.isConnected) origin.focus(); return; }
    const rows = Array.from(section.querySelectorAll("#mfa-rows tr"));
    const at = rows.indexOf(origin.closest("tr"));
    try {
      const res = await api.post("api/mfa/" + encodeURIComponent(s.id) + "/delete", {});
      if (res === null) return;
      if (shown && shown.id === s.id) hideCode();
      toast("Deleted “" + s.name + "”.");
    } catch (err) {
      toast(err.message, true);
    }
    await load(true);
    const after = Array.from(section.querySelectorAll("#mfa-rows tr"));
    const next = after[Math.min(Math.max(at, 0), after.length - 1)];
    const target = (next && next.querySelector("button")) || section.querySelector("#mfa-empty [data-add-mfa]") || $("#mfa-h1");
    target.focus();
  }

  // ---- add dialog ----------------------------------------------------------------------
  const INPUTS = ["mfa-name", "mfa-secret", "mfa-issuer", "mfa-account", "mfa-digits", "mfa-period", "mfa-algorithm"];
  function formError(text, fieldId) {
    const el = field("mfa-error");
    el.textContent = text || "";
    el.hidden = !text;
    INPUTS.forEach((id) => field(id).removeAttribute("aria-invalid"));
    if (fieldId) {
      const input = field(fieldId);
      if (input.closest("details")) input.closest("details").open = true;
      input.setAttribute("aria-invalid", "true");
      if (!input.disabled) input.focus();
    }
  }
  function revealSecret(on) {
    const btn = field("mfa-secret-toggle");
    // Dotted by CSS, not type=password: password managers would offer to save the seed as this site's password.
    field("mfa-secret").classList.toggle("masked", !on);
    btn.setAttribute("aria-pressed", String(on));
    btn.setAttribute("aria-label", on ? "Hide the secret" : "Show the secret");
    btn.title = on ? "Hide the secret" : "Show the secret";
    btn.querySelector("use").setAttribute("href", on ? "#i-eye-off" : "#i-eye");
  }
  // An otpauth:// address brings its own settings, issuer and account: the fields say so.
  function syncAdd() {
    const uri = isUri(field("mfa-secret").value);
    field("mfa-settings").disabled = uri;
    setText(field("mfa-settings-hint"), uri ? "Taken from the otpauth:// address." : "Most sites use 6 digits every 30 s with SHA1. Change these only if the site says so.");
    field("mfa-issuer").placeholder = uri ? "From the address" : "e.g. Intranet";
    field("mfa-account").placeholder = uri ? "From the address" : "e.g. checks@example.com";
  }
  function clearSecret() {
    field("mfa-secret").value = "";
    revealSecret(false);
  }
  function openAdd() {
    form.reset();
    clearSecret();
    field("mfa-advanced").open = false;
    formError("");
    syncAdd();
    dlg.showModal();
    field("mfa-name").focus();
  }
  // Which field a server refusal is about, so the right one is marked and focused.
  function fieldFor(message) {
    if (/^There is already a secret called /.test(message)) return "mfa-name";
    if (/name/i.test(message) && !/otpauth|seed/i.test(message)) return "mfa-name";
    if (/issuer/i.test(message)) return "mfa-issuer";
    if (/account/i.test(message)) return "mfa-account";
    if (/^digits/i.test(message)) return isUri(field("mfa-secret").value) ? "mfa-secret" : "mfa-digits";
    if (/^period/i.test(message)) return isUri(field("mfa-secret").value) ? "mfa-secret" : "mfa-period";
    if (/^algorithm/i.test(message)) return isUri(field("mfa-secret").value) ? "mfa-secret" : "mfa-algorithm";
    return "mfa-secret";
  }
  function addBody() {
    const name = field("mfa-name").value.trim(), secret = field("mfa-secret").value;
    if (!MFA_NAME.test(name)) {
      formError("A name is 1 to 64 characters: letters, digits, '.', '_' and '-'. Checks ask for codes by it.", "mfa-name");
      return null;
    }
    if (!secret.trim()) {
      formError("Paste the setup key the site shows, or its otpauth:// address.", "mfa-secret");
      return null;
    }
    const uri = isUri(secret), period = Number(field("mfa-period").value);
    if (!uri && !(Number.isInteger(period) && period >= 10 && period <= 300)) {
      formError("The period is a whole number of seconds from 10 to 300.", "mfa-period");
      return null;
    }
    formError("");
    return {
      name, secret, issuer: field("mfa-issuer").value.trim() || null, account: field("mfa-account").value.trim() || null,
      digits: uri ? null : Number(field("mfa-digits").value), period: uri ? null : period, algorithm: uri ? null : field("mfa-algorithm").value,
    };
  }
  field("mfa-secret").addEventListener("input", syncAdd);
  field("mfa-secret-toggle").addEventListener("click", () => {
    revealSecret(field("mfa-secret").classList.contains("masked"));
    field("mfa-secret").focus();
  });
  // Whichever way the dialog closes, the seed leaves the form.
  dlg.addEventListener("close", clearSecret);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = addBody(), btn = field("mfa-submit");
    if (!body || btn.disabled) return;
    btn.disabled = true;
    try {
      const res = await api.post("api/mfa", body);
      body.secret = null;
      if (res === null) return; // sign-in dismissed: the form stays as it was
      dlg.close();
      toast("Added “" + res.name + "”. Checks can ask for its codes now.");
      await load(true);
      const show = section.querySelector('[data-show-code="' + res.id + '"]');
      if (show) show.focus();
    } catch (err) {
      body.secret = null;
      if (err.body && (err.body.admin_token_unset || err.body.secrets_key_unset)) {
        dlg.close();
        toast(err.message, true);
        load(true);
        return;
      }
      formError(err.message, err.status === 400 ? fieldFor(err.message) : null);
    } finally {
      btn.disabled = false;
    }
  });

  // ---- events -----------------------------------------------------------------------
  section.addEventListener("click", async (e) => {
    let el;
    if (e.target.closest("[data-add-mfa]")) return openAdd();
    if ((el = e.target.closest("[data-show-code]"))) return toggleCode(el.dataset.showCode);
    if (e.target.closest("[data-hide-code]")) return hideCode();
    if ((el = e.target.closest("[data-use-code]"))) return useCode(el);
    if ((el = e.target.closest("[data-copy-code]"))) { if (shown) copy(shown.code, "the code", el); return; }
    if ((el = e.target.closest("[data-delete-mfa]"))) return confirmDelete(el.dataset.deleteMfa, el);
    if ((el = e.target.closest("[data-forget-signin]"))) return confirmForget(el.dataset.forgetSignin, el);
    if ((el = e.target.closest("[data-delete-signin]"))) return confirmDeleteSignin(el.dataset.deleteSignin, el);
    if ((el = e.target.closest("[data-share-mfa]"))) return share("mfa", el.dataset.shareMfa);
    if ((el = e.target.closest("[data-share-signin]"))) return share("signin", el.dataset.shareSignin);
    if ((el = e.target.closest("[data-scope], [data-scope-to]"))) {
      scope = el.dataset.scope || el.dataset.scopeTo;
      render();
      if (el.dataset.scopeTo) section.querySelector('#mfa-scope [data-scope="' + scope + '"]').focus();
      return;
    }
    if (e.target.closest("[data-signin]")) {
      if (await auth.signIn("Sign in to manage MFA secrets and saved sign-ins")) load(true);
      return;
    }
    if (e.target.closest("[data-reload-mfa]")) return load(true);
  });

  document.addEventListener("visibilitychange", () => {
    // A used code stays: the admin is likely off typing it into the site's tab.
    if (document.hidden) { if (!shown || shown.mode !== "used") hideCode(); }
    else if (visible) load(false);
  });
  // Hand-outs and sign-ins change as checks run: look again while the view is open (and live updates aren't paused).
  setInterval(() => {
    if (visible && !document.hidden && !(app.live && app.live.paused)) load(false);
  }, POLL_MS);
  auth.onChange((s) => {
    // Signed out from any view: the lists go too, or the palette would keep offering them.
    if (!s.signedIn && (mfa.data || signins.data)) { mfa.state = "signedout"; mfa.data = mfa.sig = null; dropSignins(); hideCode(); }
    if (!s.signedIn) scope = null; // whoever signs in next starts where the page starts
    // So does a part whose role was taken; one given comes with the next load.
    if (mfa.data && !auth.can("mfa.use")) { mfa.forbidden = true; mfa.data = mfa.sig = null; hideCode(); }
    if (signins.data && !auth.can("signins.use")) { signins.forbidden = true; dropSignins(); }
    // What the roles allow (Add secret, the scopes) shows at once, whether or not the lists turn out to be others.
    if (visible) { load(true); render(); }
  });

  return {
    show() { visible = true; Promise.resolve(load(true)).then(reveal); syncLeaseTimer(); },
    hide() { visible = false; wanted = undefined; hideCode(); syncLeaseTimer(); },
    secrets: () => (mfa.state === "ok" && mfa.data ? mfa.data.secrets : []),
    signins: () => (mfa.state === "ok" && signins.data ? signins.data.signins : []),
    // Before the route to #/mfa: the sign-in's row (or the part, for id null) to bring into view.
    showSignin(id) {
      wanted = id;
      if (visible) Promise.resolve(load(false)).then(reveal);
    },
  };
}
