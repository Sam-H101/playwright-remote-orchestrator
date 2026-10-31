'use strict';

// The MySQL registry against a real server. Opt-in, because it DROPS the gateway tables in DB_NAME:
//   GATEWAY_TEST_MYSQL=1 DB_HOST=127.0.0.1 DB_PORT=3307 DB_USER=... DB_PASSWORD=... DB_NAME=<a scratch database> npm test

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { test, before, after, afterEach } = require('node:test');
const h = require('./helpers');
const { hubConfig } = require('../lib/config');
const { MySqlRegistry } = require('../lib/registry');
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
async function insertToken(kind, { nodeName = null, expires = null, revoked = false } = {}) {
  const token = `pwg${kind === 'agent' ? 'a' : 'c'}_${crypto.randomBytes(32).toString('base64url')}`;
  const hash = crypto.createHash('sha256').update(token).digest('hex');
  const [res] = await admin.query(
    `INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt, revokedAt)
     VALUES (?, ?, ?, ?, ?, UTC_TIMESTAMP(), ${expires === null ? 'NULL' : 'UTC_TIMESTAMP() + INTERVAL ? SECOND'}, ${revoked ? 'UTC_TIMESTAMP()' : 'NULL'})`,
    [`${kind} test`, kind, hash, token.slice(0, 12), nodeName, ...(expires === null ? [] : [expires])]);
  return { token, hash, id: res.insertId };
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
  for (const table of ['portUsage', 'portStatus', 'nodes', 'accessTokens']) await admin.query(`DROP TABLE IF EXISTS ${table}`);
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
  assert.deepEqual(Object.keys(row), ['id', 'name', 'kind', 'prefix', 'nodeName', 'expiresAt', 'revokedAt']);
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
  let ws = await h.connect(`${url}/connect/mysql-a/1?gateway_token=${clientToken.token}`);
  ws.send('q');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'q');
  ws = await h.connect(`${url}/connect/mysql-a/2`, { 'x-gateway-token': clientToken.token });
  ws.send('hdr');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'hdr');
  await assert.rejects(h.connect(`${url}/connect/mysql-a/1?gateway_token=${agentToken.token}`), { status: 401 });

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
