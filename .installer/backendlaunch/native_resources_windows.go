//go:build windows

package main

import _ "embed"

// The normal native build stages backend-pin.json from the authenticated
// backend manifest before compilation. The source placeholder refuses launch.
// The same two byte strings are the named neutral PE RCDATA resources.
//
//go:embed application-manifest.json
var nativeApplicationManifest []byte

//go:embed backend-pin.json
var nativeBackendPin []byte

//go:embed backend-manifest.json
var nativeBackendManifest []byte

//go:embed control-pin.json
var nativeControlPin []byte
