package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRetrievedSearchPreservesAllSourcesThroughPublicResult(t *testing.T) {
	for _, site := range []string{"deepseek", "kimi"} {
		t.Run(site, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_API_ENABLE_SEARCH", "1")
			t.Setenv("GEO_STRUCTURED_SEARCH_LIMIT", "20")
			t.Setenv("GEO_DEEPSEEK_API_THINKING", "1")
			t.Setenv("GEO_DEEPSEEK_API_MAX_TOKENS", "8192")
			t.Setenv("GEO_DEEPSEEK_API_REASONING_EFFORT", "high")
			t.Setenv("GEO_KIMI_API_REASONING_EFFORT", "high")
			sources := make([]map[string]any, 130)
			for i := range sources {
				sources[i] = map[string]any{"title": fmt.Sprintf("来源 %d", i+1), "url": fmt.Sprintf("https://example.com/source/%d", i+1)}
			}
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				if r.URL.Path == "/tools/search" || r.URL.Path == "/v1/tools/search" {
					limit := body["limit"]
					if r.URL.Path == "/v1/tools/search" {
						limit = body["count"]
					}
					if limit != float64(20) {
						t.Error("search limit was not configured")
					}
					// Exercise parser/output coverage independently of the provider's
					// single-call limit: never discard valid records already received.
					key := "search_results"
					if r.URL.Path == "/v1/tools/search" {
						key = "results"
					}
					_ = json.NewEncoder(w).Encode(map[string]any{key: sources})
					return
				}
				if body["reasoning_effort"] != "high" {
					t.Error("configured reasoning effort lost")
				}
				if site == "deepseek" && (body["thinking"].(map[string]any)["type"] != "enabled" || body["max_tokens"] != float64(8192)) {
					t.Error("thinking options lost")
				}
				_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop","message":{"content":"参考来源的完整回答[1]。"}}]}`))
			}))
			defer api.Close()
			prefix := "GEO_" + strings.ToUpper(site)
			t.Setenv(prefix+"_API_KEY", "test-key")
			t.Setenv(prefix+"_API_BASE", api.URL)
			result := (&Queue{}).applyAPIFallback(site, "全球纯电家用车推荐", map[string]any{"ok": false, "error_code": "site_timeout"})
			if result["ok"] != true || result["api_reasoning_effort"] != "high" || result["search_result_count"] != 130 {
				t.Fatalf("unexpected metadata: %#v", result)
			}
			raw, _ := json.Marshal(&Job{Results: []map[string]any{result}})
			var stored Job
			if err := json.Unmarshal(raw, &stored); err != nil {
				t.Fatal(err)
			}
			stripInternalResults(&stored)
			public := stored.Results[0]["citations"].([]map[string]any)
			if len(public) != 130 || public[129]["url"] != "https://example.com/source/130" {
				t.Fatalf("public result lost sources: count=%d", len(public))
			}
		})
	}
}

func TestThinkingAPIBudgetDoesNotShortenWebExecution(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_FALLBACK", "1")
	t.Setenv("GEO_SINGLE_TOTAL_TIMEOUT_SEC", "120")
	t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "110")
	t.Setenv("GEO_API_RESERVE_SEC", "35")
	for _, site := range []string{"deepseek", "kimi", "qianwen", "yiyan", "yuanbao"} {
		t.Run(site, func(t *testing.T) {
			prefix := "GEO_" + strings.ToUpper(site)
			t.Setenv(prefix+"_API_KEY", "test-key")
			t.Setenv(prefix+"_API_TIMEOUT_SEC", "120")
			t.Setenv(prefix+"_SINGLE_TOTAL_TIMEOUT_SEC", "210")
			t.Setenv(prefix+"_WEB_BUDGET_SEC", "110")
			start := time.Now()
			budget := requestBudgetForSites([]string{site})
			ctx, cancel := context.WithDeadline(context.Background(), start.Add(budget))
			defer cancel()
			webEnd := requestWebDeadline(ctx, site, start.Add(240*time.Second), start, 1)
			if budget != 210*time.Second || webEnd.Sub(start) != 110*time.Second || apiTimeout(site) != 120*time.Second {
				t.Fatal("thinking API settings shortened the independent webpage budget")
			}
		})
	}
}

func TestRetrievedSourcesSurviveAnswerWithoutURLs(t *testing.T) {
	for _, site := range []string{"deepseek", "kimi"} {
		t.Run(site, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_API_ENABLE_SEARCH", "1")
			var searches, answers atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/tools/search" || r.URL.Path == "/v1/tools/search" {
					searches.Add(1)
					payload := `{"search_results":[{"title":"真实检索页","url":"https://example.com/source","snippet":"仅供参考"},{"url":"https://example.com/source"},{"url":"javascript:alert(1)"}]}`
					if r.URL.Path == "/v1/tools/search" {
						payload = `{"results":[{"title":"真实检索页","url":"https://example.com/source","snippet":"仅供参考","summary":"给模型阅读的长摘要"},{"url":"https://example.com/source"},{"url":"javascript:alert(1)"}]}`
					}
					_, _ = w.Write([]byte(payload))
					return
				}
				answers.Add(1)
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				messages, _ := body["messages"].([]any)
				if len(messages) != 3 || !strings.Contains(messages[2].(map[string]any)["content"].(string), "https://example.com/source") {
					t.Error("answer request did not include the retrieved source")
				}
				if site == "deepseek" && body["thinking"].(map[string]any)["type"] != "disabled" {
					t.Error("DeepSeek options lost")
				}
				_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop","message":{"content":"正文只有引用角标[1]，没有网址。另有模型生成的 https://invented.example/fake"}}]}`))
			}))
			defer api.Close()
			prefix := "GEO_" + strings.ToUpper(site)
			t.Setenv(prefix+"_API_KEY", "test-key")
			t.Setenv(prefix+"_API_BASE", api.URL)
			result := (&Queue{}).applyAPIFallback(site, "问题", siteError(site, "问题", "site_timeout", "网页超时", true, 100))
			if result["ok"] != true || result["web_search_status"] != "used" || result["citations_origin"] != "search_results" {
				t.Fatalf("metadata = %#v", result)
			}
			citations := result["citations"].([]map[string]any)
			if len(citations) != 1 || citations[0]["url"] != "https://example.com/source" || searches.Load() != 1 || answers.Load() != 1 {
				t.Fatal("real sources were lost or invented URLs became citations")
			}
			if site == "deepseek" && (result["native_web_search_supported"] != true || result["web_search_mode"] != "native_search_api" || result["web_search_provider"] != "deepseek_search") {
				t.Fatal("DeepSeek native search was mislabeled")
			}
		})
	}
}

func TestStructuredSearchFailureIsExplicitAndDoesNotInventReferences(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "1")
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/tools/search" {
			http.Error(w, "unavailable", 503)
			return
		}
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"普通回答 https://invented.example/fake"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_KIMI_API_KEY", "test-key")
	t.Setenv("GEO_KIMI_API_BASE", api.URL)
	result := (&Queue{}).applyAPIFallback("kimi", "问题", map[string]any{"ok": false, "error_code": "site_timeout"})
	if result["ok"] != true || result["web_search_status"] != "failed" || result["web_search_error_code"] != "search_request_failed" || result["citations_status"] != "missing" || len(result["citations"].([]map[string]any)) != 0 {
		t.Fatalf("unproven citations: %#v", result)
	}
}

func TestRetrievedAnswerRespectsOverallCancellation(t *testing.T) {
	clearOfficialAPIKeys(t)
	var answers atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/tools/search" {
			time.Sleep(100 * time.Millisecond)
			return
		}
		answers.Add(1)
	}))
	defer api.Close()
	t.Setenv("GEO_KIMI_API_KEY", "test-key")
	t.Setenv("GEO_KIMI_API_BASE", api.URL)
	provider, _ := providerFor("kimi")
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	_, _, _, err := postRetrievedAnswer(ctx, provider, "问题", map[string]any{})
	if err == nil || answers.Load() != 0 {
		t.Fatal("answer request started after the shared deadline")
	}
}
