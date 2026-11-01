//go:build integration

// Package integration runs the pwrunner binary against the real Node hub (gateway/hub.js, in-memory
// registry) with the gateway's fake run-server.
package integration

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
)

var (
	repo     string // repository root
	pwrunner string // built in TestMain
	nodeExe  string
	fakeCLI  string
)

// Setup builds pwrunner and checks the Node prerequisites; TestMain calls it.
func Setup() (cleanup func(), err error) {
	wd, _ := os.Getwd()
	repo = filepath.Clean(filepath.Join(wd, "..", "..", ".."))
	if nodeExe, err = exec.LookPath("node"); err != nil {
		return nil, fmt.Errorf("node is not on PATH")
	}
	fakeCLI = filepath.Join(repo, "gateway", "test", "fixtures", "fake-playwright", "cli.js")
	if _, err := os.Stat(filepath.Join(repo, "gateway", "node_modules", "ws")); err != nil {
		return nil, fmt.Errorf("run npm ci in gateway/ (the hub and the fake run-server need its node_modules)")
	}
	dir, err := os.MkdirTemp("", "pwrunner-it-")
	if err != nil {
		return nil, err
	}
	pwrunner = filepath.Join(dir, "pwrunner")
	if runtime.GOOS == "windows" {
		pwrunner += ".exe"
	}
	build := exec.Command("go", "build", "-ldflags", "-X main.version=it-1", "-o", pwrunner, "./cmd/pwrunner")
	build.Dir = filepath.Join(repo, "runner")
	if out, err := build.CombinedOutput(); err != nil {
		os.RemoveAll(dir)
		return nil, fmt.Errorf("go build: %v\n%s", err, out)
	}
	return func() { os.RemoveAll(dir) }, nil
}

// Hub is a running gateway/hub.js.
type Hub struct {
	Port  int
	Token string
	cmd   *exec.Cmd
	out   *syncBuffer
}

func (h *Hub) URL() string  { return fmt.Sprintf("ws://127.0.0.1:%d", h.Port) }
func (h *Hub) HTTP() string { return fmt.Sprintf("http://127.0.0.1:%d", h.Port) }
func (h *Hub) Log() string  { return h.out.String() }

var listenRE = regexp.MustCompile(`hub listening on 127\.0\.0\.1:(\d+)`)

// StartHub starts the hub on port (0 = any) with the agent token and extra environment.
func StartHub(t *testing.T, port int, token string, env ...string) *Hub {
	t.Helper()
	cmd := exec.Command(nodeExe, "hub.js")
	cmd.Dir = filepath.Join(repo, "gateway")
	cmd.Env = append(os.Environ(), "HUB_DB=memory", "HUB_PORT="+strconv.Itoa(port), "HUB_HOST=127.0.0.1",
		"GATEWAY_AGENT_TOKEN="+token, "GATEWAY_CLIENT_TOKEN=", "HEARTBEAT_TIMEOUT_S=6", "OPEN_TIMEOUT_S=5")
	cmd.Env = append(cmd.Env, env...)
	h := &Hub{Token: token, cmd: cmd, out: &syncBuffer{}}
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = io.MultiWriter(h.out, pw), h.out
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(h.Stop)
	found := make(chan int, 1)
	go func() {
		sc := bufio.NewScanner(pr)
		for sc.Scan() {
			if m := listenRE.FindStringSubmatch(sc.Text()); m != nil {
				p, _ := strconv.Atoi(m[1])
				select {
				case found <- p:
				default:
				}
			}
		}
	}()
	select {
	case h.Port = <-found:
	case <-time.After(15 * time.Second):
		t.Fatalf("hub did not start:\n%s", h.out)
	}
	return h
}

// Stop kills the hub.
func (h *Hub) Stop() {
	if h.cmd.Process != nil && h.cmd.ProcessState == nil {
		h.cmd.Process.Kill()
		h.cmd.Wait()
	}
}

// Agent is a running `pwrunner run`.
type Agent struct {
	Name       string
	BasePort   int
	StatusFile string
	cmd        *exec.Cmd
	out        *syncBuffer
	exited     chan struct{}
}

type AgentOpts struct {
	Slots   int
	Token   string
	Timings string // --timings JSON
	Group   bool   // own process group, for Ctrl-Break / SIGTERM tests
}

// StartAgent starts pwrunner run against hubURL.
func StartAgent(t *testing.T, hubURL, name string, o AgentOpts) *Agent {
	t.Helper()
	if o.Slots == 0 {
		o.Slots = 2
	}
	dir := t.TempDir()
	a := &Agent{Name: name, BasePort: FreePorts(t, o.Slots), StatusFile: filepath.Join(dir, "runtime.json"), out: &syncBuffer{}, exited: make(chan struct{})}
	args := []string{"run", "--hub", hubURL, "--name", name, "--slots", strconv.Itoa(o.Slots), "--base-port", strconv.Itoa(a.BasePort),
		"--playwright-cli", fakeCLI, "--node", nodeExe, "--status-file", a.StatusFile, "--log-dir", filepath.Join(dir, "logs")}
	if o.Timings != "" {
		args = append(args, "--timings", o.Timings)
	}
	a.cmd = exec.Command(pwrunner, args...)
	a.cmd.Env = append(os.Environ(), "GATEWAY_AGENT_TOKEN="+o.Token)
	a.cmd.Stdout, a.cmd.Stderr = a.out, a.out
	if o.Group {
		newGroup(a.cmd)
	}
	if err := a.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { a.cmd.Wait(); close(a.exited) }()
	t.Cleanup(func() {
		select {
		case <-a.exited:
		default:
			killTree(a.cmd.Process.Pid)
			select {
			case <-a.exited:
			case <-time.After(10 * time.Second):
			}
		}
		if t.Failed() {
			t.Logf("agent %s output:\n%s", name, a.out)
		}
	})
	return a
}

func (a *Agent) Log() string { return a.out.String() }
func (a *Agent) PID() int    { return a.cmd.Process.Pid }

// Status reads runtime.json (nil while it is missing or mid-rename).
func (a *Agent) Status() *StatusFile {
	b, err := os.ReadFile(a.StatusFile)
	if err != nil {
		return nil
	}
	var s StatusFile
	if json.Unmarshal(b, &s) != nil {
		return nil
	}
	return &s
}

type StatusFile struct {
	Name       string  `json:"name"`
	PID        int     `json:"pid"`
	Connected  bool    `json:"connected"`
	Registered bool    `json:"registered"`
	LastError  *string `json:"lastError"`
	Slots      []struct {
		Index    int  `json:"index"`
		Port     int  `json:"port"`
		PID      *int `json:"pid"`
		Healthy  bool `json:"healthy"`
		Restarts int  `json:"restarts"`
	} `json:"slots"`
}

// Ready waits until the agent is registered with every slot healthy.
func (a *Agent) Ready(t *testing.T) *StatusFile {
	t.Helper()
	var s *StatusFile
	WaitFor(t, "agent "+a.Name+" registered and healthy", 20*time.Second, func() bool {
		s = a.Status()
		if s == nil || !s.Registered || len(s.Slots) == 0 {
			return false
		}
		for _, sl := range s.Slots {
			if !sl.Healthy {
				return false
			}
		}
		return true
	})
	return s
}

// Node is one entry of /api/nodes.
type Node struct {
	Name              string   `json:"name"`
	Slots             int      `json:"slots"`
	HealthySlots      int      `json:"healthySlots"`
	PlaywrightVersion string   `json:"playwrightVersion"`
	AgentVersion      string   `json:"agentVersion"`
	Platform          string   `json:"platform"`
	Browsers          []string `json:"browsers"`
}

func (h *Hub) Node(name string) *Node {
	resp, err := http.Get(h.HTTP() + "/api/nodes")
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	var body struct{ Nodes []Node }
	json.NewDecoder(resp.Body).Decode(&body)
	for _, n := range body.Nodes {
		if n.Name == name {
			return &n
		}
	}
	return nil
}

// Connect opens a client session through the hub; status is the HTTP status of a refused upgrade.
func Connect(url string, header http.Header) (*websocket.Conn, int, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	c, resp, err := websocket.Dial(ctx, url, &websocket.DialOptions{HTTPHeader: header})
	if err != nil {
		if resp != nil {
			b, _ := io.ReadAll(resp.Body)
			return nil, resp.StatusCode, string(b), err
		}
		return nil, 0, "", err
	}
	c.SetReadLimit(256 << 20)
	return c, 101, "", nil
}

func Echo(t *testing.T, c *websocket.Conn, typ websocket.MessageType, msg []byte) []byte {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := c.Write(ctx, typ, msg); err != nil {
		t.Fatal(err)
	}
	gt, b, err := c.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if gt != typ {
		t.Fatalf("frame type %v, want %v", gt, typ)
	}
	return b
}

func WaitFor(t *testing.T, what string, timeout time.Duration, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ok() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("timed out after %v waiting for %s", timeout, what)
}

// FreePorts finds n consecutive free ports.
func FreePorts(t *testing.T, n int) int {
	for attempt := 0; attempt < 200; attempt++ {
		var b [2]byte
		rand.Read(b[:])
		base := 20000 + int(b[0])<<8%30000 + int(b[1])
		ok := true
		for i := 0; i < n && ok; i++ {
			l, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(base+i))
			if err != nil {
				ok = false
				break
			}
			l.Close()
		}
		if ok {
			return base
		}
	}
	t.Fatal("no free ports")
	return 0
}

func RandomToken() string {
	var b [16]byte
	rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// Proxy is a TCP relay that can be frozen: it then forwards nothing and passes on no FIN or RST, like a
// hub host that lost power. Connections are numbered in accept order from 1.
type Proxy struct {
	Port   int
	l      net.Listener
	mu     sync.Mutex
	pairs  []*pair
	frozen atomic.Bool
}

type pair struct {
	frozen atomic.Bool
}

func StartProxy(t *testing.T, target int) *Proxy {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p := &Proxy{l: l, Port: l.Addr().(*net.TCPAddr).Port}
	t.Cleanup(func() { l.Close() })
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			u, err := net.Dial("tcp", "127.0.0.1:"+strconv.Itoa(target))
			if err != nil {
				c.Close()
				continue
			}
			pr := &pair{}
			if p.frozen.Load() {
				pr.frozen.Store(true)
			}
			p.mu.Lock()
			p.pairs = append(p.pairs, pr)
			p.mu.Unlock()
			relay := func(a, b net.Conn) {
				buf := make([]byte, 64<<10)
				for {
					n, err := a.Read(buf)
					if n > 0 && !pr.frozen.Load() {
						b.Write(buf[:n])
					}
					if err != nil {
						if !pr.frozen.Load() {
							b.Close()
						}
						return
					}
				}
			}
			go relay(c, u)
			go relay(u, c)
		}
	}()
	return p
}

// Freeze stops every connection, current and future.
func (p *Proxy) Freeze() {
	p.frozen.Store(true)
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, pr := range p.pairs {
		pr.frozen.Store(true)
	}
}

// FreezeConn stops connection n (1-based) only.
func (p *Proxy) FreezeConn(n int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.pairs[n-1].frozen.Store(true)
}

// RSS is a process's resident memory in bytes.
func RSS(pid int) (int64, error) {
	if runtime.GOOS == "windows" {
		out, err := exec.Command("tasklist", "/FI", fmt.Sprintf("PID eq %d", pid), "/FO", "CSV", "/NH").Output()
		if err != nil {
			return 0, err
		}
		fields := strings.Split(strings.TrimSpace(string(out)), `","`)
		if len(fields) < 5 {
			return 0, fmt.Errorf("tasklist: %q", out)
		}
		kb := strings.NewReplacer(",", "", ".", "", " K", "", `"`, "", " ", "").Replace(fields[4])
		n, err := strconv.ParseInt(strings.TrimSpace(kb), 10, 64)
		return n * 1024, err
	}
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(line, "VmRSS:") {
			n, err := strconv.ParseInt(strings.Fields(line)[1], 10, 64)
			return n * 1024, err
		}
	}
	return 0, fmt.Errorf("no VmRSS")
}

type syncBuffer struct {
	mu sync.Mutex
	b  strings.Builder
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}
