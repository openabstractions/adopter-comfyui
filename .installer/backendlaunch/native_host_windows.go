//go:build windows

package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unicode/utf8"
	"unsafe"

	facade "github.com/openabstractions/abstraction-facade/go"
	wire "github.com/openabstractions/abstraction-facade/go-core/go/abstraction/facade"
	applications "github.com/openabstractions/abstraction-facade/go/abstraction/facade/applications"
	"github.com/openabstractions/abstraction-facade/go/programfiles"
	"github.com/openabstractions/abstraction-facade/go/supervision"
	identity "github.com/openabstractions/abstraction-identity"
	"github.com/openabstractions/abstraction-identity/fileid"
	"github.com/openabstractions/abstraction-identity/go/privatedir"
	"github.com/openabstractions/abstraction-identity/listen"
	"github.com/openabstractions/abstraction-provider-kit/go/enginelaunch"
	"github.com/openabstractions/abstraction-storage/go/layout"
	"golang.org/x/sys/windows"
)

const nativeAdapterMember = "oa_native_host.py"
const controlLimit = 16 << 20

// The adopter's existing private relay carries bytes and cancellation only.
// The shared IPC library owns framing, native peer proof and runtime discovery.
const relayLineLimit = 4 << 20
const relayDataLimit = 2<<20 + 4

type relayExpectation struct {
	Size, Version, Kind, Reserved uint32
	Principal                     *byte
	PrincipalLength               uintptr
	Program                       *byte
	ProgramLength                 uintptr
}

type relayObject struct {
	mu       sync.RWMutex
	value    uintptr
	token    *relayObject
	refs     int
	parent   *relayObject
	children map[*relayObject]bool
	signaled bool
}

type relayStatusError struct {
	status uintptr
	reason string
}

func (e relayStatusError) Error() string { return e.reason }

type relayNative struct {
	dll              windows.Handle
	release          func() error
	procs            map[string]uintptr
	mu               sync.Mutex
	next             int64
	handles, signals map[int64]*relayObject
	tokens           map[uintptr]*relayObject
	closing          bool
}

func openRelayNative(ctx context.Context) (*relayNative, error) {
	backendPin, err := nativePin(nativeBackendPin, metadataLimit)
	if err != nil {
		return nil, err
	}
	controlPin, err := nativePin(nativeControlPin, controlLimit)
	if err != nil {
		return nil, err
	}
	executable, err := os.Executable()
	if err != nil {
		return nil, err
	}
	paths, err := nativePaths(ctx, executable, backendPin)
	if err != nil {
		return nil, err
	}
	control, err := controlTree(ctx, filepath.Join(filepath.Dir(executable), "oa-control"), filepath.Join(paths.state, "control.record"), controlPin)
	if err != nil {
		return nil, err
	}
	return openRelayNativeControl(ctx, control)
}

func openRelayNativeControl(ctx context.Context, control programfiles.Tree) (*relayNative, error) {
	const member = "Lib/site-packages/abstraction/ipc/_native/abstraction_ipc.dll"
	found := false
	for _, file := range control.Manifest.Files {
		if file.Path == member {
			found = true
		}
	}
	if !found {
		return nil, errors.New("sealed IPC component is missing")
	}
	hold, err := programfiles.NewVerifier(programfiles.Options{}).Launch(ctx, control)
	if err != nil {
		return nil, err
	}
	// The held, source-pinned control tree supplies the only loadable code.
	// No relay request can select a DLL or execute another program.
	library := filepath.Join(control.Root, filepath.FromSlash(member))
	dll, err := windows.LoadLibraryEx(library, 0, windows.LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|windows.LOAD_LIBRARY_SEARCH_SYSTEM32)
	if err != nil {
		return nil, errors.Join(err, hold.Release())
	}
	r := &relayNative{dll: dll, release: hold.Release, procs: map[string]uintptr{}, handles: map[int64]*relayObject{}, signals: map[int64]*relayObject{}, tokens: map[uintptr]*relayObject{}}
	for _, name := range []string{"version", "features", "session_call", "reply_data", "reply_release", "runtime_endpoint", "select_runtime", "selected_server", "runtime_selection_release", "cancellation_create", "cancellation_signal", "cancellation_release", "open_cancelable", "open_verified", "write", "read", "close"} {
		address, err := windows.GetProcAddress(dll, "oa_ipc_"+name)
		if err != nil {
			windows.FreeLibrary(dll)
			return nil, errors.Join(err, hold.Release())
		}
		r.procs[name] = address
	}
	if r.invoke("version") != 1 {
		windows.FreeLibrary(dll)
		return nil, errors.Join(errors.New("IPC ABI1 required"), hold.Release())
	}
	return r, nil
}

func (r *relayNative) invoke(name string, args ...any) uintptr {
	var pins runtime.Pinner
	defer pins.Unpin()
	words := make([]uintptr, len(args))
	for i, arg := range args {
		if value, ok := arg.(uintptr); ok {
			words[i] = value
			continue
		}
		pins.Pin(arg)
		words[i] = reflect.ValueOf(arg).Pointer()
	}
	result, _, _ := syscall.SyscallN(r.procs[name], words...)
	runtime.KeepAlive(args)
	return result
}

func relayBytes(value string) (*byte, []byte) {
	raw := append([]byte(value), 0)
	return &raw[0], raw
}

func relayInteger(request map[string]json.RawMessage, name string, minimum, maximum int64) (int64, error) {
	var value int64
	if err := json.Unmarshal(request[name], &value); err != nil || value < minimum || value > maximum {
		return 0, errors.New("invalid " + name)
	}
	return value, nil
}

func relayText(request map[string]json.RawMessage, name string, limit int) (string, error) {
	var value string
	if err := json.Unmarshal(request[name], &value); err != nil || value == "" || len(value) > limit || strings.IndexByte(value, 0) >= 0 {
		return "", errors.New("invalid " + name)
	}
	return value, nil
}

// Token references are owned under mu by the caller handle, each blocking
// operation and each open stream. Releasing a caller token preserves streams,
// as the shared ABI requires; EOF can still signal their retained token.
func (r *relayNative) dropTokenLocked(token *relayObject) {
	token.refs--
	if token.refs == 0 {
		delete(r.tokens, token.value)
		r.invoke("cancellation_release", token.value)
		if token.parent != nil {
			delete(token.parent.children, token)
			r.dropTokenLocked(token.parent)
		}
	}
}

func (r *relayNative) newTokenLocked() (*relayObject, error) {
	var value uintptr
	status := r.invoke("cancellation_create", &value)
	if status != 0 {
		return nil, relayStatusError{status, "native cancellation unavailable"}
	}
	if value == 0 {
		return nil, relayStatusError{3, "native cancellation object is absent"}
	}
	token := &relayObject{value: value, refs: 1}
	r.tokens[value] = token
	return token, nil
}

// A stream owns a separate signal so closing one handle never cancels a
// sibling stream that the caller associated with the same cancellation token.
func (r *relayNative) borrowStreamSignal(request map[string]json.RawMessage) (*relayObject, func(), error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing {
		return nil, nil, relayStatusError{7, "relay is closing"}
	}
	var parent *relayObject
	if len(request["signal"]) > 0 && string(request["signal"]) != "null" {
		id, err := relayInteger(request, "signal", 1, 1<<53)
		if err != nil {
			return nil, nil, err
		}
		parent = r.signals[id]
		if parent == nil {
			return nil, nil, errors.New("unknown signal")
		}
	}
	token, err := r.newTokenLocked()
	if err != nil {
		return nil, nil, err
	}
	token.parent = parent
	if parent != nil {
		parent.refs++
		if parent.children == nil {
			parent.children = map[*relayObject]bool{}
		}
		parent.children[token] = true
		if parent.signaled {
			r.invoke("cancellation_signal", token.value)
		}
	}
	return token, func() { r.mu.Lock(); defer r.mu.Unlock(); r.dropTokenLocked(token) }, nil
}
func (r *relayNative) borrowSignal(request map[string]json.RawMessage) (*relayObject, func(), error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing {
		return nil, nil, relayStatusError{7, "relay is closing"}
	}
	var token *relayObject
	if len(request["signal"]) > 0 && string(request["signal"]) != "null" {
		id, err := relayInteger(request, "signal", 1, 1<<53)
		if err != nil {
			return nil, nil, err
		}
		token = r.signals[id]
		if token == nil {
			return nil, nil, errors.New("unknown signal")
		}
		token.refs++
	} else {
		var err error
		token, err = r.newTokenLocked()
		if err != nil {
			return nil, nil, err
		}
	}
	return token, func() { r.mu.Lock(); defer r.mu.Unlock(); r.dropTokenLocked(token) }, nil
}
func (r *relayNative) object(request map[string]json.RawMessage, name string) (uintptr, func(), error) {
	id, err := relayInteger(request, name, 1, 1<<53)
	if err != nil {
		return 0, nil, err
	}
	r.mu.Lock()
	object := r.handles[id]
	r.mu.Unlock()
	if object == nil {
		return 0, nil, errors.New("unknown " + name)
	}
	object.mu.RLock()
	if object.value == 0 {
		object.mu.RUnlock()
		return 0, nil, errors.New("released " + name)
	}
	return object.value, object.mu.RUnlock, nil
}
func (r *relayNative) remember(value uintptr, signals bool, token ...*relayObject) (int64, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing || value == 0 {
		return 0, relayStatusError{7, "relay is closing or native object is absent"}
	}
	if len(r.handles)+len(r.signals) >= 256 {
		return 0, errors.New("too many open objects")
	}
	r.next++
	object := &relayObject{value: value}
	if signals {
		object.refs = 1
		r.signals[r.next] = object
		r.tokens[value] = object
	} else {
		if len(token) != 1 {
			return 0, errors.New("stream cancellation owner is absent")
		}
		object.token = token[0]
		object.token.refs++
		r.handles[r.next] = object
	}
	return r.next, nil
}
func (r *relayNative) signalAll() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closing = true
	for value := range r.tokens {
		r.invoke("cancellation_signal", value)
	}
}
func (r *relayNative) closeAll() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, handle := range r.handles {
		r.invoke("close", handle.value)
		r.dropTokenLocked(handle.token)
	}
	for _, token := range r.signals {
		r.dropTokenLocked(token)
	}
	r.handles = map[int64]*relayObject{}
	r.signals = map[int64]*relayObject{}
}

func (r *relayNative) answer(request map[string]json.RawMessage) (map[string]any, error) {
	op, err := relayText(request, "op", 64)
	if err != nil {
		return nil, err
	}
	reply := map[string]any{"status": uintptr(0)}
	switch op {
	case "session_call":
		return r.sessionCall(request)
	case "runtime_endpoint":
		var needed uintptr
		status := r.invoke("runtime_endpoint", uintptr(0), uintptr(0), &needed)
		if status != 0 {
			reply["status"] = status
			return reply, nil
		}
		if needed < 2 || needed > 65536 {
			return nil, errors.New("runtime endpoint bound")
		}
		data := make([]byte, needed)
		status = r.invoke("runtime_endpoint", &data[0], uintptr(len(data)), &needed)
		reply["status"] = status
		if status == 0 {
			if needed < 2 || needed > uintptr(len(data)) || data[needed-1] != 0 {
				return nil, errors.New("runtime endpoint result")
			}
			reply["endpoint"] = string(data[:needed-1])
		}
	case "select_runtime":
		millis, err := relayInteger(request, "millis", 1, 4294967295)
		if err != nil {
			return nil, err
		}
		token, releaseSignal, err := r.borrowSignal(request)
		if err != nil {
			return nil, err
		}
		defer releaseSignal()
		signal := token.value
		var selected uintptr
		status := r.invoke("select_runtime", uintptr(millis), signal, &selected)
		reply["status"] = status
		if selected != 0 {
			defer r.invoke("runtime_selection_release", selected)
		}
		if status == 0 {
			pointer := r.invoke("selected_server", selected)
			if pointer == 0 {
				return nil, errors.New("runtime identity unavailable")
			}
			value := (*relayExpectation)(unsafe.Pointer(pointer))
			if value.Size != uint32(unsafe.Sizeof(*value)) || value.Version != 1 || value.Reserved != 0 || value.Kind < 1 || value.Kind > 2 || value.Principal == nil || value.Program == nil || value.PrincipalLength == 0 || value.PrincipalLength > 65536 || value.ProgramLength == 0 || value.ProgramLength > 65536 {
				return nil, errors.New("runtime identity bound")
			}
			reply["server"] = map[string]any{"principal_kind": value.Kind, "principal": string(unsafe.Slice(value.Principal, value.PrincipalLength)), "program": string(unsafe.Slice(value.Program, value.ProgramLength))}
		}
	case "cancellation_create":
		var token uintptr
		status := r.invoke("cancellation_create", &token)
		reply["status"] = status
		if status == 0 {
			id, err := r.remember(token, true)
			if err != nil {
				r.invoke("cancellation_release", token)
				return nil, err
			}
			reply["signal"] = id
		}
	case "cancellation_signal", "cancellation_release":
		id, err := relayInteger(request, "signal", 1, 1<<53)
		if err != nil {
			return nil, err
		}
		r.mu.Lock()
		token := r.signals[id]
		if token != nil {
			if op == "cancellation_signal" {
				token.signaled = true
				r.invoke(op, token.value)
				for child := range token.children {
					r.invoke(op, child.value)
				}
			} else {
				delete(r.signals, id)
				r.dropTokenLocked(token)
			}
		}
		r.mu.Unlock()
	case "close":
		id, err := relayInteger(request, "handle", 1, 1<<53)
		if err != nil {
			return nil, err
		}
		r.mu.Lock()
		object := r.handles[id]
		delete(r.handles, id)
		if object != nil {
			r.invoke("cancellation_signal", object.token.value)
		}
		r.mu.Unlock()
		if object != nil {
			object.mu.Lock()
			r.invoke("close", object.value)
			object.value = 0
			object.mu.Unlock()
			r.mu.Lock()
			r.dropTokenLocked(object.token)
			r.mu.Unlock()
		}
	case "open":
		endpoint, err := relayText(request, "endpoint", 65536)
		if err != nil {
			return nil, err
		}
		millis, err := relayInteger(request, "millis", 1, 4294967295)
		if err != nil {
			return nil, err
		}
		token, releaseSignal, err := r.borrowStreamSignal(request)
		if err != nil {
			return nil, err
		}
		defer releaseSignal()
		signal := token.value
		pointer, raw := relayBytes(endpoint)
		var handle uintptr
		var status uintptr
		if len(request["server"]) == 0 || string(request["server"]) == "null" {
			status = r.invoke("open_cancelable", pointer, uintptr(len(endpoint)), uintptr(millis), signal, &handle)
		} else {
			var expected struct {
				Kind      uint32 `json:"principal_kind"`
				Principal string `json:"principal"`
				Program   string `json:"program"`
			}
			if json.Unmarshal(request["server"], &expected) != nil || expected.Kind < 1 || expected.Kind > 2 || expected.Principal == "" || expected.Program == "" || len(expected.Principal) > 65536 || len(expected.Program) > 65536 || strings.ContainsAny(expected.Principal+expected.Program, "\x00") {
				return nil, errors.New("invalid server")
			}
			principal, praw := relayBytes(expected.Principal)
			program, programRaw := relayBytes(expected.Program)
			var pins runtime.Pinner
			pins.Pin(principal)
			pins.Pin(program)
			value := relayExpectation{Size: uint32(unsafe.Sizeof(relayExpectation{})), Version: 1, Kind: expected.Kind, Principal: principal, PrincipalLength: uintptr(len(expected.Principal)), Program: program, ProgramLength: uintptr(len(expected.Program))}
			status = r.invoke("open_verified", pointer, uintptr(len(endpoint)), uintptr(millis), signal, &value, &handle)
			pins.Unpin()
			runtime.KeepAlive(praw)
			runtime.KeepAlive(programRaw)
		}
		runtime.KeepAlive(raw)
		reply["status"] = status
		if status == 0 {
			id, err := r.remember(handle, false, token)
			if err != nil {
				r.invoke("close", handle)
				return nil, err
			}
			reply["handle"] = id
		}
	case "read", "write":
		handle, releaseHandle, err := r.object(request, "handle")
		if err != nil {
			return nil, err
		}
		defer releaseHandle()
		var data []byte
		if op == "read" {
			size, err := relayInteger(request, "size", 1, relayDataLimit)
			if err != nil {
				return nil, err
			}
			data = make([]byte, size)
		} else {
			var encoded string
			if json.Unmarshal(request["data"], &encoded) != nil || len(encoded) > 4*((relayDataLimit+2)/3) {
				return nil, errors.New("invalid data")
			}
			data, err = base64.StdEncoding.Strict().DecodeString(encoded)
			if err != nil || len(data) > relayDataLimit {
				return nil, errors.New("invalid data")
			}
		}
		var moved uintptr
		var pointer any = uintptr(0)
		if len(data) > 0 {
			pointer = &data[0]
		}
		reply["status"] = r.invoke(op, handle, pointer, uintptr(len(data)), &moved)
		if moved > uintptr(len(data)) {
			return nil, errors.New("native transfer bound")
		}
		if op == "read" {
			reply["data"] = base64.StdEncoding.EncodeToString(data[:moved])
		} else {
			reply["moved"] = moved
		}
	default:
		return nil, errors.New("unknown op")
	}
	return reply, nil
}

func (r *relayNative) sessionCall(request map[string]json.RawMessage) (map[string]any, error) {
	endpoint, err := relayText(request, "endpoint", 65536)
	if err != nil {
		return nil, err
	}
	millis, err := relayInteger(request, "millis", 1, 4294967295)
	if err != nil {
		return nil, err
	}
	maximum, err := relayInteger(request, "max_frame", 1, relayDataLimit)
	if err != nil {
		return nil, err
	}
	flags, err := relayInteger(request, "flags", 0, 7)
	if err != nil {
		return nil, err
	}
	token, releaseSignal, err := r.borrowSignal(request)
	if err != nil {
		return nil, err
	}
	defer releaseSignal()
	signal := token.value
	var encoded string
	if json.Unmarshal(request["data"], &encoded) != nil || len(encoded) > 4*((relayDataLimit+2)/3) {
		return nil, errors.New("invalid data")
	}
	data, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil || len(data) > int(maximum) {
		return nil, errors.New("invalid data")
	}
	pointer, endpointRaw := relayBytes(endpoint)
	var frame any = uintptr(0)
	if len(data) > 0 {
		frame = &data[0]
	}
	var expected any = uintptr(0)
	var pins runtime.Pinner
	defer pins.Unpin()
	var principalRaw, programRaw []byte
	if len(request["server"]) > 0 && string(request["server"]) != "null" {
		var value struct {
			Kind      uint32 `json:"principal_kind"`
			Principal string `json:"principal"`
			Program   string `json:"program"`
		}
		if json.Unmarshal(request["server"], &value) != nil || value.Kind < 1 || value.Kind > 2 || value.Principal == "" || value.Program == "" || len(value.Principal) > 65536 || len(value.Program) > 65536 || strings.ContainsAny(value.Principal+value.Program, "\x00") {
			return nil, errors.New("invalid server")
		}
		var principal, program *byte
		principal, principalRaw = relayBytes(value.Principal)
		program, programRaw = relayBytes(value.Program)
		pins.Pin(principal)
		pins.Pin(program)
		expected = &relayExpectation{Size: uint32(unsafe.Sizeof(relayExpectation{})), Version: 1, Kind: value.Kind, Principal: principal, PrincipalLength: uintptr(len(value.Principal)), Program: program, ProgramLength: uintptr(len(value.Program))}
	}
	var answer, sent uintptr
	status := r.invoke("session_call", pointer, uintptr(len(endpoint)), uintptr(millis), signal, expected, frame, uintptr(len(data)), uintptr(maximum), uintptr(flags), &answer, &sent)
	runtime.KeepAlive(endpointRaw)
	runtime.KeepAlive(principalRaw)
	runtime.KeepAlive(programRaw)
	if answer != 0 {
		defer r.invoke("reply_release", answer)
	}
	reply := map[string]any{"status": status, "sent": sent}
	if status == 0 && flags&1 == 0 {
		var size uintptr
		address := r.invoke("reply_data", answer, &size)
		if size > uintptr(maximum) || size > 0 && address == 0 {
			return nil, errors.New("native reply bound")
		}
		var result []byte
		if size > 0 {
			result = unsafe.Slice((*byte)(unsafe.Pointer(address)), size)
		}
		reply["data"] = base64.StdEncoding.EncodeToString(result)
	}
	return reply, nil
}

func runNativeRelay(ctx context.Context, input io.Reader, output io.Writer) error {
	r, err := openRelayNative(ctx)
	if err != nil {
		return err
	}
	return serveNativeRelay(ctx, input, output, r)
}

func serveNativeRelay(ctx context.Context, input io.Reader, output io.Writer, r *relayNative) error {
	defer func() { windows.FreeLibrary(r.dll); r.release() }()
	finished, watchDone := make(chan struct{}), make(chan struct{})
	defer func() { close(finished); <-watchDone }()
	go func() {
		defer close(watchDone)
		select {
		case <-ctx.Done():
			r.signalAll()
			if closer, ok := input.(io.Closer); ok {
				closer.Close()
			}
		case <-finished:
		}
	}()
	var writes sync.Mutex
	send := func(reply map[string]any) {
		writes.Lock()
		defer writes.Unlock()
		json.NewEncoder(output).Encode(reply)
	}
	send(map[string]any{"hello": true, "version": 1, "pid": os.Getpid(), "features": r.invoke("features")})
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 64<<10), relayLineLimit)
	var workers sync.WaitGroup
	pending := make(chan struct{}, 64)
	answer := func(request map[string]json.RawMessage) {
		reply, err := r.answer(request)
		if err != nil {
			reply = map[string]any{"status": 4, "message": err.Error()}
			var status relayStatusError
			if errors.As(err, &status) {
				reply["status"] = status.status
			}
		}
		reply["id"] = request["id"]
		send(reply)
	}
	for scanner.Scan() {
		var request map[string]json.RawMessage
		if json.Unmarshal(scanner.Bytes(), &request) != nil || request == nil {
			send(map[string]any{"id": nil, "status": 4, "message": "bounded JSON object required"})
			continue
		}
		if len(request["id"]) > 0 && string(request["id"]) != "null" {
			if _, err := relayInteger(request, "id", 0, 1<<53); err != nil {
				send(map[string]any{"id": nil, "status": 4, "message": "integer id required"})
				continue
			}
		}
		var op string
		json.Unmarshal(request["op"], &op)
		if op == "cancellation_signal" || op == "cancellation_create" {
			answer(request)
			continue
		}
		select {
		case pending <- struct{}{}:
			workers.Add(1)
			go func() { defer workers.Done(); defer func() { <-pending }(); answer(request) }()
		default:
			send(map[string]any{"id": request["id"], "status": 3, "message": "too many calls at once"})
		}
	}
	r.signalAll()
	workers.Wait()
	r.closeAll()
	return scanner.Err()
}

func init() {
	localPreviewEntrypoint = func(ctx context.Context, operation string, out io.Writer) string {
		if err := localPreviewOperation(ctx, operation, out); err != nil {
			return localPreviewSentence(err)
		}
		return ""
	}
	nativeEntrypoint = func(ctx context.Context, args []string) error {
		if len(args) == 0 {
			return errors.New("native operation required")
		}
		operation := args[0]
		if operation == "native-install-plan" {
			rest, site, err := nativePyTorchSite(args[1:])
			if err != nil {
				return err
			}
			root, err := nativePlanRoot(rest)
			if err != nil {
				return err
			}
			if site != "" && root == "" {
				return errors.New("a PyTorch component site needs the installed root")
			}
			return nativeInstallPlanFor(os.Stdout, root, site)
		}
		if operation == "seal-native-install" {
			rest, site, err := nativePyTorchSite(args[1:])
			if err != nil {
				return err
			}
			if len(rest) != 0 {
				return errors.New("exact native operation required")
			}
			return nativeInstallWith(ctx, true, nil, site)
		}
		if operation == "seal-native-control" {
			executable, err := os.Executable()
			if err != nil {
				return err
			}
			root, err := nativeControlRoot(executable, args[1:])
			if err != nil {
				return err
			}
			return sealNativeControl(ctx, root)
		}
		if len(args) != 1 {
			return errors.New("exact native operation required")
		}
		if operation == "owned-host" {
			return runNativeHost(ctx)
		}
		if operation == "register-self" {
			return registerNativeSelf(ctx, os.Stdout)
		}
		if operation == "native-relay" {
			return runNativeRelay(ctx, os.Stdin, os.Stdout)
		}
		if operation == "check-native-install" {
			return nativeInstall(ctx, false, os.Stdout)
		}
		return errors.New("foreign native operation")
	}
}

// Only fixed lifetime states cross the adopter's private control pipe. They
// convey no native authority, caller-selected operation, path or OA request.
type nativeStartup struct {
	mu         sync.Mutex
	state      byte
	ctx        context.Context
	cancel     context.CancelFunc
	done       chan struct{}
	start      func(context.Context, func()) error
	retired    bool
	userClosed bool
	result     error
	reopen     func(context.Context) error
	reopening  bool
	reopenWork sync.WaitGroup
	// events records lifecycle events in OA logging; nil writes standard error only.
	events *nativeEvents
	// ended runs once the backend failed or exited on its own while the host
	// still mediates, so the host can end and the next open starts afresh.
	ended func()
}

// browserFailed records that ComfyUI runs while its browser page did not
// open. Commands keep working; a later open retries the page.
func (s *nativeStartup) browserFailed() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state == 'R' {
		s.state = 'B'
	}
}

func newNativeStartup(start func(context.Context, func()) error) *nativeStartup {
	ctx, cancel := context.WithCancel(context.Background())
	return &nativeStartup{state: 'I', ctx: ctx, cancel: cancel, start: start}
}

func (s *nativeStartup) open() byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ctx.Err() != nil || s.retired {
		return 'U'
	}
	if s.done != nil {
		if s.state == 'V' {
			return 'A'
		}
		if s.state != 'R' && s.state != 'B' {
			return 'U'
		}
		if s.reopen != nil && !s.reopening {
			s.reopening = true
			s.reopenWork.Add(1)
			go func() {
				defer s.reopenWork.Done()
				err := s.reopen(s.ctx)
				s.mu.Lock()
				s.reopening = false
				running := s.state == 'R' || s.state == 'B'
				if running {
					s.state = 'R'
					if err != nil {
						s.state = 'B'
					}
				}
				s.mu.Unlock()
				if running && err != nil {
					s.events.log(nativeLevelWarn, "ComfyUI runs, but its browser page did not open: "+err.Error(), nil)
				}
			}()
		}
		return 'A'
	} // One retained backend, with separately coalesced browser-open requests.
	s.state, s.done = 'V', make(chan struct{})
	go func() {
		defer close(s.done)
		err := s.start(s.ctx, func() { s.mu.Lock(); s.state = 'R'; s.mu.Unlock() })
		s.mu.Lock()
		defer s.mu.Unlock()
		s.result = err
		if err != nil {
			s.state = 'F'
			var uncertain *uncertainOwnedExit
			if errors.As(err, &uncertain) {
				s.state = 'U'
			}
			fmt.Fprintln(os.Stderr, "OA Comfy startup failed:", err)
		} else {
			s.state = 'E'
		}
		if (s.state == 'F' || s.state == 'E') && s.ended != nil && !s.retired && s.ctx.Err() == nil {
			go s.ended()
		}
	}()
	return 'A' // Delivered, never a claim that verification or startup finished.
}

const nativeGraphLimit = 64 << 10
const nativeArmAckLimit = 256

type nativeArmPending struct {
	generation, nonce string
	ctx               context.Context
	done              chan struct{}
	settled           bool
	err               error
}

// These inherited pipes carry only the fixed private graph DATA protocol. The
// shared Python Server remains the sole MCP implementation and native identity
// stays with this image. The third pipe only arms one-use browser bootstrap.
type nativeGraphBridge struct {
	adapterRead, adapterWrite     *os.File
	backendRead, backendWrite     *os.File
	bootstrapRead, bootstrapWrite *os.File
	mu                            sync.Mutex
	process                       *supervision.Process
	generation                    string
	armPending                    *nativeArmPending
}

func newNativeGraphBridge() (*nativeGraphBridge, error) {
	b := &nativeGraphBridge{}
	var generation [16]byte
	if _, err := rand.Read(generation[:]); err != nil {
		return nil, err
	}
	b.generation = base64.RawURLEncoding.EncodeToString(generation[:])
	var err error
	b.backendRead, b.adapterWrite, err = os.Pipe()
	if err == nil {
		b.adapterRead, b.backendWrite, err = os.Pipe()
	}
	if err == nil {
		b.bootstrapRead, b.bootstrapWrite, err = os.Pipe()
	}
	if err != nil {
		b.close()
		return nil, err
	}
	for _, f := range []*os.File{b.adapterRead, b.adapterWrite, b.backendRead, b.backendWrite, b.bootstrapRead} {
		if err = windows.SetHandleInformation(windows.Handle(f.Fd()), windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT); err != nil {
			b.close()
			return nil, err
		}
	}
	return b, nil
}

func (b *nativeGraphBridge) close() {
	for _, f := range []*os.File{b.adapterRead, b.adapterWrite, b.backendRead, b.backendWrite, b.bootstrapRead, b.bootstrapWrite} {
		if f != nil {
			_ = f.Close()
		}
	}
}

func (b *nativeGraphBridge) prepare(settings *supervision.Options) error {
	for _, row := range []struct {
		name string
		file *os.File
	}{
		{"OA_COMFY_NATIVE_BRIDGE_READ_HANDLE", b.backendRead},
		{"OA_COMFY_NATIVE_BRIDGE_WRITE_HANDLE", b.backendWrite},
		{"OA_COMFY_NATIVE_BOOTSTRAP_HANDLE", b.bootstrapRead},
	} {
		if row.file == nil {
			return errors.New("native graph handle unavailable")
		}
		settings.Env = append(settings.Env, row.name+"="+strconv.FormatUint(uint64(row.file.Fd()), 10))
		settings.Inherit = append(settings.Inherit, row.file.Fd())
	}
	sort.Slice(settings.Env, func(i, j int) bool { return strings.ToUpper(settings.Env[i]) < strings.ToUpper(settings.Env[j]) })
	return nil
}

func (b *nativeGraphBridge) backendStarted(process *supervision.Process) {
	b.mu.Lock()
	b.process = process
	b.mu.Unlock()
	_ = b.backendRead.Close()
	_ = b.backendWrite.Close()
	_ = b.bootstrapRead.Close()
}

func nativeBootstrap(g string) ([]byte, string, error) {
	var nonce [24]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, "", err
	}
	n := base64.RawURLEncoding.EncodeToString(nonce[:])
	raw, err := json.Marshal(struct {
		Type       string `json:"type"`
		Generation string `json:"generation"`
		Nonce      string `json:"nonce"`
		Origin     string `json:"origin"`
	}{"arm", g, n, "http://127.0.0.1:8188"})
	return raw, "http://127.0.0.1:8188/oa/appmodel/bootstrap#generation=" + g + "&nonce=" + n, err
}

func (b *nativeGraphBridge) openBrowser(ctx context.Context) error {
	b.mu.Lock()
	process := b.process
	b.mu.Unlock()
	if process == nil {
		return errors.New("owned backend unavailable")
	}
	writeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	return openNativePage(writeCtx, b.generation, b.arm,
		func() error { return enginelaunch.CheckLoopback(identity.Sockets, 8188, process.Contains) },
		func(target string) error {
			verb, _ := windows.UTF16PtrFromString("open")
			url, _ := windows.UTF16PtrFromString(target)
			return windows.ShellExecute(0, verb, url, nil, nil, windows.SW_SHOWNORMAL)
		})
}

func (b *nativeGraphBridge) arm(ctx context.Context, raw []byte) error {
	var fields map[string]string
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	pending := &nativeArmPending{generation: fields["generation"], nonce: fields["nonce"], ctx: ctx, done: make(chan struct{})}
	b.mu.Lock()
	if b.armPending != nil {
		b.mu.Unlock()
		return errors.New("native bootstrap already pending")
	}
	b.armPending = pending
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		if b.armPending == pending {
			b.armPending = nil
		}
		b.mu.Unlock()
	}()
	// Cancellation interrupts an actually blocked owned write. Once the write
	// finishes, a missing ACK leaves the healthy arm stream and old page intact.
	stop := context.AfterFunc(ctx, func() { _ = b.bootstrapWrite.Close() })
	err := listen.WriteFrameTo(b.bootstrapWrite, raw, nativeGraphLimit)
	stop()
	if err != nil {
		return errors.New("native bootstrap unavailable")
	}
	select {
	case <-pending.done:
		return pending.err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (b *nativeGraphBridge) acknowledgeArm(raw []byte) error {
	var fields map[string]string
	valid := len(raw) <= nativeArmAckLimit && jsonValue(json.NewDecoder(bytes.NewReader(raw)), 0, "") == nil && json.Unmarshal(raw, &fields) == nil && len(fields) == 3 && fields["type"] == "arm_ack"
	b.mu.Lock()
	defer b.mu.Unlock()
	pending := b.armPending
	if pending == nil || pending.settled {
		return errors.New("native bootstrap acknowledgement is not pending")
	}
	pending.settled = true
	if pending.ctx.Err() != nil || !valid || fields["generation"] != pending.generation || fields["nonce"] != pending.nonce {
		pending.err = errors.New("native bootstrap acknowledgement differs")
	}
	close(pending.done)
	return pending.err
}

func openNativePage(ctx context.Context, generation string, arm func(context.Context, []byte) error, check func() error, show func(string) error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := check(); err != nil {
		return err
	}
	raw, target, err := nativeBootstrap(generation)
	if err != nil {
		return err
	}
	if err = arm(ctx, raw); err != nil {
		return errors.New("native bootstrap unavailable")
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	if err = check(); err != nil {
		return err
	}
	return show(target)
}

func (s *nativeStartup) status() byte { s.mu.Lock(); defer s.mu.Unlock(); return s.state }
func (s *nativeStartup) close() {
	s.cancel()
	s.mu.Lock()
	done := s.done
	s.mu.Unlock()
	if done != nil {
		<-done
	}
	s.reopenWork.Wait()
}

// FAC-M31: losing mediation retires future calls, while accepted startup/work
// stays with this native application until explicit shutdown or backend exit.
func (s *nativeStartup) retire() {
	s.mu.Lock()
	s.retired = true
	accepted := s.done != nil
	s.mu.Unlock()
	if !accepted {
		s.cancel()
	}
}

func (s *nativeStartup) wait(ctx context.Context) {
	s.mu.Lock()
	done := s.done
	s.mu.Unlock()
	if done == nil {
		return
	}
	select {
	case <-done:
	case <-ctx.Done():
		s.close()
	}
	s.reopenWork.Wait()
}

func (s *nativeStartup) failureReason() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state != 'F' {
		return ""
	}
	var failure *nativeBackendFailure
	if errors.As(s.result, &failure) {
		return failure.reason
	}
	return ""
}

func nativeControl(in io.Reader, out io.Writer, startup *nativeStartup) error {
	return nativeControlWithRetire(in, out, startup, func() {})
}

func nativeControlWithRetire(in io.Reader, out io.Writer, startup *nativeStartup, retire func()) error {
	return nativeControlWithArm(in, out, startup, retire, nil)
}

func nativeControlWithArm(in io.Reader, out io.Writer, startup *nativeStartup, retire func(), acknowledge func([]byte) error) error {
	var request [1]byte
	for {
		if _, err := io.ReadFull(in, request[:]); err != nil {
			return err
		}
		var response byte
		switch request[0] {
		case 'B':
			if acknowledge == nil {
				return errors.New("native bootstrap acknowledgement owner unavailable")
			}
			raw, err := listen.ReadFrameFrom(in, nativeArmAckLimit)
			if err != nil {
				return err
			}
			response = 'A'
			if acknowledge(raw) != nil {
				response = 'U'
			}
		case 'O':
			response = startup.open()
		case 'P':
			response = startup.status()
		case 'X':
			raw, err := json.Marshal(struct {
				Cause string `json:"cause"`
			}{startup.failureReason()})
			if err != nil {
				return err
			}
			if err := listen.WriteFrameTo(out, raw, 4096); err != nil {
				return err
			}
			continue
		case 'R':
			startup.retire()
			retire()
			response = startup.status()
		case 'C':
			// Only the authenticated page's explicit local confirmation reaches
			// this fixed operation. EOF and MCP retirement never call it.
			startup.retire()
			startup.close()
			startup.mu.Lock()
			var uncertain *uncertainOwnedExit
			if errors.As(startup.result, &uncertain) {
				startup.mu.Unlock()
				if _, err := out.Write([]byte{'U'}); err != nil {
					return err
				}
				continue
			}
			startup.userClosed = true
			startup.mu.Unlock()
			_, err := out.Write([]byte{'A'})
			return err
		default:
			return errors.New("foreign native control request")
		}
		if _, err := out.Write([]byte{response}); err != nil {
			return err
		}
		startup.mu.Lock()
		retired := startup.retired
		startup.mu.Unlock()
		if retired && (response == 'E' || response == 'F' || request[0] == 'R' && response == 'I') {
			return nil
		}
	}
}

func nativePin(raw []byte, limit int64) (pin, error) {
	var p pin
	if err := jsonValue(json.NewDecoder(bytes.NewReader(raw)), 0, ""); err != nil {
		return p, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&p); err != nil {
		return p, err
	}
	var tail any
	if decoder.Decode(&tail) != io.EOF || p.Bytes <= 0 || p.Bytes > limit || !digest(p.SHA256) {
		return p, errors.New("native image pin unavailable")
	}
	return p, nil
}

func controlTree(ctx context.Context, root, record string, selected pin) (programfiles.Tree, error) {
	var tree programfiles.Tree
	root, err := ordinary(ctx, root, true)
	if err != nil {
		return tree, err
	}
	h, err := fileid.Open(ctx, fileid.Self, filepath.Join(root, "manifest.json"), fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if err != nil {
		return tree, err
	}
	defer h.Close()
	if h.FileID().IsDir() || h.FileID().Links() != 1 {
		return tree, errors.New("ordinary control manifest required")
	}
	raw, err := io.ReadAll(io.LimitReader(h.File(), selected.Bytes+1))
	if err != nil {
		return tree, err
	}
	sum := sha256.Sum256(raw)
	if int64(len(raw)) != selected.Bytes || hex.EncodeToString(sum[:]) != selected.SHA256 {
		return tree, errors.New("control image pin differs")
	}
	// Reuse the backend's named pin rows and enginefiles' complete-tree validator.
	if err = jsonValue(json.NewDecoder(bytes.NewReader(raw)), 0, ""); err != nil {
		return tree, err
	}
	fields, err := exactKeys(raw, "runtime_files")
	if err != nil {
		return tree, err
	}
	var rows map[string]pin
	rowDecoder := json.NewDecoder(bytes.NewReader(fields["runtime_files"]))
	rowDecoder.DisallowUnknownFields()
	if err = rowDecoder.Decode(&rows); err != nil || len(rows) == 0 || len(rows) > 65535 {
		return tree, errors.New("control roster refused")
	}
	manifest := programfiles.Manifest{}
	var total int64
	for name, row := range rows {
		if row.Bytes < 0 || row.Bytes > 512<<20 || total > (512<<20)-row.Bytes {
			return tree, errors.New("control byte bound")
		}
		total += row.Bytes
		manifest.Files = append(manifest.Files, programfiles.File{Path: name, Size: row.Bytes, SHA256: row.SHA256})
	}
	for _, name := range []string{"python.exe", nativeAdapterMember} {
		if _, ok := rows[name]; !ok {
			return tree, errors.New("control entry missing")
		}
	}
	manifest.Files = append(manifest.Files, programfiles.File{Path: "manifest.json", Size: selected.Bytes, SHA256: selected.SHA256})
	if err = manifest.Validate(); err != nil {
		return tree, err
	}
	return programfiles.Tree{Name: "OA Comfy control", Root: root, Record: record, Manifest: manifest}, nil
}

type nativePresence struct {
	announce func(context.Context, string, wire.ApplicationPresence) (wire.ApplicationChange, error)
	withdraw func(context.Context, string, string) (wire.ApplicationChange, error)
	instance string
}

// The existing service owns signature/peer admission and the person's Keep
// question. This operation can select only its own held image and never
// supplies an expected revision, so it cannot replace an existing entry.
func registerNativeSelf(ctx context.Context, out io.Writer) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	return registerNativeSelfWith(ctx, out, func(ctx context.Context, request applications.RegisterRequest) (applications.Change, error) {
		binding, err := facade.Discover().ResolveService(ctx, "abstraction.facade/applications@3", facade.Requirements{Scope: facade.ScopeLocal})
		if err != nil {
			return applications.Change{}, err
		}
		return applications.NewApplicationsClient(binding.Transport().WithContext(ctx)).Register(request)
	})
}

func registerNativeSelfWith(ctx context.Context, out io.Writer, register func(context.Context, applications.RegisterRequest) (applications.Change, error)) (result error) {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	held, err := fileid.Open(ctx, fileid.Self, executable, fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, held.Close()) }()
	if held.FileID().IsDir() || held.FileID().Links() != 1 {
		return errors.New("ordinary self executable required")
	}
	change, err := register(ctx, applications.RegisterRequest{Program: held.FileID().Path()})
	if err != nil {
		return err
	}
	// Applied means the request was stored; it supplies no readiness or Keep
	// acknowledgement. Report the service's exact typed refusal without retry.
	if err := json.NewEncoder(out).Encode(struct {
		Outcome  string `json:"outcome"`
		Revision string `json:"revision,omitempty"`
		Reason   string `json:"reason,omitempty"`
		Approval string `json:"approval,omitempty"`
	}{change.Outcome.String(), change.Revision, change.Reason, "service-owned Keep; readiness separate"}); err != nil {
		return err
	}
	if change.Outcome != applications.OutcomeApplied {
		return fmt.Errorf("self-registration %s: %s", change.Outcome.String(), change.Reason)
	}
	return nil
}

func announceNative(ctx context.Context) (*nativePresence, error) {
	binding, err := facade.Discover().ResolveService(ctx, "abstraction.facade/applications@3", facade.Requirements{Scope: facade.ScopeLocal})
	if err != nil {
		return nil, err
	}
	p := &nativePresence{
		announce: func(ctx context.Context, key string, presence wire.ApplicationPresence) (wire.ApplicationChange, error) {
			return applications.NewApplicationsClient(binding.Transport().WithContext(ctx)).Announce(key, presence)
		},
		withdraw: func(ctx context.Context, application, instance string) (wire.ApplicationChange, error) {
			return applications.NewApplicationsClient(binding.Transport().WithContext(ctx)).Withdraw(application, instance)
		},
	}
	return beginNativePresence(ctx, p)
}

func beginNativePresence(ctx context.Context, p *nativePresence) (*nativePresence, error) {
	key, err := applications.NewRequestKey()
	if err != nil {
		return nil, err
	}
	change, err := p.announce(ctx, key, wire.ApplicationPresence{Application: "comfyui", LeaseMs: nativeLeaseMs, Interfaces: []wire.ApplicationInterface{}, Contexts: []wire.ApplicationContext{}})
	if err != nil {
		return nil, err
	}
	if change.Outcome != wire.ApplicationOutcomeApplied || change.Instance == "" {
		return nil, errors.New("native Comfy announcement unavailable")
	}
	p.instance = change.Instance
	return p, nil
}

const nativeLeaseMs = 30000

// keep renews the instance lease. An explicit refusal, a replaced instance or
// an unreachable runtime past the lease lapses it; earlier transient errors
// retry on the next tick while the lease still stands.
func (p *nativePresence) keep(ctx context.Context, every time.Duration, lapse func()) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	renewed := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			// Generated clients retain the resolver transport; each call is bounded.
			callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
			current, refused := p.renew(callCtx)
			cancel()
			if ctx.Err() != nil {
				return
			}
			if current {
				renewed = time.Now()
				continue
			}
			if refused || time.Since(renewed) >= nativeLeaseMs*time.Millisecond {
				lapse()
				return
			}
		}
	}
}

// renew reports whether the lease is current and, when it is not, whether the
// runtime answered with a refusal rather than failing to answer.
func (p *nativePresence) renew(ctx context.Context) (current, refused bool) {
	key, err := applications.NewRequestKey()
	if err != nil {
		return false, false
	}
	change, err := p.announce(ctx, key, wire.ApplicationPresence{Application: "comfyui", Instance: p.instance, LeaseMs: nativeLeaseMs, Interfaces: []wire.ApplicationInterface{}, Contexts: []wire.ApplicationContext{}})
	if err != nil {
		return false, false
	}
	current = change.Outcome == wire.ApplicationOutcomeApplied && change.Instance == p.instance
	return current, !current && change.Outcome != wire.ApplicationOutcomeUnavailable
}

// endLapsedHost ends an owned host whose lease lapsed or whose backend ended:
// the backend stops and is awaited, then the adapter stops, and the next open
// starts fresh.
func endLapsedHost(retire func(), startup *nativeStartup, stopAdapter func()) {
	retire()
	startup.close()
	stopAdapter()
}

func runNativeHost(ctx context.Context) error {
	backendPin, err := nativePin(nativeBackendPin, metadataLimit)
	if err != nil {
		return err
	}
	controlPin, err := nativePin(nativeControlPin, controlLimit)
	if err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	executable, err = ordinary(ctx, executable, false)
	if err != nil {
		return err
	}
	paths, err := nativePaths(ctx, executable, backendPin)
	if err != nil {
		return err
	}
	control, err := controlTree(ctx, filepath.Join(filepath.Dir(executable), "oa-control"), filepath.Join(paths.state, "control.record"), controlPin)
	if err != nil {
		return err
	}
	verifier := programfiles.NewVerifier(programfiles.Options{})
	// Setup's seal-native-control installs this same complete tree. A fresh
	// host still hashes every control byte before any Python instruction runs.
	hold, err := verifier.Launch(ctx, control)
	if err != nil {
		return err
	}
	presenceCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	presence, err := announceNative(presenceCtx)
	cancel()
	if err != nil {
		return errors.Join(err, hold.Release())
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
		return errors.Join(err, hold.Release())
	}
	defer graph.close()
	events := newNativeEvents()
	var startup *nativeStartup
	startup = newNativeStartup(func(ctx context.Context, started func()) error {
		return paths.start(ctx, started, startup.browserFailed, graph, events)
	})
	startup.reopen = graph.openBrowser
	startup.events = events
	fromAdapter, adapterWrite, err := os.Pipe()
	if err != nil {
		return errors.Join(err, hold.Release())
	}
	defer fromAdapter.Close()
	defer adapterWrite.Close()
	adapterRead, toAdapter, err := os.Pipe()
	if err != nil {
		return errors.Join(err, hold.Release())
	}
	defer adapterRead.Close()
	defer toAdapter.Close()
	for _, file := range []*os.File{adapterRead, adapterWrite} {
		if err = windows.SetHandleInformation(windows.Handle(file.Fd()), windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT); err != nil {
			return errors.Join(err, hold.Release())
		}
	}
	// The adapter calls the backend on the port it was started with and names
	// finished images by their path in the person-visible output folder.
	env, err := (supervision.CleanEnvironment{Temp: paths.values["--temporary"], Set: []string{"PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1", "OA_COMFY_OUTPUT_DIRECTORY=" + paths.values["--output"], "OA_COMFY_BACKEND_PORT=" + paths.values["--port"]}}).Build()
	if err != nil {
		return errors.Join(err, hold.Release())
	}
	settings := supervision.Options{Executable: filepath.Join(control.Root, "python.exe"), Args: []string{"-I", "-S", "-B", filepath.Join(control.Root, nativeAdapterMember), presence.instance, strconv.FormatUint(uint64(adapterRead.Fd()), 10), strconv.FormatUint(uint64(adapterWrite.Fd()), 10), strconv.FormatUint(uint64(graph.adapterRead.Fd()), 10), strconv.FormatUint(uint64(graph.adapterWrite.Fd()), 10), string(nativeApplicationManifest)}, Env: env, Dir: paths.values["--session"], Stdin: os.Stdin, Stdout: os.Stdout, Stderr: os.Stderr, Inherit: []uintptr{adapterRead.Fd(), adapterWrite.Fd(), graph.adapterRead.Fd(), graph.adapterWrite.Fd()}, ForceTimeout: 10 * time.Second}
	p, err := supervision.Start(ctx, settings)
	if err != nil {
		return errors.Join(err, hold.Release())
	}
	_ = adapterRead.Close()
	_ = adapterWrite.Close()
	_ = graph.adapterRead.Close()
	_ = graph.adapterWrite.Close()
	adapterLifetime, cancelAdapter := context.WithCancel(ctx)
	defer cancelAdapter()
	// selfEnded is true once the host ends itself: its lease lapsed, or its
	// backend failed or exited, and the next open starts a fresh host.
	var selfEnded atomic.Bool
	startup.ended = func() {
		// The grace lets a command waiting on the startup read its reason.
		time.AfterFunc(nativeEndedGrace, func() {
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
		controlledStop = nativeControlWithArm(fromAdapter, toAdapter, startup, retire, graph.acknowledgeArm) == nil
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
	if err = finishNativeMediation(ctx, startup, waitErr, hold.Release); err != nil {
		return err
	}
	if code != 0 && !controlledStop && !selfEnded.Load() {
		return errors.New("native control adapter failed")
	}
	return nil
}

func finishNativeMediation(ctx context.Context, startup *nativeStartup, waitErr error, release func() error) error {
	startup.retire()
	// Adapter failure or a control-hold cleanup error cannot discard accepted
	// backend ownership. Only explicit application shutdown cancels that work.
	startup.wait(ctx)
	startup.mu.Lock()
	var uncertain *uncertainOwnedExit
	unknown := errors.As(startup.result, &uncertain)
	startup.mu.Unlock()
	if unknown {
		return uncertain
	}
	if waitErr != nil {
		return waitErr
	} // Retain control holds on uncertain adapter exit.
	return release()
}

func nativeInstallPlan(out io.Writer) error {
	return nativeInstallPlanAt(out, "")
}

// The argument is bounded path DATA. Its caller binds the canonical original
// host installation root, including while the authenticated image is aside.
// No target is opened or resolved by this metadata operation.
func nativePlanRoot(args []string) (string, error) {
	if len(args) == 0 {
		return "", nil
	}
	if len(args) != 2 || args[0] != "--installed-root" {
		return "", errors.New("exact installed-root option required")
	}
	root := args[1]
	if len(root) == 0 || len(root) > 32767 || !utf8.ValidString(root) || !filepath.IsAbs(root) || filepath.Clean(root) != root || len(filepath.VolumeName(root)) != 2 || strings.ContainsAny(root, "\x00\r\n\v\f\x1c\x1d\x1e\u0085\u2028\u2029") {
		return "", errors.New("bounded ordinary absolute installed-root DATA required")
	}
	for _, component := range strings.Split(root[3:], `\`) {
		if err := fileid.SafeName(component); err != nil {
			return "", err
		}
	}
	return root, nil
}

// nativePyTorchSite takes the trailing --pytorch-site-packages ABS pair Setup
// passes when ComfyUI runs its torch from the shared PyTorch component.
func nativePyTorchSite(args []string) ([]string, string, error) {
	n := len(args)
	if n < 2 || args[n-2] != "--pytorch-site-packages" {
		return args, "", nil
	}
	if _, err := pytorchConfiguration(args[n-1]); err != nil {
		return nil, "", err
	}
	return args[:n-2], args[n-1], nil
}

func nativeInstallPlanAt(out io.Writer, installedRoot string) error {
	return nativeInstallPlanFor(out, installedRoot, "")
}

// nativeInstallPlanFor derives the installed roster, with oa-pytorch.pth
// naming site when Setup supplies the component's site-packages.
func nativeInstallPlanFor(out io.Writer, installedRoot, site string) error {
	selected, err := nativePin(nativeBackendPin, metadataLimit)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(nativeBackendManifest)
	if int64(len(nativeBackendManifest)) != selected.Bytes || hex.EncodeToString(sum[:]) != selected.SHA256 {
		return errors.New("own-image backend resource differs")
	}
	profile, manifest, err := parseManifest(nativeBackendManifest)
	if err != nil {
		return err
	}
	if installedRoot != "" {
		root, err := nativePlanRoot([]string{"--installed-root", installedRoot})
		if err != nil {
			return err
		}
		final, _, err := configuredTree(profile, programfiles.Tree{Root: filepath.Join(root, "oa-backend"), Manifest: manifest})
		if err != nil {
			return err
		}
		if site != "" && profile.PyTorch != nil {
			data, err := pytorchConfiguration(site)
			if err != nil {
				return err
			}
			if final, err = withPyTorchConfiguration(profile, final, data); err != nil {
				return err
			}
		}
		manifest = final.Manifest
	}
	type row struct {
		Path   string `json:"path"`
		Size   int64  `json:"size"`
		SHA256 string `json:"sha256"`
	}
	files := make([]row, 0, len(manifest.Files))
	dirs := map[string]bool{}
	for _, f := range manifest.Files {
		files = append(files, row{f.Path, f.Size, f.SHA256})
		for parent := path.Dir(f.Path); parent != "."; parent = path.Dir(parent) {
			dirs[parent] = true
		}
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	directories := make([]string, 0, len(dirs))
	for name := range dirs {
		directories = append(directories, name)
	}
	sort.Strings(directories)
	raw, err := json.Marshal(struct {
		Directory     string   `json:"directory"`
		Manifest      pin      `json:"manifest"`
		Files         []row    `json:"files"`
		Dirs          []string `json:"dirs"`
		InstalledRoot string   `json:"installed_root,omitempty"`
	}{"oa-backend", selected, files, directories, installedRoot})
	if err != nil {
		return err
	}
	if len(raw) > metadataLimit {
		return errors.New("native installation plan exceeds bound")
	}
	_, err = out.Write(append(raw, '\n'))
	return err
}

// Setup uses this literal operation after normal authenticated extraction.
// It launches no interpreter and announces no application presence.
func nativeControlRoot(executable string, args []string) (string, error) {
	if len(args) == 0 {
		return filepath.Join(filepath.Dir(executable), "oa-control"), nil
	}
	if len(args) != 2 || args[0] != "--root" || !filepath.IsAbs(args[1]) {
		return "", errors.New("seal-native-control requires --root ABS or no arguments")
	}
	if err := fileid.LocalPath(args[1]); err != nil {
		return "", err
	}
	return args[1], nil
}

func sealNativeControl(ctx context.Context, root string) error {
	backend, err := nativePin(nativeBackendPin, metadataLimit)
	if err != nil {
		return err
	}
	selected, err := nativePin(nativeControlPin, controlLimit)
	if err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	executable, err = ordinary(ctx, executable, false)
	if err != nil {
		return err
	}
	paths, err := nativePaths(ctx, executable, backend)
	if err != nil {
		return err
	}
	return sealNativeControlTree(ctx, root, filepath.Join(paths.state, "control.record"), selected)
}

func sealNativeControlTree(ctx context.Context, root, record string, selected pin) (result error) {
	var held []fileid.Handle
	defer func() {
		for _, h := range held {
			result = errors.Join(result, checkNativeSharedNamespace(ctx, h), h.Close())
		}
	}()
	for _, name := range []string{filepath.Dir(root), root} {
		h, err := nativeSharedNamespace(ctx, name)
		if err != nil {
			return err
		}
		held = append(held, h)
	}
	tree, err := controlTree(ctx, root, record, selected)
	if err != nil {
		return err
	}
	return programfiles.NewVerifier(programfiles.Options{}).Install(ctx, tree)
}

type nativeHostPaths struct {
	values   map[string]string
	state    string
	selected pin
}

func nativePaths(ctx context.Context, executable string, selected pin) (nativeHostPaths, error) {
	return nativePathsWith(ctx, executable, selected, true)
}

func nativePathsWith(ctx context.Context, executable string, selected pin, create bool) (nativeHostPaths, error) {
	platform := layout.Current()
	if _, err := layout.Resolve(platform); err != nil {
		return nativeHostPaths{}, err
	}
	local, _, err := layout.WindowsAccountFolders(platform)
	if err != nil {
		return nativeHostPaths{}, err
	}
	pictures, err := layout.ResolvePerson(platform, layout.Pictures)
	if err != nil {
		return nativeHostPaths{}, err
	}
	return nativePathsAt(ctx, executable, selected, create, local, pictures)
}

func nativeSharedNamespace(ctx context.Context, name string) (fileid.Handle, error) {
	h, err := fileid.Open(ctx, fileid.Self, name, fileid.Options{Links: fileid.RefuseLinks})
	if err != nil {
		return h, err
	}
	if err := checkNativeSharedNamespace(ctx, h); err != nil {
		return fileid.Handle{}, errors.Join(err, h.Close())
	}
	return h, nil
}

func checkNativeSharedNamespace(ctx context.Context, held fileid.Handle) error {
	name := held.FileID().Path()
	h, err := fileid.Open(ctx, fileid.Self, name, fileid.Options{Links: fileid.RefuseLinks})
	if err != nil {
		return err
	}
	defer h.Close()
	if !h.FileID().IsDir() || !held.FileID().Equal(h.FileID()) {
		return errors.New("shared namespace identity changed")
	}
	entry, err := layout.Inspect(name)
	if err != nil {
		return err
	}
	action, refusal := layout.JudgeOwnerOnly("windows", entry)
	if refusal != nil {
		return refusal
	}
	if action != layout.Keep && action != layout.Tighten {
		return errors.New("shared namespace is absent")
	}
	current, err := fileid.Open(ctx, fileid.Self, name, fileid.Options{Links: fileid.RefuseLinks})
	if err != nil {
		return err
	}
	if !held.FileID().Equal(current.FileID()) {
		return errors.Join(errors.New("shared namespace changed during inspection"), current.Close())
	}
	// Tighten is only a trust classification here. Shared ACLs stay unchanged.
	return current.Close()
}

func nativePathsAt(ctx context.Context, executable string, selected pin, create bool, local, pictures string) (p nativeHostPaths, result error) {
	var namespaces []fileid.Handle
	defer func() {
		for _, h := range namespaces {
			result = errors.Join(result, checkNativeSharedNamespace(ctx, h), h.Close())
		}
	}()
	// A trusted namespace can still be replaced through a foreign DELETE_CHILD
	// permission on its known-folder parent. Admit and retain both parents too.
	for _, name := range []string{local, pictures} {
		h, err := nativeSharedNamespace(ctx, name)
		if err != nil {
			return p, err
		}
		namespaces = append(namespaces, h)
	}
	leaf := func(name string) (string, error) {
		if create {
			return privateLeaf(ctx, name)
		}
		value, err := ordinary(ctx, name, true)
		if err != nil {
			return "", err
		}
		if err = privatedir.CheckDir(value); err != nil {
			return "", err
		}
		return value, nil
	}
	var err error
	// All mutable leaves are native-known-folder derived, outside both code roots.
	// The namespace parent is shared with other OA applications. Existing
	// parents need an ordinary path; Comfy-owned leaves retain private DACLs.
	sharedParent := func(root string) (string, error) {
		name := filepath.Join(root, "OpenAbstractions")
		if create {
			if _, e := os.Lstat(name); errors.Is(e, os.ErrNotExist) {
				if _, e = privateLeaf(ctx, name); e != nil {
					return "", e
				}
			} else if e != nil {
				return "", e
			}
		}
		h, err := nativeSharedNamespace(ctx, name)
		if err != nil {
			return "", err
		}
		namespaces = append(namespaces, h)
		return h.FileID().Path(), nil
	}
	parent, err := sharedParent(local)
	if err != nil {
		return p, err
	}
	parent, err = leaf(filepath.Join(parent, "ComfyUI"))
	if err != nil {
		return p, err
	}
	dataRoot := parent
	for _, name := range []string{"native", selected.SHA256} {
		parent, err = leaf(filepath.Join(parent, name))
		if err != nil {
			return p, err
		}
	}
	p.state, p.selected = parent, selected
	p.values = map[string]string{"--root": filepath.Join(filepath.Dir(executable), "oa-backend"), "--record": filepath.Join(parent, programfiles.NativeTreeRecordName), "--manifest-sha256": selected.SHA256, "--manifest-bytes": strconv.FormatInt(selected.Bytes, 10), "--state": filepath.Join(parent, "python-state.json"), "--port": "8188"}
	for _, name := range []string{"user", "input", "temporary", "models", "cache", "session"} {
		roleRoot := parent
		if name == "user" || name == "input" || name == "models" {
			roleRoot = dataRoot
		}
		value, e := leaf(filepath.Join(roleRoot, name))
		if e != nil {
			return p, e
		}
		p.values["--"+name] = value
	}
	outputParent, e := sharedParent(pictures)
	if e != nil {
		return p, e
	}
	p.values["--output"], err = leaf(filepath.Join(outputParent, "ComfyUI"))
	return p, err
}

// These fixed setup operations prove both own-image-selected whole roots and
// never dispatch Python. Check may refresh native-private engine metadata,
// while retaining code and ACL checks from the existing verifier.
func nativeInstall(ctx context.Context, install bool, report io.Writer) error {
	return nativeInstallWith(ctx, install, report, "")
}

// nativeInstallWith seals with oa-pytorch.pth naming site, when given.
func nativeInstallWith(ctx context.Context, install bool, report io.Writer, site string) (result error) {
	var paths nativeHostPaths
	// Registered before hold-release defers: report only complete successful
	// verification and release, using the same native path owner as startup.
	defer func() {
		if result == nil && report != nil {
			result = nativeInstallReport(report, paths)
		}
	}()
	backendPin, err := nativePin(nativeBackendPin, metadataLimit)
	if err != nil {
		return err
	}
	controlPin, err := nativePin(nativeControlPin, controlLimit)
	if err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	executable, err = ordinary(ctx, executable, false)
	if err != nil {
		return err
	}
	paths, err = nativePathsWith(ctx, executable, backendPin, install)
	if err != nil {
		return err
	}
	control, err := controlTree(ctx, filepath.Join(filepath.Dir(executable), "oa-control"), filepath.Join(paths.state, "control.record"), controlPin)
	if err != nil {
		return err
	}
	verifier := programfiles.NewVerifier(programfiles.Options{})
	if install {
		if err = verifier.Install(ctx, control); err != nil {
			return err
		}
	}
	controlHold, err := verifier.Launch(ctx, control)
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, controlHold.Release()) }()
	o := options{operation: "seal-backend", values: paths.values, manifestBytes: backendPin.Bytes}
	if install {
		if _, err = os.Lstat(paths.values["--record"]); errors.Is(err, os.ErrNotExist) {
			seal := o
			seal.values = map[string]string{}
			for key, value := range o.values {
				seal.values[key] = value
			}
			if site != "" {
				seal.values["--pytorch-site-packages"] = site
			}
			if _, err = executeWithInput(ctx, seal, nil, os.Stderr, nil); err != nil {
				return err
			}
		} else if err != nil {
			return err
		}
	}
	m, tree, err := loadTree(ctx, o)
	if err != nil {
		return err
	}
	if err = helperOwnCopy(ctx, m, tree); err != nil {
		return err
	}
	tree, _, err = configuredTree(m, tree)
	if err == nil {
		tree, err = placedPyTorchTree(ctx, m, tree)
	}
	if err == nil && install {
		// A sealed install whose PyTorch component moved to a new
		// per-version folder names the folder Setup gives now.
		tree, err = repointPyTorchConfiguration(ctx, verifier, m, tree, site)
	}
	if err != nil {
		return err
	}
	backendHold, err := verifier.Launch(ctx, tree)
	if err != nil {
		return err
	}
	return backendHold.Release()
}

func nativeInstallReport(out io.Writer, paths nativeHostPaths) error {
	raw, err := json.Marshal(struct {
		UserDirectory string `json:"user_directory"`
	}{paths.values["--user"]})
	if err != nil {
		return err
	}
	if len(raw)+1 > configLimit {
		return errors.New("native installation metadata exceeds bound")
	}
	_, err = out.Write(append(raw, '\n'))
	return err
}

// nativeBackendReadyLimit bounds the wait for ComfyUI's loopback listener
// after its backend process starts. Before it listens, the OA launcher inside
// the backend hashes site-packages in full whenever it holds no record of an
// earlier check (about a minute for the 33,000 files of 0.8.0), then ComfyUI
// imports the shared PyTorch component. The wait ends early when the backend
// exits.
var nativeBackendReadyLimit = 10 * time.Minute

// nativeEndedGrace is how long a host whose backend ended keeps answering
// before it ends itself.
const nativeEndedGrace = 5 * time.Second

// nativeBackendFailure retains the diagnostic for local logs and carries a
// fixed activation reason code. Native errors and backend output can contain paths.
type nativeBackendFailure struct {
	error
	reason string
}

func (e *nativeBackendFailure) Unwrap() error { return e.error }

var errNativeBackendExitedEarly = errors.New("the backend exited before ComfyUI listened on 127.0.0.1:8188")

func (p nativeHostPaths) start(ctx context.Context, started, browserFailed func(), graph *nativeGraphBridge, events *nativeEvents) (result error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	o := options{operation: "run-backend", values: withSetupModelPaths(ctx, p.values), manifestBytes: p.selected.Bytes, port: 8188, optionalModelPaths: true}
	// The same process verifier performs the mandatory complete first hash.
	// Cancellation also interrupts hashing; after Start it stops and joins the
	// existing owned tree through the carrier's cancellation-pipe owner.
	reader, writer := io.Pipe()
	defer reader.Close()
	defer writer.Close()
	go func() { <-ctx.Done(); _ = writer.Close() }()
	defer func() {
		var uncertain *uncertainOwnedExit
		if !errors.As(result, &uncertain) {
			graph.mu.Lock()
			graph.process = nil
			graph.mu.Unlock()
		}
		_ = graph.backendRead.Close()
		_ = graph.backendWrite.Close()
		_ = graph.bootstrapRead.Close()
	}()
	output, err := newNativeOutputTail(os.Stderr)
	if err != nil {
		return &nativeBackendFailure{err, "backend:output_unavailable"}
	}
	defer output.close()
	began := time.Now()
	events.log(nativeLevelInfo, "ComfyUI is starting: OpenAbstractions checks its files, then ComfyUI loads PyTorch", nil)
	listening := false
	reason := "backend:prepare_failed"
	prepare := func(settings *supervision.Options) error {
		if err := graph.prepare(settings); err != nil {
			return err
		}
		settings.Stdout, settings.Stderr = output.write, output.write
		reason = "backend:verify_or_launch_failed"
		return nil
	}
	code, err := executeWithPreparedInput(ctx, o, reader, os.Stderr, func(process *supervision.Process) error {
		reason = "backend:startup_incomplete"
		output.closeWrite()
		graph.backendStarted(process)
		exited := make(chan struct{})
		go func() { _, _ = process.Wait(); close(exited) }()
		readyCtx, cancel := context.WithTimeout(ctx, nativeBackendReadyLimit)
		defer cancel()
		if err := waitNativeListener(readyCtx, exited, func() error { return enginelaunch.CheckLoopback(identity.Sockets, 8188, process.Contains) }); err != nil {
			if ctx.Err() == nil && errors.Is(err, context.DeadlineExceeded) {
				reason = "backend:listener_timeout"
				return fmt.Errorf("ComfyUI did not listen on 127.0.0.1:8188 within %s", nativeBackendReadyLimit)
			}
			if errors.Is(err, errNativeBackendExitedEarly) {
				reason = "backend:early_exit"
			}
			return err
		}
		listening = true
		reason = "backend:stopped_after_listening"
		events.log(nativeLevelInfo, fmt.Sprintf("ComfyUI listens on 127.0.0.1:8188, %d s after the start", int(time.Since(began).Seconds())), nil)
		started()
		// ComfyUI serves commands whether or not its browser page opens.
		if err := graph.openBrowser(ctx); err != nil {
			browserFailed()
			events.log(nativeLevelWarn, "ComfyUI runs, but its browser page did not open: "+err.Error(), nil)
		}
		return nil
	}, prepare)
	if err == nil && code != 0 {
		err = fmt.Errorf("backend exited with code %d", code)
	}
	if ctx.Err() != nil {
		// The host stopped the backend: an explicit close or the host's end.
		return err
	}
	if err != nil {
		what := "ComfyUI did not start"
		if listening {
			what = "ComfyUI stopped"
		}
		attrs := map[string]string{"error": err.Error()}
		message := what + ": " + err.Error()
		if tail := output.text(); tail != "" {
			attrs["output"] = tail
			message += "\nLast output:\n" + tail
		}
		events.log(nativeLevelError, message, attrs)
		switch {
		case errors.Is(err, programfiles.ErrMismatch):
			reason = "backend:manifest_mismatch"
		case errors.Is(err, programfiles.ErrFolderACL):
			reason = "backend:folder_permissions"
		case errors.Is(err, programfiles.ErrUnreadable):
			reason = "backend:unreadable_file"
		}
		return &nativeBackendFailure{err, reason}
	}
	events.log(nativeLevelInfo, "ComfyUI exited", nil)
	return nil
}

// setupModelPathsName is the extra model-paths file OpenAbstractions Setup
// keeps in ComfyUI's data folder (beside user, input and models) for the
// picture models it prepared: one oa-models group naming their
// diffusion_models, text_encoders and vae folders.
const setupModelPathsName = "model-paths.json"

// withSetupModelPaths adds Setup's model-paths file and its digest to a
// run's values when the file is present and its folders pass the launcher's
// own check (modelConfiguration). The launcher rereads it against the digest
// and holds the folders for the run. An absent or stale file starts ComfyUI
// with its own models folder only, as does a file the launcher cannot hold
// (options.optionalModelPaths).
func withSetupModelPaths(ctx context.Context, values map[string]string) map[string]string {
	name := filepath.Join(filepath.Dir(values["--user"]), setupModelPathsName)
	raw, err := os.ReadFile(name)
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			fmt.Fprintf(os.Stderr, "Setup's model folders are not used: %v\n", err)
		}
		return values
	}
	if err = modelConfiguration(ctx, raw); err != nil {
		fmt.Fprintf(os.Stderr, "Setup's model folders are not used: %v\n", err)
		return values
	}
	out := make(map[string]string, len(values)+2)
	for key, value := range values {
		out[key] = value
	}
	sum := sha256.Sum256(raw)
	out["--extra-model-paths-config"], out["--extra-model-paths-sha256"] = name, hex.EncodeToString(sum[:])
	return out
}

// waitNativeListener checks the backend's own listener until it answers, the
// backend exits (exited closes) or ctx ends.
func waitNativeListener(ctx context.Context, exited <-chan struct{}, check func() error) error {
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := check(); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-exited:
			return errNativeBackendExitedEarly
		case <-ticker.C:
		}
	}
}

// nativeOutputTail carries the backend's standard output and error to this
// host's standard error and keeps their last bytes, for a failed start to
// name what ComfyUI or its OA launcher printed.
type nativeOutputTail struct {
	read, write *os.File
	mu          sync.Mutex
	last        []byte
	done        chan struct{}
}

const nativeOutputTailLimit = 2048

func newNativeOutputTail(forward io.Writer) (*nativeOutputTail, error) {
	r, w, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	t := &nativeOutputTail{read: r, write: w, done: make(chan struct{})}
	go func() {
		defer close(t.done)
		buffer := make([]byte, 32<<10)
		for {
			n, err := r.Read(buffer)
			if n > 0 {
				_, _ = forward.Write(buffer[:n])
				t.mu.Lock()
				t.last = append(t.last, buffer[:n]...)
				if len(t.last) > nativeOutputTailLimit {
					t.last = append([]byte(nil), t.last[len(t.last)-nativeOutputTailLimit:]...)
				}
				t.mu.Unlock()
			}
			if err != nil {
				return
			}
		}
	}()
	return t, nil
}

// closeWrite closes this host's copy of the write end once the backend holds
// its own, so the reader ends when the backend tree does.
func (t *nativeOutputTail) closeWrite() { _ = t.write.Close() }

// text is the kept output as whole lines of valid UTF-8, read once the
// backend's output ended or a second passed.
func (t *nativeOutputTail) text() string {
	t.closeWrite()
	select {
	case <-t.done:
	case <-time.After(time.Second):
	}
	t.mu.Lock()
	raw := string(t.last)
	full := len(t.last) == nativeOutputTailLimit
	t.mu.Unlock()
	if full {
		if i := strings.IndexByte(raw, '\n'); i >= 0 {
			raw = raw[i+1:]
		}
	}
	return strings.TrimSpace(strings.ToValidUTF8(strings.ReplaceAll(raw, "\r", ""), ""))
}

func (t *nativeOutputTail) close() {
	t.closeWrite()
	_ = t.read.Close()
}

// The OA logging levels (LOG-R3, the slog scale).
const (
	nativeLevelInfo  int64 = 0
	nativeLevelWarn  int64 = 4
	nativeLevelError int64 = 8
)

// nativeEvents records the host's lifecycle events. The runtime discards an
// owned host's standard error, so each event is also an OA logging record of
// this application, read with `openabstractions logs --app comfyui`.
type nativeEvents struct {
	stderr io.Writer
	write  func(ctx context.Context, level int64, message string, attrs map[string]string) error
}

func newNativeEvents() *nativeEvents {
	var mu sync.Mutex
	var sink interface {
		LogContext(context.Context, int64, string, map[string]string) error
	}
	return &nativeEvents{stderr: os.Stderr, write: func(ctx context.Context, level int64, message string, attrs map[string]string) error {
		mu.Lock()
		defer mu.Unlock()
		if sink == nil {
			client, err := facade.Discover().ResolveLog(ctx, facade.Requirements{Scope: facade.ScopeLocal})
			if err != nil {
				return err
			}
			sink = client
		}
		if err := sink.LogContext(ctx, level, message, attrs); err != nil {
			sink = nil
			return err
		}
		return nil
	}}
}

// log writes one event to standard error and to OA logging. A failed OA
// logging write becomes one more standard error line.
func (e *nativeEvents) log(level int64, message string, attrs map[string]string) {
	if e == nil {
		fmt.Fprintln(os.Stderr, "OA Comfy:", message)
		return
	}
	fmt.Fprintln(e.stderr, "OA Comfy:", message)
	if e.write == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := e.write(ctx, level, message, attrs); err != nil {
		fmt.Fprintln(e.stderr, "OA Comfy: OA logging is unavailable:", err)
	}
}

// The unsigned local preview ZIP can be extracted anywhere, including a
// folder whose permissions it does not control. install-local-preview places
// the preview in OpenAbstractions' own per-user package root, the folder
// Setup uses for installed packages:
//
//	%LOCALAPPDATA%\OpenAbstractions\apps\comfyui-local-preview\<version>
//
// The version folder is created with this account's private descriptor, so
// the launcher's existing private-parent check for its code roots holds. The
// extracted copy is only read. Sealing, checking, registering and starting
// then run from the installed launcher, whose path the operation prints.
const localPreviewIdentity = "comfyui-local-preview"

// localPreviewMembers are the payload the ZIP carries beside the launcher.
var localPreviewMembers = []string{"oa-backend", "oa-control"}

var errLocalPreviewPayload = errors.New("Extract the whole ZIP again, then run Setup OA ComfyUI.cmd")
var errLocalPreviewMissing = errors.New("Run Setup OA ComfyUI.cmd and choose 1 to set up ComfyUI first")

func localPreviewRoot(local string) string {
	return filepath.Join(local, "OpenAbstractions", "apps", localPreviewIdentity)
}

// localPreviewVersion names an installed copy by its launcher image: the
// launcher embeds the pins of both code roots.
func localPreviewVersion(executable string) (string, error) {
	f, err := os.Open(executable)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil))[:16], nil
}

func localPreviewInstalled(target string) bool {
	if privatedir.CheckDir(target) != nil {
		return false
	}
	for _, name := range append([]string{nativeLauncherName}, localPreviewMembers...) {
		if _, err := os.Lstat(filepath.Join(target, name)); err != nil {
			return false
		}
	}
	return true
}

// nativeLauncherName is the launcher's file name in the ZIP and in the
// installed copy.
const nativeLauncherName = "launch-helper.exe"

// installLocalPreviewAt copies the payload beside executable into local's
// OpenAbstractions package root and returns the installed launcher. An
// installed copy of the same launcher is reused unchanged.
func installLocalPreviewAt(ctx context.Context, executable, local string) (string, error) {
	version, err := localPreviewVersion(executable)
	if err != nil {
		return "", err
	}
	root := localPreviewRoot(local)
	target := filepath.Join(root, version)
	if localPreviewInstalled(target) {
		return filepath.Join(target, nativeLauncherName), nil
	}
	source := filepath.Dir(executable)
	for _, name := range localPreviewMembers {
		info, err := os.Lstat(filepath.Join(source, name))
		if err != nil || !info.IsDir() {
			return "", errLocalPreviewPayload
		}
	}
	// Shared parents are created private when absent and otherwise left as
	// they are; only the version folder holds code.
	for _, dir := range []string{filepath.Join(local, "OpenAbstractions"), filepath.Join(local, "OpenAbstractions", "apps"), root} {
		if _, err := os.Lstat(dir); errors.Is(err, fs.ErrNotExist) {
			if err := privatedir.Make(dir); err != nil && !errors.Is(err, fs.ErrExist) {
				return "", err
			}
		} else if err != nil {
			return "", err
		}
	}
	if _, err := os.Lstat(target); err == nil {
		// An incomplete earlier attempt: its private folder holds nothing the
		// person made. Replace it.
		if err := os.RemoveAll(target); err != nil {
			return "", err
		}
	}
	var suffix [8]byte
	if _, err := rand.Read(suffix[:]); err != nil {
		return "", err
	}
	stage := filepath.Join(root, ".stage-"+hex.EncodeToString(suffix[:]))
	if err := privatedir.Make(stage); err != nil {
		return "", err
	}
	done := false
	defer func() {
		if !done {
			_ = os.RemoveAll(stage)
		}
	}()
	for _, name := range localPreviewMembers {
		if err := copyLocalPreviewTree(ctx, filepath.Join(source, name), filepath.Join(stage, name)); err != nil {
			return "", err
		}
	}
	if err := copyLocalPreviewFile(executable, filepath.Join(stage, nativeLauncherName)); err != nil {
		return "", err
	}
	if err := os.Rename(stage, target); err != nil {
		return "", err
	}
	done = true
	if err := privatedir.CheckDir(target); err != nil {
		return "", err
	}
	return filepath.Join(target, nativeLauncherName), nil
}

// installedLocalPreviewAt is the installed launcher for executable's
// version, or errLocalPreviewMissing.
func installedLocalPreviewAt(executable, local string) (string, error) {
	version, err := localPreviewVersion(executable)
	if err != nil {
		return "", err
	}
	target := filepath.Join(localPreviewRoot(local), version)
	if !localPreviewInstalled(target) {
		return "", errLocalPreviewMissing
	}
	return filepath.Join(target, nativeLauncherName), nil
}

// copyLocalPreviewTree copies ordinary folders and files. New entries take
// the private folder's inherited descriptor. A link, junction or device
// refuses; the later seal verifies every byte against the pinned manifest.
func copyLocalPreviewTree(ctx context.Context, from, to string) error {
	return filepath.WalkDir(from, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		rel, err := filepath.Rel(from, path)
		if err != nil {
			return err
		}
		dest := filepath.Join(to, rel)
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		switch {
		case info.Mode()&fs.ModeSymlink != 0 || info.Mode()&fs.ModeIrregular != 0:
			return errLocalPreviewPayload
		case info.IsDir():
			return os.Mkdir(dest, 0o700)
		case info.Mode().IsRegular():
			return copyLocalPreviewFile(path, dest)
		}
		return errLocalPreviewPayload
	})
}

func copyLocalPreviewFile(from, to string) (err error) {
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(to, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, out.Close()) }()
	_, err = io.Copy(out, in)
	return err
}

// localPreviewSentence is the one sentence a failed preview setup prints.
func localPreviewSentence(err error) string {
	if errors.Is(err, errLocalPreviewPayload) || errors.Is(err, errLocalPreviewMissing) {
		return err.Error() + "."
	}
	return "ComfyUI could not be copied into OpenAbstractions (" + err.Error() + "); free some disk space or extract the whole ZIP again, then run Setup OA ComfyUI.cmd."
}

// localPreviewOperation runs install-local-preview or installed-local-preview
// for the running launcher and prints the installed launcher's path.
func localPreviewOperation(ctx context.Context, operation string, out io.Writer) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	executable, err = ordinary(ctx, executable, false)
	if err != nil {
		return err
	}
	local, _, err := layout.WindowsAccountFolders(layout.Current())
	if err != nil {
		return err
	}
	var installed string
	if operation == "install-local-preview" {
		installed, err = installLocalPreviewAt(ctx, executable, local)
	} else {
		installed, err = installedLocalPreviewAt(executable, local)
	}
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(out, installed)
	return err
}
