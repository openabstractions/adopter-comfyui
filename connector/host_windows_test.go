//go:build windows

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/openabstractions/abstraction-facade/go/supervision"
	"github.com/openabstractions/abstraction-identity/listen"
	"github.com/openabstractions/adopter-comfyui/connector/internal/personfolders"
	"golang.org/x/sys/windows"
)

// The stand-in ComfyUI is this test binary started with OA_STANDIN_COMFY=1:
// it listens on a free loopback port, writes the node pack's report
// (OA_STANDIN_REPORT, naming OA_STANDIN_BASE), connects the graph channel
// the connector names beside it (standInNode) and serves the few ComfyUI
// routes workflow.list reads. It ignores its arguments, so it also stands in
// for a portable ComfyUI's python.exe -s main.py. With OA_STANDIN_FOREIGN
// naming a channel file, the binary is a foreign process trying that channel.
func TestMain(m *testing.M) {
	if file := os.Getenv("OA_STANDIN_FOREIGN"); file != "" {
		os.Exit(foreignClient(file))
	}
	if os.Getenv("OA_STANDIN_COMFY") == "1" {
		os.Exit(standInComfy())
	}
	if os.Getenv("OA_STANDIN_SLEEP") == "1" {
		time.Sleep(10 * time.Minute)
		os.Exit(0)
	}
	// The node pack starts its relay as "<connector> native-relay" with a
	// reduced environment; this binary then is the connector.
	if len(os.Args) == 2 && os.Args[1] == "native-relay" {
		if err := runNativeRelay(context.Background(), os.Stdin, os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, "comfyui-connector:", err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(personfolders.Main(m))
}

// The stand-in page's canvas: one KSampler, node 7. Its Queue queues
// standInPrompt, whose output is standInOutput in the stand-in's output
// folder.
const (
	standInPage     = "page_stand_in_0000001"
	standInWorkflow = "workflow_stand_in_00000000000001"
	standInPrompt   = "prompt_stand_in_000000000000001"
	standInOutput   = "standin_00001_.glb"
)

// standInNode is the node pack's graph bridge in the stand-in ComfyUI: it
// connects each graph channel the connector names for this process,
// acknowledges each page ARM and stands in for the page that ARM opens.
func standInNode(folder string) {
	file := channelFile(folder, os.Getpid())
	used := ""
	for ; ; time.Sleep(20 * time.Millisecond) {
		raw, err := readBounded(file, channelFileLimit)
		var name channelName
		if err != nil || json.Unmarshal(raw, &name) != nil || name.PID != os.Getpid() || name.Pipe == used {
			continue
		}
		conn, err := listen.Dial(name.Pipe)
		if err != nil {
			continue
		}
		used = name.Pipe
		standInGraph(conn)
		_ = conn.Close()
	}
}

// standInGraph answers the connector's frames as the node and its page do
// (node/oa_appmodel_bridge.py and node/web/oa_appmodel_graph.mjs).
func standInGraph(conn io.ReadWriter) {
	steps, mode, sequence, generation := 20, "always", 0, ""
	send := func(value any) error {
		raw, err := json.Marshal(value)
		if err != nil {
			return err
		}
		return listen.WriteFrameTo(conn, raw, nativeGraphLimit)
	}
	canvas := func() map[string]any {
		return map[string]any{"nodes": []map[string]any{{"id": "7", "type": "KSampler", "title": "KSampler", "mode": mode,
			"widgets": []map[string]any{{"name": "steps", "type": "int", "value": steps, "min": 1, "max": 10000}},
			"inputs":  []any{}, "outputs": []any{}}}, "links": []any{}, "groups": []any{}}
	}
	for {
		raw, err := listen.ReadFrameFrom(conn, nativeGraphLimit)
		if err != nil {
			return
		}
		var m map[string]any
		if json.Unmarshal(raw, &m) != nil {
			return
		}
		switch m["type"] {
		case "agent_stop":
			outcome := "unavailable"
			if key := os.Getenv(agentStopEnvironment); key != "" && m["key"] == key {
				outcome = "requested"
			}
			if send(map[string]any{"type": "agent_stop_result", "id": m["id"], "outcome": outcome}) != nil {
				return
			}
		case "arm":
			generation, _ = m["generation"].(string)
			if send(map[string]any{"type": "arm_ack", "generation": generation, "nonce": m["nonce"]}) != nil ||
				send(map[string]any{"type": "projection", "generation": generation, "page": standInPage,
					"workflow": standInWorkflow, "sequence": sequence, "graph": canvas()}) != nil {
				return
			}
		case "request":
			arguments, _ := m["arguments"].(map[string]any)
			var answer any
			switch m["operation"] {
			case "edit":
				ops, _ := arguments["ops"].([]any)
				for _, item := range ops {
					op, _ := item.(map[string]any)
					if op["op"] == "set_widget" && op["widget"] == "steps" {
						steps = int(op["value"].(float64))
					} else if op["op"] == "set_mode" {
						mode, _ = op["mode"].(string)
					}
				}
				sequence++
				answer = map[string]any{"applied": len(ops)}
			case "run":
				answer = map[string]any{"prompts": []string{standInPrompt}}
			}
			if send(map[string]any{"type": "result", "id": m["id"], "generation": generation, "page": standInPage,
				"workflow": standInWorkflow, "outcome": "applied", "answer": answer,
				"projection": map[string]any{"epoch": standInWorkflow, "sequence": sequence, "graph": canvas()}}) != nil {
				return
			}
		}
	}
}

// foreignClient connects to the channel file names as a process other than
// the served ComfyUI and offers a page's projection. It exits 0 once the
// connector ended the connection, 1 if the connector sent it anything.
func foreignClient(file string) int {
	raw, err := readBounded(file, channelFileLimit)
	var name channelName
	if err != nil || json.Unmarshal(raw, &name) != nil {
		return 3
	}
	conn, err := listen.Dial(name.Pipe)
	if err != nil {
		return 4
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	projection, _ := json.Marshal(map[string]any{"type": "projection", "generation": "foreign_generation01", "page": "foreign_page_000001",
		"workflow": standInWorkflow, "sequence": 0, "graph": map[string]any{}})
	_ = listen.WriteFrameTo(conn, projection, nativeGraphLimit)
	if _, err := listen.ReadFrameFrom(conn, nativeGraphLimit); err == nil {
		return 1
	} else if errors.Is(err, os.ErrDeadlineExceeded) {
		return 2
	}
	return 0
}

func standInComfy() int {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 3
	}
	port := listener.Addr().(*net.TCPAddr).Port
	report, err := json.Marshal(listening{PID: os.Getpid(), Port: port, Base: os.Getenv("OA_STANDIN_BASE"), Output: os.Getenv("OA_STANDIN_OUTPUT")})
	if err != nil {
		return 3
	}
	file := os.Getenv("OA_STANDIN_REPORT")
	if err := os.MkdirAll(filepath.Dir(file), 0o700); err != nil {
		return 3
	}
	if err := os.WriteFile(file+".tmp", report, 0o600); err != nil || os.Rename(file+".tmp", file) != nil {
		return 3
	}
	go standInNode(filepath.Dir(file))
	// A started server's output and command: the lines a ComfyUI prints
	// before it listens, and what it was started with (OA_STANDIN_ARGS).
	fmt.Println("Total VRAM 8192 MB, total RAM 32768 MB")
	fmt.Printf("To see the GUI go to: http://127.0.0.1:%d\n", port)
	if record := os.Getenv("OA_STANDIN_ARGS"); record != "" {
		dir, _ := os.Getwd()
		raw, _ := json.Marshal(map[string]any{"args": os.Args[1:], "dir": dir, "encoding": os.Getenv("PYTHONIOENCODING"),
			"own": os.Getenv("OA_STANDIN_OWN")})
		_ = os.WriteFile(record, raw, 0o600)
	}
	routes := http.NewServeMux()
	answer := func(w http.ResponseWriter, value any) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(value)
	}
	routes.HandleFunc("/api/userdata", func(w http.ResponseWriter, r *http.Request) {
		answer(w, []string{"cats.json", "sub/dogs.json"})
	})
	routes.HandleFunc("/api/workflow_templates", func(w http.ResponseWriter, r *http.Request) {
		answer(w, map[string][]string{"comfyui-openabstractions": {"oa_image_generate_preview_save"}})
	})
	routes.HandleFunc("/api/queue", func(w http.ResponseWriter, r *http.Request) {
		answer(w, map[string][]any{"queue_running": {}, "queue_pending": {}})
	})
	// The page's queued prompt has finished with one output file.
	routes.HandleFunc("/api/history/"+standInPrompt, func(w http.ResponseWriter, r *http.Request) {
		answer(w, map[string]any{standInPrompt: map[string]any{
			"status":  map[string]any{"status_str": "success", "completed": true},
			"outputs": map[string]any{"9": map[string]any{"3d": []map[string]any{{"filename": standInOutput, "subfolder": "", "type": "output"}}}}}})
	})
	_ = http.Serve(listener, routes)
	return 0
}

// standInEnvironment makes the next stand-in report base's ComfyUI at report.
func standInEnvironment(t *testing.T, report, base string) {
	t.Setenv("OA_STANDIN_COMFY", "1")
	t.Setenv("OA_STANDIN_REPORT", report)
	t.Setenv("OA_STANDIN_BASE", base)
	t.Setenv("OA_STANDIN_OUTPUT", filepath.Join(base, "output"))
}

// startStandIn starts a stand-in ComfyUI the connector does not own.
func startStandIn(t *testing.T) *exec.Cmd {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(self)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _, _ = cmd.Process.Wait() })
	return cmd
}

func waitFile(t *testing.T, path string) {
	t.Helper()
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
		if _, err := os.Stat(path); err == nil {
			return
		}
	}
	t.Fatalf("%s did not appear", path)
}

func served(t *testing.T, h *host) (port int, output string) {
	t.Helper()
	var a struct {
		Port   int    `json:"port"`
		Output string `json:"output"`
	}
	if err := json.Unmarshal(h.attachment(), &a); err != nil {
		t.Fatal(err)
	}
	return a.Port, a.Output
}

func getQueue(port int) error {
	client := http.Client{Timeout: 5 * time.Second}
	response, err := client.Get("http://127.0.0.1:" + strconv.Itoa(port) + "/api/queue")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("status %d", response.StatusCode)
	}
	return nil
}

func testHost(t *testing.T, install comfyInstall, report string) (*host, *[]string) {
	t.Helper()
	graph, err := newNativeGraphBridge()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(graph.close)
	shown := &[]string{}
	graph.show = func(target string) error {
		shownMu.Lock()
		defer shownMu.Unlock()
		*shown = append(*shown, target)
		return nil
	}
	h := newHost(install, report, graph, nil)
	h.show = graph.show
	h.launch = func(program string) (desktopProcess, error) {
		t.Fatalf("launched %s", program)
		return desktopProcess{}, nil
	}
	return h, shown
}

// shownMu guards the pages a test host showed.
var shownMu sync.Mutex

// waitShown waits until n pages were shown and returns them.
func waitShown(t *testing.T, shown *[]string, n int) []string {
	t.Helper()
	for deadline := time.Now().Add(30 * time.Second); ; time.Sleep(20 * time.Millisecond) {
		shownMu.Lock()
		pages := append([]string(nil), *shown...)
		shownMu.Unlock()
		if len(pages) >= n || time.Now().After(deadline) {
			return pages
		}
	}
}

// standInAdapter reads the frames the connector relays to the adapter,
// acknowledges each page ARM through the bridge as the adapter's control
// request B does, and passes on the rest.
func standInAdapter(t *testing.T, graph *nativeGraphBridge) <-chan map[string]any {
	t.Helper()
	frames := make(chan map[string]any, 64)
	go func() {
		defer close(frames)
		for {
			raw, err := listen.ReadFrameFrom(graph.adapterRead, nativeGraphLimit)
			if err != nil {
				return
			}
			var m map[string]any
			if json.Unmarshal(raw, &m) != nil {
				return
			}
			if m["type"] == "arm_ack" {
				_ = graph.acknowledgeArm(raw)
				continue
			}
			frames <- m
		}
	}()
	return frames
}

// adapterRequest writes one graph request as the adapter does.
func adapterRequest(t *testing.T, graph *nativeGraphBridge, request map[string]any) {
	t.Helper()
	raw, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	if err := listen.WriteFrameTo(graph.adapterWrite, raw, nativeGraphLimit); err != nil {
		t.Fatal(err)
	}
}

// nextFrame is the next relayed frame of the given type.
func nextFrame(t *testing.T, frames <-chan map[string]any, kind string) map[string]any {
	t.Helper()
	timeout := time.After(30 * time.Second)
	for {
		select {
		case m, ok := <-frames:
			if !ok {
				t.Fatalf("the adapter's frames ended before a %s", kind)
			}
			if m["type"] == kind {
				return m
			}
		case <-timeout:
			t.Fatalf("no %s reached the adapter", kind)
		}
	}
}

// graphPage is the start of the graph page's address on port.
func graphPage(port int) string {
	return "http://127.0.0.1:" + strconv.Itoa(port) + "/oa/appmodel/bootstrap#generation="
}

func serve(t *testing.T, h *host, ctx context.Context) (<-chan error, <-chan struct{}) {
	t.Helper()
	done, ready := make(chan error, 1), make(chan struct{})
	go func() { done <- h.start(ctx, func() { close(ready) }) }()
	select {
	case <-ready:
	case err := <-done:
		t.Fatalf("not served: %v", err)
	case <-time.After(60 * time.Second):
		t.Fatal("ComfyUI was not served")
	}
	return done, ready
}

// open with a portable ComfyUI starts its python_embeded\python.exe as the
// connector's own process, serves it once it reports, and stops it when the
// host ends.
func TestPortableComfyIsStartedServedAndStopped(t *testing.T) {
	root := t.TempDir()
	portable := filepath.Join(root, "ComfyUI_windows_portable")
	base := filepath.Join(portable, "ComfyUI")
	writeFixture(t, filepath.Join(base, "main.py"), "")
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(self)
	if err != nil {
		t.Fatal(err)
	}
	writeFixture(t, filepath.Join(portable, "python_embeded", "python.exe"), string(raw))
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil || install.Kind != kindPortable {
		t.Fatalf("%+v %v", install, err)
	}
	report := filepath.Join(root, "Local", "OpenAbstractions", "ComfyUI", listeningName)
	standInEnvironment(t, report, base)
	h, shown := testHost(t, install, report)
	h.folders.Local = filepath.Join(root, "Local")
	frames := standInAdapter(t, h.graph)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done, _ := serve(t, h, ctx)
	port, output := served(t, h)
	if port == 0 || output != filepath.Join(base, "output") || !h.owned {
		t.Fatalf("served %d %q owned=%v", port, output, h.owned)
	}
	nextFrame(t, frames, "projection")
	if pages := waitShown(t, shown, 1); len(pages) != 1 || !strings.HasPrefix(pages[0], graphPage(port)) {
		t.Fatalf("shown %v", pages)
	}
	record, ok := readStarted(h.folders.Local, comfyApplication)
	listener, err := readListening(report)
	created, alive := liveProcess(listener.PID)
	if err != nil || !ok || !alive || !record.names(listener.PID, strconv.FormatUint(created, 10)) {
		t.Fatalf("portable listener is absent from the Panel record: %+v, %+v, %v", listener, record, err)
	}
	if stopped := h.requestAgentStop(); stopped != 'A' {
		t.Fatalf("portable request did not reach the exact node: %q", stopped)
	}
	if err := getQueue(port); err != nil {
		t.Fatal(err)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the owned ComfyUI did not stop")
	}
	if err := getQueue(port); err == nil {
		t.Fatal("the owned ComfyUI outlived the host")
	}
	if string(h.attachment()) != "{}" {
		t.Fatal("a stopped ComfyUI is still served")
	}
	if stopped := h.requestAgentStop(); stopped != 'U' {
		t.Fatalf("retired portable start can still receive stop: %q", stopped)
	}
}

func TestPortableComfyPreservesAnotherLivePanelStartRecord(t *testing.T) {
	local := t.TempDir()
	old := startedRecord{Application: comfyApplication, Processes: []startedProcess{{PID: 41, Created: "77"}}}
	if err := writeStarted(local, old); err != nil {
		t.Fatal(err)
	}
	next := startedRecord{V: 1, Application: comfyApplication, Processes: []startedProcess{{PID: 42, Created: "90"}}}
	live := func(pid int) (uint64, bool) { return 77, pid == 41 }
	if err := publishStarted(local, next, live); err == nil {
		t.Fatal("replaced another live start record")
	}
	if got, ok := readStarted(local, comfyApplication); !ok || !got.names(41, "77") {
		t.Fatalf("other record lost: %+v", got)
	}
	reusedPID := func(pid int) (uint64, bool) { return 81, pid == 41 }
	if err := publishStarted(local, next, reusedPID); err != nil {
		t.Fatal(err)
	}
	if got, ok := readStarted(local, comfyApplication); !ok || !got.names(42, "90") {
		t.Fatalf("stale record was not replaced: %+v", got)
	}
}

// A ComfyUI that already listens is attached and shown, and it keeps
// running when the host ends; its own exit ends the serving.
func TestARunningComfyIsAttachedAndNeverStopped(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil || install.Program != "" {
		t.Fatalf("%+v %v", install, err)
	}
	report := filepath.Join(root, listeningName)
	standInEnvironment(t, report, base)
	comfy := startStandIn(t)
	waitFile(t, report)
	h, shown := testHost(t, install, report)
	h.folders.Local = filepath.Join(root, "Local")
	standInAdapter(t, h.graph)
	ctx, cancel := context.WithCancel(context.Background())
	done, _ := serve(t, h, ctx)
	port, _ := served(t, h)
	if pages := waitShown(t, shown, 1); h.owned || len(pages) != 1 || !strings.HasPrefix(pages[0], graphPage(port)) {
		t.Fatalf("owned=%v shown=%v", h.owned, pages)
	}
	if _, ok := readStarted(h.folders.Local, comfyApplication); ok || h.requestAgentStop() != 'U' {
		t.Fatal("the person's attached ComfyUI acquired a connector stop")
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := getQueue(port); err != nil {
		t.Fatalf("the host stopped the person's ComfyUI: %v", err)
	}
	ctx, cancel = context.WithCancel(context.Background())
	defer cancel()
	done, _ = serve(t, h, ctx)
	_ = comfy.Process.Kill()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the attached ComfyUI's exit did not end the serving")
	}
}

func TestSeparatelyOpenedDesktopKeepsItsWindowAndGetsAFreshOAPage(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil {
		t.Fatal(err)
	}
	install.Kind, install.Program = kindDesktop, filepath.Join(root, "ComfyUI.exe")
	if err := os.WriteFile(install.Program, []byte("MZ"), 0o600); err != nil {
		t.Fatal(err)
	}
	report := filepath.Join(root, listeningName)
	standInEnvironment(t, report, base)
	startStandIn(t)
	waitFile(t, report)
	h, shown := testHost(t, install, report)
	standInAdapter(t, h.graph)
	h.launch = func(string) (desktopProcess, error) {
		t.Error("the existing Desktop was launched again")
		return desktopProcess{}, nil
	}
	h.continuity = func(int, string) error { return nil }
	h.armDesktop = func(context.Context, string) error {
		t.Error("the person's Desktop window received an ARM")
		return nil
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done, _ := serve(t, h, ctx)
	port, _ := served(t, h)
	page := waitShown(t, shown, 1)
	if len(page) != 1 || !strings.HasPrefix(page[0], graphPage(port)) {
		t.Fatalf("open browser page: %v", page)
	}
	if err := h.reopen(ctx); err != nil {
		t.Fatal(err)
	}
	next := waitShown(t, shown, 2)
	if len(next) != 2 || !strings.HasPrefix(next[1], graphPage(port)) || next[1] == page[0] {
		t.Fatalf("reopen browser page: %v", next)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

// ComfyUI Desktop and Comfy Desktop are started as the person's own program
// and attached once its server reports. Its own window is what open shows:
// no browser page opens, at the start or on a later open, and it keeps
// running when the host ends. Each showing sends a Desktop arm, which the
// node acknowledges, once the Desktop main process passed the continuity
// check; without that, none. An unsigned Desktop gets no arm: the adapter is
// told once, and its G request opens the browser page.
func TestDesktopIsLaunchedThenAttached(t *testing.T) {
	for _, run := range []struct{ kind, outcome, program, reason string }{
		{kindDesktop, "continuous", "ComfyUI.exe", "desktop_unsigned"},
		{kindDesktop, "discontinuous", "ComfyUI.exe", "desktop_unsigned"},
		{kindDesktop, "unsigned", "ComfyUI.exe", "desktop_unsigned"},
		{kindComfyDesktop, "continuous", "Comfy Desktop.exe", "comfy_desktop_unsigned"},
		{kindComfyDesktop, "discontinuous", "Comfy Desktop.exe", "comfy_desktop_unsigned"},
		{kindComfyDesktop, "unsigned", "Comfy Desktop.exe", "comfy_desktop_unsigned"},
	} {
		outcome := run.outcome
		t.Run(run.kind+"/"+outcome, func(t *testing.T) {
			continuous := outcome == "continuous"
			root := t.TempDir()
			base := filepath.Join(root, "ComfyUI")
			install, err := findInstall(nodePack(t, base), accountFolders{})
			if err != nil {
				t.Fatal(err)
			}
			install.Kind, install.Program = run.kind, filepath.Join(root, "Programs", run.program)
			if err := os.MkdirAll(filepath.Dir(install.Program), 0o700); err != nil || os.WriteFile(install.Program, []byte("MZ"), 0o600) != nil {
				t.Fatal("Desktop program fixture")
			}
			report := filepath.Join(root, listeningName)
			standInEnvironment(t, report, base)
			h, shown := testHost(t, install, report)
			started := desktopProcess{pid: 10, created: 100}
			h.openedDesktop = func(got desktopProcess, _ int, _ string) bool { return got == started }
			frames := standInAdapter(t, h.graph)
			var mu sync.Mutex
			var launched, armed []string
			var checked []int
			h.launch = func(program string) (desktopProcess, error) {
				mu.Lock()
				launched = append(launched, program)
				first := len(launched) == 1
				mu.Unlock()
				if first {
					startStandIn(t)
					return started, nil
				}
				return desktopProcess{pid: 11, created: 101}, nil
			}
			h.continuity = func(python int, program string) error {
				mu.Lock()
				checked = append(checked, python)
				mu.Unlock()
				if outcome == "unsigned" {
					return errDesktopUnsigned
				}
				if !continuous || program != install.Program {
					return errors.New("not continuous")
				}
				return nil
			}
			h.armDesktop = func(ctx context.Context, program string) error {
				err := h.graph.armDesktop(ctx, program)
				if err == nil {
					mu.Lock()
					armed = append(armed, program)
					mu.Unlock()
				}
				return err
			}
			counts := func() (int, int, int) {
				mu.Lock()
				defer mu.Unlock()
				return len(launched), len(checked), len(armed)
			}
			waitChecked := func(n int) {
				for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
					if _, c, _ := counts(); c >= n {
						return
					}
				}
			}
			ctx, cancel := context.WithCancel(context.Background())
			done, _ := serve(t, h, ctx)
			port, _ := served(t, h)
			waitChecked(1)
			time.Sleep(200 * time.Millisecond)
			want := map[bool]int{true: 1, false: 0}[continuous]
			if l, c, a := counts(); l != 1 || c != 1 || a != want || len(waitShown(t, shown, 0)) != 0 || h.owned {
				t.Fatalf("launched=%d checked=%d armed=%d shown=%v owned=%v", l, c, a, waitShown(t, shown, 0), h.owned)
			}
			mu.Lock()
			pid := checked[0]
			mu.Unlock()
			if pid != port0PID(t, h) {
				t.Fatalf("continuity checked process %d, not the served ComfyUI", pid)
			}
			err = h.reopen(ctx)
			if l, c, a := counts(); err != nil || l != 2 || c != 2 || a != 2*want || len(waitShown(t, shown, 0)) != 0 {
				t.Fatalf("reopen: %v launched=%d checked=%d armed=%d shown=%v", err, l, c, a, waitShown(t, shown, 0))
			}
			notices := 0
			for drained := false; !drained; {
				select {
				case m := <-frames:
					if m["type"] == "notice" && len(m) == 2 {
						if m["reason"] != run.reason {
							t.Errorf("notice %v", m)
						}
						notices++
					}
				case <-time.After(300 * time.Millisecond):
					drained = true
				}
			}
			wantNotices := map[bool]int{true: 1, false: 0}[outcome == "unsigned"]
			if notices != wantNotices {
				t.Fatalf("%d unsigned notices, want %d", notices, wantNotices)
			}
			answer := h.browserPage()
			if outcome != "unsigned" {
				if answer != 'N' || len(waitShown(t, shown, 0)) != 0 {
					t.Fatalf("G answered %c for a %s Desktop", answer, outcome)
				}
			} else if pages := waitShown(t, shown, 1); answer != 'A' || len(pages) != 1 || !strings.HasPrefix(pages[0], graphPage(port)) {
				t.Fatalf("G for an unsigned Desktop: %c %v", answer, pages)
			}
			cancel()
			if err := <-done; err != nil {
				t.Fatal(err)
			}
			if err := getQueue(port); err != nil {
				t.Fatalf("the host stopped Desktop's ComfyUI: %v", err)
			}
		})
	}
}

// port0PID is the served ComfyUI's process id.
func port0PID(t *testing.T, h *host) int {
	t.Helper()
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.served == nil {
		t.Fatal("no ComfyUI is served")
	}
	return h.served.PID
}

// A ComfyUI the connector serves without a Desktop program, such as a git
// clone, gets no Desktop arm.
func TestAFolderComfyGetsNoDesktopArm(t *testing.T) {
	h := newHost(comfyInstall{Kind: kindFolder}, "", nil, nil)
	h.continuity = func(int, string) error { t.Fatal("continuity checked"); return nil }
	h.armDesktop = func(context.Context, string) error { t.Fatal("armed"); return nil }
	h.admitDesktopWindow(context.Background(), listening{PID: 1, Port: 8000})
}

// A report naming another ComfyUI is never served, and a ComfyUI the
// connector knows no program for is the person's to start.
func TestAForeignReportIsNotServedAndAnUnknownComfyIsThePersons(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	other := filepath.Join(root, "Other")
	if err := os.MkdirAll(other, 0o700); err != nil {
		t.Fatal(err)
	}
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil {
		t.Fatal(err)
	}
	report := filepath.Join(root, listeningName)
	standInEnvironment(t, report, other)
	startStandIn(t)
	waitFile(t, report)
	h, shown := testHost(t, install, report)
	if err := h.start(context.Background(), func() { t.Fatal("served a foreign ComfyUI") }); !errors.Is(err, errStartYourself) {
		t.Fatalf("%v", err)
	}
	if len(waitShown(t, shown, 0)) != 0 || string(h.attachment()) != "{}" {
		t.Fatal("a foreign ComfyUI was shown or served")
	}
}

// Q answers one bounded frame with the served ComfyUI.
func TestTheControlAnswersTheServedComfy(t *testing.T) {
	s := newNativeStartup(func(context.Context, func()) error { t.Fatal("unexpected start"); return nil })
	defer s.close()
	var output bytes.Buffer
	answer := []byte(`{"port":8190,"output":"C:\\ComfyUI\\output"}`)
	err := nativeControlWith(bytes.NewReader([]byte{'Q', 'P', 'Q'}), &output, s, func() {}, nil, func() []byte { return answer }, nil)
	if err != io.EOF {
		t.Fatal(err)
	}
	for _, want := range [][]byte{answer, []byte("I"), answer} {
		if want[0] == 'I' {
			if b, _ := output.ReadByte(); b != 'I' {
				t.Fatalf("phase %q", b)
			}
			continue
		}
		raw, err := listen.ReadFrameFrom(&output, attachmentLimit)
		if err != nil || !bytes.Equal(raw, want) {
			t.Fatalf("frame %q %v", raw, err)
		}
	}
	output.Reset()
	if err := nativeControlWith(bytes.NewReader([]byte{'Q'}), &output, s, func() {}, nil, nil, nil); err != io.EOF {
		t.Fatal(err)
	}
	if raw, err := listen.ReadFrameFrom(&output, attachmentLimit); err != nil || string(raw) != "{}" {
		t.Fatalf("no attachment owner answers {}: %q %v", raw, err)
	}
}

func nodePackInput(t *testing.T, member string) string {
	t.Helper()
	path := filepath.Join("..", ".installer", "node", member)
	if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
		path = filepath.Join(privateInput(t, "..", "..", "..", "adopters", "comfyui", "node"), member)
	} else if err != nil {
		t.Fatal(err)
	}
	return path
}

// adapterNodePack lays out the node pack the extension MSI installs: the
// adapter and the OA Python SDK it carries in oa_client.
func adapterNodePack(t *testing.T, node string) {
	t.Helper()
	source := nodePackInput(t, adapterMember)
	var extension struct {
		Include []struct{ From, To string } `json:"include"`
	}
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(source), "..", "extension.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &extension); err != nil {
		t.Fatal(err)
	}
	// The installer assembles oa_client from several separate Python source trees.
	includeRoot := privateInput(t, "..", "..", "..", "openabstractions-flat")
	copyFile := func(from, to string) {
		raw, err := os.ReadFile(from)
		if err != nil {
			t.Fatal(err)
		}
		writeFixture(t, to, string(raw))
	}
	copyFile(source, filepath.Join(node, adapterMember))
	for _, include := range extension.Include {
		if !strings.HasPrefix(include.To, "oa_client/") {
			continue
		}
		from := filepath.Join(includeRoot, filepath.FromSlash(include.From))
		err := filepath.WalkDir(from, func(path string, entry os.DirEntry, err error) error {
			if err != nil || entry.IsDir() || !strings.HasSuffix(path, ".py") || strings.HasPrefix(entry.Name(), "test_") {
				return err
			}
			rel, err := filepath.Rel(from, path)
			if err != nil {
				return err
			}
			copyFile(path, filepath.Join(node, filepath.FromSlash(include.To), rel))
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
}

// mcp speaks the owned host's frames to the adapter.
type mcp struct {
	t      *testing.T
	in     io.Writer
	out    io.Reader
	nextID int
}

func (m *mcp) call(method string, params any) map[string]any {
	m.t.Helper()
	m.nextID++
	raw, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": m.nextID, "method": method, "params": params})
	if err != nil {
		m.t.Fatal(err)
	}
	if err := listen.WriteFrameTo(m.in, raw, 1<<20); err != nil {
		m.t.Fatal(err)
	}
	for {
		answer, err := listen.ReadFrameFrom(m.out, 4<<20)
		if err != nil {
			m.t.Fatalf("%s: %v", method, err)
		}
		var reply map[string]any
		if err := json.Unmarshal(answer, &reply); err != nil {
			m.t.Fatal(err)
		}
		if id, ok := reply["id"].(float64); ok && int(id) == m.nextID {
			return reply
		}
	}
}

// tool calls one tool and decodes its text answer.
func (m *mcp) tool(name string, arguments any) map[string]any {
	m.t.Helper()
	reply := m.call("tools/call", map[string]any{"name": name, "arguments": arguments})
	result, _ := reply["result"].(map[string]any)
	content, _ := result["content"].([]any)
	if len(content) == 0 {
		m.t.Fatalf("%s: %v", name, reply)
	}
	var value map[string]any
	if err := json.Unmarshal([]byte(content[0].(map[string]any)["text"].(string)), &value); err != nil {
		m.t.Fatalf("%s: %v", name, reply)
	}
	return value
}

func pythonProgram(t *testing.T) string {
	t.Helper()
	launcher, err := exec.LookPath("py")
	if err != nil {
		t.Skip("no Python launcher on this computer")
	}
	out, err := exec.Command(launcher, "-3.12", "-c", "import sys; print(sys.executable)").Output()
	if err != nil {
		t.Skip("no Python 3.12 on this computer")
	}
	return strings.TrimSpace(string(out))
}

// The connector end to end, with a stand-in ComfyUI in place of the
// person's: the adapter (node/oa_native_host.py with the SDK the node pack
// carries, the installed runtime's application-model library) answers the
// owned host's MCP frames; open attaches to the stand-in once it reports
// {pid, port}; workflow.list reads its HTTP API on the reported port.
// Presence and the runtime's gateway are left out.
func TestTheAdapterDrivesAStandInComfyThroughTheConnector(t *testing.T) {
	python := pythonProgram(t)
	library, err := runtimeLibrary(appmodelLibraryName)
	if err != nil {
		t.Skip(err)
	}
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	connector := nodePack(t, base)
	install, err := findInstall(connector, accountFolders{})
	if err != nil {
		t.Fatal(err)
	}
	install.Python = python
	adapterNodePack(t, install.Node)
	report := filepath.Join(root, listeningName)
	standInEnvironment(t, report, base)
	startStandIn(t)
	waitFile(t, report)
	h, shown := testHost(t, install, report)
	startup := newNativeStartup(h.run)
	h.browserFailed, startup.reopen = startup.browserFailed, h.reopen
	defer startup.close()

	fromAdapter, adapterWrite, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	adapterRead, toAdapter, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range []*os.File{adapterRead, adapterWrite} {
		if err := windows.SetHandleInformation(windows.Handle(file.Fd()), windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT); err != nil {
			t.Fatal(err)
		}
	}
	stdinRead, stdinWrite, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	stdoutRead, stdoutWrite, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	env, err := (supervision.CleanEnvironment{Temp: t.TempDir(), Set: []string{"PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1"}}).Build()
	if err != nil {
		t.Fatal(err)
	}
	graph := h.graph
	settings, err := adapterSettings(install, "instance-fixture", [4]uintptr{adapterRead.Fd(), adapterWrite.Fd(), graph.adapterRead.Fd(), graph.adapterWrite.Fd()}, applicationManifest, library, env)
	if err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	stderrRead, stderrWrite, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	go func() { _, _ = io.Copy(&stderr, stderrRead) }()
	settings.Stdin, settings.Stdout, settings.Stderr = stdinRead, stdoutWrite, stderrWrite
	p, err := supervision.Start(context.Background(), settings)
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range []*os.File{adapterRead, adapterWrite, graph.adapterRead, graph.adapterWrite, stdinRead, stdoutWrite, stderrWrite} {
		_ = file.Close()
	}
	retired := make(chan struct{})
	go func() {
		_ = nativeControlWith(fromAdapter, toAdapter, startup, func() { close(retired) }, graph.acknowledgeArm, h.attachment, h.browserPage)
	}()
	ended := false
	defer func() {
		if !ended {
			_ = p.Stop()
			_, _ = p.Wait()
		}
		if t.Failed() {
			t.Log("adapter output:\n" + stderr.String())
		}
	}()
	session := &mcp{t: t, in: stdinWrite, out: stdoutRead}
	if reply := session.call("initialize", map[string]any{"protocolVersion": "2025-06-18", "capabilities": map[string]any{}, "clientInfo": map[string]any{"name": "fixture", "version": "1"}}); reply["result"] == nil {
		t.Fatalf("initialize: %v", reply)
	}
	if opened := session.tool("open", map[string]any{}); opened["outcome"] != "applied" {
		t.Fatalf("open: %v", opened)
	}
	contexts := session.tool("app_contexts", map[string]any{})
	var workflows map[string]any
	for _, row := range contexts["contexts"].([]any) {
		if r, ok := row.(map[string]any)["context"].(map[string]any); ok && r["context"] == "workflows" {
			workflows = r
		}
	}
	if workflows == nil {
		t.Fatalf("no workflows context in %v", contexts)
	}
	listed := session.tool("workflow.list", map[string]any{"context": workflows, "arguments": map[string]any{}, "mode": "apply",
		"expected_revision": "", "idempotency_key": ""})
	if listed["outcome"] != "applied" {
		t.Fatalf("workflow.list: %v", listed)
	}
	var effects struct {
		Workflows []struct{ Name string } `json:"workflows"`
	}
	raw, err := json.Marshal(listed["effects"])
	if err != nil || json.Unmarshal(raw, &effects) != nil {
		t.Fatalf("workflow.list effects: %v", listed)
	}
	var names []string
	for _, row := range effects.Workflows {
		names = append(names, row.Name)
	}
	if strings.Join(names, ",") != "template:comfyui-openabstractions/oa_image_generate_preview_save,user:cats.json,user:sub/dogs.json" &&
		strings.Join(names, ",") != "user:cats.json,user:sub/dogs.json,template:comfyui-openabstractions/oa_image_generate_preview_save" {
		t.Fatalf("workflows %v", names)
	}
	port, _ := served(t, h)
	if pages := waitShown(t, shown, 1); len(pages) != 1 || !strings.HasPrefix(pages[0], graphPage(port)) {
		t.Fatalf("open showed %v", pages)
	}
	// The attached ComfyUI's page projects its canvas over the graph
	// channel: graph.read reads KSampler 7, graph.edit edits it, graph.run
	// queues it on the page and job.status answers its output.
	var graphContext map[string]any
	for deadline := time.Now().Add(30 * time.Second); graphContext == nil && time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
		for _, row := range session.tool("app_contexts", map[string]any{})["contexts"].([]any) {
			if r, ok := row.(map[string]any)["context"].(map[string]any); ok && r["context"] == "graph" {
				graphContext = r
			}
		}
	}
	if graphContext == nil {
		t.Fatal("the attached ComfyUI's graph context did not appear")
	}
	command := func(name string, arguments map[string]any, key string) map[string]any {
		t.Helper()
		contexts := session.tool("app_contexts", map[string]any{})["contexts"].([]any)
		var revision string
		for _, row := range contexts {
			if r, ok := row.(map[string]any)["context"].(map[string]any); ok && r["context"] == "graph" {
				revision, _ = row.(map[string]any)["revision"].(string)
			}
		}
		return session.tool(name, map[string]any{"context": graphContext, "arguments": arguments, "mode": "apply",
			"expected_revision": revision, "idempotency_key": key})
	}
	type canvas struct {
		Revision string `json:"revision"`
		Nodes    []struct {
			ID      string `json:"id"`
			Mode    string `json:"mode"`
			Widgets []struct {
				Name  string `json:"name"`
				Type  string `json:"type"`
				Value any    `json:"value"`
			} `json:"widgets"`
		} `json:"nodes"`
	}
	read := func() canvas {
		t.Helper()
		answer := command("graph.read", map[string]any{}, "")
		var view canvas
		raw, _ := json.Marshal(answer["effects"])
		if answer["outcome"] != "applied" || json.Unmarshal(raw, &view) != nil || len(view.Nodes) != 1 || len(view.Nodes[0].Widgets) != 1 {
			t.Fatalf("graph.read: %v", answer)
		}
		return view
	}
	view := read()
	if view.Nodes[0].ID != "7" || view.Nodes[0].Widgets[0].Name != "steps" || view.Nodes[0].Widgets[0].Value != float64(20) || view.Revision == "" {
		t.Fatalf("graph.read: %+v", view)
	}
	edited := command("graph.edit", map[string]any{"ops": []any{
		map[string]any{"op": "set_widget", "node": "7", "widget": "steps", "value": 24},
		map[string]any{"op": "set_mode", "node": "7", "mode": "bypass"}}, "expected_revision": view.Revision}, "edit-steps-24")
	if edited["outcome"] != "applied" {
		t.Fatalf("graph.edit: %v", edited)
	}
	after := read()
	if after.Nodes[0].Widgets[0].Value != float64(24) || after.Nodes[0].Mode != "bypass" || after.Revision == view.Revision {
		t.Fatalf("the edit did not reach the canvas: %+v", after)
	}
	stale := command("graph.edit", map[string]any{"ops": []any{map[string]any{"op": "set_mode", "node": "7", "mode": "always"}},
		"expected_revision": view.Revision}, "edit-stale")
	if stale["outcome"] != "conflict" || !strings.Contains(fmt.Sprint(stale["reason"]), after.Revision) {
		t.Fatalf("a stale revision: %v", stale)
	}
	if revealed := session.tool("app_reveal", map[string]any{"context": graphContext, "entities": []any{"node-7"}, "style": "scroll_to"}); revealed["outcome"] != "applied" {
		t.Fatalf("app_reveal: %v", revealed)
	}
	_, output := served(t, h)
	if err := os.MkdirAll(output, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(output, standInOutput), []byte("glTF stand-in"), 0o600); err != nil {
		t.Fatal(err)
	}
	ran := command("graph.run", map[string]any{"expected_revision": after.Revision}, "run-canvas")
	job, _ := ran["job"].(string)
	if ran["outcome"] != "accepted" || job == "" {
		t.Fatalf("graph.run: %v", ran)
	}
	var status map[string]any
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(200 * time.Millisecond) {
		answer := command("job.status", map[string]any{"job": job}, "")
		status, _ = answer["effects"].(map[string]any)
		if status != nil && status["state"] != "queued" && status["state"] != "running" {
			break
		}
	}
	outputs, _ := status["outputs"].([]any)
	if status["state"] != "done" || len(outputs) != 1 {
		t.Fatalf("job.status: %v", status)
	}
	sum := sha256.Sum256([]byte("glTF stand-in"))
	reference := outputs[0].(map[string]any)
	if reference["name"] != standInOutput || reference["digest"] != "sha256:"+hex.EncodeToString(sum[:]) || reference["size"] != float64(13) ||
		reference["kind"] != "file" || reference["path"] != nil {
		t.Fatalf("job.status output: %v", reference)
	}
	// The owned frames end: mediation retires, the attached ComfyUI keeps
	// running, and the adapter ends with the host's close.
	_ = stdinWrite.Close()
	select {
	case <-retired:
	case <-time.After(30 * time.Second):
		t.Fatal("mediation did not retire")
	}
	if err := getQueue(port); err != nil {
		t.Fatalf("retirement stopped the person's ComfyUI: %v", err)
	}
	startup.close()
	exited := make(chan error, 1)
	go func() {
		code, err := p.Wait()
		if err == nil && code != 0 {
			err = fmt.Errorf("adapter exit code %d", code)
		}
		exited <- err
	}()
	select {
	case err := <-exited:
		ended = true
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the adapter did not end once its ComfyUI was no longer served")
	}
}

// An attached ComfyUI's node connects the graph channel itself: the page's
// projection reaches the adapter, an edit request reaches the page and
// its applied result comes back. Once that ComfyUI ends, the adapter learns
// its page closed, and a request answers unavailable at once.
func TestAnAttachedComfyCarriesTheGraphBothWays(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil {
		t.Fatal(err)
	}
	report := filepath.Join(root, listeningName)
	standInEnvironment(t, report, base)
	comfy := startStandIn(t)
	waitFile(t, report)
	h, _ := testHost(t, install, report)
	frames := standInAdapter(t, h.graph)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done, _ := serve(t, h, ctx)
	projection := nextFrame(t, frames, "projection")
	if projection["page"] != standInPage || projection["generation"] != h.graph.generation {
		t.Fatalf("projection %v", projection)
	}
	request := map[string]any{"type": "request", "id": "request_000000000001", "operation": "edit",
		"generation": h.graph.generation, "page": standInPage, "workflow": standInWorkflow, "sequence": 0,
		"arguments": map[string]any{"ops": []any{map[string]any{"op": "set_widget", "node": "7", "widget": "steps", "value": 24}}}}
	adapterRequest(t, h.graph, request)
	result := nextFrame(t, frames, "result")
	projected, _ := result["projection"].(map[string]any)
	if result["id"] != request["id"] || result["outcome"] != "applied" || projected["sequence"] != float64(1) ||
		!strings.Contains(fmt.Sprint(projected["graph"]), "value:24") {
		t.Fatalf("result %v", result)
	}
	_ = comfy.Process.Kill()
	closed := nextFrame(t, frames, "closed")
	if closed["page"] != standInPage || closed["reason"] != "native_closed" {
		t.Fatalf("closed %v", closed)
	}
	request["id"] = "request_000000000002"
	adapterRequest(t, h.graph, request)
	if result := nextFrame(t, frames, "result"); result["id"] != request["id"] || result["outcome"] != "unavailable" {
		t.Fatalf("a request without a channel: %v", result)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the attached ComfyUI's exit did not end the serving")
	}
	if _, err := os.Stat(channelFile(root, comfy.Process.Pid)); !os.IsNotExist(err) {
		t.Fatalf("the channel's name outlived the serving: %v", err)
	}
}

// A process other than the served ComfyUI, in the same account, cannot join
// the graph channel: the connector ends its connection, relays nothing it
// wrote and keeps ComfyUI's own channel.
func TestAForeignProcessCannotJoinTheGraphChannel(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil {
		t.Fatal(err)
	}
	report := filepath.Join(root, listeningName)
	standInEnvironment(t, report, base)
	comfy := startStandIn(t)
	waitFile(t, report)
	h, _ := testHost(t, install, report)
	frames := standInAdapter(t, h.graph)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serve(t, h, ctx)
	nextFrame(t, frames, "projection")
	kept := h.graph.current()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	foreign := exec.Command(self)
	foreign.Env = append(os.Environ(), "OA_STANDIN_COMFY=", "OA_STANDIN_FOREIGN="+channelFile(root, comfy.Process.Pid))
	if err := foreign.Run(); err != nil {
		t.Fatalf("the foreign process was not refused: %v", err)
	}
	if h.graph.current() != kept || kept == nil {
		t.Fatal("the foreign process replaced ComfyUI's channel")
	}
	select {
	case m := <-frames:
		t.Fatalf("the foreign process reached the adapter: %v", m)
	case <-time.After(500 * time.Millisecond):
	}
	// ComfyUI's own channel still carries the graph.
	adapterRequest(t, h.graph, map[string]any{"type": "request", "id": "request_000000000003", "operation": "reveal",
		"generation": h.graph.generation, "page": standInPage, "workflow": standInWorkflow, "sequence": 0,
		"arguments": map[string]any{"node": "7"}})
	if result := nextFrame(t, frames, "result"); result["outcome"] != "applied" {
		t.Fatalf("ComfyUI's channel after the foreign try: %v", result)
	}
}

// nodeScript runs the node pack's own graph channel owner
// (node/oa_appmodel_bridge.py) as a ComfyUI process would: it listens on a
// loopback port, reports it, and connects each channel the connector names.
const nodeScript = `import asyncio, importlib.util, json, os, socket, sys, threading
node, stream, folder, base = sys.argv[1:5]
sys.path.insert(0, stream)
from abstraction.ipc.stream import read_frame_from, write_frame_to
spec = importlib.util.spec_from_file_location("oa_appmodel_bridge", os.path.join(node, "oa_appmodel_bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
report = os.path.join(folder, "listening.json")
with open(report + ".tmp", "w", encoding="utf-8") as stream:
    json.dump({"pid": os.getpid(), "port": listener.getsockname()[1], "base": base, "output": ""}, stream)
os.replace(report + ".tmp", report)
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
bridge.watch_channel(folder, {"bridge": None}, loop, read_frame_from, write_frame_to)
`

// The node pack's own Python owner connects the connector's graph channel
// from an attached ComfyUI process: the connector's ARM reaches its Bridge,
// the page opens on its port, and an adapter request comes back answered.
func TestTheNodePackConnectsTheGraphChannel(t *testing.T) {
	python := pythonProgram(t)
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	install, err := findInstall(nodePack(t, base), accountFolders{})
	if err != nil {
		t.Fatal(err)
	}
	node := filepath.Dir(nodePackInput(t, "oa_appmodel_bridge.py"))
	// Identity's Go module also ships its Python framing SDK. Resolve the same
	// pinned dependency the connector used to build in this checkout.
	identity := exec.CommandContext(t.Context(), "go", "list", "-m", "-f", "{{.Dir}}", "github.com/openabstractions/abstraction-identity")
	identity.Env = append(os.Environ(), "GOPROXY=off", "GONOPROXY=none")
	outputPath, err := identity.Output()
	if err != nil {
		t.Fatalf("locate Identity module: %v", err)
	}
	sdk := strings.TrimSpace(string(outputPath))
	if !filepath.IsAbs(sdk) {
		t.Fatalf("Identity module has no local directory: %q", sdk)
	}
	script := filepath.Join(root, "node.py")
	writeFixture(t, script, nodeScript)
	comfy := exec.Command(python, "-I", "-B", script, node, filepath.Join(sdk, "py"), root, base)
	var output bytes.Buffer
	comfy.Stdout, comfy.Stderr = &output, &output
	if err := comfy.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = comfy.Process.Kill()
		_, _ = comfy.Process.Wait()
		if t.Failed() {
			t.Log("node output:\n" + output.String())
		}
	})
	report := filepath.Join(root, listeningName)
	waitFile(t, report)
	h, shown := testHost(t, install, report)
	frames := standInAdapter(t, h.graph)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serve(t, h, ctx)
	port, _ := served(t, h)
	if pages := waitShown(t, shown, 1); len(pages) != 1 || !strings.HasPrefix(pages[0], graphPage(port)) {
		t.Fatalf("shown %v", pages)
	}
	adapterRequest(t, h.graph, map[string]any{"type": "request", "id": "request_000000000004", "operation": "edit",
		"generation": h.graph.generation, "page": standInPage, "workflow": standInWorkflow, "sequence": 0,
		"arguments": map[string]any{"ops": []any{map[string]any{"op": "set_widget", "node": "7", "widget": "steps", "value": 24}}}})
	if result := nextFrame(t, frames, "result"); result["id"] != "request_000000000004" || result["outcome"] != "stale" {
		t.Fatalf("the node's answer without a page: %v", result)
	}
}
