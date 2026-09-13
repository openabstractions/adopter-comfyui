//go:build windows

package main

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"os"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// native-relay makes this ComfyUI's OpenAbstractions calls for the node
// pack (adopters/comfyui/node/oa_relay.py): the node starts the connector
// with native-relay and sends each native IPC call as one JSON line. The
// connections are the connector's own, so OpenAbstractions knows them by
// the program it registered as the comfyui application. The relay carries
// bytes and cancellation only; the shared IPC library owns framing, native
// peer proof and runtime discovery. The library is the installed runtime's
// (runtimeLibrary); the node pack carries none.

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

// nativeMemory is an address the native library returned, as a pointer. The
// memory belongs to the library, never to Go's heap.
func nativeMemory(address uintptr) unsafe.Pointer {
	return *(*unsafe.Pointer)(unsafe.Pointer(&address))
}

func openRelayNative(context.Context) (*relayNative, error) {
	library, err := runtimeLibrary(ipcLibraryName)
	if err != nil {
		return nil, err
	}
	dll, err := windows.LoadLibraryEx(library, 0, windows.LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|windows.LOAD_LIBRARY_SEARCH_SYSTEM32)
	if err != nil {
		return nil, err
	}
	r := &relayNative{dll: dll, release: func() error { return nil }, procs: map[string]uintptr{}, handles: map[int64]*relayObject{}, signals: map[int64]*relayObject{}, tokens: map[uintptr]*relayObject{}}
	for _, name := range []string{"version", "features", "session_call", "reply_data", "reply_release", "runtime_endpoint", "select_runtime", "selected_server", "runtime_selection_release", "cancellation_create", "cancellation_signal", "cancellation_release", "open_cancelable", "open_verified", "write", "read", "close"} {
		address, err := windows.GetProcAddress(dll, "oa_ipc_"+name)
		if err != nil {
			windows.FreeLibrary(dll)
			return nil, err
		}
		r.procs[name] = address
	}
	if r.invoke("version") != 1 {
		windows.FreeLibrary(dll)
		return nil, errors.New("IPC ABI1 required")
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
			value := (*relayExpectation)(nativeMemory(pointer))
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
			result = unsafe.Slice((*byte)(nativeMemory(address)), size)
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
	// The card broker starts at the pack's first card or prompt line, so a
	// ComfyUI whose pack sends none holds no lease (card.go).
	cards := newRelayCards(ctx)
	defer cards.close()
	answer := func(request map[string]json.RawMessage) {
		reply, err := cards.answer(ctx, request, r.answer)
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
