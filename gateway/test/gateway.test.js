'use strict';

const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test, afterEach } = require('node:test');
const h = require('./helpers');
const { MemoryRegistry } = require('../lib/registry');

afterEach(h.cleanup);

test('agent registers, gets the ack and shows up in /api/nodes, /healthz and the registry', async () => {
  const { url, http, registry } = await h.startHub();
  const agent = await h.startAgent(url, { name: 'win-01', slots: 3 });
  await h.waitFor(() => h.allHealthy(agent), { what: 'registered + healthy' });

  const health = await h.getJson(`${http}/healthz`);
  assert.deepEqual(health, { status: 200, body: { ok: true, db: 'memory', nodes: 1 } });

  const nodes = await h.waitFor(async () => {
    const { body } = await h.getJson(`${http}/api/nodes`);
    return body.nodes[0] && body.nodes[0].healthySlots === 3 && body.nodes;
  }, { what: 'healthy slots reported' });
  assert.equal(nodes.length, 1);
  const node = nodes[0];
  assert.equal(node.name, 'win-01');
  assert.equal(node.slots, 3);
  assert.equal(node.playwrightVersion, '0.0.0-fake');
  assert.equal(node.agentVersion, '1.0.0');
  assert.equal(node.platform, process.platform);
  assert.deepEqual(node.browsers, ['chromium']);
  assert.equal(node.ephemeral, false);
  assert.match(node.connectedAt, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
  assert.match(node.lastSeen, /Z$/);
  assert.ok(node.remoteAddress);

  const row = registry.nodes.get('win-01');
  assert.equal(row.online, 1);
  assert.equal(row.slots, 3);
  assert.equal(row.ephemeral, 0);
  assert.deepEqual([...registry.slots.get('win-01').keys()], [1, 2, 3]);
  await h.waitFor(() => [...registry.slots.get('win-01').values()].every((s) => s.status === 1), { what: 'slot health in registry' });

  assert.equal((await h.getJson(`${http}/nope`)).status, 404);
  await agent.stop();
  await h.waitFor(() => registry.nodes.get('win-01').online === 0, { what: 'offline after agent stop' });
  assert.deepEqual((await h.getJson(`${http}/api/nodes`)).body, { nodes: [] });
});

test('pipes text and binary frames, forwards x-playwright-* headers and the query, strips the gateway token and node', async () => {
  const { url } = await h.startHub({ clientToken: 's3cret' });
  const agent = await h.startAgent(url, { name: 'pipe-node' });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });

  // The fake run-server reports playwright 0.0.0-fake, so a 0.0 client matches it.
  const ws = await h.connect(`${url}/connect?node=pipe-node&browser=chromium&gateway_token=s3cret&x=a%20b`, {
    'x-playwright-browser': 'chromium',
    'x-playwright-launch-options': '{"headless":true}',
    'user-agent': 'Playwright/0.0.0 (test)',
    authorization: 'Bearer nope',
  });
  ws.send('hello');
  let msg = await h.nextMessage(ws);
  assert.equal(msg.isBinary, false);
  assert.equal(msg.data.toString(), 'hello');

  const bytes = Buffer.from([0, 1, 2, 255, 254]);
  ws.send(bytes);
  msg = await h.nextMessage(ws);
  assert.equal(msg.isBinary, true);
  assert.deepEqual(Buffer.from(msg.data), bytes);

  const big = Buffer.alloc(5 * 1024 * 1024, 7);
  ws.send(big);
  msg = await h.nextMessage(ws);
  assert.equal(msg.data.length, big.length);

  ws.send('info');
  const info = JSON.parse((await h.nextMessage(ws)).data);
  assert.equal(info.url, '/?browser=chromium&x=a%20b');
  assert.equal(info.headers['x-playwright-browser'], 'chromium');
  assert.equal(info.headers['x-playwright-launch-options'], '{"headless":true}');
  assert.equal(info.headers['user-agent'], 'Playwright/0.0.0 (test)');
  assert.equal(info.headers.authorization, undefined);
  assert.equal(info.headers['x-gateway-token'], undefined);
  assert.ok(agent.slots.some((s) => info.headers.host === `127.0.0.1:${s.port}`), info.headers.host);

  const ws2 = await h.connect(`${url}/connect/`, { 'x-gateway-token': 's3cret' });
  ws2.send('info');
  const info2 = JSON.parse((await h.nextMessage(ws2)).data);
  assert.equal(info2.url, '/');
  assert.equal(info2.headers['x-gateway-token'], undefined);
});

test('answers 400, 401, 404, 502 and 504 with plain HTTP', async () => {
  const { url } = await h.startHub({ clientToken: 'tok', openTimeoutMs: 800 });
  const agent = await h.startAgent(url, { name: 'n1', slots: 2 });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });

  await assert.rejects(h.connect(`${url}/connect?node=n1`), { status: 401 });
  await assert.rejects(h.connect(`${url}/connect?gateway_token=wrong`), { status: 401 });
  await assert.rejects(h.connect(`${url}/connect?node=missing&gateway_token=tok`), (err) => err.status === 404 && /no node or static host named missing/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?node=bad%20name&gateway_token=tok`), (err) => err.status === 400 && /node must be/.test(err.body));
  await assert.rejects(h.connect(`${url}/connect?gateway_token=tok`, { 'x-playwright-browser': 'fire fox' }), { status: 400 });
  await assert.rejects(h.connect(`${url}/elsewhere`), { status: 404 });
  // Both slots refuse (as run-server does on a version mismatch); each is tried once.
  await assert.rejects(h.connect(`${url}/connect?gateway_token=tok`, { 'x-fake-reject': '1' }),
    (err) => err.status === 502 && /428/.test(err.body) && /version mismatch/.test(err.body) && /tried 2 slots/.test(err.body));

  // A node that registers but never answers opens.
  const silentAgent = await h.rawAgent(url);
  await h.register(silentAgent, 'mute');
  const started = Date.now();
  await assert.rejects(h.connect(`${url}/connect?node=mute&gateway_token=tok`), (err) => err.status === 504 && /did not answer/.test(err.body));
  assert.ok(Date.now() - started >= 700);
});

test('pending opens fail with 502 when the agent disconnects, and late data channels are refused', async () => {
  const { url, hub } = await h.startHub({ openTimeoutMs: 10000 });
  const raw = await h.rawAgent(url);
  await h.register(raw, 'flaky');
  const opened = h.nextMessage(raw).then(({ data }) => JSON.parse(data));
  const client = h.connect(`${url}/connect?node=flaky`);
  const open = await opened;
  assert.equal(open.type, 'open');
  assert.equal(open.slot, 1);
  assert.equal(hub.pending.size, 1);
  raw.terminate();
  await assert.rejects(client, (err) => err.status === 502 && /disconnected/.test(err.body));
  assert.equal(hub.pending.size, 0);
  await assert.rejects(h.connect(`${url}/agent/data/${open.id}`), { status: 404 });
});

test('rejects a bad agent token with 4401 and a bad node name with 4400', async () => {
  const { url } = await h.startHub({ agentToken: 'agent-secret' });
  let ws = await h.rawAgent(url, { headers: { Authorization: 'Bearer wrong' } });
  assert.equal((await h.closed(ws)).code, 4401);

  ws = await h.rawAgent(url, { headers: { Authorization: 'Bearer agent-secret' } });
  const closing = h.closed(ws);
  ws.send(JSON.stringify({ type: 'register', name: 'bad name!', slots: [{ index: 1, healthy: true }] }));
  assert.equal((await closing).code, 4400);

  const agent = await h.startAgent(url, { name: 'good', token: 'agent-secret' });
  await h.waitFor(() => agent.registered, { what: 'token agent registered' });
  const wrong = await h.startAgent(url, { name: 'wrongtok', token: 'nope' });
  await h.waitFor(() => wrong.lastError && /4401/.test(wrong.lastError), { what: '4401 seen by agent' });
  assert.equal(wrong.registered, false);
});

test('agent reconnects and re-registers after a hub restart', async () => {
  const first = await h.startHub();
  const agent = await h.startAgent(first.url, { name: 'phoenix' });
  await h.waitFor(() => agent.registered, { what: 'first registration' });
  await first.hub.stop();
  await h.waitFor(() => !agent.connected, { what: 'agent notices the hub is gone' });
  await h.sleep(300); // a few failed reconnects

  const second = await h.startHub({ port: first.port });
  await h.waitFor(() => agent.registered, { what: 're-registration' });
  assert.equal(second.registry.nodes.get('phoenix').online, 1);
  const { body } = await h.getJson(`${second.http}/api/nodes`);
  assert.deepEqual(body.nodes.map((n) => n.name), ['phoenix']);

  const ws = await h.connect(`${second.url}/connect`);
  ws.send('ping?');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'ping?');
});

test('a duplicate name replaces the old connection with 4409 and the old agent backs off', async () => {
  const { url, hub, registry } = await h.startHub();
  const old = await h.startAgent(url, { name: 'twin' });
  await h.waitFor(() => old.registered, { what: 'old registered' });
  const codes = [];
  old.on('disconnected', (code) => codes.push(code));

  const usurper = await h.rawAgent(url);
  assert.deepEqual(await h.register(usurper, 'twin', 1), { type: 'registered', name: 'twin' });
  await h.waitFor(() => codes.length === 1, { what: 'old agent closed' });
  assert.deepEqual(codes, [4409]);
  await h.sleep(1500); // the normal reconnect backoff here is 100-400 ms
  assert.equal(old.connected, false);
  assert.equal(hub.nodes.get('twin').ws.readyState, 1);
  assert.equal(registry.nodes.get('twin').online, 1);
  assert.equal(registry.nodes.get('twin').slots, 1);
});

test('drops agents that stop answering pings and marks them offline', async () => {
  const { url, registry, hub } = await h.startHub({ heartbeatTimeoutMs: 1200 });
  const ws = await h.rawAgent(url, { autoPong: false });
  await h.register(ws, 'zombie');
  assert.equal(registry.nodes.get('zombie').online, 1);
  await h.closed(ws);
  await h.waitFor(() => registry.nodes.get('zombie').online === 0, { what: 'zombie offline' });
  assert.equal(hub.nodes.size, 0);
});

test('restarts a crashed run-server and writes the status file', async () => {
  const { url } = await h.startHub();
  const agent = await h.startAgent(url, { name: 'crashy', slots: 2 });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });

  const status = await h.waitFor(() => {
    const s = JSON.parse(fs.readFileSync(agent.config.statusFile, 'utf8'));
    return s.registered && s.slots.every((x) => x.healthy) && s;
  }, { what: 'status file' });
  assert.deepEqual(Object.keys(status), ['name', 'hub', 'agentVersion', 'playwrightVersion', 'pid', 'connected', 'registered', 'lastError', 'updated', 'slots']);
  assert.equal(status.name, 'crashy');
  assert.equal(status.hub, url);
  assert.equal(status.agentVersion, '1.0.0');
  assert.equal(status.playwrightVersion, '0.0.0-fake');
  assert.equal(status.pid, process.pid);
  assert.equal(status.connected, true);
  assert.equal(status.lastError, null);
  assert.match(status.updated, /Z$/);
  assert.deepEqual(Object.keys(status.slots[0]), ['index', 'port', 'pid', 'healthy', 'restarts', 'since']);
  assert.deepEqual(status.slots.map((s) => [s.index, s.port, s.restarts]), [[1, agent.config.basePort, 0], [2, agent.config.basePort + 1, 0]]);
  assert.match(status.slots[0].since, /Z$/);
  assert.ok(status.slots[0].pid > 0);

  const oldPid = agent.slots[0].child.pid;
  process.kill(oldPid);
  await h.waitFor(() => !agent.slots[0].healthy, { what: 'slot marked unhealthy' });
  await h.waitFor(() => agent.slots[0].healthy && agent.slots[0].restarts === 1, { what: 'slot restarted' });
  assert.notEqual(agent.slots[0].child.pid, oldPid);
  const after = await h.waitFor(() => {
    const s = JSON.parse(fs.readFileSync(agent.config.statusFile, 'utf8'));
    return s.slots[0].restarts === 1 && s.slots[0].healthy && s;
  }, { what: 'status file shows the restart' });
  assert.equal(after.slots[0].pid, agent.slots[0].child.pid);
  assert.equal(after.slots[1].restarts, 0);

  const ws = await h.connect(`${url}/connect`);
  ws.send('back');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'back');

  await agent.stop();
  const stopped = JSON.parse(fs.readFileSync(agent.config.statusFile, 'utf8'));
  assert.equal(stopped.connected, false);
  assert.ok(stopped.slots.every((s) => !s.healthy && s.pid === null));
});

test('propagates close codes in both directions and maps unsendable ones', async () => {
  const { url } = await h.startHub();
  // One slot, so every session lands on the run-server that saw the previous one close.
  const agent = await h.startAgent(url, { name: 'closer', slots: 1 });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });

  let ws = await h.connect(`${url}/connect`);
  const closing = h.closed(ws);
  ws.send('close:4321:server says bye');
  assert.deepEqual(await closing, { code: 4321, reason: 'server says bye' });

  ws = await h.connect(`${url}/connect`);
  ws.close(4123, 'client says bye');
  await h.closed(ws);
  await h.sleep(200);
  ws = await h.connect(`${url}/connect`);
  ws.send('lastclose');
  assert.deepEqual(JSON.parse((await h.nextMessage(ws)).data), { code: 4123, reason: 'client says bye' });

  // A client that vanishes (1006) reaches run-server as 1011.
  ws.terminate();
  await h.sleep(300);
  ws = await h.connect(`${url}/connect`);
  ws.send('lastclose');
  assert.equal(JSON.parse((await h.nextMessage(ws)).data).code, 1011);

  // A close without a code (1005) reaches run-server as 1000.
  ws.close();
  await h.closed(ws);
  await h.sleep(200);
  ws = await h.connect(`${url}/connect`);
  ws.send('lastclose');
  assert.equal(JSON.parse((await h.nextMessage(ws)).data).code, 1000);
});

test('agent stop closes sessions and the hub marks the node offline', async () => {
  const { url, registry } = await h.startHub();
  const agent = await h.startAgent(url, { name: 'leaver' });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });
  const ws = await h.connect(`${url}/connect`);
  const closing = h.closed(ws);
  await agent.stop();
  assert.equal((await closing).code, 1001);
  await h.waitFor(() => registry.nodes.get('leaver').online === 0, { what: 'offline' });
});

test('agent.js CLI: flags work and run-servers exit when the agent is killed hard', async () => {
  const { url, hub } = await h.startHub();
  const statusFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'gateway-cli-')), 'status.json');
  const basePort = await h.freePorts(2);
  const child = spawn(process.execPath, [path.join(__dirname, '..', 'agent.js'), '--hub', url, '--name', 'cli-node', '--slots', '2',
    '--base-port', String(basePort), '--playwright-cli', h.FAKE_CLI, '--status-file', statusFile], { stdio: 'ignore' });
  h.cleanups.push(async () => child.kill());
  const status = await h.waitFor(() => {
    try {
      const s = JSON.parse(fs.readFileSync(statusFile, 'utf8'));
      return s.registered && s.slots.every((x) => x.healthy) && s;
    } catch {
      return false;
    }
  }, { what: 'CLI agent healthy' });
  assert.equal(status.pid, child.pid);
  assert.ok(hub.nodes.has('cli-node'));
  const pids = status.slots.map((s) => s.pid);
  child.kill('SIGKILL');
  const gone = (pid) => {
    try {
      process.kill(pid, 0);
      return false;
    } catch {
      return true;
    }
  };
  await h.waitFor(() => pids.every(gone), { timeout: 10000, what: 'orphaned run-servers to exit' });
  await h.waitFor(() => !hub.nodes.has('cli-node'), { what: 'hub drops the node' });
});

test('a malformed request line gets 400 and the hub keeps serving', async () => {
  const { port, http } = await h.startHub();
  const raw = h.rawSocket(port, 'GET http://[ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n');
  assert.match(await raw.response, /^HTTP\/1\.1 400 /);
  assert.equal((await h.getJson(`${http}/healthz`)).status, 200);
});

test('node names are case-insensitive: stored lower-case, routed and replaced regardless of case', async () => {
  const { url, hub, registry } = await h.startHub();
  const first = await h.rawAgent(url);
  assert.deepEqual(await h.register(first, 'Runner-A'), { type: 'registered', name: 'runner-a' });
  assert.deepEqual([...hub.nodes.keys()], ['runner-a']);
  assert.ok(registry.nodes.has('runner-a'));

  // A client using another spelling still reaches the node.
  const opened = h.nextMessage(first).then(({ data }) => JSON.parse(data));
  h.connect(`${url}/connect?node=RUNNER-a`).catch(() => {});
  assert.equal((await opened).type, 'open');

  // The same node in different case replaces the connection instead of becoming a second node.
  const replaced = h.closed(first);
  const second = await h.rawAgent(url);
  assert.deepEqual(await h.register(second, 'runner-A'), { type: 'registered', name: 'runner-a' });
  assert.equal((await replaced).code, 4409);
  assert.deepEqual([...hub.nodes.keys()], ['runner-a']);
  assert.deepEqual([...registry.nodes.keys()], ['runner-a']);
});

test('a held /connect upgrade is dropped as soon as the client leaves, and stray bytes get 400', async () => {
  const { url, port, hub } = await h.startHub({ openTimeoutMs: 10000 });
  const raw = await h.rawAgent(url);
  await h.register(raw, 'held', 2);

  let opened = h.nextMessage(raw).then(({ data }) => JSON.parse(data));
  const client = h.rawSocket(port, h.upgradeRequest('/connect?node=held'));
  const open = await opened;
  assert.equal(hub.pending.size, 1);
  client.socket.end(); // half-close: only a FIN reaches the hub
  await h.waitFor(() => hub.pending.size === 0, { timeout: 1000, what: 'pending open dropped on FIN' });
  await assert.rejects(h.connect(`${url}/agent/data/${open.id}`), { status: 404 });

  opened = h.nextMessage(raw).then(({ data }) => JSON.parse(data));
  const chatty = h.rawSocket(port, h.upgradeRequest('/connect?node=held'));
  await opened;
  chatty.socket.write('too early');
  assert.match(await chatty.response, /^HTTP\/1\.1 400 /);
  assert.equal(hub.pending.size, 0);
});

test('the hub closes a session whose client stops answering pings even though the agent side answers', async () => {
  const { url, hub } = await h.startHub({ heartbeatTimeoutMs: 3000, sessionTimeoutMs: 2000 });
  const agent = await h.startAgent(url, { name: 'halfopen' });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });

  const dead = await h.connect(`${url}/connect`);
  const live = await h.connect(`${url}/connect`);
  // A client that stopped answering pings (its host is gone but the TCP connection is not).
  dead._autoPong = false;
  await h.waitFor(() => hub.sessions.size === 2, { what: 'two sessions' });

  await h.waitFor(() => dead.readyState === 3, { timeout: 8000, what: 'silent client closed' });
  await h.waitFor(() => hub.sessions.size === 1 && agent.sessions.size === 1, { what: 'dead session closed on both sides' });
  live.send('still here');
  assert.equal((await h.nextMessage(live)).data.toString(), 'still here');
});

test('the agent ends sessions whose data channel goes silent (hub host gone without a FIN)', async () => {
  const { port, url } = await h.startHub();
  const proxy = await h.freezableProxy(port);
  const agent = await h.startAgent(`ws://127.0.0.1:${proxy.port}`, { name: 'blackhole' }, { heartbeatMs: 200, hubSilenceMs: 1500 });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });
  const client = await h.connect(`${url}/connect`);
  client.send('hi');
  assert.equal((await h.nextMessage(client)).data.toString(), 'hi');
  assert.equal(agent.sessions.size, 1);
  const [session] = agent.sessions;

  await h.sleep(2500); // idle but healthy: the pings keep it alive
  assert.equal(agent.sessions.size, 1);

  proxy.freeze();
  await h.waitFor(() => agent.sessions.size === 0, { timeout: 6000, what: 'silent session closed' });
  await h.waitFor(() => session.upstream.readyState === 3, { what: 'run-server leg closed' });
});

test('pipe applies backpressure: a client that stops reading does not grow the hub buffers', async () => {
  const { url, hub } = await h.startHub();
  const agent = await h.startAgent(url, { name: 'slowreader' });
  await h.waitFor(() => h.allHealthy(agent), { what: 'agent ready' });
  const client = await h.connect(`${url}/connect`);
  await h.waitFor(() => hub.sessions.size === 1, { what: 'session' });
  const [session] = hub.sessions;

  const received = [];
  client.on('message', (data) => received.push(data.length));
  client.pause();
  const frames = 48;
  const frame = Buffer.alloc(1024 * 1024, 1);
  for (let i = 0; i < frames; i++) client.send(frame);
  let peak = 0;
  const end = Date.now() + 2500;
  while (Date.now() < end) {
    peak = Math.max(peak, session.client.bufferedAmount);
    await h.sleep(10);
  }
  assert.ok(peak < 16 * 1024 * 1024, `hub buffered ${peak} bytes for a stalled client`);

  client.resume();
  await h.waitFor(() => received.length === frames, { timeout: 20000, what: 'every echoed frame after resuming' });
  assert.ok(received.every((n) => n === frame.length));
});

test('an ephemeral node is removed once it has been gone for the grace period', async () => {
  const { url, http, registry, hub } = await h.startHub({ ephemeralGraceMs: 400 });
  const agent = await h.startAgent(url, { name: 'Container-1', ephemeral: true });
  const keeper = await h.startAgent(url, { name: 'keeper' });
  await h.waitFor(() => agent.registered && keeper.registered, { what: 'registered' });
  const { body } = await h.getJson(`${http}/api/nodes`);
  assert.deepEqual(body.nodes.map((n) => [n.name, n.ephemeral]).sort(), [['container-1', true], ['keeper', false]]);
  assert.equal(registry.nodes.get('container-1').ephemeral, 1);

  await agent.stop();
  await keeper.stop();
  await h.waitFor(() => !registry.nodes.has('container-1'), { what: 'ephemeral node removed' });
  assert.equal(registry.slots.has('container-1'), false);
  await h.sleep(500);
  assert.equal(registry.nodes.get('keeper').online, 0); // a normal node stays, offline
  assert.equal(hub.nodes.size, 0);
});

test('an ephemeral node that reconnects within the grace period is kept', async () => {
  const { url, registry } = await h.startHub({ ephemeralGraceMs: 800 });
  let ws = await h.rawAgent(url);
  await h.register(ws, 'blip', 1, { ephemeral: true });
  ws.terminate();
  await h.waitFor(() => registry.nodes.get('blip').online === 0, { what: 'offline' });
  ws = await h.rawAgent(url);
  await h.register(ws, 'blip', 1, { ephemeral: true });
  await h.sleep(1200);
  assert.equal(registry.nodes.get('blip').online, 1);
  assert.deepEqual([...registry.slots.get('blip').keys()], [1]);
});

test('an ephemeral node keeps slots still in use until a later sweep finds them released', async () => {
  const { url, registry, hub } = await h.startHub({ ephemeralGraceMs: 300 });
  const ws = await h.rawAgent(url);
  await h.register(ws, 'busy', 3, { ephemeral: true });
  registry.slots.get('busy').get(2).inUse = 1; // a check holds slot 2
  ws.terminate();
  await h.waitFor(() => registry.slots.get('busy').size === 1, { what: 'free slots removed' });
  assert.equal(registry.nodes.get('busy').online, 0);
  assert.equal(registry.slots.get('busy').get(2).inUse, 1);
  assert.equal(registry.slots.get('busy').get(2).status, 0);

  registry.slots.get('busy').get(2).inUse = 0;
  await hub.sweepEphemeral();
  assert.equal(registry.nodes.has('busy'), false);
  assert.equal(registry.slots.has('busy'), false);
});

test('the start-up sweep removes ephemeral nodes that went away while the hub was down', async () => {
  const registry = new MemoryRegistry();
  const info = (name, ephemeral) => ({ name, slots: [{ index: 1, healthy: true }], browsers: [], ephemeral });
  await registry.register(info('gone', true));
  await registry.register(info('recent', true));
  await registry.register(info('static', false));
  const old = new Date(Date.now() - 600000).toISOString();
  registry.nodes.get('gone').lastSeen = old;
  registry.nodes.get('static').lastSeen = old;
  await h.startHub({ ephemeralGraceMs: 120000 }, registry);
  assert.deepEqual([...registry.nodes.keys()].sort(), ['recent', 'static']);
  assert.equal(registry.nodes.get('recent').online, 0);
});
