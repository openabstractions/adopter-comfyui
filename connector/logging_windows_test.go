//go:build windows

package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// comfyLoggingScript is ComfyUI's side: the node pack's relay
// (node/oa_relay.py) registered as ComfyUI loads it, and the OA nodes'
// logging adoption (adopter-comfyui/oa_logging.py) on the root logger. A
// ComfyUI logger then writes one warning carrying a credential.
const comfyLoggingScript = `
import importlib, json, logging, os, sys, types
node, flat, marker = sys.argv[1:4]
for layer in ("abstraction-identity", "abstraction-facade", "abstraction-logging"):
    sys.path.insert(0, os.path.join(flat, layer, "py"))
for name, folder in (("comfyui_openabstractions", node), ("oa_nodes", os.path.join(flat, "adopter-comfyui"))):
    package = types.ModuleType(name)
    package.__path__ = [folder]
    sys.modules[name] = package
importlib.import_module("comfyui_openabstractions.oa_relay").register()
oa_logging = importlib.import_module("oa_nodes.oa_logging")
os.environ[oa_logging.ENV] = "service"
installed = oa_logging.install()
logging.getLogger("comfyui.e2e").warning("%s fetched https://user:pa55word@example.com/m", marker)
oa_logging._handler.flush(60)
status = oa_logging.status()
json.dump({"installed": installed.code, "code": status.code, "written": status.written, "detail": status.detail}, sys.stdout)
`

// The node pack's logging reaches the installed runtime's logging service
// through the connector's native relay. The service records the connector
// as the record's verified emitter, and the credential arrives redacted.
// Needs Python 3.12 and an installed, running OpenAbstractions runtime.
func TestComfyLoggingReachesTheServiceAsTheConnector(t *testing.T) {
	python := pythonProgram(t)
	if _, err := runtimeLibrary(ipcLibraryName); err != nil {
		t.Skip(err)
	}
	records := filepath.Join(os.Getenv("LOCALAPPDATA"), "OpenAbstractions", "logging", "records.jsonl")
	if _, err := os.Stat(records); err != nil {
		t.Skip("no OpenAbstractions logging records on this computer: ", err)
	}
	connector, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	repo, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	nonce := make([]byte, 12)
	if _, err := rand.Read(nonce); err != nil {
		t.Fatal(err)
	}
	// Groups of eight keep the marker readable through redaction.
	digits := hex.EncodeToString(nonce)
	marker := "comfy-logging-e2e-" + digits[:8] + "-" + digits[8:16] + "-" + digits[16:]

	comfy := exec.Command(python, "-I", "-B", "-c", comfyLoggingScript, filepath.Join(repo, "adopters", "comfyui", "node"),
		filepath.Join(repo, "openabstractions-flat"), marker)
	comfy.Env = append(os.Environ(), "ABSTRACTION_COMFYUI_NATIVE_RELAY="+connector, "PYTHONIOENCODING=utf-8")
	var stderr bytes.Buffer
	comfy.Stderr = &stderr
	out, err := comfy.Output()
	if err != nil {
		t.Fatalf("%v\n%s", err, stderr.String())
	}
	var status struct {
		Installed, Code, Detail string
		Written                 int
	}
	if err := json.Unmarshal(out, &status); err != nil {
		t.Fatalf("%v: %s\n%s", err, out, stderr.String())
	}
	if status.Installed != "attached" || status.Code != "attached" || status.Written < 1 {
		t.Fatalf("logging status %+v\n%s", status, stderr.String())
	}

	var row struct {
		Msg     string `json:"msg"`
		Emitter struct {
			Program string `json:"program"`
		} `json:"_oa_emitter"`
		Attrs map[string]string `json:"attrs"`
	}
	for deadline := time.Now().Add(20 * time.Second); ; time.Sleep(100 * time.Millisecond) {
		if line := findRecord(t, records, marker); line != nil {
			if err := json.Unmarshal(line, &row); err != nil {
				t.Fatalf("%v: %s", err, line)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no record carrying %s in %s", marker, records)
		}
	}
	if !strings.EqualFold(filepath.Clean(row.Emitter.Program), filepath.Clean(connector)) {
		t.Fatalf("emitter %q, want the connector %q", row.Emitter.Program, connector)
	}
	if want := marker + " fetched https://[redacted]@example.com/m"; row.Msg != want {
		t.Fatalf("message %q, want %q", row.Msg, want)
	}
	if row.Attrs["logger"] != "comfyui.e2e" {
		t.Fatalf("attributes %v", row.Attrs)
	}
}

// findRecord is the last line of the records file that carries marker.
func findRecord(t *testing.T, records, marker string) []byte {
	t.Helper()
	f, err := os.Open(records)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		t.Fatal(err)
	}
	// The record is new: read the file's last 8 MiB only.
	if size := info.Size(); size > 8<<20 {
		if _, err := f.Seek(size-8<<20, 0); err != nil {
			t.Fatal(err)
		}
	}
	var found []byte
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1<<20), 4<<20)
	for scanner.Scan() {
		if bytes.Contains(scanner.Bytes(), []byte(marker)) {
			found = append([]byte(nil), scanner.Bytes()...)
		}
	}
	return found
}
