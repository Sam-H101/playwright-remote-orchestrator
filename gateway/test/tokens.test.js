'use strict';

// Managed access tokens (accessTokens, administered from the Grid UI) next to the env bootstrap tokens.

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { test, afterEach } = require('node:test');
const WebSocket = require('ws');
const h = require('./helpers');
const { createHub } = require('../hub');
const { MemoryRegistry } = require('../lib/registry');
const { hashToken, scopedHash, isManagedToken } = require('../lib/protocol');
const { createTokenAuth, tokenProblem } = require('../lib/tokens');
const { silent } = require('../lib/log');

afterEach(h.cleanup);

// The Grid UI's format: pwga_ / pwgc_ + secrets.token_urlsafe(32).
const mint = (kind = 'agent') => `pwg${kind === 'agent' ? 'a' : 'c'}_${crypto.randomBytes(32).toString('base64url')}`;
const iso = (offsetMs) => new Date(Date.now() + offsetMs).toISOString();
const bearer = (token) => ({ headers: { Authorization: `Bearer ${token}` } });

// Opens /agent and collects what the hub says until it closes: { code, reason, errors: [error messages] }.
function refused(url, token, name = 'refused-node') {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${url}/agent`, token ? bearer(token) : {});
    h.cleanups.push(async () => ws.terminate());
    const errors = [];
    ws.on('open', () => ws.send(JSON.stringify({ type: 'register', name, slots: [{ index: 1, healthy: true }] })));
    ws.on('message', (data) => {
      const msg = JSON.parse(data);
      if (msg.type === 'error') errors.push(msg.message);
      if (msg.type === 'registered') reject(new Error(`unexpectedly registered as ${msg.name}`));
    });
    ws.on('error', () => {});
    ws.on('close', (code, reason) => resolve({ code, reason: reason.toString(), errors }));
  });
}

// A client pinned to a node nobody serves: 404 means the token was accepted, 401/503 that it wasn't.
function clientStatus(url, query = '', headers = {}) {
  return h.connect(`${url}/connect?node=nobody${query.replace(/^\?/, '&')}`, headers).then(() => 101, (err) => err.status);
}

test('a managed agent token registers the node, is recorded on it, and turns agent auth on', async () => {
  const registry = new MemoryRegistry();
  const token = mint();
  const row = registry.addToken(token, { name: 'build farm' });
  assert.deepEqual(await registry.lookupToken(hashToken(token)),
    { id: row.id, name: 'build farm', kind: 'agent', prefix: token.slice(0, 12), nodeName: null, expiresAt: null, revokedAt: null, scoped: false, scope: null, storedTwice: false });
  assert.equal(await registry.lookupToken(hashToken(`${token}x`)), null);

  const { url, hub } = await h.startHub({}, registry);
  const agent = await h.startAgent(url, { name: 'Managed-1', token });
  await h.waitFor(() => h.allHealthy(agent), { what: 'managed agent registered' });
  assert.equal(registry.nodes.get('managed-1').tokenId, row.id);
  assert.equal(hub.nodes.get('managed-1').auth.via, 'managed');
  assert.match(row.lastUsedAt, /Z$/);

  // A session runs over a data channel that carries the same managed token.
  const ws = await h.connect(`${url}/connect?node=managed-1`);
  ws.send('hello');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'hello');

  // Agent auth is enforced now that a managed agent token exists.
  let r = await refused(url, null);
  assert.equal(r.code, 4401);
  assert.match(r.errors[0], /missing agent token/);
  r = await refused(url, mint());
  assert.equal(r.code, 4401);
  assert.match(r.errors[0], /unknown token pwga_/);
  assert.deepEqual([...hub.nodes.keys()], ['managed-1']);
});

test('revoked, expired and client-kind tokens are refused with 4401; a future expiry is fine', async () => {
  const registry = new MemoryRegistry();
  const revoked = mint();
  const expired = mint();
  const client = mint('client');
  const later = mint();
  registry.addToken(revoked, { revokedAt: iso(-60000) });
  registry.addToken(expired, { expiresAt: iso(-1000) });
  registry.addToken(client, { kind: 'client' });
  const laterRow = registry.addToken(later, { expiresAt: iso(3600000) });
  const { url, hub } = await h.startHub({}, registry);

  for (const [token, why] of [[revoked, /is revoked/], [expired, /is expired/], [client, /not an agent token/]]) {
    const r = await refused(url, token);
    assert.equal(r.code, 4401);
    assert.match(r.errors[0], why);
    assert.equal(r.errors[0].includes(token), false, 'the full token is never echoed');
  }
  const ok = await h.rawAgent(url, bearer(later));
  assert.deepEqual(await h.register(ok, 'later'), { type: 'registered', name: 'later' });
  assert.equal(registry.nodes.get('later').tokenId, laterRow.id);
  assert.deepEqual([...hub.nodes.keys()], ['later']);
});

test('a token bound to a node registers only that node (4403), and the agent keeps retrying', async () => {
  const registry = new MemoryRegistry();
  const bound = mint();
  const free = mint();
  registry.addToken(bound, { nodeName: 'Bound-1' });
  registry.addToken(free);
  const { url, hub } = await h.startHub({}, registry);

  const victim = await h.rawAgent(url, bearer(free));
  await h.register(victim, 'victim');
  const own = await h.rawAgent(url, bearer(bound));
  assert.deepEqual(await h.register(own, 'BOUND-1'), { type: 'registered', name: 'bound-1' });

  // It can't take another name, nor replace a node that is already there.
  for (const name of ['other', 'victim']) {
    const r = await refused(url, bound, name);
    assert.equal(r.code, 4403);
    assert.equal(r.reason, 'token not valid for this node name');
    assert.match(r.errors[0], new RegExp(`token not valid for this node name \\(${name}\\)`));
  }
  assert.equal(victim.readyState, WebSocket.OPEN);
  assert.deepEqual([...hub.nodes.keys()].sort(), ['bound-1', 'victim']);

  const agent = await h.startAgent(url, { name: 'not-bound-1', token: bound });
  const codes = [];
  const times = [];
  agent.on('disconnected', (code) => { codes.push(code); times.push(Date.now()); });
  await h.waitFor(() => codes.length >= 4, { what: 'four refused attempts' });
  assert.ok(codes.every((c) => c === 4403));
  assert.equal(agent.registered, false);
  assert.match(agent.lastError, /4403/);
  // Backoff, not a spin: 100 ms doubling to 400 ms here.
  const gaps = times.slice(1).map((t, i) => t - times[i]);
  assert.ok(gaps.every((g) => g >= 90), `gaps ${gaps}`);
  assert.ok(gaps[2] > gaps[0], `gaps ${gaps}`);
});

test('the enforcement switch: off with no tokens, on once one exists, and it stays on', async () => {
  const registry = new MemoryRegistry();
  const { url } = await h.startHub({ tokenCacheMs: 150, tokenRecheckMs: 60000 }, registry);

  // No tokens anywhere: today's behaviour, nothing is checked.
  let ws = await h.rawAgent(url);
  assert.equal((await h.register(ws, 'open-1')).type, 'registered');
  assert.equal(registry.nodes.get('open-1').tokenId, null);
  assert.equal(await clientStatus(url), 404);
  assert.equal(await clientStatus(url, '?gateway_token=whatever'), 404);

  const agentToken = mint();
  const agentRow = registry.addToken(agentToken);
  const clientToken = mint('client');
  const clientRow = registry.addToken(clientToken, { kind: 'client' });
  await h.sleep(250); // past the cache
  assert.equal((await refused(url, null)).code, 4401);
  assert.equal(await clientStatus(url), 401);
  assert.equal(await clientStatus(url, `?gateway_token=${clientToken}`), 404);
  ws = await h.rawAgent(url, bearer(agentToken));
  assert.equal((await h.register(ws, 'with-token')).type, 'registered');

  // Letting the last token lapse, revoking it (the response to a leak) or deleting the revoked rows
  // doesn't switch auth off: whoever holds the token can't get in by leaving it out or making one up.
  agentRow.expiresAt = iso(-1000);
  clientRow.expiresAt = iso(-1000);
  await h.sleep(250);
  assert.equal((await refused(url, null)).code, 4401);
  assert.equal(await clientStatus(url), 401);
  agentRow.revokedAt = iso(0);
  clientRow.revokedAt = iso(0);
  await h.sleep(250);
  for (const token of [null, agentToken, mint(), 'made-up']) {
    const r = await refused(url, token, 'with-token');
    assert.equal(r.code, 4401, `token ${token && token.slice(0, 5)}`);
  }
  assert.match((await refused(url, agentToken)).errors[0], /is revoked/);
  for (const query of ['', `?gateway_token=${clientToken}`, `?gateway_token=${mint('client')}`, '?gateway_token=whatever']) {
    assert.equal(await clientStatus(url, query), 401, query);
  }
  registry.tokens.clear();
  await h.sleep(250);
  assert.equal((await refused(url, null)).code, 4401);
  assert.equal(await clientStatus(url), 401);

  // A hub that starts with only revoked tokens in the table enforces too.
  const restarted = new MemoryRegistry();
  restarted.addToken(mint(), { revokedAt: iso(-60000) });
  restarted.addToken(mint('client'), { kind: 'client', revokedAt: iso(-60000) });
  const again = await h.startHub({}, restarted);
  assert.equal((await refused(again.url, null)).code, 4401);
  assert.equal(await clientStatus(again.url), 401);
});

test('GATEWAY_ALLOW_OPEN_AGENTS / _CLIENTS: off again once every token is revoked, but not for its holder', async () => {
  const registry = new MemoryRegistry();
  const lines = [];
  const log = { info: () => {}, warn: (m) => lines.push(m), error: () => {} };
  const hub = createHub({ ...h.hubConfig({ tokenCacheMs: 150, tokenRecheckMs: 60000 }), allowOpenAgents: true, allowOpenClients: true }, { log, registry });
  const url = `ws://127.0.0.1:${await hub.start()}`;
  h.cleanups.push(() => hub.stop());
  assert.ok(lines.some((m) => /^!!! .*GATEWAY_ALLOW_OPEN_AGENTS is set/.test(m)), lines.join('\n'));

  const agentToken = mint();
  const agentRow = registry.addToken(agentToken);
  const clientToken = mint('client');
  const clientRow = registry.addToken(clientToken, { kind: 'client' });
  await h.sleep(250);
  assert.equal((await refused(url, null)).code, 4401);
  assert.equal(await clientStatus(url), 401);
  // An expired token still counts.
  agentRow.expiresAt = iso(-1000);
  await h.sleep(250);
  assert.equal((await refused(url, null)).code, 4401);

  lines.length = 0;
  agentRow.revokedAt = iso(0);
  clientRow.revokedAt = iso(0);
  await h.sleep(250);
  const ws = await h.rawAgent(url);
  assert.equal((await h.register(ws, 'open-2')).type, 'registered');
  assert.equal(await clientStatus(url), 404);
  assert.ok(lines.some((m) => /^!!! every managed agent token is revoked and GATEWAY_ALLOW_OPEN_AGENTS is set/.test(m)), lines.join('\n'));
  assert.ok(lines.some((m) => /^!!! every managed client token is revoked and GATEWAY_ALLOW_OPEN_CLIENTS is set/.test(m)), lines.join('\n'));
  // ...but a revoked token is still refused.
  const r = await refused(url, agentToken);
  assert.equal(r.code, 4401);
  assert.match(r.errors[0], /is revoked/);
  assert.equal(await clientStatus(url, `?gateway_token=${clientToken}`), 401);
});

test('a token that is not in the managed format is refused without a database query', async () => {
  const registry = new MemoryRegistry();
  registry.addToken(mint());
  registry.addToken(mint('client'), { kind: 'client' });
  const lookups = [];
  const lookup = registry.lookupToken.bind(registry);
  registry.lookupToken = (hash) => {
    lookups.push(hash);
    return lookup(hash);
  };
  const { url } = await h.startHub({}, registry);
  for (let i = 0; i < 20; i++) assert.equal(await clientStatus(url, `?gateway_token=junk${i}`), 401);
  const r = await refused(url, 'not-a-pwga-token');
  assert.equal(r.code, 4401);
  assert.match(r.errors[0], /unknown token/);
  assert.equal(lookups.length, 0);
  // A well-formed one is looked up.
  assert.equal(await clientStatus(url, `?gateway_token=${mint('client')}`), 401);
  assert.equal(lookups.length, 1);

  // With an env token set and the database down, a mistyped token is a wrong token, not "try again later".
  const down = new MemoryRegistry();
  const { url: url2 } = await h.startHub({ agentToken: 'boot-agent', clientToken: 'boot-client' }, down);
  down.lookupToken = async () => { throw new Error('ECONNREFUSED'); };
  down.anyTokens = down.lookupToken;
  assert.equal((await refused(url2, 'boot-agnet')).code, 4401);
  assert.equal(await clientStatus(url2, '?gateway_token=boot-clinet'), 401);
  assert.equal((await refused(url2, mint())).code, 1013); // a managed one still fails closed
});

test('a node registered with a token bound to its name keeps the name against other tokens', async () => {
  const registry = new MemoryRegistry();
  const bound = mint();
  const bound2 = mint();
  const shared = mint();
  const boundRow = registry.addToken(bound, { nodeName: 'build-01' });
  const bound2Row = registry.addToken(bound2, { nodeName: 'build-01' });
  registry.addToken(shared);
  const { url, hub } = await h.startHub({ agentToken: 'boot-agent', tokenCacheMs: 100 }, registry);

  // Before a bound token has registered it, the name is anybody's (a migration from a shared token).
  let ws = await h.rawAgent(url, bearer(shared));
  assert.equal((await h.register(ws, 'build-01')).type, 'registered');
  const own = await h.rawAgent(url, bearer(bound));
  assert.deepEqual(await h.register(own, 'build-01'), { type: 'registered', name: 'build-01' });
  await h.waitFor(() => registry.nodes.get('build-01').tokenId === boundRow.id, { what: 'tokenId recorded' });

  // Now neither another unbound token nor the bootstrap token can replace it, in any spelling.
  for (const [token, name] of [[shared, 'BUILD-01'], ['boot-agent', 'build-01']]) {
    const r = await refused(url, token, name);
    assert.equal(r.code, 4403);
    assert.equal(r.reason, 'node name reserved for its bound token');
    assert.match(r.errors[0], /node name build-01 is reserved/);
  }
  assert.equal(own.readyState, WebSocket.OPEN);
  assert.equal(hub.nodes.get('build-01').ws.readyState, WebSocket.OPEN);

  // ...nor take it while the node is offline.
  own.close();
  await h.waitFor(() => !hub.nodes.has('build-01') && registry.nodes.get('build-01').online === 0, { what: 'build-01 offline' });
  assert.equal((await refused(url, shared, 'build-01')).code, 4403);

  // Another token bound to the name may (rotation), and so may the same one.
  ws = await h.rawAgent(url, bearer(bound2));
  assert.equal((await h.register(ws, 'build-01')).type, 'registered');
  await h.waitFor(() => registry.nodes.get('build-01').tokenId === bound2Row.id, { what: 'rotated tokenId' });
  const back = await h.rawAgent(url, bearer(bound));
  assert.equal((await h.register(back, 'build-01')).type, 'registered');
  await h.waitFor(() => ws.readyState === WebSocket.CLOSED, { what: 'rotated connection replaced' });

  // Revoking the bound tokens frees the name once its node is gone.
  boundRow.revokedAt = iso(0);
  bound2Row.revokedAt = iso(0);
  back.close();
  await h.waitFor(() => !hub.nodes.has('build-01'), { what: 'build-01 gone' });
  ws = await h.rawAgent(url, bearer(shared));
  assert.equal((await h.register(ws, 'build-01')).type, 'registered');
});

test('the node-name check falls back to the connected node when the database is down', async () => {
  const registry = new MemoryRegistry();
  const bound = mint();
  registry.addToken(bound, { nodeName: 'held' });
  const { url, hub } = await h.startHub({ agentToken: 'boot-agent' }, registry);
  const own = await h.rawAgent(url, bearer(bound));
  await h.register(own, 'held');
  registry.nameHolder = async () => { throw new Error('ECONNREFUSED'); };
  assert.equal((await refused(url, 'boot-agent', 'held')).code, 4403);
  const other = await h.rawAgent(url, bearer('boot-agent'));
  assert.equal((await h.register(other, 'elsewhere')).type, 'registered');
  assert.deepEqual([...hub.nodes.keys()].sort(), ['elsewhere', 'held']);
});

test('an agent that goes away while its register waits is not registered', async () => {
  const registry = new MemoryRegistry();
  let release;
  const gate = new Promise((r) => { release = r; });
  registry.nameHolder = async () => { await gate; return null; };
  const { url, hub } = await h.startHub({}, registry);
  const ws = await h.rawAgent(url);
  ws.send(JSON.stringify({ type: 'register', name: 'leaver', slots: [{ index: 1, healthy: true }] }));
  ws.send(JSON.stringify({ type: 'heartbeat', slots: [{ index: 1, healthy: true }] })); // ignored, not an error
  await h.sleep(100);
  assert.equal(ws.readyState, WebSocket.OPEN);
  ws.terminate();
  await h.sleep(100);
  release();
  await h.sleep(100);
  assert.equal(hub.nodes.size, 0);
  assert.equal(registry.nodes.size, 0);
});

test('the env bootstrap token works with the database down; managed tokens fail closed', async () => {
  const registry = new MemoryRegistry();
  const managed = mint();
  const managedClient = mint('client');
  registry.addToken(managed);
  registry.addToken(managedClient, { kind: 'client' });
  const { url } = await h.startHub({ agentToken: 'boot-agent', clientToken: 'boot-client' }, registry);
  const down = async () => { throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:3306'), { code: 'ECONNREFUSED' }); };
  const { lookupToken } = registry;
  registry.lookupToken = down;
  registry.anyTokens = down;
  registry.touchToken = down;

  const agent = await h.startAgent(url, { name: 'bootstrapped', token: 'boot-agent' });
  await h.waitFor(() => h.allHealthy(agent), { what: 'env-token agent registered' });
  assert.equal(registry.nodes.get('bootstrapped').tokenId, null);
  const ws = await h.connect(`${url}/connect?node=bootstrapped&gateway_token=boot-client`);
  ws.send('still works');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'still works');

  let r = await refused(url, managed);
  assert.equal(r.code, 1013);
  assert.match(r.errors[0], /database unavailable/);
  assert.equal((await refused(url, null)).code, 4401); // the env token makes a token mandatory anyway
  assert.equal(await clientStatus(url, `?gateway_token=${managedClient}`), 503);
  assert.equal(await clientStatus(url), 401);

  // An agent with a managed token keeps retrying through the outage and gets in once the database is back.
  const waiting = await h.startAgent(url, { name: 'waiting', token: managed });
  const codes = [];
  waiting.on('disconnected', (code) => codes.push(code));
  await h.waitFor(() => codes.length >= 2, { what: 'refused during the outage' });
  assert.ok(codes.every((c) => c === 1013));
  assert.match(waiting.lastError, /1013/);
  registry.lookupToken = lookupToken;
  await h.waitFor(() => waiting.registered, { what: 'registered after the outage' });
});

test('with no env token and the database down: the last known switch holds, an unknown one fails closed', async () => {
  const down = async () => { throw new Error('ECONNREFUSED'); };

  // The database answered at start-up (no tokens), then went away: auth stays off.
  let registry = new MemoryRegistry();
  let hub = await h.startHub({}, registry);
  registry.anyTokens = down;
  registry.lookupToken = down;
  let ws = await h.rawAgent(hub.url);
  assert.equal((await h.register(ws, 'no-auth')).type, 'registered');
  assert.equal(await clientStatus(hub.url), 404);
  // A presented token can't be looked up, and might be a revoked one.
  assert.equal((await refused(hub.url, mint())).code, 1013);

  // The database never answered: nothing gets in until it does.
  registry = new MemoryRegistry();
  registry.anyTokens = down;
  hub = await h.startHub({}, registry);
  assert.equal((await refused(hub.url, null)).code, 1013);
  assert.equal(await clientStatus(hub.url), 503);
  delete registry.anyTokens; // back to the prototype method
  ws = await h.rawAgent(hub.url);
  assert.equal((await h.register(ws, 'after')).type, 'registered');
});

test('revoking a token disconnects its agent within TOKEN_RECHECK_S, and the agent keeps retrying', async () => {
  const registry = new MemoryRegistry();
  const token = mint();
  const row = registry.addToken(token);
  const soon = mint();
  const soonRow = registry.addToken(soon, { expiresAt: iso(60000) });
  // The cache is long on purpose: the re-check must bypass it.
  const { url, hub } = await h.startHub({ tokenRecheckMs: 300, tokenCacheMs: 30000 }, registry);
  const agent = await h.startAgent(url, { name: 'revokee', token });
  const other = await h.startAgent(url, { name: 'lapses', token: soon });
  await h.waitFor(() => agent.registered && other.registered, { what: 'both registered' });
  const codes = [];
  agent.on('disconnected', (code) => codes.push(code));

  row.revokedAt = iso(0);
  const revokedAt = Date.now();
  await h.waitFor(() => codes.length >= 1, { timeout: 3000, what: 'revoked agent kicked' });
  assert.ok(Date.now() - revokedAt < 1500, `kicked after ${Date.now() - revokedAt} ms`);
  assert.equal(codes[0], 4401);
  assert.match(agent.lastError, /4401/);
  await h.waitFor(() => registry.nodes.get('revokee').online === 0, { what: 'revokee offline' });
  await h.waitFor(() => codes.length >= 3, { what: 'keeps retrying' });
  assert.ok(codes.every((c) => c === 4401));
  assert.equal(agent.registered, false);
  assert.equal(other.registered, true, 'the other token is untouched');

  // An expiry that passes while connected is caught the same way.
  const otherCodes = [];
  other.on('disconnected', (code) => otherCodes.push(code));
  soonRow.expiresAt = iso(200);
  await h.waitFor(() => otherCodes.length >= 1, { timeout: 3000, what: 'expired agent kicked' });
  assert.equal(otherCodes[0], 4401);
  await h.waitFor(() => hub.nodes.size === 0, { what: 'both nodes gone from the hub' });
});

test('an agent that connected without a token is closed once agent auth is switched on', async () => {
  const registry = new MemoryRegistry();
  const { url, hub } = await h.startHub({ tokenRecheckMs: 300 }, registry);
  const agent = await h.startAgent(url, { name: 'early-bird' });
  await h.waitFor(() => agent.registered, { what: 'registered without a token' });
  const codes = [];
  agent.on('disconnected', (code) => codes.push(code));
  registry.addToken(mint());
  await h.waitFor(() => codes.length >= 1, { timeout: 3000, what: 'tokenless agent kicked' });
  assert.equal(codes[0], 4401);
  await h.sleep(400);
  assert.equal(hub.nodes.size, 0);
});

test('a database error during the re-check disconnects nobody', async () => {
  const registry = new MemoryRegistry();
  const token = mint();
  registry.addToken(token);
  const { url, hub } = await h.startHub({ tokenRecheckMs: 200 }, registry);
  const agent = await h.startAgent(url, { name: 'steady', token });
  await h.waitFor(() => agent.registered, { what: 'registered' });
  registry.lookupToken = async () => { throw new Error('ETIMEDOUT'); };
  registry.anyTokens = registry.lookupToken;
  await h.sleep(800);
  assert.equal(agent.registered, true);
  assert.ok(hub.nodes.has('steady'));
});

test('lastUsedAt is written at most once per interval per token', async () => {
  const registry = new MemoryRegistry();
  const agentToken = mint();
  const clientToken = mint('client');
  const agentRow = registry.addToken(agentToken);
  const clientRow = registry.addToken(clientToken, { kind: 'client' });
  const touches = [];
  const touch = registry.touchToken.bind(registry);
  registry.touchToken = (id) => {
    touches.push(id);
    return touch(id);
  };
  const { url } = await h.startHub({ tokenTouchMs: 1500 }, registry);
  const agent = await h.startAgent(url, { name: 'busy', token: agentToken });
  await h.waitFor(() => h.allHealthy(agent), { what: 'ready' });
  assert.deepEqual(touches, [agentRow.id]);
  touches.length = 0;

  for (let i = 0; i < 4; i++) {
    const ws = await h.connect(`${url}/connect?node=busy`, { 'x-gateway-token': clientToken });
    ws.send('x');
    await h.nextMessage(ws);
    ws.close();
  }
  const count = (id) => touches.filter((t) => t === id).length;
  assert.equal(count(clientRow.id), 1);
  assert.ok(count(agentRow.id) <= 1, 'four data channels write lastUsedAt at most once');
  assert.match(clientRow.lastUsedAt, /Z$/);

  await h.sleep(1600);
  assert.equal(await clientStatus(url, '', { 'x-gateway-token': clientToken }), 404);
  assert.equal(count(clientRow.id), 2);

  // A failed write is logged, not thrown, and retried on the next use instead of waiting out the interval.
  registry.touchToken = async (id) => {
    touches.push(id);
    throw new Error('write failed');
  };
  await h.sleep(1600);
  assert.equal(await clientStatus(url, `?gateway_token=${clientToken}`), 404);
  await h.sleep(20);
  assert.equal(await clientStatus(url, `?gateway_token=${clientToken}`), 404);
  assert.equal(count(clientRow.id), 4);
});

test('managed client tokens work in the header and the query, are stripped, and revocation stops only new sessions', async () => {
  const registry = new MemoryRegistry();
  const clientToken = mint('client');
  const clientRow = registry.addToken(clientToken, { kind: 'client' });
  const expired = mint('client');
  registry.addToken(expired, { kind: 'client', expiresAt: iso(-1000) });
  const agentToken = mint();
  registry.addToken(agentToken);
  const { url } = await h.startHub({ tokenCacheMs: 100 }, registry);
  const agent = await h.startAgent(url, { name: 'client-node', token: agentToken });
  await h.waitFor(() => h.allHealthy(agent), { what: 'ready' });

  const viaQuery = await h.connect(`${url}/connect?node=client-node&a=1&gateway_token=${clientToken}`);
  viaQuery.send('info');
  let info = JSON.parse((await h.nextMessage(viaQuery)).data);
  assert.equal(info.url, '/?a=1');
  const viaHeader = await h.connect(`${url}/connect?node=client-node`, { 'x-gateway-token': clientToken });
  viaHeader.send('info');
  info = JSON.parse((await h.nextMessage(viaHeader)).data);
  assert.equal(info.url, '/');
  assert.equal(info.headers['x-gateway-token'], undefined);

  await assert.rejects(h.connect(`${url}/connect?node=client-node`), (err) => err.status === 401 && /missing or bad/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?node=client-node&gateway_token=${agentToken}`), (err) => err.status === 401 && /not a client token/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?node=client-node&gateway_token=${expired}`), (err) => err.status === 401 && /is expired/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?node=client-node&gateway_token=${mint('client')}`), (err) => err.status === 401 && !err.body.includes('pwgc_'));

  clientRow.revokedAt = iso(0);
  await h.sleep(200);
  await assert.rejects(h.connect(`${url}/connect?node=client-node`, { 'x-gateway-token': clientToken }), (err) => err.status === 401 && /is revoked/.test(err.body));
  viaQuery.send('still open');
  assert.equal((await h.nextMessage(viaQuery)).data.toString(), 'still open');
});

test('token lookups are cached: one query per burst, positives for TOKEN_CACHE_S, negatives for less', async () => {
  const registry = new MemoryRegistry();
  const token = mint('client');
  const row = registry.addToken(token, { kind: 'client' });
  const lookups = [];
  const switches = [];
  const lookup = registry.lookupToken.bind(registry);
  const any = registry.anyTokens.bind(registry);
  registry.lookupToken = async (hash) => {
    lookups.push(hash);
    await h.sleep(30); // a slow database, so the burst overlaps
    return lookup(hash);
  };
  registry.anyTokens = async (kind) => {
    switches.push(kind);
    return any(kind);
  };
  const { url } = await h.startHub({ tokenCacheMs: 800, tokenNegativeCacheMs: 300, tokenRecheckMs: 60000 }, registry);
  const switchesAtStart = switches.length;

  const burst = await Promise.all(Array.from({ length: 10 }, () => clientStatus(url, `?gateway_token=${token}`)));
  assert.deepEqual(burst, Array(10).fill(404));
  assert.equal(lookups.length, 1);
  assert.equal(lookups[0], hashToken(token));

  // A revocation is seen once the positive entry expires.
  row.revokedAt = iso(0);
  assert.equal(await clientStatus(url, `?gateway_token=${token}`), 404);
  await h.sleep(850);
  assert.equal(await clientStatus(url, `?gateway_token=${token}`), 401);
  assert.equal(lookups.length, 2);

  // Unknown tokens are cached too, but briefly: a token created meanwhile works soon after. (Another
  // active client token keeps client auth switched on.)
  registry.addToken(mint('client'), { kind: 'client' });
  await h.sleep(850);
  const fresh = mint('client');
  assert.equal(await clientStatus(url, `?gateway_token=${fresh}`), 401);
  registry.addToken(fresh, { kind: 'client' });
  assert.equal(await clientStatus(url, `?gateway_token=${fresh}`), 401);
  await h.sleep(350);
  assert.equal(await clientStatus(url, `?gateway_token=${fresh}`), 404);
  assert.equal(lookups.filter((x) => x === hashToken(fresh)).length, 2);

  // The enforcement switch is cached as well.
  const before = switches.length;
  await Promise.all(Array.from({ length: 5 }, () => clientStatus(url)));
  assert.ok(switches.length - before <= 1, `${switches.length - before} switch queries for a burst`);
  assert.ok(switchesAtStart >= 1);
});

test('a data channel must carry the token its control connection used', async () => {
  const registry = new MemoryRegistry();
  const mine = mint();
  const theirs = mint();
  registry.addToken(mine);
  registry.addToken(theirs);
  const { url, hub } = await h.startHub({ openTimeoutMs: 5000 }, registry);
  const raw = await h.rawAgent(url, bearer(mine));
  await h.register(raw, 'strict');

  const opened = h.nextMessage(raw).then(({ data }) => JSON.parse(data));
  const client = h.connect(`${url}/connect?node=strict`);
  const { id } = await opened;
  await assert.rejects(h.connect(`${url}/agent/data/${id}`, { Authorization: `Bearer ${theirs}` }), { status: 401 });
  await assert.rejects(h.connect(`${url}/agent/data/${id}`), { status: 401 });
  assert.equal(hub.pending.size, 1, 'a refused data channel leaves the session pending');
  const data = await h.connect(`${url}/agent/data/${id}`, { Authorization: `Bearer ${mine}` });
  const ws = await client;
  data.send('over the tunnel');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'over the tunnel');
});

// A token check on its own, as the hub builds it, with every lookup it costs written down.
function tokenAuth(registry, options = {}) {
  const lookups = [];
  const lookup = registry.lookupToken.bind(registry);
  registry.lookupToken = async (hash) => {
    lookups.push(hash);
    return lookup(hash);
  };
  return { lookups, tokens: createTokenAuth({ registry, log: silent, cacheMs: 0, ...options }) };
}

// What /connect answers a token pinned to a node nobody serves: { status, body } (404 means it was let in).
function clientAnswer(url, token) {
  return h.connect(`${url}/connect?node=nobody&gateway_token=${token}`).then(() => ({ status: 101, body: '' }), (err) => ({ status: err.status, body: err.body }));
}

const NO_SESSIONS = 'not allowed to open browser sessions (its scope leaves that out)';
const SCOPE_CHANGED = 'not usable: its scope was lost or changed in the database';
const STORED_TWICE = 'not usable: it is stored twice, with and without a scope';

test('a client token costs one lookup, with a scope or without, and so does a made-up one', async () => {
  const registry = new MemoryRegistry();
  const plain = mint('client');
  const scoped = mint('client');
  const madeUp = mint('client');
  registry.addToken(plain, { kind: 'client' });
  registry.addToken(scoped, { kind: 'client', scope: 'sessions.open' });
  const { tokens, lookups } = tokenAuth(registry, { cacheMs: 10000 });

  assert.equal((await tokens.check('client', plain)).ok, true);
  assert.deepEqual(lookups, [hashToken(plain)]);
  assert.equal((await tokens.check('client', scoped)).ok, true);
  assert.deepEqual(lookups, [hashToken(plain), hashToken(scoped)], 'asked for by the plain hash, once');
  const verdict = await tokens.check('client', madeUp);
  assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, undefined, undefined]);
  assert.match(verdict.message, /^unknown token pwgc_/);
  assert.deepEqual(lookups, [hashToken(plain), hashToken(scoped), hashToken(madeUp)]);

  // Each answer is cached as before, the negative one too.
  for (const token of [plain, scoped, madeUp]) await tokens.check('client', token);
  assert.equal(lookups.length, 3);
});

test('a scoped token with sessions.open is let in', async () => {
  const registry = new MemoryRegistry();
  const { tokens } = tokenAuth(registry);
  for (const scope of ['sessions.open', 'grid.view sessions.open', 'grid.view mfa.use sessions.open signins.add signins.use', 'future.thing sessions.open']) {
    const token = mint('client');
    const row = registry.addToken(token, { kind: 'client', name: 'ci', scope });
    assert.equal(row.tokenHash, scopedHash(hashToken(token)));
    assert.deepEqual(await tokens.check('client', token),
      { ok: true, via: 'managed', tokenId: row.id, nodeName: null, label: `token ${token.slice(0, 12)}... (#${row.id} "ci")` }, scope);
    await h.waitFor(() => row.lastUsedAt, { what: 'the use recorded' });
  }
  const { url } = await h.startHub({}, registry);
  const token = mint('client');
  registry.addToken(token, { kind: 'client', scope: 'grid.view sessions.open' });
  assert.equal(await clientStatus(url, `?gateway_token=${token}`), 404);
  assert.equal(await clientStatus(url, '', { 'x-gateway-token': token }), 404);
});

test('a scoped token without sessions.open is refused as forbidden', async () => {
  const registry = new MemoryRegistry();
  const token = mint('client');
  const row = registry.addToken(token, { kind: 'client', scope: 'grid.view mfa.use signins.add signins.use' });
  const { tokens } = tokenAuth(registry);
  const verdict = await tokens.check('client', token);
  assert.deepEqual(verdict, {
    ok: false, problem: NO_SESSIONS, forbidden: true,
    message: `client token ${token.slice(0, 12)}... (#${row.id}) is ${NO_SESSIONS}`,
  });
  assert.equal(verdict.message.includes(token), false, 'the full token is never echoed');
  assert.equal(tokenProblem(await registry.lookupToken(hashToken(token)), 'client'), NO_SESSIONS);
  // A refused token is not recorded as used.
  await h.sleep(50);
  assert.equal(row.lastUsedAt, null);
});

test('an empty scope and a scope of unknown ids are refused as forbidden', async () => {
  const registry = new MemoryRegistry();
  const { tokens } = tokenAuth(registry);
  const { url } = await h.startHub({ tokenCacheMs: 0 }, registry);
  for (const scope of ['', ' ', 'future.thing', 'future.thing other.thing']) {
    const token = mint('client');
    registry.addToken(token, { kind: 'client', scope });
    const verdict = await tokens.check('client', token);
    assert.deepEqual([verdict.ok, verdict.forbidden, verdict.problem], [false, true, NO_SESSIONS], JSON.stringify(scope));
    assert.deepEqual(await clientAnswer(url, token), { status: 403, body: `gateway token is ${NO_SESSIONS}\n` });
  }
});

test('scope ids are matched whole', async () => {
  const registry = new MemoryRegistry();
  const { tokens } = tokenAuth(registry);
  const verdictFor = (scope) => {
    const token = mint('client');
    registry.addToken(token, { kind: 'client', scope });
    return tokens.check('client', token);
  };
  for (const scope of ['sessions.openx', 'xsessions.open', 'sessions.open.more', 'sessions', 'sessions.', '.open', 'sessions open',
    'SESSIONS.OPEN', 'Sessions.Open', 'sessions.open,grid.view', 'grid.view,sessions.open', 'sessions.open;', '"sessions.open"', 'sessions_open']) {
    const verdict = await verdictFor(scope);
    assert.deepEqual([verdict.ok, verdict.forbidden], [false, true], scope);
  }
  // The stored form is ids with one space between them; the word is found among them wherever it stands.
  for (const scope of ['sessions.open', 'grid.view sessions.open', 'sessions.open signins.use', 'mfa.use sessions.open signins.use', 'sessions.openx sessions.open']) {
    assert.equal((await verdictFor(scope)).ok, true, scope);
  }
});

test('a scoped row whose scope was set to null is refused', async () => {
  const registry = new MemoryRegistry();
  const token = mint('client');
  const row = registry.addToken(token, { kind: 'client', scope: 'grid.view sessions.open' });
  const other = mint('client');
  registry.addToken(other, { kind: 'client', scope: 'sessions.open' });
  const { tokens } = tokenAuth(registry);
  const { url } = await h.startHub({ tokenCacheMs: 0 }, registry);
  assert.equal(await clientStatus(url, `?gateway_token=${token}`), 404);

  row.scope = null; // as an UPDATE by hand, or a lost column, leaves it
  assert.deepEqual(await registry.lookupToken(hashToken(token)),
    { id: row.id, name: 'test token', kind: 'client', prefix: token.slice(0, 12), nodeName: null, expiresAt: null, revokedAt: null, scoped: true, scope: null, storedTwice: false });
  const verdict = await tokens.check('client', token);
  assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, SCOPE_CHANGED, false]);
  // 401, as a token that is wrong: it is never taken for one with no limit.
  assert.deepEqual(await clientAnswer(url, token), { status: 401, body: `gateway token is ${SCOPE_CHANGED}\n` });
  assert.equal(await clientStatus(url, `?gateway_token=${other}`), 404, 'other scoped tokens are untouched');
});

test('a plain row that was given a scope is refused', async () => {
  const registry = new MemoryRegistry();
  const { tokens } = tokenAuth(registry);
  const { url } = await h.startHub({ tokenCacheMs: 0 }, registry);
  for (const scope of ['sessions.open', 'grid.view sessions.open', 'grid.view', '']) {
    const token = mint('client');
    const row = registry.addToken(token, { kind: 'client' });
    assert.equal(await clientStatus(url, `?gateway_token=${token}`), 404);
    row.scope = scope; // the hash stays the plain one
    const found = await registry.lookupToken(hashToken(token));
    assert.deepEqual([found.scoped, found.scope, found.storedTwice], [false, scope, false]);
    const verdict = await tokens.check('client', token);
    assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, SCOPE_CHANGED, false], JSON.stringify(scope));
    assert.deepEqual(await clientAnswer(url, token), { status: 401, body: `gateway token is ${SCOPE_CHANGED}\n` });
  }
});

test('a token stored both ways is refused', async () => {
  for (const order of [[null, 'sessions.open'], ['sessions.open', null]]) {
    const registry = new MemoryRegistry();
    const token = mint('client');
    for (const scope of order) registry.addToken(token, { kind: 'client', scope });
    const rows = [...registry.tokens.values()];
    assert.deepEqual(rows.map((row) => row.tokenHash).sort(), [hashToken(token), scopedHash(hashToken(token))].sort());
    assert.equal((await registry.lookupToken(hashToken(token))).storedTwice, true);
    const { tokens, lookups } = tokenAuth(registry);
    const verdict = await tokens.check('client', token);
    assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, STORED_TWICE, false]);
    assert.equal(lookups.length, 1);
    // 401, not 403: neither row is believed.
    const { url } = await h.startHub({ tokenCacheMs: 0 }, registry);
    assert.deepEqual(await clientAnswer(url, token), { status: 401, body: `gateway token is ${STORED_TWICE}\n` });
    // Revoking one of the two does not bring the other back.
    rows[0].revokedAt = iso(-1000);
    assert.equal((await tokens.check('client', token)).problem, STORED_TWICE);
    rows[0].revokedAt = null;
    rows[1].revokedAt = iso(-1000);
    assert.equal((await tokens.check('client', token)).problem, STORED_TWICE);
  }
});

test('a scoped client token is not an agent token at /agent, and an agent row with a scope or in the scoped form is refused', async () => {
  const registry = new MemoryRegistry();
  const client = mint('client');
  registry.addToken(client, { kind: 'client', scope: 'grid.view sessions.open' });
  const given = mint(); // a good agent row that someone gave a scope
  registry.addToken(given).scope = 'sessions.open';
  const scopedAgent = mint(); // an agent row made as a scoped token is made
  registry.addToken(scopedAgent, { scope: 'sessions.open' });
  const formOnly = mint(); // an agent row in the scoped form whose scope is gone
  registry.addToken(formOnly, { scope: '' }).scope = null;
  const good = mint();
  registry.addToken(good);
  const { url, hub } = await h.startHub({}, registry);

  let r = await refused(url, client);
  assert.equal(r.code, 4401);
  assert.match(r.errors[0], /client token pwgc_.{7}\.\.\. \(#\d+\) is not an agent token$/);
  for (const token of [given, scopedAgent, formOnly]) {
    r = await refused(url, token);
    assert.equal(r.code, 4401);
    assert.match(r.errors[0], /agent token pwga_.{7}\.\.\. \(#\d+\) is not usable: its scope was lost or changed in the database$/);
    assert.equal(r.errors[0].includes(token), false, 'the full token is never echoed');
    assert.equal(tokenProblem(await registry.lookupToken(hashToken(token)), 'agent'), SCOPE_CHANGED);
  }
  assert.deepEqual([...hub.nodes.keys()], []);
  const ok = await h.rawAgent(url, bearer(good));
  assert.deepEqual(await h.register(ok, 'unscoped'), { type: 'registered', name: 'unscoped' });
  // And an agent token is still not a client token, whatever its row says.
  for (const token of [given, scopedAgent, good]) {
    assert.deepEqual(await clientAnswer(url, token), { status: 401, body: 'gateway token is not a client token\n' });
  }
});

test('revoked and expired come before the scope verdicts', async () => {
  const row = (extra) => ({ id: 1, name: 't', kind: 'client', prefix: 'pwgc_AbCdEfG', nodeName: null, expiresAt: null, revokedAt: null, scoped: true, scope: 'grid.view', storedTwice: false, ...extra });
  const now = Date.parse('2026-01-01T00:00:00Z');
  assert.equal(tokenProblem(row({}), 'client', now), NO_SESSIONS);
  assert.equal(tokenProblem(row({ revokedAt: '2025-12-31T00:00:00Z' }), 'client', now), 'revoked');
  assert.equal(tokenProblem(row({ expiresAt: '2025-12-31T23:59:59Z' }), 'client', now), 'expired');
  assert.equal(tokenProblem(row({ expiresAt: '2026-01-01T00:00:01Z' }), 'client', now), NO_SESSIONS, 'a future expiry is not one');
  assert.equal(tokenProblem(row({ scope: null, revokedAt: '2025-12-31T00:00:00Z' }), 'client', now), 'revoked');
  assert.equal(tokenProblem(row({ scope: null, expiresAt: '2025-12-31T23:59:59Z' }), 'client', now), 'expired');
  assert.equal(tokenProblem(row({ scope: null }), 'client', now), SCOPE_CHANGED);
  assert.equal(tokenProblem(row({}), 'agent', now), 'not an agent token');
  assert.equal(tokenProblem(row({ storedTwice: true, revokedAt: '2025-12-31T00:00:00Z' }), 'client', now), STORED_TWICE, 'stored twice comes before everything');
  assert.equal(tokenProblem(row({ scope: 'sessions.open' }), 'client', now), null);
  // A row built without the new fields reads as a plain row with no scope.
  assert.equal(tokenProblem({ id: 1, kind: 'client', revokedAt: null, expiresAt: null }, 'client', now), null);
  assert.equal(tokenProblem(null, 'client', now), 'unknown');

  // Through the hub: 401 with the sentences there were before, never the 403 of a good token.
  const registry = new MemoryRegistry();
  const revoked = mint('client');
  const expired = mint('client');
  registry.addToken(revoked, { kind: 'client', scope: 'grid.view', revokedAt: iso(-60000) });
  registry.addToken(expired, { kind: 'client', scope: 'grid.view', expiresAt: iso(-1000) });
  const { tokens } = tokenAuth(registry);
  const { url } = await h.startHub({ tokenCacheMs: 0 }, registry);
  for (const [token, problem] of [[revoked, 'revoked'], [expired, 'expired']]) {
    const verdict = await tokens.check('client', token);
    assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, problem, false]);
    assert.deepEqual(await clientAnswer(url, token), { status: 401, body: `gateway token is ${problem}\n` });
  }
});

test('a failed lookup is unavailable, never a pass', async () => {
  const registry = new MemoryRegistry();
  const open = mint('client');
  const limited = mint('client');
  registry.addToken(open, { kind: 'client', scope: 'sessions.open' });
  registry.addToken(limited, { kind: 'client', scope: 'grid.view' });
  const { url } = await h.startHub({ tokenCacheMs: 0 }, registry);
  assert.equal(await clientStatus(url, `?gateway_token=${open}`), 404);
  registry.lookupToken = async () => { throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:3306'), { code: 'ECONNREFUSED' }); };
  const tokens = createTokenAuth({ registry, log: silent, cacheMs: 0 });
  for (const token of [open, limited]) {
    const verdict = await tokens.check('client', token);
    assert.deepEqual(verdict, { ok: false, unavailable: true, message: 'could not check the client token: connect ECONNREFUSED 127.0.0.1:3306' });
    assert.deepEqual(await tokens.checkHash('client', hashToken(token)), verdict);
    // 503 for both: the hub cannot tell which of them may open sessions, so it lets neither in and blames neither.
    assert.equal(await clientStatus(url, `?gateway_token=${token}`), 503);
  }
  // Where a missing token would be let in (GATEWAY_ALLOW_OPEN_CLIENTS, nothing active), one that is sent still isn't waved through.
  const allowed = new MemoryRegistry();
  const revoked = mint('client');
  allowed.addToken(revoked, { kind: 'client', scope: 'sessions.open', revokedAt: iso(-1000) });
  const lenient = createTokenAuth({ registry: allowed, log: silent, cacheMs: 0, allowOpen: { client: true } });
  assert.equal((await lenient.check('client', null)).via, 'open');
  allowed.lookupToken = async () => { throw new Error('ETIMEDOUT'); };
  assert.deepEqual(await lenient.check('client', revoked), { ok: false, unavailable: true, message: 'could not check the client token: ETIMEDOUT' });
});

test('checkHash re-checks a scoped token by its plain hash', async () => {
  const registry = new MemoryRegistry();
  const token = mint('client');
  const row = registry.addToken(token, { kind: 'client', name: 'ci', scope: 'grid.view sessions.open' });
  const { tokens, lookups } = tokenAuth(registry);
  const hash = hashToken(token); // what the hub keeps for a waiting client
  assert.notEqual(row.tokenHash, hash);

  assert.deepEqual(await tokens.checkHash('client', hash),
    { ok: true, via: 'managed', tokenId: row.id, nodeName: null, label: `token ${token.slice(0, 12)}... (#${row.id} "ci")` });
  assert.deepEqual(lookups, [hash]);
  row.scope = 'grid.view';
  let verdict = await tokens.checkHash('client', hash);
  assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, NO_SESSIONS, true]);
  row.scope = null;
  verdict = await tokens.checkHash('client', hash);
  assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, SCOPE_CHANGED, false]);
  row.scope = 'sessions.open';
  row.revokedAt = iso(0);
  verdict = await tokens.checkHash('client', hash);
  assert.deepEqual([verdict.ok, verdict.problem, verdict.forbidden], [false, 'revoked', false]);
  assert.deepEqual(lookups, [hash, hash, hash, hash], 'one lookup each time, always by the plain hash');
});

// A made-up token and its hashes, worked out with sha256 beforehand: the tests compare with these and derive them too.
const VECTOR = `pwgc_${'A'.repeat(43)}`;
const VECTOR_HASH = '1f0bc52f7fa82946861c65a388c72cfdd15561d997682cee0eba9fa2becc4bd0';
const VECTOR_SCOPED = 'scoped:1f0bc52f7fa82946861c65a388c72cfdd15561d997682cee0eba9fa2b';
const sha256 = (text) => crypto.createHash('sha256').update(text, 'utf8').digest('hex');

// How a hub from before scopes (commit d05b719) finds a client's row, kept here as it was: check() refuses
// anything that is not of the managed shape without a query, and lookupToken compares tokenHash with the
// sha256 of the string and nothing else. It must not call today's lookup.
function releasedLookup(registry, presented) {
  if (!isManagedToken(presented)) return null;
  const hash = hashToken(presented);
  return [...registry.tokens.values()].find((row) => row.tokenHash === hash) || null;
}

test('a hub from before scopes finds no row for a scoped token', async () => {
  for (const scoped of [VECTOR, mint('client')]) {
    const registry = new MemoryRegistry();
    const row = registry.addToken(scoped, { kind: 'client', scope: 'sessions.open' });
    const plain = mint('client');
    const plainRow = registry.addToken(plain, { kind: 'client' });
    const presentedAs = [scoped, `scoped:${scoped}`, `scoped:scoped:${scoped}`];

    for (const presented of presentedAs) assert.equal(releasedLookup(registry, presented), null);
    // Why: the two with scoped: in front are not of the managed shape, and the token has no row under its
    // sha256. Even a hub that hashed whatever it was sent would find none of the three.
    assert.deepEqual(presentedAs.map(isManagedToken), [true, false, false]);
    for (const presented of presentedAs) assert.notEqual(row.tokenHash, sha256(presented));
    // The same lookup finds a token without a scope, so it is the right lookup; and today's hub finds both.
    assert.equal(releasedLookup(registry, plain), plainRow);
    assert.equal((await registry.lookupToken(hashToken(scoped))).id, row.id);
    assert.equal((await registry.lookupToken(hashToken(plain))).id, plainRow.id);
  }
  assert.equal(sha256(`scoped:${VECTOR}`), 'c9795951395e62fd4872f788f64603b879af3dcdb0954109b0d77aad5eeabd7a');
  assert.equal(sha256(`scoped:scoped:${VECTOR}`), '4f2831d72fbb1af615980c4c88bd8c9a5759c08cf2a3964c720a0cd57c7ed067');
});

test('the scoped form is not 64 hex characters', () => {
  const registry = new MemoryRegistry();
  const scoped = registry.addToken(VECTOR, { kind: 'client', scope: 'sessions.open' });
  assert.equal(scoped.tokenHash.length, 64);
  assert.ok(scoped.tokenHash.startsWith('scoped:'));
  assert.doesNotMatch(scoped.tokenHash, /^[0-9a-f]{64}$/);
  assert.equal(scoped.tokenHash, VECTOR_SCOPED);
  assert.equal(scoped.tokenHash, `scoped:${sha256(VECTOR).slice(0, 57)}`);
  assert.equal(hashToken(VECTOR), VECTOR_HASH);
  // An empty scope is a scope: the form says so even when the scope allows nothing.
  assert.ok(registry.addToken(mint('client'), { kind: 'client', scope: '' }).tokenHash.startsWith('scoped:'));
  const plain = registry.addToken(mint('client'), { kind: 'client' });
  assert.match(plain.tokenHash, /^[0-9a-f]{64}$/);
  assert.equal(plain.scope, null);
});
