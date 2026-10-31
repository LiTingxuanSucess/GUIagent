package httpserver

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"geo-server/internal/jobs"
	"geo-server/internal/sessions"
	"geo-server/internal/sms"
)

func TestMonitorLimitsOnlyEnabledAccountsAndNoPhone(t *testing.T) {
	rows := currentMonitorLimits([]jobs.AccountLimit{{AccountID: "enabled", Site: "kimi", Kind: "quota_exhausted"}, {AccountID: "disabled", Site: "deepseek"}, {AccountID: "deleted", Site: "kimi"}}, []*sessions.Account{
		{ID: "enabled", Phone: "13900001234", Enabled: true}, {ID: "disabled", Phone: "13900005678", Enabled: false},
	})
	raw, _ := json.Marshal(rows)
	if len(rows) != 1 || rows[0].AccountLabel != "手机尾号 1234" || strings.Contains(string(raw), "13900001234") {
		t.Fatalf("unsafe limits: %s", raw)
	}
}

func TestCursorReviewIncludesMaintenanceFiltersAndExportsAllPages(t *testing.T) {
	h, token, root := monitorTestServer(t)
	for _, path := range []string{"/api/admin/monitor/cursor", "/api/admin/monitor/actions"} {
		if got := monitorTestRequest(h, "", "GET", path).Code; got != 401 {
			t.Fatalf("unprotected endpoint %s: %d", path, got)
		}
		if got := monitorTestRequest(h, token, "POST", path).Code; got != 405 {
			t.Fatalf("writable endpoint %s: %d", path, got)
		}
	}
	start, end, _ := monitorDayBounds("2026-10-20")
	rows := []map[string]any{
		{"id": "business", "job_id": "job_a", "site": "deepseek", "channel": "cursor", "account_id": "acct_a", "account_label": "手机尾号 1234", "status": "failed", "error_code": "cli_timeout", "started_at": start + 20},
		{"id": "maintenance", "job_id": "login-check", "site": "kimi", "channel": "cursor", "account_id": "acct_a", "account_label": "手机尾号 1234", "status": "waiting_sms", "error_code": "sms_required", "started_at": start + 30, "secret": "must-not-leak"},
		{"id": "tomorrow", "site": "doubao", "channel": "cursor", "status": "failed", "started_at": end},
	}
	index := map[string]any{"updated_at": start + 100, "rows": []map[string]any{{"id": "job_a", "status": "succeeded"}}, "cursor_calls": rows}
	data, _ := json.Marshal(index)
	if err := os.MkdirAll(filepath.Join(root, "data"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "data/monitor-requests.json"), data, 0600); err != nil {
		t.Fatal(err)
	}
	base := "/api/admin/monitor/cursor?window=day&date=2026-10-20"
	w := monitorTestRequest(h, token, "GET", base+"&page_size=1")
	var body struct {
		Total int                   `json:"total"`
		Rows  []monitorCursorReview `json:"rows"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || w.Code != 200 {
		t.Fatalf("%d %s %v", w.Code, w.Body.String(), err)
	}
	if body.Total != 2 || len(body.Rows) != 1 || body.Rows[0].ParentStatus != "account_maintenance" || strings.Contains(w.Body.String(), "must-not-leak") {
		t.Fatalf("maintenance missing or unsafe: %s", w.Body.String())
	}
	w = monitorTestRequest(h, token, "GET", base+"&outcome=failed")
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body.Total != 1 || body.Rows[0].ParentStatus != "succeeded" {
		t.Fatalf("attempt/business outcomes conflated: %s", w.Body.String())
	}
	w = monitorTestRequest(h, token, "GET", base+"&page_size=1&format=csv")
	if !strings.Contains(w.Body.String(), "maintenance") || !strings.Contains(w.Body.String(), "business") || strings.Contains(w.Body.String(), "tomorrow") || !strings.Contains(w.Header().Get("Content-Disposition"), "attachment") {
		t.Fatalf("export incomplete: %s", w.Body.String())
	}
	if monitorTestRequest(h, token, "GET", base+"&outcome=bad").Code != 400 {
		t.Fatal("invalid filter accepted")
	}
	if safeCSVCell("=1+2") != "'=1+2" || safeCSVCell("  @command") != "'  @command" {
		t.Fatal("unsafe spreadsheet cell")
	}
}

func TestCurrentActionsUseAccountStateAndBoundedMatchedSMS(t *testing.T) {
	now := float64(time.Now().Unix())
	phone := "13900001234"
	accounts := []*sessions.Account{
		{ID: "acct_a", Phone: phone, Enabled: true, Sites: map[string]sessions.State{
			"kimi":     {Status: "logged_in", LastSendAt: now - 20},
			"deepseek": {Status: "waiting_sms", LastSendAt: now - 60},
			"doubao":   {Status: "need_captcha", LastSendAt: now - 60},
			"qianwen":  {Status: "not_logged_in"},
		}},
		{ID: "acct_disabled", Phone: phone, Enabled: false, Sites: map[string]sessions.State{"kimi": {Status: "need_captcha"}}},
	}
	format := func(at float64) string { return time.Unix(int64(at), 0).Format("2006-01-02T15:04:05") }
	messages := []sms.Message{
		{Receiver: phone, Result: "【DeepSeek】验证码", Code: "123456", Received: format(now - 120)},
		{Receiver: phone, Result: "【Kimi】验证码", Code: "123456", Received: format(now - 10)},
		{Receiver: "13900005678", Result: "【DeepSeek】验证码", Code: "123456", Received: format(now - 10)},
	}
	rows := currentMonitorActions(accounts, messages, now)
	if len(rows) != 2 {
		t.Fatalf("historical/logged-in/disabled items became current work: %+v", rows)
	}
	var waiting monitorAction
	for _, row := range rows {
		if row.Site == "deepseek" {
			waiting = row
		}
	}
	if waiting.Kind != "automatic" || waiting.ReceivedAt != 0 || waiting.SMSState != "not_received" {
		t.Fatalf("wrong platform/account/generation matched: %+v", waiting)
	}
	messages = append(messages, sms.Message{Receiver: phone, Result: "【DeepSeek】验证码", Code: "987654", Received: format(now - 10)})
	rows = currentMonitorActions(accounts, messages, now+200)
	for _, row := range rows {
		if row.Site == "deepseek" {
			if row.Kind != "manual" || row.SMSState != "received_unconfirmed" || row.ReceivedAt == 0 {
				t.Fatalf("expired wait or receipt called login success: %+v", row)
			}
		}
	}
	raw, _ := json.Marshal(rows)
	if strings.Contains(string(raw), phone) || strings.Contains(string(raw), "987654") || strings.Contains(string(raw), "123456") {
		t.Fatalf("phone or OTP leaked: %s", raw)
	}
}
