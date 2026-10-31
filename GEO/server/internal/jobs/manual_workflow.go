package jobs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"time"

	"geo-server/internal/sessions"
)

type ManualState struct {
	Site                 string           `json:"site"`
	Status               string           `json:"status"`
	AccountID            string           `json:"account_id,omitempty"`
	AccountLabel         string           `json:"account_label,omitempty"`
	TargetID             string           `json:"target_id,omitempty"`
	ConversationURL      string           `json:"conversation_url,omitempty"`
	SendState            string           `json:"send_state,omitempty"`
	ErrorCode            string           `json:"error_code,omitempty"`
	Reason               string           `json:"reason,omitempty"`
	Action               string           `json:"action,omitempty"`
	Revision             uint64           `json:"revision"`
	UpdatedAt            string           `json:"updated_at,omitempty"`
	ExpiresAt            string           `json:"expires_at,omitempty"`
	AutoRetryAt          string           `json:"auto_retry_at,omitempty"`
	RefreshAttempts      int              `json:"refresh_attempts,omitempty"`
	AssistStatus         string           `json:"assist_status,omitempty"`
	AssistCallID         string           `json:"assist_call_id,omitempty"`
	TurnHistory          []map[string]any `json:"turn_history,omitempty"`
	AutoRecoveryAttempts int              `json:"auto_recovery_attempts,omitempty"`
	AutoRecoveryKey      string           `json:"auto_recovery_key,omitempty"`
	AutoRecoveryAt       string           `json:"auto_recovery_at,omitempty"`
	SharedURL            string           `json:"shared_url,omitempty"`
}

type workflowPool interface {
	ReserveWorkflow(string, string, string) (*sessions.Account, error)
	AcquireWorkflow(context.Context, string, string, string, string) (*sessions.Lease, error)
	ReleaseWorkflow(string)
	FocusWorkflow(context.Context, string, string, string, string) error
}

var ErrManualConflict = errors.New("任务状态已变化，请刷新后再操作")
var ErrManualNotFound = errors.New("没有这条人工任务")

func manualOwner(job, site string) string { return job + "/" + internalSiteID(site) }
func manualWaiting(status string) bool {
	return status == "waiting_manual" || status == "operator_active"
}

func (q *Queue) restoreManualReservations() {
	pool, ok := q.Sess.(workflowPool)
	if !ok {
		return
	}
	for _, job := range q.List() {
		if !job.ManualWorkflow || job.Status == "succeeded" || job.Status == "cancelled" {
			continue
		}
		for site, state := range job.ManualStates {
			if state.AccountID != "" && state.Status != "completed" {
				_, _ = pool.ReserveWorkflow(manualOwner(job.ID, site), internalSiteID(site), state.AccountID)
			}
		}
	}
}

func (q *Queue) releaseManualReservations(id string) {
	pool, ok := q.Sess.(workflowPool)
	if !ok {
		return
	}
	job, ok := q.Get(id)
	if !ok || !job.ManualWorkflow {
		return
	}
	for _, site := range job.Sites {
		pool.ReleaseWorkflow(manualOwner(id, site))
	}
}

func recoverManualJob(job *Job) {
	if job.Status == "succeeded" || job.Status == "cancelled" || job.Status == "failed" {
		return
	}
	waiting := false
	for site, state := range job.ManualStates {
		if state.Status == "automatic" || state.Status == "collecting" || state.Status == "resume_queued" {
			state.Status = "waiting_manual"
			state.Action = ""
			state.ErrorCode = "service_interrupted"
			state.Reason = "服务已重启，请检查原页面后继续；未重新提交问题"
			state.Revision++
			state.UpdatedAt = time.Now().Format(time.RFC3339Nano)
			job.ManualStates[site] = state
		}
		if manualWaiting(state.Status) {
			waiting = true
		}
	}
	if waiting {
		job.Status = "waiting_manual"
		job.WaitReason = "awaiting_manual"
	} else {
		job.Status = "queued"
	}
	job.DeadlineAt = ""
	job.FinishedAt = ""
	job.CallbackStatus = ""
	job.DeliveryPolicy = ""
	job.DeadlinePolicy = "manual_web"
}

func (q *Queue) changeManual(id, site string, fn func(*ManualState)) {
	q.patch(id, func(job *Job) {
		job.ManualStates = copyJob(job).ManualStates
		state := job.ManualStates[site]
		fn(&state)
		state.Revision++
		state.UpdatedAt = time.Now().Format(time.RFC3339Nano)
		job.ManualStates[site] = state
	})
}

func (q *Queue) manualPause(id, site, code string) {
	q.changeManual(id, site, func(s *ManualState) {
		s.Status = "waiting_manual"
		s.Action = ""
		s.ErrorCode = code
		s.Reason = manualReason(code)
		s.ExpiresAt = time.Now().Add(time.Duration(envInt("GEO_MANUAL_WAIT_MIN", 30, 1, 1440)) * time.Minute).Format(time.RFC3339Nano)
		q.readManualJournal(id, site, s)
		key := code + "/" + s.SendState + "/" + s.TargetID
		if key != s.AutoRecoveryKey {
			s.AutoRecoveryKey = key
			s.AutoRecoveryAttempts = 0
		}
		s.AutoRecoveryAt = time.Now().Add(manualRecoveryDelay(s.AutoRecoveryAttempts)).Format(time.RFC3339Nano)
		if manualAutoRecoveryEnabled() && manualSiteRecheckable(site, code) && s.AutoRecoveryAttempts < 3 {
			s.Reason += "；系统将自动复核原页面并续采，复核失败会退避，不会直接重发问题"
		}
	})
}

func manualReason(code string) string {
	switch code {
	case "platform_auth_unavailable":
		return "请在原浏览器完成登录，再确认问题是否需要手动提交"
	case "platform_risk_control":
		return "请在原页面完成安全验证；已提交的问题无需重复发送"
	case "platform_quota":
		return "平台提示额度或账号限制，请检查原页面并决定继续或取消"
	case "manual_send_unconfirmed":
		return "尚未确认这次问题已提交，请核对当前对话；代码未再次发送"
	case "manual_question_mismatch":
		return "当前对话或问题已变化，请回到本任务的原问题"
	case "recovery_page_missing":
		return "原标签页已不存在，可点击“新建平台标签页”找回本题，再核对并采集"
	case "browser_unresponsive":
		return "采集连接暂时不可用，原页面和问题保留；系统将复核后重连继续"
	case "no_account":
		return "号池没有启用账号，请添加或启用账号后继续"
	case "service_interrupted":
		return "服务已重启，请核对原页面后继续"
	default:
		return "网页执行中断，请检查原问题和回答，处理后继续采集"
	}
}

func (q *Queue) readManualJournal(id, site string, state *ManualState) {
	if state.AccountID == "" {
		return
	}
	raw, err := os.ReadFile(q.webRecoveryPath(id, site, state.AccountID))
	if err != nil {
		return
	}
	var j struct {
		SendState   string           `json:"send_state"`
		URL         string           `json:"page_url"`
		Target      string           `json:"target_id"`
		TurnHistory []map[string]any `json:"manual_turn_history"`
	}
	if json.Unmarshal(raw, &j) == nil {
		state.SendState = j.SendState
		state.ConversationURL = j.URL
		state.TurnHistory = j.TurnHistory
		if j.Target != "" {
			state.TargetID = j.Target
		}
	}
}

func (q *Queue) createManualJournal(job *Job, site string, state ManualState) error {
	path := q.webRecoveryPath(job.ID, site, state.AccountID)
	if path == "" {
		return errors.New("需要持久化目录才能启动人工流程")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(job.Prompt))
	value := map[string]any{"version": 1, "site_id": internalSiteID(site), "prompt": job.Prompt, "prompt_sha256": hex.EncodeToString(sum[:]), "question_id": "job_" + job.ID, "account_id": state.AccountID, "send_state": "before_send", "phase": "before_send", "target_id": state.TargetID, "page_url": "", "prior_logged_in": false, "manual_workflow": true}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if os.IsExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	defer f.Close()
	if err := json.NewEncoder(f).Encode(value); err != nil {
		return err
	}
	return f.Sync()
}

func (q *Queue) runManualWorkflow(ctx context.Context, job *Job, started time.Time, queuedMS int64) {
	for _, site := range job.Sites {
		if ctx.Err() != nil {
			return
		}
		current, ok := q.Get(job.ID)
		if !ok || current.Status == "cancelled" {
			return
		}
		state := current.ManualStates[site]
		if state.Status == "completed" || manualWaiting(state.Status) {
			continue
		}
		q.runManualSite(ctx, current, site, state)
	}
	final, ok := q.Get(job.ID)
	if !ok || final.Status == "cancelled" {
		return
	}
	waiting, pending := false, false
	for _, state := range final.ManualStates {
		if manualWaiting(state.Status) {
			waiting = true
		} else if state.Status != "completed" {
			pending = true
		}
	}
	q.patch(job.ID, func(j *Job) {
		j.QueuedMS = queuedMS
		j.ElapsedMS += time.Since(started).Milliseconds()
		j.TotalMS = time.Now().UnixMilli() - j.createdUnix
		j.DeadlineAt = ""
		switch {
		case waiting:
			j.Status = "waiting_manual"
			j.WaitReason = "awaiting_manual"
			j.RetryAt = ""
		case pending:
			j.Status = "queued"
			j.WaitReason = "account_busy"
			j.RetryAt = time.Now().Add(3 * time.Second).Format(time.RFC3339Nano)
			for _, state := range j.ManualStates {
				if next, err := time.Parse(time.RFC3339Nano, state.AutoRetryAt); err == nil && next.After(time.Now()) {
					j.RetryAt = state.AutoRetryAt
				}
			}
		default:
			j.Status = "succeeded"
			j.FinishedAt = time.Now().Format(time.RFC3339Nano)
			j.WaitReason = ""
			j.RetryAt = ""
			if j.CallbackURL != "" {
				j.CallbackStatus = "pending"
			}
		}
	})
	if !waiting && !pending {
		q.cleanupWebRecovery(job.ID)
		go q.callback(job.ID)
	}
}

func (q *Queue) runManualSite(parent context.Context, job *Job, site string, state ManualState) {
	if next, err := time.Parse(time.RFC3339Nano, state.AutoRetryAt); err == nil && time.Now().Before(next) {
		return
	}
	pool, ok := q.Sess.(workflowPool)
	if !ok {
		q.manualPause(job.ID, site, "no_account")
		return
	}
	owner := manualOwner(job.ID, site)
	account, err := pool.ReserveWorkflow(owner, internalSiteID(site), state.AccountID)
	if errors.Is(err, sessions.ErrWorkflowBusy) {
		return
	}
	if err != nil {
		q.manualPause(job.ID, site, "no_account")
		return
	}
	state.AccountID = account.ID
	state.AccountLabel = "手机尾号 " + account.Phone[len(account.Phone)-4:]
	if state.TargetID == "" && state.SendState == "" {
		state.Action = ""
	}
	if state.Action == "" {
		state.Status = "automatic"
	} else {
		state.Status = "collecting"
	}
	q.changeManual(job.ID, site, func(s *ManualState) { state.Revision = s.Revision; *s = state; s.ErrorCode = ""; s.Reason = "" })
	if latest, ok := q.Get(job.ID); !ok || q.saveChecked(latest) != nil {
		q.manualPause(job.ID, site, "state_save_failed")
		return
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(envInt("GEO_MANUAL_ROUND_SEC", 180, 15, 600))*time.Second)
	defer cancel()
	select {
	case q.siteSem <- struct{}{}:
		defer func() { <-q.siteSem }()
	case <-ctx.Done():
		q.manualPause(job.ID, site, "site_timeout")
		return
	}
	lease, err := pool.AcquireWorkflow(ctx, owner, account.ID, internalSiteID(site), state.TargetID)
	if err != nil {
		code := "browser_unresponsive"
		if errors.Is(err, sessions.ErrWorkflowPageMissing) {
			code = "recovery_page_missing"
		}
		if errors.Is(err, sessions.ErrWorkflowPageMismatch) {
			code = "manual_question_mismatch"
		}
		log.Printf("manual browser binding job=%s site=%s code=%s err=%v", job.ID, site, code, err)
		q.manualPause(job.ID, site, code)
		return
	}
	defer lease.Release()
	state.TargetID = lease.SiteTargetID()
	q.changeManual(job.ID, site, func(s *ManualState) { s.TargetID = state.TargetID })
	if err := q.createManualJournal(job, site, state); err != nil {
		q.manualPause(job.ID, site, "state_save_failed")
		return
	}
	action := "manual-auto"
	if state.Action != "" {
		action = "manual-" + state.Action
	}
	execution := webRecoveryExecution{StatePath: q.webRecoveryPath(job.ID, site, account.ID), AccountID: account.ID, Action: action, SharedURL: state.SharedURL}
	cdp, err := lease.CDPEndpoint()
	if err != nil {
		q.manualPause(job.ID, site, "browser_start_failed")
		return
	}
	result, err := q.manualScrape(parent, execution, cdp, site, job, lease)
	if err == nil && validDelivery(result) {
		lease.MarkAvailable("原网页回答已完成并通过核验")
		result["site_id"] = site
		result["source"] = "web"
		result["account_id"] = account.ID
		result["human_assisted"] = state.Action != ""
		q.patch(job.ID, func(j *Job) { j.Results = append(j.Results, result); j.CompletedSites = len(j.Results) })
		q.changeManual(job.ID, site, func(s *ManualState) {
			s.Status = "completed"
			s.Action = ""
			s.ErrorCode = ""
			s.Reason = ""
			s.ExpiresAt = ""
			q.readManualJournal(job.ID, site, s)
		})
		pool.ReleaseWorkflow(owner)
		return
	}
	code := "site_request_failed"
	if err != nil {
		code, _, _ = publicScrapeError(err)
	}
	if manualBusyRetry(site, code) {
		q.changeManual(job.ID, site, func(s *ManualState) {
			s.Status = "resume_queued"
			s.Action = "refresh"
			s.ErrorCode = code
			s.Reason = "平台仍繁忙，冷却后继续刷新重试；其他平台独立执行"
			s.AutoRetryAt = time.Now().Add(time.Duration(envInt("GEO_BUSY_RETRY_SEC", 60, 10, 600)) * time.Second).Format(time.RFC3339Nano)
			q.readManualJournal(job.ID, site, s)
		})
		return
	}
	// Save a fresh before-send baseline after navigation/login failures. This
	// command only reads the exact bound page and never sends a question.
	pauseCtx, stop := context.WithTimeout(parent, 10*time.Second)
	defer stop()
	execution.Action = "manual-pause"
	_, _ = q.scrapeForContext(withWebRecovery(pauseCtx, execution), cdp, internalSiteID(site), job.Prompt, job.ID, account.Phone, 10*time.Second)
	q.manualPause(job.ID, site, code)
}

type ManualTask struct {
	JobID     string `json:"job_id"`
	Prompt    string `json:"prompt"`
	JobStatus string `json:"job_status"`
	ManualState
}

func (q *Queue) ManualTasks() []ManualTask {
	out := []ManualTask{}
	for _, job := range q.List() {
		if !job.ManualWorkflow || job.Status == "cancelled" || job.Status == "succeeded" {
			continue
		}
		for _, state := range job.ManualStates {
			if state.Status != "completed" {
				out = append(out, ManualTask{job.ID, job.Prompt, job.Status, state})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UpdatedAt > out[j].UpdatedAt })
	return out
}

func (q *Queue) ManualAction(ctx context.Context, id, site, action string, revision uint64, shareURL ...string) (*Job, error) {
	return q.manualAction(ctx, id, site, action, revision, false, shareURL...)
}

func (q *Queue) manualAction(ctx context.Context, id, site, action string, revision uint64, automatic bool, shareURL ...string) (*Job, error) {
	q.manualMu.Lock()
	defer q.manualMu.Unlock()
	q.mu.Lock()
	job := q.jobs[id]
	if job == nil || !job.ManualWorkflow {
		q.mu.Unlock()
		return nil, ErrManualNotFound
	}
	state, exists := job.ManualStates[site]
	if !exists {
		q.mu.Unlock()
		return nil, ErrManualNotFound
	}
	if (action != "open" && state.Revision != revision) || !manualWaiting(state.Status) || q.activeRuns[id] || job.Status == "cancelled" {
		q.mu.Unlock()
		return nil, ErrManualConflict
	}
	if automatic && (state.Status != "waiting_manual" || state.AutoRecoveryAttempts >= 3) {
		q.mu.Unlock()
		return nil, ErrManualConflict
	}
	if action == "open" || action == "new-tab" {
		original := copyJob(job)
		q.mu.Unlock()
		if action == "new-tab" {
			creator, ok := q.Sess.(workflowPageCreator)
			if !ok || state.AccountID == "" {
				return nil, ErrManualConflict
			}
			return q.newManualPage(ctx, creator, original, site, state)
		}
		if opener, ok := q.Sess.(workflowPageOpener); ok && state.AccountID != "" {
			return q.openManualPage(ctx, opener, original, site, state)
		}
		pool, ok := q.Sess.(workflowPool)
		if !ok || state.AccountID == "" {
			return nil, ErrManualConflict
		}
		if err := pool.FocusWorkflow(ctx, manualOwner(id, site), state.AccountID, internalSiteID(site), state.TargetID); err != nil {
			return nil, err
		}
		out, _ := q.Get(id)
		return out, nil
	}
	if action != "claim" && action != "continue" && action != "submitted" && action != "refresh" && action != "restore" && action != "confirm-current" && action != "reconcile" && action != "share" {
		q.mu.Unlock()
		return nil, fmt.Errorf("未知人工操作")
	}
	if action == "share" {
		if site != "qianwen" || len(shareURL) != 1 || !verifiedPublicShareURL(site, shareURL[0]) {
			q.mu.Unlock()
			return nil, fmt.Errorf("需要本任务对应的千问公开分享地址")
		}
		state.SharedURL = shareURL[0]
	}
	previous := copyJob(job)
	next := copyJob(job)
	state.Revision++
	state.UpdatedAt = time.Now().Format(time.RFC3339Nano)
	if automatic {
		state.AutoRecoveryAttempts++
	} else {
		state.AutoRecoveryAttempts = 0
	}
	if action == "claim" {
		state.Status = "operator_active"
	} else {
		state.Status = "resume_queued"
		state.Action = action
		state.ErrorCode = ""
		state.Reason = ""
		next.Status = "queued"
		next.WaitReason = ""
		next.RetryAt = ""
	}
	next.ManualStates[site] = state
	next.UpdatedAt = state.UpdatedAt
	q.jobs[id] = next
	q.mu.Unlock()
	if err := q.saveChecked(next); err != nil {
		q.mu.Lock()
		if q.jobs[id] == next && next.Status != "cancelled" {
			q.jobs[id] = previous
		}
		q.mu.Unlock()
		q.publish(id)
		return nil, err
	}
	q.publish(id)
	if action != "claim" {
		q.enqueueJob(id, time.Now())
	}
	out, _ := q.Get(id)
	return out, nil
}
