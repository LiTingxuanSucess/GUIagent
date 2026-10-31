package httpserver

import (
	"encoding/json"
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

func TestMonitorRequiresAdminAndServesOnlySummary(t *testing.T) {
	t.Setenv("GEO_DATA_DIR", "")
	root := t.TempDir()
	cfg := config.Default(root)
	hash, _ := bcrypt.GenerateFromPassword([]byte("test-password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	a := auth.New(cfg)
	token, err := a.Login(cfg.Admin.User, "test-password")
	if err != nil {
		t.Fatal(err)
	}
	s := New(cfg, a, nil, nil, nil, nil, nil, fstest.MapFS{"monitor.html": {Data: []byte("monitor dashboard")}})
	h := s.Handler()
	for _, path := range []string{"/api/admin/monitor", "/admin/monitor"} {
		r := httptest.NewRequest("GET", path, nil)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		want := 401
		if path == "/admin/monitor" {
			want = 302
		}
		if w.Code != want {
			t.Fatalf("unauthenticated %s: %d", path, w.Code)
		}
	}
	request := func(method, path string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, nil)
		r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: token})
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	if request("GET", "/api/admin/monitor").Code != 503 {
		t.Fatal("missing monitoring state was not reported")
	}
	if err := os.Mkdir(filepath.Join(root, "data"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "data/monitor-dashboard.json"), []byte(`{"updated_at":123,"windows":{"1h":{"total":4}}}`), 0600); err != nil {
		t.Fatal(err)
	}
	w := request("GET", "/api/admin/monitor")
	var body map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if w.Code != 200 || body["updated_at"] != float64(123) || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("admin summary response invalid")
	}
	if request("POST", "/api/admin/monitor").Code != 405 {
		t.Fatal("monitor endpoint accepted writes")
	}
	if request("GET", "/admin/monitor").Body.String() != "monitor dashboard" {
		t.Fatal("monitor page did not load")
	}
}

func TestMonitorLightweightWindowAndServerFreshness(t *testing.T) {
	h, token, root := monitorTestServer(t)
	now := float64(time.Now().Unix())
	dir := filepath.Join(root, "data")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	window := map[string]any{"total": 3, "channels": map[string]any{"web": map[string]any{"total": 2, "success_rate": 50}}, "recent": []map[string]any{{"id": "heavy-details", "payload": strings.Repeat("x", 100000)}}}
	raw, _ := json.Marshal(map[string]any{"updated_at": now - 5, "windows": map[string]any{"today": window, "1h": window}})
	if err := os.WriteFile(filepath.Join(dir, "monitor-dashboard.json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	w := monitorTestRequest(h, token, "GET", "/api/admin/monitor?window=today")
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || w.Code != 200 {
		t.Fatalf("invalid summary: %d %s", w.Code, w.Body.String())
	}
	windows := body["windows"].(map[string]any)
	if len(windows) != 1 || windows["today"].(map[string]any)["total"] != float64(3) || windows["today"].(map[string]any)["recent"] != nil || len(w.Body.Bytes()) > 2000 {
		t.Fatal("summary still contains unused repeated request details")
	}
	age := body["snapshot_age_seconds"].(float64)
	if age < 4 || age > 8 || body["server_time"].(float64) < now || body["poll_interval_seconds"] != float64(10) {
		t.Fatalf("freshness metadata invalid: %v", body)
	}
	if w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("summary may be cached")
	}
	legacy := monitorTestRequest(h, token, "GET", "/api/admin/monitor")
	if !strings.Contains(legacy.Body.String(), "heavy-details") {
		t.Fatal("legacy endpoint unexpectedly lost history")
	}
	if w := monitorTestRequest(h, token, "GET", "/api/admin/monitor?window=invalid"); w.Code != 400 {
		t.Fatal("invalid window accepted")
	}
	if w := monitorTestRequest(h, token, "GET", "/api/admin/monitor?window=24h"); w.Code != 503 {
		t.Fatal("missing window not reported")
	}
}

func TestMonitorCalendarDaySummaryAndEmptyDay(t *testing.T) {
	h, token, root := monitorTestServer(t)
	dir := filepath.Join(root, "data")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	write := func(name string, value any) {
		t.Helper()
		raw, _ := json.Marshal(value)
		if err := os.WriteFile(filepath.Join(dir, name), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	now := float64(time.Now().Unix())
	write("monitor-dashboard.json", map[string]any{"updated_at": now, "available_dates": []string{"2026-10-15"}, "daily": []any{}, "windows": map[string]any{"today": map[string]any{"total": 99}}})
	path := "/api/admin/monitor?window=day&date=2026-10-15"
	if w := monitorTestRequest(h, token, "GET", path); w.Code != 503 {
		t.Fatal("missing day index should be retried")
	}
	write("monitor-daily.json", map[string]any{"updated_at": now - 3, "days": map[string]any{"2026-10-15": map[string]any{"total": 2, "p50_ms": 1500, "p95_ms": 3500, "recent": []any{strings.Repeat("x", 100000)}}}})
	w := monitorTestRequest(h, token, "GET", path)
	var body map[string]any
	if json.Unmarshal(w.Body.Bytes(), &body) != nil || w.Code != 200 {
		t.Fatal("calendar summary unavailable")
	}
	view := body["windows"].(map[string]any)["day"].(map[string]any)
	if view["total"] != float64(2) || view["p95_ms"] != float64(3500) || view["recent"] != nil || len(w.Body.Bytes()) > 2000 || body["selected_date"] != "2026-10-15" || body["updated_at"] != now-3 {
		t.Fatal("incorrect or oversized day response")
	}
	w = monitorTestRequest(h, token, "GET", "/api/admin/monitor?window=day&date=2026-10-14")
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	view = body["windows"].(map[string]any)["day"].(map[string]any)
	if w.Code != 200 || view["total"] != float64(0) || view["success_rate"] != nil || view["channels"].(map[string]any)["cursor"].(map[string]any)["success_rate"] != nil {
		t.Fatal("no-data day must return empty statistics, not a fake success rate")
	}
	for _, query := range []string{"window=day", "window=day&date=2026-02-30", "window=day&date=2026-1-02", "window=today&date=2026-10-15"} {
		if w := monitorTestRequest(h, token, "GET", "/api/admin/monitor?"+query); w.Code != 400 {
			t.Fatalf("invalid calendar query accepted: %s", query)
		}
	}
}
