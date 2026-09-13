package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	identity "github.com/openabstractions/abstraction-identity"
	"github.com/openabstractions/abstraction-identity/listen"
	res "github.com/openabstractions/abstraction-resource/go/client"
	"github.com/openabstractions/abstraction-resource/go/instrument"
	"github.com/openabstractions/abstraction-resource/go/service"
)

const cardGiB = int64(1) << 30

var cardEndpointSequence atomic.Uint64

// cardStage is a served lease book on a 48 GiB card with a fixed instrument,
// the connector's card broker on it through the resource client, as the
// native-relay runs it, and the pack's side of the relay lines.
type cardStage struct {
	t      *testing.T
	in     *instrument.Fixed
	book   *service.Book
	broker *cardBroker
	ctx    context.Context
	audit  string
}

// comfyPython is where ComfyUI's weights live: its own Python process,
// which the connector did not start (gap G1: the bytes stay unbound).
var comfyPython = instrument.Sample{PID: 41, Program: `C:\ComfyUI\python_embeded\python.exe`, Account: "S-1-5-21-7-1001"}

func newCardStage(t *testing.T) *cardStage {
	t.Helper()
	previous := cardHolderWait
	cardHolderWait = 6 * time.Second
	t.Cleanup(func() { cardHolderWait = previous })
	in := instrument.NewFixed()
	in.Set(instrument.Card0, instrument.FixedResource{Capacity: 48 * cardGiB})
	table := service.NewTable(in, nil, time.Nanosecond)
	permit := func(context.Context, *identity.Peer, string, string) error { return nil }
	auditPath := filepath.Join(t.TempDir(), "audit.jsonl")
	audit, err := service.OpenAudit(auditPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = audit.Close() })
	book, err := service.OpenBook(service.BookOptions{Table: table, Hold: permit, Settle: 2 * time.Second, Audit: audit})
	if err != nil {
		t.Fatal(err)
	}
	endpoint := listen.Endpoint(fmt.Sprintf("comfy-card-test-%d-%d", os.Getpid(), cardEndpointSequence.Add(1)))
	if err := listen.CanEver(endpoint, service.Bound); err != nil {
		t.Skip("this platform cannot bind callers here: ", err)
	}
	host, err := service.Listen(endpoint, table)
	if err != nil {
		t.Fatal(err)
	}
	if err := host.EnablePolicy(permit); err != nil {
		t.Fatal(err)
	}
	if err := host.EnableLeases(book); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	served := make(chan error, 1)
	go func() { served <- host.Serve(ctx) }()
	cards := newRelayCards(ctx)
	cards.dial = func() holderLeases { return res.New(endpoint) }
	s := &cardStage{t: t, in: in, book: book, ctx: ctx, audit: auditPath}
	s.line(cards, "prompt_end", nil) // starts the broker, as the pack's first line does
	s.broker = cards.broker
	t.Cleanup(func() {
		cards.close()
		cancel()
		host.Close()
		<-served
	})
	return s
}

func (s *cardStage) line(cards *relayCards, op string, fields map[string]any) map[string]any {
	s.t.Helper()
	request := map[string]json.RawMessage{}
	for name, value := range fields {
		raw, _ := json.Marshal(value)
		request[name] = raw
	}
	raw, _ := json.Marshal(op)
	request["op"] = raw
	reply, err := cards.answer(s.ctx, request, func(map[string]json.RawMessage) (map[string]any, error) {
		return nil, fmt.Errorf("%s reached the IPC relay", op)
	})
	if err != nil {
		s.t.Fatalf("%s: %v", op, err)
	}
	return reply
}

func (s *cardStage) call(op string, fields map[string]any) map[string]any {
	s.t.Helper()
	request := map[string]json.RawMessage{}
	for name, value := range fields {
		raw, _ := json.Marshal(value)
		request[name] = raw
	}
	reply, known, err := s.broker.handle(s.ctx, op, request)
	if !known || err != nil {
		s.t.Fatalf("%s: known %v, %v", op, known, err)
	}
	return reply
}

// loadWan is ComfyUI loading Wan 2.2 under a lease: the pack asks, the
// grant comes back, and the instrument then sees ComfyUI's Python hold it.
func (s *cardStage) loadWan() {
	s.t.Helper()
	reply := s.call("card_load", map[string]any{"name": "m1", "title": "Wan 2.2", "bytes": 18 * cardGiB, "using": []string{"m1"}})
	if reply["granted"] != true {
		s.t.Fatalf("Wan 2.2's lease read %v", reply)
	}
	held := comfyPython
	held.Amount = 18 * cardGiB
	s.in.Set(instrument.Card0, instrument.FixedResource{Capacity: 48 * cardGiB, Samples: []instrument.Sample{held}})
	s.call("card_loaded", map[string]any{"name": "m1", "bytes": 18 * cardGiB})
}

// pack is the node pack's free loop: it frees each model asked, between
// prompts, and reports it; the instrument then sees the bytes leave.
func (s *cardStage) pack(freed chan<- time.Time, told chan<- string) {
	for s.ctx.Err() == nil {
		reply := s.call("card_free_wait", map[string]any{"ms": 500})
		for _, sentence := range reply["told"].([]string) {
			told <- sentence
		}
		for _, request := range reply["free"].([]cardFree) {
			s.in.Set(instrument.Card0, instrument.FixedResource{Capacity: 48 * cardGiB})
			freed <- time.Now()
			s.call("card_freed", map[string]any{"request": request.Request, "ok": true})
		}
	}
}

// An OA engine that needs memory ComfyUI's idle Wan 2.2 holds gets it: the
// service asks ComfyUI's lease, the connector asks the pack to free the
// model, the pack frees it, the instrument shows the fall and the engine's
// lease is granted. One sentence tells the person.
func TestCardIdleComfyUIGivesWayWhenOANeedsTheMemory(t *testing.T) {
	s := newCardStage(t)
	s.loadWan()
	freed, told := make(chan time.Time, 4), make(chan string, 4)
	go s.pack(freed, told)
	engine := service.Subject{Program: `C:\oa\modelhostd.exe`, Account: "S-1-5-21-7-1001"}
	out := s.book.Acquire(s.ctx, engine, &identity.Peer{}, instrument.Card0, 25*cardGiB, 20000)
	if out.Lease == nil {
		t.Fatalf("the engine's lease read %s, asked %+v", out.Outcome, out.Asked)
	}
	select {
	case <-freed:
	default:
		t.Fatal("the pack was never asked to free Wan 2.2")
	}
	select {
	case sentence := <-told:
		const want = "OpenAbstractions freed memory in ComfyUI (Wan 2.2) for modelhostd."
		if sentence != want {
			t.Fatalf("the sentence reads %q, want %q", sentence, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no sentence told the yield")
	}
	// The Panel's Memory activity reads the lease's label and the freed
	// record from the book's audit (RES-L10).
	lines, err := service.ReadAudit(s.audit, 64)
	if err != nil {
		t.Fatal(err)
	}
	all := strings.Join(lines, "\n")
	if !strings.Contains(all, `"event":"label"`) || !strings.Contains(all, `"label":"ComfyUI: Wan 2.2"`) || !strings.Contains(all, `"event":"freed"`) {
		t.Fatalf("the audit lacks ComfyUI's label or the freed record:\n%s", all)
	}
}

// A ComfyUI prompt that is running is never cut short: the engine's ask
// waits, the pack is asked to free nothing until the prompt ends, and the
// lease is granted after it.
func TestCardBusyComfyUIYieldsOnlyAfterItsPromptEnds(t *testing.T) {
	s := newCardStage(t)
	s.loadWan()
	s.call("prompt_begin", nil)
	freed, told := make(chan time.Time, 4), make(chan string, 4)
	go s.pack(freed, told)
	ended := make(chan time.Time, 1)
	go func() {
		time.Sleep(1500 * time.Millisecond)
		ended <- time.Now()
		s.call("prompt_end", nil)
	}()
	engine := service.Subject{Program: `C:\oa\modelhostd.exe`, Account: "S-1-5-21-7-1001"}
	out := s.book.Acquire(s.ctx, engine, &identity.Peer{}, instrument.Card0, 25*cardGiB, 20000)
	if out.Lease == nil {
		t.Fatalf("the engine's lease read %s, asked %+v", out.Outcome, out.Asked)
	}
	end := <-ended
	select {
	case at := <-freed:
		if at.Before(end) {
			t.Fatalf("Wan 2.2 was freed %s before the prompt ended", end.Sub(at))
		}
	default:
		t.Fatal("the pack was never asked to free Wan 2.2")
	}
}

// While the prompt waits at its load point for a new model's lease, an idle
// model that load does not use is no longer in use, and a model it uses
// still is.
func TestCardLoadPointFreesOnlyWhatTheLoadDoesNotUse(t *testing.T) {
	b, err := newCardBroker(nil, nil)
	if err == nil || b != nil {
		t.Fatal("a broker without a lease service was made")
	}
	b, err = newCardBroker(res.New(""), func(error) {})
	if err != nil {
		t.Fatal(err)
	}
	b.running = 1
	if b.inFlight("old") != 1 {
		t.Fatal("a model was free to go while its prompt ran")
	}
	b.loads, b.loading = 1, map[string]bool{"new": true, "kept": true}
	if b.inFlight("old") != 0 || b.inFlight("kept") != 1 {
		t.Fatalf("at the load point old reads %d and kept %d", b.inFlight("old"), b.inFlight("kept"))
	}
}

// A ComfyUI with no lease service loads as it always did: the load reads
// not granted with a word, and nothing waits.
func TestCardWithoutTheServiceFailsOpen(t *testing.T) {
	cards := newRelayCards(context.Background())
	cards.dial = func() holderLeases { return res.New("") }
	t.Cleanup(cards.close)
	request := map[string]json.RawMessage{"op": json.RawMessage(`"card_load"`), "name": json.RawMessage(`"m1"`), "bytes": json.RawMessage(`1073741824`)}
	began := time.Now()
	reply, err := cards.answer(context.Background(), request, nil)
	if err != nil {
		t.Fatal(err)
	}
	if reply["granted"] != false || !strings.HasPrefix(fmt.Sprint(reply["word"]), "lease:") {
		t.Fatalf("the load read %v", reply)
	}
	if time.Since(began) > 10*time.Second {
		t.Fatalf("an unreachable service held the load %s", time.Since(began))
	}
}
