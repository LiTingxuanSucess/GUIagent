package jobs

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRecoveryFailureCacheExpiresAndInvalidatesOnObservedProgress(t *testing.T) {
	q := &Queue{}
	now := time.Now()
	probe := map[string]any{"context_valid": true, "ready": false, "target_id": "original", "challenge_visible": true}
	key := recoveryFailureKey("job", "doubao", "account", "original")
	q.rememberRecoveryFailure(key, probe, cursorRecoveryResult{CallID: "previous", Status: "failed", ErrorCode: "no_progress"}, now)
	if _, ok := q.recentRecoveryFailure(key, probe, now.Add(time.Second)); !ok {
		t.Fatal("unchanged failed page started another recovery")
	}
	changed := map[string]any{"context_valid": true, "ready": false, "target_id": "original", "challenge_visible": false, "logged_out": true}
	if _, ok := q.recentRecoveryFailure(key, changed, now.Add(time.Second)); ok {
		t.Fatal("page progress remained blocked")
	}
	q.rememberRecoveryFailure(key, probe, cursorRecoveryResult{CallID: "previous", Status: "failed", ErrorCode: "cli_timeout"}, now)
	if _, ok := q.recentRecoveryFailure(key, probe, now.Add(11*time.Minute)); ok {
		t.Fatal("recovery failure never expired")
	}
	q.rememberRecoveryFailure(key, probe, cursorRecoveryResult{CallID: "not-executed", Status: "skipped", ErrorCode: "no_progress"}, now)
	if _, ok := q.recentRecoveryFailure(key, probe, now); ok {
		t.Fatal("skipped CLI created failure evidence")
	}
}

func TestUnchangedFailedRecoveryDoesNotLaunchAnotherCLIOrResend(t *testing.T) {
	q := webRecoveryFixture(t, "unverified", "first")
	cli := filepath.Join(q.GeoRoot, "fixture-cursor")
	if err := os.WriteFile(cli, []byte("#!/bin/sh\nprintf 'called\\n' >> cli-calls\nprintf 'recovery-stopped: no_progress\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	lease := q.Sess.(*recoveryPool).leases[0]
	ctx := withWebRecovery(context.Background(), webRecoveryExecution{StatePath: q.webRecoveryPath("same-job", "doubao", lease.AccountID), AccountID: lease.AccountID})
	if _, err := q.scrapeForContext(ctx, "http://127.0.0.1:12345", "doubao", "question", "same-job", lease.Phone, 5*time.Second); err == nil {
		t.Fatal("fixture did not produce a failed webpage")
	}
	for i := 0; i < 2; i++ {
		_, err, meta := q.recoverWebAttempt(ctx, lease, "http://127.0.0.1:12345", "doubao", "question", "same-job", 1, false, recoveryFailure("platform_risk_control"))
		if err == nil {
			t.Fatal("unrepaired page reported success")
		}
		if i == 1 && (meta["error_code"] != "recovery_queue_busy" || evidenceCount(meta["retry_after_sec"]) == 0) {
			t.Fatalf("unchanged page was not deferred: %v", meta)
		}
	}
	calls, _ := os.ReadFile(filepath.Join(q.GeoRoot, "cli-calls"))
	sends, _ := os.ReadFile(filepath.Join(q.GeoRoot, "sent-once"))
	if string(calls) != "called\n" || string(sends) != "first\n" {
		t.Fatalf("repeated CLI or submission: calls=%q sends=%q", calls, sends)
	}
	if err := os.WriteFile(filepath.Join(q.GeoRoot, "repaired"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	one, err, _ := q.recoverWebAttempt(ctx, lease, "http://127.0.0.1:12345", "doubao", "question", "same-job", 1, false, recoveryFailure("platform_risk_control"))
	if err != nil || one["ok"] != true {
		t.Fatalf("externally repaired page could not resume during cooldown: %v %v", one, err)
	}
}
