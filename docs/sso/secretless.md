# Single sign-on without a client secret

The Grid UI can sign people in through Entra ID (or another provider that takes the same kind of credential) with no client secret anywhere: not in a Kubernetes Secret, not in a key vault, not in the app registration. The platform the Grid UI runs on gives it a short-lived token that says who it is; the Grid UI presents that token when it redeems a sign-in's code, and Entra accepts it because the app registration says which token to trust (a federated identity credential: an issuer, a subject and an audience). Microsoft calls this workload identity federation.

The platform's token comes from one of:

- **a file** the platform keeps fresh: on [AKS](#on-aks), on any other Kubernetes cluster, or one you refresh yourself from any issuer the provider trusts ([any other platform](#token-file)), a provider on `127.0.0.1` among them ([local development](#local-development));
- **an Azure managed identity**, on App Service, Functions, Container Apps or an Azure VM ([a managed identity](#managed-identity)).

In every case `GRID_OIDC_CLIENT_AUTH=federated` is required, and no client secret may be set. Nothing turns this on by itself: not a pod label, not a managed identity on the VM, not the variables the platform sets. The Grid UI sends the platform's token only when this setting says so, so that a token meant for Entra is never handed to whatever provider the Grid UI happens to be pointed at. Exactly one source is used: with more than one set, nobody signs in with single sign-on until all but one are removed, and the Grid UI never falls back from one to another.

!!! warning "Not yet tried against a real tenant"
    The Grid UI's side is tested against fakes only: a fake cluster, a fake managed identity endpoint and a fake identity provider. Microsoft documents a platform's token, and a managed identity's, as the client credential for the client credentials grant, and a client credential in place of a secret when a code is redeemed, but no page shows the two together for a sign-in, and that hasn't been tried against Entra. Nor have the address App Service, Functions and Container Apps give their managed identity endpoint, or what a managed identity's token says about itself (its issuer, subject and audience). So on the first cluster, app or VM, try it with a throwaway app registration before moving a grid that people use. If Entra refuses the token when a code is redeemed (an `invalid_client`, or an error about the assertion, while the same federated credential works for the client credentials grant), this version has no other way to sign in without a client secret: keep using one.

## On AKS {#on-aks}

You need AKS 1.22 or later, Azure CLI 2.47.0 or later, and the right to change the app registration.

1. **Turn on the cluster's OIDC issuer and workload identity**, and note the issuer URL:

    ```sh
    az aks update -g <resource group> -n <cluster> --enable-oidc-issuer --enable-workload-identity
    az aks show -g <resource group> -n <cluster> --query oidcIssuerProfile.issuerUrl -o tsv
    # https://westeurope.oic.prod-aks.azure.com/11111111-2222-3333-4444-555555555555/66666666-7777-8888-9999-000000000000/
    ```

    Keep it exactly as printed, trailing `/` included.

2. **Register the Grid UI in Entra ID** as in the [Entra recipe](index.md#entra-id) (a **Web** redirect URI `<GRID_UI_PUBLIC_URL>auth/callback`, the groups claim, **Assignment required**), and add **no** client secret.

3. **Add the federated credential** to that app registration: under **Certificates & secrets**, **Federated credentials**, **Add credential**, scenario **Kubernetes accessing Azure resources**; or:

    ```sh
    cat > credential.json <<'EOF'
    {
      "name": "grid-ui-on-aks",
      "issuer": "https://westeurope.oic.prod-aks.azure.com/11111111-2222-3333-4444-555555555555/66666666-7777-8888-9999-000000000000/",
      "subject": "system:serviceaccount:grid:grid-ui",
      "description": "The Grid UI's pod in the namespace grid",
      "audiences": ["api://AzureADTokenExchange"]
    }
    EOF
    az ad app federated-credential create --id <application (client) id> --parameters credential.json
    ```

    - `issuer` is the URL from step 1, character for character.
    - `subject` is `system:serviceaccount:<namespace>:<service account name>`.
    - The audience is `api://AzureADTokenExchange` (`api://AzureADTokenExchangeUSGov` in Azure Government, `api://AzureADTokenExchangeChina` in Azure operated by 21Vianet; neither cloud has been tried).

    Entra compares all three exactly and case-sensitively, and saves a credential with a typo without complaint: the mistake only shows when someone signs in.

4. **Give the Grid UI a service account, label its pod, and say `federated`:**

    ```yaml
    apiVersion: v1
    kind: ServiceAccount
    metadata:
      name: grid-ui
      namespace: grid
      annotations:
        azure.workload.identity/client-id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"   # the application (client) id
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: grid-ui
      namespace: grid
    spec:
      selector:
        matchLabels: { app: grid-ui }
      template:
        metadata:
          labels:
            app: grid-ui
            azure.workload.identity/use: "true"      # without this label the pod gets no token
        spec:
          serviceAccountName: grid-ui
          containers:
            - name: grid-ui
              image: <the Grid UI image>
              env:
                - { name: GRID_OIDC_ISSUER, value: "https://login.microsoftonline.com/ffffffff-0000-1111-2222-333333333333/v2.0" }
                - { name: GRID_OIDC_CLIENT_ID, value: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" }
                - { name: GRID_OIDC_CLIENT_AUTH, value: "federated" }      # required: the token is never used without it
                - { name: GRID_UI_PUBLIC_URL, value: "https://grid.example.com/" }
                - { name: GRID_OIDC_ADMINS, value: "sub:<your subject>" }
                # ... the rest of the Grid UI's settings, as in docker-compose.yaml. No GRID_OIDC_CLIENT_SECRET.
    ```

    The label makes AKS mount the token and set `AZURE_FEDERATED_TOKEN_FILE` in the pod, which the Grid UI reads for the path: there is no path to configure.

    - **`GRID_OIDC_CLIENT_AUTH=federated` is required.** The label alone turns nothing on, and `auto` never chooses the platform's token: the Grid UI sends it only when this setting says `federated`, so that a pod labelled for another reason never hands that token to whatever provider it is pointed at.
    - The Grid UI takes the issuer and the client id from its own settings only; the `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_AUTHORITY_HOST` that AKS also sets are not used.
    - Labels and annotations are read when a pod is created: after changing them, delete the pod so a new one starts.
    - The image runs as the user `grid`, not root, and that user must be able to read the token file.
    - Don't set `GRID_OIDC_MANAGED_IDENTITY` in a pod: the Grid UI refuses it in any pod, because the managed identity endpoint a pod can reach is the node's.

5. **Check it.** Sign in with the admin token (or as someone who may see people and roles) and open **Admin**, **Single sign-on**. "Client authentication" reads "A token the platform issues (federated): no client secret", and below it are the token's file, issuer, subject, audience and expiry. Compare issuer, subject and audience with the federated credential of step 3: they must be the same, letter for letter. Then sign in with single sign-on. A federated credential can take a few minutes to take effect after it is added.

### Another token file {#another-token-file}

`GRID_OIDC_FEDERATED_TOKEN_FILE` names the file when it isn't the one AKS announces: a pod that uses an AKS identity binding gets a token for the audience `api://AKSIdentityBinding`, which an app registration's federated credential doesn't take. Project a second service-account token for the right audience and point the setting at it:

```yaml
        volumes:
          - name: entra-token
            projected:
              sources:
                - serviceAccountToken: { audience: "api://AzureADTokenExchange", expirationSeconds: 3600, path: token }
        # and in the container:
        #   volumeMounts: [{ name: entra-token, mountPath: /var/run/secrets/grid-oidc, readOnly: true }]
        #   env: GRID_OIDC_FEDERATED_TOKEN_FILE=/var/run/secrets/grid-oidc/token
```

!!! danger "Never point it at the pod's Kubernetes API token"
    Never use `/var/run/secrets/kubernetes.io/serviceaccount/token`. That token is for the cluster's own API server; sent to an identity provider it gives someone outside the cluster a credential for the cluster, and Entra refuses it anyway because its audience is wrong. The file must hold a token projected for the identity provider's audience, as above. "Token audience" on the Single sign-on tab shows which one the Grid UI holds. The Grid UI doesn't check a token file's audience itself: with `federated` it sends the token to whichever provider `GRID_OIDC_ISSUER` names.

## App Service, Functions, Container Apps or an Azure VM: a managed identity {#managed-identity}

The Grid UI asks the platform for its managed identity's token each time someone signs in, and Entra takes it because the app registration trusts that managed identity.

### Where this is safe

A managed identity's token can be had by anything that can ask the platform's endpoint:

- On **App Service, Functions and Container Apps** the endpoint's key (`IDENTITY_HEADER`) is given to the app's own containers only. These are the recommended places.
- On an **Azure VM**, every program and container on the VM can get the VM's identities' tokens; Microsoft: "All code/scripts running on a virtual machine can request and retrieve tokens for any managed identities available on it." With the compose stack that includes the grid's runners, whose browsers any grid user drives, and `example_playwright`. Whoever gets the token can sign in to Entra as the Grid UI's app registration until it expires, which can be many hours: Azure keeps a managed identity's token for around 24 hours, and it can't be made to give out a fresh one sooner.

So on a VM, use a managed identity only when the Grid UI is the only thing that can reach `169.254.169.254`: either nothing else runs on the VM, or every other container is blocked from that address (below). A program running on the VM itself, or a container with `network_mode: host`, can't be blocked that way; if there is one, use a secret or a token file instead. Assign the identity to the Grid UI's VM only, never to a runner's. The compose runners keep their browsers off that address by default (`RUNNER_BLOCKED_NETWORKS`), but not the rest of the runner, `example_playwright` or your other containers, so the rule below is still needed.

### Blocking the other containers (compose on a VM) {#blocking-imds}

Give `grid_ui` a fixed address with an override file next to `docker-compose.yaml`:

```yaml
# docker-compose.identity.yaml: grid_ui at a fixed address, so a firewall rule can tell it apart.
services:
  grid_ui:
    networks:
      playwright:
        ipv4_address: 172.50.221.250
```

Start the stack with `-f docker-compose.yaml -f docker-compose.identity.yaml` (and `-f docker-compose.mysql.yaml` if you use it), and drop every other container's traffic to the Instance Metadata Service:

```sh
sudo iptables -I DOCKER-USER -d 169.254.169.254 ! -s 172.50.221.250 -j DROP
```

The rule covers every Docker network on the VM, and lets nothing through but the one address (Docker gives other containers addresses from the bottom of the subnet, so `.250` stays free). It needs Docker's iptables firewall, the default. The rule isn't kept across a reboot: keep it with your distribution's tool (`iptables-persistent`, a systemd unit), and add it before the stack starts. Check it from a runner (the address answers instance metadata, which is no credential; `blocked` is what you want to see):

```sh
docker compose exec runner node -e "fetch('http://169.254.169.254/metadata/instance?api-version=2021-02-01',{headers:{Metadata:'true'},signal:AbortSignal.timeout(3000)}).then(r=>console.log(r.status),()=>console.log('blocked'))"
```

### Steps

1. **Give the app or VM a user-assigned managed identity**, and note its client id, principal id and resource id:

    ```sh
    az identity create -g <resource group> -n grid-ui
    az identity show -g <resource group> -n grid-ui --query "{clientId: clientId, principalId: principalId, id: id}" -o json
    # then one of:
    az vm identity assign -g <resource group> -n <vm> --identities <the identity's resource id>
    az webapp identity assign -g <resource group> -n <app> --identities <the identity's resource id>
    az containerapp identity assign -g <resource group> -n <app> --user-assigned <the identity's resource id>
    ```

    `GRID_OIDC_MANAGED_IDENTITY=system` asks for the endpoint's default identity instead: the system-assigned one if it is turned on (on a VM with no system-assigned identity and one user-assigned one, that one). The Single sign-on tab's "Credential subject" row shows which identity answered. A system-assigned identity may work as the credential, but Microsoft's page on trusting a managed identity lists user-assigned identities only, and it hasn't been tried. The identity and the app registration must be in the same tenant.

2. **Register the Grid UI in Entra ID** as in the [Entra recipe](index.md#entra-id), and add **no** client secret.

3. **Add the federated credential** to that app registration: under **Certificates & secrets**, **Federated credentials**, **Add credential**, scenario **Managed Identity**, and select the identity; or:

    ```sh
    cat > credential.json <<'EOF'
    {
      "name": "grid-ui-managed-identity",
      "issuer": "https://login.microsoftonline.com/ffffffff-0000-1111-2222-333333333333/v2.0",
      "subject": "<the identity's principal id>",
      "description": "The Grid UI's managed identity",
      "audiences": ["api://AzureADTokenExchange"]
    }
    EOF
    az ad app federated-credential create --id <application (client) id> --parameters credential.json
    ```

    - `issuer` is your tenant's `https://login.microsoftonline.com/<tenant id>/v2.0`, the same as `GRID_OIDC_ISSUER`.
    - `subject` is the identity's **principal (object) id**, not its client id.
    - The audience is always `api://AzureADTokenExchange` (`api://AzureADTokenExchangeUSGov` in Azure Government, `api://AzureADTokenExchangeChina` in Azure operated by 21Vianet; neither cloud has been tried), whatever audience the token itself states on the Single sign-on tab. The Grid UI asks for the audience of the cloud `GRID_OIDC_ISSUER` is in.

4. **Set** `GRID_OIDC_CLIENT_AUTH=federated` and `GRID_OIDC_MANAGED_IDENTITY=<the identity's client id>`, with `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_UI_PUBLIC_URL` and `GRID_OIDC_ADMINS` as usual and no `GRID_OIDC_CLIENT_SECRET`: as app settings on App Service and Functions, as environment variables on Container Apps, in `.env` for compose on a VM. There is no address to set.

    - On App Service, Functions and Container Apps the platform sets `IDENTITY_ENDPOINT` and `IDENTITY_HEADER` and the Grid UI uses them (restart the app after turning its identity on, or they aren't there and the Grid UI says so); on a VM it asks the Instance Metadata Service at `http://169.254.169.254`, which a compose container reaches through the default network.
    - It asks over plain http at the local address the platform set (`localhost`, or a loopback, link-local or private IP address, never a public one), through no proxy whatever `HTTP_PROXY` says, and follows no redirect.
    - `GRID_OIDC_MANAGED_IDENTITY` needs an Entra ID issuer over https: the Grid UI never sends a managed identity's token anywhere else, and only to the token endpoint on the issuer's own host. `GRID_OIDC_ALLOW_HTTP` changes none of this.

5. **Check it** as in step 5 on AKS: the Single sign-on tab shows "Managed identity", "Identity endpoint", the three "Credential" rows (the federated credential to add: compare them with step 3), and the token's issuer, subject and audience as the token states them. The token's subject should be the principal id; its issuer and audience may be written differently from the credential's, which is expected. Then sign in with single sign-on.

Not supported: an Azure Arc-enabled server's identity, Service Fabric's, and the older `MSI_ENDPOINT` (the Grid UI says so by name), and a Kubernetes pod (use the token file; the Grid UI refuses a managed identity in a pod).

## Any other platform: a token file {#token-file}

`GRID_OIDC_FEDERATED_TOKEN_FILE` takes a file holding a JWT from any issuer the identity provider trusts. The Grid UI reads it at every sign-in, refuses it once it has expired, and keeps it nowhere, so whatever writes the file must replace it before it expires.

- **Another Kubernetes cluster** (self-managed, EKS, GKE, ...): with the Azure workload identity webhook installed, as on AKS; or without it, a projected service-account token for the audience `api://AzureADTokenExchange` (the volume under [Another token file](#another-token-file) above) and `GRID_OIDC_FEDERATED_TOKEN_FILE` pointing at it. The federated credential's issuer is the cluster's service account issuer, its subject `system:serviceaccount:<namespace>:<service account>`. Entra fetches the issuer's discovery document and keys itself, so they must be reachable from the internet, and the token must be signed with RS256.
- **A file you refresh yourself** (a sidecar or a timer that writes a token from your own issuer): the same, with the issuer and subject that issuer puts in its tokens.

The Single sign-on tab shows the token's issuer, subject and audience: the federated credential must name those three exactly.

## Local development {#local-development}

- **The Grid UI on this machine, with Entra** (`GRID_UI_PUBLIC_URL=http://127.0.0.1:<port>/` or `http://localhost:<port>/`): allowed without any setting and without a warning, because only the browser's hop to the Grid UI is plain http, and it stays on this machine. The issuer is Entra's usual https one; the token comes from a file, or from a managed identity when this machine is an Azure VM or app that has one. Add the redirect URI `http://localhost:<port>/auth/callback` (or the `127.0.0.1` form) to the app registration; Entra takes plain http in a redirect URI only for a loopback address, and its portal may only take `localhost`. The registered URI's host must be the one in `GRID_UI_PUBLIC_URL` (`localhost` with `localhost`, `127.0.0.1` with `127.0.0.1`), so when the portal only takes `localhost`, set `GRID_UI_PUBLIC_URL=http://localhost:<port>/`. The Single sign-on tab shows the redirect URI the Grid UI sends.
- **A provider on this machine** (`127.0.0.1`, `localhost`, `[::1]`): plain http is allowed without any setting, and goes straight to it, never through `HTTP_PROXY`. Point `GRID_OIDC_FEDERATED_TOKEN_FILE` at a token from an issuer that provider trusts. "This machine" is where the Grid UI's own process runs. In compose that is the `grid_ui` container, so a provider on the host isn't at `127.0.0.1` for it: give the provider a name both the browser and `grid_ui` reach (an `extra_hosts` entry such as `host.docker.internal:host-gateway`, or a network alias), which isn't loopback and so needs `GRID_OIDC_ALLOW_HTTP=1` (next bullet), or run the Grid UI outside compose.
- **A provider elsewhere over http** (a Keycloak at `http://keycloak:8080` in compose): set `GRID_OIDC_ALLOW_HTTP=1`. The Grid UI then sends the token over plain http, says so in its log and on the Single sign-on tab, and anyone on the network between them can read the token and use it as the Grid UI's credential until it expires, including the other containers on the same Docker network (the runners among them). Use a token from an issuer nothing else trusts, and never leave this setting on outside development. A managed identity's token never goes over plain http, with or without the setting.
- Entra ID trusts only an issuer it can reach, so with Entra even development needs a token from a public issuer (a cluster's, a managed identity's).
- The Grid UI sends the token as a JWT client assertion (RFC 7523), which a provider's discovery document calls `private_key_jwt`. A provider other than Entra must take such an assertion from your token's issuer; how each one is set up for that isn't covered here and hasn't been tried.

## What is and isn't kept

The Grid UI reads the token from its file, or asks the managed identity endpoint for it, each time someone signs in (a file also when it checks the settings; the endpoint also at the start of a sign-in and when the Single sign-on tab is opened, at most once a minute) and keeps it nowhere: not in memory between sign-ins, not in the database, not in a log or a page.

- For a minute it remembers whether the endpoint gave a token, and that token's issuer, subject, audience and expiry, never the token.
- It sends the token only to the token endpoint that `GRID_OIDC_ISSUER`'s discovery document names, over https (plain http only to a loopback host, or anywhere with `GRID_OIDC_ALLOW_HTTP=1`, which it warns about; plain http never through a proxy).
- A managed identity's token goes only to Entra ID over https, on the issuer's own host.
- The Single sign-on tab shows the token's issuer, subject, audience and expiry, and for a managed identity its client id and endpoint, which are names, not credentials, to everyone who may see people and roles ("See people and roles"), not only to admins. `IDENTITY_HEADER` is never shown or logged.

## Moving an existing grid from a client secret

1. Update the Grid UI to this version first, still with the secret.
2. Give it its token and add the federated credential, keeping the secret: label the pod (the tab then says the platform offers a token), or assign the managed identity (on a VM, after the [firewall rule](#blocking-imds)).
3. Then, in one deployment, remove the secret and set `GRID_OIDC_CLIENT_AUTH=federated` (and `GRID_OIDC_MANAGED_IDENTITY`); sign in.
4. Only then delete the secret in Entra.

A Grid UI older than this one can't sign anyone in without a secret, so a rollback to one after the secret is gone, or an update that removes the secret while older pods still run, fails sign-ins until only this version runs.

When it doesn't work, see [Troubleshooting](troubleshooting.md).
