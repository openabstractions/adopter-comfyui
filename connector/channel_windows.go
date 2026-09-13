//go:build windows

package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"

	cas "github.com/openabstractions/abstraction-cas/go"
	"github.com/openabstractions/abstraction-identity/listen"
	"golang.org/x/sys/windows"
)

// The graph channel: while the connector serves a ComfyUI, it listens on a
// named pipe of its own and names it in
//
//	%LOCALAPPDATA%\OpenAbstractions\ComfyUI\channel-<pid>.json
//	{"pid": <the served ComfyUI's process>, "pipe": "\\.\pipe\openabstractions-comfyui-graph-<32 random>"}
//
// The node pack in that ComfyUI process (node/oa_appmodel_bridge.py) reads
// the file and connects. The pipe admits this account only, with a medium
// label (listen.Listen). The connector keeps a connection only when its
// client process is the served ComfyUI's process, which the connector proved
// holds the reported listener and keeps open while it serves, so that
// process id names no other process. Any other client is refused.

const channelPrefix = `\\.\pipe\openabstractions-comfyui-graph-`
const channelFileLimit = 4096

// channelWait bounds the wait for the node to connect before a page opens.
var channelWait = 10 * time.Second

// channelFile is the channel's name file for the ComfyUI process pid.
func channelFile(folder string, pid int) string {
	return filepath.Join(folder, "channel-"+strconv.Itoa(pid)+".json")
}

type channelName struct {
	PID  int    `json:"pid"`
	Pipe string `json:"pipe"`
}

// nodeChannel is one verified connection from the node.
type nodeChannel struct {
	conn    io.ReadWriteCloser
	writeMu sync.Mutex
	once    sync.Once
}

// write sends one whole frame, bounded by ctx and five seconds.
func (c *nodeChannel) write(ctx context.Context, raw []byte) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	if d, ok := c.conn.(interface{ SetWriteDeadline(time.Time) error }); ok {
		deadline := time.Now().Add(5 * time.Second)
		if limit, ok := ctx.Deadline(); ok && limit.Before(deadline) {
			deadline = limit
		}
		_ = d.SetWriteDeadline(deadline)
	}
	return listen.WriteFrameTo(c.conn, raw, nativeGraphLimit)
}

func (c *nodeChannel) close() { c.once.Do(func() { _ = c.conn.Close() }) }

// pipeClient is the process id of an accepted pipe connection's client.
func pipeClient(conn listen.Conn) (uint32, error) {
	sc, ok := conn.(syscall.Conn)
	if !ok {
		return 0, errors.New("the graph channel has no pipe handle")
	}
	raw, err := sc.SyscallConn()
	if err != nil {
		return 0, err
	}
	var pid uint32
	var clientErr error
	if err := raw.Control(func(fd uintptr) { clientErr = windows.GetNamedPipeClientProcessId(windows.Handle(fd), &pid) }); err != nil {
		return 0, err
	}
	return pid, clientErr
}

// serveChannel serves the graph channel of the ComfyUI process pid until ctx
// ends: it names the pipe in folder, keeps the latest connection whose
// client is pid and refuses every other.
func (b *nativeGraphBridge) serveChannel(ctx context.Context, pid int, folder string, events *nativeEvents) error {
	process, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return fmt.Errorf("the served ComfyUI process %d: %w", pid, err)
	}
	defer windows.CloseHandle(process)
	var token [24]byte
	if _, err := rand.Read(token[:]); err != nil {
		return err
	}
	name := channelPrefix + base64.RawURLEncoding.EncodeToString(token[:])
	l, err := listen.Listen(name)
	if err != nil {
		return err
	}
	defer l.Close()
	stop := context.AfterFunc(ctx, func() { _ = l.Close() })
	defer stop()
	file := channelFile(folder, pid)
	if err := writeChannelName(file, channelName{PID: pid, Pipe: name}); err != nil {
		return fmt.Errorf("the graph channel could not be named: %w", err)
	}
	defer removeChannelName(file, name)
	var kept *nodeChannel
	defer func() {
		if kept != nil {
			kept.close()
		}
	}()
	for {
		conn, err := l.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		client, err := pipeClient(conn)
		if err != nil || client != uint32(pid) {
			_ = conn.Close()
			events.log(nativeLevelWarn, fmt.Sprintf("the ComfyUI graph channel refused process %d; only ComfyUI's process %d may connect", client, pid), nil)
			continue
		}
		kept = b.adopt(conn)
		events.log(nativeLevelInfo, "ComfyUI connected its graph channel", nil)
	}
}

// writeChannelName writes the name file whole through atomic-file (cas).
func writeChannelName(file string, name channelName) error {
	raw, err := json.Marshal(name)
	if err != nil {
		return err
	}
	return cas.ChangeLimit(file, channelFileLimit, func([]byte) ([]byte, error) { return raw, nil })
}

// removeChannelName removes the name file while it still names pipe.
func removeChannelName(file, pipe string) {
	raw, err := readBounded(file, channelFileLimit)
	if err != nil {
		return
	}
	var current channelName
	if json.Unmarshal(raw, &current) == nil && current.Pipe == pipe {
		_ = os.Remove(file)
		// The file is this process's own; cas keeps its lock beside it as
		// <file>.lock, and no other writer names this process's file.
		_ = os.Remove(file + ".lock")
	}
}
