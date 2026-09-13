//go:build windows

package main

import (
	"context"
	"path/filepath"
	"slices"
	"time"

	"github.com/openabstractions/abstraction-facade/go/supervision"
)

// A server OpenAbstractions started for an app is recorded in OA's own
// folder, one file per application:
//
//	%LOCALAPPDATA%\OpenAbstractions\started\<application>.json
//	{"v":1, "application":"comfyui", "title":"ComfyUI",
//	 "started_unix_ms":..., "port":8188,
//	 "processes":[{"pid":N, "created":"<FILETIME>"}, ...]}
//
// It names only the processes the connector started, the launcher first,
// each by its PID and creation time, so a reused PID never matches. The
// runtime's own log names the app whose open started the connector
// (serve/application_owned_start_record.go); the Panel joins the two through
// the launcher's parent, and the Apps page shows the line "ComfyUI is
// running (started by OpenAbstractions for OpenCode)" with Stop while a
// recorded process lives (monitor/app_started.go). Stop writes <application>.stop beside it,
// with the launcher's occurrence and the target processes' PID and creation
// times. The node pack in the named
// process ends ComfyUI once its queue is empty (node/oa_listening.py), and
// after startedStopGrace the Panel stops each recorded process by PID. The
// connector writes the same request when Comfy Desktop takes the install
// over. No agent tool writes it.

// startedStopGrace bounds the wait for the current prompt before a stop by PID.
var startedStopGrace = 5 * time.Minute

type startedProcess = supervision.StartProcess
type startedRecord supervision.StartRecord

func startedFolder(local string) string {
	return filepath.Join(local, "OpenAbstractions", "started")
}

func startedFile(local, application string) string {
	return filepath.Join(startedFolder(local), application+".json")
}

func stopFile(local, application string) string {
	return filepath.Join(startedFolder(local), application+".stop")
}

func writeStarted(local string, r startedRecord) error {
	return publishStarted(local, r, liveProcess)
}

func updateStarted(local string, previous, next startedRecord) error {
	return supervision.UpdateStartRecord(startedFile(local, previous.Application),
		supervision.StartRecord(previous), supervision.StartRecord(next))
}

func readStarted(local, application string) (startedRecord, bool) {
	r, ok := supervision.ReadStartRecord(startedFile(local, application), application)
	return startedRecord(r), ok
}

// names reports whether the record names pid with that creation time.
func (r startedRecord) names(pid int, created string) bool {
	return slices.Contains(r.Processes, startedProcess{PID: pid, Created: created})
}

// removeStarted removes the record and its stop request while the record is
// still the selected launcher and server occurrence.
func removeStarted(local string, selected startedRecord) {
	_ = supervision.PruneStartRecord(context.Background(), startedFile(local, selected.Application),
		stopFile(local, selected.Application), supervision.StartRecord(selected), nil)
}

// requestStop asks the node pack in each named process to end ComfyUI once
// its queue is empty.
func requestStop(ctx context.Context, local string, selected startedRecord, target startedProcess, now time.Time) error {
	_, err := supervision.RequestStartStop(ctx, startedFile(local, selected.Application), stopFile(local, selected.Application),
		supervision.StartRecord(selected), target, now)
	return err
}
