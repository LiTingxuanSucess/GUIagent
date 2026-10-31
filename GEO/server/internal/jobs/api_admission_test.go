package jobs

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func apiAdmissionFixture(t *testing.T) (*Queue, *atomic.Int32) {
	t.Helper()
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	t.Setenv("GEO_API_ADMISSION_WAIT_SEC", "1")
	t.Setenv("GEO_API_MIN_EXECUTION_SEC", "1")
	t.Setenv("GEO_DOUBAO_API_MIN_EXECUTION_SEC", "")
	t.Setenv("GEO_DOUBAO_API_TIMEOUT_SEC", "5")
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture-key")
	calls := &atomic.Int32{}
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		time.Sleep(25 * time.Millisecond)
		fmt.Fprint(w, `{"choices":[{"message":{"content":"API answer"}}]}`)
	}))
	t.Cleanup(api.Close)
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	return &Queue{apiSem: make(chan struct{}, 1)}, calls
}

func TestAPIAdmissionReservesUsefulExecutionWindow(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_MIN_EXECUTION_SEC", "15")
	t.Setenv("GEO_DOUBAO_API_MIN_EXECUTION_SEC", "")
	t.Setenv("GEO_DOUBAO_API_TIMEOUT_SEC", "60")
	t.Setenv("GEO_API_ADMISSION_WAIT_SEC", "5")
	now := time.Now()
	parent, cancel := context.WithDeadline(context.Background(), now.Add(16*time.Second))
	defer cancel()
	admission, stop := apiAdmissionContext(parent, "doubao")
	defer stop()
	deadline, _ := admission.Deadline()
	if !deadline.Equal(now.Add(time.Second)) {
		t.Fatalf("API admission consumed reserved execution time: %v", deadline.Sub(now))
	}
	if !hasUsefulAPIWindow(parent, "doubao", now) || hasUsefulAPIWindow(parent, "doubao", now.Add(2*time.Second)) {
		t.Fatal("the same slot would be admitted without a useful remaining window")
	}
	longParent, cancelLong := context.WithDeadline(context.Background(), now.Add(time.Minute))
	defer cancelLong()
	bounded, stopBounded := apiAdmissionContext(longParent, "doubao")
	defer stopBounded()
	boundedDeadline, _ := bounded.Deadline()
	if wait := boundedDeadline.Sub(now); wait < 5*time.Second || wait > 5100*time.Millisecond {
		t.Fatalf("API admission is not independently bounded: %s", wait)
	}
	t.Setenv("GEO_DOUBAO_API_MIN_EXECUTION_SEC", "20")
	t.Setenv("GEO_DOUBAO_API_TIMEOUT_SEC", "5")
	if apiMinimumExecution("doubao") != 5*time.Second {
		t.Fatal("the minimum API window exceeds the configured total execution limit")
	}
}

func TestAPIBudgetSkipDoesNotAcquireSlotCallProviderOrOverwriteWebEvidence(t *testing.T) {
	q, calls := apiAdmissionFixture(t)
	parent, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	failure := siteError("doubao", "question", "platform_auth_unavailable", "login failed", true, 100)
	failure["account_id"] = "acct_browser"
	failure["stage_timings"] = map[string]any{"worker_wait_ms": int64(11), "web_execution_ms": int64(100)}
	result := q.applyAPIFallbackContext(parent, "doubao", "question", failure)
	if calls.Load() != 0 || len(q.apiSem) != 0 || result["api_skipped"] != true || result["api_error_code"] != "insufficient_api_budget" || result["api_execution_ms"] != int64(0) || result["api_fallback_failed"] == true {
		t.Fatalf("unstarted API call was treated as a provider failure: %v", result)
	}
	if result["error_code"] != "platform_auth_unavailable" || result["account_id"] != "acct_browser" {
		t.Fatal("API budget exhaustion overwrote browser identity or error")
	}
	timings := result["stage_timings"].(map[string]any)
	if timings["worker_wait_ms"] != int64(11) || timings["web_execution_ms"] != int64(100) {
		t.Fatal("API admission lost the existing browser stage timings")
	}
	attempt := resultAttempts(t, result)[0]
	if attempt["channel"] != "api" || attempt["account_id"] != "api:doubao" || attempt["skipped"] != true || attempt["error_code"] != "insufficient_api_budget" {
		t.Fatalf("API skip is attributed to the webpage phone: %v", attempt)
	}
}

func TestAPISlotQueueLimitLeavesParentAndExecutionBudgetAvailable(t *testing.T) {
	q, calls := apiAdmissionFixture(t)
	q.apiSem <- struct{}{}
	parent, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	started := time.Now()
	result := q.applyAPIFallbackContext(parent, "doubao", "question", siteError("doubao", "question", "platform_busy", "busy", true, 1))
	elapsed := time.Since(started)
	if elapsed < 900*time.Millisecond || elapsed > 2*time.Second || parent.Err() != nil {
		t.Fatalf("queue waited for the whole request rather than its own limit: %s", elapsed)
	}
	if result["api_error_code"] != "api_admission_timeout" || result["api_skipped"] != true || result["api_fallback_failed"] == true || calls.Load() != 0 || len(q.apiSem) != 1 {
		t.Fatalf("API queue exhaustion was reported as an upstream failure: %v", result)
	}
	if result["api_wait_ms"].(int64) < 900 || result["api_execution_ms"] != int64(0) {
		t.Fatal("API queue waiting was charged to provider execution")
	}
}

func TestAPIAdmissionSuccessKeepsSeparateTimingsAndDoesNotAliasWebMap(t *testing.T) {
	q, calls := apiAdmissionFixture(t)
	q.apiSem <- struct{}{}
	go func() {
		time.Sleep(100 * time.Millisecond)
		<-q.apiSem
	}()
	parent, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	failure := siteError("doubao", "question", "platform_busy", "busy", true, 40)
	original := map[string]any{"web_execution_ms": int64(40), "worker_wait_ms": float64(12), "private": "SECRET_TIMING"}
	failure["stage_timings"] = original
	result := q.applyAPIFallbackContext(parent, "doubao", "question", failure)
	if calls.Load() != 1 || result["ok"] != true || len(q.apiSem) != 0 {
		t.Fatalf("an admitted API call was lost or leaked its slot: %v", result)
	}
	if result["api_wait_ms"].(int64) < 80 || result["api_execution_ms"].(int64) < 20 {
		t.Fatalf("API wait and execution were not independently measured: %v", result)
	}
	timings := result["stage_timings"].(map[string]any)
	if timings["web_execution_ms"] != int64(40) || timings["worker_wait_ms"] != float64(12) || timings["api_wait_ms"] != result["api_wait_ms"] || timings["api_execution_ms"] != result["api_execution_ms"] {
		t.Fatal("API replacement lost browser timings or disagrees with flattened fields")
	}
	if _, exists := original["api_wait_ms"]; exists {
		t.Fatal("API timing merge mutated the browser's completed metadata")
	}
	if _, exists := timings["private"]; exists {
		t.Fatal("non-numeric private data entered the timing metadata")
	}
}

func TestSearchRecoveryBudgetSkipKeepsAnswerAndTimingEvidence(t *testing.T) {
	q, calls := apiAdmissionFixture(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "1")
	web := completedUnconfirmedWebFixture()
	before := map[string]any{"web_execution_ms": int64(1234)}
	web["stage_timings"] = before
	parent, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	result := q.recoverWebSearchContext(parent, "doubao", "question", web)
	if calls.Load() != 0 || result["ok"] != true || result["answer"] != web["answer"] || result["api_skipped"] != nil || result["search_recovery_error_code"] != nil {
		t.Fatal("quality handling attempted to substitute a completed browser answer")
	}
	timings := result["stage_timings"].(map[string]any)
	if timings["web_execution_ms"] != int64(1234) || timings["api_execution_ms"] != nil || len(before) != 1 {
		t.Fatal("search recovery lost timing evidence or changed the original webpage result")
	}
	encoded, err := json.Marshal(result["attempts"])
	if err != nil || strings.Contains(string(encoded), "fixture-key") || strings.Contains(string(encoded), "question") {
		t.Fatal("private API configuration or request text entered attempt history")
	}
}

func TestAPIFailureLoggingUsesStableCodesWithoutProviderBody(t *testing.T) {
	var buffer bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&buffer)
	defer log.SetOutput(previous)
	apiFallbackLog("doubao", "platform_busy", apiFailureCode(errors.New("upstream request failed with SECRET_KEY and SECRET_BODY")))
	if output := buffer.String(); strings.Contains(output, "SECRET_") || !strings.Contains(output, "api_error_code=api_request_failed") {
		t.Fatalf("API error logging exposed a raw provider error: %s", output)
	}
}
