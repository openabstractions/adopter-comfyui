package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"

	identity "github.com/openabstractions/abstraction-identity"
)

// When ComfyUI's server listens, the node pack writes one report
// (adopters/comfyui/node/oa_listening.py):
//
//	%LOCALAPPDATA%\OpenAbstractions\ComfyUI\listening.json
//	{"pid": N, "port": P, "base": "<ComfyUI folder>", "output": "<output folder>"}
//
// The connector serves that ComfyUI only when the process holding the TCP
// listener on that port is the reported process and the reported folder is
// the connector's own ComfyUI. A ComfyUI started with --listen on another
// address, or a stale report, finds nothing.

const listeningName = "listening.json"
const listeningLimit = 64 << 10

// listening is one validated report: the ComfyUI process, its port and its
// output folder.
type listening struct {
	PID    int    `json:"pid"`
	Port   int    `json:"port"`
	Base   string `json:"base"`
	Output string `json:"output"`
}

// listeningFile is the report's place under %LOCALAPPDATA%.
func listeningFile(local string) string {
	return filepath.Join(local, "OpenAbstractions", "ComfyUI", listeningName)
}

var errNoReport = errors.New("ComfyUI has not reported that it listens")

// readListening reads the report at file. Unknown fields are ignored.
func readListening(file string) (listening, error) {
	raw, err := readBounded(file, listeningLimit)
	if err != nil {
		return listening{}, errNoReport
	}
	var r listening
	if err := json.Unmarshal(raw, &r); err != nil {
		return listening{}, fmt.Errorf("the ComfyUI report is unreadable: %w", err)
	}
	if r.PID <= 0 || r.Port < 1 || r.Port > 65535 || !filepath.IsAbs(r.Base) {
		return listening{}, errors.New("the ComfyUI report is incomplete")
	}
	if r.Output != "" && !filepath.IsAbs(r.Output) {
		r.Output = ""
	}
	return r, nil
}

// checkListening proves r belongs to the ComfyUI at base: the reported
// folder is base, and the reported process holds a TCP listener on the
// reported port on a loopback or any-address. owned, when not nil, also
// requires that process to belong to the connector's own process tree.
func checkListening(r listening, base string, sockets func() ([]identity.Socket, error), owned func(pid int) bool) error {
	if !sameFolder(r.Base, base) {
		return fmt.Errorf("the listening ComfyUI is %s, not this connector's %s", r.Base, base)
	}
	table, err := sockets()
	if err != nil {
		return fmt.Errorf("the socket owners are unreadable: %w", err)
	}
	for _, s := range table {
		addr := s.Addr.Unmap()
		if s.Proto != "tcp" || s.Port != r.Port || s.PID != r.PID || !(addr.IsLoopback() || addr.IsUnspecified()) {
			continue
		}
		if owned != nil && !owned(r.PID) {
			return fmt.Errorf("process %d on port %d is not the ComfyUI this connector started", r.PID, r.Port)
		}
		return nil
	}
	return fmt.Errorf("process %d holds no listener on 127.0.0.1:%d", r.PID, r.Port)
}
