// Package logging writes one line per event, "[yyyy-MM-dd HH:mm:ss] LEVEL message" in local time,
// and rotates log files by size.
package logging

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Logger is safe for concurrent use. A nil *Logger discards everything.
type Logger struct {
	mu     sync.Mutex
	w      io.Writer
	redact []string
	debug  bool
	hooks  []func(level, msg string)
}

// New returns a logger writing to w that replaces every occurrence of a redact value with <redacted>.
func New(w io.Writer, redact []string) *Logger {
	l := &Logger{w: w}
	l.Redact(redact...)
	return l
}

// Discard is a logger that writes nothing.
func Discard() *Logger { return New(io.Discard, nil) }

// Redact adds values that must never appear in a log line (defence in depth: the token must never be
// formatted into a message in the first place).
func (l *Logger) Redact(values ...string) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, v := range values {
		if v != "" {
			l.redact = append(l.redact, v)
		}
	}
}

// SetDebug turns DEBUG lines on or off.
func (l *Logger) SetDebug(on bool) {
	if l != nil {
		l.mu.Lock()
		l.debug = on
		l.mu.Unlock()
	}
}

// OnLog registers a function called with every line written (the service mirrors some to the Event Log).
func (l *Logger) OnLog(f func(level, msg string)) {
	if l != nil {
		l.mu.Lock()
		l.hooks = append(l.hooks, f)
		l.mu.Unlock()
	}
}

// SetOutput replaces the writer.
func (l *Logger) SetOutput(w io.Writer) {
	if l != nil {
		l.mu.Lock()
		l.w = w
		l.mu.Unlock()
	}
}

func (l *Logger) log(level, format string, a ...any) {
	if l == nil {
		return
	}
	msg := fmt.Sprintf(format, a...)
	l.mu.Lock()
	defer l.mu.Unlock()
	if level == "DEBUG" && !l.debug {
		return
	}
	for _, v := range l.redact {
		msg = strings.ReplaceAll(msg, v, "<redacted>")
	}
	msg = strings.ReplaceAll(msg, "\n", " ")
	fmt.Fprintf(l.w, "[%s] %s %s\n", time.Now().Format("2006-01-02 15:04:05"), level, msg)
	for _, h := range l.hooks {
		h(level, msg)
	}
}

func (l *Logger) Infof(format string, a ...any)  { l.log("INFO", format, a...) }
func (l *Logger) Warnf(format string, a ...any)  { l.log("WARN", format, a...) }
func (l *Logger) Errorf(format string, a ...any) { l.log("ERROR", format, a...) }
func (l *Logger) Debugf(format string, a ...any) { l.log("DEBUG", format, a...) }

// Rotating is a file that is renamed to <path>.1 (and older generations shifted up to <path>.<keep>)
// once it reaches maxBytes.
type Rotating struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
	keep     int
	f        *os.File
	size     int64
}

// OpenRotating opens (appending) or creates path, creating its directory.
func OpenRotating(path string, maxBytes int64, keep int) (*Rotating, error) {
	if maxBytes <= 0 {
		maxBytes = 10 << 20
	}
	if keep < 0 {
		keep = 0
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	r := &Rotating{path: path, maxBytes: maxBytes, keep: keep}
	if err := r.open(); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *Rotating) open() error {
	f, err := os.OpenFile(r.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	r.f, r.size = f, st.Size()
	return nil
}

func (r *Rotating) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f == nil {
		return 0, os.ErrClosed
	}
	if r.size > 0 && r.size+int64(len(p)) > r.maxBytes {
		r.rotate()
	}
	n, err := r.f.Write(p)
	r.size += int64(n)
	return n, err
}

func (r *Rotating) rotate() {
	r.f.Close()
	if r.keep == 0 {
		os.Remove(r.path)
	} else {
		os.Remove(fmt.Sprintf("%s.%d", r.path, r.keep))
		for i := r.keep - 1; i >= 1; i-- {
			os.Rename(fmt.Sprintf("%s.%d", r.path, i), fmt.Sprintf("%s.%d", r.path, i+1))
		}
		os.Rename(r.path, r.path+".1")
	}
	if err := r.open(); err != nil {
		r.f = nil
	}
}

// Close closes the file.
func (r *Rotating) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f == nil {
		return nil
	}
	err := r.f.Close()
	r.f = nil
	return err
}
