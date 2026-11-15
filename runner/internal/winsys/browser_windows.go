package winsys

import (
	"errors"
	"fmt"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

// The browser account: a local user the service starts run-server (and so the browsers) as, instead of
// LocalSystem. A check controls its browser, and a browser can read any file its account can through
// file://, so as SYSTEM a check could read agent.token, and every other file on the machine. The account
// is an ordinary member of Users: it can read and run app\, runtime\ and browsers\, but not agent.token
// (SYSTEM and Administrators only) or its own password (LSA private data). x/sys/windows lacks the LSA,
// NetUser*, profile and window station calls, hence the procs below.

var (
	modadvapi32 = windows.NewLazySystemDLL("advapi32.dll")
	modnetapi32 = windows.NewLazySystemDLL("netapi32.dll")
	moduserenv  = windows.NewLazySystemDLL("userenv.dll")
	moduser32   = windows.NewLazySystemDLL("user32.dll")

	procLogonUserW             = modadvapi32.NewProc("LogonUserW")
	procLsaOpenPolicy          = modadvapi32.NewProc("LsaOpenPolicy")
	procLsaClose               = modadvapi32.NewProc("LsaClose")
	procLsaStorePrivateData    = modadvapi32.NewProc("LsaStorePrivateData")
	procLsaRetrievePrivateData = modadvapi32.NewProc("LsaRetrievePrivateData")
	procLsaFreeMemory          = modadvapi32.NewProc("LsaFreeMemory")
	procLsaAddAccountRights    = modadvapi32.NewProc("LsaAddAccountRights")
	procLsaRemoveAccountRights = modadvapi32.NewProc("LsaRemoveAccountRights")
	procRegUnLoadKeyW          = modadvapi32.NewProc("RegUnLoadKeyW")

	procNetUserAdd              = modnetapi32.NewProc("NetUserAdd")
	procNetUserSetInfo          = modnetapi32.NewProc("NetUserSetInfo")
	procNetUserDel              = modnetapi32.NewProc("NetUserDel")
	procNetUserGetLocalGroups   = modnetapi32.NewProc("NetUserGetLocalGroups")
	procNetLocalGroupAddMembers = modnetapi32.NewProc("NetLocalGroupAddMembers")
	procNetLocalGroupDelMembers = modnetapi32.NewProc("NetLocalGroupDelMembers")

	procLoadUserProfileW   = moduserenv.NewProc("LoadUserProfileW")
	procUnloadUserProfile  = moduserenv.NewProc("UnloadUserProfile")
	procDeleteProfileW     = moduserenv.NewProc("DeleteProfileW")
	procGetProcessWinSta   = moduser32.NewProc("GetProcessWindowStation")
	procGetThreadDesktop   = moduser32.NewProc("GetThreadDesktop")
	procGetUserObjectInfo  = moduser32.NewProc("GetUserObjectInformationW")
	procOpenWindowStationW = moduser32.NewProc("OpenWindowStationW")
	procOpenDesktopW       = moduser32.NewProc("OpenDesktopW")
	procCloseWindowStation = moduser32.NewProc("CloseWindowStation")
	procCloseDesktop       = moduser32.NewProc("CloseDesktop")
)

const (
	logon32LogonBatch      = 4 // a batch logon: the account holds SeBatchLogonRight and is denied every other kind
	logon32ProviderDefault = 0

	policyGetPrivateInformation = 0x00000004
	policyCreateAccount         = 0x00000010
	policyCreateSecret          = 0x00000020
	policyLookupNames           = 0x00000800

	userPrivUser       = 1
	ufScript           = 0x0001
	ufPasswdCantChange = 0x0040
	ufDontExpirePasswd = 0x10000
	lgIncludeIndirect  = 1
	maxPreferredLength = 0xFFFFFFFF
	nerrGroupNotFound  = 2220
	nerrUserNotFound   = 2221
	nerrUserExists     = 2224
	nerrPasswordPolicy = 2245

	piNoUI  = 1
	uoiName = 2
	sidUser = 1 // SidTypeUser

	// What the browser account gets on the service's window station and desktop: enough to connect and
	// create windows and atoms, not to read the screen or clipboard, hook other windows or switch desktops.
	// winsta: ENUMDESKTOPS | READATTRIBUTES | CREATEDESKTOP | ACCESSGLOBALATOMS | ENUMERATE | READ_CONTROL
	winstaRights = 0x0001 | 0x0002 | 0x0008 | 0x0020 | 0x0100 | windows.READ_CONTROL
	// desktop: READOBJECTS | CREATEWINDOW | CREATEMENU | ENUMERATE | WRITEOBJECTS | READ_CONTROL
	desktopRights = 0x0001 | 0x0002 | 0x0004 | 0x0040 | 0x0080 | windows.READ_CONTROL
)

// browserRights are granted to the account; everything but a batch logon is denied.
var browserRights = []string{"SeBatchLogonRight", "SeDenyInteractiveLogonRight", "SeDenyRemoteInteractiveLogonRight", "SeDenyNetworkLogonRight"}

const hiddenUsersKey = `SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList`

type lsaString struct {
	Length, MaximumLength uint16
	Buffer                *uint16
}

func newLsaString(u []uint16) lsaString {
	// u is NUL-terminated (windows.UTF16FromString); Length counts bytes without the NUL.
	n := uint16((len(u) - 1) * 2)
	return lsaString{Length: n, MaximumLength: n + 2, Buffer: &u[0]}
}

type lsaObjectAttributes struct {
	Length                   uint32
	RootDirectory            windows.Handle
	ObjectName               *lsaString
	Attributes               uint32
	SecurityDescriptor       uintptr
	SecurityQualityOfService uintptr
}

func ntError(op string, status uintptr) error {
	return fmt.Errorf("%s: %w", op, windows.NTStatus(uint32(status)).Errno())
}

func netError(op string, status uintptr) error {
	switch status {
	case nerrPasswordPolicy:
		return fmt.Errorf("%s: the password does not meet this machine's password policy", op)
	case nerrUserNotFound:
		return fmt.Errorf("%s: no such user", op)
	}
	return fmt.Errorf("%s: %w", op, syscall.Errno(status))
}

func lsaOpen(access uint32) (windows.Handle, error) {
	oa := lsaObjectAttributes{}
	oa.Length = uint32(unsafe.Sizeof(oa))
	var h windows.Handle
	if r, _, _ := procLsaOpenPolicy.Call(0, uintptr(unsafe.Pointer(&oa)), uintptr(access), uintptr(unsafe.Pointer(&h))); r != 0 {
		return 0, ntError("LsaOpenPolicy", r)
	}
	return h, nil
}

func lsaClose(h windows.Handle) { procLsaClose.Call(uintptr(h)) }

// storeSecret sets the LSA private data key to value, or deletes it when value is nil.
func storeSecret(key string, value []uint16) error {
	h, err := lsaOpen(policyCreateSecret)
	if err != nil {
		return err
	}
	defer lsaClose(h)
	k, err := windows.UTF16FromString(key)
	if err != nil {
		return err
	}
	ks := newLsaString(k)
	var data uintptr
	var vs lsaString
	if value != nil {
		vs = newLsaString(value)
		data = uintptr(unsafe.Pointer(&vs))
	}
	r, _, _ := procLsaStorePrivateData.Call(uintptr(h), uintptr(unsafe.Pointer(&ks)), data)
	runtime.KeepAlive(value)
	runtime.KeepAlive(&vs)
	if r != 0 && !(value == nil && windows.NTStatus(uint32(r)) == windows.STATUS_OBJECT_NAME_NOT_FOUND) {
		return ntError("LsaStorePrivateData", r)
	}
	return nil
}

// retrieveSecret reads LSA private data as a NUL-terminated UTF-16 string the caller clears after use;
// nil when there is none.
func retrieveSecret(key string) ([]uint16, error) {
	h, err := lsaOpen(policyGetPrivateInformation)
	if err != nil {
		return nil, err
	}
	defer lsaClose(h)
	k, err := windows.UTF16FromString(key)
	if err != nil {
		return nil, err
	}
	ks := newLsaString(k)
	var data *lsaString
	r, _, _ := procLsaRetrievePrivateData.Call(uintptr(h), uintptr(unsafe.Pointer(&ks)), uintptr(unsafe.Pointer(&data)))
	if windows.NTStatus(uint32(r)) == windows.STATUS_OBJECT_NAME_NOT_FOUND {
		return nil, nil
	}
	if r != 0 {
		return nil, ntError("LsaRetrievePrivateData", r)
	}
	defer procLsaFreeMemory.Call(uintptr(unsafe.Pointer(data)))
	if data == nil || data.Buffer == nil || data.Length == 0 {
		return nil, nil
	}
	src := unsafe.Slice(data.Buffer, data.Length/2)
	out := make([]uint16, len(src)+1)
	copy(out, src)
	clear(src)
	return out, nil
}

func clearUTF16(u []uint16) { clear(u) }

// setRights adds rights to the account, or with add false removes all of its rights.
func setRights(sid *windows.SID, add bool, rights []string) error {
	h, err := lsaOpen(policyLookupNames | policyCreateAccount)
	if err != nil {
		return err
	}
	defer lsaClose(h)
	if !add {
		r, _, _ := procLsaRemoveAccountRights.Call(uintptr(h), uintptr(unsafe.Pointer(sid)), 1, 0, 0)
		if r != 0 && windows.NTStatus(uint32(r)) != windows.STATUS_OBJECT_NAME_NOT_FOUND {
			return ntError("LsaRemoveAccountRights", r)
		}
		return nil
	}
	list := make([]lsaString, len(rights))
	bufs := make([][]uint16, len(rights))
	for i, name := range rights {
		if bufs[i], err = windows.UTF16FromString(name); err != nil {
			return err
		}
		list[i] = newLsaString(bufs[i])
	}
	r, _, _ := procLsaAddAccountRights.Call(uintptr(h), uintptr(unsafe.Pointer(sid)), uintptr(unsafe.Pointer(&list[0])), uintptr(len(list)))
	runtime.KeepAlive(bufs)
	if r != 0 {
		return ntError("LsaAddAccountRights", r)
	}
	return nil
}

// localAccount is the SID of a local user (computer\name, so a domain account of the same name is never
// picked), or nil when there is none.
func localAccount(name string) (*windows.SID, error) {
	computer, err := windows.ComputerName()
	if err != nil {
		return nil, err
	}
	sid, _, use, err := windows.LookupSID("", computer+`\`+name)
	if errors.Is(err, windows.ERROR_NONE_MAPPED) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if use != sidUser {
		return nil, fmt.Errorf("%s exists but is not a user account", name)
	}
	return sid, nil
}

func builtinGroupName(sid string) (string, error) {
	s, err := windows.StringToSid(sid)
	if err != nil {
		return "", err
	}
	name, _, _, err := s.LookupAccount("")
	return name, err
}

// localGroups lists the local groups the user is in, directly or through another group.
func localGroups(name string) ([]string, error) {
	u, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return nil, err
	}
	var buf *byte
	var read, total uint32
	r, _, _ := procNetUserGetLocalGroups.Call(0, uintptr(unsafe.Pointer(u)), 0, lgIncludeIndirect, uintptr(unsafe.Pointer(&buf)),
		maxPreferredLength, uintptr(unsafe.Pointer(&read)), uintptr(unsafe.Pointer(&total)))
	if r != 0 {
		return nil, netError("NetUserGetLocalGroups", r)
	}
	defer windows.NetApiBufferFree(buf)
	var out []string
	for _, p := range unsafe.Slice((**uint16)(unsafe.Pointer(buf)), read) {
		out = append(out, windows.UTF16PtrToString(p))
	}
	return out, nil
}

func groupMember(add bool, group, member string) error {
	g, err := windows.UTF16PtrFromString(group)
	if err != nil {
		return err
	}
	m, err := windows.UTF16PtrFromString(member)
	if err != nil {
		return err
	}
	info := struct{ domainAndName *uint16 }{m} // LOCALGROUP_MEMBERS_INFO_3
	call, op, fine := procNetLocalGroupDelMembers, "NetLocalGroupDelMembers", windows.ERROR_MEMBER_NOT_IN_ALIAS
	if add {
		call, op, fine = procNetLocalGroupAddMembers, "NetLocalGroupAddMembers", windows.ERROR_MEMBER_IN_ALIAS
	}
	r, _, _ := call.Call(0, uintptr(unsafe.Pointer(g)), 3, uintptr(unsafe.Pointer(&info)), 1)
	if r != 0 && syscall.Errno(r) != fine && r != nerrGroupNotFound {
		return netError(op+" "+group, r)
	}
	return nil
}

// EnsureBrowserAccount creates the browser account, or repairs an existing one, and gives it a new random
// password, stored only in LSA private data. It returns whether the account is new. The account is in
// Users and no other local group, holds only the batch logon right, is denied interactive, remote
// desktop and network logons, and is hidden from the sign-in screen. The password is never logged.
func EnsureBrowserAccount(name, comment string) (created bool, err error) {
	pw, err := NewPassword()
	if err != nil {
		return false, err
	}
	pw16, err := windows.UTF16FromString(pw)
	if err != nil {
		return false, err
	}
	defer clearUTF16(pw16)
	key := browserSecretKey(name)
	previous, err := retrieveSecret(key)
	if err != nil {
		return false, fmt.Errorf("reading the stored password: %w", err)
	}
	defer clearUTF16(previous)
	// Stored first: if the account can't take the password, the previous one is put back, so an install that
	// fails here leaves the service able to sign in as before.
	if err := storeSecret(key, pw16); err != nil {
		return false, fmt.Errorf("storing the password: %w", err)
	}
	if created, err = setAccount(name, comment, pw16); err != nil {
		if rerr := storeSecret(key, previous); rerr != nil {
			err = fmt.Errorf("%w (and restoring the previous password failed: %v)", err, rerr)
		}
		return false, err
	}
	sid, err := localAccount(name)
	if err != nil || sid == nil {
		return created, fmt.Errorf("looking up %s after creating it: %v", name, err)
	}
	computer, _ := windows.ComputerName()
	member := computer + `\` + name
	users, err := builtinGroupName(SIDUsers)
	if err != nil {
		return created, err
	}
	if err := groupMember(true, users, member); err != nil {
		return created, err
	}
	groups, err := localGroups(name)
	if err != nil {
		return created, err
	}
	for _, g := range groups {
		if !strings.EqualFold(g, users) {
			if err := groupMember(false, g, member); err != nil {
				return created, err
			}
		}
	}
	if err := setRights(sid, false, nil); err != nil {
		return created, err
	}
	if err := setRights(sid, true, browserRights); err != nil {
		return created, err
	}
	k, _, err := registry.CreateKey(registry.LOCAL_MACHINE, hiddenUsersKey, registry.SET_VALUE)
	if err != nil {
		return created, fmt.Errorf("hiding %s from the sign-in screen: %w", name, err)
	}
	defer k.Close()
	if err := k.SetDWordValue(name, 0); err != nil {
		return created, fmt.Errorf("hiding %s from the sign-in screen: %w", name, err)
	}
	if problem, err := BrowserAccountProblem(name); err != nil || problem != "" {
		return created, fmt.Errorf("%s %s %v", name, problem, errOrNil(err))
	}
	return created, nil
}

func errOrNil(err error) string {
	if err == nil {
		return ""
	}
	return "(" + err.Error() + ")"
}

// setAccount creates the user, or sets the password, flags and comment of the existing one.
func setAccount(name, comment string, pw16 []uint16) (bool, error) {
	n, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return false, err
	}
	c, err := windows.UTF16PtrFromString(comment)
	if err != nil {
		return false, err
	}
	flags := uint32(ufScript | ufPasswdCantChange | ufDontExpirePasswd)
	info := struct { // USER_INFO_1
		name, password *uint16
		passwordAge    uint32
		priv           uint32
		homeDir        *uint16
		comment        *uint16
		flags          uint32
		scriptPath     *uint16
	}{name: n, password: &pw16[0], priv: userPrivUser, comment: c, flags: flags}
	r, _, _ := procNetUserAdd.Call(0, 1, uintptr(unsafe.Pointer(&info)), 0)
	runtime.KeepAlive(pw16)
	if r == 0 {
		return true, nil
	}
	if r != nerrUserExists {
		return false, netError("NetUserAdd "+name, r)
	}
	set := func(level uintptr, p unsafe.Pointer) error {
		if r, _, _ := procNetUserSetInfo.Call(0, uintptr(unsafe.Pointer(n)), level, uintptr(p), 0); r != 0 {
			return netError(fmt.Sprintf("NetUserSetInfo %s (level %d)", name, level), r)
		}
		return nil
	}
	password := struct{ password *uint16 }{&pw16[0]} // USER_INFO_1003
	if err := set(1003, unsafe.Pointer(&password)); err != nil {
		return false, err
	}
	runtime.KeepAlive(pw16)
	// USER_INFO_1008: this also clears a disabled or locked-out flag.
	if err := set(1008, unsafe.Pointer(&struct{ flags uint32 }{flags})); err != nil {
		return false, err
	}
	if err := set(1007, unsafe.Pointer(&struct{ comment *uint16 }{c})); err != nil {
		return false, err
	}
	return false, nil
}

// BrowserAccountProblem says what is wrong with the browser account, or "" when nothing is: it must exist,
// be in no Administrators group and, when this process can tell (elevated), have a stored password.
func BrowserAccountProblem(name string) (string, error) {
	sid, err := localAccount(name)
	if err != nil {
		return "", err
	}
	if sid == nil {
		return "does not exist", nil
	}
	admins, err := builtinGroupName(SIDAdministrators)
	if err != nil {
		return "", err
	}
	groups, err := localGroups(name)
	if err != nil {
		return "", err
	}
	for _, g := range groups {
		if strings.EqualFold(g, admins) {
			return "is a member of " + admins, nil
		}
	}
	if elevated, _ := IsElevated(); elevated {
		pw, err := retrieveSecret(browserSecretKey(name))
		if err != nil {
			return "", err
		}
		clearUTF16(pw)
		if pw == nil {
			return "has no stored password", nil
		}
	}
	return "", nil
}

// RemoveBrowserAccount deletes the account, its rights, profile, stored password and sign-in screen entry.
// It reports whether there was an account to delete.
func RemoveBrowserAccount(name string) (bool, error) {
	var errs []error
	sid, err := localAccount(name)
	if err != nil {
		return false, err
	}
	if sid != nil {
		// Before setRights takes the batch logon right away: releasing a leaked profile signs the account in.
		releaseErr := releaseLeakedProfile(name, sid.String())
		if err := setRights(sid, false, nil); err != nil {
			errs = append(errs, err)
		}
		if s, err := windows.UTF16PtrFromString(sid.String()); err == nil {
			// The service unloads the profile as it stops, but Windows may hold the hive a few seconds longer.
			// An agent that was killed (or crashed) never unloaded it, and Windows keeps that hive loaded until
			// a reboot: after a few tries the hive is unloaded here, which an elevated process may do.
			var unloadErr error
			for try := 1; ; try++ {
				r, _, e := procDeleteProfileW.Call(uintptr(unsafe.Pointer(s)), 0, 0)
				if r != 0 || errors.Is(e, windows.ERROR_FILE_NOT_FOUND) || errors.Is(e, windows.ERROR_PATH_NOT_FOUND) || errors.Is(e, windows.ERROR_INVALID_PARAMETER) {
					break
				}
				if try == 10 {
					err := fmt.Errorf("deleting the profile of %s: %w", name, e)
					if releaseErr != nil {
						err = fmt.Errorf("%w (releasing its loaded profile: %v)", err, releaseErr)
					}
					if unloadErr != nil {
						err = fmt.Errorf("%w (unloading its registry hive: %v)", err, unloadErr)
					}
					errs = append(errs, err)
					break
				}
				if try == 3 {
					unloadErr = unloadHive(sid.String())
				}
				time.Sleep(time.Second)
			}
		}
		n, _ := windows.UTF16PtrFromString(name)
		if r, _, _ := procNetUserDel.Call(0, uintptr(unsafe.Pointer(n))); r != 0 && r != nerrUserNotFound {
			errs = append(errs, netError("NetUserDel "+name, r))
		}
	}
	if err := storeSecret(browserSecretKey(name), nil); err != nil {
		errs = append(errs, err)
	}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, hiddenUsersKey, registry.SET_VALUE); err == nil {
		if err := k.DeleteValue(name); err != nil && !errors.Is(err, registry.ErrNotExist) {
			errs = append(errs, err)
		}
		k.Close()
	}
	return sid != nil, errors.Join(errs...)
}

// hiveLoaded reports whether HKEY_USERS\<sid> is loaded.
func hiveLoaded(sid string) bool {
	k, err := registry.OpenKey(registry.USERS, sid, registry.QUERY_VALUE)
	if err != nil {
		return false
	}
	k.Close()
	return true
}

// releaseLeakedProfile drops the references the User Profile Service still counts on an account's profile.
// The service process loads each browser account's profile and unloads it as it stops; a service that was
// killed (or crashed) never unloaded it, and the profile service then keeps the hive loaded, with RegUnLoadKey
// refused, until a reboot. Signed in as the account (with its stored password), each UnloadUserProfile drops
// one reference, until the hive is unloaded. A hive that isn't loaded, or an account without a stored
// password, is left as it is.
func releaseLeakedProfile(name, sid string) error {
	if !hiveLoaded(sid) {
		return nil
	}
	if err := enablePrivileges("SeBackupPrivilege", "SeRestorePrivilege"); err != nil {
		return err
	}
	pw, err := retrieveSecret(browserSecretKey(name))
	if err != nil || pw == nil {
		return err
	}
	defer clearUTF16(pw)
	user, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return err
	}
	dot, _ := windows.UTF16PtrFromString(".")
	var token windows.Token
	if r, _, e := procLogonUserW.Call(uintptr(unsafe.Pointer(user)), uintptr(unsafe.Pointer(dot)),
		uintptr(unsafe.Pointer(&pw[0])), logon32LogonBatch, logon32ProviderDefault, uintptr(unsafe.Pointer(&token))); r == 0 {
		return fmt.Errorf("signing in %s: %w", name, e)
	}
	defer token.Close()
	for try := 0; try < 32 && hiveLoaded(sid); try++ {
		k, err := registry.OpenKey(registry.USERS, sid, registry.READ)
		if err != nil {
			break
		}
		// UnloadUserProfile closes the handle.
		if r, _, e := procUnloadUserProfile.Call(uintptr(token), uintptr(k)); r == 0 {
			return fmt.Errorf("UnloadUserProfile: %w", e)
		}
		time.Sleep(250 * time.Millisecond)
	}
	if hiveLoaded(sid) {
		return fmt.Errorf(`HKEY_USERS\%s is still loaded`, sid)
	}
	return nil
}

// unloadHive unloads an account's registry hives (HKEY_USERS\<sid>_Classes, then HKEY_USERS\<sid>) that a
// process which loaded its profile left loaded by dying before UnloadUserProfile. Best effort: a hive a live
// process still has keys open in stays, and DeleteProfile then reports it. A hive that isn't loaded is no error.
func unloadHive(sid string) error {
	if err := enablePrivileges("SeBackupPrivilege", "SeRestorePrivilege"); err != nil {
		return err
	}
	var errs []error
	for _, key := range []string{sid + "_Classes", sid} {
		k, err := windows.UTF16PtrFromString(key)
		if err != nil {
			return err
		}
		r, _, _ := procRegUnLoadKeyW.Call(uintptr(windows.HKEY_USERS), uintptr(unsafe.Pointer(k)))
		if e := windows.Errno(r); r != 0 && e != windows.ERROR_FILE_NOT_FOUND && e != windows.ERROR_INVALID_PARAMETER {
			errs = append(errs, fmt.Errorf(`HKEY_USERS\%s: %w`, key, e))
		}
	}
	return errors.Join(errs...)
}

// BrowserLauncher starts one slot's run-servers as that slot's browser account from the service
// (LocalSystem): it signs the account in once (a batch logon with the stored password), loads its profile
// so its folders and registry hive exist, and gives it access to the service's window station and desktop.
type BrowserLauncher struct {
	account string
	slot    int
	mu      sync.Mutex
	token   windows.Token
	profile windows.Handle
	env     []string
	revoke  func()
}

// NewBrowserLauncher is the launcher of slot's run-servers; account "" means the slot has none, and its
// run-server is then never started.
func NewBrowserLauncher(account string, slot int) *BrowserLauncher {
	return &BrowserLauncher{account: account, slot: slot}
}

const reinstall = "run pwrunner install again, elevated, to set it up"

// Open signs the account in, unless it already is. Every failure leaves run-server unstarted: the service
// never falls back to running the browsers as LocalSystem, or as another slot's account.
func (l *BrowserLauncher) Open() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.token != 0 {
		return nil
	}
	if l.account == "" {
		return fmt.Errorf("this install has no browser account for slot %d (backend.json's browserAccounts), and run-server is not started as LocalSystem or as another slot's account: %s", l.slot, reinstall)
	}
	// CreateProcessAsUser needs the first two; LoadUserProfile the last two. LocalSystem holds all four.
	if err := enablePrivileges("SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege", "SeBackupPrivilege", "SeRestorePrivilege"); err != nil {
		return fmt.Errorf("starting run-server as %s needs the service to run as LocalSystem: %w", l.account, err)
	}
	pw, err := retrieveSecret(browserSecretKey(l.account))
	if err != nil {
		return fmt.Errorf("can't read the password of the browser account %s: %w", l.account, err)
	}
	defer clearUTF16(pw)
	if pw == nil {
		return fmt.Errorf("no password is stored for the browser account %s: %s", l.account, reinstall)
	}
	user, err := windows.UTF16PtrFromString(l.account)
	if err != nil {
		return err
	}
	dot, _ := windows.UTF16PtrFromString(".")
	var token windows.Token
	r, _, e := procLogonUserW.Call(uintptr(unsafe.Pointer(user)), uintptr(unsafe.Pointer(dot)),
		uintptr(unsafe.Pointer(&pw[0])), logon32LogonBatch, logon32ProviderDefault, uintptr(unsafe.Pointer(&token)))
	if r == 0 {
		return fmt.Errorf("can't sign in the browser account %s (%v): %s", l.account, e, reinstall)
	}
	ok := false
	defer func() {
		if !ok {
			l.release(token)
		}
	}()
	if err := notAdmin(token, l.account); err != nil {
		return err
	}
	pi := struct { // PROFILEINFOW
		size, flags                                           uint32
		userName, profilePath, defaultPath, server, policyDir *uint16
		profile                                               windows.Handle
	}{flags: piNoUI, userName: user}
	pi.size = uint32(unsafe.Sizeof(pi))
	if r, _, e := procLoadUserProfileW.Call(uintptr(token), uintptr(unsafe.Pointer(&pi))); r == 0 {
		return fmt.Errorf("can't load the profile of the browser account %s: %w", l.account, e)
	}
	l.profile = pi.profile
	if l.env, err = token.Environ(false); err != nil {
		return fmt.Errorf("can't build the environment of the browser account %s: %w", l.account, err)
	}
	tu, err := token.GetTokenUser()
	if err != nil {
		return err
	}
	if l.revoke, err = grantWindowStation(tu.User.Sid); err != nil {
		return fmt.Errorf("can't give the browser account %s the service's desktop: %w", l.account, err)
	}
	l.token, ok = token, true
	return nil
}

// notAdmin refuses a token with Administrators in it, enabled or deny-only: the account must not be one.
func notAdmin(token windows.Token, account string) error {
	groups, err := token.GetTokenGroups()
	if err != nil {
		return err
	}
	for _, g := range groups.AllGroups() {
		if s := g.Sid.String(); s == SIDAdministrators {
			return fmt.Errorf("the browser account %s is a member of Administrators, so run-server is not started as it: %s", account, reinstall)
		}
	}
	return nil
}

func (l *BrowserLauncher) release(token windows.Token) {
	if l.revoke != nil {
		l.revoke()
		l.revoke = nil
	}
	if l.profile != 0 {
		procUnloadUserProfile.Call(uintptr(token), uintptr(l.profile))
		l.profile = 0
	}
	token.Close()
}

// Prepare makes cmd start as the browser account, with the account's profile folders and user name in
// its environment (the rest of cmd.Env is kept).
func (l *BrowserLauncher) Prepare(cmd *exec.Cmd) error {
	if err := l.Open(); err != nil {
		return err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Token = syscall.Token(l.token)
	cmd.Env = proc.ForUser(cmd.Env, l.env)
	return nil
}

// Close unloads the profile, takes the window station and desktop access back and closes the token. Call
// it once the run-servers have stopped.
func (l *BrowserLauncher) Close() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.token != 0 {
		l.release(l.token)
		l.token = 0
	}
}

func userObjectName(h uintptr) (string, error) {
	buf := make([]uint16, 256)
	var need uint32
	if r, _, e := procGetUserObjectInfo.Call(h, uoiName, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)*2), uintptr(unsafe.Pointer(&need))); r == 0 {
		return "", e
	}
	return windows.UTF16ToString(buf), nil
}

// grantWindowStation gives sid access to this process's window station and desktop. A process started
// with another user's token from a service gets the service's window station and desktop (session 0's
// Service-0x0-3e7$\Default), and without access to them user32 fails to initialise and the process dies at
// start with 0xC0000142. The returned func takes the access back.
func grantWindowStation(sid *windows.SID) (func(), error) {
	winstaMu.Lock()
	defer winstaMu.Unlock()
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	ws, _, e := procGetProcessWinSta.Call()
	if ws == 0 {
		return nil, fmt.Errorf("GetProcessWindowStation: %w", e)
	}
	wsName, err := userObjectName(ws)
	if err != nil {
		return nil, fmt.Errorf("window station name: %w", err)
	}
	deskName := "Default"
	if d, _, _ := procGetThreadDesktop.Call(uintptr(windows.GetCurrentThreadId())); d != 0 {
		if n, err := userObjectName(d); err == nil && n != "" {
			deskName = n
		}
	}
	set := func(mode windows.ACCESS_MODE) error {
		return errors.Join(
			onUserObject(procOpenWindowStationW, procCloseWindowStation, wsName, func(h windows.Handle) error { return setUserObjectACE(h, sid, winstaRights, mode) }),
			onUserObject(procOpenDesktopW, procCloseDesktop, deskName, func(h windows.Handle) error { return setUserObjectACE(h, sid, desktopRights, mode) }),
		)
	}
	if err := set(windows.GRANT_ACCESS); err != nil {
		set(windows.REVOKE_ACCESS)
		return nil, err
	}
	return func() {
		winstaMu.Lock()
		defer winstaMu.Unlock()
		set(windows.REVOKE_ACCESS)
	}, nil
}

// winstaMu serialises changes to the window station's and desktop's DACLs: each slot's launcher reads,
// changes and writes them back, and two at once would drop one another's entry.
var winstaMu sync.Mutex

// onUserObject opens a window station (OpenWindowStationW) or a desktop of this process's window station
// (OpenDesktopW) by name for changing its DACL, runs fn on it and closes it.
func onUserObject(open, closeFn *windows.LazyProc, name string, fn func(windows.Handle) error) error {
	n, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return err
	}
	access := uintptr(windows.READ_CONTROL | windows.WRITE_DAC)
	var h uintptr
	var e error
	if open == procOpenDesktopW {
		h, _, e = open.Call(uintptr(unsafe.Pointer(n)), 0, 0, access)
	} else {
		h, _, e = open.Call(uintptr(unsafe.Pointer(n)), 0, access)
	}
	if h == 0 {
		return fmt.Errorf("opening %s: %w", name, e)
	}
	defer closeFn.Call(h)
	if err := fn(windows.Handle(h)); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

func setUserObjectACE(h windows.Handle, sid *windows.SID, mask uint32, mode windows.ACCESS_MODE) error {
	sd, err := windows.GetSecurityInfo(h, windows.SE_WINDOW_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	if dacl == nil {
		return nil // a NULL DACL already lets everyone in; a new one would lock everyone else out
	}
	acl, err := windows.ACLFromEntries([]windows.EXPLICIT_ACCESS{{
		AccessPermissions: windows.ACCESS_MASK(mask),
		AccessMode:        mode,
		Inheritance:       windows.NO_INHERITANCE,
		Trustee:           windows.TRUSTEE{TrusteeForm: windows.TRUSTEE_IS_SID, TrusteeType: windows.TRUSTEE_IS_USER, TrusteeValue: windows.TrusteeValueFromSID(sid)},
	}}, dacl)
	if err != nil {
		return err
	}
	return windows.SetSecurityInfo(h, windows.SE_WINDOW_OBJECT, windows.DACL_SECURITY_INFORMATION, nil, nil, acl, nil)
}
