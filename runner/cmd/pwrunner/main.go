// Command pwrunner is the Playwright runner host for Windows (and Linux): the gateway agent, its
// installer, Windows service, status tool and uninstaller in one executable.
package main

import (
	"fmt"
	"os"
	goruntime "runtime"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/runtime"
)

// version is set at build time with -ldflags "-X main.version=<v>".
var version = "dev"

const usage = `Usage: pwrunner <command> [options]

Commands:
  version     print the version, Go toolchain and the pinned runtime
`

func main() {
	cli.Exit(run(os.Args[1:]))
}

func run(args []string) error {
	if len(args) == 0 {
		fmt.Print(usage)
		return &cli.ExitError{Code: 2}
	}
	switch args[0] {
	case "version", "--version":
		fmt.Println(versionLine())
		return nil
	case "-h", "--help", "help":
		fmt.Print(usage)
		return nil
	}
	return cli.Usagef("unknown command %q\n\n%s", args[0], usage)
}

func versionLine() string {
	_, _, embedded := runtime.Embedded()
	yes := map[bool]string{true: "yes", false: "no"}[embedded]
	return fmt.Sprintf("pwrunner %s (%s, %s/%s) node v%s playwright-core %s embedded=%s",
		version, goruntime.Version(), goruntime.GOOS, goruntime.GOARCH, runtime.NodeVersion, runtime.PlaywrightVersion, yes)
}
