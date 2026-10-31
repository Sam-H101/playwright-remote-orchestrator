# playwright-remote-orchestrator

## Purpose

* This was built as a method to run multiple tests against any number of servers. It will pick a random server and port and run the checks against them. It was built as playwright currently has a limitation where one can only run checks on the same box as where the code lives. This allows the checks to be put into docker containers and run centeralized while the actual orchestrators are ran in a number of places. 

## Limitations

* Currently it does not know if the remote nodes are up or down. Future state it will use the failure counters + a pinger to mark them up / down.
* queue.db needs to be shared along with the src files between the different containers. this is because it is using sqlite databases to determine which ports and hosts are free. 


## Building 

1. Run docker-compose build


## Running the checks with local server

1. uncomment the CMD line in `docker/python/Dockerfile` (the image ships the Playwright server, no npm install needed)
2. Build the image and it should start if you do docker-compose up
* Note this has not been tested

## Running a remote server

The orchestrator image uses Playwright **1.64.1**. Playwright only accepts connections between a client and server on the same minor version, so every remote node must run the same version (see [here][l1] for install docs).

1. Install Node.js, then install the matching Playwright version and its browsers on the remote executioner host: `npm install playwright@1.64.1 && npx playwright install --with-deps`
2. start the server by running `npx playwright run-server --port <port number> --host 0.0.0.0`
   * Drop the old `--reuse-browser false` flag; current Playwright releases no longer accept it.

### Upgrading Playwright

Bump the version in all three places together: `PLAYWRIGHT_VERSION` in `docker/python/Dockerfile`, `playwright==` in `docker/python/requirements.txt`, and the `run-server` install on every remote node.

## Configuration

1. modify `src/database.yaml` with the servername, start port, and end port
2. append `--fr true` to the docker-compose file and run it once. afterwords remove the `--fr true`. This is used to initialize the database the orchestrator uses to determine if ports are open or not.

## Creating a check

* look at `full/example/example.py` for reference. If you need the logs, first make the log directory then add it to the docker-compose volumes config `- ./logs/example_playwright/:/app/logs/`. 
* This check scrapes docusign and looks to see the latest status on the Docusign Nodes. 







[l1]: https://playwright.dev/docs/intro
