//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/openabstractions/abstraction-facade/go/supervision"
	identity "github.com/openabstractions/abstraction-identity"
	"github.com/openabstractions/abstraction-storage/go/layout"
	"golang.org/x/sys/windows"
)

// The owned host: the runtime starts the connector with owned-host and
// speaks the application frames over its standard input and output. The
// connector announces the comfyui presence, starts its adapter
// (oa_native_host.py, run by this ComfyUI's own Python) with those standard
// streams, and answers the adapter's fixed control requests (core_windows.go).
//
// open serves this ComfyUI:
//   - a ComfyUI already listening (listening.go) is attached and shown;
//   - a portable ComfyUI is started as the connector's own process and shown;
//   - ComfyUI Desktop or Comfy Desktop is started as the person's own
//     program, and attached once it reports;
//   - any other ComfyUI is refused with errStartYourself.
//
// While a ComfyUI is served, its node pack connects the graph channel
// (channel_windows.go), and showing it opens its graph page. Desktop
// windows from the connector's own launch can carry that page. A ComfyUI the
// connector started stops with the connector; one it attached to keeps
// running.

const adapterMember = "oa_native_host.py"

// readyLimit bounds the wait for a started ComfyUI's report. A first start
// of ComfyUI Desktop can install its Python packages first.
var readyLimit = 10 * time.Minute

// readyPoll is how often the report is read while ComfyUI starts.
var readyPoll = 250 * time.Millisecond

// endedGrace is how long a host whose ComfyUI ended keeps answering before
// it ends itself.
const endedGrace = 5 * time.Second

var errStartYourself = errors.New("Start ComfyUI yourself, then open it again: OpenAbstractions knows no program that starts this ComfyUI")

// host serves one ComfyUI to the adapter.
type host struct {
	install comfyInstall
	// report is the listening report's file.
	report string
	graph  *nativeGraphBridge
	events *nativeEvents
	// sockets reads the TCP listener owners; tests replace it.
	sockets func() ([]identity.Socket, error)
	// launch starts a Desktop program detached; tests replace it.
	launch func(program string) (desktopProcess, error)
	// show opens a page in the browser; tests replace it.
	show          func(target string) error
	browserFailed func()
	// continuity checks ComfyUI Desktop's main process before a Desktop
	// arm, and armDesktop sends that arm; tests replace both.
	continuity     func(python int, program string) error
	armDesktop     func(ctx context.Context, program string) error
	openedDesktop  func(started desktopProcess, python int, program string) bool
	startedDesktop desktopProcess
	// unsigned is set once ComfyUI Desktop's program was found unsigned:
	// agents then use the browser page.
	unsigned atomic.Bool
	// idle reports Comfy Desktop open with no process of this install
	// (desktopWatch); installProcesses replaces the process table in tests.
	idle             func(bool)
	installProcesses func() bool

	// folders are the account's %LOCALAPPDATA% and %APPDATA%.
	folders accountFolders
	// phase moves the startup's phase and cause records why it failed
	// (desktop_owned_windows.go); headless is set while the served ComfyUI
	// is a Comfy Desktop install's server OpenAbstractions started.
	phase    func(byte, bool)
	cause    func(string)
	headless atomic.Bool

	mu      sync.Mutex
	served  *listening
	owned   bool
	stopPID int
	stopKey string
}

func (h *host) setCause(cause string) {
	if h.cause != nil {
		h.cause(cause)
	}
}

func newHost(install comfyInstall, report string, graph *nativeGraphBridge, events *nativeEvents) *host {
	h := &host{install: install, report: report, graph: graph, events: events, sockets: identity.Sockets,
		launch: launchDetached, show: showPage, browserFailed: func() {}}
	h.continuity = func(python int, program string) error {
		return desktopContinuity(windowsProcesses{}, verifyProgram, python, program, venvLaunchers(install.Base))
	}
	h.openedDesktop = func(started desktopProcess, python int, program string) bool {
		return desktopOpenedBy(windowsProcesses{}, started, python, program, venvLaunchers(install.Base))
	}
	if graph != nil {
		h.armDesktop = graph.armDesktop
	}
	return h
}

// attachment is the Q answer: the served ComfyUI's port and output folder.
func (h *host) attachment() []byte {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.served == nil {
		return []byte("{}")
	}
	raw, err := json.Marshal(struct {
		Port   int    `json:"port"`
		Output string `json:"output,omitempty"`
	}{h.served.Port, h.served.Output})
	if err != nil || len(raw) > attachmentLimit {
		return []byte("{}")
	}
	return raw
}

func (h *host) serve(r *listening, owned bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.served, h.owned = r, owned
}

// current reads the report and proves it, nil owned for a ComfyUI the
// connector did not start.
func (h *host) current(owned func(int) bool) (listening, error) {
	r, err := readListening(h.report)
	if err != nil {
		return listening{}, err
	}
	return r, checkListening(r, h.install.Base, h.sockets, owned)
}

// waitListening reads the report until it proves a listening ComfyUI, the
// started process exits (exited closes) or ctx ends.
func (h *host) waitListening(ctx context.Context, exited <-chan struct{}, owned func(int) bool, tick ...func()) (listening, error) {
	ready, cancel := context.WithTimeout(ctx, readyLimit)
	defer cancel()
	ticker := time.NewTicker(readyPoll)
	defer ticker.Stop()
	for {
		if r, err := h.current(owned); err == nil {
			return r, nil
		}
		for _, each := range tick {
			each()
		}
		select {
		case <-ready.Done():
			if ctx.Err() == nil {
				return listening{}, fmt.Errorf("ComfyUI did not report that it listens within %s", readyLimit)
			}
			return listening{}, ctx.Err()
		case <-exited:
			return listening{}, errors.New("ComfyUI exited before it listened")
		case <-ticker.C:
		}
	}
}

// run is the host's one startup: it serves this ComfyUI, and once that
// ComfyUI is no longer served, the adapter's graph reader ends.
func (h *host) run(ctx context.Context, started func()) error {
	defer h.graph.endAdapter()
	return h.start(ctx, started)
}

// start serves this ComfyUI until it ends or ctx ends.
func (h *host) start(ctx context.Context, started func()) error {
	if r, err := h.current(nil); err == nil {
		if h.install.Kind == kindComfyDesktop {
			if rec, ok := h.ownStarted(r.PID); ok {
				h.events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI's server that OpenAbstractions started listens on 127.0.0.1:%d; OpenAbstractions attaches to it", r.Port), nil)
				return h.serveOwnedDesktop(ctx, r, rec, nil, started)
			}
		}
		h.events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI already listens on 127.0.0.1:%d; OpenAbstractions attaches to it", r.Port), nil)
		return h.attach(ctx, r, started, true)
	}
	if h.install.Kind == kindPortable && h.install.Program != "" {
		return h.runPortable(ctx, started)
	}
	switch {
	case h.install.Kind == kindComfyDesktop && h.install.Program != "":
		return h.runComfyDesktop(ctx, started)
	case h.install.Program != "":
		h.events.log(nativeLevelInfo, "ComfyUI is starting: "+h.install.Program, nil)
		if err := h.startDesktop(); err != nil {
			return fmt.Errorf("ComfyUI could not be started: %w", err)
		}
		r, err := h.waitListening(ctx, nil, nil)
		if err != nil {
			return err
		}
		return h.attach(ctx, r, started, false)
	default:
		return errStartYourself
	}
}

func (h *host) startDesktop() error {
	started, err := h.launch(h.install.Program)
	if err == nil {
		h.startedDesktop = started
	}
	return err
}

// reopen shows the served ComfyUI again.
func (h *host) reopen(ctx context.Context) error {
	h.mu.Lock()
	served, owned := h.served, h.owned
	h.mu.Unlock()
	if served == nil {
		return errors.New("no ComfyUI is served")
	}
	return h.present(ctx, *served, owned, true)
}

// present shows the served ComfyUI. A Desktop this connector launched uses
// its own window. For a separately opened Desktop, reveal creates a fresh
// browser graph page and leaves the existing window detached.
func (h *host) present(ctx context.Context, r listening, owned, reveal bool) error {
	if h.desktop() {
		if !h.openedDesktop(h.startedDesktop, r.PID, h.install.Program) {
			if reveal {
				return h.graph.openBrowser(ctx)
			}
			return nil
		}
		var err error
		if reveal {
			err = h.reveal(r)
		}
		h.admitDesktopWindow(ctx, r)
		return err
	}
	err := h.graph.openBrowser(ctx)
	if err == nil || owned || !reveal || ctx.Err() != nil {
		return err
	}
	h.events.log(nativeLevelInfo, "ComfyUI is shown without its graph page: "+err.Error(), nil)
	return h.reveal(r)
}

// serveChannel serves r's graph channel until ctx ends; done closes then.
func (h *host) serveChannel(ctx context.Context, r listening) (done <-chan struct{}) {
	ended := make(chan struct{})
	go func() {
		defer close(ended)
		if err := h.graph.serveChannel(ctx, r.PID, filepath.Dir(h.report), h.events); err != nil && ctx.Err() == nil {
			h.events.log(nativeLevelWarn, "the ComfyUI graph channel is unavailable: "+err.Error(), nil)
		}
	}()
	return ended
}

// desktop reports a ComfyUI Desktop or Comfy Desktop the connector knows the
// program of: its window is ComfyUI's own.
func (h *host) desktop() bool {
	return (h.install.Kind == kindDesktop || h.install.Kind == kindComfyDesktop) && h.install.Program != "" && !h.headless.Load()
}

// desktopName is the Desktop program's name in log lines: ComfyUI Desktop
// or Comfy Desktop.
func (h *host) desktopName() string {
	if h.install.Kind == kindComfyDesktop {
		return "Comfy Desktop"
	}
	return "ComfyUI Desktop"
}

// admitDesktopWindow sends the window of ComfyUI Desktop or Comfy Desktop a
// Desktop arm, once its main process passed the continuity check. Both start
// ComfyUI through this folder's venv launcher (README, "Desktop window
// admission").
func (h *host) admitDesktopWindow(ctx context.Context, r listening) {
	if (h.install.Kind != kindDesktop && h.install.Kind != kindComfyDesktop) || h.armDesktop == nil || h.headless.Load() {
		return
	}
	name := h.desktopName()
	if !regularFile(h.install.Program) {
		h.events.log(nativeLevelWarn, name+"'s window gets no graph context: its program is gone", nil)
		return
	}
	if err := h.continuity(r.PID, h.install.Program); errors.Is(err, errDesktopUnsigned) {
		// One log line, and the adapter learns it once: app_read says so and
		// app_reveal opens the browser page.
		if h.unsigned.CompareAndSwap(false, true) {
			reason := "desktop_unsigned"
			if h.install.Kind == kindComfyDesktop {
				reason = "comfy_desktop_unsigned"
			}
			h.events.log(nativeLevelWarn, name+" isn't signed, so agents use the browser page", nil)
			if h.graph != nil {
				_ = h.graph.toAdapterFrame([]byte(`{"type":"notice","reason":"` + reason + `"}`))
			}
		}
		return
	} else if err != nil {
		h.events.log(nativeLevelWarn, name+"'s window gets no graph context: "+err.Error(), nil)
		return
	}
	if err := h.armDesktop(ctx, h.install.Program); err != nil && ctx.Err() == nil {
		h.events.log(nativeLevelWarn, name+"'s window gets no graph context: "+err.Error(), nil)
	}
}

// browserPage answers the adapter's G request: for an unsigned ComfyUI
// Desktop or Comfy Desktop it opens the graph page in the browser ('A', or
// 'U' when that failed); any other ComfyUI answers 'N'.
func (h *host) browserPage() byte {
	if !h.unsigned.Load() {
		return 'N'
	}
	ctx, cancel := context.WithTimeout(context.Background(), channelWait+10*time.Second)
	defer cancel()
	if err := h.graph.openBrowser(ctx); err != nil {
		h.events.log(nativeLevelWarn, h.desktopName()+"'s browser page did not open: "+err.Error(), nil)
		return 'U'
	}
	return 'A'
}

// reveal shows an attached ComfyUI: Desktop's own window, which starting its
// program again brings to the front, else its page.
func (h *host) reveal(r listening) error {
	if h.desktop() {
		_, err := h.launch(h.install.Program)
		return err
	}
	return h.show("http://127.0.0.1:" + strconv.Itoa(r.Port) + "/")
}

// attach serves a ComfyUI the connector did not start until its process
// exits or ctx ends; it never stops that ComfyUI.
func (h *host) attach(ctx context.Context, r listening, started func(), reveal bool) error {
	process, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(r.PID))
	if err != nil {
		return fmt.Errorf("the listening ComfyUI process %d: %w", r.PID, err)
	}
	defer windows.CloseHandle(process)
	// The held handle keeps the process id; prove it is still the listener.
	if err := checkListening(r, h.install.Base, h.sockets, nil); err != nil {
		return err
	}
	channelCtx, stopChannel := context.WithCancel(ctx)
	channelDone := h.serveChannel(channelCtx, r)
	defer func() { stopChannel(); <-channelDone }()
	h.graph.listening(r.Port, func() error { return checkListening(r, h.install.Base, h.sockets, nil) })
	defer h.graph.listening(0, nil)
	h.serve(&r, false)
	defer h.serve(nil, false)
	started()
	if h.install.Kind == kindComfyDesktop && h.folders.Roaming != "" {
		h.rememberDesktopLaunch()
	}
	if err := h.present(ctx, r, false, reveal); err != nil && ctx.Err() == nil {
		if reveal {
			h.browserFailed()
			h.events.log(nativeLevelWarn, "ComfyUI runs, but it could not be shown: "+err.Error(), nil)
		} else {
			h.events.log(nativeLevelInfo, "ComfyUI runs without its graph page: "+err.Error(), nil)
		}
	}
	h.events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI serves OpenAbstractions on 127.0.0.1:%d", r.Port), nil)
	for {
		event, err := windows.WaitForSingleObject(process, 500)
		if err != nil {
			return &uncertainOwnedExit{err}
		}
		if event == windows.WAIT_OBJECT_0 {
			h.events.log(nativeLevelInfo, "ComfyUI exited", nil)
			return nil
		}
		if ctx.Err() != nil {
			return nil
		}
	}
}

// runPortable starts the portable ComfyUI as the connector's own process
// tree, serves it and stops it when ctx ends.
func (h *host) runPortable(ctx context.Context, started func()) error {
	output, err := newNativeOutputTail(os.Stderr)
	if err != nil {
		return err
	}
	defer output.close()
	stopKey := agentStopKey()
	settings := supervision.Options{Executable: h.install.Program, Args: h.install.Args, Dir: h.install.Dir,
		Env:    mergeEnvironment(os.Environ(), []string{agentStopEnvironment + "=" + stopKey}),
		Stdout: output.write, Stderr: output.write, ForceTimeout: 10 * time.Second}
	began := time.Now()
	h.events.log(nativeLevelInfo, "ComfyUI is starting: "+h.install.Program+" -s "+filepath.Join(h.install.Base, "main.py"), nil)
	p, err := supervision.Start(ctx, settings)
	output.closeWrite()
	if err != nil {
		return fmt.Errorf("ComfyUI could not be started: %w", err)
	}
	var code int
	var waitErr error
	exited := make(chan struct{})
	go func() { code, waitErr = p.Wait(); close(exited) }()
	stop := func() {
		_ = p.Stop()
		<-exited
	}
	failed := func(what string, err error) error {
		attrs := map[string]string{"error": err.Error()}
		message := what + ": " + err.Error()
		if tail := output.text(); tail != "" {
			attrs["output"] = tail
			message += "\nLast output:\n" + tail
		}
		h.events.log(nativeLevelError, message, attrs)
		return err
	}
	owned := identity.OwnedByTree(p.Contains)
	r, err := h.waitListening(ctx, exited, owned)
	if err != nil {
		stop()
		if waitErr != nil {
			return &uncertainOwnedExit{waitErr}
		}
		if ctx.Err() != nil {
			return nil
		}
		return failed("ComfyUI did not start", err)
	}
	h.events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI listens on 127.0.0.1:%d, %d s after the start", r.Port, int(time.Since(began).Seconds())), nil)
	channelCtx, stopChannel := context.WithCancel(ctx)
	channelDone := h.serveChannel(channelCtx, r)
	defer func() { stopChannel(); <-channelDone }()
	h.graph.listening(r.Port, func() error { return checkListening(r, h.install.Base, h.sockets, owned) })
	defer h.graph.listening(0, nil)
	h.serve(&r, true)
	defer h.serve(nil, false)
	if err := h.recordPortableStart(p, r); err != nil {
		h.events.log(nativeLevelWarn, "OpenAbstractions could not record the portable server it started: "+err.Error(), nil)
	} else {
		clearAgentStop := h.bindAgentStop(r.PID, stopKey)
		defer clearAgentStop()
	}
	started()
	if err := h.present(ctx, r, true, false); err != nil && ctx.Err() == nil {
		h.browserFailed()
		h.events.log(nativeLevelWarn, "ComfyUI runs, but its browser page did not open: "+err.Error(), nil)
	}
	select {
	case <-exited:
	case <-ctx.Done():
		stop()
	}
	if waitErr != nil {
		return &uncertainOwnedExit{waitErr}
	}
	if ctx.Err() != nil {
		return nil
	}
	if code != 0 {
		return failed("ComfyUI stopped", fmt.Errorf("ComfyUI exited with code %d", code))
	}
	h.events.log(nativeLevelInfo, "ComfyUI exited", nil)
	return nil
}

// launchDetached starts a Desktop program as the person's own: outside the
// connector's job when the job allows it, with no console.
func launchDetached(program string) (desktopProcess, error) {
	start := func(flags uint32) (desktopProcess, error) {
		cmd := exec.Command(program)
		cmd.Dir = filepath.Dir(program)
		cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: flags}
		if err := cmd.Start(); err != nil {
			return desktopProcess{}, err
		}
		// Keep the process handle returned by Start until creation time is read.
		pid := cmd.Process.Pid
		created, _ := (windowsProcesses{}).created(pid)
		return desktopProcess{pid: pid, created: created}, cmd.Process.Release()
	}
	detached := uint32(windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS)
	if process, err := start(detached | windows.CREATE_BREAKAWAY_FROM_JOB); err == nil {
		return process, nil
	}
	return start(detached)
}

// adapterSettings are the adapter's start: this ComfyUI's Python running
// oa_native_host.py with the service-issued instance, its four private pipe
// handles, the application manifest and the runtime's application-model
// library.
func adapterSettings(install comfyInstall, instance string, handles [4]uintptr, manifest []byte, library string, env []string) (supervision.Options, error) {
	if install.Python == "" {
		return supervision.Options{}, errors.New("this ComfyUI has no Python the connector knows; start ComfyUI once with OpenAbstractions, then open it again")
	}
	args := []string{"-I", "-S", "-B", filepath.Join(install.Node, adapterMember), instance}
	for _, handle := range handles {
		args = append(args, strconv.FormatUint(uint64(handle), 10))
	}
	args = append(args, string(manifest), library)
	return supervision.Options{Executable: install.Python, Args: args, Env: env, Dir: install.Node,
		Stdin: os.Stdin, Stdout: os.Stdout, Stderr: os.Stderr, Inherit: handles[:], ForceTimeout: 10 * time.Second}, nil
}

// runHost is owned-host: one comfyui application instance for the runtime.
func runHost(ctx context.Context) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	local, roaming, err := layout.WindowsAccountFolders(layout.Current())
	if err != nil {
		return err
	}
	install, err := findInstall(executable, accountFolders{Local: local, Roaming: roaming})
	if err != nil {
		return err
	}
	library, err := runtimeLibrary(appmodelLibraryName)
	if err != nil {
		return err
	}
	presenceCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	presence, err := announceNative(presenceCtx)
	cancel()
	if err != nil {
		return err
	}
	lifetime, retire := context.WithCancel(ctx)
	defer retire()
	var withdrawal sync.Once
	withdraw := func() {
		withdrawal.Do(func() {
			callCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_, _ = presence.withdraw(callCtx, "comfyui", presence.instance)
		})
	}
	defer withdraw()
	graph, err := newNativeGraphBridge()
	if err != nil {
		return err
	}
	defer graph.close()
	events := newNativeEvents()
	h := newHost(install, listeningFile(local), graph, events)
	h.folders = accountFolders{Local: local, Roaming: roaming}
	startup := newNativeStartup(h.run)
	startup.agentStop = h.requestAgentStop
	h.browserFailed = startup.browserFailed
	h.idle = startup.desktopIdle
	h.phase, h.cause = startup.setPhase, startup.setCause
	startup.reopen = h.reopen
	startup.events = events
	fromAdapter, adapterWrite, err := os.Pipe()
	if err != nil {
		return err
	}
	defer fromAdapter.Close()
	defer adapterWrite.Close()
	adapterRead, toAdapter, err := os.Pipe()
	if err != nil {
		return err
	}
	defer adapterRead.Close()
	defer toAdapter.Close()
	for _, file := range []*os.File{adapterRead, adapterWrite} {
		if err = windows.SetHandleInformation(windows.Handle(file.Fd()), windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT); err != nil {
			return err
		}
	}
	roots, err := layout.Resolve(layout.Current())
	if err != nil {
		return err
	}
	if err := os.MkdirAll(roots.Tmp, 0o700); err != nil {
		return err
	}
	env, err := (supervision.CleanEnvironment{Temp: roots.Tmp, Set: []string{"PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1"}}).Build()
	if err != nil {
		return err
	}
	handles := [4]uintptr{adapterRead.Fd(), adapterWrite.Fd(), graph.adapterRead.Fd(), graph.adapterWrite.Fd()}
	settings, err := adapterSettings(install, presence.instance, handles, applicationManifest, library, env)
	if err != nil {
		return err
	}
	p, err := supervision.Start(ctx, settings)
	if err != nil {
		return err
	}
	_ = adapterRead.Close()
	_ = adapterWrite.Close()
	_ = graph.adapterRead.Close()
	_ = graph.adapterWrite.Close()
	adapterLifetime, cancelAdapter := context.WithCancel(ctx)
	defer cancelAdapter()
	// selfEnded is true once the host ends itself: its lease lapsed, or its
	// ComfyUI failed or exited, and the next open starts a fresh host.
	var selfEnded atomic.Bool
	startup.ended = func() {
		// The grace lets a command waiting on the startup read its reason.
		time.AfterFunc(endedGrace, func() {
			selfEnded.Store(true)
			endLapsedHost(retire, startup, cancelAdapter)
		})
	}
	presenceDone := make(chan struct{})
	go func() {
		defer close(presenceDone)
		defer withdraw()
		presence.keep(lifetime, 10*time.Second, func() {
			selfEnded.Store(true)
			endLapsedHost(retire, startup, cancelAdapter)
		})
	}()
	stopDone := make(chan struct{})
	go func() {
		defer close(stopDone)
		<-adapterLifetime.Done()
		_ = p.Stop()
		_ = fromAdapter.Close()
		_ = toAdapter.Close()
	}()
	controlDone := make(chan struct{})
	controlledStop := false
	go func() {
		defer close(controlDone)
		controlledStop = nativeControlWith(fromAdapter, toAdapter, startup, retire, graph.acknowledgeArm, h.attachment, h.browserPage) == nil
		retire()
		_ = p.Stop()
	}()
	code, waitErr := p.Wait()
	retire()
	cancelAdapter()
	<-stopDone
	<-controlDone
	<-presenceDone
	withdraw()
	if err = finishNativeMediation(ctx, startup, waitErr, func() error { return nil }); err != nil {
		return err
	}
	if code != 0 && !controlledStop && !selfEnded.Load() {
		return errors.New("the ComfyUI adapter failed")
	}
	return nil
}
