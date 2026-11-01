package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	goruntime "runtime"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/runtime"
)

var installDirFlag = cli.FlagSpec{Name: "install-dir", Arg: "<path>", Usage: `install folder (default: this exe's folder if it holds backend.json, else ` + defaultDir + ")"}

var sourceFlags = []cli.FlagSpec{
	{Name: "playwright-version", Arg: "<v>", Usage: "playwright-core version (default " + runtime.PlaywrightVersion + ")"},
	{Name: "playwright-integrity", Arg: "sha512-...", Usage: "npm integrity of an unpinned playwright-core version"},
	{Name: "node-version", Arg: "<v>", Usage: "Node version (default " + runtime.NodeVersion + ")"},
	{Name: "node-sha256", Arg: "<hex>", Usage: "SHA-256 of an unpinned Node version's archive"},
	{Name: "offline-dir", Arg: "<dir>", Usage: "folder holding the Node archive and playwright-core tarball"},
	{Name: "node-mirror", Arg: "<url>", Usage: "Node download mirror (default " + runtime.DefaultNodeMirror + ")"},
	{Name: "npm-registry", Arg: "<url>", Usage: "npm registry (default " + runtime.DefaultNPMRegistry + ")"},
}

// installDir resolves --install-dir: the flag, else this exe's folder when it holds backend.json, else
// the default folder.
func installDir(p cli.Parsed) string {
	if v, ok := p.Get("install-dir"); ok && v != "" {
		abs, err := filepath.Abs(v)
		if err == nil {
			return abs
		}
		return v
	}
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		if _, err := os.Stat(filepath.Join(dir, "backend.json")); err == nil {
			return dir
		}
	}
	return defaultDir
}

func sourceFrom(p cli.Parsed) runtime.Source {
	get := func(k string) string { v, _ := p.Get(k); return v }
	return runtime.Source{
		OfflineDir: get("offline-dir"), NodeMirror: get("node-mirror"), NPMRegistry: get("npm-registry"),
		NodeSHA256: get("node-sha256"), PlaywrightIntegrity: get("playwright-integrity"),
	}
}

func versionsFrom(p cli.Parsed) (node, pw string) {
	node, pw = runtime.NodeVersion, runtime.PlaywrightVersion
	if v, ok := p.Get("node-version"); ok && v != "" {
		node = strings.TrimPrefix(v, "v")
	}
	if v, ok := p.Get("playwright-version"); ok && v != "" {
		pw = v
	}
	return node, pw
}

func parse(command string, args []string, specs []cli.FlagSpec) (cli.Parsed, error) {
	p, err := cli.Parse(append([]string{command}, args...), specs)
	if err != nil {
		return p, err
	}
	if p.Help {
		fmt.Print(cli.Usage(command, specs))
		return p, nil
	}
	if len(p.Args) > 0 {
		return p, cli.Usagef("unknown argument %q", p.Args[0])
	}
	return p, nil
}

// fetchCmd is `pwrunner fetch`: download and verify the runtime archives into <D>\cache only.
func fetchCmd(args []string) error {
	p, err := parse("fetch", args, append([]cli.FlagSpec{installDirFlag}, sourceFlags...))
	if err != nil || p.Help {
		return err
	}
	dir := installDir(p)
	nodeV, pwV := versionsFrom(p)
	src := sourceFrom(p)
	if _, err := runtime.NodePin(nodeV, goruntime.GOOS, goruntime.GOARCH, src.NodeMirror, src.NodeSHA256); err != nil {
		return cli.Usagef("%v", err)
	}
	if _, _, err := runtime.PlaywrightPin(pwV, src.NPMRegistry, src.PlaywrightIntegrity); err != nil {
		return cli.Usagef("%v", err)
	}
	ctx := context.Background()
	cache := filepath.Join(dir, "cache")
	nodeArchive, err := runtime.FetchNode(ctx, cache, nodeV, goruntime.GOOS, goruntime.GOARCH, src)
	if err != nil {
		return err
	}
	fmt.Printf("node %s: %s (verified)\n", nodeV, nodeArchive)
	pwArchive, err := runtime.FetchPlaywright(ctx, cache, pwV, src)
	if err != nil {
		return err
	}
	fmt.Printf("playwright-core %s: %s (verified)\n", pwV, pwArchive)
	return nil
}

// nodeCmd is `pwrunner node -- <args>`: the private node with the run-servers' environment.
func nodeCmd(args []string) error {
	p, err := parse("node", args, []cli.FlagSpec{installDirFlag})
	if err != nil || p.Help {
		return err
	}
	dir := installDir(p)
	node := runtime.NodeBinary(dir)
	if _, err := os.Stat(node); err != nil {
		return fmt.Errorf("%s not found; run pwrunner install first", node)
	}
	set := map[string]string{}
	if os.Getenv("PLAYWRIGHT_BROWSERS_PATH") == "" {
		if _, err := os.Stat(filepath.Join(dir, "backend.json")); err == nil {
			set["PLAYWRIGHT_BROWSERS_PATH"] = filepath.Join(dir, "browsers")
		}
	}
	if b, ok, _ := config.ReadBackend(filepath.Join(dir, "backend.json")); ok && b.DownloadHost != nil && *b.DownloadHost != "" {
		set["PLAYWRIGHT_DOWNLOAD_HOST"] = *b.DownloadHost
	}
	cmd := exec.Command(node, p.Rest...)
	cmd.Env = proc.ChildEnv(os.Environ(), set)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	err = cmd.Run()
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		return &cli.ExitError{Code: ee.ExitCode()}
	}
	return err
}

// check is one selfcheck line.
type check struct {
	name string
	ok   bool
	warn bool // a warning: printed, but not a failure
	info string
}

// selfcheckCmd is `pwrunner selfcheck`: exit 0 when every check passes, 2 otherwise.
func selfcheckCmd(args []string) error {
	p, err := parse("selfcheck", args, []cli.FlagSpec{installDirFlag, {Name: "quick", Bool: true, Usage: "skip the checks that need the network"}})
	if err != nil || p.Help {
		return err
	}
	dir := installDir(p)
	quick := p.Has("quick")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	checks := runtimeChecks(ctx, dir)
	checks = append(checks, platformChecks(ctx, dir, quick)...)
	failed := 0
	for _, c := range checks {
		mark := "ok  "
		switch {
		case !c.ok && c.warn:
			mark = "WARN"
		case !c.ok:
			mark = "FAIL"
			failed++
		}
		fmt.Printf("%s  %-28s %s\n", mark, c.name, c.info)
	}
	if failed > 0 {
		return &cli.ExitError{Code: 2, Msg: fmt.Sprintf("%d check(s) failed", failed)}
	}
	return nil
}

func runtimeChecks(ctx context.Context, dir string) []check {
	var out []check
	node := runtime.NodeBinary(dir)
	v, err := runtime.NodeVersionOf(ctx, node)
	if err != nil {
		out = append(out, check{name: "node", info: err.Error()})
	} else {
		out = append(out, check{name: "node", ok: true, info: v + " (" + node + ")"})
		major := strings.SplitN(strings.TrimPrefix(v, "v"), ".", 2)[0]
		if eol, known := runtime.NodeEOL[major]; known {
			end, _ := time.Parse("2006-01-02", eol)
			left := time.Until(end)
			c := check{name: "node end of life", ok: left > 90*24*time.Hour, warn: true, info: "Node " + major + " is supported until " + eol}
			if !c.ok {
				c.info += "; update the pinned Node version"
			}
			out = append(out, c)
		}
	}
	cliPath := runtime.CLI(dir)
	if cv, err := runtime.CLIVersionOf(ctx, node, cliPath); err != nil {
		out = append(out, check{name: "playwright-core", info: err.Error()})
	} else {
		out = append(out, check{name: "playwright-core", ok: true, info: cv + " (" + cliPath + ")"})
	}
	if m, ok, err := runtime.ReadManifest(dir); err != nil || !ok {
		out = append(out, check{name: "manifest.json", info: fmt.Sprintf("missing or unreadable (%v)", err)})
	} else {
		out = append(out, check{name: "manifest.json", ok: true, info: fmt.Sprintf("node %s, playwright-core %s, from %s", m.NodeVersion, m.PlaywrightVersion, m.Source)})
	}
	return out
}
