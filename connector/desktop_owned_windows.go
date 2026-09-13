//go:build windows

package main

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/openabstractions/abstraction-facade/go/supervision"
	"golang.org/x/sys/windows"
)

// comfyApplication names this app's started record.
const comfyApplication = "comfyui"

// desktopEvery is how often a served OA server looks for Desktop's run of
// the install.
var desktopEvery = 2 * time.Second

// desktopTakeoverWait is how long after OA's server ended by another hand
// the connector looks for Desktop's run of the install.
var desktopTakeoverWait = 10 * time.Second

// setPhase moves the startup to a starting phase; force also moves a
// running one (Desktop took the install over).
func (h *host) setPhase(phase byte, force bool) {
	if h.phase != nil {
		h.phase(phase, force)
	}
}

func (h *host) desktopState() string { return desktopStateFolder(h.folders.Roaming) }

// liveStarted is OA's started record of this app while its launcher or
// server still runs with the recorded creation time.
func (h *host) liveStarted() (startedRecord, bool) {
	if h.folders.Local == "" {
		return startedRecord{}, false
	}
	r, ok := readStarted(h.folders.Local, comfyApplication)
	if !ok {
		return startedRecord{}, false
	}
	for _, p := range r.Processes {
		if created, alive := liveProcess(p.PID); alive && strconv.FormatUint(created, 10) == p.Created {
			return r, true
		}
	}
	return startedRecord{}, false
}

// ownStarted is the started record when it names pid with its creation
// time, or names pid's parent: the server a launcher started before an
// earlier connector recorded it, which is then recorded too.
func (h *host) ownStarted(pid int) (startedRecord, bool) {
	r, ok := h.liveStarted()
	if !ok {
		return startedRecord{}, false
	}
	created, alive := liveProcess(pid)
	if !alive {
		return startedRecord{}, false
	}
	self := startedProcess{PID: pid, Created: strconv.FormatUint(created, 10)}
	if r.names(self.PID, self.Created) {
		return r, true
	}
	parent, ok := (windowsProcesses{}).parent(pid)
	if !ok || len(r.Processes) != 1 || r.Processes[0].PID != parent {
		return startedRecord{}, false
	}
	if born, alive := liveProcess(parent); !alive || strconv.FormatUint(born, 10) != r.Processes[0].Created || born > created {
		return startedRecord{}, false
	}
	previous := r
	r.Processes = append(r.Processes, self)
	if err := updateStarted(h.folders.Local, previous, r); err != nil {
		h.events.log(nativeLevelWarn, "OpenAbstractions could not record the server it started: "+err.Error(), nil)
		return startedRecord{}, false
	}
	return r, true
}

// serverLogFile is the started server's output in OA's ComfyUI folder.
func (h *host) serverLogFile() string {
	return filepath.Join(filepath.Dir(h.report), "server.log")
}

// runComfyDesktop serves a Comfy Desktop install that serves no ComfyUI yet.
func (h *host) runComfyDesktop(ctx context.Context, started func()) error {
	state := h.desktopState()
	inst, instErr := findDesktopInstallation(state, h.install.Base)
	ready, cancel := context.WithTimeout(ctx, readyLimit)
	defer cancel()
	ticker := time.NewTicker(readyPoll)
	defer ticker.Stop()
	said := byte(0)
	say := func(phase byte, line string) {
		if said != phase {
			said = phase
			h.events.log(nativeLevelInfo, line, nil)
		}
	}
	output := newServerLog(h.serverLogFile())
	for {
		if r, err := h.current(nil); err == nil {
			if rec, ok := h.ownStarted(r.PID); ok {
				return h.serveOwnedDesktop(ctx, r, rec, nil, started)
			}
			return h.attach(ctx, r, started, false)
		}
		if _, ok := h.liveStarted(); ok {
			// OA's server from an earlier start is still starting.
			h.setPhase(output.phase(), false)
			say('S', "ComfyUI's server that OpenAbstractions started is still starting")
		} else if instErr == nil && desktopRunning(state, inst) {
			h.setPhase('Y', false)
			say('Y', "ComfyUI is already starting in Comfy Desktop; OpenAbstractions starts nothing and attaches once it listens")
		} else if programRuns(h.install.Program) {
			h.setPhase('D', false)
			say('D', "Comfy Desktop is open, so OpenAbstractions does not start ComfyUI beside it; it attaches once the person starts ComfyUI there")
		} else {
			break
		}
		select {
		case <-ready.Done():
			if ctx.Err() != nil {
				return nil
			}
			if said == 'D' {
				return errors.New("Comfy Desktop is open, but no ComfyUI install ran in it; start ComfyUI in Comfy Desktop, or close Comfy Desktop, then open it again")
			}
			return fmt.Errorf("ComfyUI did not report that it listens within %s", readyLimit)
		case <-ticker.C:
		}
	}
	launch, err := chooseDesktopLaunch(h.folders.Local, state, h.install.Base, programVersion(h.install.Program))
	if err != nil {
		h.events.log(nativeLevelInfo, "OpenAbstractions does not start this ComfyUI itself: "+err.Error(), nil)
		return h.launchDesktopProgram(ctx, started)
	}
	return h.runOwnedDesktop(ctx, launch, started)
}

// desktopRunning is desktopRunsInstall over the live process table.
func desktopRunning(state string, inst desktopInstallation) bool {
	_, ok := desktopRunsInstall(state, inst, liveProcess, nil)
	return ok
}

// launchDesktopProgram is the person's start: Comfy Desktop opens, and the
// connector attaches once the person starts ComfyUI in it.
func (h *host) launchDesktopProgram(ctx context.Context, started func()) error {
	h.events.log(nativeLevelInfo, "ComfyUI is starting: "+h.install.Program, nil)
	launched := time.Now()
	if err := h.startDesktop(); err != nil {
		return fmt.Errorf("ComfyUI could not be started: %w", err)
	}
	r, err := h.waitListening(ctx, nil, nil, h.desktopWatch(launched))
	if err != nil {
		if ctx.Err() == nil && !h.installRunning() {
			return errors.New("Comfy Desktop is open, but no ComfyUI install ran in it; start ComfyUI in Comfy Desktop, then open it again")
		}
		return err
	}
	return h.attach(ctx, r, started, false)
}

// runOwnedDesktop starts launch and serves it once it listens.
func (h *host) runOwnedDesktop(ctx context.Context, launch desktopLaunch, started func()) error {
	logFile := h.serverLogFile()
	h.events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI's server is starting with Comfy Desktop's command (%s): %s %s",
		launch.Source, launch.Program, strings.Join(launch.Args, " ")), map[string]string{"log": logFile})
	stopKey := agentStopKey()
	launch.Env = mergeEnvironment(launch.Env, []string{agentStopEnvironment + "=" + stopKey})
	srv, err := startDesktopServer(launch, logFile)
	if err != nil {
		return fmt.Errorf("ComfyUI's server could not be started: %w", err)
	}
	defer srv.close()
	rec := startedRecord{Application: comfyApplication, Title: "ComfyUI", StartedUnixMs: time.Now().UnixMilli(),
		Port: launch.Port, Processes: []startedProcess{srv.launcher}}
	if err := writeStarted(h.folders.Local, rec); err != nil {
		h.events.log(nativeLevelWarn, "OpenAbstractions could not record the server it started: "+err.Error(), nil)
	}
	h.setPhase('L', false)
	owned := func(pid int) bool {
		if pid == srv.launcher.PID {
			return true
		}
		parent, ok := (windowsProcesses{}).parent(pid)
		return ok && parent == srv.launcher.PID
	}
	began := time.Now()
	r, err := h.waitListening(ctx, srv.exited, owned, func() { h.setPhase(srv.log.phase(), false) })
	if err != nil {
		if ctx.Err() != nil {
			// The server keeps running; the next open attaches to it.
			return nil
		}
		cause := err.Error()
		reason := "desktop:listener_timeout"
		select {
		case <-srv.exited:
			removeStarted(h.folders.Local, rec)
			reason = "desktop:early_exit"
			cause = "ComfyUI's server exited"
			if line := srv.log.lastLine(); line != "" {
				cause += ": " + line
			}
		default:
		}
		h.setCause(reason)
		h.events.log(nativeLevelError, cause, map[string]string{"log": logFile})
		return errors.New(cause)
	}
	h.events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI's server listens on 127.0.0.1:%d, %d s after the start", r.Port, int(time.Since(began).Seconds())), nil)
	previous := rec
	if created, ok := liveProcess(r.PID); ok && r.PID != srv.launcher.PID {
		rec.Processes = append(rec.Processes, startedProcess{PID: r.PID, Created: strconv.FormatUint(created, 10)})
	}
	rec.Port = r.Port
	if err := updateStarted(h.folders.Local, previous, rec); err != nil {
		h.events.log(nativeLevelWarn, "OpenAbstractions could not record the server it started: "+err.Error(), nil)
	} else {
		clearAgentStop := h.bindAgentStop(r.PID, stopKey)
		defer clearAgentStop()
	}
	return h.serveOwnedDesktop(ctx, r, rec, srv, started)
}

// serveOwnedDesktop serves OA's server of a Comfy Desktop install until it
// exits or ctx ends; it keeps running when ctx ends. Its page is the browser
// graph page. When Comfy Desktop starts the install, the server is asked to
// stop once idle and the connector attaches to Desktop's.
func (h *host) serveOwnedDesktop(ctx context.Context, r listening, rec startedRecord, srv *ownedServer, started func()) error {
	process, err := windows.OpenProcess(windows.SYNCHRONIZE|windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(r.PID))
	if err != nil {
		return fmt.Errorf("the listening ComfyUI process %d: %w", r.PID, err)
	}
	defer windows.CloseHandle(process)
	owned := func(pid int) bool {
		created, ok := liveProcess(pid)
		return ok && rec.names(pid, strconv.FormatUint(created, 10))
	}
	if err := checkListening(r, h.install.Base, h.sockets, owned); err != nil {
		return err
	}
	h.headless.Store(true)
	defer h.headless.Store(false)
	channelCtx, stopChannel := context.WithCancel(ctx)
	channelDone := h.serveChannel(channelCtx, r)
	h.graph.listening(r.Port, func() error { return checkListening(r, h.install.Base, h.sockets, owned) })
	h.serve(&r, true)
	started()
	if err := h.present(ctx, r, true, false); err != nil && ctx.Err() == nil {
		h.browserFailed()
		h.events.log(nativeLevelWarn, "ComfyUI runs, but its browser page did not open: "+err.Error(), nil)
	}
	h.events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI serves OpenAbstractions on 127.0.0.1:%d (started by OpenAbstractions)", r.Port), nil)
	state := h.desktopState()
	inst, instErr := findDesktopInstallation(state, h.install.Base)
	ours := func(pid int) bool {
		for _, p := range rec.Processes {
			if p.PID == pid {
				return true
			}
		}
		return false
	}
	var takeover time.Time
	var checked time.Time
	stopRefused := false
	exited := false
	for !exited {
		event, err := windows.WaitForSingleObject(process, 500)
		if err != nil {
			stopChannel()
			<-channelDone
			h.graph.listening(0, nil)
			h.serve(nil, false)
			return &uncertainOwnedExit{err}
		}
		if event == windows.WAIT_OBJECT_0 {
			exited = true
			continue
		}
		if ctx.Err() != nil {
			stopChannel()
			<-channelDone
			h.graph.listening(0, nil)
			h.serve(nil, false)
			return nil
		}
		if instErr != nil || time.Since(checked) < desktopEvery {
			continue
		}
		checked = time.Now()
		if stopRefused {
			continue
		}
		if takeover.IsZero() {
			if _, ok := desktopRunsInstall(state, inst, liveProcess, ours); ok {
				created, alive := liveProcess(r.PID)
				if !alive {
					continue
				}
				target := startedProcess{PID: r.PID, Created: strconv.FormatUint(created, 10)}
				requested := time.Now()
				if err := requestStop(ctx, h.folders.Local, rec, target, requested); err != nil {
					stopRefused = true
					h.events.log(nativeLevelWarn, "the stop request could not be written: "+err.Error(), nil)
					continue
				}
				takeover = requested
				h.events.log(nativeLevelInfo, "Comfy Desktop started this ComfyUI; OpenAbstractions stops its own server once its queue is empty", nil)
			}
		} else if time.Since(takeover) > startedStopGrace {
			err := supervision.ForceStopStartRecord(ctx, startedFile(h.folders.Local, comfyApplication),
				stopFile(h.folders.Local, comfyApplication), supervision.StartRecord(rec), stopRecorded)
			if err != nil {
				stopRefused = true
				h.events.log(nativeLevelWarn, "the stop request could not be confirmed: "+err.Error(), nil)
			}
		}
	}
	stopChannel()
	<-channelDone
	h.graph.listening(0, nil)
	h.serve(nil, false)
	h.headless.Store(false)
	var code uint32
	_ = windows.GetExitCodeProcess(process, &code)
	stopped := supervision.HasStartStopRequest(stopFile(h.folders.Local, comfyApplication), supervision.StartRecord(rec))
	removeStarted(h.folders.Local, rec)
	tookOver := !takeover.IsZero()
	if !tookOver && !stopped && code != 0 && instErr == nil {
		// Desktop's "Stop process and retry" ends OA's server, then starts its own.
		for deadline := time.Now().Add(desktopTakeoverWait); !tookOver && time.Now().Before(deadline) && ctx.Err() == nil; time.Sleep(500 * time.Millisecond) {
			tookOver = desktopRunning(state, inst)
		}
	}
	if tookOver {
		h.events.log(nativeLevelInfo, "ComfyUI's server stopped; Comfy Desktop runs ComfyUI now and OpenAbstractions attaches to it", nil)
		h.setPhase('Y', true)
		next, err := h.waitListening(ctx, nil, nil)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		return h.attach(ctx, next, started, false)
	}
	if code == 0 || stopped {
		h.events.log(nativeLevelInfo, "ComfyUI's server stopped", nil)
		return nil
	}
	cause := "ComfyUI's server exited"
	if srv != nil {
		if line := srv.log.lastLine(); line != "" {
			cause += ": " + line
		}
	} else if line := newServerLog(h.serverLogFile()).lastLine(); line != "" {
		cause += ": " + line
	}
	h.setCause("desktop:server_exit")
	h.events.log(nativeLevelError, cause, nil)
	return errors.New(cause)
}

// rememberDesktopLaunch keeps the command Desktop runs the install with, read
// from the process its comfy-procs record names.
func (h *host) rememberDesktopLaunch() {
	state := h.desktopState()
	inst, err := findDesktopInstallation(state, h.install.Base)
	if err != nil {
		return
	}
	settings, err := readDesktopSettings(state)
	if err != nil {
		return
	}
	record, ok := desktopRunsInstall(state, inst, liveProcess, nil)
	if !ok || record.SessionKey == "" || record.ChildStartTime == nil {
		return
	}
	argv, err := processCommandLine(record.ChildPID)
	if err != nil || len(argv) < 3 {
		return
	}
	if !regularFile(argv[0]) || !sameFile(argv[0], venvPython(inst.InstallPath)) {
		return
	}
	// The process must still be the recorded one after its command line was read.
	if created, ok := liveProcess(record.ChildPID); !ok || strconv.FormatUint(created, 10) != *record.ChildStartTime {
		return
	}
	if err := writeRemembered(h.folders.Local, inst, settings, argv[1:], time.Now()); err != nil {
		h.events.log(nativeLevelWarn, "Comfy Desktop's start of ComfyUI is not remembered: "+err.Error(), nil)
		return
	}
	h.events.log(nativeLevelInfo, "OpenAbstractions remembers how Comfy Desktop starts this ComfyUI", nil)
}

func sameFile(a, b string) bool {
	return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}
