package sessions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func automaticChallengeFixture(t *testing.T, resident int) (*Pool, *Lease) {
	t.Helper()
	m := New("", t.TempDir(), "")
	p := &Pool{
		accounts: map[string]*Account{"challenged": newAccount("challenged", "13800138000")},
		order:    []string{"challenged"},
		runtimes: map[string]*Manager{"challenged": m},
		DataPath: filepath.Join(t.TempDir(), "accounts.json"),
	}
	if resident > 1 {
		other := New("", t.TempDir(), "")
		other.AcquireSite("doubao")
		p.runtimes["active"] = other
	}
	m.AcquireSite("doubao")
	lease := &Lease{AccountID: "challenged", Manager: m, pool: p, siteID: "doubao"}
	lease.MarkNeedsCaptcha("manual verification required")
	return p, lease
}

func TestAutomaticQuestionChallengeYieldsCapacityOnLeaseRelease(t *testing.T) {
	t.Setenv("GEO_SITE_CONCURRENCY", "2")
	for _, resident := range []int{1, 2} {
		t.Run(map[int]string{1: "spare_capacity", 2: "at_capacity"}[resident], func(t *testing.T) {
			p, lease := automaticChallengeFixture(t, resident)
			m := lease.Manager
			if !m.keepWarmTo.IsZero() || !m.autoWarmTo.After(time.Now()) {
				t.Fatal("automatic captcha changed explicit operator retention")
			}
			if !m.pendingLoginActive(time.Now(), pendingLoginGrace()) {
				t.Fatal("fixture did not exercise the fresh pending-login hold")
			}
			store := filepath.Join(m.ProfileDir, ".session-store.json")
			if err := os.WriteFile(store, []byte(`{"cookies":[]}`), 0o600); err != nil {
				t.Fatal(err)
			}
			lease.Release()
			lease.Release() // Releasing twice must remain harmless.
			if (p.runtimes["challenged"] != nil) != (resident == 1) {
				t.Fatal("automatic challenge did not yield only when browser capacity was needed")
			}
			if resident == 2 {
				select {
				case <-m.done:
				default:
					t.Fatal("reclaimed runtime was not stopped")
				}
				if p.runtimes["active"] == nil || !p.runtimes["active"].Busy() {
					t.Fatal("reclamation disturbed an active question")
				}
			}
			raw, err := os.ReadFile(p.DataPath)
			if err != nil {
				t.Fatal(err)
			}
			var saved poolStore
			if err := json.Unmarshal(raw, &saved); err != nil || len(saved.Accounts) != 1 || saved.Accounts[0].Sites["doubao"].Status != "need_captcha" {
				t.Fatal("runtime reclamation lost the persisted verification requirement")
			}
			if raw, err := os.ReadFile(store); err != nil || string(raw) != `{"cookies":[]}` {
				t.Fatal("reclamation removed the saved browser session")
			}
		})
	}
}

func TestAutomaticChallengeReclamationProtectsOperatorAndLoginWindows(t *testing.T) {
	t.Setenv("GEO_SITE_CONCURRENCY", "2")
	for _, name := range []string{"operator_login", "yuanbao_operator_login", "waiting_sms", "manual_captcha", "other_active_site"} {
		t.Run(name, func(t *testing.T) {
			p, lease := automaticChallengeFixture(t, 2)
			m := lease.Manager
			switch name {
			case "operator_login":
				m.KeepWarmFor(20 * time.Minute)
			case "yuanbao_operator_login":
				m.KeepWarmFor(45 * time.Minute)
			case "waiting_sms":
				m.set("kimi", "waiting_sms", "SMS requested")
			case "manual_captcha":
				m.set("yuanbao", "need_captcha", "operator login challenge")
			case "other_active_site":
				m.AcquireSite("deepseek")
				defer m.ReleaseSite("deepseek")
			}
			lease.Release()
			if p.runtimes["challenged"] != m {
				t.Fatal("capacity cleanup closed an operator/login/active window")
			}
		})
	}
}

func TestBusyAdminActionsProtectChallengeBeforeCustomerRelease(t *testing.T) {
	t.Setenv("GEO_SITE_CONCURRENCY", "2")
	for _, action := range []string{"screenshot", "click", "drag"} {
		t.Run(action, func(t *testing.T) {
			p, lease := automaticChallengeFixture(t, 2)
			m := lease.Manager
			// No Chrome is started: even an action that cannot render yet must
			// register the operator before returning through its busy fast path.
			switch action {
			case "screenshot":
				_, _ = p.Screenshot("challenged", "doubao")
			case "click":
				_, _ = p.ClickNorm("challenged", "doubao", 0.5, 0.5)
			case "drag":
				_, _ = p.DragNorm("challenged", "doubao", 0.2, 0.5, 0.8, 0.5)
			}
			if time.Until(m.keepWarmTo) < 9*time.Minute {
				t.Fatal("busy operator action did not establish explicit retention")
			}
			lease.Release()
			if p.runtimes["challenged"] != m {
				t.Fatal("customer release closed the operator's challenge")
			}
			m.mu.Lock()
			m.keepWarmTo = time.Now().Add(-time.Second)
			m.mu.Unlock()
			p.stopIfCold("challenged", m)
			if p.runtimes["challenged"] != nil {
				t.Fatal("an expired operator hold pinned the automatic challenge")
			}
		})
	}
}

func TestLateFailoverRegistrationReclaimsOnlyAutomaticChallenge(t *testing.T) {
	t.Setenv("GEO_SITE_CONCURRENCY", "2")
	p, lease := automaticChallengeFixture(t, 1)
	lease.Release()
	if p.runtimes["challenged"] == nil {
		t.Fatal("fixture should retain a challenge while below capacity")
	}
	first := New("", t.TempDir(), "")
	second := New("", t.TempDir(), "")
	p.runtimes["first"] = first
	p.runtimes["second"] = second
	p.mu.Lock()
	firstLock := p.accountLockLocked("first")
	p.mu.Unlock()
	firstLock.Lock() // Another profile may still be initializing.
	defer firstLock.Unlock()
	p.reclaimAutomaticChallengeCapacity("second")
	if len(p.runtimes) != 2 || p.runtimes["first"] != first || p.runtimes["second"] != second {
		t.Fatal("late failovers retained an idle challenge or disrupted initialization")
	}
}

func TestPreStartCapacityCleanupProtectsExplicitAndSMSWindows(t *testing.T) {
	t.Setenv("GEO_SITE_CONCURRENCY", "2")
	p, lease := automaticChallengeFixture(t, 1)
	m := lease.Manager
	m.KeepWarmFor(20 * time.Minute)
	lease.Release()
	sms := New("", t.TempDir(), "")
	sms.set("doubao", "waiting_sms", "SMS requested")
	p.runtimes["sms"] = sms
	p.runtimes["new"] = New("", t.TempDir(), "")
	p.reclaimAutomaticChallengeCapacity("new")
	if len(p.runtimes) != 3 || p.runtimes["challenged"] != m || p.runtimes["sms"] != sms {
		t.Fatal("pre-start cleanup reclaimed a protected operator or SMS window")
	}
}

func TestResolvedAutomaticChallengeClearsOnlyAutomaticHold(t *testing.T) {
	m := New("", t.TempDir(), "")
	lease := &Lease{Manager: m, siteID: "doubao"}
	lease.MarkNeedsCaptcha("verification required")
	m.KeepWarmFor(10 * time.Minute)
	explicit := m.keepWarmTo
	lease.MarkAvailable("verified")
	if !m.autoWarmTo.IsZero() || len(m.autoCaptcha) != 0 || !m.keepWarmTo.Equal(explicit) || !m.ShouldKeepWarm() {
		t.Fatal("resolved challenge retained automatic pressure or shortened operator hold")
	}
}
