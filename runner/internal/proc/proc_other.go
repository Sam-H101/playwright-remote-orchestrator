//go:build !windows

package proc

import (
	"errors"
	"os/exec"
	"syscall"
	"time"
)

func hide(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// KillTree sends SIGTERM to the process group, then SIGKILL after 5 s if it is still there.
func KillTree(pid int) error {
	target := -pid
	if pgid, err := syscall.Getpgid(pid); err != nil || pgid != pid {
		target = pid
	}
	if err := syscall.Kill(target, syscall.SIGTERM); err != nil {
		if errors.Is(err, syscall.ESRCH) {
			return nil
		}
		return err
	}
	go func() {
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			if !Alive(pid) {
				return
			}
			time.Sleep(100 * time.Millisecond)
		}
		syscall.Kill(target, syscall.SIGKILL)
	}()
	return nil
}

// Alive reports whether a process with this pid is running.
func Alive(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}
