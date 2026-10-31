package jobs

import (
	"context"
	"testing"
	"time"
)

func TestManualAutomaticRecheckPreservesIdentityAndIsBounded(t *testing.T) {
	t.Setenv("GEO_MANUAL_AUTO_RECOVERY", "1")
	t.Setenv("GEO_DATA_DIR", t.TempDir())
	q := manualFixture(t)
	s := q.jobs["manual-one"].ManualStates["kimi"]
	s.ErrorCode = "manual_send_unconfirmed"
	s.AutoRecoveryAt = time.Now().Add(-time.Minute).Format(time.RFC3339Nano)
	s.AutoRecoveryKey = s.ErrorCode + "/" + s.SendState + "/" + s.TargetID
	q.jobs["manual-one"].ManualStates["kimi"] = s
	for i := 1; i <= 3; i++ {
		if q.scheduleManualRecoveries(time.Now().Add(time.Hour)) != 1 {
			t.Fatal("eligible paused job not scheduled")
		}
		j, _ := q.Get("manual-one")
		state := j.ManualStates["kimi"]
		if state.AutoRecoveryAttempts != i || state.TargetID != "original-target" || state.Action != "reconcile" {
			t.Fatalf("unexpected recovery identity or budget: %+v", state)
		}
		q.manualPause(j.ID, "kimi", "manual_send_unconfirmed")
	}
	if q.scheduleManualRecoveries(time.Now().Add(time.Hour)) != 0 {
		t.Fatal("unbounded unchanged page retries")
	}
}

func TestManualAutomaticRecheckRespectsOperatorAndRealFailures(t *testing.T) {
	t.Setenv("GEO_MANUAL_AUTO_RECOVERY", "1")
	t.Setenv("GEO_DATA_DIR", t.TempDir())
	for _, code := range []string{"platform_quota", "recovery_page_missing", "no_account", "sms_timeout"} {
		q := manualFixture(t)
		s := q.jobs["manual-one"].ManualStates["kimi"]
		s.ErrorCode, s.AutoRecoveryAt = code, time.Now().Add(-time.Minute).Format(time.RFC3339Nano)
		q.jobs["manual-one"].ManualStates["kimi"] = s
		if q.scheduleManualRecoveries(time.Now()) != 0 {
			t.Fatalf("retried a real restriction: %s", code)
		}
	}
	q := manualFixture(t)
	if _, err := q.ManualAction(context.Background(), "manual-one", "kimi", "claim", 3); err != nil {
		t.Fatal(err)
	}
	if q.scheduleManualRecoveries(time.Now().Add(time.Hour)) != 0 {
		t.Fatal("operator ownership was ignored")
	}
}

func TestQianwenInterruptedCaptureCanRecheckOriginalAnswer(t *testing.T) {
	t.Setenv("GEO_MANUAL_AUTO_RECOVERY", "1")
	t.Setenv("GEO_DATA_DIR", t.TempDir())
	for _, site := range []string{"qianwen", "doubao"} {
		q := manualFixture(t)
		job := q.jobs["manual-one"]
		state := job.ManualStates["kimi"]
		delete(job.ManualStates, "kimi")
		state.Site, state.ErrorCode = site, "site_request_failed"
		state.SendState = "sent"
		state.AutoRecoveryAt = time.Now().Add(-time.Minute).Format(time.RFC3339Nano)
		job.Sites, job.ManualStates[site] = []string{site}, state
		count := q.scheduleManualRecoveries(time.Now())
		if (site == "qianwen") != (count == 1) {
			t.Fatalf("wrong site-specific recheck behavior: %s, %d", site, count)
		}
		if site == "qianwen" {
			updated, _ := q.Get(job.ID)
			if updated.ManualStates[site].Action != "reconcile" || updated.ManualStates[site].TargetID != "original-target" || updated.Prompt != "当前问题" {
				t.Fatal("Qianwen recheck changed the original question or browser target")
			}
		}
	}
}
