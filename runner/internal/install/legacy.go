package install

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/winsys"
)

// The PowerShell installer's files. They move to <dir>\legacy\ so the installed copy of
// install-backend.ps1 can no longer re-register the scheduled task and start a second agent with the
// same node name (which would take turns with this one, 4409 every 30 s).
var legacyFiles = []string{
	"install-backend.ps1", "run-backend.ps1", "backend-status.ps1", "uninstall-backend.ps1", "PlaywrightBackend.psm1",
	filepath.Join("app", "gateway"),
	filepath.Join("logs", "supervisor.log"), filepath.Join("logs", "agent.err.log"),
	filepath.Join("logs", "agent.err.log.prev"), filepath.Join("logs", "agent.log.prev"),
}

func (r *run) moveLegacyFiles() error {
	dest := r.dir("legacy")
	moved := 0
	for _, rel := range legacyFiles {
		src := r.dir(rel)
		if _, err := os.Lstat(src); err != nil {
			continue
		}
		target := filepath.Join(dest, rel)
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		os.RemoveAll(target)
		if err := os.Rename(src, target); err != nil {
			return fmt.Errorf("moving %s to legacy\\: %w", src, err)
		}
		moved++
	}
	if moved == 0 {
		return nil
	}
	readme := fmt.Sprintf("These files belong to the PowerShell installer that pwrunner.exe replaced on %s.\r\n"+
		"Use pwrunner.exe status / pwrunner.exe uninstall instead; this folder can be deleted.\r\n", time.Now().Format("2006-01-02"))
	if err := os.WriteFile(filepath.Join(dest, "README.txt"), []byte(readme), 0o644); err != nil {
		return err
	}
	r.log.Infof("Moved the PowerShell installer files to legacy\\")
	return nil
}

// stopLegacy stops what the PowerShell install runs: its scheduled task, run-backend.ps1 supervisors
// (first, so they can't restart the agent) and node.exe processes running from <dir>\app\ (the agent and
// its run-servers, which use the global node outside the install folder). Returns how many processes were
// stopped and how many of them were supervisors.
func stopLegacy(dir, task string, taskExists bool) (int, int, error) {
	runner := filepath.Join(dir, "run-backend.ps1")
	_, haveRunner := os.Stat(runner)
	_, haveGateway := os.Stat(filepath.Join(dir, "app", "gateway"))
	if !taskExists && haveRunner != nil && haveGateway != nil {
		return 0, 0, nil
	}
	if taskExists {
		schtasks("/End", "/TN", task)
	}
	supervisors, err := winsys.CommandLinesMatching(runner, "powershell.exe", "pwsh.exe")
	if err != nil {
		return 0, 0, err
	}
	if err := winsys.KillPIDs(supervisors, 15*time.Second); err != nil {
		return 0, 0, err
	}
	agents, err := winsys.CommandLinesMatching(strings.TrimRight(dir, `\`)+`\app\`, "node.exe")
	if err != nil {
		return 0, 0, err
	}
	for _, pid := range agents {
		proc.KillTree(pid)
	}
	if err := winsys.KillPIDs(agents, 15*time.Second); err != nil {
		return 0, 0, err
	}
	return len(supervisors) + len(agents), len(supervisors), nil
}

func schtasks(args ...string) error {
	cmd := exec.Command("schtasks.exe", args...)
	proc.Hide(cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("schtasks %s: %v %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return nil
}

func taskExists(name string) bool  { return schtasks("/Query", "/TN", name) == nil }
func deleteTask(name string) error { return schtasks("/Delete", "/TN", name, "/F") }
func runTask(name string) error    { return schtasks("/Run", "/TN", name) }

func netsh(args string) error {
	cmd := exec.Command("netsh.exe")
	setCmdLine(cmd, "netsh.exe advfirewall firewall "+args)
	proc.Hide(cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%v %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func firewallRuleExists(name string) bool {
	return netsh(fmt.Sprintf(`show rule name="%s"`, name)) == nil
}

func deleteFirewallRule(name string) error {
	return netsh(fmt.Sprintf(`delete rule name="%s"`, name))
}

// TaskExists reports whether a scheduled task exists.
func TaskExists(name string) bool { return taskExists(name) }
