package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/install"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/service"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

const defaultDir = `C:\PlaywrightBackend`

// platformChecks are the Windows install checks of `pwrunner selfcheck`.
func platformChecks(ctx context.Context, dir string, quick bool) []check {
	var out []check
	elevated, _ := winsys.IsElevated()
	out = append(out, check{name: "elevation", ok: elevated, warn: true, info: map[bool]string{true: "elevated", false: "not elevated: the ACL checks may be incomplete"}[elevated]})
	out = append(out, check{name: "path length", ok: len(dir) <= 150, warn: true, info: fmt.Sprintf("%s is %d characters (browser paths below it must stay under 260)", dir, len(dir))})
	if p, err := winsys.PathProblem(dir); err != nil || p != "" {
		out = append(out, check{name: "install folder ACL", info: fmt.Sprintf("%s %s %s", dir, p, errOrEmpty(err))})
	} else {
		out = append(out, check{name: "install folder ACL", ok: true, info: "only SYSTEM and Administrators can change it"})
	}
	token := filepath.Join(dir, "agent.token")
	if _, ok, _ := winsys.ReadTokenFile(token); !ok {
		out = append(out, check{name: "agent token", warn: true, info: "no agent.token: the hub rejects this node if it requires a token"})
	} else if p, err := winsys.TokenFileProblem(token); err != nil || p != "" {
		out = append(out, check{name: "agent token", info: fmt.Sprintf("agent.token %s %s", p, errOrEmpty(err))})
	} else {
		out = append(out, check{name: "agent token", ok: true, info: "readable by SYSTEM and Administrators only"})
	}
	b, ok, _ := config.ReadBackend(filepath.Join(dir, "backend.json"))
	if !ok {
		return append(out, check{name: "backend.json", info: "missing: not installed"})
	}
	name := b.ServiceName
	if name == "" {
		name = install.DefaultServiceName
	}
	st, _ := service.Query(name)
	out = append(out, check{name: "service", ok: st == service.Running, warn: st == service.Stopped, info: fmt.Sprintf("'%s' is %s", name, st)})
	for i := 0; i < b.Slots; i++ {
		addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(b.BasePort+i))
		if st == service.Running {
			c, err := net.DialTimeout("tcp", addr, 2*time.Second)
			if err == nil {
				c.Close()
			}
			out = append(out, check{name: fmt.Sprintf("slot %d", i+1), ok: err == nil, info: fmt.Sprintf("run-server on %s %s", addr, map[bool]string{true: "listening", false: "not listening"}[err == nil])})
			continue
		}
		l, err := net.Listen("tcp", addr)
		if err == nil {
			l.Close()
		}
		out = append(out, check{name: fmt.Sprintf("slot %d port", i+1), ok: err == nil, info: fmt.Sprintf("%s %s", addr, map[bool]string{true: "free", false: "in use by another program"}[err == nil])})
	}
	legacy := b.TaskName
	if legacy == "" {
		legacy = install.DefaultServiceName
	}
	if install.TaskExists(legacy) {
		out = append(out, check{name: "legacy scheduled task", info: fmt.Sprintf("'%s' still exists and would start a second agent at boot; re-run pwrunner install", legacy)})
	} else {
		out = append(out, check{name: "legacy scheduled task", ok: true, info: "none"})
	}
	if !quick {
		out = append(out, hubCheck(ctx, b.HubURL))
	}
	return out
}

func hubCheck(ctx context.Context, hub string) check {
	u := "http://" + strings.TrimPrefix(hub, "ws://")
	if strings.HasPrefix(strings.ToLower(hub), "wss://") {
		u = "https://" + hub[len("wss://"):]
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u+"/healthz", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return check{name: "hub", info: fmt.Sprintf("%s/healthz: %v", u, err)}
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return check{name: "hub", info: fmt.Sprintf("%s/healthz: HTTP %d", u, resp.StatusCode)}
	}
	return check{name: "hub", ok: true, info: u + "/healthz answers"}
}

func errOrEmpty(err error) string {
	if err == nil {
		return ""
	}
	return "(" + err.Error() + ")"
}
