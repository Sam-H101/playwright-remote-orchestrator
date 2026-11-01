//go:build !windows

// Package winsys holds the Windows specifics of an install; on other platforms these are stubs.
package winsys

import (
	"errors"
	"os"
	"strings"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
)

var errWindowsOnly = errors.New("only supported on Windows")

func IsElevated() (bool, error)                            { return os.Geteuid() == 0, nil }
func EnablePrivileges() error                              { return nil }
func CreateLockedDir(path string, usersCanRead bool) error { return os.MkdirAll(path, 0o755) }
func Protect(path string, usersCanRead bool) error         { return nil }
func PathProblem(path string) (string, error)              { return "", nil }
func TokenFileProblem(path string) (string, error)         { return "", nil }
func ResetUntrusted(installDir string, keep []string, log *logging.Logger) (string, error) {
	return "", errWindowsOnly
}

func WriteTokenFile(path, token string) error {
	os.Remove(path)
	return os.WriteFile(path, []byte(token), 0o600)
}

func ReadTokenFile(path string) (string, bool, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	t := strings.TrimSpace(string(b))
	return t, t != "", nil
}

type Job struct{}

func NewKillOnCloseJob() (*Job, error)     { return nil, errWindowsOnly }
func (j *Job) AssignCurrent() error        { return errWindowsOnly }
func (j *Job) Assign(pid int) error        { return errWindowsOnly }
func (j *Job) Terminate(exit uint32) error { return errWindowsOnly }
func (j *Job) Close() error                { return nil }

type ProcInfo struct {
	PID, ParentPID int
	Image          string
}

func ListProcesses() ([]ProcInfo, error)                                 { return nil, errWindowsOnly }
func ImagePathOf(pid int) (string, error)                                { return os.Readlink("/proc/" + itoa(pid) + "/exe") }
func UnderDir(path, dir string) bool                                     { return strings.HasPrefix(path, strings.TrimRight(dir, "/")+"/") }
func KillByImagePrefix(prefix string, wait time.Duration) ([]int, error) { return nil, nil }
func KillPIDs(pids []int, wait time.Duration) error                      { return nil }
func CommandLinesMatching(substr string, names ...string) ([]int, error) { return nil, nil }

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	s := ""
	for n > 0 {
		s = string(rune('0'+n%10)) + s
		n /= 10
	}
	return s
}
