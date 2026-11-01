// Package cli parses pwrunner's command line: a subcommand followed by flags in the forms
// "--k v" and "--k=v", "--flag" alone for booleans, and "--" before arguments passed through.
package cli

import (
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
)

// FlagSpec describes one flag a subcommand accepts.
type FlagSpec struct {
	Name    string   // "hub", "slots", ...
	Bool    bool     // may appear without a value (--ephemeral)
	Hidden  bool     // omitted from usage (--timings)
	Repeat  bool     // may appear more than once
	Aliases []string // other names stored under Name (--port-count for --slots)
	Env     string   // environment variable shown in the usage text
	Arg     string   // value placeholder in the usage text ("<url>")
	Usage   string
}

// Parsed is the result of Parse.
type Parsed struct {
	Command string
	Values  map[string][]string // raw values in order of appearance, keyed by FlagSpec.Name
	Args    []string            // positional arguments before "--"
	Rest    []string            // after "--" (pwrunner node -- ...)
	Help    bool
}

// Has reports whether the flag was given.
func (p Parsed) Has(name string) bool { _, ok := p.Values[name]; return ok }

// Get returns the last value given for the flag and whether it was given.
func (p Parsed) Get(name string) (string, bool) {
	v, ok := p.Values[name]
	if !ok || len(v) == 0 {
		return "", false
	}
	return v[len(v)-1], true
}

// ExitError carries the process exit code for an error.
type ExitError struct {
	Code int
	Msg  string
}

func (e *ExitError) Error() string { return e.Msg }

// Usagef returns an exit-2 error, the code for any usage or value error.
func Usagef(format string, a ...any) error {
	return &ExitError{Code: 2, Msg: fmt.Sprintf(format, a...)}
}

// Parse splits args (the subcommand first) into the command and its flags. An unknown flag, a missing
// value or a repeated non-Repeat flag is an exit-2 error.
func Parse(args []string, specs []FlagSpec) (Parsed, error) {
	p := Parsed{Values: map[string][]string{}}
	byName := map[string]FlagSpec{}
	for _, s := range specs {
		byName[s.Name] = s
		for _, a := range s.Aliases {
			byName[a] = s
		}
	}
	i := 0
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		p.Command = args[0]
		i = 1
	}
	for ; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			p.Rest = append([]string{}, args[i+1:]...)
			break
		}
		if arg == "-h" || arg == "--help" {
			p.Help = true
			continue
		}
		if !strings.HasPrefix(arg, "--") || len(arg) == 2 {
			if strings.HasPrefix(arg, "-") {
				return p, Usagef("unknown argument %q", arg)
			}
			p.Args = append(p.Args, arg)
			continue
		}
		name, value, hasValue := strings.Cut(arg[2:], "=")
		spec, ok := byName[name]
		if !ok {
			return p, Usagef("unknown argument %q", arg)
		}
		if !hasValue {
			if spec.Bool {
				value = "true"
			} else {
				if i+1 >= len(args) {
					return p, Usagef("--%s needs a value", name)
				}
				i++
				value = args[i]
			}
		}
		if _, seen := p.Values[spec.Name]; seen && !spec.Repeat {
			p.Values[spec.Name] = p.Values[spec.Name][:0]
		}
		p.Values[spec.Name] = append(p.Values[spec.Name], value)
	}
	return p, nil
}

// Usage renders the flag table for a command.
func Usage(command string, specs []FlagSpec) string {
	var b strings.Builder
	fmt.Fprintf(&b, "Usage: pwrunner %s [options]\n\n", command)
	rows := make([][3]string, 0, len(specs))
	for _, s := range specs {
		if s.Hidden {
			continue
		}
		left := "--" + s.Name
		if s.Arg != "" {
			left += " " + s.Arg
		}
		rows = append(rows, [3]string{left, s.Env, s.Usage})
	}
	w0, w1 := 0, 0
	for _, r := range rows {
		w0 = max(w0, len(r[0]))
		w1 = max(w1, len(r[1]))
	}
	for _, r := range rows {
		fmt.Fprintf(&b, "  %-*s  %-*s  %s\n", w0, r[0], w1, r[1], r[2])
	}
	return b.String()
}

// Exit prints the error to stderr and exits: an ExitError with its code, anything else with 1.
func Exit(err error) {
	if err == nil {
		os.Exit(0)
	}
	var ee *ExitError
	if errors.As(err, &ee) {
		if ee.Msg != "" {
			fmt.Fprintln(os.Stderr, ee.Msg)
		}
		os.Exit(ee.Code)
	}
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

// Names lists the flags that were given, sorted (for logging which settings were passed).
func (p Parsed) Names() []string {
	out := make([]string, 0, len(p.Values))
	for k := range p.Values {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
