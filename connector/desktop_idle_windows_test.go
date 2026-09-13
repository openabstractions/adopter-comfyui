//go:build windows

package main

import (
	"context"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func waitPhase(t *testing.T, startup *nativeStartup, want byte) {
	t.Helper()
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if startup.status() == want {
			return
		}
	}
	t.Fatalf("phase %c, want %c", startup.status(), want)
}

// Owner R31, 2026-11-12: open started Comfy Desktop, which stayed on its
// dashboard; no ComfyUI ever started and every command waited for one. With
// no process of this install after the grace, the phase reads D (Desktop
// idle). Once the person starts ComfyUI it reads V again, and R once ComfyUI
// serves.
func TestComfyDesktopWithoutARunningInstallSaysSo(t *testing.T) {
	grace, every := desktopIdleGrace, desktopIdleEvery
	desktopIdleGrace, desktopIdleEvery = 100*time.Millisecond, 10*time.Millisecond
	t.Cleanup(func() { desktopIdleGrace, desktopIdleEvery = grace, every })
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil {
		t.Fatal(err)
	}
	install.Kind, install.Program = kindComfyDesktop, filepath.Join(root, "Programs", "Comfy Desktop.exe")
	if err := os.MkdirAll(filepath.Dir(install.Program), 0o700); err != nil || os.WriteFile(install.Program, []byte("MZ"), 0o600) != nil {
		t.Fatal("Desktop program fixture")
	}
	report := filepath.Join(root, listeningName)
	standInEnvironment(t, report, base)
	h, _ := testHost(t, install, report)
	standInAdapter(t, h.graph)
	var launches atomic.Int32
	var running atomic.Bool
	h.launch = func(string) (desktopProcess, error) {
		launches.Add(1)
		return desktopProcess{}, nil
	}
	h.installProcesses = running.Load
	h.continuity = func(int, string) error { return nil }
	h.armDesktop = func(context.Context, string) error { return nil }
	startup := newNativeStartup(h.run)
	h.idle = startup.desktopIdle
	t.Cleanup(startup.close)
	if startup.open() != 'A' {
		t.Fatal("open not delivered")
	}
	waitPhase(t, startup, 'D')
	if startup.open() != 'A' || launches.Load() != 1 {
		t.Fatalf("a second open while Desktop is idle: launches=%d", launches.Load())
	}
	running.Store(true)
	waitPhase(t, startup, 'V')
	startStandIn(t)
	waitPhase(t, startup, 'R')
}
