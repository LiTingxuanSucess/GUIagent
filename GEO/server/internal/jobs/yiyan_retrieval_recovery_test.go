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

func TestYiyanMissingNativeSourcesCanRecoverWithoutChangingAnswerModel(t *testing.T) {
	for _, name := range []string{"recover", "native_has_sources", "search_down"} {
		t.Run(name, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_API_ENABLE_SEARCH", "1")
			t.Setenv("GEO_YIYAN_EXTERNAL_SEARCH", "1")
			var searches, regenerations atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/tools/search" {
					searches.Add(1)
					if name == "search_down" {
						w.WriteHeader(503)
						fmt.Fprint(w, `{"error":{"message":"temporarily unavailable"}}`)
						return
					}
					fmt.Fprint(w, `{"search_results":[{"url":"https://example.org/evidence","title":"retrieved source","snippet":"verified fixture evidence"}]}`)
					return
				}
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				if body["model"] != "ernie-fixture" {
					t.Error("answer model changed")
				}
				if body["stream"] == true {
					fmt.Fprintln(w, `data: {"choices":[{"delta":{"content":"native completed answer"}}]}`)
					if name == "native_has_sources" {
						fmt.Fprintln(w, `data: {"search_results":[{"url":"https://example.org/native","title":"native source"}]}`)
					}
					fmt.Fprintln(w, `data: [DONE]`)
					return
				}
				regenerations.Add(1)
				messages, _ := json.Marshal(body["messages"])
				if !strings.Contains(string(messages), "https://example.org/evidence") {
					t.Error("regenerated answer did not use retrieved sources")
				}
				fmt.Fprint(w, `{"choices":[{"finish_reason":"stop","message":{"content":"regenerated answer using evidence[1]"}}]}`)
			}))
			defer api.Close()
			t.Setenv("GEO_YIYAN_API_KEY", "yiyan-fixture")
			t.Setenv("GEO_YIYAN_API_BASE", api.URL)
			t.Setenv("GEO_YIYAN_API_MODEL", "ernie-fixture")
			t.Setenv("GEO_KIMI_API_KEY", "search-fixture")
			t.Setenv("GEO_KIMI_API_BASE", api.URL)
			r := (&Queue{}).applyAPIFallback("wenxinyiyan", "question", map[string]any{"ok": false, "error_code": "platform_busy"})
			if r["ok"] != true || r["model"] != "ernie-fixture" {
				t.Fatalf("valid answer lost: %v", r)
			}
			refs := r["citations"].([]map[string]any)
			switch name {
			case "recover":
				if len(refs) != 1 || r["web_search_provider"] != "kimi_search" || searches.Load() != 1 || regenerations.Load() != 1 {
					t.Fatalf("missing sources not recovered: %v", r)
				}
			case "native_has_sources":
				if len(refs) != 1 || searches.Load() != 0 || regenerations.Load() != 0 {
					t.Fatal("native evidence caused redundant search")
				}
			case "search_down":
				if len(refs) != 0 || r["answer"] != "native completed answer" || searches.Load() != 1 || regenerations.Load() != 0 {
					t.Fatal("search failure fabricated sources or replaced completed answer")
				}
			}
		})
	}
}
