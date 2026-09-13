package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strconv"
	"time"

	"github.com/openabstractions/abstraction-facade/go/supervision"
	"golang.org/x/sys/windows"
)

const agentStopEnvironment = "OA_COMFY_OWNED_STOP"

// Generated for a launch and inherited only by its child server. A stale
// connector, attached server or user-writable graph rendezvous has no key.
func agentStopKey() string {
	var key [32]byte
	if _, err := rand.Read(key[:]); err != nil {
		return ""
	}
	return hex.EncodeToString(key[:])
}

type nativeAgentStop struct {
	channel *nodeChannel
	id      string
	answer  chan byte
}

func (b *nativeGraphBridge) requestAgentStop(key string) byte {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	id := agentStopKey()
	b.mu.Lock()
	c, check := b.channel, b.check
	if id == "" || key == "" || c == nil || check == nil || b.stopPending != nil {
		b.mu.Unlock()
		return 'U'
	}
	request := &nativeAgentStop{channel: c, id: id, answer: make(chan byte, 1)}
	b.stopPending = request
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		b.stopPending = nil
		b.mu.Unlock()
	}()
	if check() != nil {
		return 'U'
	}
	raw, err := json.Marshal(map[string]string{"type": "agent_stop", "id": id, "key": key})
	if err != nil {
		return 'U'
	}
	if c.write(ctx, raw) != nil {
		return 'T'
	}
	select {
	case status := <-request.answer:
		return status
	case <-ctx.Done():
		return 'T'
	}
}

func (b *nativeGraphBridge) agentStopResult(c *nodeChannel, id, outcome string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	pending := b.stopPending
	if pending != nil && pending.channel == c && pending.id == id {
		status := byte('T')
		switch outcome {
		case "requested":
			status = 'A'
		case "person_using":
			status = 'P'
		case "unavailable":
			status = 'U'
		}
		select {
		case pending.answer <- status:
		default:
		}
	}
}

func (h *host) bindAgentStop(pid int, key string) func() {
	h.mu.Lock()
	h.stopPID, h.stopKey = pid, key
	h.mu.Unlock()
	return func() {
		h.mu.Lock()
		h.stopPID, h.stopKey = 0, ""
		h.mu.Unlock()
	}
}

func (h *host) requestAgentStop() byte {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.served == nil || !h.owned || h.served.PID != h.stopPID || h.stopKey == "" {
		return 'U'
	}
	return h.graph.requestAgentStop(h.stopKey)
}

// A portable start uses the same Panel record as a persistent Desktop start.
// The job supplies the exact process occurrence before either stop is offered.
func (h *host) recordPortableStart(p *supervision.Process, r listening) error {
	if h.folders.Local == "" {
		return errors.New("the account's start record folder is unavailable")
	}
	record := startedRecord{V: 1, Application: comfyApplication, Title: "ComfyUI",
		StartedUnixMs: p.StartTime().UnixMilli(), Port: r.Port}
	ids := []int{p.PID()}
	if r.PID != ids[0] {
		ids = append(ids, r.PID)
	}
	for _, pid := range ids {
		created, alive := liveProcess(pid)
		stamp := windows.Filetime{LowDateTime: uint32(created), HighDateTime: uint32(created >> 32)}
		held, err := p.Contains(pid, time.Unix(0, stamp.Nanoseconds()))
		if !alive || err != nil || !held {
			return errors.New("the portable ComfyUI process is outside the owned start")
		}
		record.Processes = append(record.Processes, startedProcess{PID: pid, Created: strconv.FormatUint(created, 10)})
	}
	return writeStarted(h.folders.Local, record)
}

func publishStarted(local string, record startedRecord, occurrence func(int) (uint64, bool)) error {
	return supervision.PublishStartRecord(startedFile(local, record.Application), supervision.StartRecord(record),
		func(process startedProcess) bool {
			created, alive := occurrence(process.PID)
			return alive && strconv.FormatUint(created, 10) == process.Created
		})
}
