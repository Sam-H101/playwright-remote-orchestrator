# Signing in

Once `GRID_ADMIN_TOKEN` or single sign-on is set up, grid actions (release, reset, forget) and the Access, MFA and Admin pages need someone signed in, with a [role](users-and-roles.md) that allows them.

Without either, the grid actions are open to anyone who can load the page (the header says **Unprotected**), and token, MFA and people management are refused.

| Set up | Reading the grid | Grid actions | Access, MFA and Admin pages |
| --- | --- | --- | --- |
| Neither | open | open to anyone (**Unprotected**) | refused |
| `GRID_ADMIN_TOKEN` only | open, unless `GRID_UI_READS=signed-in` | admin token | admin token |
| Single sign-on (with or without the admin token) | needs "See the grid", unless `GRID_UI_READS=open` | by role | by role; the admin token as break-glass |

`GRID_UI_READS` (`open` or `signed-in`) decides whether the overview, activity, sessions, `/status` (`/api/status`) and `/api/events` need "See the grid". Its default is `signed-in` with single sign-on, else `open`. Any other value means `signed-in` (logged). Without any sign-in set up they are open whatever it says.

## Single sign-on

With `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_UI_PUBLIC_URL` and one way for the Grid UI to prove it is the client set, people sign in through your OpenID Connect identity provider (Entra ID, Google, Okta, Keycloak or any other that follows the standard). The way to prove it is the client is a client secret, or a token the platform issues, so that no secret exists at all: on AKS or any Kubernetes, on App Service, Functions, Container Apps or an Azure VM with a managed identity, or from any token file (see [Without a client secret](../sso/secretless.md)).

- The Grid UI is a confidential client and uses the authorization code flow with PKCE on the server: the browser goes to the provider and back to `<GRID_UI_PUBLIC_URL>auth/callback`, and the Grid UI redeems the code itself.
- Exactly one way of proving it is the client is used; if more than one is set, or the one set can't be used, nobody signs in with single sign-on until that is fixed, the Grid UI says why, and it never tries the other.
- It uses only the ID token, once its signature, issuer, audience, times and nonce check out: the provider's access and refresh tokens are dropped at once, and its userinfo endpoint is never asked.
- Setting `GRID_OIDC_ISSUER` protects the grid at once, even while the rest is wrong: the sign-in sheet then says single sign-on isn't set up correctly, and the Grid UI's log, the Admin page's **Single sign-on** tab and the admin token's holder get what to fix.

Provider setup is in [Single sign-on](../sso/index.md).

### The public address

Sign-in always happens at `GRID_UI_PUBLIC_URL`. Started at any other address (`http://localhost:4444`, say), it is sent there first, because the cookie that carries a sign-in through the provider belongs to one address. Behind a proxy that rewrites the `Host` header, have it pass the header on.

## The admin token {#admin-token}

`GRID_ADMIN_TOKEN` is the break-glass sign-in: it holds every permission, works without the database, and the account chip's **Sign in** asks for it once per browser tab (with single sign-on, under **Use the admin token instead**).

- Signing in with it on the page is recorded in the audit log (`auth.breakglass`, at most once an hour per address and worker), and every change made with it is recorded as the admin token's.
- Whoever has the admin token can create agent tokens, so guard it like `GATEWAY_AGENT_TOKEN`.
- Scripts send it in an `X-Grid-Token` header (see [HTTP API](api.md)).

## Sessions

Signing in with single sign-on sets a session cookie that lasts `GRID_SESSION_HOURS` (12) and ends after `GRID_SESSION_IDLE_MINUTES` (120) unused. An open page keeps polling, so only closed or sleeping tabs go idle. The database keeps only the cookie's sha256.

- **Sign out** ends this browser's session and **Sign out everywhere** every one of yours.
- On the Admin page, people who manage people end those of anyone whose permissions are all within theirs (see [Users and roles](users-and-roles.md)), and disabling someone ends theirs.
- A session that stops working (expired, idle, ended, or its user disabled) stops at the next request, and the page says why.
- So does every session started through another issuer once `GRID_OIDC_ISSUER` changes: nobody signs in through that one any more, and the Admin page no longer counts or lists those sessions.
- What someone may do is worked out again on every request, so a change to their roles applies at once.

## Guessing limits {#guessing}

- After 20 wrong admin tokens (or client tokens, from checks) from one address within 5 minutes, the UI answers 429 to that address until the oldest ages out. Session cookies can't be guessed and never count.
- At most 60 sign-ins may start from one address in 10 minutes (an IPv6 address counts as its /64).
- Both are counted in memory per process, so with gunicorn's two workers the limits are up to twice as high.
- Behind a reverse proxy, set `GRID_UI_PROXY_HOPS`, or everyone shares the proxy's address for both limits (and in sessions and the audit log).
- An answer to a request whose admin token was wrong carries `X-Grid-Token-Refused: 1`, even when a session cookie or open reading let the request through, and the page then forgets the token it held: a tab still holding one from before `GRID_ADMIN_TOKEN` changed would otherwise lock its address out with its polls.

## Adding another sign-in provider

The code keeps sign-in in two places so another provider can be added without touching the views:

- `ui/auth.py`: a `Principal(name, method, user_id, permissions, role_ids, email, scope)`, the `PROVIDERS` list, and the gates every route uses: `permission_required(*perms)`, which needs any one of them (none: anyone signed in), and `admin_required()`, which needs every permission.
- `ui/static/js/auth.js`: state, credentials, the sign-in sheet, sign-out.

A new provider implements `configured()`, `attempted(request)`, `authenticate(request)` (returning a `Principal` or `None`) and `describe()` (`{id, kind, label}`), is appended to `PROVIDERS`, and is taught to `auth.js`, whose sheet knows the kinds `secret` (kept for the tab) and `redirect` (a link to the identity provider).

- A `Principal` that says nothing about permissions holds them all, as every principal did before roles.
- `scope`, the last field, is only ever set for a client token with a [token scope](../concepts/access-tokens.md#token-scopes): a sign-in provider leaves it out.
- Set `guessable = False` on a provider whose wrong credentials can't be guesses (a session token), so they never count towards the limit, and `uses_database = True` on one whose `authenticate` asks the database, so error texts never ask it again.
- `GET /api/session` tells the page who is signed in, which providers to offer and what the request may do, and the account menu shows the provider's label.

A single sign-on setting that is wrong (missing, out of range, a CA file that can't be read) stops people signing in with single sign-on until it is fixed, while grid actions stay protected: the Grid UI's log and the Admin page's **Single sign-on** tab say what is wrong.
