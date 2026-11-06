-- Orchestrator schema, shared by the Python checks (src/database.py), the gateway hub
-- (gateway/lib/registry.js) and the Grid UI. Every statement is idempotent and is run on start-up.
-- Statements are separated by a line holding only ";" so each one can be sent on its own.
-- All DATETIME columns hold UTC.

-- One row per slot. For a gateway node, PortServer is the node name and PortNumber is the
-- 1-based slot index. For a legacy static host (hosts: in database.yaml), PortServer is the
-- host name and PortNumber is the run-server port. PortStatus is 1 when the slot's
-- run-server is healthy (gateway nodes only; ignored for static hosts).
CREATE TABLE IF NOT EXISTS portStatus (
    id INT AUTO_INCREMENT PRIMARY KEY,
    PortNumber INT NOT NULL,
    PortStatus INT NOT NULL DEFAULT 0,
    PortServer VARCHAR(255) NOT NULL,
    LastUpdateTime DATETIME NULL,
    UNIQUE KEY uq_server_port (PortServer, PortNumber)
)
;
-- A slot is claimed by setting inUse 0 -> 1 with a conditional UPDATE, which also stamps
-- LastRunDate; releasing sets inUse = 0 and stamps it again. Every claimer sets claimedBy in its
-- claim UPDATE: the gateway hub writes 'hub:<hub instance>:<n>' for a /connect session, anything
-- else writes NULL. The hub releases only its own claims (claimedBy and LastRunDate unchanged), and a
-- hub's claims live as long as its row in hubs: the stale-claim sweep of the checks leaves them alone,
-- and a hub frees those of a hub whose lease ran out. The hub adds the column to a portUsage table
-- made before it.
CREATE TABLE IF NOT EXISTS portUsage (
    id INT PRIMARY KEY,
    portNumber INT NOT NULL,
    inUse TINYINT NOT NULL DEFAULT 0,
    LastRunDate DATETIME NULL,
    failCount INT NOT NULL DEFAULT 0,
    claimedBy VARCHAR(64) NULL,
    KEY idx_in_use (inUse),
    CONSTRAINT fk_port_usage_status FOREIGN KEY (id) REFERENCES portStatus (id) ON DELETE CASCADE
)
;
-- Nodes that registered themselves through the gateway hub. A slot whose PortServer has no
-- row here belongs to a legacy static host. Node names are lower-case. An ephemeral node (a
-- container registered under its ID) is removed by the hub once it has been offline for
-- EPHEMERAL_GRACE_S. tokenId is the managed access token (accessTokens.id) the node last
-- registered with; NULL means the GATEWAY_AGENT_TOKEN bootstrap token or no agent auth. The hub
-- adds the ephemeral and tokenId columns to a nodes table made before they existed.
CREATE TABLE IF NOT EXISTS nodes (
    name VARCHAR(255) NOT NULL PRIMARY KEY,
    online TINYINT NOT NULL DEFAULT 0,
    slots INT NOT NULL DEFAULT 0,
    playwrightVersion VARCHAR(32) NULL,
    agentVersion VARCHAR(32) NULL,
    platform VARCHAR(64) NULL,
    browsers VARCHAR(255) NULL,
    remoteAddress VARCHAR(255) NULL,
    ephemeral TINYINT NOT NULL DEFAULT 0,
    registeredAt DATETIME NULL,
    connectedAt DATETIME NULL,
    lastSeen DATETIME NULL,
    tokenId INT NULL
)
;
-- Managed relay access tokens, created and revoked from the Grid UI. kind 'agent' tokens let a
-- node agent dial in (/agent, /agent/data), kind 'client' tokens let a check open sessions
-- (/connect). Only the lower-case hex sha256 of the full token string is stored; prefix (its
-- first 12 characters) is kept for display. An agent token with nodeName set may only register
-- that (lower-case) node name, and once a node has registered with such a token, no other token
-- may take that name until the bound token is revoked or expires. A token is valid while
-- revokedAt is NULL and expiresAt is NULL or still in the future. Any row of a kind, revoked or
-- expired included, makes the hub require a token of that kind (unless GATEWAY_ALLOW_OPEN_AGENTS /
-- GATEWAY_ALLOW_OPEN_CLIENTS is set), so revoking the last token never lets tokenless callers in. The
-- Grid UI records who made a token in itemOwners; the hub ignores that.
CREATE TABLE IF NOT EXISTS accessTokens (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    tokenHash CHAR(64) NOT NULL,
    prefix VARCHAR(16) NOT NULL,
    nodeName VARCHAR(255) NULL,
    createdAt DATETIME NOT NULL,
    expiresAt DATETIME NULL,
    lastUsedAt DATETIME NULL,
    revokedAt DATETIME NULL,
    UNIQUE KEY uq_token_hash (tokenHash),
    KEY idx_kind (kind)
)
;
-- Gateway hubs using this database, one row per running hub process (instance is the <hub instance>
-- of its claims). A hub renews lastSeen every GATEWAY_HUB_LEASE_S / 3 seconds and deletes its row
-- when it stops. A row not renewed for GATEWAY_HUB_LEASE_S belongs to a hub that is gone: the other
-- hubs, or the next one to start, free its claims and delete the row. The Grid UI reads it to tell a
-- long session through a live hub from a claim nobody holds any more.
CREATE TABLE IF NOT EXISTS hubs (
    instance VARCHAR(16) NOT NULL PRIMARY KEY,
    host VARCHAR(255) NULL,
    port INT NULL,
    startedAt DATETIME NULL,
    lastSeen DATETIME NULL
)
;
-- Authenticator-app (TOTP, RFC 6238) enrolments the Grid UI keeps for checks that sign in to sites with
-- two-step verification. Only the Grid UI reads and writes them. seedCipher is the seed encrypted with
-- AES-256-GCM under a key derived from GRID_SECRETS_KEY: base64 of the 12-byte nonce and the ciphertext,
-- with 'mfa:<name>' as associated data, so a ciphertext copied to another row doesn't decrypt. The seed
-- itself is never stored, logged or sent anywhere: checks get codes. lastStep is the newest time step
-- whose code was handed out; each step is handed out once (sites accept each code once), so checks
-- running at the same time never get the same code. Who owns one and who it is shared with is in
-- itemOwners and itemShares.
CREATE TABLE IF NOT EXISTS mfaSecrets (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    issuer VARCHAR(128) NULL,
    account VARCHAR(255) NULL,
    algorithm VARCHAR(8) NOT NULL DEFAULT 'SHA1',
    digits INT NOT NULL DEFAULT 6,
    period INT NOT NULL DEFAULT 30,
    seedCipher VARCHAR(1024) NOT NULL,
    createdAt DATETIME NOT NULL,
    lastStep BIGINT NULL,
    lastIssuedAt DATETIME NULL,
    lastIssuedTo VARCHAR(255) NULL,
    issuedCount INT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_mfa_name (name)
)
;
-- The codes the Grid UI handed out: which secret, which time step, when, to whom (the client token's
-- name, GATEWAY_CLIENT_TOKEN or an admin) and the label the check sent. Rows older than 7 days are
-- deleted as new ones are added. Never holds a code or a seed.
CREATE TABLE IF NOT EXISTS mfaIssues (
    id INT AUTO_INCREMENT PRIMARY KEY,
    secretId INT NOT NULL,
    step BIGINT NOT NULL,
    issuedAt DATETIME NOT NULL,
    issuedTo VARCHAR(255) NOT NULL,
    label VARCHAR(128) NULL,
    KEY idx_mfa_issues_secret (secretId, issuedAt)
)
;
-- Signed-in browser states (Playwright's storage_state: cookies and local storage) the Grid UI keeps for
-- checks, so most runs reuse a sign-in instead of signing in again. Only the Grid UI reads and writes
-- them. stateCipher is the state's JSON, zlib-compressed and encrypted with AES-256-GCM under a key
-- derived from GRID_SECRETS_KEY (not the MFA seeds' key): base64 of the 12-byte nonce and the ciphertext,
-- with 'signin:<name>' as associated data. NULL means nothing usable is saved (never saved, forgotten, or
-- dropped because the site asked to sign in again or expiresAt passed). version goes up with every save
-- and every forget, so a check can tell its state from a newer one; 0 means never saved. A name deleted
-- and checked out again is a new row whose versions start over, under a new id (InnoDB doesn't reuse
-- AUTO_INCREMENT ids since MySQL 8.0), and checks send the id with the version. domains (the
-- cookies' domains, comma separated) and the counts are for display. While a check signs in, it holds
-- the lease: leaseHash is the sha256 of its lease token, and nobody else signs in for that name until
-- it saves, releases it, or leaseUntil passes. Who owns one and who it is shared with is in itemOwners
-- and itemShares; a check uses one only when its token's owner owns it or it is shared with them.
CREATE TABLE IF NOT EXISTS savedSignins (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    stateCipher MEDIUMTEXT NULL,
    version INT NOT NULL DEFAULT 0,
    domains VARCHAR(1024) NULL,
    cookieCount INT NOT NULL DEFAULT 0,
    originCount INT NOT NULL DEFAULT 0,
    savedAt DATETIME NULL,
    savedBy VARCHAR(255) NULL,
    expiresAt DATETIME NULL,
    signInCount INT NOT NULL DEFAULT 0,
    refreshCount INT NOT NULL DEFAULT 0,
    useCount INT NOT NULL DEFAULT 0,
    lastUsedAt DATETIME NULL,
    lastUsedBy VARCHAR(255) NULL,
    rejectedCount INT NOT NULL DEFAULT 0,
    lastRejectedAt DATETIME NULL,
    leaseHash CHAR(64) NULL,
    leaseUntil DATETIME NULL,
    leaseHolder VARCHAR(255) NULL,
    createdAt DATETIME NOT NULL,
    UNIQUE KEY uq_signin_name (name)
)
;
-- People who signed in to the Grid UI through its OpenID Connect identity provider (GRID_OIDC_ISSUER). Only the
-- Grid UI reads and writes them. issuer and subject are the ID token's iss and sub: who they are, compared byte
-- for byte (a sub is unique only within its issuer, so changing GRID_OIDC_ISSUER makes new users). email,
-- emailVerified and name are what the provider said at the last sign-in, for display and for GRID_OIDC_ADMINS
-- (verified emails only). idpGroups is the JSON list (at most 500 values) of the groups claim of that sign-in,
-- which groupRoles maps to roles; groupsOverage is 1 when the provider said there were too many groups to list,
-- and then none count. A disabled user can't sign in, their sessions end, and their client tokens get nothing
-- from the Grid UI (the hub accepts them for /connect until they are revoked). Users are never deleted: owners
-- and the audit log refer to them.
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    issuer VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    subject VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    email VARCHAR(255) NULL,
    emailVerified TINYINT NOT NULL DEFAULT 0,
    name VARCHAR(255) NULL,
    idpGroups MEDIUMTEXT NULL,
    groupsOverage TINYINT NOT NULL DEFAULT 0,
    createdAt DATETIME NOT NULL,
    lastSignInAt DATETIME NULL,
    lastSeenAt DATETIME NULL,
    disabledAt DATETIME NULL,
    disabledBy VARCHAR(255) NULL,
    UNIQUE KEY uq_user_identity (issuer, subject),
    KEY idx_user_email (email)
) DEFAULT CHARSET=utf8mb4
;
-- Custom roles: a name and a set of the Grid UI's fixed permissions (ui/permissions.py), space separated; ones
-- this version doesn't know are ignored. The built-in roles (Admin, Operator, Tester, Viewer) aren't rows: they
-- have fixed negative ids and their permissions are in the code, so nobody can change them.
CREATE TABLE IF NOT EXISTS roles (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    description VARCHAR(255) NULL,
    permissions VARCHAR(1024) NOT NULL DEFAULT '',
    createdAt DATETIME NOT NULL,
    createdBy VARCHAR(255) NULL,
    updatedAt DATETIME NULL,
    updatedBy VARCHAR(255) NULL,
    UNIQUE KEY uq_role_name (name)
) DEFAULT CHARSET=utf8mb4
;
-- Roles an admin gave a user on the Admin page. roleId is a roles.id or a built-in role's negative id. Roles
-- from groupRoles and GRID_OIDC_ADMINS aren't stored: they are worked out on every request.
CREATE TABLE IF NOT EXISTS userRoles (
    userId INT NOT NULL,
    roleId INT NOT NULL,
    grantedAt DATETIME NOT NULL,
    grantedBy VARCHAR(255) NULL,
    PRIMARY KEY (userId, roleId),
    KEY idx_user_roles_role (roleId)
) DEFAULT CHARSET=utf8mb4
;
-- Identity-provider groups (values of the GRID_OIDC_GROUPS_CLAIM claims) whose members get a role, compared
-- exactly with each user's idpGroups; '*' is everyone who signs in. note is the admin's name for the value (an
-- Entra group's object id says nothing by itself).
CREATE TABLE IF NOT EXISTS groupRoles (
    id INT AUTO_INCREMENT PRIMARY KEY,
    groupName VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    roleId INT NOT NULL,
    note VARCHAR(128) NULL,
    createdAt DATETIME NOT NULL,
    createdBy VARCHAR(255) NULL,
    UNIQUE KEY uq_group_role (groupName, roleId)
) DEFAULT CHARSET=utf8mb4
;
-- Browsers signed in to the Grid UI. tokenHash is the lower-case hex sha256 of the session cookie's value, which
-- is never stored. A session works while revokedAt is NULL, expiresAt is in the future, lastSeenAt is within
-- GRID_SESSION_IDLE_MINUTES and its user isn't disabled. lastSeenAt is renewed at most once a minute. Sessions
-- that ended more than 7 days ago are deleted as new ones start. address and userAgent are for the Admin page.
CREATE TABLE IF NOT EXISTS userSessions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    tokenHash CHAR(64) NOT NULL,
    userId INT NOT NULL,
    createdAt DATETIME NOT NULL,
    lastSeenAt DATETIME NOT NULL,
    expiresAt DATETIME NOT NULL,
    revokedAt DATETIME NULL,
    revokedBy VARCHAR(255) NULL,
    address VARCHAR(64) NULL,
    userAgent VARCHAR(255) NULL,
    UNIQUE KEY uq_session_token (tokenHash),
    KEY idx_sessions_user (userId)
) DEFAULT CHARSET=utf8mb4
;
-- Sign-ins on their way through the identity provider, so the callback can land on any Grid UI worker.
-- stateHash is the sha256 of the state sent to the provider, browserHash the sha256 of the grid-login cookie of
-- the browser that started it: the callback finds the row by the state, accepts it only from that browser, and
-- deletes it, so a sign-in can't be finished twice or in another browser. Rows older than 10 minutes are
-- deleted as new ones are added.
CREATE TABLE IF NOT EXISTS oidcLogins (
    id INT AUTO_INCREMENT PRIMARY KEY,
    stateHash CHAR(64) NOT NULL,
    browserHash CHAR(64) NOT NULL,
    nonce VARCHAR(64) NOT NULL,
    codeVerifier VARCHAR(128) NOT NULL,
    returnTo VARCHAR(512) NULL,
    createdAt DATETIME NOT NULL,
    UNIQUE KEY uq_login_state (stateHash),
    KEY idx_login_browser (browserHash)
) DEFAULT CHARSET=utf8mb4
;
-- Who owns an MFA secret (itemKind 'mfa', mfaSecrets.id), a saved sign-in ('signin', savedSignins.id) or an
-- access token ('token', accessTokens.id), as a users.id. No row: it is the admins' (made before users existed,
-- or with GRID_ADMIN_TOKEN, GATEWAY_CLIENT_TOKEN or a token without an owner), and a client token without one
-- acts as an admin. Deleting the item deletes its row.
CREATE TABLE IF NOT EXISTS itemOwners (
    itemKind VARCHAR(8) NOT NULL,
    itemId INT NOT NULL,
    userId INT NOT NULL,
    createdAt DATETIME NOT NULL,
    PRIMARY KEY (itemKind, itemId),
    KEY idx_item_owners_user (userId)
) DEFAULT CHARSET=utf8mb4
;
-- Who else may use an MFA secret or a saved sign-in: one user (granteeKind 'user', a users.id) or everyone with
-- a role ('role', a role id; built-in roles are negative). They get codes and states, never the seed. Deleting
-- the item or the role deletes its rows; forgetting a sign-in keeps them. vouchedFor is set when whoever shared a
-- saved sign-in could see every one: what its owner could do then, as roles.permissions lists permissions (every
-- one for the admins'). Only such a share reaches people who may do something its owner may not, since their
-- checks would run as whatever account the owner's checks saved, and only while the owner may still do all of
-- that. NULL otherwise.
CREATE TABLE IF NOT EXISTS itemShares (
    id INT AUTO_INCREMENT PRIMARY KEY,
    itemKind VARCHAR(8) NOT NULL,
    itemId INT NOT NULL,
    granteeKind VARCHAR(8) NOT NULL,
    granteeId INT NOT NULL,
    sharedAt DATETIME NOT NULL,
    sharedBy VARCHAR(255) NULL,
    vouchedFor VARCHAR(1024) NULL,
    UNIQUE KEY uq_item_share (itemKind, itemId, granteeKind, granteeId),
    KEY idx_item_shares_grantee (granteeKind, granteeId)
) DEFAULT CHARSET=utf8mb4
;
-- What people changed on the Grid UI: sign-ins, users, roles, group mappings, sharing, access tokens, MFA
-- secrets, saved sign-ins and grid actions, written in the same transaction as the change. actorName and
-- targetName are copies, so the log reads the same after renames; actorId is the users.id (NULL for
-- GRID_ADMIN_TOKEN and tokens without an owner). details is a short JSON object and never holds a secret, code,
-- token, cookie or state. Rows older than GRID_AUDIT_DAYS are deleted as new ones are added.
CREATE TABLE IF NOT EXISTS auditLog (
    id INT AUTO_INCREMENT PRIMARY KEY,
    occurredAt DATETIME NOT NULL,
    actorId INT NULL,
    actorName VARCHAR(255) NOT NULL,
    actorMethod VARCHAR(32) NOT NULL,
    address VARCHAR(64) NULL,
    action VARCHAR(48) NOT NULL,
    targetKind VARCHAR(16) NULL,
    targetId INT NULL,
    targetName VARCHAR(255) NULL,
    details TEXT NULL,
    KEY idx_audit_time (occurredAt),
    KEY idx_audit_actor (actorId),
    KEY idx_audit_target (targetKind, targetId)
) DEFAULT CHARSET=utf8mb4
;
