//go:build !windows

package main

import "context"

const defaultDir = "/opt/pwrunner"

func platformChecks(ctx context.Context, dir string, quick bool) []check { return nil }
