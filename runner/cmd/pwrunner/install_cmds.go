package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/install"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/report"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/service"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

var installFlags = append([]cli.FlagSpec{
	installDirFlag,
	{Name: "hub-url", Arg: "<url>", Usage: "ws:// or wss:// address of the hub (kept on a re-run)"},
	{Name: "token", Arg: "<token>", Usage: "agent token; prefer --token-file or --token-stdin (\"\" deletes the installed one)"},
	{Name: "token-file", Arg: "<path>", Usage: "read the agent token from a file"},
	{Name: "token-stdin", Bool: true, Usage: "read the agent token from standard input"},
	{Name: "name", Arg: "<name>", Usage: "node name (default: host name; kept on a re-run)"},
	{Name: "slots", Arg: "<n>", Aliases: []string{"port-count"}, Usage: "run-server processes, 1-64 (default 4)"},
	{Name: "base-port", Arg: "<port>", Aliases: []string{"port-start"}, Usage: "slot i listens on 127.0.0.1:<base+i-1> (default 3100)"},
	{Name: "browsers", Arg: "<a,b>", Usage: "browsers to install (default chromium)"},
	{Name: "report-url", Arg: "<url>", Usage: "POST the install report there"},
	{Name: "health-timeout", Arg: "<sec>", Usage: "how long to wait for the node to serve (default 60)"},
	{Name: "service-name", Arg: "<name>", Usage: "Windows service name (default " + install.DefaultServiceName + ")"},
	{Name: "service-account", Arg: "<account>", Hidden: true},
	{Name: "no-service", Bool: true, Usage: "run the agent in the background for this session instead of as a service"},
	{Name: "download-host", Arg: "<url>", Usage: "PLAYWRIGHT_DOWNLOAD_HOST for the browser download"},
	{Name: "browsers-from", Arg: "<dir>", Usage: "copy browsers from a folder instead of downloading them"},
	{Name: "skip-browsers", Bool: true, Usage: "don't install browsers"},
	{Name: "force-reset", Bool: true, Usage: "reset the install folder as if other users could have changed it"},
	{Name: "json", Bool: true, Usage: "print the report as JSON"},
	{Name: "bind-host", Arg: "<x>", Hidden: true},
	{Name: "advertise-host", Arg: "<x>", Hidden: true},
	{Name: "no-firewall", Bool: true, Hidden: true},
	{Name: "install-node", Bool: true, Hidden: true},
}, sourceFlags...)

func installCmd(args []string) error {
	p, err := parse("install", args, installFlags)
	if err != nil || p.Help {
		return err
	}
	if p.Has("service-account") {
		return cli.Usagef("--service-account is not supported in this release; the service runs as LocalSystem")
	}
	get := func(k string) string { v, _ := p.Get(k); return v }
	ip := install.Params{
		InstallDir: installDir(p), HubURL: get("hub-url"), Name: get("name"), ServiceName: get("service-name"),
		NoService: p.Has("no-service"), Source: sourceFrom(p), BrowsersFrom: get("browsers-from"),
		SkipBrowsers: p.Has("skip-browsers"), ForceReset: p.Has("force-reset"), Bound: map[string]bool{},
		Slots: 4, BasePort: 3100, Browsers: []string{"chromium"}, HealthTimeout: 60 * time.Second,
	}
	for _, n := range p.Names() {
		ip.Bound[n] = true
	}
	for _, d := range []string{"bind-host", "advertise-host", "no-firewall", "install-node"} {
		if p.Has(d) {
			ip.Deprecated = append(ip.Deprecated, d)
		}
	}
	ip.NodeVersion, ip.PlaywrightVersion = versionsFrom(p)
	intFlag := func(name string, min, max int, into *int) error {
		v, ok := p.Get(name)
		if !ok {
			return nil
		}
		n, err := strconv.Atoi(strings.TrimSpace(v))
		if err != nil || n < min || n > max {
			return cli.Usagef("--%s must be an integer between %d and %d, got %q", name, min, max, v)
		}
		*into = n
		return nil
	}
	if err := intFlag("slots", 1, protocol.MaxSlots, &ip.Slots); err != nil {
		return err
	}
	if err := intFlag("base-port", 1, 65535, &ip.BasePort); err != nil {
		return err
	}
	secs := 60
	if err := intFlag("health-timeout", 0, 86400, &secs); err != nil {
		return err
	}
	ip.HealthTimeout = time.Duration(secs) * time.Second
	if v, ok := p.Get("browsers"); ok {
		ip.Browsers = nil
		for _, b := range strings.Split(v, ",") {
			if b = strings.TrimSpace(b); b != "" {
				ip.Browsers = append(ip.Browsers, b)
			}
		}
	}
	if v, ok := p.Get("report-url"); ok {
		ip.ReportURL = &v
	}
	if v, ok := p.Get("download-host"); ok {
		ip.DownloadHost = &v
	}
	switch {
	case p.Has("token-stdin"):
		b, err := io.ReadAll(os.Stdin)
		if err != nil {
			return err
		}
		t := strings.TrimSpace(string(bytes.TrimPrefix(b, []byte{0xEF, 0xBB, 0xBF})))
		ip.Token, ip.TokenSource = &t, "stdin"
	case p.Has("token-file"):
		t, _, err := winsys.ReadTokenFile(get("token-file"))
		if err != nil {
			return cli.Usagef("--token-file: %v", err)
		}
		ip.Token, ip.TokenSource = &t, "file"
	case p.Has("token"):
		t := get("token")
		ip.Token, ip.TokenSource = &t, "argv"
	}
	rep, code := install.Run(context.Background(), ip, version)
	if p.Has("json") {
		b, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(b))
	} else {
		install.PrintSummary(os.Stdout, rep, ip.InstallDir)
	}
	if code != 0 {
		return &cli.ExitError{Code: code}
	}
	return nil
}

func uninstallCmd(args []string) error {
	p, err := parse("uninstall", args, []cli.FlagSpec{installDirFlag,
		{Name: "remove-files", Bool: true, Usage: "also delete the install folder, browsers included"},
		{Name: "yes", Bool: true, Usage: "don't ask for confirmation"}})
	if err != nil || p.Help {
		return err
	}
	return install.Uninstall(context.Background(), installDir(p), install.UninstallOptions{
		RemoveFiles: p.Has("remove-files"), Yes: p.Has("yes"), Interactive: stdinIsTerminal(), In: os.Stdin,
	})
}

func readInstall(dir string) (config.Backend, error) {
	b, ok, err := config.ReadBackend(filepath.Join(dir, "backend.json"))
	if err != nil {
		return b, err
	}
	if !ok || b.HubURL == "" {
		return b, &cli.ExitError{Code: 1, Msg: fmt.Sprintf("No agent install (backend.json with a hubUrl) in %s.", dir)}
	}
	return b, nil
}

func statusCmd(args []string) error {
	p, err := parse("status", args, []cli.FlagSpec{installDirFlag,
		{Name: "json", Bool: true, Usage: "print the report as JSON"},
		{Name: "report-url", Arg: "<url>", Usage: "POST the report there (default: backend.json reportUrl)"},
		{Name: "health-timeout", Arg: "<sec>", Usage: "keep re-reading for up to this long while not healthy (default 0)"}})
	if err != nil || p.Help {
		return err
	}
	dir := installDir(p)
	cfg, err := readInstall(dir)
	if err != nil {
		return err
	}
	secs := 0
	if v, ok := p.Get("health-timeout"); ok {
		if secs, err = strconv.Atoi(v); err != nil || secs < 0 {
			return cli.Usagef("--health-timeout must be a number of seconds, got %q", v)
		}
	}
	started := time.Now()
	h := report.WaitHealthy(context.Background(), dir, cfg.Slots, time.Duration(secs)*time.Second)
	result := "success"
	if h.Problem != nil {
		result = "degraded"
	}
	rep := report.Build(&cfg, h, result, version, time.Since(started))
	url, _ := p.Get("report-url")
	if url == "" && cfg.ReportURL != nil {
		url = *cfg.ReportURL
	}
	if url != "" {
		if err := report.Post(context.Background(), url, rep); err != nil {
			fmt.Fprintf(os.Stderr, "Could not report status to %s: %v\n", url, err)
		}
	}
	if p.Has("json") {
		b, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(b))
	} else {
		report.PrintHuman(os.Stdout, rep)
	}
	if h.Problem != nil {
		return &cli.ExitError{Code: 2}
	}
	return nil
}

// controlCmd is start / stop / restart: the service, or the background agent of a --no-service install.
func controlCmd(command string, args []string) error {
	p, err := parse(command, args, []cli.FlagSpec{installDirFlag, {Name: "timeout", Arg: "<sec>", Usage: "how long to wait (default 30)"}})
	if err != nil || p.Help {
		return err
	}
	dir := installDir(p)
	cfg, err := readInstall(dir)
	if err != nil {
		return err
	}
	timeout := 30 * time.Second
	if v, ok := p.Get("timeout"); ok {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			return cli.Usagef("--timeout must be a number of seconds, got %q", v)
		}
		timeout = time.Duration(n) * time.Second
	}
	name := cfg.ServiceName
	if name == "" {
		name = install.DefaultServiceName
	}
	st, err := service.Query(name)
	if err != nil {
		return err
	}
	if st == service.NotInstalled {
		return controlBackground(command, dir)
	}
	if command != "start" {
		if err := service.Stop(name, timeout); err != nil {
			return err
		}
		fmt.Printf("Stopped service '%s'\n", name)
	}
	if command != "stop" {
		if err := service.Start(name, timeout); err != nil {
			return err
		}
		fmt.Printf("Started service '%s'\n", name)
	}
	return nil
}

func controlBackground(command, dir string) error {
	pidFile := filepath.Join(dir, "pwrunner.pid")
	if command != "start" {
		if b, err := os.ReadFile(pidFile); err == nil {
			if pid, err := strconv.Atoi(strings.TrimSpace(string(b))); err == nil && proc.Alive(pid) {
				if img, err := winsys.ImagePathOf(pid); err == nil && winsys.UnderDir(img, dir) {
					proc.KillTree(pid)
					fmt.Printf("Stopped the background agent (pid %d)\n", pid)
				}
			}
			os.Remove(pidFile)
		}
	}
	if command != "stop" {
		exe := filepath.Join(dir, "pwrunner.exe")
		pid, err := install.StartDetached(exe, "service", "--install-dir", dir, "--console")
		if err != nil {
			return err
		}
		if err := os.WriteFile(pidFile, []byte(strconv.Itoa(pid)), 0o644); err != nil {
			return err
		}
		fmt.Printf("Started the background agent (pid %d)\n", pid)
	}
	return nil
}

// tokenCmd is `pwrunner token set --stdin|--file <p> | clear | show-prefix`; set and clear restart the
// backend so it picks the change up.
func tokenCmd(args []string) error {
	const usage = "usage: pwrunner token set --stdin | set --file <path> | clear | show-prefix  [--install-dir <path>]"
	if len(args) == 0 || strings.HasPrefix(args[0], "-") {
		return cli.Usagef(usage)
	}
	sub := args[0]
	p, err := parse("token "+sub, args[1:], []cli.FlagSpec{installDirFlag,
		{Name: "stdin", Bool: true, Usage: "set: read the token from standard input"},
		{Name: "file", Arg: "<path>", Usage: "set: read the token from a file"}})
	if err != nil || p.Help {
		return err
	}
	dir := installDir(p)
	file := filepath.Join(dir, "agent.token")
	switch sub {
	case "show-prefix":
		t, ok, err := winsys.ReadTokenFile(file)
		if err != nil {
			return err
		}
		if !ok {
			fmt.Println("no agent token")
			return nil
		}
		fmt.Println(protocol.TokenLabel(t))
		return nil
	case "set":
		var t string
		switch {
		case p.Has("stdin"):
			b, err := io.ReadAll(os.Stdin)
			if err != nil {
				return err
			}
			t = strings.TrimSpace(string(bytes.TrimPrefix(b, []byte{0xEF, 0xBB, 0xBF})))
		case p.Has("file"):
			v, _ := p.Get("file")
			if t, _, err = winsys.ReadTokenFile(v); err != nil {
				return err
			}
		default:
			return cli.Usagef("token set needs --stdin or --file <path>")
		}
		if t == "" {
			return cli.Usagef("the token is empty; use pwrunner token clear to remove it")
		}
		if err := winsys.WriteTokenFile(file, t); err != nil {
			return err
		}
		fmt.Printf("Stored the agent token in %s (readable by SYSTEM and Administrators only)\n", file)
	case "clear":
		if err := os.Remove(file); err != nil && !os.IsNotExist(err) {
			return err
		}
		fmt.Println("Removed the agent token")
	default:
		return cli.Usagef(usage)
	}
	if _, err := readInstall(dir); err == nil {
		return controlCmd("restart", []string{"--install-dir", dir})
	}
	return nil
}

func stdinIsTerminal() bool {
	st, err := os.Stdin.Stat()
	return err == nil && st.Mode()&os.ModeCharDevice != 0
}
