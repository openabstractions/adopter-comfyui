// Package personfolders keeps a test binary away from the person's own
// per-user folders. Only tests import it.
//
// Every Go module of this repository whose tests can reach a per-user folder
// keeps an identical copy of this file at internal/personfolders; the source
// is monitor/internal/personfolders/personfolders.go. A module publishes on
// its own, so it cannot import another module's copy. scripts/user_folders.py
// --check refuses a copy that differs from the source and a test package that
// can reach a per-user folder without a TestMain that calls Main.
package personfolders

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// under returns the environment variables that move the person's per-user
// folders inside person on every platform, laid out as each platform lays
// out a home folder: USERPROFILE, APPDATA and LOCALAPPDATA on Windows, HOME
// and the XDG_* folders on Linux, and HOME on macOS, where the folders are
// $HOME/Library/Application Support and $HOME/Library/Caches. Windows'
// known folders under the profile (windows.KnownFolderPath with
// FOLDERID_LocalAppData, FOLDERID_Pictures and the like) follow USERPROFILE
// as well, once their folder exists: the shell expands the %USERPROFILE% its
// registry records them under from the process's environment, on the
// process's first lookup. FOLDERID_Profile itself stays the account's.
// Setting one platform's variable alone leaves the account's real folders in
// use on the others.
func under(person string) map[string]string {
	return map[string]string{
		"HOME":            person,
		"USERPROFILE":     person,
		"APPDATA":         filepath.Join(person, "AppData", "Roaming"),
		"LOCALAPPDATA":    filepath.Join(person, "AppData", "Local"),
		"XDG_CONFIG_HOME": filepath.Join(person, ".config"),
		"XDG_CACHE_HOME":  filepath.Join(person, ".cache"),
		"XDG_DATA_HOME":   filepath.Join(person, ".local", "share"),
		"XDG_STATE_HOME":  filepath.Join(person, ".local", "state"),
	}
}

// folders are the folders under a stand-in person that a real account has
// before any program runs, on every platform. A Windows known folder that
// does not exist does not resolve.
var folders = []string{
	filepath.Join("AppData", "Roaming"),
	filepath.Join("AppData", "Local"),
	".config", ".cache",
	filepath.Join(".local", "share"),
	filepath.Join(".local", "state"),
	filepath.Join("Library", "Application Support"),
	filepath.Join("Library", "Caches"),
	filepath.Join("Library", "Preferences"),
	"Desktop", "Documents", "Downloads", "Pictures",
}

// Under moves the per-user folders inside person for one test: it creates
// the folders a real home has under person and sets each platform's
// variables with setenv, which is the test's t.Setenv. A test that needs an
// empty person of its own calls Under(t.TempDir(), t.Setenv).
func Under(person string, setenv func(name, value string)) error {
	if err := makeFolders(person); err != nil {
		return err
	}
	for name, value := range under(person) {
		setenv(name, value)
	}
	return nil
}

// makeFolders creates folders under person.
func makeFolders(person string) error {
	for _, folder := range folders {
		if err := os.MkdirAll(filepath.Join(person, folder), 0o700); err != nil {
			return err
		}
	}
	return nil
}

// Main runs m with every per-user folder inside a fresh temporary folder,
// removes that folder afterwards and returns the exit code for os.Exit. A
// package's TestMain ends with os.Exit(personfolders.Main(m)); m is the
// *testing.M.
//
// The toolchains' folders stay the person's: GOPATH, GOCACHE and GOENV, and
// RUSTUP_HOME and CARGO_HOME where they exist, are set to the folders the go,
// rustup and cargo commands would otherwise derive from the moved variables,
// so a test that builds keeps the installed toolchains, their caches and
// the go env settings.
func Main(m interface{ Run() int }) int {
	person, err := Enter()
	if err != nil {
		//unchecked: the exit code 2 reports the failure; stderr is where a test binary says why
		fmt.Fprintln(os.Stderr, "personfolders:", err)
		return 2
	}
	code := m.Run()
	Leave(person)
	return code
}

// marker names the stand-in person in the environment, so a process a test
// starts, the test binary itself as a helper child among them, uses its
// parent's stand-in instead of making its own.
const marker = "OA_TEST_PERSON"

// made is the stand-in this process made, which Leave removes.
var made string

// Enter moves this process's per-user folders into a fresh temporary folder
// and returns it; Leave removes it. Main calls both. A TestMain that needs
// the stand-in's folder calls them itself. Enter runs before any test looks
// a folder up: Windows keeps a process's first known-folder answer.
//
// A process that inherited a stand-in (marker) keeps it, and one whose
// environment names no home at all (neither HOME nor USERPROFILE) keeps the
// environment unchanged: that is a helper child its parent started with a
// reduced environment on purpose, and it may run where it cannot write.
func Enter() (string, error) {
	if inherited := os.Getenv(marker); inherited != "" {
		return inherited, nil
	}
	if os.Getenv("HOME") == "" && os.Getenv("USERPROFILE") == "" {
		return "", nil
	}
	if err := keepToolchain(); err != nil {
		return "", err
	}
	person, err := os.MkdirTemp("", "oa-test-person-")
	if err != nil {
		return "", err
	}
	moved := under(person)
	moved[marker] = person
	if err := makeFolders(person); err != nil {
		return "", errors.Join(err, os.RemoveAll(person))
	}
	for name, value := range moved {
		if err := os.Setenv(name, value); err != nil {
			return "", errors.Join(err, os.RemoveAll(person))
		}
	}
	made = person
	return person, nil
}

// keepToolchain names the toolchains' folders explicitly before the per-user
// variables move. A folder this account cannot resolve stays unnamed, and
// the toolchain derives it as before.
func keepToolchain() error {
	keep := map[string]string{}
	if home, err := os.UserHomeDir(); err == nil {
		keep["GOPATH"] = filepath.Join(home, "go")
		// Rust's toolchain manager and package cache live in the home folder.
		for name, folder := range map[string]string{"RUSTUP_HOME": ".rustup", "CARGO_HOME": ".cargo"} {
			if info, err := os.Stat(filepath.Join(home, folder)); err == nil && info.IsDir() {
				keep[name] = filepath.Join(home, folder)
			}
		}
	}
	if cache, err := os.UserCacheDir(); err == nil {
		keep["GOCACHE"] = filepath.Join(cache, "go-build")
	}
	if config, err := os.UserConfigDir(); err == nil {
		keep["GOENV"] = filepath.Join(config, "go", "env")
	}
	for name, folder := range keep {
		if os.Getenv(name) != "" {
			continue
		}
		if err := os.Setenv(name, folder); err != nil {
			return err
		}
	}
	return nil
}

// Leave deletes the stand-in person this process's Enter made; an inherited
// one stays for its owner. A child process a test started can still hold a
// file there for a moment on Windows, so removal retries for ten seconds; a
// folder that stays is reported and left to the temporary folder's own
// cleanup, outside the person's folders.
func Leave(person string) {
	if person == "" || person != made {
		return
	}
	made = ""
	var err error
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(200 * time.Millisecond) {
		if err = os.RemoveAll(person); err == nil || time.Now().After(deadline) {
			break
		}
	}
	if err != nil {
		//unchecked: the folder lies in the temporary folder, outside the person's; stderr is the only place to say so
		fmt.Fprintln(os.Stderr, "personfolders: the stand-in person stays in the temporary folder:", err)
	}
}
