# Linux runner node: the gateway agent plus N `playwright run-server` slots. It only dials out to the
# hub, so it publishes no ports; scale with `docker compose up --scale runner=N`. It registers under its
# container ID (the host name) as an ephemeral node, so the hub forgets it EPHEMERAL_GRACE_S after it
# goes away instead of keeping an offline node per recreated container. Build context is the
# repo root. PLAYWRIGHT_VERSION must match the checks' Playwright version (same minor at least).
ARG PLAYWRIGHT_VERSION=1.64.1
FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION
ENV NODE_ENV=production \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app/gateway
COPY gateway/package.json gateway/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
# playwright-core is pinned here rather than in package.json so the image tag and the server always
# agree; the browsers for this version are already in /ms-playwright.
RUN npm install --prefix /app/playwright --no-audit --no-fund playwright-core@${PLAYWRIGHT_VERSION} \
    && test "$(node -p "require('/app/playwright/node_modules/playwright-core/package.json').version")" = "${PLAYWRIGHT_VERSION}" \
    && npm cache clean --force
COPY gateway/hub.js gateway/agent.js ./
COPY gateway/lib ./lib
RUN mkdir -p /tmp/gateway-agent && chown pwuser:pwuser /tmp/gateway-agent
ENV HUB_URL=ws://hub:4500 \
    SLOTS=4 \
    BASE_PORT=3100 \
    BROWSERS=chromium \
    PLAYWRIGHT_CLI=/app/playwright/node_modules/playwright-core/cli.js \
    STATUS_FILE=/tmp/gateway-agent/status.json \
    EPHEMERAL=1
USER pwuser
# Healthy while registered with the hub and the status file is fresh.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["node", "-e", "const s=JSON.parse(require('fs').readFileSync(process.env.STATUS_FILE,'utf8'));process.exit(s.registered&&Date.now()-Date.parse(s.updated)<30000?0:1)"]
CMD ["node", "agent.js"]
