package sessions

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestPageOpenBudgetDefaultsToFortyFiveSeconds(t *testing.T) {
	t.Setenv("GEO_PAGE_OPEN_SEC", "")
	if pageOpenBudget() != 45*time.Second {
		t.Fatalf("default = %s", pageOpenBudget())
	}
	t.Setenv("GEO_PAGE_OPEN_SEC", "90")
	if pageOpenBudget() != 90*time.Second {
		t.Fatalf("override = %s", pageOpenBudget())
	}
	t.Setenv("GEO_PAGE_OPEN_SEC", "1")
	if pageOpenBudget() != 15*time.Second {
		t.Fatalf("floor = %s", pageOpenBudget())
	}
}

func TestLoginCountsOnlyEnabledLoggedInAccounts(t *testing.T) {
	p := NewPool("/nonexistent/chrome", t.TempDir(), "", filepath.Join(t.TempDir(), "accounts.json"))
	ready, err := p.Add("13800138001")
	if err != nil {
		t.Fatal(err)
	}
	off, err := p.Add("13800138002")
	if err != nil {
		t.Fatal(err)
	}
	other, err := p.Add("13800138003")
	if err != nil {
		t.Fatal(err)
	}
	p.mu.Lock()
	p.accounts[ready.ID].Sites["kimi"] = State{SiteID: "kimi", Status: "logged_in"}
	p.accounts[ready.ID].Sites["doubao"] = State{SiteID: "doubao", Status: "expired"}
	p.accounts[off.ID].Enabled = false
	p.accounts[off.ID].Sites["kimi"] = State{SiteID: "kimi", Status: "logged_in"}
	p.accounts[other.ID].Sites["kimi"] = State{SiteID: "kimi", Status: "not_logged_in"}
	p.mu.Unlock()
	var kimi, doubao LoginCount
	for _, row := range p.LoginCounts() {
		if row.SiteID == "kimi" {
			kimi = row
		}
		if row.SiteID == "doubao" {
			doubao = row
		}
	}
	if kimi.LoggedIn != 1 || kimi.Enabled != 2 || doubao.LoggedIn != 0 || doubao.Enabled != 2 {
		t.Fatalf("kimi=%+v doubao=%+v", kimi, doubao)
	}
}

func TestStartupDoesNotOpenUnknownAccountsWhenPoolHasReadyLogin(t *testing.T) {
	p := NewPool("/nonexistent/chrome", t.TempDir(), "", filepath.Join(t.TempDir(), "accounts.json"))
	ready, err := p.Add("13800138000")
	if err != nil {
		t.Fatal(err)
	}
	unknown, err := p.Add("13900139000")
	if err != nil {
		t.Fatal(err)
	}
	p.accounts[ready.ID].Sites["kimi"] = State{SiteID: "kimi", Status: "logged_in"}
	p.accounts[unknown.ID].Sites["kimi"] = State{SiteID: "kimi", Status: "unknown"}
	p.reconcileUnknownSessions()
	if len(p.runtimes) != 0 || p.accounts[unknown.ID].Sites["kimi"].Status != "unknown" {
		t.Fatal("startup warmed an unused browser despite a healthy pool login")
	}
}

func TestDeletedDefaultAccountDoesNotReturnAfterRestart(t *testing.T) {
	folder := t.TempDir()
	path := filepath.Join(folder, "accounts.json")
	p := NewPool("", filepath.Join(folder, "legacy"), "13800138000", path)
	if len(p.List()) != 1 {
		t.Fatal("missing store must migrate the initial default")
	}
	if err := p.Delete("default"); err != nil {
		t.Fatal(err)
	}
	reloaded := NewPool("", filepath.Join(folder, "legacy"), "13800138000", path)
	if len(reloaded.List()) != 0 {
		t.Fatal("deleted default account returned after restart")
	}
}

func TestDeleteRollbackKeepsAccountWhenPersistenceFails(t *testing.T) {
	folder := t.TempDir()
	p := NewPool("", filepath.Join(folder, "legacy"), "13800138000", filepath.Join(folder, "accounts.json"))
	blocker := filepath.Join(folder, "not-a-directory")
	if err := os.WriteFile(blocker, []byte("fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	p.DataPath = filepath.Join(blocker, "accounts.json")
	if err := p.Delete("default"); err == nil {
		t.Fatal("delete unexpectedly persisted")
	}
	if got := p.List(); len(got) != 1 || got[0].ID != "default" {
		t.Fatal("failed deletion changed the in-memory pool")
	}
}

func TestInvalidAccountStoreIsNotOverwrittenWithDefault(t *testing.T) {
	path := filepath.Join(t.TempDir(), "accounts.json")
	if err := os.WriteFile(path, []byte("invalid"), 0o600); err != nil {
		t.Fatal(err)
	}
	p := NewPool("", t.TempDir(), "13800138000", path)
	if len(p.List()) != 0 {
		t.Fatal("invalid store must not create a default account")
	}
	raw, _ := os.ReadFile(path)
	if string(raw) != "invalid" {
		t.Fatal("invalid store was overwritten")
	}
}

func TestDeletingDefaultAccountKeepsBusyProtectionAndOtherAccounts(t *testing.T) {
	folder := t.TempDir()
	p := NewPool("", filepath.Join(folder, "legacy"), "13800138000", filepath.Join(folder, "accounts.json"))
	other, err := p.Add("13900139000")
	if err != nil {
		t.Fatal(err)
	}
	m := New("", t.TempDir(), "13800138000")
	p.runtimes["default"] = m
	m.AcquireSite("doubao")
	if err := p.Delete("default"); err == nil {
		t.Fatal("busy default account was deleted")
	}
	m.ReleaseSite("doubao")
	if err := p.Delete("default"); err != nil {
		t.Fatal(err)
	}
	if got := p.List(); len(got) != 1 || got[0].ID != other.ID {
		t.Fatalf("other account was changed: %#v", got)
	}
}

func TestImportAddsManyAccountsWithOneConsistentStore(t *testing.T) {
	p := NewPool("", filepath.Join(t.TempDir(), "legacy"), "", filepath.Join(t.TempDir(), "accounts.json"))
	added, errs := p.Import([]string{"13800138000", "13900139000", "13800138000", "123"})
	if len(added) != 2 {
		t.Fatalf("added = %d, want 2", len(added))
	}
	if len(errs) != 2 {
		t.Fatalf("errors = %v, want duplicate and invalid phone", errs)
	}
	got := p.List()
	if len(got) != 2 || got[0].Phone != "13800138000" || got[1].Phone != "13900139000" {
		t.Fatalf("accounts = %#v", got)
	}
}

func TestMergeRuntimeStatePreservesLastKnownLogin(t *testing.T) {
	old := State{
		SiteID: "deepseek", Status: "logged_in", URL: "https://chat.deepseek.com/",
		LastCheckedAt: "2026-09-26T14:58:17", LastLoginAt: "2026-09-26T14:58:17",
	}
	current := State{SiteID: "deepseek", Status: "idle", Message: "未打开"}
	got := mergeRuntimeState(old, current, "13800138000")
	if got.Status != "logged_in" {
		t.Fatalf("status = %q, want logged_in", got.Status)
	}
	if got.LastCheckedAt != old.LastCheckedAt || got.LastLoginAt != old.LastLoginAt {
		t.Fatalf("timestamps were not preserved: %#v", got)
	}
	if got.Phone != "13800138000" {
		t.Fatalf("phone = %q", got.Phone)
	}
}

func TestMergeRuntimeStateMarksInterruptedLoginUnknown(t *testing.T) {
	old := State{SiteID: "kimi", Status: "waiting_sms", Message: "等待短信"}
	current := State{SiteID: "kimi", Status: "idle"}
	got := mergeRuntimeState(old, current, "13800138000")
	if got.Status != "unknown" {
		t.Fatalf("status = %q, want unknown", got.Status)
	}
	if got.Evidence == "" {
		t.Fatal("missing restart evidence")
	}
}

func TestMergeRuntimeStateCarriesTimestampsIntoCookieGuess(t *testing.T) {
	old := State{
		SiteID: "qianwen", Status: "logged_in", URL: "https://www.qianwen.com/",
		LastCheckedAt: "checked", LastLoginAt: "login",
	}
	current := State{SiteID: "qianwen", Status: "logged_in", Message: "cookie 仍在"}
	got := mergeRuntimeState(old, current, "13800138000")
	if got.LastCheckedAt != "checked" || got.LastLoginAt != "login" || got.URL != old.URL {
		t.Fatalf("last-known fields were not carried: %#v", got)
	}
}

func TestStatePriorityPrefersActiveLoginFlow(t *testing.T) {
	if statePriority("waiting_sms") <= statePriority("expired") {
		t.Fatal("waiting_sms should be visible ahead of an expired account")
	}
	if statePriority("logged_in") <= statePriority("waiting_sms") {
		t.Fatal("logged_in should remain the highest-priority public state")
	}
}

func TestInterruptedLoginStatus(t *testing.T) {
	for _, status := range []string{"logging_in", "sending", "waiting_sms", "submitting"} {
		if !isInterruptedLoginStatus(status) {
			t.Fatalf("%s should be reset after a restart", status)
		}
	}
	if isInterruptedLoginStatus("logged_in") || isInterruptedLoginStatus("not_logged_in") || isInterruptedLoginStatus("need_captcha") {
		t.Fatal("stable login states must survive a restart")
	}
}

func TestYuanbaoLoggedEvidenceRejectsGuestShell(t *testing.T) {
	if yuanbaoLoggedEvidence("未登录\n请登录后输入内容", true, false, false, false) {
		t.Fatal("guest page avatar shell must not count as a logged-in Yuanbao account")
	}
	if yuanbaoLoggedEvidence("用户中心", true, false, false, false) {
		t.Fatal("generic avatar without an account identifier must not count as logged in")
	}
	if !yuanbaoLoggedEvidence("用户中心\n用户12345678", true, false, false, false) {
		t.Fatal("authenticated account identifier should count as logged in")
	}
	if !yuanbaoLoggedEvidence("元宝\n用户6da1\n今天能帮你做什么？", false, false, false, false) {
		t.Fatal("alphanumeric Yuanbao account identifier should count as logged in")
	}
}

func TestCaptchaTextDoesNotOverrideAccount(t *testing.T) {
	page := "豆包\n新对话\n有什么我能帮你的吗？\n用户678577\n请点击安全验证"
	if captchaBlocksLogin(page, false) {
		t.Fatal("account id must beat leftover captcha copy")
	}
	if !captchaBlocksLogin("请完成安全验证", false) {
		t.Fatal("captcha copy without an account should still block")
	}
	if !captchaBlocksLogin(page, true) {
		t.Fatal("a visible captcha frame should still block")
	}
}

func TestDoubaoLoggedEvidenceRejectsGuestShell(t *testing.T) {
	if doubaoLoggedEvidence("豆包\n新对话\n有什么我能帮你的吗？", false, false, false) {
		t.Fatal("guest chat shell must not count as a logged-in Doubao account")
	}
	if doubaoLoggedEvidence("豆包\n新对话\nAPI 服务\n更多\n有什么我能帮你的吗？", false, false, false) {
		t.Fatal("current guest sidebar also renders API 服务 and 更多")
	}
	if !doubaoLoggedEvidence("豆包\n新对话\nAPI 服务\n更多\n用户ab1234", false, false, false) {
		t.Fatal("real account identifier should count as logged in")
	}
	if doubaoLoggedEvidence("新对话\nAPI 服务\n更多\n请登录后使用", false, false, false) {
		t.Fatal("explicit login gate must override account-only sidebar text")
	}
}

func TestAcquireFailsImmediatelyWithoutLoggedInAccount(t *testing.T) {
	p := &Pool{
		accounts: map[string]*Account{
			"acct_test": {
				ID: "acct_test", Phone: "13800138000", Enabled: true,
				Sites: map[string]State{
					"deepseek": {SiteID: "deepseek", Status: "not_logged_in"},
				},
			},
		},
		order:    []string{"acct_test"},
		runtimes: map[string]*Manager{},
		rr:       map[string]int{},
	}
	started := time.Now()
	if _, err := p.Acquire("deepseek"); err == nil {
		t.Fatal("acquire unexpectedly succeeded without a logged-in account")
	}
	if elapsed := time.Since(started); elapsed >= time.Second {
		t.Fatalf("acquire waited %s without a logged-in account", elapsed)
	}
}

func TestCandidatesWithStatusReturnsOnlyEnabledMatchingAccounts(t *testing.T) {
	p := &Pool{
		accounts: map[string]*Account{
			"unknown_enabled": {
				ID: "unknown_enabled", Enabled: true,
				Sites: map[string]State{"kimi": {SiteID: "kimi", Status: "unknown"}},
			},
			"unknown_disabled": {
				ID: "unknown_disabled", Enabled: false,
				Sites: map[string]State{"kimi": {SiteID: "kimi", Status: "unknown"}},
			},
			"logged_enabled": {
				ID: "logged_enabled", Enabled: true,
				Sites: map[string]State{"kimi": {SiteID: "kimi", Status: "logged_in"}},
			},
		},
		order:    []string{"unknown_enabled", "unknown_disabled", "logged_enabled"},
		runtimes: map[string]*Manager{},
	}
	got := p.candidatesWithStatus("kimi", "unknown")
	if len(got) != 1 || got[0] != "unknown_enabled" {
		t.Fatalf("unknown candidates = %#v", got)
	}
}

func TestLoginCandidatesIncludeLoggedOutEnabledAccounts(t *testing.T) {
	t.Setenv("GEO_CALLBACK_PHONES", "")
	t.Setenv("GEO_DOUBAO_PHONE", "")
	p := &Pool{
		accounts: map[string]*Account{
			"expired": {
				ID: "expired", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "expired"}},
			},
			"logged": {
				ID: "logged", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "logged_in"}},
			},
			"disabled": {
				ID: "disabled", Enabled: false,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "logged_in"}},
			},
		},
		order:    []string{"expired", "logged", "disabled"},
		runtimes: map[string]*Manager{},
		rr:       map[string]int{},
	}
	got := p.loginCandidates("doubao")
	if len(got) != 2 || got[0] != "logged" || got[1] != "expired" {
		t.Fatalf("login candidates = %#v", got)
	}
}

func TestYuanbaoLoginStaysOnCallbackPhone(t *testing.T) {
	t.Setenv("GEO_YUANBAO_PHONE", "13800138000")
	t.Setenv("GEO_CALLBACK_PHONES", "")
	t.Setenv("GEO_DOUBAO_PHONE", "")
	p := &Pool{
		accounts: map[string]*Account{
			"other": {
				ID: "other", Phone: "15200003031", Enabled: true,
				Sites: map[string]State{"yuanbao": {SiteID: "yuanbao", Status: "logged_in"}},
			},
			"callback": {
				ID: "callback", Phone: "17600008568", Enabled: true,
				Sites: map[string]State{"yuanbao": {SiteID: "yuanbao", Status: "not_logged_in"}},
			},
		},
		order:    []string{"other", "callback"},
		runtimes: map[string]*Manager{},
		rr:       map[string]int{},
	}
	got := p.loginCandidates("yuanbao")
	if len(got) != 1 || got[0] != "callback" {
		t.Fatalf("yuanbao login candidates = %#v", got)
	}
	if shouldReconcileSite("15200003031", "yuanbao") {
		t.Fatal("default phone must not be reconciled as yuanbao")
	}
	if !shouldReconcileSite("17600008568", "yuanbao") {
		t.Fatal("callback phone should still be reconciled for yuanbao")
	}
	if !shouldReconcileSite("15200003031", "deepseek") {
		t.Fatal("other sites stay eligible for startup reconcile")
	}
	if logged := p.candidates("yuanbao"); len(logged) != 1 || logged[0] != "other" {
		t.Fatalf("yuanbao logged-in candidates = %#v", logged)
	}
}

func TestLoginCandidatesPreferCallbackPhone(t *testing.T) {
	t.Setenv("GEO_CALLBACK_PHONES", "")
	t.Setenv("GEO_DOUBAO_PHONE", "17600008568")
	p := &Pool{
		accounts: map[string]*Account{
			"silent": {
				ID: "silent", Phone: "15200003031", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "not_logged_in"}},
			},
			"callback": {
				ID: "callback", Phone: "17600008568", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "need_captcha"}},
			},
			"other": {
				ID: "other", Phone: "17600008351", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "not_logged_in"}},
			},
		},
		order:    []string{"silent", "callback", "other"},
		runtimes: map[string]*Manager{},
		rr:       map[string]int{},
	}
	got := p.loginCandidates("doubao")
	if len(got) != 1 || got[0] != "callback" {
		t.Fatalf("login candidates = %#v", got)
	}
}

func TestLoginCandidatesUseOtherLoggedInWhenCallbackBlocked(t *testing.T) {
	t.Setenv("GEO_CALLBACK_PHONES", "")
	t.Setenv("GEO_DOUBAO_PHONE", "17600008568")
	p := &Pool{
		accounts: map[string]*Account{
			"callback": {
				ID: "callback", Phone: "17600008568", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "need_captcha"}},
			},
			"other": {
				ID: "other", Phone: "17600008351", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "logged_in"}},
			},
			"third": {
				ID: "third", Phone: "18500007612", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "logged_in"}},
			},
		},
		order:    []string{"callback", "other", "third"},
		runtimes: map[string]*Manager{},
		rr:       map[string]int{},
	}
	got := p.loginCandidates("doubao")
	if len(got) != 2 || got[0] != "other" || got[1] != "third" {
		t.Fatalf("blocked callback should yield other logged-in accounts, got %#v", got)
	}
}

func TestOmitIDsSkipsFailedAccounts(t *testing.T) {
	got := omitIDs([]string{"a", "b", "c"}, map[string]bool{"b": true})
	if len(got) != 2 || got[0] != "a" || got[1] != "c" {
		t.Fatalf("omit = %#v", got)
	}
	if same := omitIDs([]string{"a"}, nil); len(same) != 1 || same[0] != "a" {
		t.Fatalf("nil skip changed candidates: %#v", same)
	}
}

func TestKeepPriorLoginWhenPageUnclear(t *testing.T) {
	status, msg, evidence := keepPriorLogin("logged_in", "unknown", "未识别到输入框，可点截图操作", "页面缺少稳定的登录/退出特征")
	if status != "logged_in" || msg != "登录有效" || evidence == "" {
		t.Fatalf("prior login was dropped: %s %s %s", status, msg, evidence)
	}
	status, _, _ = keepPriorLogin("logged_in", "need_captcha", "请人工验证", "captcha")
	if status != "need_captcha" {
		t.Fatalf("captcha must replace a prior login, got %s", status)
	}
	status, _, _ = keepPriorLogin("", "unknown", "未识别到输入框，可点截图操作", "")
	if status != "unknown" {
		t.Fatalf("first sight of an unclear page should stay unknown, got %s", status)
	}
}

func TestStopIfColdReleasesSecondaryRuntime(t *testing.T) {
	m := New("", t.TempDir(), "13800138000")
	p := &Pool{
		accounts: map[string]*Account{
			"acct_test": {
				ID: "acct_test", Phone: "13800138000", Enabled: true,
				Sites: map[string]State{"deepseek": {SiteID: "deepseek", Status: "not_logged_in"}},
			},
		},
		runtimes: map[string]*Manager{"acct_test": m},
	}
	p.stopIfCold("acct_test", m)
	if _, ok := p.runtimes["acct_test"]; ok {
		t.Fatal("cold secondary runtime was not released")
	}
}

func TestStopIfColdKeepsWaitingSMSRuntime(t *testing.T) {
	m := New("", t.TempDir(), "13800138000")
	m.states["deepseek"].Status = "waiting_sms"
	p := &Pool{
		accounts: map[string]*Account{
			"acct_test": {
				ID: "acct_test", Phone: "13800138000", Enabled: true,
				Sites: map[string]State{"deepseek": {SiteID: "deepseek", Status: "waiting_sms"}},
			},
		},
		runtimes: map[string]*Manager{"acct_test": m},
	}
	p.stopIfCold("acct_test", m)
	if _, ok := p.runtimes["acct_test"]; !ok {
		t.Fatal("waiting SMS runtime was released")
	}
}

func TestStopIfColdIgnoresHistoricalCaptchaWithoutLiveLogin(t *testing.T) {
	for _, status := range []string{"need_captcha", "waiting_sms"} {
		t.Run(status, func(t *testing.T) {
			m := New("", t.TempDir(), "13800138000")
			p := &Pool{accounts: map[string]*Account{"acct_test": {ID: "acct_test", Phone: "13800138000", Enabled: true, Sites: map[string]State{"deepseek": {SiteID: "deepseek", Status: status}}}}, runtimes: map[string]*Manager{"acct_test": m}}
			p.stopIfCold("acct_test", m)
			if _, kept := p.runtimes["acct_test"]; kept {
				t.Fatal("historical login flag kept an unused Chrome alive")
			}
			if p.accounts["acct_test"].Sites["deepseek"].Status != status {
				t.Fatal("saved login state changed when releasing runtime")
			}
		})
	}
}

func TestMergeKeepsCaptchaWhenRuntimeIsIdle(t *testing.T) {
	old := State{SiteID: "doubao", Status: "need_captcha", Message: "请人工验证", LastCheckedAt: "2026-10-06T17:15:12"}
	got := mergeRuntimeState(old, State{SiteID: "doubao", Status: "idle"}, "13800138000")
	if got.Status != "need_captcha" || got.LastCheckedAt != old.LastCheckedAt {
		t.Fatalf("captcha state was cleared on idle merge: %#v", got)
	}
}

func TestLeaseMarkNeedsCaptchaUpdatesPoolState(t *testing.T) {
	m := New("", t.TempDir(), "13800138000")
	p := &Pool{
		accounts: map[string]*Account{
			"acct_test": {
				ID: "acct_test", Phone: "13800138000", Enabled: true,
				Sites: map[string]State{"doubao": {SiteID: "doubao", Status: "logged_in"}},
			},
		},
		runtimes: map[string]*Manager{"acct_test": m},
		DataPath: filepath.Join(t.TempDir(), "accounts.json"),
	}
	lease := &Lease{AccountID: "acct_test", Manager: m, pool: p, siteID: "doubao"}
	lease.MarkNeedsCaptcha("请人工验证")
	if got := p.accounts["acct_test"].Sites["doubao"]; got.Status != "need_captcha" || got.Message != "请人工验证" || got.LastCheckedAt == "" {
		t.Fatalf("pool state = %#v", got)
	}
	if got := m.states["doubao"]; got == nil || got.Status != "need_captcha" {
		t.Fatalf("manager state = %#v", got)
	}
}

func TestQuestionCaptchaKeepsBrowserForOperator(t *testing.T) {
	m := New("", t.TempDir(), "13800138000")
	p := &Pool{
		accounts: map[string]*Account{
			"acct_test": {
				ID: "acct_test", Phone: "13800138000", Enabled: true,
				Sites: map[string]State{},
			},
		},
		runtimes: map[string]*Manager{"acct_test": m},
		DataPath: filepath.Join(t.TempDir(), "accounts.json"),
	}
	lease := &Lease{AccountID: "acct_test", Manager: m, pool: p, siteID: "doubao"}
	lease.MarkNeedsCaptcha("平台触发安全验证，请完成验证后重试")
	if time.Until(m.keepWarmTo) < 10*time.Minute {
		t.Fatalf("question captcha was not held, until=%s", m.keepWarmTo)
	}
}

func TestStopIfColdKeepsExplicitlyWarmRuntime(t *testing.T) {
	m := New("", t.TempDir(), "13800138000")
	m.KeepWarmFor(time.Minute)
	p := &Pool{
		accounts: map[string]*Account{
			"acct_test": {
				ID: "acct_test", Phone: "13800138000", Enabled: true,
				Sites: map[string]State{"kimi": {SiteID: "kimi", Status: "logged_in"}},
			},
		},
		runtimes: map[string]*Manager{"acct_test": m},
	}
	p.stopIfCold("acct_test", m)
	if _, ok := p.runtimes["acct_test"]; !ok {
		t.Fatal("explicitly warm runtime was released")
	}
}

func TestBusyRuntimeCanBeObservedWithoutTakingSiteLease(t *testing.T) {
	m := New("", t.TempDir(), "13800138000")
	p := &Pool{
		runtimes: map[string]*Manager{"acct_test": m},
	}
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")

	if got := p.busyRuntime("acct_test"); got != m {
		t.Fatal("busy runtime was not available to admin observation")
	}
	if got := p.busyRuntime("missing"); got != nil {
		t.Fatal("missing account unexpectedly returned a runtime")
	}
}

func TestSetEnabledWaitsForPendingSiteUse(t *testing.T) {
	m := New("", t.TempDir(), "13800138000")
	m.mu.Lock()
	m.ready = true
	m.mu.Unlock()
	p := &Pool{
		accounts: map[string]*Account{
			"acct_test": newAccount("acct_test", "13800138000"),
		},
		runtimes: map[string]*Manager{"acct_test": m},
		locks:    map[string]*sync.Mutex{},
	}

	m.AcquireSite("deepseek")
	acquired := make(chan error, 1)
	go func() {
		_, err := p.acquireManagerSite("acct_test", "deepseek", false)
		acquired <- err
	}()
	time.Sleep(20 * time.Millisecond)

	disabled := make(chan error, 1)
	go func() {
		_, err := p.SetEnabled("acct_test", false)
		disabled <- err
	}()
	select {
	case err := <-disabled:
		t.Fatalf("disable returned before pending operation acquired the site: %v", err)
	case <-time.After(20 * time.Millisecond):
	}

	m.ReleaseSite("deepseek")
	if err := <-acquired; err != nil {
		t.Fatalf("pending operation failed: %v", err)
	}
	if err := <-disabled; err == nil {
		t.Fatal("disable succeeded while the pending operation held the site")
	}
	m.ReleaseSite("deepseek")
}

func TestManagerForLockedRevalidatesDeletedAccount(t *testing.T) {
	p := &Pool{
		accounts: map[string]*Account{},
		runtimes: map[string]*Manager{},
		locks:    map[string]*sync.Mutex{},
	}
	if _, err := p.managerForLocked("missing"); err == nil {
		t.Fatal("missing account unexpectedly created a runtime")
	}
}

func TestPreserveOldSessionOnlyForUncertainState(t *testing.T) {
	if preserveOldSession("expired", true) {
		t.Fatal("expired opened session should not preserve old credentials")
	}
	if preserveOldSession("not_logged_in", true) {
		t.Fatal("confirmed logged-out session should not preserve old credentials")
	}
	if !preserveOldSession("unknown", true) {
		t.Fatal("uncertain session should preserve the last snapshot")
	}
	if !preserveOldSession("not_logged_in", false) {
		t.Fatal("unopened session should preserve the last snapshot")
	}
}

func TestLoggedInTabStaysHot(t *testing.T) {
	if !keepHot("logged_in") {
		t.Fatal("logged-in tab must stay active so the next scrape can read it")
	}
	if keepHot("unknown") || keepHot("not_logged_in") {
		t.Fatal("idle tabs should still be allowed to freeze")
	}
}
