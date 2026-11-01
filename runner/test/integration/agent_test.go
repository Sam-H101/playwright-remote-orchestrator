//go:build integration

package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

func TestMain(m *testing.M) {
	cleanup, err := Setup()
	if err != nil {
		fmt.Fprintln(os.Stderr, "integration setup:", err)
		os.Exit(1)
	}
	code := m.Run()
	cleanup()
	os.Exit(code)
}

func platform() string {
	if runtime.GOOS == "windows" {
		return "win32"
	}
	return runtime.GOOS
}

// 1, 3, 4: registration, header forwarding and piping through the real hub.
func TestRegisterForwardAndPipe(t *testing.T) {
	hub := StartHub(t, 0, RandomToken())
	a := StartAgent(t, hub.URL(), "Runner-A", AgentOpts{Token: hub.Token})
	a.Ready(t)
	var n *Node
	WaitFor(t, "node listed with 2 healthy slots", 10*time.Second, func() bool { n = hub.Node("runner-a"); return n != nil && n.HealthySlots == 2 })
	if n.Slots != 2 || n.AgentVersion != "it-1" || n.Platform != platform() || n.PlaywrightVersion != "0.0.0-fake" || strings.Join(n.Browsers, ",") != "chromium" {
		t.Fatalf("%+v", n)
	}

	h := http.Header{}
	h.Set("User-Agent", "Playwright/0.0.0 (it)")
	h.Set("X-Playwright-Browser", "chromium")
	h.Set("X-Gateway-Token", "nope")
	c, status, body, err := Connect(hub.URL()+"/connect?node=runner-a&x=a%20b", h)
	if err != nil {
		t.Fatalf("connect: %d %s %v", status, body, err)
	}
	defer c.CloseNow()
	var info struct {
		URL        string            `json:"url"`
		Headers    map[string]string `json:"headers"`
		RawHeaders []string          `json:"rawHeaders"`
	}
	json.Unmarshal(Echo(t, c, websocket.MessageText, []byte("info")), &info)
	var uas []string
	for i := 0; i+1 < len(info.RawHeaders); i += 2 {
		if strings.EqualFold(info.RawHeaders[i], "user-agent") {
			uas = append(uas, info.RawHeaders[i+1])
		}
	}
	if strings.Join(uas, "|") != "Playwright/0.0.0 (it)" {
		t.Fatalf("user-agents reaching run-server: %q", uas)
	}
	if info.URL != "/?x=a%20b" || info.Headers["x-playwright-browser"] != "chromium" || info.Headers["x-gateway-token"] != "" || info.Headers["authorization"] != "" {
		t.Fatalf("%+v", info)
	}
	if info.Headers["host"] != fmt.Sprintf("127.0.0.1:%d", a.BasePort) && info.Headers["host"] != fmt.Sprintf("127.0.0.1:%d", a.BasePort+1) {
		t.Fatalf("host %q", info.Headers["host"])
	}
	if got := Echo(t, c, websocket.MessageText, []byte("hello")); string(got) != "hello" {
		t.Fatal(string(got))
	}
	if got := Echo(t, c, websocket.MessageBinary, []byte{0, 1, 255}); !bytes.Equal(got, []byte{0, 1, 255}) {
		t.Fatal(got)
	}
	big := bytes.Repeat([]byte{7}, 5<<20)
	if got := Echo(t, c, websocket.MessageBinary, big); !bytes.Equal(got, big) {
		t.Fatalf("5 MiB frame: %d bytes back", len(got))
	}
}

// 2: pongs and heartbeats keep the node listed well past the hub's 6 s heartbeat timeout.
func TestLiveness(t *testing.T) {
	hub := StartHub(t, 0, RandomToken())
	a := StartAgent(t, hub.URL(), "alive", AgentOpts{Token: hub.Token})
	a.Ready(t)
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if n := hub.Node("alive"); n == nil || n.HealthySlots != 2 {
			t.Fatalf("node dropped: %+v", n)
		}
		time.Sleep(time.Second)
	}
}

// 8: a client that stops reading pushes back on run-server instead of growing the agent.
func TestBackpressure(t *testing.T) {
	hub := StartHub(t, 0, RandomToken())
	a := StartAgent(t, hub.URL(), "slowreader", AgentOpts{Token: hub.Token, Slots: 1})
	a.Ready(t)
	c, status, body, err := Connect(hub.URL()+"/connect?node=slowreader", nil)
	if err != nil {
		t.Fatalf("connect: %d %s %v", status, body, err)
	}
	defer c.CloseNow()
	frame := bytes.Repeat([]byte{1}, 1<<20)
	const frames = 48
	go func() {
		for i := 0; i < frames; i++ {
			if c.Write(context.Background(), websocket.MessageBinary, frame) != nil {
				return
			}
		}
	}()
	// Stalled for longer than the websocket library's 5 s control-frame timeout while the hub pings the
	// data channel every 2 s: the session must survive (see pipe.Writes).
	var peak int64
	end := time.Now().Add(8 * time.Second)
	for time.Now().Before(end) {
		if rss, err := RSS(a.PID()); err == nil && rss > peak {
			peak = rss
		}
		time.Sleep(100 * time.Millisecond)
	}
	if peak == 0 || peak > 100<<20 {
		t.Fatalf("agent RSS peaked at %d bytes with a stalled client", peak)
	}
	for i := 0; i < frames; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		_, b, err := c.Read(ctx)
		cancel()
		if err != nil || len(b) != len(frame) {
			t.Fatalf("frame %d after resuming: %d bytes, %v", i, len(b), err)
		}
	}
	t.Logf("agent RSS peak %d MiB", peak>>20)
}

// 10: a refused token is retried, and the agent registers once the hub accepts it.
func TestTokenRefusalAndRecovery(t *testing.T) {
	good := RandomToken()
	hub := StartHub(t, 0, RandomToken())
	a := StartAgent(t, hub.URL(), "retrier", AgentOpts{Token: good})
	WaitFor(t, "4401 in lastError", 15*time.Second, func() bool {
		s := a.Status()
		return s != nil && s.LastError != nil && strings.Contains(*s.LastError, "4401") && !s.Registered
	})
	if strings.Contains(a.Log(), good) || strings.Contains(hub.Log(), good) {
		t.Fatal("the token appears in a log")
	}
	port := hub.Port
	hub.Stop()
	hub2 := StartHub(t, port, good)
	a.Ready(t)
	if hub2.Node("retrier") == nil {
		t.Fatal("not listed after recovery")
	}
}

// 12 is in the contract suite; 13: a graceful stop closes sessions with 1001 and exits 0.
func TestGracefulStop(t *testing.T) {
	hub := StartHub(t, 0, RandomToken())
	a := StartAgent(t, hub.URL(), "leaver", AgentOpts{Token: hub.Token, Group: true})
	st := a.Ready(t)
	c, status, body, err := Connect(hub.URL()+"/connect?node=leaver", nil)
	if err != nil {
		t.Fatalf("connect: %d %s %v", status, body, err)
	}
	defer c.CloseNow()
	if err := Interrupt(a.PID()); err != nil {
		t.Skipf("can't signal the agent here: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, _, err = c.Read(ctx)
	if code := websocket.CloseStatus(err); code != 1001 {
		t.Fatalf("client saw %v, want 1001", err)
	}
	select {
	case <-a.exited:
	case <-time.After(10 * time.Second):
		t.Fatal("agent did not exit within 10s")
	}
	if code := a.cmd.ProcessState.ExitCode(); code != 0 {
		t.Fatalf("exit code %d", code)
	}
	for _, sl := range st.Slots {
		if sl.PID != nil && proc.Alive(*sl.PID) {
			t.Fatalf("run-server %d still running", *sl.PID)
		}
	}
	if s := a.Status(); s == nil || s.Connected || s.Registered {
		t.Fatalf("final status %+v", s)
	}
}

// 14: an agent that answers too late gets the hub's 504, and its late data channel a 404.
func TestOpenTimeout(t *testing.T) {
	hub := StartHub(t, 0, RandomToken(), "GATEWAY_CONNECT_ATTEMPTS=1")
	a := StartAgent(t, hub.URL(), "sluggish", AgentOpts{Token: hub.Token, Slots: 1, Timings: `{"openDelayMs":7000}`})
	a.Ready(t)
	_, status, body, _ := Connect(hub.URL()+"/connect?node=sluggish", nil)
	if status != 504 || !strings.Contains(body, "did not answer") {
		t.Fatalf("status %d body %q", status, body)
	}
	WaitFor(t, "late data channel refused", 10*time.Second, func() bool {
		return strings.Contains(a.Log(), "data channel: ") && strings.Contains(a.Log(), "404")
	})
}

// 15: a data channel that goes silent (hub host gone without a FIN) is closed by the agent; an idle but
// healthy one is not.
func TestDataChannelSilence(t *testing.T) {
	hub := StartHub(t, 0, RandomToken())
	proxy := StartProxy(t, hub.Port)
	a := StartAgent(t, fmt.Sprintf("ws://127.0.0.1:%d", proxy.Port), "blackhole", AgentOpts{Token: hub.Token, Slots: 1,
		Timings: `{"heartbeatMs":500,"hubSilenceMs":3000}`})
	a.Ready(t)
	c, status, body, err := Connect(hub.URL()+"/connect?node=blackhole", nil)
	if err != nil {
		t.Fatalf("connect: %d %s %v", status, body, err)
	}
	defer c.CloseNow()
	Echo(t, c, websocket.MessageText, []byte("hi"))
	time.Sleep(5 * time.Second) // idle past the silence window, but pings keep it alive
	Echo(t, c, websocket.MessageText, []byte("still here"))
	if strings.Contains(a.Log(), "no word from the hub on its data channel") {
		t.Fatal("a healthy idle session was closed")
	}
	proxy.Freeze()
	WaitFor(t, "silent session closed", 8*time.Second, func() bool {
		return strings.Contains(a.Log(), "no word from the hub on its data channel")
	})
}

// 16: losing the control connection does not end sessions; the agent re-registers meanwhile.
func TestControlDropKeepsSessions(t *testing.T) {
	hub := StartHub(t, 0, RandomToken())
	proxy := StartProxy(t, hub.Port)
	a := StartAgent(t, fmt.Sprintf("ws://127.0.0.1:%d", proxy.Port), "steady", AgentOpts{Token: hub.Token, Slots: 1,
		Timings: `{"heartbeatMs":500,"hubSilenceMs":3000}`})
	a.Ready(t)
	c, status, body, err := Connect(hub.URL()+"/connect?node=steady", nil)
	if err != nil {
		t.Fatalf("connect: %d %s %v", status, body, err)
	}
	defer c.CloseNow()
	Echo(t, c, websocket.MessageText, []byte("before"))
	proxy.FreezeConn(1) // the control connection was the first through the proxy
	WaitFor(t, "control connection dropped for silence", 10*time.Second, func() bool {
		return strings.Contains(a.Log(), "no word from the hub for 3s; reconnecting")
	})
	WaitFor(t, "re-registered", 15*time.Second, func() bool {
		return strings.Count(a.Log(), "registered with ") >= 2
	})
	for i := 0; i < 5; i++ {
		if got := Echo(t, c, websocket.MessageText, []byte("during")); string(got) != "during" {
			t.Fatal(string(got))
		}
		time.Sleep(200 * time.Millisecond)
	}
}
