package sessions

import (
	"path/filepath"
	"testing"
	"time"
)

func TestPriorLoginSurvivesLazyRuntimeInitialization(t *testing.T) {
	for _, initial := range []string{"starting", "idle", "unknown"} {
		t.Run(initial, func(t *testing.T) {
			m := New("", t.TempDir(), "")
			m.states["doubao"].Status = initial
			m.notePriorStatus("doubao", "logged_in")
			if m.states["doubao"].Status != "logged_in" {
				t.Fatalf("persisted login lost behind %s", initial)
			}
			m.states["doubao"].Status = "need_captcha"
			m.notePriorStatus("doubao", "logged_in")
			if m.states["doubao"].Status != "need_captcha" {
				t.Fatal("live verification was cleared")
			}
		})
	}
}

func TestPoolStartupDoesNotLaunchUnknownProfiles(t *testing.T) {
	p := NewPool("/nonexistent/chrome", t.TempDir(), "", filepath.Join(t.TempDir(), "accounts.json"))
	a, err := p.Add("13800138000")
	if err != nil {
		t.Fatal(err)
	}
	p.accounts[a.ID].Sites["doubao"] = State{SiteID: "doubao", Status: "unknown"}
	if err := p.Start(); err != nil {
		t.Fatalf("startup should wait for real work before launching Chrome: %v", err)
	}
	defer p.Stop()
	if len(p.runtimes) != 0 {
		t.Fatal("startup eagerly opened unknown account")
	}
}

func TestExpiredCaptchaRuntimeCanReleaseMemory(t *testing.T) {
	m := New("", t.TempDir(), "")
	m.states["doubao"].Status = "need_captcha"
	m.states["doubao"].UpdatedAt = time.Now().Add(-time.Hour).Format("2006-01-02T15:04:05")
	p := &Pool{accounts: map[string]*Account{"account": {ID: "account", Enabled: true, Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "need_captcha"}}}}, runtimes: map[string]*Manager{"account": m}}
	m.KeepWarmFor(time.Minute)
	p.stopIfCold("account", m)
	if p.runtimes["account"] != m {
		t.Fatal("active operator keepwarm was ignored")
	}
	m.keepWarmTo = time.Now().Add(-time.Second)
	p.stopIfCold("account", m)
	if p.runtimes["account"] != nil {
		t.Fatal("old challenge retained idle Chrome forever")
	}
	if p.accounts["account"].Sites["doubao"].Status != "need_captcha" {
		t.Fatal("releasing runtime must preserve saved challenge state")
	}
}

func TestAutomaticCaptchaKeepsOnlyShortOperatorGrace(t *testing.T) {
	m := New("", t.TempDir(), "")
	l := &Lease{Manager: m, siteID: "doubao"}
	l.MarkNeedsCaptcha("verification required")
	if !m.ShouldKeepWarm() || time.Until(m.keepWarmTo) > 3*time.Minute {
		t.Fatal("automatic challenge retained a browser for too long")
	}
}

func TestCaptchaRefreshCannotExtendExpiredGrace(t *testing.T) {
	m := New("", t.TempDir(), "")
	m.set("doubao", "need_captcha", "first challenge")
	m.pendingSince["doubao"] = time.Now().Add(-time.Hour)
	m.set("doubao", "need_captcha", "background status refresh")
	if m.pendingLoginActive(time.Now(), pendingLoginGrace()) {
		t.Fatal("background refresh extended challenge residency")
	}
}

func TestSMSPhaseGetsFreshGraceAfterLongCaptchaWithoutRefreshExtension(t *testing.T) {
	m := New("", t.TempDir(), "")
	m.set("deepseek", "need_captcha", "challenge")
	m.pendingSince["deepseek"] = time.Now().Add(-3 * time.Minute)
	m.set("deepseek", "waiting_sms", "challenge finished; callback pending")
	if !m.pendingLoginActive(time.Now(), pendingLoginGrace()) {
		t.Fatal("long captcha consumed the entire SMS callback window")
	}
	m.pendingSince["deepseek"] = time.Now().Add(-3 * time.Minute)
	m.set("deepseek", "waiting_sms", "background refresh")
	if m.pendingLoginActive(time.Now(), pendingLoginGrace()) {
		t.Fatal("repeated SMS refresh extended the unattended window")
	}
}

func TestUnknownProbeBackoffIsScopedAndCanClear(t *testing.T) {
	p := &Pool{}
	at := time.Now()
	if !p.claimUnknownProbe("a", "doubao", at) || p.claimUnknownProbe("a", "doubao", at.Add(time.Minute)) {
		t.Fatal("failed probe was not backed off")
	}
	if !p.claimUnknownProbe("a", "kimi", at) || !p.claimUnknownProbe("b", "doubao", at) {
		t.Fatal("one probe blocked other accounts or platforms")
	}
	if !p.claimUnknownProbe("a", "doubao", at.Add(5*time.Minute)) {
		t.Fatal("backoff never expired")
	}
	p.clearUnknownProbe("a", "doubao")
	if !p.claimUnknownProbe("a", "doubao", at) {
		t.Fatal("successful recovery did not clear backoff")
	}
}

func TestReaperReleasesExpiredRuntimeButProtectsBusyAccount(t *testing.T) {
	old := New("", t.TempDir(), "")
	busy := New("", t.TempDir(), "")
	old.states["doubao"].Status = "need_captcha"
	old.states["doubao"].UpdatedAt = time.Now().Add(-time.Hour).Format("2006-01-02T15:04:05")
	busy.AcquireSite("doubao")
	p := &Pool{runtimes: map[string]*Manager{"old": old, "busy": busy}}
	p.reapIdleRuntimes()
	if p.runtimes["old"] != nil || p.runtimes["busy"] != busy {
		t.Fatal("reaper did not distinguish expired and active runtimes")
	}
	busy.ReleaseSite("doubao")
	p.reapIdleRuntimes()
	if len(p.runtimes) != 0 {
		t.Fatal("released account was not reclaimed")
	}
}
