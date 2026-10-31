package jobs

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestPerSiteFallbackOverrideRespectsGlobalAndKeys(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_FALLBACK", "0")
	for _, provider := range apiProviders {
		t.Setenv(provider.keyEnv, "test-key")
	}
	t.Setenv("GEO_DEEPSEEK_API_FALLBACK", "1")
	t.Setenv("GEO_DOUBAO_API_FALLBACK", "1")
	for _, provider := range apiProviders {
		want := provider.id == "deepseek" || provider.id == "doubao"
		if officialAPIConfigured(provider.id) != want {
			t.Fatalf("unexpected per-site enablement: %s", provider.id)
		}
	}
	t.Setenv("GEO_API_FALLBACK", "1")
	for _, provider := range apiProviders {
		if !officialAPIConfigured(provider.id) {
			t.Fatalf("global fallback did not enable %s", provider.id)
		}
	}
	t.Setenv("GEO_KIMI_API_FALLBACK", "0")
	if officialAPIConfigured("kimi") || !officialAPIConfigured("qianwen") {
		t.Fatal("per-site opt-out changed another site")
	}
	t.Setenv("GEO_DEEPSEEK_API_KEY", "")
	if officialAPIConfigured("deepseek") {
		t.Fatal("fallback without a key was enabled")
	}
}

func TestDeepSeekFallbackUsesNativeSearchAndMarksOrigin(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_FALLBACK", "1")
	t.Setenv("GEO_API_ENABLE_SEARCH", "1")
	t.Setenv("GEO_DEEPSEEK_API_KEY", "test-key")
	t.Setenv("GEO_DEEPSEEK_API_MODEL", "deepseek-v4-pro")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.URL.Path == "/v1/tools/search" {
			if body["query"] != "问题" || body["tools"] != nil {
				t.Errorf("search request = %#v", body)
			}
			_, _ = w.Write([]byte(`{"results":[{"title":"官方来源","url":"https://example.com/source","snippet":"摘要","summary":"长摘要"}]}`))
			return
		}
		if r.URL.Path != "/chat/completions" {
			t.Errorf("unsupported endpoint: %s", r.URL.Path)
		}
		thinking, _ := body["thinking"].(map[string]any)
		if thinking["type"] != "disabled" || body["max_tokens"] != float64(4096) || body["tools"] != nil {
			t.Errorf("request options = %#v", body)
		}
		_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop","message":{"content":"见 https://example.com/model-written ，仅为正文链接。","reasoning_content":"不要返回的思考内容"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DEEPSEEK_API_BASE", api.URL)
	failure := siteError("deepseek", "问题", "platform_auth_unavailable", "登录失效", true, 400)
	failure["diagnostic_id"] = "test-diagnostic"
	failure["web_recovery"] = map[string]any{"status": "failed", "attempts": 1, "verified": true, "resumed": false, "cli_call_id": "call_failed_resume", "error_code": "cli_resume_failed"}
	q := &Queue{}
	result := q.applyAPIFallback("deepseek", "问题", failure)
	if result["ok"] != true || result["source"] != "official_api" || result["answer_origin"] != "official_api" || result["degraded"] != true {
		t.Fatalf("source = %#v", result)
	}
	if result["url"] != "" || result["share_status"] != "not_supported" || result["share_error_code"] != "api_no_web_share" || result["web_search_status"] != "used" || result["web_search_mode"] != "native_search_api" || result["web_search_provider"] != "deepseek_search" || result["native_web_search_supported"] != true || result["citations_status"] == "missing" {
		t.Fatalf("quality = %#v", result)
	}
	citations := result["citations"].([]map[string]any)
	if len(citations) != 1 || citations[0]["url"] != "https://example.com/source" || calls.Load() != 2 || strings.Contains(result["answer"].(string), "思考内容") {
		t.Fatal("native search call, invented citation or reasoning leak")
	}
	if result["web_error_code"] != "platform_auth_unavailable" || result["diagnostic_id"] != "test-diagnostic" || result["web_elapsed_ms"] != int64(400) {
		t.Fatalf("web evidence lost: %#v", result)
	}
	if recovery, ok := result["web_recovery"].(map[string]any); !ok || recovery["status"] != "failed" || recovery["verified"] != true || recovery["resumed"] != false || recovery["cli_call_id"] != "call_failed_resume" {
		t.Fatal("API delivery discarded the preceding webpage recovery evidence")
	}
	web := map[string]any{"ok": true, "answer_origin": "web", "answer": "实际网页回答"}
	got := q.applyAPIFallback("deepseek", "问题", web)
	if got["answer_origin"] != "web" || calls.Load() != 2 {
		t.Fatal("successful web answer triggered API")
	}
}

func TestDoubaoRiskCooldownUsesAPIWithoutStartingBrowser(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_FALLBACK", "1")
	t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "1")
	t.Setenv("GEO_RISK_COOLDOWN_SEC", "600")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/responses" {
			t.Errorf("search endpoint = %s", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"API回答","annotations":[{"type":"url_citation","title":"真实引用","url":"https://example.com/source"}]}]}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	q := &Queue{DataDir: t.TempDir(), Python: "/must-not-start", siteSem: make(chan struct{}, 6), apiSem: make(chan struct{}, 6)}
	q.recordSiteCooldown("doubao", siteError("doubao", "问题", "platform_risk_control", "验证码", false, 100))
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	result := q.runOneSiteContext(ctx, "doubao", "问题", "risk-fallback", 1)
	if result["ok"] != true || result["source"] != "official_api" || result["web_skipped"] != true || result["web_error_code"] != "platform_risk_control" || result["citations_status"] != "available" || calls.Load() != 1 {
		t.Fatalf("cooldown recovery = %#v", result)
	}
}

func TestFallbackWebBudgetDoesNotReserveAPIExecutionTime(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_FALLBACK", "1")
	t.Setenv("GEO_DEEPSEEK_API_KEY", "test-key")
	t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
	t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "110")
	t.Setenv("GEO_DOUBAO_API_TIMEOUT_SEC", "60")
	t.Setenv("GEO_MULTI_WEB_BUDGET_SEC", "240")
	t.Setenv("GEO_DEEPSEEK_WEB_BUDGET_SEC", "90")
	t.Setenv("GEO_DOUBAO_WEB_BUDGET_SEC", "90")
	t.Setenv("GEO_API_RESERVE_SEC", "35")
	start := time.Now()
	for _, site := range []string{"deepseek", "doubao"} {
		for _, count := range []int{1, 6} {
			ctx, cancel := context.WithDeadline(context.Background(), start.Add(requestBudget(count)))
			deadline := requestWebDeadline(ctx, site, start.Add(240*time.Second), start, count)
			cancel()
			if deadline.Sub(start) != 90*time.Second {
				t.Fatalf("%s / %d: web budget %s", site, count, deadline.Sub(start))
			}
		}
		ctx, cancel := context.WithDeadline(context.Background(), start.Add(120*time.Second))
		late := requestWebDeadline(ctx, site, start.Add(240*time.Second), start.Add(70*time.Second), 1)
		cancel()
		if late != start.Add(120*time.Second) {
			t.Fatal("webpage budget subtracted API reserve or ignored an explicit caller deadline")
		}
		fresh := requestWebDeadline(context.Background(), site, start.Add(300*time.Second), start.Add(70*time.Second), 1)
		if fresh != start.Add(160*time.Second) {
			t.Fatal("queue waiting consumed a new webpage execution window")
		}
	}
}

func TestDoubaoAPIAndWebBudgetConfigurationAreIndependent(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_SINGLE_TOTAL_TIMEOUT_SEC", "120")
	t.Setenv("GEO_MULTI_TOTAL_TIMEOUT_SEC", "300")
	t.Setenv("GEO_API_FALLBACK", "1")
	t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
	t.Setenv("GEO_DOUBAO_API_TIMEOUT_SEC", "120")
	t.Setenv("GEO_DOUBAO_SINGLE_TOTAL_TIMEOUT_SEC", "210")
	t.Setenv("GEO_DOUBAO_WEB_BUDGET_SEC", "60")
	if apiTimeout("doubao") != 120*time.Second || apiTimeout("deepseek") != 60*time.Second {
		t.Fatal("extended Doubao timeout was ignored or changed DeepSeek")
	}
	if requestBudgetForSites([]string{"doubao"}) != 210*time.Second || requestBudgetForSites([]string{"kimi"}) != 120*time.Second || requestBudgetForSites([]string{"doubao", "kimi"}) != 300*time.Second {
		t.Fatal("single-site override changed unrelated request budgets")
	}
	start := time.Now()
	job := &Job{CreatedAt: start.Format(time.RFC3339Nano), Sites: []string{"doubao"}}
	if jobDeadline(job).Sub(start) != 210*time.Second {
		t.Fatal("restored job ignored the per-site request budget")
	}
	for _, count := range []int{1, 6} {
		overall := start.Add(300 * time.Second)
		if count == 1 {
			overall = jobDeadline(job)
		}
		ctx, cancel := context.WithDeadline(context.Background(), overall)
		webStart := start.Add(20 * time.Second)
		webEnd := requestWebDeadline(ctx, "doubao", webStart.Add(240*time.Second), webStart, count)
		cancel()
		if webEnd.Sub(webStart) != 60*time.Second {
			t.Fatal("API timeout configuration truncated the webpage budget")
		}
	}
	ctx, cancel := context.WithDeadline(context.Background(), start.Add(145*time.Second))
	defer cancel()
	webEnd := requestWebDeadline(ctx, "doubao", start.Add(240*time.Second), start.Add(20*time.Second), 1)
	if webEnd != start.Add(80*time.Second) {
		t.Fatal("API reserve shortened an independently bounded webpage attempt")
	}
	job.DeadlineAt = start.Add(90 * time.Second).Format(time.RFC3339Nano)
	if jobDeadline(job).Sub(start) != 90*time.Second {
		t.Fatal("reading a legacy estimate mutated its stored value")
	}
}

func TestOfficialAPIHTTPUsesRequestContextForBothEndpoints(t *testing.T) {
	if officialAPIHTTP.Timeout != 0 {
		t.Fatal("HTTP client would truncate the configured API budget")
	}
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(200 * time.Millisecond):
		}
	}))
	defer api.Close()
	for _, endpoint := range []string{"responses", "chat"} {
		t.Run(endpoint, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()
			start := time.Now()
			var err error
			if endpoint == "responses" {
				_, err = doAPI(ctx, http.MethodPost, api.URL+"/responses", "test-key", map[string]any{"input": "test"})
			} else {
				_, _, err = postChatCompletion(ctx, api.URL, "test-key", "test-model", "test", nil)
			}
			if err == nil || ctx.Err() == nil || time.Since(start) > time.Second {
				t.Fatal("API endpoint did not stop at the request context deadline")
			}
		})
	}
}

func TestTruncatedChatAnswerDoesNotBecomeSuccess(t *testing.T) {
	for _, reason := range []string{"length", "content_filter"} {
		payload := []byte(`{"choices":[{"finish_reason":"` + reason + `","message":{"content":"不完整回答"}}]}`)
		if _, _, err := parseChatCompletion(payload); err == nil {
			t.Fatalf("finish_reason=%s accepted as success", reason)
		}
	}
	for _, status := range []string{"incomplete", "failed"} {
		payload := []byte(`{"status":"` + status + `","output":[{"type":"message","content":[{"type":"output_text","text":"不完整回答"}]}]}`)
		if _, _, err := parseResponses(payload); err == nil {
			t.Fatalf("Responses status=%s accepted as success", status)
		}
	}
}
