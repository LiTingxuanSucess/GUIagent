package jobs

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type searchAuditTransport struct {
	base    http.RoundTripper
	mu      sync.Mutex
	records map[string][]map[string]any
}

func (a *searchAuditTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	record := map[string]any{"path": req.URL.Path, "method": req.Method}
	if req.Body != nil {
		body, _ := io.ReadAll(req.Body)
		req.Body.Close()
		req.Body = io.NopCloser(bytes.NewReader(body))
		var parsed any
		if json.Unmarshal(body, &parsed) == nil {
			record["request"] = parsed
		}
	}
	start := time.Now()
	resp, err := a.base.RoundTrip(req)
	if resp != nil {
		record["http_status"] = resp.StatusCode
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		resp.Body.Close()
		resp.Body = io.NopCloser(bytes.NewReader(body))
		if readErr == nil {
			var parsed any
			if json.Unmarshal(body, &parsed) == nil {
				record["response"] = parsed
			} else {
				record["response_text"] = string(body)
			}
		}
	}
	record["elapsed_ms"] = time.Since(start).Milliseconds()
	record["transport_failed"] = err != nil
	a.mu.Lock()
	a.records[req.URL.Host] = append(a.records[req.URL.Host], record)
	a.mu.Unlock()
	return resp, err
}

// Explicitly opt in: makes one normal production fallback attempt per provider.
// Private artifacts contain the test response and request JSON, never headers.
func TestLiveAPISearchAudit(t *testing.T) {
	if os.Getenv("GEO_LIVE_SEARCH_AUDIT") != "1" {
		t.Skip("live provider audit disabled")
	}
	dir := os.Getenv("GEO_SEARCH_AUDIT_DIR")
	if dir == "" {
		t.Fatal("private audit output directory required")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	transport := &searchAuditTransport{base: http.DefaultTransport, records: map[string][]map[string]any{}}
	old := officialAPIHTTP
	officialAPIHTTP = &http.Client{Transport: transport}
	defer func() { officialAPIHTTP = old }()
	prompt := os.Getenv("GEO_SEARCH_AUDIT_PROMPT")
	if prompt == "" {
		prompt = "请联网检索：中国科学院官网最近发布的一条科研新闻。给出新闻标题、发布日期和至少两个真实来源页面链接，简要说明，200字以内。不要编造链接；无法联网请明确说明。"
	}
	var wg sync.WaitGroup
	for _, provider := range apiProviders {
		if selected := os.Getenv("GEO_SEARCH_AUDIT_SITES"); selected != "" && !strings.Contains(","+selected+",", ","+provider.id+",") {
			continue
		}
		provider := provider
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), apiTimeout(provider.id))
			defer cancel()
			start := time.Now()
			answer, model, citations, evidence, err := askOfficialAPIWithSearch(ctx, provider.id, prompt)
			result := map[string]any{"site": provider.id, "model": model, "search_enabled": apiSearchEnabled(), "elapsed_ms": time.Since(start).Milliseconds(), "ok": err == nil, "answer": answer, "citations": citations}
			result["search_status"] = evidence.Status
			result["search_provider"] = evidence.Provider
			result["search_mode"] = evidence.Mode
			if err != nil {
				result["error"] = strings.ReplaceAll(err.Error(), provider.apiKey(), "REDACTED")
			}
			transport.mu.Lock()
			for host, records := range transport.records {
				if strings.Contains(provider.baseURL(), host) {
					result["requests"] = records
				}
			}
			transport.mu.Unlock()
			raw, _ := json.MarshalIndent(result, "", "  ")
			if err := os.WriteFile(filepath.Join(dir, provider.id+".json"), raw, 0600); err != nil {
				t.Error("could not persist private audit")
			}
			t.Logf("site=%s ok=%t elapsed_ms=%d citations=%d", provider.id, err == nil, time.Since(start).Milliseconds(), len(citations))
		}()
	}
	wg.Wait()
}

func TestLiveKimiStructuredSearch(t *testing.T) {
	if os.Getenv("GEO_LIVE_SEARCH_AUDIT") != "1" {
		t.Skip("live provider audit disabled")
	}
	provider, _ := providerFor("kimi")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	payload, err := doAPI(ctx, http.MethodPost, strings.TrimRight(provider.baseURL(), "/")+"/tools/search", provider.apiKey(), map[string]any{
		"text_query": "中国科学院 科研进展 2026年10月 最新新闻", "limit": 5, "timeout_seconds": 20,
	})
	if err != nil {
		t.Fatal(strings.ReplaceAll(err.Error(), provider.apiKey(), "REDACTED"))
	}
	dir := os.Getenv("GEO_SEARCH_AUDIT_DIR")
	if err := os.WriteFile(filepath.Join(dir, "kimi-structured-search.json"), payload, 0600); err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatal(err)
	}
	t.Logf("structured_sources=%d keys=%v", len(collectAPICitations(body)), func() []string {
		var keys []string
		for k := range body {
			keys = append(keys, k)
		}
		return keys
	}())
}
