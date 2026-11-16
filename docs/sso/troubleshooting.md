# Single sign-on troubleshooting

When signing in fails, the sign-in sheet says single sign-on isn't set up correctly, that the Grid UI couldn't confirm who you are, or (while the managed identity endpoint can't give a token for a while) that something can't be reached right now. What is wrong with the settings, the token file or the managed identity endpoint is on the Admin page's **Single sign-on** tab and in the Grid UI's log; what Entra refused is in the log only (see [the log](../grid-ui/index.md#the-log)). The Grid UI never falls back to another method.

A single sign-on setting that is wrong (missing, out of range, a CA file that can't be read) stops people signing in with single sign-on until it is fixed, while grid actions stay protected.

## Settings messages

| The Single sign-on tab or the log says | Do this |
|---|---|
| `Set GRID_OIDC_CLIENT_ID to the Grid UI's client id at the identity provider.` | Set `GRID_OIDC_CLIENT_ID` |
| `Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, ...` | `GRID_OIDC_CLIENT_AUTH=federated` is missing (or the secret is). If the tab also says "The platform offers a token", the pod is ready and that setting is all that is left |
| `GRID_OIDC_CLIENT_AUTH=<basic or post> needs GRID_OIDC_CLIENT_SECRET.` | Set the secret, or use `federated` |
| `GRID_OIDC_CLIENT_AUTH is auto, basic, post or federated.` | Use one of those values |
| `GRID_OIDC_CLIENT_AUTH=federated needs a token to send: ...` | No source: set `GRID_OIDC_MANAGED_IDENTITY` or `GRID_OIDC_FEDERATED_TOKEN_FILE`. On Kubernetes, the pod has no token: the label `azure.workload.identity/use: "true"` is missing from the pod template (not the Deployment's own labels), workload identity isn't enabled on the cluster, or the pod predates the label. Delete the pod |
| `Set only one of GRID_OIDC_CLIENT_SECRET, GRID_OIDC_FEDERATED_TOKEN_FILE and GRID_OIDC_MANAGED_IDENTITY: ...` or `GRID_OIDC_CLIENT_AUTH=federated doesn't go with GRID_OIDC_CLIENT_SECRET, ...` | Remove all but one: when moving from a client secret, remove the secret |
| `... is used only with GRID_OIDC_CLIENT_AUTH=federated: ...` | Set `GRID_OIDC_CLIENT_AUTH=federated`, or unset the setting named |
| `GRID_OIDC_MANAGED_IDENTITY is system, or the client id ...` | Use the identity's client id (a GUID), not its name or principal id |
| `GRID_OIDC_MANAGED_IDENTITY doesn't go with the AZURE_FEDERATED_TOKEN_FILE ...` | This is a Kubernetes pod: unset `GRID_OIDC_MANAGED_IDENTITY` and use the pod's token |
| `GRID_OIDC_MANAGED_IDENTITY needs an Entra ID GRID_OIDC_ISSUER ...` | A managed identity works with Entra ID only, over https |
| `GRID_OIDC_MANAGED_IDENTITY can't be used: ` followed by `... Service Fabric ...`, `... Azure Arc ...`, `MSI_ENDPOINT or MSI_SECRET is set ...`, `IDENTITY_ENDPOINT is set without the IDENTITY_HEADER ...` or `IDENTITY_ENDPOINT is <address>, and the Grid UI asks ... only over plain http at a local address ...` | This platform's managed identity isn't supported, or its endpoint isn't at a local address or has a query: use a token file or a secret. If the address is your platform's real one, open an issue with it |
| `... IDENTITY_ENDPOINT isn't an address the Grid UI can read ...` | Something changed the variable the platform set (an unclosed `[`, a port that isn't a number up to 65535): leave it as the platform sets it |
| `... IDENTITY_HEADER isn't a usable header value` | Something changed the variable the platform set (a space or line break added): leave it as the platform sets it |
| `... this is App Service, Functions or Container Apps, but IDENTITY_ENDPOINT isn't set ...` | Turn the app's managed identity on, then restart the app |
| `... this is a Kubernetes pod ...` | Unset `GRID_OIDC_MANAGED_IDENTITY` and use the pod's token file |
| `... the Azure Instance Metadata Service (http://169.254.169.254) didn't answer within 5 s` (or `refused the connection`, or `can't be reached`; on App Service, Functions and Container Apps it names `the managed identity endpoint in IDENTITY_ENDPOINT (...)` instead) | Not an Azure VM, or something blocks `169.254.169.254` (a firewall, the [`DOCKER-USER` rule](secretless.md#blocking-imds) without `grid_ui`'s address, a container network without a route). On App Service, Functions or Container Apps, restart the app. For a minute after, a sign-in is turned away at once without asking again, and the sign-in sheet says to try again in a minute |
| `... answered HTTP 429 ...`, or another `... try again in a minute ...` | Azure is busy or updating the endpoint: try again in a minute. If it keeps happening (a 500 in particular), check that the identity is turned on and assigned to this VM or app |
| `... refused to give a token: HTTP 400 ... Check that this VM or app has ...` | The identity with that client id isn't assigned to this VM or app (or no system-assigned identity is on). App Service and Container Apps may need a restart after assigning one. The Grid UI notices the fix within a minute |
| `... answered with a redirect (HTTP ...), which isn't followed`, `... answered more than 65536 bytes`, `... didn't answer with a JSON object` or `... answered without a token ...` | Something other than the platform's endpoint answers at that address (a proxy, or another program on the VM): the endpoint itself answers JSON with an `access_token` |
| `... the token ... gave has already expired; check this machine's clock` | Set this machine's clock right (NTP) |
| `... the discovery document's token_endpoint isn't https on <host>, GRID_OIDC_ISSUER's host ...` | `GRID_OIDC_ISSUER` isn't Entra's own issuer, or something rewrote its discovery document: a managed identity's token goes only to Entra |
| `GRID_OIDC_ISSUER must be an https URL (or set GRID_OIDC_ALLOW_HTTP=1 for development).`, `... the discovery document's token_endpoint isn't an https URL` (or `authorization_endpoint`, `jwks_uri`) or `... token_endpoint is plain http on a host that isn't loopback ...` | Use https; for development only, see [Local development](secretless.md#local-development) |
| `GRID_OIDC_ALLOW_HTTP=1 and GRID_OIDC_CLIENT_AUTH=federated: the platform's token goes to the identity provider over plain http, ...` | A warning, not a fault: sign-ins work. Remove `GRID_OIDC_ALLOW_HTTP` outside development |
| `The identity provider doesn't take client assertions: ...` | The provider's discovery document lists its token endpoint's methods without `private_key_jwt`. Entra lists it: check `GRID_OIDC_ISSUER` |
| `... can't be used: there is no file ...`, `... may not read ...`, `... is empty`, `... holds more than 16384 bytes` or `... doesn't hold a token (three base64url parts)` | The file named isn't a token the Grid UI can read: check the path, and that the user `grid` may read it (on Kubernetes, the pod's `securityContext`) |
| `... can't be used: the token in ... has expired; the platform should have replaced it.` | Whatever writes the file stopped. On Kubernetes the kubelet isn't refreshing the token: delete the pod; if it returns, look at the node |

## Entra error codes in the log

| Log: refused, | Do this |
|---|---|
| `error codes 700211` | The credential's issuer is wrong. For a managed identity it is `GRID_OIDC_ISSUER` ("Credential issuer"); for a token file compare with "Token issuer", trailing `/` included |
| `error codes 700213` | The credential's subject isn't the token's. Compare with "Token subject": for a managed identity its principal id, on Kubernetes namespace and service account name |
| `error codes 700212` | The credential's audience is wrong. It should be the audience for your cloud (`api://AzureADTokenExchange` in the public cloud); for a managed identity that, whatever the token's own audience line says. If "Token audience" reads `api://AKSIdentityBinding`, the pod uses an AKS identity binding: see [Another token file](secretless.md#another-token-file) |
| `error codes 70021` | No credential matches. For a managed identity, compare with the three "Credential" rows; for a token file, with the token's three. One added a few minutes ago may not have spread yet: try again |
| `error codes 70025` | The app registration has no federated credential at all (it went to another application): add the one the "Credential" rows show (managed identity) or the token's three (token file) |
| `error codes 700016` | `GRID_OIDC_CLIENT_ID` isn't an application of the tenant in `GRID_OIDC_ISSUER` |
| `error codes 700024` | The token was outside its valid time: check this machine's clock, and that the platform still gives out fresh tokens |
| `error codes 700238` | The tenant doesn't take tokens from this issuer for federated credentials: ask whoever administers the tenant |

`https://login.microsoftonline.com/error?code=<number>` gives Microsoft's text for any other number. The Grid UI keeps only the number, never Entra's message. Microsoft says its error numbers can change, so treat the hints as a place to start.
