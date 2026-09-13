package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"time"

	res "github.com/openabstractions/abstraction-resource/go/client"
	"github.com/openabstractions/abstraction-resource/go/holder"
)

// The card broker makes this ComfyUI a lease holder of card:0, the way OA's
// own engines are (abstraction-resource CONTRACT.md RES-L2, RES-L3, RES-L9).
// The node pack in ComfyUI's process (node/oa_lease.py) cannot hold a lease:
// ComfyUI's interpreter is shared and has no rights of its own. The connector
// holds them, under the program OpenAbstractions knows this ComfyUI by, and
// the pack tells it over the native-relay lines what ComfyUI loads, frees and
// runs:
//
//	card_load      {name, title, bytes, using}  before ComfyUI loads a model;
//	               answers {granted} or {granted: false, word, sentence}
//	card_loaded    {name, bytes}                what the load took
//	card_unloaded  {name}                       ComfyUI let the model go itself
//	prompt_begin   {}                           a prompt starts; waits while a yield runs
//	prompt_end     {}                           the prompt ended
//	card_free_wait {ms}                         long poll: {free: [{request, name}], told: [...]}
//	card_freed     {request, ok}                the pack freed the model between prompts
//
// Each model ComfyUI loads is one component of the provider kit's holder,
// named by the pack. A yield request reaches the holder; the holder waits
// for a moment no prompt runs (InFlight, Pass), then stops the component,
// which asks the pack to free that model and waits for its answer. The
// pack frees it between prompts, or at the load point of the running
// prompt when the model is not part of that load. Nothing is killed
// (RES-L5). Each yield writes one sentence the pack logs.
//
// The broker fails open: a lease service that cannot be reached, or that
// refuses, leaves ComfyUI loading as it always did.

// cardResource is the resource ComfyUI's weights take.
const cardResource = "card:0"

// cardLoadBudget bounds one card_load: the holders' wait plus the busy
// retries the holder makes while a prompt elsewhere finishes.
var cardLoadBudget = 120 * time.Second

// cardFreeBudget bounds the wait for the pack to free one model.
var cardFreeBudget = 30 * time.Second

// cardHolderWait is the holder's Acquire wait.
var cardHolderWait = 45 * time.Second

// cardIdle keeps ComfyUI's models resident until another holder needs the
// memory: ComfyUI's own policy keeps them, and OA frees them only on a yield.
const cardIdle = 100 * 365 * 24 * time.Hour

const cardNameLimit = 256

type cardFree struct {
	Request int64  `json:"request"`
	Name    string `json:"name"`
}

// cardModel is one model ComfyUI loaded under a lease.
type cardModel struct {
	broker *cardBroker
	name   string
	gone   chan struct{}
	once   sync.Once
}

func (m *cardModel) PID() int                    { return 0 }
func (m *cardModel) Ready(context.Context) error { return nil }
func (m *cardModel) Alive() bool {
	select {
	case <-m.gone:
		return false
	default:
		return true
	}
}
func (m *cardModel) let() { m.once.Do(func() { close(m.gone) }) }

// Stop asks the pack to free this model and reports whether it did. A model
// ComfyUI already let go is stopped.
func (m *cardModel) Stop() bool {
	if !m.Alive() {
		return true
	}
	ok := m.broker.free(m.name)
	if ok {
		m.let()
	}
	return ok
}

// cardBroker is one ComfyUI's leases.
type cardBroker struct {
	leases holder.Leases
	holder *holder.Holder

	mu       sync.Mutex
	running  int
	loading  map[string]bool // the names the outstanding card_load uses
	loads    int
	models   map[string]*cardModel
	titles   map[string]string // name -> what the person reads
	byLease  map[string]string // lease -> name, kept until its yield is told
	asks     map[string]res.YieldRequest
	next     int64
	pending  []cardFree
	answers  map[int64]chan bool
	told     []string
	notify   chan struct{}
	stopping chan struct{}
}

// cardLeases taps the lease calls: an Observe page names each request's
// asker, and a yielded Answer is told as one sentence.
type cardLeases struct {
	holder.Leases
	broker *cardBroker
}

func (l cardLeases) Observe(ctx context.Context, cursor string, wait time.Duration) (res.YieldPage, error) {
	page, err := l.Leases.Observe(ctx, cursor, wait)
	if err == nil {
		l.broker.mu.Lock()
		for _, request := range page.Requests {
			l.broker.asks[request.ID] = request
		}
		l.broker.mu.Unlock()
	}
	return page, err
}

func (l cardLeases) Answer(ctx context.Context, request string, answer res.YieldAnswer, reason string) (res.AnswerResult, error) {
	result, err := l.Leases.Answer(ctx, request, answer, reason)
	l.broker.answered(request, answer)
	return result, err
}

func newCardBroker(leases holder.Leases, onError func(error)) (*cardBroker, error) {
	if leases == nil {
		return nil, errors.New("card: a lease service is required")
	}
	b := &cardBroker{loading: map[string]bool{}, models: map[string]*cardModel{}, titles: map[string]string{},
		byLease: map[string]string{}, asks: map[string]res.YieldRequest{}, answers: map[int64]chan bool{},
		notify: make(chan struct{}, 1), stopping: make(chan struct{})}
	b.leases = cardLeases{Leases: leases, broker: b}
	h, err := holder.New(holder.Options{Leases: b.leases, Resource: cardResource, Idle: cardIdle, Wait: cardHolderWait,
		InFlight: b.inFlight, OnError: onError, Label: b.label})
	if err != nil {
		return nil, err
	}
	b.holder = h
	return b, nil
}

// label is a lease's words for the person, "ComfyUI: <model>" (RES-L10).
func (b *cardBroker) label(name string) string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if title := b.titles[name]; title != "" {
		return "ComfyUI: " + title
	}
	return "ComfyUI"
}

// run keeps the leases renewed and answers yield requests until ctx ends.
func (b *cardBroker) run(ctx context.Context) {
	b.holder.Run(ctx)
}

// close releases every lease; ComfyUI is ending.
func (b *cardBroker) close() {
	b.mu.Lock()
	select {
	case <-b.stopping:
	default:
		close(b.stopping)
	}
	for _, m := range b.models {
		m.let()
	}
	b.mu.Unlock()
	b.holder.Close()
}

// inFlight is the prompts running on a model: every model while a prompt
// runs, except at the prompt's load point, where only the models that load
// uses are in use. ComfyUI itself frees the others there (its free_memory).
func (b *cardBroker) inFlight(name string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.loads > 0 && !b.loading[name] {
		return 0
	}
	return b.running
}

// free asks the pack to free name and waits for its answer.
func (b *cardBroker) free(name string) bool {
	b.mu.Lock()
	b.next++
	request := cardFree{Request: b.next, Name: name}
	answer := make(chan bool, 1)
	b.answers[request.Request] = answer
	b.pending = append(b.pending, request)
	b.mu.Unlock()
	b.wake()
	timer := time.NewTimer(cardFreeBudget)
	defer timer.Stop()
	select {
	case ok := <-answer:
		return ok
	case <-timer.C:
	case <-b.stopping:
	}
	b.mu.Lock()
	delete(b.answers, request.Request)
	b.mu.Unlock()
	return false
}

func (b *cardBroker) wake() {
	select {
	case b.notify <- struct{}{}:
	default:
	}
}

// answered tells one yield the holder answered yielded.
func (b *cardBroker) answered(request string, answer res.YieldAnswer) {
	b.mu.Lock()
	ask, ok := b.asks[request]
	delete(b.asks, request)
	if !ok || answer != res.YieldAnswerYielded {
		b.mu.Unlock()
		return
	}
	name := b.byLease[ask.Lease]
	delete(b.byLease, ask.Lease)
	title := b.titles[name]
	if title == "" {
		title = "model"
	}
	b.told = append(b.told, fmt.Sprintf("OpenAbstractions freed memory in ComfyUI (%s) for %s.", title, askerTitle(ask.Reason)))
	if len(b.told) > 32 {
		b.told = b.told[len(b.told)-32:]
	}
	b.mu.Unlock()
	b.wake()
}

// askerTitle is the asking program as a person reads it: its file name
// without the extension.
func askerTitle(program string) string {
	base := filepath.Base(strings.ReplaceAll(program, `\`, "/"))
	base = strings.TrimSuffix(base, filepath.Ext(base))
	if base == "" || base == "." || base == "/" {
		return "another program"
	}
	return base
}

// handle answers one card_ or prompt_ line. ok false is an op it does not
// know.
func (b *cardBroker) handle(ctx context.Context, op string, request map[string]json.RawMessage) (map[string]any, bool, error) {
	switch op {
	case "card_load":
		return b.load(ctx, request)
	case "card_loaded":
		_, err := cardName(request)
		return map[string]any{}, true, err
	case "card_unloaded":
		name, err := cardName(request)
		if err != nil {
			return nil, true, err
		}
		b.unloaded(name)
		return map[string]any{}, true, nil
	case "prompt_begin":
		return b.begin(ctx)
	case "prompt_end":
		b.mu.Lock()
		if b.running > 0 {
			b.running--
		}
		b.mu.Unlock()
		return map[string]any{}, true, nil
	case "card_free_wait":
		var ms int64
		if raw := request["ms"]; len(raw) > 0 {
			if err := json.Unmarshal(raw, &ms); err != nil || ms < 0 || ms > 25000 {
				return nil, true, errors.New("invalid ms")
			}
		}
		return b.wait(ctx, time.Duration(ms)*time.Millisecond), true, nil
	case "card_freed":
		var answer struct {
			Request int64 `json:"request"`
			OK      bool  `json:"ok"`
		}
		raw, _ := json.Marshal(request)
		if err := json.Unmarshal(raw, &answer); err != nil || answer.Request <= 0 {
			return nil, true, errors.New("invalid request")
		}
		b.mu.Lock()
		channel := b.answers[answer.Request]
		delete(b.answers, answer.Request)
		b.mu.Unlock()
		if channel != nil {
			channel <- answer.OK
		}
		return map[string]any{}, true, nil
	}
	return nil, false, nil
}

func cardName(request map[string]json.RawMessage) (string, error) {
	var name string
	if err := json.Unmarshal(request["name"], &name); err != nil || name == "" || len(name) > cardNameLimit || strings.ContainsAny(name, "\x00\r\n") {
		return "", errors.New("invalid name")
	}
	return name, nil
}

// load takes a lease for one model ComfyUI is about to load. While it waits,
// the prompt is at its load point: the models it does not use may be freed.
func (b *cardBroker) load(ctx context.Context, request map[string]json.RawMessage) (map[string]any, bool, error) {
	name, err := cardName(request)
	if err != nil {
		return nil, true, err
	}
	var bytes int64
	if err := json.Unmarshal(request["bytes"], &bytes); err != nil || bytes <= 0 {
		return nil, true, errors.New("invalid bytes")
	}
	var title string
	if raw := request["title"]; len(raw) > 0 {
		_ = json.Unmarshal(raw, &title)
	}
	if len(title) > cardNameLimit || strings.ContainsAny(title, "\x00\r\n") {
		title = ""
	}
	var using []string
	if raw := request["using"]; len(raw) > 0 {
		_ = json.Unmarshal(raw, &using)
	}
	b.mu.Lock()
	if title != "" {
		b.titles[name] = title
	}
	b.loads++
	for _, n := range append(using, name) {
		b.loading[n] = true
	}
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		if b.loads--; b.loads == 0 {
			b.loading = map[string]bool{}
		}
		b.mu.Unlock()
	}()
	ctx, cancel := context.WithTimeout(ctx, cardLoadBudget)
	defer cancel()
	model := &cardModel{broker: b, name: name, gone: make(chan struct{})}
	err = b.holder.Ensure(ctx, name, func() (holder.Component, error) {
		return holder.Component{Amount: bytes, Start: func() (holder.Owned, error) {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return model, nil
		}}, nil
	})
	if err != nil {
		word := "lease:unavailable"
		var failure *holder.Failure
		if errors.As(err, &failure) && failure.Word() != "" {
			word = failure.Word()
		}
		return map[string]any{"granted": false, "word": word, "sentence": oneSentence(err.Error())}, true, nil
	}
	lease, _ := b.holder.Snapshot(name)
	b.mu.Lock()
	if current := b.models[name]; current == nil || !current.Alive() {
		b.models[name] = model
	}
	if lease != "" {
		b.byLease[lease] = name
	}
	b.mu.Unlock()
	return map[string]any{"granted": true}, true, nil
}

// unloaded releases the lease of a model ComfyUI let go by itself.
func (b *cardBroker) unloaded(name string) {
	b.mu.Lock()
	model := b.models[name]
	delete(b.models, name)
	for lease, n := range b.byLease {
		if n == name {
			delete(b.byLease, lease)
		}
	}
	b.mu.Unlock()
	if model != nil {
		model.let()
	}
	b.holder.Unload(name, "ComfyUI let it go")
}

// begin counts a prompt and holds it back while a yield runs on any model
// (holder.Pass), so a yield happens between prompts.
func (b *cardBroker) begin(ctx context.Context) (map[string]any, bool, error) {
	b.mu.Lock()
	b.running++
	b.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, cardLoadBudget)
	defer cancel()
	for _, name := range b.holder.Residents() {
		if err := b.holder.Pass(ctx, name); err != nil {
			break
		}
	}
	return map[string]any{}, true, nil
}

// wait returns the free requests and sentences pending, waiting up to max
// for one.
func (b *cardBroker) wait(ctx context.Context, max time.Duration) map[string]any {
	timer := time.NewTimer(max)
	defer timer.Stop()
	for {
		b.mu.Lock()
		if len(b.pending) > 0 || len(b.told) > 0 {
			free, told := b.pending, b.told
			b.pending, b.told = nil, nil
			b.mu.Unlock()
			if free == nil {
				free = []cardFree{}
			}
			if told == nil {
				told = []string{}
			}
			return map[string]any{"free": free, "told": told}
		}
		b.mu.Unlock()
		select {
		case <-b.notify:
		case <-timer.C:
			return map[string]any{"free": []cardFree{}, "told": []string{}}
		case <-ctx.Done():
			return map[string]any{"free": []cardFree{}, "told": []string{}}
		case <-b.stopping:
			return map[string]any{"free": []cardFree{}, "told": []string{}}
		}
	}
}

// oneSentence is an error as one line of at most 300 bytes.
func oneSentence(text string) string {
	text = strings.Join(strings.Fields(text), " ")
	if len(text) > 300 {
		text = text[:300]
	}
	return text
}
