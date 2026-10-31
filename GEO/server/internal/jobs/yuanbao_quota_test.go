package jobs

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

const yuanbaoQuotaFixture = "此模型的本月用量已用完，次月1日零点刷新，可切换至专家模式，使用Hy3模型继续对话。"

func TestYuanbaoQuotaReplySwitchesAccount(t *testing.T) {
	for _, exhausted := range []bool{false, true} {
		t.Run(fmt.Sprint("all_exhausted=", exhausted), func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_API_FALLBACK", "0")
			t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
			t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
			root := t.TempDir()
			pool := &recoveryPool{}
			for _, id := range []string{"first", "second"} {
				profile := filepath.Join(root, id)
				if err := os.Mkdir(profile, 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte("12345\n"), 0600); err != nil {
					t.Fatal(err)
				}
				manager := sessions.New("", profile, "")
				manager.AcquireSite("")
				pool.leases = append(pool.leases, &sessions.Lease{AccountID: id, Phone: id, Manager: manager})
			}
			script := filepath.Join(root, "scrape.py")
			body := fmt.Sprintf(`import json,sys
phone=sys.argv[sys.argv.index('--phone')+1]
with open('attempts','a',encoding='utf-8') as f:f.write(phone+'\n')
answer=%q if phone=='first' or %s else 'answer from second account'
print(json.dumps({'ok':True,'answer':answer,'source':'api:conversation_detail','answer_origin':'web'}))
`, yuanbaoQuotaFixture, map[bool]string{false: "False", true: "True"}[exhausted])
			if err := os.WriteFile(script, []byte(body), 0600); err != nil {
				t.Fatal(err)
			}
			q := &Queue{GeoRoot: root, Python: "python3", Script: script, Sess: pool, siteSem: make(chan struct{}, 2)}
			result := q.runOneSite("yuanbao", "current question", "fixture")
			attempts, _ := os.ReadFile(filepath.Join(root, "attempts"))
			if string(attempts) != "first\nsecond\n" || result["account_retries"] != 1 || pool.loginCalls != 0 {
				t.Fatalf("did not switch directly to the second saved login: result=%v attempts=%q loginCalls=%d", result, attempts, pool.loginCalls)
			}
			if exhausted {
				if result["ok"] != false || result["error_code"] != "platform_quota" || result["answer"] != nil {
					t.Fatalf("quota reply returned as an answer: %v", result)
				}
			} else if result["ok"] != true || result["account_id"] != "second" || result["answer"] != "answer from second account" {
				t.Fatalf("second account answer was lost: %v", result)
			}
		})
	}
}

func TestYuanbaoQuotaCheckpointCannotBecomeSuccessfulAnswer(t *testing.T) {
	root := t.TempDir()
	script := filepath.Join(root, "scrape.py")
	body := fmt.Sprintf(`import json,time
print(json.dumps({'web_answer_completed':True,'ok':True,'site_id':'yuanbao','prompt':'current question','question_id':'job_fixture','answer':%q,'answer_origin':'web','source':'api:conversation_detail'}),flush=True)
time.sleep(30)
`, yuanbaoQuotaFixture)
	if err := os.WriteFile(script, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	q := &Queue{GeoRoot: root, Python: "python3", Script: script}
	result, err := q.scrapeForContext(context.Background(), "http://127.0.0.1:9", "yuanbao", "current question", "fixture", "", 300*time.Millisecond)
	if err == nil {
		t.Fatalf("quota checkpoint returned as success: %v", result)
	}
	code, _, retry := publicScrapeError(err)
	if result != nil || code != "platform_quota" || retry {
		t.Fatalf("wrong quota classification: result=%v err=%v retry=%v", result, err, retry)
	}
}
