package sessions

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestCancelledAcquireDoesNotInitializeAnyBrowser(t *testing.T) {
	p := &Pool{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, acquire := range []func(context.Context, string) (*Lease, error){p.AcquireContext, p.AcquireForLoginContext} {
		if _, err := acquire(ctx, "kimi"); !errors.Is(err, context.Canceled) {
			t.Fatalf("cancelled acquire = %v", err)
		}
	}
}

func TestAccountInitializationDoesNotBlockOtherTryLeases(t *testing.T) {
	p := NewPool("", t.TempDir(), "", "")
	a, err := p.Add("13800000000")
	if err != nil {
		t.Fatal(err)
	}
	p.mu.Lock()
	lock := p.accountLockLocked(a.ID)
	p.mu.Unlock()
	lock.Lock()
	defer lock.Unlock()
	start := time.Now()
	if _, err := p.acquireManagerSite(a.ID, "kimi", true); err == nil {
		t.Fatal("expected busy initialization")
	}
	if time.Since(start) > 100*time.Millisecond {
		t.Fatal("try lease waited for browser initialization")
	}
}

func TestTabInitializationWaitHonorsCallerBudget(t *testing.T) {
	m := New("", t.TempDir(), "")
	tabCtx, tabCancel := context.WithCancel(context.Background())
	m.tabs["deepseek"] = tabCtx
	m.tabCancels["deepseek"] = tabCancel
	m.tabInitMu.Lock()
	defer m.tabInitMu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	start := time.Now()
	if err := m.EnsurePageContext(ctx, "deepseek"); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("tab initialization budget = %v", err)
	}
	if time.Since(start) > 150*time.Millisecond {
		t.Fatal("tab initialization ignored its caller budget")
	}
}
