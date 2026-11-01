// Package runtime acquires the private Node runtime and playwright-core that run the slots'
// `playwright run-server` processes: pinned by hash, embedded in the exe or downloaded.
package runtime

import (
	"fmt"
	"path"
	"strings"
)

// Pin is one artefact: where it comes from and its SHA-256. Size 0 means unknown.
type Pin struct {
	URL    string
	SHA256 string
	Size   int64
}

// File is the artefact's file name (the URL's last element).
func (p Pin) File() string { return path.Base(p.URL) }

// Update with `go run ./tools/updatepins --node <v> --playwright <v>`.
var NodeVersion = "22.23.4"

var NodePins = map[string]Pin{ // key "<goos>/<goarch>"
	"windows/amd64": {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-win-x64.zip", "cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20", 35604229},
	"windows/arm64": {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-win-arm64.zip", "a8b8cd79cbbb3062435f449591d98dd63730bbbf30ce2d7cc3a6376c236cb111", 31332011},
	"linux/amd64":   {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-x64.tar.gz", "b8492284c76a54baf58d61668dd0e41ee0fc98adabf9503d30f05d08f55b4406", 0},
	"linux/arm64":   {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-arm64.tar.gz", "f7a1e486dbc8b286847815b477d4fb5e9fb963ed4489c3e1588853d685f589f9", 0},
}

var PlaywrightVersion = "1.64.1"

var PlaywrightPins = map[string]string{ // version -> SRI integrity of the registry tarball
	"1.64.1": "sha512-BC4s8y6SEVA6Q1rSxZTto8hI1KUI3xXlHGZgvGqP4IqPdI/a6FX9H29IDNz7vKo9l21b6LfYosScPL6M3xxyDQ==",
}

// NodeEOL is the end-of-life date of each Node major line, for the selfcheck warning.
var NodeEOL = map[string]string{"22": "2027-04-30", "24": "2028-04-30"}

const (
	DefaultNodeMirror  = "https://nodejs.org/dist"
	DefaultNPMRegistry = "https://registry.npmjs.org"
)

// NodeArchive is the archive file name of a Node version for a platform.
func NodeArchive(version, goos, goarch string) (string, error) {
	arch := map[string]string{"amd64": "x64", "arm64": "arm64"}[goarch]
	if arch == "" {
		return "", fmt.Errorf("no Node build for %s/%s", goos, goarch)
	}
	switch goos {
	case "windows":
		return fmt.Sprintf("node-v%s-win-%s.zip", version, arch), nil
	case "linux":
		return fmt.Sprintf("node-v%s-linux-%s.tar.gz", version, arch), nil
	case "darwin":
		return fmt.Sprintf("node-v%s-darwin-%s.tar.gz", version, arch), nil
	}
	return "", fmt.Errorf("no Node build for %s/%s", goos, goarch)
}

// NodePin returns the pin for a Node version on a platform: the pinned one when the version is the
// pinned one, else one built from the mirror and an explicit SHA-256 (required).
func NodePin(version, goos, goarch, mirror, sha string) (Pin, error) {
	if mirror == "" {
		mirror = DefaultNodeMirror
	}
	mirror = strings.TrimRight(mirror, "/")
	file, err := NodeArchive(version, goos, goarch)
	if err != nil {
		return Pin{}, err
	}
	url := fmt.Sprintf("%s/v%s/%s", mirror, version, file)
	if p, ok := NodePins[goos+"/"+goarch]; ok && version == NodeVersion && sha == "" {
		p.URL = url
		return p, nil
	}
	if sha == "" {
		return Pin{}, fmt.Errorf("node %s is not pinned in this build; pass --node-sha256 with the SHA-256 of %s", version, file)
	}
	return Pin{URL: url, SHA256: strings.ToLower(sha)}, nil
}

// PlaywrightFile is the registry tarball name of a playwright-core version.
func PlaywrightFile(version string) string { return "playwright-core-" + version + ".tgz" }

// PlaywrightPin returns the tarball URL and SRI integrity of a playwright-core version.
func PlaywrightPin(version, registry, integrity string) (url, sri string, err error) {
	if registry == "" {
		registry = DefaultNPMRegistry
	}
	url = fmt.Sprintf("%s/playwright-core/-/%s", strings.TrimRight(registry, "/"), PlaywrightFile(version))
	if integrity != "" {
		return url, integrity, nil
	}
	if sri, ok := PlaywrightPins[version]; ok {
		return url, sri, nil
	}
	return "", "", fmt.Errorf("playwright-core %s is not pinned in this build; pass --playwright-integrity with its sha512 integrity", version)
}
