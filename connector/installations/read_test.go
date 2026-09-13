package installations

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReadPreservesEntryPositionsAndCallerShape(t *testing.T) {
	file := filepath.Join(t.TempDir(), "installations.json")
	raw := `[{"id":"first","extra":false}, [], {"id":"third","extra":"text"}]`
	if err := os.WriteFile(file, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	records, err := Read(file)
	if err != nil || len(records) != 3 {
		t.Fatalf("read %d records: %v", len(records), err)
	}
	var identity struct {
		ID string `json:"id"`
	}
	if err := records.Decode(0, &identity); err != nil || identity.ID != "first" {
		t.Fatalf("first record = %+v, %v", identity, err)
	}
	if err := records.Decode(1, &identity); err == nil {
		t.Fatal("array decoded as an installation")
	}
	var launch struct {
		Extra string `json:"extra"`
	}
	if err := records.Decode(0, &launch); err == nil {
		t.Fatal("launch accepted the wrong field type")
	}
	if err := records.Decode(2, &launch); err != nil || launch.Extra != "text" {
		t.Fatalf("third record = %+v, %v", launch, err)
	}
}

func TestReadDistinguishesDocumentAndReadFailures(t *testing.T) {
	file := filepath.Join(t.TempDir(), "installations.json")
	if _, err := Read(file); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("missing file: %v", err)
	}
	for _, test := range []struct {
		raw       string
		listError bool
	}{
		{"{}", true}, {"[]{}", true},
		{"[]" + strings.Repeat(" ", readLimit-2), false},
		{"[]" + strings.Repeat(" ", readLimit-1), false},
	} {
		if err := os.WriteFile(file, []byte(test.raw), 0600); err != nil {
			t.Fatal(err)
		}
		_, err := Read(file)
		if (err == nil) != (len(test.raw) == readLimit) {
			t.Fatalf("read %d bytes: %v", len(test.raw), err)
		}
		var listError *ListError
		if errors.As(err, &listError) != test.listError {
			t.Fatalf("unexpected error class: %v", err)
		}
	}
}
