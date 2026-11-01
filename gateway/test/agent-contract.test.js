'use strict';

// Black-box contract for a gateway agent: anything that implements the agent protocol (agent.js, the Go
// pwrunner) must pass it. The agent runs as a child process with its default timings, so the waits here
// are sized for those (register <= 15 s, slot restart <= 20 s, the 4409 back-off 30 s).
//
//   node --test test/agent-contract.test.js                                  # node agent.js
//   AGENT_CMD="C:\pwbuild\pwrunner.exe run" node --test test/agent-contract.test.js
//
// AGENT_CMD is split on spaces (quote a path that has some); a leading `node` is this Node binary.
// AGENT_VERSION, when set, is the agentVersion the agent must report (default for agent.js: 1.0.0).

const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test, afterEach } = require('node:test');
const h = require('./helpers');

const TOKEN = 'contract-agent-token';
const GATEWAY = path.join(__dirname, '..');

function agentCommand() {
  const raw = process.env.AGENT_CMD || 'node agent.js';
  const parts = (raw.match(/"[^"]*"|\S+/g) || []).map((p) => p.replace(/^"|"$/g, ''));
  if (parts[0] === 'node') parts[0] = process.execPath;
  return { file: parts[0], args: parts.slice(1), isDefault: !process.env.AGENT_CMD };
}

afterEach(h.cleanup);

function killTree(pid) {
  if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
  else try { process.kill(pid, 'SIGKILL'); } catch {}
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

// Starts the agent under test; resolves once its status file says it is registered with healthy slots
// (unless wait is false).
async function spawnAgent(hubUrl, { name, slots = 2, wait = true, token = TOKEN } = {}) {
  const { file, args } = agentCommand();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-contract-'));
  const statusFile = path.join(dir, 'runtime.json');
  const basePort = await h.freePorts(slots);
  const env = { ...process.env, GATEWAY_AGENT_TOKEN: token };
  for (const v of ['HUB_URL', 'NODE_NAME', 'SLOTS', 'BASE_PORT', 'PLAYWRIGHT_CLI', 'BROWSERS', 'STATUS_FILE', 'LOG_DIR', 'EPHEMERAL']) delete env[v];
  const child = spawn(file, [...args, '--hub', hubUrl, '--name', name, '--slots', String(slots), '--base-port', String(basePort),
    '--playwright-cli', h.FAKE_CLI, '--status-file', statusFile], { cwd: GATEWAY, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  let output = '';
  child.stdout.on('data', (d) => { output += d; });
  child.stderr.on('data', (d) => { output += d; });
  const exited = new Promise((resolve) => child.on('exit', (code, signal) => resolve({ code, signal })));
  h.cleanups.push(async () => {
    if (child.exitCode === null && child.signalCode === null) killTree(child.pid);
    await Promise.race([exited, h.sleep(5000)]);
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const agent = { child, statusFile, basePort, exited, output: () => output, status: () => readStatus(statusFile) };
  if (wait) {
    await h.waitFor(() => {
      const s = agent.status();
      return s && s.registered && s.slots.length === slots && s.slots.every((x) => x.healthy);
    }, { timeout: 15000, what: `agent ${name} registered with healthy slots\n${output}` });
  }
  return agent;
}

function readStatus(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

async function node(http, name) {
  const { body } = await h.getJson(`${http}/api/nodes`);
  return body.nodes.find((n) => n.name === name);
}

test('registers under its lower-cased name with healthy slots, and reports itself in /api/nodes and the status file', async () => {
  const { url, http } = await h.startHub({ agentToken: TOKEN, heartbeatTimeoutMs: 6000 });
  const agent = await spawnAgent(url, { name: 'Contract-A' });
  const n = await h.waitFor(async () => { const x = await node(http, 'contract-a'); return x && x.healthySlots === 2 && x; }, { what: 'node listed' });
  assert.equal(n.slots, 2);
  assert.equal(n.playwrightVersion, '0.0.0-fake');
  assert.equal(n.platform, process.platform);
  assert.deepEqual(n.browsers, ['chromium']);
  assert.equal(n.ephemeral, false);
  const want = process.env.AGENT_VERSION || (agentCommand().isDefault ? '1.0.0' : null);
  if (want) assert.equal(n.agentVersion, want);
  else assert.ok(typeof n.agentVersion === 'string' && n.agentVersion.length > 0);

  const status = agent.status();
  assert.deepEqual(Object.keys(status), ['name', 'hub', 'agentVersion', 'playwrightVersion', 'pid', 'connected', 'registered', 'lastError', 'updated', 'slots']);
  assert.equal(status.name, 'contract-a');
  assert.equal(status.hub, url);
  assert.equal(status.agentVersion, n.agentVersion);
  assert.equal(status.pid, agent.child.pid);
  assert.equal(status.connected, true);
  assert.equal(status.lastError, null);
  assert.match(status.updated, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
  assert.deepEqual(Object.keys(status.slots[0]), ['index', 'port', 'pid', 'healthy', 'restarts', 'since']);
  assert.deepEqual(status.slots.map((s) => [s.index, s.port, s.restarts]), [[1, agent.basePort, 0], [2, agent.basePort + 1, 0]]);
  assert.match(status.slots[0].since, /Z$/);
  assert.ok(status.slots.every((s) => s.pid > 0));
  // The hub's 6 s heartbeat timeout passes several times over while the agent stays listed.
  await h.sleep(13000);
  assert.equal((await node(http, 'contract-a')).healthySlots, 2);
});

test('pipes frames and forwards the client headers and query to run-server', async () => {
  const { url } = await h.startHub({ agentToken: TOKEN, clientToken: 'client-secret' });
  const agent = await spawnAgent(url, { name: 'contract-pipe' });
  const ws = await h.connect(`${url}/connect?node=contract-pipe&browser=chromium&gateway_token=client-secret&x=a%20b`, {
    'x-playwright-browser': 'chromium',
    'x-playwright-launch-options': '{"headless":true}',
    'user-agent': 'Playwright/0.0.0 (contract)',
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
  const userAgents = [];
  for (let i = 0; i < info.rawHeaders.length; i += 2) if (info.rawHeaders[i].toLowerCase() === 'user-agent') userAgents.push(info.rawHeaders[i + 1]);
  assert.deepEqual(userAgents, ['Playwright/0.0.0 (contract)']);
  assert.equal(info.headers.authorization, undefined);
  assert.equal(info.headers['x-gateway-token'], undefined);
  assert.ok([agent.basePort, agent.basePort + 1].some((p) => info.headers.host === `127.0.0.1:${p}`), info.headers.host);
});

test('mirrors close codes both ways and maps unsendable ones', async () => {
  const { url } = await h.startHub({ agentToken: TOKEN });
  await spawnAgent(url, { name: 'contract-close', slots: 1 });

  let ws = await h.connect(`${url}/connect`);
  const closing = h.closed(ws);
  ws.send('close:4321:server says bye');
  assert.deepEqual(await closing, { code: 4321, reason: 'server says bye' });

  ws = await h.connect(`${url}/connect`);
  ws.close(4123, 'client says bye');
  await h.closed(ws);
  await h.sleep(300);
  ws = await h.connect(`${url}/connect`);
  ws.send('lastclose');
  assert.deepEqual(JSON.parse((await h.nextMessage(ws)).data), { code: 4123, reason: 'client says bye' });

  ws.terminate();
  await h.sleep(500);
  ws = await h.connect(`${url}/connect`);
  ws.send('lastclose');
  assert.equal(JSON.parse((await h.nextMessage(ws)).data).code, 1011);

  ws.close();
  await h.closed(ws);
  await h.sleep(300);
  ws = await h.connect(`${url}/connect`);
  ws.send('lastclose');
  assert.equal(JSON.parse((await h.nextMessage(ws)).data).code, 1000);
});

test("passes run-server's 428 on without failing the slot, and turns its 400 into a 502", async () => {
  const { url } = await h.startHub({ agentToken: TOKEN });
  await spawnAgent(url, { name: 'contract-refuse' });
  await assert.rejects(h.connect(`${url}/connect?node=contract-refuse`, { 'x-fake-reject': '428' }),
    (err) => err.status === 428 && /^Playwright version mismatch: the client is 1\.50, node contract-refuse slot \d runs 0\.0\./.test(err.body));
  const ok = await h.connect(`${url}/connect?node=contract-refuse`);
  ok.send('still fine');
  assert.equal((await h.nextMessage(ok)).data.toString(), 'still fine');
  ok.close();
  await assert.rejects(h.connect(`${url}/connect?node=contract-refuse`, { 'x-fake-reject': '400' }),
    (err) => err.status === 502 && /run-server on port \d+ answered HTTP 400/.test(err.body));
});

test('a second agent with the same name replaces the first, which backs off for 30 s', async () => {
  const { url, hub } = await h.startHub({ agentToken: TOKEN });
  const first = await spawnAgent(url, { name: 'contract-twin', slots: 1 });
  const second = await spawnAgent(url, { name: 'contract-twin', slots: 1, wait: false });
  await h.waitFor(() => { const s = first.status(); return s && !s.connected && /4409/.test(s.lastError || ''); }, { timeout: 15000, what: 'first agent replaced' });
  await h.waitFor(() => { const s = second.status(); return s && s.registered; }, { timeout: 15000, what: 'second agent registered' });
  const ws = await h.connect(`${url}/connect?node=contract-twin`);
  ws.send('who');
  assert.equal((await h.nextMessage(ws)).data.toString(), 'who');
  const replacedAt = Date.now();
  await h.waitFor(() => Date.now() - replacedAt > 25000, { timeout: 30000, interval: 1000, what: 'back-off window' });
  assert.equal(first.status().connected, false);
  assert.ok(hub.nodes.has('contract-twin'));
});

test('restarts a run-server that dies', async () => {
  const { url, http } = await h.startHub({ agentToken: TOKEN });
  const agent = await spawnAgent(url, { name: 'contract-crash' });
  const oldPid = agent.status().slots[0].pid;
  killTree(oldPid);
  await h.waitFor(() => { const s = agent.status(); return s && !s.slots[0].healthy; }, { timeout: 10000, what: 'slot unhealthy' });
  const after = await h.waitFor(() => { const s = agent.status(); return s && s.slots[0].healthy && s.slots[0].restarts === 1 && s; }, { timeout: 20000, what: 'slot restarted' });
  assert.notEqual(after.slots[0].pid, oldPid);
  assert.equal(after.slots[1].restarts, 0);
  await h.waitFor(async () => (await node(http, 'contract-crash')).healthySlots === 2, { what: 'hub sees 2 healthy slots' });
});

test('run-servers exit when the agent is killed hard', async () => {
  const { url, hub } = await h.startHub({ agentToken: TOKEN });
  const agent = await spawnAgent(url, { name: 'contract-orphan' });
  const pids = agent.status().slots.map((s) => s.pid);
  if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(agent.child.pid), '/F'], { stdio: 'ignore', windowsHide: true });
  else agent.child.kill('SIGKILL');
  await h.waitFor(() => pids.every((pid) => !alive(pid)), { timeout: 10000, what: 'orphaned run-servers to exit' });
  await h.waitFor(() => !hub.nodes.has('contract-orphan'), { timeout: 15000, what: 'hub drops the node' });
});

// Windows has no signal Node can send that a child handles; the Go integration tests cover Ctrl-Break.
test('a graceful stop closes sessions with 1001 and exits 0', { skip: process.platform === 'win32' && 'no catchable signal on Windows' }, async () => {
  const { url } = await h.startHub({ agentToken: TOKEN });
  const agent = await spawnAgent(url, { name: 'contract-stop' });
  const ws = await h.connect(`${url}/connect?node=contract-stop`);
  const closing = h.closed(ws);
  agent.child.kill('SIGTERM');
  assert.equal((await closing).code, 1001);
  const { code } = await Promise.race([agent.exited, h.sleep(10000).then(() => ({ code: 'timeout' }))]);
  assert.equal(code, 0);
});

test('keeps retrying with a rejected token and reports 4401', async () => {
  const { url } = await h.startHub({ agentToken: TOKEN });
  const agent = await spawnAgent(url, { name: 'contract-badtoken', token: 'wrong', wait: false });
  await h.waitFor(() => { const s = agent.status(); return s && /4401/.test(s.lastError || '') && !s.registered; }, { timeout: 15000, what: '4401 in lastError' });
  assert.equal(agent.child.exitCode, null);
});
