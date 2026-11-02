#!/usr/bin/env bash
# Runs one POC check once through the orchestrator's grid, in a throwaway container from the checks image
# (the image main.py runs in), on the compose network, with the hub's client token taken from the hub
# container. Like main.py's containers it mounts the repo's src at /app/src and runs from /app, so checks
# can `from src import mfa` and ask the Grid UI (grid_ui) for MFA codes with that token. POC_TOTP_SEED
# comes from the environment or the repo's .env. Neither the token nor the seed is printed: they are
# replaced by <token> and <seed> in everything this script shows.
# poc_signin_reuse keeps its sign-in in the Grid UI under POC_SIGNIN_NAME, here poc-idp-session-manual unless
# set: never the scheduled poc_signin_reuse's (poc-idp-session), since two runs using one name at once break
# each other's rounds. Runs of this script one after another share theirs, so the second reuses the first's.
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
# Passed by name (-e NAME), so the values stay off the docker command line.
export GATEWAY_CLIENT_TOKEN="$token"
export POC_TOTP_SEED="${POC_TOTP_SEED:-$(sed -n 's/^POC_TOTP_SEED=//p' "$repo/.env" 2>/dev/null | tail -n 1 | tr -d '\r')}"
export POC_SIGNIN_NAME="${POC_SIGNIN_NAME:-poc-idp-session-manual}"
redact="s/${token}/<token>/g"
[ -n "$POC_TOTP_SEED" ] && redact="${redact};s/${POC_TOTP_SEED}/<seed>/g"
name="poc-run-$(basename "$check" .py | tr '_' '-')-$$"
start=$(date +%s)
docker run --rm --name "$name" --init --network playwright-remote-orchestrator_playwright \
  -v "$here:/app/poc" -v "$repo/src:/app/src" -w /app \
  -e PW_WS_ENDPOINT="$endpoint" -e POC_DURATION_S="${POC_DURATION_S:-180}" \
  -e GRID_UI_URL="${GRID_UI_URL:-http://grid_ui:4444}" -e GATEWAY_CLIENT_TOKEN \
  -e POC_TOTP_SEED -e POC_TOTP_SECRET_NAME -e POC_TOTP_PASSWORD \
  -e POC_SIGNIN_SECRET_NAME -e POC_SIGNIN_NAME -e POC_SIGNIN_USER -e POC_SIGNIN_PASSWORD \
  playwright_orchestrator_web python3 -m pytest -p no:cacheprovider -q -rA ${PYTEST_ARGS:-} "/app/poc/$check" 2>&1 \
  | sed "$redact"
code=${PIPESTATUS[0]}
echo "== $check: exit $code after $(( $(date +%s) - start ))s"
exit "$code"
