//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	facade "github.com/openabstractions/abstraction-facade/go"
	wire "github.com/openabstractions/abstraction-facade/go-core/go/abstraction/facade"
	applications "github.com/openabstractions/abstraction-facade/go/abstraction/facade/applications"
	"github.com/openabstractions/abstraction-identity/listen"
)

// The owned host's lifetime pieces: the startup state the adapter polls, the
// fixed control requests it sends, the application presence and the
// lifecycle events. The adapter (adopters/comfyui/node/oa_native_host.py)
// sends one byte per request over a private pipe:
//
//	O open          A delivered, U unavailable
//	P phase         I idle, V starting, D Comfy Desktop open with no install
//	                running, L launching, K loading custom nodes, W
//	                listening, Y already starting in Comfy Desktop, R
//	                running, B running without its browser page, F failed,
//	                E exited, U exit unknown
//	X               one frame: {"cause"}, F's cause code, "" otherwise
//	R retire        the phase; MCP mediation ended
//	C close         A once the owned ComfyUI stopped, U when unknown
//	S agent stop    A requested, P person using it, U unavailable, T uncertain
//	B <frame>       the node's page ARM acknowledgement; A or U
//	Q               one frame: {"port", "output"} of the served ComfyUI, {} when none
//
// None carries an OA request, path, command or environment.

type uncertainOwnedExit struct{ error }

func (e *uncertainOwnedExit) Unwrap() error { return e.error }

// Only fixed lifetime states cross the adopter's private control pipe. They
// convey no native authority, caller-selected operation, path or OA request.
type nativeStartup struct {
	mu         sync.Mutex
	state      byte
	ctx        context.Context
	cancel     context.CancelFunc
	done       chan struct{}
	start      func(context.Context, func()) error
	retired    bool
	userClosed bool
	result     error
	reopen     func(context.Context) error
	agentStop  func() byte
	reopening  bool
	reopenWork sync.WaitGroup
	// events records lifecycle events in OA logging; nil writes standard error only.
	events *nativeEvents
	// ended runs once the backend failed or exited on its own while the host
	// still mediates, so the host can end and the next open starts afresh.
	ended func()
	// cause is the plain cause of a failed start, one line.
	cause string
}

// startingPhase is a phase in which ComfyUI does not serve yet and open
// waits for it.
func startingPhase(state byte) bool { return strings.IndexByte("VDLKWY", state) >= 0 }

// setPhase moves a starting startup to phase. force also moves a running
// one: Comfy Desktop took the install over and the host waits for its run.
func (s *nativeStartup) setPhase(phase byte, force bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if startingPhase(s.state) || force && (s.state == 'R' || s.state == 'B') {
		s.state = phase
	}
}

// setCause records the private cause code of a failed start.
func (s *nativeStartup) setCause(cause string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(cause) > 512 {
		cause = cause[:512]
	}
	s.cause = cause
}

// browserFailed records that ComfyUI runs while its browser page did not
// open. Commands keep working; a later open retries the page.
func (s *nativeStartup) browserFailed() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state == 'R' {
		s.state = 'B'
	}
}

// desktopIdle records whether Comfy Desktop is open with no process of
// this install while the start waits: D while idle, V again once one runs.
func (s *nativeStartup) desktopIdle(idle bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch {
	case idle && s.state == 'V':
		s.state = 'D'
	case !idle && s.state == 'D':
		s.state = 'V'
	}
}

func newNativeStartup(start func(context.Context, func()) error) *nativeStartup {
	ctx, cancel := context.WithCancel(context.Background())
	return &nativeStartup{state: 'I', ctx: ctx, cancel: cancel, start: start}
}

func (s *nativeStartup) open() byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ctx.Err() != nil || s.retired {
		return 'U'
	}
	if s.done != nil {
		if startingPhase(s.state) {
			return 'A'
		}
		if s.state != 'R' && s.state != 'B' {
			return 'U'
		}
		if s.reopen != nil && !s.reopening {
			s.reopening = true
			s.reopenWork.Add(1)
			go func() {
				defer s.reopenWork.Done()
				err := s.reopen(s.ctx)
				s.mu.Lock()
				s.reopening = false
				running := s.state == 'R' || s.state == 'B'
				if running {
					s.state = 'R'
					if err != nil {
						s.state = 'B'
					}
				}
				s.mu.Unlock()
				if running && err != nil {
					s.events.log(nativeLevelWarn, "ComfyUI runs, but its browser page did not open: "+err.Error(), nil)
				}
			}()
		}
		return 'A'
	} // One retained backend, with separately coalesced browser-open requests.
	s.state, s.done = 'V', make(chan struct{})
	go func() {
		defer close(s.done)
		err := s.start(s.ctx, func() { s.mu.Lock(); s.state = 'R'; s.mu.Unlock() })
		s.mu.Lock()
		defer s.mu.Unlock()
		s.result = err
		if err != nil {
			s.state = 'F'
			var uncertain *uncertainOwnedExit
			if errors.As(err, &uncertain) {
				s.state = 'U'
			}
			fmt.Fprintln(os.Stderr, "OA Comfy startup failed:", err)
		} else {
			s.state = 'E'
		}
		if (s.state == 'F' || s.state == 'E') && s.ended != nil && !s.retired && s.ctx.Err() == nil {
			go s.ended()
		}
	}()
	return 'A' // Delivered, never a claim that verification or startup finished.
}

func (s *nativeStartup) status() byte { s.mu.Lock(); defer s.mu.Unlock(); return s.state }
func (s *nativeStartup) close() {
	s.cancel()
	s.mu.Lock()
	done := s.done
	s.mu.Unlock()
	if done != nil {
		<-done
	}
	s.reopenWork.Wait()
}

// FAC-M31: losing mediation retires future calls, while accepted startup/work
// stays with this native application until explicit shutdown or backend exit.
func (s *nativeStartup) retire() {
	s.mu.Lock()
	s.retired = true
	accepted := s.done != nil
	s.mu.Unlock()
	if !accepted {
		s.cancel()
	}
}

func (s *nativeStartup) wait(ctx context.Context) {
	s.mu.Lock()
	done := s.done
	s.mu.Unlock()
	if done == nil {
		return
	}
	select {
	case <-done:
	case <-ctx.Done():
		s.close()
	}
	s.reopenWork.Wait()
}

func nativeControl(in io.Reader, out io.Writer, startup *nativeStartup) error {
	return nativeControlWithRetire(in, out, startup, func() {})
}

func nativeControlWithRetire(in io.Reader, out io.Writer, startup *nativeStartup, retire func()) error {
	return nativeControlWithArm(in, out, startup, retire, nil)
}

func nativeControlWithArm(in io.Reader, out io.Writer, startup *nativeStartup, retire func(), acknowledge func([]byte) error) error {
	return nativeControlWith(in, out, startup, retire, acknowledge, nil, nil)
}

// attachmentLimit bounds the Q answer frame.
const attachmentLimit = 4096

// nativeControlWith answers the adapter's control requests. attachment
// gives the Q answer: the served ComfyUI's port and output folder as JSON,
// {} when none. browserPage gives the G answer: 'A' when it opened the graph
// page for an unsigned ComfyUI Desktop, 'U' when that failed, 'N' otherwise.
func nativeControlWith(in io.Reader, out io.Writer, startup *nativeStartup, retire func(), acknowledge func([]byte) error, attachment func() []byte, browserPage func() byte) error {
	var request [1]byte
	for {
		if _, err := io.ReadFull(in, request[:]); err != nil {
			return err
		}
		var response byte
		switch request[0] {
		case 'B':
			if acknowledge == nil {
				return errors.New("native bootstrap acknowledgement owner unavailable")
			}
			raw, err := listen.ReadFrameFrom(in, nativeArmAckLimit)
			if err != nil {
				return err
			}
			response = 'A'
			if acknowledge(raw) != nil {
				response = 'U'
			}
		case 'Q':
			raw := []byte("{}")
			if attachment != nil {
				raw = attachment()
			}
			if err := listen.WriteFrameTo(out, raw, attachmentLimit); err != nil {
				return err
			}
			continue
		case 'X':
			startup.mu.Lock()
			cause := ""
			if startup.state == 'F' {
				cause = startup.cause
			}
			startup.mu.Unlock()
			raw, err := json.Marshal(struct {
				Cause string `json:"cause"`
			}{cause})
			if err != nil {
				return err
			}
			if err := listen.WriteFrameTo(out, raw, attachmentLimit); err != nil {
				return err
			}
			continue
		case 'G':
			response = 'N'
			if browserPage != nil {
				response = browserPage()
			}
		case 'S':
			response = 'U'
			if startup.agentStop != nil {
				response = startup.agentStop()
			}
		case 'O':
			response = startup.open()
		case 'P':
			response = startup.status()
		case 'R':
			startup.retire()
			retire()
			response = startup.status()
		case 'C':
			// Only the authenticated page's explicit local confirmation reaches
			// this fixed operation. EOF and MCP retirement never call it.
			startup.retire()
			startup.close()
			startup.mu.Lock()
			var uncertain *uncertainOwnedExit
			if errors.As(startup.result, &uncertain) {
				startup.mu.Unlock()
				if _, err := out.Write([]byte{'U'}); err != nil {
					return err
				}
				continue
			}
			startup.userClosed = true
			startup.mu.Unlock()
			_, err := out.Write([]byte{'A'})
			return err
		default:
			return errors.New("foreign native control request")
		}
		if _, err := out.Write([]byte{response}); err != nil {
			return err
		}
		startup.mu.Lock()
		retired := startup.retired
		startup.mu.Unlock()
		if retired && (response == 'E' || response == 'F' || request[0] == 'R' && response == 'I') {
			return nil
		}
	}
}

type nativePresence struct {
	announce func(context.Context, string, wire.ApplicationPresence) (wire.ApplicationChange, error)
	withdraw func(context.Context, string, string) (wire.ApplicationChange, error)
	instance string
}

func announceNative(ctx context.Context) (*nativePresence, error) {
	binding, err := facade.Discover().ResolveService(ctx, "abstraction.facade/applications@3", facade.Requirements{Scope: facade.ScopeLocal})
	if err != nil {
		return nil, err
	}
	p := &nativePresence{
		announce: func(ctx context.Context, key string, presence wire.ApplicationPresence) (wire.ApplicationChange, error) {
			return applications.NewApplicationsClient(binding.Transport().WithContext(ctx)).Announce(key, presence)
		},
		withdraw: func(ctx context.Context, application, instance string) (wire.ApplicationChange, error) {
			return applications.NewApplicationsClient(binding.Transport().WithContext(ctx)).Withdraw(application, instance)
		},
	}
	return beginNativePresence(ctx, p)
}

func beginNativePresence(ctx context.Context, p *nativePresence) (*nativePresence, error) {
	key, err := applications.NewRequestKey()
	if err != nil {
		return nil, err
	}
	change, err := p.announce(ctx, key, wire.ApplicationPresence{Application: "comfyui", LeaseMs: nativeLeaseMs, Interfaces: []wire.ApplicationInterface{}, Contexts: []wire.ApplicationContext{}})
	if err != nil {
		return nil, err
	}
	if change.Outcome != wire.ApplicationOutcomeApplied || change.Instance == "" {
		return nil, errors.New("native Comfy announcement unavailable")
	}
	p.instance = change.Instance
	return p, nil
}

const nativeLeaseMs = 30000

// keep renews the instance lease. An explicit refusal, a replaced instance or
// an unreachable runtime past the lease lapses it; earlier transient errors
// retry on the next tick while the lease still stands.
func (p *nativePresence) keep(ctx context.Context, every time.Duration, lapse func()) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	renewed := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			// Generated clients retain the resolver transport; each call is bounded.
			callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
			current, refused := p.renew(callCtx)
			cancel()
			if ctx.Err() != nil {
				return
			}
			if current {
				renewed = time.Now()
				continue
			}
			if refused || time.Since(renewed) >= nativeLeaseMs*time.Millisecond {
				lapse()
				return
			}
		}
	}
}

// renew reports whether the lease is current and, when it is not, whether the
// runtime answered with a refusal rather than failing to answer.
func (p *nativePresence) renew(ctx context.Context) (current, refused bool) {
	key, err := applications.NewRequestKey()
	if err != nil {
		return false, false
	}
	change, err := p.announce(ctx, key, wire.ApplicationPresence{Application: "comfyui", Instance: p.instance, LeaseMs: nativeLeaseMs, Interfaces: []wire.ApplicationInterface{}, Contexts: []wire.ApplicationContext{}})
	if err != nil {
		return false, false
	}
	current = change.Outcome == wire.ApplicationOutcomeApplied && change.Instance == p.instance
	return current, !current && change.Outcome != wire.ApplicationOutcomeUnavailable
}

// endLapsedHost ends an owned host whose lease lapsed or whose backend ended:
// the backend stops and is awaited, then the adapter stops, and the next open
// starts fresh.
func endLapsedHost(retire func(), startup *nativeStartup, stopAdapter func()) {
	retire()
	startup.close()
	stopAdapter()
}

func finishNativeMediation(ctx context.Context, startup *nativeStartup, waitErr error, release func() error) error {
	startup.retire()
	// Adapter failure or a control-hold cleanup error cannot discard accepted
	// backend ownership. Only explicit application shutdown cancels that work.
	startup.wait(ctx)
	startup.mu.Lock()
	var uncertain *uncertainOwnedExit
	unknown := errors.As(startup.result, &uncertain)
	startup.mu.Unlock()
	if unknown {
		return uncertain
	}
	if waitErr != nil {
		return waitErr
	} // Retain control holds on uncertain adapter exit.
	return release()
}

// nativeOutputTail carries the backend's standard output and error to this
// host's standard error and keeps their last bytes, for a failed start to
// name what ComfyUI or its OA launcher printed.
type nativeOutputTail struct {
	read, write *os.File
	mu          sync.Mutex
	last        []byte
	done        chan struct{}
}

const nativeOutputTailLimit = 2048

func newNativeOutputTail(forward io.Writer) (*nativeOutputTail, error) {
	r, w, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	t := &nativeOutputTail{read: r, write: w, done: make(chan struct{})}
	go func() {
		defer close(t.done)
		buffer := make([]byte, 32<<10)
		for {
			n, err := r.Read(buffer)
			if n > 0 {
				_, _ = forward.Write(buffer[:n])
				t.mu.Lock()
				t.last = append(t.last, buffer[:n]...)
				if len(t.last) > nativeOutputTailLimit {
					t.last = append([]byte(nil), t.last[len(t.last)-nativeOutputTailLimit:]...)
				}
				t.mu.Unlock()
			}
			if err != nil {
				return
			}
		}
	}()
	return t, nil
}

// closeWrite closes this host's copy of the write end once the backend holds
// its own, so the reader ends when the backend tree does.
func (t *nativeOutputTail) closeWrite() { _ = t.write.Close() }

// text is the kept output as whole lines of valid UTF-8, read once the
// backend's output ended or a second passed.
func (t *nativeOutputTail) text() string {
	t.closeWrite()
	select {
	case <-t.done:
	case <-time.After(time.Second):
	}
	t.mu.Lock()
	raw := string(t.last)
	full := len(t.last) == nativeOutputTailLimit
	t.mu.Unlock()
	if full {
		if i := strings.IndexByte(raw, '\n'); i >= 0 {
			raw = raw[i+1:]
		}
	}
	return strings.TrimSpace(strings.ToValidUTF8(strings.ReplaceAll(raw, "\r", ""), ""))
}

func (t *nativeOutputTail) close() {
	t.closeWrite()
	_ = t.read.Close()
}

// The OA logging levels (LOG-R3, the slog scale).
const (
	nativeLevelInfo  int64 = 0
	nativeLevelWarn  int64 = 4
	nativeLevelError int64 = 8
)

// nativeEvents records the host's lifecycle events. The runtime discards an
// owned host's standard error, so each event is also an OA logging record of
// this application, read with `openabstractions logs --app comfyui`.
type nativeEvents struct {
	stderr io.Writer
	write  func(ctx context.Context, level int64, message string, attrs map[string]string) error
}

func newNativeEvents() *nativeEvents {
	var mu sync.Mutex
	var sink interface {
		LogContext(context.Context, int64, string, map[string]string) error
	}
	return &nativeEvents{stderr: os.Stderr, write: func(ctx context.Context, level int64, message string, attrs map[string]string) error {
		mu.Lock()
		defer mu.Unlock()
		if sink == nil {
			client, err := facade.Discover().ResolveLog(ctx, facade.Requirements{Scope: facade.ScopeLocal})
			if err != nil {
				return err
			}
			sink = client
		}
		if err := sink.LogContext(ctx, level, message, attrs); err != nil {
			sink = nil
			return err
		}
		return nil
	}}
}

// log writes one event to standard error and to OA logging. A failed OA
// logging write becomes one more standard error line.
func (e *nativeEvents) log(level int64, message string, attrs map[string]string) {
	if e == nil {
		fmt.Fprintln(os.Stderr, "OA Comfy:", message)
		return
	}
	fmt.Fprintln(e.stderr, "OA Comfy:", message)
	if e.write == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := e.write(ctx, level, message, attrs); err != nil {
		fmt.Fprintln(e.stderr, "OA Comfy: OA logging is unavailable:", err)
	}
}
