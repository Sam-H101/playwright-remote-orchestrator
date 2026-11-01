package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

// Backend is backend.json in the install dir; field order is the file's order. It is a superset of
// what install-backend.ps1 writes, which operators' scripts and backend-status.ps1 parse.
type Backend struct {
	HubURL            string   `json:"hubUrl"`
	NodeName          string   `json:"nodeName"`
	Slots             int      `json:"slots"`
	BasePort          int      `json:"basePort"`
	PlaywrightVersion string   `json:"playwrightVersion"`
	Browsers          []string `json:"browsers"`
	NodeExe           string   `json:"nodeExe"`
	NodeVersion       string   `json:"nodeVersion"`
	PlaywrightCLI     string   `json:"playwrightCli"`
	BrowsersPath      string   `json:"browsersPath"`
	TaskName          string   `json:"taskName,omitempty"` // legacy scheduled task name: read, never written
	ServiceName       string   `json:"serviceName"`
	ServiceAccount    string   `json:"serviceAccount"`
	ReportURL         *string  `json:"reportUrl"`
	InstallDir        string   `json:"installDir"`
	DownloadHost      *string  `json:"downloadHost"`
	Ephemeral         bool     `json:"ephemeral"`
	LogMaxBytes       int64    `json:"logMaxBytes"`
	LogKeep           int      `json:"logKeep"`
	RunnerVersion     string   `json:"runnerVersion"`
	InstalledAt       string   `json:"installedAt"`
	LegacyPortCount   *int     `json:"portCount,omitempty"` // read, never written
}

const (
	DefaultLogMaxBytes = 10 << 20
	DefaultLogKeep     = 5
)

// ReadBackend reads backend.json; exists is false when there is none. The legacy file may carry a
// UTF-8 BOM (Windows PowerShell's Set-Content -Encoding UTF8).
func ReadBackend(path string) (b Backend, exists bool, err error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return Backend{}, false, nil
	}
	if err != nil {
		return Backend{}, true, err
	}
	raw = bytes.TrimPrefix(raw, []byte("\xef\xbb\xbf"))
	if err := json.Unmarshal(raw, &b); err != nil {
		return Backend{}, true, fmt.Errorf("%s: %w", path, err)
	}
	return b, true, nil
}

// WriteBackend writes backend.json as ASCII 2-space JSON with a trailing newline, atomically. The
// read-only legacy fields are dropped.
func WriteBackend(path string, b Backend) error {
	b.TaskName = ""
	b.LegacyPortCount = nil
	if b.LogMaxBytes == 0 {
		b.LogMaxBytes = DefaultLogMaxBytes
	}
	if b.LogKeep == 0 {
		b.LogKeep = DefaultLogKeep
	}
	if b.Browsers == nil {
		b.Browsers = []string{}
	}
	out, err := MarshalASCII(b)
	if err != nil {
		return err
	}
	return WriteFileAtomic(path, out)
}

// MarshalASCII is MarshalIndent with two spaces, every non-ASCII character escaped, and a newline.
func MarshalASCII(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	var out strings.Builder
	for _, r := range buf.String() {
		if r < 0x80 {
			out.WriteRune(r)
		} else if r < 0x10000 {
			fmt.Fprintf(&out, `\u%04x`, r)
		} else {
			r -= 0x10000
			fmt.Fprintf(&out, `\u%04x\u%04x`, 0xD800+(r>>10), 0xDC00+(r&0x3FF))
		}
	}
	return []byte(out.String()), nil
}

// WriteFileAtomic writes <path>.<pid>.tmp and renames it over path.
func WriteFileAtomic(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + "." + strconv.Itoa(os.Getpid()) + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}

// value is a backend.json setting as a `run` flag value.
func (b Backend) value(flag string) string {
	switch flag {
	case "hub":
		return b.HubURL
	case "name":
		return b.NodeName
	case "slots":
		if b.Slots > 0 {
			return strconv.Itoa(b.Slots)
		}
	case "base-port":
		if b.BasePort > 0 {
			return strconv.Itoa(b.BasePort)
		}
	case "browsers":
		return strings.Join(b.Browsers, ",")
	case "playwright-cli":
		return b.PlaywrightCLI
	case "node":
		return b.NodeExe
	case "ephemeral":
		if b.Ephemeral {
			return "true"
		}
	}
	return ""
}

// AgentSettings is what the service runs: everything from backend.json and the install dir, nothing
// from the environment, so machine-wide variables cannot redirect the node. The token is loaded
// separately from agent.token.
func (b Backend) AgentSettings(installDir string) Settings {
	nodeExe := filepath.Join(installDir, "runtime", "node", "node.exe")
	if os.PathSeparator != '\\' {
		nodeExe = filepath.Join(installDir, "runtime", "node", "node")
	}
	s := Settings{
		Hub:           strings.TrimRight(b.HubURL, "/"),
		Name:          strings.ToLower(b.NodeName),
		Slots:         b.Slots,
		BasePort:      b.BasePort,
		PlaywrightCLI: filepath.Join(installDir, "app", "node_modules", "playwright-core", "cli.js"),
		NodeExe:       nodeExe,
		Browsers:      append([]string{}, b.Browsers...),
		StatusFile:    filepath.Join(installDir, "runtime.json"),
		LogDir:        filepath.Join(installDir, "logs"),
		Watchdog:      filepath.Join(installDir, "app", "lib", "parent-watchdog.js"),
		Ephemeral:     b.Ephemeral,
		BrowsersPath:  filepath.Join(installDir, "browsers"),
		Timings:       DefaultTimings,
	}
	if b.DownloadHost != nil {
		s.DownloadHost = *b.DownloadHost
	}
	if len(s.Browsers) == 0 {
		s.Browsers = []string{"chromium"}
	}
	return s
}

// Validate checks the settings the way FromArgs does (for the service, whose settings come from a file).
func (s Settings) Validate() error {
	if !hubRE.MatchString(s.Hub) {
		return fmt.Errorf("hubUrl must be a ws:// or wss:// URL, got %q", s.Hub)
	}
	if !protocol.NameRE.MatchString(s.Name) {
		return fmt.Errorf("nodeName may only use A-Z a-z 0-9 . _ - (1-128 chars), got %q", s.Name)
	}
	if s.Slots < 1 || s.Slots > 64 {
		return fmt.Errorf("slots must be an integer between 1 and 64, got %d", s.Slots)
	}
	if s.BasePort < 1 || s.BasePort > 65535-s.Slots+1 {
		return fmt.Errorf("basePort must be an integer between 1 and %d, got %d", 65535-s.Slots+1, s.BasePort)
	}
	return nil
}
