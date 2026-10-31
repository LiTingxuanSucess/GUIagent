package jobs

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func clearOfficialAPIKeys(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"GEO_API_FALLBACK",
		"GEO_API_FALLBACK_WEB_RETRIES",
		"GEO_API_ENABLE_SEARCH",
		"GEO_API_ADMISSION_WAIT_SEC",
		"GEO_API_MIN_EXECUTION_SEC",
		"GEO_DOUBAO_API_MIN_EXECUTION_SEC",
		"GEO_DEEPSEEK_API_MIN_EXECUTION_SEC",
		"GEO_KIMI_API_MIN_EXECUTION_SEC",
		"GEO_QIANWEN_API_MIN_EXECUTION_SEC",
		"GEO_YIYAN_API_MIN_EXECUTION_SEC",
		"GEO_YUANBAO_API_MIN_EXECUTION_SEC",
		"GEO_DOUBAO_API_FALLBACK",
		"GEO_YUANBAO_API_FALLBACK",
		"GEO_KIMI_API_FALLBACK",
		"GEO_YIYAN_API_FALLBACK",
		"GEO_QIANWEN_API_FALLBACK",
		"GEO_DOUBAO_API_TIMEOUT_SEC",
		"GEO_DOUBAO_SINGLE_TOTAL_TIMEOUT_SEC",
		"GEO_DOUBAO_WEB_BUDGET_SEC",
		"GEO_DOUBAO_API_KEY",
		"GEO_YUANBAO_API_KEY",
		"GEO_KIMI_API_KEY",
		"GEO_YIYAN_API_KEY",
		"GEO_QIANWEN_API_KEY",
		"GEO_DEEPSEEK_API_KEY",
		"GEO_DEEPSEEK_API_FALLBACK",
		"GEO_DEEPSEEK_EXTERNAL_SEARCH",
		"GEO_STRUCTURED_SEARCH_TIMEOUT_SEC",
		"GEO_DEEPSEEK_API_TIMEOUT_SEC",
		"GEO_DEEPSEEK_API_THINKING",
		"GEO_DEEPSEEK_API_REASONING_EFFORT",
		"GEO_DEEPSEEK_SINGLE_TOTAL_TIMEOUT_SEC",
		"GEO_KIMI_API_REASONING_EFFORT",
		"GEO_KIMI_API_TIMEOUT_SEC",
		"GEO_KIMI_SINGLE_TOTAL_TIMEOUT_SEC",
		"GEO_STRUCTURED_SEARCH_LIMIT",
		"GEO_QIANWEN_API_THINKING",
		"GEO_QIANWEN_API_TIMEOUT_SEC",
		"GEO_QIANWEN_SINGLE_TOTAL_TIMEOUT_SEC",
		"GEO_YIYAN_API_TIMEOUT_SEC",
		"GEO_YIYAN_EXTERNAL_SEARCH",
		"GEO_YIYAN_API_REQUIRE_SEARCH",
		"GEO_YIYAN_SINGLE_TOTAL_TIMEOUT_SEC",
		"GEO_YUANBAO_API_REQUIRE_SEARCH",
		"GEO_YUANBAO_EXTERNAL_SEARCH",
		"GEO_YUANBAO_API_TIMEOUT_SEC",
		"GEO_YUANBAO_SINGLE_TOTAL_TIMEOUT_SEC",
		"GEO_DEEPSEEK_API_MAX_TOKENS",
		"GEO_DEEPSEEK_WEB_BUDGET_SEC",
	} {
		t.Setenv(key, "")
	}
}

func TestSiteExecutionDeadlineCapsWebpageWhenAPIConfigured(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_SITE_BUDGET_SEC", "420")
	t.Setenv("GEO_SITE_TIMEOUT_SEC", "300")
	t.Setenv("GEO_API_FALLBACK_WEB_BUDGET_SEC", "240")
	start := time.Now()
	uncapped := siteExecutionDeadline("deepseek", start, 2)
	if got := uncapped.Sub(start); got != 10*time.Minute {
		t.Fatalf("budget without a key = %s, want 10m", got)
	}
	t.Setenv("GEO_DEEPSEEK_API_KEY", "test-key")
	capped := siteExecutionDeadline("deepseek", start, 2)
	if got := capped.Sub(start); got != 240*time.Second {
		t.Fatalf("budget with a key = %s, want 240s", got)
	}
}

func TestApplyAPIFallbackUsesOfficialAnswerAfterSearchRejection(t *testing.T) {
	clearOfficialAPIKeys(t)
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/chat/completions" {
			t.Errorf("path = %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer test-key" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		var body map[string]any
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		if _, ok := body["enable_search"]; ok {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":{"message":"unknown field enable_search"}}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"哈希是固定长度指纹。"}}],"search_results":[{"title":"示例","url":"https://example.com/a"}]}`))
	}))
	defer srv.Close()
	t.Setenv("GEO_QIANWEN_API_KEY", "test-key")
	t.Setenv("GEO_QIANWEN_API_BASE", srv.URL)
	t.Setenv("GEO_QIANWEN_API_MODEL", "qwen-plus")

	got := (&Queue{}).applyAPIFallback("qianwen", "什么是哈希", map[string]any{
		"ok": false, "error": "网页失败", "platform_retries": 3, "account_id": "acct",
	})
	if ok, _ := got["ok"].(bool); !ok {
		t.Fatalf("result = %#v", got)
	}
	if got["answer"] != "哈希是固定长度指纹。" || got["source"] != "official_api" {
		t.Fatalf("answer = %#v", got)
	}
	if got["model"] != "qwen-plus" || got["platform_retries"] != 3 || got["account_id"] != "acct" {
		t.Fatalf("meta = %#v", got)
	}
	if got["site_name"] != "通义千问" {
		t.Fatalf("site_name = %#v", got["site_name"])
	}
	citations, _ := got["citations"].([]map[string]any)
	if len(citations) != 1 || citations[0]["url"] != "https://example.com/a" {
		t.Fatalf("citations = %#v", got["citations"])
	}
	if calls != 2 {
		t.Fatalf("calls = %d, want search attempt then plain retry", calls)
	}
}

func TestApplyAPIFallbackKeepsWebpageFailure(t *testing.T) {
	clearOfficialAPIKeys(t)
	failure := map[string]any{"ok": false, "error": "网页失败", "error_code": "site_timeout"}
	got := (&Queue{}).applyAPIFallback("deepseek", "问题", failure)
	if got["error_code"] != "site_timeout" {
		t.Fatalf("missing key should keep the webpage error, got %#v", got)
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"error":{"message":"invalid api key"}}`, http.StatusUnauthorized)
	}))
	defer srv.Close()
	t.Setenv("GEO_DEEPSEEK_API_KEY", "bad")
	t.Setenv("GEO_DEEPSEEK_API_BASE", srv.URL)
	got = (&Queue{}).applyAPIFallback("deepseek", "问题", failure)
	if got["error_code"] != "site_timeout" {
		t.Fatalf("api error should keep the webpage error, got %#v", got)
	}

	t.Setenv("GEO_API_FALLBACK", "off")
	got = (&Queue{}).applyAPIFallback("deepseek", "问题", map[string]any{"ok": false, "error_code": "platform_busy"})
	if got["error_code"] != "platform_busy" {
		t.Fatalf("disabled fallback changed the result: %#v", got)
	}
}

func TestScrapeRetryLimitWithAPIRetriesOnlyTransientFailures(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_KIMI_API_KEY", "test-key")
	busy := &scrapeFailure{code: "platform_busy", raw: "busy", retryable: true}
	if got := scrapeRetryLimit("kimi", busy); got != 1 {
		t.Fatalf("kimi busy retries = %d, want 1", got)
	}
	timeout := &scrapeFailure{code: "site_timeout", raw: "超时", retryable: true}
	if got := scrapeRetryLimit("kimi", timeout); got != 1 {
		t.Fatalf("kimi timeout retries = %d, want 1", got)
	}
	captcha := &scrapeFailure{code: "platform_risk_control", raw: "captcha", retryable: true}
	if got := scrapeRetryLimit("kimi", captcha); got != 0 {
		t.Fatalf("captcha retries = %d, want 0", got)
	}
	if got := scrapeRetryLimit("kimi", assertError("BrowserContext.new_page: Failed to open a new tab")); got != 1 {
		t.Fatalf("raw browser error retries = %d, want 1", got)
	}
	t.Setenv("GEO_API_FALLBACK_WEB_RETRIES", "2")
	if got := scrapeRetryLimit("kimi", busy); got != 2 {
		t.Fatalf("override retries = %d, want 2", got)
	}
}

func TestParseDotEnvReadsAPIKeys(t *testing.T) {
	vals := parseDotEnv("# comment\nGEO_DOUBAO_API_KEY=\"ark-key\"\nGEO_API_FALLBACK_WEB_RETRIES=3\n\nnot a pair\n")
	if vals["GEO_DOUBAO_API_KEY"] != "ark-key" || vals["GEO_API_FALLBACK_WEB_RETRIES"] != "3" {
		t.Fatalf("vals = %#v", vals)
	}
}

func TestLiveOfficialSearchLinks(t *testing.T) {
	if os.Getenv("GEO_LIVE_API") != "1" {
		t.Skip("set GEO_LIVE_API=1 to call the six provider APIs")
	}
	prompt := "2026年10月9日北京的天气怎么样？请根据联网搜索回答，并给出参考网页。"
	for _, id := range []string{"doubao", "yuanbao", "kimi", "yiyan", "qianwen", "deepseek"} {
		t.Run(id, func(t *testing.T) {
			answer, model, citations, err := askOfficialAPI(context.Background(), id, prompt)
			if err != nil {
				t.Fatalf("error: %v", err)
			}
			answer = strings.TrimSpace(answer)
			if answer == "" {
				t.Fatal("empty answer")
			}
			head := answer
			if len(head) > 80 {
				head = head[:80]
			}
			t.Logf("model=%s citations=%d head=%s", model, len(citations), strings.ReplaceAll(head, "\n", " "))
			if len(citations) == 0 {
				t.Errorf("no search links")
			}
			for i, item := range citations {
				if i >= 2 {
					break
				}
				t.Logf("link=%s", item["url"])
			}
		})
	}
}

func TestLiveYiyanPublicSearchLinks(t *testing.T) {
	if os.Getenv("GEO_LIVE_API") != "1" {
		t.Skip("set GEO_LIVE_API=1 to call Yiyan web search")
	}
	answer, model, citations, err := askOfficialAPI(context.Background(), "yiyan", "2026年诺贝尔物理学奖得主是谁？请给出新闻出处链接。")
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("model=%s citations=%d head=%s", model, len(citations), strings.ReplaceAll(strings.TrimSpace(answer), "\n", " ")[:80])
	if len(citations) == 0 {
		t.Fatal("no search links")
	}
	t.Logf("link=%s", citations[0]["url"])
}

func TestParseChatCompletionAcceptsTextParts(t *testing.T) {
	answer, citations, err := parseChatCompletion([]byte(`{
		"choices":[{"message":{"content":[{"type":"text","text":"第一段"},{"type":"text","text":"第二段"}]}}],
		"search_info":{"search_results":[{"title":"标题","url":"https://a.example/x","site_name":"示例站"}]}
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if answer != "第一段\n第二段" {
		t.Fatalf("answer = %q", answer)
	}
	if len(citations) != 1 || citations[0]["title"] != "标题" || citations[0]["site_name"] != "示例站" {
		t.Fatalf("citations = %#v", citations)
	}
	if _, _, err = parseChatCompletion([]byte(`{"choices":[{"message":{"content":""}}]}`)); err == nil {
		t.Fatal("empty content should fail")
	}
}
