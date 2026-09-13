//go:build windows

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	wire "github.com/openabstractions/abstraction-facade/go-core/go/abstraction/facade"
	applications "github.com/openabstractions/abstraction-facade/go/abstraction/facade/applications"
	"github.com/openabstractions/abstraction-identity/fileid"
	"github.com/openabstractions/abstraction-identity/listen"
)

// The lifetime tests the connector keeps from the OA-build host
// (adopters/comfyui/installer/backendlaunch): registration, the relay's
// cancellation ownership, presence, the startup state, the control requests
// and the page bootstrap.

func TestNativeSelfRegistrationCreateOnlyAndTypedResults(t *testing.T) {
	for _, outcome := range []applications.Outcome{applications.OutcomeApplied, applications.OutcomeConflict, applications.OutcomeForbidden, applications.OutcomeUnavailable, applications.OutcomeInvalid} {
		t.Run(outcome.String(), func(t *testing.T) {
			var out bytes.Buffer
			calls := 0
			ctx := context.WithValue(context.Background(), struct{ self bool }{true}, "retained")
			err := registerSelfWith(ctx, &out, func(got context.Context, request applications.RegisterRequest) (applications.Change, error) {
				calls++
				if got != ctx || request.ExpectedRevision != "" {
					t.Fatal("context changed or replacement requested")
				}
				self, err := os.Executable()
				if err != nil {
					t.Fatal(err)
				}
				if same, err := fileid.SameFile(self, request.Program); err != nil || !same {
					t.Fatal("request did not select this actual image", err)
				}
				return applications.Change{Outcome: outcome, Revision: "service-revision", Reason: "service-reason"}, nil
			})
			if calls != 1 || (err == nil) != (outcome == applications.OutcomeApplied) {
				t.Fatalf("calls=%d error=%v", calls, err)
			}
			var result map[string]string
			if err := json.Unmarshal(out.Bytes(), &result); err != nil {
				t.Fatal(err)
			}
			if result["outcome"] != outcome.String() || result["reason"] != "service-reason" || result["revision"] != "service-revision" {
				t.Fatal(result)
			}
			if _, exists := result["ready"]; exists {
				t.Fatal("registration claimed readiness")
			}
		})
	}
}

func TestNativeSelfRegistrationTransportFailureDoesNotReplay(t *testing.T) {
	var out bytes.Buffer
	calls := 0
	want := errors.New("transport ambiguous")
	err := registerSelfWith(context.Background(), &out, func(context.Context, applications.RegisterRequest) (applications.Change, error) {
		calls++
		return applications.Change{}, want
	})
	if !errors.Is(err, want) || calls != 1 || out.Len() != 0 {
		t.Fatalf("calls=%d output=%s error=%v", calls, out.String(), err)
	}
}

// Windows callbacks carry the pinned ABI output pointer as uintptr.
//
//go:nocheckptr
func relayTestSetOutput(out, value uintptr) { *(*uintptr)(nativeMemory(out)) = value }

func TestNativeRelayCancellationOwnership(t *testing.T) {
	created := uintptr(0)
	released := map[uintptr]int{}
	signaled := map[uintptr]int{}
	closed := map[uintptr]int{}
	selectionEntered, selectionCancelled := make(chan struct{}), make(chan struct{})
	var selectionToken uintptr
	var cancelSelection sync.Once
	defer cancelSelection.Do(func() { close(selectionCancelled) })
	r := &relayNative{procs: map[string]uintptr{}, tokens: map[uintptr]*relayObject{}, signals: map[int64]*relayObject{}, handles: map[int64]*relayObject{}}
	r.procs["cancellation_create"] = syscall.NewCallback(func(out uintptr) uintptr { created++; relayTestSetOutput(out, created); return 0 })
	r.procs["cancellation_release"] = syscall.NewCallback(func(value uintptr) uintptr { released[value]++; return 0 })
	r.procs["cancellation_signal"] = syscall.NewCallback(func(value uintptr) uintptr {
		signaled[value]++
		if value == selectionToken {
			cancelSelection.Do(func() { close(selectionCancelled) })
		}
		return 0
	})
	r.procs["close"] = syscall.NewCallback(func(value uintptr) uintptr { closed[value]++; return 0 })
	r.procs["select_runtime"] = syscall.NewCallback(func(millis, token, out uintptr) uintptr {
		selectionToken = token
		close(selectionEntered)
		<-selectionCancelled
		return 7
	})
	request := func(value map[string]any) map[string]json.RawMessage {
		raw, _ := json.Marshal(value)
		var result map[string]json.RawMessage
		json.Unmarshal(raw, &result)
		return result
	}
	createdReply, err := r.answer(request(map[string]any{"op": "cancellation_create"}))
	if err != nil {
		t.Fatal(err)
	}
	id := createdReply["signal"].(int64)
	parent := r.signals[id]
	one, finishOne, err := r.borrowStreamSignal(request(map[string]any{"signal": id}))
	if err != nil {
		t.Fatal(err)
	}
	oneID, err := r.remember(101, false, one)
	if err != nil {
		t.Fatal(err)
	}
	finishOne()
	two, finishTwo, err := r.borrowStreamSignal(request(map[string]any{"signal": id}))
	if err != nil {
		t.Fatal(err)
	}
	_, err = r.remember(102, false, two)
	if err != nil {
		t.Fatal(err)
	}
	finishTwo()
	if _, err = r.answer(request(map[string]any{"op": "cancellation_release", "signal": id})); err != nil {
		t.Fatal(err)
	}
	if released[parent.value] != 0 {
		t.Fatal("caller release freed a token retained by streams")
	}
	if _, err = r.answer(request(map[string]any{"op": "close", "handle": oneID})); err != nil {
		t.Fatal(err)
	}
	if signaled[parent.value] != 0 || signaled[two.value] != 0 {
		t.Fatal("closing one stream cancelled its siblings")
	}
	nilSignal, finishNil, err := r.borrowSignal(request(map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	selectionResult := make(chan map[string]any, 1)
	go func() {
		reply, err := r.answer(request(map[string]any{"op": "select_runtime", "millis": 30000}))
		if err != nil {
			reply = map[string]any{"error": err}
		}
		selectionResult <- reply
	}()
	select {
	case <-selectionEntered:
	case <-time.After(5 * time.Second):
		t.Fatal("nil-signal native selection did not enter")
	}
	r.signalAll()
	select {
	case reply := <-selectionResult:
		if reply["status"] != uintptr(7) {
			t.Fatalf("selection cancellation: %v", reply)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("EOF did not join nil-signal native selection")
	}
	if signaled[two.value] == 0 || signaled[nilSignal.value] == 0 {
		t.Fatal("EOF omitted a retained or nil-signal operation")
	}
	finishNil()
	r.closeAll()
	if len(r.tokens) != 0 {
		t.Fatal("token custody survived complete close")
	}
	for value := uintptr(1); value <= created; value++ {
		if released[value] != 1 {
			t.Fatalf("token %d released %d times", value, released[value])
		}
	}
	if closed[101] != 1 || closed[102] != 1 {
		t.Fatalf("stream close ownership: %v", closed)
	}
}

func TestNativePresenceInitialAndRenewalUseSDKKeys(t *testing.T) {
	keys := map[string]bool{}
	calls := 0
	p := &nativePresence{announce: func(_ context.Context, key string, presence wire.ApplicationPresence) (wire.ApplicationChange, error) {
		if !strings.HasPrefix(key, "a3.") || keys[key] {
			t.Fatal("foreign or reused request key")
		}
		keys[key] = true
		want := ""
		if calls > 0 {
			want = "service-issued"
		}
		if presence.Instance != want {
			t.Fatal("wrong announcement instance")
		}
		calls++
		return wire.ApplicationChange{Outcome: wire.ApplicationOutcomeApplied, Instance: "service-issued"}, nil
	}}
	actual, err := beginNativePresence(context.Background(), p)
	if current, _ := p.renew(context.Background()); err != nil || actual != p || !current || calls != 2 {
		t.Fatalf("presence %v %d", err, calls)
	}
}

func TestNativePresenceRefusalEndsBackendAndAdapter(t *testing.T) {
	p := &nativePresence{instance: "i", announce: func(context.Context, string, wire.ApplicationPresence) (wire.ApplicationChange, error) {
		return wire.ApplicationChange{Outcome: wire.ApplicationOutcomeStale}, nil
	}}
	backendStopped := make(chan struct{})
	startup := newNativeStartup(func(ctx context.Context, started func()) error {
		started()
		<-ctx.Done()
		close(backendStopped)
		return nil
	})
	if startup.open() != 'A' {
		t.Fatal("backend not started")
	}
	lifetime, retire := context.WithCancel(context.Background())
	defer retire()
	adapter, stopAdapter := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		p.keep(lifetime, time.Millisecond, func() { endLapsedHost(retire, startup, stopAdapter) })
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("keep did not return after refusal")
	}
	select {
	case <-backendStopped:
	default:
		t.Fatal("backend still running")
	}
	if adapter.Err() == nil || lifetime.Err() == nil || startup.open() != 'U' {
		t.Fatal("adapter or host still live")
	}
}

func TestNativePresenceTransientErrorKeepsHost(t *testing.T) {
	calls := 0
	p := &nativePresence{instance: "i", announce: func(context.Context, string, wire.ApplicationPresence) (wire.ApplicationChange, error) {
		calls++
		if calls == 1 {
			return wire.ApplicationChange{}, context.DeadlineExceeded
		}
		return wire.ApplicationChange{Outcome: wire.ApplicationOutcomeApplied, Instance: "i"}, nil
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	lapsed := false
	p.keep(ctx, time.Millisecond, func() { lapsed = true })
	if lapsed || calls < 2 {
		t.Fatalf("transient error lapsed the lease: %v %d", lapsed, calls)
	}
}

func TestNativeStartupDefersHeavyVerificationAndCoalesces(t *testing.T) {
	var calls atomic.Int32
	entered, exited := make(chan struct{}), make(chan struct{})
	s := newNativeStartup(func(ctx context.Context, started func()) error {
		calls.Add(1)
		close(entered)
		// This boundary represents full backend verification, before exec.
		<-ctx.Done()
		close(exited)
		return ctx.Err()
	})
	if s.status() != 'I' || calls.Load() != 0 {
		t.Fatal("control initialization touched backend")
	}
	if s.open() != 'A' {
		t.Fatal("startup delivery refused")
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("startup not retained")
	}
	for i := 0; i < 10; i++ {
		if s.open() != 'A' {
			t.Fatal("duplicate delivery")
		}
	}
	if calls.Load() != 1 || s.status() != 'V' {
		t.Fatal("duplicate startup or premature running")
	}
	s.retire()
	select {
	case <-exited:
		t.Fatal("EOF canceled accepted verification")
	default:
	}
	if s.open() != 'U' {
		t.Fatal("retired mediation accepted more work")
	}
	s.close()
	select {
	case <-exited:
	default:
		t.Fatal("explicit shutdown did not join verification")
	}
	if s.open() != 'U' {
		t.Fatal("retired host restarted")
	}
}

func TestNativeRepeatedOpenRenewsPageWithoutRestartingBackend(t *testing.T) {
	var starts, pages atomic.Int32
	running, opening, release := make(chan struct{}), make(chan struct{}), make(chan struct{})
	s := newNativeStartup(func(ctx context.Context, started func()) error {
		starts.Add(1)
		started()
		close(running)
		<-ctx.Done()
		return ctx.Err()
	})
	s.reopen = func(ctx context.Context) error {
		pages.Add(1)
		close(opening)
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if s.open() != 'A' {
		t.Fatal("first open")
	}
	<-running
	if s.open() != 'A' {
		t.Fatal("page reopen")
	}
	<-opening
	for i := 0; i < 10; i++ {
		if s.open() != 'A' {
			t.Fatal("coalesced reopen")
		}
	}
	if starts.Load() != 1 || pages.Load() != 1 {
		t.Fatal("reopen restarted owned work")
	}
	close(release)
	s.retire()
	s.close()
}

func TestNativePageBootstrapIsFreshPrivateAndOwned(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 2; i++ {
		var framed bytes.Buffer
		checks, shown := 0, ""
		err := openNativePage(context.Background(), "generation_123456", 8189, func(_ context.Context, raw []byte) error { return listen.WriteFrameTo(&framed, raw, nativeGraphLimit) }, func() error { checks++; return nil }, func(target string) error { shown = target; return nil })
		if err != nil || checks != 2 {
			t.Fatal("owned listener fence", err)
		}
		raw, err := listen.ReadFrameFrom(&framed, nativeGraphLimit)
		if err != nil {
			t.Fatal(err)
		}
		var arm struct{ Type, Generation, Nonce, Origin string }
		if err = json.Unmarshal(raw, &arm); err != nil || arm.Type != "arm" || arm.Generation != "generation_123456" || arm.Origin != "http://127.0.0.1:8189" || len(arm.Nonce) != 32 || seen[arm.Nonce] {
			t.Fatal("bootstrap shape/freshness", err)
		}
		seen[arm.Nonce] = true
		if shown != "http://127.0.0.1:8189/oa/appmodel/bootstrap#generation="+arm.Generation+"&nonce="+arm.Nonce {
			t.Fatal("fragment URL differs from private arm")
		}
	}
	var framed bytes.Buffer
	if err := openNativePage(context.Background(), "generation_123456", 8189, func(_ context.Context, raw []byte) error { return listen.WriteFrameTo(&framed, raw, nativeGraphLimit) }, func() error { return errors.New("foreign listener") }, func(string) error { t.Fatal("opened foreign listener"); return nil }); err == nil || framed.Len() != 0 {
		t.Fatal("foreign listener received bootstrap")
	}
}

// nativeArmFixture opens a page while the test holds the node's end of the
// graph channel.
func nativeArmFixture(t *testing.T, ctx context.Context, b *nativeGraphBridge) (map[string]string, <-chan error, *atomic.Bool) {
	t.Helper()
	node, connector := net.Pipe()
	t.Cleanup(func() { _ = node.Close() })
	b.adopt(connector)
	shown := &atomic.Bool{}
	result := make(chan error, 1)
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		result <- openNativePage(ctx, b.generation, 8188, b.arm, func() error { return nil }, func(string) error { shown.Store(true); return nil })
	}()
	t.Cleanup(func() {
		b.close()
		select {
		case <-finished:
		case <-time.After(time.Second):
			t.Error("owned ARM fixture did not join")
		}
	})
	raw, err := listen.ReadFrameFrom(node, nativeGraphLimit)
	if err != nil {
		t.Fatal(err)
	}
	var arm map[string]string
	if err = json.Unmarshal(raw, &arm); err != nil {
		t.Fatal(err)
	}
	arm["type"] = "arm_ack"
	delete(arm, "origin")
	return arm, result, shown
}

func TestNativeBrowserWaitsForExactAppliedArmAcknowledgement(t *testing.T) {
	b, err := newNativeGraphBridge()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	ack, result, shown := nativeArmFixture(t, ctx, b)
	// The node reader has received bytes, but has not installed its ARM yet.
	select {
	case <-result:
		t.Fatal("browser proceeded on buffered ARM")
	case <-time.After(10 * time.Millisecond):
	}
	if shown.Load() {
		t.Fatal("GET could precede node ARM installation")
	}
	raw, _ := json.Marshal(ack)
	// This is emitted by the node only after its atomic installation succeeds.
	if err = b.acknowledgeArm(raw); err != nil {
		t.Fatal(err)
	}
	if err = <-result; err != nil || !shown.Load() {
		t.Fatal("applied ARM did not release browser", err)
	}
	if b.acknowledgeArm(raw) == nil {
		t.Fatal("duplicate acknowledgement retained")
	}
}

func TestNativeForeignArmAcknowledgementsNeverLaunchBrowser(t *testing.T) {
	for _, kind := range []string{"generation", "nonce", "type", "extra", "duplicate", "oversize"} {
		t.Run(kind, func(t *testing.T) {
			b, err := newNativeGraphBridge()
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			ack, result, shown := nativeArmFixture(t, ctx, b)
			switch kind {
			case "generation":
				ack["generation"] = "foreign_generation"
			case "nonce":
				first := "x"
				if ack["nonce"][:1] == first {
					first = "y"
				}
				ack["nonce"] = first + ack["nonce"][1:]
			case "type":
				ack["type"] = "foreign"
			case "extra":
				ack["extra"] = "x"
			}
			raw, _ := json.Marshal(ack)
			if kind == "duplicate" {
				raw = []byte(`{"type":"arm_ack","generation":"x","generation":"x","nonce":"x"}`)
			}
			if kind == "oversize" {
				raw = append(raw, bytes.Repeat([]byte(" "), nativeArmAckLimit)...)
			}
			if b.acknowledgeArm(raw) == nil {
				t.Fatal("foreign ACK accepted")
			}
			if err = <-result; err == nil || shown.Load() {
				t.Fatal("foreign ACK launched browser", err)
			}
		})
	}
}

func TestNativeMissingAndLateArmAcknowledgementsAreNotReused(t *testing.T) {
	b, err := newNativeGraphBridge()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	old, result, shown := nativeArmFixture(t, ctx, b)
	if err = <-result; err == nil || shown.Load() {
		t.Fatal("missing ACK launched browser")
	}
	if b.current() == nil {
		t.Fatal("ACK loss closed the healthy old-page graph channel")
	}
	raw, _ := json.Marshal(old)
	if b.acknowledgeArm(raw) == nil {
		t.Fatal("late ACK cached")
	}
	nextCtx, nextCancel := context.WithTimeout(context.Background(), time.Second)
	defer nextCancel()
	next, nextResult, nextShown := nativeArmFixture(t, nextCtx, b)
	if old["nonce"] == next["nonce"] {
		t.Fatal("nonce reused")
	}
	if b.acknowledgeArm(raw) == nil {
		t.Fatal("stale ACK satisfied new arm")
	}
	if err = <-nextResult; err == nil || nextShown.Load() {
		t.Fatal("stale ACK launched next browser")
	}
}

func TestNativeArmAcknowledgementUsesOnlyBoundedFixedControl(t *testing.T) {
	s := newNativeStartup(func(context.Context, func()) error { t.Fatal("unexpected startup"); return nil })
	defer s.close()
	var input, output bytes.Buffer
	input.WriteByte('B')
	raw := []byte(`{"type":"arm_ack","generation":"generation_123456","nonce":"nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn"}`)
	if err := listen.WriteFrameTo(&input, raw, nativeArmAckLimit); err != nil {
		t.Fatal(err)
	}
	called := 0
	err := nativeControlWithArm(&input, &output, s, func() {}, func(actual []byte) error {
		called++
		if !bytes.Equal(actual, raw) {
			t.Fatal("ACK changed")
		}
		return nil
	})
	if err != io.EOF || called != 1 || output.String() != "A" {
		t.Fatal("fixed ACK relay", err, output.String())
	}
}

func TestNativePartialArmFrameEOFUnblocksWithoutCancellingAcceptedStartup(t *testing.T) {
	for _, raw := range [][]byte{{'B'}, {'B', 0, 0}, {'B', 0, 0, 0, 5, '{'}} {
		t.Run(fmt.Sprint(len(raw)), func(t *testing.T) {
			started := make(chan struct{})
			s := newNativeStartup(func(ctx context.Context, ready func()) error {
				close(started)
				<-ctx.Done()
				return ctx.Err()
			})
			defer s.close()
			if s.open() != 'A' {
				t.Fatal("open refused")
			}
			<-started
			done := make(chan error, 1)
			go func() {
				done <- nativeControlWithArm(bytes.NewReader(raw), io.Discard, s, func() {}, func([]byte) error { t.Error("partial ACK dispatched"); return nil })
			}()
			select {
			case err := <-done:
				if err == nil {
					t.Fatal("partial frame accepted")
				}
			case <-time.After(time.Second):
				t.Fatal("partial frame did not unblock on EOF")
			}
			select {
			case <-s.ctx.Done():
				t.Fatal("EOF cancelled accepted startup")
			default:
			}
		})
	}
}

func TestNativeExplicitCloseJoinsAcceptedWorkBeforeAcknowledgement(t *testing.T) {
	started, joined := make(chan struct{}), make(chan struct{})
	s := newNativeStartup(func(ctx context.Context, ready func()) error {
		ready()
		close(started)
		<-ctx.Done()
		close(joined)
		return ctx.Err()
	})
	if s.open() != 'A' {
		t.Fatal("open")
	}
	<-started
	var output bytes.Buffer
	if err := nativeControl(bytes.NewReader([]byte{'C'}), &output, s); err != nil || output.String() != "A" {
		t.Fatal("close acknowledgement", err)
	}
	select {
	case <-joined:
	default:
		t.Fatal("acknowledged before positive join")
	}
	if s.open() != 'U' || !s.userClosed {
		t.Fatal("closed app accepted work")
	}
}

func TestNativeMediationRetirementKeepsLocalCloseRoute(t *testing.T) {
	started, joined := make(chan struct{}), make(chan struct{})
	s := newNativeStartup(func(ctx context.Context, ready func()) error {
		ready()
		close(started)
		<-ctx.Done()
		close(joined)
		return ctx.Err()
	})
	if s.open() != 'A' {
		t.Fatal("open")
	}
	<-started
	retired := 0
	var output bytes.Buffer
	// R withdraws mediation; the same retained application control still accepts C.
	if err := nativeControlWithRetire(bytes.NewReader([]byte{'R', 'P', 'C'}), &output, s, func() {
		retired++
		select {
		case <-joined:
			t.Fatal("retirement canceled backend")
		default:
		}
	}); err != nil {
		t.Fatal(err)
	}
	if retired != 1 || output.String() != "RRA" {
		t.Fatal("retained close route", output.String())
	}
	select {
	case <-joined:
	default:
		t.Fatal("local close did not join")
	}
}

func TestNativeUnknownJoinNeverAcknowledgesCloseOrReleasesControlHold(t *testing.T) {
	s := newNativeStartup(func(context.Context, func()) error {
		return &uncertainOwnedExit{errors.New("owned fixture wait unknown")}
	})
	if s.open() != 'A' {
		t.Fatal("delivery")
	}
	<-s.done
	var output bytes.Buffer
	if err := nativeControl(bytes.NewReader([]byte{'C'}), &output, s); err != io.EOF || output.String() != "U" || s.userClosed {
		t.Fatal("unknown exit acknowledged", err, output.String())
	}
	released := false
	if err := finishNativeMediation(context.Background(), s, nil, func() error { released = true; return nil }); err == nil || released {
		t.Fatal("uncertain ownership released")
	}
}

func TestNativeStartupFailureRemainsReadableUntilMediationRetires(t *testing.T) {
	s := newNativeStartup(func(context.Context, func()) error { return errors.New("inert startup refusal") })
	if s.open() != 'A' {
		t.Fatal("delivery")
	}
	<-s.done
	var output bytes.Buffer
	retired := 0
	if err := nativeControlWithRetire(bytes.NewReader([]byte{'P', 'P', 'R'}), &output, s, func() { retired++ }); err != nil || retired != 1 || output.String() != "FFF" {
		t.Fatal("startup observation ended before mediation", err, output.String())
	}
}

func TestNativeStartupFailureAndExitRemainObservable(t *testing.T) {
	for _, fail := range []bool{false, true} {
		s := newNativeStartup(func(ctx context.Context, started func()) error {
			started()
			if fail {
				return errors.New("fixture failure")
			}
			return nil
		})
		if s.open() != 'A' {
			t.Fatal("delivery")
		}
		<-s.done
		want := byte('E')
		if fail {
			want = 'F'
		}
		if s.status() != want {
			t.Fatal("startup outcome lost")
		}
		previous := s.done
		if s.open() != 'U' || s.done != previous {
			t.Fatal("completed startup silently retried")
		}
		s.close()
	}
}

func TestNativeControlClosedOperations(t *testing.T) {
	s := newNativeStartup(func(context.Context, func()) error { t.Fatal("unexpected backend start"); return nil })
	defer s.close()
	var output bytes.Buffer
	if err := nativeControl(bytes.NewReader([]byte{'P', 'Z'}), &output, s); err == nil {
		t.Fatal("foreign control operation accepted")
	}
	if output.String() != "I" {
		t.Fatalf("unexpected control output %q", output.String())
	}
	if err := nativeControl(bytes.NewReader(nil), io.Discard, s); err != io.EOF {
		t.Fatal(err)
	}
}

func TestNativeMediationFailureRetainsAcceptedLifetime(t *testing.T) {
	finish := make(chan struct{})
	s := newNativeStartup(func(context.Context, func()) error { <-finish; return nil })
	if s.open() != 'A' {
		t.Fatal("delivery")
	}
	waitError := errors.New("fixture uncertain adapter exit")
	returned := make(chan error, 1)
	go func() {
		returned <- finishNativeMediation(context.Background(), s, waitError, func() error { t.Error("uncertain control hold released"); return nil })
	}()
	select {
	case <-returned:
		t.Fatal("accepted backend owner discarded")
	case <-time.After(10 * time.Millisecond):
	}
	close(finish)
	if err := <-returned; !errors.Is(err, waitError) {
		t.Fatal(err)
	}
	s.close()
}

func TestNativeBackendEndEndsTheHostOnce(t *testing.T) {
	for _, tc := range []struct {
		name   string
		retire bool
		err    error
		ends   bool
	}{
		{"failed", false, errors.New("backend exited with code 3"), true},
		{"exited", false, nil, true},
		{"retired", true, errors.New("backend exited with code 3"), false},
		{"uncertain", false, &uncertainOwnedExit{errors.New("wait unknown")}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			release := make(chan struct{})
			s := newNativeStartup(func(context.Context, func()) error { <-release; return tc.err })
			var ended atomic.Int32
			s.ended = func() { ended.Add(1) }
			if s.open() != 'A' {
				t.Fatal("delivery")
			}
			if tc.retire {
				s.retire()
			}
			close(release)
			<-s.done
			time.Sleep(50 * time.Millisecond)
			if got := ended.Load(); (got == 1) != tc.ends || got > 1 {
				t.Fatalf("ended %d times, want ends=%v", got, tc.ends)
			}
		})
	}
}

func TestNativeBrowserFailureKeepsTheBackendRunning(t *testing.T) {
	release := make(chan struct{})
	s := newNativeStartup(func(ctx context.Context, started func()) error {
		started()
		<-release
		return nil
	})
	if s.open() != 'A' {
		t.Fatal("delivery")
	}
	for s.status() != 'R' {
		time.Sleep(time.Millisecond)
	}
	s.browserFailed()
	if s.status() != 'B' {
		t.Fatal("browser failure not recorded")
	}
	close(release)
	<-s.done
	s.browserFailed()
	if s.status() != 'E' {
		t.Fatal("a browser failure rewrote an exited backend")
	}
}
