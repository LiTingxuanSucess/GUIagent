package jobs

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"geo-server/internal/sessions"
)

type webRecoveryContextKey struct{}

type webRecoveryExecution struct {
	StatePath, AccountID, Action string
	SharedURL                    string
	PriorLoggedIn                bool
	RequireFreshAuth             bool
}

type recoveryAccountPool interface {
	AcquireForRecoveryContext(context.Context, string, map[string]bool) (*sessions.Lease, error)
	RecoveryAccountIDs(string) []string
}

func recoveryComponent(value string) string {
	if value != "" && !strings.ContainsAny(value, `/\\.`) {
		valid := true
		for _, c := range value {
			if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-') {
				valid = false
				break
			}
		}
		if valid {
			return value
		}
	}
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:12])
}

func (q *Queue) webRecoveryPath(jobID, site, accountID string) string {
	root := ""
	if q.DataDir != "" {
		root = filepath.Join(filepath.Dir(q.DataDir), "web-recovery")
	} else if q.GeoRoot != "" {
		root = filepath.Join(q.GeoRoot, ".web-recovery")
	}
	if root == "" {
		return ""
	}
	return filepath.Join(root, recoveryComponent(jobID), recoveryComponent(internalSiteID(site)), recoveryComponent(accountID)+".json")
}

// Final answers and evidence are already stored in the job and diagnostics.
// Journals are execution state, and cannot replay a terminal request.
func (q *Queue) cleanupWebRecovery(jobID string) {
	q.releaseManualReservations(jobID)
	q.releaseRecoveryTargets(jobID, "")
	path := q.webRecoveryPath(jobID, "site", "account")
	if path != "" {
		_ = os.RemoveAll(filepath.Dir(filepath.Dir(path)))
	}
}

func withWebRecovery(parent context.Context, execution webRecoveryExecution) context.Context {
	return context.WithValue(parent, webRecoveryContextKey{}, execution)
}

func recoveryEnv(parent context.Context, env []string) []string {
	env = append(env, "GEO_WEB_RECOVERY_FILE=", "GEO_WEB_RECOVERY_ACCOUNT_ID=", "GEO_WEB_RECOVERY_PREPARE=0", "GEO_WEB_RECOVERY_PRIOR_LOGGED_IN=0", "GEO_WEB_RECOVERY_REQUIRE_FRESH_AUTH=0")
	execution, ok := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
	if !ok || execution.StatePath == "" {
		return env
	}
	env = append(env, "GEO_WEB_RECOVERY_FILE="+execution.StatePath, "GEO_WEB_RECOVERY_ACCOUNT_ID="+execution.AccountID)
	if execution.PriorLoggedIn {
		env = append(env, "GEO_WEB_RECOVERY_PRIOR_LOGGED_IN=1")
	} else {
		env = append(env, "GEO_WEB_RECOVERY_PRIOR_LOGGED_IN=0")
	}
	if execution.RequireFreshAuth {
		env = append(env, "GEO_WEB_RECOVERY_REQUIRE_FRESH_AUTH=1")
	}
	return env
}

func (q *Queue) setWebRecoveryPhase(jobID, site, accountID, phase, callID, code string) {
	site = internalSiteID(site)
	q.patchIf(jobID, func(j *Job) bool {
		if j.Status != "running" && !(q.dispatchSem != nil && q.activeRuns[jobID] && j.Status == "queued") {
			return false
		}
		next := make(map[string]map[string]any, len(j.RecoveryStates)+1)
		for key, value := range j.RecoveryStates {
			next[key] = value
		}
		next[site] = map[string]any{"phase": phase, "account_id": accountID, "cli_call_id": callID, "error_code": code, "updated_at": time.Now().Format(time.RFC3339Nano)}
		j.RecoveryStates = next
		return true
	})
}

func (q *Queue) recoveryAccountIDs(site string) []string {
	if pool, ok := q.Sess.(recoveryAccountPool); ok {
		return q.withoutBlockedAccounts(site, pool.RecoveryAccountIDs(site))
	}
	return nil
}

func (q *Queue) acquireRecoveryAccount(ctx context.Context, site string, tried map[string]bool) (*sessions.Lease, error) {
	if pool, ok := q.Sess.(recoveryAccountPool); ok {
		return pool.AcquireForRecoveryContext(ctx, site, tried)
	}
	return nil, fmt.Errorf("没有待恢复的网页账号")
}

func recoveryFailure(code string) error {
	return &scrapeFailure{code: code, raw: "网页会话恢复未完成，已保留本次问题及页面状态", retryable: false}
}

// Probes are read-only and are not counted as new webpage requests. The Python
// verifier binds the journal to the exact account, question and existing target.
func (q *Queue) runWebRecoveryProbe(parent context.Context, cdp, site, prompt, jobID, phone string, prepare bool) map[string]any {
	ctx, cancel := context.WithTimeout(parent, time.Duration(envInt("GEO_WEB_RECOVERY_PROBE_SEC", 30, 1, 60))*time.Second)
	defer cancel()
	execution, _ := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
	if execution.StatePath == "" {
		return map[string]any{"error_code": "recovery_state_missing"}
	}
	if err := os.MkdirAll(filepath.Dir(execution.StatePath), 0700); err != nil {
		return map[string]any{"error_code": "recovery_state_unavailable"}
	}
	cmd := exec.CommandContext(ctx, q.Python, q.Script, "--cdp-url", cdp, "--site", site, "--prompt", prompt,
		"--question-id", "job_"+jobID, "--phone", phone, "--recovery-action", "probe", "--recovery-file", execution.StatePath)
	deadline, _ := ctx.Deadline()
	cmd.Env = recoveryEnv(parent, append(os.Environ(), fmt.Sprintf("GEO_DIAGNOSTIC_DEADLINE_MS=%d", deadline.UnixMilli())))
	if prepare {
		cmd.Env = append(cmd.Env, "GEO_WEB_RECOVERY_PREPARE=1")
	}
	cmd.Dir = q.GeoRoot
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.WaitDelay = 2 * time.Second
	cmd.Cancel = func() error {
		if cmd.Process != nil {
			return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		return nil
	}
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	// The private journal contains the original question; never expose probe
	// stderr or raw exception text as dashboard metadata.
	_ = cmd.Run()
	if ctx.Err() != nil {
		return map[string]any{"error_code": "recovery_probe_timeout"}
	}
	var result map[string]any
	if json.Unmarshal(lastJSON(stdout.Bytes()), &result) != nil {
		return map[string]any{"error_code": "recovery_probe_failed"}
	}
	if stringField(result, "recovery_action") != "probe" {
		if stringField(result, "error_code") == "" {
			return map[string]any{"error_code": "recovery_probe_failed"}
		}
		result["recovery_action"] = "probe"
		if _, ok := result["context_valid"]; !ok {
			result["context_valid"] = false
		}
		if _, ok := result["ready"]; !ok {
			result["ready"] = false
		}
	}
	return result
}

func webRecoveryMayResume(code string) bool {
	switch code {
	case "platform_risk_control", "platform_auth_unavailable", "platform_setting_unavailable", "send_timeout", "generation_timeout", "answer_extract_timeout", "browser_unresponsive", "site_timeout", "browser_start_failed":
		return true
	}
	return false
}

func pendingWebSend(parent context.Context) bool {
	execution, ok := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
	if !ok || execution.StatePath == "" {
		return false
	}
	raw, err := os.ReadFile(execution.StatePath)
	if err != nil {
		return false
	}
	var journal struct {
		SendState string `json:"send_state"`
	}
	return json.Unmarshal(raw, &journal) == nil && (journal.SendState == "sending" || journal.SendState == "sent")
}

// Await an already-issued code through the existing SMS callback. The CLI is
// stopped, and each probe drains the narrow callback channel before reading.
func (q *Queue) waitWebRecoverySMS(parent context.Context, lease *sessions.Lease, cdp, site, prompt, jobID, callID string) map[string]any {
	ctx, cancel := context.WithTimeout(parent, time.Duration(envInt("GEO_WEB_RECOVERY_SMS_WAIT_SEC", 120, 1, 300))*time.Second)
	defer cancel()
	lease.MarkRecoveryWaitingSMS()
	q.setWebRecoveryPhase(jobID, site, lease.AccountID, "waiting_sms", callID, "")
	q.extendJobDeadline(jobID, time.Now().Add(5*time.Minute+siteAttemptTimeout()+apiTimeout(site)))
	for {
		unavailable := false
		waitErr := withIdleBrowser(ctx, func() error {
			var admit func() bool
			if permit := browserPermitFrom(ctx); permit != nil {
				admit = func() bool { return permit.resume(ctx) }
			}
			closeSMS, err := lease.AllowRecoverySMSWithAdmission(admit)
			if err != nil {
				unavailable = true
				return err
			}
			defer closeSMS()
			timer := time.NewTimer(2 * time.Second)
			defer timer.Stop()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-timer.C:
				return nil
			}
		})
		if unavailable {
			return map[string]any{"error_code": "recovery_sms_unavailable"}
		}
		if waitErr != nil {
			return map[string]any{"error_code": "sms_timeout"}
		}
		probe := q.runWebRecoveryProbe(ctx, cdp, internalSiteID(site), prompt, jobID, lease.Phone, false)
		if probe["context_valid"] != true || probe["waiting_sms"] != true {
			return probe
		}
	}
}

// Keep the exact account lease throughout CLI, independent verification and
// resume. Python owns the send journal; the CLI never submits a new question.
func (q *Queue) recoverWebAttempt(parent context.Context, lease *sessions.Lease, cdp, site, prompt, jobID string, siteCount int, prepare bool, original error) (one map[string]any, err error, metadata map[string]any) {
	started := time.Now()
	metadata = map[string]any{"status": "skipped", "attempts": 0, "verified": false, "resumed": false, "cli_assisted": false, "elapsed_ms": int64(0)}
	defer func() {
		metadata["elapsed_ms"] = time.Since(started).Milliseconds()
		if parent.Err() != nil {
			metadata["status"], metadata["error_code"] = "cancelled", "request_cancelled"
		}
	}()
	if parent.Err() != nil {
		metadata["status"] = "cancelled"
		return nil, original, metadata
	}
	code, _, _ := publicScrapeError(original)
	if code == "platform_auth_unavailable" || code == "platform_risk_control" || prepare {
		execution, _ := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
		execution.RequireFreshAuth = true
		parent = withWebRecovery(parent, execution)
	}
	if !prepare {
		execution, _ := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
		if _, stateErr := os.Stat(execution.StatePath); stateErr != nil {
			metadata["error_code"] = "recovery_state_missing"
			return nil, original, metadata
		}
	}
	if q.dispatchSem != nil && pendingWebSend(parent) {
		if !q.reserveRecoveryTarget(site, lease.AccountID, jobID) {
			metadata["error_code"] = "recovery_target_owned"
			return nil, recoveryFailure("recovery_target_owned"), metadata
		}
		lease.MarkRecoveryPending()
		lease.Manager.KeepWarmFor(time.Duration(envInt("GEO_RECOVERY_PAGE_RETAIN_SEC", 360, 60, 900)) * time.Second)
		defer func() {
			if err != nil {
				lease.MarkRecoveryPending()
			}
		}()
	}
	if q.recoverySem != nil {
		select {
		case q.recoverySem <- struct{}{}:
			defer func() { <-q.recoverySem }()
		default:
			metadata["status"], metadata["error_code"] = "queued", "recovery_queue_busy"
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "queued_recovery", "", "recovery_queue_busy")
			return nil, recoveryFailure("recovery_queue_busy"), metadata
		}
	}
	guard, guardErr := lease.BeginWebRecovery()
	if guardErr != nil {
		metadata["error_code"] = "recovery_account_busy"
		return nil, original, metadata
	}
	defer guard()
	// The held site lease protects this page from idle cleanup throughout
	// probe/CLI/resume. Failed probes must not extend a profile's idle lifetime;
	// explicit operator warmth and semantic-challenge warmth stay independent.
	q.setWebRecoveryPhase(jobID, site, lease.AccountID, "verifying", "", "")
	probe := q.runWebRecoveryProbe(parent, cdp, internalSiteID(site), prompt, jobID, lease.Phone, prepare)
	if prepare && (probe["context_valid"] != true || stringField(probe, "target_id") == "") && lease.SiteTargetID() == "" && lease.Manager != nil {
		// Nothing was sent yet. An account without a tab for this site would
		// otherwise fail preparation in two seconds on every job, forever.
		if lease.Manager.EnsurePageContext(parent, internalSiteID(site)) == nil {
			probe = q.runWebRecoveryProbe(parent, cdp, internalSiteID(site), prompt, jobID, lease.Phone, prepare)
		}
	}
	if prepare && (probe["context_valid"] != true || stringField(probe, "target_id") == "") {
		if id := lease.SiteTargetID(); id != "" {
			probe["target_id"] = id
			probe["context_valid"] = true
			probe["ready"] = false
			probe["logged_out"] = true
			// The tab was just created. This is not an observed login page,
			// so do not spend a CLI turn before the first real snapshot.
			probe["synthesized_login"] = true
		}
	}
	if probe["context_valid"] != true || stringField(probe, "target_id") == "" {
		metadata["error_code"] = stringField(probe, "error_code")
		if metadata["error_code"] == "" {
			metadata["error_code"] = "recovery_context_unconfirmed"
		}
		q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", "", stringField(metadata, "error_code"))
		return nil, original, metadata
	}
	callID := ""
	originalTarget := stringField(probe, "target_id")
	if probe["waiting_sms"] == true {
		probe = q.waitWebRecoverySMS(parent, lease, cdp, site, prompt, jobID, "")
		if stringField(probe, "error_code") == "sms_timeout" {
			metadata["status"], metadata["error_code"] = "failed", "sms_timeout"
			metadata["auth_unavailable_proven"] = true
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", "", "sms_timeout")
			return nil, recoveryFailure("sms_timeout"), metadata
		}
	}
	if probe["context_valid"] != true || stringField(probe, "target_id") != originalTarget {
		metadata["status"], metadata["error_code"] = "failed", "recovery_context_unconfirmed"
		q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", "", "recovery_context_unconfirmed")
		return nil, original, metadata
	}
	failureKey := recoveryFailureKey(jobID, site, lease.AccountID, originalTarget)
	previousFailure, recentFailure := q.recentRecoveryFailure(failureKey, probe, time.Now())
	if probe["ready"] != true {
		if recentFailure {
			if previousFailure.attempts >= 2 {
				metadata["status"], metadata["error_code"] = "failed", "no_progress"
				metadata["auth_unavailable_proven"] = true
				return nil, recoveryFailure("no_progress"), metadata
			}
			metadata["status"], metadata["error_code"] = "queued", "recovery_queue_busy"
			metadata["last_cli_call_id"] = previousFailure.callID
			// Re-probe soon so an externally repaired page can resume without
			// waiting for the expensive CLI retry cooldown to expire.
			metadata["retry_after_sec"] = min(envInt("GEO_RECOVERY_RETRY_SEC", 10, 2, 60), max(1, int(time.Until(previousFailure.expires).Seconds())+1))
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "queued_recovery", "", "recovery_queue_busy")
			return nil, recoveryFailure("recovery_queue_busy"), metadata
		}
		if probe["challenge_visible"] != true && code != "platform_risk_control" && code != "platform_auth_unavailable" && code != "platform_setting_unavailable" {
			metadata["status"], metadata["error_code"] = "failed", "recovery_unconfirmed"
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", "", "recovery_unconfirmed")
			return nil, original, metadata
		}
		execution, _ := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
		q.extendJobDeadline(jobID, time.Now().Add(15*time.Minute+siteAttemptTimeout()+apiTimeout(site)))
		var result cursorRecoveryResult
		if probe["synthesized_login"] == true {
			// No observed page yet. The server SMS path can open it.
			result = cursorRecoveryResult{Status: "failed", ErrorCode: "sms_required"}
		} else {
			// A real logged-out page is a login task for the CLI: it opens the
			// SMS form and clicks send. The code itself still arrives through
			// the existing server callback.
			// Login can itself be covered by a captcha. Retain login intent so
			// clearing that challenge can continue to the SMS hand-off.
			login := probe["logged_out"] == true
			reason := code
			if login {
				reason = "logged_out"
			}
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "waiting_cli", "", "")
			result = q.runCursorRecovery(parent, cursorRecoveryRequest{AccountID: lease.AccountID, Phone: lease.Phone, Site: internalSiteID(site), JobID: jobID, CDP: cdp, TargetID: stringField(probe, "target_id"), StatePath: execution.StatePath, Reason: reason, Login: login})
		}
		callID = result.CallID
		metadata["cli_call_id"] = callID
		if result.Status != "skipped" {
			metadata["attempts"] = 1
		}
		q.setWebRecoveryPhase(jobID, site, lease.AccountID, "verifying", callID, "")
		if parent.Err() != nil {
			metadata["status"], metadata["error_code"] = "cancelled", "request_cancelled"
			q.markCursorRecovery(callID, false, false, "completed", map[string]any{"reason": "request_cancelled", "error_code": "request_cancelled"})
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", callID, "request_cancelled")
			return nil, original, metadata
		}
		execution.RequireFreshAuth = true
		parent = withWebRecovery(parent, execution)
		probe = q.runWebRecoveryProbe(parent, cdp, internalSiteID(site), prompt, jobID, lease.Phone, false)
		if result.ErrorCode == "sms_required" && probe["waiting_sms"] != true && probe["context_valid"] == true && stringField(probe, "target_id") == originalTarget && lease != nil && lease.Manager != nil {
			// The page is a login that the CLI must not submit. Use the
			// existing server send once, then wait for the callback.
			if closeSMS, smsErr := lease.AllowRecoverySMS(); smsErr == nil {
				_, _ = lease.Manager.SendCodeOnExistingPage(parent, internalSiteID(site), originalTarget)
				closeSMS()
				probe = q.runWebRecoveryProbe(parent, cdp, internalSiteID(site), prompt, jobID, lease.Phone, false)
			}
		}
		// Sending from a prepared form can reveal a new challenge. Resolve it
		// once on this held target; never send another SMS or another question.
		if result.ErrorCode == "sms_required" && probe["challenge_visible"] == true && probe["context_valid"] == true && stringField(probe, "target_id") == originalTarget && parent.Err() == nil {
			q.markCursorRecovery(callID, false, false, "completed", probe)
			result = q.runCursorRecovery(parent, cursorRecoveryRequest{AccountID: lease.AccountID, Phone: lease.Phone, Site: internalSiteID(site), JobID: jobID, CDP: cdp, TargetID: originalTarget, StatePath: execution.StatePath, Reason: "challenge_after_sms_send"})
			callID = result.CallID
			metadata["cli_call_id"] = callID
			if result.Status != "skipped" {
				metadata["attempts"] = evidenceCount(metadata["attempts"]) + 1
			}
			probe = q.runWebRecoveryProbe(parent, cdp, internalSiteID(site), prompt, jobID, lease.Phone, false)
		}
		if probe["waiting_sms"] == true || result.ErrorCode == "sms_required" {
			probe = q.waitWebRecoverySMS(parent, lease, cdp, site, prompt, jobID, callID)
		}
		if stringField(probe, "error_code") == "sms_timeout" {
			metadata["status"], metadata["error_code"] = "failed", "sms_timeout"
			metadata["auth_unavailable_proven"] = true
			q.markCursorRecovery(callID, false, false, "completed", probe)
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", callID, "sms_timeout")
			return nil, recoveryFailure("sms_timeout"), metadata
		}
		probe["target_match"] = stringField(probe, "target_id") == originalTarget
		if probe["context_valid"] != true || probe["ready"] != true || probe["target_match"] != true {
			if parent.Err() == nil && probe["target_match"] == true {
				q.rememberRecoveryFailure(failureKey, probe, result, time.Now())
			}
			metadata["status"], metadata["error_code"] = "failed", "cli_verification_failed"
			switch result.ErrorCode {
			case "sms_required":
				metadata["error_code"] = "sms_required"
				metadata["auth_unavailable_proven"] = true
			case "platform_quota", "no_progress", "target_missing":
				// Keep the CLI's own stop. A muted or stuck page used to be
				// rewritten as a generic verification miss and called again.
				metadata["error_code"] = result.ErrorCode
			}
			// An actual CLI attempt plus an independent check of the same
			// original page proves failure. A busy/skipped CLI, missing page,
			// changed target or cancelled probe proves no such exhaustion.
			if result.Status != "skipped" && parent.Err() == nil && probe["context_valid"] == true && probe["target_match"] == true &&
				(probe["logged_out"] == true || probe["challenge_visible"] == true || probe["waiting_sms"] == true || stringField(probe, "error_code") == "platform_auth_unavailable") {
				metadata["auth_unavailable_proven"] = true
			}
			if result.Status == "skipped" {
				metadata["status"], metadata["error_code"] = "skipped", result.ErrorCode
			}
			if stringField(probe, "reason") == "" && probe["target_match"] != true {
				probe["reason"] = "target_changed"
			}
			q.markCursorRecovery(callID, false, false, "completed", probe)
			q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", callID, stringField(metadata, "error_code"))
			switch result.ErrorCode {
			case "platform_quota", "no_progress", "target_missing":
				return nil, recoveryFailure(result.ErrorCode), metadata
			}
			return nil, original, metadata
		}
		// Challenge teardown is asynchronous. Require a fresh, stable ready
		// snapshot before resuming this exact original send journal.
		select {
		case <-parent.Done():
			return nil, original, metadata
		case <-time.After(750 * time.Millisecond):
		}
		stable := q.runWebRecoveryProbe(parent, cdp, internalSiteID(site), prompt, jobID, lease.Phone, false)
		if stable["ready"] != true || stable["context_valid"] != true || stringField(stable, "target_id") != originalTarget {
			q.markCursorRecovery(callID, false, false, "completed", stable)
			metadata["status"], metadata["error_code"] = "failed", "recovery_unstable"
			return nil, recoveryFailure("recovery_unstable"), metadata
		}
		q.markCursorRecovery(callID, true, false, "verify", stable)
	}
	metadata["verified"] = true
	if q.dispatchSem == nil || !pendingWebSend(parent) {
		lease.MarkAvailable("网页会话恢复并通过独立复核")
	}
	q.setWebRecoveryPhase(jobID, site, lease.AccountID, "resuming", callID, "")
	execution, _ := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
	execution.Action = "resume"
	resumeCtx := withWebRecovery(parent, execution)
	deadline := q.webpageDeadline(resumeCtx, internalSiteID(site), jobID, siteCount)
	one, err = q.scrapeForContext(resumeCtx, cdp, internalSiteID(site), prompt, jobID, lease.Phone, time.Until(deadline))
	if err == nil && one["ok"] == true {
		lease.MarkAvailable("原网页回答已完成并通过独立复核")
		q.clearAccountBlock(site, lease.AccountID)
		q.releaseRecoveryTargets(jobID, site)
		metadata["status"], metadata["resumed"] = "succeeded", true
		q.markCursorRecovery(callID, true, true, "completed")
		metadata["cli_assisted"] = q.cursorAssisted(callID)
		q.setWebRecoveryPhase(jobID, site, lease.AccountID, "succeeded", callID, "")
		return one, nil, metadata
	}
	metadata["status"], metadata["error_code"] = "failed", "web_resume_failed"
	if parent.Err() != nil {
		metadata["status"], metadata["error_code"] = "cancelled", "request_cancelled"
	}
	resumeCode := "recovery_unconfirmed"
	if err != nil {
		resumeCode, _, _ = publicScrapeError(err)
	}
	metadata["resume_error_code"] = resumeCode
	q.markCursorRecovery(callID, true, false, "completed", map[string]any{"reason": stringField(metadata, "error_code"), "error_code": resumeCode, "ready": true})
	q.setWebRecoveryPhase(jobID, site, lease.AccountID, "failed", callID, stringField(metadata, "error_code"))
	if err == nil {
		err = recoveryFailure("recovery_unconfirmed")
	}
	return nil, err, metadata
}

func mergeWebRecovery(previous, latest map[string]any) map[string]any {
	if latest == nil {
		return previous
	}
	if previous == nil {
		return latest
	}
	for _, key := range []string{"attempts", "elapsed_ms"} {
		value := func(m map[string]any) int64 {
			switch n := m[key].(type) {
			case int:
				return int64(n)
			case int64:
				return n
			case float64:
				return int64(n)
			}
			return 0
		}
		latest[key] = value(previous) + value(latest)
	}
	return latest
}
