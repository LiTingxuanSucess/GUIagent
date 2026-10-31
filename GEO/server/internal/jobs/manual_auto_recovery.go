package jobs

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"geo-server/internal/sessions"
)

func (q *Queue) notifyManualRecovery(job, site, stage, code, call, detail string) {
	config := os.Getenv("GEO_SAMPLING_NOTIFY_CONFIG")
	if config == "" {
		return
	}
	event, _ := json.Marshal(map[string]string{"job_id": job, "site": site, "stage": stage, "code": code, "call_id": call, "detail": detail})
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		python := q.Python
		if python == "" {
			python = "python3"
		}
		cmd := exec.CommandContext(ctx, python, filepath.Join(q.GeoRoot, "server", "scripts", "sampling_alert.py"), "--config", config, "--event")
		cmd.Stdin = bytes.NewReader(event)
		_ = cmd.Run()
	}()
}

func manualAutoRecoveryEnabled() bool { return os.Getenv("GEO_MANUAL_AUTO_RECOVERY") == "1" }

func manualBusyRetry(site, code string) bool {
	return manualAutoRecoveryEnabled() && code == "platform_busy" && (site == "kimi" || site == "doubao")
}

func (q *Queue) manualScrape(parent context.Context, execution webRecoveryExecution, cdp, site string, job *Job, lease *sessions.Lease) (map[string]any, error) {
	attempt := func(action string) (map[string]any, error) {
		window := time.Duration(envInt("GEO_MANUAL_ROUND_SEC", 180, 15, 600)) * time.Second
		ctx, cancel := context.WithTimeout(parent, window)
		defer cancel()
		execution.Action = action
		return q.scrapeForContext(withWebRecovery(ctx, execution), cdp, internalSiteID(site), job.Prompt, job.ID, lease.Phone, window)
	}
	result, err := attempt(execution.Action)
	if err == nil || !manualAutoRecoveryEnabled() || parent.Err() != nil {
		return result, err
	}
	code, _, _ := publicScrapeError(err)
	if manualBusyRetry(site, code) {
		q.changeManual(job.ID, site, func(s *ManualState) {
			s.Reason = "平台繁忙，正在刷新原页面并核对发送状态"
			s.RefreshAttempts++
		})
		q.notifyManualRecovery(job.ID, site, "刷新重试", code, "", "只在明确的平台拒绝后重试；已有回答继续采集，不重复发送状态未明的问题。")
		result, err = attempt("manual-refresh")
		if err == nil {
			return result, nil
		}
		code, _, _ = publicScrapeError(err)
	}
	if code != "platform_risk_control" && code != "platform_auth_unavailable" && code != "platform_setting_unavailable" && code != "recovery_unconfirmed" && code != "generation_timeout" {
		return result, err
	}
	q.changeManual(job.ID, site, func(s *ManualState) {
		s.Reason = "正在复核原页面，必要时调用 Cursor CLI 辅助恢复"
		s.AssistStatus = "running"
	})
	q.notifyManualRecovery(job.ID, site, "发现验证，开始 Cursor 辅助", code, "", "Cursor 只处理原账号、原页面的验证；完成后由服务端核对原问题并继续采集。")
	ctx, cancel := context.WithTimeout(parent, 5*time.Minute)
	defer cancel()
	result, recoveredErr, meta := q.recoverWebAttempt(withWebRecovery(ctx, execution), lease, cdp, site, job.Prompt, job.ID, 1, false, err)
	call := stringField(meta, "cli_call_id")
	status := stringField(meta, "status")
	recoveryCode := stringField(meta, "error_code")
	q.changeManual(job.ID, site, func(s *ManualState) { s.AssistCallID = call; s.AssistStatus = status })
	q.notifyManualRecovery(job.ID, site, "Cursor 辅助结束："+status, recoveryCode, call, "网页恢复与回答采集分别核验；未通过时保留原页面等待处理，其余平台继续运行。")
	return result, recoveredErr
}
