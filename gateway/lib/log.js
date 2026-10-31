'use strict';

function createLogger(prefix) {
  const tag = prefix ? `[${prefix}] ` : '';
  const write = (stream, args) => stream.write(`${new Date().toISOString()} ${tag}${args.map(String).join(' ')}\n`);
  return {
    info: (...args) => write(process.stdout, args),
    warn: (...args) => write(process.stdout, ['WARN', ...args]),
    error: (...args) => write(process.stderr, ['ERROR', ...args]),
  };
}

const silent = { info() {}, warn() {}, error() {} };

module.exports = { createLogger, silent };
