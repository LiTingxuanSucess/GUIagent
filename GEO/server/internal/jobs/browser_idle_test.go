package jobs

import (
	"context"
	"testing"
	"time"
)

func TestIdleRecoveryYieldsOnlyGlobalSlotAndReclaimsBeforeResume(t *testing.T) {
	t.Setenv("GEO_BROWSER_IDLE_MIN_AVAILABLE_MB", "0")
	q := schedulerFixture()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	p := &browserPermit{q: q, timings: &stageTimings{}}
	ctx = context.WithValue(ctx, browserPermitKey{}, p)
	first, ok := q.acquireBrowserResources(ctx, "deepseek", "recovering", p.timings)
	if !ok {
		t.Fatal("initial admission failed")
	}
	defer first()
	second, _ := q.acquireBrowserResources(context.Background(), "yuanbao", "working", &stageTimings{})
	defer second()
	other := make(chan func(), 1)
	go func() {
		if release, ok := q.acquireBrowserResources(context.Background(), "kimi", "other", &stageTimings{}); ok {
			other <- release
		}
	}()
	waitForBrowserWaiters(t, q, 1)
	waitDone, resumed := make(chan struct{}), make(chan error, 1)
	go func() { resumed <- withIdleBrowser(ctx, func() error { <-waitDone; return nil }) }()
	var third func()
	select {
	case third = <-other:
	case <-ctx.Done():
		t.Fatal("idle recovery blocked unrelated work")
	}
	q.mu.Lock()
	reserved := q.poolActive["deepseek"] == 1 && len(q.siteGates["deepseek"]) == 1
	q.mu.Unlock()
	if !reserved {
		t.Fatal("yield released the protected account/site reservation")
	}
	close(waitDone)
	select {
	case <-resumed:
		t.Fatal("recovery resumed without a global slot")
	default:
	}
	third()
	select {
	case err := <-resumed:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("recovery did not resume")
	}
	if len(q.siteSem) != 2 {
		t.Fatal("global browser limit changed during recovery")
	}
	first()
	second()
	if len(q.siteSem) != 0 || q.poolActive["deepseek"] != 0 {
		t.Fatal("recovery leaked a reservation")
	}
}

func TestIdleRecoveryCancellationAndFinalReleaseDoNotStealAnotherSlot(t *testing.T) {
	t.Setenv("GEO_BROWSER_IDLE_MIN_AVAILABLE_MB", "0")
	q := schedulerFixture()
	q.siteSem = make(chan struct{}, 1)
	ctx, cancel := context.WithCancel(context.Background())
	p := &browserPermit{q: q, timings: &stageTimings{}}
	ctx = context.WithValue(ctx, browserPermitKey{}, p)
	release, _ := q.acquireBrowserResources(ctx, "deepseek", "recovering", p.timings)
	if !p.pause() {
		t.Fatal("pause failed")
	}
	other, _ := q.acquireBrowserResources(context.Background(), "kimi", "other", &stageTimings{})
	cancel()
	if p.resume(ctx) {
		t.Fatal("cancelled recovery reacquired a slot")
	}
	release()
	release()
	if len(q.siteSem) != 1 {
		t.Fatal("cleanup stole another request's permit")
	}
	other()
	if len(q.siteSem) != 0 {
		t.Fatal("permit leaked")
	}
}

func TestClosedRecoveryUnblocksPendingReadmission(t *testing.T) {
	t.Setenv("GEO_BROWSER_IDLE_MIN_AVAILABLE_MB", "0")
	q := schedulerFixture()
	q.siteSem = make(chan struct{}, 1)
	p := &browserPermit{q: q, timings: &stageTimings{}}
	ctx := context.WithValue(context.Background(), browserPermitKey{}, p)
	release, _ := q.acquireBrowserResources(ctx, "deepseek", "recovering", p.timings)
	p.pause()
	other, _ := q.acquireBrowserResources(context.Background(), "kimi", "other", &stageTimings{})
	done := make(chan bool, 1)
	go func() { done <- p.resume(ctx) }()
	release()
	select {
	case ok := <-done:
		if ok {
			t.Fatal("closed recovery resumed")
		}
	case <-time.After(time.Second):
		t.Fatal("release left readmission blocked")
	}
	other()
}
