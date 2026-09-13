package main

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/openabstractions/abstraction-identity/fileid"
	"github.com/openabstractions/adopter-comfyui/connector/installations"
)

// The connector ships inside the integration's node pack, which the
// extension MSI places in the ComfyUI it connects:
//
//	<ComfyUI folder>\custom_nodes\comfyui-openabstractions\comfyui-connector.exe
//
// The ComfyUI it serves is the one around it. It never names another
// folder or a program from a file someone could edit: the program open
// starts comes from that ComfyUI's own layout. Comfy Desktop's layout is not
// documented, so every read of its settings is bounded and a file of another
// shape finds nothing (the same rules as Setup's discovery,
// monitor/internal/setup/comfy_official.go).

// The kinds of ComfyUI the connector serves.
const (
	kindPortable     = "portable"
	kindDesktop      = "desktop"
	kindComfyDesktop = "comfy-desktop"
	kindFolder       = "folder"
)

// nodeFolder is the node pack's folder name (adopters/comfyui/extension.json).
const nodeFolder = "comfyui-openabstractions"

// comfyInstall is the ComfyUI this connector serves.
type comfyInstall struct {
	// Base is the ComfyUI folder, the one holding custom_nodes.
	Base string
	// Node is the node pack's folder, the connector's own.
	Node string
	// Kind is kindPortable, kindDesktop, kindComfyDesktop or kindFolder.
	Kind string
	// Program is what open starts: a portable ComfyUI's python.exe, or the
	// Desktop program. "" when the connector knows none.
	Program string
	// Args and Dir are a portable start's arguments and working folder.
	Args []string
	Dir  string
	// Python is the interpreter the connector runs its adapter with, "" when
	// none is found.
	Python string
}

// accountFolders are %LOCALAPPDATA% and %APPDATA%.
type accountFolders struct {
	Local, Roaming string
}

const settingsLimit = 1 << 20
const entryLimit = 64

var errNotInNodePack = errors.New("the connector is not inside a ComfyUI's custom_nodes\\" + nodeFolder + " folder")

// findInstall reads the ComfyUI around executable.
func findInstall(executable string, folders accountFolders) (comfyInstall, error) {
	node := filepath.Dir(filepath.Clean(executable))
	custom := filepath.Dir(node)
	if !strings.EqualFold(filepath.Base(node), nodeFolder) || !strings.EqualFold(filepath.Base(custom), "custom_nodes") {
		return comfyInstall{}, errNotInNodePack
	}
	base := filepath.Dir(custom)
	found := comfyInstall{Base: base, Node: node, Kind: kindFolder}
	root := filepath.Dir(base)
	portable := filepath.Join(root, "python_embeded", "python.exe")
	switch {
	case strings.EqualFold(filepath.Base(base), "ComfyUI") && regularFile(portable) && regularFile(filepath.Join(base, "main.py")):
		found.Kind, found.Program, found.Dir = kindPortable, portable, root
		found.Args = []string{"-s", filepath.Join(base, "main.py")}
	case folders.Roaming != "" && sameFolderSetting(filepath.Join(folders.Roaming, "ComfyUI", "config.json"), base):
		found.Kind, found.Program = kindDesktop, desktopProgram(folders.Local)
	case comfyDesktopInstall(base, folders):
		found.Kind, found.Program = kindComfyDesktop, comfyDesktopProgram(folders.Local)
	}
	for _, python := range []string{
		filepath.Join(node, "helper", "python.exe"),
		portable,
		filepath.Join(base, ".venv", "Scripts", "python.exe"),
	} {
		if regularFile(python) {
			found.Python = python
			break
		}
	}
	return found, nil
}

// comfyDesktopInstall is whether base is one of Comfy Desktop's installs:
// a folder under %LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs, the ComfyUI
// folder of a local install installations.json lists, or the basePath its
// settings name.
func comfyDesktopInstall(base string, folders accountFolders) bool {
	if folders.Local != "" {
		installs := filepath.Join(folders.Local, "Comfy-Desktop", "ComfyUI-Installs")
		if inside, err := fileid.Within(base, installs); err == nil && inside {
			return true
		}
	}
	if folders.Roaming == "" {
		return false
	}
	if listedComfyDesktopInstall(filepath.Join(folders.Roaming, "Comfy Desktop", "installations.json"), base) {
		return true
	}
	for _, name := range []string{"config.json", "settings.json"} {
		if sameFolderSetting(filepath.Join(folders.Roaming, "Comfy Desktop", name), base) {
			return true
		}
	}
	return false
}

// listedComfyDesktopInstall is whether Comfy Desktop's installations.json
// lists a local install with status "installed" whose ComfyUI folder
// (installPath\ComfyUI, or installPath itself) is base, compared by file
// identity. Comfy Cloud's entry names no folder; a relative or UNC
// installPath is passed over.
func listedComfyDesktopInstall(file, base string) bool {
	records, err := installations.Read(file)
	if err != nil {
		return false
	}
	for i := range records {
		var entry struct {
			InstallPath string `json:"installPath"`
			SourceID    string `json:"sourceId"`
			Status      string `json:"status"`
			RemoteURL   string `json:"remoteUrl"`
		}
		if records.Decode(i, &entry) != nil || entry.Status != "installed" || entry.RemoteURL != "" || entry.SourceID == "cloud" {
			continue
		}
		volume := filepath.VolumeName(entry.InstallPath)
		if !filepath.IsAbs(entry.InstallPath) || len(volume) != 2 || volume[1] != ':' {
			continue
		}
		for _, folder := range []string{filepath.Join(entry.InstallPath, "ComfyUI"), entry.InstallPath} {
			if sameFolder(folder, base) {
				return true
			}
		}
	}
	return false
}

// sameFolderSetting is whether the settings file's top-level basePath (or
// base_path, installPath) is base, compared by file identity.
func sameFolderSetting(file, base string) bool {
	raw, err := readBounded(file, settingsLimit)
	if err != nil {
		return false
	}
	var settings map[string]json.RawMessage
	if json.Unmarshal(raw, &settings) != nil {
		return false
	}
	for _, key := range []string{"basePath", "base_path", "installPath"} {
		var value string
		if json.Unmarshal(settings[key], &value) == nil && value != "" && filepath.IsAbs(value) && sameFolder(value, base) {
			return true
		}
	}
	return false
}

// desktopProgram is ComfyUI Desktop's program under local, "" when absent.
func desktopProgram(local string) string {
	if local == "" {
		return ""
	}
	program := filepath.Join(local, "Programs", "@comfyorgcomfyui-electron", "ComfyUI.exe")
	if regularFile(program) {
		return program
	}
	return ""
}

// comfyDesktopProgram is Comfy Desktop's program, found by name: the first
// program whose name holds "comfy" in a Comfy folder of local's Programs
// folder, or in local\Comfy-Desktop. ComfyUI Desktop's folder is passed over.
func comfyDesktopProgram(local string) string {
	if local == "" {
		return ""
	}
	programs := filepath.Join(local, "Programs")
	if program := filepath.Join(programs, "Comfy Desktop", "Comfy Desktop.exe"); regularFile(program) {
		return program
	}
	folders := []string{filepath.Join(local, "Comfy-Desktop")}
	for _, entry := range entries(programs) {
		name := strings.ToLower(entry.Name())
		if entry.IsDir() && strings.Contains(name, "comfy") && name != "@comfyorgcomfyui-electron" {
			folders = append(folders, filepath.Join(programs, entry.Name()))
		}
	}
	for _, folder := range folders {
		for _, entry := range entries(folder) {
			name := strings.ToLower(entry.Name())
			if strings.HasSuffix(name, ".exe") && strings.Contains(name, "comfy") && !strings.Contains(name, "uninstall") &&
				regularFile(filepath.Join(folder, entry.Name())) {
				return filepath.Join(folder, entry.Name())
			}
		}
	}
	return ""
}

func entries(folder string) []os.DirEntry {
	f, err := os.Open(folder)
	if err != nil {
		return nil
	}
	defer f.Close()
	list, _ := f.ReadDir(entryLimit)
	slices.SortFunc(list, func(a, b os.DirEntry) int { return strings.Compare(a.Name(), b.Name()) })
	return list
}

func regularFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

// sameFolder is whether a and b name the same existing folder, by file
// identity (fileid.SameFile).
func sameFolder(a, b string) bool {
	if info, err := os.Stat(a); err != nil || !info.IsDir() {
		return false
	}
	same, err := fileid.SameFile(a, b)
	return err == nil && same
}

func readBounded(file string, limit int64) ([]byte, error) {
	f, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > limit {
		return nil, errors.New("file too large")
	}
	return raw, nil
}
