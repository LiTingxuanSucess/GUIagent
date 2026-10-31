package httpserver

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"golang.org/x/crypto/bcrypt"
)

func monitorTestServer(t *testing.T) (http.Handler, string, string) {
	t.Helper()
	t.Setenv("GEO_DATA_DIR", "")
	root := t.TempDir()
	cfg := config.Default(root)
	hash, _ := bcrypt.GenerateFromPassword([]byte("fixture-password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	a := auth.New(cfg)
	token, err := a.Login(cfg.Admin.User, "fixture-password")
	if err != nil {
		t.Fatal(err)
	}
	return New(cfg, a, nil, nil, nil, nil, nil, fstest.MapFS{}).Handler(), token, root
}

func monitorTestRequest(h http.Handler, token, method, path string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, nil)
	if token != "" {
		r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: token})
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestMonitorRequestsPaginationFiltersAndSafeDetails(t *testing.T) {
	h, token, root := monitorTestServer(t)
	for _, path := range []string{"/api/admin/monitor/requests", "/api/admin/monitor/requests/job_a"} {
		if w := monitorTestRequest(h, "", "GET", path); w.Code != 401 {
			t.Fatalf("unprotected request metadata: %s %d", path, w.Code)
		}
	}
	if w := monitorTestRequest(h, token, "GET", "/api/admin/monitor/requests"); w.Code != 503 {
		t.Fatalf("missing index: %d", w.Code)
	}
	now := time.Date(2026, 10, 16, 17, 0, 0, 0, time.FixedZone("CST", 8*3600))
	rows := []map[string]any{}
	for i := 0; i < 61; i++ {
		rows = append(rows, map[string]any{"id": fmt.Sprintf("job_%02d", i), "created": now.Add(time.Duration(-i) * time.Minute).Unix(), "updated": now.Unix(), "status": "succeeded", "sites": []string{"yuanbao"}, "prompt": "SECRET_PROMPT", "ak": "SECRET_KEY", "results": []map[string]any{{"site": "yuanbao", "ok": true, "source": "api", "account_id": "", "attempts": []map[string]any{
			{"channel": "web", "account_id": "acct_first", "account_label": "尾号 8351", "ok": false, "status": "failed", "error_code": "platform_quota", "started_at": now.Unix(), "elapsed_ms": 100},
			{"channel": "web", "account_id": "acct_second", "account_label": "尾号 7132", "ok": true, "status": "success", "error_code": "", "started_at": now.Unix(), "elapsed_ms": 200},
			{"channel": "api", "account_id": "api:yuanbao", "account_label": "元宝 API", "ok": true, "status": "success", "error_code": "", "started_at": now.Unix(), "elapsed_ms": 500},
		}}}})
	}
	rows = append(rows, map[string]any{"id": "morning", "created": time.Date(2026, 10, 16, 10, 0, 0, 0, now.Location()).Unix(), "status": "failed", "sites": []string{"doubao"}, "results": []map[string]any{{"site": "doubao", "ok": false, "attempts": []map[string]any{{"channel": "web", "account_id": "", "ok": nil, "status": "skipped", "skipped": true, "skip_reason": "site_cooldown"}}}}})
	path := filepath.Join(root, "data/monitor-requests.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(map[string]any{"updated_at": now.Unix(), "rows": rows})
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	request := func(query string) map[string]any {
		t.Helper()
		w := monitorTestRequest(h, token, "GET", "/api/admin/monitor/requests?"+query)
		if w.Code != 200 {
			t.Fatalf("query %s returned %d: %s", query, w.Code, w.Body.String())
		}
		if w.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("request data may be cached")
		}
		if strings.Contains(w.Body.String(), "SECRET_") {
			t.Fatal("non-allowlisted data leaked")
		}
		var body map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		return body
	}
	page := request("window=afternoon&page=2&page_size=25")
	if page["total"] != float64(61) || len(page["rows"].([]any)) != 25 || page["rows"].([]any)[0].(map[string]any)["id"] != "job_25" {
		t.Fatalf("invalid pagination: %v", page)
	}
	if got := request("window=today&channel=web&account=acct_first&outcome=failed&error=platform_quota"); got["total"] != float64(61) {
		t.Fatalf("lost failed account after API recovery: %v", got)
	}
	if got := request("window=today&channel=api&account=acct_first&outcome=success"); got["total"] != float64(0) {
		t.Fatal("combined filters matched different attempts")
	}
	if got := request("window=afternoon&site=doubao"); got["total"] != float64(0) {
		t.Fatal("morning request in afternoon")
	}
	if got := request("window=today&channel=web&outcome=skipped"); got["total"] != float64(1) {
		t.Fatal("skipped filter invalid")
	}
	if got := request("window=today&account=unknown"); got["total"] != float64(1) {
		t.Fatal("unknown account filter included known identities")
	}
	w := monitorTestRequest(h, token, "GET", "/api/admin/monitor/requests/job_00")
	if w.Code != 200 || strings.Contains(w.Body.String(), "SECRET_") || !strings.Contains(w.Body.String(), "acct_first") || !strings.Contains(w.Body.String(), "api:yuanbao") {
		t.Fatalf("unsafe or incomplete detail: %d %s", w.Code, w.Body.String())
	}
	if w := monitorTestRequest(h, token, "GET", "/api/admin/monitor/requests/not_found"); w.Code != 404 {
		t.Fatal("missing request not reported")
	}
	for _, q := range []string{"page=0", "page=-1", "page_size=101", "page_size=bad", "page=999999999999999999", "window=all", "channel=secret", "outcome=all", "account=a%2Fb", "status=done"} {
		if w := monitorTestRequest(h, token, "GET", "/api/admin/monitor/requests?"+q); w.Code != 400 {
			t.Fatalf("accepted malformed filter %s: %d", q, w.Code)
		}
	}
	if w := monitorTestRequest(h, token, "POST", "/api/admin/monitor/requests"); w.Code != 405 {
		t.Fatal("write accepted")
	}
}

func TestMonitorWindowsUseBeijingMidnightAndNoon(t *testing.T) {
	now := float64(time.Date(2026, 10, 16, 5, 0, 0, 0, time.UTC).Unix()) // 13:00 Beijing
	for _, tc := range []struct {
		window string
		want   time.Time
	}{{"today", time.Date(2026, 10, 15, 16, 0, 0, 0, time.UTC)}, {"afternoon", time.Date(2026, 10, 16, 4, 0, 0, 0, time.UTC)}, {"1h", time.Unix(int64(now)-3600, 0)}} {
		if got := monitorWindowStart(tc.window, now); got != float64(tc.want.Unix()) {
			t.Fatalf("%s boundary %f", tc.window, got)
		}
	}
}

func TestMonitorRecoveryMetadataPreservesEvidenceAndDropsPrivateFields(t *testing.T) {
	h, token, root := monitorTestServer(t)
	now := time.Now().Unix()
	row := map[string]any{"id": "recovery_job", "created": now, "status": "running", "results": []any{
		map[string]any{"site": "doubao", "ok": true, "web_recovery": map[string]any{
			"status": "succeeded", "attempts": 1, "cli_call_id": "call_done", "verified": true, "resumed": true, "cli_assisted": true, "elapsed_ms": 2000,
			"prompt": "SECRET_PROMPT", "raw_output": "SECRET_OUTPUT", "phone": "13800138000"}, "attempts": []any{
			map[string]any{"channel": "cursor", "task_type": "web_recovery", "phase": "completed", "verified": true, "resume_ok": false, "ok": true, "status": "success", "error_code": "cli_resume_failed"}}},
	}, "recovery_states": map[string]any{
		"doubao":      map[string]any{"phase": "waiting_sms", "account_id": "acct_live", "account_label": "手机尾号 8000", "cli_call_id": "call_live", "updated_at": now, "raw": "SECRET_RAW"},
		"SECRET_SITE": map[string]any{"phase": "verifying"}, "kimi": map[string]any{"phase": "SECRET_PHASE"},
	}}
	writeMonitorJSON(t, filepath.Join(root, "data/monitor-requests.json"), map[string]any{"updated_at": now, "rows": []any{row}})
	for _, path := range []string{"/api/admin/monitor/requests?window=today", "/api/admin/monitor/requests/recovery_job"} {
		w := monitorTestRequest(h, token, "GET", path)
		if w.Code != 200 || strings.Contains(w.Body.String(), "SECRET") || strings.Contains(w.Body.String(), "13800138000") {
			t.Fatalf("unsafe recovery metadata %s: %d %s", path, w.Code, w.Body.String())
		}
		var document map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &document); err != nil {
			t.Fatal(err)
		}
		if rows, ok := document["rows"].([]any); ok {
			document = rows[0].(map[string]any)
		}
		states := document["recovery_states"].(map[string]any)
		if len(states) != 1 || states["doubao"].(map[string]any)["phase"] != "waiting_sms" {
			t.Fatal("live recovery evidence lost")
		}
		result := document["results"].([]any)[0].(map[string]any)
		evidence := result["web_recovery"].(map[string]any)
		if evidence["verified"] != true || evidence["resumed"] != true || evidence["cli_assisted"] != true || evidence["cli_call_id"] != "call_done" {
			t.Fatal("terminal recovery evidence lost")
		}
		attempt := result["attempts"].([]any)[0].(map[string]any)
		if attempt["verified"] != true || attempt["resume_ok"] != false || attempt["phase"] != "completed" {
			t.Fatal("CLI and resume outcomes conflated")
		}
	}
	for _, raw := range []string{`[]`, `"invalid"`, `{"status":"failed","attempts":true,"verified":"true","resumed":false,"cli_assisted":"true","elapsed_ms":1e400,"raw":"SECRET"}`} {
		var evidence monitorWebRecovery
		if err := json.Unmarshal([]byte(raw), &evidence); err != nil || evidence.Attempts != 0 || evidence.Verified != nil || evidence.CLIAssisted != nil {
			t.Fatalf("invalid optional evidence broke the index: %s", raw)
		}
	}
	for _, raw := range []string{`{"status":"succeeded","cli_assisted":false}`, `{"status":"succeeded"}`} {
		var evidence monitorWebRecovery
		if err := json.Unmarshal([]byte(raw), &evidence); err != nil {
			t.Fatal(err)
		}
		if strings.Contains(raw, "false") && (evidence.CLIAssisted == nil || *evidence.CLIAssisted) {
			t.Fatal("explicit non-CLI attribution was lost")
		}
		if !strings.Contains(raw, "false") && evidence.CLIAssisted != nil {
			t.Fatal("historical CLI attribution was inferred")
		}
	}
}

func TestMonitorStageTimingsAreAllowlistedAndMalformedValuesDoNotBreakIndex(t *testing.T) {
	h, token, root := monitorTestServer(t)
	now := time.Now().Unix()
	row := map[string]any{"id": "timed_job", "created": now, "status": "failed", "queued_ms": 321, "deadline_policy": "web_first", "sites": []string{"doubao"}, "results": []any{
		map[string]any{"site": "doubao", "ok": false, "optional_stage_error_code": "share_timeout", "share_error_code": "share_generation_failed", "stage_timings": map[string]any{
			"pool_wait_ms": 0, "site_gate_wait_ms": 25.5, "web_execution_ms": 3000, "api_wait_ms": 5000,
			"api_execution_ms": 0, "session_setup_ms": -1, "cloud_slot_wait_ms": "500", "web_slot_wait_ms": true,
			"session_acquire_ms": nil, "SECRET_CREDENTIAL_FIELD": "SECRET_VALUE", "answer": "SECRET_TEXT",
		}},
	}}
	writeMonitorJSON(t, filepath.Join(root, "data/monitor-requests.json"), map[string]any{"updated_at": now, "rows": []any{row}})
	for _, path := range []string{"/api/admin/monitor/requests?window=today", "/api/admin/monitor/requests/timed_job"} {
		w := monitorTestRequest(h, token, "GET", path)
		if w.Code != 200 || strings.Contains(w.Body.String(), "SECRET_") || strings.Contains(w.Body.String(), "cloud_slot_wait_ms") {
			t.Fatalf("unsafe or broken timing metadata %s: %d %s", path, w.Code, w.Body.String())
		}
		var document map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &document); err != nil {
			t.Fatal(err)
		}
		if rows, ok := document["rows"].([]any); ok {
			document = rows[0].(map[string]any)
		}
		if document["queued_ms"] != float64(321) {
			t.Fatal("worker queue timing was lost")
		}
		if document["deadline_policy"] != "web_first" {
			t.Fatal("deadline policy was lost")
		}
		result := document["results"].([]any)[0].(map[string]any)
		if result["optional_stage_error_code"] != "share_timeout" || result["share_error_code"] != "share_generation_failed" {
			t.Fatal("optional quality-stage reasons were lost")
		}
		timings := result["stage_timings"].(map[string]any)
		if len(timings) != 5 || timings["pool_wait_ms"] != float64(0) || timings["site_gate_wait_ms"] != 25.5 || timings["api_execution_ms"] != float64(0) {
			t.Fatalf("valid timings were lost or invalid fields retained: %#v", timings)
		}
	}
	for _, raw := range []string{`null`, `[]`, `"bad"`, `{"api_wait_ms":1e400}`, `{"api_wait_ms":false}`} {
		var timings monitorStageTimings
		if err := json.Unmarshal([]byte(raw), &timings); err != nil || len(timings) != 0 {
			t.Fatalf("invalid optional timing should be ignored: %s %#v %v", raw, timings, err)
		}
	}
}
