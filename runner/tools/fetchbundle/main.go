// Command fetchbundle downloads the pinned Node archives and the playwright-core tarball, verifies them
// against internal/runtime/pins.go and puts them in a folder: internal/runtime/files for the
// embedruntime build, or any folder for `pwrunner install --offline-dir`.
//
//	go run ./tools/fetchbundle --out ./internal/runtime/files [--targets windows/amd64,windows/arm64]
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/runtime"
)

func main() {
	out := flag.String("out", filepath.Join("internal", "runtime", "files"), "destination folder")
	targets := flag.String("targets", "windows/amd64,windows/arm64", "comma-separated <goos>/<goarch> Node builds to fetch")
	flag.Parse()

	ctx := context.Background()
	for _, t := range strings.Split(*targets, ",") {
		goos, goarch, ok := strings.Cut(strings.TrimSpace(t), "/")
		if !ok {
			fail(fmt.Errorf("bad target %q", t))
		}
		pin, err := runtime.NodePin(runtime.NodeVersion, goos, goarch, "", "")
		if err != nil {
			fail(err)
		}
		fetch(ctx, filepath.Join(*out, pin.File()), pin.URL, runtime.NewSHA256(pin.SHA256))
	}
	url, sri, err := runtime.PlaywrightPin(runtime.PlaywrightVersion, "", "")
	if err != nil {
		fail(err)
	}
	v, err := runtime.NewSRI(sri)
	if err != nil {
		fail(err)
	}
	fetch(ctx, filepath.Join(*out, runtime.PlaywrightFile(runtime.PlaywrightVersion)), url, v)
}

func fetch(ctx context.Context, dest, url string, v *runtime.Verifier) {
	if _, err := os.Stat(dest); err == nil {
		if err := runtime.VerifyFile(dest, v); err == nil {
			fmt.Printf("ok      %s (already present)\n", dest)
			return
		}
		os.Remove(dest)
		v = v.Fresh()
	}
	if err := runtime.Download(ctx, url, dest, v); err != nil {
		fail(err)
	}
	fmt.Printf("fetched %s\n", dest)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "fetchbundle:", err)
	os.Exit(1)
}
