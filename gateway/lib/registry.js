'use strict';

const fs = require('node:fs');
const { hashToken } = require('./protocol');

// Both registries share this interface. `info` is
// { name, slots: [{index, healthy}], playwrightVersion, agentVersion, platform, browsers: [], remoteAddress, ephemeral, tokenId }.
//   init()                    create the tables and mark every node offline (the hub owns all live connections)
//   register(info)            upsert the node and its slots
//   heartbeat(name, slots)    refresh lastSeen and slot health; resolves false if the node row is missing
//   disconnect(name)          mark the node offline
//   staleEphemeral(graceMs)   names of offline ephemeral nodes last seen more than graceMs ago
//   reapEphemeral(name)       if the node is offline and ephemeral, delete its free slots and, when no
//                             slot is still in use, its row; in-use slots are marked down. Resolves
//                             'removed', 'held' (in-use slots remain) or 'skipped'
//   lookupToken(hash)         the managed access token whose sha256 hex is `hash`, as
//                             { id, name, kind, prefix, nodeName, expiresAt, revokedAt } (times ISO-8601 UTC or null), or null
//   anyTokens(kind, activeOnly)  whether a token of that kind exists; revoked and expired ones count too, so
//                             revoking or letting the last token lapse doesn't switch enforcement off.
//                             activeOnly leaves out revoked ones (GATEWAY_ALLOW_OPEN_AGENTS / _CLIENTS)
//   nameHolder(name)          the still-valid agent token bound to `name` that the node last registered with,
//                             as { id, name, prefix }, or null: that token's node name is reserved for it
//   touchToken(id)            set the token's lastUsedAt to now
//   close()

function splitSchema(sql) {
  return sql.split(/^[ \t]*;[ \t]*\r?$/m)
    .map((stmt) => stmt.split(/\r?\n/).filter((line) => !/^\s*--/.test(line)).join('\n').trim())
    .filter(Boolean);
}

const healthyList = (slots) => slots.filter((s) => s.healthy).map((s) => s.index).join(',');

// Columns added to the nodes table after it first shipped; CREATE TABLE IF NOT EXISTS won't add them.
const NODE_COLUMNS = [
  ['ephemeral', 'TINYINT NOT NULL DEFAULT 0'],
  ['tokenId', 'INT NULL'],
];

// mysql2 hands DATETIMEs back as 'YYYY-MM-DD HH:MM:SS' (dateStrings); they hold UTC.
const isoUtc = (value) => (value ? `${String(value).replace(' ', 'T')}Z` : null);

class MySqlRegistry {
  constructor(settings, { schemaPath, log, retries = 40, retryDelayMs = 3000 } = {}) {
    this.kind = 'mysql';
    this.settings = settings;
    this.schemaPath = schemaPath;
    this.log = log;
    this.retries = retries;
    this.retryDelayMs = retryDelayMs;
    this.pool = null;
  }

  async init() {
    const mysql = require('mysql2/promise');
    const statements = splitSchema(fs.readFileSync(this.schemaPath, 'utf8'));
    if (!statements.length) throw new Error(`no SQL statements in ${this.schemaPath}`);
    this.statements = statements;
    this.pool = mysql.createPool({
      ...this.settings,
      connectionLimit: 5,
      enableKeepAlive: true,
      timezone: 'Z',
      dateStrings: true,
    });
    // Same idea as src/database.py: MySQL may still be starting when the hub comes up.
    for (let attempt = 1; ; attempt++) {
      try {
        await this.applySchema();
        break;
      } catch (err) {
        if (attempt >= this.retries || !isConnectionError(err)) throw err;
        this.log.warn(`waiting for mysql at ${this.settings.host}:${this.settings.port}: ${err.code || err.message} (attempt ${attempt}/${this.retries})`);
        await new Promise((r) => setTimeout(r, this.retryDelayMs));
      }
    }
    await this.pool.query('UPDATE nodes SET online = 0');
  }

  async register(info) {
    const n = info.slots.length;
    const values = [n, info.playwrightVersion, info.agentVersion, info.platform, info.browsers.join(',').slice(0, 255), info.remoteAddress,
      info.ephemeral ? 1 : 0, info.tokenId ?? null];
    await this.transaction(async (conn) => {
      const [known] = await conn.query('SELECT 1 FROM nodes WHERE name = ?', [info.name]);
      // Names compare case-insensitively here, so a node that comes back as "runner-a" after registering
      // as "Runner-A" hits the same row; the row and its slots take the new spelling.
      await conn.query(
        `INSERT INTO nodes (name, online, slots, playwrightVersion, agentVersion, platform, browsers, remoteAddress, ephemeral, tokenId, registeredAt, connectedAt, lastSeen)
         VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP())
         ON DUPLICATE KEY UPDATE name = ?, online = 1, slots = ?, playwrightVersion = ?, agentVersion = ?, platform = ?, browsers = ?,
           remoteAddress = ?, ephemeral = ?, tokenId = ?, connectedAt = UTC_TIMESTAMP(), lastSeen = UTC_TIMESTAMP()`,
        [info.name, ...values, info.name, ...values]);
      await conn.query('UPDATE portStatus SET PortServer = ? WHERE PortServer = ?', [info.name, info.name]);
      // Insert only the missing slots: even an ignored duplicate insert burns an AUTO_INCREMENT id.
      const [rows] = await conn.query('SELECT PortNumber FROM portStatus WHERE PortServer = ?', [info.name]);
      const existing = new Set(rows.map((r) => Number(r.PortNumber)));
      const missing = info.slots.filter((s) => !existing.has(s.index));
      if (missing.length) {
        await conn.query(
          `INSERT IGNORE INTO portStatus (PortNumber, PortStatus, PortServer, LastUpdateTime) VALUES ${missing.map(() => '(?, ?, ?, UTC_TIMESTAMP())').join(', ')}`,
          missing.flatMap((s) => [s.index, s.healthy ? 1 : 0, info.name]));
      }
      await this.updateSlots(conn, info.name, info.slots);
      await conn.query(
        `INSERT IGNORE INTO portUsage (id, portNumber, inUse, LastRunDate, failCount)
         SELECT id, PortNumber, 0, NULL, 0 FROM portStatus WHERE PortServer = ?`,
        [info.name]);
      // Slots beyond the new count go away unless a check still holds them; those are just marked down.
      // FOR UPDATE keeps a check from claiming one between the select and the delete.
      const [extra] = await conn.query(
        `SELECT ps.id, COALESCE(pu.inUse, 0) AS inUse FROM portStatus ps LEFT JOIN portUsage pu ON pu.id = ps.id
         WHERE ps.PortServer = ? AND ps.PortNumber > ? FOR UPDATE`,
        [info.name, n]);
      if (!known.length && extra.length) {
        this.log.warn(`node ${info.name} takes over ${extra.length} slot(s) of the static host of the same name; remove it from hosts: in database.yaml`);
      }
      const idle = extra.filter((r) => !Number(r.inUse)).map((r) => r.id);
      if (idle.length) await conn.query('DELETE FROM portStatus WHERE id IN (?)', [idle]);
      await conn.query(
        'UPDATE portStatus SET PortStatus = 0, LastUpdateTime = UTC_TIMESTAMP() WHERE PortServer = ? AND PortNumber > ? AND PortStatus <> 0',
        [info.name, n]);
    });
  }

  async heartbeat(name, slots) {
    let result;
    try {
      [result] = await this.pool.query('UPDATE nodes SET lastSeen = UTC_TIMESTAMP(), online = 1 WHERE name = ?', [name]);
    } catch (err) {
      // The tables were dropped under us (e.g. MySQL came back without its data): recreate them, and
      // resolving false makes the hub register the node again.
      if (err.code !== 'ER_NO_SUCH_TABLE') throw err;
      this.log.warn(`table missing (${err.message}); recreating the schema`);
      await this.applySchema();
      return false;
    }
    if (!result.affectedRows) return false;
    await this.updateSlots(this.pool, name, slots);
    // A slot beyond the count that a check still held at register time goes away once it is released.
    // InnoDB share-locks the subquery rows, so a check can't claim one between the read and the delete.
    await this.pool.query(
      'DELETE FROM portStatus WHERE PortServer = ? AND PortNumber > ? AND id IN (SELECT id FROM portUsage WHERE inUse = 0)',
      [name, slots.length]);
    return true;
  }

  async disconnect(name) {
    await this.pool.query('UPDATE nodes SET online = 0 WHERE name = ?', [name]);
  }

  async staleEphemeral(graceMs) {
    const [rows] = await this.pool.query(
      'SELECT name FROM nodes WHERE ephemeral = 1 AND online = 0 AND (lastSeen IS NULL OR lastSeen < UTC_TIMESTAMP() - INTERVAL ? SECOND)',
      [Math.ceil(graceMs / 1000)]);
    return rows.map((r) => r.name);
  }

  async reapEphemeral(name) {
    let result = 'skipped';
    await this.transaction(async (conn) => {
      result = 'skipped';
      const [[node]] = await conn.query('SELECT online, ephemeral FROM nodes WHERE name = ? FOR UPDATE', [name]);
      if (!node || Number(node.online) || !Number(node.ephemeral)) return;
      // FOR UPDATE keeps a check from claiming a slot between the select and the delete.
      const [slots] = await conn.query(
        `SELECT ps.id, COALESCE(pu.inUse, 0) AS inUse FROM portStatus ps LEFT JOIN portUsage pu ON pu.id = ps.id
         WHERE ps.PortServer = ? FOR UPDATE`,
        [name]);
      const idle = slots.filter((r) => !Number(r.inUse)).map((r) => r.id);
      if (idle.length) await conn.query('DELETE FROM portStatus WHERE id IN (?)', [idle]);
      if (idle.length < slots.length) {
        await conn.query('UPDATE portStatus SET PortStatus = 0, LastUpdateTime = UTC_TIMESTAMP() WHERE PortServer = ? AND PortStatus <> 0', [name]);
        result = 'held';
        return;
      }
      await conn.query('DELETE FROM nodes WHERE name = ?', [name]);
      result = 'removed';
    });
    return result;
  }

  async lookupToken(hash) {
    const [[row]] = await this.query(
      'SELECT id, name, kind, prefix, nodeName, expiresAt, revokedAt FROM accessTokens WHERE tokenHash = ?', [hash]);
    if (!row) return null;
    return {
      id: Number(row.id), name: row.name, kind: row.kind, prefix: row.prefix,
      nodeName: row.nodeName ? String(row.nodeName).toLowerCase() : null,
      expiresAt: isoUtc(row.expiresAt), revokedAt: isoUtc(row.revokedAt),
    };
  }

  async anyTokens(kind, activeOnly = false) {
    const [rows] = await this.query(`SELECT 1 FROM accessTokens WHERE kind = ?${activeOnly ? ' AND revokedAt IS NULL' : ''} LIMIT 1`, [kind]);
    return rows.length > 0;
  }

  async nameHolder(name) {
    const [[row]] = await this.query(
      `SELECT t.id, t.name, t.prefix FROM nodes n JOIN accessTokens t ON t.id = n.tokenId
       WHERE n.name = ? AND t.kind = 'agent' AND LOWER(t.nodeName) = ? AND t.revokedAt IS NULL
         AND (t.expiresAt IS NULL OR t.expiresAt > UTC_TIMESTAMP())`,
      [name, name.toLowerCase()]);
    return row ? { id: Number(row.id), name: row.name, prefix: row.prefix } : null;
  }

  async touchToken(id) {
    await this.query('UPDATE accessTokens SET lastUsedAt = UTC_TIMESTAMP() WHERE id = ?', [id]);
  }

  // A one-off query that survives the tables being dropped under us (see heartbeat).
  async query(sql, params) {
    try {
      return await this.pool.query(sql, params);
    } catch (err) {
      if (err.code !== 'ER_NO_SUCH_TABLE') throw err;
      this.log.warn(`table missing (${err.message}); recreating the schema`);
      await this.applySchema();
      return this.pool.query(sql, params);
    }
  }

  async applySchema() {
    for (const stmt of this.statements) await this.pool.query(stmt);
    const [cols] = await this.pool.query(
      `SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nodes'`);
    const have = new Set(cols.map((c) => String(c.name).toLowerCase()));
    for (const [column, type] of NODE_COLUMNS) {
      if (have.has(column.toLowerCase())) continue;
      try {
        await this.pool.query(`ALTER TABLE nodes ADD COLUMN ${column} ${type}`);
        this.log.info(`added the ${column} column to the nodes table`);
      } catch (err) {
        if (err.code !== 'ER_DUP_FIELDNAME') throw err; // another process added it first
      }
    }
  }

  async close() {
    const pool = this.pool;
    this.pool = null;
    if (pool) await pool.end().catch(() => {});
  }

  // LastUpdateTime records the last health change. MySQL applies single-table SET assignments left
  // to right, so it is computed before PortStatus is overwritten.
  updateSlots(conn, name, slots) {
    const healthy = healthyList(slots);
    return conn.query(
      `UPDATE portStatus
       SET LastUpdateTime = IF(PortStatus = (FIND_IN_SET(PortNumber, ?) > 0), LastUpdateTime, UTC_TIMESTAMP()),
           PortStatus = (FIND_IN_SET(PortNumber, ?) > 0)
       WHERE PortServer = ? AND PortNumber BETWEEN 1 AND ?`,
      [healthy, healthy, name, slots.length]);
  }

  async transaction(fn) {
    for (let attempt = 1; ; attempt++) {
      const conn = await this.pool.getConnection();
      try {
        await conn.beginTransaction();
        await fn(conn);
        await conn.commit();
        return;
      } catch (err) {
        await conn.rollback().catch(() => {});
        // Checks claim slots concurrently; a deadlock only means try again.
        if (attempt >= 3 || !['ER_LOCK_DEADLOCK', 'ER_LOCK_WAIT_TIMEOUT', 'ER_NO_SUCH_TABLE'].includes(err.code)) throw err;
        if (err.code === 'ER_NO_SUCH_TABLE') {
          this.log.warn(`table missing (${err.message}); recreating the schema`);
          await this.applySchema();
        }
      } finally {
        conn.release();
      }
    }
  }
}

// Errors worth waiting out at start-up. Access/database errors are included because the MySQL image
// creates the user and database on first boot, after the server already accepts connections.
function isConnectionError(err) {
  return err.fatal === true || ['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT', 'ECONNRESET', 'EHOSTUNREACH',
    'PROTOCOL_CONNECTION_LOST', 'ER_CON_COUNT_ERROR', 'ER_SERVER_SHUTDOWN', 'ER_ACCESS_DENIED_ERROR', 'ER_BAD_DB_ERROR'].includes(err.code);
}

class MemoryRegistry {
  constructor() {
    this.kind = 'memory';
    this.nodes = new Map();
    this.slots = new Map();
    this.tokens = new Map(); // id -> accessTokens row; tests add them with addToken
    this.nextTokenId = 1;
  }

  async init() {
    for (const node of this.nodes.values()) node.online = 0;
  }

  async register(info) {
    const now = new Date().toISOString();
    const prev = this.nodes.get(info.name);
    this.nodes.set(info.name, {
      name: info.name, online: 1, slots: info.slots.length,
      playwrightVersion: info.playwrightVersion, agentVersion: info.agentVersion, platform: info.platform,
      browsers: info.browsers.join(','), remoteAddress: info.remoteAddress, ephemeral: info.ephemeral ? 1 : 0,
      tokenId: info.tokenId ?? null, registeredAt: prev ? prev.registeredAt : now, connectedAt: now, lastSeen: now,
    });
    const slots = this.slots.get(info.name) || new Map();
    for (const [index, slot] of slots) {
      if (index <= info.slots.length) continue;
      if (slot.inUse) slot.status = 0;
      else slots.delete(index);
    }
    for (const s of info.slots) {
      const slot = slots.get(s.index) || { inUse: 0 };
      slot.status = s.healthy ? 1 : 0;
      slots.set(s.index, slot);
    }
    this.slots.set(info.name, slots);
  }

  async heartbeat(name, slots) {
    const node = this.nodes.get(name);
    if (!node) return false;
    node.lastSeen = new Date().toISOString();
    node.online = 1;
    const rows = this.slots.get(name);
    for (const s of slots) if (rows.has(s.index)) rows.get(s.index).status = s.healthy ? 1 : 0;
    return true;
  }

  async disconnect(name) {
    const node = this.nodes.get(name);
    if (node) node.online = 0;
  }

  async staleEphemeral(graceMs) {
    const cutoff = Date.now() - graceMs;
    return [...this.nodes.values()]
      .filter((n) => n.ephemeral && !n.online && Date.parse(n.lastSeen) < cutoff)
      .map((n) => n.name);
  }

  async reapEphemeral(name) {
    const node = this.nodes.get(name);
    if (!node || node.online || !node.ephemeral) return 'skipped';
    const slots = this.slots.get(name) || new Map();
    for (const [index, slot] of slots) {
      if (slot.inUse) slot.status = 0;
      else slots.delete(index);
    }
    if (slots.size) return 'held';
    this.slots.delete(name);
    this.nodes.delete(name);
    return 'removed';
  }

  // What the Grid UI does when it creates a token: keep only the hash. Returns the stored row; tests set its
  // revokedAt / expiresAt (ISO-8601 strings) directly.
  addToken(token, { name = 'test token', kind = 'agent', nodeName = null, expiresAt = null, revokedAt = null } = {}) {
    const row = {
      id: this.nextTokenId++, name, kind, tokenHash: hashToken(token), prefix: token.slice(0, 12),
      nodeName: nodeName ? nodeName.toLowerCase() : null, createdAt: new Date().toISOString(), expiresAt, lastUsedAt: null, revokedAt,
    };
    this.tokens.set(row.id, row);
    return row;
  }

  async lookupToken(hash) {
    const t = [...this.tokens.values()].find((row) => row.tokenHash === hash);
    if (!t) return null;
    return { id: t.id, name: t.name, kind: t.kind, prefix: t.prefix, nodeName: t.nodeName, expiresAt: t.expiresAt, revokedAt: t.revokedAt };
  }

  async anyTokens(kind, activeOnly = false) {
    return [...this.tokens.values()].some((t) => t.kind === kind && !(activeOnly && t.revokedAt));
  }

  async nameHolder(name) {
    const node = this.nodes.get(name.toLowerCase());
    const t = node && node.tokenId ? this.tokens.get(node.tokenId) : null;
    if (!t || t.kind !== 'agent' || t.nodeName !== name.toLowerCase() || t.revokedAt) return null;
    if (t.expiresAt && Date.parse(t.expiresAt) <= Date.now()) return null;
    return { id: t.id, name: t.name, prefix: t.prefix };
  }

  async touchToken(id) {
    const token = this.tokens.get(id);
    if (token) token.lastUsedAt = new Date().toISOString();
  }

  async close() {}
}

function createRegistry(config, log) {
  if (config.db === 'memory') return new MemoryRegistry();
  return new MySqlRegistry(config.mysql, { schemaPath: config.schemaPath, log });
}

module.exports = { MySqlRegistry, MemoryRegistry, createRegistry, splitSchema };
