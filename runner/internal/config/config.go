// Package config builds the agent's Settings from flags, the environment and backend.json, with the
// names, defaults, validation and error texts of gateway/lib/config.js.
package config

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

// Timings are the agent's intervals in milliseconds (agent.js DEFAULT_TIMINGS).
type Timings struct {
	HeartbeatMs     int `json:"heartbeatMs"`
	HealthMs        int `json:"healthMs"`
	StatusMs        int `json:"statusMs"`
	RestartBaseMs   int `json:"restartBaseMs"`
	RestartMaxMs    int `json:"restartMaxMs"`
	StableMs        int `json:"stableMs"` // a run-server that stays up this long resets its restart backoff
	ReconnectBaseMs int `json:"reconnectBaseMs"`
	ReconnectMaxMs  int `json:"reconnectMaxMs"`
	HubSilenceMs    int `json:"hubSilenceMs"`    // no ping or frame from the hub for this long means the connection is dead
	ReplacedDelayMs int `json:"replacedDelayMs"` // after a 4409, so two agents with one name don't take turns kicking each other
	OpenDelayMs     int `json:"openDelayMs"`     // test hook: wait this long before dialling a data channel
}

var DefaultTimings = Timings{
	HeartbeatMs: 10000, HealthMs: 5000, StatusMs: 5000, RestartBaseMs: 5000, RestartMaxMs: 300000, StableMs: 60000,
	ReconnectBaseMs: 1000, ReconnectMaxMs: 30000, HubSilenceMs: 45000, ReplacedDelayMs: 30000,
}

// Secret is a token. It never formats as its value.
type Secret string

func (s Secret) String() string {
	if s == "" {
		return "<unset>"
	}
	return "<set>"
}

// GoString keeps %#v from printing the value too.
func (s Secret) GoString() string { return s.String() }

// Value is the token itself, for the Authorization header only.
func (s Secret) Value() string { return string(s) }

// Settings is what the agent runs with.
type Settings struct {
	Hub           string // ^wss?://[^/]+, trailing "/" stripped
	Token         Secret
	Name          string // canonical lower-case
	Slots         int
	BasePort      int
	PlaywrightCLI string
	NodeExe       string
	Browsers      []string
	StatusFile    string // "" = not written
	LogDir        string // "" = slot output to the agent's log, prefixed "[slot i]"
	Watchdog      string // parent-watchdog.js; "" = the caller writes the embedded copy
	Ephemeral     bool
	BrowsersPath  string // PLAYWRIGHT_BROWSERS_PATH for the run-servers; "" = the process's own value
	DownloadHost  string // PLAYWRIGHT_DOWNLOAD_HOST for browser installs; "" = the process's own value
	Timings       Timings
}

// RunFlags are the flags of `pwrunner run`; Env names the variable each one falls back to.
var RunFlags = []cli.FlagSpec{
	{Name: "hub", Arg: "<url>", Env: "HUB_URL", Usage: "hub to dial out to (required)"},
	{Name: "token", Arg: "<token>", Env: "GATEWAY_AGENT_TOKEN", Usage: `sent as "Authorization: Bearer <token>" (prefer the variable)`},
	{Name: "name", Arg: "<name>", Env: "NODE_NAME", Usage: "node name, case-insensitive (default: host name)"},
	{Name: "slots", Arg: "<n>", Env: "SLOTS", Usage: fmt.Sprintf("run-server processes, 1-%d (default 4)", protocol.MaxSlots)},
	{Name: "base-port", Arg: "<port>", Env: "BASE_PORT", Usage: "slot i listens on 127.0.0.1:<base+i-1> (default 3100)"},
	{Name: "playwright-cli", Arg: "<path>", Env: "PLAYWRIGHT_CLI", Usage: `playwright-core cli.js (default: app\node_modules\playwright-core\cli.js next to the exe)`},
	{Name: "node", Arg: "<path>", Env: "PWRUNNER_NODE", Usage: `node binary for the run-servers (default: runtime\node\node.exe next to the exe, else node on PATH)`},
	{Name: "browsers", Arg: "<list>", Env: "BROWSERS", Usage: "browsers installed here (default chromium)"},
	{Name: "status-file", Arg: "<path>", Env: "STATUS_FILE", Usage: "JSON status, rewritten every few seconds"},
	{Name: "log-dir", Arg: "<dir>", Env: "LOG_DIR", Usage: "run-server output goes to <dir>/slot-<i>.log"},
	{Name: "watchdog", Arg: "<path>", Env: "PWRUNNER_WATCHDOG", Usage: "parent-watchdog.js preloaded into run-server (default: the built-in copy)"},
	{Name: "ephemeral", Bool: true, Env: "EPHEMERAL=1", Usage: "the hub removes this node once it has been gone for EPHEMERAL_GRACE_S"},
	{Name: "config", Arg: "<backend.json>", Usage: "read defaults from an install's backend.json"},
	{Name: "log-level", Arg: "info|debug", Usage: "log verbosity"},
	{Name: "timings", Hidden: true},
}

var envOf = map[string]string{
	"hub": "HUB_URL", "token": "GATEWAY_AGENT_TOKEN", "name": "NODE_NAME", "slots": "SLOTS", "base-port": "BASE_PORT",
	"playwright-cli": "PLAYWRIGHT_CLI", "node": "PWRUNNER_NODE", "browsers": "BROWSERS", "status-file": "STATUS_FILE",
	"log-dir": "LOG_DIR", "watchdog": "PWRUNNER_WATCHDOG", "ephemeral": "EPHEMERAL",
}

// Context is what FromArgs reads besides the arguments.
type Context struct {
	Env      func(string) string
	Hostname string
	Cwd      string
	ExeDir   string // default locations of cli.js and node.exe
}

// DefaultContext reads the real process.
func DefaultContext() Context {
	host, _ := os.Hostname()
	cwd, _ := os.Getwd()
	exeDir := ""
	if exe, err := os.Executable(); err == nil {
		exeDir = filepath.Dir(exe)
	}
	return Context{Env: os.Getenv, Hostname: host, Cwd: cwd, ExeDir: exeDir}
}

var hubRE = regexp.MustCompile(`(?i)^wss?://[^/]+`)

// FromArgs parses `run` flags (without the command) into Settings. Errors are exit-2 errors with the
// texts of config.js.
func FromArgs(args []string, c Context) (Settings, cli.Parsed, error) {
	p, err := cli.Parse(append([]string{"run"}, args...), RunFlags)
	if err != nil || p.Help {
		return Settings{}, p, err
	}
	if len(p.Args) > 0 {
		return Settings{}, p, cli.Usagef("unknown argument %q", p.Args[0])
	}
	var file *Backend
	if path, ok := p.Get("config"); ok {
		b, exists, err := ReadBackend(abs(c.Cwd, path))
		if err != nil {
			return Settings{}, p, cli.Usagef("can't read %s: %v", path, err)
		}
		if !exists {
			return Settings{}, p, cli.Usagef("--config: %s not found", path)
		}
		file = &b
	}
	get := func(flag string) string {
		if v, ok := p.Get(flag); ok {
			return v
		}
		if v := c.Env(envOf[flag]); v != "" {
			return v
		}
		if file != nil {
			return file.value(flag)
		}
		return ""
	}
	s, err := build(get, c)
	if err != nil {
		return Settings{}, p, err
	}
	if v, ok := p.Get("timings"); ok && v != "" {
		if err := json.Unmarshal([]byte(v), &s.Timings); err != nil {
			return Settings{}, p, cli.Usagef("--timings must be a JSON object: %v", err)
		}
	}
	return s, p, nil
}

func build(get func(string) string, c Context) (Settings, error) {
	s := Settings{Timings: DefaultTimings}
	s.Hub = strings.TrimRight(get("hub"), "/")
	if !hubRE.MatchString(s.Hub) {
		return s, cli.Usagef("--hub / HUB_URL must be a ws:// or wss:// URL, got %q\n\n%s", s.Hub, cli.Usage("run", RunFlags))
	}
	s.Token = Secret(get("token"))
	name := get("name")
	if name == "" {
		name = protocol.SanitizeName(c.Hostname)
	}
	s.Name = strings.ToLower(name)
	if !protocol.NameRE.MatchString(s.Name) {
		return s, cli.Usagef("--name / NODE_NAME may only use A-Z a-z 0-9 . _ - (1-128 chars), got %q", s.Name)
	}
	var err error
	if s.Slots, err = intValue(get("slots"), 4, "--slots / SLOTS", 1, protocol.MaxSlots); err != nil {
		return s, err
	}
	if s.BasePort, err = intValue(get("base-port"), 3100, "--base-port / BASE_PORT", 1, 65535-s.Slots+1); err != nil {
		return s, err
	}
	if s.PlaywrightCLI, err = resolveCLI(get("playwright-cli"), c); err != nil {
		return s, err
	}
	s.NodeExe = resolveNode(get("node"), c)
	for _, b := range strings.Split(get("browsers"), ",") {
		if b = strings.TrimSpace(b); b != "" {
			s.Browsers = append(s.Browsers, b)
		}
	}
	if len(s.Browsers) == 0 {
		s.Browsers = []string{"chromium"}
	}
	if v := get("status-file"); v != "" {
		s.StatusFile = abs(c.Cwd, v)
	}
	if v := get("log-dir"); v != "" {
		s.LogDir = abs(c.Cwd, v)
	}
	if v := get("watchdog"); v != "" {
		s.Watchdog = abs(c.Cwd, v)
	}
	if s.Ephemeral, err = boolValue(get("ephemeral"), "--ephemeral / EPHEMERAL"); err != nil {
		return s, err
	}
	return s, nil
}

// intValue parses like config.js int(): Number(value) must be an integer in [min, max].
func intValue(v string, fallback int, name string, min, max int) (int, error) {
	if v == "" {
		return fallback, nil
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil || f != math.Trunc(f) || f < float64(min) || f > float64(max) {
		return 0, cli.Usagef("%s must be an integer between %d and %d, got %q", name, min, max, v)
	}
	return int(f), nil
}

func boolValue(v, name string) (bool, error) {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "":
		if v == "" {
			return false, nil
		}
	case "1", "true", "yes", "on":
		return true, nil
	case "0", "false", "no", "off":
		return false, nil
	}
	return false, cli.Usagef("%s must be 1/true or 0/false, got %q", name, v)
}

func abs(cwd, p string) string {
	if filepath.IsAbs(p) || cwd == "" {
		return filepath.Clean(p)
	}
	return filepath.Join(cwd, p)
}

func resolveCLI(v string, c Context) (string, error) {
	if v != "" {
		cli := abs(c.Cwd, v)
		if _, err := os.Stat(cli); err != nil {
			return "", usageErr("PLAYWRIGHT_CLI not found: " + cli)
		}
		return cli, nil
	}
	if c.ExeDir != "" {
		cli := filepath.Join(c.ExeDir, "app", "node_modules", "playwright-core", "cli.js")
		if _, err := os.Stat(cli); err == nil {
			return cli, nil
		}
	}
	return "", usageErr(`playwright-core not found next to pwrunner (app\node_modules\playwright-core\cli.js); install it or set --playwright-cli / PLAYWRIGHT_CLI`)
}

func usageErr(msg string) error { return &cli.ExitError{Code: 2, Msg: msg} }

func resolveNode(v string, c Context) string {
	if v != "" {
		if strings.ContainsAny(v, `/\`) {
			return abs(c.Cwd, v)
		}
		return v
	}
	if c.ExeDir != "" {
		name := "node"
		if os.PathSeparator == '\\' {
			name = "node.exe"
		}
		p := filepath.Join(c.ExeDir, "runtime", "node", name)
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return "node"
}

// PlaywrightVersion reads the version from the package.json next to cli.js, or "unknown".
func PlaywrightVersion(cliPath string) string {
	b, err := os.ReadFile(filepath.Join(filepath.Dir(cliPath), "package.json"))
	if err != nil {
		return "unknown"
	}
	var pkg struct {
		Version string `json:"version"`
	}
	if json.Unmarshal(b, &pkg) != nil || pkg.Version == "" {
		return "unknown"
	}
	return pkg.Version
}
