package jobs

import (
	"context"
	"time"
)

func manualRecoveryDelay(attempts int) time.Duration {
	delay := time.Duration(envInt("GEO_MANUAL_RECHECK_SEC", 60, 15, 600)) * time.Second
	for i := 0; i < attempts && delay < 10*time.Minute; i++ {
		delay *= 3
	}
	return min(delay, 10*time.Minute)
}

func manualRecheckable(code string) bool {
	switch code {
	case "platform_risk_control", "platform_auth_unavailable", "platform_setting_unavailable", "recovery_unconfirmed", "generation_timeout", "service_interrupted", "manual_send_unconfirmed", "manual_question_mismatch", "browser_unresponsive":
		return true
	}
	return false
}

func manualSiteRecheckable(site, code string) bool {
	return manualRecheckable(code) || site == "qianwen" && code == "site_request_failed"
}

// Re-read and resume the original send only. An operator claim, changed
// question, missing target or quota restriction always remains manual.
func (q *Queue) scheduleManualRecoveries(now time.Time) int {
	if !manualAutoRecoveryEnabled() {
		return 0
	}
	health := q.CursorHealth()
	count := 0
	for _, task := range q.ManualTasks() {
		if task.Status != "waiting_manual" || !manualSiteRecheckable(task.Site, task.ErrorCode) || task.AutoRecoveryAttempts >= 3 {
			continue
		}
		due, err := time.Parse(time.RFC3339Nano, task.AutoRecoveryAt)
		if err != nil {
			updated, err := time.Parse(time.RFC3339Nano, task.UpdatedAt)
			if err != nil {
				continue
			}
			due = updated.Add(manualRecoveryDelay(task.AutoRecoveryAttempts))
		}
		if now.Before(due) || health.State == "blocked" && (task.ErrorCode == "platform_risk_control" || task.ErrorCode == "platform_auth_unavailable") {
			continue
		}
		if _, err := q.manualAction(context.Background(), task.JobID, task.Site, "reconcile", task.Revision, true); err == nil {
			count++
		}
	}
	return count
}
