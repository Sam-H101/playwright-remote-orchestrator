// Package agent wires the control connection, the slots, the sessions and runtime.json together: the Go
// counterpart of gateway/agent.js's Agent class.
package agent

import (
	"context"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/hubconn"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/session"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/slot"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/status"
)

type Agent struct {
	s          config.Settings
	version    string
	pwVersion  string
	log        *logging.Logger
	slots      []*slot.Slot
	hub        *hubconn.Conn
	sessions   *session.Manager
	writer     *status.Writer
	statusMu   sync.Mutex
	OnSlowSlot func(index int, delay time.Duration) // the service raises an Event Log warning
}

// New builds an agent. Settings.Watchdog must name parent-watchdog.js on disk.
func New(s config.Settings, version string, log *logging.Logger) *Agent {
	log.Redact(s.Token.Value())
	a := &Agent{s: s, version: version, pwVersion: config.PlaywrightVersion(s.PlaywrightCLI), log: log}
	a.writer = status.NewWriter(s.StatusFile, log)
	env := childEnv(s)
	for i := 1; i <= s.Slots; i++ {
		a.slots = append(a.slots, slot.New(slot.Options{
			Index: i, Port: s.BasePort + i - 1, NodeExe: s.NodeExe, CLI: s.PlaywrightCLI, Env: env, LogDir: s.LogDir,
			Timings: s.Timings, Logger: log,
			OnHealth: func(int, bool) { a.changed(true) },
			OnChange: func() { a.changed(false) },
			OnSlow: func(index int, delay time.Duration) {
				if a.OnSlowSlot != nil {
					a.OnSlowSlot(index, delay)
				}
			},
		}))
	}
	a.hub = hubconn.New(hubconn.Options{
		HubURL: s.Hub, Token: s.Token, Timings: s.Timings, Logger: log,
		Register:   a.register,
		SlotHealth: a.slotHealth,
		OnOpen:     func(ctx context.Context, f protocol.Open) { a.sessions.HandleOpen(ctx, f) },
		OnState:    func(hubconn.State) { a.changed(false) },
	})
	a.sessions = session.NewManager(session.Options{
		HubURL: s.Hub, Token: s.Token, Timings: s.Timings, Logger: log,
		SlotPort: func(i int) (int, bool) {
			if i < 1 || i > s.Slots {
				return 0, false
			}
			return s.BasePort + i - 1, true
		},
		Report: a.hub.SendOpenFailed,
	})
	return a
}

// childEnv is the run-servers' environment: the allow-list of decision 14 plus the watchdog preload.
func childEnv(s config.Settings) []string {
	environ := os.Environ()
	orig := proc.LookupEnv(environ, "NODE_OPTIONS")
	nodeOptions, stash := slot.NodeOptions(orig, s.Watchdog)
	set := map[string]string{"NODE_OPTIONS": nodeOptions, "GATEWAY_AGENT_NODE_OPTIONS": stash}
	if s.BrowsersPath != "" {
		set["PLAYWRIGHT_BROWSERS_PATH"] = s.BrowsersPath
	}
	if s.DownloadHost != "" {
		set["PLAYWRIGHT_DOWNLOAD_HOST"] = s.DownloadHost
	}
	env := proc.ChildEnv(environ, set)
	if stash == "" {
		// The watchdog restores NODE_OPTIONS from the stash only when the stash variable exists.
		env = append(env, "GATEWAY_AGENT_NODE_OPTIONS=")
	}
	return env
}

func (a *Agent) register() protocol.Register {
	return protocol.Register{
		Type: "register", Name: a.s.Name, Slots: a.slotHealth(), PlaywrightVersion: a.pwVersion,
		AgentVersion: a.version, Platform: protocol.Platform(), Browsers: a.s.Browsers, Ephemeral: a.s.Ephemeral,
	}
}

func (a *Agent) slotHealth() []protocol.Slot {
	out := make([]protocol.Slot, len(a.slots))
	for i, sl := range a.slots {
		out[i] = protocol.Slot{Index: i + 1, Healthy: sl.Healthy()}
	}
	return out
}

// changed writes runtime.json; urgent also pushes slot health to the hub at once.
func (a *Agent) changed(urgent bool) {
	a.writeStatus()
	if urgent {
		a.hub.SendHeartbeat()
	}
}

func (a *Agent) writeStatus() {
	a.statusMu.Lock()
	defer a.statusMu.Unlock()
	a.writer.Write(a.Snapshot())
}

// Snapshot is the runtime.json content.
func (a *Agent) Snapshot() status.File {
	st := a.hub.State()
	f := status.File{
		Name: a.s.Name, Hub: a.s.Hub, AgentVersion: a.version, PlaywrightVersion: a.pwVersion, PID: os.Getpid(),
		Connected: st.Connected, Registered: st.Registered, LastError: st.LastError, Updated: status.Now(),
		Slots: make([]status.SlotStatus, len(a.slots)),
	}
	for i, sl := range a.slots {
		ss := sl.Status()
		f.Slots[i] = status.SlotStatus{Index: ss.Index, Port: ss.Port, PID: ss.PID, Healthy: ss.Healthy, Restarts: ss.Restarts}
		if ss.Since != nil {
			since := status.ISO(*ss.Since)
			f.Slots[i].Since = &since
		}
	}
	return f
}

// Sessions is the number of piped sessions.
func (a *Agent) Sessions() int { return a.sessions.Count() }

// Run starts the slots and the control connection and blocks until ctx is done, then stops: sessions
// closed with 1001, the control connection closed, run-servers killed, a final runtime.json written.
func (a *Agent) Run(ctx context.Context) error {
	s := a.s
	token := "unset"
	if s.Token != "" {
		token = "set"
	}
	a.log.Infof("agent %s node %s hub %s slots %d base-port %d cli %s node %s browsers %s token %s",
		a.version, s.Name, s.Hub, s.Slots, s.BasePort, s.PlaywrightCLI, s.NodeExe, strings.Join(s.Browsers, ","), token)
	a.log.Infof("playwright %s, run-servers on 127.0.0.1:%d-%d", a.pwVersion, s.BasePort, s.BasePort+s.Slots-1)
	if s.Token == "" {
		a.log.Warnf("no GATEWAY_AGENT_TOKEN set; the hub will reject this agent if it requires one")
	}

	slotCtx, stopSlots := context.WithCancel(context.Background())
	defer stopSlots()
	var slotsDone sync.WaitGroup
	for _, sl := range a.slots {
		slotsDone.Add(1)
		go func(sl *slot.Slot) { defer slotsDone.Done(); sl.Run(slotCtx) }(sl)
	}
	hubCtx, stopHub := context.WithCancel(context.Background())
	hubDone := make(chan struct{})
	go func() { a.hub.Run(hubCtx); close(hubDone) }()

	heartbeat := time.NewTicker(time.Duration(s.Timings.HeartbeatMs) * time.Millisecond)
	defer heartbeat.Stop()
	statusTick := time.NewTicker(time.Duration(s.Timings.StatusMs) * time.Millisecond)
	defer statusTick.Stop()
	a.writeStatus()
loop:
	for {
		select {
		case <-ctx.Done():
			break loop
		case now := <-heartbeat.C:
			a.sessions.Tick(now)
			a.hub.Tick()
		case <-statusTick.C:
			a.writeStatus()
		}
	}

	a.log.Infof("stopping: closing sessions and run-servers")
	stopped := make(chan struct{})
	go func() {
		a.sessions.CloseAll(1001, "agent is shutting down")
		stopHub()
		<-hubDone
		var wg sync.WaitGroup
		for _, sl := range a.slots {
			wg.Add(1)
			go func(sl *slot.Slot) { defer wg.Done(); sl.Stop() }(sl)
		}
		wg.Wait()
		stopSlots()
		slotsDone.Wait()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(10 * time.Second):
		a.log.Warnf("stop did not finish within 10s")
	}
	a.writeStatus()
	return nil
}
