package proc

import (
	"runtime"
	"strings"
	"testing"
)

func TestChildEnvAllowList(t *testing.T) {
	environ := []string{
		"PATH=/bin", "TEMP=/t", "NODE_OPTIONS=--inspect", "BROWSERS=firefox", "EPHEMERAL=1", "HUB_URL=ws://x",
		"GATEWAY_AGENT_TOKEN=s3cret", "PW_VERSION_OVERRIDE=1", "HTTPS_PROXY=http://p:3128", "no_proxy=localhost",
		"PLAYWRIGHT_BROWSERS_PATH=/old",
	}
	env := ChildEnv(environ, map[string]string{"PLAYWRIGHT_BROWSERS_PATH": "/new", "NODE_OPTIONS": "--require w.js", "TEMP": ""})
	joined := strings.Join(env, "\n")
	for _, banned := range []string{"BROWSERS=firefox", "EPHEMERAL", "HUB_URL", "GATEWAY_AGENT_TOKEN", "s3cret", "PW_VERSION_OVERRIDE", "--inspect", "/old", "TEMP="} {
		if strings.Contains(joined, banned) {
			t.Fatalf("%q leaked into the child env:\n%s", banned, joined)
		}
	}
	for _, want := range []string{"PATH=/bin", "HTTPS_PROXY=http://p:3128", "no_proxy=localhost", "PLAYWRIGHT_BROWSERS_PATH=/new", "NODE_OPTIONS=--require w.js"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("%q missing:\n%s", want, joined)
		}
	}
}

func TestChildEnvCaseOnWindows(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("case-insensitive names are a Windows thing")
	}
	env := ChildEnv([]string{"Path=C:\\x", "SYSTEMROOT=C:\\Windows"}, nil)
	if strings.Join(env, ";") != `Path=C:\x;SYSTEMROOT=C:\Windows` {
		t.Fatal(env)
	}
	if LookupEnv([]string{"Path=a"}, "PATH") != "a" {
		t.Fatal("lookup")
	}
}
