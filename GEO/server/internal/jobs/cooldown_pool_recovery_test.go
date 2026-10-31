package jobs

import "testing"

func TestVerifiedLocalAccountsCanRecoverDuringRiskCooldown(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "1")
	p := &changingCapacityPool{}
	q := &Queue{DataDir: t.TempDir(), Sess: p}
	q.recordSiteCooldown("doubao", siteError("doubao", "question", "platform_risk_control", "captcha", false, 1))
	if q.siteCooldown("doubao", "doubao", "question") == nil {
		t.Fatal("pool with no verified accounts ignored risk cooldown")
	}
	p.count.Store(2) // The operator verified two logins or added valid accounts.
	if q.siteCooldown("doubao", "doubao", "question") != nil {
		t.Fatal("platform cooldown blocked verified local accounts")
	}
	p.count.Store(0)
	if q.siteCooldown("doubao", "doubao", "question") == nil {
		t.Fatal("allowing a verified account erased the remaining risk cooldown")
	}
	p.count.Store(2)
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	if q.siteCooldown("doubao", "doubao", "question") == nil {
		t.Fatal("local logins cleared the unrelated cloud-browser cooldown")
	}
}
