//go:build !embedruntime || !windows || !(amd64 || arm64)

package runtime

// Embedded reports that this build carries no runtime; the slim exe downloads it.
func Embedded() (nodeZip, pwTgz []byte, ok bool) { return nil, nil, false }
