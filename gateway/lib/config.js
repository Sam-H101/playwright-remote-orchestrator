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
    // A client token in /connect's URL (?gateway_token=) ends up in logs, proxies and shell history, so
    // it is refused; this lets checks that still send it that way in while they are moved to the header.
    allowQueryToken: bool(env.GATEWAY_ALLOW_QUERY_TOKEN, 'GATEWAY_ALLOW_QUERY_TOKEN'),
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
  'run-server-user': 'RUN_SERVER_USER',
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
  --run-server-user <u>   RUN_SERVER_USER      run run-server, and so the browsers, as this user (Linux and
                                               macOS; the agent must start as root). Checks can read any
                                               file and process of the browsers' user, so keep the agent
                                               token out of its reach. {slot} in the name gives each slot
                                               its own user (pwuser{slot}: pwuser1, pwuser2, ...), so checks
                                               running at the same time can't read each other's profiles
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
  // playwright-core's "exports" may not list cli.js, so go through package.json. In a checkout,
  // runner/playwright is where the pinned version is installed (npm ci there).
  const pinned = path.resolve(__dirname, '..', '..', 'runner', 'playwright');
  for (const request of ['playwright-core/cli.js', 'playwright-core/package.json']) {
    try {
      const resolved = require.resolve(request, { paths: [cwd, __dirname, pinned] });
      return path.join(path.dirname(resolved), 'cli.js');
    } catch {}
  }
  throw new Error('playwright-core not found; install the version runner/playwright pins (npm ci there) or set PLAYWRIGHT_CLI');
}

// The user run-server runs as (RUN_SERVER_USER), from /etc/passwd: { name, uid, gid, home }, or null to run it as
// the agent's own user. A check's browser can read every file and /proc entry its user can, the agent's
// environment (and so its token) included when that is the agent's user, so the agent starts as root and hands
// run-server to another user; leaving root drops every capability. system replaces the platform, getuid and
// /etc/passwd for tests.
function runServerUser(name, system = {}) {
  if (!name) return null;
  const { platform = process.platform, getuid = process.getuid, passwd = () => fs.readFileSync('/etc/passwd', 'utf8') } = system;
  const setting = '--run-server-user / RUN_SERVER_USER';
  if (platform === 'win32') throw new Error(`${setting} is not supported on Windows`);
  if (!getuid || getuid() !== 0) throw new Error(`${setting} needs the agent to start as root, so that it can start run-server as ${name}`);
  const fields = passwd().split(/\r?\n/).map((line) => line.split(':')).find((f) => f.length >= 7 && f[0] === name);
  if (!fields) throw new Error(`${setting}: there is no user "${name}" in /etc/passwd`);
  const uid = Number(fields[2]);
  const gid = Number(fields[3]);
  if (!Number.isInteger(uid) || !Number.isInteger(gid) || fields[2] === '' || fields[3] === '') {
    throw new Error(`${setting}: /etc/passwd has no usable uid and gid for "${name}"`);
  }
  if (uid === 0) throw new Error(`${setting}: "${name}" is root; run-server must run as another user than the agent`);
  return { name, uid, gid, home: fields[5] || '/' };
}

// The user of each slot, slot 1 first, or null. A name with {slot} gives each slot its own user (pwuser{slot}:
// pwuser1, pwuser2, ...), so a check's browser can't read the profiles of checks running at the same time in other
// slots; a plain name is one user for every slot.
function runServerUsers(name, slots, system = {}) {
  if (!name) return null;
  if (!name.includes('{slot}')) {
    const user = runServerUser(name, system);
    return Array.from({ length: slots }, () => user);
  }
  const users = [];
  const byUid = new Map();
  for (let i = 1; i <= slots; i++) {
    const user = runServerUser(name.replaceAll('{slot}', String(i)), system);
    const other = byUid.get(user.uid);
    if (other) {
      throw new Error(`--run-server-user / RUN_SERVER_USER: "${other.name}" and "${user.name}" are the same user (uid ${user.uid}); each slot needs its own`);
    }
    byUid.set(user.uid, user);
    users.push(user);
  }
  return users;
}

// Whether two slots run their browsers as one user, where a check can read the browser profiles of the other slots'.
function sharedRunServerUser(runAs) {
  return !!runAs && runAs.length > 1 && new Set(runAs.map((u) => u.uid)).size < runAs.length;
}

function agentConfig(argv = process.argv.slice(2), env = process.env, cwd = process.cwd(), system = {}) {
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
  const runAs = runServerUsers(get('run-server-user'), slots, system);
  return { hub, token: get('token') || null, name, slots, basePort, cli, browsers, statusFile, logDir, ephemeral, runAs };
}

module.exports = { hubConfig, agentConfig, parseArgs, resolveCli, runServerUser, runServerUsers, sharedRunServerUser, AGENT_USAGE };
