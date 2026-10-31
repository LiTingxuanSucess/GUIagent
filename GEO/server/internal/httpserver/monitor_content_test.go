package httpserver

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeMonitorJSON(t *testing.T, path string, body any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestMonitorDetailLoadsAllowlistedContentWithoutExpandingList(t *testing.T) {
	h, token, root := monitorTestServer(t)
	now := time.Date(2026, 10, 16, 16, 0, 0, 0, time.FixedZone("Beijing", 8*3600)).Unix()
	prompt := "完整问题第一行\n第二行：保留中文和 <script>文本</script>"
	answer := "完整平台回答\n包含 <img src=x onerror=alert(1)> 原文"
	row := map[string]any{"id": "content_job", "created": now, "status": "succeeded", "sites": []string{"wenxinyiyan", "yuanbao"}, "prompt": "INDEX_PROMPT_MUST_NOT_LEAK", "request_id": "INDEX_REQUEST_ID_MUST_NOT_LEAK", "results": []map[string]any{
		{"site": "wenxinyiyan", "ok": true, "citations": 8, "answer": "INDEX_ANSWER_MUST_NOT_LEAK", "share_url": "https://example.org/index-leak", "citation_details": []map[string]any{{"url": "https://example.org/index-leak"}}, "attempts": []map[string]any{{"channel": "web", "ok": true}, {"channel": "cursor", "ok": nil, "status": "unknown"}}},
		{"site": "yuanbao", "ok": true, "citations": 1},
	}}
	writeMonitorJSON(t, filepath.Join(root, "data/monitor-requests.json"), map[string]any{"updated_at": now, "rows": []any{row}})
	writeMonitorJSON(t, filepath.Join(root, "data/jobs/content_job.json"), map[string]any{
		"job_id": "content_job", "request_id": "customer-request-42", "prompt": prompt,
		"ak": "CREDENTIAL_AK_NEVER_RETURN", "sk": "CREDENTIAL_SK_NEVER_RETURN", "cookie": "CREDENTIAL_COOKIE_NEVER_RETURN",
		"results": []map[string]any{
			{"site_id": "yuanbao", "answer": "另一平台完整回答", "url": "javascript:alert(1)", "citations": []any{"http://127.0.0.1:9222/json/version", "https://example.org/yuanbao"}},
			{"site_id": "yiyan", "answer": "raw markdown answer", "answer_plain": answer, "answer_raw": "RAW_VARIANT_MUST_NOT_LEAK", "url": "https://wenxin.baidu.com/share/fixture", "cookie": "CREDENTIAL_NESTED_NEVER_RETURN", "run_dir": "/private/debug/path", "cdp_url": "http://127.0.0.1:9222",
				"citation_details": []any{map[string]any{"url": "https://example.org/a", "title": "标题<script>alert(1)</script>", "site_name": "来源站", "headers": "CREDENTIAL_HEADERS_NEVER_RETURN"}, map[string]any{"url": "https://secret:password@example.org/private"}, map[string]any{"url": "file:///etc/passwd"}, map[string]any{"url": "https://10.0.0.1/internal"}},
				"citations":        []any{"https://example.org/a", "https://example.net/b", map[string]any{"url": "https://example.com/c", "title": "对象形式"}, "data:text/html,<script>alert(1)</script>", "https://example.test:9222/json"}},
		},
	})
	path := "/api/admin/monitor/requests/content_job"
	if w := monitorTestRequest(h, "", http.MethodGet, path); w.Code != 401 || strings.Contains(w.Body.String(), prompt) {
		t.Fatal("unauthenticated user could read job content")
	}
	w := monitorTestRequest(h, token, http.MethodGet, path)
	if w.Code != 200 || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("detail request failed: %d %s", w.Code, w.Body.String())
	}
	var body monitorRequestContent
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if !body.ContentAvailable || body.Prompt != prompt || body.RequestID != "customer-request-42" || len(body.Results) != 2 || body.Results[0].Answer != answer || body.Results[0].ShareURL != "https://wenxin.baidu.com/share/fixture" || body.Results[0].Citations != 8 || len(body.Results[0].CitationDetails) != 3 || len(body.Results[0].Attempts) != 2 {
		t.Fatalf("full content, result association, metadata or references were lost: %+v", body)
	}
	if body.Results[1].ShareURL != "" || len(body.Results[1].CitationDetails) != 1 || body.Results[1].Answer != "另一平台完整回答" {
		t.Fatal("unsafe link was retained or reordered results received another platform's answer")
	}
	for _, forbidden := range []string{"CREDENTIAL_", "INDEX_", "RAW_VARIANT_", "127.0.0.1", "10.0.0.1", "9222", "secret:password", "file:///", "javascript:", "data:text", "<script>", "<img", "/private/debug/path", "cdp_url"} {
		if strings.Contains(w.Body.String(), forbidden) {
			t.Fatalf("non-allowlisted content or unescaped HTML leaked: %q", forbidden)
		}
	}
	list := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests?window=today")
	for _, forbidden := range []string{"INDEX_", "完整问题", "完整平台回答", "customer-request-42", `"answer"`, `"prompt"`, `"share_url"`, `"citation_details"`} {
		if strings.Contains(list.Body.String(), forbidden) {
			t.Fatalf("full content was included in request list: %q", forbidden)
		}
	}
}

func TestMonitorDetailSupportsLegacyAndUnavailableContent(t *testing.T) {
	h, token, root := monitorTestServer(t)
	data := t.TempDir()
	t.Setenv("GEO_DATA_DIR", data)
	now := time.Now().Unix()
	rows := []any{}
	for _, id := range []string{"legacy", "missing", "corrupt", "mismatched", "symlink"} {
		rows = append(rows, map[string]any{"id": id, "created": now, "sites": []string{"doubao"}, "results": []map[string]any{{"site": "doubao", "ok": true, "citations": 2}}})
	}
	writeMonitorJSON(t, filepath.Join(data, "monitor-requests.json"), map[string]any{"updated_at": now, "rows": rows})
	writeMonitorJSON(t, filepath.Join(data, "jobs/legacy.json"), map[string]any{"results": []map[string]any{{"site": "doubao", "prompt": "旧记录问题", "answer_raw": "旧记录回答", "share_url": "https://www.doubao.com/thread/fixture", "citation_details": []any{}, "citations": []any{"https://example.org/source", "https://example.org/source", map[string]any{"url": "https://example.org/source", "title": "补齐来源标题"}}}}})
	writeMonitorJSON(t, filepath.Join(data, "jobs/mismatched.json"), map[string]any{"job_id": "another-id", "prompt": "WRONG_JOB_CONTENT"})
	if err := os.WriteFile(filepath.Join(data, "jobs/corrupt.json"), []byte("{broken"), 0600); err != nil {
		t.Fatal(err)
	}
	private := filepath.Join(root, "private.json")
	writeMonitorJSON(t, private, map[string]any{"prompt": "PRIVATE_SYMLINK_CONTENT"})
	if err := os.Symlink(private, filepath.Join(data, "jobs/symlink.json")); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"legacy", "missing", "corrupt", "mismatched", "symlink"} {
		w := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests/"+id)
		var detail monitorRequestContent
		if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil || w.Code != 200 || len(detail.Results) != 1 {
			t.Fatalf("legacy metadata was lost: %s %d %v", id, w.Code, err)
		}
		if id == "legacy" {
			if !detail.ContentAvailable || detail.Prompt != "旧记录问题" || detail.RequestID != "" || detail.Results[0].Answer != "旧记录回答" || len(detail.Results[0].CitationDetails) != 1 || detail.Results[0].CitationDetails[0].Title != "补齐来源标题" {
				t.Fatalf("legacy content was not normalized: %+v", detail)
			}
		} else if detail.ContentAvailable || detail.Prompt != "" || detail.Results[0].Answer != "" {
			t.Fatalf("unavailable or mismatched job exposed unrelated content: %s %+v", id, detail)
		}
	}
	writeMonitorJSON(t, filepath.Join(data, "jobs/unindexed.json"), map[string]any{"job_id": "unindexed", "prompt": "UNINDEXED_CONTENT"})
	if w := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests/unindexed"); w.Code != 404 || strings.Contains(w.Body.String(), "UNINDEXED_CONTENT") {
		t.Fatal("an unindexed arbitrary job file was read")
	}
}

func TestMonitorPublicURLRejectsUnsafeLinks(t *testing.T) {
	for _, raw := range []string{"javascript:alert(1)", "data:text/html,test", "file:///etc/passwd", "//example.org/path", "https://user:pass@example.org/path", "http://127.0.0.1/a", "http://127.1/a", "http://2130706433/a", "http://0x7f.0.0.1/a", "http://[::1]/a", "http://[::ffff:127.0.0.1]/a", "http://[fd00::1]/a", "http://169.254.169.254/latest", "http://10.0.0.1/a", "http://172.16.1.1/a", "http://192.168.1.1/a", "https://localhost/a", "https://host.local/a", "https://host.internal/a", "https://example.org:9222/json/version", "https://example.org/devtools/browser/id", "https://example.org/json/list", "https://example.org:99999/a"} {
		if got := monitorPublicURL(raw); got != "" {
			t.Errorf("unsafe URL accepted: %q -> %q", raw, got)
		}
	}
	for _, raw := range []string{"https://example.org/article?q=1#section", "http://example.org/source", "https://8.8.8.8/source"} {
		if got := monitorPublicURL(raw); got != raw {
			t.Errorf("valid public URL lost: %q -> %q", raw, got)
		}
	}
}
