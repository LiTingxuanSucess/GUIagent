package jobs

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

func TestAPIReserveDoesNotRemoveTheFirstWebAttempt(t *testing.T) {
	for _, remaining := range []time.Duration{140 * time.Millisecond, 2 * time.Second, 7 * time.Second, 38 * time.Second} {
		t.Run(remaining.String(), func(t *testing.T) {
			q := attemptFixtureQueue(t, "import json\nprint(json.dumps({'ok':True,'answer':'completed web answer','answer_origin':'web'}))\n", "acct_healthy")
			t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "45")
			t.Setenv("GEO_API_ENABLE_SEARCH", "0")
			t.Setenv("GEO_DEEPSEEK_API_KEY", "fixture")
			t.Setenv("GEO_DEEPSEEK_API_TIMEOUT_SEC", "120")
			var calls atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				fmt.Fprint(w, `{"choices":[{"message":{"content":"API answer"}}]}`)
			}))
			defer api.Close()
			t.Setenv("GEO_DEEPSEEK_API_BASE", api.URL)
			q.DataDir = filepath.Join(t.TempDir(), "jobs")
			ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second+remaining)
			defer cancel()
			result := q.runOneSiteContext(ctx, "deepseek", "question", "too-late", 1)
			attempts := resultAttempts(t, result)
			if result["ok"] != true || result["answer_origin"] != "web" || calls.Load() != 0 || len(attempts) != 1 || attempts[0]["skipped"] == true || attempts[0]["account_id"] != "acct_healthy" {
				t.Fatalf("API reserve displaced a usable webpage attempt: %v", result)
			}
			if len(q.siteSem) != 0 || q.poolActive["deepseek"] != 0 {
				t.Fatal("completed webpage leaked browser resources")
			}
			if _, err := os.Stat(q.cooldownPath("deepseek")); !os.IsNotExist(err) {
				t.Fatal("local budget shortage altered platform cooldown")
			}
		})
	}
}

func waitForBrowserWaiters(t *testing.T, q *Queue, count int) {
	t.Helper()
	limit := time.Now().Add(time.Second)
	for time.Now().Before(limit) {
		q.browserMu.Lock()
		n := len(q.browserWaiters)
		q.browserMu.Unlock()
		if n == count {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("expected %d queued browser requests", count)
}

func schedulerFixture() *Queue {
	pool := &concurrentPool{}
	for i := 0; i < 5; i++ {
		pool.leases = append(pool.leases, &sessions.Lease{AccountID: fmt.Sprint(i)})
	}
	return &Queue{Sess: pool, siteSem: make(chan struct{}, 2)}
}

func TestBrowserSchedulingIsFairAndDoesNotPreReserveAccounts(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
	q := schedulerFixture()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	first, _ := q.acquireBrowserResources(ctx, "deepseek", "blocker-first", &stageTimings{})
	second, _ := q.acquireBrowserResources(ctx, "yuanbao", "blocker-second", &stageTimings{})
	defer second()
	type grant struct {
		name    string
		release func()
	}
	grants := make(chan grant, 3)
	for i, tc := range []struct{ name, site, job string }{{"old-first", "deepseek", "old-job"}, {"old-second", "deepseek", "old-job"}, {"other-platform", "kimi", "new-job"}} {
		go func(name, site, job string) {
			release, ok := q.acquireBrowserResources(ctx, site, job, &stageTimings{})
			if ok {
				grants <- grant{name, release}
			}
		}(tc.name, tc.site, tc.job)
		waitForBrowserWaiters(t, q, i+1)
	}
	q.mu.Lock()
	if q.poolActive["deepseek"] != 1 || q.poolActive["kimi"] != 0 {
		q.mu.Unlock()
		t.Fatal("queued fanout reserved accounts before a global slot existed")
	}
	q.mu.Unlock()
	first()
	for _, want := range []string{"old-first", "other-platform", "old-second"} {
		select {
		case got := <-grants:
			if got.name != want {
				t.Fatalf("grant=%s want=%s", got.name, want)
			}
			got.release()
			got.release()
		case <-ctx.Done():
			t.Fatal("fair scheduler stalled")
		}
	}
	second()
	if len(q.siteSem) != 0 {
		t.Fatal("browser resources leaked after idempotent releases")
	}
}

func TestBrowserBurstCancellationNeverExceedsTwoSlots(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
	q := schedulerFixture()
	var active, peak atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 36; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			if i%3 == 0 {
				time.AfterFunc(time.Millisecond, cancel)
			}
			release, ok := q.acquireBrowserResources(ctx, []string{"deepseek", "doubao", "yuanbao", "kimi", "qianwen", "yiyan"}[i%6], fmt.Sprint(i/6), &stageTimings{})
			if !ok {
				return
			}
			count := active.Add(1)
			for old := peak.Load(); count > old && !peak.CompareAndSwap(old, count); old = peak.Load() {
			}
			time.Sleep(2 * time.Millisecond)
			active.Add(-1)
			release()
		}(i)
	}
	wg.Wait()
	q.browserMu.Lock()
	pending := len(q.browserWaiters)
	q.browserMu.Unlock()
	q.mu.Lock()
	defer q.mu.Unlock()
	if peak.Load() > 2 || peak.Load() == 0 || pending != 0 || len(q.siteSem) != 0 {
		t.Fatalf("peak=%d pending=%d slots=%d", peak.Load(), pending, len(q.siteSem))
	}
	for site, count := range q.poolActive {
		if count != 0 {
			t.Fatalf("pool capacity leaked for %s", site)
		}
	}
}

func TestSessionAcquisitionDoesNotConsumeWebExecutionBudget(t *testing.T) {
	q := attemptFixtureQueue(t, "import json,os,time\nprint(json.dumps({'ok':True,'answer':'completed web answer','remaining_ms':int(os.environ['GEO_DIAGNOSTIC_DEADLINE_MS'])-int(time.time()*1000)}))\n", "acct_healthy")
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "1")
	q.Sess = &delayedSessionPool{accountPool: q.Sess, wait: 300 * time.Millisecond}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	result := q.runOneSiteContext(ctx, "yuanbao", "question", "slow-session", 1)
	attempts := resultAttempts(t, result)
	stages := result["stage_timings"].(map[string]any)
	remaining, _ := result["remaining_ms"].(float64)
	if result["ok"] != true || result["web_skipped"] == true || len(attempts) != 1 || attempts[0]["account_id"] != "acct_healthy" || stages["session_acquire_ms"].(int64) < 280 || remaining < 800 || remaining > 1000 || len(q.siteSem) != 0 {
		t.Fatalf("session acquisition shortened the webpage's own execution budget: %v", result)
	}
}

type delayedSessionPool struct {
	accountPool
	wait time.Duration
}

func TestScraperDeadlineDoesNotDependOnDiagnosticCapture(t *testing.T) {
	t.Setenv("GEO_DIAGNOSTICS_ENABLED", "0")
	root := t.TempDir()
	script := filepath.Join(root, "deadline.py")
	if err := os.WriteFile(script, []byte("import json,os\nprint(json.dumps({'ok':True,'answer':'fixture','observed_deadline_ms':int(os.environ.get('GEO_DIAGNOSTIC_DEADLINE_MS','0'))}))\n"), 0600); err != nil {
		t.Fatal(err)
	}
	q := &Queue{GeoRoot: root, Python: "python3", Script: script}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	deadline, _ := ctx.Deadline()
	result, err := q.scrapeForContext(ctx, "http://127.0.0.1:9", "doubao", "question", "deadline-fixture", "", 5*time.Second)
	if err != nil || result["observed_deadline_ms"] != float64(deadline.UnixMilli()) {
		t.Fatalf("shared deadline was lost without diagnostics: result=%v err=%v", result, err)
	}
}

func TestSessionWaitingStillRespectsExplicitCallerCancellation(t *testing.T) {
	q := attemptFixtureQueue(t, "raise AssertionError('session never became ready')\n", "acct_healthy")
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	q.Sess = &blockedSessionPool{q.Sess}
	ctx, cancel := context.WithTimeout(context.Background(), 1150*time.Millisecond)
	defer cancel()
	started := time.Now()
	result := q.runOneSiteContext(ctx, "deepseek", "question", "blocked-session", 1)
	elapsed := time.Since(started)
	if result["error_code"] != "session_setup_timeout" || result["web_skipped"] != true || elapsed < time.Second || elapsed > 1700*time.Millisecond || len(q.siteSem) != 0 {
		t.Fatalf("session wait ignored caller cancellation or consumed a reserved web window: %v elapsed=%s", result, elapsed)
	}
}

func TestSessionSetupHasItsOwnBudgetAndInheritsCancellation(t *testing.T) {
	t.Setenv("GEO_SESSION_ACQUIRE_WAIT_SEC", "120")
	ctx, cancel := sessionWaitContext(context.Background(), "deepseek")
	deadline, ok := ctx.Deadline()
	if !ok || time.Until(deadline) < 119*time.Second || time.Until(deadline) > 120*time.Second {
		t.Fatal("session setup did not receive its independent 120 second budget")
	}
	cancel()
	parent, cancelParent := context.WithCancel(context.Background())
	ctx, cancel = sessionWaitContext(parent, "deepseek")
	defer cancel()
	cancelParent()
	if ctx.Err() != context.Canceled {
		t.Fatal("independent session budget detached caller cancellation")
	}
}

func TestWebFirstExecutionStillStopsOnExplicitCallerCancellation(t *testing.T) {
	q := attemptFixtureQueue(t, "import pathlib,time\npathlib.Path('web-started').touch()\ntime.sleep(10)\n", "ready-account")
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	completed := make(chan map[string]any, 1)
	go func() { completed <- q.runOneSiteContext(ctx, "doubao", "question", "cancel-running-web", 1) }()
	ready := false
	until := time.Now().Add(time.Second)
	for time.Now().Before(until) {
		if _, err := os.Stat(filepath.Join(q.GeoRoot, "web-started")); err == nil {
			ready = true
			break
		}
		time.Sleep(time.Millisecond)
	}
	started := time.Now()
	cancel()
	select {
	case result := <-completed:
		attempts := resultAttempts(t, result)
		if !ready || result["ok"] == true || calls.Load() != 0 || len(attempts) != 1 || attempts[0]["skipped"] == true || len(q.siteSem) != 0 || time.Since(started) > 700*time.Millisecond {
			t.Fatalf("fresh webpage budgets detached caller cancellation or started API: result=%v ready=%t calls=%d", result, ready, calls.Load())
		}
	case <-time.After(time.Second):
		t.Fatal("caller cancellation did not stop the running webpage process")
	}
}

type blockedSessionPool struct{ accountPool }

func (p *blockedSessionPool) AcquireContext(ctx context.Context, site string) (*sessions.Lease, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func (p *delayedSessionPool) AcquireContext(ctx context.Context, site string) (*sessions.Lease, error) {
	timer := time.NewTimer(p.wait)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-timer.C:
		return p.accountPool.AcquireContext(ctx, site)
	}
}
