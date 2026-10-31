package jobs

import (
	"fmt"

	"geo-server/internal/sessions"
)

type OperatorLoginResult struct {
	State    *sessions.State
	CLIUsed  bool
	CLIError string
}

// CheckAccountLogin shares the actual browser/CLI budgets with customer work.
// Admission is immediate: an operator sweep must not queue behind, or take an
// occupied account away from, a customer request.
func (q *Queue) CheckAccountLogin(pool *sessions.Pool, account, site string, onCLI func()) (OperatorLoginResult, error) {
	var result OperatorLoginResult
	q.browserMu.Lock()
	if len(q.browserWaiters) > 0 || !tryBrowserSlot(q.siteSem) {
		q.browserMu.Unlock()
		return result, sessions.ErrOperatorLoginBusy
	}
	q.browserMu.Unlock()
	defer func() {
		<-q.siteSem
		q.browserMu.Lock()
		q.dispatchBrowsersLocked()
		q.browserMu.Unlock()
		q.wakeFairResourceWaiters()
	}()
	state, err := pool.CheckWithLoginAssist(account, site, func(cdp, target string) error {
		prepared, err := q.prepareManualPageAssist(account, site, cdp, target)
		if err != nil {
			if cursorProviderCooldown(err.Error()) > 0 || err.Error() == "cli_provider_probing" || err.Error() == "cli_health_unavailable" {
				result.CLIError = err.Error()
				return err
			}
			switch err {
			case ErrManualCLIBusy:
				result.CLIError = "cli_busy"
			case ErrManualCLIDisabled:
				result.CLIError = "cli_disabled"
			default:
				result.CLIError = "cli_unavailable"
			}
			return err
		}
		if onCLI != nil {
			onCLI()
		}
		call := q.runManualPageAssist(prepared.call, prepared.workspace, prepared.bin, prepared.instructions, cdp, target, func() {})
		result.CLIUsed = call.ProcessPID > 0
		result.CLIError = call.ErrorCode
		if call.Status != "success" && call.ErrorCode != "sms_required" {
			return fmt.Errorf("CLI page verification incomplete")
		}
		return nil
	})
	result.State = state
	return result, err
}
