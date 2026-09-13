//go:build !windows

package main

import (
	"os"
	"testing"

	"github.com/openabstractions/adopter-comfyui/connector/internal/personfolders"
)

// TestMain runs this package's tests with the person's per-user folders
// inside a temporary folder (personfolders.Main).
func TestMain(m *testing.M) { os.Exit(personfolders.Main(m)) }
