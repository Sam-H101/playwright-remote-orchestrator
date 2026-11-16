# Windows host (pwrunner)

`pwrunner.exe` turns a Windows machine into a runner node. It is one file: it carries a private Node 22 and `playwright-core` (hash-pinned), installs itself as a Windows service, and needs no Node, npm, winget or repository checkout on the host. It is also the agent itself (a drop-in for `gateway/agent.js` against the same hub), its status tool and its uninstaller.

## Install

Download `pwrunner-<version>-windows-amd64.exe` (or `-arm64`) from the [releases](https://github.com/Sam-H101/playwright-remote-orchestrator/releases) and check it against `SHA256SUMS.txt` (the builds are not code-signed yet). Then, in an elevated PowerShell:

```powershell
Get-Content .\agent-token.txt | .\pwrunner.exe install --hub-url ws://<orchestrator-host>:4500 --token-stdin --name win-07 --slots 4
```

The agent token is one made for this host on the Grid UI's Access page (see [Access tokens](../concepts/access-tokens.md)), or `GATEWAY_AGENT_TOKEN`. `--token-file <path>` works too; `--token <value>` is accepted but puts the token on a command line.

- The host only needs outbound access to the hub's port. No firewall rule, no inbound port and no `database.yaml` entry: the node registers itself as `--name` (default: the host name).
- Installs into `C:\PlaywrightBackend` (`--install-dir`): `pwrunner.exe`, the private Node in `runtime\`, `playwright-core` in `app\`, and only the browsers you list (`--browsers chromium` by default) in `browsers\`. Keep the folder short: Windows can't start a browser whose path is over 260 characters.
- `--offline-dir <dir>` installs from the release's `pwrunner-runtime-<version>-windows-amd64.zip` contents without downloading; `--browsers-from <dir>` copies browsers instead of downloading them.
- Runs the agent as the Windows service `PlaywrightBackend` (`--service-name`), LocalSystem, automatic start, restarted by the service manager if it exits. `--no-service` runs it in the background for the current session instead and needs no elevation.
- Elevated, the install folder is writable only by SYSTEM and Administrators. A folder other users could change (for example from the older installer) is reset and gets a fresh `app\` and `browsers\`. The token is stored in `agent.token`, readable only by SYSTEM and Administrators, and never appears on a command line, in a log or in a JSON file.
- Waits for the node to register and reports the result: a console summary, `status.json`, an optional JSON POST to `--report-url`, and the exit code (`0` registered and every slot healthy, `2` not registered or some slots unhealthy, `1` failed).

All install flags are in [Command line](../reference/cli.md#pwrunner-install).

## Browser accounts {#browser-accounts}

The browsers don't run as LocalSystem. The install creates a local account for each slot's browsers, `pwb-<id>-1`, `pwb-<id>-2` and so on (`<id>` is 8 hex digits derived from the service name), and the service starts each slot's run-server and browsers as that slot's account. Checks running at the same time therefore can't read each other's browser profiles, and no check can read `agent.token`.

Each account is:

- an ordinary member of Users only, whose password never expires and can't be changed by the user,
- hidden from the sign-in screen,
- allowed to log on as a batch job, and denied local, Remote Desktop and network logons.

Each account's 40-character random password is replaced at every install and kept only in LSA private data (`L$pwrunner-<account>`), which SYSTEM and Administrators can read; it is never logged or written to a file. The accounts are listed, slot 1 first, as `browserAccounts` in `backend.json`.

At start the service signs each slot's account in (a batch logon), loads its profile, gives it access to the service's window station and desktop and starts that slot's run-server with its token, so the browsers read what Users can: `app\`, `runtime\`, `browsers\`, the logs and the JSON files, but not `agent.token`, and not the other slots' profiles.

- Changing the slot count means running `install` again, which adds or removes the accounts.
- A slot without its account (or its stored password) doesn't start. The service does not fall back to LocalSystem or to another slot's account: the node registers with the slot unhealthy, and `logs\service.log`, `logs\agent.log` and the Event Log say to run `pwrunner install` again.
- Installs from before took one account, `pwbrowser-<id>`, for every slot (an older `backend.json` names it `browserAccount`); `install` removes it.
- `pwrunner run` in a console, and `install --no-service`, run the browsers as the console's user and warn about it.

## Upgrade

- Re-running `install` keeps the installed settings (hub, token, node name, slots, base port, browsers, report URL, service name) unless you pass them again, and skips the runtime when it is already at the pinned version.
- `C:\PlaywrightBackend\pwrunner.exe install` with no flags re-applies the install.
- `--playwright-version` is not kept, so a newer exe upgrades.
- If an upgrade fails, `app\` and `runtime\` are rolled back and the previous backend is restarted.

!!! warning "Upgrade with `install`, not by swapping the exe"
    Upgrade by running the new exe's `install`, not by swapping `pwrunner.exe`: the install sets up the browsers' accounts, and a service without them starts no browsers (its slots show unhealthy and `service.log` says to run `pwrunner install` again).

To move Playwright without a new exe, pass `--playwright-version <new> --playwright-integrity <sha512-...>` (the `integrity` in `runner/playwright/package-lock.json`) to the existing exe. See [Upgrading Playwright](../operations/upgrading-playwright.md).

## After the install {#after-the-install}

| Command | Does |
| --- | --- |
| `pwrunner status [--json] [--report-url <url>] [--health-timeout <sec>]` | Node health with the same exit codes (`1` = not installed) |
| `pwrunner start` / `stop` / `restart` | Controls the service |
| `pwrunner token set --stdin` / `set --file <path>` / `clear` / `show-prefix` | Rotates the token and restarts the service; `show-prefix` prints only a managed token's 12-character label |
| `pwrunner selfcheck [--quick]` | Checks each slot's browser account, the folder and token permissions, the runtime, the slot ports, the hub and the Node end-of-life date |
| `pwrunner uninstall [--remove-files] [--yes]` | Removes the service and the browsers' accounts with their profiles; `--remove-files` also deletes the folder, browsers included |
| `pwrunner version` | Prints the version, the Go toolchain and the pinned runtime |

## Uninstall

`pwrunner uninstall` removes the service and every browser account (the old single account too), with their profiles, rights and stored passwords. `--remove-files` also deletes the install folder, browsers included; `--yes` skips the confirmation.

## Logs

Logs are in `C:\PlaywrightBackend\logs`: `install.log`, `service.log`, `agent.log` (rotated at 10 MiB) and `slot-<i>.log`. The service also writes start, stop and repeated-crash events to the Windows Event Log under `PlaywrightBackend`.

## Settings the service reads

The Windows service reads only `backend.json` and `agent.token` in the install folder, never the agent's environment variables. Its run-servers get an allow-listed environment (system paths, `TEMP`, proxy variables, `PLAYWRIGHT_BROWSERS_PATH`, `PLAYWRIGHT_DOWNLOAD_HOST`), never the agent token or a machine-wide `NODE_OPTIONS`.

## Migrating from the PowerShell installer

An install made by the older PowerShell installer is migrated in place: its scheduled task and supervisor are removed and its scripts are moved to `C:\PlaywrightBackend\legacy\`. See also [Migrating from the per-port setup](../operations/migrating.md).

To go back to the PowerShell installer, run `pwrunner uninstall`, then `installer\windows\install-backend.ps1` from a checkout as before (see `installer/windows/DEPRECATED.md`).
