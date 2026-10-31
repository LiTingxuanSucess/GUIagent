package sessions

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func recoveryLeaseFixture(t *testing.T) *Pool {
	t.Helper()
	root := t.TempDir()
	p := NewPool("/nonexistent/chrome", root, "", filepath.Join(root, "accounts.json"))
	for _, item := range []struct{ id, status string }{{"disabled", "need_captcha"}, {"quota", "expired"}, {"unknown", "unknown"}, {"healthy", "logged_in"}, {"captcha", "need_captcha"}, {"expired", "expired"}} {
		a := newAccount(item.id, "13800138000")
		st := a.Sites["doubao"]
		st.Status = item.status
		if item.id == "disabled" {
			a.Enabled = false
		}
		if item.id == "quota" {
			st.Message = "本月用量已用完"
		}
		a.Sites["doubao"] = st
		p.accounts[item.id], p.order = a, append(p.order, item.id)
		profile := filepath.Join(root, item.id)
		if err := os.Mkdir(profile, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte("12345\n"), 0600); err != nil {
			t.Fatal(err)
		}
		m := New("/nonexistent/chrome", profile, a.Phone)
		m.ready, m.started = true, true
		m.tabs["doubao"] = context.Background()
		m.states["doubao"] = &st
		m.KeepWarmFor(20 * time.Minute)
		p.runtimes[item.id] = m
	}
	return p
}

func TestRecoveryLeaseSelectsOnlyExplicitEligibleAccountsWithoutProbing(t *testing.T) {
	p := recoveryLeaseFixture(t)
	if got := p.RecoveryAccountIDs("doubao"); !reflect.DeepEqual(got, []string{"captcha", "expired"}) {
		t.Fatalf("disabled/quota/unknown/healthy account entered recovery: %v", got)
	}
	prior := p.accounts["expired"].Sites["doubao"]
	manager := p.runtimes["expired"]
	tab := manager.tabs["doubao"]
	lease, err := p.AcquireForRecoveryContext(context.Background(), "doubao", map[string]bool{"captcha": true})
	if err != nil || lease.AccountID != "expired" || lease.Manager != manager || manager.tabs["doubao"] != tab || p.accounts["expired"].Sites["doubao"] != prior {
		t.Fatalf("recovery acquisition navigated/probed the page or chose the wrong account: lease=%v err=%v", lease, err)
	}
	if manager.TryAcquireSite("doubao") {
		manager.ReleaseSite("doubao")
		t.Fatal("another question could share the recovery lease")
	}
	lease.Release()
	lease.Release()
	if manager.isBusy("doubao") {
		t.Fatal("recovery lease was not released idempotently")
	}
}

func TestRecoveryLeaseBusyCancellationDoesNotProbeOrPoisonState(t *testing.T) {
	p := recoveryLeaseFixture(t)
	m := p.runtimes["captcha"]
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")
	prior := p.accounts["captcha"].Sites["doubao"]
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	_, err := p.AcquireForRecoveryContext(ctx, "doubao", map[string]bool{"expired": true})
	if !errors.Is(err, context.DeadlineExceeded) || p.accounts["captcha"].Sites["doubao"] != prior {
		t.Fatalf("waiting for recovery lease changed state or ignored cancellation: %v", err)
	}
}

func TestRecoveryLeaseMovesPastBadSetupOnceToNextExplicitCandidate(t *testing.T) {
	p := recoveryLeaseFixture(t)
	bad := p.runtimes["captcha"]
	if err := os.Remove(filepath.Join(bad.ProfileDir, "DevToolsActivePort")); err != nil {
		t.Fatal(err)
	}
	prior := p.accounts["unknown"].Sites["doubao"]
	lease, err := p.AcquireForRecoveryContext(context.Background(), "doubao", nil)
	if err != nil || lease.AccountID != "expired" {
		t.Fatalf("first candidate's CDP failure blocked the next eligible account: lease=%v err=%v", lease, err)
	}
	defer lease.Release()
	if bad.isBusy("doubao") || p.accounts["unknown"].Sites["doubao"] != prior {
		t.Fatal("failed setup leaked its lock or probed an unrelated unknown account")
	}
}

func TestRecoverySMSAllowsOnlyExplicitCallbackWithoutReacquiringLease(t *testing.T) {
	p := recoveryLeaseFixture(t)
	m := p.runtimes["captcha"]
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")
	lease := &Lease{AccountID: "captcha", Manager: m, siteID: "doubao"}
	guard, err := lease.BeginWebRecovery()
	if err != nil {
		t.Fatal(err)
	}
	defer guard()
	if _, err := p.SubmitCode("captcha", "doubao", "123456"); err == nil || !strings.Contains(err.Error(), "未开放") {
		t.Fatalf("SMS callback entered while CLI owned the page: %v", err)
	}
	closeSMS, err := lease.AllowRecoverySMS()
	if err != nil {
		t.Fatal(err)
	}
	defer closeSMS()
	// The allowed branch reaches the existing Manager without retaking the
	// held site lease. A missing fixture tab fails locally before any CDP run.
	m.mu.Lock()
	delete(m.tabs, "doubao")
	m.mu.Unlock()
	callback := make(chan error, 1)
	go func() { _, err := p.SubmitCode("captcha", "doubao", "123456"); callback <- err }()
	select {
	case err := <-callback:
		if err == nil || !strings.Contains(err.Error(), "site not started") {
			t.Fatalf("controlled callback did not reach the existing leased manager: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("controlled SMS callback deadlocked on the recovery lease")
	}
	if !m.isBusy("doubao") || len(p.opSem) != 0 {
		t.Fatal("callback released the customer's lease or occupied the global operation queue")
	}
	if manual, err := beginManualTargetAction(m, "doubao"); err == nil {
		manual()
		t.Fatal("allowing SMS also opened manual clicks/drags")
	}
	closeSMS()
	if _, err := p.SubmitCode("captcha", "doubao", "123456"); err == nil || !strings.Contains(err.Error(), "未开放") {
		t.Fatal("SMS callback remained open during independent probe/resume")
	}
}

func TestRecoverySMSWindowCloseWaitsForCallbackAndRejectsNewOnes(t *testing.T) {
	p := recoveryLeaseFixture(t)
	m := p.runtimes["captcha"]
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")
	lease := &Lease{Manager: m, siteID: "doubao"}
	guard, err := lease.BeginWebRecovery()
	if err != nil {
		t.Fatal(err)
	}
	defer guard()
	closeSMS, err := lease.AllowRecoverySMS()
	if err != nil {
		t.Fatal(err)
	}
	defer closeSMS()
	releaseCallback, recovering := beginRecoverySMSAction(m, "doubao")
	if !recovering || releaseCallback == nil {
		t.Fatal("could not admit an existing callback")
	}
	defer releaseCallback()
	closed := make(chan struct{})
	go func() { closeSMS(); close(closed) }()
	until := time.Now().Add(time.Second)
	windowClosed := false
	for time.Now().Before(until) {
		recoveryTargetMu.Lock()
		windowClosed = recoveryTargets[recoveryTargetKey{m, "doubao"}].smsAllowed == 0
		recoveryTargetMu.Unlock()
		if windowClosed {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if !windowClosed {
		t.Error("SMS admission did not close before waiting for the callback")
	}
	if windowClosed {
		if extra, recovering := beginRecoverySMSAction(m, "doubao"); !recovering || extra != nil {
			if extra != nil {
				extra()
			}
			t.Error("closed SMS window admitted another callback")
		}
	}
	select {
	case <-closed:
		t.Error("probe could begin before the active SMS callback finished")
	default:
	}
	releaseCallback()
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("closed SMS window did not drain its admitted callback")
	}
}

func TestRecoveryWaitingSMSRoutesExistingCallbackAndSurvivesLeaseRelease(t *testing.T) {
	p := recoveryLeaseFixture(t)
	m := p.runtimes["captcha"]
	const sentAt = 1234.5
	m.states["doubao"].LastSendAt = sentAt
	prior := p.accounts["captcha"].Sites["doubao"]
	prior.LastSendAt = sentAt
	p.accounts["captcha"].Sites["doubao"] = prior
	m.AcquireSite("doubao")
	lease := &Lease{AccountID: "captcha", Manager: m, pool: p, siteID: "doubao"}
	guard, err := lease.BeginWebRecovery()
	if err != nil {
		t.Fatal(err)
	}
	lease.MarkRecoveryWaitingSMS()
	guard()
	lease.Release()
	waiting := p.WaitingSMS()
	found := false
	for _, candidate := range waiting {
		if candidate.AccountID == "captcha" && candidate.SiteID == "doubao" {
			found = true
		}
	}
	if !found || p.accounts["captcha"].Sites["doubao"].LastSendAt != sentAt || m.states["doubao"].LastSendAt != sentAt {
		t.Fatal("SMS wait could not route a late existing callback or invented a new send time")
	}
}

func TestWebRecoveryGuardRejectsConcurrentManualWritesAndPreservesOtherSites(t *testing.T) {
	p := recoveryLeaseFixture(t)
	m := p.runtimes["captcha"]
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")
	lease := &Lease{AccountID: "captcha", Manager: m, siteID: "doubao"}
	release, err := lease.BeginWebRecovery()
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	if _, err := p.ClickNorm("captcha", "doubao", .5, .5); err == nil || !strings.Contains(err.Error(), "自动恢复") {
		t.Fatalf("manual click reached an exclusively recovering target: %v", err)
	}
	if _, err := p.DragNorm("captcha", "doubao", .1, .1, .8, .8); err == nil || !strings.Contains(err.Error(), "自动恢复") {
		t.Fatalf("manual drag reached an exclusively recovering target: %v", err)
	}
	if other, err := beginManualTargetAction(m, "kimi"); err != nil {
		t.Fatal("one platform's recovery blocked a different target")
	} else {
		other()
	}
	if _, err := lease.BeginWebRecovery(); err == nil {
		t.Fatal("a second recovery entered the same target")
	}
	release()
	release()
	manual, err := beginManualTargetAction(m, "doubao")
	if err != nil {
		t.Fatal("completed recovery kept manual writes locked")
	}
	if _, err := lease.BeginWebRecovery(); err == nil {
		t.Fatal("CLI recovery raced an already in-flight manual operation")
	}
	manual()
	manual()
	if again, err := lease.BeginWebRecovery(); err != nil {
		t.Fatal("finished manual operation kept recovery locked")
	} else {
		again()
	}
}
