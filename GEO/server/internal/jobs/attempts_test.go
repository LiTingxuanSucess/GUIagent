package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

func attemptFixtureQueue(t *testing.T, body string, accountIDs ...string) *Queue {
	t.Helper()
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
	t.Setenv("GEO_CURSOR_CAPTCHA_AGENT", "0")
	t.Setenv("GEO_SITE_FAILURE_RETRY_SEC", "0")
	root := t.TempDir()
	pool := &recoveryPool{}
	for _, id := range accountIDs {
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
	script := filepath.Join(root, "fixture.py")
	if err := os.WriteFile(script, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	return &Queue{GeoRoot: root, Python: "python3", Script: script, Sess: pool, siteSem: make(chan struct{}, 2)}
}

func TestCrashAttemptRetainsReasonAndPrivateMemoryEvidence(t *testing.T) {
	q := attemptFixtureQueue(t, `import json
print(json.dumps({'ok':False,'error':'Page crashed','error_code':'site_request_failed','retryable':False}))
`, "first")
	q.DataDir = t.TempDir()
	ctx, collector := newAttemptContext(context.Background())
	ctx = withAttemptAccount(ctx, "first")
	_, err := q.scrapeForContext(ctx, "http://127.0.0.1:1", "kimi", "private question", "crash-fixture", "private phone", time.Second)
	if err == nil {
		t.Fatal("fixture did not fail")
	}
	if len(collector.entries) != 1 || collector.entries[0]["failure_reason"] != "browser_page_crashed" || collector.entries[0]["error_code"] != "site_request_failed" {
		t.Fatalf("lost crash cause: %+v", collector.entries)
	}
	id := stringField(collector.entries[0], "diagnostic_id")
	raw, readErr := os.ReadFile(filepath.Join(q.diagnosticRoot(), id, "process.json"))
	if readErr != nil {
		t.Fatal(readErr)
	}
	var process map[string]any
	if json.Unmarshal(raw, &process) != nil || process["failure_reason"] != "browser_page_crashed" || process["process_exit_code"] != float64(0) {
		t.Fatalf("invalid process evidence: %s", raw)
	}
	for _, secret := range []string{"private question", "private phone", "http://127.0.0.1:1"} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("private data leaked: %s", raw)
		}
	}
}

func resultAttempts(t *testing.T, result map[string]any) []map[string]any {
	t.Helper()
	if _, exists := result["attempts"]; !exists {
		t.Fatalf("missing attempt history: %v", result)
	}
	entries := cloneAttemptList(result["attempts"])
	for _, entry := range entries {
		if _, err := time.Parse(time.RFC3339Nano, stringField(entry, "started_at")); err != nil {
			t.Fatalf("invalid attempt timestamp: %v", entry)
		}
		if _, exists := entry["elapsed_ms"]; !exists {
			t.Fatalf("missing attempt duration: %v", entry)
		}
	}
	return entries
}

func TestAttemptHistoryRetainsFailedAccountBeforeSuccessfulFailover(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,sys
phone=sys.argv[sys.argv.index('--phone')+1]
if phone=='first':
 print(json.dumps({'ok':True,'answer':`+fmt.Sprintf("%q", yuanbaoQuotaFixture)+`,'answer_origin':'web'}))
else:
 print(json.dumps({'ok':True,'answer':'completed answer','answer_origin':'web'}))
`, "first", "second")
	result := q.runOneSite("yuanbao", "question", "failover")
	entries := resultAttempts(t, result)
	if result["ok"] != true || len(entries) != 2 || entries[0]["channel"] != "web" || entries[0]["account_id"] != "first" || entries[0]["ok"] != false || entries[0]["error_code"] != "platform_quota" || entries[1]["account_id"] != "second" || entries[1]["ok"] != true || entries[1]["error_code"] != "" {
		t.Fatalf("account failover lost or misclassified an attempt: %v", result)
	}
}

func TestAttemptHistoryRetainsSameAccountRetries(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,pathlib
state=pathlib.Path('retried')
if not state.exists():
 state.write_text('yes',encoding='utf-8')
 print(json.dumps({'ok':False,'error_code':'platform_busy','error':'busy','retryable':True}))
else:
 print(json.dumps({'ok':True,'answer':'completed answer','answer_origin':'web'}))
`, "same-account")
	result := q.runOneSite("doubao", "question", "retry")
	entries := resultAttempts(t, result)
	if result["ok"] != true || result["platform_retries"] != 1 || len(entries) != 2 || entries[0]["account_id"] != "same-account" || entries[1]["account_id"] != "same-account" || entries[0]["ok"] != false || entries[0]["error_code"] != "platform_busy" || entries[1]["ok"] != true {
		t.Fatalf("same-account retry lost its original failure: %v", result)
	}
}

func TestAttemptHistoryPersistsWebFailureAndAPISuccess(t *testing.T) {
	q := attemptFixtureQueue(t, `import json
print(json.dumps({'ok':False,'error_code':'platform_risk_control','error':'captcha','retryable':False}))
`, "blocked-account")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_DOUBAO_API_KEY", "never-persist-this-key")
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"choices":[{"message":{"content":"API answer"}}]}`)
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	result := q.runOneSite("doubao", "question", "web-api")
	entries := resultAttempts(t, result)
	if result["ok"] != true || result["source"] != "official_api" || len(entries) != 2 || entries[0]["account_id"] != "blocked-account" || entries[0]["ok"] != false || entries[0]["error_code"] != "platform_risk_control" || entries[1]["channel"] != "api" || entries[1]["account_id"] != "api:doubao" || entries[1]["ok"] != true {
		t.Fatalf("API answer masked the account failure: %v", result)
	}
	q.DataDir = t.TempDir()
	job := &Job{ID: "persisted", Results: []map[string]any{result}}
	q.save(job)
	raw, err := os.ReadFile(filepath.Join(q.DataDir, "persisted.json"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "never-persist-this-key") {
		t.Fatal("the API credential was persisted with attempt evidence")
	}
	var stored Job
	if err := json.Unmarshal(raw, &stored); err != nil || len(stored.Results) != 1 || len(resultAttempts(t, stored.Results[0])) != 2 {
		t.Fatalf("persisted attempt evidence was lost: %v %s", err, raw)
	}
	public := copyJob(job)
	stripInternalResults(public)
	if public.Results[0]["attempts"] != nil || len(resultAttempts(t, job.Results[0])) != 2 {
		t.Fatal("public filtering leaked account attempts or mutated the stored job")
	}
}

func TestAttemptHistoryPreservesWebSuccessDuringSearchRecovery(t *testing.T) {
	for _, apiSucceeds := range []bool{true, false} {
		t.Run(fmt.Sprint(apiSucceeds), func(t *testing.T) {
			q := attemptFixtureQueue(t, `import json
print(json.dumps({'ok':True,'answer':'web answer','answer_origin':'web','web_search_status':'not_confirmed'}))
`, "web-account")
			t.Setenv("GEO_API_ENABLE_SEARCH", "1")
			t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				t.Error("completed webpage answer must not trigger an API supplement")
				if !apiSucceeds {
					w.WriteHeader(403)
					fmt.Fprint(w, `{"error":{"message":"account has an overdue balance"}}`)
					return
				}
				fmt.Fprint(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"searched API answer","annotations":[{"type":"url_citation","url":"https://example.org/source"}]}]}]}`)
			}))
			defer api.Close()
			t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
			result := q.runOneSite("doubao", "question", "search-recovery")
			entries := resultAttempts(t, result)
			if result["ok"] != true || len(entries) != 1 || entries[0]["channel"] != "web" || entries[0]["account_id"] != "web-account" || entries[0]["ok"] != true || entries[0]["error_code"] != "" {
				t.Fatalf("search recovery rewrote the successful web stage: %v", result)
			}
			if result["answer"] != "web answer" || result["web_search_error_code"] != "search_not_confirmed" {
				t.Fatal("quality handling discarded the completed webpage answer or missing-search evidence")
			}
		})
	}
}

func TestAttemptHistoryMarksUnstartedStagesAsSkipped(t *testing.T) {
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	for _, reason := range []string{"no_logged_in_account", "browser_admission", "site_cooldown"} {
		t.Run(reason, func(t *testing.T) {
			q := attemptFixtureQueue(t, "raise AssertionError('browser must not start')\n")
			if reason == "browser_admission" {
				q.siteSem <- struct{}{}
				q.siteSem <- struct{}{}
			}
			if reason == "site_cooldown" {
				t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "1")
				q.DataDir = t.TempDir()
				q.recordSiteCooldown("doubao", siteError("doubao", "question", "platform_risk_control", "captcha", false, 1))
			}
			ctx, cancel := context.WithTimeout(context.Background(), 1100*time.Millisecond)
			defer cancel()
			result := q.runOneSiteContext(ctx, "doubao", "question", reason, 1)
			entries := resultAttempts(t, result)
			want := reason
			if reason == "browser_admission" {
				want = "request_deadline_exceeded"
			}
			if len(entries) != 1 || entries[0]["channel"] != "web" || entries[0]["skipped"] != true || entries[0]["skip_reason"] != want || result["web_skipped"] != true {
				t.Fatalf("unstarted browser was counted as an attempt: %v", result)
			}
		})
	}
}

func TestAttemptHistoryRetainsFailureWhenAPIAdmissionIsSkipped(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	q := &Queue{apiSem: make(chan struct{}, 1)}
	q.apiSem <- struct{}{}
	ctx, attempts := newAttemptContext(context.Background())
	attempts.record("web", "failed-account", false, "platform_quota", time.Now())
	ctx, cancel := context.WithTimeout(ctx, 20*time.Millisecond)
	defer cancel()
	result := q.applyAPIFallbackContext(ctx, "doubao", "question", siteError("doubao", "question", "platform_quota", "quota", false, 1))
	entries := resultAttempts(t, result)
	if len(entries) != 2 || entries[0]["skipped"] == true || entries[1]["skipped"] != true || entries[1]["skip_reason"] != "insufficient_api_budget" || entries[1]["account_id"] != "api:doubao" {
		t.Fatalf("API admission hid the previous failure or became a failed API call: %v", result)
	}
}

func TestAPIReplacementRetainsDecodedAttemptHistory(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"choices":[{"message":{"content":"API answer"}}]}`)
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	var failure map[string]any
	if err := json.Unmarshal([]byte(`{"ok":false,"error_code":"platform_quota","attempts":[{"channel":"web","account_id":"original-account","ok":false,"error_code":"platform_quota","started_at":"2026-10-16T04:00:00Z","elapsed_ms":100}]}`), &failure); err != nil {
		t.Fatal(err)
	}
	result := (&Queue{}).applyAPIFallbackContext(context.Background(), "doubao", "question", failure)
	entries := resultAttempts(t, result)
	if result["ok"] != true || len(entries) != 2 || entries[0]["account_id"] != "original-account" || entries[0]["ok"] != false || entries[1]["account_id"] != "api:doubao" || entries[1]["ok"] != true {
		t.Fatalf("API replacement lost an existing decoded history: %v", result)
	}
	if len(resultAttempts(t, failure)) != 1 {
		t.Fatal("replacing a result mutated the original attempt history")
	}
}

func TestSearchRecoveryRetainsHistoryWhenKeepingOriginalWebResult(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "1")
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"API answer without search evidence"}]}]}`)
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	web := completedUnconfirmedWebFixture()
	_, previous := newAttemptContext(context.Background())
	previous.record("web", "fixture-account", true, "", time.Now())
	previous.attach(web)
	result := (&Queue{}).recoverWebSearchContext(context.Background(), "doubao", "original question", web)
	entries := resultAttempts(t, result)
	if result["answer"] != web["answer"] || len(entries) != 1 || entries[0]["channel"] != "web" || entries[0]["ok"] != true || len(resultAttempts(t, web)) != 1 {
		t.Fatalf("keeping the web answer changed its real attempt history: %v", result)
	}
}

func TestAttemptCollectorsAreIsolatedBoundedAndSnapshotSafe(t *testing.T) {
	parent, _ := newAttemptContext(context.Background())
	_, first := newAttemptContext(parent)
	_, second := newAttemptContext(parent)
	var workers sync.WaitGroup
	for _, collector := range []*attemptCollector{first, second} {
		workers.Add(1)
		go func(c *attemptCollector) {
			defer workers.Done()
			for i := 0; i <= maxResultAttempts; i++ {
				c.record("web", fmt.Sprint(c == first), true, "", time.Now())
			}
		}(collector)
	}
	workers.Wait()
	for index, collector := range []*attemptCollector{first, second} {
		result := map[string]any{}
		collector.attach(result)
		entries := resultAttempts(t, result)
		if len(entries) != maxResultAttempts || result["attempts_truncated"] != true || result["attempts_dropped"] != 1 || entries[0]["account_id"] != fmt.Sprint(index == 0) {
			t.Fatal("collectors shared entries or exceeded the persistence bound")
		}
		entries = result["attempts"].([]map[string]any)
		entries[0]["account_id"] = "changed-copy"
		collector.attach(result)
		if resultAttempts(t, result)[0]["account_id"] == "changed-copy" {
			t.Fatal("a result snapshot mutated the live collector")
		}
	}
}
