//go:build integration && !windows

package integration

import (
	"os/exec"
	"syscall"
)

func newGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// Interrupt sends SIGTERM to the agent.
func Interrupt(pid int) error { return syscall.Kill(pid, syscall.SIGTERM) }

func killTree(pid int) {
	syscall.Kill(-pid, syscall.SIGKILL)
	syscall.Kill(pid, syscall.SIGKILL)
}

// KillHard ends the agent alone.
func KillHard(pid int) { syscall.Kill(pid, syscall.SIGKILL) }
