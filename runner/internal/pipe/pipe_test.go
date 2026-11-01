package pipe

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

// setup builds client ca -> [sa | pipe | cb] -> sb: the test drives ca and sb.
type setup struct {
	ca, sb *websocket.Conn
	done   chan [2]Result
	toSA   *Writes // frames being written to sa, the agent's side of ca
}

func newSetup(t *testing.T) *setup {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	toSA := &Writes{}
	accepted := func(opts *websocket.AcceptOptions) (*httptest.Server, chan *websocket.Conn) {
		ch := make(chan *websocket.Conn, 1)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			c, err := websocket.Accept(w, r, opts)
			if err != nil {
				return
			}
			c.SetReadLimit(protocol.MaxPayload)
			ch <- c
			<-ctx.Done()
		}))
		t.Cleanup(srv.Close)
		return srv, ch
	}
	// sa answers pings as the session code configures it: not while a write to it is in progress.
	s1, s1c := accepted(&websocket.AcceptOptions{OnPingReceived: func(context.Context, []byte) bool { return !toSA.InProgress() }})
	s2, s2c := accepted(nil)
	dial := func(url string) *websocket.Conn {
		c, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(url, "http"), nil)
		if err != nil {
			t.Fatal(err)
		}
		c.SetReadLimit(protocol.MaxPayload)
		return c
	}
	s := &setup{done: make(chan [2]Result, 1), toSA: toSA}
	s.ca = dial(s1.URL)
	sa := <-s1c
	cb := dial(s2.URL)
	s.sb = <-s2c
	go func() {
		a, b := Run(ctx, sa, cb, toSA, nil)
		s.done <- [2]Result{a, b}
	}()
	t.Cleanup(func() { s.ca.CloseNow(); s.sb.CloseNow() })
	return s
}

func read(t *testing.T, c *websocket.Conn) (websocket.MessageType, []byte, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return c.Read(ctx)
}

func TestFramesKeepTheirType(t *testing.T) {
	s := newSetup(t)
	ctx := context.Background()
	s.ca.Write(ctx, websocket.MessageText, []byte("hello"))
	typ, b, err := read(t, s.sb)
	if err != nil || typ != websocket.MessageText || string(b) != "hello" {
		t.Fatal(typ, string(b), err)
	}
	s.sb.Write(ctx, websocket.MessageBinary, []byte{0, 1, 2, 255})
	typ, b, err = read(t, s.ca)
	if err != nil || typ != websocket.MessageBinary || !bytes.Equal(b, []byte{0, 1, 2, 255}) {
		t.Fatal(typ, b, err)
	}
	big := bytes.Repeat([]byte{7}, 5<<20)
	go s.ca.Write(ctx, websocket.MessageBinary, big)
	_, b, err = read(t, s.sb)
	if err != nil || !bytes.Equal(b, big) {
		t.Fatalf("5 MiB frame: %d bytes, %v", len(b), err)
	}
}

func closeOf(t *testing.T, c *websocket.Conn) (int, string) {
	t.Helper()
	_, _, err := read(t, c)
	var ce websocket.CloseError
	if !errors.As(err, &ce) {
		t.Fatalf("want a close, got %v", err)
	}
	return int(ce.Code), ce.Reason
}

func TestCloseMirroring(t *testing.T) {
	t.Run("client code and reason reach the server", func(t *testing.T) {
		s := newSetup(t)
		go s.ca.Close(4123, "client says bye")
		if code, reason := closeOf(t, s.sb); code != 4123 || reason != "client says bye" {
			t.Fatal(code, reason)
		}
		r := <-s.done
		if r[0].Code != 4123 || r[1].Code != 4123 {
			t.Fatalf("results %+v", r)
		}
	})
	t.Run("server code reaches the client", func(t *testing.T) {
		s := newSetup(t)
		go s.sb.Close(4321, "server says bye")
		if code, reason := closeOf(t, s.ca); code != 4321 || reason != "server says bye" {
			t.Fatal(code, reason)
		}
		<-s.done
	})
	t.Run("an abrupt loss becomes 1011", func(t *testing.T) {
		s := newSetup(t)
		s.ca.CloseNow()
		if code, _ := closeOf(t, s.sb); code != 1011 {
			t.Fatal(code)
		}
		<-s.done
	})
	t.Run("a close without a code becomes 1000", func(t *testing.T) {
		s := newSetup(t)
		go s.ca.Close(websocket.StatusNoStatusRcvd, "")
		if code, _ := closeOf(t, s.sb); code != 1000 {
			t.Fatal(code)
		}
		<-s.done
	})
}

func TestMirrorMapping(t *testing.T) {
	long := strings.Repeat("x", 200)
	for _, c := range []struct {
		in       Result
		code     int
		reasonLn int
	}{{Result{Code: 1006}, 1011, 0}, {Result{Code: 1005}, 1000, 0}, {Result{Code: 1015}, 1011, 0}, {Result{Code: 4000, Reason: long}, 4000, 123}} {
		code, reason := mirror(c.in)
		if code != c.code || len(reason) != c.reasonLn {
			t.Fatalf("%+v -> %d %d", c.in, code, len(reason))
		}
	}
}

func TestBackpressure(t *testing.T) {
	s := newSetup(t)
	frame := bytes.Repeat([]byte{1}, 1<<20)
	const frames = 48
	var written atomic.Int32
	go func() {
		for i := 0; i < frames; i++ {
			if s.sb.Write(context.Background(), websocket.MessageBinary, frame) != nil {
				return
			}
			written.Add(1)
		}
	}()
	time.Sleep(2 * time.Second)
	if n := written.Load(); n >= 24 {
		t.Fatalf("%d of %d frames went through with nobody reading: no backpressure", n, frames)
	}
	for i := 0; i < frames; i++ {
		if _, b, err := read(t, s.ca); err != nil || len(b) != len(frame) {
			t.Fatalf("frame %d: %d bytes, %v", i, len(b), err)
		}
	}
}

// A peer that stops reading for longer than the websocket library's 5 s control-frame timeout, and pings
// meanwhile, must not lose the connection: the pong would queue behind the blocked write and the
// library would close the connection when its timeout fired.
func TestPingDuringStallKeepsConnection(t *testing.T) {
	s := newSetup(t)
	frame := bytes.Repeat([]byte{1}, 1<<20)
	const frames = 48
	go func() {
		for i := 0; i < frames; i++ {
			if s.sb.Write(context.Background(), websocket.MessageBinary, frame) != nil {
				return
			}
		}
	}()
	deadline := time.Now().Add(2 * time.Second)
	for !s.toSA.InProgress() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !s.toSA.InProgress() {
		t.Fatal("the write toward the stalled reader never blocked")
	}
	for i := 0; i < 3; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		s.ca.Ping(ctx) // no pong expected while the write is blocked
		cancel()
		time.Sleep(500 * time.Millisecond)
	}
	time.Sleep(4 * time.Second) // well past the library's 5 s timeout since the first ping
	for i := 0; i < frames; i++ {
		if _, b, err := read(t, s.ca); err != nil || len(b) != len(frame) {
			t.Fatalf("frame %d after the stall: %d bytes, %v", i, len(b), err)
		}
	}
}
