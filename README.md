# playwright-remote-orchestrator

## Building 

1. Run docker-compose build


## Running the checks

1. uncomment the CMD line in `docker/python/Dockerfile`
2. Build the image and it should start if you do docker-compose up
* Note this has not been tested

## Running a remote server

1. Install playwright server on the remote executioner host. Can be found [here][l1]
2. once installed, start the server by running `node_modules/.bin/playwright.cmd run-server --port <port number> --reuse-browser false`

## Configuration

1. modify `src/database.yaml` with the servername, start port, and end port
2. append `--fr true` to the docker-compose file and run it once. afterwords remove the `--fr true`. This is used to initialize the database the orchestrator uses to determine if ports are open or not.

## Creating a check

* look at `full/example/example.py` for reference. If you need the logs, first make the log directory then add it to the docker-compose volumes config `- ./logs/example_playwright/:/app/logs/`. 
* This check scrapes docusign and looks to see the latest status on the Docusign Nodes. 







[l1]: https://playwright.dev/docs/intro
