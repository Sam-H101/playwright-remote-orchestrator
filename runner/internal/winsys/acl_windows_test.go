package winsys

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
)

func elevated(t *testing.T) {
	t.Helper()
	if ok, _ := IsElevated(); !ok {
		t.Skip("needs an elevated prompt")
	}
}

func run(t *testing.T, name string, args ...string) {
	t.Helper()
	if out, err := exec.Command(name, args...).CombinedOutput(); err != nil {
		t.Fatalf("%s %v: %v\n%s", name, args, err, out)
	}
}

func TestLockedDirAndPlantedWriter(t *testing.T) {
	elevated(t)
	dir := filepath.Join(t.TempDir(), "locked")
	if err := CreateLockedDir(dir, true); err != nil {
		t.Fatal(err)
	}
	if p, err := PathProblem(dir); err != nil || p != "" {
		t.Fatalf("fresh locked dir: %q %v", p, err)
	}
	run(t, "icacls", dir, "/grant", "*"+SIDUsers+":(OI)(CI)W")
	p, err := PathProblem(dir)
	if err != nil || !strings.HasPrefix(p, "is writable by ") || !strings.Contains(p, "Users") {
		t.Fatalf("planted writer: %q %v", p, err)
	}
	if err := Protect(dir, true); err != nil {
		t.Fatal(err)
	}
	if p, _ := PathProblem(dir); p != "" {
		t.Fatalf("after Protect: %q", p)
	}
	if p, _ := PathProblem(t.TempDir()); p == "" {
		t.Fatal("a plain temp dir inherits permissions and must be a problem")
	}
}

func TestResetUntrustedDeletesLinksNotTargets(t *testing.T) {
	elevated(t)
	root := t.TempDir()
	dir := filepath.Join(root, "install")
	target := filepath.Join(root, "outside")
	os.MkdirAll(filepath.Join(dir, "app", "node_modules"), 0o755)
	os.MkdirAll(target, 0o755)
	os.WriteFile(filepath.Join(target, "precious.txt"), []byte("keep me"), 0o644)
	os.WriteFile(filepath.Join(dir, "app", "node_modules", "planted.js"), []byte("evil"), 0o644)
	os.WriteFile(filepath.Join(dir, "backend.json"), []byte("{}"), 0o644)
	os.WriteFile(filepath.Join(dir, "install-backend.ps1"), []byte("#"), 0o644)
	os.MkdirAll(filepath.Join(dir, "logs"), 0o755)
	os.WriteFile(filepath.Join(dir, "agent.token"), []byte("tok\r\n"), 0o644)
	run(t, "cmd", "/c", "mklink", "/J", filepath.Join(dir, "junction"), target)
	run(t, "cmd", "/c", "mklink", "/J", filepath.Join(dir, "app", "deep-junction"), target)
	run(t, "icacls", dir, "/setowner", "*"+SIDUsers, "/T")

	token, err := ResetUntrusted(dir, []string{"logs", "backend.json", "*.ps1", "*.psm1"}, logging.Discard())
	if err != nil {
		t.Fatal(err)
	}
	if token != "tok" {
		t.Fatalf("token %q", token)
	}
	for _, gone := range []string{"app", "junction"} {
		if _, err := os.Lstat(filepath.Join(dir, gone)); err == nil {
			t.Fatalf("%s survived the reset", gone)
		}
	}
	for _, kept := range []string{"backend.json", "install-backend.ps1", "logs", "agent.token"} {
		if _, err := os.Stat(filepath.Join(dir, kept)); err != nil {
			t.Fatalf("%s was deleted", kept)
		}
	}
	if b, _ := os.ReadFile(filepath.Join(target, "precious.txt")); string(b) != "keep me" {
		t.Fatal("the reset followed a junction")
	}
	if p, _ := PathProblem(dir); p != "" {
		t.Fatalf("after reset: %q", p)
	}
}

func TestTokenFile(t *testing.T) {
	elevated(t)
	path := filepath.Join(t.TempDir(), "agent.token")
	os.WriteFile(path, []byte("old"), 0o644)
	if err := WriteTokenFile(path, "s3cret"); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(path); string(b) != "s3cret" {
		t.Fatalf("content %q (no BOM, no newline)", b)
	}
	if p, err := TokenFileProblem(path); err != nil || p != "" {
		t.Fatalf("token DACL: %q %v", p, err)
	}
	if tok, ok, _ := ReadTokenFile(path); !ok || tok != "s3cret" {
		t.Fatal(tok)
	}
	os.WriteFile(path+"2", []byte("\xef\xbb\xbf  t2 \n"), 0o644)
	if tok, ok, _ := ReadTokenFile(path + "2"); !ok || tok != "t2" {
		t.Fatalf("%q", tok)
	}
}

func TestKillOnCloseJob(t *testing.T) {
	job, err := NewKillOnCloseJob()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("cmd", "/c", "ping -n 60 127.0.0.1 >nul")
	cmd.SysProcAttr = &windowsSysProcAttr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	if err := job.Assign(cmd.Process.Pid); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { cmd.Wait(); close(done) }()
	job.Close()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		cmd.Process.Kill()
		t.Fatal("child survived the job's close")
	}
}

func TestKillByImagePrefixSparesOthers(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "decoy.exe")
	src, _ := exec.LookPath("ping.exe")
	b, _ := os.ReadFile(src)
	os.WriteFile(exe, b, 0o755)
	inside := exec.Command(exe, "-n", "60", "127.0.0.1")
	outside := exec.Command(src, "-n", "60", "127.0.0.1")
	for _, c := range []*exec.Cmd{inside, outside} {
		c.SysProcAttr = &windowsSysProcAttr
		if err := c.Start(); err != nil {
			t.Fatal(err)
		}
	}
	defer outside.Process.Kill()
	killed, err := KillByImagePrefix(dir, 5*time.Second)
	if err != nil || len(killed) != 1 || killed[0] != inside.Process.Pid {
		t.Fatalf("killed %v (inside %d) %v", killed, inside.Process.Pid, err)
	}
	inside.Wait()
	if img, err := ImagePathOf(outside.Process.Pid); err != nil || !strings.EqualFold(img, src) {
		t.Fatalf("the process outside the prefix was touched: %q %v", img, err)
	}
}
