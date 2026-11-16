# Single sign-on (OpenID Connect)

The Grid UI can sign people in through any OpenID Connect identity provider: Entra ID, Google, Okta, Keycloak or any other that follows the standard. People then sign in as themselves and do what their [roles](../grid-ui/users-and-roles.md) allow, and `GRID_ADMIN_TOKEN` stays as the break-glass way in. How sign-in behaves once it is on is in [Signing in](../grid-ui/signing-in.md).

## Settings

| Variable | Needed | Is |
| --- | --- | --- |
| `GRID_OIDC_ISSUER` | yes | The issuer exactly as its ID tokens state it. Setting it turns single sign-on on, and grid actions then need sign-in, even while the other settings are wrong. Multi-tenant Entra addresses (`common`, `organizations`, `consumers`) are refused |
| `GRID_OIDC_CLIENT_ID` | yes | The UI's client id at the provider |
| `GRID_OIDC_CLIENT_SECRET` | unless federated | The client secret; not allowed with `GRID_OIDC_CLIENT_AUTH=federated` (see [Without a client secret](secretless.md)) |
| `GRID_UI_PUBLIC_URL` | yes | The address people open the UI at, e.g. `https://grid.example.com/`. Register `<it>auth/callback` as the redirect URI. Must be https unless its host is loopback (or `GRID_OIDC_ALLOW_HTTP=1`) |
| `GRID_OIDC_ADMINS` | for the first admin | `sub:<subject>` or a verified email address, comma-separated |

The rest (`GRID_OIDC_LABEL`, `GRID_OIDC_SCOPES`, `GRID_OIDC_GROUPS_CLAIM`, `GRID_OIDC_CLIENT_AUTH`, `GRID_OIDC_FEDERATED_TOKEN_FILE`, `GRID_OIDC_MANAGED_IDENTITY`, `GRID_OIDC_END_SESSION`, `GRID_OIDC_CA_FILE`, `GRID_OIDC_ALLOW_HTTP`, `GRID_OIDC_CLOCK_SKEW_S`, `GRID_SESSION_HOURS`, `GRID_SESSION_IDLE_MINUTES`) are in [Configuration](../reference/configuration.md#grid-ui).

## Identity provider recipes {#recipes}

Register the Grid UI with the provider as a confidential web application, and the redirect URI `<GRID_UI_PUBLIC_URL>auth/callback` (for `GRID_UI_PUBLIC_URL=https://grid.example.com/`, `https://grid.example.com/auth/callback`). Set `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_OIDC_CLIENT_SECRET` (or, with Entra ID, none: see [Without a client secret](secretless.md)), `GRID_UI_PUBLIC_URL` and `GRID_OIDC_ADMINS`, then follow your provider's recipe.

### Entra ID {#entra-id}

- `GRID_OIDC_ISSUER=https://login.microsoftonline.com/<tenant id>/v2.0`: your tenant's, since the multi-tenant `common`, `organizations` and `consumers` addresses are refused.
- In the app registration, a **Web** redirect URI and a client secret, or a federated credential and no secret at all ([Without a client secret](secretless.md)).
- Under **Token configuration**, add the groups claim (security groups, as object ids) and map those ids; or define app roles, assign them and set `GRID_OIDC_GROUPS_CLAIM=roles`. Past 200 groups Entra leaves them out of the ID token, and the Grid UI then counts none of that person's groups (their sheet on the Admin page says so): use app roles, or limit the claim to the groups assigned to the application.
- Turn on **Assignment required** on the enterprise application to limit who signs in, and list subjects in `GRID_OIDC_ADMINS`, not email addresses.
- Provider sign-out (`GRID_OIDC_END_SESSION=1`) works: add `GRID_UI_PUBLIC_URL` itself as a redirect URI too, so Entra sends people back to it.

### Google

- `GRID_OIDC_ISSUER=https://accounts.google.com`, an OAuth client of type **Web application**, and an **Internal** consent screen so only your organization signs in.
- Google sends no groups: give roles by hand, or to everyone with the `*` mapping.
- Leave `GRID_OIDC_END_SESSION` unset: Google has no end-session endpoint.

### Okta

- `GRID_OIDC_ISSUER=https://<org>.okta.com` (the org authorization server) or `https://<org>.okta.com/oauth2/<server id>` (a custom one), and an OIDC **Web Application** assigned to the people and groups who may sign in.
- For groups, add a groups claim filter for the ID token, and `GRID_OIDC_SCOPES=groups` where the claim needs the scope.
- If the application's client authentication is **Client secret post**, set `GRID_OIDC_CLIENT_AUTH=post`.
- Leave `GRID_OIDC_END_SESSION` unset: Okta's end-session endpoint needs an ID token hint, which the Grid UI doesn't keep.

### Keycloak

- `GRID_OIDC_ISSUER=https://<host>/realms/<realm>`, and an OpenID Connect client with **Client authentication** on.
- For groups, a **Group Membership** mapper with Token Claim Name `groups`, **Full group path** off and **Add to ID token** on.
- For realm roles instead, a **User Realm Role** mapper with Token Claim Name `roles`, **Multivalued** and **Add to ID token** on, and `GRID_OIDC_GROUPS_CLAIM=roles` (only top-level claims are read, so the default `realm_access.roles` isn't).
- Provider sign-out works on Keycloak 18 and later, which asks people to confirm, since no ID token hint is sent: add `GRID_UI_PUBLIC_URL` to **Valid post logout redirect URIs**.
- The Grid UI fetches the issuer's discovery document and keys itself, so the issuer's host must resolve from the `grid_ui` container to the same Keycloak the browser reaches: in compose, set `KC_HOSTNAME` to a name both use, and give `grid_ui` an `extra_hosts` entry or a network alias for it.

### A private CA

For a provider whose certificate a private CA issued, mount the CA certificates (PEM) into the container (`docker-compose.yaml` has a commented example) and point `GRID_OIDC_CA_FILE` at them; they are added to the system's.

## Checking the setup

Sign in with the admin token (or as someone who may see people and roles) and open **Admin**, **Single sign-on**: it shows the settings the Grid UI read and what is wrong with them. Problems are also in the Grid UI's log. See [Troubleshooting](troubleshooting.md).
