//go:build windows

package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"time"
	"unsafe"

	identity "github.com/openabstractions/abstraction-identity"
	"github.com/openabstractions/abstraction-identity/fileid"
	"golang.org/x/sys/windows"
)

// ComfyUI Desktop's own window is admitted to the graph by its process (the
// connector README, "Desktop window admission"). The connector checks the
// Desktop main process, then sends a Desktop arm: its nonce stays in the
// channel, and the node pack's bridge admits the window whose connection
// belongs to desktop_path's process tree.

// processTable answers a process's parent, full image path and creation
// time; ok is false when the process is gone or unreadable.
type processTable interface {
	parent(pid int) (int, bool)
	image(pid int) (string, bool)
	created(pid int) (int64, bool)
}

// errDesktopUnsigned is the continuity check's answer for a Desktop program
// without a trusted signature: it gets no Desktop arm, and agents use the
// browser page instead.
var errDesktopUnsigned = errors.New("ComfyUI Desktop isn't signed, so agents use the browser page")

// venvLaunchers are this ComfyUI folder's venv launchers, the python.exe and
// pythonw.exe ComfyUI Desktop starts from base\.venv\Scripts.
func venvLaunchers(base string) []string {
	scripts := filepath.Join(base, ".venv", "Scripts")
	return []string{filepath.Join(scripts, "python.exe"), filepath.Join(scripts, "pythonw.exe")}
}

// desktopMain is the ComfyUI Desktop main process that started the served
// ComfyUI Python process. It is the Python process's parent, or, when that
// parent is one of launchers (by full path), the launcher's parent; exactly
// one launcher, a direct child of the main process created after it and
// before the Python process. The main process's image is program, and it was
// created before the Python process.
func desktopMain(table processTable, python int, program string, launchers []string) (int, error) {
	main, ok := table.parent(python)
	if !ok {
		return 0, errors.New("the ComfyUI process has no parent")
	}
	child, childOK := table.created(python)
	if !childOK {
		return 0, errors.New("the ComfyUI process is gone")
	}
	image, ok := table.image(main)
	if ok && launcherPath(image, launchers) {
		hop := main
		hopBorn, hopOK := table.created(hop)
		if main, ok = table.parent(hop); !ok || !hopOK || hopBorn >= child {
			return 0, errors.New("the ComfyUI launcher is no direct child of ComfyUI Desktop")
		}
		image, ok = table.image(main)
		if born, bornOK := table.created(main); !bornOK || born >= hopBorn {
			return 0, errors.New("ComfyUI Desktop's process is newer than the ComfyUI launcher")
		}
	}
	if !ok || !fileid.SamePathName(image, program) {
		return 0, errors.New("ComfyUI was not started by ComfyUI Desktop's program")
	}
	if born, bornOK := table.created(main); !bornOK || born >= child {
		return 0, errors.New("ComfyUI Desktop's process is newer than ComfyUI")
	}
	return main, nil
}

func launcherPath(image string, launchers []string) bool {
	for _, launcher := range launchers {
		if fileid.SamePathName(image, launcher) {
			return true
		}
	}
	return false
}

// desktopProcess is the identity returned by the connector's own start.
type desktopProcess struct {
	pid     int
	created int64
}

// desktopOpenedBy joins the listener's Desktop main to that exact start.
func desktopOpenedBy(table processTable, started desktopProcess, python int, program string, launchers []string) bool {
	if started.pid <= 0 || started.created <= 0 {
		return false
	}
	main, err := desktopMain(table, python, program, launchers)
	if err != nil || main != started.pid {
		return false
	}
	created, ok := table.created(main)
	return ok && created == started.created
}

// desktopContinuity is OA's program-continuity check of the Desktop main
// process: the program it runs carries a code signature Windows trusts. An
// unsigned Desktop program, in a user-writable folder, answers
// errDesktopUnsigned.
func desktopContinuity(table processTable, verify func(string) (identity.Code, error), python int, program string, launchers []string) error {
	if _, err := desktopMain(table, python, program, launchers); err != nil {
		return err
	}
	code, err := verify(program)
	if err != nil {
		return fmt.Errorf("ComfyUI Desktop's signature: %w", err)
	}
	if !code.Trusted {
		return errDesktopUnsigned
	}
	return nil
}

// desktopArmFrame is a Desktop arm for the listener on port: the bridge
// keeps its nonce and admits only desktop_path's process tree.
func desktopArmFrame(generation string, port int, program string) ([]byte, error) {
	var nonce [24]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, err
	}
	return json.Marshal(struct {
		Type        string `json:"type"`
		Generation  string `json:"generation"`
		Nonce       string `json:"nonce"`
		Origin      string `json:"origin"`
		Peer        string `json:"peer"`
		DesktopPath string `json:"desktop_path"`
	}{"arm", generation, base64.RawURLEncoding.EncodeToString(nonce[:]), "http://127.0.0.1:" + strconv.Itoa(port), "desktop", program})
}

// armDesktop sends a Desktop arm once the node connected the graph channel.
func (b *nativeGraphBridge) armDesktop(ctx context.Context, program string) error {
	b.mu.Lock()
	port, check := b.port, b.check
	b.mu.Unlock()
	if check == nil {
		return errors.New("the served ComfyUI does not listen")
	}
	if _, err := b.waitChannel(ctx, channelWait); err != nil {
		return err
	}
	writeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	return armNativeDesktop(writeCtx, b.generation, port, program, b.arm, check)
}

func armNativeDesktop(ctx context.Context, generation string, port int, program string, arm func(context.Context, []byte) error, check func() error) error {
	if err := check(); err != nil {
		return err
	}
	raw, err := desktopArmFrame(generation, port, program)
	if err != nil {
		return err
	}
	if err = arm(ctx, raw); err != nil {
		return errors.New("Desktop window admission unavailable")
	}
	return nil
}

// windowsProcesses reads the live process table.
type windowsProcesses struct{}

func (windowsProcesses) parent(pid int) (int, bool) {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return 0, false
	}
	defer windows.CloseHandle(snapshot)
	var entry windows.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	for err = windows.Process32First(snapshot, &entry); err == nil; err = windows.Process32Next(snapshot, &entry) {
		if int(entry.ProcessID) == pid {
			return int(entry.ParentProcessID), entry.ParentProcessID != 0
		}
	}
	return 0, false
}

func (windowsProcesses) image(pid int) (string, bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return "", false
	}
	defer windows.CloseHandle(h)
	buffer := make([]uint16, windows.MAX_LONG_PATH)
	size := uint32(len(buffer))
	if windows.QueryFullProcessImageName(h, 0, &buffer[0], &size) != nil {
		return "", false
	}
	return windows.UTF16ToString(buffer[:size]), true
}

func (windowsProcesses) created(pid int) (int64, bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return 0, false
	}
	defer windows.CloseHandle(h)
	var creation, exit, kernel, user windows.Filetime
	if windows.GetProcessTimes(h, &creation, &exit, &kernel, &user) != nil {
		return 0, false
	}
	return creation.Nanoseconds(), true
}

// verifyProgram is identity's code-signature verification of a program file.
func verifyProgram(path string) (identity.Code, error) {
	code, _, err := identity.VerifyFile(path, nil)
	return code, err
}
