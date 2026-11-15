'use strict';

// Preloaded into each run-server (NODE_OPTIONS=--require) by the agent. If the agent dies without
// cleaning up (killed from Task Manager, OOM, power-cycled service), this exits run-server too, so
// orphans don't keep holding the slot ports and its browsers are closed by Playwright's exit hooks.
//
// It also gives run-server its endpoint path (--path) from GATEWAY_AGENT_RUN_SERVER_PATH: a random path the
// agent dials, so a browser of another slot that reaches this port on 127.0.0.1 can't open a session here.
// It comes through the environment, which only run-server's own user can read, and not the command line,
// which every user can read in /proc.
const parent = process.ppid;
if (process.env.GATEWAY_AGENT_NODE_OPTIONS !== undefined) {
  process.env.NODE_OPTIONS = process.env.GATEWAY_AGENT_NODE_OPTIONS;
  delete process.env.GATEWAY_AGENT_NODE_OPTIONS;
  if (!process.env.NODE_OPTIONS) delete process.env.NODE_OPTIONS;
}
if (process.env.GATEWAY_AGENT_RUN_SERVER_PATH !== undefined) {
  const endpoint = process.env.GATEWAY_AGENT_RUN_SERVER_PATH;
  delete process.env.GATEWAY_AGENT_RUN_SERVER_PATH;
  if (process.argv.includes('run-server')) process.argv.push('--path', endpoint);
}
setInterval(() => {
  try {
    process.kill(parent, 0);
  } catch (err) {
    if (err.code === 'ESRCH') process.exit(0);
  }
}, 2000).unref();
