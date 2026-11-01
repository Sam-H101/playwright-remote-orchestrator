//go:build integration && windows

package integration

import (
	"os/exec"
	"strconv"
	"syscall"

	"golang.org/x/sys/windows"
)

func newGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NEW_PROCESS_GROUP}
}

// Interrupt sends Ctrl-Break to the agent's process group (it must have been started with Group).
func Interrupt(pid int) error {
	return windows.GenerateConsoleCtrlEvent(windows.CTRL_BREAK_EVENT, uint32(pid))
}

func killTree(pid int) {
	exec.Command("taskkill", "/pid", strconv.Itoa(pid), "/T", "/F").Run()
}

// KillHard ends the agent alone, as Task Manager does.
func KillHard(pid int) {
	exec.Command("taskkill", "/pid", strconv.Itoa(pid), "/F").Run()
}
