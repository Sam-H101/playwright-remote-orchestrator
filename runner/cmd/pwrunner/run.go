package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/assets"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/agent"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/service"
)

// runAgent is `pwrunner run`: the agent in the foreground, configured like agent.js (flags, then the
// environment, then --config backend.json), logging to stderr.
func runAgent(args []string) error {
	s, p, err := config.FromArgs(args, config.DefaultContext())
	if err != nil {
		return err
	}
	if p.Help {
		fmt.Print(cli.Usage("run", config.RunFlags))
		return nil
	}
	log := logging.New(os.Stderr, []string{s.Token.Value()})
	if lvl, _ := p.Get("log-level"); lvl == "debug" {
		log.SetDebug(true)
	}
	if s.Watchdog == "" {
		if s.Watchdog, err = writeWatchdog(); err != nil {
			return fmt.Errorf("can't write parent-watchdog.js: %w", err)
		}
	}
	a := agent.New(s, version, log)
	return service.RunConsole(func(ctx context.Context) error { return a.Run(ctx) })
}

// writeWatchdog puts the built-in parent-watchdog.js in <temp>\pwrunner-<version>\ (rewritten at every
// start) for run-servers to preload.
func writeWatchdog() (string, error) {
	dir := filepath.Join(os.TempDir(), "pwrunner-"+safe(version))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(dir, "parent-watchdog.js")
	return path, os.WriteFile(path, assets.ParentWatchdogJS, 0o644)
}

func safe(s string) string {
	out := []rune{}
	for _, r := range s {
		if r == '/' || r == '\\' || r == ':' || r == '+' {
			r = '-'
		}
		out = append(out, r)
	}
	return string(out)
}
