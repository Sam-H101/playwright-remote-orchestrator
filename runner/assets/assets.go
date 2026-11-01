// Package assets holds files pwrunner writes to disk for its children.
package assets

import _ "embed"

// ParentWatchdogJS is preloaded into every run-server (NODE_OPTIONS=--require) so it exits when the
// agent dies without cleaning up. It is a byte-identical copy of gateway/lib/parent-watchdog.js.
//
//go:embed parent-watchdog.js
var ParentWatchdogJS []byte
