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
		dir, err := writeWatchdog()
		if err != nil {
			return fmt.Errorf("can't write parent-watchdog.js: %w", err)
		}
		defer os.RemoveAll(dir)
		s.Watchdog = filepath.Join(dir, "parent-watchdog.js")
	}
	a := agent.New(s, version, log)
	return service.RunConsole(func(ctx context.Context) error { return a.Run(ctx) })
}

// writeWatchdog puts the built-in parent-watchdog.js in a new folder under the temp folder for run-servers
// to preload, and returns the folder. A new folder rather than a fixed name: with --run-server-user the
// agent runs as root, and another user could have created a fixed path in /tmp first, or linked it
// elsewhere. The folder is world-readable so a run-server started as another user can load the file.
func writeWatchdog() (string, error) {
	dir, err := os.MkdirTemp("", "pwrunner-"+safe(version)+"-")
	if err != nil {
		return "", err
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		os.RemoveAll(dir)
		return "", err
	}
	if err := os.WriteFile(filepath.Join(dir, "parent-watchdog.js"), assets.ParentWatchdogJS, 0o644); err != nil {
		os.RemoveAll(dir)
		return "", err
	}
	return dir, nil
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
