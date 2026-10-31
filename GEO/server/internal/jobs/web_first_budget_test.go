package jobs

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func recordedWebBudgets(t *testing.T, q *Queue) []int {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(q.GeoRoot, "budgets-ms"))
	if err != nil {
		t.Fatal(err)
	}
	var budgets []int
	for _, line := range strings.Fields(string(raw)) {
		value, err := strconv.Atoi(line)
		if err != nil || value < 750 || value > 1000 {
			t.Fatalf("attempt did not receive a fresh one second window: %q (%v)", raw, err)
		}
		budgets = append(budgets, value)
	}
	return budgets
}

func TestWebRetryRenewsExpiredPriorDeadlineAndRemainsFinite(t *testing.T) {
	for _, succeeds := range []bool{true, false} {
		name := "bounded_failure"
		if succeeds {
			name = "retry_success"
		}
		t.Run(name, func(t *testing.T) {
			body := `import json,os,pathlib,time
state=pathlib.Path('budgets-ms')
count=len(state.read_text(encoding='utf-8').splitlines()) if state.exists() else 0
with state.open('a',encoding='utf-8') as f:
 f.write(str(int(os.environ['GEO_DIAGNOSTIC_DEADLINE_MS'])-int(time.time()*1000))+'\n')
if count==0:
 time.sleep(0.65)
`
			if succeeds {
				body += "print(json.dumps({'ok':False,'error_code':'platform_busy','retryable':True}) if count==0 else json.dumps({'ok':True,'answer':'completed retry answer'}))\n"
			} else {
				body += "print(json.dumps({'ok':False,'error_code':'platform_busy','retryable':True}))\n"
			}
			q := attemptFixtureQueue(t, body, "same-account")
			t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "1")
			t.Setenv("GEO_SITE_FAILURE_RETRIES", "2")
			parent, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			parent = context.WithValue(parent, webExecutionBudgetKey{}, 1)
			started := time.Now()
			// Renewal must happen before checking this already expired previous
			// estimate, both on the first attempt and on later retries.
			result, err, retries := q.scrapeWithRetriesContext(parent, "http://127.0.0.1:9", "doubao", "question", name, "same-account", nil, time.Now().Add(-time.Second))
			wantAttempts := 3
			if succeeds {
				wantAttempts = 2
				if err != nil || result["ok"] != true {
					t.Fatalf("retry lost its fresh execution budget: result=%v err=%v", result, err)
				}
			} else if err == nil {
				t.Fatal("repeated failure became success")
			}
			budgets := recordedWebBudgets(t, q)
			if len(budgets) != wantAttempts || retries != wantAttempts-1 || time.Since(started) > 2*time.Second {
				t.Fatalf("retry was starved or unbounded: budgets=%v retries=%d elapsed=%s", budgets, retries, time.Since(started))
			}
		})
	}
}

func TestAccountFailoverReceivesFreshBudgetAndNeverRevisitsAccount(t *testing.T) {
	for _, succeeds := range []bool{true, false} {
		name := "all_accounts_fail"
		if succeeds {
			name = "second_account_succeeds"
		}
		t.Run(name, func(t *testing.T) {
			body := `import json,os,pathlib,sys,time
phone=sys.argv[sys.argv.index('--phone')+1]
with pathlib.Path('budgets-ms').open('a',encoding='utf-8') as f:
 f.write(str(int(os.environ['GEO_DIAGNOSTIC_DEADLINE_MS'])-int(time.time()*1000))+'\n')
if phone=='first':
 time.sleep(0.65)
`
			if succeeds {
				body += "print(json.dumps({'ok':False,'error_code':'platform_quota','retryable':False}) if phone=='first' else json.dumps({'ok':True,'answer':'completed failover answer'}))\n"
			} else {
				body += "print(json.dumps({'ok':False,'error_code':'platform_quota','retryable':False}))\n"
			}
			q := attemptFixtureQueue(t, body, "first", "second", "third")
			t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "1")
			parent, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			started := time.Now()
			result := q.runOneSiteContext(parent, "doubao", "question", name, 1)
			attempts := resultAttempts(t, result)
			budgets := recordedWebBudgets(t, q)
			wantAttempts := 3
			if succeeds {
				wantAttempts = 2
			}
			if result["ok"] != succeeds || len(attempts) != wantAttempts || len(budgets) != wantAttempts || time.Since(started) > 2*time.Second {
				t.Fatalf("account failover was starved or unbounded: result=%v budgets=%v", result, budgets)
			}
			seen := map[string]bool{}
			for _, attempt := range attempts {
				account := stringField(attempt, "account_id")
				if account == "" || seen[account] || attempt["skipped"] == true {
					t.Fatalf("failover retried an exhausted account or skipped a ready one: %v", attempts)
				}
				seen[account] = true
			}
			if len(q.siteSem) != 0 {
				t.Fatal("account failover leaked its browser slot")
			}
		})
	}
}
