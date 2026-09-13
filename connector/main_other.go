//go:build !windows

package main

import (
	"fmt"
	"os"
)

// The connector serves ComfyUI on Windows in this release.
func main() {
	fmt.Fprintln(os.Stderr, "comfyui-connector: the ComfyUI connector runs on Windows in this release")
	os.Exit(2)
}
