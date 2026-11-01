//go:build !windows

package install

import (
	"os/exec"
	"syscall"
)

func setCmdLine(cmd *exec.Cmd, line string) {}

func StartDetached(exe string, args ...string) (int, error) {
	cmd := exec.Command(exe, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	pid := cmd.Process.Pid
	cmd.Process.Release()
	return pid, nil
}
