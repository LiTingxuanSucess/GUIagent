package jobs

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestBlockedAccountUsesStatedEndTimeAndExpires(t *testing.T) {
	q := &Queue{}
	end := time.Now().In(time.FixedZone("CST", 8*3600)).Add(2 * time.Hour).Truncate(time.Minute)
	q.blockAccount("deepseek", "muted", "DeepSeek 账号已被禁言至 "+end.Format("2006-01-02 15:04")+"，暂停使用该账号")
	if got := q.accountBlocks["deepseek/muted"]; !got.Equal(end) {
		t.Fatalf("mute end time not used: %s want %s", got, end)
	}
	q.blockAccount("yuanbao", "quota", "元宝此模型的本月用量已用完，次月1日零点刷新")
	now := time.Now().In(end.Location())
	nextMonth := time.Date(now.Year(), now.Month()+1, 1, 0, 0, 0, 0, now.Location())
	if got := q.accountBlocks["yuanbao/quota"]; !got.Equal(nextMonth) {
		t.Fatalf("monthly quota reset = %s want %s", got, nextMonth)
	}
	if ids := q.blockedAccounts("deepseek"); len(ids) != 1 || ids[0] != "muted" {
		t.Fatalf("blocked deepseek accounts = %v", ids)
	}
	q.accountBlocks["deepseek/muted"] = time.Now().Add(-time.Second)
	if ids := q.blockedAccounts("deepseek"); len(ids) != 0 {
		t.Fatalf("expired block still applied: %v", ids)
	}
}

func TestMutedAccountIsSkippedByLaterQuestions(t *testing.T) {
	end := time.Now().In(time.FixedZone("CST", 8*3600)).Add(time.Hour).Format("2006-01-02 15:04")
	q := attemptFixtureQueue(t, `import json,pathlib,sys
a=sys.argv; phone=a[a.index('--phone')+1]
with pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(phone+'\n')
if phone=='first':print(json.dumps({'ok':False,'error_code':'platform_quota','error':'DeepSeek 账号已被禁言至 `+end+`，暂停使用该账号','retryable':False},ensure_ascii=False))
else:print(json.dumps({'ok':True,'answer':'second account answer','source':'web'}))
`, "first", "second")
	for i, want := range []string{"first\nsecond\n", "first\nsecond\nsecond\n"} {
		one := q.runOneSiteContext(context.Background(), "deepseek", "question", "blocked-fixture", 1)
		events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
		if one["ok"] != true || one["account_id"] != "second" || string(events) != want {
			t.Fatalf("round %d: result=%v events=%q", i, one, events)
		}
	}
}
