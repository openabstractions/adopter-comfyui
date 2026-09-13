package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"sync"

	res "github.com/openabstractions/abstraction-resource/go/client"
	"github.com/openabstractions/abstraction-resource/go/holder"
)

// relayCards routes the native-relay's card_ and prompt_ lines to one card
// broker, started at the first such line against the installed runtime's
// lease service.
type relayCards struct {
	ctx    context.Context
	cancel context.CancelFunc
	dial   func() holderLeases
	once   sync.Once
	broker *cardBroker
	err    error
	done   sync.WaitGroup
}

func newRelayCards(ctx context.Context) *relayCards {
	ctx, cancel := context.WithCancel(ctx)
	return &relayCards{ctx: ctx, cancel: cancel, dial: func() holderLeases { return res.Discover() }}
}

// holderLeases is the lease service the broker holds through.
type holderLeases = holder.Leases

func cardOp(op string) bool {
	return strings.HasPrefix(op, "card_") || strings.HasPrefix(op, "prompt_")
}

// answer answers a card line through the broker and every other line
// through other.
func (c *relayCards) answer(ctx context.Context, request map[string]json.RawMessage,
	other func(map[string]json.RawMessage) (map[string]any, error)) (map[string]any, error) {
	var op string
	_ = json.Unmarshal(request["op"], &op)
	if !cardOp(op) {
		return other(request)
	}
	c.once.Do(func() {
		c.broker, c.err = newCardBroker(c.dial(), func(err error) {
			fmt.Fprintln(os.Stderr, "card:", oneSentence(err.Error()))
		})
		if c.err == nil {
			c.done.Add(1)
			go func() { defer c.done.Done(); c.broker.run(c.ctx) }()
		}
	})
	if c.err != nil {
		return nil, c.err
	}
	reply, known, err := c.broker.handle(ctx, op, request)
	if !known {
		return nil, fmt.Errorf("unknown op %s", op)
	}
	if err != nil {
		return nil, err
	}
	reply["status"] = 0
	return reply, nil
}

// close releases every lease this ComfyUI held; the relay is ending.
func (c *relayCards) close() {
	if c.broker != nil {
		c.broker.close()
	}
	c.cancel()
	c.done.Wait()
}
