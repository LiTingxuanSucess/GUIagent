package procguard

import (
	"os"
	"path/filepath"
	"testing"
)

func TestMemoryEvidenceReadOnlyAndUnavailable(t *testing.T) {
	root := t.TempDir()
	if readMemoryEvidence(root) != nil {
		t.Fatal("invented memory evidence")
	}
	for file, value := range map[string]string{"memory.current": "12345", "memory.high": "4000", "memory.max": "max", "memory.events": "oom 2\noom_kill 1\nbad value\n", "memory.pressure": "some avg10=1.00 total=42"} {
		if err := os.WriteFile(filepath.Join(root, file), []byte(value), 0400); err != nil {
			t.Fatal(err)
		}
	}
	e := readMemoryEvidence(root)
	if e == nil || e.CurrentBytes != 12345 || e.Events["oom"] != 2 || e.Events["oom_kill"] != 1 || e.Max != "max" || e.Scope != "shared_browser_cgroup" {
		t.Fatalf("%+v", e)
	}
	raw, _ := os.ReadFile(filepath.Join(root, "memory.events"))
	if string(raw) != "oom 2\noom_kill 1\nbad value\n" {
		t.Fatal("snapshot mutated cgroup")
	}
}
