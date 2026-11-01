package logging

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func TestLineFormatAndRedaction(t *testing.T) {
	var b bytes.Buffer
	l := New(&b, []string{"s3cret"})
	l.Infof("token is %s", "s3cret")
	l.Debugf("hidden")
	l.SetDebug(true)
	l.Debugf("shown")
	out := b.String()
	if strings.Contains(out, "s3cret") || !strings.Contains(out, "token is <redacted>") {
		t.Fatalf("redaction failed: %q", out)
	}
	if strings.Contains(out, "hidden") || !strings.Contains(out, "DEBUG shown") {
		t.Fatalf("debug gating: %q", out)
	}
	if !regexp.MustCompile(`^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] INFO token`).MatchString(out) {
		t.Fatalf("format: %q", out)
	}
}

func TestNilLoggerIsSafe(t *testing.T) {
	var l *Logger
	l.Infof("x")
	l.Redact("y")
}

func TestRotation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "logs", "agent.log")
	r, err := OpenRotating(path, 100, 2)
	if err != nil {
		t.Fatal(err)
	}
	line := strings.Repeat("x", 39) + "\n" // 40 bytes
	for i := 0; i < 12; i++ {
		fmt.Fprint(r, line)
	}
	r.Close()
	for _, name := range []string{"agent.log", "agent.log.1", "agent.log.2"} {
		st, err := os.Stat(filepath.Join(dir, "logs", name))
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if st.Size() > 100 {
			t.Fatalf("%s is %d bytes", name, st.Size())
		}
	}
	if _, err := os.Stat(path + ".3"); !os.IsNotExist(err) {
		t.Fatalf("keep=2 must not leave a .3 generation")
	}
}
