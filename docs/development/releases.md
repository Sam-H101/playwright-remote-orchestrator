# Releases and images

## Published images {#published-images}

CI builds every image and publishes it to GitHub Container Registry (`.github/workflows/docker-images.yml`):

| Image | Is |
| --- | --- |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/hub` | the gateway hub (amd64, arm64) |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/runner` | a Linux runner node: the agent and Chromium, Firefox and WebKit (amd64, arm64) |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/grid-ui` | the Grid UI (amd64, arm64) |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/checks` | the image `main.py` checks run in (amd64) |

Tags:

- a version such as `1.2.0` for each [release](#releases), `1.2` for the newest 1.2.x, and `latest` for the newest release;
- `edge` and `sha-<commit>` for every change on master that touches the images.

The packages are public: pulling needs no login. Each one's page is under **Packages** on the repository page. To run them with compose, see [Getting started](../getting-started.md#published-images); `IMAGE_TAG` in `.env` pins the version (default `latest`).

## Releases {#releases}

Every merge to master is a release (`.github/workflows/release.yml`):

- The next version is tagged `vX.Y.Z`.
- A GitHub Release lists the pull requests merged since the last one.
- The images above are published as `X.Y.Z`, `X.Y` and `latest`.

A pull request's label picks the version:

- `release:minor` or `release:major`; otherwise it's a patch.
- `release:skip` makes no release, for changes that ship nothing (docs, CI). What it changed goes out with the next release. If an earlier merge is still waiting to go out, it releases that.

The labels exist in the repository; any other label counts as a patch. Each pull request's **next version** check says, in its summary, what merging it would release and which merges it counted. That's as of its last push or label change: re-run it after other merges.

A release takes master as it is when its run starts, so merges that land while a release is running go out together in the next one, and a commit that a release already contains is never released again. If a release's images fail to build, re-run the failed jobs. `latest` only ever moves to the newest release, and `X.Y` to the newest of that X.Y, so re-running an older release can't take them back.

## The Windows runner's binaries {#windows-runner}

Every release carries the Windows runner's binaries: after tagging, `release.yml` calls `.github/workflows/runner-go.yml`, which builds that tag and attaches them. To add them to a release made without them, run it by hand with the tag and `dry_run` off:

```powershell
gh workflow run runner-go.yml -f tag=v2.0.11 -f dry_run=false
```

A `pwrunner/vX.Y.Z` tag pushed by hand still makes a release of its own with the binaries and publishes the images, so give it the next free version number. The automatic releases count on from it.

```powershell
git tag pwrunner/v1.3.0 ; git push origin pwrunner/v1.3.0
```

Each release holds the embedded amd64 and arm64 exes, the slim exe, the two Linux builds, `pwrunner-runtime-<v>-windows-amd64.zip` (for `install --offline-dir`) and `SHA256SUMS.txt`. `gh workflow run runner-go.yml -f dry_run=true` builds the same set without releasing (artefacts on the run). The builds are not code-signed yet; operators verify the download against `SHA256SUMS.txt`.

Building `pwrunner` locally (Go 1.26 or newer), from `runner/`:

```powershell
go build -o ..\dist\pwrunner.exe .\cmd\pwrunner                     # slim build: downloads the runtime at install
go run .\tools\fetchbundle --out .\internal\runtime\files            # pinned Node + playwright-core, hash-verified
go build -tags embedruntime -o ..\dist\pwrunner.exe .\cmd\pwrunner   # runtime embedded (about 42 MB)
.\build\build.ps1 -Version 1.1.0                                     # every release artefact + SHA256SUMS.txt into dist\
```

Node's pinned version and hashes live in `runner/internal/runtime/pins.go`; `go run ./tools/updatepins --node <v>` prints a new block for it.

## Dependabot

Dependabot (`.github/dependabot.yml`) opens update pull requests every Monday at 06:00 UTC, for the gateway's npm packages, the runner's Go modules, the Python requirements, Playwright, the images' base images and the workflows' actions. Merging one releases a patch.

- Its GitHub Actions updates come labelled `release:skip`, and its Playwright updates `release:minor` (see [Upgrading Playwright](../operations/upgrading-playwright.md)).
- An update to the hub, runner or checks image is tested by building the images and running a check through the hub and a runner with them. That check doesn't include the Grid UI, so try a Grid UI update before merging it.
- A version is only offered once it has been out for a week (base images: a day; Playwright: two weeks). Security updates never wait.

## Documentation changes

Changes that only touch `docs/` or `mkdocs.yml` ship nothing in the images: label their pull requests `release:skip`. The site is rebuilt and deployed from master by `.github/workflows/docs.yml`.
