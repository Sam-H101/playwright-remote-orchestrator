// Package session serves the hub's open frames: dial the slot's run-server, dial a data channel back to
// the hub, and pipe the two (port of agent.js:327-404).
package session

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/pipe"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

type Options struct {
	HubURL   string
	Token    config.Secret
	SlotPort func(index int) (port int, ok bool) // run-server port of slot 1..N
	Report   func(protocol.OpenFailed)
	Timings  config.Timings
	Logger   *logging.Logger
}

type session struct {
	id       string
	slot     int
	upstream *websocket.Conn
	data     *websocket.Conn
	last     atomic.Int64 // unix nanos of the last frame, ping or pong on the data channel
}

func (s *session) seen() { s.last.Store(time.Now().UnixNano()) }

type Manager struct {
	o        Options
	mu       sync.Mutex
	sessions map[*session]struct{}
}

var noProxy = &http.Client{Transport: &http.Transport{Proxy: nil}}

// openDeadline bounds the two dials, inside the hub's 15 s open timeout. The piped session has no deadline.
const openDeadline = 14 * time.Second

func NewManager(o Options) *Manager {
	return &Manager{o: o, sessions: map[*session]struct{}{}}
}

// Count is the number of piped sessions.
func (m *Manager) Count() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.sessions)
}

// HandleOpen runs one session to its end. ctx is the agent's lifetime.
func (m *Manager) HandleOpen(ctx context.Context, f protocol.Open) {
	log := m.o.Logger
	if !protocol.SessionIDRE.MatchString(f.ID) {
		log.Warnf("ignoring an open frame without a valid id")
		return
	}
	failed := func(message string, status *int) {
		log.Warnf("session %s: %s", f.ID, message)
		m.o.Report(protocol.OpenFailed{Type: "open-failed", ID: f.ID, Message: message, Status: status})
	}
	index, ok := slotIndex(f.Slot)
	port := 0
	if ok {
		port, ok = m.o.SlotPort(index)
	}
	if !ok {
		failed("no slot "+rawText(f.Slot), nil)
		return
	}
	path := "/"
	var p string
	if json.Unmarshal(f.Path, &p) == nil && strings.HasPrefix(p, "/") {
		path = p
	}
	query := ""
	var q string
	if json.Unmarshal(f.Query, &q) == nil && q != "" {
		query = "?" + q
	}
	headers := protocol.ForwardHeaders(headerValues(f.Headers))

	dialCtx, cancel := context.WithTimeout(ctx, openDeadline)
	defer cancel()
	upCtx, upCancel := context.WithTimeout(dialCtx, 10*time.Second)
	upstream, resp, err := websocket.Dial(upCtx, fmt.Sprintf("ws://127.0.0.1:%d%s%s", port, path, query), &websocket.DialOptions{
		HTTPHeader: headers,
		HTTPClient: noProxy,
	})
	upCancel()
	if err != nil {
		if resp != nil && resp.StatusCode != http.StatusSwitchingProtocols {
			// run-server refuses a bad version or launch options with an HTTP error whose body says why.
			// The websocket library keeps the first 1024 bytes of it, enough for the version lines.
			status := resp.StatusCode
			msg := fmt.Sprintf("run-server on port %d answered HTTP %d", port, status)
			if body := readBody(resp); body != "" {
				msg += ": " + body
			}
			failed(msg, &status)
			return
		}
		failed(fmt.Sprintf("run-server on port %d: %v", port, err), nil)
		return
	}
	upstream.SetReadLimit(protocol.MaxPayload)

	if d := m.o.Timings.OpenDelayMs; d > 0 {
		time.Sleep(time.Duration(d) * time.Millisecond)
	}
	s := &session{id: f.ID, slot: index, upstream: upstream}
	s.seen()
	header := http.Header{}
	if m.o.Token != "" {
		header.Set("Authorization", "Bearer "+m.o.Token.Value())
	}
	dataCtx, dataCancel := context.WithTimeout(dialCtx, 10*time.Second)
	data, _, err := websocket.Dial(dataCtx, fmt.Sprintf("%s/agent/data/%s", m.o.HubURL, f.ID), &websocket.DialOptions{
		HTTPHeader:     header,
		HTTPClient:     noProxy,
		OnPingReceived: func(context.Context, []byte) bool { s.seen(); return true },
		OnPongReceived: func(context.Context, []byte) { s.seen() },
	})
	dataCancel()
	if err != nil {
		failed(fmt.Sprintf("data channel: %v", err), nil)
		upstream.Close(websocket.StatusInternalError, "")
		return
	}
	cancel()
	data.SetReadLimit(protocol.MaxPayload)
	s.data = data
	s.seen()
	m.mu.Lock()
	m.sessions[s] = struct{}{}
	m.mu.Unlock()
	log.Infof("session %s on slot %d opened", f.ID, index)
	up, dn := pipe.Run(ctx, upstream, data)
	m.mu.Lock()
	delete(m.sessions, s)
	m.mu.Unlock()
	log.Infof("session %s on slot %d closed (run-server %d, hub %d)", f.ID, index, up.Code, dn.Code)
}

// slotIndex accepts what JavaScript's slots[msg.slot - 1] would: an integer.
func slotIndex(raw json.RawMessage) (int, bool) {
	var n float64
	if json.Unmarshal(raw, &n) != nil || n != math.Trunc(n) || n < 1 || n > protocol.MaxSlots {
		return 0, false
	}
	return int(n), true
}

func rawText(raw json.RawMessage) string {
	var s *string
	if json.Unmarshal(raw, &s) == nil && s != nil {
		return *s
	}
	if len(raw) == 0 {
		return "undefined"
	}
	return string(raw)
}

// headerValues keeps string and all-string-array values; anything else is dropped.
func headerValues(h map[string]json.RawMessage) map[string][]string {
	out := map[string][]string{}
	for k, raw := range h {
		var s string
		if json.Unmarshal(raw, &s) == nil {
			out[k] = []string{s}
			continue
		}
		var list []string
		if json.Unmarshal(raw, &list) == nil && list != nil {
			out[k] = list
		}
	}
	return out
}

func readBody(resp *http.Response) string {
	if resp.Body == nil {
		return ""
	}
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 2000))
	return strings.TrimSpace(string(b))
}

// Tick runs every HeartbeatMs: a data channel silent for HubSilenceMs means the hub (or the path to it)
// is gone, so both legs are dropped and run-server closes the session's browser; the others are pinged.
func (m *Manager) Tick(now time.Time) {
	m.mu.Lock()
	list := make([]*session, 0, len(m.sessions))
	for s := range m.sessions {
		list = append(list, s)
	}
	m.mu.Unlock()
	silence := time.Duration(m.o.Timings.HubSilenceMs) * time.Millisecond
	for _, s := range list {
		if now.Sub(time.Unix(0, s.last.Load())) > silence {
			m.o.Logger.Warnf("session %s: no word from the hub on its data channel for %ds; closing it", s.id, m.o.Timings.HubSilenceMs/1000)
			s.data.CloseNow()
			s.upstream.CloseNow()
			continue
		}
		go func(c *websocket.Conn) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			c.Ping(ctx)
		}(s.data)
	}
}

// CloseAll closes both legs of every session (on stop: 1001 "agent is shutting down"), giving each
// close handshake up to 2 s.
func (m *Manager) CloseAll(code int, reason string) {
	m.mu.Lock()
	list := make([]*session, 0, len(m.sessions))
	for s := range m.sessions {
		list = append(list, s)
	}
	m.mu.Unlock()
	var wg sync.WaitGroup
	for _, s := range list {
		for _, c := range []*websocket.Conn{s.data, s.upstream} {
			wg.Add(1)
			go func(c *websocket.Conn) {
				defer wg.Done()
				done := make(chan struct{})
				go func() { c.Close(websocket.StatusCode(code), reason); close(done) }()
				select {
				case <-done:
				case <-time.After(2 * time.Second):
					c.CloseNow()
				}
			}(c)
		}
	}
	wg.Wait()
}
