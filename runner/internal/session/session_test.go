package session

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

const id = "0123456789abcdef-0123456789abcdef-01"

// upstream is a fake run-server: an echo server that records the upgrade request, or refuses it.
type upstream struct {
	srv    *httptest.Server
	port   int
	mu     sync.Mutex
	req    *http.Request
	reject int
}

func newUpstream(t *testing.T) *upstream {
	u := &upstream{}
	u.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u.mu.Lock()
		u.req = r.Clone(context.Background())
		reject := u.reject
		u.mu.Unlock()
		if reject != 0 {
			w.WriteHeader(reject)
			fmt.Fprint(w, "Playwright version mismatch:\n  - server version: v0.0\n  - client version: v1.50\n")
			return
		}
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		for {
			typ, b, err := c.Read(context.Background())
			if err != nil {
				return
			}
			c.Write(context.Background(), typ, b)
		}
	}))
	t.Cleanup(u.srv.Close)
	u.port, _ = strconv.Atoi(u.srv.URL[strings.LastIndex(u.srv.URL, ":")+1:])
	return u
}

// hub accepts data channels.
type hub struct {
	srv    *httptest.Server
	status int // non-zero: refuse data channels with this status
	conns  chan *websocket.Conn
	auth   chan string
}

func newHub(t *testing.T) *hub {
	h := &hub{conns: make(chan *websocket.Conn, 4), auth: make(chan string, 4)}
	h.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.auth <- r.Header.Get("Authorization")
		if h.status != 0 || r.URL.Path != "/agent/data/"+id {
			http.Error(w, "no pending session with that id", max(h.status, 404))
			return
		}
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		h.conns <- c
		<-r.Context().Done()
	}))
	t.Cleanup(h.srv.Close)
	return h
}

type reports struct {
	mu   sync.Mutex
	list []protocol.OpenFailed
}

func (r *reports) add(f protocol.OpenFailed) { r.mu.Lock(); r.list = append(r.list, f); r.mu.Unlock() }
func (r *reports) all() []protocol.OpenFailed {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]protocol.OpenFailed{}, r.list...)
}

func manager(h *hub, port int, rep *reports, timings config.Timings) *Manager {
	return NewManager(Options{
		HubURL: "ws" + strings.TrimPrefix(h.srv.URL, "http"), Token: "tok", Timings: timings, Logger: logging.Discard(),
		SlotPort: func(i int) (int, bool) { return port, i == 1 },
		Report:   rep.add,
	})
}

func open(slot string, headers map[string]any) protocol.Open {
	raw, _ := json.Marshal(map[string]any{"type": "open", "id": id, "slot": json.RawMessage(slot), "path": "/", "query": "a=1", "headers": headers})
	return protocol.ParseOpen(raw)
}

func TestSessionForwardsHeadersAndPipes(t *testing.T) {
	u, h, rep := newUpstream(t), newHub(t), &reports{}
	m := manager(h, u.port, rep, config.DefaultTimings)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go m.HandleOpen(ctx, open("1", map[string]any{
		"user-agent": "Playwright/1.64.1 (test)", "x-playwright-browser": "chromium", "x-multi": []string{"a", "b"},
		"host": "evil", "authorization": "Bearer client", "x-gateway-token": "t", "sec-websocket-key": "k", "proxy-authorization": "p", "x-bad": 7,
	}))
	var data *websocket.Conn
	select {
	case data = <-h.conns:
	case <-time.After(10 * time.Second):
		t.Fatalf("no data channel; reports %v", rep.all())
	}
	if auth := <-h.auth; auth != "Bearer tok" {
		t.Fatalf("data channel authorization %q", auth)
	}
	u.mu.Lock()
	req := u.req
	u.mu.Unlock()
	if ua := req.Header.Values("User-Agent"); len(ua) != 1 || ua[0] != "Playwright/1.64.1 (test)" {
		t.Fatalf("user-agent %v", ua)
	}
	if req.Host != fmt.Sprintf("127.0.0.1:%d", u.port) || req.URL.RawQuery != "a=1" || req.Header.Get("X-Playwright-Browser") != "chromium" ||
		strings.Join(req.Header.Values("X-Multi"), ",") != "a,b" || req.Header.Get("Authorization") != "" || req.Header.Get("X-Gateway-Token") != "" ||
		req.Header.Get("Proxy-Authorization") != "" || req.Header.Get("X-Bad") != "" {
		t.Fatalf("upstream request: host %s query %s headers %v", req.Host, req.URL.RawQuery, req.Header)
	}
	data.Write(context.Background(), websocket.MessageText, []byte("hello"))
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	if _, b, err := data.Read(rctx); err != nil || string(b) != "hello" {
		t.Fatal(string(b), err)
	}
	if m.Count() != 1 {
		t.Fatal("session not counted")
	}
	// The dial deadline is over; the session must keep working without one.
	time.Sleep(time.Second)
	data.Write(context.Background(), websocket.MessageText, []byte("later"))
	if _, b, err := data.Read(rctx); err != nil || string(b) != "later" {
		t.Fatal(string(b), err)
	}
	go m.CloseAll(1001, "agent is shutting down")
	cctx, ccancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer ccancel()
	if _, _, err := data.Read(cctx); websocket.CloseStatus(err) != 1001 {
		t.Fatalf("want 1001, got %v", err)
	}
}

func TestNoUserAgentSendsNone(t *testing.T) {
	u, h, rep := newUpstream(t), newHub(t), &reports{}
	m := manager(h, u.port, rep, config.DefaultTimings)
	go m.HandleOpen(context.Background(), open("1", map[string]any{}))
	select {
	case c := <-h.conns:
		defer c.CloseNow()
	case <-time.After(10 * time.Second):
		t.Fatal("no data channel")
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	if ua := u.req.Header.Values("User-Agent"); len(ua) != 0 {
		t.Fatalf("user-agent %v", ua)
	}
}

func TestOpenFailures(t *testing.T) {
	u, h := newUpstream(t), newHub(t)
	run := func(f protocol.Open, port int) []protocol.OpenFailed {
		rep := &reports{}
		manager(h, port, rep, config.DefaultTimings).HandleOpen(context.Background(), f)
		return rep.all()
	}
	if got := run(protocol.Open{ID: "nope"}, u.port); len(got) != 0 {
		t.Fatalf("bad id must be ignored: %v", got)
	}
	for slot, want := range map[string]string{"2": "no slot 2", `"x"`: "no slot x", "1.5": "no slot 1.5", "null": "no slot null"} {
		got := run(open(slot, nil), u.port)
		if len(got) != 1 || got[0].Message != want || got[0].Status != nil || got[0].ID != id {
			t.Fatalf("slot %s: %+v", slot, got)
		}
	}
	u.reject = 428
	got := run(open("1", nil), u.port)
	if len(got) != 1 || got[0].Status == nil || *got[0].Status != 428 ||
		!strings.Contains(got[0].Message, fmt.Sprintf("run-server on port %d answered HTTP 428: Playwright version mismatch:", u.port)) ||
		!strings.Contains(got[0].Message, "server version: v0.0") || !strings.Contains(got[0].Message, "client version: v1.50") {
		t.Fatalf("428: %+v", got)
	}
	u.reject = 0
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	closedPort := l.Addr().(*net.TCPAddr).Port
	l.Close()
	got = run(open("1", nil), closedPort)
	if len(got) != 1 || got[0].Status != nil || !strings.HasPrefix(got[0].Message, fmt.Sprintf("run-server on port %d: ", closedPort)) {
		t.Fatalf("refused: %+v", got)
	}
	h.status = 404
	got = run(open("1", nil), u.port)
	if len(got) != 1 || !strings.HasPrefix(got[0].Message, "data channel: ") || !strings.Contains(got[0].Message, "404") {
		t.Fatalf("data channel 404: %+v", got)
	}
}

func TestDataChannelSilenceClosesBothLegs(t *testing.T) {
	u, h, rep := newUpstream(t), newHub(t), &reports{}
	timings := config.DefaultTimings
	timings.HubSilenceMs = 200
	m := manager(h, u.port, rep, timings)
	done := make(chan struct{})
	go func() { m.HandleOpen(context.Background(), open("1", nil)); close(done) }()
	var data *websocket.Conn
	select {
	case data = <-h.conns:
	case <-time.After(10 * time.Second):
		t.Fatal("no data channel")
	}
	defer data.CloseNow()
	// The hub side never reads, so it answers no pings; after the silence window Tick drops the session.
	time.Sleep(400 * time.Millisecond)
	m.Tick(time.Now())
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("silent session not closed")
	}
	if m.Count() != 0 {
		t.Fatal("session still counted")
	}
}
