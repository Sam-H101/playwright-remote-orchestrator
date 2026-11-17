# Repository and tests

## Repository layout

| Path | Holds |
| --- | --- |
| `gateway/` | The hub (`hub.js`), the Node agent (`agent.js`) and their shared code in `lib/` (`config.js`, `protocol.js`, `registry.js`, `routing.js`, `tokens.js`, `pipe.js`, `parent-watchdog.js`, `log.js`); tests in `test/` |
| `runner/` | `pwrunner`, the Go agent and Windows installer (`cmd/pwrunner`, `internal/`, `assets/`, `tools/`, `build/`); `runner/playwright/` pins Playwright for the whole repository. See `runner/README.md` |
| `ui/` | The Grid UI (Flask, plain ES modules in `static/`, `templates/`); `auth.py`, `oidc.py`, `accounts.py`, `permissions.py`, `mfa.py`, `recorder.py`, `store.py`, `logs.py`, `demo.py`, `gunicorn_conf.py`; tests in `ui/tests/` |
| `main.py`, `src/` | The orchestrator: `main.py` runs checks; `src/` has the database code, the config (`database.yaml`), the schema (`schema.sql`) and the check-side clients `mfa.py` and `signin.py` |
| `full/` | Checks: `example/example.py` and the proof-of-concept checks in `poc/` |
| `tests/` | The orchestrator's tests |
| `docker/` | Dockerfiles for `hub`, `runner` (with its `entrypoint.sh`), `ui` and `python` (the checks) |
| `docker-compose*.yaml` | The stack, MySQL, the published images and the proof-of-concept checks |
| `installer/windows/` | The deprecated PowerShell installer |
| `.github/` | Workflows, scripts (`action-pins.sh`, `next-version.sh`, `e2e.sh`) and Dependabot's settings |
| `docs/`, `mkdocs.yml` | This site |

## Running the tests

None of these need docker (the Go tests need Go 1.26 or newer).

- **Gateway**: `cd gateway && npm ci && npm test`. `GATEWAY_TEST_MYSQL=1` plus `DB_*` also runs the MySQL registry tests; they drop the gateway tables in `DB_NAME`, so point it at a scratch database.
- **Orchestrator and Grid UI**: `pip install -r ui/requirements.txt pytest && python -m pytest tests ui/tests`.
    - The example-check and browser tests run when the `playwright` package is installed; the MySQL tests run when `ORCHESTRATOR_TEST_DB` (plus `DB_*`) points at a scratch database; the tests of gunicorn's log lines (`ui/tests/test_logs.py`) run when `gunicorn` is installed.
    - Single sign-on, people, roles and sharing are tested by `ui/tests/test_oidc.py`, `test_accounts.py`, `test_sharing.py` and `test_accounts_mysql.py`, and in the browser by `test_browser_oidc.py` and `test_browser_admin.py`, against a fake identity provider the tests run in-process (`ui/tests/fake_idp.py`), so they need no network and no real provider. The browser tests serve it at `localhost` while the Grid UI is at `127.0.0.1`, so the two are cross-site, as a real provider is.
- **Agent contract**: `cd gateway && node --test test/agent-contract.test.js` runs any agent as a child process against an in-process hub (`AGENT_CMD`, default `node agent.js`; `AGENT_CMD="..\dist\pwrunner.exe run"` for the Go agent).
- **Go runner**: `cd runner && go test ./...`, and `go test -tags integration ./test/integration/...` against the real hub (needs `gateway/node_modules`). See `runner/README.md` for `gofmt`, `go vet`, `staticcheck` and the real-runtime test. The `winsys` tests that change ACLs and the install tests need an elevated prompt and skip otherwise. Never point a test at a live hub: use `HUB_DB=memory` and a free port.
- **Windows install**: `.github/workflows/windows-backend-installer.yml` migrates a PowerShell install to `pwrunner.exe` against a hub on a Windows runner and connects Playwright through it.

## As CI runs them

| Workflow | Runs |
| --- | --- |
| `python-tests.yml` | The Grid UI's tests inside the Grid UI image built from the commit, and the orchestrator's on the checks image's Python and packages, with MySQL (`mysql:8.4` service) and the browser tests switched on. Each step fails if pytest skipped anything |
| `runner-go.yml` | The Go runner's build and tests; for every release (called from `release.yml`) and on a `pwrunner/v*` tag, the release binaries |
| `docker-images.yml` | Builds every image; on pull requests and master runs a check through the hub on a runner, in every browser, with the images just built (`.github/scripts/e2e.sh`) |
| `windows-backend-installer.yml` | The Windows install test above |
| `release.yml` | The next version and the release; runs `.github/scripts/action-pins.sh` on each pull request into master |
| `docs.yml` | Builds this site with `mkdocs build --strict`; deploys it to GitHub Pages from master |

To reproduce the Grid UI job locally the way CI does, build the image (`docker build -f docker/ui/Dockerfile -t grid-ui:test .`) and run the tests inside it with `ui/tests` mounted, as `python-tests.yml` shows.

## Workflow conventions

Every third-party action is pinned to a full commit SHA with its version in a comment on the same line (`uses: actions/checkout@<sha> # v7.0.1`). `.github/scripts/action-pins.sh` fails any `uses:` that isn't; Dependabot moves the pins and the comments. To pin a new action, look the commit up with `gh api repos/OWNER/REPO/commits/tags/vX.Y.Z --jq .sha`.

## Building this site

The site is Material for MkDocs, version pinned in `docs/requirements.txt`. Build it in a container without installing anything:

```sh
docker run --rm -v "$PWD:/docs" squidfunk/mkdocs-material:9.8.0 build --strict
```

`serve --dev-addr 0.0.0.0:8000` with `-p 8000:8000` serves it with live reload. The same build publishes to GitHub Pages at <https://playwright-remote-orchestration.sdevwork.com> (`.github/workflows/docs.yml`; the custom domain is set in Settings → Pages) and GitLab Pages (`.gitlab-ci.yml`, the `pages` job).
