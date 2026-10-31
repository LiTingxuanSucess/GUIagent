package jobs

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLimitDatesAndUnknownBackoff(t *testing.T) {
	at := time.Date(2026, 12, 31, 23, 0, 0, 0, time.FixedZone("CST", 8*3600))
	for _, test := range []struct{ message, want string }{
		{"元宝本月用量已用完，次月1日零点刷新", "2027-01-01T00:00:00+08:00"},
		{"Kimi 免费额度已用完，平台提示 01-02 重置", "2027-01-02T00:00:00+08:00"},
		{"账号已被禁言至 2027-01-03 12:45", "2027-01-03T12:45:00+08:00"},
		{"额度已用完 02-30 重置", ""},
		{"额度已用完", ""},
	} {
		_, reset := platformLimitDetails(test.message, at)
		if test.want == "" {
			if !reset.IsZero() {
				t.Errorf("invented reset for %q: %s", test.message, reset)
			}
			continue
		}
		if reset.Format(time.RFC3339) != test.want {
			t.Errorf("%q: %s", test.message, reset)
		}
	}
	t.Setenv("GEO_QUOTA_COOLDOWN_SEC", "1800")
	t.Setenv("GEO_QUOTA_MAX_COOLDOWN_SEC", "7200")
	q := &Queue{}
	for i, minutes := range []int{30, 60, 120, 120} {
		limit := q.recordAccountLimitLocked("kimi", "acct_a", "额度已用完", at, "live")
		if !limit.ResetAt.IsZero() || limit.NextProbeAt.Sub(at) != time.Duration(minutes)*time.Minute || limit.DetectionCount != i+1 || !limit.FirstDetectedAt.Equal(at) {
			t.Fatalf("limit=%+v", limit)
		}
	}
}

func TestLimitPersistsAndOnlyWebSuccessClearsObservation(t *testing.T) {
	q := &Queue{DataDir: t.TempDir()}
	q.blockAccount("kimi", "acct_a", "免费额度已用完")
	first := q.AccountLimits()[0]
	q.accountBlocks["kimi/acct_a"] = time.Now().Add(-time.Second)
	limit := q.accountLimits["kimi/acct_a"]
	limit.NextProbeAt = time.Now().Add(-time.Second)
	q.accountLimits["kimi/acct_a"] = limit
	q.blockedAccounts("kimi")
	if row := q.AccountLimits()[0]; row.Status != "pending_recheck" || !row.RecoveredAt.IsZero() {
		t.Fatalf("expiry called recovery: %+v", row)
	}
	// The business job succeeds via API; only the explicit web success hook
	// below is allowed to clear account protection.
	q.jobs = map[string]*Job{"api": {Results: []map[string]any{{"source": "api", "ok": true, "site_id": "kimi", "account_id": "acct_a"}}}}
	q.saveAccountProtectionLocked()
	reloaded := &Queue{DataDir: q.DataDir, jobs: q.jobs}
	reloaded.loadAccountBlocks()
	if row := reloaded.AccountLimits()[0]; row.Status == "recovered" || !row.FirstDetectedAt.Equal(first.FirstDetectedAt) {
		t.Fatalf("lost persisted episode: %+v", row)
	}
	reloaded.clearAccountBlock("kimi", "acct_a")
	if row := reloaded.AccountLimits()[0]; row.Status != "recovered" || row.RecoveredAt.IsZero() {
		t.Fatalf("web success not recorded: %+v", row)
	}
	reloaded.blockAccount("kimi", "acct_a", "额度已用完")
	if row := reloaded.AccountLimits()[0]; row.Status != "blocked" || row.DetectionCount != 1 || !row.RecoveredAt.IsZero() {
		t.Fatalf("new episode not reset: %+v", row)
	}
}

func TestLegacyLimitMigrationUsesWebAttemptsAndStatedMute(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "server", "data", "jobs")
	q := &Queue{GeoRoot: root, DataDir: data, jobs: map[string]*Job{}}
	first := time.Now().Add(-time.Hour).Truncate(time.Second)
	end := time.Now().In(time.FixedZone("CST", 8*3600)).Add(24 * time.Hour).Truncate(time.Minute)
	quota := func(account, code, channel string, ok bool, at time.Time) map[string]any {
		return map[string]any{"account_id": account, "channel": channel, "ok": ok, "error_code": code, "started_at": at.Format(time.RFC3339Nano)}
	}
	q.jobs["one"] = &Job{Results: []map[string]any{{"site_id": "deepseek", "attempts": []map[string]any{
		quota("a", "platform_quota", "web", false, first), quota("a", "", "api", true, first.Add(time.Minute)),
		quota("b", "platform_quota", "web", false, first), quota("b", "", "web", true, first.Add(2*time.Minute)),
	}}}}
	if err := os.MkdirAll(filepath.Dir(q.protectionPath()), 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(accountProtectionState{BlockedUntil: map[string]time.Time{"deepseek/a": first.Add(30 * time.Minute)}, RecoveryOwner: map[string]string{"deepseek/a": "pending"}})
	if err := os.WriteFile(q.protectionPath(), raw, 0600); err != nil {
		t.Fatal(err)
	}
	q.jobs["pending"] = &Job{Status: "queued"}
	line := first.In(time.Local).Format("2006/01/02 15:04:05") + " scrape failed job=one site=deepseek account=a elapsed_ms=100: DeepSeek 账号已被禁言至 " + end.Format("2006-01-02 15:04") + "，暂停使用该账号\n"
	if err := os.WriteFile(filepath.Join(root, "server", "geo-server.out.log"), []byte(line), 0600); err != nil {
		t.Fatal(err)
	}
	q.loadAccountBlocks()
	rows := q.AccountLimits()
	if len(rows) != 2 || rows[0].Kind != "account_muted" || !rows[0].ResetAt.Equal(end) || !rows[0].FirstDetectedAt.Equal(first) || rows[0].Status != "blocked" || rows[1].Status != "recovered" {
		t.Fatalf("migration=%+v", rows)
	}
	if !q.accountBlocks["deepseek/a"].Equal(end) || q.recoveryOwners["deepseek/a"] != "pending" {
		t.Fatal("migration lost protection or target owner")
	}
}
