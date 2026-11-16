# Kubernetes and AKS

The repository ships no Kubernetes manifests or Helm chart. The published images (see [Releases and images](../development/releases.md)) run on a cluster as they do under compose; this page collects what the code and the rest of the documentation say about running them there.

## What each image needs

| Image | Runs as | Needs |
| --- | --- | --- |
| `hub` | user `node`, listens on 4500 (`HUB_PORT`) | MySQL (`DB_*`), the bootstrap tokens if you use them, and a Service runner hosts and checks can reach. It is the only component runner hosts connect to |
| `runner` | root at start, browsers as `pwuser1`..`pwuserN` | The capabilities compose gives it (`SETUID`, `SETGID`, `KILL`, plus `NET_ADMIN` and `SETPCAP` while `RUNNER_BLOCKED_NETWORKS` is set), a larger `/dev/shm` than 64 MB for Chromium, and outbound access to the hub. No Service: it dials out |
| `grid-ui` | user `grid`, gunicorn on 4444 with 2 workers | MySQL (`DB_*`), `GRID_SECRETS_KEY`, `GRID_ADMIN_TOKEN` or single sign-on |
| `checks` | the checks' image `main.py` runs in | The hub's address in `GATEWAY_URL`, a client token, and the Grid UI's address in `GRID_UI_URL` for MFA codes or saved sign-ins |

The compose settings for each service in `docker-compose.yaml` are the reference for their environment; every variable is in [Configuration](../reference/configuration.md).

## Network isolation

A check controls a browser on a runner, and that browser can reach any address the runner can. Compose puts the runners on a network of their own with the hub and nothing else. On Kubernetes, do the same with a NetworkPolicy: let the runner pods reach the hub and the sites your checks test, and nothing else (in particular not MySQL, the Grid UI, the cluster's API server or the node's metadata endpoint). The runner image can also close `RUNNER_BLOCKED_NETWORKS` to the browsers' users itself (see [Security](../security.md#network)).

## Grid UI single sign-on on AKS without a client secret

On AKS the Grid UI can sign people in through Entra ID with workload identity federation, so no client secret exists anywhere. The steps (OIDC issuer, federated credential, service account, pod label, `GRID_OIDC_CLIENT_AUTH=federated`) are in [Single sign-on without a client secret: on AKS](../sso/secretless.md#on-aks). Points that matter for the pod spec:

- The pod template needs the label `azure.workload.identity/use: "true"`, and the service account the annotation `azure.workload.identity/client-id`. Labels and annotations are read when a pod is created: after changing them, delete the pod so a new one starts.
- The image runs as the user `grid`, not root, and that user must be able to read the token file (the pod's `securityContext`).
- Never set `GRID_OIDC_MANAGED_IDENTITY` in a pod: the Grid UI refuses it in any pod, because the managed identity endpoint a pod can reach is the node's.
- Never point `GRID_OIDC_FEDERATED_TOKEN_FILE` at the pod's Kubernetes API token.

## Logs

The Grid UI writes its log to stderr, where `kubectl logs` or a cluster's log collector reads it; `GRID_UI_LOG_FORMAT=json` writes one JSON object per line. See [Grid UI: the log](../grid-ui/index.md#the-log).

## Reverse proxies and ingress

- The hub speaks plain `ws://`. Across any network you don't trust, put a TLS-terminating proxy in front of it, allow WebSocket upgrades and long-lived connections (the agent reconnects if it hears nothing for 45 s), and use `wss://` URLs for the agents and `GATEWAY_URL`.
- Behind an ingress, set `GRID_UI_PROXY_HOPS` for the Grid UI so the sign-in limits and the audit log see people's addresses, set `GRID_UI_PUBLIC_URL`, and have the proxy pass the `Host` header on.
