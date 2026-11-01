// Package winsys holds the Windows specifics of an install: ACLs, the token file, Job Objects, process
// discovery by image path and elevation. Other platforms get stubs.
package winsys

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
)

const (
	SIDSystem         = "S-1-5-18"
	SIDAdministrators = "S-1-5-32-544"
	SIDUsers          = "S-1-5-32-545"
	// GENERIC_ALL/WRITE, WRITE_OWNER, WRITE_DAC, DELETE and every write, append and delete-child right.
	UntrustedWriteMask = 0x500D0156
	usersReadExecute   = "0x1200a9"
)

// SDDLInstallDir: owner Administrators, protected, SYSTEM and Administrators full control, Users read and
// execute, all inherited by files and folders below.
const (
	SDDLInstallDir = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;" + usersReadExecute + ";;;BU)"
	SDDLTokenFile  = "O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)"
)

// IsElevated reports whether this process runs elevated (UAC).
func IsElevated() (bool, error) { return windows.GetCurrentProcessToken().IsElevated(), nil }

func currentUserSID() (string, error) {
	u, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return "", err
	}
	return u.User.Sid.String(), nil
}

// securityFor is the SDDL for an install item: SYSTEM and Administrators full control, Users read and
// execute when usersCanRead, and when not elevated the current user too (a --no-service agent runs as
// that user) with no owner change (only an administrator can make Administrators the owner).
func securityFor(dir, usersCanRead, elevated bool) (string, error) {
	inherit := ""
	if dir {
		inherit = "OICI"
	}
	ace := func(rights, sid string) string { return fmt.Sprintf("(A;%s;%s;;;%s)", inherit, rights, sid) }
	sddl := "D:P" + ace("FA", "SY") + ace("FA", "BA")
	if !elevated {
		sid, err := currentUserSID()
		if err != nil {
			return "", err
		}
		sddl += ace("FA", sid)
	} else {
		sddl = "O:BA" + sddl
	}
	if usersCanRead {
		sddl += ace(usersReadExecute, "BU")
	}
	return sddl, nil
}

func attrsFor(sddl string) (*windows.SecurityAttributes, error) {
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return nil, err
	}
	return &windows.SecurityAttributes{Length: uint32(unsafe.Sizeof(windows.SecurityAttributes{})), SecurityDescriptor: sd}, nil
}

// CreateLockedDir creates path already protected (its parents as usual), so nobody can put anything in
// it before it is locked.
func CreateLockedDir(path string, usersCanRead bool) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	elevated, _ := IsElevated()
	sddl, err := securityFor(true, usersCanRead, elevated)
	if err != nil {
		return err
	}
	sa, err := attrsFor(sddl)
	if err != nil {
		return err
	}
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	if err := windows.CreateDirectory(p, sa); err != nil && !errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
		return &os.PathError{Op: "mkdir", Path: path, Err: err}
	}
	return nil
}

// Protect gives an existing file or folder the install permissions; elevated, it first makes
// Administrators the owner (an owner can always change the permissions back).
func Protect(path string, usersCanRead bool) error {
	st, err := os.Lstat(path)
	if err != nil {
		return err
	}
	elevated, _ := IsElevated()
	if elevated {
		EnablePrivileges()
	}
	sddl, err := securityFor(st.IsDir(), usersCanRead, elevated)
	if err != nil {
		return err
	}
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	info := windows.SECURITY_INFORMATION(windows.DACL_SECURITY_INFORMATION | windows.PROTECTED_DACL_SECURITY_INFORMATION)
	var owner *windows.SID
	if elevated {
		owner, _, _ = sd.Owner()
		info |= windows.OWNER_SECURITY_INFORMATION
	}
	if err := windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, info, owner, nil, dacl, nil); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// setOwner makes sid the owner of path (needs SeTakeOwnershipPrivilege / SeRestorePrivilege).
func setOwner(path, sid string) error {
	s, err := windows.StringToSid(sid)
	if err != nil {
		return err
	}
	if err := windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.OWNER_SECURITY_INFORMATION, s, nil, nil, nil); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// inheritOnly drops an item's own entries and protection, so it has only what its folder passes down.
// The DACL is an empty one (from "D:"), never a NULL DACL, which would grant everyone everything.
func inheritOnly(path string) error {
	sd, err := windows.SecurityDescriptorFromString("D:")
	if err != nil {
		return err
	}
	empty, _, err := sd.DACL()
	if err != nil || empty == nil {
		return fmt.Errorf("empty DACL: %v", err)
	}
	info := windows.SECURITY_INFORMATION(windows.DACL_SECURITY_INFORMATION | windows.UNPROTECTED_DACL_SECURITY_INFORMATION)
	if err := windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, info, nil, nil, empty, nil); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

func trusted(sid string) bool { return sid == SIDSystem || sid == SIDAdministrators }

func accountName(sid *windows.SID) string {
	account, domain, _, err := sid.LookupAccount("")
	if err != nil {
		return sid.String()
	}
	if domain != "" {
		return domain + `\` + account
	}
	return account
}

// PathProblem says why users other than SYSTEM and Administrators could have changed what is under
// path, or "" when they could not: it must be owned by one of those two, not inherit its permissions,
// and grant no write right to anyone else.
func PathProblem(path string) (string, error) {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return fmt.Sprintf("has permissions that lock out Administrators (%v)", err), nil
	}
	owner, _, err := sd.Owner()
	if err != nil || owner == nil {
		return "has no owner", nil
	}
	if !trusted(owner.String()) {
		return "is owned by " + accountName(owner), nil
	}
	control, _, err := sd.Control()
	if err != nil {
		return "", err
	}
	if control&windows.SE_DACL_PROTECTED == 0 {
		return "inherits its permissions from its parent folder", nil
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return "", err
	}
	if dacl == nil {
		return "grants everyone full control (no DACL)", nil
	}
	var writers []string
	seen := map[string]bool{}
	for i := uint32(0); i < uint32(dacl.AceCount); i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, i, &ace); err != nil {
			return "", err
		}
		if ace.Header.AceType != windows.ACCESS_ALLOWED_ACE_TYPE || uint32(ace.Mask)&UntrustedWriteMask == 0 {
			continue
		}
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		if trusted(sid.String()) {
			continue
		}
		if name := accountName(sid); !seen[name] {
			seen[name] = true
			writers = append(writers, name)
		}
	}
	if len(writers) > 0 {
		sort.Strings(writers)
		return "is writable by " + strings.Join(writers, ", "), nil
	}
	return "", nil
}

func isReparsePoint(path string) bool {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return false
	}
	attrs, err := windows.GetFileAttributes(p)
	return err == nil && attrs&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0
}

// removeLink deletes a symlink or junction itself, never what it points to.
func removeLink(path string) error {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	attrs, _ := windows.GetFileAttributes(p)
	if attrs&windows.FILE_ATTRIBUTE_DIRECTORY != 0 {
		return windows.RemoveDirectory(p)
	}
	return windows.DeleteFile(p)
}

// resetTree makes Administrators the owner of everything below dir and drops each item's own
// permissions, deleting links instead of following them.
func resetTree(dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		p := filepath.Join(dir, e.Name())
		if isReparsePoint(p) {
			if err := removeLink(p); err != nil {
				return fmt.Errorf("can't delete link %s: %w", p, err)
			}
			continue
		}
		if err := setOwner(p, SIDAdministrators); err != nil {
			return err
		}
		if err := inheritOnly(p); err != nil {
			return err
		}
		if e.IsDir() {
			if err := resetTree(p); err != nil {
				return err
			}
		}
	}
	return nil
}

// ResetUntrusted takes over an install folder other users could change: lock the folder, take ownership
// of everything in it, delete links without following them, and delete everything except keep (names or
// "*.ext" patterns) and the running exe, so nothing planted there runs as SYSTEM. agent.token is locked
// and its content returned so it can be written to a fresh file.
func ResetUntrusted(installDir string, keep []string, log *logging.Logger) (string, error) {
	if err := EnablePrivileges(); err != nil {
		return "", err
	}
	if err := setOwner(installDir, SIDAdministrators); err != nil {
		return "", err
	}
	if err := Protect(installDir, true); err != nil {
		return "", err
	}
	self, _ := os.Executable()
	kept := func(name string) bool {
		for _, k := range keep {
			if ok, _ := filepath.Match(strings.ToLower(k), strings.ToLower(name)); ok {
				return true
			}
		}
		return false
	}
	entries, err := os.ReadDir(installDir)
	if err != nil {
		return "", err
	}
	token := ""
	for _, e := range entries {
		p := filepath.Join(installDir, e.Name())
		if isReparsePoint(p) {
			if err := removeLink(p); err != nil {
				return "", fmt.Errorf("can't delete link %s: %w", p, err)
			}
			continue
		}
		if strings.EqualFold(e.Name(), "agent.token") && !e.IsDir() {
			if err := Protect(p, false); err != nil {
				return "", err
			}
			token, _, _ = ReadTokenFile(p)
			continue
		}
		if err := setOwner(p, SIDAdministrators); err != nil {
			return "", err
		}
		if err := inheritOnly(p); err != nil {
			return "", err
		}
		if e.IsDir() {
			if err := resetTree(p); err != nil {
				return "", err
			}
		}
		if kept(e.Name()) || strings.EqualFold(p, self) {
			continue
		}
		if err := os.RemoveAll(p); err != nil {
			return "", fmt.Errorf("could not delete %s: %w", p, err)
		}
	}
	return token, nil
}

// WriteTokenFile writes the token to a new file that is locked from the moment it exists. An existing
// file is deleted first: its owner, or a handle opened on it earlier, could read what goes into it.
func WriteTokenFile(path, token string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	elevated, _ := IsElevated()
	sddl := SDDLTokenFile
	if !elevated {
		var err error
		if sddl, err = securityFor(false, false, false); err != nil {
			return err
		}
	}
	sa, err := attrsFor(sddl)
	if err != nil {
		return err
	}
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	h, err := windows.CreateFile(p, windows.GENERIC_WRITE, 0, sa, windows.CREATE_NEW, windows.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return &os.PathError{Op: "create", Path: path, Err: err}
	}
	f := os.NewFile(uintptr(h), path)
	_, err = f.Write([]byte(token))
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	return err
}

// TokenFileProblem checks the token file's DACL: protected, and only SYSTEM and Administrators.
func TokenFileProblem(path string) (string, error) {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return "", err
	}
	control, _, _ := sd.Control()
	if control&windows.SE_DACL_PROTECTED == 0 {
		return "inherits permissions", nil
	}
	dacl, _, err := sd.DACL()
	if err != nil || dacl == nil {
		return "has no DACL", err
	}
	var sids []string
	for i := uint32(0); i < uint32(dacl.AceCount); i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, i, &ace); err != nil {
			return "", err
		}
		sids = append(sids, (*windows.SID)(unsafe.Pointer(&ace.SidStart)).String())
	}
	sort.Strings(sids)
	if strings.Join(sids, ",") != SIDSystem+","+SIDAdministrators {
		return "grants access to " + strings.Join(sids, ", "), nil
	}
	return "", nil
}

// ReadTokenFile returns the trimmed token, and false when the file is missing or empty.
func ReadTokenFile(path string) (string, bool, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	t := strings.TrimSpace(string(bytes.TrimPrefix(b, []byte{0xEF, 0xBB, 0xBF})))
	return t, t != "", nil
}

// EnablePrivileges turns on SeTakeOwnershipPrivilege and SeRestorePrivilege for this process.
func EnablePrivileges() error {
	var token windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_ADJUST_PRIVILEGES|windows.TOKEN_QUERY, &token); err != nil {
		return err
	}
	defer token.Close()
	for _, name := range []string{"SeTakeOwnershipPrivilege", "SeRestorePrivilege"} {
		var luid windows.LUID
		n, _ := windows.UTF16PtrFromString(name)
		if err := windows.LookupPrivilegeValue(nil, n, &luid); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		tp := windows.Tokenprivileges{PrivilegeCount: 1}
		tp.Privileges[0] = windows.LUIDAndAttributes{Luid: luid, Attributes: windows.SE_PRIVILEGE_ENABLED}
		if err := windows.AdjustTokenPrivileges(token, false, &tp, 0, nil, nil); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		if errno := windows.GetLastError(); errno == windows.ERROR_NOT_ALL_ASSIGNED {
			return fmt.Errorf("%s: not held by this account", name)
		}
	}
	return nil
}
