package sessions

import (
	"context"
	"errors"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func preparationFixture(t *testing.T) *Pool {
	t.Helper()
	t.Setenv("GEO_BROWSER_MEMORY_HIGH_MB", "0")
	t.Setenv("GEO_BROWSER_MEMORY_MAX_MB", "0")
	root := t.TempDir()
	p := NewPool("/nonexistent/geo-test-chrome", root, "", filepath.Join(root, "accounts.json"))
	for _, id := range []string{"first", "second"} {
		a := newAccount(id, "13800138000")
		st := a.Sites["doubao"]
		st.Status = "logged_in"
		a.Sites["doubao"] = st
		p.accounts[id], p.order = a, append(p.order, id)
	}
	return p
}

func TestPreparationAllActualBrowserStartsFailed(t *testing.T) {
	p := preparationFixture(t)
	_, err := p.AcquireContext(context.Background(), "doubao")
	var evidence *PreparationError
	if !errors.As(err, &evidence) || !evidence.AllCandidatesFailed() {
		t.Fatalf("completed start failures did not return exhaustion evidence: %v", err)
	}
	if !reflect.DeepEqual(evidence.CandidateIDs, []string{"first", "second"}) || !reflect.DeepEqual(evidence.FailedIDs, evidence.CandidateIDs) {
		t.Fatalf("wrong attempted candidates: %+v", evidence)
	}
	for _, id := range evidence.FailedIDs {
		if evidence.FailureCodes[id] != "browser_start_failed" {
			t.Fatalf("raw browser error escaped stable evidence: %+v", evidence.FailureCodes)
		}
	}
}

func TestPreparationBusyAndCanceledAreNotExhausted(t *testing.T) {
	p := preparationFixture(t)
	for _, id := range p.order {
		lk := p.accountLockLocked(id)
		lk.Lock()
		defer lk.Unlock()
	}
	ctx, stop := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer stop()
	_, err := p.AcquireContext(ctx, "doubao")
	var evidence *PreparationError
	if !errors.Is(err, context.DeadlineExceeded) || !errors.As(err, &evidence) || evidence.AllCandidatesFailed() || len(evidence.FailedIDs) != 0 {
		t.Fatalf("pure initialization wait fabricated preparation failures: %+v err=%v", evidence, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = p.AcquireContext(ctx, "doubao")
	if !errors.Is(err, context.Canceled) || !errors.As(err, &evidence) || evidence.AllCandidatesFailed() {
		t.Fatalf("caller cancellation was treated as exhaustion: %v", err)
	}
}

func TestPreparationAuthChallengeAndNoCandidatesAreNotExhausted(t *testing.T) {
	p := preparationFixture(t)
	for _, id := range p.order {
		a := p.accounts[id]
		st := a.Sites["doubao"]
		st.Status = "need_captcha"
		a.Sites["doubao"] = st
	}
	_, err := p.AcquireContext(context.Background(), "doubao")
	var evidence *PreparationError
	if !errors.As(err, &evidence) || evidence.AllCandidatesFailed() || len(evidence.FailedIDs) != 0 {
		t.Fatalf("login challenge was treated as an actual preparation failure: %v", err)
	}
	if len(p.runtimes) != 0 {
		t.Fatal("acquisition probed challenge accounts instead of leaving them for recovery")
	}
}

func TestPreparationPartialFailureAndCanceledCompleteEvidenceAreNotExhausted(t *testing.T) {
	evidence := &PreparationError{Site: "doubao"}
	evidence.candidates([]string{"first", "second", "first"})
	evidence.failed("first", "browser_start_failed")
	if evidence.AllCandidatesFailed() {
		t.Fatal("one busy/unattempted account counted as failed")
	}
	evidence.failed("second", "session_setup_failed")
	if !evidence.AllCandidatesFailed() {
		t.Fatal("two actual failures were not recognized")
	}
	evidence.withCause(context.Canceled)
	if evidence.AllCandidatesFailed() {
		t.Fatal("caller cancellation authorized fallback")
	}
}

func TestPreparationInternalStartupTimeoutIsDistinctFromCallerCancellation(t *testing.T) {
	var failed *preparationStageFailure
	if !errors.As(preparationStartError(context.Background(), context.DeadlineExceeded), &failed) || failed.code != "browser_start_failed" {
		t.Fatal("completed internal startup timeout was mistaken for account waiting")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if errors.As(preparationStartError(ctx, context.Canceled), &failed) {
		t.Fatal("caller interruption fabricated an actual browser start failure")
	}
}
