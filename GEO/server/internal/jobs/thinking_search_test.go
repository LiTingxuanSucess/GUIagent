package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestDashScopeThinkingPreservesSourcesAndRejectsTruncation(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_QIANWEN_API_THINKING", "1")
	for _, finish := range []string{"stop", "length"} {
		t.Run(finish, func(t *testing.T) {
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var request map[string]any
				_ = json.NewDecoder(r.Body).Decode(&request)
				if request["parameters"].(map[string]any)["enable_thinking"] != true {
					t.Error("thinking was not enabled in native DashScope parameters")
				}
				refs := make([]map[string]any, 30)
				for i := range refs {
					refs[i] = map[string]any{"url": fmt.Sprintf("https://example.com/source/%d", i)}
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"output": map[string]any{
					"choices":     []any{map[string]any{"finish_reason": finish, "message": map[string]any{"content": "最终回答", "reasoning_content": "内部推理"}}},
					"search_info": map[string]any{"search_results": refs},
				}})
			}))
			defer api.Close()
			answer, sources, err := postDashScopeSearch(context.Background(), api.URL, "test-key", "qwen-plus", "问题")
			if finish == "length" {
				if err == nil || answer != "" {
					t.Fatal("truncated answer was accepted")
				}
			} else if err != nil || answer != "最终回答" || len(sources) != 30 {
				t.Fatalf("lost answer or sources: answer=%q count=%d err=%v", answer, len(sources), err)
			}
		})
	}
}

func TestYiyanSearchKeepsMultipleReferenceBatches(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("data: {\"search_results\":[{\"url\":\"https://example.com/first\"}],\"choices\":[{\"delta\":{\"content\":\"最终\"}}]}\n\n" +
			"data: {\"search_results\":[{\"url\":\"https://example.com/second\"}],\"choices\":[{\"delta\":{\"content\":\"回答\"}}]}\n\ndata: [DONE]\n"))
	}))
	defer api.Close()
	answer, sources, err := postYiyanSearch(context.Background(), api.URL, "test-key", "model", "问题")
	if err != nil || answer != "最终回答" || len(sources) != 2 {
		t.Fatalf("reference batches lost: answer=%q count=%d err=%v", answer, len(sources), err)
	}
}
