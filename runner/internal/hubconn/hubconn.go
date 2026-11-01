// Package hubconn keeps the agent's control connection to the hub: register, heartbeats, the hub-silence
// watchdog and reconnects with backoff (port of agent.js:234-325).
package hubconn

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

type Options struct {
	HubURL     string
	Token      config.Secret
	Register   func() protocol.Register // built at every connect, with the current slot health
	SlotHealth func() []protocol.Slot
	// OnOpen runs in its own goroutine per open frame. Its ctx is the one passed to Run (the agent's
	// lifetime), never the control connection's: sessions are their own sockets and outlive control
	// reconnects, as in agent.js.
	OnOpen  func(ctx context.Context, f protocol.Open)
	OnState func(State)
	Timings config.Timings
	Logger  *logging.Logger
	Rand    func() float64 // jitter source; injectable for tests
}

// State is what runtime.json and the tests observe.
type State struct {
	Connected  bool
	Registered bool
	LastError  *string
	Name       string // as the hub confirmed it
}

type Conn struct {
	o       Options
	mu      sync.Mutex
	state   State
	ws      *websocket.Conn
	silent  bool // this connection was dropped for hub silence
	delay   time.Duration
	lastHub atomic.Int64 // unix nanos of the last frame, ping or pong from the hub
	runCtx  context.Context
}

var noProxy = &http.Client{Transport: &http.Transport{Proxy: nil}}

func New(o Options) *Conn {
	if o.Rand == nil {
		o.Rand = rand.Float64
	}
	return &Conn{o: o, delay: ms(o.Timings.ReconnectBaseMs), runCtx: context.Background()}
}

func ms(n int) time.Duration { return time.Duration(n) * time.Millisecond }

// State returns a copy of the current state.
func (c *Conn) State() State {
	c.mu.Lock()
	defer c.mu.Unlock()
	s := c.state
	if s.LastError != nil {
		e := *s.LastError
		s.LastError = &e
	}
	return s
}

func (c *Conn) update(f func(*State)) {
	c.mu.Lock()
	f(&c.state)
	s := c.state
	c.mu.Unlock()
	if c.o.OnState != nil {
		c.o.OnState(s)
	}
}

func strp(s string) *string { return &s }

// Run connects and reconnects until ctx is done; on the way out the connection is closed with 1001.
func (c *Conn) Run(ctx context.Context) error {
	c.runCtx = ctx
	for ctx.Err() == nil {
		code, reason, wasRegistered := c.connect(ctx)
		if ctx.Err() != nil {
			break
		}
		c.mu.Lock()
		delay := nextDelay(c.delay, c.o.Rand(), code, c.o.Timings)
		c.delay = min(c.delay*2, ms(c.o.Timings.ReconnectMaxMs))
		lastErr := ""
		if c.state.LastError != nil {
			lastErr = *c.state.LastError
		}
		c.mu.Unlock()
		c.logClose(code, reason, wasRegistered, lastErr, delay)
		select {
		case <-ctx.Done():
		case <-time.After(delay):
		}
	}
	c.update(func(s *State) { s.Connected, s.Registered = false, false })
	return nil
}

// nextDelay is the reconnect delay: the current backoff plus up to 30% jitter, at least
// ReplacedDelayMs after a 4409 so two agents with one name don't kick each other in lock-step.
func nextDelay(cur time.Duration, r float64, code int, t config.Timings) time.Duration {
	d := cur + time.Duration(float64(cur)*0.3*r).Truncate(time.Millisecond)
	if code == protocol.CloseReplaced {
		d = max(d, ms(t.ReplacedDelayMs))
	}
	return d
}

func (c *Conn) logClose(code int, reason string, wasRegistered bool, lastErr string, delay time.Duration) {
	l, secs := c.o.Logger, int((delay+500*time.Millisecond)/time.Second)
	name := ""
	if c.o.Register != nil {
		name = c.o.Register().Name
	}
	or := func(fallback string) string {
		if reason != "" {
			return reason
		}
		return fallback
	}
	switch code {
	case protocol.CloseReplaced:
		l.Errorf(`another agent registered as "%s" and replaced this one; give each node a unique --name / NODE_NAME. Retrying in %ds`, name, secs)
	case protocol.CloseUnauthorized:
		l.Errorf("the hub rejected the agent token (%s); check --token / GATEWAY_AGENT_TOKEN or issue a new agent token. Retrying in %ds", or("unauthorized"), secs)
	case protocol.CloseForbidden:
		l.Errorf(`the hub won't let this agent token register as "%s" (%s); use the --name / NODE_NAME the token is bound to, or a token bound to this node. Retrying in %ds`, name, or("forbidden"), secs)
	case protocol.CloseTryAgainLater:
		l.Warnf("the hub can't check agent tokens right now (its database is unavailable); retrying in %ds", secs)
	default:
		what := "could not reach the hub"
		if wasRegistered {
			what = "lost the hub"
		}
		l.Warnf("%s (%s); retrying in %ds", what, lastErr, secs)
	}
}

// connect runs one control connection to its end and returns how it closed.
func (c *Conn) connect(ctx context.Context) (code int, reason string, wasRegistered bool) {
	header := http.Header{}
	if c.o.Token != "" {
		header.Set("Authorization", "Bearer "+c.o.Token.Value())
	}
	seen := func() { c.lastHub.Store(time.Now().UnixNano()) }
	dialCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	ws, _, err := websocket.Dial(dialCtx, c.o.HubURL+"/agent", &websocket.DialOptions{
		HTTPHeader:     header,
		HTTPClient:     noProxy,
		OnPingReceived: func(context.Context, []byte) bool { seen(); return true },
		OnPongReceived: func(context.Context, []byte) { seen() },
	})
	cancel()
	if err != nil {
		c.update(func(s *State) { s.Connected, s.Registered, s.LastError = false, false, strp(err.Error()) })
		return 1006, "", false
	}
	ws.SetReadLimit(protocol.ControlMaxFrame)
	seen()
	c.mu.Lock()
	c.ws, c.silent = ws, false
	c.mu.Unlock()
	c.update(func(s *State) { s.Connected = true })
	c.send(ws, c.o.Register())

	connCtx, connCancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		select {
		case <-ctx.Done():
			closeWithin(ws, 1001, "agent is shutting down", 2*time.Second)
		case <-connCtx.Done():
		}
	}()
	var readErr error
	for {
		typ, data, err := ws.Read(connCtx)
		if err != nil {
			readErr = err
			break
		}
		seen()
		if typ != websocket.MessageText {
			c.o.Logger.Warnf("ignoring a malformed frame from the hub")
			continue
		}
		c.handle(data)
	}
	connCancel()
	<-stopped
	ws.CloseNow()

	code = 1006
	var ce websocket.CloseError
	if errors.As(readErr, &ce) {
		code, reason = int(ce.Code), ce.Reason
	}
	c.mu.Lock()
	c.ws = nil
	silent := c.silent
	wasRegistered = c.state.Registered
	c.mu.Unlock()
	c.update(func(s *State) {
		s.Connected, s.Registered = false, false
		if code != 1006 || !silent || s.LastError == nil {
			text := fmt.Sprintf("hub closed the connection (%d)", code)
			if reason != "" {
				text = fmt.Sprintf("hub closed the connection (%d %s)", code, reason)
			}
			s.LastError = &text
		}
	})
	return code, reason, wasRegistered
}

func closeWithin(ws *websocket.Conn, code websocket.StatusCode, reason string, d time.Duration) {
	done := make(chan struct{})
	go func() {
		ws.Close(code, reason)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(d):
		ws.CloseNow()
	}
}

func (c *Conn) handle(data []byte) {
	in, err := protocol.ParseInbound(data)
	if err != nil {
		c.o.Logger.Warnf("ignoring a malformed frame from the hub")
		return
	}
	switch in.Type {
	case "registered":
		name, _ := protocol.StringField(in.Raw, "name")
		c.mu.Lock()
		c.delay = ms(c.o.Timings.ReconnectBaseMs)
		c.mu.Unlock()
		c.update(func(s *State) { s.Registered, s.LastError, s.Name = true, nil, name })
		c.o.Logger.Infof("registered with %s as %s", c.o.HubURL, name)
	case "open":
		if c.o.OnOpen != nil {
			go c.o.OnOpen(c.runCtx, protocol.ParseOpen(in.Raw))
		}
	case "error":
		msg, ok := protocol.StringField(in.Raw, "message")
		if !ok {
			var obj map[string]json.RawMessage
			json.Unmarshal(in.Raw, &obj)
			msg = string(obj["message"])
		}
		c.update(func(s *State) { s.LastError = strp(msg) })
		c.o.Logger.Errorf("hub: %s", msg)
	}
}

func (c *Conn) send(ws *websocket.Conn, v any) bool {
	b, err := json.Marshal(v)
	if err != nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := ws.Write(ctx, websocket.MessageText, b); err != nil {
		ws.CloseNow()
		return false
	}
	return true
}

func (c *Conn) current() (*websocket.Conn, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ws, c.state.Registered
}

// Tick runs every HeartbeatMs: drop a connection the hub has been silent on for HubSilenceMs, else ping
// it and, once registered, send a heartbeat.
func (c *Conn) Tick() {
	ws, registered := c.current()
	if ws == nil {
		return
	}
	silence := time.Since(time.Unix(0, c.lastHub.Load()))
	if silence > ms(c.o.Timings.HubSilenceMs) {
		c.o.Logger.Warnf("no word from the hub for %ds; reconnecting", c.o.Timings.HubSilenceMs/1000)
		c.mu.Lock()
		c.silent = true
		c.mu.Unlock()
		c.update(func(s *State) { s.LastError = strp("hub stopped answering") })
		ws.CloseNow()
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), ms(c.o.Timings.HeartbeatMs))
		defer cancel()
		ws.Ping(ctx)
	}()
	if registered {
		c.send(ws, protocol.Heartbeat{Type: "heartbeat", Slots: c.o.SlotHealth()})
	}
}

// SendHeartbeat pushes slot health now (urgent: a slot changed); a no-op until registered.
func (c *Conn) SendHeartbeat() {
	if ws, registered := c.current(); ws != nil && registered {
		c.send(ws, protocol.Heartbeat{Type: "heartbeat", Slots: c.o.SlotHealth()})
	}
}

// SendOpenFailed reports an open that could not be served.
func (c *Conn) SendOpenFailed(f protocol.OpenFailed) {
	f.Type = "open-failed"
	f.Message = truncate(f.Message, protocol.MaxOpenFailedMessage)
	if ws, _ := c.current(); ws != nil {
		c.send(ws, f)
	}
}

// truncate cuts s to at most n bytes on a rune boundary.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && s[n]&0xC0 == 0x80 {
		n--
	}
	return s[:n]
}
