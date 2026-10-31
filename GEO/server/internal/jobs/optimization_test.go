package jobs

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestDeploymentHoldRestoresOnlyUndispatchedJobs(t *testing.T) {
	for _, name := range []string{"held", "wrong_marker", "expired", "allowed", "has_answer", "attempt_failed"} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			dir := filepath.Join(root, "jobs")
			if err := os.Mkdir(dir, 0o700); err != nil {
				t.Fatal(err)
			}
			deadline := time.Now().Add(time.Minute).Format(time.RFC3339Nano)
			job := Job{ID: "fixture", Status: "running", RequestID: "customer-request", CreatedAt: time.Now().Format(time.RFC3339Nano), DeadlineAt: deadline}
			if name == "has_answer" {
				job.Results = []map[string]any{{"ok": true, "answer": "accepted answer"}}
			}
			if name == "attempt_failed" {
				job.Results = []map[string]any{{"ok": false, "error_code": "site_timeout"}}
			}
			raw, _ := json.Marshal(job)
			if err := os.WriteFile(filepath.Join(dir, "fixture.json"), raw, 0o600); err != nil {
				t.Fatal(err)
			}
			expires := time.Now().Add(time.Minute).Unix()
			if name == "expired" {
				expires = time.Now().Add(-time.Minute).Unix()
			}
			allow := []string{}
			if name == "allowed" {
				allow = append(allow, "job_fixture")
			}
			raw, _ = json.Marshal(map[string]any{"expires_at": expires, "allow": allow})
			if err := os.WriteFile(filepath.Join(root, ".deployment-drain.json"), raw, 0o600); err != nil {
				t.Fatal(err)
			}
			questionID := "job_fixture"
			if name == "wrong_marker" {
				questionID = "job_other"
			}
			raw, _ = json.Marshal(map[string]string{"question_id": questionID})
			if err := os.WriteFile(filepath.Join(root, ".deployment-held-job_fixture.json"), raw, 0o600); err != nil {
				t.Fatal(err)
			}
			q := &Queue{DataDir: dir, jobs: map[string]*Job{}}
			_, queued, _ := q.load()
			if name == "held" {
				if len(queued) != 1 || q.jobs["fixture"].Status != "queued" {
					t.Fatal("held request was not restored")
				}
				if q.jobs["fixture"].RequestID != "customer-request" || q.jobs["fixture"].DeadlineAt != deadline {
					t.Fatal("request identity or deadline changed")
				}
			} else if len(queued) != 0 || q.jobs["fixture"].ErrorCode != "service_interrupted" {
				t.Fatal("a dispatched or unproven request would be replayed")
			}
		})
	}
}

func TestRiskCooldownSkipsBrowserAndSurvivesRestart(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "1")
	t.Setenv("GEO_RISK_COOLDOWN_SEC", "600")
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"API回答"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	dir := t.TempDir()
	script := filepath.Join(dir, "scrape.sh")
	count := filepath.Join(dir, "count")
	body := "#!/bin/sh\nprintf x >> '" + count + "'\nprintf '%s\\n' '{\"ok\":false,\"error_code\":\"platform_risk_control\",\"retryable\":false}'\n"
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	newQueue := func() *Queue {
		return &Queue{GeoRoot: dir, Python: "/bin/sh", Script: script, DataDir: dir, siteSem: make(chan struct{}, 6), cloudSem: make(chan struct{}, 3)}
	}
	q := newQueue()
	first := q.runOneSite("doubao", "问题", "first")
	if first["source"] != "official_api" || first["web_error_code"] != "platform_risk_control" {
		t.Fatalf("first result = %#v", first)
	}
	until := first["cooldown_until"]
	for _, queue := range []*Queue{q, newQueue()} {
		result := queue.runOneSite("doubao", "问题", "next")
		if result["source"] != "official_api" || result["web_skipped"] != true || result["cooldown_until"] != until {
			t.Fatalf("cooled result = %#v", result)
		}
	}
	raw, err := os.ReadFile(count)
	if err != nil || string(raw) != "x" {
		t.Fatalf("browser calls = %q, %v; want one", raw, err)
	}
	if q.siteCooldown("kimi", "kimi", "问题") != nil {
		t.Fatal("one platform cooled another")
	}
	state, _ := json.Marshal(siteCooldownState{Until: time.Now().Add(-time.Second), Code: "platform_risk_control"})
	if err := os.WriteFile(q.cooldownPath("doubao"), state, 0o600); err != nil {
		t.Fatal(err)
	}
	if q.siteCooldown("doubao", "doubao", "问题") != nil {
		t.Fatal("expired cooldown still blocks")
	}
}

func TestAPITimeoutStopsSearchAndPlainFallbackTogether(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_QIANWEN_API_KEY", "test-key")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		select {
		case <-r.Context().Done():
		case <-time.After(200 * time.Millisecond):
		}
	}))
	defer api.Close()
	t.Setenv("GEO_QIANWEN_API_BASE", api.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	_, _, _, err := askOfficialAPI(ctx, "qianwen", "问题")
	if err == nil || ctx.Err() == nil {
		t.Fatalf("expected overall deadline, got %v", err)
	}
	if calls.Load() != 1 {
		t.Fatalf("calls = %d, want no plain request after search timeout", calls.Load())
	}
}

func TestCloudPlatformsOverlapWithoutExceedingSlots(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	dir := t.TempDir()
	script := filepath.Join(dir, "scrape.py")
	events := filepath.Join(dir, "events")
	body := `import json, os, sys, time
site = sys.argv[sys.argv.index('--site')+1]
def emit(kind):
    fd = os.open('events', os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
    os.write(fd, (kind+' '+site+'\n').encode())
    os.close(fd)
emit('start')
time.sleep(0.15)
emit('end')
print(json.dumps({'ok': True, 'answer': 'answer'}))
`
	if err := os.WriteFile(script, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	q := &Queue{GeoRoot: dir, Python: "python3", Script: script, siteSem: make(chan struct{}, 6), cloudSem: make(chan struct{}, 3)}
	var wg sync.WaitGroup
	for _, site := range []string{"deepseek", "doubao", "kimi", "yuanbao", "yiyan", "qianwen"} {
		wg.Add(1)
		go func(site string) {
			defer wg.Done()
			if result := q.runOneSite(site, "问题", site); result["ok"] != true {
				t.Errorf("%s: %#v", site, result)
			}
		}(site)
	}
	wg.Wait()
	raw, err := os.ReadFile(events)
	if err != nil {
		t.Fatal(err)
	}
	active, peak := 0, 0
	for _, event := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if strings.HasPrefix(event, "start ") {
			active++
		} else {
			active--
		}
		if active > peak {
			peak = active
		}
		if active < 0 || active > 3 {
			t.Fatalf("unexpected concurrency %d: %s", active, raw)
		}
	}
	if active != 0 || peak != 3 {
		t.Fatalf("active=%d peak=%d, want 0 and 3: %s", active, peak, raw)
	}
}

func TestSixPlatformsStartTogetherAndSlowOneHasIndependentWebDeadline(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	t.Setenv("GEO_MULTI_WEB_BUDGET_SEC", "1")
	t.Setenv("GEO_SITE_FAILURE_RETRIES", "0")
	t.Setenv("GEO_KIMI_TIMEOUT_RETRIES", "0")
	t.Setenv("GEO_SITE_FAILURE_RETRY_SEC", "0")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	dir := t.TempDir()
	script := filepath.Join(dir, "scrape.py")
	// Each child waits until all six have started, proving the execution is
	// simultaneous rather than two batches of three. One platform then hangs.
	body := `import json, pathlib, sys, time
site = sys.argv[sys.argv.index('--site')+1]
pathlib.Path('started-'+site).touch()
while len(list(pathlib.Path('.').glob('started-*'))) < 6:
    time.sleep(0.005)
if site == 'doubao':
    time.sleep(10)
print(json.dumps({'ok': True, 'answer': 'answer'}))
`
	if err := os.WriteFile(script, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	sites := []string{"deepseek", "doubao", "kimi", "yuanbao", "yiyan", "qianwen"}
	job := &Job{ID: "six", Prompt: "问题", Sites: sites, Status: "queued", CreatedAt: time.Now().Format(time.RFC3339Nano), DeadlineAt: time.Now().Add(1200 * time.Millisecond).Format(time.RFC3339Nano), createdUnix: time.Now().UnixMilli()}
	q := &Queue{GeoRoot: dir, Python: "python3", Script: script, jobs: map[string]*Job{job.ID: job}, siteSem: make(chan struct{}, 6), cloudSem: make(chan struct{}, 6)}
	started := time.Now()
	q.run(job.ID)
	if time.Since(started) > 2800*time.Millisecond {
		t.Fatal("slow platform ran beyond its independent webpage budget")
	}
	got, _ := q.Get(job.ID)
	if got.Status != "partial" || len(got.Results) != 6 || got.CompletedSites != 6 {
		t.Fatalf("job = %#v", got)
	}
	for _, result := range got.Results {
		if result["site_id"] == "doubao" {
			if result["error_code"] != "site_timeout" {
				t.Fatalf("slow result = %#v", result)
			}
		} else if result["ok"] != true {
			t.Fatalf("fast result lost: %#v", result)
		}
	}
}

func TestExpiredBusySiteCannotStartFallback(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	gate := make(chan struct{}, 1)
	gate <- struct{}{}
	q := &Queue{siteGates: map[string]chan struct{}{"doubao": gate}, siteSem: make(chan struct{}, 6)}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	cancel()
	started := time.Now()
	result := q.runOneSiteContext(ctx, "doubao", "问题", "busy", 1)
	if time.Since(started) > 500*time.Millisecond || result["error_code"] != "site_timeout" {
		t.Fatalf("wait result = %#v", result)
	}
	if calls.Load() != 0 {
		t.Fatal("expired request started API fallback")
	}
}

func TestWebOnlyConfigurationNeverSubstitutesOfficialAPI(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_FALLBACK", "0")
	t.Setenv("GEO_KIMI_API_KEY", "test-key")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer api.Close()
	t.Setenv("GEO_KIMI_API_BASE", api.URL)
	failure := siteError("kimi", "问题", "platform_busy", "网页繁忙", true, 1)
	result := (&Queue{}).applyAPIFallback("kimi", "问题", failure)
	if result["ok"] != false || result["source"] == "official_api" || calls.Load() != 0 || officialAPIConfigured("kimi") {
		t.Fatalf("web-only request was substituted: %#v", result)
	}
}

func TestBusyDeepSeekWaitsPastOldAdmissionLimitThenExecutesWeb(t *testing.T) {
	q := attemptFixtureQueue(t, "import json\nprint(json.dumps({'ok':True,'answer':'web answer','answer_origin':'web'}))\n", "ready-account")
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	t.Setenv("GEO_DEEPSEEK_API_KEY", "test-key")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_BROWSER_ADMISSION_WAIT_SEC", "1")
	t.Setenv("GEO_API_RESERVE_SEC", "5")
	t.Setenv("GEO_DEEPSEEK_API_TIMEOUT_SEC", "5")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"API回答"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DEEPSEEK_API_BASE", api.URL)
	gate := make(chan struct{}, 1)
	gate <- struct{}{}
	q.siteGates = map[string]chan struct{}{"deepseek": gate}
	release := time.AfterFunc(1100*time.Millisecond, func() { <-gate })
	defer release.Stop()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	started := time.Now()
	result := q.runOneSiteContext(ctx, "deepseek", "问题", "busy-api", 1)
	elapsed := time.Since(started)
	if result["ok"] != true || result["answer_origin"] != "web" || calls.Load() != 0 || elapsed < time.Second || elapsed > 3*time.Second {
		t.Fatalf("browser wait displaced the webpage with API: %#v, elapsed=%s calls=%d", result, elapsed, calls.Load())
	}
	if len(gate) != 0 || len(q.siteSem) != 0 {
		t.Fatal("completed webpage leaked admission resources")
	}
}

func TestWebBudgetStartsAtExecutionAndRespectsExplicitCallerDeadline(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_KIMI_API_KEY", "test-key")
	t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "80")
	t.Setenv("GEO_API_RESERVE_SEC", "35")
	start := time.Now()
	ctx, cancel := context.WithDeadline(context.Background(), start.Add(120*time.Second))
	defer cancel()
	deadline := requestWebDeadline(ctx, "kimi", start.Add(180*time.Second), start, 1)
	if deadline.Sub(start) != 80*time.Second {
		t.Fatalf("web deadline = %s", deadline.Sub(start))
	}
	// Explicit caller limits remain meaningful, without subtracting an API
	// reserve. Normal asynchronous workers do not carry an acceptance deadline.
	late := requestWebDeadline(ctx, "kimi", start.Add(300*time.Second), start.Add(100*time.Second), 1)
	if late != start.Add(120*time.Second) {
		t.Fatal("web deadline ignored the caller or still subtracted API reserve")
	}
	fresh := requestWebDeadline(context.Background(), "kimi", start.Add(300*time.Second), start.Add(100*time.Second), 1)
	if fresh != start.Add(180*time.Second) {
		t.Fatal("admission wait shortened a fresh webpage execution budget")
	}
}

func TestQueuedJobWithExpiredLegacyDeadlineStillExecutesWeb(t *testing.T) {
	q := attemptFixtureQueue(t, "import json\nprint(json.dumps({'ok':True,'answer':'web answer','answer_origin':'web'}))\n", "ready-account")
	created := time.Now().Add(-time.Minute)
	job := &Job{ID: "queued-past-estimate", Status: "queued", Prompt: "question", Sites: []string{"doubao"}, CreatedAt: created.Format(time.RFC3339Nano), DeadlineAt: created.Add(30 * time.Second).Format(time.RFC3339Nano), createdUnix: created.UnixMilli()}
	q.jobs = map[string]*Job{job.ID: job}
	q.run(job.ID)
	got, _ := q.Get(job.ID)
	if got.Status != "succeeded" || len(got.Results) != 1 || got.Results[0]["answer_origin"] != "web" || got.QueuedMS < 60000 || got.CreatedAt != job.CreatedAt {
		t.Fatalf("queue age displaced the webpage or lost original latency: %+v", got)
	}
}

func TestFallbackCannotRestartItsBudgetAfterWebpageDeadline(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		select {
		case <-r.Context().Done():
		case <-time.After(200 * time.Millisecond):
		}
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	started := time.Now()
	result := (&Queue{}).applyAPIFallbackContext(ctx, "doubao", "问题", siteError("doubao", "问题", "site_timeout", "网页超时", true, 80))
	if time.Since(started) > 500*time.Millisecond || result["api_skipped"] != true || result["api_error_code"] != "insufficient_api_budget" || result["api_fallback_failed"] == true {
		t.Fatalf("API exceeded request deadline: %#v", result)
	}
	if calls.Load() != 0 {
		t.Fatalf("API calls = %d", calls.Load())
	}
}

func TestSixSeparateSinglePlatformJobsOverlapThroughWorkers(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_QUEUE_WORKERS", "6")
	t.Setenv("GEO_SITE_CONCURRENCY", "6")
	t.Setenv("GEO_AGENTBAY_CONCURRENCY", "6")
	t.Setenv("GEO_SINGLE_TOTAL_TIMEOUT_SEC", "3")
	dir := t.TempDir()
	script := filepath.Join(dir, "server", "scripts", "run_scrape.py")
	if err := os.MkdirAll(filepath.Dir(script), 0o700); err != nil {
		t.Fatal(err)
	}
	body := `import json, pathlib, sys, time
site = sys.argv[sys.argv.index('--site')+1]
pathlib.Path('started-'+site).touch()
deadline=time.monotonic()+2
while len(list(pathlib.Path('.').glob('started-*'))) < 6:
    if time.monotonic()>deadline:
        raise RuntimeError('single-platform jobs did not overlap')
    time.sleep(0.005)
print(json.dumps({'ok': True, 'answer': 'answer'}))
`
	if err := os.WriteFile(script, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	q := New(dir, nil, nil, filepath.Join(dir, "jobs"))
	var ids []string
	for _, site := range []string{"deepseek", "doubao", "kimi", "yuanbao", "yiyan", "qianwen"} {
		job, _, err := q.Submit("问题", []string{site}, "", "", "")
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, job.ID)
	}
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		allDone := true
		for _, id := range ids {
			job, _ := q.Get(id)
			if job.Status == "failed" || job.Status == "partial" {
				t.Fatalf("single job did not succeed: %#v", job)
			}
			if job.Status != "succeeded" {
				allDone = false
			}
		}
		if allDone {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("six single-platform requests did not all complete")
}

func TestAPISlotWaitRespectsOverallRequestDeadline(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
	q := &Queue{apiSem: make(chan struct{}, 1)}
	q.apiSem <- struct{}{}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	started := time.Now()
	got := q.applyAPIFallbackContext(ctx, "doubao", "问题", siteError("doubao", "问题", "platform_busy", "网页繁忙", true, 0))
	if time.Since(started) > 500*time.Millisecond || got["api_skipped"] != true || got["api_error_code"] != "insufficient_api_budget" || got["api_fallback_failed"] == true {
		t.Fatalf("API slot wait = %#v", got)
	}
}

func TestAPITimeoutSeparatesWebAuthenticationFailure(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DEEPSEEK_API_KEY", "test-key")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_API_MIN_EXECUTION_SEC", "1")
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
		}
	}))
	defer api.Close()
	t.Setenv("GEO_DEEPSEEK_API_BASE", api.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 1100*time.Millisecond)
	defer cancel()
	q := &Queue{apiSem: make(chan struct{}, 6)}
	result := q.applyAPIFallbackContext(ctx, "deepseek", "问题", siteError("deepseek", "问题", "platform_auth_unavailable", "网页登录失效", true, 1))
	if result["error_code"] != "platform_auth_unavailable" || result["api_error_code"] != "api_timeout" || result["api_fallback_failed"] != true {
		t.Fatalf("two failure stages were conflated: %#v", result)
	}
}

func TestKilledScraperKeepsLastCheckpointAndTerminationReason(t *testing.T) {
	dir := t.TempDir()
	q := &Queue{GeoRoot: dir, Python: "/bin/sh", Script: filepath.Join(dir, "scrape.sh"), DataDir: filepath.Join(dir, "jobs")}
	body := `#!/bin/sh
printf '%s' '{"phase":"02_wait","status":"running"}' > "$GEO_DIAGNOSTIC_RUN_DIR/manifest.json"
printf '%s' '<html>last checkpoint</html>' > "$GEO_DIAGNOSTIC_RUN_DIR/frame-0.html"
sleep 5
`
	if err := os.WriteFile(q.Script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	_, err := q.scrapeForContext(context.Background(), "http://127.0.0.1:9", "doubao", "private prompt", "timeout-diagnostic", "", 100*time.Millisecond)
	if err == nil {
		t.Fatal("scraper did not time out")
	}
	result := siteError("doubao", "private prompt", "site_timeout", "timeout", true, 100)
	q.attachDiagnostic(result, "doubao", "timeout-diagnostic")
	id := stringField(result, "diagnostic_id")
	if id == "" {
		t.Fatal("diagnostic reference missing")
	}
	root := filepath.Join(q.diagnosticRoot(), id)
	raw, err := os.ReadFile(filepath.Join(root, "process.json"))
	if err != nil {
		t.Fatal(err)
	}
	var process map[string]any
	if err := json.Unmarshal(raw, &process); err != nil {
		t.Fatal(err)
	}
	if process["status"] != "terminated" || process["error_code"] != "generation_timeout" || strings.Contains(string(raw), "private prompt") {
		t.Fatalf("termination evidence = %#v", process)
	}
	if _, err := os.Stat(filepath.Join(root, "frame-0.html")); err != nil {
		t.Fatal("last checkpoint was lost")
	}
	q.recordAPIDiagnostic(result, time.Now(), nil)
	if _, err := os.Stat(filepath.Join(root, "api-fallback.json")); err != nil {
		t.Fatal(err)
	}
}
