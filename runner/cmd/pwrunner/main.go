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
  install     install or upgrade this host as a runner node (Windows service)
  status      report the node's health (exit 0 healthy, 2 problem, 1 not installed)
  uninstall   stop and remove the service (--remove-files: and the install folder)
  start, stop, restart
              control the installed service
  token       set --stdin | set --file <path> | clear | show-prefix
  run         run the agent in the foreground (flags, then HUB_URL & co., then --config backend.json)
  fetch       download and verify the pinned Node and playwright-core into <install dir>\cache
  node        run the private node with the run-servers' environment: pwrunner node -- <args>
  selfcheck   check an install (--quick: no network)
  version     print the version, Go toolchain and the pinned runtime

Run "pwrunner <command> --help" for a command's options.
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
	case "run":
		return runAgent(args[1:])
	case "install":
		return installCmd(args[1:])
	case "uninstall":
		return uninstallCmd(args[1:])
	case "status":
		return statusCmd(args[1:])
	case "start", "stop", "restart":
		return controlCmd(args[0], args[1:])
	case "token":
		return tokenCmd(args[1:])
	case "service":
		return serviceCmd(args[1:])
	case "fetch":
		return fetchCmd(args[1:])
	case "node":
		return nodeCmd(args[1:])
	case "selfcheck":
		return selfcheckCmd(args[1:])
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
