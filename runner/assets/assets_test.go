package assets

import (
	"bytes"
	"os"
	"testing"
)

func TestWatchdogMatchesGateway(t *testing.T) {
	src, err := os.ReadFile("../../gateway/lib/parent-watchdog.js")
	if err != nil {
		t.Skipf("gateway/lib not available: %v", err)
	}
	if !bytes.Equal(src, ParentWatchdogJS) {
		t.Fatal("update runner/assets/parent-watchdog.js from gateway/lib")
	}
}
