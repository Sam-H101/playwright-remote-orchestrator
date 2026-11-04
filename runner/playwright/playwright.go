// Package playwright is the playwright-core every runner serves. package.json here pins its version and
// package-lock.json holds its integrity; Dependabot updates both, and pwrunner, the runner image and the
// checks image's Playwright all follow them (README.md, "Upgrading Playwright").
package playwright

import (
	_ "embed"
	"encoding/json"
	"fmt"
)

//go:embed package-lock.json
var lockFile []byte

// Version and Integrity (the registry tarball's SRI) of the pinned playwright-core.
var Version, Integrity = mustPin(lockFile)

// pin reads playwright-core's version and integrity from a package-lock.json.
func pin(lock []byte) (version, integrity string, err error) {
	var l struct {
		Packages map[string]struct {
			Version   string `json:"version"`
			Integrity string `json:"integrity"`
		} `json:"packages"`
	}
	if err := json.Unmarshal(lock, &l); err != nil {
		return "", "", fmt.Errorf("package-lock.json: %w", err)
	}
	p := l.Packages["node_modules/playwright-core"]
	if p.Version == "" || p.Integrity == "" {
		return "", "", fmt.Errorf("package-lock.json has no playwright-core version and integrity")
	}
	return p.Version, p.Integrity, nil
}

func mustPin(lock []byte) (string, string) {
	v, sri, err := pin(lock)
	if err != nil {
		panic(err)
	}
	return v, sri
}
