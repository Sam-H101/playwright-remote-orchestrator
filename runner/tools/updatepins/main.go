// Command updatepins prints the pins.go declarations for a Node version, read from the Node release's
// SHASUMS256.txt. Paste the output over the matching block of internal/runtime/pins.go and update the Node
// file names in internal/runtime/embed_windows_*.go. playwright-core is pinned in runner/playwright
// instead, which Dependabot updates.
//
//	go run ./tools/updatepins --node 22.23.4
package main

import (
	"bufio"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	node := flag.String("node", "", "Node version, e.g. 22.23.4")
	flag.Parse()
	if *node == "" {
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
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "updatepins:", err)
		os.Exit(1)
	}
}
