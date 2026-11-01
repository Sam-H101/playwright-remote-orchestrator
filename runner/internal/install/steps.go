package install

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/runtime"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/service"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

var hubURLRE = regexp.MustCompile(`(?i)^wss?://[^/\s]+`)

func (r *run) preflight(ctx context.Context) error {
	p := &r.p
	if !p.NoService && !r.elevated {
		return fail2("Registering the Windows service needs an elevated prompt. Run elevated, or pass --no-service.")
	}
	for _, d := range p.Deprecated {
		r.log.Warnf("--%s is deprecated and ignored: the node dials out to the hub and opens no inbound ports.", d)
	}
	if p.TokenSource == "argv" {
		r.log.Warnf("token passed on the command line; prefer --token-file or --token-stdin")
	}
	if _, err := os.Stat(p.InstallDir); err == nil && !r.elevated {
		if problem, _ := winsys.PathProblem(p.InstallDir); problem == "" {
			return fail2("%s was installed from an elevated prompt and only Administrators can change it. Re-run elevated.", p.InstallDir)
		}
	}
	old, exists, err := config.ReadBackend(r.dir("backend.json"))
	if err != nil {
		return err
	}
	if exists {
		r.old = &old
		r.keep(old)
	}
	p.HubURL = strings.TrimRight(p.HubURL, "/")
	if p.HubURL == "" {
		return fail2("--hub-url is required, e.g. --hub-url ws://orchestrator:4500")
	}
	if !hubURLRE.MatchString(p.HubURL) {
		return fail2("--hub-url must be a ws:// or wss:// URL, got '%s'", p.HubURL)
	}
	if p.Name == "" {
		host, _ := os.Hostname()
		p.Name = protocol.SanitizeName(host)
	} else if !protocol.NameRE.MatchString(p.Name) {
		return fail2("--name may only use A-Z a-z 0-9 . _ - (1-128 chars), got '%s'", p.Name)
	}
	if p.Slots < 1 || p.Slots > protocol.MaxSlots {
		return fail2("--slots must be between 1 and %d, got %d", protocol.MaxSlots, p.Slots)
	}
	if p.BasePort < 1 || p.BasePort+p.Slots-1 > 65535 {
		return fail2("--base-port %d leaves no room for %d slots", p.BasePort, p.Slots)
	}
	// browsers\chromium_headless_shell-NNNN\chrome-headless-shell-win64\chrome-headless-shell.exe adds about
	// 95 characters, and Windows can't start an exe whose path is over MAX_PATH (260): run-server would look
	// healthy but every session would fail with "spawn ... ENOENT".
	if len(p.InstallDir) > 150 {
		r.log.Warnf("--install-dir is %d characters long; browser paths under it may exceed 260 characters and fail to launch. Use a shorter path.", len(p.InstallDir))
	}
	if _, _, err := runtime.PlaywrightPin(p.PlaywrightVersion, p.Source.NPMRegistry, p.Source.PlaywrightIntegrity); err != nil {
		return fail2("%v", err)
	}
	r.probeHub(ctx)
	r.log.Infof("Node '%s', %d slots on 127.0.0.1:%d-%d, hub %s", p.Name, p.Slots, p.BasePort, p.BasePort+p.Slots-1, p.HubURL)
	return nil
}

// keep applies the re-run rules: settings that are not passed again keep their installed value.
func (r *run) keep(old config.Backend) {
	p := &r.p
	if !p.Bound["hub-url"] && old.HubURL != "" {
		p.HubURL = old.HubURL
	}
	oldSlots := old.Slots
	if oldSlots == 0 && old.LegacyPortCount != nil {
		oldSlots = *old.LegacyPortCount
	}
	type setting struct {
		flag, key string
		was, now  string
		apply     func()
	}
	var reportURL string
	if old.ReportURL != nil {
		reportURL = *old.ReportURL
	}
	cur := func(s *string) string {
		if s == nil {
			return ""
		}
		return *s
	}
	settings := []setting{
		{"name", "nodeName", old.NodeName, p.Name, func() { p.Name = old.NodeName }},
		{"slots", "slots", itoa(oldSlots), itoa(p.Slots), func() {
			if oldSlots > protocol.MaxSlots {
				r.log.Warnf("The old install ran %d servers; a node has at most 64 slots, so it gets 64.", oldSlots)
				oldSlots = protocol.MaxSlots
			}
			p.Slots = oldSlots
		}},
		{"base-port", "basePort", itoa(old.BasePort), itoa(p.BasePort), func() { p.BasePort = old.BasePort }},
		{"browsers", "browsers", strings.Join(old.Browsers, ","), strings.Join(p.Browsers, ","), func() { p.Browsers = append([]string{}, old.Browsers...) }},
		{"report-url", "reportUrl", reportURL, cur(p.ReportURL), func() { v := reportURL; p.ReportURL = &v }},
		{"service-name", "serviceName", old.ServiceName, p.ServiceName, func() { p.ServiceName = old.ServiceName }},
		{"download-host", "downloadHost", cur(old.DownloadHost), cur(p.DownloadHost), func() { p.DownloadHost = old.DownloadHost }},
	}
	var kept []string
	for _, s := range settings {
		if s.was == "" || s.was == "0" {
			continue
		}
		if p.Bound[s.flag] {
			if s.was != s.now {
				r.log.Infof("Changing %s from '%s' to '%s'", s.key, s.was, s.now)
			}
			continue
		}
		s.apply()
		kept = append(kept, s.key+"="+s.was)
	}
	if len(kept) > 0 {
		r.log.Infof("Keeping the installed settings: %s", strings.Join(kept, " "))
	}
}

func itoa(n int) string {
	if n == 0 {
		return ""
	}
	return strconv.Itoa(n)
}

// probeHub warns when the hub's /healthz does not answer; the agent keeps retrying anyway.
func (r *run) probeHub(ctx context.Context) {
	u := "http://" + r.p.HubURL[len("ws://"):]
	if strings.HasPrefix(strings.ToLower(r.p.HubURL), "wss://") {
		u = "https://" + r.p.HubURL[len("wss://"):]
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u+"/healthz", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		r.log.Warnf("The hub does not answer at %s/healthz (%v); installing anyway, the agent keeps retrying", u, err)
		return
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		r.log.Warnf("The hub answered %s/healthz with HTTP %d; installing anyway", u, resp.StatusCode)
	}
}

var untrustedKeep = []string{"logs", "backend.json", "*.ps1", "*.psm1"}

func (r *run) stopExisting() error {
	p := r.p
	st, err := service.Query(p.ServiceName)
	if err != nil {
		return err
	}
	r.prev.ServiceExisted = st != service.NotInstalled
	r.prev.ServiceRunning = st == service.Running || st == service.StartPending
	r.prev.LegacyTask = DefaultServiceName
	if r.old != nil && r.old.TaskName != "" {
		r.prev.LegacyTask = r.old.TaskName
	}
	r.prev.LegacyTaskExists = taskExists(r.prev.LegacyTask)
	if r.prev.ServiceExisted {
		if err := service.Stop(p.ServiceName, 60*time.Second); err != nil {
			return fmt.Errorf("stopping service '%s': %w", p.ServiceName, err)
		}
		r.log.Infof("Stopped service '%s'", p.ServiceName)
	}
	n, supervisors, err := stopLegacy(p.InstallDir, r.prev.LegacyTask, r.prev.LegacyTaskExists)
	if err != nil {
		return err
	}
	r.prev.LegacySupervisor = supervisors > 0
	n += stopNoService(p.InstallDir)
	killed, err := winsys.KillByImagePrefix(p.InstallDir, 15*time.Second)
	if err != nil {
		return err
	}
	if n += len(killed); n > 0 {
		r.log.Infof("Stopped %d backend process(es)", n)
	}
	if p.NoService {
		if r.prev.ServiceExisted {
			if r.elevated {
				if err := service.Remove(p.ServiceName); err != nil {
					return err
				}
				r.log.Infof("Removed service '%s' (--no-service)", p.ServiceName)
				r.prev.ServiceExisted = false
			} else {
				r.log.Warnf("Service '%s' from an earlier install still exists; remove it elevated (pwrunner uninstall) or it starts a second agent at boot.", p.ServiceName)
			}
		}
		if r.prev.LegacyTaskExists {
			if r.elevated && deleteTask(r.prev.LegacyTask) == nil {
				r.log.Infof("Removed scheduled task '%s' (--no-service)", r.prev.LegacyTask)
				r.prev.LegacyTaskExists = false
			} else {
				r.log.Warnf("Scheduled task '%s' from an earlier install still exists; remove it elevated or it starts a second agent at boot.", r.prev.LegacyTask)
			}
		}
	}
	// A stale file would make the health check pass before the new agent has registered.
	os.Remove(r.dir("runtime.json"))
	if !r.elevated {
		return nil
	}
	// Before the downloads, so nothing can be added under the folder while they run.
	problem, err := winsys.PathProblem(p.InstallDir)
	if err != nil {
		return err
	}
	if problem == "" && p.ForceReset {
		problem = "is being reset (--force-reset)"
	}
	if problem != "" {
		r.log.Warnf("%s %s, so other users may have changed files the backend runs. Locking it and installing app\\ and browsers\\ from scratch.", p.InstallDir, problem)
		r.prev.Wiped = true // even half done, this leaves nothing worth restarting after a failure
		token, err := winsys.ResetUntrusted(p.InstallDir, untrustedKeep, r.log)
		if err != nil {
			return err
		}
		r.keptToken = token
		r.log.Redact(token)
		r.reopenLog()
	} else if err := winsys.Protect(p.InstallDir, true); err != nil {
		return err
	}
	r.log.Infof("Restricted %s to read-only for non-administrators", p.InstallDir)
	return nil
}

// stopNoService ends an agent a --no-service install started (pwrunner.pid).
func stopNoService(dir string) int {
	b, err := os.ReadFile(filepath.Join(dir, "pwrunner.pid"))
	if err != nil {
		return 0
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil || pid == os.Getpid() || !proc.Alive(pid) {
		return 0
	}
	if img, err := winsys.ImagePathOf(pid); err != nil || !winsys.UnderDir(img, dir) {
		return 0
	}
	proc.KillTree(pid)
	os.Remove(filepath.Join(dir, "pwrunner.pid"))
	return 1
}

func (r *run) nodePackages(ctx context.Context) error {
	if err := os.MkdirAll(r.dir("browsers"), 0o755); err != nil {
		return err
	}
	want, err := runtime.Want(r.p.NodeVersion, r.p.PlaywrightVersion, r.version, r.p.Source)
	if err != nil {
		return err
	}
	changed, err := runtime.LayDown(ctx, r.p.InstallDir, want, r.p.Source, r.log)
	r.laidDown = changed
	return err
}

func (r *run) installAgent(ctx context.Context) error {
	target := r.dir("pwrunner.exe")
	self, err := filepath.EvalSymlinks(r.p.Exe)
	if err != nil {
		self = r.p.Exe
	}
	if strings.EqualFold(filepath.Clean(self), target) {
		r.log.Infof("Running the installed copy; keeping %s", target)
	} else {
		old := r.dir("pwrunner.old.exe")
		os.Remove(old)
		if _, err := os.Stat(target); err == nil {
			// A running copy can't be overwritten, but it can be renamed; the next start deletes it.
			if err := os.Rename(target, old); err != nil {
				return fmt.Errorf("can't replace %s: %w", target, err)
			}
		}
		if err := copyFile(self, target); err != nil {
			return err
		}
		os.Remove(old)
		r.log.Infof("Installed pwrunner %s as %s", r.version, target)
	}
	sctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(sctx, target, "version")
	proc.Hide(cmd)
	out, err := cmd.Output()
	if err != nil || !strings.HasPrefix(string(out), "pwrunner ") {
		return fmt.Errorf("%s version failed: %v %s", target, err, strings.TrimSpace(string(out)))
	}
	if r.elevated {
		if err := winsys.Protect(target, true); err != nil {
			return err
		}
	}
	return r.moveLegacyFiles()
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

func (r *run) browsers(ctx context.Context) error {
	if r.p.SkipBrowsers {
		r.log.Infof("Skipping the browser download (--skip-browsers)")
		return nil
	}
	dest := r.dir("browsers")
	if r.p.BrowsersFrom != "" {
		r.log.Infof("Copying browsers from %s", r.p.BrowsersFrom)
		return copyTree(r.p.BrowsersFrom, dest)
	}
	r.log.Infof("Ensuring browsers: %s (already-downloaded ones are skipped)", strings.Join(r.p.Browsers, ", "))
	host := ""
	if r.p.DownloadHost != nil {
		host = *r.p.DownloadHost
	}
	return runtime.InstallBrowsers(ctx, runtime.NodeBinary(r.p.InstallDir), runtime.CLI(r.p.InstallDir), dest, r.p.Browsers, host, r.logWriter())
}

func copyTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if !d.Type().IsRegular() {
			return nil
		}
		return copyFile(path, target)
	})
}

func (r *run) writeConfig() error {
	p := r.p
	tokenFile := r.dir("agent.token")
	switch {
	case p.Token != nil && *p.Token != "":
		if err := winsys.WriteTokenFile(tokenFile, *p.Token); err != nil {
			return err
		}
		r.log.Infof("Stored the agent token in %s (readable by SYSTEM and Administrators only)", tokenFile)
	case p.Token != nil:
		os.Remove(tokenFile)
		r.log.Infof("Removed the agent token (--token \"\")")
	case r.keptToken != "":
		// Taken over from a folder other users could change: written to a new file nobody else can open.
		if err := winsys.WriteTokenFile(tokenFile, r.keptToken); err != nil {
			return err
		}
		r.log.Warnf("Re-created the installed agent token in a new file. Other users could have read the old one, so consider rotating it.")
	}
	if _, ok, _ := winsys.ReadTokenFile(tokenFile); !ok {
		r.log.Warnf("no agent token: the hub will reject this node if it sets GATEWAY_AGENT_TOKEN")
	}
	m, _, _ := runtime.ReadManifest(p.InstallDir)
	b := config.Backend{
		HubURL: p.HubURL, NodeName: p.Name, Slots: p.Slots, BasePort: p.BasePort, PlaywrightVersion: m.PlaywrightVersion,
		Browsers: p.Browsers, NodeExe: runtime.NodeBinary(p.InstallDir), NodeVersion: m.NodeVersion, PlaywrightCLI: runtime.CLI(p.InstallDir),
		BrowsersPath: r.dir("browsers"), ServiceName: p.ServiceName, ServiceAccount: "LocalSystem", ReportURL: p.ReportURL,
		InstallDir: p.InstallDir, DownloadHost: p.DownloadHost, RunnerVersion: r.version, InstalledAt: time.Now().UTC().Format("2006-01-02T15:04:05.000Z"),
	}
	if p.NoService {
		b.ServiceAccount = currentUser()
	}
	if r.old != nil {
		b.LogMaxBytes, b.LogKeep = r.old.LogMaxBytes, r.old.LogKeep
	}
	if err := config.WriteBackend(r.dir("backend.json"), b); err != nil {
		return err
	}
	written, _, err := config.ReadBackend(r.dir("backend.json"))
	r.cfg = &written
	return err
}

func currentUser() string {
	if u := os.Getenv("USERNAME"); u != "" {
		if d := os.Getenv("USERDOMAIN"); d != "" {
			return d + `\` + u
		}
		return u
	}
	return os.Getenv("USER")
}

func (r *run) firewall() {
	names := []string{r.p.ServiceName}
	if r.prev.LegacyTask != "" && !slices.Contains(names, r.prev.LegacyTask) {
		names = append(names, r.prev.LegacyTask)
	}
	for _, n := range names {
		rule := "Playwright Backend (" + n + ")"
		if !firewallRuleExists(rule) {
			continue
		}
		if !r.elevated {
			r.log.Warnf("Firewall rule '%s' from an older install is still open; remove it from an elevated prompt.", rule)
			continue
		}
		if err := deleteFirewallRule(rule); err != nil {
			r.log.Warnf("Could not remove firewall rule '%s': %v", rule, err)
			continue
		}
		r.log.Infof("Removed legacy inbound firewall rule '%s'", rule)
	}
}

func (r *run) start() error {
	p := r.p
	// From here on the previous backend is being replaced, so a failure no longer restarts it.
	r.prev.Replaced = true
	if r.prev.LegacyTaskExists {
		if r.elevated && deleteTask(r.prev.LegacyTask) == nil {
			r.log.Infof("Removed scheduled task '%s' (replaced by the '%s' service)", r.prev.LegacyTask, p.ServiceName)
		} else {
			r.log.Warnf("Scheduled task '%s' from an earlier install still exists; remove it elevated or it starts a second agent at boot.", r.prev.LegacyTask)
		}
	}
	exe := r.dir("pwrunner.exe")
	if p.NoService {
		pid, err := StartDetached(exe, "service", "--install-dir", p.InstallDir, "--console")
		if err != nil {
			return err
		}
		if err := os.WriteFile(r.dir("pwrunner.pid"), []byte(strconv.Itoa(pid)), 0o644); err != nil {
			return err
		}
		r.log.Infof("Started the agent in the background (pid %d; no service, it stops at logoff).", pid)
		return nil
	}
	cfg := service.Config{
		Name: p.ServiceName, DisplayName: "Playwright runner (" + p.ServiceName + ")", ExePath: exe, InstallDir: p.InstallDir,
		Description: fmt.Sprintf("Playwright gateway agent: node %s, %d slots, hub %s", p.Name, p.Slots, p.HubURL),
	}
	if err := service.Install(cfg); err != nil {
		return fmt.Errorf("registering service '%s': %w", p.ServiceName, err)
	}
	if err := service.Start(p.ServiceName, 60*time.Second); err != nil {
		return fmt.Errorf("starting service '%s': %w", p.ServiceName, err)
	}
	r.log.Infof("Registered and started service '%s'.", p.ServiceName)
	return nil
}

// restorePrevious starts the previous backend again after a failure that left it stopped.
func (r *run) restorePrevious() bool {
	if r.prev.Replaced || r.prev.Wiped {
		return false
	}
	switch {
	case r.prev.ServiceExisted && r.prev.ServiceRunning:
		if err := service.Start(r.p.ServiceName, 60*time.Second); err != nil {
			r.log.Warnf("Could not restart the previous install: %v", err)
			return false
		}
		r.log.Warnf("Restarted service '%s' so the previous install keeps serving.", r.p.ServiceName)
		return true
	case r.prev.LegacyTaskExists:
		if err := runTask(r.prev.LegacyTask); err != nil {
			r.log.Warnf("Could not restart the previous install: %v", err)
			return false
		}
		r.log.Warnf("Restarted scheduled task '%s' so the previous install keeps serving.", r.prev.LegacyTask)
		return true
	case r.prev.LegacySupervisor:
		runner := r.dir("run-backend.ps1")
		if _, err := os.Stat(runner); err != nil {
			return false
		}
		if _, err := StartDetached("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", runner); err != nil {
			r.log.Warnf("Could not restart the previous install: %v", err)
			return false
		}
		r.log.Warnf("Restarted the previous supervisor in the background so the previous install keeps serving.")
		return true
	}
	return false
}
