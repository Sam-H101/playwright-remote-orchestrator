# playwright-remote-orchestrator

## Purpose

* This was built as a method to run multiple tests against any number of servers. It will pick a random server and port and run the checks against them. It was built as playwright currently has a limitation where one can only run checks on the same box as where the code lives. This allows the checks to be put into docker containers and run centeralized while the actual orchestrators are ran in a number of places. 

## Limitations

* Currently it does not know if the remote nodes are up or down. Future state it will use the failure counters + a pinger to mark them up / down.
* Port and host state is kept in MySQL, so every orchestrator container just needs to reach the MySQL service. Ports are claimed with an atomic update, so two containers never get the same port.


## Building 

1. Run docker-compose build

## Running with MySQL

The MySQL service lives in its own compose file, `docker-compose.mysql.yaml`. Start it together with the main file:

```
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d
```

Credentials default to `orchestrator` / `orchestrator`. Override them with a `.env` file or shell variables: `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`. The orchestrator containers read `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD`, which fall back to the `mysql:` section of `src/database.yaml`.

To use an existing MySQL server instead, skip `docker-compose.mysql.yaml` and set those `DB_*` variables (or `src/database.yaml`) to point at it.


## Running the checks with local server

1. uncomment the CMD line in `docker/python/Dockerfile`
2. Build the image and it should start if you do docker-compose up
* Note this has not been tested

## Running a remote server

1. Install playwright server on the remote executioner host. Can be found [here][l1]
2. once installed, start the server by running `node_modules/.bin/playwright.cmd run-server --port <port number> --reuse-browser false`

## Configuration

1. modify `src/database.yaml` with the servername, start port, and end port
2. The tables are created and seeded automatically the first time a check runs. You can also do it up front by appending `--fr true` to the command and running it once. Re-running it is safe, and it adds rows for any new hosts or ports you add to `src/database.yaml`.

## Creating a check

* look at `full/example/example.py` for reference. If you need the logs, first make the log directory then add it to the docker-compose volumes config `- ./logs/example_playwright/:/app/logs/`. 
* This check scrapes docusign and looks to see the latest status on the Docusign Nodes. 







[l1]: https://playwright.dev/docs/intro
