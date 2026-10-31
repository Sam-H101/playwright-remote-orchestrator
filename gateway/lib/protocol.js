'use strict';

const crypto = require('node:crypto');
const WebSocket = require('ws');

const AGENT_VERSION = require('../package.json').version;
const NAME_RE = /^[A-Za-z0-9._-]{1,128}$/;
const MAX_SLOTS = 64;
// Playwright moves screenshots, traces and downloads over the socket; stay above its own limit.
const MAX_PAYLOAD = 256 * 1024 * 1024;

const CLOSE = {
  TRY_AGAIN_LATER: 1013, // the hub can't check the token right now (its database is down)
  BAD_REQUEST: 4400,
  UNAUTHORIZED: 4401,
  FORBIDDEN: 4403, // a managed agent token bound to another node name, or a name reserved for its bound token
  REPLACED: 4409,
};
// Managed access tokens as the Grid UI makes them: pwga_ (agent) or pwgc_ (client) + 43 base64url chars.
const MANAGED_TOKEN_RE = /^pwg[ac]_[A-Za-z0-9_-]{43}$/;

const DROP_HEADERS = new Set([
  'host', 'connection', 'upgrade', 'content-length', 'transfer-encoding', 'authorization',
  'x-gateway-token', 'keep-alive', 'te', 'trailer',
]);

function isValidName(name) {
  return typeof name === 'string' && NAME_RE.test(name);
}

// Node names are case-insensitive (MySQL's default collation compares them that way), so the hub and
// the agent both use the lower-case form.
function canonicalName(name) {
  return String(name).toLowerCase();
}

function sanitizeName(name) {
  return String(name).replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 128) || 'node';
}

// Client request headers the agent passes on to run-server. x-playwright-* and user-agent carry the
// browser name, launch options and client version that run-server needs.
function forwardHeaders(headers) {
  const out = {};
  for (const [key, value] of Object.entries(headers)) {
    const k = key.toLowerCase();
    if (DROP_HEADERS.has(k) || k.startsWith('sec-websocket-') || k.startsWith('proxy-')) continue;
    out[k] = value;
  }
  return out;
}

// Drops gateway_token from a raw query string but keeps every other pair byte for byte.
function stripQueryParam(query, param) {
  if (!query) return '';
  return query.split('&').filter((pair) => {
    if (!pair) return false;
    const key = pair.split('=')[0].replace(/\+/g, ' ');
    try {
      return decodeURIComponent(key) !== param;
    } catch {
      return true;
    }
  }).join('&');
}

function queryParam(query, param) {
  return new URLSearchParams(query || '').get(param);
}

// Some close codes are reserved for reporting and must never be sent on the wire.
function sendableCloseCode(code) {
  if ((code >= 1000 && code <= 1014 && code !== 1004 && code !== 1005 && code !== 1006) || (code >= 3000 && code <= 4999)) return code;
  return code === 1005 ? 1000 : 1011;
}

function closeReason(reason) {
  let text = Buffer.isBuffer(reason) ? reason.toString('utf8') : String(reason || '');
  while (Buffer.byteLength(text) > 123) text = text.slice(0, -1);
  return text;
}

function safeClose(ws, code = 1000, reason = '') {
  if (!ws) return;
  try {
    if (ws.readyState === WebSocket.CONNECTING) ws.terminate();
    else if (ws.readyState === WebSocket.OPEN) ws.close(sendableCloseCode(code), closeReason(reason));
  } catch {
    ws.terminate();
  }
}

function safeSend(ws, message) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(message), (err) => err && ws.terminate());
  return true;
}

function parseMessage(data, isBinary) {
  if (isBinary) return null;
  try {
    const message = JSON.parse(data.toString('utf8'));
    return message && typeof message === 'object' && typeof message.type === 'string' ? message : null;
  } catch {
    return null;
  }
}

function tokenMatches(expected, actual) {
  if (typeof actual !== 'string') return false;
  const a = crypto.createHash('sha256').update(expected).digest();
  const b = crypto.createHash('sha256').update(actual).digest();
  return crypto.timingSafeEqual(a, b);
}

function bearer(header) {
  const match = /^Bearer\s+(.+)$/i.exec(header || '');
  return match ? match[1].trim() : null;
}

// What accessTokens.tokenHash holds: lower-case hex sha256 of the exact token string.
function hashToken(token) {
  return crypto.createHash('sha256').update(token, 'utf8').digest('hex');
}

// Whether `token` has the shape of a managed token; nothing else can be in accessTokens.
function isManagedToken(token) {
  return typeof token === 'string' && MANAGED_TOKEN_RE.test(token);
}

// A token is never logged; a managed one may be named by its display prefix (the first 12 chars,
// which leave 36 of its 43 random chars unknown). Anything else could be short enough to give away.
function tokenLabel(token) {
  return isManagedToken(token) ? `token ${token.slice(0, 12)}...` : 'token';
}

function normalizeSlots(slots) {
  if (!Array.isArray(slots) || slots.length < 1 || slots.length > MAX_SLOTS) return null;
  const out = slots.map((s, i) => ({ index: i + 1, healthy: !!(s && s.healthy) }));
  return slots.every((s, i) => s && s.index === i + 1) ? out : null;
}

module.exports = {
  AGENT_VERSION, MAX_SLOTS, MAX_PAYLOAD, CLOSE,
  isValidName, canonicalName, sanitizeName, forwardHeaders, stripQueryParam, queryParam,
  sendableCloseCode, closeReason, safeClose, safeSend, parseMessage, tokenMatches, bearer, hashToken, isManagedToken, tokenLabel,
  normalizeSlots,
};
