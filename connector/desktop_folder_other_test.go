//go:build !windows

package main

import (
	"path/filepath"
	"testing"
)

func wantListedDesktop(string) (kind, program string) { return kindFolder, "" }

func TestInsideFolderUsesNativePathCaseAndBoundaries(t *testing.T) {
	parent := t.TempDir()
	install := filepath.Join(parent, "ComfyUI")
	for _, test := range []struct {
		name   string
		path   string
		folder string
		want   bool
	}{
		{"child", filepath.Join(install, "child"), install, true},
		{"empty folder", filepath.Join(install, "child"), "", false},
		{"same folder", install, install, false},
		{"sibling", filepath.Join(parent, "ComfyUI-copy", "child"), install, false},
		{"parent component", install + "/../child", install, false},
		{"different case", filepath.Join(parent, "comfyui", "child"), install, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := insideFolder(test.path, test.folder); got != test.want {
				t.Fatalf("insideFolder(%q, %q) = %v, want %v", test.path, test.folder, got, test.want)
			}
		})
	}
}
