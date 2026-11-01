// Package report builds the install/status report (status.json, the console summary, the POST to a
// report URL) from backend.json and the agent's runtime.json, as New-BackendReport did.
package report

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	goruntime "runtime"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/status"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

type Slot struct {
	Index    int  `json:"index"`
	Port     int  `json:"port"`
	Healthy  bool `json:"healthy"`
	Restarts int  `json:"restarts"`
	PID      *int `json:"pid"`
}

// Report is status.json; key order is the PowerShell installer's, then the pwrunner additions.
type Report struct {
	Node              string  `json:"node"`
	HubURL            *string `json:"hubUrl"`
	Result            string  `json:"result"`
	PlaywrightVersion *string `json:"playwrightVersion"`
	AgentVersion      *string `json:"agentVersion"`
	Connected         bool    `json:"connected"`
	Registered        bool    `json:"registered"`
	Problem           *string `json:"problem"`
	LastError         *string `json:"lastError"`
	HealthySlots      int     `json:"healthySlots"`
	TotalSlots        int     `json:"totalSlots"`
	Slots             []Slot  `json:"slots"`
	FailedStep        *string `json:"failedStep"`
	Error             *string `json:"error"`
	DurationSec       float64 `json:"durationSec"`
	Steps             *Steps  `json:"steps,omitempty"`
	Timestamp         string  `json:"timestamp"`
	RestartedPrevious *bool   `json:"restartedPrevious,omitempty"`
	RunnerVersion     string  `json:"runnerVersion"`
	NodeVersion       *string `json:"nodeVersion"`
}

// Steps are step durations in seconds, in the order they ran.
type Steps struct {
	names []string
	secs  map[string]float64
}

func NewSteps() *Steps { return &Steps{secs: map[string]float64{}} }

func (s *Steps) Set(name string, secs float64) {
	if _, ok := s.secs[name]; !ok {
		s.names = append(s.names, name)
	}
	s.secs[name] = secs
}

func (s *Steps) Names() []string             { return append([]string{}, s.names...) }
func (s *Steps) Seconds(name string) float64 { return s.secs[name] }

func (s *Steps) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, n := range s.names {
		if i > 0 {
			b.WriteByte(',')
		}
		k, _ := json.Marshal(n)
		v, _ := json.Marshal(s.secs[n])
		b.Write(k)
		b.WriteByte(':')
		b.Write(v)
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}

// ReadRuntime reads runtime.json; a file caught mid-rename is retried 3 times 100 ms apart.
func ReadRuntime(path string) (*status.File, error) {
	var last error
	for i := 0; i < 3; i++ {
		b, err := os.ReadFile(path)
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		if err == nil {
			var f status.File
			if err = json.Unmarshal(b, &f); err == nil {
				return &f, nil
			}
		}
		last = err
		time.Sleep(100 * time.Millisecond)
	}
	return nil, last
}

// AgentAlive: runtime.json outlives the agent, so it only counts while its writer runs (on Windows: as
// <installDir>\pwrunner.exe) and keeps it fresh (updated at most 30 s ago).
func AgentAlive(f *status.File, installDir string) bool {
	if f == nil || f.PID == 0 || !proc.Alive(f.PID) {
		return false
	}
	if goruntime.GOOS == "windows" && installDir != "" {
		img, err := winsys.ImagePathOf(f.PID)
		if err == nil && !strings.EqualFold(img, filepath.Join(installDir, "pwrunner.exe")) {
			return false
		}
	}
	t, err := time.Parse(time.RFC3339Nano, f.Updated)
	return err == nil && time.Since(t) <= 30*time.Second
}

// Problem says why the agent is not serving, or nil when it is connected, registered and every slot
// is healthy.
func Problem(f *status.File, alive bool, expectedSlots int) *string {
	p := func(format string, a ...any) *string { s := fmt.Sprintf(format, a...); return &s }
	switch {
	case f == nil:
		return p("the agent has not written runtime.json yet")
	case !alive:
		return p("the agent (pid %d) is not running or has stopped updating runtime.json", f.PID)
	case !f.Connected:
		le := ""
		if f.LastError != nil {
			le = *f.LastError
		}
		return p("not connected to the hub: %s", le)
	case !f.Registered:
		return p("connected but not registered with the hub yet")
	case expectedSlots > 0 && len(f.Slots) != expectedSlots:
		return p("the agent runs %d slots, expected %d", len(f.Slots), expectedSlots)
	}
	var down []string
	for _, s := range f.Slots {
		if !s.Healthy {
			down = append(down, fmt.Sprint(s.Index))
		}
	}
	if len(down) > 0 {
		return p("unhealthy slots: %s", strings.Join(down, ", "))
	}
	return nil
}

// Health is a runtime.json reading with its verdict.
type Health struct {
	Runtime *status.File
	Alive   bool
	Problem *string
}

// Check reads runtime.json once.
func Check(installDir string, slots int) Health {
	f, _ := ReadRuntime(filepath.Join(installDir, "runtime.json"))
	alive := AgentAlive(f, installDir)
	return Health{Runtime: f, Alive: alive, Problem: Problem(f, alive, slots)}
}

// WaitHealthy polls every 500 ms until the node is healthy or timeout passes.
func WaitHealthy(ctx context.Context, installDir string, slots int, timeout time.Duration) Health {
	deadline := time.Now().Add(timeout)
	for {
		h := Check(installDir, slots)
		if h.Problem == nil || !time.Now().Before(deadline) {
			return h
		}
		select {
		case <-ctx.Done():
			return h
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func strp(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// Build assembles the report. cfg may be nil (the install failed before writing it).
func Build(cfg *config.Backend, h Health, result, runnerVersion string, duration time.Duration) Report {
	r := Report{Result: result, Problem: h.Problem, DurationSec: round1(duration.Seconds()), Timestamp: time.Now().UTC().Format("2006-01-02T15:04:05.0000000Z"),
		RunnerVersion: runnerVersion, Slots: []Slot{}}
	r.Node, _ = os.Hostname()
	if cfg != nil {
		r.Node = cfg.NodeName
		r.HubURL = strp(cfg.HubURL)
		r.PlaywrightVersion = strp(cfg.PlaywrightVersion)
		r.NodeVersion = strp(cfg.NodeVersion)
		r.TotalSlots = cfg.Slots
	}
	if f := h.Runtime; f != nil {
		r.Node = f.Name
		r.AgentVersion = strp(f.AgentVersion)
		r.Connected = h.Alive && f.Connected
		r.Registered = h.Alive && f.Registered
		r.LastError = f.LastError
		for _, s := range f.Slots {
			sl := Slot{Index: s.Index, Port: s.Port, Healthy: h.Alive && s.Healthy, Restarts: s.Restarts, PID: s.PID}
			if sl.Healthy {
				r.HealthySlots++
			}
			r.Slots = append(r.Slots, sl)
		}
		if cfg == nil {
			r.TotalSlots = len(f.Slots)
		}
	}
	return r
}

func round1(f float64) float64 { return float64(int64(f*10+0.5)) / 10 }

// WriteStatusJSON writes status.json (ASCII, 2-space indent).
func WriteStatusJSON(path string, r Report) error {
	b, err := config.MarshalASCII(r)
	if err != nil {
		return err
	}
	return config.WriteFileAtomic(path, b)
}

// Post sends the report as JSON to url (15 s timeout).
func Post(ctx context.Context, url string, r Report) error {
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// PrintHuman prints the `status` view of a report.
func PrintHuman(w io.Writer, r Report) {
	fmt.Fprintf(w, "%s: %s (%d/%d slots healthy, hub %s, Playwright %s)\n", r.Node, strings.ToUpper(r.Result), r.HealthySlots, r.TotalSlots, deref(r.HubURL), deref(r.PlaywrightVersion))
	fmt.Fprintf(w, "  connected: %v  registered: %v\n", r.Connected, r.Registered)
	if r.Problem != nil {
		fmt.Fprintf(w, "  problem: %s\n", *r.Problem)
	}
	PrintSlots(w, r)
}

// PrintSlots prints the slot table.
func PrintSlots(w io.Writer, r Report) {
	if len(r.Slots) == 0 {
		return
	}
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "\nindex\tport\thealthy\trestarts\tpid")
	fmt.Fprintln(tw, "-----\t----\t-------\t--------\t---")
	for _, s := range r.Slots {
		pid := ""
		if s.PID != nil {
			pid = fmt.Sprint(*s.PID)
		}
		fmt.Fprintf(tw, "%d\t%d\t%v\t%d\t%s\n", s.Index, s.Port, s.Healthy, s.Restarts, pid)
	}
	tw.Flush()
}
