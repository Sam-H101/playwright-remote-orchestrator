// Package slot supervises one `playwright run-server` on 127.0.0.1: start, health probe, restart with
// backoff, stop (port of agent.js:39-186).
package slot

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

type Options struct {
	Index    int
	Port     int
	NodeExe  string
	CLI      string   // playwright-core cli.js
	Env      []string // the child's environment, NODE_OPTIONS included (see NodeOptions)
	LogDir   string   // "" -> the agent's log, prefixed "[slot i]"
	Timings  config.Timings
	OnHealth func(index int, healthy bool) // a health transition: the agent sends an urgent heartbeat
	OnChange func()                        // anything else runtime.json shows (pid, restarts)
	OnSlow   func(index int, delay time.Duration)
	Logger   *logging.Logger
}

type Status struct {
	Index, Port int
	PID         *int
	Healthy     bool
	Restarts    int
	Since       *time.Time
}

type Slot struct {
	o        Options
	mu       sync.Mutex
	pid      int
	healthy  bool
	restarts int
	since    *time.Time
	backoff  time.Duration
	stop     chan struct{}
	stopOnce sync.Once
	done     chan struct{}
}

func New(o Options) *Slot {
	return &Slot{o: o, backoff: ms(o.Timings.RestartBaseMs), stop: make(chan struct{}), done: make(chan struct{})}
}

func ms(n int) time.Duration { return time.Duration(n) * time.Millisecond }

// NodeOptions is NODE_OPTIONS for run-server: the original plus the watchdog preload (JSON-quoted path,
// as agent.js:52-62). The original goes into GATEWAY_AGENT_NODE_OPTIONS so the watchdog can restore it
// and browsers don't inherit the preload.
func NodeOptions(orig, watchdog string) (nodeOptions, stash string) {
	q, _ := jsonQuote(watchdog)
	return trimSpace(orig + " --require " + q), orig
}

func jsonQuote(s string) (string, error) {
	b := []byte{'"'}
	for _, r := range s {
		switch {
		case r == '"' || r == '\\':
			b = append(b, '\\', byte(r))
		case r < 0x20:
			b = append(b, fmt.Sprintf(`\u%04x`, r)...)
		default:
			b = append(b, string(r)...)
		}
	}
	return string(append(b, '"')), nil
}

func trimSpace(s string) string {
	for len(s) > 0 && (s[0] == ' ' || s[0] == '\t') {
		s = s[1:]
	}
	for len(s) > 0 && (s[len(s)-1] == ' ' || s[len(s)-1] == '\t') {
		s = s[:len(s)-1]
	}
	return s
}

// Status is a snapshot for runtime.json and heartbeats.
func (s *Slot) Status() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := Status{Index: s.o.Index, Port: s.o.Port, Healthy: s.healthy, Restarts: s.restarts, Since: s.since}
	if s.pid != 0 {
		pid := s.pid
		st.PID = &pid
	}
	return st
}

// Healthy is the slot's health as last probed.
func (s *Slot) Healthy() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.healthy
}

func (s *Slot) setHealthy(h bool) {
	s.mu.Lock()
	changed := s.healthy != h
	s.healthy = h
	s.mu.Unlock()
	if changed {
		s.o.Logger.Infof("slot %d: %s", s.o.Index, map[bool]string{true: "healthy", false: "unhealthy"}[h])
		if s.o.OnHealth != nil {
			s.o.OnHealth(s.o.Index, h)
		}
	}
}

func (s *Slot) changed() {
	if s.o.OnChange != nil {
		s.o.OnChange()
	}
}

// Run starts run-server and keeps it running until Stop (or ctx) ends it.
func (s *Slot) Run(ctx context.Context) {
	defer close(s.done)
	for {
		select {
		case <-s.stop:
			return
		case <-ctx.Done():
			return
		default:
		}
		started := time.Now()
		exit, cmd, err := s.start()
		var why string
		if err != nil {
			why = err.Error()
		} else {
			why = s.supervise(ctx, cmd, exit, started)
			if why == "" {
				return // stopped
			}
		}
		s.mu.Lock()
		s.pid, s.since = 0, nil
		if time.Since(started) >= ms(s.o.Timings.StableMs) {
			s.backoff = ms(s.o.Timings.RestartBaseMs)
		}
		delay := s.backoff
		s.backoff = min(s.backoff*2, ms(s.o.Timings.RestartMaxMs))
		s.restarts++
		s.mu.Unlock()
		s.setHealthy(false)
		s.o.Logger.Warnf("slot %d: run-server exited (%s); restarting in %ds", s.o.Index, why, int((delay+500*time.Millisecond)/time.Second))
		if delay > time.Minute && s.o.OnSlow != nil {
			s.o.OnSlow(s.o.Index, delay)
		}
		s.changed()
		select {
		case <-s.stop:
			return
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
	}
}

func (s *Slot) start() (<-chan error, *exec.Cmd, error) {
	cmd := exec.Command(s.o.NodeExe, s.o.CLI, "run-server", "--port", strconv.Itoa(s.o.Port), "--host", "127.0.0.1")
	cmd.Dir = filepath.Dir(s.o.CLI)
	cmd.Env = s.o.Env
	cmd.Stdin = nil
	proc.Hide(cmd)
	var file *os.File
	if s.o.LogDir != "" {
		f, err := openSlotLog(s.o.LogDir, s.o.Index)
		if err != nil {
			s.o.Logger.Warnf("slot %d: can't write %s (%v); logging to the agent log", s.o.Index, filepath.Join(s.o.LogDir, fmt.Sprintf("slot-%d.log", s.o.Index)), err)
		} else {
			file = f
		}
	}
	if file != nil {
		cmd.Stdout, cmd.Stderr = file, file
	} else {
		w := &prefixWriter{log: s.o.Logger, prefix: fmt.Sprintf("[slot %d] ", s.o.Index)}
		cmd.Stdout, cmd.Stderr = w, w
		cmd.WaitDelay = time.Second // browsers inheriting the pipe must not hold Wait
	}
	err := cmd.Start()
	if file != nil {
		file.Close()
	}
	if err != nil {
		return nil, nil, err
	}
	now := time.Now()
	s.mu.Lock()
	s.pid, s.since = cmd.Process.Pid, &now
	s.mu.Unlock()
	s.o.Logger.Infof("slot %d: started run-server on 127.0.0.1:%d (pid %d)", s.o.Index, s.o.Port, cmd.Process.Pid)
	s.changed()
	exit := make(chan error, 1)
	go func() { exit <- cmd.Wait() }()
	return exit, cmd, nil
}

// supervise probes the child until it exits (returns why) or the slot is stopped (returns "").
func (s *Slot) supervise(ctx context.Context, cmd *exec.Cmd, exit <-chan error, started time.Time) string {
	check := time.NewTicker(ms(s.o.Timings.HealthMs))
	defer check.Stop()
	warm := time.NewTicker(250 * time.Millisecond)
	defer warm.Stop()
	var exitErr error
	// probe reports true when the child died while the probe ran (exitErr then says how).
	probe := func() bool {
		ok := Probe(s.o.Port, 2*time.Second)
		select {
		case exitErr = <-exit:
			return true
		default:
			s.setHealthy(ok)
			return false
		}
	}
	for {
		select {
		case <-s.stop:
			s.kill(cmd, exit)
			return ""
		case <-ctx.Done():
			s.kill(cmd, exit)
			return ""
		case err := <-exit:
			return exitReason(err)
		case <-check.C:
			if probe() {
				return exitReason(exitErr)
			}
		case <-warm.C:
			if s.Healthy() || time.Since(started) > 30*time.Second {
				warm.Stop()
				continue
			}
			if probe() {
				return exitReason(exitErr)
			}
		}
	}
}

func exitReason(err error) string {
	if err == nil {
		return "code 0"
	}
	if ee, ok := err.(*exec.ExitError); ok {
		return "code " + strconv.Itoa(ee.ExitCode())
	}
	return err.Error()
}

// kill ends the run-server and its browsers at once (it has no graceful stop: it never reads stdin) and
// waits up to 8 s for it to go.
func (s *Slot) kill(cmd *exec.Cmd, exit <-chan error) {
	if err := proc.KillTree(cmd.Process.Pid); err != nil {
		cmd.Process.Kill()
	}
	select {
	case <-exit:
	case <-time.After(8 * time.Second):
		s.o.Logger.Warnf("slot %d: run-server (pid %d) did not exit within 8s", s.o.Index, cmd.Process.Pid)
	}
	s.mu.Lock()
	s.pid, s.since = 0, nil
	s.mu.Unlock()
	s.setHealthy(false)
}

// Stop cancels a pending restart, kills run-server and waits for Run to return.
func (s *Slot) Stop() {
	s.stopOnce.Do(func() { close(s.stop) })
	<-s.done
}

// Probe reports whether something accepts TCP connections on 127.0.0.1:port.
func Probe(port int, timeout time.Duration) bool {
	c, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), timeout)
	if err != nil {
		return false
	}
	c.Close()
	return true
}

// openSlotLog opens <dir>/slot-<i>.log, keeping the previous run's output as .log.prev.
func openSlotLog(dir string, index int) (*os.File, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	file := filepath.Join(dir, fmt.Sprintf("slot-%d.log", index))
	if _, err := os.Stat(file); err == nil {
		os.Remove(file + ".prev")
		if err := os.Rename(file, file+".prev"); err != nil {
			return nil, err
		}
	}
	return os.Create(file)
}

// prefixWriter logs each complete line of a child's output.
type prefixWriter struct {
	log    *logging.Logger
	prefix string
	mu     sync.Mutex
	rest   []byte
}

func (w *prefixWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.rest = append(w.rest, p...)
	for {
		i := bytes.IndexByte(w.rest, '\n')
		if i < 0 {
			break
		}
		if line := strings.TrimRight(string(w.rest[:i]), "\r"); line != "" {
			w.log.Infof("%s%s", w.prefix, line)
		}
		w.rest = w.rest[i+1:]
	}
	return len(p), nil
}
