// Admin, Single sign-on: the identity provider settings as the Grid UI reads them, read only (they come from
// its environment), and what's wrong with them. The client secret is never sent here.
import * as api from "../../api.js";
import { html, icon, setHTML } from "../../html.js";
import { loader } from "./common.js";

const CLIENT_AUTH = { auto: "Whichever the provider lists first (auto)", basic: "HTTP Basic (basic)", post: "In the request body (post)" };

export function createSignin(el, ctx) {
  setHTML(el, html`<div class="sso-body" id="sso-body"><div class="empty"><p>Loading…</p></div></div>`);
  const $ = (sel) => el.querySelector(sel);
  let visible = false;

  const unset = html`<span class="dim">Not set</span>`;
  const mono = (v) => (v ? html`<code class="mono">${v}</code>` : unset);

  function render(d) {
    const session = d.session.hours + " h" + (d.session.idleMinutes ? ", or " + d.session.idleMinutes + " min without use" : "");
    setHTML($("#sso-body"), html`
      ${d.problem ? html`<p class="form-error notice sso-problem" role="alert">${d.problem}</p>` : ""}
      <section class="panel sso-facts" aria-label="Single sign-on settings">
        <dl class="facts">
          <dt>Label</dt><dd>${d.label}</dd>
          <dt>Issuer</dt><dd>${mono(d.issuer)}</dd>
          <dt>Client id</dt><dd>${mono(d.clientId)}</dd>
          <dt>Client authentication</dt><dd>${CLIENT_AUTH[d.clientAuth] || d.clientAuth}</dd>
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
  }

  const load = loader(() => api.get("api/admin/sign-in"), render, ctx, () => visible);

  return {
    show() { visible = true; load(false); },
    hide() { visible = false; },
    load(force) { if (force) load.forget(); return load(force); },
    items: () => [],
  };
}
