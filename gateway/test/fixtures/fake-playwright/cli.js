'use strict';

// Stand-in for playwright-core's cli.js in tests: `cli.js run-server --port <p> --host <h>` starts a
// WebSocket echo server. Text commands: "info" returns the upgrade url and headers, "lastclose" the
// close code/reason of the previous connection, "close:<code>:<reason>" closes from the server side.
// A client sending x-fake-reject: 1 gets HTTP 428, like run-server on a version mismatch.

const http = require('node:http');
const WebSocket = require('ws');

const args = process.argv.slice(2);
const opt = (name) => args[args.indexOf(name) + 1];
if (args[0] !== 'run-server') {
  console.error(`unsupported command ${args[0]}`);
  process.exit(1);
}

let lastClose = null;
const server = http.createServer((req, res) => res.writeHead(404).end());
const wss = new WebSocket.WebSocketServer({ noServer: true });
server.on('upgrade', (req, socket, head) => {
  if (req.headers['x-fake-reject']) {
    socket.end('HTTP/1.1 428 Precondition Required\r\nConnection: close\r\nContent-Length: 16\r\n\r\nversion mismatch');
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    ws.on('message', (data, isBinary) => {
      const text = isBinary ? null : data.toString();
      if (text === 'info') return ws.send(JSON.stringify({ url: req.url, headers: req.headers }));
      if (text === 'lastclose') return ws.send(JSON.stringify(lastClose));
      if (text && text.startsWith('close:')) {
        const [, code, reason] = text.split(':');
        return ws.close(Number(code), reason);
      }
      ws.send(data, { binary: isBinary });
    });
    ws.on('close', (code, reason) => { lastClose = { code, reason: reason.toString() }; });
  });
});
server.listen(Number(opt('--port')), opt('--host'), () => console.log(`Listening on ws://${opt('--host')}:${opt('--port')}/`));
