// Command updatepins prints the pins.go declarations for a Node and a playwright-core version, read from
// the Node release's SHASUMS256.txt and the npm registry's dist.integrity. Paste the output over the
// matching block of internal/runtime/pins.go and update the go:embed file names.
//
//	go run ./tools/updatepins --node 22.23.4 --playwright 1.64.1
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	node := flag.String("node", "", "Node version, e.g. 22.23.4")
	pw := flag.String("playwright", "", "playwright-core version, e.g. 1.64.1")
	flag.Parse()
	if *node == "" || *pw == "" {
		flag.Usage()
		os.Exit(2)
	}
	client := &http.Client{Timeout: time.Minute}

	sums := map[string]string{}
	resp, err := client.Get(fmt.Sprintf("https://nodejs.org/dist/v%s/SHASUMS256.txt", *node))
	check(err)
	sc := bufio.NewScanner(resp.Body)
	for sc.Scan() {
		if f := strings.Fields(sc.Text()); len(f) == 2 {
			sums[f[1]] = f[0]
		}
	}
	resp.Body.Close()

	fmt.Printf("var NodeVersion = %q\n\nvar NodePins = map[string]Pin{ // key \"<goos>/<goarch>\"\n", *node)
	for _, t := range [][3]string{{"windows/amd64", "win-x64", "zip"}, {"windows/arm64", "win-arm64", "zip"}, {"linux/amd64", "linux-x64", "tar.gz"}, {"linux/arm64", "linux-arm64", "tar.gz"}} {
		file := fmt.Sprintf("node-v%s-%s.%s", *node, t[1], t[2])
		sum, ok := sums[file]
		if !ok {
			check(fmt.Errorf("%s is not in SHASUMS256.txt", file))
		}
		url := fmt.Sprintf("https://nodejs.org/dist/v%s/%s", *node, file)
		var size int64
		if head, err := client.Head(url); err == nil {
			size = head.ContentLength
			head.Body.Close()
		}
		fmt.Printf("\t%q: {%q, %q, %d},\n", t[0], url, sum, max(size, 0))
	}
	fmt.Println("}")

	resp, err = client.Get(fmt.Sprintf("https://registry.npmjs.org/playwright-core/%s", *pw))
	check(err)
	var meta struct {
		Dist struct{ Integrity string } `json:"dist"`
	}
	check(json.NewDecoder(resp.Body).Decode(&meta))
	resp.Body.Close()
	if meta.Dist.Integrity == "" {
		check(fmt.Errorf("playwright-core %s has no dist.integrity", *pw))
	}
	fmt.Printf("\nvar PlaywrightVersion = %q\n\nvar PlaywrightPins = map[string]string{ // version -> SRI integrity of the registry tarball\n\t%q: %q,\n}\n", *pw, *pw, meta.Dist.Integrity)
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "updatepins:", err)
		os.Exit(1)
	}
}
