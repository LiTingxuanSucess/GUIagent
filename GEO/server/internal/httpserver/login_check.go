package httpserver

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"geo-server/internal/sessions"
)

type loginCheckRow struct {
	AccountID    string `json:"account_id"`
	AccountLabel string `json:"account_label"`
	SiteID       string `json:"site_id"`
	Status       string `json:"status"`
	Detail       string `json:"detail,omitempty"`
	CLIUsed      bool   `json:"cli_used"`
	CLIError     string `json:"cli_error,omitempty"`
}

type loginCheckTask struct {
	ID         string          `json:"id,omitempty"`
	Status     string          `json:"status"`
	Total      int             `json:"total"`
	Completed  int             `json:"completed"`
	StartedAt  string          `json:"started_at,omitempty"`
	FinishedAt string          `json:"finished_at,omitempty"`
	Results    []loginCheckRow `json:"results"`
}

type monitorLoginChecks struct {
	mu   sync.Mutex
	task *loginCheckTask
}

func (c *monitorLoginChecks) snapshot() loginCheckTask {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.task == nil {
		return loginCheckTask{Status: "idle", Results: []loginCheckRow{}}
	}
	copy := *c.task
	copy.Results = append([]loginCheckRow{}, c.task.Results...)
	return copy
}

func (c *monitorLoginChecks) start(rows []loginCheckRow, run func(loginCheckRow, func()) loginCheckRow) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.task != nil && c.task.Status == "running" {
		return
	}
	task := &loginCheckTask{ID: fmt.Sprintf("%x", time.Now().UnixNano()), Status: "running", Total: len(rows),
		StartedAt: time.Now().Format(time.RFC3339), Results: append([]loginCheckRow{}, rows...)}
	c.task = task
	go func() {
		defer func() {
			failure := recover()
			c.mu.Lock()
			defer c.mu.Unlock()
			task.Status = "completed"
			if failure != nil {
				task.Status = "failed"
				log.Printf("operator login check stopped unexpectedly")
			}
			task.FinishedAt = time.Now().Format(time.RFC3339)
		}()
		for i, row := range rows {
			c.mu.Lock()
			task.Results[i].Status = "checking"
			c.mu.Unlock()
			result := run(row, func() {
				c.mu.Lock()
				task.Results[i].Status = "assisting"
				c.mu.Unlock()
			})
			c.mu.Lock()
			task.Results[i] = result
			task.Completed++
			c.mu.Unlock()
		}
	}()
}

func (s *Server) adminLoginCheck(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method == http.MethodGet {
		writeJSON(w, http.StatusOK, s.loginChecks.snapshot())
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "method not allowed"})
		return
	}
	if s.sess == nil || s.jobs == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"detail": "登录检测暂不可用"})
		return
	}
	var body struct {
		SiteID     string   `json:"site_id"`
		AccountIDs []string `json:"account_ids"`
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil && err != io.EOF {
		writeJSON(w, http.StatusBadRequest, map[string]any{"detail": "请求格式错误"})
		return
	}
	if body.SiteID != "" {
		if _, ok := sessions.SiteByID(body.SiteID); !ok {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": "未知平台"})
			return
		}
	}
	accounts := s.sess.List()
	selected := map[string]bool{}
	if len(body.AccountIDs) > 50 {
		writeJSON(w, 400, map[string]any{"detail": "单次最多选择 50 个账号"})
		return
	}
	for _, id := range body.AccountIDs {
		id = strings.TrimSpace(id)
		found := false
		for _, account := range accounts {
			if account.ID == id && account.Enabled {
				found = true
				break
			}
		}
		if !found {
			writeJSON(w, 400, map[string]any{"detail": "选择的账号不存在或已停用"})
			return
		}
		selected[id] = true
	}
	if current := s.loginChecks.snapshot(); current.Status == "running" {
		writeJSON(w, http.StatusAccepted, current)
		return
	}
	rows := []loginCheckRow{}
	for _, account := range accounts {
		if !account.Enabled || len(selected) > 0 && !selected[account.ID] {
			continue
		}
		label := "账号"
		if len(account.Phone) >= 4 {
			label = "手机尾号 " + account.Phone[len(account.Phone)-4:]
		}
		for _, site := range sessions.Sites {
			if body.SiteID == "" || body.SiteID == site.ID {
				rows = append(rows, loginCheckRow{AccountID: account.ID, AccountLabel: label, SiteID: site.ID, Status: "pending"})
			}
		}
	}
	s.loginChecks.start(rows, s.runLoginCheck)
	writeJSON(w, http.StatusAccepted, s.loginChecks.snapshot())
}

func (s *Server) runLoginCheck(row loginCheckRow, onCLI func()) loginCheckRow {
	out, err := s.jobs.CheckAccountLogin(s.sess, row.AccountID, row.SiteID, onCLI)
	row.CLIUsed, row.CLIError = out.CLIUsed, out.CLIError
	if errors.Is(err, sessions.ErrOperatorLoginBusy) {
		row.Status, row.Detail = "busy", "账号或浏览器正忙，本次未检测，可稍后重试"
		return row
	}
	if out.State == nil {
		row.Status, row.Detail = "error", "登录检测失败，请稍后重试"
		return row
	}
	state := out.State
	if s.sms != nil {
		state = s.applyRecentSMS(row.AccountID, row.SiteID, state)
	}
	row.Status = state.Status
	switch row.Status {
	case "logged_in":
		row.Detail = "已通过登录检测"
	case "waiting_sms":
		row.Detail = "等待短信回调或人工填写验证码"
	case "need_captcha":
		row.Detail = "验证尚未通过，请处理验证后重试"
	default:
		row.Detail = "尚未确认登录，请在号池中查看该账号"
	}
	if err != nil {
		log.Printf("operator login check account=%s site=%s cli_code=%s: %v", row.AccountID, row.SiteID, row.CLIError, err)
	}
	return row
}
