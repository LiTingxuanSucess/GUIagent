package httpserver

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/jobs"
	"geo-server/internal/results"
	"geo-server/internal/sessions"
	"geo-server/internal/sms"
)

type Server struct {
	cfg             *config.Config
	auth            *auth.Store
	keys            *auth.KeyStore
	res             *results.Store
	sess            *sessions.Pool
	jobs            *jobs.Queue
	sms             *sms.Inbox
	pages           fs.FS
	monitorRequests monitorRequestCache
	loginChecks     monitorLoginChecks
	smsHandoffs     smsHandoffStore
}

func New(cfg *config.Config, a *auth.Store, keys *auth.KeyStore, r *results.Store, s *sessions.Pool, q *jobs.Queue, inbox *sms.Inbox, pages fs.FS) *Server {
	server := &Server{cfg: cfg, auth: a, keys: keys, res: r, sess: s, jobs: q, sms: inbox, pages: pages}
	server.loadSMSHandoffs()
	return server
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(acmeChallengePrefix, s.acmeChallenge)
	mux.Handle("/static/", http.StripPrefix("/static/", http.FileServer(http.FS(mustSub(s.pages, "static")))))
	mux.HandleFunc("/", s.pageIndex)
	mux.HandleFunc("/admin", s.pageAdmin)
	mux.HandleFunc("/admin/monitor", s.pageMonitor)
	mux.HandleFunc("/admin/manual", func(w http.ResponseWriter, r *http.Request) {
		if !s.auth.IsAdmin(r) {
			http.Redirect(w, r, "/admin/login?next=%2Fadmin%2Fmanual", http.StatusFound)
			return
		}
		s.servePage(w, "manual.html")
	})
	mux.HandleFunc("/admin/login", s.pageLogin)

	mux.HandleFunc("/api/admin/login", s.adminLogin)
	mux.HandleFunc("/api/admin/logout", s.adminLogout)
	mux.HandleFunc("/api/admin/me", s.adminMe)
	mux.HandleFunc("/api/admin/phone", s.adminPhone)
	mux.HandleFunc("/api/admin/sessions", s.adminSessions)
	mux.HandleFunc("/api/admin/sessions/", s.adminSessionAction)
	mux.HandleFunc("/api/admin/login-counts", s.requireAdmin(s.adminLoginCounts))
	mux.HandleFunc("/api/admin/login-check", s.requireAdmin(s.adminLoginCheck))
	mux.HandleFunc("/api/admin/cursor-health", s.requireAdmin(s.apiCursorHealth))
	mux.HandleFunc("/api/admin/accounts", s.requireAdmin(s.adminAccounts))
	mux.HandleFunc("/api/admin/accounts/", s.requireAdmin(s.adminAccounts))
	mux.HandleFunc("/api/admin/manual-tasks", s.requireAdmin(s.adminManualTasks))
	mux.HandleFunc("/api/admin/manual-tasks/", s.requireAdmin(s.adminManualTasks))
	mux.HandleFunc("/api/admin/sampling/export", s.requireAdmin(s.adminSamplingExport))
	mux.HandleFunc("/api/admin/tenants", s.requireAdmin(s.apiTenants))
	mux.HandleFunc("/api/admin/monitor", s.requireAdmin(s.apiMonitor))
	mux.HandleFunc("/api/admin/monitor/cursor", s.requireAdmin(s.apiMonitorCursor))
	mux.HandleFunc("/api/admin/monitor/actions", s.requireAdmin(s.apiMonitorActions))
	mux.HandleFunc("/api/admin/request-routing", s.requireAdmin(s.adminRoutingPolicy))
	mux.HandleFunc("/api/admin/monitor/requests", s.requireAdmin(s.apiMonitorRequests))
	mux.HandleFunc("/api/admin/monitor/requests/", s.requireAdmin(s.apiMonitorRequest))

	mux.HandleFunc("/api/console/login", s.consoleLogin)
	mux.HandleFunc("/api/console/logout", s.consoleLogout)
	mux.HandleFunc("/api/console/me", s.requireConsole(s.consoleMe))
	mux.HandleFunc("/api/console/jobs", s.requireConsole(s.consoleJobs))
	mux.HandleFunc("/api/console/jobs/", s.requireConsole(s.consoleJob))
	mux.HandleFunc("/api/console/usage", s.requireConsole(s.consoleUsage))

	mux.HandleFunc("/api/tree", s.requireRead(s.apiTree))
	mux.HandleFunc("/api/batches", s.requireRead(s.apiBatches))
	mux.HandleFunc("/api/batches/", s.requireRead(s.apiBatch))
	mux.HandleFunc("/api/latest", s.requireRead(s.apiLatest))
	mux.HandleFunc("/api/result", s.requireRead(s.apiResult))

	mux.HandleFunc("/api/v1/questions", s.requireKey(s.apiTree))
	mux.HandleFunc("/api/v1/batches", s.requireKey(s.apiBatches))
	mux.HandleFunc("/api/v1/latest", s.requireKey(s.apiLatest))
	mux.HandleFunc("/api/v1/result", s.requireKey(s.apiResult))
	mux.HandleFunc("/api/v1/sites", s.requireAKSK(s.apiSites))
	mux.HandleFunc("/api/v1/ask", s.requireAKSK(s.apiAsk))
	mux.HandleFunc("/api/v1/jobs", s.requireAKSK(s.apiJobs))
	mux.HandleFunc("/api/v1/jobs/", s.requireAKSK(s.apiJob))
	mux.HandleFunc("/api/sms/inbound", s.smsInbound)
	mux.HandleFunc("/api/admin/sms", s.requireAdmin(s.adminSMS))
	return mux
}

func mustSub(fsys fs.FS, dir string) fs.FS {
	sub, err := fs.Sub(fsys, dir)
	if err != nil {
		return fsys
	}
	return sub
}

func (s *Server) pageIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	s.servePage(w, "console.html")
}

func (s *Server) pageAdmin(w http.ResponseWriter, r *http.Request) {
	if !s.auth.IsAdmin(r) {
		http.Redirect(w, r, "/admin/login?next=%2Fadmin", http.StatusFound)
		return
	}
	s.servePage(w, "admin.html")
}

func (s *Server) pageLogin(w http.ResponseWriter, r *http.Request) {
	s.servePage(w, "login.html")
}

func (s *Server) servePage(w http.ResponseWriter, name string) {
	raw, err := fs.ReadFile(s.pages, name)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(raw)
}

func (s *Server) adminLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	var body struct {
		User     string `json:"user"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"detail": "invalid json"})
		return
	}
	tok, err := s.auth.Login(body.User, body.Password)
	if err != nil {
		writeJSON(w, 401, map[string]any{"detail": err.Error()})
		return
	}
	s.auth.SetCookie(w, r, tok)
	writeJSON(w, 200, map[string]any{"ok": true, "user": s.cfg.Admin.User})
}

func (s *Server) adminLogout(w http.ResponseWriter, r *http.Request) {
	s.auth.Logout(auth.CookieToken(r))
	s.auth.ClearCookie(w, r)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) adminMe(w http.ResponseWriter, r *http.Request) {
	if !s.auth.IsAdmin(r) {
		writeJSON(w, 401, map[string]any{"detail": "unauthorized"})
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "user": s.cfg.Admin.User})
}

func (s *Server) requireAdmin(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.auth.IsAdmin(r) {
			writeJSON(w, 401, map[string]any{"detail": "需要 admin 登录"})
			return
		}
		next(w, r)
	}
}

func (s *Server) requireRead(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.auth.CanRead(r) {
			writeJSON(w, 401, map[string]any{"detail": "需要 admin 登录或 API Key"})
			return
		}
		next(w, r)
	}
}

func (s *Server) requireAKSK(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.auth.IsAdmin(r) {
			next(w, r)
			return
		}
		body, err := auth.ReadBody(r)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": "读 body 失败"})
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		if _, err := s.keys.Verify(r, body); err != nil {
			writeJSON(w, 401, map[string]any{"detail": err.Error()})
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		next(w, r)
	}
}

func (s *Server) apiSites(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	writeJSON(w, 200, publicSnapshot(s.sess.Snapshot()))
}

func (s *Server) apiAsk(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	var body struct {
		Site        string   `json:"site"`
		Sites       []string `json:"sites"`
		Prompt      string   `json:"prompt"`
		CallbackURL string   `json:"callback_url"`
		RequestID   string   `json:"request_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"detail": "invalid json"})
		return
	}
	ids := body.Sites
	if body.Site != "" {
		ids = append([]string{body.Site}, ids...)
	}
	seen := map[string]bool{}
	uniq := make([]string, 0, len(ids))
	for _, id := range ids {
		if seen[id] {
			continue
		}
		seen[id] = true
		uniq = append(uniq, id)
	}
	ak, admin := s.identity(r)
	if admin && ak == "" {
		ak = r.Header.Get(auth.HeaderAK)
	}
	requestID := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if requestID != "" && body.RequestID != "" && requestID != strings.TrimSpace(body.RequestID) {
		writeJSON(w, 400, map[string]any{"detail": "Idempotency-Key 与 request_id 不一致"})
		return
	}
	if requestID == "" {
		requestID = body.RequestID
	}
	job, created, err := s.jobs.Submit(body.Prompt, uniq, body.CallbackURL, ak, requestID)
	if err != nil {
		writeJSON(w, 400, map[string]any{"detail": err.Error()})
		return
	}
	if !admin {
		job.AK = ""
	}
	if !created {
		w.Header().Set("X-Geo-Idempotent-Replay", "true")
		writeJSON(w, http.StatusOK, job)
		return
	}
	writeJSON(w, http.StatusAccepted, job)
}

func (s *Server) identity(r *http.Request) (ak string, admin bool) {
	if s.auth.IsAdmin(r) {
		return "", true
	}
	if t := s.auth.TenantAK(r); t != "" {
		return t, false
	}
	return r.Header.Get(auth.HeaderAK), false
}

func (s *Server) apiJobs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	ak, admin := s.identity(r)
	s.writeJobList(w, r, ak, admin)
}

func (s *Server) apiJob(w http.ResponseWriter, r *http.Request) {
	id := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/v1/jobs/"), "/")
	ak, admin := s.identity(r)
	if strings.HasSuffix(id, "/cancel") {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
			return
		}
		id = strings.TrimSuffix(id, "/cancel")
		if id == "" || strings.Contains(id, "/") {
			writeJSON(w, 404, map[string]any{"detail": "job not found"})
			return
		}
		job, err := s.jobs.CancelVisible(id, ak, admin)
		if err != nil {
			if errors.Is(err, jobs.ErrJobNotFound) {
				writeJSON(w, 404, map[string]any{"detail": "job not found"})
			} else {
				writeJSON(w, 503, map[string]any{"detail": "取消状态持久化失败，请重试同一任务"})
			}
			return
		}
		writeJSON(w, 200, job)
		return
	}
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	if id == "" {
		s.writeJobList(w, r, ak, admin)
		return
	}
	s.writeVisibleJob(w, id, ak, admin)
}

func (s *Server) writeJobList(w http.ResponseWriter, r *http.Request, ak string, admin bool) {
	limit := 100
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > 200 {
			writeJSON(w, 400, map[string]any{"detail": "limit 需为 1–200"})
			return
		}
		limit = n
	}
	status := strings.TrimSpace(r.URL.Query().Get("status"))
	if status != "" {
		valid := map[string]bool{"queued": true, "running": true, "succeeded": true, "partial": true, "failed": true, "cancelled": true}
		if !valid[status] {
			writeJSON(w, 400, map[string]any{"detail": "status 无效"})
			return
		}
	}
	requestID := strings.TrimSpace(r.URL.Query().Get("request_id"))
	all := false
	if raw := strings.TrimSpace(r.URL.Query().Get("all")); raw != "" {
		var err error
		all, err = strconv.ParseBool(raw)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": "all 需为 true/false 或 1/0"})
			return
		}
	}
	if all {
		if status != "queued" {
			writeJSON(w, 400, map[string]any{"detail": "all=true 仅支持 status=queued"})
			return
		}
		limit = 0
	}
	rows, total := s.jobs.ListFilteredVisible(ak, admin, status, requestID, limit)
	response := map[string]any{"jobs": rows, "total": total, "limit": limit}
	if all {
		response["all"] = true
	}
	writeJSON(w, 200, response)
}

func (s *Server) requireKey(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.auth.IsAPIKey(r) || (s.cfg.APIKey == "" && auth.IsLoopback(r)) || s.auth.IsAdmin(r) {
			next(w, r)
			return
		}
		writeJSON(w, 401, map[string]any{"detail": "需要 X-API-Key"})
	}
}

func (s *Server) adminPhone(w http.ResponseWriter, r *http.Request) {
	s.requireAdmin(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
			return
		}
		var body struct {
			Phone string `json:"phone"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid json"})
			return
		}
		if err := s.sess.SetPhone(body.Phone); err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true, "phone": s.sess.CurrentPhone()})
	})(w, r)
}

func (s *Server) adminSessions(w http.ResponseWriter, r *http.Request) {
	s.requireAdmin(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, s.sess.Snapshot())
	})(w, r)
}

func (s *Server) adminSessionAction(w http.ResponseWriter, r *http.Request) {
	s.requireAdmin(func(w http.ResponseWriter, r *http.Request) {
		accountID := s.defaultAccountID()
		if accountID == "" {
			writeJSON(w, 404, map[string]any{"detail": "号池为空"})
			return
		}
		path := strings.TrimPrefix(r.URL.Path, "/api/admin/sessions/")
		parts := strings.Split(path, "/")
		if len(parts) == 0 || parts[0] == "" {
			writeJSON(w, 404, map[string]any{"detail": "missing site"})
			return
		}
		siteID := parts[0]
		action := ""
		if len(parts) > 1 {
			action = parts[1]
		}
		switch {
		case action == "screenshot" && r.Method == http.MethodGet:
			buf, err := s.sess.Screenshot(accountID, siteID)
			if err != nil {
				writeJSON(w, 503, map[string]any{"detail": err.Error()})
				return
			}
			w.Header().Set("Content-Type", "image/png")
			w.Header().Set("Cache-Control", "no-store")
			_, _ = w.Write(buf)
		case action == "send-code" && r.Method == http.MethodPost:
			st, err := s.sess.SendCode(accountID, siteID, false)
			if err != nil {
				writeJSON(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			writeJSON(w, 200, s.applyRecentSMS(accountID, siteID, st))
		case action == "submit-code" && r.Method == http.MethodPost:
			var body struct {
				Code string `json:"code"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				writeJSON(w, 400, map[string]any{"detail": "invalid json"})
				return
			}
			st, err := s.sess.SubmitCode(accountID, siteID, body.Code)
			if err != nil {
				writeJSON(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			writeJSON(w, 200, st)
		case action == "click" && r.Method == http.MethodPost:
			var body struct {
				NX float64 `json:"nx"`
				NY float64 `json:"ny"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				writeJSON(w, 400, map[string]any{"detail": "invalid json"})
				return
			}
			st, err := s.sess.ClickNorm(accountID, siteID, body.NX, body.NY)
			if err != nil {
				writeJSON(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			writeJSON(w, 200, st)
		case action == "refresh" && r.Method == http.MethodPost:
			st, err := s.sess.Check(accountID, siteID)
			if err != nil {
				writeJSON(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			writeJSON(w, 200, st)
		default:
			writeJSON(w, 404, map[string]any{"detail": "unknown action"})
		}
	})(w, r)
}

func (s *Server) defaultAccountID() string {
	accounts := s.sess.List()
	for _, a := range accounts {
		if a.ID == "default" && a.Enabled {
			return a.ID
		}
	}
	for _, a := range accounts {
		if a.Enabled {
			return a.ID
		}
	}
	return ""
}

func (s *Server) adminLoginCounts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	writeJSON(w, 200, map[string]any{"sites": s.sess.LoginCounts()})
}

func (s *Server) adminAccounts(w http.ResponseWriter, r *http.Request) {
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/admin/accounts"), "/")
	if path == "" {
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, 200, map[string]any{"accounts": s.sess.List(), "sites": sessions.Sites})
		case http.MethodPost:
			var body struct {
				Phone  string   `json:"phone"`
				Phones []string `json:"phones"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				writeJSON(w, 400, map[string]any{"detail": "invalid json"})
				return
			}
			if strings.TrimSpace(body.Phone) != "" {
				body.Phones = append(body.Phones, body.Phone)
			}
			added, errs := s.sess.Import(body.Phones)
			if len(added) == 0 && len(errs) > 0 {
				writeJSON(w, 400, map[string]any{"detail": strings.Join(errs, "；"), "errors": errs})
				return
			}
			writeJSON(w, 201, map[string]any{"ok": true, "accounts": added, "errors": errs})
		default:
			writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		}
		return
	}

	if path == "batch-check" || path == "batch-login" {
		if r.Method != http.MethodPost {
			writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
			return
		}
		var body struct {
			AccountIDs []string `json:"account_ids"`
			SiteID     string   `json:"site_id"`
		}
		r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid json"})
			return
		}
		if _, ok := sessions.SiteByID(body.SiteID); !ok || len(body.AccountIDs) == 0 {
			writeJSON(w, 400, map[string]any{"detail": "需要有效的 site_id 和 account_ids"})
			return
		}
		limit := 50
		if path == "batch-login" {
			limit = 20
		}
		seen := make(map[string]bool, len(body.AccountIDs))
		ids := make([]string, 0, len(body.AccountIDs))
		for _, id := range body.AccountIDs {
			id = strings.TrimSpace(id)
			if id == "" || seen[id] {
				continue
			}
			seen[id] = true
			account, ok := s.sess.Get(id)
			if !ok || account == nil || !account.Enabled {
				writeJSON(w, 400, map[string]any{"detail": "账号不存在或已停用: " + id})
				return
			}
			ids = append(ids, id)
		}
		if len(ids) == 0 || len(ids) > limit {
			writeJSON(w, 400, map[string]any{"detail": fmt.Sprintf("单次最多操作 %d 个有效账号", limit)})
			return
		}
		action := path
		for _, id := range ids {
			accountID := id
			go func() {
				var err error
				if action == "batch-login" {
					_, err = s.sess.SendCode(accountID, body.SiteID, false)
				} else {
					_, err = s.sess.Check(accountID, body.SiteID)
				}
				if err != nil {
					log.Printf("%s account=%s site=%s: %v", action, accountID, body.SiteID, err)
				}
			}()
		}
		writeJSON(w, http.StatusAccepted, map[string]any{"ok": true, "accepted": len(ids)})
		return
	}

	parts := strings.Split(path, "/")
	accountID := parts[0]
	if len(parts) == 1 {
		switch r.Method {
		case http.MethodPatch:
			var body struct {
				Enabled *bool `json:"enabled"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Enabled == nil {
				writeJSON(w, 400, map[string]any{"detail": "需要 enabled"})
				return
			}
			a, err := s.sess.SetEnabled(accountID, *body.Enabled)
			if err != nil {
				writeJSON(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			writeJSON(w, 200, a)
		case http.MethodDelete:
			if err := s.sess.Delete(accountID); err != nil {
				writeJSON(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			writeJSON(w, 200, map[string]any{"ok": true})
		default:
			writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		}
		return
	}
	if len(parts) < 4 || parts[1] != "sites" {
		writeJSON(w, 404, map[string]any{"detail": "unknown action"})
		return
	}
	siteID, action := parts[2], parts[3]
	if _, ok := sessions.SiteByID(siteID); !ok {
		writeJSON(w, 404, map[string]any{"detail": "未知站点"})
		return
	}
	switch {
	case action == "screenshot" && r.Method == http.MethodGet:
		buf, err := s.sess.Screenshot(accountID, siteID)
		if err != nil {
			writeJSON(w, 503, map[string]any{"detail": err.Error()})
			return
		}
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(buf)
	case action == "check" && r.Method == http.MethodPost:
		st, err := s.sess.Check(accountID, siteID)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, st)
	case action == "send-code" && r.Method == http.MethodPost:
		st, err := s.sess.SendCode(accountID, siteID, false)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, s.applyRecentSMS(accountID, siteID, st))
	case action == "submit-code" && r.Method == http.MethodPost:
		var body struct {
			Code string `json:"code"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid json"})
			return
		}
		st, err := s.sess.SubmitCode(accountID, siteID, body.Code)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, st)
	case action == "clear" && r.Method == http.MethodPost:
		st, err := s.sess.ClearSiteSession(accountID, siteID)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, st)
	case action == "click" && r.Method == http.MethodPost:
		var body struct {
			NX float64 `json:"nx"`
			NY float64 `json:"ny"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid json"})
			return
		}
		st, err := s.sess.ClickNorm(accountID, siteID, body.NX, body.NY)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, st)
	case action == "cursor-assist" && (r.Method == http.MethodPost || r.Method == http.MethodGet):
		if s.jobs == nil {
			writeJSON(w, 503, map[string]any{"detail": "页面处理暂不可用"})
			return
		}
		if r.Method == http.MethodGet {
			callID := strings.TrimSpace(r.URL.Query().Get("call_id"))
			status, err := s.jobs.ManualPageAssistStatus(callID, accountID, siteID)
			if err != nil {
				writeJSON(w, 404, map[string]any{"detail": "没有这次页面处理记录"})
				return
			}
			writeJSON(w, 200, status)
			return
		}
		cdp, targetID, release, err := s.sess.HoldManualPage(accountID, siteID)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		callID, err := s.jobs.StartManualPageAssist(accountID, siteID, cdp, targetID, release)
		if err != nil {
			release()
			code := 400
			detail := "当前页暂时不能交给 Cursor CLI"
			switch {
			case errors.Is(err, jobs.ErrManualCLIDisabled):
				detail = "Cursor CLI 恢复未启用"
			case errors.Is(err, jobs.ErrManualCLIBusy):
				code = 409
				detail = "该账号已有 Cursor CLI 正在处理页面"
			case errors.Is(err, jobs.ErrManualCLIUnavailable):
				detail = "Cursor CLI 不可用"
			default:
				if label := jobs.CursorProviderErrorLabel(err.Error()); label != "" {
					code = 503
					detail = label
				}
			}
			writeJSON(w, code, map[string]any{"detail": detail})
			return
		}
		writeJSON(w, http.StatusAccepted, map[string]any{"ok": true, "call_id": callID, "status": "running"})
		return
	case action == "drag" && r.Method == http.MethodPost:
		var body struct {
			NX  float64 `json:"nx"`
			NY  float64 `json:"ny"`
			NX2 float64 `json:"nx2"`
			NY2 float64 `json:"ny2"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid json"})
			return
		}
		st, err := s.sess.DragNorm(accountID, siteID, body.NX, body.NY, body.NX2, body.NY2)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, st)
	default:
		writeJSON(w, 404, map[string]any{"detail": "unknown action"})
	}
}

func (s *Server) apiTree(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, s.res.Tree())
}

func (s *Server) apiBatches(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, s.res.ListBatches())
}

func (s *Server) apiBatch(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/api/batches/")
	id = strings.TrimPrefix(id, "/api/v1/batches/")
	b, err := s.res.Find(id)
	if err != nil {
		if os.IsNotExist(err) {
			writeJSON(w, 404, map[string]any{"detail": "batch not found"})
			return
		}
		writeJSON(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	writeJSON(w, 200, b)
}

func (s *Server) apiLatest(w http.ResponseWriter, r *http.Request) {
	b, err := s.res.Latest()
	if err != nil {
		writeJSON(w, 404, map[string]any{"detail": "no exports"})
		return
	}
	writeJSON(w, 200, b)
}

func (s *Server) apiResult(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	res, err := s.res.Result(q.Get("batch_id"), q.Get("site_id"), q.Get("question_id"))
	if err != nil {
		writeJSON(w, 404, map[string]any{"detail": "result not found"})
		return
	}
	writeJSON(w, 200, res)
}

func (s *Server) requireConsole(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.auth.IsAdmin(r) || s.auth.TenantAK(r) != "" {
			next(w, r)
			return
		}
		writeJSON(w, 401, map[string]any{"detail": "需要登录"})
	}
}

func (s *Server) consoleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	var body struct {
		User     string `json:"user"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"detail": "invalid json"})
		return
	}
	if tok, err := s.auth.Login(body.User, body.Password); err == nil {
		s.auth.ClearTenantCookie(w, r)
		s.auth.SetCookie(w, r, tok)
		writeJSON(w, 200, map[string]any{"ok": true, "role": "admin", "name": s.cfg.Admin.User})
		return
	}
	k, err := s.keys.AuthenticateUser(body.User, body.Password)
	if err != nil {
		writeJSON(w, 401, map[string]any{"detail": err.Error()})
		return
	}
	tok, err := s.auth.LoginTenant(k.AK)
	if err != nil {
		writeJSON(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	s.auth.ClearCookie(w, r)
	s.auth.SetTenantCookie(w, r, tok)
	name := k.Name
	if name == "" {
		name = k.User
	}
	writeJSON(w, 200, map[string]any{"ok": true, "role": "tenant", "ak": k.AK, "user": k.User, "name": name})
}

func (s *Server) consoleLogout(w http.ResponseWriter, r *http.Request) {
	s.auth.ClearTenantCookie(w, r)
	s.auth.Logout(auth.CookieToken(r))
	s.auth.ClearCookie(w, r)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) consoleMe(w http.ResponseWriter, r *http.Request) {
	if s.auth.IsAdmin(r) {
		writeJSON(w, 200, map[string]any{"ok": true, "role": "admin", "name": s.cfg.Admin.User})
		return
	}
	ak := s.auth.TenantAK(r)
	name, user := ak, ""
	if k, ok := s.keys.Lookup(ak); ok {
		if k.Name != "" {
			name = k.Name
		}
		user = k.User
	}
	writeJSON(w, 200, map[string]any{"ok": true, "role": "tenant", "ak": ak, "user": user, "name": name})
}

func (s *Server) consoleJobs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	ak, admin := s.identity(r)
	if admin {
		if q := strings.TrimSpace(r.URL.Query().Get("ak")); q != "" {
			ak = q
			admin = false
		}
	}
	limit, offset := 50, 0
	for key, dst := range map[string]*int{"limit": &limit, "offset": &offset} {
		if value := r.URL.Query().Get(key); value != "" {
			n, err := strconv.Atoi(value)
			if err != nil || n < 0 || (key == "limit" && (n < 1 || n > 100)) {
				writeJSON(w, 400, map[string]any{"detail": "limit 应为 1–100，offset 应为非负整数"})
				return
			}
			*dst = n
		}
	}
	rows, total := s.jobs.ListSummariesVisible(ak, admin, offset, limit)
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, 200, map[string]any{"jobs": rows, "total": total, "limit": limit, "offset": offset})
}

func (s *Server) consoleJob(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/api/console/jobs/")
	id = strings.Trim(id, "/")
	ak, admin := s.identity(r)
	s.writeVisibleJob(w, id, ak, admin)
}

func (s *Server) consoleUsage(w http.ResponseWriter, r *http.Request) {
	ak, admin := s.identity(r)
	writeJSON(w, 200, map[string]any{"usage": s.jobs.Usage(ak, admin), "admin": admin})
}

func (s *Server) apiTenants(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, 200, map[string]any{"tenants": s.keys.PublicList()})
	case http.MethodPost:
		var body struct {
			Name     string `json:"name"`
			User     string `json:"user"`
			Password string `json:"password"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid json"})
			return
		}
		k, err := s.keys.Create(body.Name, body.User, body.Password, s.cfg.Admin.User)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{
			"ok": true, "ak": k.AK, "sk": k.SK, "user": k.User, "name": k.Name, "created_at": k.CreatedAt,
		})
	case http.MethodPut:
		var body struct {
			AK       string `json:"ak"`
			User     string `json:"user"`
			Password string `json:"password"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid json"})
			return
		}
		if err := s.keys.SetLogin(body.AK, body.User, body.Password, s.cfg.Admin.User); err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true, "ak": body.AK, "user": body.User})
	default:
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
	}
}

// applyRecentSMS fills a code that arrived before the login page was marked
// waiting. The inbound webhook only auto-submits while a site is already waiting.
func (s *Server) applyRecentSMS(accountID, siteID string, st *sessions.State) *sessions.State {
	if st == nil || (st.Status != "waiting_sms" && st.Status != "login_form") || st.Phone == "" {
		return st
	}
	now := time.Now()
	for _, msg := range s.sms.Recent(20) {
		if msg.Code == "" || !sms.PhoneMatch(msg.Receiver, st.Phone) {
			continue
		}
		if sms.SiteFromText(msg.Result) != siteID {
			continue
		}
		recv, err := time.ParseInLocation("2006-01-02T15:04:05", msg.Received, time.Local)
		if err != nil || now.Sub(recv) > 8*time.Minute || recv.Sub(now) > time.Minute {
			continue
		}
		if !smsMatchesSend(st.LastSendAt, recv) {
			continue
		}
		s.startSMSHandoff(accountID, siteID, msg)
		return st
	}
	return st
}

func smsMatchesSend(sentAt float64, received time.Time) bool {
	return sentAt <= 0 || float64(received.UnixNano())/1e9+2 >= sentAt
}

func (s *Server) smsInbound(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"ok": false, "detail": "method not allowed"})
		return
	}
	token := r.URL.Query().Get("token")
	if token == "" {
		token = r.Header.Get("X-Sms-Token")
	}
	if s.cfg.SMSToken != "" && token != s.cfg.SMSToken {
		writeJSON(w, 401, map[string]any{"ok": false, "detail": "token 无效"})
		return
	}
	var body struct {
		Type      string `json:"type"`
		Sender    string `json:"sender"`
		Receiver  string `json:"receiver"`
		RequestID string `json:"requestid"`
		Result    string `json:"result"`
		Time      string `json:"time"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "invalid json"})
		return
	}
	typ := strings.ToLower(strings.TrimSpace(body.Type))
	if typ != "" && typ != "recieve" && typ != "receive" {
		writeJSON(w, 200, map[string]any{"ok": true, "ignored": true})
		return
	}
	msg, added := s.sms.AddIfNew(sms.Message{
		Type: body.Type, Sender: body.Sender, Receiver: body.Receiver,
		RequestID: body.RequestID, Result: body.Result, Time: body.Time,
	})
	if !added {
		writeJSON(w, 200, map[string]any{
			"ok": true, "duplicate": true, "requestid": msg.RequestID,
			"code_detected": msg.Code != "", "pending": []string{},
		})
		return
	}
	pending := []string{}
	if msg.Code != "" {
		targetSite := sms.SiteFromText(msg.Result)
		type match struct{ accountID, siteID string }
		var matches []match
		for _, waiting := range s.sess.WaitingSMS() {
			if !sms.PhoneMatch(msg.Receiver, waiting.Phone) {
				continue
			}
			pending = append(pending, waiting.AccountID+":"+waiting.SiteID)
			if targetSite != "" && waiting.SiteID != targetSite {
				continue
			}
			matches = append(matches, match{waiting.AccountID, waiting.SiteID})
		}
		// A signed SMS can be filled even when the same phone is waiting on
		// another platform. An unsigned SMS is filled only when exactly one
		// login is waiting, so it is not typed into the wrong form.
		if len(matches) == 1 {
			s.startSMSHandoff(matches[0].accountID, matches[0].siteID, msg)
		} else if targetSite != "" {
			// The code arrived while a captcha still covered the login form, so
			// the site was not waiting yet. Keep trying until the form is ready.
			for _, account := range s.sess.List() {
				if account == nil || !account.Enabled || !sms.PhoneMatch(msg.Receiver, account.Phone) {
					continue
				}
				s.startSMSHandoff(account.ID, targetSite, msg)
				break
			}
		}
	}
	writeJSON(w, 200, map[string]any{
		"ok": true, "requestid": msg.RequestID, "code_detected": msg.Code != "", "pending": pending,
	})
}

func (s *Server) adminSMS(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	writeJSON(w, 200, map[string]any{"messages": s.sms.Recent(0)})
}

func publicSnapshot(snap map[string]any) map[string]any {
	out := map[string]any{"ready": snap["ready"]}
	raw, err := json.Marshal(snap["sites"])
	if err != nil {
		out["sites"] = []any{}
		return out
	}
	var sites []map[string]any
	if err := json.Unmarshal(raw, &sites); err != nil {
		out["sites"] = []any{}
		return out
	}
	for _, st := range sites {
		delete(st, "phone")
		delete(st, "last_send_target_id")
	}
	out["sites"] = sites
	return out
}

func (s *Server) writeVisibleJob(w http.ResponseWriter, id, ak string, admin bool) {
	if body, ok := s.jobs.VisibleBody(id, ak, admin); ok {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
		return
	}
	job, ok := s.jobs.GetVisible(id, ak, admin)
	if !ok {
		writeJSON(w, 404, map[string]any{"detail": "job not found"})
		return
	}
	writeJSON(w, 200, job)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
