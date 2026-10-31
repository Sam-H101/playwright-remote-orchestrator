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
-- GATEWAY_ALLOW_OPEN_CLIENTS is set), so revoking the last token never lets tokenless callers in.
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
