#!/usr/bin/env bash
# End to end, for docker-images.yml: a hub (no database) and a runner container from the given images,
# then e2e_check.py in the checks image opens a page in Chromium, Firefox and WebKit through the hub,
# the way a check connects. It fails when the checks' Playwright and the runner's disagree, a browser
# won't start, or the runner never registers.
#
#   .github/scripts/e2e.sh <hub image> <runner image> <checks image>
set -euo pipefail
shopt -s inherit_errexit # a failure inside $(...) counts too

hub_image=$1 runner_image=$2 checks_image=$3
dir=$(cd "$(dirname "$0")" && pwd)
net=e2e-$$
agent_token=e2e-agent-$$ client_token=e2e-client-$$

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    for c in e2e-hub e2e-runner; do echo "--- docker logs $c"; docker logs "$c" 2>&1 | tail -n 40 || true; done
  fi
  docker rm -f e2e-hub e2e-runner > /dev/null 2>&1 || true
  docker network rm "$net" > /dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

docker network create "$net" > /dev/null
docker run -d --name e2e-hub --network "$net" -e HUB_DB=memory \
  -e GATEWAY_AGENT_TOKEN="$agent_token" -e GATEWAY_CLIENT_TOKEN="$client_token" "$hub_image" > /dev/null
docker run -d --name e2e-runner --network "$net" --init --shm-size 1g -e HUB_URL=ws://e2e-hub:4500 \
  -e GATEWAY_AGENT_TOKEN="$agent_token" -e NODE_NAME=e2e-runner -e SLOTS=3 "$runner_image" > /dev/null

# Until the runner's three run-servers are up and healthy.
nodes() {
  docker exec e2e-hub node -e "fetch('http://127.0.0.1:4500/api/nodes').then(r => r.text()).then(console.log, () => console.log('{}'))"
}
for _ in $(seq 1 90); do
  if nodes | grep -q '"healthySlots":3'; then break; fi
  sleep 2
done
echo "hub: $(nodes)"
nodes | grep -q '"healthySlots":3' || { echo "::error::the runner did not register three healthy slots"; exit 1; }

docker run --rm --network "$net" -v "$dir/e2e_check.py:/e2e_check.py:ro" \
  -e PW_WS_ENDPOINT=ws://e2e-hub:4500/connect -e GATEWAY_CLIENT_TOKEN="$client_token" \
  "$checks_image" python /e2e_check.py
