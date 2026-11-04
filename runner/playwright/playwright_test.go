package playwright

import (
	"encoding/json"
	"os"
	"regexp"
	"strings"
	"testing"
)

func TestPinMatchesPackageJSON(t *testing.T) {
	// Everything that isn't Go reads the version from package.json (Windows PowerShell 5.1 can't parse a
	// lock file, whose root package has an empty name), so the two must agree, and the pin must be exact.
	raw, err := os.ReadFile("package.json")
	if err != nil {
		t.Fatal(err)
	}
	var pkg struct {
		Dependencies map[string]string `json:"dependencies"`
	}
	if err := json.Unmarshal(raw, &pkg); err != nil {
		t.Fatal(err)
	}
	if want := pkg.Dependencies["playwright-core"]; want != Version {
		t.Fatalf("package.json pins playwright-core %q, package-lock.json has %q: run npm install in runner/playwright", want, Version)
	}
	if !regexp.MustCompile(`^\d+\.\d+\.\d+$`).MatchString(Version) {
		t.Fatalf("playwright-core %q: pin an exact version", Version)
	}
	if !strings.HasPrefix(Integrity, "sha512-") {
		t.Fatalf("integrity %q is not a sha512 SRI", Integrity)
	}
}

func TestPinErrors(t *testing.T) {
	for _, lock := range []string{`{`, `{"packages":{}}`, `{"packages":{"node_modules/playwright-core":{"version":"1.2.3"}}}`} {
		if _, _, err := pin([]byte(lock)); err == nil {
			t.Errorf("%s: no error", lock)
		}
	}
}
