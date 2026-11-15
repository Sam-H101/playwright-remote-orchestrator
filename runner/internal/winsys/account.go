package winsys

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"regexp"
	"strconv"
	"strings"
)

var accountNameRE = regexp.MustCompile(`^(pwb-[0-9a-f]{8}-[1-9][0-9]?|pwbrowser-[0-9a-f]{8})$`)

// MaxSlots bounds the slot accounts an install may have left behind (protocol.MaxSlots).
const MaxSlots = 64

func serviceHash(service string) string {
	sum := sha256.Sum256([]byte(strings.ToLower(service)))
	return hex.EncodeToString(sum[:4])
}

// BrowserAccountName is the local account slot i's run-server and browsers run as: "pwb-", eight hex digits
// of a hash of the service name (case-insensitive, as service names are), "-" and the slot number, e.g.
// pwb-1a2b3c4d-2. Each slot has its own account, so a check can't read the browser profile of a check
// running at the same time in another slot, and two installs with different service names get different
// accounts. At most 15 characters, within Windows' 20-character limit.
func BrowserAccountName(service string, slot int) string {
	return "pwb-" + serviceHash(service) + "-" + strconv.Itoa(slot)
}

// BrowserAccountNames are the accounts of slots 1..slots.
func BrowserAccountNames(service string, slots int) []string {
	out := make([]string, slots)
	for i := range out {
		out[i] = BrowserAccountName(service, i+1)
	}
	return out
}

// LegacyBrowserAccountName is the one account every slot ran as before each slot had its own:
// "pwbrowser-" and the same eight hex digits. Install and uninstall remove it.
func LegacyBrowserAccountName(service string) string {
	return "pwbrowser-" + serviceHash(service)
}

// StaleBrowserAccounts are the accounts of a service that an install with this many slots no longer uses:
// the legacy single account and those of slots above the count (up to MaxSlots), plus any of listed (the
// accounts an earlier backend.json names, such as those of another service name) that is not one of the
// slots' own. A listed name that is not a browser account name pwrunner makes is never included, so a
// changed backend.json can't get another account deleted.
func StaleBrowserAccounts(service string, slots int, listed ...string) []string {
	keep := map[string]bool{}
	for _, n := range BrowserAccountNames(service, slots) {
		keep[strings.ToLower(n)] = true
	}
	var out []string
	add := func(n string) {
		if accountNameRE.MatchString(n) && !keep[strings.ToLower(n)] {
			keep[strings.ToLower(n)] = true // once
			out = append(out, n)
		}
	}
	add(LegacyBrowserAccountName(service))
	for _, n := range listed {
		add(n)
	}
	for i := slots + 1; i <= MaxSlots; i++ {
		add(BrowserAccountName(service, i))
	}
	return out
}

// browserSecretKey names the LSA private data that holds the account's password. "L$" makes it a local
// secret: never replicated, and readable only by SYSTEM and Administrators through the LSA.
func browserSecretKey(account string) string { return "L$pwrunner-" + account }

const (
	passwordLength = 40
	passwordUpper  = "ABCDEFGHJKLMNPQRSTUVWXYZ"
	passwordLower  = "abcdefghijkmnopqrstuvwxyz"
	passwordDigits = "23456789"
	passwordSymbol = "!#%+-.:=?@^_~"
)

// NewPassword is a random 40-character password from crypto/rand with at least one upper-case letter,
// lower-case letter, digit and symbol, so it meets any local complexity policy. Nobody types it: it is
// set at every install and only the service reads it back.
func NewPassword() (string, error) {
	const chars = passwordUpper + passwordLower + passwordDigits + passwordSymbol
	limit := byte(256 - 256%len(chars)) // bytes at or above this would favour the first characters
	for {
		out := make([]byte, 0, passwordLength)
		buf := make([]byte, 64)
		for len(out) < passwordLength {
			if _, err := rand.Read(buf); err != nil {
				return "", err
			}
			for _, b := range buf {
				if b < limit && len(out) < passwordLength {
					out = append(out, chars[int(b)%len(chars)])
				}
			}
		}
		s := string(out)
		if strings.ContainsAny(s, passwordUpper) && strings.ContainsAny(s, passwordLower) &&
			strings.ContainsAny(s, passwordDigits) && strings.ContainsAny(s, passwordSymbol) {
			return s, nil
		}
	}
}
