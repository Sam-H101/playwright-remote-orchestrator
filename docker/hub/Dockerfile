# Gateway hub: the one port checks and runner agents connect to. Build context is the repo root.
FROM node:22-slim
ENV NODE_ENV=production
WORKDIR /app/gateway
COPY gateway/package.json gateway/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
# Only the runtime files, so a host node_modules or the tests never end up in the image.
COPY gateway/hub.js gateway/agent.js ./
COPY gateway/lib ./lib
COPY src/schema.sql /app/src/schema.sql
ENV HUB_HOST=0.0.0.0 \
    HUB_PORT=4500 \
    SCHEMA_PATH=/app/src/schema.sql
USER node
EXPOSE 4500
# start-period covers the hub waiting for MySQL on first boot.
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.HUB_PORT||4500)+'/healthz').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]
CMD ["node", "hub.js"]
