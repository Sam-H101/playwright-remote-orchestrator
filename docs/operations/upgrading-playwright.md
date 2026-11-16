# Upgrading Playwright

The checks' client and every run-server must be on the same minor version (see [Playwright's docs](https://playwright.dev/docs/intro)). The version is pinned once, in `runner/playwright/package.json` (with its integrity in `package-lock.json`), and every image and `pwrunner` built from this repo is built with it:

- The runner image installs that playwright-core and its browsers.
- The checks image installs the Python `playwright` with the same major.minor, the newest up to that version (the Python package skips some of npm's patch releases), and its browsers.
- `pwrunner` embeds it and installs it by default. The Windows install test and the deprecated `installer/windows/install-backend.ps1` default to it too.

## The update pull request

Dependabot proposes each new version as one pull request that changes those two files, two weeks after it comes out. Its checks build the images and run a check through the hub on a runner in Chromium, Firefox and WebKit, and build `pwrunner` and install it on Windows.

The Python package usually comes out a week or two after npm's, but it has taken five; until it has, the checks image fails with `No Playwright X.Y up to X.Y.Z on PyPI?`. Leave the pull request open and re-run its failed jobs once it's out.

The pull request is labelled `release:minor`.

## After merging

Upgrade every place Playwright runs:

- **The orchestrator**: `git pull` and `docker compose ... up -d --build`, or with `docker-compose.ghcr.yaml`, `pull` and `up -d`. That upgrades the checks and the compose runners together. An `IMAGE_TAG` of `X.Y` never moves to the next minor version, which is what a Playwright update is: use `latest` or the new release.
- **Windows hosts**: release a new `pwrunner` (a `pwrunner/vX.Y.Z` tag, see [Releases](../development/releases.md#windows-runner)) and re-run `pwrunner install` with it on every host, or pass `--playwright-version <new> --playwright-integrity <sha512-...>` (the `integrity` in `package-lock.json`) to the existing exe.
- **Linux/macOS agent hosts**: after a `git pull`, repeat the copy, `npm ci` and browser install steps from [Linux or macOS host](../deployment/linux-macos.md#install), then restart the agent.
- **Runner containers on other docker hosts**: pull the new image and recreate the container (the same `docker run`).
- **Legacy static hosts**: the `run-server` install.
- **Any other Playwright client** that connects to the hub: the same minor version.

## Moving to a version by hand

Change it in `runner/playwright/package.json` and run `npm install` in that folder, which updates `package-lock.json`.

## During a rolling upgrade

The hub itself needs no upgrade: it passes frames through without reading them. It does read each client's version, and only routes it to nodes on the same minor version, so during a rolling upgrade clients go to the nodes that match them. A client whose version no node runs gets 428 at once.
