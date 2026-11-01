//go:build !windows

package service

import (
	"context"
	"errors"
	"time"
)

type Config struct {
	Name, DisplayName, Description, ExePath, InstallDir string
}

type State string

const (
	NotInstalled State = "not installed"
	Stopped      State = "stopped"
	StartPending State = "start pending"
	Running      State = "running"
	StopPending  State = "stop pending"
	Other        State = "other"
)

var errUnsupported = errors.New("services are only supported on Windows; use pwrunner run")

type ExitCodeError struct {
	Code int
	Err  error
}

func (e *ExitCodeError) Error() string { return e.Err.Error() }

func Install(c Config) error                                         { return errUnsupported }
func Remove(name string) error                                       { return nil }
func Start(name string, timeout time.Duration) error                 { return errUnsupported }
func Stop(name string, timeout time.Duration) error                  { return nil }
func Query(name string) (State, error)                               { return NotInstalled, nil }
func IsWindowsService() bool                                         { return false }
func RunAsService(name string, fn func(context.Context) error) error { return errUnsupported }

func EventLog(name string) Events { return nil }
