package status

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

func TestKeyOrderAndFormat(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sub", "runtime.json")
	w := NewWriter(path, nil)
	pid, since := 42, ISO(time.Date(2026, 1, 2, 3, 4, 5, 6e6, time.UTC))
	f := File{Name: "n", Hub: "ws://h", AgentVersion: "v", PlaywrightVersion: "1.64.1", PID: 7, Connected: true, Updated: Now(),
		Slots: []SlotStatus{{Index: 1, Port: 3100, PID: &pid, Healthy: true, Since: &since}, {Index: 2, Port: 3101}}}
	if err := w.Write(f); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	text := string(raw)
	keys := regexp.MustCompile(`(?m)^  "(\w+)"`).FindAllStringSubmatch(text, -1)
	var got []string
	for _, k := range keys {
		got = append(got, k[1])
	}
	if strings.Join(got, ",") != "name,hub,agentVersion,playwrightVersion,pid,connected,registered,lastError,updated,slots" {
		t.Fatalf("key order %v", got)
	}
	if !strings.Contains(text, `"index": 1,
      "port": 3100,
      "pid": 42,
      "healthy": true,
      "restarts": 0,
      "since": "2026-01-02T03:04:05.006Z"`) || !strings.Contains(text, `"pid": null`) || !strings.Contains(text, `"lastError": null`) || !strings.HasSuffix(text, "}\n") {
		t.Fatalf("file:\n%s", text)
	}
	if !regexp.MustCompile(`^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$`).MatchString(f.Updated) {
		t.Fatal(f.Updated)
	}
	var back File
	if err := json.Unmarshal(raw, &back); err != nil || back.Slots[0].PID == nil || *back.Slots[0].PID != 42 {
		t.Fatal(err)
	}
	matches, _ := filepath.Glob(filepath.Join(filepath.Dir(path), "*.tmp"))
	if len(matches) != 0 {
		t.Fatalf("tmp left behind: %v", matches)
	}
}
