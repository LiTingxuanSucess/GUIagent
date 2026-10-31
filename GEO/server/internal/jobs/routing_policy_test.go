package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRoutingPolicyPersistConflictAndFailureDoNotChangeLiveMode(t *testing.T) {
	q := &Queue{DataDir: t.TempDir()}
	initial, err := q.RoutingPolicy()
	if err != nil || !initial.WebFirst {
		t.Fatal(initial, err)
	}
	off, err := q.SetRoutingPolicy(false, initial.Revision)
	if err != nil || off.Mode != RoutingAPIOnly {
		t.Fatal(off, err)
	}
	reloaded := &Queue{DataDir: q.DataDir}
	reloaded.loadRoutingPolicy()
	state, err := reloaded.RoutingPolicy()
	if err != nil || !reflect.DeepEqual(state, off) {
		t.Fatalf("lost state: %+v %v", state, err)
	}
	if _, err := q.SetRoutingPolicy(true, initial.Revision); !errors.Is(err, ErrRoutingConflict) {
		t.Fatalf("stale write accepted: %v", err)
	}
	// A path occupied by a directory makes atomic replacement fail even as root.
	if err := os.Remove(q.routingPolicyPath()); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(q.routingPolicyPath(), 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := q.SetRoutingPolicy(true, off.Revision); err == nil {
		t.Fatal("failed persist reported success")
	}
	state, _ = q.RoutingPolicy()
	if !reflect.DeepEqual(state, off) {
		t.Fatal("failed persist changed live routing")
	}
}

func TestLegacyRoutingMigrationAndPlatformIsolation(t *testing.T) {
	for _, initial := range []bool{true, false} {
		t.Run(fmt.Sprint(initial), func(t *testing.T) {
			q := &Queue{DataDir: t.TempDir()}
			if err := os.MkdirAll(filepath.Dir(q.routingPolicyPath()), 0700); err != nil {
				t.Fatal(err)
			}
			raw := []byte(fmt.Sprintf(`{"web_first":%t,"revision":7,"updated_at":"legacy"}`, initial))
			if err := os.WriteFile(q.routingPolicyPath(), raw, 0600); err != nil {
				t.Fatal(err)
			}
			q.loadRoutingPolicy()
			policy, err := q.RoutingPolicy()
			if err != nil || len(policy.Sites) != 6 || policy.Revision != 7 {
				t.Fatal(policy, err)
			}
			for site, value := range policy.Sites {
				if value.WebFirst != initial {
					t.Fatalf("legacy setting changed for %s", site)
				}
			}
			after, _ := os.ReadFile(q.routingPolicyPath())
			if string(raw) != string(after) {
				t.Fatal("reading migration rewrote persisted policy")
			}
			changed, err := q.SetSiteRoutingPolicy("deepseek", !initial, policy.Revision)
			if err != nil || changed.Mode != RoutingMixed || changed.Revision != 8 {
				t.Fatal(changed, err)
			}
			for site, value := range changed.Sites {
				want := initial
				if site == "deepseek" {
					want = !initial
				}
				if value.WebFirst != want {
					t.Fatalf("platform toggle changed %s", site)
				}
			}
			reloaded := &Queue{DataDir: q.DataDir}
			reloaded.loadRoutingPolicy()
			got, err := reloaded.RoutingPolicy()
			if err != nil || !reflect.DeepEqual(got, changed) {
				t.Fatal("platform routing lost after restart", got, err)
			}
			// Returned configuration is detached from the store.
			got.Sites["doubao"] = siteRouting(!initial)
			fresh, _ := reloaded.RoutingPolicy()
			if fresh.Sites["doubao"].WebFirst != initial {
				t.Fatal("caller mutated live configuration")
			}
			if _, err := q.SetSiteRoutingPolicy("kimi", !initial, 7); !errors.Is(err, ErrRoutingConflict) {
				t.Fatal("stale platform write accepted", err)
			}
			if err := os.Remove(q.routingPolicyPath()); err != nil {
				t.Fatal(err)
			}
			if err := os.Mkdir(q.routingPolicyPath(), 0700); err != nil {
				t.Fatal(err)
			}
			if _, err := q.SetSiteRoutingPolicy("doubao", !initial, 8); err == nil {
				t.Fatal("failed platform persistence accepted")
			}
			fresh, _ = q.RoutingPolicy()
			if !reflect.DeepEqual(fresh, changed) {
				t.Fatal("disk failure changed routing")
			}
		})
	}
}

func TestPlatformSnapshotsPreserveMixedJobsAliasesAndOldJobs(t *testing.T) {
	q := &Queue{DataDir: t.TempDir(), jobs: map[string]*Job{}}
	if _, err := q.SetSiteRoutingPolicy("deepseek", false, 0); err != nil {
		t.Fatal(err)
	}
	job, _, err := q.Submit("fixture", []string{"deepseek", "doubao", "wenxinyiyan"}, "", "tenant", "mixed")
	if err != nil || job.ExecutionMode != RoutingMixed || job.routingMode("deepseek") != RoutingAPIOnly || job.routingMode("doubao") != RoutingWebFirst || job.routingMode("yiyan") != RoutingWebFirst {
		t.Fatal(job, err)
	}
	job.SiteExecutionModes["deepseek"] = RoutingWebFirst
	if _, err := q.SetSiteRoutingPolicy("deepseek", true, 1); err != nil {
		t.Fatal(err)
	}
	again, created, err := q.Submit("fixture", []string{"deepseek", "doubao", "wenxinyiyan"}, "", "tenant", "mixed")
	if err != nil || created || again.routingMode("deepseek") != RoutingAPIOnly {
		t.Fatal("idempotent job changed its accepted routing", again, err)
	}
	reloaded := &Queue{DataDir: q.DataDir, jobs: map[string]*Job{}}
	reloaded.load()
	reloaded.loadRoutingPolicy()
	saved := reloaded.jobs[again.ID]
	if !reflect.DeepEqual(saved.SiteExecutionModes, again.SiteExecutionModes) {
		t.Fatal("restart lost site snapshot")
	}
	for _, legacy := range []string{"", RoutingWebFirst, RoutingAPIOnly} {
		old := &Job{ExecutionMode: legacy}
		if old.routingMode("deepseek") != canonicalRoutingMode(legacy) {
			t.Fatal("legacy job changed behavior")
		}
	}
}

func TestMixedPlatformRoutingActuallyUsesBothChannelsAndKeepsRetryRoute(t *testing.T) {
	q := attemptFixtureQueue(t, `import json
print(json.dumps({'ok':True,'answer':'fixture web answer','citations':[]}))
`, "first")
	q.apiSem = make(chan struct{}, 1)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"fixture API answer"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DEEPSEEK_API_KEY", "fixture-key")
	t.Setenv("GEO_DEEPSEEK_API_BASE", api.URL)
	job := &Job{ID: "mixed", Prompt: "fixture", Sites: []string{"deepseek", "doubao"}, ExecutionMode: RoutingMixed, SiteExecutionModes: map[string]string{"deepseek": RoutingAPIOnly, "doubao": RoutingWebFirst}}
	ctx := context.WithValue(context.Background(), routingSitesContextKey{}, job.SiteExecutionModes)
	for _, site := range job.Sites {
		one := q.runOneSiteContext(ctx, site, job.Prompt, job.ID, 2)
		entries := cloneAttemptList(one["attempts"])
		want := "web"
		if site == "deepseek" {
			want = "api"
		}
		if one["ok"] != true || len(entries) != 1 || entries[0]["channel"] != want {
			t.Fatalf("mixed %s used wrong channel: %v", site, one)
		}
	}
	// The job-scoped snapshot also governs the delivery retry path, even if
	// the caller's default context and current global policy prefer the web.
	one := q.runDeliverySite(context.Background(), job, "deepseek", map[string]any{"account_id": "api:deepseek", "error_code": "api_timeout"})
	if one["ok"] != true || one["routing_mode"] != RoutingAPIOnly || calls.Load() != 2 {
		t.Fatal("mixed delivery retry entered web recovery", one)
	}
}

func TestRoutingSnapshotPreservesAcceptedJobsAndIdempotency(t *testing.T) {
	q := &Queue{DataDir: t.TempDir(), jobs: map[string]*Job{}}
	first, created, err := q.Submit("fixture", []string{"doubao"}, "", "tenant", "first")
	if err != nil || !created || first.ExecutionMode != RoutingWebFirst {
		t.Fatal(first, created, err)
	}
	policy, _ := q.RoutingPolicy()
	if _, err := q.SetRoutingPolicy(false, policy.Revision); err != nil {
		t.Fatal(err)
	}
	second, _, err := q.Submit("fixture", []string{"doubao"}, "", "tenant", "second")
	if err != nil || second.ExecutionMode != RoutingAPIOnly {
		t.Fatal(second, err)
	}
	again, created, err := q.Submit("fixture", []string{"doubao"}, "", "tenant", "first")
	if err != nil || created || again.ID != first.ID || again.ExecutionMode != RoutingWebFirst {
		t.Fatal("accepted job changed policy", again, created, err)
	}
	// Loading does not rewrite old or queued records to the current global mode.
	reloaded := &Queue{DataDir: q.DataDir, jobs: map[string]*Job{}}
	reloaded.load()
	reloaded.loadRoutingPolicy()
	if reloaded.jobs[first.ID].ExecutionMode != RoutingWebFirst || reloaded.jobs[second.ID].ExecutionMode != RoutingAPIOnly {
		t.Fatal("restart changed accepted routing")
	}
}

func routingAPIStub(t *testing.T) (*Queue, *atomic.Int32) {
	t.Helper()
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_API_FALLBACK", "0") // Direct API routing is independent of fallback enablement.
	count := &atomic.Int32{}
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		count.Add(1)
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("invalid fixture request")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"fixture API answer"}}]}`))
	}))
	t.Cleanup(api.Close)
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture-key")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	return &Queue{apiSem: make(chan struct{}, 1)}, count
}

func TestDirectAPIBypassesEveryBrowserGateAndHasNoWebAttempts(t *testing.T) {
	q, count := routingAPIStub(t)
	q.siteSem = make(chan struct{}, 1)
	q.siteSem <- struct{}{} // A full browser lane must not block direct API.
	q.Script = filepath.Join(t.TempDir(), "must-not-run.py")
	ctx := context.WithValue(context.Background(), routingModeContextKey{}, RoutingAPIOnly)
	one := q.runOneSiteContext(ctx, "doubao", "unchanged question", "api-only-fixture", 1)
	if one["ok"] != true || one["answer"] != "fixture API answer" || one["routing_mode"] != RoutingAPIOnly || count.Load() != 1 {
		t.Fatalf("%v calls=%d", one, count.Load())
	}
	for _, key := range []string{"web_error_code", "web_skipped", "web_skip_reason", "api_fallback_failed"} {
		if _, exists := one[key]; exists {
			t.Fatalf("invented web attempt: %s=%v", key, one[key])
		}
	}
	entries := cloneAttemptList(one["attempts"])
	if len(entries) != 1 || entries[0]["channel"] != "api" || entries[0]["ok"] != true {
		t.Fatalf("ledger=%v", entries)
	}
	// Retry with an API account must never be treated as an outstanding web send.
	job := &Job{ID: "retry", Prompt: "unchanged question", Sites: []string{"doubao"}, ExecutionMode: RoutingAPIOnly}
	previous := map[string]any{"account_id": "api:doubao", "source": "official_api", "ok": false, "error_code": "api_timeout"}
	if one = q.runDeliverySite(context.Background(), job, "doubao", previous); one["ok"] != true || count.Load() != 2 {
		t.Fatalf("API retry touched browser recovery: %v", one)
	}
}

func TestDirectAPIFailureAndMissingKeyRemainAPIOnly(t *testing.T) {
	q, count := routingAPIStub(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "")
	one := q.runDirectAPIContext(context.Background(), "doubao", "fixture")
	if one["error_code"] != "api_not_configured" || count.Load() != 0 || one["web_skipped"] != nil {
		t.Fatalf("missing key=%v", one)
	}
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture-key")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	one = q.runDirectAPIContext(ctx, "doubao", "fixture")
	if count.Load() != 0 || one["api_skipped"] != true || one["web_error_code"] != nil {
		t.Fatalf("cancellation=%v", one)
	}
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(429)
		_, _ = w.Write([]byte(`{"error":{"message":"rate limited"}}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	one = q.runDirectAPIContext(context.Background(), "doubao", "fixture")
	if one["ok"] != false || !strings.HasPrefix(stringField(one, "error_code"), "api_") {
		t.Fatalf("API failure=%v", one)
	}
	for _, attempt := range cloneAttemptList(one["attempts"]) {
		if attempt["channel"] != "api" {
			t.Fatalf("invented web attempt: %v", attempt)
		}
	}
}

func TestRoutingEnabledKeepsWebBeforeAPIFallback(t *testing.T) {
	q := attemptFixtureQueue(t, `import json
print(json.dumps({'ok':False,'error_code':'platform_busy','error':'fixture busy','retryable':False}))
`, "first")
	t.Setenv("GEO_PLATFORM_BUSY_RETRIES", "0")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"fallback fixture answer"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture-key")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	one := q.runOneSiteContext(context.Background(), "doubao", "fixture", "web-first-fixture", 1)
	entries := cloneAttemptList(one["attempts"])
	if one["ok"] != true || calls.Load() != 1 || len(entries) != 2 || entries[0]["channel"] != "web" || entries[1]["channel"] != "api" {
		t.Fatalf("web-first order lost: %v", one)
	}
}

func TestMalformedRoutingStateCannotSilentlyChangeNewJobs(t *testing.T) {
	q := &Queue{DataDir: t.TempDir()}
	if err := os.MkdirAll(filepath.Dir(q.routingPolicyPath()), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(q.routingPolicyPath(), []byte(`{"revision":2}`), 0600); err != nil {
		t.Fatal(err)
	}
	q.loadRoutingPolicy()
	if _, err := q.RoutingPolicy(); err == nil {
		t.Fatal("missing bool silently interpreted as API only")
	}
}

func TestAcceptedRoutingModesReachWorkersAndSurviveLaterSwitches(t *testing.T) {
	for _, delivery := range []bool{false, true} {
		t.Run(fmt.Sprint("delivery=", delivery), func(t *testing.T) { testAcceptedRoutingWorkers(t, delivery) })
	}
}

func testAcceptedRoutingWorkers(t *testing.T, delivery bool) {
	_, calls := routingAPIStub(t)
	t.Setenv("GEO_DEEPSEEK_API_KEY", "fixture-key")
	t.Setenv("GEO_DEEPSEEK_API_BASE", os.Getenv("GEO_DOUBAO_API_BASE"))
	t.Setenv("GEO_API_FALLBACK", "1")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_REQUIRE_DELIVERY", map[bool]string{false: "0", true: "1"}[delivery])
	t.Setenv("GEO_PLATFORM_DISPATCH", map[bool]string{false: "0", true: "1"}[delivery])
	t.Setenv("GEO_CURSOR_CAPTCHA_AGENT", "0")
	t.Setenv("GEO_PLATFORM_BUSY_RETRIES", "0")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
	root := t.TempDir()
	data := filepath.Join(root, "server", "data")
	jobsDir := filepath.Join(data, "jobs")
	if err := os.MkdirAll(jobsDir, 0700); err != nil {
		t.Fatal(err)
	}
	scripts := filepath.Join(root, "server", "scripts")
	if err := os.MkdirAll(scripts, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(scripts, "run_scrape.py"), []byte("import json\nprint(json.dumps({'ok':False,'error_code':'platform_busy','error':'fixture web failure','retryable':False}))\n"), 0600); err != nil {
		t.Fatal(err)
	}
	barrier := filepath.Join(data, ".deployment-drain.json")
	raw, _ := json.Marshal(map[string]any{"expires_at": time.Now().Add(time.Minute).Unix(), "allow": []string{}})
	if err := os.WriteFile(barrier, raw, 0600); err != nil {
		t.Fatal(err)
	}
	q := New(root, nil, nil, jobsDir)
	t.Cleanup(func() {
		for _, job := range q.List() {
			_, _ = q.CancelVisible(job.ID, "", true)
		}
	})
	first, _, err := q.Submit("fixture", []string{"doubao"}, "", "tenant", "web-job")
	if err != nil {
		t.Fatal(err)
	}
	p, _ := q.RoutingPolicy()
	if _, err := q.SetRoutingPolicy(false, p.Revision); err != nil {
		t.Fatal(err)
	}
	second, _, err := q.Submit("fixture", []string{"doubao"}, "", "tenant", "api-job")
	if err != nil {
		t.Fatal(err)
	}
	p, _ = q.RoutingPolicy()
	if _, err := q.SetRoutingPolicy(true, p.Revision); err != nil {
		t.Fatal(err)
	}
	p, _ = q.RoutingPolicy()
	if _, err := q.SetSiteRoutingPolicy("deepseek", false, p.Revision); err != nil {
		t.Fatal(err)
	}
	mixed, _, err := q.Submit("fixture", []string{"doubao", "deepseek"}, "", "tenant", "mixed-job")
	if err != nil || mixed.ExecutionMode != RoutingMixed {
		t.Fatal(mixed, err)
	}
	p, _ = q.RoutingPolicy()
	if _, err := q.SetRoutingPolicy(false, p.Revision); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(barrier); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(10 * time.Second)
	for _, job := range []*Job{first, second, mixed} {
		for {
			got, _ := q.Get(job.ID)
			if got.Status == "succeeded" {
				entries := cloneAttemptList(got.Results[0]["attempts"])
				if job.ID == first.ID && (len(entries) != 2 || entries[0]["channel"] != "web" || entries[1]["channel"] != "api") {
					t.Fatalf("web route lost: %v", entries)
				}
				if job.ID == second.ID && (len(entries) != 1 || entries[0]["channel"] != "api") {
					t.Fatalf("API-only route lost: %v", entries)
				}
				if job.ID == mixed.ID {
					if len(got.Results) != 2 {
						t.Fatal("mixed task lost a platform", got.Results)
					}
					for _, one := range got.Results {
						entries := cloneAttemptList(one["attempts"])
						if one["site_id"] == "deepseek" {
							if len(entries) != 1 || entries[0]["channel"] != "api" {
								t.Fatal("mixed DeepSeek used web", one)
							}
						} else if len(entries) != 2 || entries[0]["channel"] != "web" || entries[1]["channel"] != "api" {
							t.Fatal("mixed Doubao bypassed web", one)
						}
					}
				}
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("job did not finish in saved mode: %s %s %v", got.ID, got.Status, got.Results)
			}
			time.Sleep(10 * time.Millisecond)
		}
	}
	if calls.Load() != 4 {
		t.Fatalf("unexpected API calls: %d", calls.Load())
	}
}
