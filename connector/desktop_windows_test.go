//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"

	identity "github.com/openabstractions/abstraction-identity"
)

const testDesktop = `C:\Users\person\AppData\Local\Programs\@comfyorgcomfyui-electron\ComfyUI.exe`

func wantListedDesktop(comfy string) (kind, program string) { return kindComfyDesktop, comfy }

// fakeProcesses: Desktop main 10 started Python 30, directly or through the
// venv launcher 20.
type fakeProcesses struct {
	parents map[int]int
	images  map[int]string
	times   map[int]int64
}

func (f fakeProcesses) parent(pid int) (int, bool)    { p, ok := f.parents[pid]; return p, ok }
func (f fakeProcesses) image(pid int) (string, bool)  { p, ok := f.images[pid]; return p, ok }
func (f fakeProcesses) created(pid int) (int64, bool) { p, ok := f.times[pid]; return p, ok }

func desktopTree() fakeProcesses {
	return fakeProcesses{
		parents: map[int]int{30: 10, 10: 1},
		images:  map[int]string{10: testDesktop, 30: `C:\Python312\python.exe`},
		times:   map[int]int64{1: 1, 10: 100, 30: 200},
	}
}

func trusted(string) (identity.Code, error) {
	return identity.Code{Trusted: true, Status: "valid"}, nil
}

const testBase = `C:\Users\person\Documents\ComfyUI`

// launchedTree puts this ComfyUI folder's venv launcher 20 between Desktop
// main 10 and Python 30.
func launchedTree(image string) fakeProcesses {
	f := desktopTree()
	f.parents[30], f.parents[20] = 20, 10
	f.images[20] = image
	f.times[20] = 150
	return f
}

func TestDesktopWindowMustComeFromThisConnector(t *testing.T) {
	launchers := venvLaunchers(testBase)
	for _, tree := range []fakeProcesses{desktopTree(), launchedTree(launchers[0])} {
		started := desktopProcess{pid: 10, created: 100}
		if !desktopOpenedBy(tree, started, 30, testDesktop, launchers) {
			t.Fatal("the Desktop launched by this connector was refused")
		}
		// The process table claims the connector as parent. There is no
		// matching launch returned by the connector's native Start call.
		if desktopOpenedBy(tree, desktopProcess{}, 30, testDesktop, launchers) {
			t.Fatal("a separately launched Desktop was admitted")
		}
		tree.times[10] = 101
		if desktopOpenedBy(tree, started, 30, testDesktop, launchers) {
			t.Fatal("a reused Desktop PID was admitted")
		}
	}
}

func TestDesktopContinuityChecksTheMainProcessAndItsSignature(t *testing.T) {
	launchers := venvLaunchers(testBase)
	if err := desktopContinuity(desktopTree(), trusted, 30, strings.ToUpper(testDesktop), launchers); err != nil {
		t.Fatal(err)
	}
	for _, launcher := range []string{testBase + `\.venv\Scripts\python.exe`, strings.ToUpper(testBase) + `\.VENV\Scripts\pythonw.exe`} {
		if err := desktopContinuity(launchedTree(launcher), trusted, 30, testDesktop, launchers); err != nil {
			t.Fatalf("the venv launcher %s is passed over: %v", launcher, err)
		}
	}
	for name, f := range map[string]fakeProcesses{
		"other folder's launcher": launchedTree(`C:\Users\person\Documents\Other\.venv\Scripts\python.exe`),
		"launcher by name only":   launchedTree(`C:\Python312\python.exe`),
		"launcher newer than Python": func() fakeProcesses {
			f := launchedTree(launchers[0])
			f.times[20] = 250
			return f
		}(),
		"launcher older than Desktop": func() fakeProcesses {
			f := launchedTree(launchers[0])
			f.times[20] = 50
			return f
		}(),
		"two launchers": func() fakeProcesses {
			f := launchedTree(launchers[0])
			f.parents[20], f.parents[21] = 21, 10
			f.images[21], f.times[21] = launchers[0], 120
			return f
		}(),
		"other program": func() fakeProcesses { f := desktopTree(); f.images[10] = `C:\Other\Other.exe`; return f }(),
		"newer main":    func() fakeProcesses { f := desktopTree(); f.times[10] = 300; return f }(),
		"no parent":     func() fakeProcesses { f := desktopTree(); delete(f.parents, 30); return f }(),
		"gone main":     func() fakeProcesses { f := desktopTree(); delete(f.images, 10); return f }(),
	} {
		if desktopContinuity(f, trusted, 30, testDesktop, launchers) == nil {
			t.Errorf("%s passed continuity", name)
		}
	}
	unsigned := func(string) (identity.Code, error) { return identity.Code{Status: "no signature"}, nil }
	if err := desktopContinuity(desktopTree(), unsigned, 30, testDesktop, launchers); !errors.Is(err, errDesktopUnsigned) ||
		err.Error() != "ComfyUI Desktop isn't signed, so agents use the browser page" {
		t.Fatalf("an unsigned Desktop: %v", err)
	}
	failed := func(string) (identity.Code, error) { return identity.Code{}, errors.New("unreadable") }
	if err := desktopContinuity(desktopTree(), failed, 30, testDesktop, launchers); err == nil || errors.Is(err, errDesktopUnsigned) {
		t.Fatalf("an unverifiable Desktop: %v", err)
	}
}

// Comfy Desktop 1.1.9's tree, read on a live install (2026-11-11): its main
// process starts <installPath>\ComfyUI\.venv\Scripts\python.exe, the uv venv
// launcher, which runs <installPath>\standalone-env\python.exe, the ComfyUI
// process. The ComfyUI folder is <installPath>\ComfyUI.
func TestComfyDesktopsTreePassesContinuity(t *testing.T) {
	const install = `C:\Users\person\AppData\Local\Comfy-Desktop\ComfyUI-Installs\ComfyUI`
	const program = `C:\Users\person\AppData\Local\Programs\Comfy Desktop\Comfy Desktop.exe`
	base := install + `\ComfyUI`
	tree := fakeProcesses{
		parents: map[int]int{48632: 15392, 47752: 48632, 36480: 48632, 38136: 36480},
		images: map[int]string{48632: program, 47752: program, 36480: base + `\.venv\Scripts\python.exe`,
			38136: install + `\standalone-env\python.exe`},
		times: map[int]int64{15392: 1, 48632: 100, 47752: 101, 36480: 200, 38136: 201},
	}
	launchers := venvLaunchers(base)
	if main, err := desktopMain(tree, 38136, program, launchers); err != nil || main != 48632 {
		t.Fatalf("Comfy Desktop's main: %d %v", main, err)
	}
	if err := desktopContinuity(tree, trusted, 38136, program, launchers); err != nil {
		t.Fatal(err)
	}
	// The standalone interpreter is no launcher: it never stands between.
	other := tree
	other.parents = map[int]int{48632: 15392, 36480: 48632, 37000: 36480, 38136: 37000}
	other.images = map[int]string{48632: program, 36480: base + `\.venv\Scripts\python.exe`, 37000: install + `\standalone-env\python.exe`,
		38136: install + `\standalone-env\python.exe`}
	other.times = map[int]int64{15392: 1, 48632: 100, 36480: 200, 37000: 201, 38136: 202}
	if desktopContinuity(other, trusted, 38136, program, launchers) == nil {
		t.Fatal("a second hop passed continuity")
	}
}

func TestADesktopArmCarriesThePeerAndProgramAndKeepsItsNonceInTheChannel(t *testing.T) {
	var sent []byte
	arm := func(_ context.Context, raw []byte) error { sent = raw; return nil }
	if err := armNativeDesktop(context.Background(), "generation_123456", 8000, testDesktop, arm, func() error { return nil }); err != nil {
		t.Fatal(err)
	}
	var fields map[string]string
	if err := json.Unmarshal(sent, &fields); err != nil {
		t.Fatal(err)
	}
	if len(fields) != 6 || fields["type"] != "arm" || fields["peer"] != "desktop" || fields["desktop_path"] != testDesktop ||
		fields["origin"] != "http://127.0.0.1:8000" || fields["generation"] != "generation_123456" || len(fields["nonce"]) != 32 {
		t.Fatalf("Desktop arm %s", sent)
	}
	sent = nil
	if armNativeDesktop(context.Background(), "g", 8000, testDesktop, arm, func() error { return errors.New("gone") }) == nil || sent != nil {
		t.Fatal("a Desktop arm went to a ComfyUI that no longer listens")
	}
}

func TestTheLiveProcessTableReadsThisProcess(t *testing.T) {
	var table windowsProcesses
	parent, ok := table.parent(os.Getpid())
	if !ok || parent != os.Getppid() {
		t.Fatalf("parent %d %v, want %d", parent, ok, os.Getppid())
	}
	self, _ := os.Executable()
	if image, ok := table.image(os.Getpid()); !ok || !strings.EqualFold(image, self) {
		t.Fatalf("image %q %v, want %q", image, ok, self)
	}
	child, ok := table.created(os.Getpid())
	born, parentOK := table.created(parent)
	if !ok || !parentOK || born >= child {
		t.Fatalf("creation times %d %d", born, child)
	}
}
