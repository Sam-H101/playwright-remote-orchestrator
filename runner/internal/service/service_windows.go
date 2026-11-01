package service

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
	"golang.org/x/sys/windows/svc/mgr"
)

// Config describes the Windows service.
type Config struct {
	Name, DisplayName, Description, ExePath, InstallDir string
}

// State is a service's state as Query reports it.
type State string

const (
	NotInstalled State = "not installed"
	Stopped      State = "stopped"
	StartPending State = "start pending"
	Running      State = "running"
	StopPending  State = "stop pending"
	Other        State = "other"
)

// Install creates the service (LocalSystem, automatic start, `<exe> service --install-dir <dir>`) or
// updates an existing one, sets the recovery actions (restart after 5 s, 60 s, 300 s; reset after a
// day; also on a non-zero exit) and registers the Event Log source. It is safe to re-run.
func Install(c Config) error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	cfg := mgr.Config{StartType: mgr.StartAutomatic, DisplayName: c.DisplayName, Description: c.Description, ErrorControl: mgr.ErrorNormal}
	s, err := m.OpenService(c.Name)
	if err == nil {
		cur, err := s.Config()
		if err != nil {
			s.Close()
			return err
		}
		cur.BinaryPathName = fmt.Sprintf(`"%s" service --install-dir "%s"`, c.ExePath, c.InstallDir)
		cur.StartType, cur.DisplayName, cur.Description, cur.ServiceStartName = cfg.StartType, cfg.DisplayName, cfg.Description, "LocalSystem"
		cur.ServiceType = windows.SERVICE_WIN32_OWN_PROCESS
		if err := s.UpdateConfig(cur); err != nil {
			s.Close()
			return err
		}
	} else {
		s, err = m.CreateService(c.Name, c.ExePath, cfg, "service", "--install-dir", c.InstallDir)
		if err != nil {
			return err
		}
	}
	defer s.Close()
	actions := []mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 5 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 300 * time.Second},
	}
	if err := s.SetRecoveryActions(actions, 86400); err != nil {
		return fmt.Errorf("recovery actions: %w", err)
	}
	if err := s.SetRecoveryActionsOnNonCrashFailures(true); err != nil {
		return fmt.Errorf("recovery on non-crash failures: %w", err)
	}
	if err := eventlog.InstallAsEventCreate(c.Name, eventlog.Error|eventlog.Warning|eventlog.Info); err != nil && !strings.Contains(err.Error(), "registry key already exists") {
		return fmt.Errorf("event log source: %w", err)
	}
	return nil
}

// Remove stops and deletes the service and its Event Log source.
func Remove(name string) error {
	if err := Stop(name, 30*time.Second); err != nil && !errors.Is(err, errNotInstalled) {
		return err
	}
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	s, err := m.OpenService(name)
	if err != nil {
		return nil
	}
	err = s.Delete()
	s.Close()
	eventlog.Remove(name)
	return err
}

var errNotInstalled = errors.New("service not installed")

func open(name string) (*mgr.Mgr, *mgr.Service, error) {
	m, err := mgr.Connect()
	if err != nil {
		return nil, nil, err
	}
	s, err := m.OpenService(name)
	if err != nil {
		m.Disconnect()
		return nil, nil, errNotInstalled
	}
	return m, s, nil
}

// Query reports the service's state.
func Query(name string) (State, error) {
	m, s, err := open(name)
	if errors.Is(err, errNotInstalled) {
		return NotInstalled, nil
	}
	if err != nil {
		return "", err
	}
	defer m.Disconnect()
	defer s.Close()
	st, err := s.Query()
	if err != nil {
		return "", err
	}
	switch st.State {
	case svc.Stopped:
		return Stopped, nil
	case svc.StartPending:
		return StartPending, nil
	case svc.Running:
		return Running, nil
	case svc.StopPending:
		return StopPending, nil
	}
	return Other, nil
}

// Start starts the service and waits until it runs.
func Start(name string, timeout time.Duration) error {
	m, s, err := open(name)
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
		return err
	}
	return waitFor(s, svc.Running, timeout)
}

// Stop stops the service and waits until it has stopped.
func Stop(name string, timeout time.Duration) error {
	m, s, err := open(name)
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	st, err := s.Query()
	if err != nil {
		return err
	}
	if st.State == svc.Stopped {
		return nil
	}
	if _, err := s.Control(svc.Stop); err != nil && !errors.Is(err, windows.ERROR_SERVICE_NOT_ACTIVE) {
		return err
	}
	return waitFor(s, svc.Stopped, timeout)
}

func waitFor(s *mgr.Service, want svc.State, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		st, err := s.Query()
		if err != nil {
			return err
		}
		if st.State == want {
			return nil
		}
		if want == svc.Running && st.State == svc.Stopped {
			return fmt.Errorf("service %s stopped while starting (exit code %d/%d)", s.Name, st.Win32ExitCode, st.ServiceSpecificExitCode)
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("service %s did not reach the wanted state within %v", s.Name, timeout)
		}
		time.Sleep(250 * time.Millisecond)
	}
}

// IsWindowsService reports whether the SCM started this process.
func IsWindowsService() bool {
	ok, _ := svc.IsWindowsService()
	return ok
}

// ExitCodeError makes RunAsService report a service-specific exit code (2 = bad configuration).
type ExitCodeError struct {
	Code int
	Err  error
}

func (e *ExitCodeError) Error() string { return e.Err.Error() }

type handler struct {
	run func(ctx context.Context) error
}

func (h *handler) Execute(_ []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- h.run(ctx) }()
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for {
		select {
		case err := <-done:
			return exitCode(err)
		case c := <-req:
			switch c.Cmd {
			case svc.Interrogate:
				status <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending, WaitHint: 25000}
				cancel()
				select {
				case err := <-done:
					return exitCode(err)
				case <-time.After(25 * time.Second):
					return false, 0
				}
			}
		}
	}
}

func exitCode(err error) (bool, uint32) {
	if err == nil {
		return false, 0
	}
	var ec *ExitCodeError
	if errors.As(err, &ec) {
		return true, uint32(ec.Code)
	}
	return true, 1
}

// RunAsService runs fn as the named service: StartPending, then Running; Stop or Shutdown cancels fn's
// context and waits up to 25 s. An error exit is reported as a service-specific exit code, which the
// recovery actions (on non-crash failures too) answer with a restart.
func RunAsService(name string, fn func(ctx context.Context) error) error {
	return svc.Run(name, &handler{run: fn})
}

// EventLog opens the service's Event Log source (nil when it isn't registered).
func EventLog(name string) Events {
	l, err := eventlog.Open(name)
	if err != nil {
		return nil
	}
	return l
}
