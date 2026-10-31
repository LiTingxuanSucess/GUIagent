package sessions

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/chromedp/chromedp"
)

func TestExistingRecoveryAdmissionInspectionIsReadOnly(t *testing.T) {
	p, m, tab := attachedExistingRecoveryFixture(t)
	prior := p.accounts["healthy"].Sites["doubao"]
	accountLocks, siteLocks, runtimes := len(p.locks), len(m.siteMu), len(p.runtimes)
	if state := p.InspectExistingForRecovery("doubao", "healthy"); state != ExistingRecoveryReady {
		t.Fatalf("existing original page not ready: %d", state)
	}
	if m.Busy() || m.tabs["doubao"] != tab || m.opened["doubao"] ||
		p.accounts["healthy"].Sites["doubao"] != prior || len(p.locks) != accountLocks ||
		len(m.siteMu) != siteLocks || len(p.runtimes) != runtimes {
		t.Fatal("inspection claimed, initialized, navigated, refreshed or replaced state")
	}
	if !m.TryAcquireSite("doubao") {
		t.Fatal("inspection retained the site lock")
	}
	m.ReleaseSite("doubao")
}

func TestExistingRecoveryAdmissionMissingNeverStartsOrSubstitutesBrowser(t *testing.T) {
	for _, cause := range []string{"account", "disabled", "runtime", "not_ready", "tab", "closed_tab", "target", "cdp", "site"} {
		t.Run(cause, func(t *testing.T) {
			p, m, tab := attachedExistingRecoveryFixture(t)
			site, account := "doubao", "healthy"
			switch cause {
			case "account":
				account = "absent"
			case "disabled":
				p.accounts[account].Enabled = false
			case "runtime":
				delete(p.runtimes, account)
			case "not_ready":
				m.ready = false
			case "tab":
				delete(m.tabs, site)
			case "closed_tab":
				closed, cancel := context.WithCancel(tab)
				cancel()
				m.tabs[site] = closed
			case "target":
				chromedp.FromContext(tab).Target = nil
			case "cdp":
				if err := os.Remove(filepath.Join(m.ProfileDir, "DevToolsActivePort")); err != nil {
					t.Fatal(err)
				}
			case "site":
				site = "absent"
			}
			before := len(p.runtimes)
			if state := p.InspectExistingForRecovery(site, account); state != ExistingRecoveryMissing {
				t.Fatalf("missing page not classified as missing: %d", state)
			}
			lease, err := p.TryAcquireExistingForRecoveryContext(context.Background(), site, account)
			if lease != nil || !errors.Is(err, ErrExistingRecoveryMissing) || m.Busy() || m.opened["doubao"] || len(p.runtimes) != before {
				t.Fatalf("lost target acquired or substituted state: lease=%t error=%v", lease != nil, err)
			}
		})
	}
}

func TestExistingRecoveryAdmissionBusyLocksNeverWaitOrReportLostTarget(t *testing.T) {
	for _, lock := range []string{"pool", "account", "manager", "site", "site_before_busy_flag", "initializing"} {
		t.Run(lock, func(t *testing.T) {
			p, m, _ := attachedExistingRecoveryFixture(t)
			var unlock func()
			switch lock {
			case "pool":
				p.mu.Lock()
				unlock = p.mu.Unlock
			case "account", "initializing":
				lk := p.accountLockLocked("healthy")
				lk.Lock()
				unlock = lk.Unlock
				if lock == "initializing" {
					delete(p.runtimes, "healthy")
				}
			case "manager":
				m.mu.Lock()
				unlock = m.mu.Unlock
			case "site":
				m.AcquireSite("doubao")
				unlock = func() { m.ReleaseSite("doubao") }
			case "site_before_busy_flag":
				lk := m.lockFor("doubao")
				lk.Lock()
				unlock = lk.Unlock
			}
			defer unlock()
			done := make(chan bool, 1)
			go func() {
				state := p.InspectExistingForRecovery("doubao", "healthy")
				lease, err := p.TryAcquireExistingForRecoveryContext(context.Background(), "doubao", "healthy")
				done <- state == ExistingRecoveryBusy && lease == nil && errors.Is(err, ErrExistingRecoveryBusy)
			}()
			select {
			case correct := <-done:
				if !correct {
					t.Fatal("busy lock became a lost target or was claimed")
				}
			case <-time.After(250 * time.Millisecond):
				t.Fatal("admission inspection or exact claim blocked behind a lock")
			}
		})
	}
}

func TestExistingRecoveryFastClaimRechecksAfterAdvisoryAdmission(t *testing.T) {
	for _, change := range []string{"busy", "lost"} {
		t.Run(change, func(t *testing.T) {
			p, m, _ := attachedExistingRecoveryFixture(t)
			if p.InspectExistingForRecovery("doubao", "healthy") != ExistingRecoveryReady {
				t.Fatal("fixture not ready before admission")
			}
			wanted := ErrExistingRecoveryMissing
			if change == "busy" {
				m.AcquireSite("doubao")
				defer m.ReleaseSite("doubao")
				wanted = ErrExistingRecoveryBusy
			} else {
				delete(m.tabs, "doubao")
			}
			lease, err := p.TryAcquireExistingForRecoveryContext(context.Background(), "doubao", "healthy")
			if lease != nil || !errors.Is(err, wanted) {
				t.Fatalf("post-admission change not rechecked: lease=%t error=%v", lease != nil, err)
			}
			if change == "busy" && !m.isBusy("doubao") {
				t.Fatal("fast claim released another operation's lease")
			}
		})
	}
}

func TestExistingRecoveryFastClaimPreservesCancellationAndOriginalPage(t *testing.T) {
	p, m, tab := attachedExistingRecoveryFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if lease, err := p.TryAcquireExistingForRecoveryContext(ctx, "doubao", "healthy"); lease != nil || !errors.Is(err, context.Canceled) || m.Busy() {
		t.Fatal("cancelled caller acquired an existing page")
	}
	lease, err := p.TryAcquireExistingForRecoveryContext(context.Background(), "doubao", "healthy")
	if err != nil || lease == nil || lease.AccountID != "healthy" || lease.Manager != m || m.tabs["doubao"] != tab || m.opened["doubao"] {
		t.Fatalf("exact existing page was not leased without navigation: error=%v", err)
	}
	defer lease.Release()
	if guard, err := lease.BeginWebRecovery(); err != nil {
		t.Fatal("fast claim did not hold the site lease required by recovery")
	} else {
		guard()
	}
	if second, err := p.TryAcquireExistingForRecoveryContext(context.Background(), "doubao", "healthy"); second != nil || !errors.Is(err, ErrExistingRecoveryBusy) {
		t.Fatal("one original page acquired two simultaneous recovery leases")
	}
}

func TestExistingRecoveryConcurrentFastClaimsHaveOneOwner(t *testing.T) {
	p, _, _ := attachedExistingRecoveryFixture(t)
	var wg sync.WaitGroup
	start := make(chan struct{})
	results := make(chan *Lease, 2)
	errorsSeen := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			lease, err := p.TryAcquireExistingForRecoveryContext(context.Background(), "doubao", "healthy")
			results <- lease
			errorsSeen <- err
		}()
	}
	close(start)
	wg.Wait()
	close(results)
	close(errorsSeen)
	owners, busy := 0, 0
	for lease := range results {
		if lease != nil {
			owners++
			defer lease.Release()
		}
	}
	for err := range errorsSeen {
		if errors.Is(err, ErrExistingRecoveryBusy) {
			busy++
		} else if err != nil {
			t.Fatal(err)
		}
	}
	if owners != 1 || busy != 1 {
		t.Fatalf("concurrent original-page ownership: owners=%d busy=%d", owners, busy)
	}
}
