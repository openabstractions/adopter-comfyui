//go:build windows

package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
	"unsafe"

	"github.com/openabstractions/abstraction-facade/go/supervision"
	"golang.org/x/sys/windows"
)

// Starting a Comfy Desktop install's server (architect ruling 2026-11-12,
// adopters/comfyui/connector/README.md, "OA starts a Comfy Desktop install"; desktop_launch.go chooses the command).
//
// open, for a Comfy Desktop install with no ComfyUI serving:
//   - Desktop runs the install (a live comfy-procs record or port lock):
//     phase Y, nothing starts, and the connector attaches once it listens.
//   - Desktop's own program runs: phase D. OA starts nothing while it runs,
//     since two servers on one install share its user folder and assets
//     database, and Desktop refuses a second copy for that reason. The
//     connector attaches when the person starts ComfyUI there, and starts
//     the server itself once Desktop has closed.
//   - Otherwise the connector starts the install's server headless with
//     Desktop's command, in a job object without kill-on-close, outside the
//     connector's job: it outlives the connector and a runtime restart. Its
//     output goes to OA's server log, the launcher and server are recorded
//     by PID and creation time (started.go), and the phases read L
//     launching, K loading custom nodes, W listening, then R once the
//     connector serves it.
//   - A missing fact keeps the person's start: Comfy Desktop opens on its
//     dashboard as before (decf4a337).
//
// The served server stops only by the Apps page's Stop or when Desktop takes
// the install over: a live comfy-procs record of the install beside OA's
// server makes the connector ask it to stop once idle, then attach to
// Desktop's.

// stillActive is GetExitCodeProcess's code of a running process.
const stillActive = 259

func init() { processImage = windowsProcesses{}.image }

// liveProcess reads pid's creation time as a FILETIME count while it runs.
func liveProcess(pid int) (uint64, bool) {
	if pid <= 0 {
		return 0, false
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return 0, false
	}
	defer windows.CloseHandle(h)
	return handleLiveness(h)
}

func handleLiveness(h windows.Handle) (uint64, bool) {
	var code uint32
	if windows.GetExitCodeProcess(h, &code) != nil || code != stillActive {
		return 0, false
	}
	var creation, exit, kernel, user windows.Filetime
	if windows.GetProcessTimes(h, &creation, &exit, &kernel, &user) != nil {
		return 0, false
	}
	return uint64(creation.HighDateTime)<<32 | uint64(creation.LowDateTime), true
}

// processCommandLine reads pid's command line, split as Windows splits it.
func processCommandLine(pid int) ([]string, error) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return nil, err
	}
	defer windows.CloseHandle(h)
	buffer := make([]byte, 64<<10)
	var size uint32
	if err := windows.NtQueryInformationProcess(h, windows.ProcessCommandLineInformation, unsafe.Pointer(&buffer[0]), uint32(len(buffer)), &size); err != nil {
		return nil, err
	}
	line := (*windows.NTUnicodeString)(unsafe.Pointer(&buffer[0])).String()
	return windows.DecomposeCommandLine(line)
}

// programVersion is program's product version as major.minor.patch, the
// version an Electron app reports; "" when unreadable.
func programVersion(program string) string {
	size, err := windows.GetFileVersionInfoSize(program, nil)
	if err != nil || size == 0 || size > 1<<20 {
		return ""
	}
	data := make([]byte, size)
	if windows.GetFileVersionInfo(program, 0, size, unsafe.Pointer(&data[0])) != nil {
		return ""
	}
	var fixed *windows.VS_FIXEDFILEINFO
	var length uint32
	if windows.VerQueryValue(unsafe.Pointer(&data[0]), `\`, unsafe.Pointer(&fixed), &length) != nil || fixed == nil || length < uint32(unsafe.Sizeof(*fixed)) {
		return ""
	}
	return fmt.Sprintf("%d.%d.%d", fixed.ProductVersionMS>>16, fixed.ProductVersionMS&0xffff, fixed.ProductVersionLS>>16)
}

// programRuns is whether a process runs program, compared without case.
func programRuns(program string) bool {
	if program == "" {
		return false
	}
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return false
	}
	defer windows.CloseHandle(snapshot)
	name := strings.ToLower(filepath.Base(program))
	var entry windows.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	for err = windows.Process32First(snapshot, &entry); err == nil; err = windows.Process32Next(snapshot, &entry) {
		if strings.ToLower(windows.UTF16ToString(entry.ExeFile[:])) != name {
			continue
		}
		if image, ok := (windowsProcesses{}).image(int(entry.ProcessID)); ok && strings.EqualFold(filepath.Clean(image), filepath.Clean(program)) {
			return true
		}
	}
	return false
}

// ownedServer is a server the connector started.
type ownedServer struct {
	launcher startedProcess
	process  *supervision.RetainedProcess
	exited   chan struct{}
	code     uint32
	log      *serverLog
}

// startDesktopServer starts the selected backend in a retained supervision job.
// Its output goes to logFile.
func startDesktopServer(launch desktopLaunch, logFile string) (*ownedServer, error) {
	if err := os.MkdirAll(filepath.Dir(logFile), 0o700); err != nil {
		return nil, err
	}
	out, err := os.OpenFile(logFile, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	defer out.Close()
	process, err := supervision.StartRetained(context.Background(), supervision.Options{
		Executable: launch.Program, Args: launch.Args, Dir: launch.Dir,
		Env: mergeEnvironment(os.Environ(), launch.Env), Stdout: out, Stderr: out,
	})
	if err != nil {
		return nil, fmt.Errorf("start ComfyUI's server outside the connector job: %w", err)
	}
	s := &ownedServer{process: process, exited: make(chan struct{}), log: newServerLog(logFile)}
	created := windows.NsecToFiletime(process.Started().UnixNano())
	filetime := uint64(created.HighDateTime)<<32 | uint64(created.LowDateTime)
	s.launcher = startedProcess{PID: process.PID(), Created: strconv.FormatUint(filetime, 10)}
	go func() {
		code, err := process.Wait()
		if err != nil {
			code = 1
		}
		s.code = uint32(code)
		close(s.exited)
	}()
	return s, nil
}

func (s *ownedServer) close() {
	s.process.Release()
}

// mergeEnvironment is base with set's variables replacing those of the same
// name.
func mergeEnvironment(base, set []string) []string {
	key := func(entry string) string {
		name, _, _ := strings.Cut(entry, "=")
		return strings.ToUpper(name)
	}
	replaced := map[string]bool{}
	for _, entry := range set {
		replaced[key(entry)] = true
	}
	out := make([]string, 0, len(base)+len(set))
	for _, entry := range base {
		if !replaced[key(entry)] {
			out = append(out, entry)
		}
	}
	return append(out, set...)
}

// serverLog follows the started server's output file: its phase and its
// last line.
type serverLog struct {
	file string
	mu   sync.Mutex
	read int64
	last string
	seen map[string]bool
}

func newServerLog(file string) *serverLog { return &serverLog{file: file, seen: map[string]bool{}} }

// serverMarks are the output lines each phase starts with.
var serverMarks = map[string][]string{
	"custom_nodes": {"Prestartup times for custom nodes", "Total VRAM", "custom_nodes", "Import times for custom nodes"},
	"listening":    {"Starting server", "To see the GUI go to"},
}

// poll reads the output written since the last poll, at most 1 MiB.
func (l *serverLog) poll() {
	l.mu.Lock()
	defer l.mu.Unlock()
	f, err := os.Open(l.file)
	if err != nil {
		return
	}
	defer f.Close()
	if _, err := f.Seek(l.read, io.SeekStart); err != nil {
		return
	}
	chunk, _ := io.ReadAll(io.LimitReader(f, 1<<20))
	end := bytes.LastIndexByte(chunk, '\n')
	if end < 0 {
		return
	}
	l.read += int64(end + 1)
	for _, line := range strings.Split(string(chunk[:end]), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		l.last = line
		for phase, marks := range serverMarks {
			for _, mark := range marks {
				if strings.Contains(line, mark) {
					l.seen[phase] = true
				}
			}
		}
	}
}

// phase is L, K or W from the output so far.
func (l *serverLog) phase() byte {
	l.poll()
	l.mu.Lock()
	defer l.mu.Unlock()
	switch {
	case l.seen["listening"]:
		return 'W'
	case l.seen["custom_nodes"]:
		return 'K'
	}
	return 'L'
}

// lastLine is the output's last line, at most 400 bytes; the partial line
// at the end counts once the server has exited.
func (l *serverLog) lastLine() string {
	l.poll()
	l.mu.Lock()
	last := l.last
	l.mu.Unlock()
	if raw, err := readBounded(l.file, 8<<20); err == nil {
		if tail := strings.TrimSpace(string(raw[min(int(l.read), len(raw)):])); tail != "" {
			lines := strings.Split(tail, "\n")
			last = strings.TrimSpace(lines[len(lines)-1])
		}
	}
	if len(last) > 400 {
		last = last[:400]
	}
	return last
}

// stopRecorded stops each recorded process that still runs with its recorded
// creation time.
func stopRecorded(processes []startedProcess) {
	for _, p := range processes {
		h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION|windows.PROCESS_TERMINATE, false, uint32(p.PID))
		if err != nil {
			continue
		}
		if created, ok := handleLiveness(h); ok && strconv.FormatUint(created, 10) == p.Created {
			_ = windows.TerminateProcess(h, 1)
		}
		windows.CloseHandle(h)
	}
}

// waitRecorded waits up to grace for every recorded process to end.
func waitRecorded(ctx context.Context, processes []startedProcess, grace time.Duration) bool {
	deadline := time.Now().Add(grace)
	for {
		alive := false
		for _, p := range processes {
			if created, ok := liveProcess(p.PID); ok && strconv.FormatUint(created, 10) == p.Created {
				alive = true
			}
		}
		if !alive {
			return true
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			return false
		}
		time.Sleep(500 * time.Millisecond)
	}
}

// errDesktopTookOver ends serving OA's server once Desktop runs the install.
var errDesktopTookOver = errors.New("Comfy Desktop took the install over")
