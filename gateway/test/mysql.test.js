'use strict';

// The MySQL registry against a real server. Opt-in, because it DROPS the gateway tables in DB_NAME:
//   GATEWAY_TEST_MYSQL=1 DB_HOST=127.0.0.1 DB_PORT=3307 DB_USER=... DB_PASSWORD=... DB_NAME=<a scratch database> npm test

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { test, before, after, afterEach } = require('node:test');
const WebSocket = require('ws');
const h = require('./helpers');
const { hubConfig } = require('../lib/config');
const { MySqlRegistry } = require('../lib/registry');
const { tokenProblem } = require('../lib/tokens');
const { silent } = require('../lib/log');

const enabled = process.env.GATEWAY_TEST_MYSQL === '1';
const skip = enabled ? false : 'set GATEWAY_TEST_MYSQL=1 and DB_* to run against a scratch MySQL database';
const config = hubConfig({ ...process.env, HUB_DB: 'mysql' });

let admin; // a plain connection for setting up and checking rows behind the registry's back
const registries = [];

// The hub runs init() itself, so a registry handed to it is left uninitialised (a second init leaks a pool).
async function newRegistry({ init = true } = {}) {
  const registry = new MySqlRegistry(config.mysql, { schemaPath: config.schemaPath, log: silent, retries: 2, retryDelayMs: 500 });
  registries.push(registry);
  if (init) await registry.init();
  return registry;
}

// What the Grid UI does: a pwga_/pwgc_ token, stored as the sha256 hex of the exact string.
// A token with a scope is stored as "scoped:" and the first 57 characters of that hash, with its scope beside it.
async function insertToken(kind, { nodeName = null, expires = null, revoked = false, scope = null } = {}) {
  const token = `pwg${kind === 'agent' ? 'a' : 'c'}_${crypto.randomBytes(32).toString('base64url')}`;
  const hash = crypto.createHash('sha256').update(token).digest('hex');
  const stored = scope === null ? hash : `scoped:${hash.slice(0, 57)}`;
  const [res] = await admin.query(
    `INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt, revokedAt${scope === null ? '' : ', scope'})
     VALUES (?, ?, ?, ?, ?, UTC_TIMESTAMP(), ${expires === null ? 'NULL' : 'UTC_TIMESTAMP() + INTERVAL ? SECOND'}, ${revoked ? 'UTC_TIMESTAMP()' : 'NULL'}${scope === null ? '' : ', ?'})`,
    [`${kind} test`, kind, stored, token.slice(0, 12), nodeName, ...(expires === null ? [] : [expires]), ...(scope === null ? [] : [scope])]);
  return { token, hash, stored, id: res.insertId };
}

before(async () => {
  if (!enabled) return;
  admin = await require('mysql2/promise').createConnection({ ...config.mysql, timezone: 'Z', dateStrings: true });
});

after(async () => {
  for (const registry of registries) await registry.close();
  if (admin) await admin.end();
});

afterEach(h.cleanup);

test('init adds tokenId (and ephemeral) to a nodes table made before them, keeps its rows, and is idempotent', { skip }, async () => {
  await admin.query('SET FOREIGN_KEY_CHECKS = 0');
  for (const table of ['portUsage', 'portStatus', 'nodes', 'accessTokens', 'hubs']) await admin.query(`DROP TABLE IF EXISTS ${table}`);
  await admin.query('SET FOREIGN_KEY_CHECKS = 1');
  // nodes as the first gateway build made it.
  await admin.query(`CREATE TABLE nodes (
    name VARCHAR(255) NOT NULL PRIMARY KEY, online TINYINT NOT NULL DEFAULT 0, slots INT NOT NULL DEFAULT 0,
    playwrightVersion VARCHAR(32) NULL, agentVersion VARCHAR(32) NULL, platform VARCHAR(64) NULL, browsers VARCHAR(255) NULL,
    remoteAddress VARCHAR(255) NULL, registeredAt DATETIME NULL, connectedAt DATETIME NULL, lastSeen DATETIME NULL)`);
  await admin.query("INSERT INTO nodes (name, online, slots) VALUES ('old-node', 1, 2)");

  const registry = await newRegistry();
  const columns = async () => (await admin.query(
    `SELECT COLUMN_NAME AS name, IS_NULLABLE AS nullable, DATA_TYPE AS type FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nodes' ORDER BY ORDINAL_POSITION`))[0];
  const cols = await columns();
  assert.deepEqual(cols.slice(-2).map((c) => [c.name, c.nullable, c.type]), [['ephemeral', 'NO', 'tinyint'], ['tokenId', 'YES', 'int']]);
  const [[row]] = await admin.query("SELECT online, slots, ephemeral, tokenId FROM nodes WHERE name = 'old-node'");
  assert.deepEqual(row, { online: 0, slots: 2, ephemeral: 0, tokenId: null }); // init marks every node offline
  const [tables] = await admin.query(
    "SELECT TABLE_NAME AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'accessTokens'");
  assert.equal(tables.length, 1);

  await registry.applySchema(); // a second run changes nothing
  assert.deepEqual(await columns(), cols);
  const [keys] = await admin.query("SHOW INDEX FROM accessTokens WHERE Key_name = 'uq_token_hash'");
  assert.equal(keys[0].Non_unique, 0);
});

test('lookupToken, anyTokens and touchToken on rows inserted with SQL', { skip }, async () => {
  const registry = await newRegistry();
  await admin.query('DELETE FROM accessTokens');
  assert.equal(await registry.anyTokens('agent'), false);
  assert.equal(await registry.lookupToken('0'.repeat(64)), null);

  const bound = await insertToken('agent', { nodeName: 'Runner-7', expires: 3600 });
  const revoked = await insertToken('client', { revoked: true });
  assert.equal(await registry.anyTokens('agent'), true);
  assert.equal(await registry.anyTokens('client'), true, 'a revoked token counts');
  assert.equal(await registry.anyTokens('client', true), false, 'unless only active ones are asked for');
  const expiredClient = await insertToken('client', { expires: -60 });
  assert.equal(await registry.anyTokens('client', true), true, 'an expired one counts as active');

  const row = await registry.lookupToken(bound.hash);
  assert.deepEqual(Object.keys(row), ['id', 'name', 'kind', 'prefix', 'nodeName', 'expiresAt', 'revokedAt', 'scoped', 'scope', 'storedTwice']);
  assert.deepEqual([row.scoped, row.scope, row.storedTwice], [false, null, false]);
  assert.equal(row.id, bound.id);
  assert.equal(row.kind, 'agent');
  assert.equal(row.prefix, bound.token.slice(0, 12));
  assert.equal(row.nodeName, 'runner-7');
  assert.equal(row.revokedAt, null);
  // The server runs in a non-UTC zone; the stored UTC time must come back as UTC.
  assert.match(row.expiresAt, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.ok(Math.abs(Date.parse(row.expiresAt) - (Date.now() + 3600000)) < 10000, row.expiresAt);
  assert.ok(Date.parse((await registry.lookupToken(revoked.hash)).revokedAt) <= Date.now() + 2000);
  assert.ok(Date.parse((await registry.lookupToken(expiredClient.hash)).expiresAt) < Date.now());

  await registry.touchToken(bound.id);
  const [[used]] = await admin.query('SELECT TIMESTAMPDIFF(SECOND, lastUsedAt, UTC_TIMESTAMP()) AS age FROM accessTokens WHERE id = ?', [bound.id]);
  assert.ok(used.age >= 0 && used.age < 5, `lastUsedAt is ${used.age}s from UTC now`);

  // A dropped table is recreated instead of failing every lookup.
  await admin.query('DROP TABLE accessTokens');
  assert.equal(await registry.lookupToken(bound.hash), null);
  assert.equal(await registry.anyTokens('agent'), false);
});

test('nameHolder: the still-valid token bound to the name that the node last registered with', { skip }, async () => {
  const registry = await newRegistry();
  await admin.query('DELETE FROM accessTokens');
  await admin.query("DELETE FROM nodes WHERE name LIKE 'holder-%'");
  const bound = await insertToken('agent', { nodeName: 'Holder-1' });
  const other = await insertToken('agent', { nodeName: 'somewhere-else' });
  const free = await insertToken('agent');
  const info = (name, tokenId) => ({ name, slots: [{ index: 1, healthy: true }], browsers: [], remoteAddress: null, ephemeral: false, tokenId });
  assert.equal(await registry.nameHolder('holder-1'), null, 'no node row yet');
  await registry.register(info('holder-1', bound.id));
  assert.deepEqual(await registry.nameHolder('holder-1'), { id: bound.id, name: 'agent test', prefix: bound.token.slice(0, 12) });
  assert.equal((await registry.nameHolder('HOLDER-1')).id, bound.id);
  await registry.register(info('holder-2', free.id));
  assert.equal(await registry.nameHolder('holder-2'), null, 'an unbound token holds nothing');
  await registry.register(info('holder-3', other.id));
  assert.equal(await registry.nameHolder('holder-3'), null, 'a token bound to another name holds nothing');
  await admin.query('UPDATE accessTokens SET expiresAt = UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE id = ?', [bound.id]);
  assert.equal(await registry.nameHolder('holder-1'), null, 'an expired one lets go');
  await admin.query('UPDATE accessTokens SET expiresAt = UTC_TIMESTAMP() + INTERVAL 1 HOUR, revokedAt = UTC_TIMESTAMP() WHERE id = ?', [bound.id]);
  assert.equal(await registry.nameHolder('holder-1'), null, 'so does a revoked one');
});

test('register stores the tokenId, and a re-register without a managed token clears it', { skip }, async () => {
  const registry = await newRegistry();
  const info = (tokenId) => ({ name: 'tok-node', slots: [{ index: 1, healthy: true }], browsers: ['chromium'], remoteAddress: '10.0.0.5', ephemeral: false, tokenId });
  await registry.register(info(42));
  let [[row]] = await admin.query("SELECT tokenId, online FROM nodes WHERE name = 'tok-node'");
  assert.deepEqual(row, { tokenId: 42, online: 1 });
  await registry.register(info(null));
  [[row]] = await admin.query("SELECT tokenId FROM nodes WHERE name = 'tok-node'");
  assert.equal(row.tokenId, null);
});

test('hub on MySQL: managed tokens, node binding, revocation and expiry set with SQL', { skip }, async () => {
  const registry = await newRegistry({ init: false });
  await admin.query('DELETE FROM accessTokens');
  const agentToken = await insertToken('agent');
  const bound = await insertToken('agent', { nodeName: 'mysql-bound' });
  const lapsing = await insertToken('agent', { expires: 4 });
  const clientToken = await insertToken('client');
  const { url, hub } = await h.startHub({ tokenRecheckMs: 300 }, registry);

  const a = await h.startAgent(url, { name: 'mysql-a', token: agentToken.token });
  const b = await h.startAgent(url, { name: 'mysql-bound', token: bound.token });
  const c = await h.startAgent(url, { name: 'mysql-c', token: lapsing.token });
  await h.waitFor(() => h.allHealthy(a) && h.allHealthy(b) && c.registered, { what: 'three agents registered' });
  await h.waitFor(async () => (await admin.query("SELECT COUNT(*) AS n FROM nodes WHERE tokenId IS NOT NULL AND online = 1"))[0][0].n === 3,
    { what: 'tokenId on the node rows' });
  const [rows] = await admin.query("SELECT name, tokenId FROM nodes WHERE name LIKE 'mysql-%' ORDER BY name");
  assert.deepEqual(rows, [{ name: 'mysql-a', tokenId: agentToken.id }, { name: 'mysql-bound', tokenId: bound.id }, { name: 'mysql-c', tokenId: lapsing.id }]);
  const [[used]] = await admin.query('SELECT lastUsedAt FROM accessTokens WHERE id = ?', [agentToken.id]);
  assert.ok(used.lastUsedAt);

  const wrong = await h.startAgent(url, { name: 'not-it', token: bound.token });
  await h.waitFor(() => wrong.lastError && /4403/.test(wrong.lastError), { what: '4403 for the wrong name' });

  // Client tokens: header and query; an agent token is not a client token.
  let ws = await h.connect(`${url}/connect?node=mysql-a&gateway_token=${clientToken.token}`);
  ws.send('q');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'q');
  ws = await h.connect(`${url}/connect?node=mysql-a`, { 'x-gateway-token': clientToken.token });
  ws.send('hdr');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'hdr');
  await assert.rejects(h.connect(`${url}/connect?node=mysql-a&gateway_token=${agentToken.token}`), { status: 401 });

  // Revoked in the database (as the UI does it): the agent is closed with 4401 within the re-check interval.
  const codes = [];
  a.on('disconnected', (code) => codes.push(code));
  await admin.query('UPDATE accessTokens SET revokedAt = UTC_TIMESTAMP() WHERE id = ?', [agentToken.id]);
  await h.waitFor(() => codes.length >= 1, { timeout: 3000, what: 'revoked agent kicked' });
  assert.equal(codes[0], 4401);
  await h.waitFor(async () => (await admin.query("SELECT online FROM nodes WHERE name = 'mysql-a'"))[0][0].online === 0, { what: 'mysql-a offline' });
  // The expiry (UTC in the database, the server itself on local time) is honoured within a few seconds.
  await h.waitFor(() => !c.registered && c.lastError && /4401/.test(c.lastError), { timeout: 10000, what: 'lapsed agent kicked' });
  assert.equal(b.registered, true);
  assert.deepEqual([...hub.nodes.keys()], ['mysql-bound']);
});

test('init adds claimedBy to a portUsage table made before it; the claim, stamp and lease methods', { skip }, async () => {
  await admin.query('SET FOREIGN_KEY_CHECKS = 0');
  for (const table of ['portUsage', 'portStatus', 'nodes', 'hubs']) await admin.query(`DROP TABLE IF EXISTS ${table}`);
  await admin.query('SET FOREIGN_KEY_CHECKS = 1');
  // portStatus and portUsage as they were before the hub routed clients.
  await admin.query(`CREATE TABLE portStatus (id INT AUTO_INCREMENT PRIMARY KEY, PortNumber INT NOT NULL, PortStatus INT NOT NULL DEFAULT 0,
    PortServer VARCHAR(255) NOT NULL, LastUpdateTime DATETIME NULL, UNIQUE KEY uq_server_port (PortServer, PortNumber))`);
  await admin.query(`CREATE TABLE portUsage (id INT PRIMARY KEY, portNumber INT NOT NULL, inUse TINYINT NOT NULL DEFAULT 0, LastRunDate DATETIME NULL,
    failCount INT NOT NULL DEFAULT 0, KEY idx_in_use (inUse), CONSTRAINT fk_port_usage_status FOREIGN KEY (id) REFERENCES portStatus (id) ON DELETE CASCADE)`);
  await admin.query("INSERT INTO portStatus (PortNumber, PortStatus, PortServer) VALUES (3000, 0, 'static-a'), (3001, 0, 'static-a')");
  await admin.query('INSERT INTO portUsage (id, portNumber, inUse, failCount) SELECT id, PortNumber, 0, 0 FROM portStatus');

  const registry = await newRegistry();
  const [cols] = await admin.query(
    "SELECT COLUMN_NAME AS name, IS_NULLABLE AS nullable, DATA_TYPE AS type FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'portUsage' ORDER BY ORDINAL_POSITION");
  assert.deepEqual(cols.at(-1), { name: 'claimedBy', nullable: 'YES', type: 'varchar' });
  await registry.applySchema(); // idempotent
  await registry.register({ name: 'gw-node', slots: [{ index: 1, healthy: true }, { index: 2, healthy: false }], browsers: ['chromium', 'firefox'],
    playwrightVersion: '1.64.1', agentVersion: 'test', platform: 'linux', remoteAddress: null, ephemeral: false, tokenId: null });

  const states = async () => (await registry.slotStates(3, 300000)).sort((a, b) => a.server.localeCompare(b.server) || a.slot - b.slot);
  let rows = await states();
  assert.deepEqual(rows.map((r) => [r.server, r.slot, r.healthy, r.inUse, r.failing, r.claimedBy]),
    [['gw-node', 1, true, false, false, null], ['gw-node', 2, false, false, false, null], ['static-a', 3000, false, false, false, null], ['static-a', 3001, false, false, false, null]]);
  assert.deepEqual(rows[0].node, { online: true, browsers: ['chromium', 'firefox'], playwrightVersion: '1.64.1' });
  assert.equal(rows[2].node, null);

  // Claiming is the conditional UPDATE: of two concurrent claims exactly one wins.
  const id = rows[0].id;
  const results = await Promise.all([registry.claimSlot(id, 'hub:aaaa0000:1'), registry.claimSlot(id, 'hub:aaaa0000:2')]);
  assert.equal(results.filter((r) => r === null).length, 1);
  const stamp = results.find((r) => r !== null);
  const [[claim]] = await admin.query('SELECT inUse, claimedBy, LastRunDate, TIMESTAMPDIFF(SECOND, LastRunDate, UTC_TIMESTAMP()) AS age FROM portUsage WHERE id = ?', [id]);
  assert.equal(claim.inUse, 1);
  assert.match(claim.claimedBy, /^hub:aaaa0000:[12]$/);
  assert.equal(stamp, claim.LastRunDate, 'the claim resolves its LastRunDate');
  assert.equal((await states())[0].lastRun, stamp, 'and slotStates reports the same stamp');
  assert.ok(claim.age >= 0 && claim.age < 5, `LastRunDate is ${claim.age}s from UTC now`);
  const other = claim.claimedBy.endsWith('1') ? 'hub:aaaa0000:2' : 'hub:aaaa0000:1';
  assert.equal(await registry.releaseSlot(id, other), false, 'only the claim that holds it releases it');
  assert.equal(await registry.releaseSlot(id, claim.claimedBy, '2000-01-01 00:00:00'), false, 'nor one whose stamp is not the claim\'s');
  // An older stale-claim sweep freed it without touching claimedBy: the hub may claim it again.
  await admin.query('UPDATE portUsage SET inUse = 0 WHERE id = ?', [id]);
  assert.equal(await registry.reassertClaim(id, other), false);
  assert.equal(await registry.reassertClaim(id, claim.claimedBy, '2000-01-01 00:00:00'), false);
  assert.equal(await registry.reassertClaim(id, claim.claimedBy, stamp), true);
  assert.equal(await registry.releaseSlot(id, claim.claimedBy, stamp), true);
  rows = await states();
  assert.deepEqual([rows[0].inUse, rows[0].claimedBy], [false, null]);

  // Failures and the cooldown, on the database clock (this server is not on UTC).
  for (let i = 0; i < 3; i++) await registry.addFailure(id);
  await admin.query('UPDATE portUsage SET LastRunDate = UTC_TIMESTAMP() - INTERVAL 100 SECOND WHERE id = ?', [id]);
  rows = await states();
  assert.equal(rows[0].failCount, 3);
  assert.equal(rows[0].failing, true);
  assert.ok(rows[0].retryInMs > 195000 && rows[0].retryInMs <= 202000, String(rows[0].retryInMs));
  await admin.query('UPDATE portUsage SET LastRunDate = UTC_TIMESTAMP() - INTERVAL 301 SECOND WHERE id = ?', [id]);
  assert.equal((await states())[0].failing, false, 'the cooldown is over');
  await registry.resetFailures(id);
  assert.equal((await states())[0].failCount, 0);

  // Leases: a hub frees the claims of a hub whose lease ran out, never those of a live hub, its own, or
  // anyone else's; _ and % in the instance are literal.
  const [g1, g2, s1, s2] = [rows[0].id, rows[1].id, rows[2].id, rows[3].id];
  await registry.renewLease('live0001', { host: 'a', port: 4500 });
  await registry.renewLease('me000001', { host: 'b', port: 4501 });
  await admin.query("INSERT INTO hubs (instance, host, port, startedAt, lastSeen) VALUES ('dead0001', 'c', 4502, UTC_TIMESTAMP(), UTC_TIMESTAMP() - INTERVAL 200 SECOND)");
  assert.deepEqual((await registry.liveHubs(90000)).map((hub) => hub.instance).sort(), ['live0001', 'me000001']);
  await registry.claimSlot(g1, 'hub:live0001:1');
  await registry.claimSlot(g2, 'hub:dead0001:1');
  await registry.claimSlot(s1, 'hubx:1');
  await admin.query('UPDATE portUsage SET inUse = 1, LastRunDate = UTC_TIMESTAMP(), claimedBy = NULL WHERE id = ?', [s2]); // another claimer
  assert.equal(await registry.releaseDeadHubClaims('me_00001', 90000), 1);
  const [after] = await admin.query('SELECT id, inUse, claimedBy FROM portUsage WHERE id IN (?)', [[g1, g2, s1, s2]]);
  const byId = new Map(after.map((r) => [r.id, [r.inUse, r.claimedBy]]));
  assert.deepEqual([g1, g2, s1, s2].map((i) => byId.get(i)), [[1, 'hub:live0001:1'], [0, null], [1, 'hubx:1'], [1, null]]);
  const [hubs] = await admin.query('SELECT instance FROM hubs ORDER BY instance');
  assert.deepEqual(hubs.map((r) => r.instance), ['live0001', 'me000001'], 'the dead hub\'s row is gone');
  // A hub never takes itself for dead, even with a lapsed lease.
  await registry.claimSlot(g2, 'hub:me000001:2');
  await admin.query("UPDATE hubs SET lastSeen = UTC_TIMESTAMP() - INTERVAL 500 SECOND WHERE instance = 'me000001'");
  assert.equal(await registry.releaseDeadHubClaims('me000001', 90000), 0);
  await registry.leaveHub('me000001');
  assert.equal(await registry.releaseDeadHubClaims('live0001', 90000), 1, 'a hub that left is dead to the others');
});

test('hub on MySQL: /connect claims the slot in portUsage, releases it, and dials a static host', { skip }, async () => {
  const registry = await newRegistry({ init: false });
  await admin.query('SET FOREIGN_KEY_CHECKS = 0');
  for (const table of ['portUsage', 'portStatus', 'nodes', 'hubs']) await admin.query(`DROP TABLE IF EXISTS ${table}`);
  await admin.query('SET FOREIGN_KEY_CHECKS = 1');
  await admin.query('DELETE FROM accessTokens'); // no managed tokens: agents and clients need none
  const { url, hub } = await h.startHub({ queuePollMs: 100 }, registry);
  const agent = await h.startAgent(url, { name: 'mysql-route', slots: 1 });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });
  await h.waitFor(async () => (await admin.query("SELECT COUNT(*) AS n FROM portStatus WHERE PortServer = 'mysql-route' AND PortStatus = 1"))[0][0].n === 1,
    { what: 'slot healthy in portStatus' });

  const usage = async (server) => (await admin.query(
    'SELECT pu.inUse, pu.claimedBy, pu.failCount FROM portUsage pu JOIN portStatus ps ON ps.id = pu.id WHERE ps.PortServer = ?', [server]))[0][0];
  const ws = await h.connect(`${url}/connect`, { 'x-playwright-browser': 'chromium' });
  ws.send('hello');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'hello');
  let row = await usage('mysql-route');
  assert.equal(row.inUse, 1);
  assert.match(row.claimedBy, /^hub:[0-9a-f]{8}:\d+$/);

  // The only chromium slot is busy, so a second client waits; it gets the slot once the first one ends.
  const second = h.connect(`${url}/connect`, { 'x-playwright-browser': 'chromium' });
  await h.waitFor(() => hub.queue.length === 1, { what: 'second client queued' });
  ws.close();
  const ws2 = await second;
  ws2.send('next');
  assert.equal((await h.nextMessage(ws2)).data.toString(), 'next');
  ws2.close();
  await h.waitFor(async () => (row = await usage('mysql-route')) && row.inUse === 0, { what: 'released in portUsage' });
  assert.equal(row.claimedBy, null);

  // A legacy static host (a portStatus row with no nodes row): the hub dials it directly.
  const wss = new WebSocket.WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise((r) => wss.once('listening', r));
  h.cleanups.push(async () => wss.close());
  wss.on('connection', (sock, req) => sock.on('message', () => sock.send(`static saw ${req.headers['x-playwright-browser']}`)));
  const staticPort = wss.address().port;
  await admin.query("INSERT INTO portStatus (PortNumber, PortStatus, PortServer, LastUpdateTime) VALUES (?, 0, '127.0.0.1', UTC_TIMESTAMP())", [staticPort]);
  await admin.query("INSERT INTO portUsage (id, portNumber, inUse, failCount) SELECT id, PortNumber, 0, 0 FROM portStatus WHERE PortServer = '127.0.0.1'");
  const viaStatic = await h.connect(`${url}/connect?node=127.0.0.1`, { 'x-playwright-browser': 'webkit' });
  viaStatic.send('hi');
  assert.equal((await h.nextMessage(viaStatic)).data.toString(), 'static saw webkit');
  assert.equal((await usage('127.0.0.1')).inUse, 1);
  const [[lease]] = await admin.query('SELECT host, TIMESTAMPDIFF(SECOND, lastSeen, UTC_TIMESTAMP()) AS age FROM hubs WHERE instance = ?', [hub.instance]);
  assert.ok(lease && lease.age >= 0 && lease.age < 5, 'the hub holds a lease while it runs');
  await hub.stop();
  row = await usage('127.0.0.1');
  assert.deepEqual([row.inUse, row.claimedBy], [0, null], 'stopping the hub released it');
  assert.equal((await admin.query('SELECT COUNT(*) AS n FROM hubs WHERE instance = ?', [hub.instance]))[0][0].n, 0, 'and gave up its lease');
});

const tokenColumns = async () => (await admin.query(
  `SELECT COLUMN_NAME AS name, IS_NULLABLE AS nullable, COLUMN_TYPE AS type FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'accessTokens' ORDER BY ORDINAL_POSITION`))[0];

test('init adds accessTokens.scope', { skip }, async () => {
  await admin.query('DROP TABLE IF EXISTS accessTokens');
  // accessTokens as it was before scopes.
  await admin.query(`CREATE TABLE accessTokens (
    id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(128) NOT NULL, kind VARCHAR(16) NOT NULL, tokenHash CHAR(64) NOT NULL,
    prefix VARCHAR(16) NOT NULL, nodeName VARCHAR(255) NULL, createdAt DATETIME NOT NULL, expiresAt DATETIME NULL,
    lastUsedAt DATETIME NULL, revokedAt DATETIME NULL, UNIQUE KEY uq_token_hash (tokenHash), KEY idx_kind (kind))`);
  const old = `pwgc_${crypto.randomBytes(32).toString('base64url')}`;
  const oldHash = crypto.createHash('sha256').update(old).digest('hex');
  await admin.query("INSERT INTO accessTokens (name, kind, tokenHash, prefix, createdAt) VALUES ('made before scopes', 'client', ?, ?, UTC_TIMESTAMP())",
    [oldHash, old.slice(0, 12)]);

  const lines = [];
  const registry = new MySqlRegistry(config.mysql, { schemaPath: config.schemaPath, log: { ...silent, info: (line) => lines.push(line) }, retries: 2, retryDelayMs: 500 });
  registries.push(registry);
  await registry.init();
  const cols = await tokenColumns();
  assert.deepEqual(cols.filter((c) => c.name === 'scope'), [{ name: 'scope', nullable: 'YES', type: 'varchar(255)' }]);
  assert.equal(cols.length, 11);
  assert.deepEqual(lines.filter((l) => /scope/.test(l)), ['added the scope column to the accessTokens table']);

  // No row is changed: a token made before scopes has none, and is found as before.
  const [[stored]] = await admin.query('SELECT name, tokenHash, scope FROM accessTokens');
  assert.deepEqual(stored, { name: 'made before scopes', tokenHash: oldHash, scope: null });
  const row = await registry.lookupToken(oldHash);
  assert.deepEqual([row.name, row.scoped, row.scope, row.storedTwice], ['made before scopes', false, null, false]);
  assert.equal(tokenProblem(row, 'client'), null);

  lines.length = 0;
  await registry.applySchema(); // a second run changes nothing
  assert.deepEqual(await tokenColumns(), cols);
  assert.deepEqual(lines, []);
  const [keys] = await admin.query("SHOW INDEX FROM accessTokens WHERE Key_name = 'uq_token_hash'");
  assert.equal(keys[0].Non_unique, 0);
});

test('lookupToken finds a token by its plain hash in either form and says which', { skip }, async () => {
  const registry = await newRegistry();
  await admin.query('DELETE FROM accessTokens');
  const plain = await insertToken('client');
  const scoped = await insertToken('client', { scope: 'grid.view sessions.open' });
  const empty = await insertToken('client', { scope: '' });
  const agent = await insertToken('agent');
  assert.match(plain.stored, /^[0-9a-f]{64}$/);
  for (const t of [scoped, empty]) {
    assert.equal(t.stored, `scoped:${t.hash.slice(0, 57)}`);
    assert.equal(t.stored.length, 64);
  }
  const [stored] = await admin.query('SELECT id, tokenHash, scope FROM accessTokens ORDER BY id');
  assert.deepEqual(stored, [
    { id: plain.id, tokenHash: plain.hash, scope: null }, { id: scoped.id, tokenHash: scoped.stored, scope: 'grid.view sessions.open' },
    { id: empty.id, tokenHash: empty.stored, scope: '' }, { id: agent.id, tokenHash: agent.hash, scope: null },
  ]);

  const said = async (t) => {
    const row = await registry.lookupToken(t.hash);
    return [row.id, row.kind, row.prefix, row.scoped, row.scope, row.storedTwice];
  };
  assert.deepEqual(await said(plain), [plain.id, 'client', plain.token.slice(0, 12), false, null, false]);
  assert.deepEqual(await said(scoped), [scoped.id, 'client', scoped.token.slice(0, 12), true, 'grid.view sessions.open', false]);
  assert.deepEqual(await said(empty), [empty.id, 'client', empty.token.slice(0, 12), true, '', false], 'an empty scope is not no scope');
  assert.deepEqual(await said(agent), [agent.id, 'agent', agent.token.slice(0, 12), false, null, false]);
  assert.equal(Object.hasOwn(await registry.lookupToken(scoped.hash), 'tokenHash'), false, 'the stored form is not handed on');
  assert.equal(await registry.lookupToken(crypto.createHash('sha256').update(`scoped:${scoped.token}`).digest('hex')), null);

  assert.equal(tokenProblem(await registry.lookupToken(plain.hash), 'client'), null);
  assert.equal(tokenProblem(await registry.lookupToken(scoped.hash), 'client'), null);
  assert.equal(tokenProblem(await registry.lookupToken(empty.hash), 'client'), 'not allowed to open browser sessions (its scope leaves that out)');
  // Changed by hand: a scoped row with its scope taken off, and a plain row given one.
  await admin.query('UPDATE accessTokens SET scope = NULL WHERE id = ?', [scoped.id]);
  await admin.query("UPDATE accessTokens SET scope = 'sessions.open' WHERE id IN (?, ?)", [plain.id, agent.id]);
  assert.deepEqual(await said(scoped), [scoped.id, 'client', scoped.token.slice(0, 12), true, null, false]);
  assert.deepEqual(await said(plain), [plain.id, 'client', plain.token.slice(0, 12), false, 'sessions.open', false]);
  for (const [t, kind] of [[scoped, 'client'], [plain, 'client'], [agent, 'agent']]) {
    assert.equal(tokenProblem(await registry.lookupToken(t.hash), kind), 'not usable: its scope was lost or changed in the database');
  }
});

test('lookupToken says when a token is stored twice', { skip }, async () => {
  const registry = await newRegistry();
  await admin.query('DELETE FROM accessTokens');
  const token = await insertToken('client');
  const single = await registry.lookupToken(token.hash);
  assert.deepEqual([single.id, single.storedTwice], [token.id, false]);
  // The same token again in the scoped form: the unique key does not stop it, the two values differ.
  await admin.query(
    "INSERT INTO accessTokens (name, kind, tokenHash, prefix, createdAt, scope) VALUES ('client test', 'client', ?, ?, UTC_TIMESTAMP(), 'sessions.open')",
    [`scoped:${token.hash.slice(0, 57)}`, token.token.slice(0, 12)]);
  const row = await registry.lookupToken(token.hash);
  assert.equal(row.storedTwice, true);
  assert.equal(tokenProblem(row, 'client'), 'not usable: it is stored twice, with and without a scope');

  // The hub turns it away with 401, and another token is not affected.
  const other = await insertToken('client', { scope: 'sessions.open' });
  assert.equal((await registry.lookupToken(other.hash)).storedTwice, false);
  const hubRegistry = await newRegistry({ init: false });
  const { url } = await h.startHub({ tokenCacheMs: 0 }, hubRegistry);
  await assert.rejects(h.connect(`${url}/connect?node=nobody&gateway_token=${token.token}`),
    (err) => err.status === 401 && /^gateway token is not usable: it is stored twice, with and without a scope\n$/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?node=nobody&gateway_token=${other.token}`), { status: 404 });
});

test('a dropped scope column is put back on the next lookup and scoped rows are then refused', { skip }, async () => {
  const lines = [];
  const registry = new MySqlRegistry(config.mysql, { schemaPath: config.schemaPath, log: { ...silent, warn: (line) => lines.push(line), info: (line) => lines.push(line) }, retries: 2, retryDelayMs: 500 });
  registries.push(registry);
  const { url } = await h.startHub({ tokenCacheMs: 0 }, registry);
  await admin.query('DELETE FROM accessTokens');
  const plain = await insertToken('client');
  const scoped = await insertToken('client', { scope: 'grid.view sessions.open' });
  const limited = await insertToken('client', { scope: 'grid.view' });
  const answer = (t) => h.connect(`${url}/connect?node=nobody&gateway_token=${t.token}`).then(() => 101, (err) => [err.status, err.body.trim()]);
  assert.deepEqual(await answer(plain), [404, 'no node or static host named nobody is known to the hub']);
  assert.deepEqual(await answer(scoped), [404, 'no node or static host named nobody is known to the hub']);
  assert.deepEqual(await answer(limited), [403, 'gateway token is not allowed to open browser sessions (its scope leaves that out)']);

  await admin.query('ALTER TABLE accessTokens DROP COLUMN scope');
  assert.equal((await tokenColumns()).some((c) => c.name === 'scope'), false);
  lines.length = 0;
  const row = await registry.lookupToken(scoped.hash);
  assert.deepEqual([row.id, row.scoped, row.scope, row.storedTwice], [scoped.id, true, null, false]);
  assert.deepEqual((await tokenColumns()).filter((c) => c.name === 'scope'), [{ name: 'scope', nullable: 'YES', type: 'varchar(255)' }]);
  assert.equal(lines.length, 2, lines.join('\n'));
  assert.match(lines[0], /^table or column missing \(Unknown column 'scope' in 'field list'\); recreating the schema$/);
  assert.equal(lines[1], 'added the scope column to the accessTokens table');

  // Every scope is gone with the column. A row in the scoped form is refused, never taken for a token
  // with no limit; a token that never had a scope works as before.
  const lost = [401, 'gateway token is not usable: its scope was lost or changed in the database'];
  assert.deepEqual(await answer(scoped), lost);
  assert.deepEqual(await answer(limited), lost);
  assert.deepEqual(await answer(plain), [404, 'no node or static host named nobody is known to the hub']);
  assert.equal((await admin.query('SELECT COUNT(*) AS n FROM accessTokens WHERE scope IS NOT NULL'))[0][0].n, 0);
  assert.equal((await admin.query("SELECT COUNT(*) AS n FROM accessTokens WHERE tokenHash LIKE 'scoped:%'"))[0][0].n, 2, 'the rows stay');
});
