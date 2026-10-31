package jobs

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

type deliveryAdmissionFixturePool struct {
	*deliveryExistingPool
	state                    sessions.ExistingRecoveryAvailability
	tryErr                   error
	inspected, tried, legacy atomic.Int32
	onInspect                func()
	onTry                    func(context.Context)
}

func (p *deliveryAdmissionFixturePool) InspectExistingForRecovery(string, string) sessions.ExistingRecoveryAvailability {
	p.inspected.Add(1)
	if p.onInspect != nil {
		p.onInspect()
	}
	return p.state
}

func (p *deliveryAdmissionFixturePool) TryAcquireExistingForRecoveryContext(ctx context.Context, _, _ string) (*sessions.Lease, error) {
	p.tried.Add(1)
	if p.onTry != nil {
		p.onTry(ctx)
	}
	return nil, p.tryErr
}

func (p *deliveryAdmissionFixturePool) AcquireExistingForRecoveryContext(context.Context, string, string) (*sessions.Lease, error) {
	p.legacy.Add(1)
	return nil, fmt.Errorf("fast pool must not use legacy account waiting")
}

func existingAdmissionJobFixture(t *testing.T) (*Queue, *Job, *deliveryAdmissionFixturePool, *atomic.Int32) {
	t.Helper()
	q, calls := webFirstLocalFailureFixture(t, "original-account")
	job := deliveryFixtureJob(t, q, "doubao")
	writeDeliveryJournal(t, q, job, "doubao", "original-account", "sent", nil)
	pool := &deliveryAdmissionFixturePool{deliveryExistingPool: &deliveryExistingPool{q.Sess.(*recoveryPool)}}
	q.Sess = pool
	t.Setenv("GEO_DELIVERY_ADMISSION_SEC", "1")
	return q, job, pool, calls
}

func assertNoRecoveryScraper(t *testing.T, q *Queue) {
	t.Helper()
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "web-started")); !os.IsNotExist(err) {
		t.Fatal("existing-page admission unexpectedly started a scraper")
	}
}

func TestDeliveryExistingMissingPreflightBypassesFullBrowserQueueAndUsesFinalAPI(t *testing.T) {
	q, job, pool, calls := existingAdmissionJobFixture(t)
	pool.state = sessions.ExistingRecoveryMissing
	for i := 0; i < cap(q.siteSem); i++ {
		q.siteSem <- struct{}{}
	}
	started := time.Now()
	q.run(job.ID)
	current, _ := q.Get(job.ID)
	if time.Since(started) >= 500*time.Millisecond || current.Status != "succeeded" || calls.Load() != 1 ||
		pool.inspected.Load() != 1 || pool.tried.Load() != 0 || pool.legacy.Load() != 0 || len(q.siteSem) != cap(q.siteSem) {
		t.Fatalf("lost target waited for global capacity or lost final fallback: status=%s API=%d inspect=%d try=%d legacy=%d",
			current.Status, calls.Load(), pool.inspected.Load(), pool.tried.Load(), pool.legacy.Load())
	}
	row := current.Results[0]
	if row["source"] != "official_api" || row["web_error_code"] != "recovery_target_missing" || len(q.siteGates) != 0 {
		t.Fatal("definite target loss was not classified before browser admission")
	}
	entries := resultAttempts(t, row)
	if len(entries) != 2 || entries[0]["channel"] != "web" || entries[0]["skipped"] != true ||
		entries[1]["channel"] != "api" || entries[1]["ok"] != true {
		t.Fatal("target-loss fallback invented an actual webpage execution")
	}
	assertNoRecoveryScraper(t, q)
}

func TestDeliveryExistingBusyPreflightNeverWaitsForGlobalCapacityOrCallsAPI(t *testing.T) {
	for _, state := range []sessions.ExistingRecoveryAvailability{sessions.ExistingRecoveryBusy, 255} {
		t.Run(fmt.Sprint(state), func(t *testing.T) {
			q, job, pool, calls := existingAdmissionJobFixture(t)
			pool.state = state
			for i := 0; i < cap(q.siteSem); i++ {
				q.siteSem <- struct{}{}
			}
			started := time.Now()
			q.run(job.ID)
			current, _ := q.Get(job.ID)
			if time.Since(started) >= 500*time.Millisecond || current.Status != "queued" || calls.Load() != 0 ||
				pool.inspected.Load() != 1 || pool.tried.Load() != 0 || pool.legacy.Load() != 0 ||
				len(q.siteSem) != cap(q.siteSem) || len(q.siteGates) != 0 {
				t.Fatal("busy or uncertain target consumed admission capacity, waited, or substituted API")
			}
			row := current.Results[0]
			if row["error_code"] != "session_setup_timeout" || row["web_skipped"] != true ||
				row["delivery_resume_required"] != true || row["delivery_pending"] != true || row["account_id"] != nil {
				t.Fatal("busy target did not retain unsent execution and original recovery intent")
			}
			entries := resultAttempts(t, row)
			if len(entries) != 1 || entries[0]["skipped"] != true || entries[0]["skip_reason"] != "session_busy" {
				t.Fatal("account busy was not an explicit skipped stage")
			}
			assertNoRecoveryScraper(t, q)
		})
	}
}

func TestDeliveryExistingPostAdmissionBusyReleasesAllCapacityWithoutAPI(t *testing.T) {
	for _, failure := range []error{sessions.ErrExistingRecoveryBusy, context.DeadlineExceeded, context.Canceled} {
		t.Run(failure.Error(), func(t *testing.T) {
			q, job, pool, calls := existingAdmissionJobFixture(t)
			pool.state, pool.tryErr = sessions.ExistingRecoveryReady, failure
			pool.onTry = func(context.Context) {
				q.mu.Lock()
				owned := len(q.siteSem) == 1 && q.poolActive["doubao"] == 1 && len(q.siteGates["doubao"]) == 1
				q.mu.Unlock()
				if !owned {
					t.Fatal("fast account claim ran before global/site/pool admission")
				}
			}
			q.run(job.ID)
			current, _ := q.Get(job.ID)
			if current.Status != "queued" || calls.Load() != 0 || pool.tried.Load() != 1 || pool.legacy.Load() != 0 ||
				len(q.siteSem) != 0 || q.poolActive["doubao"] != 0 || len(q.siteGates["doubao"]) != 0 {
				t.Fatal("post-admission account race leaked capacity, used legacy waiting, or called API")
			}
			if current.Results[0]["error_code"] != "session_setup_timeout" || current.Results[0]["delivery_pending"] != true {
				t.Fatal("post-admission account race lost original-page retry state")
			}
			entries := resultAttempts(t, current.Results[0])
			if len(entries) != 1 || entries[0]["skipped"] != true {
				t.Fatal("failed nonblocking account claim counted a webpage call")
			}
			assertNoRecoveryScraper(t, q)
		})
	}
}

func TestDeliveryExistingCallerCancellationStartsNeitherAdmissionNorFallback(t *testing.T) {
	for _, when := range []string{"already_cancelled", "expired", "after_preflight", "after_admission"} {
		t.Run(when, func(t *testing.T) {
			q, job, pool, calls := existingAdmissionJobFixture(t)
			pool.state = sessions.ExistingRecoveryReady
			var parent context.Context
			var cancel context.CancelFunc
			if when == "expired" {
				parent, cancel = context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
			} else {
				parent, cancel = context.WithCancel(context.Background())
			}
			defer cancel()
			switch when {
			case "already_cancelled":
				cancel()
			case "after_preflight":
				pool.onInspect = cancel
			case "after_admission":
				pool.onTry = func(context.Context) { cancel() }
				pool.tryErr = context.Canceled
			}
			row := q.resumeDeliverySite(context.WithValue(parent, eventualDeliveryKey{}, true), job, "doubao", "original-account")
			if row["ok"] != false || row["web_skipped"] != true || row["delivery_resume_required"] != true ||
				calls.Load() != 0 || pool.legacy.Load() != 0 || len(q.siteSem) != 0 || q.poolActive["doubao"] != 0 {
				t.Fatal("caller cancellation claimed resources, called API or discarded original-page intent")
			}
			if (when == "already_cancelled" || when == "expired") && (pool.inspected.Load() != 0 || pool.tried.Load() != 0) {
				t.Fatal("an already cancelled caller inspected or claimed a page")
			}
			entries := resultAttempts(t, row)
			if len(entries) != 1 || entries[0]["skipped"] != true || entries[0]["channel"] != "web" {
				t.Fatal("cancelled recovery invented an executed stage")
			}
			assertNoRecoveryScraper(t, q)
		})
	}
}
