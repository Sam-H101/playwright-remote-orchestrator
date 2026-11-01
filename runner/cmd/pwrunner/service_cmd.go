package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/agent"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/install"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/service"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

// serviceCmd is the service body: the agent configured only from backend.json and agent.token (never
// the environment, so machine-wide variables can't redirect the node), logging to logs\agent.log.
func serviceCmd(args []string) error {
	p, err := parse("service", args, []cli.FlagSpec{installDirFlag, {Name: "console", Bool: true, Usage: "run in the foreground"}})
	if err != nil || p.Help {
		return err
	}
	dir := installDir(p)
	b, ok, err := config.ReadBackend(filepath.Join(dir, "backend.json"))
	if err != nil || !ok {
		return &cli.ExitError{Code: 2, Msg: fmt.Sprintf("no backend.json in %s (%v)", dir, err)}
	}
	name := b.ServiceName
	if name == "" {
		name = install.DefaultServiceName
	}
	asService := service.IsWindowsService() && !p.Has("console")

	agentLog, err := logging.OpenRotating(filepath.Join(dir, "logs", "agent.log"), b.LogMaxBytes, b.LogKeep)
	if err != nil {
		return err
	}
	defer agentLog.Close()
	svcLog, err := logging.OpenRotating(filepath.Join(dir, "logs", "service.log"), b.LogMaxBytes, b.LogKeep)
	if err != nil {
		return err
	}
	defer svcLog.Close()
	var out io.Writer = agentLog
	if !asService {
		out = io.MultiWriter(agentLog, os.Stderr)
	}
	log := logging.New(out, nil)
	slog := logging.New(svcLog, nil)

	// A configuration error is returned from inside run, so the SCM gets exit code 2 rather than a
	// process that never reported its status.
	s := b.AgentSettings(dir)
	token, _, err := winsys.ReadTokenFile(filepath.Join(dir, "agent.token"))
	var startErr error
	if err != nil {
		startErr = fmt.Errorf("can't read agent.token: %w", err)
	} else if err := s.Validate(); err != nil {
		startErr = fmt.Errorf("bad backend.json: %w", err)
	}
	s.Token = config.Secret(token)
	log.Redact(token)
	slog.Redact(token)
	ev := service.EventLog(name)
	if ev != nil {
		defer ev.Close()
	}

	os.Remove(filepath.Join(dir, "pwrunner.old.exe"))
	if job, err := winsys.NewKillOnCloseJob(); err == nil {
		// The run-servers and their browsers inherit the job, so they die with this process however it ends.
		if err := job.AssignCurrent(); err != nil {
			slog.Warnf("could not join a kill-on-close job (%v); run-servers rely on the watchdog alone", err)
		}
	}
	// Run-servers a previous instance left behind would hold the slot ports.
	if killed, _ := winsys.KillByImagePrefix(filepath.Join(dir, "runtime", "node"), 10*time.Second); len(killed) > 0 {
		slog.Warnf("Stopped %d process(es) left over from a previous run", len(killed))
	}

	run := func(ctx context.Context) error {
		if startErr != nil {
			slog.Errorf("%v", startErr)
			if ev != nil && asService {
				ev.Error(4, "pwrunner can't start: "+startErr.Error())
			}
			return &service.ExitCodeError{Code: 2, Err: startErr}
		}
		slog.Infof("service starting: pwrunner %s, node %s, %d slots, hub %s", version, s.Name, s.Slots, s.Hub)
		a := agent.New(s, version, log)
		if ev != nil && asService {
			ev.Info(1, fmt.Sprintf("pwrunner %s started: node %s, %d slots, hub %s", version, s.Name, s.Slots, s.Hub))
			defer ev.Info(2, "pwrunner stopped")
			a.OnSlowSlot = func(index int, delay time.Duration) {
				ev.Warning(3, fmt.Sprintf("slot %d keeps crashing; next restart in %s (see logs\\slot-%d.log)", index, delay, index))
			}
		}
		err := a.Run(ctx)
		slog.Infof("service stopped")
		return err
	}
	if asService {
		return service.RunAsService(name, run)
	}
	return service.RunConsole(run)
}
