package jobs

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestDoubaoBurstWaitsForExistingWebLeaseInsteadOfUsingAPI(t *testing.T) {
	q := concurrentPoolFixture(t, []string{"one"}, "import json\nprint(json.dumps({'ok':True,'answer':'web answer','answer_origin':'web'}))\n")
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_DOUBAO_API_FALLBACK", "1")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_DOUBAO_POOL_ADMISSION_WAIT_SEC", "1")
	t.Setenv("GEO_DOUBAO_API_TIMEOUT_SEC", "5")
	t.Setenv("GEO_API_RESERVE_SEC", "5")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"备用回答"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	q.apiSem = make(chan struct{}, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	release, ok := q.acquirePoolCapacity(ctx, "doubao")
	if !ok {
		t.Fatal("could not reserve the existing request's account capacity")
	}
	defer release()
	start := time.Now()
	completed := make(chan map[string]any, 1)
	go func() { completed <- q.runOneSiteContext(ctx, "doubao", "问题", "overflow-fixture", 1) }()
	waitForBrowserWaiters(t, q, 1)
	time.Sleep(1100 * time.Millisecond)
	q.mu.Lock()
	active := q.poolActive["doubao"]
	q.mu.Unlock()
	if active != 1 || calls.Load() != 0 || len(q.siteSem) != 0 {
		t.Error("queued request used API or released the occupied request's capacity")
	}
	select {
	case result := <-completed:
		t.Fatalf("request stopped waiting before the account was released: %v", result)
	default:
	}
	release()
	var result map[string]any
	select {
	case result = <-completed:
	case <-ctx.Done():
		t.Fatal("request did not resume after its account became available")
	}
	elapsed := time.Since(start)
	if result["ok"] != true || result["answer_origin"] != "web" || result["web_skipped"] == true || calls.Load() != 0 || elapsed < time.Second || elapsed > 3*time.Second {
		t.Fatalf("burst did not retain its webpage path: result=%v elapsed=%s", result, elapsed)
	}
	if q.poolActive["doubao"] != 0 || len(q.siteSem) != 0 {
		t.Fatal("completed burst request leaked pool/browser capacity")
	}
}
