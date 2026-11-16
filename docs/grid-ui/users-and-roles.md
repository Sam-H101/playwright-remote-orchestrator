# Users and roles

With single sign-on (see [Signing in](signing-in.md)), everyone the identity provider signs in gets a user on the grid at their first sign-in, and may do what their roles allow. Roles come from the Admin page, from groups the provider says they are in, and from `GRID_OIDC_ADMINS`.

Restrict who may sign in at the provider (assign the application only to the people who need it): anyone it signs in for this client gets a user, with no roles unless a mapping gives them some. Users are never deleted: disable them instead. The admin token, client tokens nobody owns and `GATEWAY_CLIENT_TOKEN` keep every permission.

## Permissions

Each permission brings what it includes:

| Permission | Lets them | Includes |
| --- | --- | --- |
| See the grid (`grid.view`) | Open the overview, activity and sessions when the grid needs sign-in to view them (`GRID_UI_READS`) | |
| Run the grid (`grid.operate`) | Release slots, reset failure counts and forget nodes | See the grid |
| Make client tokens (`tokens.own`) | Make client tokens for their own checks, and see, revoke and delete their own. A check using one acts as them | |
| Add hosts (`tokens.agents`) | Make agent tokens, and see, revoke and delete every agent token. **A host sees everything the checks sent to it do, sign-ins and codes included**: give this only to people you'd trust with those | |
| Every access token (`tokens.all`) | See, revoke and delete everyone's tokens, give a client token nobody owns to a person, and make the grid's first client token or delete its last | Make client tokens, Add hosts |
| Use MFA secrets (`mfa.use`) | See the MFA secrets they own or that are shared with them, show and use their codes, and let their checks get codes | |
| Add MFA secrets (`mfa.add`) | Add MFA secrets (they own them), and share and delete their own | Use MFA secrets |
| Every MFA secret (`mfa.all`) | See, use, share, delete and change the owner of every MFA secret | Add MFA secrets |
| Use saved sign-ins (`signins.use`) | See the saved sign-ins they own or that are shared with them, and let their checks reuse them | |
| Add saved sign-ins (`signins.add`) | Let their checks start saved sign-ins under new names (they own them), and forget, share and delete their own | Use saved sign-ins |
| Every saved sign-in (`signins.all`) | See, forget, share, delete and change the owner of every saved sign-in. Their checks still use only the ones [Saved sign-ins](saved-sign-ins.md#whose-checks-reuse-it) says | Add saved sign-ins |
| See people and roles (`users.view`) | See users, roles, group mappings and the single sign-on settings, and how many MFA secrets, saved sign-ins and tokens someone owns (by name only those the reader may see) | |
| Manage people and roles (`users.manage`) | Disable and enable users, end their sessions, revoke their tokens, give and take roles, make, change and delete custom roles and map groups, but only with permissions they have themselves | See people and roles |
| Read the audit log (`audit.view`) | Read every recorded change. The names of MFA secrets, saved sign-ins and tokens show only to those who may see them; others read as "an MFA secret you may not see", and a check's token as "client token (owner)" | |

`GET /api/permissions` lists every permission and what the built-in roles hold.

## Built-in roles

The built-in roles can't be changed or deleted (**Duplicate** one to make your own):

| Role | Permissions | For |
| --- | --- | --- |
| Admin | All of them | Everything, including people, roles and every secret |
| Operator | Run the grid, Make client tokens, Add hosts, Add MFA secrets, Add saved sign-ins | Running the grid and writing checks. Adding hosts lets them run a machine that sees everything the checks sent to it do, sign-ins and codes included |
| Tester | See the grid, Make client tokens, Add MFA secrets, Add saved sign-ins | Writing checks |
| Viewer | See the grid | Seeing the grid and changing nothing. Matters only when the grid needs sign-in to view it |

## Custom roles

Make them on the Admin page's **Roles** tab, with any of the permissions. A name is 1-64 characters without control or invisible characters, and can't be another role's, a built-in role's or "Admins" (the owner of what nobody owns). Names are compared ignoring case and compatibility forms, so `ＱＡ leads` is taken when `qa leads` is. A change applies at once to everyone with the role, people signed in now included.

## Nobody hands out more than they have

- Giving, taking, changing, mapping or deleting a role needs every permission it brings.
- Changing someone's access (their roles, disabling or enabling them, ending their sessions, revoking their tokens, moving their items) needs every permission they have, counted as if they were enabled: only a full admin enables a disabled admin again.
- Giving a role, or mapping a group to it, is also refused while MFA secrets or saved sign-ins you can't see are shared with it, since a role brings those too.
- Nobody changes their own roles, disables themselves or revokes their own tokens on the Admin page; the admin token, which is nobody, may.
- What the person making a change may do is worked out again as it is made, so a request from someone demoted a moment ago gets what they have now.
- These refusals are 403s, recorded in the audit log as `access.denied`.

## Group mappings

On the **Group mappings** tab, map a value of the ID token's groups claim (`GRID_OIDC_GROUPS_CLAIM`, `groups` by default) to a role: a person has the role for as long as their latest sign-in carried the value.

- Map group ids (an Entra group's object id), not names, which anyone who may create a group could copy.
- `*`, "Everyone who signs in", gives a role to everyone, which is how new people get a default role, but never a role that manages people.
- A change to a mapping applies at once; a change to someone's groups at the provider, at their next sign-in.
- A role someone has through a group can only be taken away by removing the mapping or their membership.

## The first admin: `GRID_OIDC_ADMINS`

`GRID_OIDC_ADMINS` lists people who are always admins, which is how the first admin gets in: `sub:<subject>`, or an email address. An email counts only when the ID token says `email_verified: true`, since otherwise anyone who could put that address on an account at the provider would be an admin. Entra ID doesn't verify email addresses, so list subjects there: each person's is in **Your access** (the account menu), even before they have a role, and on the Admin page.

## The last admin

A change that would leave no enabled user who holds every permission is refused (409): disabling, taking a role, deleting a mapping, and changing or deleting a role. Only users of the current `GRID_OIDC_ISSUER` count (those of another issuer can't sign in), and the admin token doesn't: it is for when nobody can sign in. A grid with no admin yet (being set up) can still be changed.

## Disabling someone

Disabling someone ends their sessions, stops them signing in, and stops their client tokens getting anything from the Grid UI (403 `owner_disabled`). What they own stays theirs, with its shares.

!!! warning "The hub doesn't know about users"
    A disabled person's client tokens still open browser sessions at `/connect` until they are revoked, so the disable dialog offers to revoke them, and the hub turns revoked ones away within `TOKEN_CACHE_S`. Taking away "Make client tokens" doesn't revoke the tokens someone made either: the Admin page counts them and offers to.

## Owners

With single sign-on, MFA secrets, saved sign-ins and access tokens have owners. An MFA secret belongs to whoever added it, a saved sign-in to the owner of the client token whose check first used its name, and a token to whoever made it. What was added with the admin token, by a client token nobody owns or `GATEWAY_CLIENT_TOKEN`, or before single sign-on, is **Admins'**.

## Sharing {#sharing}

**Share** an MFA secret or a saved sign-in with people or roles, and they and their checks may use it: never change, forget, delete or share it on. A share still needs the permission to use what it is, and one with a role covers its members now and later.

A saved sign-in can be shared only with people and roles whose permissions are all within yours, unless you hold "Every saved sign-in" (otherwise 403, recorded as `access.denied`): their checks would run as whatever account its owner's checks saved for it. So only people with "Every saved sign-in" share a sign-in with the Admin role.

The same holds whenever a check uses it: a share reaches only people whose permissions are all within its owner's, unless someone with "Every saved sign-in" made it, and then only for as long as its owner keeps at least the permissions they had when it was made (`itemShares.vouchedFor` records them). So:

- a sign-in shared with Viewer, or with any other role an admin also has, never reaches the admin's checks unless someone with "Every saved sign-in" made it (then it reaches every member, admins included, for as long as its owner keeps the permissions they had then);
- someone given more than its owner after it was shared with them, or with a role that was given more, stops getting it, unless someone with "Every saved sign-in" made the share;
- an admin who shared their own sign-in with the Admin role and is then made a Tester can't put an account of their choosing on the admins' checks: the share stops reaching them.

The Share dialog names the people, and the roles as a whole, it no longer reaches, and says when that is because its owner may now do less than when it was shared. It doesn't name a role's members who have more through another role (admins among them): their own MFA page says their checks can't use it. Someone with "Every saved sign-in" can stop sharing and share it again to make it reach them, which records what its owner may do now.

## Changing an owner

Changing an owner needs "Every MFA secret" or "Every saved sign-in", and gives an item to a person or to Admins. A saved sign-in loses its shares, any sign-in under way and its saved state with its owner: that state is an account the old owner's checks signed in as, and the new owner's checks sign in afresh. An MFA secret keeps its shares. **Move all to…** on a person's sheet does the same for every MFA secret and saved sign-in they own; their tokens stay theirs (revoke them, or have the new owner make their own).

!!! note "After the upgrade to single sign-on"
    Every existing MFA secret and saved sign-in is Admins'. Share each one once with the roles or people whose checks need it, or change its owner.

## Client tokens act as whoever made them {#client-tokens}

A client token acts as whoever made it, with their permissions at the time of each request, narrowed by its [token scope](../concepts/access-tokens.md#token-scopes) when it has one: its checks get the MFA secrets and saved sign-ins that person may use. A token given to a person keeps its scope.

- Legacy tokens, tokens made with the admin token and `GATEWAY_CLIENT_TOKEN` act as an admin and get every MFA secret, and the saved sign-ins that are Admins', or shared with Admin for as long as each one's owner keeps at least the permissions they had when it was shared (see [Sharing](#sharing)).
- The Access page counts the client tokens that act as an admin: **Give to…** each one to the person whose checks use it (only ever that way round: a token someone owns never changes hands).
- Once people use their own tokens, `GATEWAY_CLIENT_TOKEN` can come out of `grid_ui`'s environment, but compose's checks ask the Grid UI with it: give those that use MFA codes or saved sign-ins (`poc_totp_login`, `poc_signin_reuse`) a client token of their own as `GRID_MFA_TOKEN` in `.env` first, or they get 401, and count as wrong tokens towards the guessing limit. Without it, the Access page also asks for "Every access token" to make the grid's first client token or delete its last (below).
- A token whose owner is disabled, or no longer known, gets nothing (403 `owner_disabled`), never an admin's access.

## The grid's first client token, and its last

They need "Every access token" (or `GATEWAY_CLIENT_TOKEN` given to the UI): the hub turns away checks without a client token as soon as one exists, revoked or expired ones included, so making the first or deleting the last changes things for everyone's checks. Revoking is never restricted, so anyone can revoke a leaked token of theirs.

## The audit log {#audit-log}

The audit log (`auditLog`) records sign-ins, sign-outs and refused sign-ins, every change to people, roles, mappings, tokens, MFA secrets, saved sign-ins and the grid, codes shown or used on the page, and the refusals above, with who, from which address and when. It never holds a seed, code, state, token or cookie.

The **Audit log** tab ("Read the audit log") lists it newest first, narrowed by who and by nine categories. Rows older than `GRID_AUDIT_DAYS` (365) are deleted as new ones are added; nothing else changes or removes them, though anyone who can write the database can.

## Personal data

- For each person: the provider's subject, their name, email and whether it is verified, their groups at their latest sign-in, and when they first signed in, last signed in and were last seen.
- For each session: the address and browser (user agent) it signed in from; sessions that ended over 7 days ago are deleted as new ones start.
- Audit rows, which hold names and addresses, are kept `GRID_AUDIT_DAYS`.
- A sign-in on its way through the provider is deleted after 10 minutes.

## Tables {#tables}

Users, roles and their mappings, sessions, sign-ins on their way, owners, shares and the audit log are nine tables in `src/schema.sql`, which the hub, the checks and the Grid UI (on MySQL, when its database user may create tables) run at startup. They are utf8mb4, so names, emails and groups in any language fit.

People's names also go into existing tables (whom a code was handed to, who saved or used a sign-in), so those must be utf8mb4 too, MySQL 8's default. For a database upgraded from MySQL 5.7 as latin1, run `ALTER TABLE <table> CONVERT TO CHARACTER SET utf8mb4;` for `mfaSecrets`, `mfaIssues` and `savedSignins`.

Until the new tables are there, nobody can sign in with single sign-on, and on a database that has users but no `itemOwners` table, client tokens get 503 rather than be taken for an admin's.
