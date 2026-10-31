package jobs

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestCursorCapacityWaitHonorsCancellationAndDoesNotLeak(t *testing.T) {
	t.Setenv("GEO_CURSOR_CONCURRENCY", "1")
	if !claimCursorCapacity() {
		t.Fatal("could not occupy CLI capacity")
	}
	defer releaseCursorCapacity()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	if err := waitForCursorCapacity(ctx); err != context.DeadlineExceeded {
		t.Fatalf("occupied CLI capacity ignored deadline: %v", err)
	}
	if claimCursorCapacity() {
		releaseCursorCapacity()
		t.Fatal("waiting overcommitted CLI capacity")
	}
	canceled, stop := context.WithCancel(context.Background())
	stop()
	if err := waitForCursorCapacity(canceled); err != context.Canceled {
		t.Fatalf("canceled caller acquired CLI capacity: %v", err)
	}
}

func TestManualPageAssistStaysOffWhenCLIDisabled(t *testing.T) {
	t.Setenv("GEO_CURSOR_WEB_RECOVERY", "")
	t.Setenv("GEO_CURSOR_CAPTCHA_AGENT", "")
	released := false
	_, err := (&Queue{}).StartManualPageAssist("acct", "kimi", "http://127.0.0.1:9", "TARGET", func() {
		released = true
	})
	if err != ErrManualCLIDisabled || released {
		t.Fatalf("err=%v released=%v", err, released)
	}
}

func TestManualAssistRequiresFreshPageEvidence(t *testing.T) {
	for _, tc := range []struct {
		name         string
		probe        map[string]any
		status, code string
		verified     bool
	}{
		{"unreadable", map[string]any{"ok": false}, "failed", "cli_verification_failed", false},
		{"captcha despite candidate", map[string]any{"ok": true, "challenge_visible": true, "editor_ready": true, "account_marker": true}, "failed", "cli_verification_failed", false},
		{"SMS handoff", map[string]any{"ok": true, "challenge_visible": false, "waiting_sms": true}, "waiting_sms", "sms_required", true},
		{"guest editor", map[string]any{"ok": true, "challenge_visible": false, "editor_ready": true}, "failed", "cli_verification_failed", false},
		{"authenticated editor", map[string]any{"ok": true, "challenge_visible": false, "editor_ready": true, "account_marker": true}, "success", "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			status, code, verified := manualCursorVerdict(tc.probe)
			if status != tc.status || code != tc.code || verified != tc.verified {
				t.Fatalf("got %s %s %v", status, code, verified)
			}
		})
	}
}

func TestCursorWatchdogExpiresAfterInitialOutputStalls(t *testing.T) {
	t.Setenv("GEO_CURSOR_STARTUP_SEC", "")
	t.Setenv("GEO_CURSOR_SILENCE_SEC", "45")
	now := time.Now()
	out := &cursorOutput{}
	out.Write([]byte("{\"type\":\"system\"}\n"))
	out.lastWrite = now.Add(-46 * time.Second)
	if out.silenceRemaining(now.Add(-time.Minute), now) > 0 {
		t.Fatal("initial output disabled silence watchdog")
	}
	out.Write([]byte("progress\n"))
	if out.silenceRemaining(now.Add(-time.Minute), time.Now()) <= 0 {
		t.Fatal("new output did not renew watchdog")
	}
}

func TestCursorCapacityIsSharedAcrossAccounts(t *testing.T) {
	t.Setenv("GEO_CURSOR_CONCURRENCY", "1")
	if !claimCursorCapacity() {
		t.Fatal("idle capacity rejected")
	}
	defer releaseCursorCapacity()
	if claimCursorCapacity() {
		releaseCursorCapacity()
		t.Fatal("different account could overcommit the CLI host")
	}
}

func TestManualToolRulesDoNotRequireMissingJournal(t *testing.T) {
	rules := cursorToolRules("/workspace", "kimi", "http://localhost:1", "target", "")
	if !strings.Contains(rules, "不要调用 ready") || strings.Contains(rules, "必须先跑 ready") {
		t.Fatal("manual assist requires a journal it never receives")
	}
}

func TestManualPageAssistStatusRejectsOtherRecords(t *testing.T) {
	if _, err := (&Queue{GeoRoot: t.TempDir()}).ManualPageAssistStatus("../secret", "acct", "kimi"); err == nil {
		t.Fatal("path escape was accepted")
	}
}
