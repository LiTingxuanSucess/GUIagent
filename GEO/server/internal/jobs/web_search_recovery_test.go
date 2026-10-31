package jobs

import (
	"context"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"
)

func completedUnconfirmedWebFixture() map[string]any {
	return map[string]any{
		"ok": true, "answer": "completed webpage answer", "source": "dom", "answer_origin": "web", "prompt": "original question",
		"web_search_status": "not_confirmed", "url": "https://www.doubao.com/thread/fixture", "share_status": "available",
		"citations": []map[string]any{}, "elapsed_ms": int64(1234), "account_id": "fixture-account",
	}
}

func TestCompletedWebQualityNeverCallsAPIOrReplacesConversationContent(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "1")
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture-key")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		t.Error("completed webpage answer triggered API substitution")
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	for _, search := range []string{"not_confirmed", "failed", "used", ""} {
		web := completedUnconfirmedWebFixture()
		web["web_search_status"] = search
		web["citations"] = []map[string]any{{"url": "https://example.org/actual-web-source", "title": "Original source"}}
		web["quality_issues"] = []string{"existing_quality_issue"}
		web["stage_timings"] = map[string]any{"web_execution_ms": 1234}
		web["share_status"] = "generation_failed"
		result := (&Queue{}).recoverWebSearchContext(context.Background(), "doubao", "original question", web)
		result = (&Queue{}).applyAPIFallbackContext(context.Background(), "doubao", "original question", result)
		for _, key := range []string{"ok", "answer", "source", "answer_origin", "url", "share_status", "citations", "elapsed_ms", "stage_timings"} {
			if !reflect.DeepEqual(result[key], web[key]) {
				t.Fatalf("quality handling replaced webpage %s: %#v", key, result)
			}
		}
		if calls.Load() != 0 || result["search_recovery_status"] != nil || result["api_skipped"] != nil || result["api_error_code"] != nil {
			t.Fatal("quality metadata invented an API attempt")
		}
		if result["share_error_code"] != "share_generation_failed" || web["share_error_code"] != nil || len(web["quality_issues"].([]string)) != 1 {
			t.Fatal("quality reason was lost or mutated the input result")
		}
		if search == "not_confirmed" && result["web_search_error_code"] != "search_not_confirmed" || search == "failed" && result["web_search_error_code"] != "search_request_failed" {
			t.Fatal("missing search evidence was not reported as quality metadata")
		}
	}
}

func TestWebQualityPreservesExistingReasonsAndDoesNotInventHistory(t *testing.T) {
	for _, scenario := range []string{"legacy", "failed_web", "official_api", "empty_answer"} {
		web := completedUnconfirmedWebFixture()
		switch scenario {
		case "legacy":
			delete(web, "web_search_status")
		case "failed_web":
			web["ok"] = false
		case "official_api":
			web["source"] = "official_api"
		case "empty_answer":
			web["answer"] = " "
		}
		result := (&Queue{}).recoverWebSearchContext(context.Background(), "doubao", "question", web)
		if !reflect.DeepEqual(result, web) {
			t.Fatalf("ineligible result gained invented quality evidence: %s", scenario)
		}
	}
	web := completedUnconfirmedWebFixture()
	web["web_search_error_code"] = "search_rate_limited"
	web["share_status"] = "failed"
	web["share_error_code"] = "share_timeout"
	web["quality_issues"] = []any{"search_rate_limited", "share_timeout"}
	result := (&Queue{}).recoverWebSearchContext(context.Background(), "doubao", "question", web)
	if !reflect.DeepEqual(result["quality_issues"], []string{"search_rate_limited", "share_timeout"}) || result["share_error_code"] != "share_timeout" {
		t.Fatal("quality metadata replaced or duplicated a real reason")
	}
}
