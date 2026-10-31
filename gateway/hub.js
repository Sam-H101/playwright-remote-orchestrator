'use strict';

// Gateway hub: the one port everything talks to. Agents dial out to /agent (control) and
// /agent/data/<id> (one per session). Checks connect to /connect (optionally ?node=<name>): the hub
// picks a free slot that offers the browser and Playwright version the client asked for, claims it
// in portUsage, and pipes the client through to that slot's run-server, on the agent's host or on a
// legacy static host. Clients wait in a queue while every matching slot is busy.

const crypto = require('node:crypto');
const http = require('node:http');
const WebSocket = require('ws');
const { hubConfig } = require('./lib/config');
const { createLogger } = require('./lib/log');
const { pipe } = require('./lib/pipe');
const { createRegistry } = require('./lib/registry');
const { createTokenAuth } = require('./lib/tokens');
const P = require('./lib/protocol');
const R = require('./lib/routing');

const REGISTER_TIMEOUT_MS = 10000;
const EPHEMERAL_SWEEP_MS = 60000;
const QUEUE_POLL_MS = 1000; // re-reads the slots while clients wait: other claimers release without telling the hub
const CLAIM_SYNC_MS = 30000;
const SLOT_ROUTE_GONE = 'the /connect/<node>/<slot> endpoints were removed: connect to /connect and the hub picks a free slot '
  + 'for the browser you launch (add ?node=<name> to stay on one node)';

function createHub(config, { log = createLogger('hub'), registry = createRegistry(config, log) } = {}) {
  // Control frames are small; only the session tunnels need Playwright-sized frames.
  const controlWss = new WebSocket.WebSocketServer({ noServer: true, maxPayload: 1024 * 1024, clientTracking: false });
  const wss = new WebSocket.WebSocketServer({ noServer: true, maxPayload: P.MAX_PAYLOAD, clientTracking: false });
  const server = http.createServer(onRequest);
  const nodes = new Map(); // name -> registered agent connection
  const agents = new Set(); // every control connection, registered or not
  const pending = new Map(); // open id -> session an agent was asked to open
  const sessions = new Set(); // piped [client, data] pairs
  // /connect clients from the token check to the 101: queued, claiming, opening. `queue` holds the queued
  // ones in arrival order.
  const held = new Set();
  const queue = [];
  // This hub's claims in portUsage, by claimedBy. A restarted hub frees every 'hub:' claim, so the prefix
  // only has to tell this instance's claims from those of a hub that ran before it.
  const claimPrefix = `hub:${crypto.randomBytes(4).toString('hex')}:`;
  const claims = new Map();
  const unreleased = new Map(); // claimedBy -> slot id: releases the database refused, retried by the claim sync
  const lostClaims = new Set(); // claimedBy of claims taken from this hub, which it then never releases
  const releasing = new Set(); // releases on their way to the database
  const queuePollMs = config.queuePollMs ?? QUEUE_POLL_MS;
  const claimSyncMs = config.claimSyncMs ?? CLAIM_SYNC_MS;
  const queueTimeoutMs = config.queueTimeoutMs ?? 60000;
  const queueMax = config.queueMax ?? 200;
  const connectAttempts = config.connectAttempts ?? 3;
  const failThreshold = config.failThreshold ?? 3;
  const failCooldownMs = config.failCooldownMs ?? 300000;
  let claimSeq = 0;
  let clientSeq = 0;
  let pumpRun = null; // the pump() pass under way
  let pumpAgain = false;
  let pollTimer = null;
  let syncing = false;
  let claimSyncer = null;
  const dbQueues = new Map(); // name -> promise chain, so writes for one node stay in order
  const pingInterval = Math.max(1000, Math.min(10000, Math.floor(config.heartbeatTimeoutMs / 3)));
  // A session leg that answers no ping for this long is half-open (tests set it lower).
  const sessionTimeoutMs = config.sessionTimeoutMs ?? Math.max(config.heartbeatTimeoutMs, 60000);
  const ephemeralGraceMs = config.ephemeralGraceMs ?? 120000;
  const ephemeralSweepMs = config.ephemeralSweepMs ?? EPHEMERAL_SWEEP_MS;
  const reapTimers = new Map(); // ephemeral node name -> timer that removes it unless it reconnects
  const tokens = createTokenAuth({
    registry, log, envTokens: { agent: config.agentToken, client: config.clientToken },
    allowOpen: { agent: !!config.allowOpenAgents, client: !!config.allowOpenClients },
    cacheMs: config.tokenCacheMs ?? 10000, negativeCacheMs: config.tokenNegativeCacheMs, touchMs: config.tokenTouchMs,
  });
  const tokenRecheckMs = config.tokenRecheckMs ?? 15000;
  let tokenRechecker = null;
  let rechecking = false;
  let ephemeralSweeper = null;
  let keepalive = null;
  let stopping = false;

  function dbWrite(name, what, fn) {
    const prev = dbQueues.get(name) || Promise.resolve();
    const next = prev.then(fn).catch((err) => log.error(`db ${what} for ${name} failed: ${err.message}`));
    dbQueues.set(name, next);
    next.then(() => { if (dbQueues.get(name) === next) dbQueues.delete(name); });
    return next;
  }

  // Never lets a throw escape: an uncaught error here would end the process and every session with it.
  function onRequest(req, res) {
    const json = (status, body) => {
      if (res.headersSent) return res.end();
      res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      res.end(JSON.stringify(body));
    };
    let pathname;
    try {
      ({ pathname } = new URL(req.url, 'http://hub'));
    } catch {
      return json(400, { error: 'bad request' });
    }
    try {
      if (req.method !== 'GET' && req.method !== 'HEAD') return json(405, { error: 'method not allowed' });
      if (pathname === '/healthz') return json(200, { ok: true, db: registry.kind, nodes: nodes.size });
      if (pathname === '/api/nodes') return json(200, { nodes: [...nodes.values()].map(describe) });
      json(404, { error: 'not found' });
    } catch (err) {
      log.error(`request ${req.method} ${req.url} failed: ${err.stack || err.message}`);
      json(500, { error: 'internal error' });
    }
  }

  function describe(agent) {
    return {
      name: agent.name,
      slots: agent.slots.length,
      healthySlots: agent.slots.filter((s) => s.healthy).length,
      playwrightVersion: agent.info.playwrightVersion,
      agentVersion: agent.info.agentVersion,
      platform: agent.info.platform,
      browsers: agent.info.browsers,
      ephemeral: agent.info.ephemeral,
      remoteAddress: agent.remoteAddress,
      connectedAt: agent.connectedAt.toISOString(),
      lastSeen: agent.lastSeen.toISOString(),
    };
  }

  // Ends a held or rejected upgrade with plain HTTP so the client sees a real status, not a dropped socket.
  function reject(socket, status, message) {
    if (socket.destroyed) return;
    const body = `${message}\n`;
    socket.end(`HTTP/1.1 ${status} ${http.STATUS_CODES[status]}\r\nConnection: close\r\n` +
      `Content-Type: text/plain; charset=utf-8\r\nContent-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);
    setTimeout(() => socket.destroy(), 1000).unref();
  }

  function onUpgrade(req, socket, head) {
    socket.on('error', () => {});
    if (stopping) return reject(socket, 503, 'hub is shutting down');
    let url;
    try {
      url = new URL(req.url, 'http://hub');
    } catch {
      return reject(socket, 400, 'bad request');
    }
    const remote = (req.socket.remoteAddress || '').replace(/^::ffff:/, '');
    if (url.pathname === '/agent') return acceptAgent(req, socket, head);
    const data = /^\/agent\/data\/([0-9a-f-]{36})$/.exec(url.pathname);
    if (data) return acceptData(req, socket, head, data[1]);
    if (url.pathname === '/connect' || url.pathname === '/connect/') return acceptClient(req, socket, head, url, remote);
    if (url.pathname.startsWith('/connect/')) return reject(socket, 410, SLOT_ROUTE_GONE);
    reject(socket, 404, 'not found');
  }

  // What an agent connection authenticated with. The hash (never the token) is kept so the data channels
  // can be held to the same token and the periodic re-check can look it up again.
  function agentAuth(verdict, token) {
    const hash = token && verdict.via !== 'env' ? P.hashToken(token) : null;
    return { via: verdict.via, tokenId: verdict.tokenId, nodeName: verdict.nodeName, label: verdict.label, hash };
  }

  function acceptAgent(req, socket, head) {
    const remoteAddress = (req.socket.remoteAddress || '').replace(/^::ffff:/, '') || null;
    const token = P.bearer(req.headers.authorization);
    // The WebSocket is opened even for a refused token, so the agent gets a close code it can act on.
    tokens.check('agent', token).then((verdict) => {
      if (stopping) return reject(socket, 503, 'hub is shutting down');
      controlWss.handleUpgrade(req, socket, head, (ws) => {
        if (verdict.ok) return controlConnection(ws, remoteAddress, agentAuth(verdict, token));
        if (verdict.unavailable) {
          log.error(`agent from ${remoteAddress} turned away: ${verdict.message}`);
          P.safeSend(ws, { type: 'error', message: 'the hub cannot check agent tokens right now (database unavailable); try again later' });
          return P.safeClose(ws, P.CLOSE.TRY_AGAIN_LATER, 'token check unavailable');
        }
        log.warn(`agent from ${remoteAddress} rejected: ${verdict.message}`);
        P.safeSend(ws, { type: 'error', message: `agent token rejected: ${verdict.message}` });
        P.safeClose(ws, P.CLOSE.UNAUTHORIZED, verdict.problem ? `token ${verdict.problem}` : 'unauthorized');
      });
    }).catch((err) => {
      log.error(`agent from ${remoteAddress}: token check failed: ${err.stack || err.message}`);
      socket.destroy();
    });
  }

  function controlConnection(ws, remoteAddress, auth) {
    const agent = { ws, name: null, slots: [], info: null, auth, remoteAddress, connectedAt: null, lastSeen: new Date(), alive: Date.now(), opens: new Set(), synced: false };
    agents.add(agent);
    const registerTimer = setTimeout(() => fail('no register frame'), REGISTER_TIMEOUT_MS);

    function fail(message) {
      P.safeSend(ws, { type: 'error', message });
      P.safeClose(ws, P.CLOSE.BAD_REQUEST, message);
    }

    ws.on('pong', () => { agent.alive = Date.now(); });
    ws.on('error', (err) => log.warn(`agent ${agent.name || remoteAddress}: ${err.message}`));
    ws.on('message', (data, isBinary) => {
      agent.alive = Date.now();
      const msg = P.parseMessage(data, isBinary);
      if (!msg) return fail('expected a JSON control frame');
      if (!agent.name) {
        // Registering waits for the node-name check; nothing else is expected before 'registered'.
        if (agent.registering) return msg.type === 'register' ? fail('already registered') : undefined;
        if (msg.type !== 'register') return fail('first frame must be register');
        clearTimeout(registerTimer);
        agent.registering = true;
        return register(agent, msg, fail).catch((err) => {
          log.error(`agent ${remoteAddress}: register failed: ${err.stack || err.message}`);
          fail('register failed');
        }).finally(() => { agent.registering = false; });
      }
      if (msg.type === 'heartbeat') return heartbeat(agent, msg);
      if (msg.type === 'open-failed') return openFailed(agent, msg);
      if (msg.type === 'register') return fail('already registered');
      log.warn(`agent ${agent.name}: ignoring unknown frame type ${msg.type}`);
    });
    ws.on('close', (code) => {
      clearTimeout(registerTimer);
      agents.delete(agent);
      // Not the slot's fault, so no failure is counted; the client is tried on another slot.
      for (const id of [...agent.opens]) failOpen(id, 502, `node ${agent.name} disconnected`, { count: false });
      if (agent.name && nodes.get(agent.name) === agent) {
        nodes.delete(agent.name);
        log.info(`node ${agent.name} disconnected (${code})`);
        dbWrite(agent.name, 'disconnect', () => registry.disconnect(agent.name));
        if (agent.info.ephemeral && !stopping) scheduleReap(agent.name);
      }
    });
  }

  async function register(agent, msg, fail) {
    const slots = P.normalizeSlots(msg.slots);
    if (!P.isValidName(msg.name)) return fail('node name must be 1-128 characters of A-Z a-z 0-9 . _ -');
    if (!slots) return fail(`slots must be a list of 1-${P.MAX_SLOTS} entries indexed from 1`);
    const str = (v, max) => (typeof v === 'string' && v ? v.slice(0, max) : null);
    // MySQL compares names case-insensitively, so the hub does too: one canonical (lower-case) name.
    const name = P.canonicalName(msg.name);
    // Checked before anything else happens, so a token bound to another node can't even replace it.
    if (agent.auth.nodeName && agent.auth.nodeName !== name) {
      log.warn(`agent from ${agent.remoteAddress} rejected: ${agent.auth.label} is bound to node ${agent.auth.nodeName}, not ${name}`);
      P.safeSend(agent.ws, { type: 'error', message: `token not valid for this node name (${name})` });
      return P.safeClose(agent.ws, P.CLOSE.FORBIDDEN, 'token not valid for this node name');
    }
    // A node that registered with a token bound to its name keeps that name: no other token (unbound,
    // or the env bootstrap token) can replace it while it is connected, or take the name while it is
    // offline, until its bound token is revoked or expires. Another token bound to the name may.
    if (!agent.auth.nodeName) {
      const holder = await nameHolder(name);
      if (stopping || agent.ws.readyState !== WebSocket.OPEN) return;
      if (holder) {
        log.warn(`agent from ${agent.remoteAddress} rejected: node ${name} is reserved for ${holder}, and ${agent.auth.label} is not bound to it`);
        P.safeSend(agent.ws, { type: 'error', message: `node name ${name} is reserved for the agent token bound to it` });
        return P.safeClose(agent.ws, P.CLOSE.FORBIDDEN, 'node name reserved for its bound token');
      }
    }
    agent.name = name;
    agent.slots = slots;
    agent.info = {
      playwrightVersion: str(msg.playwrightVersion, 32),
      agentVersion: str(msg.agentVersion, 32),
      platform: str(msg.platform, 64),
      browsers: Array.isArray(msg.browsers) ? msg.browsers.filter((b) => typeof b === 'string').map((b) => b.slice(0, 64)) : [],
      ephemeral: msg.ephemeral === true,
      tokenId: agent.auth.tokenId,
    };
    agent.connectedAt = agent.lastSeen = new Date();
    clearTimeout(reapTimers.get(agent.name));
    reapTimers.delete(agent.name);

    // The newest connection wins: the old one is likely half-dead after a network blip.
    const old = nodes.get(agent.name);
    if (old && old !== agent) {
      const swap = old.auth.tokenId === agent.auth.tokenId ? '' : ` with ${agent.auth.label}, not ${old.auth.label},`;
      log.warn(`node ${agent.name} re-registered from ${agent.remoteAddress}${swap}; replacing the connection from ${old.remoteAddress}`);
      P.safeSend(old.ws, { type: 'error', message: 'replaced by a newer connection with the same name' });
      P.safeClose(old.ws, P.CLOSE.REPLACED, 'replaced');
    }
    nodes.set(agent.name, agent);
    P.safeSend(agent.ws, { type: 'registered', name: agent.name });
    log.info(`node ${agent.name} registered from ${agent.remoteAddress} with ${agent.auth.label}: ${slots.length} slots, playwright ${agent.info.playwrightVersion}, ${agent.info.platform}`);
    syncDb(agent);
  }

  // Who `name` is reserved for (a label), or null. The connected node is checked first; a database error
  // leaves only that check, since tokens that need the database can't get this far without it anyway.
  async function nameHolder(name) {
    let row = null;
    try {
      row = await registry.nameHolder(name);
    } catch (err) {
      log.warn(`could not check whether node name ${name} is reserved: ${err.message}`);
    }
    const live = nodes.get(name); // looked at after the query: it may have connected meanwhile
    if (live && live.auth.nodeName === name) return live.auth.label;
    return row ? `token ${row.prefix}... (#${row.id} "${row.name}")` : null;
  }

  function syncDb(agent) {
    const info = { name: agent.name, slots: agent.slots, remoteAddress: agent.remoteAddress, ...agent.info };
    return dbWrite(agent.name, 'register', async () => {
      await registry.register(info);
      agent.synced = true;
      pump(); // its slots can be claimed now
    });
  }

  function heartbeat(agent, msg) {
    const slots = P.normalizeSlots(msg.slots);
    if (slots && slots.length === agent.slots.length) {
      const recovered = slots.some((s, i) => s.healthy && !agent.slots[i].healthy);
      agent.slots = slots;
      if (recovered && nodes.get(agent.name) === agent) pump();
    }
    agent.lastSeen = new Date();
    if (nodes.get(agent.name) !== agent) return;
    // A failed register write (or a wiped table) is repaired on the next heartbeat.
    if (!agent.synced) return syncDb(agent);
    dbWrite(agent.name, 'heartbeat', async () => {
      if (!(await registry.heartbeat(agent.name, agent.slots))) {
        agent.synced = false;
        await registry.register({ name: agent.name, slots: agent.slots, remoteAddress: agent.remoteAddress, ...agent.info });
        agent.synced = true;
      }
    });
  }

  // An ephemeral node (a container named after its ID) never comes back under the same name, so once it
  // has been gone for the grace period its free slots and its row are removed instead of piling up
  // offline. Slots a check still holds stay (marked down) until a later sweep finds them released.
  function scheduleReap(name) {
    clearTimeout(reapTimers.get(name));
    const timer = setTimeout(() => {
      reapTimers.delete(name);
      reap(name);
    }, ephemeralGraceMs);
    timer.unref();
    reapTimers.set(name, timer);
  }

  function reap(name) {
    if (stopping || nodes.has(name)) return Promise.resolve();
    return dbWrite(name, 'ephemeral cleanup', async () => {
      // It may have registered again while this write waited its turn.
      if (stopping || nodes.has(name)) return;
      const result = await registry.reapEphemeral(name);
      if (result === 'removed') log.info(`ephemeral node ${name} was offline for ${ephemeralGraceMs / 1000}s; removed it`);
      else if (result === 'held') log.info(`ephemeral node ${name} is offline; removed its free slots, the rest go once checks release them`);
    });
  }

  async function sweepEphemeral() {
    try {
      const names = await registry.staleEphemeral(ephemeralGraceMs);
      await Promise.all(names.map((name) => reap(P.canonicalName(name))));
    } catch (err) {
      log.error(`ephemeral node sweep failed: ${err.message}`);
    }
  }

  // Every TOKEN_RECHECK_S the tokens of the connected agents are looked up again, past the cache, and an
  // agent whose token was revoked or expired is closed with 4401; so is one that came in without a token
  // before enforcement was switched on. Its reconnect is then refused. A database error closes nothing.
  // Client sessions already open are left alone: a revoked client token only stops new ones.
  async function recheckTokens() {
    if (rechecking || stopping) return;
    rechecking = true;
    let unavailable = 0;
    try {
      tokens.clear();
      await Promise.all([...agents].filter((a) => a.auth.via !== 'env').map(async (agent) => {
        const verdict = await tokens.checkHash('agent', agent.auth.hash);
        if (stopping || agent.ws.readyState !== WebSocket.OPEN) return;
        const who = `agent ${agent.name || agent.remoteAddress}`;
        if (verdict.unavailable) {
          unavailable++;
        } else if (!verdict.ok) {
          const reason = verdict.problem ? `token ${verdict.problem}` : 'agent token required';
          log.warn(`${who} disconnected: ${verdict.message}`);
          P.safeSend(agent.ws, { type: 'error', message: `agent token rejected: ${verdict.message}` });
          P.safeClose(agent.ws, P.CLOSE.UNAUTHORIZED, reason);
        } else if (verdict.nodeName && agent.name && verdict.nodeName !== agent.name) {
          log.warn(`${who} disconnected: ${verdict.label} is bound to node ${verdict.nodeName}`);
          P.safeClose(agent.ws, P.CLOSE.FORBIDDEN, 'token not valid for this node name');
        } else if (verdict.tokenId !== agent.auth.tokenId) {
          agent.auth = { ...agent.auth, via: verdict.via, tokenId: verdict.tokenId, nodeName: verdict.nodeName, label: verdict.label };
          if (agent.info) agent.info.tokenId = verdict.tokenId;
          if (agent.name && nodes.get(agent.name) === agent) syncDb(agent);
        }
      }));
      if (unavailable) log.warn(`could not re-check the token of ${unavailable} agent(s): the database is unavailable; keeping them connected`);
    } catch (err) {
      log.error(`agent token re-check failed: ${err.stack || err.message}`);
    } finally {
      rechecking = false;
    }
  }

  function acceptClient(req, socket, head, url, remote) {
    // Catch a broken handshake now rather than after a slot has been claimed for it.
    if ((req.headers.upgrade || '').toLowerCase() !== 'websocket' || !req.headers['sec-websocket-key']) {
      return reject(socket, 400, 'expected a WebSocket upgrade');
    }
    const token = req.headers['x-gateway-token'] ?? P.queryParam(url.search, 'gateway_token');
    tokens.check('client', token).then((verdict) => {
      if (stopping) return reject(socket, 503, 'hub is shutting down');
      if (socket.destroyed) return;
      if (verdict.unavailable) {
        log.error(`client ${remote} turned away: ${verdict.message}`);
        return reject(socket, 503, 'the hub cannot check gateway tokens right now (database unavailable); try again later');
      }
      if (!verdict.ok) {
        log.warn(`client ${remote} rejected: ${verdict.message}`);
        return reject(socket, 401, verdict.problem ? `gateway token is ${verdict.problem}` : 'missing or bad gateway token');
      }
      route(req, socket, head, url, remote);
    }).catch((err) => {
      log.error(`client ${remote}: token check failed: ${err.stack || err.message}`);
      reject(socket, 500, 'internal error');
    });
  }

  // A client whose token checked out. Its upgrade is held (no 101 yet) in the queue until pump() finds it
  // a slot, or it gives up, or it leaves.
  function route(req, socket, head, url, remote) {
    const { want, error } = R.clientWants(url.searchParams, req.headers);
    if (error) return reject(socket, 400, error);
    const query = P.stripQueryParam(P.stripQueryParam(url.search.replace(/^\?/, ''), 'gateway_token'), 'node');
    const client = {
      seq: ++clientSeq, label: `client ${remote}`, req, socket, head, want, query, headers: P.forwardHeaders(req.headers),
      arrived: Date.now(), state: 'queued', evaluated: false, expired: false, attempts: 0, failed: new Set(), lastError: null,
      summary: null, claim: null, open: null, timer: null,
    };
    // Reading the held socket is what lets a client's FIN show up as 'end'. A client sends nothing
    // before the 101, so any bytes mean a broken client.
    client.onSocketClose = () => leave(client);
    client.onSocketEnd = () => {
      if (leave(client)) socket.destroy();
    };
    client.onSocketData = () => finish(client, 400, 'data sent before the WebSocket handshake finished');
    socket.on('close', client.onSocketClose);
    socket.on('end', client.onSocketEnd);
    socket.on('data', client.onSocketData);
    held.add(client);
    if (queueTimeoutMs > 0) {
      client.timer = setTimeout(() => {
        client.expired = true;
        if (client.state === 'queued') giveUp(client);
      }, queueTimeoutMs);
    }
    enqueue(client);
    pump();
  }

  const slotWanted = ({ want }) => `${want.browser ? `${want.browser} ` : ''}slot${want.node ? ` on node ${want.node}` : ''}`;
  const waited = (client) => `${((Date.now() - client.arrived) / 1000).toFixed(1)}s`;
  const slotName = (c) => (c.gateway ? `${c.server}/${c.slot}` : `static host ${c.server}:${c.slot}`);

  function enqueue(client) {
    client.state = 'queued';
    let i = queue.length;
    while (i > 0 && queue[i - 1].seq > client.seq) i--;
    queue.splice(i, 0, client);
  }

  function dequeue(client) {
    const i = queue.indexOf(client);
    if (i >= 0) queue.splice(i, 1);
  }

  function unhold(client) {
    held.delete(client);
    clearTimeout(client.timer);
    client.socket.removeListener('close', client.onSocketClose);
    client.socket.removeListener('end', client.onSocketEnd);
    client.socket.removeListener('data', client.onSocketData);
  }

  // Takes a client out of whatever it was in (the queue, a claim on the way, a session being opened) and
  // gives its slot back. A claim still on its way is given back when it lands. False if it was done already.
  function abandon(client) {
    if (client.state === 'done') return false;
    const { state } = client;
    client.state = 'done';
    unhold(client);
    if (state === 'queued') dequeue(client);
    if (state === 'opening') {
      cancelOpen(client);
      release(client.claim);
      client.claim = null;
    }
    return true;
  }

  // Ends a held client with a plain HTTP answer.
  function finish(client, status, message) {
    if (abandon(client)) reject(client.socket, status, message);
  }

  function leave(client) {
    const { state, claim } = client;
    if (!abandon(client)) return false;
    const where = state === 'queued' ? `while waiting for a free ${slotWanted(client)}`
      : state === 'opening' ? `before ${slotName(claim)} opened its session` : 'while its slot was being claimed';
    log.info(`${client.label} left after ${waited(client)}, ${where}`);
    return true;
  }

  function giveUp(client) {
    const summary = client.summary || 'the hub could not look at the slots in time';
    const within = queueTimeoutMs ? ` within ${Math.round(queueTimeoutMs / 1000)}s` : '';
    const last = client.lastError ? `; the last try failed: ${client.lastError.message}` : '';
    log.warn(`${client.label} gave up after ${waited(client)}: no free ${slotWanted(client)} (${summary})`);
    finish(client, 503, `no free ${slotWanted(client)}${within}: ${summary}${last}`);
  }

  // Hands free slots to waiting clients, oldest first. One pass runs at a time; anything that may have
  // freed a slot (a release, a node registering or recovering, the poll while clients wait) asks for
  // another one.
  function pump() {
    if (pumpRun) {
      pumpAgain = true;
      return pumpRun;
    }
    pumpRun = runPump().finally(() => {
      pumpRun = null;
      if (queue.length && !pollTimer && !stopping) {
        pollTimer = setTimeout(() => {
          pollTimer = null;
          pump();
        }, queuePollMs);
      }
    });
    return pumpRun;
  }

  async function runPump() {
    try {
      do {
        pumpAgain = false;
        if (stopping || !queue.length) return;
        let rows;
        try {
          rows = await registry.slotStates(failThreshold, failCooldownMs);
        } catch (err) {
          log.error(`cannot read the slots to route ${queue.length} waiting client(s): ${err.message}`);
          for (const client of [...queue]) finish(client, 503, 'the hub cannot reach its database to pick a slot right now; try again later');
          return;
        }
        const taken = new Set([...claims.values()].map((c) => c.id));
        for (const client of [...queue]) {
          if (stopping) return;
          if (client.state === 'queued') await serve(client, rows, taken);
        }
      } while (pumpAgain);
    } catch (err) {
      log.error(`routing failed: ${err.stack || err.message}`);
    }
  }

  async function serve(client, rows, taken) {
    const view = R.evaluate(client.want, rows, nodes, { failed: client.failed, taken, lastError: client.lastError });
    if (view.fatal) {
      // Every slot it could have had failed it already: it gets the last error.
      const tried = view.fatal === client.lastError && client.attempts > 1 ? ` (tried ${client.attempts} slots)` : '';
      log.warn(`${client.label} refused (${view.fatal.status}): ${view.fatal.message}${tried}`);
      return finish(client, view.fatal.status, `${view.fatal.message}${tried}`);
    }
    client.summary = view.summary;
    let free = view.free;
    while (free.length && client.state === 'queued') {
      const pick = R.choose(free);
      free = free.filter((f) => f !== pick);
      taken.add(pick.row.id);
      if (await claim(client, pick)) return;
    }
    if (client.state !== 'queued') return;
    if (client.expired || queueTimeoutMs === 0) return giveUp(client);
    if (client.evaluated) return;
    client.evaluated = true;
    if (queue.filter((c) => c.evaluated).length > queueMax) {
      log.warn(`${client.label} refused: the queue is full (${queueMax} clients waiting)`);
      return finish(client, 503, `the hub's queue is full (${queueMax} clients waiting for a slot); try again later`);
    }
    log.info(`${client.label} waits for a free ${slotWanted(client)}: ${view.summary}`);
  }

  // Claims the slot with the same conditional UPDATE every claimer uses. Resolves true when the client is
  // dealt with (its session is being opened, or it is gone), false when someone else got the slot first.
  async function claim(client, { server, row }) {
    const c = { claimedBy: `${claimPrefix}${++claimSeq}`, id: row.id, server: server.name, slot: row.slot, gateway: server.gateway, pending: true };
    claims.set(c.claimedBy, c);
    dequeue(client);
    client.state = 'claiming';
    let got;
    try {
      got = await registry.claimSlot(c.id, c.claimedBy);
    } catch (err) {
      claims.delete(c.claimedBy);
      log.error(`could not claim ${slotName(c)} for ${client.label}: ${err.message}`);
      if (client.state === 'claiming') finish(client, 503, 'the hub cannot reach its database to claim a slot right now; try again later');
      return true;
    }
    c.pending = false;
    if (!got) {
      claims.delete(c.claimedBy);
      if (client.state === 'claiming') enqueue(client);
      return false;
    }
    if (client.state !== 'claiming') {
      release(c); // the client left meanwhile
      return true;
    }
    client.state = 'opening';
    client.claim = c;
    client.attempts++;
    const attempt = client.attempts > 1 ? `, attempt ${client.attempts}` : '';
    log.info(`${client.label} -> ${slotName(c)} (${client.want.browser || 'browser not named'}, waited ${waited(client)}${attempt})`);
    if (c.gateway) openOnAgent(client, c);
    else openOnStatic(client, c);
    return true;
  }

  function openOnAgent(client, c) {
    const agent = nodes.get(c.server);
    if (!agent) return failAttempt(client, 502, `node ${c.server} disconnected`, false);
    const id = crypto.randomUUID();
    const open = {
      id, agent, client, claim: c, slot: c.slot,
      timer: setTimeout(() => failOpen(id, 504, `node ${c.server} slot ${c.slot} did not answer within ${config.openTimeoutMs / 1000}s`), config.openTimeoutMs),
    };
    client.open = open;
    pending.set(id, open);
    agent.opens.add(id);
    P.safeSend(agent.ws, { type: 'open', id, slot: c.slot, path: '/', query: client.query, headers: client.headers });
  }

  // A legacy static host: the hub dials its run-server itself, with the headers an agent slot would get.
  function openOnStatic(client, c) {
    const where = `static host ${c.server}:${c.slot}`;
    const open = { client, claim: c, upstream: null, settled: false, timer: null };
    const fail = (status, message) => {
      if (open.settled) return;
      open.settled = true;
      failAttempt(client, status, message, true);
    };
    const host = c.server.includes(':') ? `[${c.server}]` : c.server;
    try {
      open.upstream = new WebSocket(`ws://${host}:${c.slot}/${client.query ? `?${client.query}` : ''}`, {
        headers: client.headers, perMessageDeflate: false, maxPayload: P.MAX_PAYLOAD,
      });
    } catch (err) {
      return fail(502, `${where}: ${err.message}`);
    }
    client.open = open;
    open.timer = setTimeout(() => fail(504, `${where} did not answer within ${config.openTimeoutMs / 1000}s`), config.openTimeoutMs);
    // run-server refuses a version mismatch with an HTTP error whose body says why.
    open.upstream.on('unexpected-response', (req, res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { if (body.length < 2000) body += chunk; });
      res.on('end', () => fail(502, `${where} answered HTTP ${res.statusCode}${body.trim() ? `: ${body.trim()}` : ''}`));
      res.on('error', () => {});
    });
    open.upstream.on('error', (err) => fail(502, `${where}: ${err.message}`));
    open.upstream.once('open', () => {
      if (open.settled) return P.safeClose(open.upstream, 1001, 'client went away');
      open.settled = true;
      clearTimeout(open.timer);
      client.open = null;
      connected(client, c, open.upstream);
    });
  }

  function cancelOpen(client) {
    const open = client.open;
    client.open = null;
    if (!open) return;
    clearTimeout(open.timer);
    if (open.id) {
      pending.delete(open.id);
      open.agent.opens.delete(open.id);
    }
    if (open.upstream) {
      open.settled = true;
      open.upstream.terminate();
    }
  }

  function failOpen(id, status, message, { count = true } = {}) {
    const open = pending.get(id);
    if (!open) return;
    log.warn(`open ${id} on ${slotName(open.claim)} failed (${status}): ${message}`);
    failAttempt(open.client, status, message, count);
  }

  // The chosen slot couldn't open the session. Unless it wasn't the slot's fault, that is a connect
  // failure on it (failCount + 1, as main.py counted them); the slot goes back and the client is tried
  // on another one, up to GATEWAY_CONNECT_ATTEMPTS slots, before it gets this error.
  function failAttempt(client, status, message, count) {
    const c = client.claim;
    cancelOpen(client);
    client.claim = null;
    release(c, count ? addFailure(c) : null);
    client.failed.add(c.id);
    client.lastError = { status, message };
    if (client.state !== 'opening') return;
    if (stopping) return finish(client, 503, 'hub is shutting down');
    if (client.attempts >= connectAttempts || client.expired) {
      log.warn(`${client.label} gets ${status} after ${client.attempts} attempt(s): ${message}`);
      return finish(client, status, client.attempts > 1 ? `${message} (tried ${client.attempts} slots)` : message);
    }
    log.info(`${client.label}: ${slotName(c)} failed (${message}); trying another slot`);
    enqueue(client);
    pump();
  }

  function openFailed(agent, msg) {
    const open = pending.get(msg.id);
    if (!open || open.agent !== agent) return;
    failOpen(msg.id, 502, `node ${agent.name} slot ${open.slot}: ${String(msg.message || 'open failed').slice(0, 500)}`);
  }

  // A data channel must carry the token its control connection authenticated with.
  function acceptData(req, socket, head, id) {
    const open = pending.get(id);
    if (!open) return reject(socket, 404, 'no pending session with that id');
    const token = P.bearer(req.headers.authorization);
    const { auth } = open.agent;
    if (auth.via === 'env') {
      if (!P.tokenMatches(config.agentToken, token)) return reject(socket, 401, 'bad agent token');
      return attachData(req, socket, head, id, open);
    }
    if (auth.via === 'managed') {
      if (!token || !P.tokenMatches(auth.hash, P.hashToken(token))) return reject(socket, 401, 'bad agent token');
      tokens.touch(auth.tokenId);
      return attachData(req, socket, head, id, open);
    }
    // The control connection needed no token; enforcement may have been switched on since.
    tokens.check('agent', token).then((verdict) => {
      if (!verdict.ok) return reject(socket, verdict.unavailable ? 503 : 401, verdict.unavailable ? 'token check unavailable' : 'bad agent token');
      if (pending.get(id) !== open) return reject(socket, 404, 'no pending session with that id');
      attachData(req, socket, head, id, open);
    }).catch((err) => {
      log.error(`data channel ${id}: token check failed: ${err.stack || err.message}`);
      socket.destroy();
    });
  }

  function attachData(req, socket, head, id, open) {
    const { client } = open;
    cancelOpen(client);
    if (client.socket.destroyed || !client.socket.writable) {
      reject(socket, 410, 'client went away');
      return leave(client);
    }
    // Without verifyClient, handleUpgrade calls back synchronously or not at all (it answered the
    // socket itself because the handshake was invalid or the socket died), so a flag tells which.
    let dataUp = false;
    wss.handleUpgrade(req, socket, head, (data) => {
      dataUp = true;
      connected(client, open.claim, data);
    });
    if (!dataUp) failAttempt(client, 502, `node ${open.agent.name} sent a bad data channel handshake`, true);
  }

  // The slot's run-server took the session (its 101 is through), which also ends the slot's run of
  // connect failures. The held client is upgraded and piped to it; the slot is released when either side ends.
  function connected(client, c, data) {
    if (client.state !== 'opening') return P.safeClose(data, 1001, 'client went away');
    resetFailures(c);
    unhold(client);
    client.state = 'done';
    client.claim = null;
    let clientUp = false;
    wss.handleUpgrade(client.req, client.socket, client.head, (ws) => {
      clientUp = true;
      const session = { client: ws, data, clientAlive: Date.now(), dataAlive: Date.now(), name: slotName(c), claim: c };
      sessions.add(session);
      // Each leg proves itself: the agent's always-answering leg must not keep a dead client's session up.
      const clientSeen = () => { session.clientAlive = Date.now(); };
      const dataSeen = () => { session.dataAlive = Date.now(); };
      ws.on('pong', clientSeen).on('message', clientSeen);
      data.on('pong', dataSeen).on('message', dataSeen);
      pipe(ws, data, () => {
        sessions.delete(session);
        release(c);
      });
    });
    if (!clientUp) {
      P.safeClose(data, 1001, 'client went away');
      release(c);
    }
  }

  const addFailure = (c) => registry.addFailure(c.id).catch((err) => log.error(`could not count a connect failure on ${slotName(c)}: ${err.message}`));
  const resetFailures = (c) => registry.resetFailures(c.id).catch((err) => log.error(`could not reset the failures of ${slotName(c)}: ${err.message}`));

  // Gives a claim back (after `after`, such as the failure count, is written). Only this claim is
  // released: if the slot was released from elsewhere and claimed again since, it is left alone. A
  // release the database refuses is retried by the claim sync.
  function release(c, after = null) {
    if (!c || c.released) return;
    c.released = true;
    const done = Promise.resolve(after).then(() => registry.releaseSlot(c.id, c.claimedBy)).then((mine) => {
      if (!mine) log.warn(`${slotName(c)} was no longer this hub's claim when its session ended; left it alone`);
    }, (err) => {
      unreleased.set(c.claimedBy, c.id);
      log.error(`could not release ${slotName(c)}: ${err.message}; retrying every ${claimSyncMs / 1000}s`);
    }).finally(() => {
      claims.delete(c.claimedBy);
      releasing.delete(done);
      pump();
    });
    releasing.add(done);
  }

  // Every CLAIM_SYNC_MS: retries releases the database refused, frees claims of this hub that nothing
  // holds, and checks that the live ones are still this hub's. The stale-claim sweep in src/database.py
  // frees old claims without clearing claimedBy; a live session's slot it freed is claimed again if
  // nobody took it meanwhile. A claim released from the Grid UI (claimedBy cleared) or taken over is
  // let go: its session runs on, and its end releases nothing.
  async function syncClaims() {
    if (syncing || stopping) return;
    syncing = true;
    try {
      for (const [claimedBy, id] of [...unreleased]) {
        await registry.releaseSlot(id, claimedBy);
        unreleased.delete(claimedBy);
        log.info(`released slot #${id}, which an earlier release could not`);
      }
      const rows = await registry.slotStates(failThreshold, failCooldownMs);
      const byId = new Map(rows.map((r) => [r.id, r]));
      for (const row of rows) {
        const mine = typeof row.claimedBy === 'string' && row.claimedBy.startsWith(claimPrefix);
        if (!mine || claims.has(row.claimedBy) || unreleased.has(row.claimedBy) || lostClaims.has(row.claimedBy)) continue;
        if (await registry.releaseSlot(row.id, row.claimedBy)) log.warn(`released ${row.server}/${row.slot}: this hub claimed it, but no session holds it`);
      }
      for (const c of [...claims.values()]) {
        if (c.released || c.pending) continue;
        const row = byId.get(c.id);
        if (row && row.claimedBy === c.claimedBy && row.inUse) continue;
        if (row && row.claimedBy === c.claimedBy && await registry.reassertClaim(c.id, c.claimedBy)) {
          log.warn(`${slotName(c)} was freed as a stale claim while its session is still open; claimed it again`);
          continue;
        }
        c.released = true;
        claims.delete(c.claimedBy);
        lostClaims.add(c.claimedBy);
        log.warn(`${slotName(c)} is no longer this hub's claim (${row ? 'released or claimed from elsewhere' : 'the slot was removed'}); its session runs on, and its end releases nothing`);
      }
    } catch (err) {
      log.error(`claim sync failed: ${err.message}`);
    } finally {
      syncing = false;
    }
  }

  // Pings find half-open TCP connections that would otherwise sit there forever.
  function sweep() {
    const now = Date.now();
    for (const agent of agents) {
      if (now - agent.alive > config.heartbeatTimeoutMs) {
        log.warn(`agent ${agent.name || agent.remoteAddress} missed heartbeats for ${config.heartbeatTimeoutMs / 1000}s; dropping it`);
        agent.ws.terminate();
      } else if (agent.ws.readyState === WebSocket.OPEN) {
        agent.ws.ping();
      }
    }
    for (const session of sessions) {
      const silent = now - session.clientAlive > sessionTimeoutMs ? 'client' : now - session.dataAlive > sessionTimeoutMs ? 'agent' : null;
      if (silent) {
        log.warn(`session ${session.name}: the ${silent} side stopped answering pings; closing it`);
        session.client.terminate();
        session.data.terminate();
        continue;
      }
      for (const ws of [session.client, session.data]) if (ws.readyState === WebSocket.OPEN) ws.ping();
    }
  }

  async function start() {
    await registry.init();
    // Sessions don't outlive the hub that piped them, so claims a hub made before this one are leftovers.
    try {
      const freed = await registry.releaseClaims('hub:');
      if (freed) log.warn(`released ${freed} slot(s) a previous hub run still had claimed`);
    } catch (err) {
      log.error(`could not release the claims of a previous hub run: ${err.message}`);
    }
    const managed = {};
    for (const kind of ['agent', 'client']) {
      if (config[`${kind}Token`]) continue;
      try {
        managed[kind] = await tokens.enforced(kind);
      } catch (err) {
        log.error(`could not read the managed ${kind} tokens: ${err.message}; ${kind}s are refused until the database answers`);
        managed[kind] = true;
      }
    }
    if (!config.agentToken && !managed.agent) {
      const none = config.allowOpenAgents ? 'no unrevoked managed agent token (GATEWAY_ALLOW_OPEN_AGENTS is set)' : 'no managed agent token';
      log.warn(`!!! GATEWAY_AGENT_TOKEN is not set and there is ${none}: ANY agent that can reach this port can register as a node. `
        + 'Create an agent token in the Grid UI (Access) or set GATEWAY_AGENT_TOKEN. !!!');
    }
    // Ephemeral nodes that went away while the hub was down.
    await sweepEphemeral();
    ephemeralSweeper = setInterval(sweepEphemeral, ephemeralSweepMs);
    server.on('upgrade', (req, socket, head) => {
      try {
        onUpgrade(req, socket, head);
      } catch (err) {
        log.error(`upgrade ${req.url} failed: ${err.stack || err.message}`);
        socket.destroy();
      }
    });
    server.on('clientError', (err, socket) => socket.destroy());
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(config.port, config.host, () => {
        server.removeListener('error', reject);
        resolve();
      });
    });
    keepalive = setInterval(sweep, pingInterval);
    tokenRechecker = setInterval(recheckTokens, tokenRecheckMs);
    claimSyncer = setInterval(syncClaims, claimSyncMs);
    const { port } = server.address();
    const mode = (kind) => (config[`${kind}Token`] ? 'required' : managed[kind] ? 'required (managed tokens)' : 'not required');
    log.info(`hub listening on ${config.host}:${port} (db: ${registry.kind}, agent token ${mode('agent')}, client token ${mode('client')})`);
    return port;
  }

  async function stop() {
    if (stopping) return;
    stopping = true;
    clearInterval(keepalive);
    clearInterval(ephemeralSweeper);
    clearInterval(tokenRechecker);
    clearInterval(claimSyncer);
    clearTimeout(pollTimer);
    for (const timer of reapTimers.values()) clearTimeout(timer);
    reapTimers.clear();
    server.close();
    for (const client of [...held]) finish(client, 503, 'hub is shutting down');
    for (const session of sessions) {
      P.safeClose(session.client, 1001, 'hub is shutting down');
      P.safeClose(session.data, 1001, 'hub is shutting down');
    }
    for (const agent of agents) P.safeClose(agent.ws, 1001, 'hub is shutting down');
    const names = [...nodes.keys()];
    nodes.clear();
    await Promise.all(names.map((name) => dbWrite(name, 'disconnect', () => registry.disconnect(name))));
    await Promise.all([...dbQueues.values()]);
    // Give the close frames a moment, then cut whatever is left.
    await new Promise((r) => setTimeout(r, 200));
    for (const agent of agents) agent.ws.terminate();
    for (const session of sessions) {
      session.client.terminate();
      session.data.terminate();
    }
    server.closeAllConnections();
    // Every claim goes back before the database connection does: the sessions are gone, and a claim
    // still on its way is released when it lands.
    await pumpRun;
    for (const c of claims.values()) release(c);
    await Promise.all([...releasing]);
    await registry.close();
  }

  return {
    start, stop, server, nodes, pending, sessions, queue, claims, registry, sweepEphemeral, recheckTokens, syncClaims,
  };
}

async function main() {
  const log = createLogger('hub');
  let config;
  try {
    config = hubConfig();
  } catch (err) {
    log.error(err.message);
    process.exit(2);
  }
  const hub = createHub(config, { log });
  process.on('unhandledRejection', (err) => log.error(`unhandled rejection: ${err && err.stack ? err.stack : err}`));
  let exiting = false;
  const shutdown = (signal) => {
    if (exiting) return;
    exiting = true;
    log.info(`${signal} received, shutting down`);
    setTimeout(() => process.exit(0), 5000).unref();
    hub.stop().then(() => process.exit(0), (err) => {
      log.error(`shutdown: ${err.message}`);
      process.exit(1);
    });
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  try {
    await hub.start();
  } catch (err) {
    log.error(`hub failed to start: ${err.stack || err.message}`);
    await hub.stop().catch(() => {});
    process.exit(1);
  }
}

if (require.main === module) main();

module.exports = { createHub };
