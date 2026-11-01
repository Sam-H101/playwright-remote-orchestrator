package hubconn

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

// fakeHub accepts control connections and hands each to the test.
type fakeHub struct {
	srv    *httptest.Server
	conns  chan *hubSide
	auth   chan string
	silent bool // stop reading after the register frame: no pongs, as from a vanished hub host
}

type hubSide struct {
	c      *websocket.Conn
	frames chan map[string]any
	closed chan websocket.CloseError
}

func newFakeHub(t *testing.T) *fakeHub {
	h := &fakeHub{conns: make(chan *hubSide, 8), auth: make(chan string, 8)}
	h.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/agent" {
			http.NotFound(w, r)
			return
		}
		h.auth <- r.Header.Get("Authorization")
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		side := &hubSide{c: c, frames: make(chan map[string]any, 64), closed: make(chan websocket.CloseError, 1)}
		h.conns <- side
		for {
			_, data, err := c.Read(context.Background())
			if err != nil {
				ce, _ := err.(websocket.CloseError)
				var ce2 websocket.CloseError
				if asClose(err, &ce2) {
					ce = ce2
				}
				side.closed <- ce
				return
			}
			var m map[string]any
			json.Unmarshal(data, &m)
			side.frames <- m
			if h.silent {
				<-r.Context().Done()
				return
			}
		}
	}))
	t.Cleanup(h.srv.Close)
	return h
}

func asClose(err error, ce *websocket.CloseError) bool {
	code := websocket.CloseStatus(err)
	if code == -1 {
		return false
	}
	*ce = websocket.CloseError{Code: code}
	s := err.Error()
	if i := strings.LastIndex(s, ": "); i >= 0 {
		ce.Reason = s[i+2:]
	}
	return true
}

func (h *fakeHub) url() string { return "ws" + strings.TrimPrefix(h.srv.URL, "http") }

func (s *hubSide) next(t *testing.T) map[string]any {
	t.Helper()
	select {
	case m := <-s.frames:
		return m
	case <-time.After(5 * time.Second):
		t.Fatal("no frame from the agent")
		return nil
	}
}

func (s *hubSide) send(t *testing.T, v any) {
	b, _ := json.Marshal(v)
	if err := s.c.Write(context.Background(), websocket.MessageText, b); err != nil {
		t.Fatal(err)
	}
}

var fast = config.Timings{HeartbeatMs: 100, ReconnectBaseMs: 50, ReconnectMaxMs: 200, HubSilenceMs: 5000, ReplacedDelayMs: 1500}

type harness struct {
	c      *Conn
	cancel context.CancelFunc
	done   chan struct{}
	opens  chan protocol.Open
}

func start(t *testing.T, h *fakeHub, timings config.Timings, token string) *harness {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	hr := &harness{cancel: cancel, done: make(chan struct{}), opens: make(chan protocol.Open, 4)}
	hr.c = New(Options{
		HubURL: h.url(), Token: config.Secret(token), Timings: timings, Logger: logging.Discard(), Rand: func() float64 { return 0 },
		Register: func() protocol.Register {
			return protocol.Register{Type: "register", Name: "n1", Slots: []protocol.Slot{{Index: 1, Healthy: true}}, Browsers: []string{"chromium"}}
		},
		SlotHealth: func() []protocol.Slot { return []protocol.Slot{{Index: 1, Healthy: true}} },
		OnOpen:     func(_ context.Context, f protocol.Open) { hr.opens <- f },
	})
	go func() { hr.c.Run(ctx); close(hr.done) }()
	ticker := time.NewTicker(time.Duration(timings.HeartbeatMs) * time.Millisecond)
	go func() {
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				hr.c.Tick()
			}
		}
	}()
	t.Cleanup(func() { cancel(); <-hr.done })
	return hr
}

func (h *fakeHub) accept(t *testing.T) *hubSide {
	t.Helper()
	select {
	case s := <-h.conns:
		return s
	case <-time.After(5 * time.Second):
		t.Fatal("agent did not connect")
		return nil
	}
}

func waitState(t *testing.T, c *Conn, what string, ok func(State) bool) State {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if s := c.State(); ok(s) {
			return s
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s: %+v", what, c.State())
	return State{}
}

func TestRegisterHeartbeatAndOpen(t *testing.T) {
	h := newFakeHub(t)
	hr := start(t, h, fast, "tok")
	if auth := <-h.auth; auth != "Bearer tok" {
		t.Fatalf("authorization %q", auth)
	}
	side := h.accept(t)
	reg := side.next(t)
	if reg["type"] != "register" || reg["name"] != "n1" {
		t.Fatalf("first frame %v", reg)
	}
	// Nothing but pings before registered: no heartbeat frame may arrive.
	select {
	case m := <-side.frames:
		t.Fatalf("frame before registered: %v", m)
	case <-time.After(300 * time.Millisecond):
	}
	side.send(t, map[string]any{"type": "registered", "name": "n1"})
	st := waitState(t, hr.c, "registered", func(s State) bool { return s.Registered })
	if !st.Connected || st.LastError != nil || st.Name != "n1" {
		t.Fatalf("%+v", st)
	}
	if hb := side.next(t); hb["type"] != "heartbeat" {
		t.Fatalf("want heartbeat, got %v", hb)
	}
	side.send(t, map[string]any{"type": "open", "id": strings.Repeat("a", 36), "slot": 1, "path": "/", "query": "", "headers": map[string]any{}})
	select {
	case f := <-hr.opens:
		if f.ID != strings.Repeat("a", 36) {
			t.Fatal(f.ID)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("open not dispatched")
	}
	side.send(t, map[string]any{"type": "error", "message": "something odd"})
	waitState(t, hr.c, "lastError", func(s State) bool { return s.LastError != nil && *s.LastError == "something odd" })
}

func TestCloseCodesAndReconnect(t *testing.T) {
	h := newFakeHub(t)
	hr := start(t, h, fast, "")
	if auth := <-h.auth; auth != "" {
		t.Fatalf("no token must mean no header, got %q", auth)
	}
	side := h.accept(t)
	side.next(t)
	side.c.Close(protocol.CloseUnauthorized, "token unknown")
	waitState(t, hr.c, "4401 in lastError", func(s State) bool {
		return !s.Connected && s.LastError != nil && *s.LastError == "hub closed the connection (4401 token unknown)"
	})
	<-h.auth
	side = h.accept(t) // retried with the normal backoff
	side.next(t)
	replacedAt := time.Now()
	side.c.Close(protocol.CloseReplaced, "replaced")
	<-h.auth
	if waited := time.Since(replacedAt); waited < 1400*time.Millisecond {
		t.Fatalf("reconnected %v after 4409; the floor is 1.5s", waited)
	}
	h.accept(t)
}

func TestHubSilence(t *testing.T) {
	h := newFakeHub(t)
	timings := fast
	timings.HubSilenceMs = 300
	h.silent = true
	hr := start(t, h, timings, "")
	<-h.auth
	side := h.accept(t)
	side.next(t)
	waitState(t, hr.c, "hub stopped answering", func(s State) bool {
		return !s.Connected && s.LastError != nil && *s.LastError == "hub stopped answering"
	})
}

func TestStopSends1001(t *testing.T) {
	h := newFakeHub(t)
	hr := start(t, h, fast, "")
	<-h.auth
	side := h.accept(t)
	side.next(t)
	hr.cancel()
	select {
	case ce := <-side.closed:
		if ce.Code != 1001 {
			t.Fatalf("close %v", ce)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no close")
	}
	<-hr.done
	select {
	case <-h.auth:
		t.Fatal("reconnected after stop")
	case <-time.After(300 * time.Millisecond):
	}
}

func TestNextDelay(t *testing.T) {
	tm := config.DefaultTimings
	if d := nextDelay(time.Second, 0, 1006, tm); d != time.Second {
		t.Fatal(d)
	}
	if d := nextDelay(time.Second, 0.999, 1006, tm); d < time.Second || d > 1300*time.Millisecond {
		t.Fatal(d)
	}
	if d := nextDelay(time.Second, 0.5, protocol.CloseReplaced, tm); d != 30*time.Second {
		t.Fatal(d)
	}
}

func TestTruncate(t *testing.T) {
	if got := truncate(strings.Repeat("é", 3000), 4000); len(got) != 4000 {
		t.Fatal(len(got))
	}
	if got := truncate("a"+strings.Repeat("é", 3000), 4000); len(got) != 3999 {
		t.Fatal(len(got))
	}
}
