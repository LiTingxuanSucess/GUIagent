package procguard

import (
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestReadPPIDMatchesParent(t *testing.T) {
	if got := readPPID(os.Getpid()); got != os.Getppid() {
		t.Fatalf("ppid=%d want %d", got, os.Getppid())
	}
}

func TestDescendantsIncludeChild(t *testing.T) {
	cmd := exec.Command("sleep", "30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill() }()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		for _, pid := range descendantPIDs(os.Getpid()) {
			if pid == cmd.Process.Pid {
				return
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("child %d not found", cmd.Process.Pid)
}
