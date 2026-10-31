package httpserver

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"testing/fstest"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/jobs"
	"geo-server/internal/sessions"
	"golang.org/x/crypto/bcrypt"
)

func TestManualHTTPRequiresAdminAndRevision(t *testing.T) {
	t.Setenv("GEO_MANUAL_WORKFLOW", "1")
	root := t.TempDir()
	pool := sessions.NewPool("/no/chrome", root, "", filepath.Join(root, "accounts.json"))
	q := jobs.New(root, pool, nil, filepath.Join(root, "jobs"))
	cfg := &config.Config{}
	cfg.Admin.User = "admin"
	hash, _ := bcrypt.GenerateFromPassword([]byte("fixture-password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	s := &Server{cfg: cfg, auth: auth.New(cfg), jobs: q, pages: fstest.MapFS{"manual.html": &fstest.MapFile{Data: []byte("manual workflow page")}}}
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()
	anonymous := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	for path, want := range map[string]int{"/api/admin/manual-tasks": 401, "/api/admin/sampling/export": 401, "/admin/manual": 302} {
		r, err := anonymous.Get(srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		if r.StatusCode != want {
			t.Fatalf("anonymous %s: %d", path, r.StatusCode)
		}
	}
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Jar: jar}
	call := func(path string, body any) (int, []byte) {
		t.Helper()
		var payload io.Reader
		method := http.MethodGet
		if body != nil {
			raw, _ := json.Marshal(body)
			payload = bytes.NewReader(raw)
			method = http.MethodPost
		}
		r, err := client.Do(mustManualRequest(t, method, srv.URL+path, payload))
		if err != nil {
			t.Fatal(err)
		}
		defer r.Body.Close()
		raw, _ := io.ReadAll(r.Body)
		return r.StatusCode, raw
	}
	if status, _ := call("/api/admin/login", map[string]string{"user": "admin", "password": "fixture-password"}); status != 200 {
		t.Fatal("admin login failed")
	}
	status, raw := call("/api/v1/ask", map[string]any{"site": "kimi", "prompt": "人工接管 HTTP 测试", "request_id": "manual-http-fixture"})
	if status != 202 {
		t.Fatalf("submit: %d %s", status, raw)
	}
	var submitted jobs.Job
	_ = json.Unmarshal(raw, &submitted)
	if !submitted.ManualWorkflow {
		t.Fatal("new submission did not snapshot manual workflow")
	}
	deadline := time.Now().Add(2 * time.Second)
	var row jobs.ManualTask
	for time.Now().Before(deadline) {
		_, raw = call("/api/admin/manual-tasks", nil)
		var data struct {
			Tasks []jobs.ManualTask `json:"tasks"`
		}
		_ = json.Unmarshal(raw, &data)
		if len(data.Tasks) == 1 && data.Tasks[0].Status == "waiting_manual" {
			row = data.Tasks[0]
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if row.JobID == "" {
		t.Fatal("waiting task not exposed")
	}
	path := fmt.Sprintf("/api/admin/manual-tasks/%s/kimi", row.JobID)
	if status, _ := call(path, map[string]any{"action": "continue"}); status != 400 {
		t.Fatal("missing revision accepted")
	}
	if status, _ := call(path, map[string]any{"action": "continue", "revision": row.Revision + 1}); status != 409 {
		t.Fatal("stale revision accepted")
	}
	if status, _ := call("/api/v1/jobs/"+row.JobID+"/cancel", map[string]any{}); status != 200 {
		t.Fatal("waiting task could not be cancelled")
	}
	if status, raw := call("/admin/manual", nil); status != 200 || string(raw) != "manual workflow page" {
		t.Fatal("admin page unavailable")
	}
}

func mustManualRequest(t *testing.T, method, url string, body io.Reader) *http.Request {
	t.Helper()
	r, err := http.NewRequest(method, url, body)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		r.Header.Set("Content-Type", "application/json")
	}
	return r
}
