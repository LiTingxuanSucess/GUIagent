package jobs

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestCursorBillingBlocksAllPlatformsAndCannotBecomeSuccessful(t *testing.T) {
	q, req := cursorRecoveryFixture(t, "printf 'called\\n' >> provider-calls\nprintf 'ActionRequiredError: You have an unpaid invoice\\n'\nexit 1\n")
	first := q.runCursorRecovery(context.Background(), req)
	if first.Status != "failed" || first.ErrorCode != "cli_billing_required" {
		t.Fatalf("billing misclassified: %+v", first)
	}
	q.markCursorRecovery(first.CallID, true, true, "completed")
	call := readRecoveryCall(t, q, first.CallID)
	if call.Status != "failed" || call.ErrorCode != "cli_billing_required" || call.ProcessStatus != "failed" || call.RecoveryOrigin != "external" || call.Assisted == nil || *call.Assisted || call.OK == nil || *call.OK {
		t.Fatalf("external recovery hid CLI failure: %+v", call)
	}
	req.Site = "deepseek"
	req.JobID = "another-job"
	req.AccountID = "another-account"
	second := q.runCursorRecovery(context.Background(), req)
	if second.Status != "skipped" || second.ErrorCode != "cli_billing_required" {
		t.Fatalf("outage not shared: %+v", second)
	}
	raw, _ := os.ReadFile(filepath.Join(q.GeoRoot, "provider-calls"))
	if string(raw) != "called\n" {
		t.Fatalf("provider retried during outage: %q", raw)
	}
	if _, err := q.prepareManualPageAssist("manual", "kimi", "cdp", "target"); err == nil || err.Error() != "cli_billing_required" {
		t.Fatalf("manual path bypassed breaker: %v", err)
	}
	if (&Queue{GeoRoot: q.GeoRoot}).CursorHealth().State != "blocked" {
		t.Fatal("restart lost provider outage")
	}
}

func TestCursorProviderRecheckConfirmsRealResponseAfterCooldown(t *testing.T) {
	q, _ := cursorRecoveryFixture(t, "printf '%s\\n' '{\"type\":\"result\",\"result\":\"GEO_PROVIDER_OK\",\"is_error\":false}'\n")
	p, _ := acquireCursorProvider(q.cursorWorkspace(), time.Now().Add(-16*time.Minute))
	p.complete("cli_billing_required", false, time.Now().Add(-16*time.Minute))
	health := q.RecheckCursorProvider(context.Background())
	if health.State != "ready" || health.ErrorCode != "" {
		t.Fatalf("real model response did not clear old outage: %+v", health)
	}
}

func TestCursorProviderRecheckDoesNotMistakeAuthenticationForModelSuccess(t *testing.T) {
	q, _ := cursorRecoveryFixture(t, "printf '%s\\n' 'Logged in'\n")
	p, _ := acquireCursorProvider(q.cursorWorkspace(), time.Now().Add(-16*time.Minute))
	p.complete("cli_billing_required", false, time.Now().Add(-16*time.Minute))
	if health := q.RecheckCursorProvider(context.Background()); health.State == "ready" {
		t.Fatalf("authentication output became model availability: %+v", health)
	}
}

func TestCursorProviderHalfOpenIsSingleFlightAndOlderSuccessCannotClearOutage(t *testing.T) {
	t.Setenv("GEO_DATA_DIR", t.TempDir())
	t.Setenv("GEO_SERVER_ROOT", "")
	now := time.Now()
	p, _ := acquireCursorProvider("workspace", now)
	older, _ := acquireCursorProvider("workspace", now)
	p.complete("cli_billing_required", false, now)
	if code := older.canStart(now.Add(time.Second)); code != "cli_billing_required" {
		t.Fatal("waiting invocation launched after a sibling opened the breaker")
	}
	older.complete("", true, now.Add(time.Second))
	if permit, code := acquireCursorProvider("workspace", now.Add(time.Minute)); permit != nil || code != "cli_billing_required" {
		t.Fatal("stale success reopened provider")
	}
	var granted atomic.Int32
	var chosen *cursorProviderPermit
	var mu sync.Mutex
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			permit, _ := acquireCursorProvider("workspace", now.Add(16*time.Minute))
			if permit != nil {
				granted.Add(1)
				mu.Lock()
				chosen = permit
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if granted.Load() != 1 {
		t.Fatalf("half-open probes=%d", granted.Load())
	}
	chosen.complete("", true, now.Add(16*time.Minute))
	if permit, code := acquireCursorProvider("workspace", now.Add(17*time.Minute)); permit == nil || code != "" {
		t.Fatalf("provider never recovered: %s", code)
	}
}

func TestProviderErrorsCannotBeInjectedByPageTextAndSplitErrorsSurvive(t *testing.T) {
	out := &cursorOutput{}
	out.Write([]byte("{\"type\":\"user\",\"message\":\"unpaid invoice\"}\n{\"type\":\"assistant\",\"text\":\"unpaid invoice\"}\nrecovery-candidate\n"))
	if status, _, _ := out.recoveryOutcome(nil); status != "candidate" {
		t.Fatal("page text forged provider outage")
	}
	for _, tc := range []struct{ text, code string }{{"unpaid invoice", "cli_billing_required"}, {"Unauthorized", "cli_auth_failed"}, {"rate limit exceeded", "cli_rate_limited"}, {"usage limit exceeded", "cli_quota_exhausted"}, {"fetch failed", "cli_transport_failed"}} {
		out = &cursorOutput{}
		out.Write([]byte(tc.text[:3]))
		out.Write([]byte(tc.text[3:]))
		if _, code, _ := out.recoveryOutcome(errors.New("exit 1")); code != tc.code {
			t.Fatalf("got %s want %s", code, tc.code)
		}
	}
}

func TestRecoveryRetryBudgetSurvivesJobsTargetsAndRestart(t *testing.T) {
	t.Setenv("GEO_DATA_DIR", t.TempDir())
	t.Setenv("GEO_SERVER_ROOT", "")
	q := &Queue{GeoRoot: t.TempDir()}
	now := time.Now()
	probe := map[string]any{"context_valid": true, "challenge_visible": true, "target_id": "old", "text": "retry in 60 seconds"}
	key := recoveryFailureKey("job1", "qianwen", "account", "old")
	q.rememberRecoveryFailure(key, probe, cursorRecoveryResult{CallID: "first", Status: "failed", ErrorCode: "no_progress"}, now)
	probe["target_id"], probe["text"] = "new", "retry in 59 seconds"
	key2 := recoveryFailureKey("job2", "qianwen", "account", "new")
	q = &Queue{GeoRoot: q.GeoRoot}
	if _, ok := q.recentRecoveryFailure(key2, probe, now.Add(time.Second)); !ok {
		t.Fatal("job, target, countdown, or restart bypassed cooldown")
	}
	if _, ok := q.recentRecoveryFailure(key2, probe, now.Add(121*time.Second)); ok {
		t.Fatal("first attempt never cooled down")
	}
	q.rememberRecoveryFailure(key2, probe, cursorRecoveryResult{CallID: "second", Status: "failed", ErrorCode: "no_progress"}, now.Add(121*time.Second))
	if _, ok := q.recentRecoveryFailure(key2, probe, now.Add(5*time.Minute)); !ok {
		t.Fatal("two-attempt budget allowed repeated work")
	}
	probe["challenge_visible"] = false
	probe["waiting_sms"] = true
	if _, ok := q.recentRecoveryFailure(key2, probe, now.Add(5*time.Minute)); ok {
		t.Fatal("actual state change could not proceed")
	}
}

func TestCursorToolBudgetWaitsForInFlightActions(t *testing.T) {
	t.Setenv("GEO_CURSOR_MAX_TOOLS", "4")
	out := &cursorOutput{}
	for i := 0; i < 4; i++ {
		out.Write([]byte("{\"type\":\"tool_call\",\"subtype\":\"started\"}\n"))
	}
	if out.toolBudgetExceeded() {
		t.Fatal("stopped while an action was in flight")
	}
	for i := 0; i < 4; i++ {
		out.Write([]byte("{\"type\":\"tool_call\",\"subtype\":\"completed\"}\n"))
	}
	if !out.toolBudgetExceeded() {
		t.Fatal("unbounded tool loop")
	}
}

func TestCursorStartupBudgetDoesNotChangeActiveToolSilenceBudget(t *testing.T) {
	t.Setenv("GEO_CURSOR_STARTUP_SEC", "75")
	t.Setenv("GEO_CURSOR_SILENCE_SEC", "45")
	now := time.Now()
	out := &cursorOutput{lastWrite: now.Add(-46 * time.Second)}
	if remain := out.silenceRemaining(now.Add(-60*time.Second), now); remain != 15*time.Second {
		t.Fatalf("startup remaining=%v", remain)
	}
	out.firstTool = now.Add(-50 * time.Second)
	if out.silenceRemaining(now.Add(-time.Minute), now) > 0 {
		t.Fatal("startup grace extended an already active tool's silence budget")
	}
}
