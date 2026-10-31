'use strict';

const WebSocket = require('ws');
const { safeClose } = require('./protocol');

// Past this many unsent bytes on one side, the other side stops being read until they drain, so a slow
// reader pushes back on the sender (through TCP) instead of growing this process's memory.
const HIGH_WATER = 4 * 1024 * 1024;
const LOW_WATER = 1024 * 1024;

// Joins two sockets frame for frame. Frames that arrive while the other side is still connecting are
// queued. When either side closes, the other is closed with the same (sendable) code and reason.
function pipe(a, b, onDone, { highWater = HIGH_WATER, lowWater = LOW_WATER } = {}) {
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    if (onDone) onDone();
  };
  const forward = (from, to) => {
    const queue = [];
    let paused = false;
    const sent = () => {
      if (paused && to.bufferedAmount <= lowWater) {
        paused = false;
        from.resume();
      }
    };
    const send = (data, isBinary) => {
      to.send(data, { binary: isBinary }, sent);
      if (!paused && to.bufferedAmount > highWater) {
        paused = true;
        from.pause();
      }
    };
    from.on('message', (data, isBinary) => {
      if (to.readyState === WebSocket.OPEN) send(data, isBinary);
      else if (to.readyState === WebSocket.CONNECTING) queue.push([data, isBinary]);
    });
    to.once('open', () => {
      for (const [data, isBinary] of queue.splice(0)) send(data, isBinary);
    });
    from.on('close', (code, reason) => {
      safeClose(to, code, reason);
      finish();
    });
    // 'close' always follows 'error', so the close handler does the cleanup.
    from.on('error', () => {});
  };
  forward(a, b);
  forward(b, a);
}

module.exports = { pipe };
