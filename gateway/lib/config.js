'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { MAX_SLOTS, isValidName, canonicalName, sanitizeName } = require('./protocol');

function int(value, fallback, name, min, max) {
  if (value === undefined || value === null || value === '') return fallback;
  const n = Number(value);
  if (!Number.isInteger(n) || n < min || n > max) throw new Error(`${name} must be an integer between ${min} and ${max}, got "${value}"`);
  return n;
}

function bool(value, name) {
  if (value === undefined || value === null || value === '') return false;
  const v = String(value).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(v)) return true;
  if (['0', 'false', 'no', 'off'].includes(v)) return false;
  throw new Error(`${name} must be 1/true or 0/false, got "${value}"`);
}

function hubConfig(env = process.env) {
  const db = (env.HUB_DB || 'mysql').toLowerCase();
  if (db !== 'mysql' && db !== 'memory') throw new Error(`HUB_DB must be mysql or memory, got "${env.HUB_DB}"`);
  return {
    port: int(env.HUB_PORT, 4500, 'HUB_PORT', 0, 65535),
    host: env.HUB_HOST || '0.0.0.0',
    agentToken: env.GATEWAY_AGENT_TOKEN || null,
    clientToken: env.GATEWAY_CLIENT_TOKEN || null,
    // Once a managed token of a kind has existed, that kind needs a token for good; these opt back into
    // letting tokenless agents / clients in after every managed token of the kind is revoked.
    allowOpenAgents: bool(env.GATEWAY_ALLOW_OPEN_AGENTS, 'GATEWAY_ALLOW_OPEN_AGENTS'),
    allowOpenClients: bool(env.GATEWAY_ALLOW_OPEN_CLIENTS, 'GATEWAY_ALLOW_OPEN_CLIENTS'),
    db,
    mysql: {
      host: env.DB_HOST || 'mysql',
      port: int(env.DB_PORT, 3306, 'DB_PORT', 1, 65535),
      database: env.DB_NAME || 'orchestrator',
      user: env.DB_USER || 'orchestrator',
      password: env.DB_PASSWORD ?? 'orchestrator',
    },
    schemaPath: env.SCHEMA_PATH || path.resolve(__dirname, '..', '..', 'src', 'schema.sql'),
    heartbeatTimeoutMs: int(env.HEARTBEAT_TIMEOUT_S, 30, 'HEARTBEAT_TIMEOUT_S', 1, 3600) * 1000,
    openTimeoutMs: int(env.OPEN_TIMEOUT_S, 15, 'OPEN_TIMEOUT_S', 1, 3600) * 1000,
    ephemeralGraceMs: int(env.EPHEMERAL_GRACE_S, 120, 'EPHEMERAL_GRACE_S', 0, 86400) * 1000,
    // Managed access tokens: how long a lookup is cached, and how often connected agents' tokens are re-checked.
    tokenCacheMs: int(env.TOKEN_CACHE_S, 10, 'TOKEN_CACHE_S', 0, 3600) * 1000,
    tokenRecheckMs: int(env.TOKEN_RECHECK_S, 15, 'TOKEN_RECHECK_S', 1, 86400) * 1000,
    // Routing /connect: how long and how many clients may wait for a free slot, and how many slots one
    // client is tried on before it gets the error.
    queueTimeoutMs: int(env.GATEWAY_QUEUE_TIMEOUT_S, 60, 'GATEWAY_QUEUE_TIMEOUT_S', 0, 86400) * 1000,
    queueMax: int(env.GATEWAY_QUEUE_MAX, 200, 'GATEWAY_QUEUE_MAX', 0, 100000),
    connectAttempts: int(env.GATEWAY_CONNECT_ATTEMPTS, 3, 'GATEWAY_CONNECT_ATTEMPTS', 1, 20),
    // A slot with this many consecutive connect failures is skipped, except for one try per cooldown
    // counted from its last run. The Grid UI reads the same names with the same defaults (ui/store.py).
    failThreshold: int(env.SLOT_FAIL_THRESHOLD || env.GRID_FAIL_THRESHOLD, 3, 'SLOT_FAIL_THRESHOLD', 0, 1000000),
    failCooldownMs: int(env.SLOT_FAIL_COOLDOWN_S, 300, 'SLOT_FAIL_COOLDOWN_S', 0, 86400 * 365) * 1000,
    // A hub renews its row in hubs every third of this; one not renewed for this long is taken for gone
    // and its claims are freed. The Grid UI reads it too, to tell a long session from a leftover claim.
    hubLeaseMs: int(env.GATEWAY_HUB_LEASE_S, 90, 'GATEWAY_HUB_LEASE_S', 10, 86400) * 1000,
  };
}

const AGENT_FLAGS = {
  hub: 'HUB_URL',
  token: 'GATEWAY_AGENT_TOKEN',
  name: 'NODE_NAME',
  slots: 'SLOTS',
  'base-port': 'BASE_PORT',
  'playwright-cli': 'PLAYWRIGHT_CLI',
  browsers: 'BROWSERS',
  'status-file': 'STATUS_FILE',
  'log-dir': 'LOG_DIR',
  ephemeral: 'EPHEMERAL',
};
// Flags that take no value: `--ephemeral` alone means true, `--ephemeral=false` turns it off.
const BOOLEAN_FLAGS = new Set(['ephemeral']);

const AGENT_USAGE = `Usage: node agent.js --hub ws://hub:4500 [options]

  --hub <url>             HUB_URL              hub to dial out to (required)
  --token <token>         GATEWAY_AGENT_TOKEN  sent as "Authorization: Bearer <token>"
  --name <name>           NODE_NAME            node name, case-insensitive (default: host name)
  --slots <n>             SLOTS                run-server processes, 1-${MAX_SLOTS} (default 4)
  --base-port <port>      BASE_PORT            slot i listens on 127.0.0.1:<base+i-1> (default 3100)
  --playwright-cli <path> PLAYWRIGHT_CLI       playwright-core cli.js (default: resolved from node_modules)
  --browsers <list>       BROWSERS             browsers installed here (default chromium); the hub only
                                               routes clients asking for one of them to this node
  --status-file <path>    STATUS_FILE          JSON status, rewritten every few seconds
  --log-dir <dir>         LOG_DIR              run-server output goes to <dir>/slot-<i>.log
  --ephemeral             EPHEMERAL=1          the hub removes this node once it has been gone for
                                               EPHEMERAL_GRACE_S (for containers named after their ID)
`;

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '-h' || arg === '--help') {
      out.help = true;
      continue;
    }
    const match = /^--([a-z-]+)(?:=(.*))?$/.exec(arg);
    if (!match || !(match[1] in AGENT_FLAGS)) throw new Error(`unknown argument "${arg}"`);
    let value = match[2];
    if (value === undefined && BOOLEAN_FLAGS.has(match[1])) value = 'true';
    if (value === undefined) {
      value = argv[++i];
      if (value === undefined) throw new Error(`--${match[1]} needs a value`);
    }
    out[match[1]] = value;
  }
  return out;
}

function resolveCli(value, cwd) {
  if (value) {
    const cli = path.resolve(cwd, value);
    if (!fs.existsSync(cli)) throw new Error(`PLAYWRIGHT_CLI not found: ${cli}`);
    return cli;
  }
  // playwright-core's "exports" may not list cli.js, so go through package.json.
  for (const request of ['playwright-core/cli.js', 'playwright-core/package.json']) {
    try {
      const resolved = require.resolve(request, { paths: [cwd, __dirname] });
      return path.join(path.dirname(resolved), 'cli.js');
    } catch {}
  }
  throw new Error('playwright-core not found; install it (npm install playwright-core@1.64.1) or set PLAYWRIGHT_CLI');
}

function agentConfig(argv = process.argv.slice(2), env = process.env, cwd = process.cwd()) {
  const args = parseArgs(argv);
  if (args.help) return { help: true, usage: AGENT_USAGE };
  const get = (flag) => args[flag] ?? env[AGENT_FLAGS[flag]] ?? undefined;

  const hub = (get('hub') || '').replace(/\/+$/, '');
  if (!/^wss?:\/\/[^/]+/i.test(hub)) throw new Error(`--hub / HUB_URL must be a ws:// or wss:// URL, got "${hub}"\n\n${AGENT_USAGE}`);
  const name = canonicalName(get('name') || sanitizeName(os.hostname()));
  if (!isValidName(name)) throw new Error(`--name / NODE_NAME may only use A-Z a-z 0-9 . _ - (1-128 chars), got "${name}"`);
  const slots = int(get('slots'), 4, '--slots / SLOTS', 1, MAX_SLOTS);
  const basePort = int(get('base-port'), 3100, '--base-port / BASE_PORT', 1, 65535 - slots + 1);
  const cli = resolveCli(get('playwright-cli'), cwd);
  const browsers = (get('browsers') || 'chromium').split(',').map((b) => b.trim()).filter(Boolean);
  const statusFile = get('status-file') ? path.resolve(cwd, get('status-file')) : null;
  const logDir = get('log-dir') ? path.resolve(cwd, get('log-dir')) : null;
  const ephemeral = bool(get('ephemeral'), '--ephemeral / EPHEMERAL');
  return { hub, token: get('token') || null, name, slots, basePort, cli, browsers, statusFile, logDir, ephemeral };
}

module.exports = { hubConfig, agentConfig, parseArgs, resolveCli, AGENT_USAGE };
