package httpserver

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/fstest"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/jobs"
	"golang.org/x/crypto/bcrypt"
)

func TestRoutingEndpointAdminOnlyValidatesAndRejectsStaleWrites(t *testing.T) {
	cfg := config.Default(t.TempDir())
	hash, _ := bcrypt.GenerateFromPassword([]byte("fixture-password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	a := auth.New(cfg)
	token, err := a.Login(cfg.Admin.User, "fixture-password")
	if err != nil {
		t.Fatal(err)
	}
	q := &jobs.Queue{DataDir: t.TempDir()}
	h := New(cfg, a, nil, nil, nil, q, nil, fstest.MapFS{}).Handler()
	request := func(method, body string, admin bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "/api/admin/request-routing", strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
		if admin {
			r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: token})
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	for _, method := range []string{"GET", "PUT"} {
		if w := request(method, `{"web_first":false,"revision":0}`, false); w.Code != 401 {
			t.Fatalf("unprotected policy %s: %d", method, w.Code)
		}
	}
	if w := request("POST", `{}`, true); w.Code != 405 {
		t.Fatal("unsupported mutation accepted")
	}
	for _, body := range []string{`{}`, `{"web_first":"false","revision":0}`, `{"web_first":false}`, `{"web_first":false,"revision":0,"phone":"private"}`, `{"web_first":false,"revision":0} {}`, `{"web_first":false,"revision":0,"site_id":null}`, `{"web_first":false,"revision":0,"site_id":["deepseek"]}`} {
		if w := request("PUT", body, true); w.Code != 400 {
			t.Fatalf("invalid input accepted: %s: %d", body, w.Code)
		}
	}
	w := request("PUT", `{"web_first":false,"revision":0}`, true)
	var state jobs.RoutingPolicy
	if json.Unmarshal(w.Body.Bytes(), &state) != nil || w.Code != 200 || state.WebFirst || state.Mode != jobs.RoutingAPIOnly || state.Revision != 1 {
		t.Fatalf("switch failed: %d %s", w.Code, w.Body.String())
	}
	if w := request("PUT", `{"web_first":true,"revision":0}`, true); w.Code != 409 {
		t.Fatalf("stale admin write accepted: %d", w.Code)
	}
	w = request("GET", "", true)
	if json.Unmarshal(w.Body.Bytes(), &state) != nil || state.WebFirst || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("GET lost persisted state")
	}
	w = request("PUT", `{"site_id":"deepseek","web_first":true,"revision":1}`, true)
	if json.Unmarshal(w.Body.Bytes(), &state) != nil || w.Code != 200 || state.Mode != jobs.RoutingMixed || !state.Sites["deepseek"].WebFirst || state.Sites["doubao"].WebFirst {
		t.Fatalf("platform switch changed siblings: %d %s", w.Code, w.Body.String())
	}
	for _, site := range []string{"", "unknown"} {
		body, _ := json.Marshal(map[string]any{"site_id": site, "web_first": false, "revision": 2})
		if w := request("PUT", string(body), true); w.Code != 400 {
			t.Fatalf("unknown site accepted: %s: %d", site, w.Code)
		}
	}
	w = request("PUT", `{"site_id":"yiyan","web_first":true,"revision":2}`, true)
	if json.Unmarshal(w.Body.Bytes(), &state) != nil || w.Code != 200 || !state.Sites["wenxinyiyan"].WebFirst || state.Sites["doubao"].WebFirst {
		t.Fatalf("platform alias lost: %d %s", w.Code, w.Body.String())
	}
	if w := request("PUT", `{"site_id":"kimi","web_first":true,"revision":2}`, true); w.Code != 409 {
		t.Fatalf("stale per-platform write accepted: %d", w.Code)
	}
	w = request("PUT", `{"web_first":false,"revision":3}`, true)
	if json.Unmarshal(w.Body.Bytes(), &state) != nil || w.Code != 200 || state.Mode != jobs.RoutingAPIOnly {
		t.Fatalf("legacy global write did not reset overrides: %d %s", w.Code, w.Body.String())
	}
}
