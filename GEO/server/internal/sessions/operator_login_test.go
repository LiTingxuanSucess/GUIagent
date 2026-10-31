package sessions

import (
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestOperatorLoginDoesNotOpenProtectedOrBusyAccounts(t *testing.T) {
	p := NewPool("/nonexistent/chrome", t.TempDir(), "", filepath.Join(t.TempDir(), "accounts.json"))
	a, _ := p.Add("13800138351")
	p.mu.Lock()
	p.accounts[a.ID].Sites["kimi"] = State{SiteID: "kimi", Status: "recovery_pending"}
	p.mu.Unlock()
	if _, err := p.CheckWithLoginAssist(a.ID, "kimi", nil); !errors.Is(err, ErrOperatorLoginBusy) {
		t.Fatalf("retained question touched: %v", err)
	}
	p.mu.Lock()
	p.accounts[a.ID].Sites["kimi"] = State{SiteID: "kimi", Status: "unknown"}
	lk := p.accountLockLocked(a.ID)
	p.mu.Unlock()
	lk.Lock()
	_, err := p.CheckWithLoginAssist(a.ID, "kimi", nil)
	lk.Unlock()
	if !errors.Is(err, ErrOperatorLoginBusy) {
		t.Fatalf("busy account waited or launched Chrome: %v", err)
	}
	if len(p.runtimes) != 0 || len(p.opSem) != 0 {
		t.Fatal("skipped operation retained runtime or permit")
	}
}

func TestOperatorLoginRetainsOriginalSMSPageAfterLongCLI(t *testing.T) {
	for _, status := range []string{"waiting_sms", "need_captcha", "logged_in", "not_logged_in"} {
		t.Run(status, func(t *testing.T) {
			p, m, original := attachedExistingRecoveryFixture(t)
			m.keepWarmTo = time.Time{}
			m.set("doubao", status, "CLI completed")
			m.pendingSince = map[string]time.Time{"doubao": time.Now().Add(-3 * time.Minute)}
			retainOperatorLoginPage(m, "doubao")
			p.stopIfCold("healthy", m)
			pending := status == "waiting_sms" || status == "need_captcha"
			if pending && (p.runtimes["healthy"] != m || m.tabs["doubao"] != original || !m.ShouldKeepWarm()) {
				t.Fatal("CLI duration consumed callback grace and original page was reclaimed")
			}
			if !pending && p.runtimes["healthy"] != nil {
				t.Fatal("completed login retained an unnecessary browser")
			}
		})
	}
}
