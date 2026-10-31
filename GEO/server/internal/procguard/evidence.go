package procguard

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// MemoryEvidence only reads counters. The cgroup is shared by all browser
// tasks, so a changed OOM counter cannot identify which page caused it.
type MemoryEvidence struct {
	CurrentBytes uint64            `json:"current_bytes"`
	High         string            `json:"high"`
	Max          string            `json:"max"`
	Events       map[string]uint64 `json:"events"`
	Pressure     string            `json:"pressure"`
	Scope        string            `json:"scope"`
}

func BrowserMemoryEvidence() *MemoryEvidence { return readMemoryEvidence(cgroupDir) }

func readMemoryEvidence(root string) *MemoryEvidence {
	read := func(name string) string {
		data, _ := os.ReadFile(filepath.Join(root, name))
		return strings.TrimSpace(string(data))
	}
	current, err := strconv.ParseUint(read("memory.current"), 10, 64)
	if err != nil {
		return nil
	}
	events := map[string]uint64{}
	for _, line := range strings.Split(read("memory.events"), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			continue
		}
		n, err := strconv.ParseUint(fields[1], 10, 64)
		if err == nil {
			events[fields[0]] = n
		}
	}
	return &MemoryEvidence{CurrentBytes: current, High: read("memory.high"), Max: read("memory.max"), Events: events, Pressure: read("memory.pressure"), Scope: "shared_browser_cgroup"}
}
