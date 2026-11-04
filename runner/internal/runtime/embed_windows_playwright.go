//go:build embedruntime && windows && (amd64 || arm64)

package runtime

import "embed"

// The playwright-core tarballs in files/. The build takes the pinned one by name, so a Dependabot update
// of runner/playwright needs no change here; fetchbundle leaves only that one, so the exe carries no others.
//
//go:embed files/playwright-core-*.tgz
var playwrightFiles embed.FS

// embeddedPlaywright is the pinned version's tarball. An exe built without it panics at start (pwrunner
// version included), rather than carrying a stale tarball that fails on a host.
var embeddedPlaywright = func() []byte {
	b, err := playwrightFiles.ReadFile("files/" + PlaywrightFile(PlaywrightVersion))
	if err != nil {
		panic("this embedruntime build has no internal/runtime/files/" + PlaywrightFile(PlaywrightVersion) + ": run go run ./tools/fetchbundle and build again")
	}
	return b
}()
