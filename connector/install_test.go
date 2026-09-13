package main

import (
	"errors"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"

	identity "github.com/openabstractions/abstraction-identity"
)

func writeFixture(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func jsonPath(path string) string { return strings.ReplaceAll(path, `\`, `\\`) }

// nodePack makes base\custom_nodes\comfyui-openabstractions and returns the
// connector's place in it.
func nodePack(t *testing.T, base string) string {
	t.Helper()
	connector := filepath.Join(base, "custom_nodes", nodeFolder, "comfyui-connector.exe")
	writeFixture(t, connector, "connector")
	return connector
}

// The connector serves the ComfyUI around it and starts it by that
// ComfyUI's own layout: a portable folder's python_embeded, ComfyUI
// Desktop's program when its config names this folder, Comfy Desktop's by
// its installs folder, and nothing for any other folder.
func TestTheConnectorFindsTheComfyUIAroundIt(t *testing.T) {
	root := t.TempDir()
	local, roaming := filepath.Join(root, "Local"), filepath.Join(root, "Roaming")
	folders := accountFolders{Local: local, Roaming: roaming}

	portable := filepath.Join(root, "ComfyUI_windows_portable")
	base := filepath.Join(portable, "ComfyUI")
	writeFixture(t, filepath.Join(base, "main.py"), "")
	writeFixture(t, filepath.Join(portable, "python_embeded", "python.exe"), "")
	found, err := findInstall(nodePack(t, base), folders)
	if err != nil {
		t.Fatal(err)
	}
	python := filepath.Join(portable, "python_embeded", "python.exe")
	if found.Kind != kindPortable || found.Base != base || found.Program != python || found.Dir != portable ||
		strings.Join(found.Args, " ") != "-s "+filepath.Join(base, "main.py") || found.Python != python {
		t.Fatalf("portable %+v", found)
	}
	writeFixture(t, filepath.Join(found.Node, "helper", "python.exe"), "")
	if again, _ := findInstall(nodePack(t, base), folders); again.Python != filepath.Join(found.Node, "helper", "python.exe") {
		t.Fatalf("the pack's own interpreter copy comes first: %+v", again)
	}

	desktop := filepath.Join(root, "Documents", "ComfyUI")
	writeFixture(t, filepath.Join(roaming, "ComfyUI", "config.json"), `{"basePath":"`+jsonPath(desktop)+`"}`)
	program := filepath.Join(local, "Programs", "@comfyorgcomfyui-electron", "ComfyUI.exe")
	writeFixture(t, program, "")
	writeFixture(t, filepath.Join(desktop, ".venv", "Scripts", "python.exe"), "")
	found, err = findInstall(nodePack(t, desktop), folders)
	if err != nil || found.Kind != kindDesktop || found.Program != program || found.Python != filepath.Join(desktop, ".venv", "Scripts", "python.exe") {
		t.Fatalf("desktop %+v %v", found, err)
	}

	installed := filepath.Join(local, "Comfy-Desktop", "ComfyUI-Installs", "one")
	comfy := filepath.Join(local, "Programs", "Comfy Desktop", "Comfy Desktop.exe")
	writeFixture(t, comfy, "")
	writeFixture(t, filepath.Join(local, "Programs", "Comfy Desktop", "Uninstall Comfy Desktop.exe"), "")
	found, err = findInstall(nodePack(t, installed), folders)
	if err != nil || found.Kind != kindComfyDesktop || found.Program != comfy {
		t.Fatalf("comfy desktop %+v %v", found, err)
	}

	// An install Comfy Desktop keeps in another folder is found through
	// installations.json; one still installing, or Comfy Cloud, is not.
	listed := filepath.Join(root, "D", "Comfy", "Second")
	writeFixture(t, filepath.Join(listed, "ComfyUI", ".venv", "Scripts", "python.exe"), "")
	entries := func(status string) string {
		return `[{"id":"inst-2","status":"installed","sourceId":"cloud","remoteUrl":"https://cloud.comfy.org/"},` +
			`{"id":"inst-1","installPath":"` + jsonPath(listed) + `","sourceId":"standalone","status":"` + status + `"}]`
	}
	writeFixture(t, filepath.Join(roaming, "Comfy Desktop", "installations.json"), entries("installing"))
	if found, err = findInstall(nodePack(t, filepath.Join(listed, "ComfyUI")), folders); err != nil || found.Kind != kindFolder {
		t.Fatalf("an install still installing %+v %v", found, err)
	}
	writeFixture(t, filepath.Join(roaming, "Comfy Desktop", "installations.json"), entries("installed"))
	found, err = findInstall(nodePack(t, filepath.Join(listed, "ComfyUI")), folders)
	wantKind, wantProgram := wantListedDesktop(comfy)
	if err != nil || found.Kind != wantKind || found.Program != wantProgram ||
		found.Python != filepath.Join(listed, "ComfyUI", ".venv", "Scripts", "python.exe") {
		t.Fatalf("listed comfy desktop %+v %v", found, err)
	}

	clone := filepath.Join(root, "src", "ComfyUI")
	found, err = findInstall(nodePack(t, clone), folders)
	if err != nil || found.Kind != kindFolder || found.Program != "" || found.Python != "" {
		t.Fatalf("clone %+v %v", found, err)
	}

	for _, outside := range []string{filepath.Join(root, "comfyui-connector.exe"), filepath.Join(base, "custom_nodes", "other", "comfyui-connector.exe")} {
		if _, err := findInstall(outside, folders); !errors.Is(err, errNotInNodePack) {
			t.Fatalf("%s: %v", outside, err)
		}
	}
}

// A settings file of another shape, or one naming another folder, makes no
// Desktop: the connector never starts a program for a folder it cannot tie
// to that program.
func TestDesktopSettingsAreReadDefensively(t *testing.T) {
	root := t.TempDir()
	local, roaming := filepath.Join(root, "Local"), filepath.Join(root, "Roaming")
	writeFixture(t, filepath.Join(local, "Programs", "@comfyorgcomfyui-electron", "ComfyUI.exe"), "")
	base := filepath.Join(root, "ComfyUI")
	connector := nodePack(t, base)
	other := filepath.Join(root, "other")
	if err := os.MkdirAll(other, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, body := range []string{"", "[]", `{"basePath":3}`, `{"basePath":"relative"}`, `{"basePath":"` + jsonPath(other) + `"}`,
		strings.Repeat(" ", settingsLimit+1)} {
		writeFixture(t, filepath.Join(roaming, "ComfyUI", "config.json"), body)
		if found, err := findInstall(connector, accountFolders{Local: local, Roaming: roaming}); err != nil || found.Kind != kindFolder || found.Program != "" {
			t.Fatalf("%q: %+v %v", body, found, err)
		}
	}
}

func socketsOf(rows ...identity.Socket) func() ([]identity.Socket, error) {
	return func() ([]identity.Socket, error) { return rows, nil }
}

// A report serves only when its process holds the reported listener and its
// folder is the connector's ComfyUI; any other report is stale or foreign.
func TestTheReportIsProvedByTheListenerAndTheFolder(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "ComfyUI")
	other := filepath.Join(root, "Other")
	for _, folder := range []string{base, other} {
		if err := os.MkdirAll(folder, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	report := filepath.Join(root, "listening.json")
	if _, err := readListening(report); !errors.Is(err, errNoReport) {
		t.Fatalf("absent report: %v", err)
	}
	for _, body := range []string{"{", `{"pid":0,"port":8188,"base":"` + jsonPath(base) + `"}`, `{"pid":7,"port":70000,"base":"` + jsonPath(base) + `"}`,
		`{"pid":7,"port":8188,"base":"relative"}`} {
		writeFixture(t, report, body)
		if _, err := readListening(report); err == nil {
			t.Fatalf("accepted %s", body)
		}
	}
	writeFixture(t, report, `{"pid":7,"port":8190,"base":"`+jsonPath(base)+`","output":"relative","extra":true}`)
	r, err := readListening(report)
	if err != nil || r.PID != 7 || r.Port != 8190 || r.Output != "" {
		t.Fatalf("report %+v %v", r, err)
	}
	loopback := netip.MustParseAddr("127.0.0.1")
	any4, wide := netip.MustParseAddr("0.0.0.0"), netip.MustParseAddr("192.168.1.2")
	for _, tc := range []struct {
		name  string
		base  string
		rows  []identity.Socket
		owned func(int) bool
		ok    bool
	}{
		{"loopback", base, []identity.Socket{{Proto: "tcp", Addr: loopback, Port: 8190, PID: 7}}, nil, true},
		{"any address", base, []identity.Socket{{Proto: "tcp", Addr: any4, Port: 8190, PID: 7}}, nil, true},
		{"another process", base, []identity.Socket{{Proto: "tcp", Addr: loopback, Port: 8190, PID: 8}}, nil, false},
		{"another port", base, []identity.Socket{{Proto: "tcp", Addr: loopback, Port: 8188, PID: 7}}, nil, false},
		{"another address", base, []identity.Socket{{Proto: "tcp", Addr: wide, Port: 8190, PID: 7}}, nil, false},
		{"udp", base, []identity.Socket{{Proto: "udp", Addr: loopback, Port: 8190, PID: 7}}, nil, false},
		{"another folder", other, []identity.Socket{{Proto: "tcp", Addr: loopback, Port: 8190, PID: 7}}, nil, false},
		{"owned", base, []identity.Socket{{Proto: "tcp", Addr: loopback, Port: 8190, PID: 7}}, func(pid int) bool { return pid == 7 }, true},
		{"not owned", base, []identity.Socket{{Proto: "tcp", Addr: loopback, Port: 8190, PID: 7}}, func(int) bool { return false }, false},
	} {
		if err := checkListening(r, tc.base, socketsOf(tc.rows...), tc.owned); (err == nil) != tc.ok {
			t.Errorf("%s: %v", tc.name, err)
		}
	}
	failing := func() ([]identity.Socket, error) { return nil, errors.New("table unreadable") }
	if err := checkListening(r, base, failing, nil); err == nil {
		t.Fatal("an unreadable owner table served")
	}
}
