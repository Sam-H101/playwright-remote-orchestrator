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

1. Install playwright server on the remote executioner host. Can be found [here][l1]
2. once installed, start the server by running `node_modules/.bin/playwright.cmd run-server --port <port number> --reuse-browser false`

## Configuration

1. modify `src/database.yaml` with the servername, start port, and end port
2. append `--fr true` to the docker-compose file and run it once. afterwords remove the `--fr true`. This is used to initialize the database the orchestrator uses to determine if ports are open or not.

## Grid UI

A management console in the style of a Selenium Grid dashboard lives in `ui/`. It shows every node (a host from `src/database.yaml`), its slots (ports), live sessions (ports currently in use), failure counts and the port queue, and refreshes every 5 seconds.

* With docker: `docker-compose up grid_ui`, then open http://localhost:4444
* Locally: `pip install -r ui/requirements.txt`, then `python -m ui.app` from the repo root

Pages and endpoints:

* **Overview**: one card per node with status (UP / DEGRADED / DOWN), sessions vs. max concurrency, and a map of every slot. Click a slot to see its details, reset its failure count or force release it.
* **Sessions**: sortable table of running sessions with their `ws://` endpoint, start time and duration.
* `GET /status` (or `/api/status`): the same data as JSON, plus a `ready` flag.

Settings (environment variables):

| Variable | Default | Purpose |
| --- | --- | --- |
| `GRID_DB_PATH` | `src/runner.db` | SQLite database with the slot tables |
| `GRID_QUEUE_PATH` | `queue.db` | persist-queue directory used for the queue size |
| `GRID_FAIL_THRESHOLD` | `3` | Failures at which a slot counts as failing |
| `GRID_STALE_AFTER` | `3600` | Seconds after which a running session is flagged as stale (likely a check that died without releasing its port) |
| `GRID_ADMIN_TOKEN` | unset | If set, management actions need this token |
| `GRID_DB_ENGINE` | `sqlite` | Set to `mysql` to read from MySQL (`MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`) |

The compose file publishes the UI on 127.0.0.1 only. Set `GRID_ADMIN_TOKEN` before exposing it more widely.

## Creating a check

* look at `full/example/example.py` for reference. If you need the logs, first make the log directory then add it to the docker-compose volumes config `- ./logs/example_playwright/:/app/logs/`. 
* This check scrapes docusign and looks to see the latest status on the Docusign Nodes. 







[l1]: https://playwright.dev/docs/intro
