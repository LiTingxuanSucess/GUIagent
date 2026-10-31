package sessions

import (
	"testing"
	"time"
)

func TestBackgroundLoginDefersToCustomerLeases(t *testing.T) {
	t.Setenv("GEO_SITE_CONCURRENCY", "2")
	m := New("", t.TempDir(), "")
	p := &Pool{runtimes: map[string]*Manager{"active": m}}
	m.AcquireSite("doubao")
	if !p.autoLoginShouldDefer("") || !p.autoLoginShouldDefer("another") {
		t.Fatal("background login could launch Chrome during a customer lease")
	}
	m.ReleaseSite("doubao")
	if p.autoLoginShouldDefer("another") {
		t.Fatal("idle pool did not allow automatic recovery")
	}
}

func TestBackgroundBrowserLimitAllowsExistingLoginToFinish(t *testing.T) {
	t.Setenv("GEO_SITE_CONCURRENCY", "2")
	p := &Pool{runtimes: map[string]*Manager{
		"one": New("", t.TempDir(), ""), "two": New("", t.TempDir(), ""),
	}}
	if !p.autoLoginShouldDefer("third") {
		t.Fatal("background recovery exceeded customer browser capacity")
	}
	if p.autoLoginShouldDefer("one") {
		t.Fatal("existing SMS login was stranded at the browser limit")
	}
	p.stopping = true
	if !p.autoLoginShouldDefer("one") {
		t.Fatal("background login started during shutdown")
	}
}

func TestBackgroundAndExplicitLoginRetentionDiffer(t *testing.T) {
	for _, test := range []struct {
		name                  string
		warm                  time.Duration
		keepAfterThreeMinutes bool
	}{
		{"automatic", pendingLoginGrace(), false},
		{"operator", 20 * time.Minute, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			m := New("", t.TempDir(), "")
			m.set("doubao", "waiting_sms", "SMS requested")
			m.KeepWarmFor(test.warm)
			m.mu.Lock()
			m.keepWarmTo = m.keepWarmTo.Add(-3 * time.Minute)
			m.pendingSince["doubao"] = time.Now().Add(-3 * time.Minute)
			m.mu.Unlock()
			if m.ShouldKeepWarm() != test.keepAfterThreeMinutes {
				t.Fatal("unattended login pinned Chrome or operator window was lost")
			}
			if m.pendingLoginActive(time.Now(), pendingLoginGrace()) {
				t.Fatal("expired automatic pending grace still active")
			}
		})
	}
}

func TestQuestionCaptchaCannotBeReclassifiedByUnattendedLogin(t *testing.T) {
	for _, status := range []string{"need_captcha", "waiting_sms", "submitting", "logged_in"} {
		if needsAutoRelogin(status) {
			t.Fatalf("automatic login must preserve %s for the existing session/operator", status)
		}
	}
	if !needsAutoRelogin("expired") {
		t.Fatal("expired sessions must remain eligible for background recovery")
	}
	p := &Pool{accounts: map[string]*Account{"account": {Enabled: true, Sites: map[string]State{"doubao": {Status: "expired"}}}}}
	if !p.automaticLoginStillNeeded("account", "doubao") {
		t.Fatal("expired login was ineligible")
	}
	p.accounts["account"].Sites["doubao"] = State{Status: "need_captcha"}
	if p.automaticLoginStillNeeded("account", "doubao") {
		t.Fatal("stale background candidate erased a newly reported challenge")
	}
}

func TestFreshLoginCaptchaUsesHandlerOnlyWhenIdleAndStillChallenged(t *testing.T) {
	m := New("", t.TempDir(), "")
	p := &Pool{accounts: map[string]*Account{"account": {Enabled: true, Sites: map[string]State{"doubao": {Status: "need_captcha"}}}}, runtimes: map[string]*Manager{"account": m}}
	calls := 0
	p.SetLoginChallengeAssist(func(id, site string) error { calls++; return nil })
	p.assistFreshLoginChallenge("account", "doubao", &State{Status: "waiting_sms"})
	if calls != 0 {
		t.Fatal("SMS was handed to CLI")
	}
	m.AcquireSite("kimi")
	p.assistFreshLoginChallenge("account", "doubao", &State{Status: "need_captcha"})
	if calls != 0 {
		t.Fatal("background CLI competed with customer lease")
	}
	m.ReleaseSite("kimi")
	p.assistFreshLoginChallenge("account", "doubao", &State{Status: "need_captcha"})
	if calls != 1 {
		t.Fatal("fresh login captcha was never handed off")
	}
	p.accounts["account"].Sites["doubao"] = State{Status: "logged_in"}
	p.assistFreshLoginChallenge("account", "doubao", &State{Status: "need_captcha"})
	if calls != 1 {
		t.Fatal("stale captcha observation started CLI")
	}
}
