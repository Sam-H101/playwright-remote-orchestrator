'use strict';

// Preloaded into each run-server (NODE_OPTIONS=--require) by the agent. If the agent dies without
// cleaning up (killed from Task Manager, OOM, power-cycled service), this exits run-server too, so
// orphans don't keep holding the slot ports and its browsers are closed by Playwright's exit hooks.
const parent = process.ppid;
if (process.env.GATEWAY_AGENT_NODE_OPTIONS !== undefined) {
  process.env.NODE_OPTIONS = process.env.GATEWAY_AGENT_NODE_OPTIONS;
  delete process.env.GATEWAY_AGENT_NODE_OPTIONS;
  if (!process.env.NODE_OPTIONS) delete process.env.NODE_OPTIONS;
}
setInterval(() => {
  try {
    process.kill(parent, 0);
  } catch (err) {
    if (err.code === 'ESRCH') process.exit(0);
  }
}, 2000).unref();
