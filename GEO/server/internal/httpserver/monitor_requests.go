package httpserver

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// The request index contains metadata only. Full question/answer content is
// loaded separately, for an authenticated detail request for one validated ID.
type monitorAttempt struct {
	Channel          string              `json:"channel"`
	AccountID        string              `json:"account_id"`
	AccountLabel     string              `json:"account_label"`
	OK               *bool               `json:"ok"`
	Status           string              `json:"status"`
	ErrorCode        string              `json:"error_code"`
	ErrorLabel       string              `json:"error_label,omitempty"`
	FailureStage     string              `json:"failure_stage,omitempty"`
	FailureReason    string              `json:"failure_reason,omitempty"`
	DiagnosticID     string              `json:"diagnostic_id,omitempty"`
	StartedAt        float64             `json:"started_at"`
	ElapsedMS        int64               `json:"elapsed_ms"`
	Skipped          bool                `json:"skipped"`
	SkipReason       string              `json:"skip_reason,omitempty"`
	Inferred         bool                `json:"inferred,omitempty"`
	TaskType         string              `json:"task_type,omitempty"`
	Verified         *bool               `json:"verified"`
	ResumeOK         *bool               `json:"resume_ok"`
	Phase            monitorCLIPhase     `json:"phase,omitempty"`
	ProcessStatus    string              `json:"process_status,omitempty"`
	ProcessErrorCode string              `json:"process_error_code,omitempty"`
	RecoveryOrigin   string              `json:"recovery_origin,omitempty"`
	ResumeErrorCode  string              `json:"resume_error_code,omitempty"`
	BuildID          string              `json:"build_id,omitempty"`
	ConfigHash       string              `json:"config_hash,omitempty"`
	Model            string              `json:"model,omitempty"`
	CLIAssisted      *bool               `json:"cli_assisted,omitempty"`
	ToolCalls        int                 `json:"tool_calls,omitempty"`
	CLIStageTimings  monitorStageTimings `json:"stage_timings,omitempty"`
}

type monitorResult struct {
	Site                        string              `json:"site"`
	OK                          bool                `json:"ok"`
	Source                      string              `json:"source"`
	AccountID                   string              `json:"account_id"`
	AccountLabel                string              `json:"account_label"`
	ErrorCode                   string              `json:"error_code"`
	ErrorLabel                  string              `json:"error_label,omitempty"`
	WebErrorCode                string              `json:"web_error_code"`
	APIErrorCode                string              `json:"api_error_code"`
	WebSearchErrorCode          string              `json:"web_search_error_code"`
	WebSearchStatus             string              `json:"web_search_status"`
	WebSearchMode               string              `json:"web_search_mode"`
	WebSearchProvider           string              `json:"web_search_provider"`
	ShareStatus                 string              `json:"share_status"`
	OptionalStageErrorCode      string              `json:"optional_stage_error_code,omitempty"`
	ShareErrorCode              string              `json:"share_error_code,omitempty"`
	Citations                   int                 `json:"citations"`
	WebMS                       int64               `json:"web_ms"`
	APIMS                       int64               `json:"api_ms"`
	WebSkipped                  bool                `json:"web_skipped"`
	APIFallbackFailed           bool                `json:"api_fallback_failed"`
	DiagnosticID                string              `json:"diagnostic_id,omitempty"`
	Attempts                    []monitorAttempt    `json:"attempts"`
	StageTimings                monitorStageTimings `json:"stage_timings,omitempty"`
	WebRecovery                 *monitorWebRecovery `json:"web_recovery,omitempty"`
	DeliveryPending             bool                `json:"delivery_pending,omitempty"`
	RetryState                  monitorRetryState   `json:"retry_state,omitempty"`
	AttemptsTruncated           bool                `json:"attempts_truncated,omitempty"`
	AttemptsDropped             int64               `json:"attempts_dropped,omitempty"`
	DeliveryCheckpointRecovered bool                `json:"delivery_checkpoint_recovered,omitempty"`
}

// Keep timing metadata numeric and bounded to public stage names. Malformed
// optional timings must not make the entire historical request index unreadable.
type monitorStageTimings map[string]float64

func (timings *monitorStageTimings) UnmarshalJSON(raw []byte) error {
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil {
		*timings = nil
		return nil
	}
	values := monitorStageTimings{}
	for _, key := range []string{"pool_wait_ms", "site_gate_wait_ms", "cloud_slot_wait_ms", "web_slot_wait_ms", "session_acquire_ms", "session_setup_ms", "web_execution_ms", "api_wait_ms", "api_execution_ms", "cli_wait_ms", "cli_preflight_ms", "cli_execution_ms", "cli_first_output_ms", "cli_first_tool_ms", "cli_verify_ms", "cli_resume_ms"} {
		var value float64
		encoded, exists := fields[key]
		if !exists || string(encoded) == "null" || json.Unmarshal(encoded, &value) != nil || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
			continue
		}
		values[key] = value
	}
	if len(values) == 0 {
		values = nil
	}
	*timings = values
	return nil
}

type monitorCursorCall struct {
	monitorAttempt
	ID         string  `json:"id"`
	JobID      string  `json:"job_id"`
	Site       string  `json:"site"`
	FinishedAt float64 `json:"finished_at"`
}

type monitorRequest struct {
	ID             string                `json:"id"`
	Status         string                `json:"status"`
	Created        float64               `json:"created"`
	Updated        float64               `json:"updated"`
	Finished       float64               `json:"finished"`
	Deadline       float64               `json:"deadline"`
	DeadlinePolicy string                `json:"deadline_policy,omitempty"`
	DeliveryPolicy monitorDeliveryPolicy `json:"delivery_policy,omitempty"`
	RetryAt        monitorRetryAt        `json:"retry_at,omitempty"`
	RetryCount     int                   `json:"retry_count,omitempty"`
	WaitReason     string                `json:"wait_reason,omitempty"`
	TotalMS        int64                 `json:"total_ms"`
	QueuedMS       int64                 `json:"queued_ms"`
	Sites          []string              `json:"sites"`
	Results        []monitorResult       `json:"results"`
	ErrorCode      string                `json:"error_code"`
	CallbackFailed bool                  `json:"callback_failed"`
	CursorCalls    []monitorCursorCall   `json:"cursor_calls,omitempty"`
	RecoveryStates monitorRecoveryStates `json:"recovery_states,omitempty"`
}

type monitorRequestIndex struct {
	UpdatedAt   float64             `json:"updated_at"`
	Rows        []monitorRequest    `json:"rows"`
	CursorCalls []monitorCursorCall `json:"cursor_calls"`
}

type monitorRequestCache struct {
	mu       sync.Mutex
	path     string
	modified time.Time
	size     int64
	index    *monitorRequestIndex
}

func (s *Server) readMonitorRequests() (*monitorRequestIndex, error) {
	root := filepath.Join(s.cfg.Root, "data")
	if configured := os.Getenv("GEO_DATA_DIR"); configured != "" {
		root = configured
	}
	path := filepath.Join(root, "monitor-requests.json")
	c := &s.monitorRequests
	c.mu.Lock()
	defer c.mu.Unlock()
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if c.index != nil && c.path == path && c.modified.Equal(info.ModTime()) && c.size == info.Size() {
		return c.index, nil
	}
	if info.Size() > 32<<20 {
		return nil, fmt.Errorf("monitor request index exceeds limit")
	}
	var index monitorRequestIndex
	if err := json.NewDecoder(io.LimitReader(f, 32<<20)).Decode(&index); err != nil {
		return nil, err
	}
	if index.Rows == nil {
		index.Rows = []monitorRequest{}
	}
	sort.SliceStable(index.Rows, func(i, j int) bool {
		if index.Rows[i].Created == index.Rows[j].Created {
			return index.Rows[i].ID > index.Rows[j].ID
		}
		return index.Rows[i].Created > index.Rows[j].Created
	})
	c.path, c.modified, c.size, c.index = path, info.ModTime(), info.Size(), &index
	return &index, nil
}

var monitorIdentifier = regexp.MustCompile(`^[a-zA-Z0-9_:-]{1,100}$`)

type monitorQuery struct {
	window, date, site, channel, account, status, outcome, errorCode string
	page, pageSize                                                   int
}

func parseMonitorQuery(r *http.Request) (monitorQuery, error) {
	v := r.URL.Query()
	q := monitorQuery{window: v.Get("window"), date: v.Get("date"), site: v.Get("site"), channel: v.Get("channel"), account: v.Get("account"), status: v.Get("status"), outcome: v.Get("outcome"), errorCode: v.Get("error"), page: 1, pageSize: 25}
	if q.window == "" {
		q.window = "today"
	}
	valid := func(value string, allowed ...string) bool {
		for _, a := range allowed {
			if value == a {
				return true
			}
		}
		return false
	}
	if !valid(q.window, "15m", "1h", "6h", "24h", "today", "afternoon", "day") ||
		!valid(q.site, "", "deepseek", "doubao", "yuanbao", "kimi", "qianwen", "wenxinyiyan", "yiyan") ||
		!valid(q.channel, "", "web", "api", "cursor") || !valid(q.status, "", "queued", "running", "succeeded", "partial", "failed", "cancelled") ||
		!valid(q.outcome, "", "success", "failed", "skipped", "unknown", "running", "waiting_sms", "action_required") ||
		(q.account != "" && !monitorIdentifier.MatchString(q.account)) || (q.errorCode != "" && !monitorIdentifier.MatchString(q.errorCode)) {
		return q, fmt.Errorf("筛选条件无效")
	}
	if q.window == "day" {
		if _, _, err := monitorDayBounds(q.date); err != nil {
			return q, err
		}
	} else if _, present := v["date"]; present {
		return q, fmt.Errorf("date 仅支持 window=day")
	}
	for _, p := range []struct {
		name   string
		target *int
		max    int
	}{{"page", &q.page, 1000000}, {"page_size", &q.pageSize, 100}} {
		if text := v.Get(p.name); text != "" {
			n, err := strconv.Atoi(text)
			if err != nil || n < 1 || n > p.max {
				return q, fmt.Errorf("分页参数无效")
			}
			*p.target = n
		}
	}
	if q.site == "yiyan" {
		q.site = "wenxinyiyan"
	}
	return q, nil
}

var monitorCalendarDate = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}$`)

func monitorDayBounds(date string) (start float64, end float64, err error) {
	if !monitorCalendarDate.MatchString(date) {
		return 0, 0, fmt.Errorf("日期须为有效的 YYYY-MM-DD 北京时间日期")
	}
	day, err := time.ParseInLocation("2006-01-02", date, time.FixedZone("Asia/Shanghai", 8*60*60))
	if err != nil || day.Year() < 1 {
		return 0, 0, fmt.Errorf("日期须为有效的 YYYY-MM-DD 北京时间日期")
	}
	return float64(day.Unix()), float64(day.AddDate(0, 0, 1).Unix()), nil
}

func monitorWindowStart(window string, now float64) float64 {
	seconds := map[string]float64{"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}
	if span, ok := seconds[window]; ok {
		return now - span
	}
	t := time.Unix(int64(now), 0).In(time.FixedZone("Asia/Shanghai", 8*60*60))
	hour := 0
	if window == "afternoon" {
		hour = 12
	}
	return float64(time.Date(t.Year(), t.Month(), t.Day(), hour, 0, 0, 0, t.Location()).Unix())
}

func matchesMonitorRequest(row monitorRequest, q monitorQuery) bool {
	if q.status != "" && row.Status != q.status {
		return false
	}
	attemptFilter := q.channel != "" || q.account != "" || q.outcome != "" || q.errorCode != ""
	if !attemptFilter {
		if q.site == "" {
			return true
		}
		for _, site := range row.Sites {
			if site == q.site || (site == "yiyan" && q.site == "wenxinyiyan") {
				return true
			}
		}
	}
	for _, result := range row.Results {
		if q.site != "" && result.Site != q.site && !(result.Site == "yiyan" && q.site == "wenxinyiyan") {
			continue
		}
		for _, attempt := range result.Attempts {
			if matchesMonitorAttempt(attempt, q) {
				return true
			}
		}
	}
	for _, call := range row.CursorCalls {
		if matchesMonitorCursorCall(call, q) {
			return true
		}
	}
	return false
}

func matchesMonitorCursorCall(call monitorCursorCall, q monitorQuery) bool {
	return (q.site == "" || call.Site == q.site || call.Site == "yiyan" && q.site == "wenxinyiyan") && matchesMonitorAttempt(call.monitorAttempt, q)
}

func matchesMonitorAttempt(attempt monitorAttempt, q monitorQuery) bool {
	if q.channel != "" && attempt.Channel != q.channel ||
		q.account == "unknown" && attempt.AccountID != "" ||
		q.account != "" && q.account != "unknown" && attempt.AccountID != q.account ||
		q.errorCode != "" && attempt.ErrorCode != q.errorCode {
		return false
	}
	status := attempt.Status
	if status == "" {
		switch {
		case attempt.Skipped:
			status = "skipped"
		case attempt.OK == nil:
			status = "unknown"
		case *attempt.OK:
			status = "success"
		default:
			status = "failed"
		}
	}
	return q.outcome == "" || status == q.outcome
}

func matchesMonitorRequestInWindow(row monitorRequest, q monitorQuery, start, end float64) bool {
	within := func(timestamp float64) bool {
		return timestamp >= start && (timestamp < end || q.window != "day" && timestamp == end)
	}
	if q.channel != "cursor" {
		return within(row.Created) && matchesMonitorRequest(row, q)
	}
	if q.status != "" && row.Status != q.status {
		return false
	}
	for _, result := range row.Results {
		if q.site != "" && result.Site != q.site && !(result.Site == "yiyan" && q.site == "wenxinyiyan") {
			continue
		}
		for _, attempt := range result.Attempts {
			if within(attempt.StartedAt) && matchesMonitorAttempt(attempt, q) {
				return true
			}
		}
	}
	for _, call := range row.CursorCalls {
		if within(call.StartedAt) && matchesMonitorCursorCall(call, q) {
			return true
		}
	}
	return false
}

func (s *Server) apiMonitorRequests(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	q, err := parseMonitorQuery(r)
	if err != nil {
		writeJSON(w, 400, map[string]any{"detail": err.Error()})
		return
	}
	index, err := s.readMonitorRequests()
	if err != nil {
		writeJSON(w, 503, map[string]any{"detail": "请求明细尚未就绪，请稍后刷新"})
		return
	}
	start := monitorWindowStart(q.window, index.UpdatedAt)
	end := index.UpdatedAt
	if q.window == "day" {
		start, end, _ = monitorDayBounds(q.date)
	}
	rows := []monitorRequest{}
	offset := (q.page - 1) * q.pageSize
	total := 0
	for _, row := range index.Rows {
		if !matchesMonitorRequestInWindow(row, q, start, end) {
			continue
		}
		if total >= offset && len(rows) < q.pageSize {
			rows = append(rows, row)
		}
		total++
	}
	writeJSON(w, 200, map[string]any{"updated_at": index.UpdatedAt, "total": total, "page": q.page, "page_size": q.pageSize, "rows": rows})
}

func (s *Server) apiMonitorRequest(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/api/admin/monitor/requests/")
	if !monitorIdentifier.MatchString(id) || strings.Contains(id, ":") {
		writeJSON(w, 404, map[string]any{"detail": "请求不存在"})
		return
	}
	index, err := s.readMonitorRequests()
	if err != nil {
		writeJSON(w, 503, map[string]any{"detail": "请求明细尚未就绪，请稍后刷新"})
		return
	}
	for _, row := range index.Rows {
		if row.ID == id {
			writeJSON(w, 200, s.monitorRequestContent(row))
			return
		}
	}
	writeJSON(w, 404, map[string]any{"detail": "请求不存在或已超出看板保留范围"})
}
