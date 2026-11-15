#!/bin/sh
# Starts the runner's agent. It runs as root, first: a check controls a browser, and the browser can reach any
# address the container can, so the networks in RUNNER_BLOCKED_NETWORKS (comma-separated, IPv4 or IPv6) are closed
# to the browsers' users (RUN_SERVER_USER) here, with the agent itself still free to dial the hub. Setting them needs
# NET_ADMIN, which is then dropped for good, with SETPCAP, so neither the agent nor a browser can open them again.
set -eu

log() { echo "[entrypoint] $*" >&2; }

# The bounding set as a number: what this process and everything it starts can ever hold.
bounding=$((0x$(awk '/^CapBnd:/ { print $2 }' /proc/self/status)))
NET_ADMIN=$((1 << 12))
SETPCAP=$((1 << 8))

nets=$(printf '%s' "${RUNNER_BLOCKED_NETWORKS:-}" | tr ',' ' ')
if [ -n "$(printf '%s' "$nets" | tr -d ' ')" ]; then
  if [ -z "${RUN_SERVER_USER:-}" ]; then
    log "RUNNER_BLOCKED_NETWORKS needs RUN_SERVER_USER: the networks are closed to the browsers' users only"
    exit 1
  fi
  # The browsers' users: RUN_SERVER_USER, or with {slot} every user it names for slots 1-64 (the most an agent
  # runs) that exists, so a slot count given on the command line can't get past the rules.
  case "$RUN_SERVER_USER" in
    *'{slot}'*)
      users=''
      i=1
      while [ "$i" -le 64 ]; do
        user=$(printf '%s' "$RUN_SERVER_USER" | sed "s/{slot}/$i/g")
        if id -u "$user" >/dev/null 2>&1; then users="$users $user"; fi
        i=$((i + 1))
      done
      if [ -z "$users" ]; then
        log "RUN_SERVER_USER=$RUN_SERVER_USER names no user that exists"
        exit 1
      fi
      ;;
    *) users=$RUN_SERVER_USER ;;
  esac
  for net in $nets; do
    case "$net" in
      *:*) table=ip6tables ;;
      *) table=iptables ;;
    esac
    for user in $users; do
      if ! "$table" -w -A OUTPUT -m owner --uid-owner "$user" -d "$net" -j REJECT; then
        log "can't close $net to $user: the container needs the NET_ADMIN capability for RUNNER_BLOCKED_NETWORKS" \
          "(docker-compose.yaml adds it), or set RUNNER_BLOCKED_NETWORKS empty"
        exit 1
      fi
    done
  done
  log "browsers ($RUN_SERVER_USER) can't reach $(printf '%s' "$nets" | sed 's/^ *//; s/  */, /g')"
fi

if [ $((bounding & (NET_ADMIN | SETPCAP))) -eq 0 ]; then
  exec "$@"
fi
if [ $((bounding & SETPCAP)) -eq 0 ]; then
  log "the container has NET_ADMIN without SETPCAP, so the agent would keep NET_ADMIN: add SETPCAP or drop NET_ADMIN"
  exit 1
fi
exec setpriv --bounding-set -net_admin,-setpcap --inh-caps -all -- "$@"
