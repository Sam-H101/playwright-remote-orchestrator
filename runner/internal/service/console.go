// Package service runs the agent as a Windows service or in the foreground.
package service

import (
	"context"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// Events is the service's Event Log source.
type Events interface {
	Info(eid uint32, msg string) error
	Warning(eid uint32, msg string) error
	Error(eid uint32, msg string) error
	Close() error
}

// RunConsole runs fn until Ctrl-C, Ctrl-Break (Windows) or SIGTERM/SIGHUP, then cancels its context and
// waits for it to return; a second signal or 10 s without returning exits the process.
func RunConsole(fn func(ctx context.Context) error) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigs := make(chan os.Signal, 2)
	signal.Notify(sigs, os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	defer signal.Stop(sigs)
	done := make(chan error, 1)
	go func() { done <- fn(ctx) }()
	select {
	case err := <-done:
		return err
	case <-sigs:
	}
	cancel()
	select {
	case err := <-done:
		return err
	case <-sigs:
	case <-time.After(10 * time.Second):
	}
	os.Exit(0)
	return nil
}
