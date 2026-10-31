package httpserver

import (
	"encoding/csv"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"geo-server/internal/jobs"
	"geo-server/internal/sessions"
	"geo-server/internal/sms"
)

// Historical handoffs are immutable attempts, not today's operator queue.
type monitorCursorReview struct {
	monitorCursorCall
	ParentStatus string `json:"parent_status"`
	Assessment   string `json:"assessment"`
}

func cursorAssessment(call monitorCursorCall) string {
	switch call.ErrorCode {
	case "platform_quota":
		return "账号额度或禁言限制尚未到复查时间，未启动 CLI；恢复时间见额度与账号限制区"
	case "cli_billing_required":
		return "Cursor 账单阻断，本次未完成页面修复；不代表当前仍欠费"
	case "cli_timeout":
		return "执行预算用尽，修复未完成；需结合工具调用数与页面复核定位"
	case "cli_no_output":
		return "一段时间内没有新的 CLI 输出，不等同于从未执行工具"
	case "sms_required":
		return "当次转交短信流程；请在当前待处理区确认现在是否仍需处理"
	case "sms_timeout":
		return "当次等待短信或页面复核超时；不等于回调接口故障"
	case "cli_resume_failed":
		return "页面复核通过，但原网页续跑失败；最终业务结果单独查看"
	case "target_missing":
		return "原网页目标已丢失，不能自动重发原问题"
	case "no_progress":
		return "当次页面没有进展，修复未完成"
	case "cli_account_wait_timeout", "cli_busy":
		return "账号正在被其他恢复占用，本次未执行"
	case "service_interrupted":
		return "调用随服务停止中断"
	case "cli_verification_failed":
		return "CLI 结束后的独立页面复核未通过"
	case "operator_required":
		return "当次请求人工处理；当前是否仍需处理以账号状态为准"
	}
	if call.Status == "success" {
		return "当次页面恢复成功；业务回答及网页续跑分别统计"
	}
	return "现有证据不足以进一步定位，请查看关联请求"
}

func (s *Server) apiMonitorCursor(w http.ResponseWriter, r *http.Request) {
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
	if format := r.URL.Query().Get("format"); format != "" && format != "csv" {
		writeJSON(w, 400, map[string]any{"detail": "导出格式无效"})
		return
	}
	index, err := s.readMonitorRequests()
	if err != nil {
		writeJSON(w, 503, map[string]any{"detail": "CLI 明细暂不可用"})
		return
	}
	now := float64(time.Now().UnixNano()) / 1e9
	start, end := monitorWindowStart(q.window, now), now
	if q.window == "day" {
		start, end, _ = monitorDayBounds(q.date)
	}
	parents := map[string]string{}
	for _, row := range index.Rows {
		parents[row.ID] = row.Status
	}
	rows := []monitorCursorReview{}
	reasons := map[string]int{}
	for _, call := range index.CursorCalls {
		if call.StartedAt < start || call.StartedAt > end || q.window == "day" && call.StartedAt == end {
			continue
		}
		if !matchesMonitorCursorCall(call, q) || q.status != "" && parents[call.JobID] != q.status {
			continue
		}
		parent := parents[call.JobID]
		if parent == "" {
			parent = "unrecorded"
			if call.JobID == "login-check" || call.JobID == "manual-login" {
				parent = "account_maintenance"
			}
		}
		rows = append(rows, monitorCursorReview{call, parent, cursorAssessment(call)})
		if call.Status == "failed" {
			reasons[call.ErrorCode]++
		}
	}
	sort.SliceStable(rows, func(i, j int) bool {
		if rows[i].StartedAt == rows[j].StartedAt {
			return rows[i].ID > rows[j].ID
		}
		return rows[i].StartedAt > rows[j].StartedAt
	})
	if r.URL.Query().Get("format") == "csv" {
		w.Header().Set("Content-Type", "text/csv; charset=utf-8")
		w.Header().Set("Content-Disposition", `attachment; filename="cursor-calls.csv"`)
		_, _ = w.Write([]byte("\xef\xbb\xbf"))
		out := csv.NewWriter(w)
		_ = out.Write([]string{"调用编号", "关联请求", "用途", "平台", "账号", "当次结果", "原因代码", "逐条分析", "业务任务状态", "页面复核", "原网页续跑", "耗时毫秒", "工具调用数", "开始时间(北京时间)", "版本"})
		for _, row := range rows {
			fields := []string{row.ID, row.JobID, row.TaskType, row.Site, row.AccountLabel, row.Status, row.ErrorCode, row.Assessment, row.ParentStatus, optionalBool(row.Verified), optionalBool(row.ResumeOK), strconv.FormatInt(row.ElapsedMS, 10), strconv.Itoa(row.ToolCalls), time.Unix(int64(row.StartedAt), 0).In(time.FixedZone("CST", 8*3600)).Format(time.RFC3339), row.BuildID}
			for i, value := range fields {
				fields[i] = safeCSVCell(value)
			}
			_ = out.Write(fields)
		}
		out.Flush()
		return
	}
	total := len(rows)
	offset := (q.page - 1) * q.pageSize
	if offset > total {
		offset = total
	}
	limit := offset + q.pageSize
	if limit > total {
		limit = total
	}
	writeJSON(w, 200, map[string]any{"updated_at": index.UpdatedAt, "rows": rows[offset:limit], "total": total, "page": q.page, "page_size": q.pageSize, "failure_reasons": reasons})
}

func optionalBool(value *bool) string {
	if value == nil {
		return "unknown"
	}
	return strconv.FormatBool(*value)
}
func safeCSVCell(value string) string {
	trimmed := strings.TrimLeft(value, " \t\r\n")
	if trimmed != "" && strings.ContainsRune("=+-@", rune(trimmed[0])) {
		return "'" + value
	}
	return value
}

type monitorAction struct {
	AccountID        string  `json:"account_id"`
	AccountLabel     string  `json:"account_label"`
	Site             string  `json:"site"`
	Status           string  `json:"status"`
	Kind             string  `json:"kind"`
	Reason           string  `json:"reason"`
	NextAction       string  `json:"next_action"`
	SMSState         string  `json:"sms_state"`
	SentAt           float64 `json:"sent_at"`
	ReceivedAt       float64 `json:"received_at"`
	UpdatedAt        string  `json:"updated_at"`
	URL              string  `json:"url"`
	Busy             bool    `json:"busy"`
	HandoffStage     string  `json:"handoff_stage,omitempty"`
	HandoffUpdatedAt string  `json:"handoff_updated_at,omitempty"`
}

func monitorSMSReceived(messages []sms.Message, phone, site string, sent float64) float64 {
	if sent <= 0 {
		return 0
	}
	latest := float64(0)
	for _, msg := range messages {
		if msg.Code == "" || !sms.PhoneMatch(msg.Receiver, phone) || sms.SiteFromText(msg.Result) != site {
			continue
		}
		at, err := time.ParseInLocation("2006-01-02T15:04:05", msg.Received, time.Local)
		if err != nil {
			at, err = time.Parse(time.RFC3339, msg.Received)
		}
		stamp := float64(at.Unix())
		// Metadata evidence only: do not expose the OTP or infer a successful submit.
		if err == nil && stamp+2 >= sent && stamp > latest {
			latest = stamp
		}
	}
	return latest
}

func currentMonitorActions(accounts []*sessions.Account, messages []sms.Message, now float64) []monitorAction {
	rows := []monitorAction{}
	for _, account := range accounts {
		if account == nil || !account.Enabled {
			continue
		}
		for site, state := range account.Sites {
			reason, next := "", ""
			switch state.Status {
			case "need_captcha":
				reason = "页面验证尚未通过"
				next = "打开原页面处理验证码，再重新检测"
			case "waiting_sms":
				reason = "等待短信流程完成"
				next = "查看短信接收与原登录页面"
			case "login_form", "expired", "not_logged_in":
				if state.LastSendAt <= 0 && state.LastCheckedAt == "" {
					continue
				}
				reason = "登录尚未完成"
				next = "打开原页面确认登录状态"
			default:
				continue
			}
			label := "账号"
			if len(account.Phone) >= 4 {
				label = "手机尾号 " + account.Phone[len(account.Phone)-4:]
			}
			row := monitorAction{AccountID: account.ID, AccountLabel: label, Site: site, Status: state.Status, Kind: "manual", Reason: reason, NextAction: next, SentAt: state.LastSendAt, UpdatedAt: state.UpdatedAt, SMSState: "not_sent"}
			row.URL = "/admin?" + url.Values{"account": {account.ID}, "site": {site}, "inspect": {"1"}}.Encode()
			if state.LastSendAt > 0 {
				row.ReceivedAt = monitorSMSReceived(messages, account.Phone, site, state.LastSendAt)
				row.SMSState = "not_received"
				if row.ReceivedAt > 0 {
					row.SMSState = "received_unconfirmed"
				}
			}
			if state.Status == "waiting_sms" {
				switch {
				case state.LastSendAt <= 0:
					row.Reason = "页面显示待短信，但未记录发码确认"
					row.NextAction = "查看原页面确认是否已发码"
				case now-state.LastSendAt > 180:
					row.Reason = "短信等待已超过自动接续窗口"
					row.NextAction = "检查短信通道及原页面；确认后再决定是否重新发码"
					if row.ReceivedAt == 0 {
						row.SMSState = "callback_missing"
					} else {
						row.SMSState = "received_unconfirmed"
					}
				default:
					row.Kind = "automatic"
					row.NextAction = "等待自动回填；无需手工抄码或重复发码"
				}
			}
			rows = append(rows, row)
		}
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].Kind != rows[j].Kind {
			return rows[i].Kind == "manual"
		}
		if rows[i].AccountID != rows[j].AccountID {
			return rows[i].AccountID < rows[j].AccountID
		}
		return rows[i].Site < rows[j].Site
	})
	return rows
}

type monitorAccountLimit struct {
	jobs.AccountLimit
	AccountLabel string `json:"account_label"`
}

func currentMonitorLimits(limits []jobs.AccountLimit, accounts []*sessions.Account) []monitorAccountLimit {
	labels := map[string]string{}
	for _, a := range accounts {
		if a == nil || !a.Enabled {
			continue
		}
		label := "账号"
		if len(a.Phone) >= 4 {
			label = "手机尾号 " + a.Phone[len(a.Phone)-4:]
		}
		labels[a.ID] = label
	}
	rows := []monitorAccountLimit{}
	for _, limit := range limits {
		if label, exists := labels[limit.AccountID]; exists {
			rows = append(rows, monitorAccountLimit{AccountLimit: limit, AccountLabel: label})
		}
	}
	return rows
}

func (s *Server) apiMonitorActions(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	if s.sess == nil || s.sms == nil {
		writeJSON(w, 503, map[string]any{"detail": "当前待处理状态暂不可用"})
		return
	}
	now := float64(time.Now().UnixNano()) / 1e9
	accounts := s.sess.List()
	rows := currentMonitorActions(accounts, s.sms.Recent(0), now)
	for i := range rows {
		rows[i].HandoffStage, rows[i].HandoffUpdatedAt = s.monitorHandoff(rows[i].AccountID, rows[i].Site, rows[i].SentAt)
	}
	rows = s.appendInterruptedHandoffActions(rows, accounts)
	limits := []monitorAccountLimit{}
	if s.jobs != nil {
		limits = currentMonitorLimits(s.jobs.AccountLimits(), accounts)
	}
	for i := range rows {
		switch rows[i].HandoffStage {
		case "submission_unknown", "submit_failed", "expired", "target_changed", "target_missing", "target_or_generation_changed", "send_unconfirmed":
			rows[i].Kind = "manual"
			rows[i].Reason = "短信自动接续未完成，需要检查原页面"
			rows[i].NextAction = "查看接续阶段与原页面，确认登录结果后再操作"
		}
		if s.sess.InspectExistingForRecovery(rows[i].Site, rows[i].AccountID) == sessions.ExistingRecoveryBusy {
			rows[i].Busy = true
			rows[i].Kind = "automatic"
			rows[i].Reason = "原页面正由任务或自动恢复占用"
			rows[i].NextAction = "等待自动处理结束；页面释放后仍需处理的项目会进入人工清单"
		}
	}
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].AccountID+"/"+rows[i].Site < rows[j].AccountID+"/"+rows[j].Site })
	manual, automatic := 0, 0
	for _, row := range rows {
		if row.Kind == "manual" {
			manual++
		} else {
			automatic++
		}
	}
	writeJSON(w, 200, map[string]any{"updated_at": now, "manual": manual, "automatic": automatic, "rows": rows, "account_limits": limits, "note": fmt.Sprintf("按当前启用账号的平台状态去重；共 %d 项，不受历史统计日期影响", len(rows))})
}
