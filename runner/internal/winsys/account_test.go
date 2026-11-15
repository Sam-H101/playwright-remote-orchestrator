package winsys

import (
	"regexp"
	"strings"
	"testing"
)

func TestBrowserAccountName(t *testing.T) {
	a, b := BrowserAccountName("PlaywrightBackend", 1), BrowserAccountName("PlaywrightBackend2", 1)
	if !regexp.MustCompile(`^pwb-[0-9a-f]{8}-1$`).MatchString(a) {
		t.Fatalf("%q: want pwb-<8 hex>-1", a)
	}
	if a == b {
		t.Fatal("two service names share an account")
	}
	if BrowserAccountName("playwrightbackend", 1) != a {
		t.Fatal("service names are case-insensitive, so the account name must be too")
	}
	if last := BrowserAccountName("PlaywrightBackend", MaxSlots); len(last) > 20 || last != a[:len(a)-1]+"64" {
		t.Fatalf("%q: want at most 20 characters (the SAM limit), same prefix as slot 1", last)
	}
	names := BrowserAccountNames("PlaywrightBackend", 3)
	if len(names) != 3 || names[0] != a || names[1] == names[0] || names[2] != a[:len(a)-1]+"3" {
		t.Fatalf("one account per slot: %v", names)
	}
	legacy := LegacyBrowserAccountName("PlaywrightBackend")
	if legacy != "pwbrowser-"+a[4:12] || len(legacy) > 20 {
		t.Fatalf("legacy name %q", legacy)
	}
	if !strings.HasPrefix(browserSecretKey(a), "L$") {
		t.Fatal("the password must be a local LSA secret")
	}
}

func TestStaleBrowserAccounts(t *testing.T) {
	const svc = "PlaywrightBackend"
	stale := StaleBrowserAccounts(svc, 2)
	if len(stale) != 1+MaxSlots-2 || stale[0] != LegacyBrowserAccountName(svc) || stale[1] != BrowserAccountName(svc, 3) || stale[len(stale)-1] != BrowserAccountName(svc, MaxSlots) {
		t.Fatalf("2 slots: want the legacy account and slots 3-%d, got %v", MaxSlots, stale)
	}
	for _, n := range stale {
		if n == BrowserAccountName(svc, 1) || n == BrowserAccountName(svc, 2) {
			t.Fatalf("%s is in use", n)
		}
	}
	// What an old backend.json lists: the legacy account (named twice, removed once), an account of another
	// service name, the slots' own (kept) and names pwrunner never makes (never removed).
	other := BrowserAccountName("Renamed", 1)
	stale = StaleBrowserAccounts(svc, 2, LegacyBrowserAccountName(svc), "", other, BrowserAccountName(svc, 1), "Administrator", "pwb-zz-1", "PWB-"+other[4:])
	count := map[string]int{}
	for _, n := range stale {
		count[strings.ToLower(n)]++
	}
	if count[strings.ToLower(LegacyBrowserAccountName(svc))] != 1 || count[other] != 1 || count["administrator"] != 0 || count["pwb-zz-1"] != 0 || count[strings.ToLower(BrowserAccountName(svc, 1))] != 0 {
		t.Fatalf("listed accounts: %v", stale)
	}
	if all := StaleBrowserAccounts(svc, 0); len(all) != 1+MaxSlots || all[1] != BrowserAccountName(svc, 1) {
		t.Fatalf("uninstall removes every slot's account and the legacy one: %v", all)
	}
}

func TestNewPassword(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		p, err := NewPassword()
		if err != nil {
			t.Fatal(err)
		}
		if len(p) != passwordLength || seen[p] {
			t.Fatalf("%d characters, repeated %v", len(p), seen[p])
		}
		seen[p] = true
		for _, class := range []string{passwordUpper, passwordLower, passwordDigits, passwordSymbol} {
			if !strings.ContainsAny(p, class) {
				t.Fatalf("no character from %q", class)
			}
		}
		if strings.Trim(p, passwordUpper+passwordLower+passwordDigits+passwordSymbol) != "" {
			t.Fatal("character outside the set")
		}
	}
}
