package jobs

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

// Exercise the complete site runner: a failed webpage must still return the
// official Doubao answer, including when the API provides no reference links.
func TestDoubaoWebFailuresReturnAPIAnswer(t *testing.T) {
	for _, code := range []string{"site_timeout", "platform_busy", "platform_auth_unavailable", "platform_risk_control"} {
		t.Run(code, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_API_FALLBACK", "0")
			t.Setenv("GEO_DOUBAO_API_FALLBACK", "1")
			t.Setenv("GEO_DOUBAO_API_KEY", "test-key")
			t.Setenv("GEO_API_ENABLE_SEARCH", "0")
			t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
			t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
			var calls atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop","message":{"content":"豆包官方 API 完整回答"}}]}`))
			}))
			defer api.Close()
			t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
			root := t.TempDir()
			script := filepath.Join(root, "failed_web.sh")
			body := "#!/bin/sh\nprintf '%s\\n' '{\"ok\":false,\"error_code\":\"" + code + "\",\"error\":\"" + code + "\",\"retryable\":false}'\n"
			if err := os.WriteFile(script, []byte(body), 0700); err != nil {
				t.Fatal(err)
			}
			q := &Queue{GeoRoot: root, Python: "/bin/sh", Script: script, DataDir: root, siteSem: make(chan struct{}, 2), cloudSem: make(chan struct{}, 2)}
			got := q.runOneSite("doubao", "当前问题", "web-failed-"+code)
			if got["ok"] != true || got["answer"] != "豆包官方 API 完整回答" || got["answer_origin"] != "official_api" || got["web_error_code"] != code || calls.Load() != 1 {
				t.Fatalf("web failure lost API answer: %#v, calls=%d", got, calls.Load())
			}
			if got["share_status"] != "not_supported" || got["citations_status"] != "missing" || len(got["citation_details"].([]map[string]any)) != 0 {
				t.Fatalf("API answer quality metadata is inaccurate: %#v", got)
			}
		})
	}
}
