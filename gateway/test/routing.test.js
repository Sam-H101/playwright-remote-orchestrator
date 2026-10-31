'use strict';

// /connect: the hub picks the slot (browser, Playwright version, ?node=), claims it in portUsage, queues
// clients while every matching slot is busy, fails over to another slot, and always gives the claim back.

const assert = require('node:assert/strict');
const http = require('node:http');
const { test, afterEach } = require('node:test');
const WebSocket = require('ws');
const h = require('./helpers');
const { createHub } = require('../hub');
const { MemoryRegistry } = require('../lib/registry');
const R = require('../lib/routing');

afterEach(h.cleanup);

// What run-server 1.64 answers a 1.50 client with (userAgentVersionMatchesErrorMessage), box and all.
const VERSION_BOX = ['╔═══════════════════════════════════════╗', '║ Playwright version mismatch:          ║',
  '║   - server version: v1.64             ║', '║   - client version: v1.50             ║', '╚═══════════════════════════════════════╝'].join('\n');

const UA = (version) => ({ 'user-agent': `Playwright/${version} (x64; linux 6.8) node/20.17` });
const browserHeaders = (browser, version = '1.64.1') => ({ 'x-playwright-browser': browser, ...UA(version) });

// An in-process agent: registers like agent.js and answers each open frame as told. 'echo' opens the
// data channel and echoes frames back, 'fail' answers open-failed, 'mute' never answers. failStatus is
// the HTTP status the open-failed frame says run-server answered.
async function fakeNode(url, name, { slots = 1, browsers = ['chromium'], playwrightVersion = '1.64.1', mode = 'echo', healthy = true, failStatus, oldAgent = false } = {}) {
  const ws = await h.rawAgent(url);
  const node = { ws, name, opens: [], channels: [], mode, failStatus };
  ws.on('message', (data) => {
    const msg = JSON.parse(data);
    if (msg.type !== 'open') return;
    node.opens.push(msg);
    if (node.mode === 'fail') {
      const message = node.failStatus === 428 ? `run-server on port 3100 answered HTTP 428: ${VERSION_BOX}` : 'run-server refused';
      return ws.send(JSON.stringify({ type: 'open-failed', id: msg.id, message, status: oldAgent ? undefined : node.failStatus }));
    }
    if (node.mode === 'mute') return;
    const channel = new WebSocket(`${url}/agent/data/${msg.id}`);
    h.cleanups.push(async () => channel.terminate());
    channel.on('error', () => {});
    channel.on('message', (d, isBinary) => {
      if (!isBinary && d.toString() === 'whoami') return channel.send(JSON.stringify({ node: name, slot: msg.slot, query: msg.query, headers: msg.headers }));
      channel.send(d, { binary: isBinary });
    });
    node.channels.push(channel);
  });
  const reply = h.register(ws, name, slots, { browsers, playwrightVersion });
  const msg = await reply;
  assert.equal(msg.type, 'registered');
  if (!healthy) ws.send(JSON.stringify({ type: 'heartbeat', slots: Array.from({ length: slots }, (_, i) => ({ index: i + 1, healthy: false })) }));
  return node;
}

async function whoami(ws) {
  ws.send('whoami');
  return JSON.parse((await h.nextMessage(ws)).data);
}

// Captures what the hub logs, so tests can check the routing lines (and that no token is in them).
function recorder() {
  const lines = [];
  const push = (level) => (...args) => lines.push(`${level} ${args.join(' ')}`);
  return { lines, log: { info: push('info'), warn: push('warn'), error: push('error') } };
}

async function startHub(overrides = {}, registry = new MemoryRegistry(), log) {
  if (!log) return h.startHub({ queuePollMs: 100, ...overrides }, registry);
  const hub = createHub(h.hubConfig({ queuePollMs: 100, ...overrides }), { log, registry });
  const port = await hub.start();
  h.cleanups.push(() => hub.stop());
  return { hub, port, registry, url: `ws://127.0.0.1:${port}` };
}

const slotRow = (registry, server, slot) => registry.slots.get(server).get(slot);
const claimed = (registry) => [...registry.slots.values()].flatMap((m) => [...m.values()]).filter((s) => s.inUse);

// A stand-in for a legacy static host: a bare run-server-like WebSocket server on 127.0.0.1.
async function staticHost({ refuse = null } = {}) {
  const server = http.createServer((req, res) => res.writeHead(404).end());
  const wss = new WebSocket.WebSocketServer({ noServer: true });
  const seen = [];
  server.on('upgrade', (req, socket, head) => {
    seen.push({ url: req.url, headers: req.headers });
    if (refuse) {
      const body = refuse === 428 ? VERSION_BOX : 'Forbidden';
      return socket.end(`HTTP/1.1 ${refuse} ${http.STATUS_CODES[refuse]}\r\nConnection: close\r\nContent-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);
    }
    wss.handleUpgrade(req, socket, head, (ws) => {
      ws.on('message', (d, isBinary) => {
        if (!isBinary && d.toString() === 'whoami') return ws.send(JSON.stringify({ node: 'static', url: req.url, headers: req.headers }));
        ws.send(d, { binary: isBinary });
      });
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  h.cleanups.push(async () => {
    for (const ws of wss.clients) ws.terminate();
    server.close();
  });
  return { port: server.address().port, seen, wss };
}

// A free port with nothing listening on it.
async function closedPort() {
  const server = http.createServer();
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address();
  await new Promise((r) => server.close(r));
  return port;
}

test('routing helpers: what the client asks for, versions, browser families, choice', () => {
  const params = (q) => new URLSearchParams(q);
  // run-server reads ?browser= before the header, so the hub does too.
  assert.deepEqual(R.clientWants(params('browser=firefox'), { 'x-playwright-browser': 'chromium', ...UA('1.64.1') }),
    { want: { browser: 'firefox', version: '1.64', node: null } });
  assert.deepEqual(R.clientWants(params('node=Runner-7'), { 'x-playwright-browser': 'WebKit' }),
    { want: { browser: 'webkit', version: null, node: 'runner-7' } });
  assert.deepEqual(R.clientWants(params(''), {}), { want: { browser: null, version: null, node: null } });
  assert.match(R.clientWants(params('node=a/b'), {}).error, /node must be/);
  assert.match(R.clientWants(params('node='), {}).error, /node must be/);
  assert.match(R.clientWants(params(''), { 'x-playwright-browser': 'x y' }).error, /bad browser/);
  assert.equal(R.clientWants(params(''), { 'x-playwright-browser': '_bidiFirefox' }).want.browser, '_bidifirefox');

  // The User-Agent Playwright's connect sends, from JS and from the Python driver.
  assert.equal(R.clientVersion('Playwright/1.64.1 (x64; windows 10.0) node/20.17'), '1.64');
  assert.equal(R.clientVersion('Playwright/1.64.0-beta-1791568591000 (x64; ubuntu 24.04) python/3.12'), '1.64');
  assert.equal(R.clientVersion('Mozilla/5.0'), null);
  assert.equal(R.clientVersion(undefined), null);
  assert.equal(R.majorMinor('1.64.1'), '1.64');
  assert.equal(R.majorMinor('1.65.0-alpha-2026'), '1.65');
  assert.equal(R.majorMinor('unknown'), null);
  assert.equal(R.majorMinor(null), null);

  assert.equal(R.offers(['chromium', 'firefox'], 'firefox'), true);
  assert.equal(R.offers(['chromium'], 'firefox'), false);
  assert.equal(R.offers(['chrome'], 'chromium'), true, 'a Chromium channel serves chromium');
  assert.equal(R.offers(['msedge-beta'], 'chromium'), true);
  assert.equal(R.offers(['chromium-headless-shell'], 'chromium'), true);
  assert.equal(R.offers(['chromium'], 'chrome'), false);
  assert.equal(R.offers([], 'webkit'), true, 'a server that lists nothing takes anything');
  assert.equal(R.offers(['firefox'], null), true, 'a client that names no browser takes anything');

  // Gateway nodes first, then the largest share of free slots, random among equals.
  const gw = (name, total) => ({ name, gateway: true, connected: true, agent: { slots: Array(total).fill({ healthy: true }) }, slots: [] });
  const a = gw('a', 4);
  const b = gw('b', 2);
  const st = { name: 'static', gateway: false, connected: false, slots: [{}, {}, {}] };
  const free = [{ server: a, row: { id: 1 } }, { server: b, row: { id: 2 } }, { server: b, row: { id: 3 } }, { server: st, row: { id: 4 } }];
  assert.equal(R.choose(free).server, b, 'b has 2 of 2 free, a 1 of 4');
  assert.equal(R.choose([{ server: st, row: { id: 4 } }]).row.id, 4);
  const tie = [{ server: a, row: { id: 1 } }, { server: gw('c', 4), row: { id: 5 } }];
  assert.equal(R.choose(tie, () => 0).row.id, 1);
  assert.equal(R.choose(tie, () => 0.99).row.id, 5);
  assert.equal(R.choose([]), null);

  assert.equal(R.summarize({ busy: 4 }, 4, Infinity), '4 matching slots, all busy');
  assert.equal(R.summarize({ busy: 1 }, 1, Infinity), '1 matching slot, busy');
  assert.equal(R.summarize({ busy: 2, failing: 1 }, 3, 42500), '3 matching slots: 2 busy, 1 failing (next retry in 43s)');
});

// 503 rather than 410: checks of the previous release count any other answer against the slot they claimed.
test('the slot route is gone: /connect/<node>/<slot> gets 503 saying to use /connect', async () => {
  const { url } = await startHub({ clientToken: 'tok' });
  await fakeNode(url, 'n1');
  for (const path of ['/connect/n1/1', '/connect/n1/1?gateway_token=tok', '/connect/n1']) {
    await assert.rejects(h.connect(`${url}${path}`), (err) => err.status === 503 && /endpoints were removed: connect to \/connect/.test(err.body) && /\?node=/.test(err.body));
  }
});

test('routes by the browser the client launches and by its Playwright version', async () => {
  const { url } = await startHub();
  await fakeNode(url, 'cr', { browsers: ['chromium'] });
  await fakeNode(url, 'ff', { browsers: ['firefox', 'webkit'] });
  await fakeNode(url, 'edge', { browsers: ['msedge'], playwrightVersion: '1.63.0' });

  let ws = await h.connect(`${url}/connect`, browserHeaders('firefox'));
  assert.equal((await whoami(ws)).node, 'ff');
  ws.close();
  ws = await h.connect(`${url}/connect`, browserHeaders('webkit'));
  assert.equal((await whoami(ws)).node, 'ff');
  ws.close();
  // ?browser= wins over the header, as it does in run-server.
  ws = await h.connect(`${url}/connect?browser=firefox`, browserHeaders('chromium'));
  assert.equal((await whoami(ws)).node, 'ff');
  ws.close();
  // chromium on 1.64: msedge counts as chromium, but that node runs 1.63.
  for (let i = 0; i < 4; i++) {
    ws = await h.connect(`${url}/connect`, browserHeaders('chromium'));
    assert.equal((await whoami(ws)).node, 'cr');
    ws.close();
    await h.closed(ws);
  }
  ws = await h.connect(`${url}/connect`, browserHeaders('chromium', '1.63.2'));
  assert.equal((await whoami(ws)).node, 'edge');
  ws.close();

  await assert.rejects(h.connect(`${url}/connect`, browserHeaders('android')),
    (err) => err.status === 503 && /no node offers android \(offered: chromium, firefox, msedge, webkit\)/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect`, browserHeaders('firefox', '1.62.0')),
    (err) => err.status === 428 && /version mismatch: the client is 1\.62, the nodes offering firefox run 1\.64/.test(err.body));
});

test('a client that names no browser or version may get any node; a node of unknown version takes any client', async () => {
  const { url } = await startHub();
  await fakeNode(url, 'only', { browsers: ['webkit'], playwrightVersion: 'unknown' });
  let ws = await h.connect(`${url}/connect`);
  assert.equal((await whoami(ws)).node, 'only');
  ws.close();
  await h.closed(ws);
  ws = await h.connect(`${url}/connect`, browserHeaders('webkit', '1.40.0'));
  assert.equal((await whoami(ws)).node, 'only');
});

test('?node= pins the node (any case); the slot is still the hub\'s choice', async () => {
  const { url } = await startHub();
  await fakeNode(url, 'alpha', { slots: 2 });
  await fakeNode(url, 'beta', { slots: 2, browsers: ['chromium', 'firefox'] });
  // Two sessions held at once: the hub fills both of beta's slots and never touches alpha.
  const seen = new Set();
  for (let i = 0; i < 2; i++) {
    const ws = await h.connect(`${url}/connect?node=BETA`, browserHeaders('chromium'));
    const me = await whoami(ws);
    assert.equal(me.node, 'beta');
    assert.equal(me.query, '', 'node= is not passed on to run-server');
    seen.add(me.slot);
  }
  assert.deepEqual([...seen].sort(), [1, 2]);
  await assert.rejects(h.connect(`${url}/connect?node=alpha`, browserHeaders('firefox')),
    (err) => err.status === 503 && /node alpha does not offer firefox \(offered: chromium\)/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?node=alpha`, browserHeaders('chromium', '1.70.0')),
    (err) => err.status === 428 && /node alpha runs 1\.64/.test(err.body));
});

test('spreads sessions: the node with the largest share of free slots gets the next one', async () => {
  const { url } = await startHub();
  await fakeNode(url, 'big', { slots: 4 });
  await fakeNode(url, 'small', { slots: 2 });
  const where = [];
  for (let i = 0; i < 6; i++) where.push((await whoami(await h.connect(`${url}/connect`))).node);
  const count = (n) => where.filter((w) => w === n).length;
  assert.deepEqual([count('big'), count('small')], [4, 2]);
  // After 2 of big's and 1 of small's (big 2/4 free, small 1/2): the shares stay balanced the whole way.
  assert.ok(where.slice(0, 3).filter((w) => w === 'small').length >= 1, where.join(','));
});

test('queues clients FIFO while every matching slot is busy, and hands a freed slot to the oldest', async () => {
  const { url, hub, registry } = await startHub();
  await fakeNode(url, 'solo');
  const first = await h.connect(`${url}/connect`);
  const order = [];
  const second = h.connect(`${url}/connect`).then((ws) => { order.push('second'); return ws; });
  await h.waitFor(() => hub.queue.length === 1, { what: 'second queued' });
  const third = h.connect(`${url}/connect`).then((ws) => { order.push('third'); return ws; });
  await h.waitFor(() => hub.queue.length === 2, { what: 'third queued' });
  await h.sleep(300);
  assert.deepEqual(order, []);
  assert.equal(claimed(registry).length, 1);

  first.close();
  const ws2 = await second;
  await h.sleep(200);
  assert.deepEqual(order, ['second']);
  ws2.send('x');
  assert.equal((await h.nextMessage(ws2)).data.toString(), 'x');
  ws2.terminate();
  await third;
  assert.deepEqual(order, ['second', 'third']);
  assert.equal(hub.queue.length, 0);
});

test('a waiting client gives up after GATEWAY_QUEUE_TIMEOUT_S with a 503 saying why', async () => {
  const { url, hub } = await startHub({ queueTimeoutMs: 1000 });
  await fakeNode(url, 'busy-ff', { slots: 2, browsers: ['firefox'] });
  await h.connect(`${url}/connect`, browserHeaders('firefox'));
  await h.connect(`${url}/connect`, browserHeaders('firefox'));
  const started = Date.now();
  await assert.rejects(h.connect(`${url}/connect`, browserHeaders('firefox')),
    (err) => err.status === 503 && err.body.trim() === 'no free firefox slot within 1s: 2 matching slots, all busy');
  assert.ok(Date.now() - started >= 900, `gave up after ${Date.now() - started} ms`);
  assert.equal(hub.queue.length, 0);

  // GATEWAY_QUEUE_TIMEOUT_S=0: no waiting at all.
  const nowait = await startHub({ queueTimeoutMs: 0 });
  await fakeNode(nowait.url, 'one');
  await h.connect(`${nowait.url}/connect`);
  const t = Date.now();
  await assert.rejects(h.connect(`${nowait.url}/connect?node=one`), (err) => err.status === 503 && /no free slot on node one: 1 matching slot, busy/.test(err.body));
  assert.ok(Date.now() - t < 500);
});

test('the queue is bounded by GATEWAY_QUEUE_MAX; a client a free slot can serve is never refused for it', async () => {
  const { url, hub } = await startHub({ queueMax: 1 });
  await fakeNode(url, 'cr1');
  await fakeNode(url, 'ff1', { browsers: ['firefox'] });
  await h.connect(`${url}/connect`, browserHeaders('chromium'));
  const waiting = h.connect(`${url}/connect`, browserHeaders('chromium'));
  waiting.catch(() => {});
  await h.waitFor(() => hub.queue.length === 1, { what: 'one waiting' });
  await assert.rejects(h.connect(`${url}/connect`, browserHeaders('chromium')), (err) => err.status === 503 && /queue is full \(1 clients waiting/.test(err.body));
  // A free firefox slot: served at once even with the queue full.
  const ff = await h.connect(`${url}/connect`, browserHeaders('firefox'));
  assert.equal((await whoami(ff)).node, 'ff1');
  assert.equal(hub.queue.length, 1);
});

test('a client that disconnects while queued leaves the queue and is never handed a slot', async () => {
  const { url, port, hub, registry } = await startHub();
  const node = await fakeNode(url, 'q1');
  const holder = await h.connect(`${url}/connect`);
  const quitter = h.rawSocket(port, h.upgradeRequest('/connect'));
  await h.waitFor(() => hub.queue.length === 1, { what: 'queued' });
  quitter.socket.destroy();
  await h.waitFor(() => hub.queue.length === 0, { what: 'left the queue' });
  const halfClosed = h.rawSocket(port, h.upgradeRequest('/connect'));
  await h.waitFor(() => hub.queue.length === 1, { what: 'queued again' });
  halfClosed.socket.end(); // only a FIN
  await h.waitFor(() => hub.queue.length === 0, { what: 'left on FIN' });

  holder.close();
  await h.waitFor(() => claimed(registry).length === 0, { what: 'released' });
  await h.sleep(300);
  assert.equal(node.opens.length, 1, 'nobody was opened for the clients that left');
  assert.equal(claimed(registry).length, 0);
});

test('fails fast when nothing could ever serve the request, but waits for a known node that is offline', async () => {
  const registry = new MemoryRegistry();
  // A firefox node that registered before and is offline now.
  await registry.register({ name: 'ff-later', slots: [{ index: 1, healthy: true }], browsers: ['firefox'], playwrightVersion: '1.64.1', ephemeral: false });
  await registry.disconnect('ff-later');
  const { url, hub } = await startHub({ queueTimeoutMs: 10000 }, registry);
  await fakeNode(url, 'cr-now');

  const started = Date.now();
  await assert.rejects(h.connect(`${url}/connect`, browserHeaders('webkit')), (err) => err.status === 503 && /no node offers webkit/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?node=nope`), { status: 404 });
  assert.ok(Date.now() - started < 1000, 'no waiting');

  const ff = h.connect(`${url}/connect`, browserHeaders('firefox'));
  await h.waitFor(() => hub.queue.length === 1, { what: 'waiting for the offline firefox node' });
  await h.sleep(300);
  await fakeNode(url, 'ff-later', { browsers: ['firefox'] });
  assert.equal((await whoami(await ff)).node, 'ff-later');
});

test('a waiting client is served when a slot recovers or another claimer releases one', async () => {
  const { url, hub, registry } = await startHub();
  const node = await fakeNode(url, 'sick', { slots: 1, healthy: false });
  await h.waitFor(() => hub.nodes.get('sick').slots[0].healthy === false, { what: 'unhealthy' });
  const waiting = h.connect(`${url}/connect`);
  await h.waitFor(() => hub.queue.length === 1, { what: 'queued' });
  await h.sleep(200);
  node.ws.send(JSON.stringify({ type: 'heartbeat', slots: [{ index: 1, healthy: true }] }));
  const ws = await waiting;
  assert.equal((await whoami(ws)).node, 'sick');
  ws.close();
  await h.waitFor(() => claimed(registry).length === 0, { what: 'released' });

  // Claimed by someone else (main.py, another hub): the hub sees the release on its next look.
  const row = slotRow(registry, 'sick', 1);
  Object.assign(row, { inUse: 1, lastRun: Date.now(), claimedBy: null });
  const next = h.connect(`${url}/connect`);
  await h.waitFor(() => hub.queue.length === 1, { what: 'queued behind the other claimer' });
  await h.sleep(300);
  assert.equal(hub.queue.length, 1);
  row.inUse = 0;
  assert.equal((await whoami(await next)).node, 'sick');
});

test('failing slots are skipped until their cooldown ends, like main.py did', async () => {
  const { url, hub, registry } = await startHub({ failThreshold: 2, failCooldownMs: 1500 });
  await fakeNode(url, 'flaky-one');
  Object.assign(slotRow(registry, 'flaky-one', 1), { failCount: 2, lastRun: Date.now() });
  const started = Date.now();
  const waiting = h.connect(`${url}/connect`);
  await h.waitFor(() => hub.queue.length === 1, { what: 'queued' });
  const ws = await waiting;
  assert.ok(Date.now() - started >= 1200, `served after ${Date.now() - started} ms`);
  await h.waitFor(() => slotRow(registry, 'flaky-one', 1).failCount === 0, { what: 'a session that got through resets failCount' });
  ws.close();
});

test('fails over to another slot when one cannot open, counting the failure against it', async () => {
  const { url, registry } = await startHub();
  await fakeNode(url, 'broken', { mode: 'fail' });
  await fakeNode(url, 'fine');
  // Fill the good slot first, so the next client has to try the broken one.
  const holder = await h.connect(`${url}/connect?node=fine`);
  const client = h.connect(`${url}/connect`);
  await h.waitFor(() => slotRow(registry, 'broken', 1).failCount === 1, { what: 'failure counted' });
  assert.equal(slotRow(registry, 'broken', 1).inUse, 0, 'the broken slot was given back');
  holder.close();
  const ws = await client;
  assert.equal((await whoami(ws)).node, 'fine');
  assert.equal(slotRow(registry, 'broken', 1).failCount, 1);
});

test('GATEWAY_CONNECT_ATTEMPTS caps the slots one client is tried on', async () => {
  const { url, registry } = await startHub({ connectAttempts: 2 });
  const nodes = [];
  for (const name of ['b1', 'b2', 'b3']) nodes.push(await fakeNode(url, name, { mode: 'fail' }));
  await assert.rejects(h.connect(`${url}/connect`), (err) => err.status === 502 && /run-server refused \(tried 2 slots\)/.test(err.body));
  assert.equal(nodes.reduce((n, node) => n + node.opens.length, 0), 2);
  const failures = ['b1', 'b2', 'b3'].map((n) => slotRow(registry, n, 1).failCount);
  assert.deepEqual(failures.sort(), [0, 1, 1]);
  assert.equal(claimed(registry).length, 0);
});

test('an open timeout counts as a failure and the client moves on to the next slot', async () => {
  const { url, registry } = await startHub({ openTimeoutMs: 500 });
  const mute = await fakeNode(url, 'mute', { mode: 'mute' });
  await fakeNode(url, 'ok');
  const holder = await h.connect(`${url}/connect?node=ok`);
  const client = h.connect(`${url}/connect`);
  await h.waitFor(() => mute.opens.length === 1, { what: 'opened on the mute node' });
  await h.waitFor(() => slotRow(registry, 'mute', 1).failCount === 1, { what: 'timeout counted' });
  assert.equal(slotRow(registry, 'mute', 1).inUse, 0);
  holder.close();
  assert.equal((await whoami(await client)).node, 'ok');
  // The data channel the mute node opens late finds nothing.
  await assert.rejects(h.connect(`${url}/agent/data/${mute.opens[0].id}`), { status: 404 });
});

test('the claim is released on every way a session or an attempt can end', async () => {
  const { url, port, hub, registry } = await startHub({ openTimeoutMs: 400 });
  const node = await fakeNode(url, 'rel', { slots: 1 });
  const row = slotRow(registry, 'rel', 1);
  const free = (what) => h.waitFor(() => !row.inUse && row.claimedBy === null && hub.claims.size === 0, { what });

  // The client closes.
  let ws = await h.connect(`${url}/connect`);
  assert.equal(row.inUse, 1);
  assert.match(row.claimedBy, /^hub:[0-9a-f]{8}:\d+$/);
  const claimedAt = row.lastRun;
  ws.close();
  await free('released after a client close');
  assert.ok(row.lastRun >= claimedAt, 'release stamps LastRunDate');

  // The client vanishes without a close frame.
  ws = await h.connect(`${url}/connect`);
  ws.terminate();
  await free('released after an abrupt disconnect');

  // The run-server side ends it.
  ws = await h.connect(`${url}/connect`);
  const closing = h.closed(ws);
  node.channels.at(-1).close(4000, 'done');
  assert.equal((await closing).code, 4000);
  await free('released after the server side closed');

  // The client leaves while its session is being opened.
  node.mode = 'mute';
  const leaver = h.rawSocket(port, h.upgradeRequest('/connect'));
  await h.waitFor(() => hub.pending.size === 1, { what: 'open pending' });
  assert.equal(row.inUse, 1);
  leaver.socket.destroy();
  await free('released when the client left mid-open');
  assert.equal(hub.pending.size, 0);

  // The open times out (and the only slot failed it, so the client gets the 504).
  await assert.rejects(h.connect(`${url}/connect`), { status: 504 });
  await free('released after an open timeout');
  row.failCount = 0;

  // The agent's host goes away while a session is open.
  node.mode = 'echo';
  ws = await h.connect(`${url}/connect`);
  const ended = h.closed(ws);
  node.ws.terminate();
  node.channels.at(-1).terminate();
  await ended;
  await free('released after the agent went away');
});

test('the agent disconnecting mid-open is not counted against the slot, and the client tries elsewhere', async () => {
  const { url, registry, hub } = await startHub({ openTimeoutMs: 5000 });
  const vanishing = await fakeNode(url, 'vanish', { mode: 'mute' });
  await fakeNode(url, 'stays');
  const holder = await h.connect(`${url}/connect?node=stays`);
  const client = h.connect(`${url}/connect`);
  await h.waitFor(() => vanishing.opens.length === 1, { what: 'open sent' });
  vanishing.ws.terminate();
  await h.waitFor(() => hub.queue.length === 1, { what: 'back in the queue' });
  assert.equal(slotRow(registry, 'vanish', 1).failCount, 0);
  assert.equal(slotRow(registry, 'vanish', 1).inUse, 0);
  holder.close();
  assert.equal((await whoami(await client)).node, 'stays');
});

test('stopping the hub releases every claim; a restarted hub frees what a crashed one left', async () => {
  const registry = new MemoryRegistry();
  const first = await startHub({}, registry);
  await fakeNode(first.url, 'keep', { slots: 2 });
  await h.connect(`${first.url}/connect`);
  await h.connect(`${first.url}/connect`);
  assert.equal(claimed(registry).length, 2);
  await first.hub.stop();
  assert.equal(claimed(registry).length, 0);
  assert.ok([...registry.slots.get('keep').values()].every((s) => s.claimedBy === null));

  // A hub that died with sessions open: its claims are still in the table when the next one starts.
  Object.assign(slotRow(registry, 'keep', 1), { inUse: 1, claimedBy: 'hub:deadbeef:7' });
  Object.assign(slotRow(registry, 'keep', 2), { inUse: 1, claimedBy: null }); // main.py's; not the hub's to free
  await startHub({}, registry);
  assert.equal(slotRow(registry, 'keep', 1).inUse, 0);
  assert.equal(slotRow(registry, 'keep', 1).claimedBy, null);
  assert.equal(slotRow(registry, 'keep', 2).inUse, 1);
});

test('the claim sync: retries failed releases, frees orphaned claims, re-claims a live session\'s slot freed under it', async () => {
  const registry = new MemoryRegistry();
  const { url, hub } = await startHub({ claimSyncMs: 60000 }, registry);
  await fakeNode(url, 'sync', { slots: 3 });
  const ws = await h.connect(`${url}/connect?node=sync`);
  const [mine] = [...hub.claims.values()];
  const row = registry.slotById(mine.id);

  // An older stale-claim sweep frees it (inUse = 0, claimedBy and LastRunDate untouched): claimed again.
  row.inUse = 0;
  await hub.syncClaims();
  assert.equal(row.inUse, 1);
  assert.equal(row.claimedBy, mine.claimedBy);

  // A claim with this hub's prefix that no session holds (say a lost release) is freed.
  const prefix = mine.claimedBy.replace(/\d+$/, '');
  const orphan = [...registry.slots.get('sync').values()].find((s) => s !== row);
  Object.assign(orphan, { inUse: 1, claimedBy: `${prefix}999`, lastRun: Date.now() });
  await hub.syncClaims();
  assert.equal(orphan.inUse, 0);
  assert.equal(orphan.claimedBy, null);

  // The Grid UI force-releases the live session's slot (claimedBy cleared): the session is still open,
  // so the hub claims the slot again rather than let it be handed out twice.
  Object.assign(row, { inUse: 0, claimedBy: null, lastRun: Date.now() + 5 });
  await hub.syncClaims();
  assert.equal(row.inUse, 1);
  assert.equal(row.claimedBy, mine.claimedBy);
  ws.close();
  await h.waitFor(() => !row.inUse && row.claimedBy === null && hub.claims.size === 0, { what: 'released when the session ended' });

  // A release the database refused is retried.
  const releaseSlot = registry.releaseSlot.bind(registry);
  registry.releaseSlot = async () => { throw new Error('ECONNRESET'); };
  const second = await h.connect(`${url}/connect?node=sync`);
  const [claim2] = [...hub.claims.values()];
  second.close();
  await h.waitFor(() => hub.claims.size === 0, { what: 'release attempted' });
  assert.equal(registry.slotById(claim2.id).inUse, 1, 'still claimed: the release failed');
  registry.releaseSlot = releaseSlot;
  await hub.syncClaims();
  assert.equal(registry.slotById(claim2.id).inUse, 0);
  assert.equal(registry.slotById(claim2.id).claimedBy, null);
});

test('the claim sync never takes a claim that landed while it read the slots for a lost one', async () => {
  // The read takes a while (a SELECT on a busy MySQL); a client is claimed and connected meanwhile.
  class SlowRegistry extends MemoryRegistry {
    async slotStates(...args) {
      const rows = await super.slotStates(...args);
      if (!this.slow) return rows;
      this.slow = false;
      await h.sleep(300);
      return rows;
    }
  }
  const registry = new SlowRegistry();
  const { url, hub } = await startHub({ claimSyncMs: 60000, queueTimeoutMs: 1000, openTimeoutMs: 400 }, registry);
  const node = await fakeNode(url, 'one');
  const row = slotRow(registry, 'one', 1);

  registry.slow = true;
  const sync = hub.syncClaims();
  await h.sleep(50);
  const ws = await h.connect(`${url}/connect?node=one`);
  await sync;
  assert.equal(hub.claims.size, 1, 'still this hub\'s claim');
  ws.close();
  await h.waitFor(() => !row.inUse && row.claimedBy === null, { what: 'released when the session ended' });
  const next = await h.connect(`${url}/connect?node=one`);
  assert.equal((await whoami(next)).node, 'one');
  next.close();
  await h.waitFor(() => !row.inUse, { what: 'released again' });

  // The same while a session is being opened and never answers: the open timeout releases it.
  node.mode = 'mute';
  registry.slow = true;
  const sync2 = hub.syncClaims();
  await h.sleep(50);
  const timedOut = assert.rejects(h.connect(`${url}/connect?node=one`), { status: 504 });
  await sync2;
  await timedOut;
  await h.waitFor(() => !row.inUse && row.claimedBy === null && hub.claims.size === 0, { what: 'released after the open timeout' });
});

test('a slot taken from a live session is never routed to again while that session runs', async () => {
  const registry = new MemoryRegistry();
  const { url, hub } = await startHub({ claimSyncMs: 60000, queueTimeoutMs: 800 }, registry);
  await fakeNode(url, 'taken', { slots: 1 });
  const ws = await h.connect(`${url}/connect`);
  const row = slotRow(registry, 'taken', 1);

  // Released from the Grid UI, and another claimer (writing claimedBy NULL) gets the slot before the sync.
  Object.assign(row, { inUse: 1, claimedBy: null, lastRun: Date.now() + 5 });
  await hub.syncClaims();
  assert.equal(hub.claims.size, 1, 'kept until its session ends');
  // The other claimer is done meanwhile; the hub still pipes the first session there, so a new client waits.
  row.inUse = 0;
  await assert.rejects(h.connect(`${url}/connect`), (err) => err.status === 503 && /1 matching slot, busy/.test(err.body));
  ws.close();
  await h.waitFor(() => hub.claims.size === 0, { what: 'the lost claim goes when its session ends' });
  const next = await h.connect(`${url}/connect`);
  assert.equal((await whoami(next)).node, 'taken');
  next.close();
  await h.waitFor(() => !row.inUse, { what: 'released' });

  // An older sweep frees a live session's slot, and a claimer that leaves claimedBy alone (an old main.py)
  // claims it before the sync: the row still says this hub, but its stamp is another claim's.
  const third = await h.connect(`${url}/connect`);
  Object.assign(row, { inUse: 1, lastRun: row.lastRun + 1000 });
  await hub.syncClaims();
  third.close();
  await h.sleep(300);
  assert.equal(row.inUse, 1, 'the end of the hub\'s session left the other claim alone');
  assert.match(row.claimedBy, /^hub:/);
});

test('hubs sharing a database: each frees only the claims of a hub whose lease ran out', async () => {
  const registry = new MemoryRegistry();
  const a = await startHub({ claimSyncMs: 60000, hubLeaseMs: 3000 }, registry);
  await fakeNode(a.url, 'n1', { slots: 2 });
  const ws = await h.connect(`${a.url}/connect`);
  const [claim] = [...a.hub.claims.values()];
  const row = registry.slotById(claim.id);
  const other = [...registry.slots.get('n1').values()].find((s) => s !== row);
  assert.ok(registry.hubs.has(a.hub.instance));

  // A second hub starts on the same database: hub a's live claim is left alone, and b says a is there.
  const { lines, log } = recorder();
  const b = await startHub({ claimSyncMs: 60000, hubLeaseMs: 3000 }, registry, log);
  assert.equal(row.inUse, 1);
  assert.equal(row.claimedBy, claim.claimedBy);
  assert.ok(lines.some((l) => l.startsWith(`warn another hub uses this database: instance ${a.hub.instance}`)), lines.join('\n'));
  await b.hub.syncClaims();
  assert.equal(row.inUse, 1);
  assert.equal(a.hub.claims.size, 1);

  // A hub that crashed with a session open: once its lease runs out, hub b frees its claim.
  Object.assign(other, { inUse: 1, claimedBy: 'hub:0badcafe:3', lastRun: Date.now() });
  registry.hubs.set('0badcafe', { host: 'gone', port: 1, startedAt: Date.now() - 5000, lastSeen: Date.now() - 1000 });
  await b.hub.syncClaims();
  assert.equal(other.inUse, 1, 'its lease has not run out yet');
  registry.hubs.get('0badcafe').lastSeen = Date.now() - 3500;
  await b.hub.syncClaims();
  assert.deepEqual([other.inUse, other.claimedBy], [0, null]);
  assert.equal(registry.hubs.has('0badcafe'), false);
  assert.deepEqual([row.inUse, row.claimedBy], [1, claim.claimedBy], 'hub a renews its lease, so its claim stays');

  // A hub that stops cleanly gives its claims back and removes its row.
  ws.close();
  await a.hub.stop();
  assert.equal(registry.hubs.has(a.hub.instance), false);
  assert.ok(registry.hubs.has(b.hub.instance));
  assert.equal(claimed(registry).length, 0);
});

test('a failed start-up release of a dead hub\'s claims is retried by the claim sync', async () => {
  const registry = new MemoryRegistry();
  await registry.register({ name: 'left', slots: [{ index: 1, healthy: true }], browsers: ['chromium'], playwrightVersion: '1.64.1', ephemeral: false });
  Object.assign(slotRow(registry, 'left', 1), { inUse: 1, claimedBy: 'hub:deadbeef:1', lastRun: Date.now() });
  const release = registry.releaseDeadHubClaims.bind(registry);
  registry.releaseDeadHubClaims = async () => { throw new Error('ECONNRESET'); };
  const { hub } = await startHub({ claimSyncMs: 60000 }, registry);
  assert.equal(slotRow(registry, 'left', 1).inUse, 1);
  registry.releaseDeadHubClaims = release;
  await hub.syncClaims();
  assert.deepEqual([slotRow(registry, 'left', 1).inUse, slotRow(registry, 'left', 1).claimedBy], [0, null]);
});

test('a queued client whose managed token is revoked or expires while it waits gets no session', async () => {
  const crypto = require('node:crypto');
  const mint = () => `pwgc_${crypto.randomBytes(32).toString('base64url')}`;
  const registry = new MemoryRegistry();
  const token = mint();
  const row = registry.addToken(token, { kind: 'client', name: 'ci' });
  const other = mint();
  registry.addToken(other, { kind: 'client', name: 'other' });
  const { lines, log } = recorder();
  const { url, hub } = await startHub({ tokenCacheMs: 0, tokenRecheckMs: 60000 }, registry, log);
  await fakeNode(url, 'solo');
  const first = await h.connect(`${url}/connect?gateway_token=${token}`);
  const queued = h.connect(`${url}/connect?gateway_token=${token}`);
  const unaffected = h.connect(`${url}/connect`, { 'x-gateway-token': other });
  await h.waitFor(() => hub.queue.length === 2, { what: 'both queued' });

  row.revokedAt = new Date().toISOString();
  await assert.rejects(h.connect(`${url}/connect?gateway_token=${token}`), { status: 401 });
  first.close(); // frees the slot: the revoked client is refused, the next one in line gets it
  await assert.rejects(queued, (err) => err.status === 401 && /gateway token is revoked/.test(err.body));
  assert.equal((await whoami(await unaffected)).node, 'solo');
  assert.ok(lines.some((l) => /^warn client #2 127\.0\.0\.1 dropped after \d+\.\ds: client token pwgc_/.test(l)), lines.join('\n'));
  assert.ok(lines.every((l) => !l.includes(token)));

  // The periodic token re-check drops a queued client too, without waiting for a slot to free.
  row.revokedAt = null;
  row.expiresAt = new Date(Date.now() + 600).toISOString();
  const waiting = h.connect(`${url}/connect?gateway_token=${token}`);
  await h.waitFor(() => hub.queue.length === 1, { what: 'queued again' });
  await h.sleep(700);
  await hub.recheckTokens();
  await assert.rejects(waiting, (err) => err.status === 401 && /gateway token is expired/.test(err.body));
  assert.equal(hub.queue.length, 0);
});

test('chromium goes to the narrowest node first, keeping slots that serve other browsers for them', async () => {
  const gw = (name, browsers, total) => ({ name, gateway: true, connected: true, browsers, agent: { slots: Array(total).fill({ healthy: true }) }, slots: [] });
  assert.equal(R.breadth(['chromium'], 'chromium'), 0);
  assert.equal(R.breadth(['chromium', 'chrome', 'msedge'], 'chromium'), 0, 'channels are chromium');
  assert.equal(R.breadth(['chromium', 'firefox'], 'chromium'), 1);
  assert.equal(R.breadth(['chromium', 'firefox'], null), 2);
  assert.equal(R.breadth([], 'chromium'), Infinity);
  const chrome = gw('chrome', ['chromium'], 4);
  const fox = gw('fox', ['chromium', 'firefox'], 4);
  // fox has all 4 free, chrome 1 of 4: chromium still goes to chrome.
  const free = [{ server: chrome, row: { id: 1 } }, ...[2, 3, 4, 5].map((id) => ({ server: fox, row: { id } }))];
  for (const random of [() => 0, () => 0.99]) assert.equal(R.choose(free, random, 'chromium').server, chrome);
  assert.equal(R.choose(free.slice(1), () => 0, 'chromium').server, fox, 'and to fox once chrome is full');

  // End to end: 4 chromium sessions, then 4 firefox ones, on 4 + 4 slots: nobody waits.
  const { url, hub } = await startHub({ queueTimeoutMs: 500 });
  await fakeNode(url, 'chrome-only', { slots: 4, browsers: ['chromium'] });
  await fakeNode(url, 'fox-too', { slots: 4, browsers: ['chromium', 'firefox'] });
  const chromium = [];
  for (let i = 0; i < 4; i++) chromium.push((await whoami(await h.connect(`${url}/connect`, browserHeaders('chromium')))).node);
  assert.deepEqual(chromium, ['chrome-only', 'chrome-only', 'chrome-only', 'chrome-only']);
  for (let i = 0; i < 4; i++) assert.equal((await whoami(await h.connect(`${url}/connect`, browserHeaders('firefox')))).node, 'fox-too');
  assert.equal(hub.queue.length, 0);
});

test('the database down: routing fails closed with 503', async () => {
  const registry = new MemoryRegistry();
  const { url, hub } = await startHub({}, registry);
  await fakeNode(url, 'dbless');
  const slotStates = registry.slotStates.bind(registry);
  registry.slotStates = async () => { throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:3306'), { code: 'ECONNREFUSED' }); };
  await assert.rejects(h.connect(`${url}/connect`), (err) => err.status === 503 && /cannot reach its database to pick a slot/.test(err.body));
  registry.slotStates = slotStates;
  registry.claimSlot = async () => { throw new Error('ETIMEDOUT'); };
  await assert.rejects(h.connect(`${url}/connect`), (err) => err.status === 503 && /cannot reach its database to claim a slot/.test(err.body));
  assert.equal(hub.queue.length, 0);
  assert.equal(hub.claims.size, 0);
});

test('the token is checked before a client is queued or routed', async () => {
  const { url, hub } = await startHub({ clientToken: 'tok' });
  const node = await fakeNode(url, 'guarded');
  await h.connect(`${url}/connect?gateway_token=tok`);
  await assert.rejects(h.connect(`${url}/connect`), { status: 401 });
  await assert.rejects(h.connect(`${url}/connect?node=nowhere&gateway_token=bad`), { status: 401 });
  await assert.rejects(h.connect(`${url}/connect`, browserHeaders('webkit')), { status: 401 });
  assert.equal(hub.queue.length, 0);
  assert.equal(node.opens.length, 1);
  const waiting = h.connect(`${url}/connect`, { 'x-gateway-token': 'tok' });
  waiting.catch(() => {});
  await h.waitFor(() => hub.queue.length === 1, { what: 'a client with a token waits' });
});

test('static hosts: the hub dials them itself, forwards the headers without the token, and prefers gateway nodes', async () => {
  const registry = new MemoryRegistry();
  const host = await staticHost();
  registry.addStaticSlot('127.0.0.1', host.port);
  const { url } = await startHub({ clientToken: 'tok' }, registry);
  await fakeNode(url, 'gw');

  // A free gateway slot is preferred.
  const onGateway = await h.connect(`${url}/connect?gateway_token=tok`, browserHeaders('chromium'));
  assert.equal((await whoami(onGateway)).node, 'gw');
  assert.equal(host.seen.length, 0);

  // The gateway slot is busy: the static host takes any browser.
  const ws = await h.connect(`${url}/connect?gateway_token=tok&x=1`, { ...browserHeaders('firefox'), 'x-playwright-launch-options': '{"headless":true}' });
  const me = await whoami(ws);
  assert.equal(me.node, 'static');
  assert.equal(me.url, '/?x=1');
  assert.equal(me.headers['x-playwright-browser'], 'firefox');
  assert.equal(me.headers['x-playwright-launch-options'], '{"headless":true}');
  assert.match(me.headers['user-agent'], /^Playwright\/1\.64\.1/);
  assert.equal(me.headers['x-gateway-token'], undefined);
  ws.send(Buffer.from([1, 2, 3]));
  assert.deepEqual(Buffer.from((await h.nextMessage(ws)).data), Buffer.from([1, 2, 3]));
  const row = slotRow(registry, '127.0.0.1', host.port);
  assert.equal(row.inUse, 1);
  ws.close();
  await h.waitFor(() => !row.inUse && row.claimedBy === null, { what: 'static slot released' });

  // Pinned by its host name.
  const pinned = await h.connect(`${url}/connect?node=127.0.0.1&gateway_token=tok`);
  assert.equal((await whoami(pinned)).node, 'static');
});

test('static hosts: a refusal or an unreachable host is a connect failure, and the client moves on', async () => {
  const registry = new MemoryRegistry();
  const refusing = await staticHost({ refuse: 403 });
  const gone = await closedPort();
  registry.addStaticSlot('127.0.0.1', refusing.port);
  registry.addStaticSlot('localhost', gone);
  const elsewhere = await staticHost();
  registry.addStaticSlot('127.0.0.2', elsewhere.port); // it listens on 127.0.0.1 only, so this one is unreachable too
  const { url } = await startHub({ connectAttempts: 3 }, registry);

  await assert.rejects(h.connect(`${url}/connect`), (err) => err.status === 502 && /tried 3 slots/.test(err.body));
  assert.equal(slotRow(registry, '127.0.0.1', refusing.port).failCount, 1);
  assert.equal(slotRow(registry, 'localhost', gone).failCount, 1);
  assert.equal(claimed(registry).length, 0);
  await assert.rejects(h.connect(`${url}/connect?node=127.0.0.1`), (err) => err.status === 502 && /answered HTTP 403: Forbidden/.test(err.body));
});

// Static hosts (and nodes that don't know their version) take a client of any version, so run-server
// is the one to refuse it. That is the client's doing: it costs no slot a failure and isn't tried
// elsewhere, so a client on the wrong version can't put healthy slots into cooldown.
test('a run-server 428 for the client\'s Playwright version is passed on and never counted against a slot', async () => {
  const registry = new MemoryRegistry();
  const hosts = [await staticHost({ refuse: 428 }), await staticHost({ refuse: 428 })];
  for (const host of hosts) registry.addStaticSlot('127.0.0.1', host.port);
  const { url } = await startHub({ connectAttempts: 3, failThreshold: 3 }, registry);
  const old = (version) => browserHeaders('chromium', version);

  for (let i = 0; i < 4; i++) {
    await assert.rejects(h.connect(`${url}/connect`, old('1.50.0')), (err) => err.status === 428 && !/tried/.test(err.body)
      && /^Playwright version mismatch: the client is 1\.50, static host 127\.0\.0\.1:\d+ runs 1\.64\. Keep the client/.test(err.body));
  }
  assert.equal(hosts.reduce((n, host) => n + host.seen.length, 0), 4, 'one host per client: no failover');
  assert.deepEqual(hosts.map((host) => slotRow(registry, '127.0.0.1', host.port).failCount), [0, 0]);
  assert.ok((await registry.slotStates(3, 300000)).every((row) => !row.failing && !row.inUse));

  // A node that doesn't know its Playwright version: the same.
  const unknown = await fakeNode(url, 'unknown-version', { playwrightVersion: 'unknown', mode: 'fail', failStatus: 428 });
  for (let i = 0; i < 4; i++) {
    await assert.rejects(h.connect(`${url}/connect`, old('1.50.0')),
      (err) => err.status === 428 && /the client is 1\.50, node unknown-version slot 1 runs 1\.64\./.test(err.body));
  }
  assert.equal(unknown.opens.length, 4);
  assert.equal(slotRow(registry, 'unknown-version', 1).failCount, 0);
  // So a client on the right version gets that slot at once.
  unknown.mode = 'echo';
  const ws = await h.connect(`${url}/connect`, old('1.64.1'));
  assert.equal((await whoami(ws)).node, 'unknown-version');
  ws.close();

  // An agent from before open-failed carried the status: the 428 in its message is enough.
  await fakeNode(url, 'old-agent', { playwrightVersion: 'unknown', mode: 'fail', failStatus: 428, oldAgent: true });
  await assert.rejects(h.connect(`${url}/connect?node=old-agent`, old('1.50.0')), { status: 428 });
  assert.equal(slotRow(registry, 'old-agent', 1).failCount, 0);
  assert.equal(claimed(registry).length, 0);
});

test('logs one line per routing decision and per queue timeout, and never a token', async () => {
  const { lines, log } = recorder();
  const { url } = await startHub({ clientToken: 's3cret-token', queueTimeoutMs: 600 }, new MemoryRegistry(), log);
  await fakeNode(url, 'logged');
  await h.connect(`${url}/connect?gateway_token=s3cret-token`, browserHeaders('chromium'));
  await assert.rejects(h.connect(`${url}/connect`, { 'x-gateway-token': 's3cret-token', ...browserHeaders('chromium') }), { status: 503 });
  assert.ok(lines.some((l) => /^info client #1 127\.0\.0\.1 -> logged\/1 \(chromium, waited \d+\.\ds\)$/.test(l)), lines.join('\n'));
  assert.ok(lines.some((l) => /^info client #2 127\.0\.0\.1 waits for a free chromium slot: 1 matching slot, busy$/.test(l)), lines.join('\n'));
  assert.ok(lines.some((l) => /^warn client #2 127\.0\.0\.1 gave up after \d+\.\ds: no free chromium slot \(1 matching slot, busy\)$/.test(l)), lines.join('\n'));
  assert.ok(lines.every((l) => !l.includes('s3cret-token')));
});

test('a burst of clients on few slots: every client served, never two sessions on one slot, nothing left claimed', async () => {
  const { url, hub, registry } = await startHub();
  await fakeNode(url, 'burst-a', { slots: 2 });
  await fakeNode(url, 'burst-b', { slots: 2 });
  const live = new Map(); // "node/slot" -> sessions on it right now
  let peak = 0;
  const run = async (i) => {
    const ws = await h.connect(`${url}/connect`);
    const { node, slot } = await whoami(ws);
    const key = `${node}/${slot}`;
    live.set(key, (live.get(key) || 0) + 1);
    peak = Math.max(peak, ...live.values());
    await h.sleep(20 + (i % 5) * 10);
    live.set(key, live.get(key) - 1);
    const closing = h.closed(ws);
    ws.close();
    await closing;
    return key;
  };
  const keys = await Promise.all(Array.from({ length: 24 }, (_, i) => run(i)));
  assert.equal(keys.length, 24);
  assert.equal(peak, 1, 'two sessions shared a slot');
  assert.equal(new Set(keys).size, 4, 'all four slots were used');
  await h.waitFor(() => claimed(registry).length === 0 && hub.claims.size === 0 && hub.queue.length === 0, { what: 'everything released' });
});
