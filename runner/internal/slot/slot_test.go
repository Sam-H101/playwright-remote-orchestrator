package slot

import (
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

// fixture finds the system node and the gateway's fake run-server; the test is skipped without them.
func fixture(t *testing.T) (node, cli string) {
	t.Helper()
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node is not on PATH")
	}
	cli, _ = filepath.Abs(filepath.Join("..", "..", "..", "gateway", "test", "fixtures", "fake-playwright", "cli.js"))
	if _, err := os.Stat(filepath.Join(filepath.Dir(cli), "..", "..", "..", "node_modules", "ws")); err != nil {
		t.Skip("run npm ci in gateway/ (the fake run-server needs ws)")
	}
	return node, cli
}

func freePort(t *testing.T) int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

func waitFor(t *testing.T, what string, timeout time.Duration, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ok() {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestSlotLifecycle(t *testing.T) {
	node, cli := fixture(t)
	port := freePort(t)
	logDir := t.TempDir()
	os.WriteFile(filepath.Join(logDir, "slot-1.log"), []byte("previous run\n"), 0o644)
	var mu sync.Mutex
	var transitions []bool
	timings := config.Timings{HealthMs: 100, RestartBaseMs: 200, RestartMaxMs: 1000, StableMs: 60000}
	s := New(Options{
		Index: 1, Port: port, NodeExe: node, CLI: cli, Env: proc.ChildEnv(os.Environ(), nil), LogDir: logDir, Timings: timings,
		Logger:   logging.Discard(),
		OnHealth: func(_ int, h bool) { mu.Lock(); transitions = append(transitions, h); mu.Unlock() },
	})
	done := make(chan struct{})
	go func() { s.Run(t.Context()); close(done) }()
	waitFor(t, "healthy", 30*time.Second, s.Healthy)
	first := s.Status()
	if first.PID == nil || first.Since == nil || first.Restarts != 0 || first.Port != port {
		t.Fatalf("%+v", first)
	}
	if prev, _ := os.ReadFile(filepath.Join(logDir, "slot-1.log.prev")); string(prev) != "previous run\n" {
		t.Fatalf(".prev rotation: %q", prev)
	}

	proc.KillTree(*first.PID)
	waitFor(t, "unhealthy after the kill", 10*time.Second, func() bool { return !s.Healthy() })
	waitFor(t, "restarted and healthy", 20*time.Second, func() bool { st := s.Status(); return st.Healthy && st.Restarts == 1 })
	second := s.Status()
	if *second.PID == *first.PID {
		t.Fatal("same pid after restart")
	}
	mu.Lock()
	if len(transitions) < 3 || !transitions[0] || transitions[1] || !transitions[2] {
		t.Fatalf("health transitions %v", transitions)
	}
	mu.Unlock()

	stopped := time.Now()
	s.Stop()
	<-done
	waitFor(t, "port bindable again", 8*time.Second, func() bool {
		l, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(port))
		if err != nil {
			return false
		}
		l.Close()
		return true
	})
	if time.Since(stopped) > 8*time.Second {
		t.Fatal("stop took too long")
	}
	if st := s.Status(); st.PID != nil || st.Healthy {
		t.Fatalf("after stop %+v", st)
	}
	if proc.Alive(*second.PID) {
		t.Fatal("run-server survived Stop")
	}
}

func TestNodeOptions(t *testing.T) {
	no, stash := NodeOptions("", `C:\Program Files\pw "x"\parent-watchdog.js`)
	if no != `--require "C:\\Program Files\\pw \"x\"\\parent-watchdog.js"` || stash != "" {
		t.Fatalf("%q %q", no, stash)
	}
	no, stash = NodeOptions("--max-old-space-size=4096", "/opt/w.js")
	if no != `--max-old-space-size=4096 --require "/opt/w.js"` || stash != "--max-old-space-size=4096" {
		t.Fatalf("%q %q", no, stash)
	}
}

func TestPrefixWriter(t *testing.T) {
	var b strings.Builder
	w := &prefixWriter{log: logging.New(&b, nil), prefix: "[slot 2] "}
	w.Write([]byte("Listening on ws://127.0.0.1:3100/\r\npart"))
	w.Write([]byte("ial\n\n"))
	out := b.String()
	if !strings.Contains(out, "INFO [slot 2] Listening on ws://127.0.0.1:3100/\n") || !strings.Contains(out, "INFO [slot 2] partial\n") || strings.Count(out, "\n") != 2 {
		t.Fatalf("%q", out)
	}
}
