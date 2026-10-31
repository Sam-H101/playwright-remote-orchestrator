'use strict';

// Managed access tokens (accessTokens, administered from the Grid UI) next to the env bootstrap tokens.

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { test, afterEach } = require('node:test');
const WebSocket = require('ws');
const h = require('./helpers');
const { createHub } = require('../hub');
const { MemoryRegistry } = require('../lib/registry');
const { hashToken } = require('../lib/protocol');

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

// A client upgrade to a node nobody serves: 404 means the token was accepted, 401/503 that it wasn't.
function clientStatus(url, query = '', headers = {}) {
  return h.connect(`${url}/connect/nobody/1${query}`, headers).then(() => 101, (err) => err.status);
}

test('a managed agent token registers the node, is recorded on it, and turns agent auth on', async () => {
  const registry = new MemoryRegistry();
  const token = mint();
  const row = registry.addToken(token, { name: 'build farm' });
  assert.deepEqual(await registry.lookupToken(hashToken(token)),
    { id: row.id, name: 'build farm', kind: 'agent', prefix: token.slice(0, 12), nodeName: null, expiresAt: null, revokedAt: null });
  assert.equal(await registry.lookupToken(hashToken(`${token}x`)), null);

  const { url, hub } = await h.startHub({}, registry);
  const agent = await h.startAgent(url, { name: 'Managed-1', token });
  await h.waitFor(() => h.allHealthy(agent), { what: 'managed agent registered' });
  assert.equal(registry.nodes.get('managed-1').tokenId, row.id);
  assert.equal(hub.nodes.get('managed-1').auth.via, 'managed');
  assert.match(row.lastUsedAt, /Z$/);

  // A session runs over a data channel that carries the same managed token.
  const ws = await h.connect(`${url}/connect/managed-1/1`);
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
  const ws = await h.connect(`${url}/connect/bootstrapped/1?gateway_token=boot-client`);
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
    const ws = await h.connect(`${url}/connect/busy/${(i % 2) + 1}`, { 'x-gateway-token': clientToken });
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

  const viaQuery = await h.connect(`${url}/connect/client-node/1?a=1&gateway_token=${clientToken}`);
  viaQuery.send('info');
  let info = JSON.parse((await h.nextMessage(viaQuery)).data);
  assert.equal(info.url, '/?a=1');
  const viaHeader = await h.connect(`${url}/connect/client-node/2`, { 'x-gateway-token': clientToken });
  viaHeader.send('info');
  info = JSON.parse((await h.nextMessage(viaHeader)).data);
  assert.equal(info.url, '/');
  assert.equal(info.headers['x-gateway-token'], undefined);

  await assert.rejects(h.connect(`${url}/connect/client-node/1`), (err) => err.status === 401 && /missing or bad/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect/client-node/1?gateway_token=${agentToken}`), (err) => err.status === 401 && /not a client token/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect/client-node/1?gateway_token=${expired}`), (err) => err.status === 401 && /is expired/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect/client-node/1?gateway_token=${mint('client')}`), (err) => err.status === 401 && !err.body.includes('pwgc_'));

  clientRow.revokedAt = iso(0);
  await h.sleep(200);
  await assert.rejects(h.connect(`${url}/connect/client-node/1`, { 'x-gateway-token': clientToken }), (err) => err.status === 401 && /is revoked/.test(err.body));
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
  const client = h.connect(`${url}/connect/strict/1`);
  const { id } = await opened;
  await assert.rejects(h.connect(`${url}/agent/data/${id}`, { Authorization: `Bearer ${theirs}` }), { status: 401 });
  await assert.rejects(h.connect(`${url}/agent/data/${id}`), { status: 401 });
  assert.equal(hub.pending.size, 1, 'a refused data channel leaves the session pending');
  const data = await h.connect(`${url}/agent/data/${id}`, { Authorization: `Bearer ${mine}` });
  const ws = await client;
  data.send('over the tunnel');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'over the tunnel');
});
