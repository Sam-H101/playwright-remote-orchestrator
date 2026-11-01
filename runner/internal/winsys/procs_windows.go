package winsys

import (
	"errors"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// ProcInfo is one running process; Image is empty when access to it is denied.
type ProcInfo struct {
	PID, ParentPID int
	Image          string
}

// ListProcesses snapshots the running processes with their image paths.
func ListProcesses() ([]ProcInfo, error) {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil, err
	}
	defer windows.CloseHandle(snap)
	var e windows.ProcessEntry32
	e.Size = uint32(unsafe.Sizeof(e))
	var out []ProcInfo
	for err = windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		pid := int(e.ProcessID)
		image, _ := ImagePathOf(pid)
		out = append(out, ProcInfo{PID: pid, ParentPID: int(e.ParentProcessID), Image: image})
	}
	if errors.Is(err, windows.ERROR_NO_MORE_FILES) {
		err = nil
	}
	return out, err
}

// ImagePathOf is the full path of a process's executable.
func ImagePathOf(pid int) (string, error) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return "", err
	}
	defer windows.CloseHandle(h)
	buf := make([]uint16, windows.MAX_LONG_PATH)
	n := uint32(len(buf))
	if err := windows.QueryFullProcessImageName(h, 0, &buf[0], &n); err != nil {
		return "", err
	}
	return windows.UTF16ToString(buf[:n]), nil
}

// UnderDir reports whether path is inside dir (case-insensitively, as Windows paths are).
func UnderDir(path, dir string) bool {
	dir = strings.TrimRight(dir, `\/`) + `\`
	return len(path) > len(dir) && strings.EqualFold(path[:len(dir)], dir)
}

// KillByImagePrefix terminates every process whose image is under prefix (never this process) and
// waits up to wait for them to exit. Nothing outside prefix is touched.
func KillByImagePrefix(prefix string, wait time.Duration) ([]int, error) {
	procs, err := ListProcesses()
	if err != nil {
		return nil, err
	}
	var pids []int
	for _, p := range procs {
		if p.PID != os.Getpid() && p.Image != "" && UnderDir(p.Image, prefix) {
			pids = append(pids, p.PID)
		}
	}
	return pids, KillPIDs(pids, wait)
}

// KillPIDs terminates processes and waits up to wait for all of them.
func KillPIDs(pids []int, wait time.Duration) error {
	var handles []windows.Handle
	var errs []error
	for _, pid := range pids {
		h, err := windows.OpenProcess(windows.PROCESS_TERMINATE|windows.SYNCHRONIZE, false, uint32(pid))
		if err != nil {
			continue // already gone
		}
		if err := windows.TerminateProcess(h, 1); err != nil && !errors.Is(err, windows.ERROR_ACCESS_DENIED) {
			errs = append(errs, err)
		}
		handles = append(handles, h)
	}
	deadline := time.Now().Add(wait)
	for _, h := range handles {
		left := time.Until(deadline)
		if left < 0 {
			left = 0
		}
		windows.WaitForSingleObject(h, uint32(left/time.Millisecond))
		windows.CloseHandle(h)
	}
	return errors.Join(errs...)
}

// CommandLinesMatching returns the pids of processes named one of names whose command line contains
// substr (case-insensitive). Only the migration from the PowerShell install uses it, to find its
// supervisor and the node.exe agent it started, which run from outside the install folder.
func CommandLinesMatching(substr string, names ...string) ([]int, error) {
	filter := ""
	if len(names) > 0 {
		q := make([]string, len(names))
		for i, n := range names {
			q[i] = "Name='" + strings.ReplaceAll(n, "'", "") + "'"
		}
		filter = " -Filter \"" + strings.Join(q, " OR ") + "\""
	}
	script := "Get-CimInstance Win32_Process" + filter + " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script)
	hideWindow(cmd)
	out, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	var pids []int
	needle := strings.ToLower(substr)
	for _, line := range strings.Split(string(out), "\n") {
		id, cmdline, ok := strings.Cut(strings.TrimSpace(line), "\t")
		if !ok || !strings.Contains(strings.ToLower(cmdline), needle) {
			continue
		}
		if pid, err := strconv.Atoi(id); err == nil && pid != os.Getpid() {
			pids = append(pids, pid)
		}
	}
	return pids, nil
}

func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &windowsSysProcAttr
}
