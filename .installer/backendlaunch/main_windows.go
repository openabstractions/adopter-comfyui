//go:build windows

// Private ComfyUI Desktop carrier. It reuses the existing environment,
// immutable-engine and owned-process-tree owners; it is no general executor.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/openabstractions/abstraction-cas/go"
	"github.com/openabstractions/abstraction-facade/go/programfiles"
	"github.com/openabstractions/abstraction-facade/go/supervision"
	identity "github.com/openabstractions/abstraction-identity"
	"github.com/openabstractions/abstraction-identity/fileid"
	"github.com/openabstractions/abstraction-identity/go/privatedir"
)

const metadataLimit = 16 << 20
const expandedLimit int64 = 16 << 30
const archiveLimit int64 = 8 << 30
const pythonMember = "standalone-env/python.exe"
const launcherMember = "ComfyUI/oa_comfyui_launch.py"
const interpreterMember = "ComfyUI/oa_interpreter.py"
const mainMember = "ComfyUI/main.py"
const rocmSDKMember = "standalone-env/Lib/site-packages/rocm_sdk/_dist_info.py"
const rocmDetectorMember = "standalone-env/Lib/site-packages/_rocm_sdk_core/lib/llvm/bin/offload-arch.exe"
const helperConfigMember = "ComfyUI/custom_nodes/abstraction_downloads/helper/pyvenv.cfg"
const helperProgramMember = "ComfyUI/custom_nodes/abstraction_downloads/helper/python.exe"
const helperConfigTemplate = "home = {backend}/standalone-env\ninclude-system-site-packages = false\n"

// ComfyUI's site-packages names the shared PyTorch engine component's
// site-packages in this .pth file. The backend ships the comment template;
// Setup supplies the installed component's folder when it seals the tree.
const pytorchConfigMember = "standalone-env/Lib/site-packages/oa-pytorch.pth"
const pytorchConfigTemplate = "# OpenAbstractions Setup names the shared PyTorch engine component here.\n"
const backendPython = "3.12.10"
const backendPBS = "20250409"
const configLimit = 64 << 10

const usage = `Private OA ComfyUI backend carrier (Windows).
  prepare-interpreter --home ABS
  install-local-preview
  installed-local-preview
  owned-host
  register-self
  native-relay
  seal-native-control [--root ABS]
  seal-native-install
  check-native-install
  native-install-plan [--installed-root ABS]
  archive-environment --temporary ABS --backend-parent ABS
  seal-backend --root ABS --manifest-sha256 HEX --manifest-bytes N --record ABS
    [--pytorch-site-packages ABS]
  check-backend --root ABS --manifest-sha256 HEX --manifest-bytes N --record ABS
  unlock-backend --root ABS --manifest-sha256 HEX --manifest-bytes N --record ABS
    --record-sha256 HEX --restoration-record ABS
  run-backend --root ABS --manifest-sha256 HEX --manifest-bytes N --record ABS
    --state ABSDIR --user ABSDIR --input ABSDIR --output ABSDIR
    --temporary ABSDIR --models ABSDIR --cache ABSDIR --session ABSDIR --port N
    [--extra-model-paths-config ABSFILE --extra-model-paths-sha256 HEX]
    [--development-runtime ABSFILE --development-endpoint ENDPOINT]
The manifest pin must come independently from the authenticated signed Desktop
resource. Python, launcher, RECORD identity and argv are derived, never supplied.
Run stdin is an owned cancellation pipe: any byte or EOF stops the owned tree.
No arguments after --, environment overrides, install tools or detached mode.
Unlock opens only a verified private update window. No target may start before
the update owner runs seal-backend with a new record, including after a refusal.
`

type options struct {
	operation     string
	values        map[string]string
	manifestBytes int64
	port          int
	// optionalModelPaths starts the run without its extra model-paths
	// pair when that file cannot be held: the native host's copy of
	// Setup's file (withSetupModelPaths). An operator's explicit pair stays
	// strict.
	optionalModelPaths bool
}

func parseOptions(args []string) (options, error) {
	o := options{values: map[string]string{}}
	if len(args) == 0 || len(args) == 1 && (args[0] == "--help" || args[0] == "-h") {
		return o, nil
	}
	o.operation = args[0]
	if len(args) == 2 && (args[1] == "--help" || args[1] == "-h") {
		o.operation = ""
		return o, nil
	}
	allowed := []string{"--temporary", "--backend-parent"}
	optional := map[string]bool{}
	switch o.operation {
	case "archive-environment":
	case "seal-backend", "check-backend":
		allowed = []string{"--root", "--manifest-sha256", "--manifest-bytes", "--record"}
		optional["--pytorch-site-packages"] = o.operation == "seal-backend"
	case "unlock-backend":
		allowed = []string{"--root", "--manifest-sha256", "--manifest-bytes", "--record", "--record-sha256", "--restoration-record"}
	case "run-backend":
		allowed = []string{"--root", "--manifest-sha256", "--manifest-bytes", "--record", "--state", "--user", "--input", "--output", "--temporary", "--models", "--cache", "--session", "--port"}
		for _, key := range []string{"--extra-model-paths-config", "--extra-model-paths-sha256", "--development-runtime", "--development-endpoint"} {
			optional[key] = true
		}
	default:
		return o, errors.New("unknown private operation")
	}
	if len(args)%2 != 1 || len(args) < 1+len(allowed)*2 {
		return o, errors.New("exact named arguments required")
	}
	for i := 1; i < len(args); i += 2 {
		key, value := args[i], args[i+1]
		known := optional[key]
		for _, name := range allowed {
			if name == key {
				known = true
				break
			}
		}
		if !known || value == "" || len(value) > 32767 || strings.IndexByte(value, 0) >= 0 {
			return o, errors.New("unknown or malformed argument")
		}
		if _, exists := o.values[key]; exists {
			return o, errors.New("duplicate argument")
		}
		o.values[key] = value
	}
	for _, key := range allowed {
		if o.values[key] == "" {
			return o, errors.New("missing argument")
		}
	}
	if o.operation != "archive-environment" {
		if !digest(o.values["--manifest-sha256"]) {
			return o, errors.New("invalid independent manifest pin")
		}
		n, e := strconv.ParseInt(o.values["--manifest-bytes"], 10, 64)
		if e != nil || n <= 0 || n > metadataLimit {
			return o, errors.New("manifest bound refused")
		}
		o.manifestBytes = n
	}
	if site := o.values["--pytorch-site-packages"]; site != "" {
		if _, e := pytorchConfiguration(site); e != nil {
			return o, e
		}
	}
	if o.operation == "unlock-backend" && !digest(o.values["--record-sha256"]) {
		return o, errors.New("invalid independent record pin")
	}
	if o.operation == "run-backend" {
		for _, pair := range [][2]string{{"--extra-model-paths-config", "--extra-model-paths-sha256"}, {"--development-runtime", "--development-endpoint"}} {
			if (o.values[pair[0]] == "") != (o.values[pair[1]] == "") {
				return o, errors.New("optional DATA arguments require their complete pair")
			}
		}
		if pin := o.values["--extra-model-paths-sha256"]; pin != "" && !digest(pin) {
			return o, errors.New("model configuration pin refused")
		}
		n, e := strconv.Atoi(o.values["--port"])
		if e != nil || n < 1 || n > 65535 {
			return o, errors.New("port refused")
		}
		o.port = n
	}
	return o, nil
}

type pin struct {
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}
type namedPin struct {
	Name   string `json:"name"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}
type launcherPin struct {
	Name        string   `json:"name"`
	Bytes       int64    `json:"bytes"`
	SHA256      string   `json:"sha256"`
	Interpreter namedPin `json:"interpreter"`
}
type helperConfig struct {
	Name     string `json:"name"`
	Template pin    `json:"template"`
}

// pytorchConfig is the optional shared PyTorch component profile: the fixed
// .pth member and template, the component release the backend was built
// against and the GPU family its ROCm libraries serve.
type pytorchConfig struct {
	Name         string `json:"name"`
	Release      string `json:"release"`
	TargetFamily string `json:"target_family"`
	Template     pin    `json:"template"`
}
type pythonSeal struct {
	Removed []string `json:"removed_installers"`
	Sealed  int64    `json:"sealed_bytecode_rows"`
	Entry   *string  `json:"entrypoint_change"`
	Records string   `json:"records_sha256"`
	Check   struct {
		Files  int64 `json:"files"`
		Hashed int64 `json:"hashed"`
	} `json:"launch_check"`
	Startup string `json:"required_startup"`
}
type backendManifest struct {
	ID           string            `json:"id"`
	Version      string            `json:"version"`
	Build        int64             `json:"build"`
	Archive      string            `json:"archive_base"`
	Files        []json.RawMessage `json:"files"`
	Ref          string            `json:"comfyui_ref"`
	Commit       string            `json:"comfyui_commit"`
	Python       string            `json:"python_version"`
	Torch        string            `json:"torch_version"`
	Vision       string            `json:"torchvision_version"`
	Audio        string            `json:"torchaudio_version"`
	PBS          string            `json:"pbs_release"`
	UV           string            `json:"uv_version"`
	Requirements string            `json:"vendor_requirements"`
	VendorText   string            `json:"vendor_requirements_content"`
	ComfyText    string            `json:"comfyui_requirements_content"`
	ManagerText  string            `json:"manager_requirements_content"`
	Date         string            `json:"build_date"`
	Manager      bool              `json:"manager"`
	Descriptor   string            `json:"input_descriptor_sha256"`
	Seal         pythonSeal        `json:"private_python_seal"`
	Removed      []string          `json:"removed_generated_tools"`
	Launcher     launcherPin       `json:"oa_launcher"`
	Integration  namedPin          `json:"oa_integration"`
	Runtime      map[string]pin    `json:"runtime_files"`
	HelperConfig *helperConfig     `json:"helper_python_config,omitempty"`
	PyTorch      *pytorchConfig    `json:"pytorch_component_config,omitempty"`
}

func digest(value string) bool {
	return lowerHex(value, 64)
}

func lowerHex(value string, size int) bool {
	if len(value) != size {
		return false
	}
	for _, c := range value {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

// Decode once for duplicate keys, lexical overflow and bounded nesting before
// typed decoding. No value from this metadata can select an executable or flag.
func jsonValue(d *json.Decoder, depth int, path string) error {
	if depth > 16 {
		return errors.New("manifest nesting refused")
	}
	t, e := d.Token()
	if e != nil {
		return e
	}
	if t == nil && path != "private_python_seal.entrypoint_change" {
		return errors.New("null manifest member refused")
	}
	if number, ok := t.(json.Number); ok {
		if _, e := number.Int64(); e != nil {
			return errors.New("noninteger or overflowing manifest number")
		}
		return nil
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		seen := map[string]bool{}
		for d.More() {
			key, e := d.Token()
			if e != nil {
				return e
			}
			name, ok := key.(string)
			if !ok || seen[name] {
				return errors.New("duplicate manifest key")
			}
			seen[name] = true
			childPath := name
			if path != "" {
				childPath = path + "." + name
			}
			if e := jsonValue(d, depth+1, childPath); e != nil {
				return e
			}
		}
		t, e = d.Token()
		if e != nil || t != json.Delim('}') {
			return errors.New("malformed object")
		}
	case '[':
		for d.More() {
			if e := jsonValue(d, depth+1, path+"[]"); e != nil {
				return e
			}
		}
		t, e = d.Token()
		if e != nil || t != json.Delim(']') {
			return errors.New("malformed array")
		}
	default:
		return errors.New("unexpected manifest delimiter")
	}
	return nil
}

func exactKeys(raw json.RawMessage, names ...string) (map[string]json.RawMessage, error) {
	var object map[string]json.RawMessage
	if len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &object) != nil || len(object) != len(names) {
		return nil, errors.New("closed manifest object differs")
	}
	for _, name := range names {
		if _, ok := object[name]; !ok {
			return nil, errors.New("missing manifest member " + name)
		}
	}
	return object, nil
}

func parseManifest(raw []byte) (backendManifest, programfiles.Manifest, error) {
	var value backendManifest
	var tree programfiles.Manifest
	if len(raw) == 0 || len(raw) > metadataLimit || !json.Valid(raw) {
		return value, tree, errors.New("bounded manifest JSON required")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	if e := jsonValue(d, 0, ""); e != nil {
		return value, tree, e
	}
	if _, e := d.Token(); e != io.EOF {
		return value, tree, errors.New("trailing manifest value")
	}
	names := []string{"id", "version", "build", "archive_base", "files", "comfyui_ref", "comfyui_commit", "python_version", "torch_version", "torchvision_version", "torchaudio_version", "pbs_release", "uv_version", "vendor_requirements", "vendor_requirements_content", "comfyui_requirements_content", "manager_requirements_content", "build_date", "manager", "input_descriptor_sha256", "private_python_seal", "removed_generated_tools", "oa_launcher", "oa_integration", "runtime_files"}
	// One optional adopter profile. Ordinary manifests keep their exact roster.
	var shape map[string]json.RawMessage
	if e := json.Unmarshal(raw, &shape); e != nil {
		return value, tree, e
	}
	if profile, ok := shape["helper_python_config"]; ok {
		names = append(names, "helper_python_config")
		fields, e := exactKeys(profile, "name", "template")
		if e != nil {
			return value, tree, e
		}
		if _, e = exactKeys(fields["template"], "bytes", "sha256"); e != nil {
			return value, tree, e
		}
	}
	if profile, ok := shape["pytorch_component_config"]; ok {
		names = append(names, "pytorch_component_config")
		fields, e := exactKeys(profile, "name", "release", "target_family", "template")
		if e != nil {
			return value, tree, e
		}
		if _, e = exactKeys(fields["template"], "bytes", "sha256"); e != nil {
			return value, tree, e
		}
	}
	top, e := exactKeys(bytes.TrimSpace(raw), names...)
	if e != nil {
		return value, tree, e
	}
	seal, e := exactKeys(top["private_python_seal"], "removed_installers", "sealed_bytecode_rows", "entrypoint_change", "records_sha256", "launch_check", "required_startup")
	if e != nil {
		return value, tree, e
	}
	if !bytes.Equal(bytes.TrimSpace(seal["entrypoint_change"]), []byte("null")) {
		return value, tree, errors.New("selected standalone entrypoint marker differs")
	}
	if _, e = exactKeys(seal["launch_check"], "files", "hashed"); e != nil {
		return value, tree, e
	}
	launch, e := exactKeys(top["oa_launcher"], "name", "bytes", "sha256", "interpreter")
	if e != nil {
		return value, tree, e
	}
	if _, e = exactKeys(launch["interpreter"], "name", "bytes", "sha256"); e != nil {
		return value, tree, e
	}
	if _, e = exactKeys(top["oa_integration"], "name", "bytes", "sha256"); e != nil {
		return value, tree, e
	}
	var entries map[string]json.RawMessage
	if e = json.Unmarshal(top["runtime_files"], &entries); e != nil || len(entries) == 0 || len(entries) > 65536 {
		return value, tree, errors.New("runtime roster refused")
	}
	for _, entry := range entries {
		if _, e = exactKeys(entry, "bytes", "sha256"); e != nil {
			return value, tree, e
		}
	}
	d = json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if e = d.Decode(&value); e != nil {
		return value, tree, e
	}
	if value.ID != "win-amd" || value.Version != "oa-env1" || value.Build != 1 || value.Python != backendPython || value.PBS != backendPBS || value.Manager || len(value.Files) != 0 || value.Ref != value.Commit || !lowerHex(value.Commit, 40) || !digest(value.Descriptor) || !digest(value.Seal.Records) || value.Seal.Startup != "-S; OA launch check before site-packages activation" {
		return value, tree, errors.New("selected backend identity differs")
	}
	if value.Seal.Sealed < 0 || value.Seal.Check.Files < 0 || value.Seal.Check.Hashed < 0 {
		return value, tree, errors.New("negative seal counters")
	}
	// The producer records the original integration ZIP as provenance after
	// installing its contents. It is not an additional runtime tree member.
	if e = fileid.SafeName(value.Integration.Name); e != nil {
		return value, tree, e
	}
	if value.Integration.Bytes <= 0 || value.Integration.Bytes > archiveLimit || !digest(value.Integration.SHA256) {
		return value, tree, errors.New("integration archive pin refused")
	}
	total := int64(len(raw))
	hasLicense := false
	for name, p := range value.Runtime {
		for _, component := range strings.Split(name, "/") {
			if e := fileid.SafeName(component); e != nil {
				return value, tree, e
			}
		}
		if name == "manifest.json" || p.Bytes < 0 || p.Bytes > expandedLimit-total {
			return value, tree, errors.New("runtime closure size refused")
		}
		total += p.Bytes
		tree.Files = append(tree.Files, programfiles.File{Path: name, Size: p.Bytes, SHA256: p.SHA256})
		if strings.HasPrefix(name, "OA-LICENSES/") {
			hasLicense = true
		}
	}
	for _, required := range []string{pythonMember, "standalone-env/uv.exe", launcherMember, interpreterMember, mainMember, "requirements-amd-win.txt"} {
		if _, ok := value.Runtime[required]; !ok {
			return value, tree, errors.New("required runtime member absent")
		}
	}
	if !hasLicense || value.Launcher.Name != launcherMember || value.Launcher.Interpreter.Name != interpreterMember || value.Runtime[launcherMember] != (pin{value.Launcher.Bytes, value.Launcher.SHA256}) || value.Runtime[interpreterMember] != (pin{value.Launcher.Interpreter.Bytes, value.Launcher.Interpreter.SHA256}) {
		return value, tree, errors.New("launcher or licence identity differs")
	}
	if e = tree.Validate(); e != nil {
		return value, tree, e
	}
	if value.HelperConfig != nil {
		profile := value.HelperConfig
		sum := sha256.Sum256([]byte(helperConfigTemplate))
		expected := pin{int64(len(helperConfigTemplate)), hex.EncodeToString(sum[:])}
		staged, ok := value.Runtime[helperConfigMember]
		_, program := value.Runtime[helperProgramMember]
		if profile.Name != helperConfigMember || profile.Template != expected || !ok || staged != expected || !program || value.Runtime[helperProgramMember] != value.Runtime[pythonMember] {
			return value, tree, errors.New("fixed helper config profile differs")
		}
	}
	if profile := value.PyTorch; profile != nil {
		sum := sha256.Sum256([]byte(pytorchConfigTemplate))
		expected := pin{int64(len(pytorchConfigTemplate)), hex.EncodeToString(sum[:])}
		staged, ok := value.Runtime[pytorchConfigMember]
		if profile.Name != pytorchConfigMember || profile.Template != expected || !ok || staged != expected ||
			profile.Release == "" || len(profile.Release) > 256 || !utf8.ValidString(profile.Release) || !gpuFamily.MatchString(profile.TargetFamily) {
			return value, tree, errors.New("fixed PyTorch component profile differs")
		}
	}
	sum := sha256.Sum256(raw)
	tree.Files = append(tree.Files, programfiles.File{Path: "manifest.json", Size: int64(len(raw)), SHA256: hex.EncodeToString(sum[:])})
	return value, tree, tree.Validate()
}

func ordinary(ctx context.Context, name string, directory bool) (string, error) {
	if !filepath.IsAbs(name) {
		return "", errors.New("absolute local path required")
	}
	h, e := fileid.Open(ctx, fileid.Self, name, fileid.Options{Links: fileid.RefuseLinks})
	if e != nil {
		return "", e
	}
	defer h.Close()
	if h.FileID().IsDir() != directory || !directory && h.FileID().Links() != 1 {
		return "", errors.New("ordinary unaliased path required")
	}
	return h.FileID().Path(), nil
}

func outside(ctx context.Context, name, root string, directory bool) (string, error) {
	p, e := ordinary(ctx, name, directory)
	if e != nil {
		return "", e
	}
	_, inside, e := identity.InsideRel(p, root)
	if e != nil {
		return "", e
	}
	if inside || identity.SamePathName(p, root) || identity.PathNameWithin(p, root) {
		return "", errors.New("mutable path overlaps immutable code")
	}
	return p, nil
}

func outputFile(ctx context.Context, name, root string) (string, error) {
	if !filepath.IsAbs(name) {
		return "", errors.New("absolute metadata file required")
	}
	parent, e := outside(ctx, filepath.Dir(name), root, true)
	if e != nil {
		return "", e
	}
	if e = privatedir.CheckDir(parent); e != nil {
		return "", e
	}
	if e := fileid.SafeName(filepath.Base(name)); e != nil {
		return "", errors.New("unsafe metadata name")
	}
	if _, e := os.Lstat(name); e == nil {
		return outside(ctx, name, root, false)
	} else if !errors.Is(e, os.ErrNotExist) {
		return "", e
	}
	return filepath.Join(parent, filepath.Base(name)), nil
}

// Resolve both existing objects and not-yet-created metadata names through the
// same ordinary filesystem owner before comparing reserved targets.
func reservedTarget(ctx context.Context, name, root string) (string, error) {
	info, err := os.Lstat(name)
	if err == nil {
		return outside(ctx, name, root, info.IsDir())
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	return outputFile(ctx, name, root)
}

// The Python launcher owns a record FILE inside this private state DIRECTORY.
// Repeated launches keep the same ordinary directory; a file or reparse point
// at that name refuses before launching or changing it.
func stateDirectory(ctx context.Context, name, root string) (string, error) {
	if !filepath.IsAbs(name) {
		return "", errors.New("absolute state directory required")
	}
	parent, err := outside(ctx, filepath.Dir(name), root, true)
	if err != nil {
		return "", err
	}
	if err = privatedir.CheckDir(parent); err != nil {
		return "", err
	}
	directory, err := privateLeaf(ctx, filepath.Join(parent, filepath.Base(name)))
	if err != nil {
		return "", err
	}
	return outside(ctx, directory, root, true)
}

func loadTree(ctx context.Context, o options) (backendManifest, programfiles.Tree, error) {
	var value backendManifest
	var tree programfiles.Tree
	root, e := ordinary(ctx, o.values["--root"], true)
	if e != nil {
		return value, tree, e
	}
	h, e := fileid.Open(ctx, fileid.Self, filepath.Join(root, "manifest.json"), fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if e != nil {
		return value, tree, e
	}
	defer h.Close()
	if h.FileID().IsDir() || h.FileID().Links() != 1 {
		return value, tree, errors.New("ordinary manifest required")
	}
	raw, e := io.ReadAll(io.LimitReader(h.File(), o.manifestBytes+1))
	if e != nil {
		return value, tree, e
	}
	sum := sha256.Sum256(raw)
	if int64(len(raw)) != o.manifestBytes || hex.EncodeToString(sum[:]) != o.values["--manifest-sha256"] {
		return value, tree, errors.New("independent manifest pin differs")
	}
	value, manifest, e := parseManifest(raw)
	if e != nil {
		return value, tree, e
	}
	record, e := outputFile(ctx, o.values["--record"], root)
	if e != nil {
		return value, tree, e
	}
	tree = programfiles.Tree{Name: "OA ComfyUI backend", Root: root, Record: record, Manifest: manifest}
	return value, tree, nil
}

func configuration(root string) ([]byte, error) {
	if !filepath.IsAbs(root) || !utf8.ValidString(root) || strings.ContainsAny(root, "\x00\r\n\v\f\x1c\x1d\x1e\u0085\u2028\u2029") {
		return nil, errors.New("helper home must be one native path line")
	}
	data := []byte("home = " + filepath.Join(root, "standalone-env") + "\ninclude-system-site-packages = false\n")
	if len(data) > configLimit {
		return nil, errors.New("derived helper config exceeds 64KiB")
	}
	return data, nil
}

func helperOwnCopy(ctx context.Context, m backendManifest, t programfiles.Tree) error {
	if m.HelperConfig == nil {
		return nil
	}
	base, e := fileid.Open(ctx, fileid.Self, filepath.Join(t.Root, filepath.FromSlash(pythonMember)), fileid.Options{Links: fileid.RefuseLinks})
	if e != nil {
		return e
	}
	defer base.Close()
	if base.FileID().IsDir() || base.FileID().Links() != 1 {
		return errors.New("ordinary base interpreter required")
	}
	prefix := strings.TrimSuffix(helperConfigMember, "pyvenv.cfg")
	for name := range m.Runtime {
		if !strings.HasPrefix(name, prefix) {
			continue
		}
		h, e := fileid.Open(ctx, fileid.Self, filepath.Join(t.Root, filepath.FromSlash(name)), fileid.Options{Links: fileid.RefuseLinks})
		if e != nil {
			return e
		}
		own := !h.FileID().IsDir() && h.FileID().Links() == 1 && !h.FileID().Equal(base.FileID())
		closeErr := h.Close()
		if !own {
			return errors.New("helper requires distinct ordinary owned copies")
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return nil
}

// The raw signed manifest always describes the original template. Only this
// computed manifest changes one member to its independently derived final pin.
func configuredTree(m backendManifest, original programfiles.Tree) (programfiles.Tree, []byte, error) {
	if m.HelperConfig == nil {
		return original, nil, nil
	}
	data, e := configuration(original.Root)
	if e != nil {
		return original, nil, e
	}
	final := original
	final.Manifest.Files = append([]programfiles.File(nil), original.Manifest.Files...)
	sum := sha256.Sum256(data)
	total := int64(0)
	found := false
	for i, f := range final.Manifest.Files {
		if f.Path == helperConfigMember {
			f.Size = int64(len(data))
			f.SHA256 = hex.EncodeToString(sum[:])
			final.Manifest.Files[i] = f
			found = true
		}
		if f.Size < 0 || f.Size > expandedLimit-total {
			return original, nil, errors.New("derived tree exceeds selected budget")
		}
		total += f.Size
	}
	if !found {
		return original, nil, errors.New("fixed helper config absent")
	}
	return final, data, final.Manifest.Validate()
}

func placeConfiguration(ctx context.Context, original, final programfiles.Tree, data []byte) error {
	if data == nil {
		return nil
	}
	target := filepath.Join(original.Root, filepath.FromSlash(helperConfigMember))
	h, e := fileid.Open(ctx, fileid.Self, target, fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if e != nil {
		return e
	}
	if h.FileID().IsDir() || h.FileID().Links() != 1 {
		h.Close()
		return errors.New("ordinary helper config required")
	}
	actual, e := io.ReadAll(io.LimitReader(h.File(), configLimit+1))
	closeErr := h.Close()
	if e != nil || closeErr != nil {
		return errors.Join(e, closeErr)
	}
	if bytes.Equal(actual, data) {
		return programfiles.Verify(ctx, final.Name, final.Root, final.Manifest)
	}
	if !bytes.Equal(actual, []byte(helperConfigTemplate)) {
		return errors.New("helper config is neither original nor current-root derived")
	}
	if e = programfiles.Verify(ctx, original.Name, original.Root, original.Manifest); e != nil {
		return e
	}
	// The fixed external side keeps CAS locks/stages out of the sealed tree.
	// Validate both trees before privateLeaf may create this one owned leaf.
	side := original.Record + ".configuration"
	for _, root := range []string{original.Root, final.Root} {
		if _, e = outside(ctx, filepath.Dir(side), root, true); e != nil {
			return e
		}
	}
	side, e = privateLeaf(ctx, side)
	if e != nil {
		return e
	}
	for _, root := range []string{original.Root, final.Root} {
		if _, e = outside(ctx, side, root, true); e != nil {
			return e
		}
	}
	// Both directories exist. Placement checks the actual volume before any
	// replacement and compares bytes under its cooperative writer lock.
	placement, e := cas.NewPlacement(original.Root, side)
	if e != nil {
		return e
	}
	if e = ctx.Err(); e != nil {
		return e
	}
	return placement.Write(target, []byte(helperConfigTemplate), data)
}

var gpuFamily = regexp.MustCompile(`^gfx[0-9a-f]{3,5}$`)

// pytorchConfiguration is oa-pytorch.pth naming one component site-packages:
// the template comment and one absolute local path line. Python appends that
// folder to sys.path; the line can never be an import line.
func pytorchConfiguration(site string) ([]byte, error) {
	if !filepath.IsAbs(site) || filepath.Clean(site) != site || len(filepath.VolumeName(site)) != 2 || !utf8.ValidString(site) ||
		strings.TrimSpace(site) != site || strings.ContainsAny(site, "\x00\r\n\v\f\x1c\x1d\x1e\u0085\u2028\u2029") {
		return nil, errors.New("the PyTorch component site-packages must be one absolute local path line")
	}
	data := []byte(pytorchConfigTemplate + site + "\n")
	if len(data) > configLimit {
		return nil, errors.New("PyTorch component configuration exceeds 64KiB")
	}
	return data, nil
}

// placedPyTorchConfiguration reads the tree's oa-pytorch.pth: the shipped
// template or one configuration pytorchConfiguration derives. It is nil for
// a manifest without the profile.
func placedPyTorchConfiguration(ctx context.Context, m backendManifest, t programfiles.Tree) ([]byte, error) {
	if m.PyTorch == nil {
		return nil, nil
	}
	h, e := fileid.Open(ctx, fileid.Self, filepath.Join(t.Root, filepath.FromSlash(pytorchConfigMember)), fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if e != nil {
		return nil, e
	}
	if h.FileID().IsDir() || h.FileID().Links() != 1 {
		h.Close()
		return nil, errors.New("ordinary PyTorch component configuration required")
	}
	raw, e := io.ReadAll(io.LimitReader(h.File(), configLimit+1))
	if e = errors.Join(e, h.Close()); e != nil {
		return nil, e
	}
	if string(raw) == pytorchConfigTemplate {
		return raw, nil
	}
	site, found := strings.CutPrefix(string(raw), pytorchConfigTemplate)
	site, newline := strings.CutSuffix(site, "\n")
	if !found || !newline {
		return nil, errors.New("oa-pytorch.pth is neither the template nor one component path")
	}
	derived, e := pytorchConfiguration(site)
	if e != nil || !bytes.Equal(derived, raw) {
		return nil, errors.Join(e, errors.New("oa-pytorch.pth is neither the template nor one component path"))
	}
	return raw, nil
}

// withPyTorchConfiguration pins the profile's member to data in a copy of t.
func withPyTorchConfiguration(m backendManifest, t programfiles.Tree, data []byte) (programfiles.Tree, error) {
	if m.PyTorch == nil || data == nil {
		return t, nil
	}
	final := t
	final.Manifest.Files = append([]programfiles.File(nil), t.Manifest.Files...)
	sum := sha256.Sum256(data)
	found := false
	for i, f := range final.Manifest.Files {
		if f.Path == pytorchConfigMember {
			final.Manifest.Files[i].Size = int64(len(data))
			final.Manifest.Files[i].SHA256 = hex.EncodeToString(sum[:])
			found = true
		}
	}
	if !found {
		return t, errors.New("fixed PyTorch component configuration absent")
	}
	return final, final.Manifest.Validate()
}

// placedPyTorchTree pins the configuration the tree holds now.
func placedPyTorchTree(ctx context.Context, m backendManifest, t programfiles.Tree) (programfiles.Tree, error) {
	data, e := placedPyTorchConfiguration(ctx, m, t)
	if e != nil {
		return t, e
	}
	return withPyTorchConfiguration(m, t, data)
}

// placePyTorchConfiguration replaces the verified template with data through
// the same external-side CAS placement the helper configuration uses.
func placePyTorchConfiguration(ctx context.Context, current programfiles.Tree, data []byte) error {
	return replacePyTorchConfiguration(ctx, current, []byte(pytorchConfigTemplate), data)
}

// replacePyTorchConfiguration replaces the verified configuration base with
// data through the same CAS placement.
func replacePyTorchConfiguration(ctx context.Context, current programfiles.Tree, base, data []byte) error {
	if e := programfiles.Verify(ctx, current.Name, current.Root, current.Manifest); e != nil {
		return e
	}
	side := current.Record + ".configuration"
	if _, e := outside(ctx, filepath.Dir(side), current.Root, true); e != nil {
		return e
	}
	side, e := privateLeaf(ctx, side)
	if e != nil {
		return e
	}
	if _, e = outside(ctx, side, current.Root, true); e != nil {
		return e
	}
	placement, e := cas.NewPlacement(current.Root, side)
	if e != nil {
		return e
	}
	if e = ctx.Err(); e != nil {
		return e
	}
	return placement.Write(filepath.Join(current.Root, filepath.FromSlash(pytorchConfigMember)), base, data)
}

// repointPyTorchConfiguration names site in an installed tree whose
// oa-pytorch.pth names another component: Setup updated the PyTorch
// component into a new per-version folder. It proves the installed tree,
// lifts its read-only ACL, replaces the one path line and installs the tree
// again with a new record. A failure after the unlock restores the proven
// tree's ACL and record. It returns the tree to launch.
func repointPyTorchConfiguration(ctx context.Context, verifier *programfiles.Verifier, m backendManifest, current programfiles.Tree, site string) (programfiles.Tree, error) {
	if m.PyTorch == nil || site == "" {
		return current, nil
	}
	wanted, e := pytorchConfiguration(site)
	if e != nil {
		return current, e
	}
	placed, e := placedPyTorchConfiguration(ctx, m, current)
	if e != nil || bytes.Equal(placed, wanted) {
		return current, e
	}
	if string(placed) == pytorchConfigTemplate {
		return current, errors.New("an unsealed oa-pytorch.pth is placed by the seal, not repointed")
	}
	hold, e := verifier.Launch(ctx, current)
	if e != nil {
		return current, e
	}
	if e = hold.Release(); e != nil {
		return current, e
	}
	restore := func(cleanup context.Context) error { return verifier.Install(cleanup, current) }
	if e = openBackendUpdate(ctx, current.Root, programfiles.Unlock, restore); e != nil {
		return current, e
	}
	next, e := withPyTorchConfiguration(m, current, wanted)
	if e == nil {
		e = replacePyTorchConfiguration(ctx, current, placed, wanted)
	}
	if e == nil {
		e = verifier.Install(ctx, next)
	}
	if e != nil {
		cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Minute)
		defer stop()
		if placedNow, readErr := placedPyTorchConfiguration(cleanup, m, current); readErr == nil && bytes.Equal(placedNow, wanted) {
			return current, errors.Join(e, verifier.Install(cleanup, next))
		}
		return current, errors.Join(e, restore(cleanup))
	}
	return next, nil
}

// sealConfigurations places Setup's component configuration beside the helper
// configuration and returns the tree to install. site "" keeps what the
// tree holds; a tree already naming another folder refuses.
func sealConfigurations(ctx context.Context, m backendManifest, t programfiles.Tree, site string) (programfiles.Tree, error) {
	final, data, e := configuredTree(m, t)
	if e != nil {
		return t, e
	}
	placed, e := placedPyTorchConfiguration(ctx, m, t)
	if e != nil {
		return t, e
	}
	wanted := placed
	if m.PyTorch != nil && site != "" {
		if wanted, e = pytorchConfiguration(site); e != nil {
			return t, e
		}
		if !bytes.Equal(placed, wanted) && string(placed) != pytorchConfigTemplate {
			return t, errors.New("oa-pytorch.pth already names another PyTorch component")
		}
	}
	original, e := withPyTorchConfiguration(m, t, placed)
	if e != nil {
		return t, e
	}
	current, e := withPyTorchConfiguration(m, final, placed)
	if e != nil {
		return t, e
	}
	if e = placeConfiguration(ctx, original, current, data); e != nil {
		return t, e
	}
	if !bytes.Equal(placed, wanted) {
		if e = placePyTorchConfiguration(ctx, current, wanted); e != nil {
			return t, e
		}
	}
	return withPyTorchConfiguration(m, final, wanted)
}

func archiveEnvironment(ctx context.Context, temporary, backendParent string) ([]string, error) {
	if _, e := privateLeaf(ctx, backendParent); e != nil {
		return nil, e
	}
	dir, e := privateLeaf(ctx, temporary)
	if e != nil {
		return nil, e
	}
	return (supervision.CleanEnvironment{Temp: dir}).Build()
}

// Only the two declared app-owned leaves are created. Existing directories
// retain their descriptor and must already satisfy the canonical private owner.
func privateLeaf(ctx context.Context, name string) (string, error) {
	if !filepath.IsAbs(name) {
		return "", errors.New("absolute private leaf required")
	}
	parent, e := ordinary(ctx, filepath.Dir(name), true)
	if e != nil {
		return "", e
	}
	if e = fileid.SafeName(filepath.Base(name)); e != nil {
		return "", e
	}
	dir := filepath.Join(parent, filepath.Base(name))
	if _, e = os.Lstat(dir); errors.Is(e, os.ErrNotExist) {
		if e = privatedir.Make(dir); e != nil {
			return "", e
		}
	} else if e != nil {
		return "", e
	}
	if _, e = ordinary(ctx, dir, true); e != nil {
		return "", e
	}
	if e = privatedir.CheckDir(dir); e != nil {
		return "", e
	}
	return dir, nil
}

func runOptions(ctx context.Context, o options, m backendManifest, t programfiles.Tree) (supervision.Options, error) {
	paths := map[string]string{}
	for _, key := range []string{"--user", "--input", "--output", "--temporary", "--models", "--cache", "--session"} {
		// Refuse a code parent before creating any selected mutable leaf.
		if _, e := outside(ctx, filepath.Dir(o.values[key]), t.Root, true); e != nil {
			return supervision.Options{}, e
		}
		if _, e := privateLeaf(ctx, o.values[key]); e != nil {
			return supervision.Options{}, e
		}
		p, e := outside(ctx, o.values[key], t.Root, true)
		if e != nil {
			return supervision.Options{}, e
		}
		paths[key] = p
	}
	reservedState, e := reservedTarget(ctx, o.values["--state"], t.Root)
	if e != nil {
		return supervision.Options{}, e
	}
	reservedRecord, e := reservedTarget(ctx, t.Record, t.Root)
	if e != nil {
		return supervision.Options{}, e
	}
	if identity.SamePathName(reservedState, reservedRecord) {
		return supervision.Options{}, errors.New("metadata record and state directory must be distinct")
	}
	state, e := stateDirectory(ctx, o.values["--state"], t.Root)
	if e != nil {
		return supervision.Options{}, e
	}
	if identity.SamePathName(state, reservedRecord) {
		return supervision.Options{}, errors.New("metadata records must be distinct")
	}
	previews, e := privateLeaf(ctx, filepath.Join(paths["--cache"], "previews"))
	if e != nil {
		return supervision.Options{}, e
	}
	if e = privatedir.CheckDir(paths["--temporary"]); e != nil {
		return supervision.Options{}, e
	}
	set := []string{"PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1", "PIP_NO_INDEX=1", "PIP_CONFIG_FILE=NUL", "XDG_CACHE_HOME=" + paths["--cache"]}
	// These are the existing PyTorch provider's supported cache/offline inputs.
	// Every writable destination is a checked private child outside the code tree.
	for _, item := range []struct{ name, child string }{
		{"HF_HOME", "hf"}, {"TORCH_HOME", "torch"},
		{"TORCHINDUCTOR_CACHE_DIR", "inductor"}, {"TRITON_CACHE_DIR", "triton"},
		{"AMD_COMGR_CACHE_DIR", "comgr"}, {"MIOPEN_USER_DB_PATH", "miopen-db"},
		{"MIOPEN_CUSTOM_CACHE_DIR", "miopen-cache"},
	} {
		dir, err := privateLeaf(ctx, filepath.Join(paths["--cache"], item.child))
		if err != nil {
			return supervision.Options{}, err
		}
		set = append(set, item.name+"="+dir)
	}
	set = append(set, "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1", "HF_DATASETS_OFFLINE=1", "HF_HUB_DISABLE_TELEMETRY=1", "HF_HUB_DISABLE_IMPLICIT_TOKEN=1", "DO_NOT_TRACK=1")
	if m.PyTorch != nil {
		// rocm_sdk in the shared component resolves the GPU family from this,
		// as it does for pytorchd, instead of running offload-arch from PATH.
		set = append(set, "ROCM_SDK_TARGET_FAMILY="+m.PyTorch.TargetFamily)
	}
	program, err := os.Executable()
	if err != nil {
		return supervision.Options{}, err
	}
	set = append(set, "ABSTRACTION_COMFYUI_NATIVE_RELAY="+identity.CanonicalProgramPath(program))
	if runtime := o.values["--development-runtime"]; runtime != "" {
		program, err := ordinary(ctx, runtime, false)
		if err != nil {
			return supervision.Options{}, err
		}
		endpoint := o.values["--development-endpoint"]
		if strings.ContainsAny(endpoint, "\r\n\x00") {
			return supervision.Options{}, errors.New("development endpoint refused")
		}
		set = append(set, "ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM="+program, "ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT="+endpoint)
	}
	env, e := (supervision.CleanEnvironment{Temp: paths["--temporary"], Set: set}).Build()
	if e != nil {
		return supervision.Options{}, e
	}
	if _, sdk := m.Runtime[rocmSDKMember]; sdk {
		bin, err := rocmDetectorDirectory(ctx, m, t)
		if err != nil {
			return supervision.Options{}, err
		}
		// Preserve the builder's kernel-derived Windows PATH. The SDK prepends
		// its Scripts directory and discovers this recorded raw LLVM executable.
		for i, entry := range env {
			name, value, _ := strings.Cut(entry, "=")
			if strings.EqualFold(name, "PATH") {
				env[i] = name + "=" + bin + ";" + value
			}
		}
	}
	args := []string{"-B", "-s", "-S", filepath.Join(t.Root, filepath.FromSlash(launcherMember)), "--state", state, "--records", m.Seal.Records, "--", filepath.Join(t.Root, filepath.FromSlash(mainMember)), "--listen", "127.0.0.1", "--port", strconv.Itoa(o.port), "--disable-auto-launch", "--offline", "--user-directory", paths["--user"], "--input-directory", paths["--input"], "--output-directory", paths["--output"], "--temp-directory", paths["--temporary"], "--models-directory", paths["--models"], "--previews-directory", previews}
	if config := o.values["--extra-model-paths-config"]; config != "" {
		args = append(args, "--extra-model-paths-config", config)
	}
	return supervision.Options{Executable: filepath.Join(t.Root, filepath.FromSlash(pythonMember)), Args: args, Env: env, Dir: paths["--session"], Stdout: os.Stdout, Stderr: os.Stderr, ForceTimeout: 10 * time.Second}, nil
}

// rocmDetectorDirectory binds discovery to the same manifest and ordinary tree
// that execute subsequently verifies and holds through the owned child lifetime.
// Older supported manifests without rocm_sdk retain their previous PATH.
func rocmDetectorDirectory(ctx context.Context, m backendManifest, t programfiles.Tree) (string, error) {
	p, ok := m.Runtime[rocmDetectorMember]
	if !ok || p.Bytes <= 0 || !digest(p.SHA256) {
		return "", errors.New("recorded ROCm detector required")
	}
	listed := false
	for _, entry := range t.Manifest.Files {
		if entry.Path == rocmDetectorMember && entry.Size == p.Bytes && entry.SHA256 == p.SHA256 {
			listed = true
		}
	}
	if !listed {
		return "", errors.New("ROCm detector differs from held tree manifest")
	}
	path := filepath.Join(t.Root, filepath.FromSlash(rocmDetectorMember))
	dir, err := ordinary(ctx, filepath.Dir(path), true)
	if err != nil {
		return "", err
	}
	h, err := fileid.Open(ctx, fileid.Self, path, fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if err != nil {
		return "", err
	}
	defer h.Close()
	if h.FileID().IsDir() || h.FileID().Links() != 1 {
		return "", errors.New("ordinary ROCm detector required")
	}
	sum := sha256.New()
	n, err := io.Copy(sum, io.LimitReader(h.File(), p.Bytes+1))
	if err != nil {
		return "", err
	}
	if n != p.Bytes || hex.EncodeToString(sum.Sum(nil)) != p.SHA256 {
		return "", errors.New("ROCm detector pin differs")
	}
	return dir, nil
}

type child interface {
	Stop() error
	Wait() (int, error)
}
type launchHold interface{ Release() error }

type joinedHolds []launchHold

func (holds joinedHolds) Release() error {
	var result error
	for _, hold := range holds {
		result = errors.Join(result, hold.Release())
	}
	return result
}

// This is the JSON subset of ComfyUI's category YAML input. One fixed group
// carries exactly the three existing Klein categories, each an absolute folder.
// No YAML tags, aliases, arbitrary categories or code paths are interpreted.
func modelConfiguration(ctx context.Context, raw []byte) error {
	if len(raw) == 0 || len(raw) > configLimit || !utf8.Valid(raw) {
		return errors.New("model configuration bound refused")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	if err := jsonValue(d, 0, ""); err != nil {
		return err
	}
	if _, err := d.Token(); err != io.EOF {
		return errors.New("model configuration trailing data")
	}
	group, err := exactKeys(raw, "oa-models")
	if err != nil {
		return err
	}
	fields, err := exactKeys(group["oa-models"], "diffusion_models", "text_encoders", "vae")
	if err != nil {
		return err
	}
	for _, key := range []string{"diffusion_models", "text_encoders", "vae"} {
		var path string
		if err := json.Unmarshal(fields[key], &path); err != nil || path == "" || strings.ContainsAny(path, "\r\n\v\f\u0085\u2028\u2029") {
			return errors.New("model category must name one ordinary absolute folder")
		}
		if _, err := ordinary(ctx, path, true); err != nil {
			return err
		}
	}
	return nil
}

// holdRunModelConfiguration holds the run's model-paths file. An optional
// file the launcher cannot hold is dropped with one line on log, and the run
// goes on with ComfyUI's own models folder.
func holdRunModelConfiguration(ctx context.Context, o options, t programfiles.Tree, log io.Writer) (launchHold, options, error) {
	hold, err := holdModelConfiguration(ctx, o, t)
	if err != nil && o.optionalModelPaths && o.values["--extra-model-paths-config"] != "" {
		fmt.Fprintf(log, "ComfyUI starts without OpenAbstractions' model folders: %v\n", err)
		o = withoutModelPaths(o)
		hold, err = holdModelConfiguration(ctx, o, t)
	}
	return hold, o, err
}

// withoutModelPaths is o without its extra model-paths pair.
func withoutModelPaths(o options) options {
	values := make(map[string]string, len(o.values))
	for key, value := range o.values {
		if key != "--extra-model-paths-config" && key != "--extra-model-paths-sha256" {
			values[key] = value
		}
	}
	o.values = values
	return o
}

func holdModelConfiguration(ctx context.Context, o options, t programfiles.Tree) (launchHold, error) {
	name := o.values["--extra-model-paths-config"]
	if name == "" {
		return joinedHolds{}, nil
	}
	path, err := outside(ctx, name, t.Root, false)
	if err != nil {
		return nil, err
	}
	if err = privatedir.CheckDir(filepath.Dir(path)); err != nil {
		return nil, err
	}
	state, err := reservedTarget(ctx, o.values["--state"], t.Root)
	if err != nil {
		return nil, err
	}
	backendRecord, err := reservedTarget(ctx, t.Record, t.Root)
	if err != nil {
		return nil, err
	}
	if identity.SamePathName(path, backendRecord) || identity.SamePathName(path, state) {
		return nil, errors.New("model configuration collides with mutable state")
	}
	h, err := fileid.Open(ctx, fileid.Self, path, fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if err != nil {
		return nil, err
	}
	raw, readErr := io.ReadAll(io.LimitReader(h.File(), configLimit+1))
	closeErr := h.Close()
	if err = errors.Join(readErr, closeErr); err != nil {
		return nil, err
	}
	if err = modelConfiguration(ctx, raw); err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != o.values["--extra-model-paths-sha256"] {
		return nil, errors.New("model configuration digest mismatch")
	}
	record, err := outputFile(ctx, backendRecord+".model-paths.json", t.Root)
	if err != nil {
		return nil, err
	}
	if identity.SamePathName(path, record) || identity.SamePathName(record, state) {
		return nil, errors.New("model configuration record collision")
	}
	hold, err := programfiles.NewVerifier(programfiles.Options{}).LaunchPlaced(ctx, "comfy-model-paths", []programfiles.Placed{{Path: path, Size: int64(len(raw)), SHA256: hex.EncodeToString(sum[:])}}, record)
	if err != nil {
		return nil, err
	}
	o.values["--extra-model-paths-config"] = hold.Paths()[0]
	holds := joinedHolds{hold}
	var mapping map[string]map[string]string
	if err = json.Unmarshal(raw, &mapping); err != nil {
		return nil, errors.Join(err, holds.Release())
	}
	for _, category := range []string{"diffusion_models", "text_encoders", "vae"} {
		directory, err := programfiles.HoldDirectory(mapping["oa-models"][category])
		if err != nil {
			return nil, errors.Join(err, holds.Release())
		}
		holds = append(holds, directory)
	}
	return holds, nil
}

type uncertainOwnedExit struct{ error }

func (e *uncertainOwnedExit) Unwrap() error { return e.error }

func waitOwned(p child, hold launchHold, cancel io.Reader) (int, error) {
	done := make(chan struct{})
	defer close(done)
	go func() {
		var one [1]byte
		_, _ = cancel.Read(one[:])
		select {
		case <-done:
			return
		default:
			_ = p.Stop()
		}
	}()
	code, e := p.Wait()
	if e != nil {
		return 125, &uncertainOwnedExit{e}
	} // Do not release a hold on uncertain tree exit.
	if e = hold.Release(); e != nil {
		return 125, &uncertainOwnedExit{e}
	}
	return code, nil
}

func execute(ctx context.Context, o options) (int, error) {
	return executeWithInput(ctx, o, os.Stdin, os.Stdout, nil)
}

// The native host retains the same verifier/start/wait owner. Its private
// protocol owns stdout, and its cancellation pipe belongs to the host lifetime.
func executeWithInput(ctx context.Context, o options, cancel io.Reader, output *os.File, started func(*supervision.Process) error) (int, error) {
	return executeWithPreparedInput(ctx, o, cancel, output, started, nil)
}

func executeWithPreparedInput(ctx context.Context, o options, cancel io.Reader, output *os.File, started func(*supervision.Process) error, prepare func(*supervision.Options) error) (int, error) {
	if o.operation == "" {
		fmt.Print(usage)
		return 0, nil
	}
	if o.operation == "archive-environment" {
		env, e := archiveEnvironment(ctx, o.values["--temporary"], o.values["--backend-parent"])
		if e != nil {
			return 2, e
		}
		object := map[string]string{}
		for _, entry := range env {
			k, v, _ := strings.Cut(entry, "=")
			object[k] = v
		}
		if e = json.NewEncoder(os.Stdout).Encode(object); e != nil {
			return 2, e
		}
		return 0, nil
	}
	m, t, e := loadTree(ctx, o)
	if e != nil {
		return 2, e
	}
	if e = helperOwnCopy(ctx, m, t); e != nil {
		return 2, e
	}
	verifier := programfiles.NewVerifier(programfiles.Options{})
	if o.operation == "unlock-backend" || o.operation == "check-backend" {
		final, _, err := configuredTree(m, t)
		if err == nil {
			final, err = placedPyTorchTree(ctx, m, final)
		}
		if err != nil {
			return 2, err
		}
		if o.operation == "check-backend" {
			return checkBackend(ctx, verifier, final)
		}
		return unlockBackend(ctx, verifier, final, o.values["--record-sha256"], o.values["--restoration-record"])
	}
	if o.operation == "seal-backend" {
		if _, e = os.Lstat(t.Record); !errors.Is(e, os.ErrNotExist) {
			return 2, errors.New("seal requires a new metadata record")
		}
		parent, e := ordinary(ctx, filepath.Dir(t.Root), true)
		if e != nil {
			return 2, e
		}
		if e = privatedir.CheckDir(parent); e != nil {
			return 2, e
		}
		if t, e = sealConfigurations(ctx, m, t, o.values["--pytorch-site-packages"]); e != nil {
			return 2, e
		}
		if e = verifier.Install(ctx, t); e != nil {
			return 2, e
		}
		return 0, nil
	}
	t, _, e = configuredTree(m, t)
	if e == nil {
		t, e = placedPyTorchTree(ctx, m, t)
	}
	if e != nil {
		return 2, e
	}
	configHold, o, e := holdRunModelConfiguration(ctx, o, t, os.Stderr)
	if e != nil {
		return 2, e
	}
	settings, e := runOptions(ctx, o, m, t)
	if e != nil {
		return 2, errors.Join(e, configHold.Release())
	}
	settings.Stdout = output
	if prepare != nil {
		if e = prepare(&settings); e != nil {
			return 2, errors.Join(e, configHold.Release())
		}
	}
	hold, e := verifier.Launch(ctx, t)
	if e != nil {
		return 2, errors.Join(e, configHold.Release())
	}
	p, e := supervision.Start(ctx, settings)
	if e != nil {
		return 2, errors.Join(e, hold.Release(), configHold.Release())
	}
	if started != nil {
		if err := started(p); err != nil {
			stopErr := p.Stop()
			_, waitErr := waitOwned(p, joinedHolds{hold, configHold}, cancel)
			return 2, errors.Join(err, stopErr, waitErr)
		}
	}
	return waitOwned(p, joinedHolds{hold, configHold}, cancel)
}

func checkBackend(ctx context.Context, verifier *programfiles.Verifier, tree programfiles.Tree) (int, error) {
	if err := verifier.Check(ctx, tree); err != nil {
		return 2, err
	}
	return 0, nil
}

func unlockBackend(ctx context.Context, verifier *programfiles.Verifier, tree programfiles.Tree, recordPin, restoration string) (int, error) {
	parent, err := ordinary(ctx, filepath.Dir(tree.Root), true)
	if err != nil {
		return 2, err
	}
	if err = privatedir.CheckDir(parent); err != nil {
		return 2, err
	}
	restoreRecord, err := outputFile(ctx, restoration, tree.Root)
	if err != nil {
		return 2, err
	}
	if _, err = os.Lstat(restoreRecord); !errors.Is(err, os.ErrNotExist) {
		return 2, errors.New("update restoration requires a new record")
	}
	h, err := fileid.Open(ctx, fileid.Self, tree.Record, fileid.Options{Links: fileid.RefuseLinks, Read: true})
	if err != nil {
		return 2, err
	}
	if h.FileID().IsDir() || h.FileID().Links() != 1 {
		h.Close()
		return 2, errors.New("ordinary update record required")
	}
	raw, readErr := io.ReadAll(io.LimitReader(h.File(), metadataLimit+1))
	closeErr := h.Close()
	sum := sha256.Sum256(raw)
	if readErr != nil || closeErr != nil || len(raw) > metadataLimit || hex.EncodeToString(sum[:]) != recordPin {
		return 2, errors.Join(errors.New("independent update record pin differs"), readErr, closeErr)
	}
	hold, err := verifier.Launch(ctx, tree)
	if err != nil {
		return 2, err
	}
	if err = hold.Release(); err != nil {
		return 2, err
	}
	restoreTree := tree
	restoreTree.Record = restoreRecord
	if err = openBackendUpdate(ctx, tree.Root, programfiles.Unlock, func(cleanup context.Context) error {
		return verifier.Install(cleanup, restoreTree)
	}); err != nil {
		return 2, err
	}
	return 0, nil
}

// The caller owns projection plus unconditional seal-backend in its finally
// path. A failed/cancelled Unlock itself restores through the same full Install
// owner before returning; cancellation cannot cancel that restoration attempt.
func openBackendUpdate(ctx context.Context, root string, unlock func(string) error, restore func(context.Context) error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	err := unlock(root)
	if err == nil {
		err = ctx.Err()
	}
	if err == nil {
		return nil
	}
	cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Minute)
	defer stop()
	return errors.Join(err, restore(cleanup))
}

// Explicit legacy single-file builds retain their existing closed operations.
// The native package adds this fixed entry through its own source/resource unit.
var nativeEntrypoint func(context.Context, []string) error

// localPreviewEntrypoint runs install-local-preview or installed-local-preview
// and returns the one sentence a failure prints. Like nativeEntrypoint, only
// the native package sets it; the single-file Desktop build leaves it nil.
var localPreviewEntrypoint func(ctx context.Context, operation string, out io.Writer) (failure string)

const interpreterFileLimit int64 = 64 << 20

var interpreterProgram = regexp.MustCompile(`(?i)^python\.exe$`)
var interpreterDLL = regexp.MustCompile(`(?i)^python3[0-9]+\.dll$`)
var interpreterCopy = regexp.MustCompile(`(?i)^(python\.exe|python3[0-9]*\.dll|vcruntime140(_[0-9]+)?\.dll)$`)

type interpreterSource struct {
	Name   string `json:"name"`
	ID     string `json:"id"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
	path   string
	data   []byte
}
type interpreterStamp struct {
	Profile string              `json:"profile"`
	Home    string              `json:"home"`
	HomeID  string              `json:"home_id"`
	Files   []interpreterSource `json:"files"`
}

func interpreterSources(ctx context.Context, home string) ([]interpreterSource, error) {
	entries, err := os.ReadDir(home)
	if err != nil {
		return nil, err
	}
	var files []interpreterSource
	program, dll := false, false
	for _, entry := range entries {
		if !interpreterCopy.MatchString(entry.Name()) {
			continue
		}
		if len(files) >= 16 {
			return nil, errors.New("interpreter source roster exceeds its bound")
		}
		path := filepath.Join(home, entry.Name())
		h, err := fileid.Open(ctx, fileid.Self, path, fileid.Options{Links: fileid.RefuseLinks, Read: true})
		if err != nil {
			return nil, err
		}
		info, err := h.File().Stat()
		if err != nil || !info.Mode().IsRegular() || info.Size() > interpreterFileLimit {
			h.Close()
			return nil, errors.New("interpreter source is not a bounded plain file")
		}
		data, readErr := io.ReadAll(io.LimitReader(h.File(), interpreterFileLimit+1))
		closeErr := h.Close()
		if err := errors.Join(readErr, closeErr, ctx.Err()); err != nil {
			return nil, err
		}
		if int64(len(data)) != info.Size() {
			return nil, errors.New("interpreter source changed while read")
		}
		sum := sha256.Sum256(data)
		files = append(files, interpreterSource{Name: entry.Name(), ID: h.FileID().Key(), Bytes: info.Size(), SHA256: hex.EncodeToString(sum[:]), path: path, data: data})
		program = program || interpreterProgram.MatchString(entry.Name())
		dll = dll || interpreterDLL.MatchString(entry.Name())
	}
	if !program || !dll {
		return nil, errors.New("interpreter home lacks python.exe and its Python DLL")
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Name < files[j].Name })
	return files, nil
}

func interpreterCopies(ctx context.Context, folder string, files []interpreterSource) bool {
	for _, source := range files {
		copy, err := fileid.Open(ctx, fileid.Self, filepath.Join(folder, source.Name), fileid.Options{Links: fileid.RefuseLinks, Read: true})
		if err != nil {
			return false
		}
		data, readErr := io.ReadAll(io.LimitReader(copy.File(), source.Bytes+1))
		id := copy.FileID()
		closeErr := copy.Close()
		sum := sha256.Sum256(data)
		if readErr != nil || closeErr != nil || id.IsDir() || id.Links() != 1 || id.Key() == source.ID || int64(len(data)) != source.Bytes || hex.EncodeToString(sum[:]) != source.SHA256 {
			return false
		}
	}
	return true
}

// prepareInterpreterAt is the existing app-owned operation. Its executable
// parent supplies extension; no external caller can select an output path.
func prepareInterpreterAt(ctx context.Context, extension, home string) (_ string, result error) {
	if !filepath.IsAbs(home) || !filepath.IsAbs(extension) {
		return "", errors.New("absolute interpreter home and extension required")
	}
	if err := identity.LocalPath(home); err != nil {
		return "", err
	}
	homeHandle, err := fileid.Open(ctx, fileid.Self, home, fileid.Options{})
	if err != nil {
		return "", err
	}
	defer homeHandle.Close()
	if !homeHandle.FileID().IsDir() {
		return "", errors.New("interpreter home is not a directory")
	}
	home = homeHandle.FileID().Path()
	files, err := interpreterSources(ctx, home)
	if err != nil {
		return "", err
	}
	root, err := fileid.Open(ctx, fileid.Self, extension, fileid.Options{})
	if err != nil {
		return "", err
	}
	defer root.Close()
	if !root.FileID().IsDir() {
		return "", errors.New("extension is not a directory")
	}
	extension = root.FileID().Path()
	rootHold, err := programfiles.HoldDirectory(extension)
	if err != nil {
		return "", err
	}
	defer func() { result = errors.Join(result, rootHold.Release()) }()
	current, err := fileid.Resolve(ctx, fileid.Self, extension, fileid.Options{Links: fileid.RefuseLinks})
	if err != nil || !current.Equal(root.FileID()) {
		return "", errors.New("extension identity changed")
	}
	folder := filepath.Join(extension, "helper")
	if err := os.Mkdir(folder, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return "", err
	}
	helper, err := fileid.Open(ctx, fileid.Self, folder, fileid.Options{Links: fileid.RefuseLinks})
	if err != nil {
		return "", err
	}
	defer helper.Close()
	if !helper.FileID().IsDir() || helper.FileID().Equal(homeHandle.FileID()) {
		return "", errors.New("helper folder is not a distinct plain directory")
	}
	helperHold, err := programfiles.HoldDirectory(folder)
	if err != nil {
		return "", err
	}
	defer func() { result = errors.Join(result, helperHold.Release()) }()
	current, err = fileid.Resolve(ctx, fileid.Self, folder, fileid.Options{Links: fileid.RefuseLinks})
	if err != nil || !current.Equal(helper.FileID()) {
		return "", errors.New("helper identity changed before hold")
	}
	proof, err := os.MkdirTemp(folder, ".preparing-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(proof)
	placed := make([]programfiles.Placed, len(files))
	for i, source := range files {
		placed[i] = programfiles.Placed{Path: source.path, Size: source.Bytes, SHA256: source.SHA256}
	}
	hold, err := programfiles.NewVerifier(programfiles.Options{}).LaunchPlaced(ctx, "Comfy interpreter sources", placed, filepath.Join(proof, "inputs.json"))
	if err != nil {
		return "", err
	}
	defer func() { result = errors.Join(result, hold.Release()) }()
	current, err = fileid.Resolve(ctx, fileid.Self, home, fileid.Options{Links: fileid.RefuseLinks})
	if err != nil || !current.Equal(homeHandle.FileID()) {
		return "", errors.New("interpreter home identity changed before hold")
	}
	for _, source := range files {
		key, err := fileid.FileKey(source.path)
		if err != nil || key != source.ID {
			return "", errors.New("interpreter source identity changed before hold")
		}
	}
	stamp := interpreterStamp{Profile: "oa-comfy-interpreter@2", Home: home, HomeID: homeHandle.FileID().Key(), Files: files}
	raw, err := json.Marshal(stamp)
	if err != nil {
		return "", err
	}
	raw = append(raw, '\n')
	config := []byte("home = " + home + "\ninclude-system-site-packages = false\n")
	err = cas.ChangeLimitContext(ctx, filepath.Join(folder, "source.json"), 64<<10, func(previous []byte) ([]byte, error) {
		cfg, err := cas.ReadLimitContext(ctx, filepath.Join(folder, "pyvenv.cfg"), 64<<10)
		if err != nil {
			return nil, err
		}
		if bytes.Equal(previous, raw) && bytes.Equal(cfg, config) && interpreterCopies(ctx, folder, files) {
			return previous, nil
		}
		wanted := map[string]bool{}
		for _, source := range files {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			wanted[source.Name] = true
			target := filepath.Join(folder, source.Name)
			leaf, err := fileid.ResolveLeaf(ctx, fileid.Self, target)
			if err != nil {
				return nil, err
			}
			stage, err := os.CreateTemp(folder, leaf.Name()+".*.tmp")
			if err != nil {
				return nil, err
			}
			name := stage.Name()
			_, writeErr := stage.Write(source.data)
			syncErr := stage.Sync()
			closeErr := stage.Close()
			if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
				os.Remove(name)
				return nil, err
			}
			id, err := fileid.Resolve(ctx, fileid.Self, name, fileid.Options{Links: fileid.RefuseLinks})
			if err == nil {
				err = cas.ReplaceStagedContext(ctx, target, name, id, sha256.Sum256(source.data), source.Bytes)
			}
			if err != nil {
				os.Remove(name)
				return nil, fmt.Errorf("interpreter refresh refused; close ComfyUI before retrying: %w", err)
			}
		}
		entries, err := os.ReadDir(folder)
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			if interpreterCopy.MatchString(entry.Name()) && !wanted[entry.Name()] {
				if err := os.Remove(filepath.Join(folder, entry.Name())); err != nil {
					return nil, err
				}
			}
		}
		if !interpreterCopies(ctx, folder, files) {
			return nil, errors.New("refreshed interpreter is not its admitted source copy")
		}
		if err := cas.ChangeLimitContext(ctx, filepath.Join(folder, "pyvenv.cfg"), 64<<10, func([]byte) ([]byte, error) { return config, nil }); err != nil {
			return nil, err
		}
		return raw, nil
	})
	if err != nil {
		return "", err
	}
	for _, source := range files {
		if interpreterProgram.MatchString(source.Name) {
			return filepath.Join(folder, source.Name), nil
		}
	}
	return "", errors.New("interpreter program unavailable")
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "prepare-interpreter" {
		if len(os.Args) != 4 || os.Args[2] != "--home" {
			fmt.Fprintln(os.Stderr, "exact prepare-interpreter --home ABS arguments required")
			os.Exit(2)
		}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
		defer cancel()
		self, err := os.Executable()
		var program string
		if err == nil {
			program, err = prepareInterpreterAt(ctx, filepath.Dir(self), os.Args[3])
		}
		if err != nil {
			message := err.Error()
			if len(message) > 4096 {
				message = message[:4096]
			}
			fmt.Fprintln(os.Stderr, message)
			os.Exit(2)
		}
		fmt.Println(program)
		return
	}
	if len(os.Args) == 2 && localPreviewEntrypoint != nil && (os.Args[1] == "install-local-preview" || os.Args[1] == "installed-local-preview") {
		if failure := localPreviewEntrypoint(context.Background(), os.Args[1], os.Stdout); failure != "" {
			fmt.Fprintln(os.Stderr, failure)
			os.Exit(2)
		}
		return
	}
	if len(os.Args) >= 2 && nativeEntrypoint != nil && !(len(os.Args) == 3 && (os.Args[2] == "--help" || os.Args[2] == "-h")) && (os.Args[1] == "owned-host" || os.Args[1] == "register-self" || os.Args[1] == "native-relay" || os.Args[1] == "seal-native-control" || os.Args[1] == "seal-native-install" || os.Args[1] == "check-native-install" || os.Args[1] == "native-install-plan") {
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		if err := nativeEntrypoint(ctx, os.Args[1:]); err != nil {
			fmt.Fprintln(os.Stderr, "OA native Comfy host unavailable:", err)
			os.Exit(2)
		}
		return
	}
	o, e := parseOptions(os.Args[1:])
	if e != nil {
		fmt.Fprintln(os.Stderr, "OA backend refused:", e)
		os.Exit(2)
	}
	code, e := execute(context.Background(), o)
	if e != nil {
		fmt.Fprintln(os.Stderr, "OA backend refused:", e)
	}
	os.Exit(code)
}
