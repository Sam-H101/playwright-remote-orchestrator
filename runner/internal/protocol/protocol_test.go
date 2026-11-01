package protocol

import (
	"encoding/json"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func TestForwardHeaders(t *testing.T) {
	in := map[string][]string{
		"host": {"hub:4500"}, "connection": {"Upgrade"}, "upgrade": {"websocket"}, "sec-websocket-key": {"k"},
		"sec-websocket-version": {"13"}, "sec-websocket-extensions": {"permessage-deflate"}, "content-length": {"0"},
		"transfer-encoding": {"chunked"}, "authorization": {"Bearer x"}, "x-gateway-token": {"t"}, "proxy-authorization": {"p"},
		"user-agent": {"Playwright/1.64.1"}, "x-playwright-browser": {"chromium"}, "x-playwright-launch-options": {"{}"},
		"x-custom": {"1", "2"},
	}
	want := http.Header{
		"User-Agent": {"Playwright/1.64.1"}, "X-Playwright-Browser": {"chromium"},
		"X-Playwright-Launch-Options": {"{}"}, "X-Custom": {"1", "2"},
	}
	if got := ForwardHeaders(in); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v", got)
	}
}

func TestForwardHeadersWithoutUserAgentSuppressesGoDefault(t *testing.T) {
	got := ForwardHeaders(map[string][]string{"x-playwright-browser": {"chromium"}})
	if v, ok := got["User-Agent"]; !ok || len(v) != 1 || v[0] != "" {
		t.Fatalf("User-Agent must be present and empty, got %v", got)
	}
}

func TestSendableCloseCode(t *testing.T) {
	for _, c := range []int{1000, 1001, 1011, 1014, 3000, 4409, 4999} {
		if SendableCloseCode(c) != c {
			t.Fatalf("%d must pass", c)
		}
	}
	if SendableCloseCode(1005) != 1000 {
		t.Fatal("1005 -> 1000")
	}
	for _, c := range []int{1004, 1006, 1015, 999, 2000, 5000, 0, -1} {
		if SendableCloseCode(c) != 1011 {
			t.Fatalf("%d -> 1011", c)
		}
	}
}

func TestCloseReason(t *testing.T) {
	if got := CloseReason(strings.Repeat("é", 100)); len(got) != 122 {
		t.Fatalf("got %d bytes", len(got))
	}
	if got := CloseReason(strings.Repeat("a", 200)); len(got) != 123 {
		t.Fatalf("got %d bytes", len(got))
	}
	if got := CloseReason(strings.Repeat("😀", 40)); len(got) != 120 {
		t.Fatalf("got %d bytes", len(got))
	}
	if CloseReason("bye") != "bye" {
		t.Fatal("short reasons pass")
	}
}

func TestNames(t *testing.T) {
	if CanonicalName("HOST-1.example_a") != "host-1.example_a" {
		t.Fatal("canonical")
	}
	for _, bad := range []string{"", "a b", "x/y", strings.Repeat("a", 129)} {
		if CanonicalName(bad) != "" {
			t.Fatalf("%q must be invalid", bad)
		}
	}
	if SanitizeName("My PC (2)") != "My-PC--2-" || SanitizeName("") != "node" || len(SanitizeName(strings.Repeat("a", 200))) != 128 {
		t.Fatal("sanitize")
	}
}

func TestTokenHelpers(t *testing.T) {
	if TokenHash("abc") != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" {
		t.Fatal("hash")
	}
	managed := "pwga_" + strings.Repeat("A1b2_-", 7) + "x"
	if TokenLabel(managed) != "token pwga_A1b2_-A..." {
		t.Fatalf("label %q", TokenLabel(managed))
	}
	for _, other := range []string{"agent-secret-123", "pwga_" + strings.Repeat("a", 42), ""} {
		if TokenLabel(other) != "token" {
			t.Fatalf("%q", other)
		}
	}
}

func TestParseInbound(t *testing.T) {
	for _, bad := range []string{"", "null", "[]", "1", `{"type":1}`, `{"x":"y"}`, "{"} {
		if _, err := ParseInbound([]byte(bad)); err == nil {
			t.Fatalf("%q must be malformed", bad)
		}
	}
	in, err := ParseInbound([]byte(`{"type":"registered","name":"a"}`))
	if err != nil || in.Type != "registered" {
		t.Fatal(in, err)
	}
	if name, ok := StringField(in.Raw, "name"); !ok || name != "a" {
		t.Fatal("name")
	}
}

func TestParseOpen(t *testing.T) {
	o := ParseOpen(json.RawMessage(`{"type":"open","id":"0123","slot":"x","path":5,"headers":{"a":"b","c":["d"],"e":1}}`))
	if o.ID != "0123" || string(o.Slot) != `"x"` || string(o.Path) != "5" || len(o.Headers) != 3 {
		t.Fatalf("%+v", o)
	}
	if ParseOpen(json.RawMessage(`{"id":7}`)).ID != "" {
		t.Fatal("non-string id")
	}
}

func TestFrameFieldOrder(t *testing.T) {
	b, _ := json.Marshal(Register{Type: "register", Name: "n", Slots: []Slot{{1, true}}, Browsers: []string{"chromium"}})
	want := `{"type":"register","name":"n","slots":[{"index":1,"healthy":true}],"playwrightVersion":"","agentVersion":"","platform":"","browsers":["chromium"],"ephemeral":false}`
	if string(b) != want {
		t.Fatalf("got %s", b)
	}
	b, _ = json.Marshal(OpenFailed{Type: "open-failed", ID: "i", Message: "m"})
	if string(b) != `{"type":"open-failed","id":"i","message":"m"}` {
		t.Fatalf("got %s", b)
	}
}
