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
	"geo-server/internal/jobs"
)

func TestAllQueuedJobsAreCompleteTenantScopedAndReadOnly(t *testing.T) {
	t.Setenv("GEO_REQUIRE_DELIVERY", "0")
	root := t.TempDir()
	keys, key, err := auth.LoadKeys(filepath.Join(root, "keys.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "jobs"), 0700); err != nil {
		t.Fatal(err)
	}
	barrier, _ := json.Marshal(map[string]any{"expires_at": time.Now().Add(time.Minute).Unix(), "allow": []string{}})
	if err := os.WriteFile(filepath.Join(root, ".deployment-drain.json"), barrier, 0600); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 208; i++ {
		status, ak := "queued", key.AK
		if i == 205 {
			status = "running"
		}
		if i == 206 {
			status = "succeeded"
		}
		if i == 207 {
			ak = "other-tenant"
		}
		job := jobs.Job{ID: fmt.Sprintf("fixture_%03d", i), AK: ak, Status: status, Prompt: "fixture", Sites: []string{"doubao"}, RequestID: fmt.Sprintf("req-%03d", i), CreatedAt: time.Now().Add(-time.Duration(i) * time.Second).Format(time.RFC3339Nano), DispatchTracked: true, Results: []map[string]any{{"site_id": "doubao", "ok": false, "account_id": "PRIVATE_ACCOUNT", "attempts": []any{map[string]any{"account_id": "PRIVATE_ACCOUNT"}}}}}
		encoded, _ := json.Marshal(job)
		if err := os.WriteFile(filepath.Join(root, "jobs", job.ID+".json"), encoded, 0600); err != nil {
			t.Fatal(err)
		}
	}
	q := jobs.New(root, nil, keys, filepath.Join(root, "jobs"))
	t.Cleanup(func() {
		for _, job := range q.List() {
			_, _ = q.CancelVisible(job.ID, "", true)
		}
	})
	cfg := config.Default(root)
	h := New(cfg, auth.New(cfg), keys, nil, nil, q, nil, fstest.MapFS{}).Handler()
	sequence := 0
	request := func(path string, signed bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest("GET", path, nil)
		if signed {
			sequence++
			ts := fmt.Sprint(time.Now().Unix())
			nonce := fmt.Sprintf("queued-fixture-%d", sequence)
			r.Header.Set(auth.HeaderAK, key.AK)
			r.Header.Set(auth.HeaderTimestamp, ts)
			r.Header.Set(auth.HeaderNonce, nonce)
			r.Header.Set(auth.HeaderSign, auth.Sign(key.SK, auth.Canonical("GET", r.URL.Path, ts, nonce, auth.BodyHash(nil))))
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	if request("/api/v1/jobs?status=queued&all=true", false).Code != http.StatusUnauthorized {
		t.Fatal("unsigned all-queue accepted")
	}
	for _, test := range []struct {
		query              string
		size, total, limit int
		all                bool
	}{
		{"status=queued", 100, 205, 100, false},
		{"status=queued&limit=200", 200, 205, 200, false},
		{"status=queued&all=true", 205, 205, 0, true},
		{"status=queued&all=1&limit=1&ak=other-tenant", 205, 205, 0, true},
		{"status=queued&all=false", 100, 205, 100, false},
		{"status=queued&all=true&request_id=req-010", 1, 1, 0, true},
		{"status=queued&all=true&request_id=missing", 0, 0, 0, true},
	} {
		w := request("/api/v1/jobs?"+test.query, true)
		var body struct {
			Jobs  []jobs.Job `json:"jobs"`
			Total int        `json:"total"`
			Limit int        `json:"limit"`
			All   bool       `json:"all"`
		}
		if json.Unmarshal(w.Body.Bytes(), &body) != nil || w.Code != 200 || len(body.Jobs) != test.size || body.Total != test.total || body.Limit != test.limit || body.All != test.all {
			t.Fatalf("query %s: %d %s", test.query, w.Code, w.Body.String())
		}
		for _, job := range body.Jobs {
			if job.Status != "queued" || job.AK != "" || job.ID == "fixture_207" {
				t.Fatalf("tenant/status leak: %+v", job)
			}
		}
		if strings.Contains(w.Body.String(), "PRIVATE_ACCOUNT") {
			t.Fatal("internal account evidence leaked")
		}
	}
	for _, query := range []string{"all=true", "all=true&status=running", "status=queued&all=garbage", "status=queued&all=true&limit=0"} {
		if w := request("/api/v1/jobs?"+query, true); w.Code != 400 {
			t.Fatalf("invalid parameters accepted: %s %d", query, w.Code)
		}
	}
	if stored, _ := q.Get("fixture_010"); stored.Status != "queued" || stored.Results[0]["account_id"] != "PRIVATE_ACCOUNT" {
		t.Fatal("read-only listing changed stored work")
	}
}
