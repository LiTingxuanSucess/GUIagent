package httpserver

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"testing/fstest"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/jobs"
	"geo-server/internal/sessions"
	"golang.org/x/crypto/bcrypt"
)

func TestLoginCheckIsSingleFlightAndDoesNotTurnSMSOrBusyIntoSuccess(t *testing.T) {
	var checks monitorLoginChecks
	entered, release := make(chan struct{}), make(chan struct{})
	var calls atomic.Int32
	rows := []loginCheckRow{{AccountID: "one", SiteID: "kimi", Status: "pending"}, {AccountID: "two", SiteID: "doubao", Status: "pending"}}
	run := func(row loginCheckRow, onCLI func()) loginCheckRow {
		if calls.Add(1) == 1 {
			onCLI()
			close(entered)
			<-release
			row.Status, row.CLIUsed = "waiting_sms", true
		} else {
			row.Status = "busy"
		}
		return row
	}
	checks.start(rows, run)
	<-entered
	first := checks.snapshot()
	checks.start(rows, run)
	second := checks.snapshot()
	if first.ID != second.ID || calls.Load() != 1 || second.Results[0].Status != "assisting" {
		t.Fatal("repeat start duplicated work or lost CLI phase")
	}
	second.Results[0].Status = "logged_in"
	if checks.snapshot().Results[0].Status != "assisting" {
		t.Fatal("snapshot modified live task")
	}
	close(release)
	deadline := time.Now().Add(time.Second)
	for checks.snapshot().Status == "running" && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	final := checks.snapshot()
	if final.Status != "completed" || final.Completed != 2 || final.Results[0].Status != "waiting_sms" || final.Results[1].Status != "busy" || calls.Load() != 2 {
		t.Fatalf("incorrect final outcomes: %+v", final)
	}
}

func TestSMSReplayDoesNotUseEarlierSendGeneration(t *testing.T) {
	received := time.Now()
	if smsMatchesSend(float64(received.Add(time.Minute).Unix()), received) {
		t.Fatal("earlier SMS accepted for a new send generation")
	}
	if !smsMatchesSend(float64(received.Add(-time.Minute).Unix()), received) {
		t.Fatal("fresh callback rejected")
	}
}

func TestCursorHealthRequiresAuthenticationAndDoesNotStartWork(t *testing.T) {
	root := t.TempDir()
	t.Setenv("GEO_DATA_DIR", root)
	cfg := config.Default(root)
	hash, _ := bcrypt.GenerateFromPassword([]byte("password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	a := auth.New(cfg)
	token, _ := a.Login(cfg.Admin.User, "password")
	s := New(cfg, a, nil, nil, nil, &jobs.Queue{GeoRoot: root}, nil, fstest.MapFS{})
	for _, tc := range []struct {
		method string
		admin  bool
		want   int
	}{{"GET", false, 401}, {"GET", true, 200}, {"POST", false, 401}, {"DELETE", true, 405}} {
		r := httptest.NewRequest(tc.method, "/api/admin/cursor-health", nil)
		if tc.admin {
			r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: token})
		}
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Fatalf("health %s admin=%v: %d", tc.method, tc.admin, w.Code)
		}
	}
}

func TestLoginCheckHTTPRequiresAdminMasksPhoneAndSkipsDisabled(t *testing.T) {
	root := t.TempDir()
	cfg := config.Default(root)
	hash, _ := bcrypt.GenerateFromPassword([]byte("fixture-password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	a := auth.New(cfg)
	token, _ := a.Login(cfg.Admin.User, "fixture-password")
	pool := sessions.NewPool("/nonexistent/chrome", filepath.Join(root, "profile"), "", filepath.Join(root, "accounts.json"))
	account, _ := pool.Add("13800138351")
	off, _ := pool.Add("13800137132")
	if _, err := pool.SetEnabled(off.ID, false); err != nil {
		t.Fatal(err)
	}
	// No browser capacity: a real run must report busy without opening Chrome.
	s := New(cfg, a, nil, nil, pool, &jobs.Queue{}, nil, fstest.MapFS{})
	h := s.Handler()
	request := func(method, body string, authenticated bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "/api/admin/login-check", strings.NewReader(body))
		if authenticated {
			r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: token})
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	for _, method := range []string{http.MethodGet, http.MethodPost} {
		if w := request(method, "{}", false); w.Code != http.StatusUnauthorized {
			t.Fatal("login actions exposed without admin")
		}
	}
	if request(http.MethodDelete, "", true).Code != http.StatusMethodNotAllowed {
		t.Fatal("unexpected HTTP method accepted")
	}
	if request(http.MethodPost, `{"site_id":"invalid"}`, true).Code != http.StatusBadRequest {
		t.Fatal("invalid site accepted")
	}
	w := request(http.MethodPost, `{"site_id":"kimi"}`, true)
	if w.Code != http.StatusAccepted || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("start: %d %s", w.Code, w.Body.String())
	}
	deadline := time.Now().Add(time.Second)
	for s.loginChecks.snapshot().Status == "running" && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	w = request(http.MethodGet, "", true)
	var task loginCheckTask
	if err := json.Unmarshal(w.Body.Bytes(), &task); err != nil {
		t.Fatal(err)
	}
	if task.Status != "completed" || task.Total != 1 || task.Results[0].AccountID != account.ID || task.Results[0].AccountLabel != "手机尾号 8351" || task.Results[0].Status != "busy" {
		t.Fatalf("bad result: %+v", task)
	}
	if strings.Contains(w.Body.String(), "13800138351") || strings.Contains(w.Body.String(), off.ID) {
		t.Fatal("phone or disabled account leaked")
	}
	if request(http.MethodPost, `{"account_ids":["missing"]}`, true).Code != 400 {
		t.Fatal("unknown account scope accepted")
	}
	if request(http.MethodPost, `{"account_ids":["`+off.ID+`"]}`, true).Code != 400 {
		t.Fatal("disabled account scope accepted")
	}
	_, _ = pool.Add("13900005678")
	w = request(http.MethodPost, `{"site_id":"doubao","account_ids":["`+account.ID+`","`+account.ID+`"]}`, true)
	if json.Unmarshal(w.Body.Bytes(), &task) != nil || w.Code != http.StatusAccepted || task.Total != 1 || task.Results[0].AccountID != account.ID {
		t.Fatalf("login scope escaped selection: %s", w.Body.String())
	}
	deadline = time.Now().Add(time.Second)
	for s.loginChecks.snapshot().Status == "running" && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if w = request(http.MethodPost, "{}", true); w.Code != http.StatusAccepted {
		t.Fatal("could not start another completed sweep")
	}
}
