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

// SetUser makes cmd start as uid/gid with no supplementary groups (an empty list, so the agent's groups
// are dropped rather than inherited). The agent must run as root.
func SetUser(cmd *exec.Cmd, uid, gid uint32) error {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Credential = &syscall.Credential{Uid: uid, Gid: gid, Groups: []uint32{}}
	return nil
}

// KillTree sends SIGTERM to the process group, then SIGKILL after 5 s if it is still there. As root it
// reaches a group that runs as another user (--run-server-user) too.
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
