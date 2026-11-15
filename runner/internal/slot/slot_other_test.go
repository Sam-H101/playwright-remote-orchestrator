//go:build !windows

package slot

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

var endpointRE = regexp.MustCompile(`^/[0-9a-f]{64}$`)

// Each start gives run-server a new random endpoint path, in its environment only (never on the command
// line, which every user can read in /proc), and Path follows it; the log never shows it.
func TestEndpointPathPerStart(t *testing.T) {
	dir := t.TempDir()
	script := filepath.Join(dir, "cli.sh")
	// Stands in for cli.js: records its environment's path and its arguments, then waits to be killed.
	os.WriteFile(script, []byte(`n=$(ls "`+dir+`" | grep -c '^start-')
printf '%s\n' "$`+PathEnv+`" > "`+dir+`/start-$n.tmp"
printf '%s\n' "$*" >> "`+dir+`/start-$n.tmp"
echo "Listening on ws://127.0.0.1:1$`+PathEnv+`"
mv "`+dir+`/start-$n.tmp" "`+dir+`/start-$n"
exec sleep 60
`), 0o755)
	var b strings.Builder
	var mu sync.Mutex
	env := proc.ChildEnv(os.Environ(), map[string]string{PathEnv: "/must-be-replaced"})
	s := New(Options{
		Index: 1, Port: freePort(t), NodeExe: "/bin/sh", CLI: script, Env: append(env, PathEnv+"=/must-be-replaced-too"),
		Timings: config.Timings{HealthMs: 100, RestartBaseMs: 20, RestartMaxMs: 40, StableMs: 60000},
		Logger:  logging.New(&lockedWriter{w: &b, mu: &mu}, nil),
	})
	if s.Path() != "" {
		t.Fatal("no endpoint before run-server starts")
	}
	done := make(chan struct{})
	go func() { s.Run(t.Context()); close(done) }()
	read := func(n int) (path, args string) {
		var raw []byte
		waitFor(t, "start "+string(rune('0'+n)), 10*time.Second, func() bool {
			var err error
			raw, err = os.ReadFile(filepath.Join(dir, "start-"+string(rune('0'+n))))
			return err == nil
		})
		lines := strings.SplitN(strings.TrimSpace(string(raw)), "\n", 2)
		return lines[0], lines[1]
	}
	first, args := read(0)
	if !endpointRE.MatchString(first) {
		t.Fatalf("endpoint %q: want / and 64 hex digits", first)
	}
	if strings.Contains(args, first[1:]) || strings.Contains(args, "--path") {
		t.Fatalf("endpoint on the command line: %s", args)
	}
	waitFor(t, "Path", 5*time.Second, func() bool { return s.Path() == first })
	pid := *s.Status().PID
	proc.KillTree(pid)
	second, _ := read(1)
	if !endpointRE.MatchString(second) || second == first {
		t.Fatalf("restart: endpoint %q after %q; want a new one", second, first)
	}
	waitFor(t, "Path after the restart", 5*time.Second, func() bool { return s.Path() == second })
	s.Stop()
	<-done
	if s.Path() != "" {
		t.Fatal("endpoint kept after stop")
	}
	mu.Lock()
	defer mu.Unlock()
	if log := b.String(); strings.Contains(log, first[1:]) || strings.Contains(log, second[1:]) || strings.Count(log, "[slot 1] Listening on ws://127.0.0.1:1/<endpoint>\n") != 2 {
		t.Fatalf("endpoint in the log, or run-server's output lost:\n%s", log)
	}
}

func TestNewPath(t *testing.T) {
	a, err := NewPath()
	b, _ := NewPath()
	if err != nil || !endpointRE.MatchString(a) || a == b {
		t.Fatalf("%q %q %v", a, b, err)
	}
	env := withEnv([]string{"A=1", PathEnv + "=/old", "B=2"}, PathEnv, a)
	if strings.Join(env, " ") != "A=1 B=2 "+PathEnv+"="+a {
		t.Fatal(env)
	}
}
