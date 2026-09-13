//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/openabstractions/abstraction-facade/go/supervision"
	"golang.org/x/sys/windows"
)

func TestDesktopServerRequiresBreakawayFromOuterJob(t *testing.T) {
	const modeEnv = "OA_COMFY_RETAINED_JOB_TEST"
	const releaseEnv = "OA_COMFY_RETAINED_JOB_RELEASE"
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	const selector = "-test.run=^TestDesktopServerRequiresBreakawayFromOuterJob$"
	mode := os.Getenv(modeEnv)
	if mode != "" {
		if mode == "child" {
			gate := os.Getenv(releaseEnv)
			deadline := time.Now().Add(5 * time.Second)
			for time.Now().Before(deadline) {
				if _, err := os.Stat(gate); err == nil {
					return
				}
				time.Sleep(10 * time.Millisecond)
			}
			t.Fatal("retained launcher never observed the release")
		}
		gate := filepath.Join(t.TempDir(), "released")
		server, err := startDesktopServer(desktopLaunch{Program: self, Args: []string{selector},
			Env: []string{modeEnv + "=child", releaseEnv + "=" + gate}}, filepath.Join(t.TempDir(), "server.log"))
		if server != nil {
			created, alive := liveProcess(server.launcher.PID)
			if !alive || strconv.FormatUint(created, 10) != server.launcher.Created {
				t.Fatal("retained launcher identity differs")
			}
			server.close()
			if err := os.WriteFile(gate, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			<-server.exited
		}
		if mode == "denied" {
			if server != nil || !errors.Is(err, windows.ERROR_ACCESS_DENIED) {
				t.Fatalf("expected outer-job refusal, got server=%t err=%v", server != nil, err)
			}
		} else if err != nil || server == nil || server.code != 0 {
			t.Fatalf("breakaway start failed: server=%t err=%v", server != nil, err)
		}
		return
	}
	for _, mode := range []string{"allowed", "denied"} {
		t.Run(mode, func(t *testing.T) {
			log, err := os.Create(filepath.Join(t.TempDir(), "helper.log"))
			if err != nil {
				t.Fatal(err)
			}
			defer log.Close()
			parent, err := supervision.Start(context.Background(), supervision.Options{
				Executable: self, Args: []string{selector}, Env: append(os.Environ(), modeEnv+"="+mode),
				Stdout: log, Stderr: log, ForceTimeout: 10 * time.Second, AllowBreakaway: mode == "allowed",
			})
			if err != nil {
				t.Fatal(err)
			}
			code, err := parent.Wait()
			if err != nil || code != 0 {
				raw, _ := os.ReadFile(log.Name())
				t.Fatalf("supervised helper exit %d: %v\n%s", code, err, raw)
			}
		})
	}
}

// comfyDesktopFixture is a Comfy Desktop install in a temporary account:
// Desktop's installations.json, settings.json and model-paths yaml, the
// install's venv launcher (this test binary, a stand-in ComfyUI) and a
// Comfy Desktop program file. Desktop's files are the shape Comfy Desktop
// 1.1.9 writes.
type comfyDesktopFixture struct {
	local, roaming, state, installPath, base, yaml, input, output, models string
	install                                                               comfyInstall
	report                                                                string
}

func newComfyDesktopFixture(t *testing.T, launchArgs *string) comfyDesktopFixture {
	t.Helper()
	root := t.TempDir()
	f := comfyDesktopFixture{local: filepath.Join(root, "Local"), roaming: filepath.Join(root, "Roaming")}
	f.state = filepath.Join(f.roaming, "Comfy Desktop")
	f.installPath = filepath.Join(f.local, "Comfy-Desktop", "ComfyUI-Installs", "ComfyUI")
	f.base = filepath.Join(f.installPath, "ComfyUI")
	shared := filepath.Join(f.local, "Comfy-Desktop", "ComfyUI-Shared")
	f.input, f.output, f.models = filepath.Join(shared, "input"), filepath.Join(shared, "output"), filepath.Join(shared, "models")
	f.yaml = filepath.Join(f.state, "instance-model-paths", "inst-1794424347439.yaml")
	writeFixture(t, filepath.Join(f.base, "main.py"), "")
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(self)
	if err != nil {
		t.Fatal(err)
	}
	writeFixture(t, venvPython(f.installPath), string(raw))
	writeFixture(t, filepath.Join(f.local, "Programs", "Comfy Desktop", "Comfy Desktop.exe"), "MZ")
	entry := map[string]any{"id": "inst-1794424347439", "name": "ComfyUI", "installPath": f.installPath, "sourceId": "standalone",
		"status": "installed", "variant": "win-amd", "envVars": map[string]string{"OA_STANDIN_OWN": "yes", "PYTHONIOENCODING": "latin-1"}}
	if launchArgs != nil {
		entry["launchArgs"] = *launchArgs
	}
	list, _ := json.Marshal([]any{entry, map[string]any{"id": "inst-cloud", "status": "installed", "name": "Comfy Cloud", "sourceId": "cloud", "remoteUrl": "https://cloud.comfy.org/"}})
	writeFixture(t, filepath.Join(f.state, "installations.json"), string(list))
	settings, _ := json.Marshal(map[string]any{"modelsDirs": []string{f.models}, "inputDir": f.input, "outputDir": f.output, "onAppClose": "quit"})
	writeFixture(t, filepath.Join(f.state, "settings.json"), string(settings))
	writeFixture(t, f.yaml, "comfy.desktop_0:\n  base_path: '"+f.models+"'\n")
	f.install, err = findInstall(nodePack(t, f.base), accountFolders{Local: f.local, Roaming: f.roaming})
	if err != nil || f.install.Kind != kindComfyDesktop || f.install.Program == "" {
		t.Fatalf("Comfy Desktop install: %+v %v", f.install, err)
	}
	f.report = listeningFile(f.local)
	return f
}

// phases records the startup phases a host moved through.
type phases struct {
	mu   sync.Mutex
	seen []byte
}

func (p *phases) set(phase byte, _ bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.seen) == 0 || p.seen[len(p.seen)-1] != phase {
		p.seen = append(p.seen, phase)
	}
}

func (p *phases) has(phase byte) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return slices.Contains(p.seen, phase)
}

func (f comfyDesktopFixture) host(t *testing.T) (*host, *phases) {
	t.Helper()
	h, _ := testHost(t, f.install, f.report)
	h.folders = accountFolders{Local: f.local, Roaming: f.roaming}
	p := &phases{}
	h.phase = p.set
	return h, p
}

// serveOnce starts h and waits until it serves; later serving after a
// takeover calls started again.
func serveOnce(t *testing.T, h *host, ctx context.Context) <-chan error {
	t.Helper()
	done, ready := make(chan error, 1), make(chan struct{})
	var once sync.Once
	go func() { done <- h.start(ctx, func() { once.Do(func() { close(ready) }) }) }()
	select {
	case <-ready:
	case err := <-done:
		t.Fatalf("not served: %v", err)
	case <-time.After(60 * time.Second):
		t.Fatal("ComfyUI was not served")
	}
	return done
}

// stopStartedOnCleanup stops the servers the record names when the test ends.
func stopStartedOnCleanup(t *testing.T, local string) {
	t.Cleanup(func() {
		if r, ok := readStarted(local, comfyApplication); ok {
			stopRecorded(r.Processes)
		}
	})
}

// A suspended copy of the running test binary probes the inherited Job policy
// without executing even its first instruction.
func desktopFixtureCanBreakAway(t *testing.T) bool {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(self)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: windows.CREATE_BREAKAWAY_FROM_JOB | windows.CREATE_SUSPENDED,
	}
	if err := cmd.Start(); errors.Is(err, windows.ERROR_ACCESS_DENIED) {
		return false
	} else if err != nil {
		t.Fatalf("probe fixture breakaway: %v", err)
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatalf("stop suspended breakaway probe: %v", err)
	}
	var exit *exec.ExitError
	if err := cmd.Wait(); !errors.As(err, &exit) || !exit.Exited() {
		t.Fatalf("join suspended breakaway probe: %v", err)
	}
	return true
}

// open for a Comfy Desktop install with no ComfyUI running starts the
// install's server headless with the command Desktop's files give, records
// the processes it started, serves it, and leaves it running when the
// connector ends. A later connector attaches to it. When Comfy Desktop then
// runs the install, the connector asks OA's server to stop, stops it by PID
// after the grace, and attaches to Desktop's, whose command it remembers for
// the next start (ruling 2026-11-12, points 1, 3 and 4).
func TestOpenStartsAComfyDesktopInstallsServerAndDesktopTakesOver(t *testing.T) {
	args := "--enable-manager --preview-method 'auto'"
	f := newComfyDesktopFixture(t, &args)
	standInEnvironment(t, f.report, f.base)
	record := filepath.Join(t.TempDir(), "started-with.json")
	t.Setenv("OA_STANDIN_ARGS", record)
	stopStartedOnCleanup(t, f.local)

	h, seen := f.host(t)
	if !desktopFixtureCanBreakAway(t) {
		// Release SOURCE encloses the test in a Job that retains its children.
		// Open must refuse this retained launch before it records or serves a backend.
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		err := h.start(ctx, func() { t.Error("served after breakaway was denied") })
		if !errors.Is(err, windows.ERROR_ACCESS_DENIED) ||
			!strings.Contains(err.Error(), "start ComfyUI's server outside the connector job") {
			t.Fatalf("expected the retained launch to refuse breakaway: %v", err)
		}
		if _, ok := readStarted(f.local, comfyApplication); ok || seen.has('L') {
			t.Fatal("refused retained launch recorded a server or reached its listening phase")
		}
		if _, err := os.Stat(record); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("refused retained launch ran the server: %v", err)
		}
		t.Log("retained launch refused by the inherited Job")
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := serveOnce(t, h, ctx)
	port, _ := served(t, h)
	if err := getQueue(port); err != nil || !h.owned || !h.headless.Load() {
		t.Fatalf("OA's server is not served as its own: %v owned=%v headless=%v", err, h.owned, h.headless.Load())
	}
	if !seen.has('L') {
		t.Fatalf("phases %q", seen.seen)
	}
	raw, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	var started struct {
		Args     []string `json:"args"`
		Dir      string   `json:"dir"`
		Encoding string   `json:"encoding"`
		Own      string   `json:"own"`
	}
	_ = json.Unmarshal(raw, &started)
	want := []string{"-s", `ComfyUI\main.py`, "--enable-manager", "--preview-method", "auto", "--extra-model-paths-config", f.yaml,
		"--input-directory", f.input, "--output-directory", f.output}
	if !slices.Equal(started.Args, want) || !strings.EqualFold(filepath.Clean(started.Dir), f.installPath) ||
		started.Encoding != "utf-8" || started.Own != "yes" {
		t.Fatalf("started with %s", raw)
	}
	rec, ok := readStarted(f.local, comfyApplication)
	if !ok || rec.Title != "ComfyUI" || rec.Port != port || len(rec.Processes) != 1 {
		t.Fatalf("started record %+v %v", rec, ok)
	}
	server := rec.Processes[0]
	if created, alive := liveProcess(server.PID); !alive || strconv.FormatUint(created, 10) != server.Created {
		t.Fatalf("the record names no live server: %+v", server)
	}

	// The connector ends; the server keeps running.
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the host did not end")
	}
	if err := getQueue(port); err != nil {
		t.Fatalf("OA's server ended with the connector: %v", err)
	}

	// A later connector attaches to the server OA started.
	startedStopGrace, desktopEvery = time.Second, 100*time.Millisecond
	t.Cleanup(func() { startedStopGrace, desktopEvery = 5*time.Minute, 2*time.Second })
	h2, seen2 := f.host(t)
	ctx2, cancel2 := context.WithCancel(context.Background())
	defer cancel2()
	done2 := serveOnce(t, h2, ctx2)
	if again, _ := served(t, h2); again != port || !h2.headless.Load() {
		t.Fatalf("the later connector serves %d, not OA's server on %d", again, port)
	}

	// Comfy Desktop starts the install: its comfy-procs record names its
	// process. The connector asks OA's server to stop, then stops it by PID.
	desktopArgs := []string{"-s", `ComfyUI\main.py`, "--feature-flag", "show_signin_button=true", "--enable-assets", "--enable-manager"}
	desktop := exec.Command(venvPython(f.installPath), desktopArgs...)
	desktop.Dir = f.installPath
	desktop.Env = append(os.Environ(), "OA_STANDIN_ARGS=")
	desktopRecord := func() {
		created, _ := liveProcess(desktop.Process.Pid)
		childStart := strconv.FormatUint(created, 10)
		proc, _ := json.Marshal(map[string]any{"v": 1, "sessionKey": "session-1", "installationId": "inst-1794424347439", "installPath": f.installPath,
			"childPid": desktop.Process.Pid, "desktopPid": os.Getpid(), "port": 8188, "spawnedAt": time.Now().UnixMilli(),
			"desktopStartTime": nil, "childStartTime": childStart})
		writeFixture(t, filepath.Join(f.state, "comfy-procs", "session-1.json"), string(proc))
	}
	// Desktop's server reports only once OA's has ended, as a second server
	// on one install does.
	if err := os.Remove(f.report); err != nil {
		t.Fatal(err)
	}
	if err := desktop.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = desktop.Process.Kill(); _, _ = desktop.Process.Wait() })
	desktopRecord()
	waitFile(t, stopFile(f.local, comfyApplication))
	stop, _ := os.ReadFile(stopFile(f.local, comfyApplication))
	if !strings.Contains(string(stop), `"processes":[{"pid":`+strconv.Itoa(server.PID)+`,"created":"`+server.Created+`"}]`) {
		t.Fatalf("stop request %s", stop)
	}
	for deadline := time.Now().Add(30 * time.Second); ; time.Sleep(100 * time.Millisecond) {
		if _, alive := liveProcess(server.PID); !alive {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("OA's server outlived the takeover's grace")
		}
	}
	for deadline := time.Now().Add(30 * time.Second); ; time.Sleep(100 * time.Millisecond) {
		if next, _ := served(t, h2); next != 0 && next != port && !h2.headless.Load() {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the connector did not attach to Desktop's ComfyUI; phases %q", seen2.seen)
		}
	}
	if !seen2.has('Y') {
		t.Fatalf("no already-running phase after the takeover: %q", seen2.seen)
	}
	if _, ok := readStarted(f.local, comfyApplication); ok {
		t.Fatal("the started record outlived OA's server")
	}
	// Desktop's command is remembered for the next start.
	for deadline := time.Now().Add(10 * time.Second); !regularFile(rememberedFile(f.local, "inst-1794424347439")); time.Sleep(50 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("Desktop's command was not remembered")
		}
	}
	launch, err := chooseDesktopLaunch(f.local, f.state, f.base, "1.1.9")
	if err != nil || launch.Source != "desktop" || !slices.Equal(launch.Args, desktopArgs) || !slices.Contains(launch.Env, "__COMFYUI_DESKTOP_VERSION__=1.1.9") {
		t.Fatalf("remembered launch %+v %v", launch, err)
	}
	cancel2()
	<-done2
}

// While Comfy Desktop's own program runs, open starts nothing beside it and
// says so (ruling point 4: two servers on one install share its database).
func TestOpenStartsNothingWhileComfyDesktopIsOpen(t *testing.T) {
	args := "--enable-manager"
	f := newComfyDesktopFixture(t, &args)
	self, _ := os.Executable()
	raw, _ := os.ReadFile(self)
	writeFixture(t, f.install.Program, string(raw))
	desktop := exec.Command(f.install.Program)
	desktop.Env = append(os.Environ(), "OA_STANDIN_SLEEP=1")
	if err := desktop.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = desktop.Process.Kill(); _, _ = desktop.Process.Wait() })
	readyLimit = 2 * time.Second
	t.Cleanup(func() { readyLimit = 10 * time.Minute })
	h, seen := f.host(t)
	err := h.start(context.Background(), func() { t.Fatal("served with Comfy Desktop open") })
	if err == nil || !strings.Contains(err.Error(), "close Comfy Desktop") || !seen.has('D') {
		t.Fatalf("open beside Comfy Desktop: %v %q", err, seen.seen)
	}
	if _, ok := readStarted(f.local, comfyApplication); ok {
		t.Fatal("a server was started beside Comfy Desktop")
	}
}

// A missing fact keeps the person's start: Comfy Desktop opens, and OA
// guesses no arguments.
func TestAMissingDesktopFactOpensComfyDesktop(t *testing.T) {
	f := newComfyDesktopFixture(t, nil)
	readyLimit, desktopIdleGrace = 2*time.Second, time.Hour
	t.Cleanup(func() { readyLimit, desktopIdleGrace = 10*time.Minute, 15*time.Second })
	h, _ := f.host(t)
	var launched []string
	h.launch = func(program string) (desktopProcess, error) {
		launched = append(launched, program)
		return desktopProcess{}, nil
	}
	if err := h.start(context.Background(), func() {}); err == nil || len(launched) != 1 || launched[0] != f.install.Program {
		t.Fatalf("fallback: %v launched %v", err, launched)
	}
	if _, ok := readStarted(f.local, comfyApplication); ok {
		t.Fatal("a server was started without Desktop's arguments")
	}
}

// The adapter reads the phases and a failed start's cause through the
// control pipe.
func TestTheControlCarriesStartPhasesAndCause(t *testing.T) {
	s := newNativeStartup(func(ctx context.Context, _ func()) error { <-ctx.Done(); return nil })
	defer s.close()
	if s.open() != 'A' {
		t.Fatal("open not delivered")
	}
	s.setPhase('K', false)
	if s.status() != 'K' || s.open() != 'A' {
		t.Fatalf("phase %q", s.status())
	}
	s.mu.Lock()
	s.state = 'F'
	s.mu.Unlock()
	s.setCause("desktop:early_exit")
	var out strings.Builder
	if err := nativeControl(strings.NewReader("PX"), &out, s); err == nil || !strings.HasPrefix(out.String(), "F") ||
		!strings.HasSuffix(out.String(), `{"cause":"desktop:early_exit"}`) {
		t.Fatalf("control %q %v", out.String(), err)
	}
}

// The phases after launching come from the server's output, and a failed
// start's cause is its last line, a partial last line included.
func TestTheServerOutputGivesPhaseAndLastLine(t *testing.T) {
	file := filepath.Join(t.TempDir(), "server.log")
	writeFixture(t, file, "")
	l := newServerLog(file)
	if l.phase() != 'L' || l.lastLine() != "" {
		t.Fatal("an empty output is not launching")
	}
	writeFixture(t, file, "[assets] --enable-assets passed\r\nTotal VRAM 8192 MB\r\n")
	if l.phase() != 'K' || l.lastLine() != "Total VRAM 8192 MB" {
		t.Fatalf("custom nodes phase %q %q", l.phase(), l.lastLine())
	}
	writeFixture(t, file, "[assets] --enable-assets passed\r\nTotal VRAM 8192 MB\r\nStarting server\n\nOSError: [Errno 10048] address in use")
	if l.phase() != 'W' || l.lastLine() != "OSError: [Errno 10048] address in use" {
		t.Fatalf("listening phase %q %q", l.phase(), l.lastLine())
	}
}
