//go:build windows

package main

import (
	"os"
	"path/filepath"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Comfy Desktop opens on its dashboard and starts an install's ComfyUI only
// when the person launches it there (or when its own "launch on startup"
// setting names one). Comfy Desktop 1.1.9 reads no install from its command
// line, registers no link scheme, and a second start only focuses its
// window. While the connector waits for the report, it watches for a
// process of this install: with none after desktopIdleGrace, the startup
// phase reads D, and commands and open say that the person starts ComfyUI
// in Comfy Desktop.

// desktopIdleGrace is how long after starting Comfy Desktop the connector
// waits for a process of this install before it reports Desktop idle.
var desktopIdleGrace = 15 * time.Second

// desktopIdleEvery is how often the process table is read for it.
var desktopIdleEvery = 2 * time.Second

// desktopWatch reports, once desktopIdleGrace passed since launched, whether
// a process of this install runs, through h.idle. It reads the process table
// at most once per desktopIdleEvery and reports changes only.
func (h *host) desktopWatch(launched time.Time) func() {
	var checked time.Time
	reported, idle := false, false
	return func() {
		now := time.Now()
		if h.idle == nil || now.Sub(launched) < desktopIdleGrace || now.Sub(checked) < desktopIdleEvery {
			return
		}
		checked = now
		running := h.installRunning()
		if reported && idle == !running {
			return
		}
		reported, idle = true, !running
		if idle {
			h.events.log(nativeLevelInfo, "Comfy Desktop is open, but no ComfyUI install runs in it; OpenAbstractions waits for the person to start ComfyUI in Comfy Desktop", nil)
		} else {
			h.events.log(nativeLevelInfo, "ComfyUI is starting in Comfy Desktop", nil)
		}
		h.idle(idle)
	}
}

// installRunning is whether a process whose program lies in this install's
// folder runs, other than the connector's own adapter.
func (h *host) installRunning() bool {
	if h.installProcesses != nil {
		return h.installProcesses()
	}
	return installProcessRuns(filepath.Dir(h.install.Base), h.install.Node, os.Getpid())
}

// installProcessRuns reads the process table for a program inside root,
// outside node, whose parent and grandparent are not self.
func installProcessRuns(root, node string, self int) bool {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return false
	}
	defer windows.CloseHandle(snapshot)
	parents := map[int]int{}
	var entry windows.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	for err = windows.Process32First(snapshot, &entry); err == nil; err = windows.Process32Next(snapshot, &entry) {
		parents[int(entry.ProcessID)] = int(entry.ParentProcessID)
	}
	for pid, parent := range parents {
		if pid == self || parent == self || parents[parent] == self || pid == 0 {
			continue
		}
		image, ok := windowsProcesses{}.image(pid)
		if ok && insideFolder(image, root) && !insideFolder(image, node) {
			return true
		}
	}
	return false
}

// insideFolder compares Windows paths without case.
func insideFolder(path, folder string) bool {
	if folder == "" {
		return false
	}
	prefix := strings.ToLower(filepath.Clean(folder)) + string(filepath.Separator)
	return strings.HasPrefix(strings.ToLower(filepath.Clean(path)), prefix)
}
