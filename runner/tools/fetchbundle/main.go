// Command fetchbundle downloads the pinned Node archives and the playwright-core tarball, verifies them
// against internal/runtime/pins.go (and runner/playwright, which pins playwright-core) and puts them in
// a folder: internal/runtime/files for the embedruntime build, or any folder for
// `pwrunner install --offline-dir`. In internal/runtime/files it also removes other playwright-core
// tarballs, which the build would otherwise embed too.
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
	embedDir := filepath.Join("internal", "runtime", "files")
	out := flag.String("out", embedDir, "destination folder")
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
	keep := runtime.PlaywrightFile(runtime.PlaywrightVersion)
	fetch(ctx, filepath.Join(*out, keep), url, v)
	// The embedruntime build embeds every files/playwright-core-*.tgz, so drop earlier pins' there. Leave
	// any other folder alone: it may be an offline folder kept for several versions.
	if !sameDir(*out, embedDir) {
		return
	}
	old, _ := filepath.Glob(filepath.Join(*out, runtime.PlaywrightFile("*")))
	for _, f := range old {
		if filepath.Base(f) != keep {
			if err := os.Remove(f); err != nil {
				fail(err)
			}
			fmt.Printf("removed %s (not the pinned version)\n", f)
		}
	}
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

func sameDir(a, b string) bool {
	sa, err := os.Stat(a)
	if err != nil {
		return false
	}
	sb, err := os.Stat(b)
	return err == nil && os.SameFile(sa, sb)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "fetchbundle:", err)
	os.Exit(1)
}
