// Package proc spawns and stops the agent's child processes.
package proc

import (
	"os/exec"
	goruntime "runtime"
	"sort"
	"strings"
)

// allowedEnv lists the variables children inherit from the agent (decision 14 of the plan). It is an
// allow-list so a machine-wide NODE_OPTIONS, BROWSERS, EPHEMERAL, HUB_URL or the agent token never reach
// run-server or the browser installer. Besides the plan's list it keeps the variables Windows itself and
// Chromium expect in a user environment (profile folders, computer and user name) and, on Linux, HOME,
// USER, the locale and the display.
var allowedEnv = []string{
	"SystemRoot", "SystemDrive", "windir", "PATH", "TEMP", "TMP", "PATHEXT", "ProgramData", "LOCALAPPDATA",
	"APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PUBLIC", "ALLUSERSPROFILE", "ProgramFiles", "ProgramFiles(x86)",
	"ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "ComSpec", "COMPUTERNAME",
	"USERNAME", "USERDOMAIN", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
	"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "DISPLAY",
	"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
	"PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_DOWNLOAD_HOST",
}

// ChildEnv builds a child's environment from environ (os.Environ()): only allow-listed variables, then
// set (a value of "" removes the variable). Windows matches names case-insensitively.
func ChildEnv(environ []string, set map[string]string) []string {
	fold := goruntime.GOOS == "windows"
	key := func(k string) string {
		if fold {
			return strings.ToUpper(k)
		}
		return k
	}
	allowed := map[string]bool{}
	for _, k := range allowedEnv {
		allowed[key(k)] = true
	}
	out := map[string]string{}
	names := map[string]string{}
	for _, kv := range environ {
		k, v, ok := strings.Cut(kv, "=")
		if !ok || k == "" || !allowed[key(k)] {
			continue
		}
		out[key(k)] = v
		names[key(k)] = k
	}
	for k, v := range set {
		if v == "" {
			delete(out, key(k))
			continue
		}
		out[key(k)] = v
		names[key(k)] = k
	}
	env := make([]string, 0, len(out))
	for k, v := range out {
		env = append(env, names[k]+"="+v)
	}
	sort.Strings(env)
	return env
}

// LookupEnv returns a variable's value from an environ list.
func LookupEnv(environ []string, name string) string {
	for _, kv := range environ {
		k, v, ok := strings.Cut(kv, "=")
		if ok && (k == name || (goruntime.GOOS == "windows" && strings.EqualFold(k, name))) {
			return v
		}
	}
	return ""
}

// Hide makes a child run without a console window (Windows) or in its own process group (Unix), so a
// tree kill reaches its children too.
func Hide(cmd *exec.Cmd) { hide(cmd) }
