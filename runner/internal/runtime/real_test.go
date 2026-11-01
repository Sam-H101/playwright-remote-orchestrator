//go:build realruntime

package runtime

import (
	"context"
	"flag"
	"os"
	"testing"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
)

var realDir = flag.String("dir", "", "install folder to lay the pinned runtime down in")

// TestLayDownReal lays the pinned Node and playwright-core down for real:
//
//	go test -run TestLayDownReal -tags realruntime ./internal/runtime/ -args -dir <folder>
func TestLayDownReal(t *testing.T) {
	if *realDir == "" {
		t.Skip("pass -args -dir <folder>")
	}
	want, err := Want(NodeVersion, PlaywrightVersion, "realruntime-test", Source{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := LayDown(context.Background(), *realDir, want, Source{}, logging.New(os.Stderr, nil)); err != nil {
		t.Fatal(err)
	}
	if v, err := NodeVersionOf(context.Background(), NodeBinary(*realDir)); err != nil || v != "v"+NodeVersion {
		t.Fatal(v, err)
	}
	if v, err := CLIVersionOf(context.Background(), NodeBinary(*realDir), CLI(*realDir)); err != nil || v != "Version "+PlaywrightVersion {
		t.Fatal(v, err)
	}
}
