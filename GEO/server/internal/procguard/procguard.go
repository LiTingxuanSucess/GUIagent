// Package procguard keeps browser and scraper processes from freezing the
// API. Those processes move into their own memory cgroup and run at a lower
// CPU priority, so a status poll can still be served when Chrome is busy.
package procguard

import (
	"bytes"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	defaultHighMB = 1600
	defaultMaxMB  = 1900
	cgroupDir     = "/sys/fs/cgroup/geo-browsers"
)

var (
	once      sync.Once
	procsPath string
)

// Adopt moves pid and the processes it starts into the browser cgroup.
// Later children inherit that cgroup, and the walk repeats briefly so
// processes forked during startup are included.
func Adopt(pid int) {
	if pid <= 1 {
		return
	}
	prepare()
	go func() {
		deadline := time.Now().Add(20 * time.Second)
		for {
			moveTree(pid, map[int]bool{})
			if time.Now().After(deadline) {
				return
			}
			time.Sleep(500 * time.Millisecond)
		}
	}()
}

func prepare() {
	once.Do(func() {
		highMB := envMB("GEO_BROWSER_MEMORY_HIGH_MB", defaultHighMB)
		maxMB := envMB("GEO_BROWSER_MEMORY_MAX_MB", defaultMaxMB)
		if highMB <= 0 && maxMB <= 0 {
			log.Printf("browser cgroup disabled")
			return
		}
		if err := os.MkdirAll(cgroupDir, 0o755); err != nil {
			log.Printf("browser cgroup mkdir: %v", err)
			return
		}
		if highMB > 0 {
			if err := writeLimit("memory.high", highMB); err != nil {
				log.Printf("browser cgroup memory.high: %v", err)
			}
		}
		if maxMB > 0 {
			if maxMB < highMB {
				maxMB = highMB
			}
			if err := writeLimit("memory.max", maxMB); err != nil {
				log.Printf("browser cgroup memory.max: %v", err)
			}
		}
		procsPath = cgroupDir + "/cgroup.procs"
		log.Printf("browser cgroup %s memory.high=%dMiB memory.max=%dMiB", cgroupDir, highMB, maxMB)
	})
}

func writeLimit(name string, mb int) error {
	return os.WriteFile(cgroupDir+"/"+name, []byte(strconv.Itoa(mb<<20)), 0o644)
}

func envMB(key string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 || n > 65536 {
		return fallback
	}
	return n
}

func moveTree(pid int, seen map[int]bool) {
	if pid <= 1 || seen[pid] {
		return
	}
	seen[pid] = true
	place(pid)
	for _, child := range descendantPIDs(pid) {
		if child <= 1 || seen[child] {
			continue
		}
		seen[child] = true
		place(child)
	}
}

func place(pid int) {
	_ = syscall.Setpriority(syscall.PRIO_PROCESS, pid, 10)
	_ = os.WriteFile(fmt.Sprintf("/proc/%d/oom_score_adj", pid), []byte("300\n"), 0o644)
	if procsPath != "" {
		_ = os.WriteFile(procsPath, []byte(strconv.Itoa(pid)), 0o644)
	}
}

// descendantPIDs walks /proc because /proc/pid/task/pid/children is empty
// on this kernel. The browser process forks renderers after it starts.
func descendantPIDs(root int) []int {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil
	}
	children := map[int][]int{}
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid <= 1 {
			continue
		}
		parent := readPPID(pid)
		if parent > 1 {
			children[parent] = append(children[parent], pid)
		}
	}
	var out []int
	stack := append([]int(nil), children[root]...)
	seen := map[int]bool{}
	for len(stack) > 0 {
		pid := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if seen[pid] {
			continue
		}
		seen[pid] = true
		out = append(out, pid)
		stack = append(stack, children[pid]...)
	}
	return out
}

func readPPID(pid int) int {
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0
	}
	end := bytes.LastIndexByte(raw, ')')
	if end < 0 || end+2 >= len(raw) {
		return 0
	}
	fields := strings.Fields(string(raw[end+2:]))
	if len(fields) < 2 {
		return 0
	}
	n, err := strconv.Atoi(fields[1])
	if err != nil {
		return 0
	}
	return n
}
