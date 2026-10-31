package jobs

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type cursorLogWriteError struct{}

func (cursorLogWriteError) Write([]byte) (int, error) { return 0, errors.New("fixture disk full") }

func TestBoundedCursorLogDropsOverflowWithoutBreakingOutput(t *testing.T) {
	var saved bytes.Buffer
	bounded := &boundedCursorLog{writer: &saved, remaining: 5}
	for _, chunk := range []string{"abc", "defgh", "ignored"} {
		if n, err := bounded.Write([]byte(chunk)); n != len(chunk) || err != nil {
			t.Fatal("log truncation interrupted CLI output")
		}
	}
	if saved.String() != "abcde" {
		t.Fatalf("per-call log exceeded its cap: %q", saved.String())
	}
	failed := &boundedCursorLog{writer: cursorLogWriteError{}, remaining: 5}
	if n, err := failed.Write([]byte("output")); n != 6 || err != nil || failed.remaining != 0 {
		t.Fatal("a log disk error propagated to the CLI output pipe")
	}
}

func TestCursorRecoveryLogCapPreservesFinalMarkerAndPrunesOnlyExpiredLogs(t *testing.T) {
	q, req := cursorRecoveryFixture(t, "head -c 2097152 /dev/zero\nprintf '\\nrecovery-candidate\\n'\n")
	t.Setenv("GEO_CURSOR_LOG_MAX_MB", "1")
	t.Setenv("GEO_CURSOR_LOG_KEEP_DAYS", "2")
	dataDir := filepath.Dir(cursorCallDirectory(q.cursorWorkspace()))
	logDir := filepath.Join(dataDir, "cursor-logs")
	if err := os.MkdirAll(logDir, 0700); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-3 * 24 * time.Hour)
	expired := filepath.Join(logDir, "expired.log")
	preserved := []string{filepath.Join(logDir, "recent.log"), filepath.Join(logDir, "old.json"), filepath.Join(q.GeoRoot, "legacy-captcha-agent.log")}
	for _, path := range append([]string{expired}, preserved...) {
		if err := os.WriteFile(path, []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
		if path != preserved[0] {
			if err := os.Chtimes(path, old, old); err != nil {
				t.Fatal(err)
			}
		}
	}
	directory := filepath.Join(logDir, "directory.log")
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(directory, old, old); err != nil {
		t.Fatal(err)
	}
	symlink := filepath.Join(logDir, "symlink.log")
	if err := os.Symlink(preserved[2], symlink); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(cursorCallDirectory(q.cursorWorkspace()), 0700); err != nil {
		t.Fatal(err)
	}
	stats := filepath.Join(cursorCallDirectory(q.cursorWorkspace()), "old-stats.json")
	if err := os.WriteFile(stats, []byte(`{"fixture":"keep"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(stats, old, old); err != nil {
		t.Fatal(err)
	}
	result := q.runCursorRecovery(context.Background(), req)
	if result.Status != "candidate" {
		t.Fatalf("discarded log overflow lost the bounded stdout task marker: %v", result)
	}
	info, err := os.Stat(filepath.Join(logDir, result.CallID+".log"))
	if err != nil || info.Size() != 1<<20 {
		t.Fatalf("configured log cap was not enforced: info=%v err=%v", info, err)
	}
	if _, err := os.Stat(expired); !os.IsNotExist(err) {
		t.Fatal("expired per-call raw log was not pruned")
	}
	for _, path := range append(preserved, directory, symlink, stats) {
		if _, err := os.Lstat(path); err != nil {
			t.Fatalf("log retention removed a recent log, nonregular file, legacy log or stats record: %v", err)
		}
	}
}
