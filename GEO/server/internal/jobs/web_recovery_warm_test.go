package jobs

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestFailedRecoveryProbeDoesNotCreateOrClearLongWarmWindow(t *testing.T) {
	for _, mode := range []string{"cold", "operator_warm", "semantic_challenge"} {
		t.Run(mode, func(t *testing.T) {
			q := attemptFixtureQueue(t, "import json\nprint(json.dumps({'recovery_action':'probe','context_valid':False,'error_code':'recovery_page_missing'}))\n", "account_fixture")
			lease := q.Sess.(*recoveryPool).leases[0]
			switch mode {
			case "operator_warm":
				lease.Manager.KeepWarmFor(10 * time.Minute)
			case "semantic_challenge":
				lease.MarkNeedsCaptcha("豆包触发语义安全验证，请人工完成验证后重试")
			}
			defer lease.Release()
			wasWarm := lease.Manager.ShouldKeepWarm()
			ctx := withWebRecovery(context.Background(), webRecoveryExecution{StatePath: filepath.Join(q.GeoRoot, "private-journal.json"), AccountID: lease.AccountID})
			original := recoveryFailure("platform_auth_unavailable")
			_, err, metadata := q.recoverWebAttempt(ctx, lease, "http://127.0.0.1:9", "doubao", "question", "failed-probe", 1, true, original)
			if err != original || metadata["error_code"] != "recovery_page_missing" || metadata["attempts"] != 0 {
				t.Fatalf("failed probe was not returned without CLI: err=%v metadata=%v", err, metadata)
			}
			if got := lease.Manager.ShouldKeepWarm(); got != wasWarm || got != (mode != "cold") {
				t.Fatalf("failed probe changed profile warmth: mode=%s before=%t after=%t", mode, wasWarm, got)
			}
			if !lease.Manager.Busy() {
				t.Fatal("failed recovery released the caller's protective account lease")
			}
			// The recovery guard ends on return, while the original lease is
			// still held and can safely be handed to another recovery decision.
			if guard, err := lease.BeginWebRecovery(); err != nil {
				t.Fatalf("failed probe leaked its exclusive recovery guard: %v", err)
			} else {
				guard()
			}
		})
	}
}

func TestRecoveryProbeKeepsLeaseAndPageExclusiveWithoutLongWarmWindow(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,pathlib,time
pathlib.Path('probe-started').touch()
while not pathlib.Path('release-probe').exists():time.sleep(.005)
print(json.dumps({'recovery_action':'probe','context_valid':False,'error_code':'recovery_page_missing'}))
`, "account_fixture")
	lease := q.Sess.(*recoveryPool).leases[0]
	defer lease.Release()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	ctx = withWebRecovery(ctx, webRecoveryExecution{StatePath: filepath.Join(q.GeoRoot, "private-journal.json"), AccountID: lease.AccountID})
	done := make(chan struct{})
	go func() {
		q.recoverWebAttempt(ctx, lease, "http://127.0.0.1:9", "doubao", "question", "blocked-probe", 1, true, recoveryFailure("platform_auth_unavailable"))
		close(done)
	}()
	until := time.Now().Add(time.Second)
	started := false
	for time.Now().Before(until) {
		if _, err := os.Stat(filepath.Join(q.GeoRoot, "probe-started")); err == nil {
			started = true
			break
		}
		time.Sleep(time.Millisecond)
	}
	if !started {
		cancel()
		<-done
		t.Fatal("mock probe did not start")
	}
	if !lease.Manager.Busy() || lease.Manager.ShouldKeepWarm() {
		t.Error("in-flight recovery requires a busy lease, without starting an idle warm timer")
	}
	if lease.Manager.TryAcquireSite("") {
		lease.Manager.ReleaseSite("")
		t.Error("another action acquired the site while recovery held it")
	}
	if guard, err := lease.BeginWebRecovery(); err == nil {
		guard()
		t.Error("another recovery entered the protected page during probe")
	}
	if err := os.WriteFile(filepath.Join(q.GeoRoot, "release-probe"), nil, 0600); err != nil {
		cancel()
		<-done
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("mock recovery did not return after releasing probe")
	}
	if !lease.Manager.Busy() || lease.Manager.ShouldKeepWarm() {
		t.Fatal("returning from probe extended warmth or released the caller's lease")
	}
}
