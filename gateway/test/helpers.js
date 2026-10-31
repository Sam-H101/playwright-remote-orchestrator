'use strict';

const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const WebSocket = require('ws');
const { createHub } = require('../hub');
const { createAgent } = require('../agent');
const { MemoryRegistry } = require('../lib/registry');
const { silent } = require('../lib/log');

const FAKE_CLI = path.join(__dirname, 'fixtures', 'fake-playwright', 'cli.js');
const FAST = {
  heartbeatMs: 300, healthMs: 100, statusMs: 200, restartBaseMs: 200, restartMaxMs: 1000,
  reconnectBaseMs: 100, reconnectMaxMs: 400, hubSilenceMs: 5000,
};

const cleanups = [];
async function cleanup() {
  while (cleanups.length) await cleanups.pop()().catch(() => {});
}

async function waitFor(fn, { timeout = 10000, interval = 25, what = 'condition' } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, interval));
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function listenOn(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(false));
    srv.listen(port, '127.0.0.1', () => srv.close(() => resolve(true)));
  });
}

// n consecutive free ports for the slots.
async function freePorts(n) {
  for (;;) {
    const base = 20000 + Math.floor(Math.random() * 30000);
    let ok = true;
    for (let i = 0; i < n && ok; i++) ok = await listenOn(base + i);
    if (ok) return base;
  }
}

function hubConfig(overrides = {}) {
  return {
    port: 0, host: '127.0.0.1', agentToken: null, clientToken: null, db: 'memory',
    heartbeatTimeoutMs: 30000, openTimeoutMs: 5000, ...overrides,
  };
}

async function startHub(overrides = {}, registry = new MemoryRegistry()) {
  const hub = createHub(hubConfig(overrides), { log: silent, registry });
  const port = await hub.start();
  cleanups.push(() => hub.stop());
  return { hub, port, registry, url: `ws://127.0.0.1:${port}`, http: `http://127.0.0.1:${port}` };
}

let tmpDir;
function tmp() {
  tmpDir = tmpDir || fs.mkdtempSync(path.join(os.tmpdir(), 'gateway-test-'));
  return tmpDir;
}

async function startAgent(hubUrl, overrides = {}, timings = {}) {
  const slots = overrides.slots || 2;
  const name = overrides.name || `node-${Math.random().toString(36).slice(2, 8)}`;
  const config = {
    hub: hubUrl, token: null, name, slots, basePort: await freePorts(slots), cli: FAKE_CLI, browsers: ['chromium'],
    statusFile: path.join(tmp(), `${name}.status.json`), logDir: null, ...overrides,
  };
  const agent = createAgent(config, { log: silent, timings: { ...FAST, ...timings } });
  agent.start();
  cleanups.push(() => agent.stop());
  return agent;
}

const allHealthy = (agent) => agent.registered && agent.slots.every((s) => s.healthy);

// Resolves with the open socket, or rejects with { status, body } for a plain HTTP answer.
function connect(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url, { headers });
    ws.on('unexpected-response', (req, res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => {
        reject(Object.assign(new Error(`HTTP ${res.statusCode}`), { status: res.statusCode, body }));
        req.destroy();
      });
    });
    ws.once('open', () => {
      cleanups.push(async () => ws.terminate());
      resolve(ws);
    });
    ws.once('error', reject);
  });
}

function nextMessage(ws) {
  return new Promise((resolve, reject) => {
    ws.once('message', (data, isBinary) => resolve({ data, isBinary }));
    ws.once('close', (code) => reject(new Error(`closed ${code}`)));
  });
}

function closed(ws) {
  return new Promise((resolve) => ws.once('close', (code, reason) => resolve({ code, reason: reason.toString() })));
}

// A hand-driven agent for protocol edge cases.
function rawAgent(hubUrl, opts = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${hubUrl}/agent`, opts);
    ws.once('open', () => {
      cleanups.push(async () => ws.terminate());
      resolve(ws);
    });
    ws.once('error', reject);
  });
}

function register(ws, name, slots = 1, extra = {}) {
  ws.send(JSON.stringify({
    type: 'register', name, slots: Array.from({ length: slots }, (_, i) => ({ index: i + 1, healthy: true })),
    playwrightVersion: '1.64.1', agentVersion: 'test', platform: 'linux', browsers: ['chromium'], ...extra,
  }));
  return nextMessage(ws).then(({ data }) => JSON.parse(data));
}

async function getJson(url) {
  const res = await fetch(url);
  return { status: res.status, body: await res.json() };
}

// Sends raw bytes and collects whatever comes back; `response` resolves when the server closes.
function rawSocket(port, text) {
  const socket = net.connect(port, '127.0.0.1');
  cleanups.push(async () => socket.destroy());
  let received = '';
  socket.setEncoding('utf8');
  socket.on('data', (chunk) => { received += chunk; });
  socket.on('error', () => {});
  const response = new Promise((resolve) => socket.on('close', () => resolve(received)));
  if (text) socket.write(text);
  return { socket, response, received: () => received };
}

function upgradeRequest(pathname) {
  return `GET ${pathname} HTTP/1.1\r\nHost: hub\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n` +
    'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n';
}

// A TCP relay to the hub that can be frozen: it then forwards nothing and passes on no FIN or RST,
// like a hub host that lost power or a network path that black-holes traffic.
async function freezableProxy(targetPort) {
  const pairs = new Set();
  let frozen = false;
  const server = net.createServer((c) => {
    const u = net.connect(targetPort, '127.0.0.1');
    const pair = [c, u];
    pairs.add(pair);
    c.on('data', (d) => { if (!frozen) u.write(d); });
    u.on('data', (d) => { if (!frozen) c.write(d); });
    for (const [a, b] of [[c, u], [u, c]]) {
      a.on('error', () => {});
      a.on('close', () => {
        if (!frozen) b.destroy();
        if (b.destroyed) pairs.delete(pair);
      });
    }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const close = async () => {
    server.close();
    for (const pair of pairs) pair.forEach((s) => s.destroy());
  };
  cleanups.push(close);
  return { port: server.address().port, freeze: () => { frozen = true; }, close };
}

module.exports = {
  FAKE_CLI, cleanups, cleanup, waitFor, sleep, freePorts, hubConfig, startHub, startAgent, allHealthy,
  connect, nextMessage, closed, rawAgent, register, getJson, rawSocket, upgradeRequest, freezableProxy,
};
