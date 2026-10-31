'use strict';

// Gateway hub: the one port everything talks to. Agents dial out to /agent (control) and
// /agent/data/<id> (one per session); checks connect to /connect/<node>/<slot> and get piped
// through to that slot's run-server on the agent's host.

const crypto = require('node:crypto');
const http = require('node:http');
const WebSocket = require('ws');
const { hubConfig } = require('./lib/config');
const { createLogger } = require('./lib/log');
const { pipe } = require('./lib/pipe');
const { createRegistry } = require('./lib/registry');
const { createTokenAuth } = require('./lib/tokens');
const P = require('./lib/protocol');

const REGISTER_TIMEOUT_MS = 10000;
const EPHEMERAL_SWEEP_MS = 60000;

function createHub(config, { log = createLogger('hub'), registry = createRegistry(config, log) } = {}) {
  // Control frames are small; only the session tunnels need Playwright-sized frames.
  const controlWss = new WebSocket.WebSocketServer({ noServer: true, maxPayload: 1024 * 1024, clientTracking: false });
  const wss = new WebSocket.WebSocketServer({ noServer: true, maxPayload: P.MAX_PAYLOAD, clientTracking: false });
  const server = http.createServer(onRequest);
  const nodes = new Map(); // name -> registered agent connection
  const agents = new Set(); // every control connection, registered or not
  const pending = new Map(); // open id -> held client upgrade
  const sessions = new Set(); // piped [client, data] pairs
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
    const connect = /^\/connect\/([^/]+)\/(\d+)\/?$/.exec(url.pathname);
    if (connect) return acceptClient(req, socket, head, url, connect[1], Number(connect[2]), remote);
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
      for (const id of agent.opens) failOpen(id, 502, `node ${agent.name} disconnected`);
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
    });
  }

  function heartbeat(agent, msg) {
    const slots = P.normalizeSlots(msg.slots);
    if (slots && slots.length === agent.slots.length) agent.slots = slots;
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

  function acceptClient(req, socket, head, url, rawName, slot, remote) {
    // Catch a broken handshake now rather than after the agent has opened a session for it.
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
      openSession(req, socket, head, url, rawName, slot, remote);
    }).catch((err) => {
      log.error(`client ${remote}: token check failed: ${err.stack || err.message}`);
      reject(socket, 500, 'internal error');
    });
  }

  function openSession(req, socket, head, url, rawName, slot, remote) {
    let name;
    try {
      name = decodeURIComponent(rawName);
    } catch {
      return reject(socket, 404, 'unknown node');
    }
    const agent = nodes.get(P.canonicalName(name));
    if (!agent) return reject(socket, 404, `node ${name} is not connected`);
    if (slot < 1 || slot > agent.slots.length) return reject(socket, 404, `node ${name} has no slot ${slot} (it has ${agent.slots.length})`);

    const id = crypto.randomUUID();
    const open = {
      id, agent, socket, req, head, slot,
      timer: setTimeout(() => failOpen(id, 504, `node ${name} slot ${slot} did not answer within ${config.openTimeoutMs / 1000}s`), config.openTimeoutMs),
      onSocketClose: () => dropOpen(id),
      // Reading the held socket is what lets a client's FIN show up as 'end'. A client sends nothing
      // before the 101, so any bytes mean a broken client.
      onSocketEnd: () => {
        if (!dropOpen(id)) return;
        log.info(`client ${remote} left before ${name}/${slot} answered (${id})`);
        socket.destroy();
      },
      onSocketData: () => failOpen(id, 400, 'data sent before the WebSocket handshake finished'),
    };
    pending.set(id, open);
    agent.opens.add(id);
    socket.on('close', open.onSocketClose);
    socket.on('end', open.onSocketEnd);
    socket.on('data', open.onSocketData);
    const query = P.stripQueryParam(url.search.replace(/^\?/, ''), 'gateway_token');
    P.safeSend(agent.ws, { type: 'open', id, slot, path: '/', query, headers: P.forwardHeaders(req.headers) });
    log.info(`client ${remote} -> ${name}/${slot} (${id})`);
  }

  function dropOpen(id) {
    const open = pending.get(id);
    if (!open) return null;
    pending.delete(id);
    open.agent.opens.delete(id);
    clearTimeout(open.timer);
    open.socket.removeListener('close', open.onSocketClose);
    open.socket.removeListener('end', open.onSocketEnd);
    open.socket.removeListener('data', open.onSocketData);
    return open;
  }

  function failOpen(id, status, message) {
    const open = dropOpen(id);
    if (!open) return;
    log.warn(`open ${id} failed (${status}): ${message}`);
    reject(open.socket, status, message);
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
    dropOpen(id);
    if (open.socket.destroyed || !open.socket.writable) return reject(socket, 410, 'client went away');
    // Without verifyClient, handleUpgrade calls back synchronously or not at all (it answered the
    // socket itself because the handshake was invalid or the socket died), so a flag tells which.
    let dataUp = false;
    wss.handleUpgrade(req, socket, head, (data) => {
      dataUp = true;
      let clientUp = false;
      wss.handleUpgrade(open.req, open.socket, open.head, (client) => {
        clientUp = true;
        const session = { client, data, clientAlive: Date.now(), dataAlive: Date.now(), name: `${open.agent.name}/${open.slot}` };
        sessions.add(session);
        // Each leg proves itself: the agent's always-answering leg must not keep a dead client's session up.
        const clientSeen = () => { session.clientAlive = Date.now(); };
        const dataSeen = () => { session.dataAlive = Date.now(); };
        client.on('pong', clientSeen).on('message', clientSeen);
        data.on('pong', dataSeen).on('message', dataSeen);
        pipe(client, data, () => sessions.delete(session));
      });
      if (!clientUp) P.safeClose(data, 1001, 'client went away');
    });
    if (!dataUp) reject(open.socket, 502, `node ${open.agent.name} sent a bad data channel handshake`);
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
    for (const timer of reapTimers.values()) clearTimeout(timer);
    reapTimers.clear();
    server.close();
    for (const id of [...pending.keys()]) failOpen(id, 503, 'hub is shutting down');
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
    await registry.close();
  }

  return { start, stop, server, nodes, pending, sessions, registry, sweepEphemeral, recheckTokens };
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
