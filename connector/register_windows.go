//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	facade "github.com/openabstractions/abstraction-facade/go"
	applications "github.com/openabstractions/abstraction-facade/go/abstraction/facade/applications"
	"github.com/openabstractions/abstraction-identity/fileid"
)

// registerSelf asks the installed runtime to register this connector as the
// comfyui application (applications@3 Register, create-only). The service
// reads the manifest from this image's OA_APPLICATION_MANIFEST resource,
// pins an unsigned image by its SHA-256 and leaves the entry pending until
// the person keeps it. The command reports the service's answer once, with
// no replay or replacement.
func registerSelf(ctx context.Context, out io.Writer) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	return registerSelfWith(ctx, out, func(ctx context.Context, request applications.RegisterRequest) (applications.Change, error) {
		binding, err := facade.Discover().ResolveService(ctx, "abstraction.facade/applications@3", facade.Requirements{Scope: facade.ScopeLocal})
		if err != nil {
			return applications.Change{}, err
		}
		return applications.NewApplicationsClient(binding.Transport().WithContext(ctx)).Register(request)
	})
}

func registerSelfWith(ctx context.Context, out io.Writer, register func(context.Context, applications.RegisterRequest) (applications.Change, error)) (result error) {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	held, err := fileid.Open(ctx, fileid.Self, executable, fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, held.Close()) }()
	if held.FileID().IsDir() || held.FileID().Links() != 1 {
		return errors.New("ordinary self executable required")
	}
	change, err := register(ctx, applications.RegisterRequest{Program: held.FileID().Path()})
	if err != nil {
		return err
	}
	// One line for Setup and the node pack: the outcome word and, when the
	// service gave them, its revision and reason.
	answer := map[string]string{"outcome": change.Outcome.String()}
	for key, value := range map[string]string{"revision": change.Revision, "reason": change.Reason} {
		if value != "" {
			answer[key] = value
		}
	}
	if err := json.NewEncoder(out).Encode(answer); err != nil {
		return err
	}
	if change.Outcome != applications.OutcomeApplied {
		return fmt.Errorf("self-registration %s: %s", change.Outcome.String(), change.Reason)
	}
	return nil
}
