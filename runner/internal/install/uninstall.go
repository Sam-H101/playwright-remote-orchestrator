package install

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/cli"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/service"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

// UninstallOptions are the uninstall flags.
type UninstallOptions struct {
	RemoveFiles bool
	Yes         bool
	Interactive bool      // stdin is a terminal, so a confirmation can be asked
	In          io.Reader // answers to the confirmation
	Console     io.Writer
}

// Uninstall stops the backend and removes its service, the legacy scheduled task and firewall rule,
// and with RemoveFiles the install folder (browsers included; the token is zero-filled first).
func Uninstall(ctx context.Context, dir string, o UninstallOptions) error {
	if o.Console == nil {
		o.Console = os.Stderr
	}
	log := logging.New(o.Console, nil)
	b, _, _ := config.ReadBackend(filepath.Join(dir, "backend.json"))
	svcName := b.ServiceName
	if svcName == "" {
		svcName = DefaultServiceName
	}
	task := b.TaskName
	if task == "" {
		task = DefaultServiceName
	}
	st, err := service.Query(svcName)
	if err != nil {
		return err
	}
	hasTask := taskExists(task)
	elevated, _ := winsys.IsElevated()
	if (st != service.NotInstalled || hasTask) && !elevated {
		return &cli.ExitError{Code: 2, Msg: "Removing the service needs an elevated prompt. Run elevated."}
	}
	if !o.Yes {
		if !o.Interactive {
			return &cli.ExitError{Code: 2, Msg: "pass --yes to uninstall without a prompt"}
		}
		what := fmt.Sprintf("Remove the %s service", svcName)
		if o.RemoveFiles {
			what += " and delete " + dir
		}
		fmt.Fprintf(o.Console, "%s? [y/N] ", what)
		answer, _ := bufio.NewReader(o.In).ReadString('\n')
		if a := strings.ToLower(strings.TrimSpace(answer)); a != "y" && a != "yes" {
			return &cli.ExitError{Code: 2, Msg: "uninstall cancelled"}
		}
	}

	log.Infof("Stopping backend")
	if st != service.NotInstalled {
		if err := service.Remove(svcName); err != nil {
			return fmt.Errorf("removing service '%s': %w", svcName, err)
		}
		log.Infof("Removed service '%s'", svcName)
	}
	if _, _, err := stopLegacy(dir, task, hasTask); err != nil {
		log.Warnf("Stopping the PowerShell install's processes: %v", err)
	}
	if hasTask {
		if err := deleteTask(task); err != nil {
			return err
		}
		log.Infof("Removed scheduled task '%s'", task)
	}
	stopNoService(dir)
	if killed, _ := winsys.KillByImagePrefix(dir, 15*time.Second); len(killed) > 0 {
		log.Infof("Stopped %d backend process(es)", len(killed))
	}
	for _, n := range uniq(svcName, task) {
		rule := "Playwright Backend (" + n + ")"
		if firewallRuleExists(rule) {
			if err := deleteFirewallRule(rule); err != nil {
				log.Warnf("Could not remove firewall rule '%s': %v", rule, err)
			} else {
				log.Infof("Removed legacy inbound firewall rule '%s'", rule)
			}
		}
	}
	if o.RemoveFiles {
		if _, err := os.Stat(dir); err == nil {
			if err := removeInstall(dir, log); err != nil {
				return err
			}
		}
	}
	log.Infof("Uninstall complete")
	return nil
}

func uniq(a, b string) []string {
	if strings.EqualFold(a, b) {
		return []string{a}
	}
	return []string{a, b}
}

func removeInstall(dir string, log *logging.Logger) error {
	token := filepath.Join(dir, "agent.token")
	if st, err := os.Stat(token); err == nil {
		// Zero-filled first, so the token is not left in freed disk blocks.
		if f, err := os.OpenFile(token, os.O_WRONLY, 0); err == nil {
			f.Write(make([]byte, st.Size()))
			f.Sync()
			f.Close()
		}
		os.Remove(token)
	}
	// Give Windows a moment to release handles held by the processes just stopped.
	time.Sleep(2 * time.Second)
	os.Chdir(os.TempDir())
	self, _ := os.Executable()
	inside := winsys.UnderDir(self, dir)
	err := os.RemoveAll(dir)
	if inside {
		// This exe can't delete itself while it runs; a detached cmd finishes the job a moment later.
		if _, serr := StartDetached("cmd.exe", "/c", "ping -n 3 127.0.0.1 >nul & rd /s /q \""+dir+"\""); serr == nil {
			log.Infof("Deleting %s once this program exits", dir)
			return nil
		}
	}
	if err != nil {
		return fmt.Errorf("could not delete %s; a process may still hold files in it: %w", dir, err)
	}
	log.Infof("Deleted %s", dir)
	return nil
}
