package runtime

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	goruntime "runtime"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/assets"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

// Manifest records what is laid down in an install dir (manifest.json).
type Manifest struct {
	NodeVersion         string `json:"nodeVersion"`
	NodeSHA256          string `json:"nodeSha256"`
	PlaywrightVersion   string `json:"playwrightVersion"`
	PlaywrightIntegrity string `json:"playwrightIntegrity"`
	RunnerVersion       string `json:"runnerVersion"`
	Source              string `json:"source"`  // embedded | offline | download | cache
	Fetched             string `json:"fetched"` // ISO-8601 UTC
}

// Same reports whether two manifests describe the same Node and playwright-core.
func (m Manifest) Same(o Manifest) bool {
	return m.NodeVersion == o.NodeVersion && strings.EqualFold(m.NodeSHA256, o.NodeSHA256) &&
		m.PlaywrightVersion == o.PlaywrightVersion && m.PlaywrightIntegrity == o.PlaywrightIntegrity
}

func manifestPath(dir string) string { return filepath.Join(dir, "manifest.json") }

func ReadManifest(installDir string) (Manifest, bool, error) {
	b, err := os.ReadFile(manifestPath(installDir))
	if errors.Is(err, os.ErrNotExist) {
		return Manifest{}, false, nil
	}
	if err != nil {
		return Manifest{}, true, err
	}
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return Manifest{}, true, err
	}
	return m, true, nil
}

func WriteManifest(installDir string, m Manifest) error {
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp := manifestPath(installDir) + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, manifestPath(installDir))
}

// NodeBinary is the private node in an install dir.
func NodeBinary(installDir string) string {
	if goruntime.GOOS == "windows" {
		return filepath.Join(installDir, "runtime", "node", "node.exe")
	}
	return filepath.Join(installDir, "runtime", "node", "node")
}

// CLI is playwright-core's cli.js in an install dir.
func CLI(installDir string) string {
	return filepath.Join(installDir, "app", "node_modules", "playwright-core", "cli.js")
}

// Watchdog is parent-watchdog.js in an install dir.
func Watchdog(installDir string) string {
	return filepath.Join(installDir, "app", "lib", "parent-watchdog.js")
}

// Want is the manifest for the pinned (or explicitly hashed) versions.
func Want(nodeVersion, playwrightVersion, runnerVersion string, src Source) (Manifest, error) {
	pin, err := NodePin(nodeVersion, goruntime.GOOS, goruntime.GOARCH, src.NodeMirror, src.NodeSHA256)
	if err != nil {
		return Manifest{}, err
	}
	_, sri, err := PlaywrightPin(playwrightVersion, src.NPMRegistry, src.PlaywrightIntegrity)
	if err != nil {
		return Manifest{}, err
	}
	return Manifest{NodeVersion: nodeVersion, NodeSHA256: pin.SHA256, PlaywrightVersion: playwrightVersion, PlaywrightIntegrity: sri, RunnerVersion: runnerVersion}, nil
}

// LayDown makes installDir run the wanted Node and playwright-core. When manifest.json already
// describes them and the files are there it logs "already installed, skipping npm" and changes nothing.
// Otherwise it fetches and verifies both archives into <dir>\cache, builds app.new\ and runtime.new\,
// checks `node --version`, and swaps them in (the previous generation becomes app.prev\ and
// runtime.prev\, which Rollback restores).
func LayDown(ctx context.Context, installDir string, want Manifest, src Source, log *logging.Logger) (bool, error) {
	if have, ok, _ := ReadManifest(installDir); ok && have.Same(want) && exists(NodeBinary(installDir)) && exists(CLI(installDir)) {
		log.Infof("node %s and playwright-core %s already installed, skipping npm", have.NodeVersion, have.PlaywrightVersion)
		writeWatchdog(installDir)
		return false, nil
	}
	cache := filepath.Join(installDir, "cache")
	src.NodeSHA256 = want.NodeSHA256
	src.PlaywrightIntegrity = want.PlaywrightIntegrity
	nodeArchive, nodeFrom, err := fetchNode(ctx, cache, want.NodeVersion, goruntime.GOOS, goruntime.GOARCH, src)
	if err != nil {
		return false, err
	}
	log.Infof("node %s: %s (%s)", want.NodeVersion, filepath.Base(nodeArchive), nodeFrom)
	pwArchive, pwFrom, err := fetchPlaywright(ctx, cache, want.PlaywrightVersion, src)
	if err != nil {
		return false, err
	}
	log.Infof("playwright-core %s: %s (%s)", want.PlaywrightVersion, filepath.Base(pwArchive), pwFrom)

	appNew, runtimeNew := filepath.Join(installDir, "app.new"), filepath.Join(installDir, "runtime.new")
	for _, d := range []string{appNew, runtimeNew} {
		if err := os.RemoveAll(d); err != nil {
			return false, err
		}
	}
	if err := ExtractNode(nodeArchive, filepath.Join(runtimeNew, "node")); err != nil {
		return false, err
	}
	if err := ExtractPlaywright(pwArchive, filepath.Join(appNew, "node_modules", "playwright-core")); err != nil {
		return false, err
	}
	if err := os.MkdirAll(filepath.Join(appNew, "lib"), 0o755); err != nil {
		return false, err
	}
	if err := os.WriteFile(filepath.Join(appNew, "lib", "parent-watchdog.js"), assets.ParentWatchdogJS, 0o644); err != nil {
		return false, err
	}
	node := filepath.Join(runtimeNew, "node", filepath.Base(NodeBinary(installDir)))
	if got, err := nodeVersionOf(ctx, node); err != nil {
		return false, err
	} else if got != "v"+want.NodeVersion {
		return false, fmt.Errorf("%s --version printed %q, want v%s", node, got, want.NodeVersion)
	}

	if err := swap(installDir, "app"); err != nil {
		return false, err
	}
	if err := swap(installDir, "runtime"); err != nil {
		Rollback(installDir)
		return false, err
	}
	if old, ok, _ := ReadManifest(installDir); ok {
		b, _ := json.MarshalIndent(old, "", "  ")
		os.WriteFile(filepath.Join(installDir, "manifest.prev.json"), append(b, '\n'), 0o644)
	} else {
		os.Remove(filepath.Join(installDir, "manifest.prev.json"))
	}
	want.Source = nodeFrom
	if pwFrom != nodeFrom {
		want.Source = nodeFrom + "+" + pwFrom
	}
	want.Fetched = time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
	if err := WriteManifest(installDir, want); err != nil {
		Rollback(installDir)
		return false, err
	}
	log.Infof("installed node %s and playwright-core %s", want.NodeVersion, want.PlaywrightVersion)
	return true, nil
}

// swap moves <name> to <name>.prev (dropping an older .prev) and <name>.new to <name>.
func swap(dir, name string) error {
	cur, prev, next := filepath.Join(dir, name), filepath.Join(dir, name+".prev"), filepath.Join(dir, name+".new")
	if err := os.RemoveAll(prev); err != nil {
		return err
	}
	if exists(cur) {
		if err := os.Rename(cur, prev); err != nil {
			return fmt.Errorf("can't move %s aside (is a run-server still running from it?): %w", cur, err)
		}
	}
	if err := os.Rename(next, cur); err != nil {
		os.Rename(prev, cur)
		return err
	}
	return nil
}

// Rollback puts app.prev\ and runtime.prev\ (and manifest.prev.json) back after a failed upgrade.
func Rollback(installDir string) error {
	var errs []error
	for _, name := range []string{"app", "runtime"} {
		cur, prev := filepath.Join(installDir, name), filepath.Join(installDir, name+".prev")
		if !exists(prev) {
			continue
		}
		if err := os.RemoveAll(cur); err != nil {
			errs = append(errs, err)
			continue
		}
		if err := os.Rename(prev, cur); err != nil {
			errs = append(errs, err)
		}
	}
	mprev := filepath.Join(installDir, "manifest.prev.json")
	if exists(mprev) {
		if err := os.Rename(mprev, manifestPath(installDir)); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func writeWatchdog(installDir string) {
	p := Watchdog(installDir)
	if b, err := os.ReadFile(p); err == nil && string(b) == string(assets.ParentWatchdogJS) {
		return
	}
	os.MkdirAll(filepath.Dir(p), 0o755)
	os.WriteFile(p, assets.ParentWatchdogJS, 0o644)
}

func exists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// nodeVersionOf is replaced in tests, whose archives hold no real node.
var nodeVersionOf = NodeVersionOf

// NodeVersionOf runs `node --version`.
func NodeVersionOf(ctx context.Context, node string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, node, "--version")
	cmd.Env = proc.ChildEnv(os.Environ(), map[string]string{"NODE_OPTIONS": ""})
	proc.Hide(cmd)
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("%s --version: %w", node, err)
	}
	return strings.TrimSpace(string(out)), nil
}

// CLIVersionOf runs `node cli.js --version` ("Version 1.64.1").
func CLIVersionOf(ctx context.Context, node, cli string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, node, cli, "--version")
	cmd.Env = proc.ChildEnv(os.Environ(), map[string]string{"NODE_OPTIONS": ""})
	proc.Hide(cmd)
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("%s --version: %w", cli, err)
	}
	return strings.TrimSpace(string(out)), nil
}
