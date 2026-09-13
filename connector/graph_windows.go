//go:build windows

package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/openabstractions/abstraction-identity/listen"
	"golang.org/x/sys/windows"
)

// The page graph bridge (the graph.* commands, the graph context and reveal)
// joins the adapter and the node pack's bridge in the served ComfyUI
// (node/oa_appmodel_bridge.py). The adapter holds two private pipes to this
// connector; the node connects the graph channel (channel_windows.go) to it,
// whether the connector started that ComfyUI or attached to it. The
// connector relays the graph frames between them unchanged and adds its own
// page ARMs on the node's side.

// nativeGraphLimit is one graph frame: a whole canvas projection, bounded by
// the page to 512 nodes and 4,096 links (adopters/comfyui/GRAPH.md).
const nativeGraphLimit = 1 << 20
const nativeArmAckLimit = 256

type nativeArmPending struct {
	generation, nonce string
	ctx               context.Context
	done              chan struct{}
	settled           bool
	err               error
}

// These pipes carry only the fixed private graph DATA protocol. The shared
// Python Server remains the sole MCP implementation and native identity stays
// with this image.
type nativeGraphBridge struct {
	// adapterRead and adapterWrite are the adapter's ends, inherited by it;
	// toAdapter and fromAdapter are this connector's.
	adapterRead, adapterWrite *os.File
	toAdapter, fromAdapter    *os.File
	// toAdapterMu keeps each frame to the adapter whole.
	toAdapterMu sync.Mutex
	mu          sync.Mutex
	// port and check are the served ComfyUI's listener and its check, set
	// once it listens; check is nil before and after.
	port       int
	check      func() error
	generation string
	armPending *nativeArmPending
	// channel is the node's verified graph channel, nil while none;
	// changed closes whenever it changes.
	channel     *nodeChannel
	changed     chan struct{}
	stopPending *nativeAgentStop
	// show opens the page in the person's browser; tests replace it.
	show func(string) error
}

func newNativeGraphBridge() (*nativeGraphBridge, error) {
	b := &nativeGraphBridge{show: showPage, changed: make(chan struct{})}
	var generation [16]byte
	if _, err := rand.Read(generation[:]); err != nil {
		return nil, err
	}
	b.generation = base64.RawURLEncoding.EncodeToString(generation[:])
	var err error
	b.fromAdapter, b.adapterWrite, err = os.Pipe()
	if err == nil {
		b.adapterRead, b.toAdapter, err = os.Pipe()
	}
	if err != nil {
		b.close()
		return nil, err
	}
	for _, f := range []*os.File{b.adapterRead, b.adapterWrite} {
		if err = windows.SetHandleInformation(windows.Handle(f.Fd()), windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT); err != nil {
			b.close()
			return nil, err
		}
	}
	go b.relayAdapter()
	return b, nil
}

func (b *nativeGraphBridge) close() {
	for _, f := range []*os.File{b.adapterRead, b.adapterWrite, b.toAdapter, b.fromAdapter} {
		if f != nil {
			_ = f.Close()
		}
	}
	if c := b.current(); c != nil {
		c.close()
	}
}

// listening records the served ComfyUI's port and listener check; port 0
// clears them.
func (b *nativeGraphBridge) listening(port int, check func() error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.port, b.check = port, check
	if port == 0 {
		b.check = nil
	}
}

// current is the node's graph channel, nil while none.
func (b *nativeGraphBridge) current() *nodeChannel {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.channel
}

// waitChannel waits up to limit for the node's graph channel.
func (b *nativeGraphBridge) waitChannel(ctx context.Context, limit time.Duration) (*nodeChannel, error) {
	timer := time.NewTimer(limit)
	defer timer.Stop()
	for {
		b.mu.Lock()
		c, changed := b.channel, b.changed
		b.mu.Unlock()
		if c != nil {
			return c, nil
		}
		select {
		case <-changed:
		case <-timer.C:
			return nil, errors.New("the ComfyUI node pack did not connect its graph channel; restart ComfyUI once after installing OpenAbstractions for ComfyUI")
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}

// endAdapter ends the adapter's graph reader: no ComfyUI is served any more.
func (b *nativeGraphBridge) endAdapter() {
	b.toAdapterMu.Lock()
	defer b.toAdapterMu.Unlock()
	_ = b.toAdapter.Close()
}

// toAdapterFrame writes one whole frame to the adapter.
func (b *nativeGraphBridge) toAdapterFrame(raw []byte) error {
	b.toAdapterMu.Lock()
	defer b.toAdapterMu.Unlock()
	return listen.WriteFrameTo(b.toAdapter, raw, nativeGraphLimit)
}

// relayAdapter carries the adapter's graph requests to the node. A request
// that finds no channel is answered "unavailable" at once, so the adapter
// retires its graph instead of waiting for an answer no page can give.
func (b *nativeGraphBridge) relayAdapter() {
	for {
		raw, err := listen.ReadFrameFrom(b.fromAdapter, nativeGraphLimit)
		if err != nil {
			return
		}
		if c := b.current(); c != nil {
			if c.write(context.Background(), raw) == nil {
				continue
			}
			c.close()
		}
		var request struct {
			Type       string `json:"type"`
			ID         string `json:"id"`
			Generation string `json:"generation"`
			Page       string `json:"page"`
			Workflow   string `json:"workflow"`
		}
		if json.Unmarshal(raw, &request) != nil || request.Type != "request" || request.ID == "" {
			continue
		}
		answer, err := json.Marshal(struct {
			Type       string `json:"type"`
			ID         string `json:"id"`
			Generation string `json:"generation"`
			Page       string `json:"page"`
			Workflow   string `json:"workflow"`
			Outcome    string `json:"outcome"`
		}{"result", request.ID, request.Generation, request.Page, request.Workflow, "unavailable"})
		if err == nil {
			_ = b.toAdapterFrame(answer)
		}
	}
}

// adopt makes conn the node's graph channel, replacing any earlier one.
func (b *nativeGraphBridge) adopt(conn io.ReadWriteCloser) *nodeChannel {
	c := &nodeChannel{conn: conn}
	b.mu.Lock()
	old := b.channel
	b.channel = c
	close(b.changed)
	b.changed = make(chan struct{})
	b.mu.Unlock()
	if old != nil {
		old.close()
	}
	go b.relayNode(c)
	return c
}

// relayNode carries the node's frames to the adapter until the channel
// ends. The adapter then learns that the last page it saw closed.
func (b *nativeGraphBridge) relayNode(c *nodeChannel) {
	var generation, page string
	for {
		raw, err := listen.ReadFrameFrom(c.conn, nativeGraphLimit)
		if err != nil {
			break
		}
		var head struct {
			Generation string `json:"generation"`
			Page       string `json:"page"`
			Type       string `json:"type"`
			ID         string `json:"id"`
			Outcome    string `json:"outcome"`
		}
		if json.Unmarshal(raw, &head) == nil {
			if head.Type == "agent_stop_result" {
				b.agentStopResult(c, head.ID, head.Outcome)
				continue
			}
			if head.Generation != "" && head.Page != "" {
				generation, page = head.Generation, head.Page
			}
		}
		if b.toAdapterFrame(raw) != nil {
			break
		}
	}
	c.close()
	b.mu.Lock()
	if b.channel == c {
		b.channel = nil
		close(b.changed)
		b.changed = make(chan struct{})
	}
	b.mu.Unlock()
	if page == "" {
		return
	}
	if closed, err := json.Marshal(map[string]string{"type": "closed", "generation": generation, "page": page, "reason": "native_closed"}); err == nil {
		_ = b.toAdapterFrame(closed)
	}
}

func nativeBootstrap(g string, port int) ([]byte, string, error) {
	var nonce [24]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, "", err
	}
	n := base64.RawURLEncoding.EncodeToString(nonce[:])
	origin := "http://127.0.0.1:" + strconv.Itoa(port)
	raw, err := json.Marshal(struct {
		Type       string `json:"type"`
		Generation string `json:"generation"`
		Nonce      string `json:"nonce"`
		Origin     string `json:"origin"`
	}{"arm", g, n, origin})
	return raw, origin + "/oa/appmodel/bootstrap#generation=" + g + "&nonce=" + n, err
}

// openBrowser opens the served ComfyUI's graph page in the person's browser
// once its node connected the graph channel.
func (b *nativeGraphBridge) openBrowser(ctx context.Context) error {
	b.mu.Lock()
	port, check := b.port, b.check
	b.mu.Unlock()
	if check == nil {
		return errors.New("the served ComfyUI does not listen")
	}
	if _, err := b.waitChannel(ctx, channelWait); err != nil {
		return err
	}
	writeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	return openNativePage(writeCtx, b.generation, port, b.arm, check, b.show)
}

func (b *nativeGraphBridge) arm(ctx context.Context, raw []byte) error {
	var fields map[string]string
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	c := b.current()
	if c == nil {
		return errors.New("native bootstrap unavailable")
	}
	pending := &nativeArmPending{generation: fields["generation"], nonce: fields["nonce"], ctx: ctx, done: make(chan struct{})}
	b.mu.Lock()
	if b.armPending != nil {
		b.mu.Unlock()
		return errors.New("native bootstrap already pending")
	}
	b.armPending = pending
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		if b.armPending == pending {
			b.armPending = nil
		}
		b.mu.Unlock()
	}()
	// A write that ctx interrupts ends the channel; once the write finishes,
	// a missing ACK leaves the healthy channel and old page intact.
	if err := c.write(ctx, raw); err != nil {
		c.close()
		return errors.New("native bootstrap unavailable")
	}
	select {
	case <-pending.done:
		return pending.err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (b *nativeGraphBridge) acknowledgeArm(raw []byte) error {
	var fields map[string]string
	valid := len(raw) <= nativeArmAckLimit && jsonValue(json.NewDecoder(bytes.NewReader(raw)), 0, "") == nil && json.Unmarshal(raw, &fields) == nil && len(fields) == 3 && fields["type"] == "arm_ack"
	b.mu.Lock()
	defer b.mu.Unlock()
	pending := b.armPending
	if pending == nil || pending.settled {
		return errors.New("native bootstrap acknowledgement is not pending")
	}
	pending.settled = true
	if pending.ctx.Err() != nil || !valid || fields["generation"] != pending.generation || fields["nonce"] != pending.nonce {
		pending.err = errors.New("native bootstrap acknowledgement differs")
	}
	close(pending.done)
	return pending.err
}

// showPage opens target in the person's browser.
func showPage(target string) error {
	verb, _ := windows.UTF16PtrFromString("open")
	url, _ := windows.UTF16PtrFromString(target)
	return windows.ShellExecute(0, verb, url, nil, nil, windows.SW_SHOWNORMAL)
}

func openNativePage(ctx context.Context, generation string, port int, arm func(context.Context, []byte) error, check func() error, show func(string) error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := check(); err != nil {
		return err
	}
	raw, target, err := nativeBootstrap(generation, port)
	if err != nil {
		return err
	}
	if err = arm(ctx, raw); err != nil {
		return errors.New("native bootstrap unavailable")
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	if err = check(); err != nil {
		return err
	}
	return show(target)
}

// jsonValue decodes one JSON value, refusing duplicate keys, null, numbers
// that are no 64-bit integer and nesting deeper than 16.
func jsonValue(d *json.Decoder, depth int, path string) error {
	if depth > 16 {
		return errors.New("JSON nesting refused")
	}
	t, e := d.Token()
	if e != nil {
		return e
	}
	if t == nil {
		return errors.New("JSON null refused")
	}
	if number, ok := t.(json.Number); ok {
		if _, e := number.Int64(); e != nil {
			return errors.New("noninteger or overflowing JSON number")
		}
		return nil
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		seen := map[string]bool{}
		for d.More() {
			key, e := d.Token()
			if e != nil {
				return e
			}
			name, ok := key.(string)
			if !ok || seen[name] {
				return errors.New("duplicate JSON key")
			}
			seen[name] = true
			if e := jsonValue(d, depth+1, path+"."+name); e != nil {
				return e
			}
		}
		if t, e = d.Token(); e != nil || t != json.Delim('}') {
			return errors.New("malformed JSON object")
		}
	case '[':
		for d.More() {
			if e := jsonValue(d, depth+1, path+"[]"); e != nil {
				return e
			}
		}
		if t, e = d.Token(); e != nil || t != json.Delim(']') {
			return errors.New("malformed JSON array")
		}
	default:
		return errors.New("unexpected JSON delimiter")
	}
	return nil
}
