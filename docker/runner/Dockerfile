# Linux runner node: the gateway agent plus N `playwright run-server` slots. It only dials out to the
# hub, so it publishes no ports; scale with `docker compose up --scale runner=N`. It registers under its
# container ID (the host name) as an ephemeral node, so the hub forgets it EPHEMERAL_GRACE_S after it
# goes away instead of keeping an offline node per recreated container. Build context is the
# repo root.
# playwright-core is the one runner/playwright pins (Dependabot updates it), and the browsers are the
# ones that version installs, so they always match: Chromium, Firefox and WebKit, which BROWSERS offers
# to the hub. Ubuntu 24.04 and Node 26 with the browsers' system packages, like Playwright's own image.
FROM node:26-slim AS node

FROM ubuntu:noble
ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NODE_ENV=production \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Node from the official image (Dependabot moves it along with the hub's); its binary needs libatomic.
# tzdata, as in Playwright's image, so a TZ given to the container works.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates libatomic1 tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash pwuser
WORKDIR /app/playwright
COPY runner/playwright/package.json runner/playwright/package-lock.json ./
# install-deps runs apt-get itself. The browser folders are open to every user, as in Playwright's image.
RUN npm ci --no-audit --no-fund \
    && node node_modules/playwright-core/cli.js install-deps chromium firefox webkit \
    && node node_modules/playwright-core/cli.js install chromium firefox webkit \
    && rm -rf /var/lib/apt/lists/* \
    && npm cache clean --force \
    && chmod -R 777 /ms-playwright
WORKDIR /app/gateway
COPY gateway/package.json gateway/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY gateway/hub.js gateway/agent.js ./
COPY gateway/lib ./lib
RUN mkdir -p /tmp/gateway-agent && chown pwuser:pwuser /tmp/gateway-agent
ENV HUB_URL=ws://hub:4500 \
    SLOTS=4 \
    BASE_PORT=3100 \
    BROWSERS=chromium,firefox,webkit \
    PLAYWRIGHT_CLI=/app/playwright/node_modules/playwright-core/cli.js \
    STATUS_FILE=/tmp/gateway-agent/status.json \
    EPHEMERAL=1
USER pwuser
# Healthy while registered with the hub and the status file is fresh.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["node", "-e", "const s=JSON.parse(require('fs').readFileSync(process.env.STATUS_FILE,'utf8'));process.exit(s.registered&&Date.now()-Date.parse(s.updated)<30000?0:1)"]
CMD ["node", "agent.js"]
