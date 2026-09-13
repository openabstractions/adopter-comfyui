// Package installations reads Comfy Desktop's installation records. Consumers
// decide which records can be discovered, attached, or started.
package installations

import (
	"encoding/json"
	"errors"
	"io"
	"os"
)

const readLimit = 1 << 20

// Records preserves every entry in its original order, including malformed
// entries. Decode each entry against the fields its caller consumes.
type Records []json.RawMessage

func (r Records) Decode(index int, entry any) error {
	return json.Unmarshal(r[index], entry)
}

// ListError distinguishes an invalid document from a file read failure.
type ListError struct{ error }

// Read reads the selected installations.json without exceeding the document
// bound. Individual entry errors are reported by Decode.
func Read(file string) (Records, error) {
	f, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, readLimit+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > readLimit {
		return nil, errors.New("settings file too large")
	}
	var records Records
	if err := json.Unmarshal(raw, &records); err != nil {
		return nil, &ListError{err}
	}
	return records, nil
}
