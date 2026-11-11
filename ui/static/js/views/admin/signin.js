// Admin, Single sign-on: the identity provider settings as the Grid UI reads them, read only (they come from
// its environment), and what's wrong with them. The client secret, the platform's token and the managed identity
// endpoint's header are never sent here.
import { app } from "../../app.js";
import * as api from "../../api.js";
import { html, icon, setHTML } from "../../html.js";
import { fullDate, parseTime } from "../../format.js";
import { loader } from "./common.js";

const CLIENT_METHOD = {
  secret: "A client secret, sent with HTTP Basic when the provider lists client_secret_basic or lists nothing, else in the request body",
  basic: "A client secret, sent with HTTP Basic",
  post: "A client secret, sent in the request body",
  federated: "A token the platform issues (federated): no client secret",
};

export function createSignin(el, ctx) {
  setHTML(el, html`<div class="sso-body" id="sso-body"><div class="empty"><p>Loading…</p></div></div>`);
  const $ = (sel) => el.querySelector(sel);
  let visible = false;

  const unset = html`<span class="dim">Not set</span>`;
  const mono = (v) => (v ? html`<code class="mono">${v}</code>` : unset);

  // Where the platform's token comes from (a file, or a managed identity's endpoint), and the token as it states
  // itself. For a file that is what the identity provider's federated credential must name; for a managed identity
  // the server says what it must name. Each value is the server's text, escaped by html``. Never the token.
  function sourceRows(t) {
    if (t.source !== "managed-identity") {
      return html`
          <dt>Token file</dt><dd>${mono(t.file)}
            <p class="hint">${t.setting === "AZURE_FEDERATED_TOKEN_FILE" ? "From AZURE_FEDERATED_TOKEN_FILE, which Azure workload identity sets in a Kubernetes pod"
              : "GRID_OIDC_FEDERATED_TOKEN_FILE"}</p></dd>`;
    }
    const c = t.credential || {};
    const given = c.subject ? html`<code class="mono">${c.subject}</code>` : html`<span class="dim">No token was given</span>`;
    return html`
          <dt>Managed identity</dt><dd>${t.identity === "system" ? "The endpoint's default (system-assigned, if turned on)"
            : html`User-assigned, client id <code class="mono">${t.identity}</code>`}
            <p class="hint">GRID_OIDC_MANAGED_IDENTITY</p></dd>
          <dt>Identity endpoint</dt><dd>${mono(t.endpoint)}
            ${t.host === "imds" ? html`<p class="hint">The Azure Instance Metadata Service of this VM. Every program and container on this VM can get
              this identity's token, the grid's runners included, unless a firewall rule stops them: see the README.</p>`
              : t.host === "app-service" ? html`<p class="hint">IDENTITY_ENDPOINT, which App Service, Functions and Container Apps set</p>` : ""}</dd>
          <dt>Credential issuer</dt><dd>${mono(c.issuer)}</dd>
          <dt>Credential subject</dt><dd>${given}</dd>
          <dt>Credential audience</dt><dd>${mono(c.audience)}
            <p class="hint">Add a federated credential with exactly these three values (in the portal: scenario Managed Identity, which fills
              them in). The token's own lines below are for comparing: its subject should be the identity's principal id; its issuer and
              audience may be written differently.</p></dd>`;
  }

  function tokenRows(t) {
    const identity = t.source === "managed-identity";
    const none = html`<span class="dim">${t.decoded ? "Not in the token" : identity ? "No token was given" : "The file holds no token"}</span>`;
    const claim = (v) => (v ? html`<code class="mono">${v}</code>` : none);
    const audience = Array.isArray(t.audience) ? t.audience : [];
    return html`${sourceRows(t)}
          <dt>Token issuer</dt><dd>${claim(t.issuer)}</dd>
          <dt>Token subject</dt><dd>${claim(t.subject)}</dd>
          <dt>Token audience</dt><dd>${audience.length ? audience.map((a, i) => html`${i ? ", " : ""}<code class="mono">${a}</code>`) : none}</dd>
          <dt>Token expires</dt><dd>${!t.decoded ? none : !t.expiresAt ? "It doesn't say"
            : html`<span title="${fullDate(parseTime(t.expiresAt))}" data-until="${t.expiresAt}"></span>${t.expired
              ? html` <span class="warn-text">expired</span>` : ""}`}
            <p class="hint">${identity ? "As the token states them. The token itself is never shown."
              : "The identity provider's federated credential must name this issuer, subject and audience exactly. The token itself is never shown."}</p></dd>`;
  }

  function render(d) {
    const session = d.session.hours + " h" + (d.session.idleMinutes ? ", or " + d.session.idleMinutes + " min without use" : "");
    setHTML($("#sso-body"), html`
      ${d.problem ? html`<p class="form-error notice sso-problem" role="alert">${d.problem}</p>` : ""}
      ${d.warning ? html`<p class="hint notice sso-warning" role="status">${d.warning}</p>` : ""}
      <section class="panel sso-facts" aria-label="Single sign-on settings">
        <dl class="facts">
          <dt>Label</dt><dd>${d.label}</dd>
          <dt>Issuer</dt><dd>${mono(d.issuer)}</dd>
          <dt>Client id</dt><dd>${mono(d.clientId)}</dd>
          <dt>Client authentication</dt><dd>${CLIENT_METHOD[d.clientMethod] || unset}
            <p class="hint">GRID_OIDC_CLIENT_AUTH=${d.clientAuth}</p>
            ${d.platformTokenUnused ? html`<p class="hint sso-unused">The platform offers a token (AZURE_FEDERATED_TOKEN_FILE).
              It is used only with GRID_OIDC_CLIENT_AUTH=federated and no client secret.</p>` : ""}</dd>
          ${d.clientToken ? tokenRows(d.clientToken) : ""}
          <dt>Redirect URI</dt><dd>${d.redirectUri ? html`<span class="copyable"><code class="mono">${d.redirectUri}</code><button type="button" class="icon-btn tiny"
            data-copy="${d.redirectUri}" data-copy-label="the redirect URI" aria-label="Copy the redirect URI">${icon("i-copy")}</button></span>
            <p class="hint">Register this exact address with your identity provider</p>` : html`${unset}<p class="hint">Set GRID_UI_PUBLIC_URL, the address people open the Grid UI at.</p>`}</dd>
          <dt>Scopes</dt><dd>${mono(d.scopes.join(" "))}</dd>
          <dt>Groups claims</dt><dd>${mono(d.groupsClaims.join(", "))}</dd>
          <dt>Session</dt><dd>${session}</dd>
          <dt>Overview, activity and sessions</dt><dd>${d.readsOpen ? "Open to anyone who can reach the page" : "Need sign-in"}</dd>
          <dt>Always admins</dt><dd>${d.admins.length ? html`<ul class="plain-list sso-admins">${d.admins.map((a) => html`<li><code class="mono">${a.entry}</code>
            <span class="dim">· ${a.userId != null ? a.name : "hasn't signed in yet"}</span></li>`)}</ul>`
            : html`<span class="dim">Nobody (GRID_OIDC_ADMINS)</span>`}</dd>
          <dt>Admin token</dt><dd>${d.adminToken ? "Set" : "Not set"}</dd>
          <dt>Provider sign-out</dt><dd>${d.endSession ? "On" : "Off"}</dd>
        </dl>
      </section>
      <p class="hint">These come from the Grid UI's environment: change them there and restart it.</p>`);
    if (app.tick) app.tick(); // when the token expires, at once
  }

  const load = loader(() => api.get("api/admin/sign-in"), render, ctx, () => visible);

  return {
    show() { visible = true; load(false); },
    hide() { visible = false; },
    load(force) { if (force) load.forget(); return load(force); },
    items: () => [],
  };
}
