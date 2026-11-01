package report

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/status"
)

func TestProblemTable(t *testing.T) {
	le := "dial tcp: refused"
	ok := &status.File{PID: 9, Connected: true, Registered: true, Slots: []status.SlotStatus{{Index: 1, Healthy: true}, {Index: 2, Healthy: true}}}
	cases := []struct {
		f     *status.File
		alive bool
		want  string
	}{
		{nil, false, "the agent has not written runtime.json yet"},
		{&status.File{PID: 42}, false, "the agent (pid 42) is not running or has stopped updating runtime.json"},
		{&status.File{PID: 1, LastError: &le}, true, "not connected to the hub: dial tcp: refused"},
		{&status.File{PID: 1, Connected: true}, true, "connected but not registered with the hub yet"},
		{&status.File{PID: 1, Connected: true, Registered: true, Slots: []status.SlotStatus{{Index: 1}}}, true, "the agent runs 1 slots, expected 2"},
		{&status.File{PID: 1, Connected: true, Registered: true, Slots: []status.SlotStatus{{Index: 1}, {Index: 2, Healthy: true}}}, true, "unhealthy slots: 1"},
	}
	for _, c := range cases {
		if got := Problem(c.f, c.alive, 2); got == nil || *got != c.want {
			t.Fatalf("want %q, got %v", c.want, got)
		}
	}
	if p := Problem(ok, true, 2); p != nil {
		t.Fatal(*p)
	}
}

func TestAgentAliveNeedsFreshUpdate(t *testing.T) {
	f := &status.File{PID: os.Getpid(), Updated: status.ISO(time.Now().Add(-time.Minute))}
	if AgentAlive(f, "") {
		t.Fatal("stale runtime.json must not count")
	}
	f.Updated = status.Now()
	if !AgentAlive(f, "") {
		t.Fatal("fresh and running")
	}
	if AgentAlive(f, filepath.Join(t.TempDir(), "elsewhere")) && os.PathSeparator == '\\' {
		t.Fatal("on Windows the writer must be <installDir>\\pwrunner.exe")
	}
}

func TestReportKeyOrderAndPost(t *testing.T) {
	cfg := &config.Backend{HubURL: "ws://h", NodeName: "n", Slots: 2, PlaywrightVersion: "1.64.1", NodeVersion: "22.23.4"}
	pid := 7
	h := Health{Alive: true, Runtime: &status.File{Name: "n", AgentVersion: "v", Connected: true, Registered: true,
		Slots: []status.SlotStatus{{Index: 1, Port: 3100, PID: &pid, Healthy: true}, {Index: 2, Port: 3101}}}}
	r := Build(cfg, h, "degraded", "1.1.0", 1234*time.Millisecond)
	steps := NewSteps()
	steps.Set("preflight", 0.1)
	steps.Set("stop existing backend", 1.5)
	r.Steps = steps
	path := filepath.Join(t.TempDir(), "status.json")
	if err := WriteStatusJSON(path, r); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	keys := regexp.MustCompile(`(?m)^  "(\w+)"`).FindAllStringSubmatch(string(raw), -1)
	var got []string
	for _, k := range keys {
		got = append(got, k[1])
	}
	want := "node,hubUrl,result,playwrightVersion,agentVersion,connected,registered,problem,lastError,healthySlots,totalSlots,slots,failedStep,error,durationSec,steps,timestamp,runnerVersion,nodeVersion"
	if strings.Join(got, ",") != want {
		t.Fatalf("keys %v", got)
	}
	if !strings.Contains(string(raw), `"preflight": 0.1,`) || strings.Index(string(raw), "preflight") > strings.Index(string(raw), "stop existing") {
		t.Fatalf("steps must keep their order:\n%s", raw)
	}
	if r.HealthySlots != 1 || r.TotalSlots != 2 || r.DurationSec != 1.2 {
		t.Fatalf("%+v", r)
	}
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		b, _ := io.ReadAll(req.Body)
		json.Unmarshal(b, &body)
	}))
	defer srv.Close()
	if err := Post(t.Context(), srv.URL, r); err != nil || body["result"] != "degraded" || body["node"] != "n" {
		t.Fatal(err, body)
	}
}
