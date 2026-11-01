// Package status writes runtime.json, the agent's live status that the installer, `pwrunner status`
// and the Docker HEALTHCHECK read. Key order is part of the contract (gateway.test.js:212,221).
package status

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
)

type SlotStatus struct {
	Index    int     `json:"index"`
	Port     int     `json:"port"`
	PID      *int    `json:"pid"`
	Healthy  bool    `json:"healthy"`
	Restarts int     `json:"restarts"`
	Since    *string `json:"since"`
}

type File struct {
	Name              string       `json:"name"`
	Hub               string       `json:"hub"`
	AgentVersion      string       `json:"agentVersion"`
	PlaywrightVersion string       `json:"playwrightVersion"`
	PID               int          `json:"pid"`
	Connected         bool         `json:"connected"`
	Registered        bool         `json:"registered"`
	LastError         *string      `json:"lastError"`
	Updated           string       `json:"updated"`
	Slots             []SlotStatus `json:"slots"`
}

// ISO formats a time as JavaScript's toISOString does.
func ISO(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000Z") }

// Now is the current time in ISO form.
func Now() string { return ISO(time.Now()) }

// Writer writes the file atomically; readers never see a half-written file.
type Writer struct {
	path    string
	log     *logging.Logger
	mu      sync.Mutex
	lastErr string
}

func NewWriter(path string, log *logging.Logger) *Writer { return &Writer{path: path, log: log} }

// Write marshals with two-space indent and a trailing newline to <path>.<pid>.tmp, then renames it over
// the target. On Windows a reader holding the file open can refuse the rename; it is retried for 250 ms
// and otherwise left to the next write. A failure is logged once until it changes.
func (w *Writer) Write(f File) error {
	if w == nil || w.path == "" {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	data, err := json.MarshalIndent(f, "", "  ")
	if err == nil {
		err = w.write(append(data, '\n'))
	}
	if err != nil {
		if err.Error() != w.lastErr {
			w.log.Warnf("can't write status file %s: %v", w.path, err)
		}
		w.lastErr = err.Error()
		return err
	}
	w.lastErr = ""
	return nil
}

func (w *Writer) write(data []byte) error {
	if err := os.MkdirAll(filepath.Dir(w.path), 0o755); err != nil {
		return err
	}
	tmp := w.path + "." + strconv.Itoa(os.Getpid()) + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	var err error
	for i := 0; i < 5; i++ {
		if err = os.Rename(tmp, w.path); err == nil {
			return nil
		}
		time.Sleep(50 * time.Millisecond)
	}
	os.Remove(tmp)
	return err
}
