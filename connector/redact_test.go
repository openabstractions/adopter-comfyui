package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/openabstractions/abstraction-facade/go-core/redact/redacttest"
)

// The node pack redacts ComfyUI's log records before they leave through the
// connector (adopter-comfyui/oa_redact.py). Both its one-line reading and
// its multi-line reading answer OA's own bypass table.
const redactScript = `
import json, sys
sys.path.insert(0, sys.argv[1])
import oa_redact
cases = json.loads(sys.stdin.buffer.read().decode("utf-8"))
json.dump([[oa_redact.line(c), oa_redact.text(c)] for c in cases], sys.stdout)
`

func TestTheNodePackRedactsAsOADoes(t *testing.T) {
	python := redactPython(t)
	source := redactSourceDir(t)
	inputs := make([]string, len(redacttest.Cases))
	for i, c := range redacttest.Cases {
		inputs[i] = c.In
	}
	raw, err := json.Marshal(inputs)
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(python, "-I", "-B", "-c", redactScript, source)
	cmd.Stdin = bytes.NewReader(raw)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("%v: %s", err, stderr.String())
	}
	var got [][2]string
	if err := json.Unmarshal(out, &got); err != nil || len(got) != len(inputs) {
		t.Fatalf("%v: %s", err, out)
	}
	for i, c := range redacttest.Cases {
		line, text := got[i][0], got[i][1]
		if line != c.Want {
			t.Errorf("line(%q) = %q, want %q", c.In, line, c.Want)
		}
		if joined := strings.Join(strings.Fields(text), " "); joined != c.Want {
			t.Errorf("text(%q) = %q, want %q once spaces join", c.In, text, c.Want)
		}
		for _, secret := range redacttest.Secrets {
			if strings.Contains(text, secret) && !strings.Contains(c.Want, secret) {
				t.Errorf("text(%q) keeps %q", c.In, secret)
			}
		}
	}
	// A traceback keeps its lines and indentation.
	cmd = exec.Command(python, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import oa_redact; "+
		"sys.stdout.buffer.write(oa_redact.text('Traceback:\\n  File \"x.py\", line 3\\n    get(\"https://u:pa55word@host/m\")\\nError: token=abc123').encode())",
		source)
	out, err = cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	want := "Traceback:\n  File \"x.py\", line 3\n    get(\"https://[redacted]@host/m\")\nError: token=[redacted]"
	if string(out) != want {
		t.Fatalf("text kept %q, want %q", out, want)
	}
}

// A long run of name characters in one log record redacts in well under a
// second. Unbounded name parts made Python's backtracking cubic: an 8 KB
// record of "pw-" held the interpreter lock for minutes and froze ComfyUI.
const redactTimingScript = `
import json, sys, time
sys.path.insert(0, sys.argv[1])
import oa_redact
took = {}
for value in ("pw-" * 2730, "a." * 4096, "sig." * 2048, "key." * 2048, "pw-" * 2729 + "=", "a-" * 4000 + " is x"):
    start = time.perf_counter()
    oa_redact.line(value)
    oa_redact.text(value)
    took[value[:8] + "..." + str(len(value))] = time.perf_counter() - start
json.dump(took, sys.stdout)
`

func TestTheNodePackRedactsALongNameRunQuickly(t *testing.T) {
	python := redactPython(t)
	source := redactSourceDir(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, python, "-I", "-B", "-c", redactTimingScript, source)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if ctx.Err() != nil {
		t.Fatal("redacting the long name runs took over two minutes")
	}
	if err != nil {
		t.Fatalf("%v: %s", err, stderr.String())
	}
	var took map[string]float64
	if err := json.Unmarshal(out, &took); err != nil || len(took) == 0 {
		t.Fatalf("%v: %s", err, out)
	}
	for value, seconds := range took {
		if seconds >= 1 {
			t.Errorf("line and text of %s took %.2fs, want under 1s", value, seconds)
		}
	}
}

// redactSourceDir locates the node pack in the published or private checkout.
func redactSourceDir(t *testing.T) string {
	t.Helper()
	source := filepath.Join("..", "oa_redact.py")
	if info, err := os.Stat(source); err != nil || !info.Mode().IsRegular() {
		source = privateInput(t, "..", "..", "..", "openabstractions-flat", "adopter-comfyui", "oa_redact.py")
	}
	path, err := filepath.Abs(source)
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Dir(path)
}

// privateInput resolves a fixture present in the private workspace layout.
func privateInput(t *testing.T, parts ...string) string {
	t.Helper()
	path := filepath.Join(parts...)
	if _, err := os.Stat(path); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			t.Skipf("%s is private-workspace input this repository does not publish; the private gate runs this check", filepath.ToSlash(path))
		}
		t.Fatal(err)
	}
	return path
}

func redactPython(t *testing.T) string {
	t.Helper()
	launcher, err := exec.LookPath("py")
	if err != nil {
		if python, err := exec.LookPath("python3"); err == nil {
			return python
		}
		t.Skip("no Python on this computer")
	}
	out, err := exec.Command(launcher, "-3.12", "-c", "import sys; print(sys.executable)").Output()
	if err != nil {
		t.Skip("no Python 3.12 on this computer")
	}
	return strings.TrimSpace(string(out))
}
