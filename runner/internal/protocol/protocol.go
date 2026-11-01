// Package protocol holds the agent protocol's constants and helpers, ported from gateway/lib/protocol.js.
package protocol

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"regexp"
	goruntime "runtime"
	"strings"
)

const (
	MaxSlots        = 64
	MaxPayload      = 256 << 20 // Playwright moves screenshots, traces and downloads over the socket
	ControlMaxFrame = 1 << 20   // the hub's maxPayload on the control channel (hub.js:40)
)

// Close codes the hub sends on the control channel.
const (
	CloseTryAgainLater = 1013 // the hub can't check the token right now (its database is down)
	CloseBadRequest    = 4400
	CloseUnauthorized  = 4401
	CloseForbidden     = 4403 // a token bound to another node name, or a name reserved for its bound token
	CloseReplaced      = 4409
)

var (
	NameRE         = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)
	SessionIDRE    = regexp.MustCompile(`^[0-9a-f-]{36}$`)
	managedTokenRE = regexp.MustCompile(`^pwg[ac]_[A-Za-z0-9_-]{43}$`)
	unsafeNameRE   = regexp.MustCompile(`[^A-Za-z0-9._-]`)
)

// DropHeaders are never forwarded to run-server, nor are sec-websocket-* and proxy-* headers.
var DropHeaders = map[string]bool{
	"host": true, "connection": true, "upgrade": true, "content-length": true, "transfer-encoding": true,
	"authorization": true, "x-gateway-token": true, "keep-alive": true, "te": true, "trailer": true,
}

// CanonicalName is the lower-case form of a valid node name, or "" for an invalid one. Node names are
// case-insensitive; the hub and the agent both use the lower-case form.
func CanonicalName(s string) string {
	if !NameRE.MatchString(s) {
		return ""
	}
	return strings.ToLower(s)
}

// SanitizeName turns a host name into a node name: anything outside A-Z a-z 0-9 . _ - becomes "-".
func SanitizeName(hostname string) string {
	s := unsafeNameRE.ReplaceAllString(hostname, "-")
	if len(s) > 128 {
		s = s[:128]
	}
	if s == "" {
		return "node"
	}
	return s
}

// ForwardHeaders filters a client's upgrade headers for run-server. Keys are added in canonical form:
// net/http only recognises User-Agent that way, and a lower-case key would be sent next to Go's default
// User-Agent, which Node would keep instead of the client's Playwright/x.y.z (silently disabling
// run-server's version check). Without a forwarded user-agent, User-Agent is set to "" so net/http
// sends none.
func ForwardHeaders(h map[string][]string) http.Header {
	out := http.Header{}
	for key, values := range h {
		k := strings.ToLower(key)
		if DropHeaders[k] || strings.HasPrefix(k, "sec-websocket-") || strings.HasPrefix(k, "proxy-") {
			continue
		}
		for _, v := range values {
			out.Add(k, v)
		}
	}
	if len(out.Values("User-Agent")) == 0 {
		out["User-Agent"] = []string{""}
	}
	return out
}

// SendableCloseCode maps a close code to one that may go on the wire: 1000-1014 except 1004-1006 and
// 3000-4999 pass, 1005 (no code) becomes 1000, anything else 1011.
func SendableCloseCode(code int) int {
	if (code >= 1000 && code <= 1014 && code != 1004 && code != 1005 && code != 1006) || (code >= 3000 && code <= 4999) {
		return code
	}
	if code == 1005 {
		return 1000
	}
	return 1011
}

// CloseReason cuts a close reason to at most 123 UTF-8 bytes on a rune boundary.
func CloseReason(s string) string {
	for len(s) > 123 {
		_, size := lastRune(s)
		s = s[:len(s)-size]
	}
	return s
}

func lastRune(s string) (rune, int) {
	for i := len(s) - 1; i >= 0 && i >= len(s)-4; i-- {
		if s[i]&0xC0 != 0x80 {
			return rune(s[i]), len(s) - i
		}
	}
	return 0, 1
}

// TokenLabel names a token without giving it away: a managed token by its 12-character display prefix,
// anything else only as "token".
func TokenLabel(token string) string {
	if managedTokenRE.MatchString(token) {
		return "token " + token[:12] + "..."
	}
	return "token"
}

// TokenHash is what the hub stores: the lower-case hex SHA-256 of the exact token string.
func TokenHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// Platform is the hub's name for this OS (Node's process.platform).
func Platform() string {
	if goruntime.GOOS == "windows" {
		return "win32"
	}
	return goruntime.GOOS
}
