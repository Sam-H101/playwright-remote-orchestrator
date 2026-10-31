'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const P = require('../lib/protocol');
const { splitSchema } = require('../lib/registry');
const { agentConfig, hubConfig } = require('../lib/config');
const { FAKE_CLI } = require('./helpers');

test('forwardHeaders keeps Playwright headers and drops hop-by-hop, handshake and auth headers', () => {
  const out = P.forwardHeaders({
    host: 'hub:4500', connection: 'Upgrade', upgrade: 'websocket', 'sec-websocket-key': 'k', 'sec-websocket-version': '13',
    'sec-websocket-extensions': 'permessage-deflate', 'content-length': '0', 'transfer-encoding': 'chunked',
    authorization: 'Bearer x', 'x-gateway-token': 't', 'proxy-authorization': 'p',
    'user-agent': 'Playwright/1.64.1', 'x-playwright-browser': 'chromium', 'x-playwright-launch-options': '{}', 'x-custom': '1',
  });
  assert.deepEqual(out, { 'user-agent': 'Playwright/1.64.1', 'x-playwright-browser': 'chromium', 'x-playwright-launch-options': '{}', 'x-custom': '1' });
});

test('stripQueryParam removes only gateway_token and keeps the rest verbatim', () => {
  assert.equal(P.stripQueryParam('a=1&gateway_token=x&b=%20c+d', 'gateway_token'), 'a=1&b=%20c+d');
  assert.equal(P.stripQueryParam('gateway%5Ftoken=x', 'gateway_token'), '');
  assert.equal(P.stripQueryParam('', 'gateway_token'), '');
  assert.equal(P.stripQueryParam('launch-options=%7B%7D', 'gateway_token'), 'launch-options=%7B%7D');
});

test('sendableCloseCode passes valid codes and maps reserved ones', () => {
  for (const code of [1000, 1001, 1011, 1014, 3000, 4409, 4999]) assert.equal(P.sendableCloseCode(code), code);
  assert.equal(P.sendableCloseCode(1005), 1000);
  for (const code of [1004, 1006, 1015, 999, 2000, 5000, undefined]) assert.equal(P.sendableCloseCode(code), 1011);
  assert.equal(Buffer.byteLength(P.closeReason('é'.repeat(100))), 122);
});

test('node names', () => {
  assert.ok(P.isValidName('HOST-1.example_a'));
  for (const bad of ['', 'a b', 'x/y', 'a'.repeat(129), 7, null]) assert.equal(P.isValidName(bad), false);
  assert.equal(P.sanitizeName('My PC (2)'), 'My-PC--2-');
  assert.equal(P.canonicalName('Runner-A.Example'), 'runner-a.example');
});

test('splitSchema yields the four CREATE TABLE statements of src/schema.sql', () => {
  const statements = splitSchema(fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'schema.sql'), 'utf8'));
  assert.equal(statements.length, 4);
  assert.deepEqual(statements.map((s) => /CREATE TABLE IF NOT EXISTS (\w+)/.exec(s)[1]), ['portStatus', 'portUsage', 'nodes', 'accessTokens']);
  assert.ok(statements.every((s) => !s.includes('--') && !/;\s*$/.test(s)));
  assert.equal(splitSchema('-- c\nSELECT 1\r\n;\r\n-- only a comment\r\n;\r\n').length, 1);
  assert.match(statements[2], /\bephemeral TINYINT NOT NULL DEFAULT 0\b/);
  assert.match(statements[2], /\btokenId INT NULL\b/);
  assert.match(statements[3], /\btokenHash CHAR\(64\) NOT NULL\b/);
  assert.match(statements[3], /\bUNIQUE KEY uq_token_hash \(tokenHash\)/);
});

test('hashToken is the lower-case hex sha256 of the exact string; tokenLabel shows only a managed prefix', () => {
  assert.equal(P.hashToken('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.notEqual(P.hashToken('abc '), P.hashToken('abc'));
  const managed = `pwga_${'A1b2_-'.repeat(7)}x`;
  assert.equal(managed.length, 48);
  assert.equal(P.tokenLabel(managed), 'token pwga_A1b2_-A...');
  assert.equal(P.tokenLabel(`pwgc_${'z'.repeat(43)}`), 'token pwgc_zzzzzzz...');
  // Anything else may be short enough that 12 chars give it away.
  for (const other of ['agent-secret-123', `pwga_${'a'.repeat(42)}`, null, undefined]) assert.equal(P.tokenLabel(other), 'token');
  assert.deepEqual([P.CLOSE.TRY_AGAIN_LATER, P.CLOSE.UNAUTHORIZED, P.CLOSE.FORBIDDEN], [1013, 4401, 4403]);
});

test('agent config: flags override env, defaults apply', () => {
  const env = { HUB_URL: 'ws://env:1', SLOTS: '8', NODE_NAME: 'from-env', PLAYWRIGHT_CLI: FAKE_CLI };
  let c = agentConfig([], env);
  assert.deepEqual([c.hub, c.slots, c.name, c.basePort, c.browsers, c.token, c.cli], ['ws://env:1', 8, 'from-env', 3100, ['chromium'], null, FAKE_CLI]);
  c = agentConfig(['--hub', 'wss://flag/', '--slots=2', '--name', 'n', '--base-port', '4000', '--browsers', 'chromium, firefox', '--token', 't'], env);
  assert.deepEqual([c.hub, c.slots, c.name, c.basePort, c.browsers, c.token], ['wss://flag', 2, 'n', 4000, ['chromium', 'firefox'], 't']);
  assert.throws(() => agentConfig(['--slots', '65'], env), /SLOTS/);
  assert.throws(() => agentConfig([], { ...env, HUB_URL: 'http://x' }), /ws:\/\//);
  assert.throws(() => agentConfig(['--bogus', '1'], env), /unknown argument/);
  assert.throws(() => agentConfig(['--name', 'a b'], env), /NODE_NAME/);
  assert.match(agentConfig([], { HUB_URL: 'ws://h', PLAYWRIGHT_CLI: FAKE_CLI }).name, /^[a-z0-9._-]+$/);
  assert.equal(agentConfig(['--name', 'Win-01'], env).name, 'win-01');
});

test('agent config: --ephemeral / EPHEMERAL', () => {
  const env = { HUB_URL: 'ws://env:1', PLAYWRIGHT_CLI: FAKE_CLI };
  assert.equal(agentConfig([], env).ephemeral, false);
  for (const value of ['1', 'true', 'TRUE', 'yes']) assert.equal(agentConfig([], { ...env, EPHEMERAL: value }).ephemeral, true);
  for (const value of ['0', 'false', '']) assert.equal(agentConfig([], { ...env, EPHEMERAL: value }).ephemeral, false);
  // The bare flag takes no value, so the next flag is still parsed.
  const c = agentConfig(['--ephemeral', '--name', 'n'], env);
  assert.deepEqual([c.ephemeral, c.name], [true, 'n']);
  assert.equal(agentConfig(['--ephemeral=false'], { ...env, EPHEMERAL: '1' }).ephemeral, false);
  assert.throws(() => agentConfig([], { ...env, EPHEMERAL: 'maybe' }), /EPHEMERAL/);
});

test('hub config defaults', () => {
  const c = hubConfig({});
  assert.equal(c.port, 4500);
  assert.equal(c.host, '0.0.0.0');
  assert.equal(c.db, 'mysql');
  assert.deepEqual(c.mysql, { host: 'mysql', port: 3306, database: 'orchestrator', user: 'orchestrator', password: 'orchestrator' });
  assert.equal(c.heartbeatTimeoutMs, 30000);
  assert.equal(c.openTimeoutMs, 15000);
  assert.equal(c.ephemeralGraceMs, 120000);
  assert.equal(c.tokenCacheMs, 10000);
  assert.equal(c.tokenRecheckMs, 15000);
  assert.deepEqual([hubConfig({ TOKEN_CACHE_S: '0' }).tokenCacheMs, hubConfig({ TOKEN_RECHECK_S: '2' }).tokenRecheckMs], [0, 2000]);
  assert.throws(() => hubConfig({ TOKEN_RECHECK_S: '0' }), /TOKEN_RECHECK_S/);
  assert.deepEqual([c.allowOpenAgents, c.allowOpenClients], [false, false]);
  const open = hubConfig({ GATEWAY_ALLOW_OPEN_AGENTS: '1', GATEWAY_ALLOW_OPEN_CLIENTS: 'true' });
  assert.deepEqual([open.allowOpenAgents, open.allowOpenClients], [true, true]);
  assert.throws(() => hubConfig({ GATEWAY_ALLOW_OPEN_AGENTS: 'maybe' }), /GATEWAY_ALLOW_OPEN_AGENTS/);
  assert.equal(hubConfig({ EPHEMERAL_GRACE_S: '30' }).ephemeralGraceMs, 30000);
  assert.throws(() => hubConfig({ EPHEMERAL_GRACE_S: '-1' }), /EPHEMERAL_GRACE_S/);
  assert.ok(fs.existsSync(c.schemaPath));
  assert.equal(hubConfig({ HUB_DB: 'memory' }).db, 'memory');
  assert.throws(() => hubConfig({ HUB_DB: 'sqlite' }), /HUB_DB/);
});
