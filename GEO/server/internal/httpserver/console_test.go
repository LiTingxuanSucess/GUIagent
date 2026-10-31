package httpserver

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"testing/fstest"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/jobs"
	"golang.org/x/crypto/bcrypt"
)

func TestConsoleListPaginationAndAuthentication(t *testing.T) {
	cfg := config.Default(t.TempDir())
	hash, _ := bcrypt.GenerateFromPassword([]byte("test-password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	a := auth.New(cfg)
	tok, _ := a.Login(cfg.Admin.User, "test-password")
	q := jobs.New(t.TempDir(), nil, nil, t.TempDir())
	s := New(cfg, a, nil, nil, nil, q, nil, fstest.MapFS{})
	h := s.Handler()
	request := func(path string, authenticated bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest("GET", path, nil)
		if authenticated {
			r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: tok})
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	if request("/api/console/jobs", false).Code != 401 {
		t.Fatal("unauthenticated console history exposed")
	}
	for _, query := range []string{"limit=0", "limit=101", "limit=bad", "offset=-1", "offset=bad", "offset=99999999999999999999999"} {
		if request("/api/console/jobs?"+query, true).Code != 400 {
			t.Fatalf("accepted invalid pagination: %s", query)
		}
	}
	w := request("/api/console/jobs", true)
	var body struct {
		Jobs   []jobs.JobSummary `json:"jobs"`
		Total  int               `json:"total"`
		Limit  int               `json:"limit"`
		Offset int               `json:"offset"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || w.Code != 200 || body.Limit != 50 || body.Total != 0 || body.Jobs == nil || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("invalid default page: %s", w.Body.String())
	}
}
