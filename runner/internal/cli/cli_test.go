package cli

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

var specs = []FlagSpec{
	{Name: "hub", Arg: "<url>", Env: "HUB_URL", Usage: "hub to dial out to"},
	{Name: "slots", Aliases: []string{"port-count"}},
	{Name: "ephemeral", Bool: true},
	{Name: "timings", Hidden: true},
}

func TestParseForms(t *testing.T) {
	p, err := Parse([]string{"run", "--hub", "ws://a", "--slots=3", "--ephemeral", "--timings="}, specs)
	if err != nil {
		t.Fatal(err)
	}
	if p.Command != "run" {
		t.Fatalf("command %q", p.Command)
	}
	want := map[string][]string{"hub": {"ws://a"}, "slots": {"3"}, "ephemeral": {"true"}, "timings": {""}}
	if !reflect.DeepEqual(p.Values, want) {
		t.Fatalf("values %v", p.Values)
	}
	if v, ok := p.Get("timings"); !ok || v != "" {
		t.Fatalf("--k= must be an empty value, got %q %v", v, ok)
	}
}

func TestBoolWithValue(t *testing.T) {
	p, err := Parse([]string{"run", "--ephemeral=false"}, specs)
	if err != nil {
		t.Fatal(err)
	}
	if v, _ := p.Get("ephemeral"); v != "false" {
		t.Fatalf("got %q", v)
	}
}

func TestAlias(t *testing.T) {
	p, err := Parse([]string{"install", "--port-count", "5"}, specs)
	if err != nil {
		t.Fatal(err)
	}
	if v, _ := p.Get("slots"); v != "5" {
		t.Fatalf("got %q", v)
	}
}

func TestErrorsExit2(t *testing.T) {
	for _, args := range [][]string{{"run", "--nope"}, {"run", "--hub"}, {"run", "-x"}} {
		_, err := Parse(args, specs)
		var ee *ExitError
		if !errors.As(err, &ee) || ee.Code != 2 {
			t.Fatalf("%v: want exit 2, got %v", args, err)
		}
	}
	_, err := Parse([]string{"run", "--nope"}, specs)
	if err.Error() != `unknown argument "--nope"` {
		t.Fatalf("message %q", err)
	}
	_, err = Parse([]string{"run", "--hub"}, specs)
	if err.Error() != "--hub needs a value" {
		t.Fatalf("message %q", err)
	}
}

func TestHelpAndRest(t *testing.T) {
	p, err := Parse([]string{"node", "--help", "--", "cli.js", "--version"}, specs)
	if err != nil {
		t.Fatal(err)
	}
	if !p.Help || !reflect.DeepEqual(p.Rest, []string{"cli.js", "--version"}) {
		t.Fatalf("help %v rest %v", p.Help, p.Rest)
	}
}

func TestUsageHidesHidden(t *testing.T) {
	u := Usage("run", specs)
	if strings.Contains(u, "timings") || !strings.Contains(u, "--hub <url>") || !strings.Contains(u, "HUB_URL") {
		t.Fatalf("usage:\n%s", u)
	}
}
