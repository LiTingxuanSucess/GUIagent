package jobs

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func webFirstLocalFailureFixture(t *testing.T, accounts ...string) (*Queue, *atomic.Int32) {
	t.Helper()
	q := attemptFixtureQueue(t, `from pathlib import Path
Path('web-started').touch()
print('{"ok":true,"answer":"unexpected webpage execution"}')
`, accounts...)
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"unexpected API answer"}}]}`))
	}))
	t.Cleanup(api.Close)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_DOUBAO_API_KEY", "local-regression-fixture")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	if !officialAPIConfigured("doubao") {
		t.Fatal("regression requires a configured API fallback")
	}
	return q, &calls
}

func TestWebFirstLocalCDPFailurePersistsAccountsWithoutCallingAPI(t *testing.T) {
	q, calls := webFirstLocalFailureFixture(t, "first", "second")
	for _, account := range []string{"first", "second"} {
		if err := os.Remove(filepath.Join(q.GeoRoot, account, "DevToolsActivePort")); err != nil {
			t.Fatal(err)
		}
	}
	now := time.Now()
	q.DataDir = t.TempDir()
	q.jobs = map[string]*Job{"local-cdp-failure": {
		ID: "local-cdp-failure", Status: "queued", Sites: []string{"doubao"}, Prompt: "fixture question",
		CreatedAt: now.Format(time.RFC3339Nano), createdUnix: now.UnixMilli(),
	}}
	q.run("local-cdp-failure")
	raw, err := os.ReadFile(filepath.Join(q.DataDir, "local-cdp-failure.json"))
	if err != nil {
		t.Fatal(err)
	}
	var saved Job
	if err := json.Unmarshal(raw, &saved); err != nil {
		t.Fatal(err)
	}
	if saved.Status != "failed" || len(saved.Results) != 1 {
		t.Fatalf("local failure was not persisted: %#v", saved)
	}
	result := saved.Results[0]
	if result["ok"] != false || result["error_code"] != "platform_session_unavailable" || result["account_id"] != "second" || result["account_retries"] != float64(1) || result["source"] == "official_api" {
		t.Fatalf("CDP failover lost its account failure: %#v", result)
	}
	entries := resultAttempts(t, result)
	if len(entries) != 2 {
		t.Fatalf("want two skipped setup stages, got %#v", entries)
	}
	for i, account := range []string{"first", "second"} {
		if entries[i]["channel"] != "web" || entries[i]["account_id"] != account || entries[i]["skipped"] != true || entries[i]["error_code"] != "platform_session_unavailable" {
			t.Fatalf("setup failure counted as an executed webpage: %#v", entries[i])
		}
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "web-started")); !os.IsNotExist(err) || calls.Load() != 0 {
		t.Fatalf("local CDP failure started web/API: marker=%v API_calls=%d", err, calls.Load())
	}
}

func TestWebFirstExpiredCallerStartsNeitherWebNorAPI(t *testing.T) {
	q, calls := webFirstLocalFailureFixture(t, "ready")
	ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	result := q.runOneSiteContext(ctx, "doubao", "fixture question", "expired-caller", 1)
	if result["ok"] != false || result["web_skipped"] != true || result["source"] == "official_api" || result["api_skipped"] != nil {
		t.Fatalf("expired caller dispatched a stage: %#v", result)
	}
	entries := resultAttempts(t, result)
	if len(entries) != 1 || entries[0]["channel"] != "web" || entries[0]["skipped"] != true || entries[0]["skip_reason"] != "request_deadline_exceeded" {
		t.Fatalf("expired caller recorded an execution: %#v", entries)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "web-started")); !os.IsNotExist(err) || calls.Load() != 0 || len(q.siteSem) != 0 {
		t.Fatalf("expired caller acquired resources or called web/API: marker=%v API_calls=%d slots=%d", err, calls.Load(), len(q.siteSem))
	}
}
