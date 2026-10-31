'use strict';

// Gateway agent: runs N `playwright run-server` processes on 127.0.0.1, keeps them alive, and dials
// OUT to the hub so the host needs no inbound ports. Each session the hub asks for gets its own
// outbound data socket, piped to the slot's run-server.

const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const WebSocket = require('ws');
const { agentConfig } = require('./lib/config');
const { createLogger } = require('./lib/log');
const { pipe } = require('./lib/pipe');
const P = require('./lib/protocol');

const DEFAULT_TIMINGS = {
  heartbeatMs: 10000,
  healthMs: 5000,
  statusMs: 5000,
  restartBaseMs: 5000,
  restartMaxMs: 300000,
  stableMs: 60000, // a run-server that stays up this long resets its restart backoff
  reconnectBaseMs: 1000,
  reconnectMaxMs: 30000,
  replacedDelayMs: 30000, // after a 4409, so two agents with the same name don't take turns kicking each other
  hubSilenceMs: 45000, // no ping or frame from the hub for this long means the connection is dead
};

function readPlaywrightVersion(cli) {
  try {
    return JSON.parse(fs.readFileSync(path.join(path.dirname(cli), 'package.json'), 'utf8')).version || 'unknown';
  } catch {
    return 'unknown';
  }
}

function probe(port, timeoutMs = 2000) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port });
    const done = (ok) => {
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(timeoutMs, () => done(false));
    socket.once('connect', () => done(true));
    socket.once('error', () => done(false));
  });
}

// The watchdog preload lets run-server notice when the agent is gone; the original NODE_OPTIONS is
// restored inside the child so browsers and other grandchildren don't inherit it.
function childEnv() {
  const watchdog = path.join(__dirname, 'lib', 'parent-watchdog.js');
  const original = process.env.NODE_OPTIONS || '';
  return {
    ...process.env,
    NODE_OPTIONS: `${original} --require ${JSON.stringify(watchdog)}`.trim(),
    GATEWAY_AGENT_NODE_OPTIONS: original,
  };
}

function killTree(child) {
  return new Promise((resolve) => {
    if (!child || child.exitCode !== null || child.signalCode !== null) return resolve();
    const force = setTimeout(() => child.kill('SIGKILL'), 5000);
    const giveUp = setTimeout(resolve, 8000);
    child.once('exit', () => {
      clearTimeout(force);
      clearTimeout(giveUp);
      resolve();
    });
    // run-server's browsers are its children; on Windows they outlive a plain kill.
    if (process.platform === 'win32') {
      const tk = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
      tk.on('error', () => child.kill());
    } else {
      child.kill('SIGTERM');
    }
  });
}

class Slot {
  constructor(index, agent) {
    this.index = index;
    this.port = agent.config.basePort + index - 1;
    this.agent = agent;
    this.child = null;
    this.healthy = false;
    this.restarts = 0;
    this.since = null;
    this.backoff = agent.t.restartBaseMs;
    this.restartTimer = null;
  }

  start() {
    const { config, log, t } = this.agent;
    if (this.agent.stopping) return;
    let fd = null;
    if (config.logDir) {
      const file = path.join(config.logDir, `slot-${this.index}.log`);
      try {
        fs.mkdirSync(config.logDir, { recursive: true });
        if (fs.existsSync(file)) fs.renameSync(file, `${file}.prev`);
        fd = fs.openSync(file, 'w');
      } catch (err) {
        log.warn(`slot ${this.index}: can't write ${file} (${err.message}); logging to stdout`);
      }
    }
    const child = spawn(process.execPath, [config.cli, 'run-server', '--port', String(this.port), '--host', '127.0.0.1'], {
      env: childEnv(),
      stdio: ['ignore', fd ?? 'pipe', fd ?? 'pipe'],
      windowsHide: true,
    });
    if (fd !== null) fs.closeSync(fd);
    else {
      for (const stream of [child.stdout, child.stderr]) {
        let rest = '';
        stream.setEncoding('utf8');
        stream.on('data', (chunk) => {
          const lines = (rest + chunk).split(/\r?\n/);
          rest = lines.pop();
          for (const line of lines) if (line) log.info(`[slot ${this.index}] ${line}`);
        });
      }
    }
    this.child = child;
    const startedAt = Date.now();
    this.since = new Date(startedAt).toISOString();
    log.info(`slot ${this.index}: started run-server on 127.0.0.1:${this.port} (pid ${child.pid})`);

    let exited = false;
    const onExit = (code, signal, err) => {
      if (exited) return;
      exited = true;
      if (this.child === child) this.child = null;
      this.since = null;
      this.setHealthy(false);
      if (this.agent.stopping) return;
      if (Date.now() - startedAt >= t.stableMs) this.backoff = t.restartBaseMs;
      const delay = this.backoff;
      this.backoff = Math.min(this.backoff * 2, t.restartMaxMs);
      this.restarts++;
      const why = err ? err.message : signal ? `signal ${signal}` : `code ${code}`;
      log.warn(`slot ${this.index}: run-server exited (${why}); restarting in ${Math.round(delay / 1000)}s`);
      this.agent.changed();
      this.restartTimer = setTimeout(() => {
        this.restartTimer = null;
        this.start();
      }, delay);
    };
    child.on('exit', (code, signal) => onExit(code, signal));
    child.on('error', (err) => onExit(null, null, err));
    this.agent.changed();
    // Report the slot as soon as it listens rather than at the next regular check.
    const warmup = setInterval(() => {
      if (this.healthy || this.child !== child || Date.now() - startedAt > 30000) return clearInterval(warmup);
      this.check();
    }, 250);
  }

  async check() {
    const child = this.child;
    const alive = !!child && child.exitCode === null && child.signalCode === null;
    const ok = alive && (await probe(this.port));
    // The process may have died or been replaced while the probe ran.
    this.setHealthy(ok && this.child === child);
  }

  setHealthy(healthy) {
    if (this.healthy === healthy) return;
    this.healthy = healthy;
    this.agent.log.info(`slot ${this.index}: ${healthy ? 'healthy' : 'unhealthy'}`);
    this.agent.changed(true);
  }

  async stop() {
    clearTimeout(this.restartTimer);
    await killTree(this.child);
  }

  status() {
    return { index: this.index, port: this.port, pid: this.child ? this.child.pid : null, healthy: this.healthy, restarts: this.restarts, since: this.since };
  }
}

class Agent extends EventEmitter {
  constructor(config, { log = createLogger('agent'), timings = {} } = {}) {
    super();
    this.config = config;
    this.log = log;
    this.t = { ...DEFAULT_TIMINGS, ...timings };
    this.playwrightVersion = readPlaywrightVersion(config.cli);
    this.slots = Array.from({ length: config.slots }, (_, i) => new Slot(i + 1, this));
    this.ws = null;
    this.connected = false;
    this.registered = false;
    this.lastError = null;
    this.lastHubActivity = 0;
    this.reconnectDelay = this.t.reconnectBaseMs;
    this.reconnectTimer = null;
    this.sessions = new Set();
    this.timers = [];
    this.stopping = false;
  }

  start() {
    const { config, log, t } = this;
    log.info(`agent ${P.AGENT_VERSION} starting: node ${config.name}, ${config.slots} slots on 127.0.0.1:${config.basePort}-${config.basePort + config.slots - 1}, playwright ${this.playwrightVersion} (${config.cli}), hub ${config.hub}`);
    if (!config.token) log.warn('no GATEWAY_AGENT_TOKEN set; the hub will reject this agent if it requires one');
    for (const slot of this.slots) slot.start();
    this.timers.push(setInterval(() => this.slots.forEach((s) => s.check()), t.healthMs));
    this.timers.push(setInterval(() => this.tick(), t.heartbeatMs));
    if (config.statusFile) this.timers.push(setInterval(() => this.writeStatus(), t.statusMs));
    this.connect();
  }

  // Called on every state change; `urgent` pushes slot health to the hub without waiting for the next heartbeat.
  changed(urgent = false) {
    this.writeStatus();
    this.emit('status', this.status());
    if (urgent && this.registered) this.send({ type: 'heartbeat', slots: this.slotHealth() });
  }

  slotHealth() {
    return this.slots.map((s) => ({ index: s.index, healthy: s.healthy }));
  }

  send(message) {
    return P.safeSend(this.ws, message);
  }

  connect() {
    if (this.stopping) return;
    const { config, log } = this;
    const ws = new WebSocket(`${config.hub}/agent`, {
      headers: config.token ? { Authorization: `Bearer ${config.token}` } : {},
      handshakeTimeout: 10000,
      perMessageDeflate: false,
    });
    this.ws = ws;
    ws.on('open', () => {
      this.connected = true;
      this.lastHubActivity = Date.now();
      this.send({
        type: 'register',
        name: config.name,
        slots: this.slotHealth(),
        playwrightVersion: this.playwrightVersion,
        agentVersion: P.AGENT_VERSION,
        platform: process.platform,
        browsers: config.browsers,
        ephemeral: !!config.ephemeral,
      });
      this.changed();
    });
    ws.on('ping', () => { this.lastHubActivity = Date.now(); });
    ws.on('pong', () => { this.lastHubActivity = Date.now(); });
    ws.on('message', (data, isBinary) => {
      this.lastHubActivity = Date.now();
      const msg = P.parseMessage(data, isBinary);
      if (!msg) return log.warn('ignoring a malformed frame from the hub');
      if (msg.type === 'registered') {
        this.registered = true;
        this.lastError = null;
        this.reconnectDelay = this.t.reconnectBaseMs;
        log.info(`registered with ${config.hub} as ${msg.name}`);
        this.changed();
        this.emit('registered');
      } else if (msg.type === 'open') {
        this.openSession(msg);
      } else if (msg.type === 'error') {
        this.lastError = String(msg.message);
        log.error(`hub: ${msg.message}`);
      }
    });
    ws.on('error', (err) => {
      this.lastError = err.message;
      log.warn(`hub connection: ${err.message}`);
    });
    ws.on('close', (code, reason) => {
      if (this.ws === ws) this.ws = null;
      const wasRegistered = this.registered;
      this.connected = false;
      this.registered = false;
      if (code !== 1006 || !this.lastError) this.lastError = `hub closed the connection (${code}${reason.length ? ` ${reason}` : ''})`;
      if (this.stopping) return this.changed();
      let delay = this.reconnectDelay + Math.floor(Math.random() * this.reconnectDelay * 0.3);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.t.reconnectMaxMs);
      if (code === P.CLOSE.REPLACED) {
        delay = Math.max(delay, this.t.replacedDelayMs);
        log.error(`another agent registered as "${config.name}" and replaced this one; give each node a unique --name / NODE_NAME. Retrying in ${Math.round(delay / 1000)}s`);
      } else if (code === P.CLOSE.UNAUTHORIZED) {
        // Missing, wrong, revoked or expired: only a new token fixes it, but keep trying in case it was a hub-side mistake.
        log.error(`the hub rejected the agent token (${reason.length ? reason : 'unauthorized'}); check --token / GATEWAY_AGENT_TOKEN or issue a new agent token. Retrying in ${Math.round(delay / 1000)}s`);
      } else if (code === P.CLOSE.FORBIDDEN) {
        // Either this token is bound to another name, or the name is reserved for the token bound to it.
        log.error(`the hub won't let this agent token register as "${config.name}" (${reason.length ? reason : 'forbidden'}); use the --name / NODE_NAME the token is bound to, or a token bound to this node. Retrying in ${Math.round(delay / 1000)}s`);
      } else if (code === P.CLOSE.TRY_AGAIN_LATER) {
        log.warn(`the hub can't check agent tokens right now (its database is unavailable); retrying in ${Math.round(delay / 1000)}s`);
      } else {
        log.warn(`${wasRegistered ? 'lost the hub' : 'could not reach the hub'} (${this.lastError}); retrying in ${Math.round(delay / 1000)}s`);
      }
      this.changed();
      this.emit('disconnected', code);
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this.connect();
      }, delay);
    });
  }

  tick() {
    this.checkSessions();
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - this.lastHubActivity > this.t.hubSilenceMs) {
      this.log.warn(`no word from the hub for ${Math.round(this.t.hubSilenceMs / 1000)}s; reconnecting`);
      this.lastError = 'hub stopped answering';
      return ws.terminate();
    }
    ws.ping();
    if (this.registered) this.send({ type: 'heartbeat', slots: this.slotHealth() });
  }

  // A data channel is its own TCP connection, so the control socket's health says nothing about it. The
  // hub pings every data channel and so does this, so silence means the hub (or the path to it) is gone;
  // ending both legs makes run-server close the browser it launched for the session.
  checkSessions() {
    const now = Date.now();
    for (const s of this.sessions) {
      if (s.data.readyState !== WebSocket.OPEN) continue;
      if (now - s.lastActivity > this.t.hubSilenceMs) {
        this.log.warn(`session ${s.id}: no word from the hub on its data channel for ${Math.round(this.t.hubSilenceMs / 1000)}s; closing it`);
        s.data.terminate();
        s.upstream.terminate();
      } else {
        s.data.ping();
      }
    }
  }

  openSession(msg) {
    const { config, log } = this;
    const id = typeof msg.id === 'string' && /^[0-9a-f-]{36}$/.test(msg.id) ? msg.id : null;
    if (!id) return log.warn('ignoring an open frame without a valid id');
    // status: the HTTP status run-server refused the upgrade with, so the hub can tell the client's fault
    // (428: its Playwright version) from the slot's.
    const failed = (message, status = undefined) => {
      log.warn(`session ${id}: ${message}`);
      this.send({ type: 'open-failed', id, message, status });
    };
    const slot = this.slots[msg.slot - 1];
    if (!slot) return failed(`no slot ${msg.slot}`);
    const reqPath = typeof msg.path === 'string' && msg.path.startsWith('/') ? msg.path : '/';
    const query = typeof msg.query === 'string' && msg.query ? `?${msg.query}` : '';
    const headers = {};
    for (const [key, value] of Object.entries(P.forwardHeaders(msg.headers && typeof msg.headers === 'object' ? msg.headers : {}))) {
      if (typeof value === 'string' || (Array.isArray(value) && value.every((v) => typeof v === 'string'))) headers[key] = value;
    }

    let settled = false;
    const upstream = new WebSocket(`ws://127.0.0.1:${slot.port}${reqPath}${query}`, {
      headers, perMessageDeflate: false, handshakeTimeout: 10000, maxPayload: P.MAX_PAYLOAD,
    });
    // run-server rejects a bad version or launch options with an HTTP error whose body says why.
    upstream.on('unexpected-response', (req, res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { if (body.length < 2000) body += chunk; });
      res.on('end', () => {
        settled = true;
        failed(`run-server on port ${slot.port} answered HTTP ${res.statusCode}${body.trim() ? `: ${body.trim()}` : ''}`, res.statusCode);
        upstream.terminate();
      });
      res.on('error', () => {});
    });
    upstream.on('error', (err) => {
      if (settled) return;
      settled = true;
      failed(`run-server on port ${slot.port}: ${err.message}`);
    });
    upstream.once('open', () => {
      settled = true;
      const data = new WebSocket(`${config.hub}/agent/data/${id}`, {
        headers: config.token ? { Authorization: `Bearer ${config.token}` } : {},
        perMessageDeflate: false, handshakeTimeout: 10000, maxPayload: P.MAX_PAYLOAD,
      });
      // If the data channel never opens, tell the hub now instead of letting the client wait for the timeout.
      data.once('error', (err) => {
        if (data.readyState !== WebSocket.OPEN) this.send({ type: 'open-failed', id, message: `data channel: ${err.message}` });
      });
      const session = { id, upstream, data, lastActivity: Date.now() };
      const seen = () => { session.lastActivity = Date.now(); };
      data.on('open', seen).on('ping', seen).on('pong', seen).on('message', seen);
      this.sessions.add(session);
      pipe(upstream, data, () => {
        this.sessions.delete(session);
        log.info(`session ${id} on slot ${slot.index} closed`);
      });
      log.info(`session ${id} on slot ${slot.index} opened`);
    });
  }

  status() {
    return {
      name: this.config.name,
      hub: this.config.hub,
      agentVersion: P.AGENT_VERSION,
      playwrightVersion: this.playwrightVersion,
      pid: process.pid,
      connected: this.connected,
      registered: this.registered,
      lastError: this.lastError,
      updated: new Date().toISOString(),
      slots: this.slots.map((s) => s.status()),
    };
  }

  // Readers never see a half-written file: write a temp file, then rename it over the old one.
  writeStatus() {
    const file = this.config.statusFile;
    if (!file) return;
    const tmp = `${file}.${process.pid}.tmp`;
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(tmp, `${JSON.stringify(this.status(), null, 2)}\n`);
      fs.renameSync(tmp, file);
      this.statusError = null;
    } catch (err) {
      // On Windows a reader holding the file open can make the rename fail; the next write retries.
      if (this.statusError !== err.message) this.log.warn(`can't write status file ${file}: ${err.message}`);
      this.statusError = err.message;
      fs.rm(tmp, { force: true }, () => {});
    }
  }

  async stop() {
    if (this.stopping) return;
    this.stopping = true;
    this.timers.forEach(clearInterval);
    clearTimeout(this.reconnectTimer);
    for (const { upstream, data } of this.sessions) {
      P.safeClose(data, 1001, 'agent is shutting down');
      P.safeClose(upstream, 1001, 'agent is shutting down');
    }
    if (this.ws) P.safeClose(this.ws, 1001, 'agent is shutting down');
    await Promise.all(this.slots.map((s) => s.stop()));
    this.connected = false;
    this.registered = false;
    this.writeStatus();
    if (this.ws) this.ws.terminate();
  }
}

function main() {
  const log = createLogger('agent');
  let config;
  try {
    config = agentConfig();
  } catch (err) {
    log.error(err.message);
    process.exit(2);
  }
  if (config.help) {
    process.stdout.write(config.usage);
    process.exit(0);
  }
  const agent = new Agent(config, { log });
  process.on('unhandledRejection', (err) => log.error(`unhandled rejection: ${err && err.stack ? err.stack : err}`));
  let exiting = false;
  const shutdown = (signal) => {
    if (exiting) return;
    exiting = true;
    log.info(`${signal} received, stopping run-servers`);
    setTimeout(() => process.exit(0), 10000).unref();
    agent.stop().finally(() => process.exit(0));
  };
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK']) {
    try {
      process.on(signal, () => shutdown(signal));
    } catch {}
  }
  agent.start();
}

if (require.main === module) main();

module.exports = { Agent, createAgent: (config, opts) => new Agent(config, opts) };
