package jobs

import (
	"context"
	"fmt"
	"time"

	"geo-server/internal/sessions"
)

// AssistLoginChallenge is called only for a captcha freshly observed by the
// idle pool's login check/send. It cannot create/navigate a browser, wait for a
// customer lease, select another account, or restart a question-time challenge.
func (q *Queue) AssistLoginChallenge(accountID, site string) error {
	if q.quotaBlocked(site, accountID) {
		return fmt.Errorf("账号额度或禁言限制尚未到复查时间")
	}
	if !cursorRecoveryEnabled() {
		return ErrManualCLIDisabled
	}
	pool, ok := q.Sess.(interface {
		TryAcquireExistingForRecoveryContext(context.Context, string, string) (*sessions.Lease, error)
	})
	if !ok {
		return fmt.Errorf("existing login page unavailable")
	}
	lease, err := pool.TryAcquireExistingForRecoveryContext(context.Background(), site, accountID)
	if err != nil {
		return err
	}
	defer lease.Release()
	guard, err := lease.BeginWebRecovery()
	if err != nil {
		return err
	}
	defer guard()
	cdp, err := lease.Manager.CDPEndpoint()
	if err != nil {
		return err
	}
	targetID := lease.SiteTargetID()
	if targetID == "" {
		return fmt.Errorf("existing login target unavailable")
	}
	prepared, err := q.prepareManualPageAssist(accountID, site, cdp, targetID)
	if err != nil {
		return err
	}
	call := prepared.call
	call.JobID = "login-check"
	call.TaskType = "login_check_assist"
	if err := call.save(prepared.workspace); err != nil {
		releaseCaptchaAgent(accountID)
		releaseCursorCapacity()
		return err
	}
	// The held lease keeps this page alive without the operator's ten-minute
	// retention. The existing SMS callback regains access after guard returns.
	q.runManualPageAssist(call, prepared.workspace, prepared.bin, prepared.instructions, cdp, targetID, func() {})
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	probe := q.cursorPageTool(ctx, prepared.workspace, site, cdp, targetID, "state")
	if probe["ok"] == true && probe["challenge_visible"] == false {
		if probe["waiting_sms"] == true {
			lease.MarkRecoveryWaitingSMS()
		} else if probe["logged_out"] == false && probe["account_marker"] == true && probe["editor_ready"] == true {
			lease.MarkAvailable("定时登录检测的验证层已清除并通过页面复核")
		}
	}
	return nil
}
