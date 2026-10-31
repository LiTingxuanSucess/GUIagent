package httpserver

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"
	"time"
)

func TestMonitorHistoryUsesHalfOpenBeijingDayAndStrictDates(t *testing.T) {
	h, token, root := monitorTestServer(t)
	start := time.Date(2026, 10, 15, 16, 0, 0, 0, time.UTC).Unix()
	rows := []any{}
	for _, item := range []struct {
		id      string
		created int64
	}{{"before", start - 1}, {"midnight", start}, {"late", start + 86399}, {"next_midnight", start + 86400}} {
		rows = append(rows, map[string]any{"id": item.id, "created": item.created, "status": "succeeded", "sites": []string{"doubao"}, "results": []map[string]any{{"site": "doubao", "attempts": []map[string]any{{"channel": "web", "account_id": "acct_fixture", "ok": true}}}}})
	}
	writeMonitorJSON(t, filepath.Join(root, "data/monitor-requests.json"), map[string]any{"updated_at": start + 7*86400, "rows": rows})
	w := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests?window=day&date=2026-10-16&site=doubao&channel=web&account=acct_fixture&outcome=success")
	var body struct {
		Total int              `json:"total"`
		Rows  []monitorRequest `json:"rows"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || w.Code != 200 || body.Total != 2 || body.Rows[0].ID != "late" || body.Rows[1].ID != "midnight" {
		t.Fatalf("Beijing historical day boundaries/filters incorrect: %d %s", w.Code, w.Body.String())
	}
	for _, query := range []string{"window=day", "window=day&date=", "window=day&date=2026-02-29", "window=day&date=2026-04-31", "window=day&date=2026-1-01", "window=day&date=2026-01-1", "window=day&date=0000-01-01", "window=day&date=2026-10-16T00:00:00", "window=today&date=2026-10-16", "date=2026-10-16", "window=1h&date="} {
		if got := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests?"+query); got.Code != 400 {
			t.Errorf("invalid date query accepted: %s -> %d", query, got.Code)
		}
	}
	for _, date := range []string{"2024-02-29", "2026-10-18", "2030-01-01"} {
		w := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests?window=day&date="+date)
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || w.Code != 200 || body.Total != 0 || body.Rows == nil {
			t.Fatalf("empty valid day did not return an empty success: %s %d %s", date, w.Code, w.Body.String())
		}
	}
}

func TestMonitorCursorFilterUsesSameEventDateAndSupportsCallsWithoutResult(t *testing.T) {
	h, token, root := monitorTestServer(t)
	start := time.Date(2026, 10, 15, 16, 0, 0, 0, time.UTC).Unix()
	call := func(at int64, account, status string) map[string]any {
		return map[string]any{"id": account + status, "site": "doubao", "channel": "cursor", "account_id": account, "started_at": at, "status": status, "ok": nil, "task_type": "captcha_recovery"}
	}
	rows := []any{
		map[string]any{"id": "previous_job", "created": start - 86400, "sites": []string{"doubao"}, "cursor_calls": []any{call(start+60, "acct_running", "running")}},
		map[string]any{"id": "result_job", "created": start - 86400, "sites": []string{"doubao"}, "results": []any{map[string]any{"site": "doubao", "attempts": []any{call(start+120, "acct_success", "success")}}}},
		map[string]any{"id": "wrong_day", "created": start + 600, "sites": []string{"doubao"}, "results": []any{map[string]any{"site": "doubao", "attempts": []any{call(start-1, "acct_running", "running"), call(start+120, "acct_other", "unknown"), call(start+86400, "acct_running", "running")}}}},
	}
	writeMonitorJSON(t, filepath.Join(root, "data/monitor-requests.json"), map[string]any{"updated_at": start + 2*86400, "rows": rows})
	for _, query := range []string{"channel=cursor&outcome=running&account=acct_running", "channel=cursor&outcome=success&account=acct_success"} {
		w := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests?window=day&date=2026-10-16&site=doubao&"+query)
		var body struct {
			Total int `json:"total"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || w.Code != 200 || body.Total != 1 {
			t.Fatalf("cursor filters matched another event/date or lost a prior-day job: %s %d %s", query, w.Code, w.Body.String())
		}
	}
	w := monitorTestRequest(h, token, http.MethodGet, "/api/admin/monitor/requests/previous_job")
	var detail monitorRequestContent
	if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil || w.Code != 200 || len(detail.CursorCalls) != 1 || detail.CursorCalls[0].TaskType != "captcha_recovery" || detail.CursorCalls[0].OK != nil || detail.CursorCalls[0].Status != "running" {
		t.Fatal("independent CLI metadata was lost from request details")
	}
}
