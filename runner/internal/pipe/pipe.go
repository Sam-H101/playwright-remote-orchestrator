// Package pipe joins two WebSocket connections frame for frame (port of gateway/lib/pipe.js).
package pipe

import (
	"context"
	"errors"
	"io"
	"sync"

	"github.com/coder/websocket"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/protocol"
)

// Result is how one side ended: the close code it sent (1005 for none, 1006 for an abrupt loss) and
// reason, or the error when it was not a close.
type Result struct {
	Code   int
	Reason string
	Err    error
}

// Run copies frames in both directions until either side closes, then closes the other side with the
// same code and reason (made sendable). Frame types are preserved and frames are streamed, never
// buffered whole: a direction reads its next frame only once the previous one is fully written, so a
// slow reader pushes back on the sender through TCP instead of growing memory.
func Run(ctx context.Context, a, b *websocket.Conn) (closeA, closeB Result) {
	conns := [2]*websocket.Conn{a, b}
	type end struct {
		from      int
		srcClosed bool
		res       Result
	}
	ends := make(chan end, 2)
	for i := range conns {
		go func(i int) {
			closed, res := copyFrames(ctx, conns[i], conns[1-i])
			ends <- end{i, closed, res}
		}(i)
	}
	var results [2]*Result
	var mirrored [2]bool
	var wg sync.WaitGroup
	for n := 0; n < 2; n++ {
		e := <-ends
		if !e.srcClosed {
			continue // writing to the other side failed; its own reader reports how it closed
		}
		res := e.res
		results[e.from] = &res
		other := 1 - e.from
		if results[other] == nil && !mirrored[other] {
			mirrored[other] = true
			code, reason := mirror(res)
			wg.Add(1)
			go func() {
				defer wg.Done()
				conns[other].Close(websocket.StatusCode(code), reason)
			}()
		}
	}
	wg.Wait()
	a.CloseNow()
	b.CloseNow()
	for i := range results {
		if results[i] == nil {
			results[i] = &Result{Code: 1006}
		}
	}
	return *results[0], *results[1]
}

func copyFrames(ctx context.Context, src, dst *websocket.Conn) (srcClosed bool, res Result) {
	buf := make([]byte, 64<<10)
	for {
		typ, r, err := src.Reader(ctx)
		if err != nil {
			return true, result(err)
		}
		w, err := dst.Writer(ctx, typ)
		if err != nil {
			return false, Result{Err: err}
		}
		er := &errReader{r: r}
		_, err = io.CopyBuffer(w, er, buf)
		if er.err != nil {
			w.Close()
			return true, result(er.err)
		}
		if err == nil {
			err = w.Close()
		}
		if err != nil {
			return false, Result{Err: err}
		}
	}
}

// errReader remembers a read error other than the end of the message, to tell the source failing from
// the destination failing.
type errReader struct {
	r   io.Reader
	err error
}

func (e *errReader) Read(p []byte) (int, error) {
	n, err := e.r.Read(p)
	if err != nil && !errors.Is(err, io.EOF) {
		e.err = err
	}
	return n, err
}

func result(err error) Result {
	var ce websocket.CloseError
	if errors.As(err, &ce) {
		return Result{Code: int(ce.Code), Reason: ce.Reason}
	}
	return Result{Code: 1006, Err: err}
}

// mirror is what to close the other side with: an abrupt loss (1006) becomes 1011, a close without a
// code (1005) becomes 1000, everything else passes through SendableCloseCode and CloseReason.
func mirror(r Result) (int, string) {
	if r.Code == 1006 || r.Code < 0 {
		return 1011, ""
	}
	return protocol.SendableCloseCode(r.Code), protocol.CloseReason(r.Reason)
}
