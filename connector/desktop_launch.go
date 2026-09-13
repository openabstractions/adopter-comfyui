package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	cas "github.com/openabstractions/abstraction-cas/go"
	"github.com/openabstractions/adopter-comfyui/connector/installations"
)

// OA starts a Comfy Desktop install's server (architect ruling 2026-11-12,
// adopters/comfyui/connector/README.md, "OA starts a Comfy Desktop install"). The command is the one Comfy Desktop
// used for that install. Comfy Desktop 1.1.9's own launch record,
// comfy-procs\<session>.json, names the install, the port and the process
// Desktop spawned (childPid with its creation time) but not the arguments.
// While a ComfyUI Desktop started is served, the connector reads that
// process's command line and keeps it in OA's own folder, beside the install
// facts it was started under (rememberedLaunch). The next start uses it while
// those facts are unchanged. Without it, the command is rebuilt from
// installations.json, settings.json and the install's model-paths yaml the
// way Desktop's source builds it (getLaunchCommand, applyStorageLaunchArgs).
// A missing fact returns errDesktopFallback: the person starts ComfyUI.
// Desktop's own files are only read.

// errDesktopFallback marks a start OA cannot make without guessing.
var errDesktopFallback = errors.New("Comfy Desktop's files do not say how it starts this ComfyUI")

// desktopStateFolder is Comfy Desktop's state folder under %APPDATA%.
func desktopStateFolder(roaming string) string {
	if roaming == "" {
		return ""
	}
	return filepath.Join(roaming, "Comfy Desktop")
}

// desktopInstallation is one local install installations.json lists.
type desktopInstallation struct {
	ID              string            `json:"id"`
	Name            string            `json:"name"`
	InstallPath     string            `json:"installPath"`
	SourceID        string            `json:"sourceId"`
	Status          string            `json:"status"`
	LaunchArgs      *string           `json:"launchArgs"`
	Adopted         bool              `json:"adopted"`
	UseSharedModels *bool             `json:"useSharedModels"`
	UseSharedInput  *bool             `json:"useSharedInput"`
	UseSharedOutput *bool             `json:"useSharedOutput"`
	ModelDirs       []string          `json:"modelDirs"`
	InputDir        string            `json:"inputDir"`
	OutputDir       string            `json:"outputDir"`
	EnvVars         map[string]string `json:"envVars"`
}

// desktopSettings are settings.json's storage folders.
type desktopSettings struct {
	ModelsDirs []string `json:"modelsDirs"`
	InputDir   string   `json:"inputDir"`
	OutputDir  string   `json:"outputDir"`
}

var desktopIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// findDesktopInstallation is the installed local entry whose ComfyUI folder
// (installPath\ComfyUI) is base.
func findDesktopInstallation(state, base string) (desktopInstallation, error) {
	records, err := installations.Read(filepath.Join(state, "installations.json"))
	if err != nil {
		var listError *installations.ListError
		if errors.As(err, &listError) {
			return desktopInstallation{}, fmt.Errorf("%w: installations.json is not a list", errDesktopFallback)
		}
		return desktopInstallation{}, fmt.Errorf("%w: installations.json is unreadable", errDesktopFallback)
	}
	for i := range records {
		var entry desktopInstallation
		if records.Decode(i, &entry) != nil || entry.Status != "installed" || !desktopIDPattern.MatchString(entry.ID) || !localFolder(entry.InstallPath) {
			continue
		}
		if sameFolder(filepath.Join(entry.InstallPath, "ComfyUI"), base) {
			return entry, nil
		}
	}
	return desktopInstallation{}, fmt.Errorf("%w: installations.json lists no installed ComfyUI at %s", errDesktopFallback, base)
}

// localFolder is an absolute path on a lettered drive.
func localFolder(path string) bool {
	volume := filepath.VolumeName(path)
	return filepath.IsAbs(path) && len(volume) == 2 && volume[1] == ':'
}

func readDesktopSettings(state string) (desktopSettings, error) {
	raw, err := readBounded(filepath.Join(state, "settings.json"), settingsLimit)
	if err != nil {
		return desktopSettings{}, fmt.Errorf("%w: settings.json is unreadable", errDesktopFallback)
	}
	var s desktopSettings
	if json.Unmarshal(raw, &s) != nil {
		return desktopSettings{}, fmt.Errorf("%w: settings.json is unreadable", errDesktopFallback)
	}
	return s, nil
}

// desktopLaunch is the start of one install's server.
type desktopLaunch struct {
	InstallationID string
	InstallPath    string
	Program        string
	Args           []string
	Dir            string
	// Env holds the variables Comfy Desktop sets beside the person's own.
	Env  []string
	Port int
	// Source is "desktop" for the command Desktop last used, "rebuilt" for
	// one built from Desktop's files.
	Source string
}

// venvPython is the install's venv launcher, the program Desktop starts.
func venvPython(installPath string) string {
	return filepath.Join(installPath, "ComfyUI", ".venv", "Scripts", "python.exe")
}

// desktopReservedEnv are the variables Desktop sets itself and never takes
// from an install's envVars (RESERVED_ENV_VARS).
var desktopReservedEnv = []string{"PYTHONIOENCODING", "PYTHONFAULTHANDLER", "__COMFY_CLI_SESSION__", "CM_USE_PYGIT2", "__COMFYUI_DESKTOP_VERSION__"}

// desktopEnv is buildLaunchEnv's set: the install's own variables, then
// Desktop's. version is Comfy Desktop's, "" when unknown; the session path
// Desktop uses for its restart marker is left out.
func desktopEnv(inst desktopInstallation, version string) []string {
	keys := make([]string, 0, len(inst.EnvVars))
	for key := range inst.EnvVars {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	var out []string
	for _, key := range keys {
		name := strings.TrimSpace(key)
		if name == "" || strings.ContainsAny(name, "=\x00") || slices.Contains(desktopReservedEnv, strings.ToUpper(name)) {
			continue
		}
		out = append(out, name+"="+inst.EnvVars[key])
	}
	out = append(out, "PYTHONIOENCODING=utf-8", "PYTHONFAULTHANDLER=1")
	if version != "" && (inst.SourceID == "standalone" || inst.SourceID == "comfybuilder") {
		out = append(out, "__COMFYUI_DESKTOP_VERSION__="+version)
	}
	return out
}

// parseDesktopArgs splits launchArgs the way Desktop's parseArgs does:
// whitespace separates, single or double quotes group, no escapes.
func parseDesktopArgs(s string) []string {
	var out []string
	var current strings.Builder
	var quote rune
	for _, ch := range s {
		switch {
		case quote != 0:
			if ch == quote {
				quote = 0
				continue
			}
			current.WriteRune(ch)
		case ch == '"' || ch == '\'':
			quote = ch
		case ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' || ch == '\f' || ch == '\v':
			if current.Len() > 0 {
				out = append(out, current.String())
				current.Reset()
			}
		default:
			current.WriteRune(ch)
		}
	}
	if current.Len() > 0 {
		out = append(out, current.String())
	}
	return out
}

// desktopPort is extractPort: the last --port, 8188 by default.
func desktopPort(args []string) int {
	port := 8188
	for i := 0; i < len(args); i++ {
		value := ""
		if args[i] == "--port" && i+1 < len(args) {
			value = args[i+1]
			i++
		} else if v, ok := strings.CutPrefix(args[i], "--port="); ok {
			value = v
		} else {
			continue
		}
		if n, err := strconv.Atoi(value); err == nil && n > 0 && n < 65536 {
			port = n
		}
	}
	return port
}

// desktopModelDirs is resolveLauncherModelDirs: the shared folders unless
// the install opts out, then its own, without the install's models folder.
func desktopModelDirs(inst desktopInstallation, settings desktopSettings) []string {
	own := filepath.Clean(filepath.Join(inst.InstallPath, "ComfyUI", "models"))
	var dirs []string
	add := func(dir string) {
		if dir == "" || !filepath.IsAbs(dir) {
			return
		}
		dir = filepath.Clean(dir)
		if strings.EqualFold(dir, own) {
			return
		}
		for _, d := range dirs {
			if strings.EqualFold(d, dir) {
				return
			}
		}
		dirs = append(dirs, dir)
	}
	if inst.UseSharedModels == nil || *inst.UseSharedModels {
		for _, d := range settings.ModelsDirs {
			add(d)
		}
	}
	for _, d := range inst.ModelDirs {
		add(d)
	}
	return dirs
}

// rebuildDesktopLaunch builds the command from Desktop's files. Desktop adds
// its own computed flags at launch (feature flags, --enable-assets, campaign
// arguments); a rebuilt start leaves those out.
func rebuildDesktopLaunch(state string, inst desktopInstallation, settings desktopSettings, version string) (desktopLaunch, error) {
	missing := func(what string) (desktopLaunch, error) {
		return desktopLaunch{}, fmt.Errorf("%w: %s", errDesktopFallback, what)
	}
	if inst.Adopted || inst.SourceID != "standalone" {
		return missing("the install is not one of Comfy Desktop's standalone installs")
	}
	if inst.LaunchArgs == nil {
		return missing("installations.json names no startup arguments for the install")
	}
	program := venvPython(inst.InstallPath)
	if !regularFile(program) || !regularFile(filepath.Join(inst.InstallPath, "ComfyUI", "main.py")) {
		return missing("the install has no ComfyUI\\.venv or main.py")
	}
	args := append([]string{"-s", filepath.Join("ComfyUI", "main.py")}, parseDesktopArgs(strings.TrimSpace(*inst.LaunchArgs))...)
	if dirs := desktopModelDirs(inst, settings); len(dirs) > 0 {
		yaml := filepath.Join(state, "instance-model-paths", inst.ID+".yaml")
		if !regularFile(yaml) {
			return missing("the install's model-paths yaml is missing")
		}
		args = append(args, "--extra-model-paths-config", yaml)
	}
	folder := func(shared *bool, sharedDir, ownDir, what string) ([]string, error) {
		if shared == nil || *shared {
			if !filepath.IsAbs(sharedDir) {
				return nil, fmt.Errorf("%w: settings.json names no %s folder", errDesktopFallback, what)
			}
			return []string{"--" + what + "-directory", sharedDir}, nil
		}
		if ownDir != "" && filepath.IsAbs(ownDir) {
			return []string{"--" + what + "-directory", ownDir}, nil
		}
		return nil, nil
	}
	input, err := folder(inst.UseSharedInput, settings.InputDir, inst.InputDir, "input")
	if err != nil {
		return desktopLaunch{}, err
	}
	output, err := folder(inst.UseSharedOutput, settings.OutputDir, inst.OutputDir, "output")
	if err != nil {
		return desktopLaunch{}, err
	}
	args = append(append(args, input...), output...)
	return desktopLaunch{InstallationID: inst.ID, InstallPath: inst.InstallPath, Program: program, Args: args, Dir: inst.InstallPath,
		Env: desktopEnv(inst, version), Port: desktopPort(args), Source: "rebuilt"}, nil
}

// desktopLaunchFacts is a digest of the install facts a remembered command
// was started under: when one changes in Desktop, the remembered command is
// stale until Desktop starts the install again.
func desktopLaunchFacts(inst desktopInstallation, settings desktopSettings) string {
	raw, _ := json.Marshal([]any{inst.ID, inst.InstallPath, inst.SourceID, inst.LaunchArgs, inst.Adopted, inst.UseSharedModels, inst.UseSharedInput,
		inst.UseSharedOutput, inst.ModelDirs, inst.InputDir, inst.OutputDir, settings.ModelsDirs, settings.InputDir, settings.OutputDir})
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// rememberedLaunch is OA's copy of the command Desktop last started an
// install with, in OA's own folder.
type rememberedLaunch struct {
	InstallationID string   `json:"installation_id"`
	InstallPath    string   `json:"install_path"`
	Args           []string `json:"args"`
	Facts          string   `json:"facts"`
	CapturedUnixMs int64    `json:"captured_unix_ms"`
}

const rememberedLimit = 64 << 10

// rememberedFile is the remembered command of one install.
func rememberedFile(local, id string) string {
	return filepath.Join(local, "OpenAbstractions", "ComfyUI", "desktop-launch-"+id+".json")
}

func readRemembered(file string) (rememberedLaunch, bool) {
	raw, err := readBounded(file, rememberedLimit)
	if err != nil {
		return rememberedLaunch{}, false
	}
	var r rememberedLaunch
	if json.Unmarshal(raw, &r) != nil || len(r.Args) < 2 || len(r.Args) > 256 {
		return rememberedLaunch{}, false
	}
	return r, true
}

// writeRemembered keeps args, the command line Desktop's process ran without
// its program, when it starts ComfyUI\main.py.
func writeRemembered(local string, inst desktopInstallation, settings desktopSettings, args []string, now time.Time) error {
	if len(args) < 2 || args[0] != "-s" || !strings.EqualFold(filepath.Clean(args[1]), filepath.Join("ComfyUI", "main.py")) || len(args) > 256 {
		return errors.New("Comfy Desktop's ComfyUI runs another command than ComfyUI\\main.py")
	}
	raw, err := json.Marshal(rememberedLaunch{InstallationID: inst.ID, InstallPath: inst.InstallPath, Args: args,
		Facts: desktopLaunchFacts(inst, settings), CapturedUnixMs: now.UnixMilli()})
	if err != nil {
		return err
	}
	file := rememberedFile(local, inst.ID)
	return cas.ReplaceLimit(file, rememberedLimit, raw)
}

// chooseDesktopLaunch is the start for the install at base: the command
// Desktop last used while its facts are unchanged, else the rebuilt one.
func chooseDesktopLaunch(local, state, base, version string) (desktopLaunch, error) {
	if state == "" {
		return desktopLaunch{}, fmt.Errorf("%w: no Comfy Desktop folder", errDesktopFallback)
	}
	inst, err := findDesktopInstallation(state, base)
	if err != nil {
		return desktopLaunch{}, err
	}
	settings, err := readDesktopSettings(state)
	if err != nil {
		return desktopLaunch{}, err
	}
	rebuilt, rebuildErr := rebuildDesktopLaunch(state, inst, settings, version)
	if r, ok := readRemembered(rememberedFile(local, inst.ID)); ok && r.InstallationID == inst.ID && strings.EqualFold(r.InstallPath, inst.InstallPath) &&
		r.Facts == desktopLaunchFacts(inst, settings) && regularFile(venvPython(inst.InstallPath)) && !inst.Adopted {
		return desktopLaunch{InstallationID: inst.ID, InstallPath: inst.InstallPath, Program: venvPython(inst.InstallPath), Args: r.Args,
			Dir: inst.InstallPath, Env: desktopEnv(inst, version), Port: desktopPort(r.Args), Source: "desktop"}, nil
	}
	return rebuilt, rebuildErr
}

// desktopProcRecord is one comfy-procs record (Desktop's isRecord, v 1).
type desktopProcRecord struct {
	V              int     `json:"v"`
	SessionKey     string  `json:"sessionKey"`
	InstallationID string  `json:"installationId"`
	InstallPath    string  `json:"installPath"`
	ChildPID       int     `json:"childPid"`
	DesktopPID     int     `json:"desktopPid"`
	Port           int     `json:"port"`
	ChildStartTime *string `json:"childStartTime"`
}

// desktopProcRecords lists the valid records in comfy-procs.
func desktopProcRecords(state string) []desktopProcRecord {
	folder := filepath.Join(state, "comfy-procs")
	var out []desktopProcRecord
	for _, entry := range entries(folder) {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		raw, err := readBounded(filepath.Join(folder, entry.Name()), settingsLimit)
		if err != nil {
			continue
		}
		var r desktopProcRecord
		if json.Unmarshal(raw, &r) != nil || r.V != 1 || r.SessionKey == "" || r.InstallationID == "" || r.InstallPath == "" ||
			r.ChildPID <= 1 || r.DesktopPID <= 0 || r.Port <= 0 || r.Port > 65535 {
			continue
		}
		out = append(out, r)
	}
	return out
}

// desktopPortLock is one port-locks\port-<n>.json.
type desktopPortLock struct {
	PID              int    `json:"pid"`
	InstallationName string `json:"installationName"`
}

func desktopPortLocks(state string) []desktopPortLock {
	folder := filepath.Join(state, "port-locks")
	var out []desktopPortLock
	for _, entry := range entries(folder) {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), "port-") || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		raw, err := readBounded(filepath.Join(folder, entry.Name()), settingsLimit)
		if err != nil {
			continue
		}
		var l desktopPortLock
		if json.Unmarshal(raw, &l) == nil && l.PID > 0 {
			out = append(out, l)
		}
	}
	return out
}

// processLiveness reads a process's creation time as a FILETIME count, ok
// false once it has exited or cannot be read.
type processLiveness func(pid int) (created uint64, ok bool)

// processImage reads a process's program path; nil where unknown.
var processImage func(pid int) (string, bool)

// runsFromInstall is whether pid's program lies in the install's folder,
// true where program paths cannot be read.
func runsFromInstall(pid int, installPath string) bool {
	if processImage == nil {
		return true
	}
	image, ok := processImage(pid)
	return ok && insideFolder(image, installPath)
}

// desktopRunsInstall is whether Comfy Desktop runs the install now: a
// comfy-procs record of it whose child is alive (with the recorded creation
// time when Desktop recorded one), or a port lock of the install's name with
// a live PID. except names a process tree of OA's own that is passed over.
func desktopRunsInstall(state string, inst desktopInstallation, live processLiveness, except func(pid int) bool) (desktopProcRecord, bool) {
	for _, r := range desktopProcRecords(state) {
		if r.InstallationID != inst.ID && !strings.EqualFold(filepath.Clean(r.InstallPath), filepath.Clean(inst.InstallPath)) {
			continue
		}
		if except != nil && except(r.ChildPID) {
			continue
		}
		created, ok := live(r.ChildPID)
		if !ok {
			continue
		}
		if r.ChildStartTime != nil && *r.ChildStartTime != strconv.FormatUint(created, 10) ||
			r.ChildStartTime == nil && !runsFromInstall(r.ChildPID, inst.InstallPath) {
			continue
		}
		return r, true
	}
	for _, l := range desktopPortLocks(state) {
		if inst.Name == "" || l.InstallationName != inst.Name || except != nil && except(l.PID) {
			continue
		}
		if _, ok := live(l.PID); ok && runsFromInstall(l.PID, inst.InstallPath) {
			return desktopProcRecord{ChildPID: l.PID, InstallationID: inst.ID, InstallPath: inst.InstallPath}, true
		}
	}
	return desktopProcRecord{}, false
}
