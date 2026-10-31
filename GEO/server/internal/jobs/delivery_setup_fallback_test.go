package jobs

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"
)

func setupWaitFixture(count int, started time.Time) map[string]any {
	attempts := make([]map[string]any, 0, count)
	for i := 0; i < count; i++ {
		attempts = append(attempts, map[string]any{"channel": "web", "skipped": true, "error_code": "session_setup_timeout", "skip_reason": "session_setup", "started_at": started.Format(time.RFC3339Nano)})
	}
	return map[string]any{"ok": false, "site_id": "doubao", "error_code": "session_setup_timeout", "web_skipped": true, "web_skip_reason": "session_setup", "attempts": attempts}
}

func TestDeliverySetupRepeatedWaitCompletesOriginalJobAfterReload(t *testing.T) {
	q, calls := webFirstLocalFailureFixture(t, "account")
	job := deliveryFixtureJob(t, q, "doubao")
	writeDeliveryJournal(t, q, job, "doubao", "account", "before_send", nil)
	job.Results = []map[string]any{setupWaitFixture(3, time.Now().Add(-10*time.Minute))}
	q.save(job)
	q.jobs = make(map[string]*Job)
	q.load()
	q.run(job.ID)
	current, _ := q.Get(job.ID)
	if current.Status != "succeeded" || current.RequestID != job.RequestID || calls.Load() != 1 {
		t.Fatalf("original persisted job not completed once: status=%s request=%s API=%d", current.Status, current.RequestID, calls.Load())
	}
	row := current.Results[0]
	if row["source"] != "official_api" || row["fallback_reason"] != "web_setup_wait_exhausted" || len(cloneAttemptList(row["attempts"])) != 4 {
		t.Fatalf("lost or duplicated original evidence: %#v", row)
	}
	if _, err := os.Stat(q.GeoRoot + "/web-started"); !os.IsNotExist(err) {
		t.Fatalf("exhausted wait started another browser: %v", err)
	}
}

func TestDeliverySetupWaitBudgetAndSendSafety(t *testing.T) {
	for _, mode := range []string{"first_wait", "second_wait", "elapsed", "sent", "sending", "corrupt", "missing", "disabled", "cancelled", "admission"} {
		t.Run(mode, func(t *testing.T) {
			q, calls := webFirstLocalFailureFixture(t, "account")
			job := deliveryFixtureJob(t, q, "doubao")
			state := "before_send"
			if mode == "sent" || mode == "sending" {
				state = mode
			}
			if mode != "missing" {
				writeDeliveryJournal(t, q, job, "doubao", "account", state, nil)
			}
			if mode == "corrupt" {
				if err := os.WriteFile(q.webRecoveryPath(job.ID, "doubao", "account"), []byte("broken"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			if mode == "disabled" {
				t.Setenv("GEO_DOUBAO_API_FALLBACK", "0")
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if mode == "cancelled" {
				cancel()
			}
			previous := setupWaitFixture(1, time.Now())
			if mode == "first_wait" {
				previous = nil
			}
			if mode == "elapsed" {
				previous = setupWaitFixture(1, time.Now().Add(-4*time.Minute))
			}
			if mode == "admission" {
				previous["error_code"] = "browser_admission_timeout"
				previous["web_skip_reason"] = "browser_admission"
				previous["attempts"] = nil
			}
			var result map[string]any
			if mode == "elapsed" {
				result = q.deliverySetupFallback(ctx, job, "doubao", previous)
			} else {
				current := setupWaitFixture(1, time.Now())
				if mode == "admission" {
					current = previous
				}
				result = q.applyDeliverySetupWaitFallback(ctx, job, "doubao", previous, current)
			}
			want := int32(0)
			if mode == "second_wait" || mode == "elapsed" {
				want = 1
			}
			if calls.Load() != want || want == 1 && (result == nil || !validDelivery(result)) {
				t.Fatalf("unsafe or missing fallback: mode=%s calls=%d want=%d result=%v", mode, calls.Load(), want, result)
			}
		})
	}
}

func TestDeliverySetupNeverSentUsesCurrentDispatchRecord(t *testing.T) {
	q, calls := webFirstLocalFailureFixture(t, "account")
	job := deliveryFixtureJob(t, q, "doubao")
	job.DispatchTracked = true
	stale, _ := q.Get(job.ID)
	q.patch(job.ID, func(current *Job) { current.WebDispatched = []string{"doubao"} })
	if q.deliverySetupFallback(context.Background(), stale, "doubao", setupWaitFixture(3, time.Now())) != nil || calls.Load() != 0 {
		t.Fatal("stale dispatch snapshot allowed duplicate submission")
	}
}

func TestDeliverySetupBudgetSurvivesAttemptTruncationAndOtherWait(t *testing.T) {
	previous := map[string]any{"web_setup_wait_count": 3, "web_setup_wait_started_at": time.Now().Add(-time.Hour).Format(time.RFC3339Nano)}
	merged := mergeDeliveryResult(previous, map[string]any{"ok": false, "error_code": "browser_admission_timeout"})
	count, first := deliverySetupWaitEvidence(merged)
	if count != 3 || first.IsZero() || !deliverySetupBudgetExhausted(count, first, time.Now()) {
		t.Fatalf("budget lost: %v", merged)
	}
}

func TestDeliverySetupTwoPreparationRoundsThenAPIFallback(t *testing.T) {
	q, calls := webFirstLocalFailureFixture(t, "first", "second")
	pool := &deliveryPartialPreparationPool{recoveryPool: q.Sess.(*recoveryPool), mode: "busy"}
	q.Sess = pool
	job := deliveryFixtureJob(t, q, "doubao")
	job.DispatchTracked = true
	q.run(job.ID)
	first, _ := q.Get(job.ID)
	if first.Status != "queued" || calls.Load() != 0 || pool.calls != 1 {
		t.Fatalf("first wait did not retain web opportunity: status=%s calls=%d pool=%d", first.Status, calls.Load(), pool.calls)
	}
	expireDeliveryRetry(q, job.ID)
	q.run(job.ID)
	last, _ := q.Get(job.ID)
	if last.Status != "succeeded" || calls.Load() != 1 || pool.calls != 2 || len(cloneAttemptList(last.Results[0]["attempts"])) != 3 {
		t.Fatalf("repeated preparation loop not bounded: status=%s calls=%d pool=%d result=%v", last.Status, calls.Load(), pool.calls, last.Results)
	}
}

func TestDeliverySetupAPIRetryDoesNotRestartPreparation(t *testing.T) {
	q, _ := webFirstLocalFailureFixture(t, "account")
	var attempt atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if attempt.Add(1) == 1 {
			w.WriteHeader(503)
			return
		}
		w.Write([]byte(`{"choices":[{"message":{"content":"recovered API answer"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	job := deliveryFixtureJob(t, q, "doubao")
	writeDeliveryJournal(t, q, job, "doubao", "account", "before_send", nil)
	job.Results = []map[string]any{setupWaitFixture(2, time.Now())}
	q.run(job.ID)
	first, _ := q.Get(job.ID)
	if first.Status != "queued" || attempt.Load() != 1 || first.Results[0]["api_fallback_failed"] != true {
		t.Fatalf("API failure lost: %v", first)
	}
	expireDeliveryRetry(q, job.ID)
	q.run(job.ID)
	last, _ := q.Get(job.ID)
	if last.Status != "succeeded" || attempt.Load() != 2 || len(cloneAttemptList(last.Results[0]["attempts"])) != 4 {
		t.Fatalf("API retry lost evidence: %v calls=%d", last, attempt.Load())
	}
	if _, err := os.Stat(q.GeoRoot + "/web-started"); !os.IsNotExist(err) {
		t.Fatal("API retry restarted browser preparation")
	}
}
