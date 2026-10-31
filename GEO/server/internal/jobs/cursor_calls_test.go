package jobs

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCursorTaskOutcomeRequiresConfirmedMarker(t *testing.T) {
	for _, tc := range []struct {
		name, output, status, code string
		err                        error
		want                       *bool
	}{
		{"confirmed", "working\ncaptcha-ok\n", "success", "", nil, cursorBool(true)},
		{"stopped", "captcha-stopped: 页面已关闭\n", "failed", "cli_task_stopped", nil, cursorBool(false)},
		{"exit_zero_only", "completed without marker\n", "unknown", "cli_result_unconfirmed", nil, nil},
		{"quoted_marker", "instruction: output captcha-ok\n", "unknown", "cli_result_unconfirmed", nil, nil},
		{"nonzero_overrides_marker", "captcha-ok\n", "failed", "cli_process_failed", errors.New("exit 1"), cursorBool(false)},
		{"last_marker_wins", "captcha-ok\ncaptcha-stopped: 后续验证失败\n", "failed", "cli_task_stopped", nil, cursorBool(false)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out := &cursorOutput{}
			_, _ = out.Write([]byte(tc.output))
			status, code, ok := out.outcome(tc.err)
			if status != tc.status || code != tc.code || (ok == nil) != (tc.want == nil) || (ok != nil && *ok != *tc.want) {
				t.Fatalf("outcome %s %s %v", status, code, ok)
			}
		})
	}
	out := &cursorOutput{}
	_, _ = out.Write([]byte(strings.Repeat("x", 1<<20) + "\ncaptcha-ok\n"))
	if len(out.tail) > 64<<10 {
		t.Fatal("unbounded CLI output")
	}
	status, _, _ := out.outcome(nil)
	if status != "success" {
		t.Fatal("bounded tail lost task marker")
	}
}

func cursorBool(value bool) *bool { return &value }

func TestCursorCallDataDirectoryUsesServerRoot(t *testing.T) {
	serverRoot, workspace := t.TempDir(), t.TempDir()
	t.Setenv("GEO_DATA_DIR", "")
	t.Setenv("GEO_SERVER_ROOT", serverRoot)
	call := newCursorCall("acct_fixture", "doubao", "fixture")
	if err := call.save(workspace); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(serverRoot, "data", "cursor-calls", call.ID+".json")); err != nil {
		t.Fatal("CLI workspace changed the server data directory")
	}
	if _, err := os.Stat(filepath.Join(workspace, "server", "data")); !os.IsNotExist(err) {
		t.Fatal("event was written under CLI workspace")
	}
}

func TestCursorInvocationPersistsOutcomeWithoutRawOutput(t *testing.T) {
	for _, tc := range []struct{ name, script, status string }{
		{"success", "#!/bin/sh\nprintf 'private CLI output\\ncaptcha-ok\\n'\n", "success"},
		{"unknown", "#!/bin/sh\nprintf 'private CLI output\\n'\n", "unknown"},
		{"failure", "#!/bin/sh\nexit 2\n", "failed"},
		{"unavailable", "", "skipped"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			data := filepath.Join(root, "private-data")
			if err := os.MkdirAll(filepath.Join(root, "server"), 0700); err != nil {
				t.Fatal(err)
			}
			bin := filepath.Join(root, "fixture-agent")
			if tc.script != "" {
				if err := os.WriteFile(bin, []byte(tc.script), 0700); err != nil {
					t.Fatal(err)
				}
			}
			t.Setenv("GEO_CURSOR_AGENT", bin)
			t.Setenv("GEO_CURSOR_WORKSPACE", root)
			t.Setenv("GEO_DATA_DIR", data)
			runCursorCaptchaAgent("acct_fixture", "13800138000", "doubao", "fixture-job")
			files, _ := filepath.Glob(filepath.Join(data, "cursor-calls", "*.json"))
			if len(files) != 1 {
				t.Fatalf("persisted %d calls", len(files))
			}
			raw, err := os.ReadFile(files[0])
			if err != nil {
				t.Fatal(err)
			}
			var call cursorCall
			if json.Unmarshal(raw, &call) != nil || call.Status != tc.status || call.FinishedAt == nil || call.TaskType != "captcha_recovery" || call.JobID != "fixture-job" {
				t.Fatalf("invalid invocation record: %s", raw)
			}
			if strings.Contains(string(raw), "13800138000") || strings.Contains(string(raw), "private CLI output") {
				t.Fatal("raw CLI output or phone leaked into metrics")
			}
			info, _ := os.Stat(files[0])
			if info.Mode().Perm() != 0600 {
				t.Fatal("metrics record is not private")
			}
		})
	}
}
