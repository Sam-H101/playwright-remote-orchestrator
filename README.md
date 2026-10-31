# playwright-remote-orchestrator

## Purpose

* This was built as a method to run multiple tests against any number of servers. It will pick a random server and port and run the checks against them. It was built as playwright currently has a limitation where one can only run checks on the same box as where the code lives. This allows the checks to be put into docker containers and run centeralized while the actual orchestrators are ran in a number of places. 

## Limitations

* Currently it does not know if the remote nodes are up or down. Future state it will use the failure counters + a pinger to mark them up / down.
* queue.db needs to be shared along with the src files between the different containers. this is because it is using sqlite databases to determine which ports and hosts are free. 


## Building 

1. Run docker-compose build


## Running the checks with local server

1. uncomment the CMD line in `docker/python/Dockerfile`
2. Build the image and it should start if you do docker-compose up
* Note this has not been tested

## Running a remote server

### Windows

`installer/windows/install-backend.ps1` sets up a Windows host as a remote executioner. It needs Node.js 20+ (or pass `-InstallNode` to get it from winget) and an elevated PowerShell.

```powershell
.\installer\windows\install-backend.ps1 -PortStart 3000 -PortCount 4
```

* Installs `playwright-core` and only the browsers you list (`-Browsers chromium` by default) into `C:\PlaywrightBackend`. Re-running skips anything already at the right version, so upgrades only download what changed.
* `-PlaywrightVersion` must match the major.minor of the Playwright client the checks use.
* Runs one `run-server` per port under a supervisor (scheduled task `PlaywrightBackend`, runs as SYSTEM at startup) that restarts crashed servers, and opens the ports in Windows Firewall.
* Health-checks every port before it exits and reports the result: a console summary with the entry to paste into `src/database.yaml`, `C:\PlaywrightBackend\status.json`, an optional JSON POST to `-ReportUrl`, and the exit code (`0` all ports healthy, `2` some ports unhealthy, `1` failed).
* `C:\PlaywrightBackend\backend-status.ps1 [-Json] [-ReportUrl <url>]` checks the host later with the same exit codes. `uninstall-backend.ps1 [-RemoveFiles]` removes it.
* Logs are in `C:\PlaywrightBackend\logs` (`install.log`, `supervisor.log`, `server-<port>.*.log`).

### Other platforms

1. Install Playwright on the remote executioner host. Can be found [here][l1]
2. Start a server per port with `npx playwright run-server --port <port number> --host 0.0.0.0`

## Configuration

1. modify `src/database.yaml` with the servername, start port, and end port
2. append `--fr true` to the docker-compose file and run it once. afterwords remove the `--fr true`. This is used to initialize the database the orchestrator uses to determine if ports are open or not.

## Creating a check

* look at `full/example/example.py` for reference. If you need the logs, first make the log directory then add it to the docker-compose volumes config `- ./logs/example_playwright/:/app/logs/`. 
* This check scrapes docusign and looks to see the latest status on the Docusign Nodes. 







[l1]: https://playwright.dev/docs/intro
