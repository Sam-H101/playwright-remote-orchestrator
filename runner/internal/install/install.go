// Package install installs, upgrades and uninstalls the runner on a host: the port of
// installer/windows/install-backend.ps1 and uninstall-backend.ps1. Step names and the log lines the CI
// and operators look for are kept.
package install

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/report"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/runtime"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

// Params are the install flags. Pointers are nil when the flag was not given.
type Params struct {
	InstallDir        string
	HubURL            string
	Name              string
	Slots, BasePort   int
	Browsers          []string
	Token             *string // nil = keep the installed token, "" = delete it
	TokenSource       string  // "argv" | "file" | "stdin"
	PlaywrightVersion string
	NodeVersion       string
	ReportURL         *string
	DownloadHost      *string
	HealthTimeout     time.Duration
	ServiceName       string
	NoService         bool
	Source            runtime.Source
	BrowsersFrom      string
	SkipBrowsers      bool
	ForceReset        bool
	Bound             map[string]bool // install flags given explicitly (the re-run keep rules)
	Deprecated        []string        // ignored flags that were given
	Exe               string          // the running exe; default os.Executable()
	Console           io.Writer       // log lines and the summary; default os.Stderr
}

type StepName string

const (
	Preflight    StepName = "preflight"
	StopExisting StepName = "stop existing backend"
	NodePackages StepName = "install node packages"
	InstallAgent StepName = "install agent"
	Browsers     StepName = "install browsers"
	WriteConfig  StepName = "write config"
	Firewall     StepName = "remove legacy firewall rule"
	StartBackend StepName = "start backend"
	HealthCheck  StepName = "health check"
)

const DefaultServiceName = "PlaywrightBackend"

// failure carries the exit code of a failed step (2 for bad input or missing elevation, else 1).
type failure struct {
	code int
	err  error
}

func (f *failure) Error() string { return f.err.Error() }

func fail2(format string, a ...any) error { return &failure{code: 2, err: fmt.Errorf(format, a...)} }

// previous is what this run stopped, so a failure before the new backend starts can bring it back.
type previous struct {
	ServiceExisted, ServiceRunning bool
	LegacyTask                     string
	LegacyTaskExists               bool
	LegacySupervisor               bool
	Wiped, Replaced                bool
}

type run struct {
	p         Params
	version   string
	log       *logging.Logger
	logFile   *logging.Rotating
	elevated  bool
	old       *config.Backend
	cfg       *config.Backend
	prev      previous
	keptToken string
	laidDown  bool
	steps     *report.Steps
	health    report.Health
	current   StepName
}

func (r *run) dir(elem ...string) string {
	return filepath.Join(append([]string{r.p.InstallDir}, elem...)...)
}

// Run installs or upgrades and returns the report and the exit code: 0 success, 2 degraded (installed
// but not healthy in time) or bad input, 1 failed.
func Run(ctx context.Context, p Params, version string) (report.Report, int) {
	started := time.Now()
	if p.Console == nil {
		p.Console = os.Stderr
	}
	if p.ServiceName == "" {
		p.ServiceName = DefaultServiceName
	}
	if p.Exe == "" {
		p.Exe, _ = os.Executable()
	}
	if p.Bound == nil {
		p.Bound = map[string]bool{}
	}
	r := &run{p: p, version: version, steps: report.NewSteps(), log: logging.New(p.Console, nil)}
	if p.Token != nil {
		r.log.Redact(*p.Token)
	}
	r.elevated, _ = winsys.IsElevated()
	r.openLog()
	defer func() {
		if r.logFile != nil {
			r.logFile.Close()
		}
	}()

	err := r.runSteps(ctx)
	result, code := "success", 0
	var restored *bool
	if err == nil && r.health.Problem != nil {
		result, code = "degraded", 2
	}
	if err != nil {
		result, code = "failed", 1
		var f *failure
		if errors.As(err, &f) {
			code = f.code
		}
		r.log.Errorf("Step '%s' failed: %v", r.current, err)
		if r.laidDown {
			if rerr := runtime.Rollback(r.p.InstallDir); rerr != nil {
				r.log.Warnf("Could not roll back app\\ and runtime\\: %v", rerr)
			} else {
				r.log.Warnf("Rolled back app\\ and runtime\\ to the previous install")
			}
		}
		b := r.restorePrevious()
		restored = &b
	}
	rep := report.Build(r.cfg, r.health, result, version, time.Since(started))
	if r.cfg == nil && r.old != nil {
		rep.HubURL, rep.TotalSlots = strp(r.p.HubURL), r.p.Slots
		if rep.Node == "" {
			rep.Node = r.p.Name
		}
	}
	rep.Steps = r.steps
	if err != nil {
		step := string(r.current)
		msg := err.Error()
		rep.FailedStep, rep.Error, rep.RestartedPrevious = &step, &msg, restored
	}
	if werr := report.WriteStatusJSON(r.dir("status.json"), rep); werr != nil {
		r.log.Warnf("Could not write status.json: %v", werr)
	}
	reportURL := ""
	if r.p.ReportURL != nil {
		reportURL = *r.p.ReportURL
	} else if r.cfg != nil && r.cfg.ReportURL != nil {
		reportURL = *r.cfg.ReportURL
	}
	if reportURL != "" {
		if perr := report.Post(ctx, reportURL, rep); perr != nil {
			r.log.Warnf("Could not report status to %s: %v", reportURL, perr)
		} else {
			r.log.Infof("Reported status to %s", reportURL)
		}
	}
	return rep, code
}

func strp(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// openLog opens logs\install.log, creating the install folder first: locked from the start when
// elevated, so nothing can be put in it before it is protected; for the current user too when not.
func (r *run) openLog() {
	if _, err := os.Stat(r.p.InstallDir); errors.Is(err, os.ErrNotExist) {
		if err := winsys.CreateLockedDir(r.p.InstallDir, true); err != nil {
			r.log.Warnf("Could not create %s: %v", r.p.InstallDir, err)
			return
		}
		if !r.elevated {
			r.log.Warnf("Created %s without elevation: it is owned by you, so a later elevated install resets it and installs app\\ and browsers\\ from scratch", r.p.InstallDir)
		}
	}
	r.reopenLog()
}

func (r *run) reopenLog() {
	if r.logFile != nil {
		r.logFile.Close()
		r.logFile = nil
	}
	f, err := logging.OpenRotating(r.dir("logs", "install.log"), config.DefaultLogMaxBytes, config.DefaultLogKeep)
	if err != nil {
		r.log.SetOutput(r.p.Console)
		return
	}
	r.logFile = f
	r.log.SetOutput(io.MultiWriter(r.p.Console, f))
}

// logWriter is where child output (browser downloads) goes.
func (r *run) logWriter() io.Writer {
	if r.logFile != nil {
		return r.logFile
	}
	return io.Discard
}

func (r *run) step(name StepName, body func() error) error {
	r.current = name
	r.log.Infof("==> %s", name)
	t := time.Now()
	err := body()
	r.steps.Set(string(name), float64(int64(time.Since(t).Seconds()*10+0.5))/10)
	return err
}

func (r *run) runSteps(ctx context.Context) error {
	for _, s := range []struct {
		name StepName
		body func() error
	}{
		{Preflight, func() error { return r.preflight(ctx) }},
		{StopExisting, func() error { return r.stopExisting() }},
		{NodePackages, func() error { return r.nodePackages(ctx) }},
		{InstallAgent, func() error { return r.installAgent(ctx) }},
		{Browsers, func() error { return r.browsers(ctx) }},
		{WriteConfig, func() error { return r.writeConfig() }},
		{Firewall, func() error { r.firewall(); return nil }},
		{StartBackend, func() error { return r.start() }},
		{HealthCheck, func() error { return r.healthCheck(ctx) }},
	} {
		if err := r.step(s.name, s.body); err != nil {
			return err
		}
	}
	return nil
}

func (r *run) healthCheck(ctx context.Context) error {
	r.health = report.WaitHealthy(ctx, r.p.InstallDir, r.p.Slots, r.p.HealthTimeout)
	if r.health.Problem != nil {
		r.log.Warnf("The node is installed but not serving after %ds: %s. See %s", int(r.p.HealthTimeout.Seconds()), *r.health.Problem, r.dir("logs", "agent.log"))
	}
	return nil
}

// PrintSummary prints the console summary of an install.
func PrintSummary(w io.Writer, rep report.Report, installDir string) {
	fmt.Fprintf(w, "\nResult: %s  (%d/%d slots healthy, %gs)\n", strings.ToUpper(rep.Result), rep.HealthySlots, rep.TotalSlots, rep.DurationSec)
	if rep.Steps != nil {
		for _, n := range rep.Steps.Names() {
			fmt.Fprintf(w, "  %-28s %6.1fs\n", n, rep.Steps.Seconds(n))
		}
	}
	report.PrintSlots(w, rep)
	if rep.Result != "failed" && rep.HubURL != nil {
		fmt.Fprintf(w, "\nNode '%s' registers itself with the hub at %s (and again after any disconnect).\n", rep.Node, *rep.HubURL)
		fmt.Fprintf(w, "Connect with %s/connect?node=%s, or %s/connect to let the hub pick a node.\n", *rep.HubURL, rep.Node, *rep.HubURL)
	}
	fmt.Fprintf(w, "\nStatus written to %s\n", filepath.Join(installDir, "status.json"))
}
