//go:build !windows

package main

import (
	"path/filepath"
	"strings"
)

// insideFolder checks for a strict descendant with exact path-component case.
func insideFolder(path, folder string) bool {
	if folder == "" {
		return false
	}
	prefix := filepath.Clean(folder) + string(filepath.Separator)
	return strings.HasPrefix(filepath.Clean(path), prefix)
}
