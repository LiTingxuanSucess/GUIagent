package jobs

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestAPIRejectionClassification(t *testing.T) {
	for _, test := range []struct {
		message string
		code    string
		blocked bool
	}{
		{"HTTP 403: The request failed because your account has an overdue balance", "api_quota_exhausted", true},
		{"HTTP 403: AccountOverdueError", "api_quota_exhausted", true},
		{"HTTP 403: OperationDenied.ServiceOverdue", "api_quota_exhausted", true},
		{"HTTP 402: quota has been exhausted", "api_quota_exhausted", true},
		{"HTTP 403: AccessDenied", "api_auth_failed", true},
		{"HTTP 401: invalid api key", "api_auth_failed", true},
		{"HTTP 400: The request was rejected because it was considered high risk", "api_risk_control", true},
		{"HTTP 429: rate limited", "api_rate_limited", false},
		{"HTTP 503: temporarily unavailable", "api_request_failed", false},
	} {
		t.Run(test.code+test.message, func(t *testing.T) {
			err := fmt.Errorf("%s", test.message)
			if apiFailureCode(err) != test.code || permanentAPIRejection(err) != test.blocked {
				t.Fatalf("classification=%s blocked=%t", apiFailureCode(err), permanentAPIRejection(err))
			}
		})
	}
}

func TestPermanentRejectionDoesNotRepeatPlainAPICall(t *testing.T) {
	for _, test := range []struct {
		status  int
		message string
		code    string
	}{
		{403, "The request failed because your account has an overdue balance", "api_quota_exhausted"},
		{401, "invalid api key", "api_auth_failed"},
		{400, "The request was rejected because it was considered high risk", "api_risk_control"},
	} {
		t.Run(test.code, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_API_ENABLE_SEARCH", "1")
			var calls atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.URL.Path == "/responses" {
					w.WriteHeader(test.status)
					_, _ = fmt.Fprintf(w, `{"error":{"message":%q}}`, test.message)
					return
				}
				_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"不应调用到这里"}}]}`))
			}))
			defer api.Close()
			t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
			t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
			result := (&Queue{}).applyAPIFallback("doubao", "问题", siteError("doubao", "问题", "site_timeout", "网页超时", true, 60000))
			if calls.Load() != 1 || result["ok"] == true || result["api_error_code"] != test.code || result["api_error"] == "" {
				t.Fatalf("calls=%d code=%v ok=%v", calls.Load(), result["api_error_code"], result["ok"])
			}
			if strings.Contains(fmt.Sprint(result["api_error"]), "test-key") {
				t.Fatal("public error leaked a credential")
			}
		})
	}
}

func TestEnabledSearchWithoutSourcesIsAnExplicitQualityIssue(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_API_ENABLE_SEARCH", "1")
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		messages, _ := body["messages"].([]any)
		if len(messages) != 1 || messages[0].(map[string]any)["content"] != "问题" {
			t.Error("native search changed the user's question")
		}
		if body["web_search_options"].(map[string]any)["enable"] != true {
			t.Error("native search request was not enabled")
		}
		_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop","message":{"content":"普通回答，没有检索来源。"}}]}`))
	}))
	defer api.Close()
	t.Setenv("GEO_YUANBAO_API_KEY", "test-key")
	t.Setenv("GEO_YUANBAO_API_BASE", api.URL)
	result := (&Queue{}).applyAPIFallback("yuanbao", "问题", map[string]any{"ok": false, "error_code": "platform_busy"})
	if result["ok"] != true || result["web_search_status"] != "not_confirmed" || result["web_search_error_code"] != "search_not_confirmed" || result["citations_origin"] != "none" {
		t.Fatal("unconfirmed search was mislabeled")
	}
}
