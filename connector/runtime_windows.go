//go:build windows

package main

import (
	"errors"
	"os"
	"path/filepath"

	"golang.org/x/sys/windows/registry"
)

// The installed OpenAbstractions runtime records its native IPC library's
// full path as Software\OpenAbstractions\Abstraction ipc_library, machine-wide
// or for this account (installer/abstraction.wxs). Its folder, the runtime's
// tools folder, also holds the application-model library the adapter loads.
const (
	runtimeKey          = `Software\OpenAbstractions\Abstraction`
	ipcLibraryName      = "abstraction_ipc.dll"
	appmodelLibraryName = "abstraction_facade_appmodel.dll"
)

var errNoRuntime = errors.New("OpenAbstractions is not installed on this computer; install it from https://github.com/openabstractions/redist/releases/latest")

// runtimeTools is the installed runtime's tools folder: the folder of its
// recorded ipc_library, machine-wide first.
func runtimeTools() (string, error) {
	for _, view := range []struct {
		root   registry.Key
		access uint32
	}{{registry.LOCAL_MACHINE, registry.WOW64_64KEY}, {registry.CURRENT_USER, 0}} {
		key, err := registry.OpenKey(view.root, runtimeKey, registry.QUERY_VALUE|view.access)
		if err != nil {
			continue
		}
		value, _, err := key.GetStringValue("ipc_library")
		key.Close()
		if err == nil && filepath.IsAbs(value) && regularFile(value) {
			return filepath.Dir(value), nil
		}
	}
	return "", errNoRuntime
}

// runtimeLibrary is one library of the installed runtime's tools folder.
func runtimeLibrary(name string) (string, error) {
	tools, err := runtimeTools()
	if err != nil {
		return "", err
	}
	library := filepath.Join(tools, name)
	info, err := os.Lstat(library)
	if err != nil || !info.Mode().IsRegular() {
		return "", errors.New("the installed OpenAbstractions has no " + name + "; install a newer release")
	}
	return library, nil
}
