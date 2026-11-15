package agent

import (
	"errors"
	"os/exec"
	"strings"
	"testing"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/slot"
)

func TestRunServerNeverGetsTheAgentToken(t *testing.T) {
	t.Setenv("GATEWAY_AGENT_TOKEN", "secret-agent-token")
	t.Setenv("HOME", "/root")
	t.Setenv("USER", "root")
	t.Setenv(slot.PathEnv, "/inherited")
	s := config.Settings{Watchdog: "/opt/w.js", Slots: 2}
	env := childEnv(s, 1)
	if joined := strings.Join(env, "\n"); strings.Contains(joined, "secret-agent-token") || proc.LookupEnv(env, "HOME") != "/root" {
		t.Fatalf("own user:\n%s", joined)
	}
	if proc.LookupEnv(env, slot.PathEnv) != "" {
		t.Fatal("the endpoint path is the slot's to set at each start, never inherited")
	}
	s.RunAs = []config.RunAs{{Name: "pwuser1", UID: 1001, GID: 1001, Home: "/home/pwuser1"}, {Name: "pwuser2", UID: 1002, GID: 1002, Home: "/home/pwuser2"}}
	for i := 1; i <= 2; i++ {
		env = childEnv(s, i)
		if strings.Contains(strings.Join(env, "\n"), "secret-agent-token") {
			t.Fatal("token in the run-as env")
		}
		got := strings.Join([]string{proc.LookupEnv(env, "HOME"), proc.LookupEnv(env, "USER"), proc.LookupEnv(env, "LOGNAME")}, ",")
		u := s.RunAs[i-1]
		if got != u.Home+","+u.Name+","+u.Name {
			t.Fatalf("slot %d run-as env %v", i, got)
		}
	}
}

func TestRunServerAs(t *testing.T) {
	if runServerAs(config.Settings{Slots: 1}, 1) != nil {
		t.Fatal("no account: run-server starts as the agent's user")
	}
	var called []string
	launcher := func(name string) config.Launcher {
		return config.Launcher{Account: name, Prepare: func(*exec.Cmd) error { called = append(called, name); return nil }}
	}
	s := config.Settings{Slots: 3, Launchers: []config.Launcher{launcher("pwb-x-1"), launcher("pwb-x-2"), {}}}
	for i := 1; i <= 2; i++ {
		if err := runServerAs(s, i)(exec.Command("x")); err != nil {
			t.Fatalf("slot %d: %v", i, err)
		}
	}
	if strings.Join(called, ",") != "pwb-x-1,pwb-x-2" {
		t.Fatalf("each slot must use its own launcher: %v", called)
	}
	// A slot without an account is never started: not as the service's account, not as another slot's.
	for _, i := range []int{3, 4} {
		if err := runServerAs(s, i)(exec.Command("x")); err == nil || !strings.Contains(err.Error(), "no browser account") {
			t.Fatalf("slot %d: %v", i, err)
		}
	}
	if len(called) != 2 {
		t.Fatalf("another slot's launcher ran: %v", called)
	}
}

func TestRunAsSummary(t *testing.T) {
	one := config.RunAs{Name: "pwuser", UID: 1001}
	if got := runAsSummary(config.Settings{Slots: 2, RunAs: []config.RunAs{one, one}}); got != "pwuser (uid 1001)" {
		t.Fatal(got)
	}
	s := config.Settings{Slots: 2, RunAs: []config.RunAs{{Name: "pw1", UID: 2001}, {Name: "pw2", UID: 2002}}}
	if got := runAsSummary(s); got != "pw1 (uid 2001) in slot 1, pw2 (uid 2002) in slot 2" {
		t.Fatal(got)
	}
	fail := func(*exec.Cmd) error { return errors.New("x") }
	s = config.Settings{Slots: 3, Launchers: []config.Launcher{{Account: "pwb-a-1", Prepare: fail}, {Account: "pwb-a-2", Prepare: fail}}}
	if got := runAsSummary(s); got != "pwb-a-1 in slot 1, pwb-a-2 in slot 2, nobody (no browser account, so not started) in slot 3" {
		t.Fatal(got)
	}
}
