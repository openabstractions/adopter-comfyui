//go:build windows

// The ComfyUI connector: the program OpenAbstractions knows the person's own
// ComfyUI by. It ships in the integration's node pack (the extension MSI
// places it in custom_nodes\comfyui-openabstractions) and serves the
// ComfyUI around it. It builds, seals and starts no ComfyUI of its own.
package main

import (
	"context"
	_ "embed"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

// applicationManifest is the comfyui application manifest. The same bytes
// are this image's OA_APPLICATION_MANIFEST resource (connector.rc,
// rsrc_windows_amd64.syso), which the registration service reads.
//
//go:embed application-manifest.json
var applicationManifest []byte

const usage = `comfyui-connector: OpenAbstractions for the person's own ComfyUI (Windows).

usage: comfyui-connector OPERATION

  register-self   register this connector as the comfyui application; the
                  person keeps it in OpenAbstractions. Setup's Connect and
                  ComfyUI's "Connect to OpenAbstractions" run it.
  owned-host      serve the comfyui application; the OpenAbstractions runtime
                  starts it. open attaches to a running ComfyUI or starts this
                  one, then the agent's workflow.list, workflow.inputs,
                  workflow.run, workflow.download_models and job.status use
                  ComfyUI's own HTTP API.
  native-relay    make this ComfyUI's OpenAbstractions calls; the node pack
                  starts it.

The connector serves the ComfyUI whose custom_nodes folder holds it. A
running ComfyUI reports its process and port in
%LOCALAPPDATA%\OpenAbstractions\ComfyUI\listening.json; the connector serves
it when that process holds the listener and its folder is the connector's.
While it serves that ComfyUI, the node pack in it connects the graph channel
the connector names in channel-<pid>.json beside the report; the graph
context, graph.read, graph.choices, graph.edit, graph.run and reveal use it.
`

func main() {
	if len(os.Args) != 2 {
		fmt.Print(usage)
		if len(os.Args) == 1 {
			return
		}
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var err error
	switch os.Args[1] {
	case "--help", "-h", "help":
		fmt.Print(usage)
		return
	case "register-self":
		err = registerSelf(ctx, os.Stdout)
	case "owned-host":
		err = runHost(ctx)
	case "native-relay":
		err = runNativeRelay(ctx, os.Stdin, os.Stdout)
	default:
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "comfyui-connector:", err)
		os.Exit(1)
	}
}
