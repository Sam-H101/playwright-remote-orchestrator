package winsys

import (
	"fmt"
	"os"
	"strings"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// Creates, repairs and removes a real local account, so it runs elevated only (CI's Windows runner is).
func TestBrowserAccountLifecycle(t *testing.T) {
	elevated(t)
	name := BrowserAccountName(fmt.Sprintf("pwrunner-test-%d-%d", os.Getpid(), time.Now().UnixNano()), 1)
	t.Cleanup(func() { RemoveBrowserAccount(name) })

	created, err := EnsureBrowserAccount(name, "pwrunner test account")
	if err != nil || !created {
		t.Fatalf("create: %v %v", created, err)
	}
	first, err := retrieveSecret(browserSecretKey(name))
	if err != nil || len(first) < passwordLength {
		t.Fatalf("stored password: %d chars, %v", len(first), err)
	}
	firstPassword := windows.UTF16ToString(first)
	created, err = EnsureBrowserAccount(name, "pwrunner test account")
	if err != nil || created {
		t.Fatalf("repair: %v %v", created, err)
	}
	second, _ := retrieveSecret(browserSecretKey(name))
	if windows.UTF16ToString(second) == firstPassword {
		t.Fatal("a re-run must set a new password")
	}
	if p, err := BrowserAccountProblem(name); err != nil || p != "" {
		t.Fatalf("problem %q %v", p, err)
	}
	groups, err := localGroups(name)
	users, _ := builtinGroupName(SIDUsers)
	if err != nil || len(groups) != 1 || groups[0] != users {
		t.Fatalf("groups %v %v: want only %s", groups, err, users)
	}
	k, err := registry.OpenKey(registry.LOCAL_MACHINE, hiddenUsersKey, registry.QUERY_VALUE)
	if err != nil {
		t.Fatal(err)
	}
	if v, _, err := k.GetIntegerValue(name); err != nil || v != 0 {
		t.Fatalf("not hidden from the sign-in screen: %v %v", v, err)
	}
	k.Close()

	// The stored password signs the account in as a batch job, and the token is no administrator's.
	u, _ := windows.UTF16PtrFromString(name)
	dot, _ := windows.UTF16PtrFromString(".")
	var token windows.Token
	if r, _, e := procLogonUserW.Call(uintptr(unsafe.Pointer(u)), uintptr(unsafe.Pointer(dot)), uintptr(unsafe.Pointer(&second[0])),
		logon32LogonBatch, logon32ProviderDefault, uintptr(unsafe.Pointer(&token))); r == 0 {
		t.Fatalf("batch logon with the stored password: %v", e)
	}
	if err := notAdmin(token, name); err != nil {
		t.Fatal(err)
	}
	token.Close()
	// An interactive logon is denied.
	if r, _, _ := procLogonUserW.Call(uintptr(unsafe.Pointer(u)), uintptr(unsafe.Pointer(dot)), uintptr(unsafe.Pointer(&second[0])),
		2, logon32ProviderDefault, uintptr(unsafe.Pointer(&token))); r != 0 {
		token.Close()
		t.Fatal("an interactive logon succeeded")
	}

	removed, err := RemoveBrowserAccount(name)
	if err != nil || !removed {
		t.Fatalf("remove: %v %v", removed, err)
	}
	if p, _ := BrowserAccountProblem(name); p != "does not exist" {
		t.Fatalf("after remove: %q", p)
	}
	if pw, _ := retrieveSecret(browserSecretKey(name)); pw != nil {
		t.Fatal("the stored password survived the removal")
	}
}

func TestLauncherWithoutAccountFailsClosed(t *testing.T) {
	l := NewBrowserLauncher("", 3)
	defer l.Close()
	err := l.Open()
	if err == nil {
		t.Fatal("no account must mean no run-server, not one as LocalSystem")
	}
	if !strings.Contains(err.Error(), "slot 3") || !strings.Contains(err.Error(), "pwrunner install") {
		t.Fatalf("the error must name the slot and the fix: %v", err)
	}
}
