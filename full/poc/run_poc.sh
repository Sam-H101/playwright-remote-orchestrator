#!/usr/bin/env bash
# Runs one POC check once through the orchestrator's grid, in a throwaway container from the checks image
# (the image main.py runs in), on the compose network, with the hub's client token taken from the hub
# container. Like main.py's containers it mounts the repo's src at /app/src and runs from /app. The token
# is not printed: it is replaced by <token> in everything this script shows.
# Run inside the WSL distro that hosts the compose project:
#   bash full/poc/run_poc.sh poc_shop_journey.py [node]        (node: pin the run to one runner)
# Env: POC_DURATION_S (default 180), PWDEBUG-style extra pytest args in PYTEST_ARGS.
set -u
check=${1:?usage: run_poc.sh <check file> [node]}
node=${2:-}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
token=$(docker exec playwright_hub printenv GATEWAY_CLIENT_TOKEN | tr -d '\r\n')
[ -n "$token" ] || { echo "could not read the hub's client token"; exit 2; }
endpoint="ws://hub:4500/connect?gateway_token=${token}"
[ -n "$node" ] && endpoint="${endpoint}&node=${node}"
redact="s/${token}/<token>/g"
name="poc-run-$(basename "$check" .py | tr '_' '-')-$$"
start=$(date +%s)
docker run --rm --name "$name" --init --network playwright-remote-orchestrator_playwright \
  -v "$here:/app/poc" -v "$repo/src:/app/src" -w /app \
  -e PW_WS_ENDPOINT="$endpoint" -e POC_DURATION_S="${POC_DURATION_S:-180}" \
  playwright_orchestrator_web python3 -m pytest -p no:cacheprovider -q -rA ${PYTEST_ARGS:-} "/app/poc/$check" 2>&1 \
  | sed "$redact"
code=${PIPESTATUS[0]}
echo "== $check: exit $code after $(( $(date +%s) - start ))s"
exit "$code"
