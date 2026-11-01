package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
)

func ctx(t *testing.T, env map[string]string) Context {
	t.Helper()
	dir := t.TempDir()
	cliPath := filepath.Join(dir, "cli.js")
	os.WriteFile(cliPath, []byte("//"), 0o644)
	os.WriteFile(filepath.Join(dir, "package.json"), []byte(`{"version":"1.2.3"}`), 0o644)
	if env == nil {
		env = map[string]string{}
	}
	if _, ok := env["PLAYWRIGHT_CLI"]; !ok {
		env["PLAYWRIGHT_CLI"] = cliPath
	}
	return Context{Env: func(k string) string { return env[k] }, Hostname: "My PC (2)", Cwd: dir}
}

func mustFail(t *testing.T, args []string, c Context, want string) {
	t.Helper()
	_, _, err := FromArgs(args, c)
	var ee *cli.ExitError
	if !errors.As(err, &ee) || ee.Code != 2 {
		t.Fatalf("%v: want exit 2, got %v", args, err)
	}
	if !strings.Contains(err.Error(), want) {
		t.Fatalf("%v: %q does not contain %q", args, err, want)
	}
}

func TestPrecedenceAndDefaults(t *testing.T) {
	c := ctx(t, map[string]string{"HUB_URL": "ws://env:1", "SLOTS": "8", "NODE_NAME": "from-env"})
	s, _, err := FromArgs(nil, c)
	if err != nil {
		t.Fatal(err)
	}
	if s.Hub != "ws://env:1" || s.Slots != 8 || s.Name != "from-env" || s.BasePort != 3100 || strings.Join(s.Browsers, ",") != "chromium" || s.Token != "" {
		t.Fatalf("%+v", s)
	}
	s, _, err = FromArgs([]string{"--hub", "wss://flag/", "--slots=2", "--name", "Win-01", "--base-port", "4000", "--browsers", "chromium, firefox,", "--token", "t"}, c)
	if err != nil {
		t.Fatal(err)
	}
	if s.Hub != "wss://flag" || s.Slots != 2 || s.Name != "win-01" || s.BasePort != 4000 || strings.Join(s.Browsers, ",") != "chromium,firefox" || s.Token.Value() != "t" {
		t.Fatalf("%+v", s)
	}
	s, _, _ = FromArgs([]string{"--hub", "ws://h"}, ctx(t, nil))
	if s.Name != "my-pc--2-" {
		t.Fatalf("default name %q", s.Name)
	}
}

func TestConfigFile(t *testing.T) {
	c := ctx(t, map[string]string{"SLOTS": "3"})
	path := filepath.Join(c.Cwd, "backend.json")
	os.WriteFile(path, []byte("\xef\xbb\xbf"+`{"hubUrl":"ws://file:9","nodeName":"File-Node","slots":6,"basePort":5000,"browsers":["firefox"],"portCount":12}`), 0o644)
	s, _, err := FromArgs([]string{"--config", path, "--base-port", "6000"}, c)
	if err != nil {
		t.Fatal(err)
	}
	if s.Hub != "ws://file:9" || s.Name != "file-node" || s.Slots != 3 || s.BasePort != 6000 || s.Browsers[0] != "firefox" {
		t.Fatalf("flag > env > file broken: %+v", s)
	}
}

func TestErrors(t *testing.T) {
	c := ctx(t, map[string]string{"HUB_URL": "ws://h"})
	mustFail(t, []string{"--slots", "65"}, c, `--slots / SLOTS must be an integer between 1 and 64, got "65"`)
	mustFail(t, []string{"--slots", "1.5"}, c, "--slots / SLOTS")
	mustFail(t, []string{"--slots", "4", "--base-port", "65534"}, c, `--base-port / BASE_PORT must be an integer between 1 and 65532, got "65534"`)
	mustFail(t, []string{"--bogus", "1"}, c, `unknown argument "--bogus"`)
	mustFail(t, []string{"--name", "a b"}, c, `--name / NODE_NAME may only use A-Z a-z 0-9 . _ - (1-128 chars), got "a b"`)
	mustFail(t, []string{"--ephemeral=maybe"}, c, `--ephemeral / EPHEMERAL must be 1/true or 0/false, got "maybe"`)
	mustFail(t, []string{"--playwright-cli", "nope.js"}, c, "PLAYWRIGHT_CLI not found: ")
	mustFail(t, []string{"--hub"}, c, "--hub needs a value")
	mustFail(t, []string{"stray"}, c, `unknown argument "stray"`)
	mustFail(t, nil, ctx(t, map[string]string{"HUB_URL": "http://x"}), `--hub / HUB_URL must be a ws:// or wss:// URL, got "http://x"`)
	mustFail(t, []string{"--timings", "{"}, c, "--timings must be a JSON object")
}

func TestEphemeralTable(t *testing.T) {
	c := ctx(t, map[string]string{"HUB_URL": "ws://h"})
	for v, want := range map[string]bool{"1": true, "TRUE": true, " yes ": true, "on": true, "0": false, "false": false, "No": false, "off": false} {
		s, _, err := FromArgs([]string{"--ephemeral=" + v}, c)
		if err != nil || s.Ephemeral != want {
			t.Fatalf("%q: %v %v", v, s.Ephemeral, err)
		}
	}
	s, _, _ := FromArgs([]string{"--ephemeral"}, c)
	if !s.Ephemeral {
		t.Fatal("--ephemeral alone is true")
	}
}

func TestTimingsOverride(t *testing.T) {
	c := ctx(t, map[string]string{"HUB_URL": "ws://h"})
	s, _, err := FromArgs([]string{"--timings", `{"heartbeatMs":300,"hubSilenceMs":3000}`}, c)
	if err != nil {
		t.Fatal(err)
	}
	if s.Timings.HeartbeatMs != 300 || s.Timings.HubSilenceMs != 3000 || s.Timings.ReconnectMaxMs != DefaultTimings.ReconnectMaxMs {
		t.Fatalf("%+v", s.Timings)
	}
}

func TestSecretNeverFormats(t *testing.T) {
	s := Settings{Token: "very-secret"}
	for _, out := range []string{fmt.Sprint(s.Token), fmt.Sprintf("%v %+v %#v %s", s, s, s, s.Token)} {
		if strings.Contains(out, "very-secret") {
			t.Fatalf("token leaked: %s", out)
		}
	}
	if Secret("").String() != "<unset>" || s.Token.Value() != "very-secret" {
		t.Fatal("Secret")
	}
}

func TestPlaywrightVersion(t *testing.T) {
	c := ctx(t, nil)
	if v := PlaywrightVersion(c.Env("PLAYWRIGHT_CLI")); v != "1.2.3" {
		t.Fatal(v)
	}
	if PlaywrightVersion(filepath.Join(t.TempDir(), "cli.js")) != "unknown" {
		t.Fatal("missing package.json")
	}
}

func TestBackendRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "backend.json")
	rep := "http://r/é"
	n := 12
	b := Backend{HubURL: "ws://h", NodeName: "n", Slots: 2, BasePort: 3100, Browsers: []string{"chromium"}, TaskName: "Old", ReportURL: &rep, LegacyPortCount: &n}
	if err := WriteBackend(path, b); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	text := string(raw)
	if strings.Contains(text, "taskName") || strings.Contains(text, "portCount") || !strings.Contains(text, `\`+"u00e9") || !strings.HasSuffix(text, "}\n") || !strings.HasPrefix(text, "{\n  \"hubUrl\"") {
		t.Fatalf("file:\n%s", text)
	}
	got, exists, err := ReadBackend(path)
	if err != nil || !exists || *got.ReportURL != rep || got.LogKeep != DefaultLogKeep || got.LogMaxBytes != DefaultLogMaxBytes {
		t.Fatalf("%+v %v %v", got, exists, err)
	}
	if _, exists, err := ReadBackend(filepath.Join(t.TempDir(), "none.json")); exists || err != nil {
		t.Fatal("missing file")
	}
}
