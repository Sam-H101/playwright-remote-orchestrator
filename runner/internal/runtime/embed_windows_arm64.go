//go:build embedruntime && windows && arm64

package runtime

import _ "embed"

//go:embed files/node-v22.23.4-win-arm64.zip
var embeddedNode []byte

//go:embed files/playwright-core-1.64.1.tgz
var embeddedPlaywright []byte

// Embedded returns the Node zip and playwright-core tarball built into this exe (their pinned
// versions are NodeVersion and PlaywrightVersion); they are verified against the pins before use.
//
// noinline keeps the archives in the binary even where a caller discards them.
//
//go:noinline
func Embedded() (nodeZip, pwTgz []byte, ok bool) { return embeddedNode, embeddedPlaywright, true }
