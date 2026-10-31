package jobs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"geo-server/internal/sessions"
)

type eventualDeliveryKey struct{}

func requireDelivery() bool { return envInt("GEO_REQUIRE_DELIVERY", 0, 0, 1) == 1 }

func isEventualDelivery(ctx context.Context) bool { return ctx.Value(eventualDeliveryKey{}) == true }

func validDelivery(result map[string]any) bool {
	return result != nil && result["ok"] == true && strings.TrimSpace(stringField(result, "answer")) != ""
}

func deliveryDue(job *Job, now time.Time) bool {
	if job.ManualWorkflow && job.RetryAt != "" {
		at, err := time.Parse(time.RFC3339Nano, job.RetryAt)
		return err != nil || !now.Before(at)
	}
	if job.DeliveryPolicy != "eventual" || job.RetryAt == "" {
		return true
	}
	if hasDueDeliveryResources(job, now) {
		return true
	}
	at, err := time.Parse(time.RFC3339Nano, job.RetryAt)
	return err != nil || !now.Before(at)
}

// A full worker buffer does not reserve the job. The next tick can still
// place a never-started job ahead of an older retry.
func (q *Queue) enqueueJob(id string, now time.Time) bool {
	q.mu.Lock()
	job := q.jobs[id]
	if q.ch == nil || job == nil || job.Status != "queued" || !deliveryDue(job, now) || q.enqueued[id] || q.activeRuns[id] {
		q.mu.Unlock()
		return false
	}
	if q.enqueued == nil {
		q.enqueued = make(map[string]bool)
	}
	// Send without blocking the queue lock. A full buffer leaves the job
	// unenqueued so the next tick can still prefer a job that has never started.
	select {
	case q.ch <- id:
		q.enqueued[id] = true
		q.mu.Unlock()
		return true
	default:
		q.mu.Unlock()
		return false
	}
}

func deliveryNeverStarted(job *Job) bool {
	return job != nil && job.RetryCount == 0 && len(job.Results) == 0
}

func (q *Queue) scheduleDeliveries(now time.Time) int {
	q.mu.Lock()
	ids := make([]string, 0)
	for id, job := range q.jobs {
		if job.Status == "queued" && deliveryDue(job, now) && !q.enqueued[id] && !q.activeRuns[id] {
			ids = append(ids, id)
		}
	}
	sort.Slice(ids, func(i, j int) bool {
		a, b := q.jobs[ids[i]], q.jobs[ids[j]]
		if fresh, other := deliveryNeverStarted(a), deliveryNeverStarted(b); fresh != other {
			return fresh
		}
		return a.CreatedAt < b.CreatedAt
	})
	q.mu.Unlock()
	count := 0
	for _, id := range ids {
		if q.enqueueJob(id, now) {
			count++
		}
	}
	return count
}

func (q *Queue) deliveryScheduler() {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for now := range ticker.C {
		q.scheduleDeliveries(now)
		if now.Unix()%15 == 0 {
			q.scheduleManualRecoveries(now)
		}
	}
}

func deliveryAdmissionTimeout() time.Duration {
	// Waiting for a free browser is separate from the page run. Fifteen seconds
	// expired while the two running pages were still opening, so the waiter
	// left and came back. Forty-five seconds matches the page-open budget.
	return time.Duration(envInt("GEO_DELIVERY_ADMISSION_SEC", 45, 1, 120)) * time.Second
}

func deliveryDelay(count int, state string) time.Duration {
	base := envInt("GEO_DELIVERY_RETRY_BASE_SEC", 30, 1, 300)
	capSeconds := envInt("GEO_DELIVERY_RETRY_MAX_SEC", 300, base, 3600)
	if state == "operator_wait" || state == "awaiting_confirmation" {
		return time.Duration(envInt("GEO_DELIVERY_OPERATOR_RECHECK_SEC", capSeconds, base, 3600)) * time.Second
	}
	seconds := base
	for i := 1; i < count && seconds < capSeconds; i++ {
		if seconds > capSeconds/2 {
			seconds = capSeconds
			break
		}
		seconds *= 2
	}
	if seconds > capSeconds {
		seconds = capSeconds
	}
	return time.Duration(seconds) * time.Second
}

func webFailureAllowsAPI(result map[string]any) bool {
	code := stringField(result, "error_code")
	if code == "recovery_queue_busy" || code == "recovery_target_owned" {
		return false
	}
	if localWebFailure(code) || code == "platform_session_unavailable" {
		return result["web_prepare_exhausted"] == true || result["web_recovery_exhausted"] == true || result["web_setup_wait_exhausted"] == true
	}
	return true
}

func allPrepareCandidatesFailed(candidates []string, failed map[string]bool) bool {
	if len(candidates) == 0 {
		return false
	}
	for _, id := range candidates {
		if id == "" || !failed[id] {
			return false
		}
	}
	return true
}

func deliveryWaitState(site string, result map[string]any) (string, string) {
	code := stringField(result, "error_code")
	apiCode := stringField(result, "api_error_code")
	if code == "recovery_queue_busy" {
		return "retry_wait", code
	}
	if deliveryPermanentAPIRejection(result) {
		return "operator_wait", apiCode
	}
	if stringField(result, "routing_mode") == RoutingAPIOnly {
		if code == "api_not_configured" {
			return "operator_wait", code
		}
		if apiCode != "" {
			return "retry_wait", apiCode
		}
		if code == "" {
			code = "empty_answer"
		}
		return "retry_wait", code
	}
	if code == "browser_admission_timeout" || code == "session_setup_timeout" {
		return "retry_wait", code
	}
	if stringField(result, "retry_state") == "awaiting_confirmation" || strings.HasPrefix(code, "recovery_") || code == "target_missing" {
		return "awaiting_confirmation", code
	}
	if !officialAPIConfigured(site) {
		if !siteAPIFallbackEnabled(site) {
			return "operator_wait", "api_disabled"
		}
		return "operator_wait", "api_not_configured"
	}
	switch code {
	case "platform_auth_unavailable", "platform_quota", "platform_risk_control", "sms_timeout", "operator_required", "cli_disabled", "cli_unavailable", "no_progress":
		return "operator_wait", code
	}
	if apiCode != "" {
		return "retry_wait", apiCode
	}
	if code == "" {
		code = "empty_answer"
	}
	return "retry_wait", code
}

func deliveryResults(job *Job) []map[string]any {
	results := make([]map[string]any, len(job.Sites))
	for i, site := range job.Sites {
		for _, result := range job.Results {
			if internalSiteID(stringField(result, "site_id")) == internalSiteID(site) {
				results[i] = result
				break
			}
		}
	}
	return results
}

func compactDeliveryResults(results []map[string]any) ([]map[string]any, int) {
	rows := make([]map[string]any, 0, len(results))
	completed := 0
	for _, row := range results {
		if row != nil {
			rows = append(rows, row)
		}
		if validDelivery(row) {
			completed++
		}
	}
	return rows, completed
}

func evidenceCount(value any) int {
	switch n := value.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	}
	return 0
}

// Retain the newest actual attempts, including the eventual successful call.
// Old dropped evidence remains explicit instead of inventing historical rates.
func mergeDeliveryResult(previous, next map[string]any) map[string]any {
	if next == nil {
		next = map[string]any{"ok": false, "error_code": "empty_answer"}
	}
	out := make(map[string]any, len(next)+3)
	for key, value := range next {
		out[key] = value
	}
	// Preparation budgets survive unrelated waits and bounded attempt history.
	for _, key := range []string{"web_setup_wait_count", "web_setup_wait_started_at"} {
		if _, exists := out[key]; !exists && previous[key] != nil {
			out[key] = previous[key]
		}
	}
	attempts := append(cloneAttemptList(previous["attempts"]), cloneAttemptList(next["attempts"])...)
	dropped := evidenceCount(previous["attempts_dropped"]) + evidenceCount(next["attempts_dropped"])
	if len(attempts) > maxResultAttempts {
		dropped += len(attempts) - maxResultAttempts
		attempts = attempts[len(attempts)-maxResultAttempts:]
	}
	if len(attempts) > 0 {
		out["attempts"] = attempts
	}
	if dropped > 0 || previous["attempts_truncated"] == true || next["attempts_truncated"] == true {
		out["attempts_truncated"], out["attempts_dropped"] = true, dropped
	}
	if prior, ok := previous["web_recovery"].(map[string]any); ok {
		if latest, ok := next["web_recovery"].(map[string]any); ok {
			cloned := make(map[string]any, len(latest))
			for key, value := range latest {
				cloned[key] = value
			}
			out["web_recovery"] = mergeWebRecovery(prior, cloned)
		} else {
			out["web_recovery"] = prior
		}
	}
	if !validDelivery(out) && out["ok"] == true {
		out["ok"], out["error_code"] = false, "empty_answer"
	}
	if validDelivery(out) {
		delete(out, "delivery_pending")
		delete(out, "retry_state")
		delete(out, "delivery_resume_required")
	}
	return out
}

func (q *Queue) runEventualDelivery(parent context.Context, job *Job, started time.Time, queuedMS int64) {
	ctx := context.WithValue(parent, eventualDeliveryKey{}, true)
	now := time.Now()
	ensureDeliverySiteSchedule(job, now)
	results := deliveryResults(job)
	var wg sync.WaitGroup
	var mu sync.Mutex
	seen := map[string]bool{}
	scheduled := 0
	for i, site := range job.Sites {
		if seen[internalSiteID(site)] || !deliverySiteDue(results[i], now) {
			continue
		}
		seen[internalSiteID(site)] = true
		scheduled++
		i, site := i, site
		wg.Add(1)
		go func() {
			defer wg.Done()
			one := mergeDeliveryResult(results[i], q.runDeliverySite(ctx, job, site, results[i]))
			if validDelivery(one) {
				q.releaseRecoveryTargets(job.ID, site)
			}
			one["site_id"] = site
			scheduleDeliverySiteRetry(site, results[i], one, job.RetryCount, time.Now())
			mu.Lock()
			for k, requested := range job.Sites {
				if internalSiteID(requested) == internalSiteID(site) && !validDelivery(results[k]) {
					results[k] = one
				}
			}
			rows, completed := compactDeliveryResults(results)
			q.patch(job.ID, func(j *Job) {
				j.Results, j.CompletedSites = rows, completed
				j.ElapsedMS = job.ElapsedMS + time.Since(started).Milliseconds()
				j.QueuedMS = queuedMS - job.ElapsedMS
				if j.QueuedMS < 0 {
					j.QueuedMS = 0
				}
				j.TotalMS = time.Now().UnixMilli() - j.createdUnix
			})
			mu.Unlock()
		}()
	}
	wg.Wait()
	rows, completed := compactDeliveryResults(results)
	if completed == len(job.Sites) && completed > 0 {
		q.patch(job.ID, func(j *Job) {
			j.Status, j.Results, j.CompletedSites = "succeeded", rows, completed
			j.FinishedAt = time.Now().Format(time.RFC3339Nano)
			j.ElapsedMS = job.ElapsedMS + time.Since(started).Milliseconds()
			j.TotalMS = time.Now().UnixMilli() - j.createdUnix
			j.Error, j.ErrorCode, j.WaitReason, j.RetryAt = "", "", "", ""
			j.Retryable = false
			if j.CallbackURL != "" {
				j.CallbackStatus, j.CallbackAttempts = "pending", 0
			}
		})
		q.cleanupWebRecovery(job.ID)
		go q.callback(job.ID)
		return
	}
	retryAt, reason := nextDeliverySiteRetry(rows, time.Now())
	q.patch(job.ID, func(j *Job) {
		j.Status, j.Results, j.CompletedSites = "queued", rows, completed
		if scheduled > 0 {
			j.RetryCount++
		}
		j.RetryAt, j.WaitReason = retryAt.Format(time.RFC3339Nano), reason
		j.FinishedAt, j.DeadlineAt = "", ""
		j.Error, j.ErrorCode, j.Retryable = "", "", true
		j.CallbackStatus, j.CallbackError, j.CallbackAttempts = "", "", 0
		j.ElapsedMS = job.ElapsedMS + time.Since(started).Milliseconds()
		j.TotalMS = time.Now().UnixMilli() - j.createdUnix
	})
}

func recoverDeliveryJob(job *Job, heldBeforeSend bool) {
	results := deliveryResults(job)
	for i, site := range job.Sites {
		if validDelivery(results[i]) {
			continue
		}
		one := mergeDeliveryResult(nil, results[i])
		one["site_id"], one["delivery_pending"] = site, true
		if !heldBeforeSend {
			one["delivery_resume_required"], one["retry_state"] = true, "awaiting_confirmation"
			if stringField(one, "error_code") == "" {
				one["error_code"] = "service_interrupted"
			}
		} else {
			one["retry_state"] = "retry_wait"
		}
		results[i] = one
	}
	job.Results, job.CompletedSites = compactDeliveryResults(results)
	job.Status, job.FinishedAt, job.DeadlineAt = "queued", "", ""
	job.Error, job.ErrorCode, job.CallbackStatus, job.CallbackError = "", "", "", ""
	job.CallbackAttempts = 0
	job.DeadlinePolicy = "web_first"
	job.UpdatedAt = time.Now().Format(time.RFC3339Nano)
}

type deliveryJournal struct {
	AccountID  string         `json:"account_id"`
	SiteID     string         `json:"site_id"`
	QuestionID string         `json:"question_id"`
	Prompt     string         `json:"prompt"`
	SendState  string         `json:"send_state"`
	PromptHash string         `json:"prompt_sha256"`
	Checkpoint map[string]any `json:"checkpoint"`
}

func readDeliveryJournal(q *Queue, job *Job, site, path string) (*deliveryJournal, bool) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > 8_000_000 {
		return nil, false
	}
	raw, err := os.ReadFile(path)
	var journal deliveryJournal
	if err != nil || json.Unmarshal(raw, &journal) != nil || journal.AccountID == "" || journal.SiteID != internalSiteID(site) || journal.QuestionID != "job_"+job.ID || journal.Prompt != job.Prompt || q.webRecoveryPath(job.ID, site, journal.AccountID) != path {
		return nil, false
	}
	if journal.PromptHash != "" {
		hash := sha256.Sum256([]byte(job.Prompt))
		if journal.PromptHash != hex.EncodeToString(hash[:]) {
			return nil, false
		}
	}
	return &journal, true
}

func (q *Queue) completedDeliveryCheckpoint(job *Job, site string) map[string]any {
	base := q.webRecoveryPath(job.ID, site, "account")
	if base == "" {
		return nil
	}
	paths, _ := filepath.Glob(filepath.Join(filepath.Dir(base), "*.json"))
	for _, path := range paths {
		journal, valid := readDeliveryJournal(q, job, site, path)
		if !valid || journal.SendState != "sent" || journal.Checkpoint == nil {
			continue
		}
		if account := stringField(journal.Checkpoint, "account_id"); account != "" && account != journal.AccountID {
			continue
		}
		raw, err := json.Marshal(journal.Checkpoint)
		if err != nil {
			continue
		}
		one := completedWebCheckpoint(raw, internalSiteID(site), job.Prompt, job.ID)
		if !validDelivery(one) || yuanbaoQuotaReply(internalSiteID(site), stringField(one, "answer")) {
			continue
		}
		one["site_id"], one["account_id"] = site, journal.AccountID
		if share := stringField(journal.Checkpoint, "url"); stringField(journal.Checkpoint, "share_status") == "verified" && verifiedPublicShareURL(site, share) {
			one["url"], one["share_status"], one["share_error_code"] = share, "verified", ""
			delete(one, "share_skipped")
			delete(one, "share_skip_reason")
			if strings.HasPrefix(stringField(one, "optional_stage_error_code"), "share_") {
				delete(one, "optional_stage_error_code")
			}
		} else {
			one["optional_stage_error_code"], one["share_error_code"] = "share_not_attempted", "share_not_attempted"
			one["share_skipped"], one["share_skip_reason"] = true, "share_not_attempted"
		}
		one["delivery_checkpoint_recovered"] = true
		return one
	}
	return nil
}

// Only an identity-bound before_send journal permits another send after an
// interruption. Missing/corrupt state is uncertainty, never permission.
func (q *Queue) deliveryJournalState(job *Job, site string) (account string, pending, safe bool) {
	base := q.webRecoveryPath(job.ID, site, "account")
	if base == "" {
		return "", false, false
	}
	pattern := filepath.Join(filepath.Dir(base), "*.json")
	paths, _ := filepath.Glob(pattern)
	pendingCount := 0
	for _, path := range paths {
		journal, valid := readDeliveryJournal(q, job, site, path)
		if !valid {
			return "", true, false
		}
		switch journal.SendState {
		case "sending", "sent":
			account = journal.AccountID
			pendingCount++
		case "before_send":
			safe = true
		default:
			return "", true, false
		}
	}
	if pendingCount > 1 {
		return "", true, false
	}
	if pendingCount == 1 {
		return account, true, false
	}
	return "", false, safe
}

// A restart flags every unfinished site of a running job for resume. When the
// durable dispatch record proves this site never started a scraper, and it
// has no journal, it runs fresh instead of falling straight to the API.
func (q *Queue) deliveryNeverSent(job *Job, site string) bool {
	if job == nil || !job.DispatchTracked || usesAgentBay() || doubaoUsesVolcengine() {
		return false
	}
	for _, sent := range job.WebDispatched {
		if sent == internalSiteID(site) {
			return false
		}
	}
	base := q.webRecoveryPath(job.ID, site, "account")
	if base == "" {
		return false
	}
	paths, err := filepath.Glob(filepath.Join(filepath.Dir(base), "*"))
	return err == nil && len(paths) == 0
}

func hasDeliveredWebAttempt(result map[string]any) bool {
	for _, attempt := range cloneAttemptList(result["attempts"]) {
		if attempt["channel"] == "web" && attempt["skipped"] != true {
			return true
		}
	}
	return result != nil && result["web_skipped"] != true && stringField(result, "account_id") != ""
}

func unconfirmedDeliverySend(parent context.Context) bool {
	execution, ok := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution)
	if !ok || execution.StatePath == "" {
		return true
	}
	raw, err := os.ReadFile(execution.StatePath)
	var journal deliveryJournal
	return err != nil || json.Unmarshal(raw, &journal) != nil || journal.SendState != "sent"
}

func (q *Queue) runDeliverySite(ctx context.Context, job *Job, site string, previous map[string]any) map[string]any {
	if job.routingMode(site) == RoutingAPIOnly {
		return q.runDirectAPIContext(ctx, site, job.Prompt)
	}
	if saved := q.completedDeliveryCheckpoint(job, site); saved != nil {
		return saved
	}
	account, pending, beforeSend := q.deliveryJournalState(job, site)
	if pending {
		return q.resumeDeliverySite(ctx, job, site, account)
	}
	if !beforeSend && (hasDeliveredWebAttempt(previous) || (previous["delivery_resume_required"] == true && !q.deliveryNeverSent(job, site))) {
		return q.resumeDeliverySite(ctx, job, site, "")
	}
	if fallback := q.deliverySetupFallback(ctx, job, site, previous); fallback != nil {
		return fallback
	}
	one := q.runOneSiteContext(ctx, site, job.Prompt, job.ID, len(job.Sites))
	one = q.applyDeliverySetupWaitFallback(ctx, job, site, previous, one)
	return q.applyDeliverySessionWaitFallback(ctx, site, job.Prompt, previous, one)
}

type existingRecoveryPool interface {
	AcquireExistingForRecoveryContext(context.Context, string, string) (*sessions.Lease, error)
}

func (q *Queue) resumeDeliverySite(parent context.Context, job *Job, site, account string) (out map[string]any) {
	ctx, attempts := newAttemptContext(parent)
	started := time.Now()
	timings := &stageTimings{}
	out = siteError(site, job.Prompt, "recovery_unconfirmed", "原网页发送状态待确认，未重复提交问题", true, 0)
	out["delivery_resume_required"] = true
	defer func() {
		if !attempts.hasChannel("web") {
			reason := stringField(out, "web_skip_reason")
			if reason == "" {
				reason = "awaiting_confirmation"
			}
			attempts.skip("web", account, stringField(out, "error_code"), reason, started)
		}
		timings.attach(out)
		if !validDelivery(out) && webFailureAllowsAPI(out) && parent.Err() == nil {
			out = q.applyAPIFallbackContext(ctx, site, job.Prompt, out)
		}
		if !validDelivery(out) {
			out["delivery_resume_required"] = true
		}
		out["total_ms"] = time.Since(started).Milliseconds()
		attempts.attach(out)
	}()
	if q.recoverySem != nil && len(q.recoverySem) >= cap(q.recoverySem) {
		out["error_code"] = "recovery_queue_busy"
		out["error"] = "原网页等待恢复通道，已保留发送状态"
		return out
	}
	if parent.Err() != nil {
		out = webSkip(site, job.Prompt, "session_setup_timeout", "request_cancelled", "网页恢复已取消，保留原问题状态")
		return out
	}
	pool, ok := q.Sess.(existingRecoveryPool)
	if !ok || account == "" {
		out["error_code"] = "recovery_state_missing"
		return out
	}
	fast, fastAvailable := q.Sess.(existingRecoveryAdmissionPool)
	if fastAvailable {
		switch fast.InspectExistingForRecovery(internalSiteID(site), account) {
		case sessions.ExistingRecoveryReady:
		case sessions.ExistingRecoveryMissing:
			q.releaseRecoveryTargets(job.ID, site)
			out["error_code"] = "recovery_target_missing"
			return out
		default:
			out = webSkip(site, job.Prompt, "session_setup_timeout", "session_busy", "原网页账号仍被占用，保留原问题并退避等待")
			return out
		}
	}
	admission, stopAdmission := q.platformAdmissionContext(parent)
	release, admitted := q.acquireDeliveryBrowserResources(parent, admission, internalSiteID(site), job.ID, timings)
	stopAdmission()
	if !admitted {
		out = webSkip(site, job.Prompt, "browser_admission_timeout", "browser_admission", "等待网页资源，保留原问题状态")
		return out
	}
	defer release()
	lease, err := q.acquireExistingDeliveryLease(parent, pool, fast, site, account)
	if err != nil || lease == nil {
		if existingDeliveryLeaseBusy(err) {
			out = webSkip(site, job.Prompt, "session_setup_timeout", "session_setup_timeout", "原网页账号仍被占用，保留原问题并退避等待")
			return out
		}
		out["error_code"] = "recovery_target_missing"
		return out
	}
	defer lease.Release()
	if lease.AccountID != account {
		out["error_code"] = "recovery_context_unconfirmed"
		return out
	}
	cdp, err := lease.CDPEndpoint()
	if err != nil {
		out["error_code"] = "recovery_target_missing"
		return out
	}
	execution := webRecoveryExecution{StatePath: q.webRecoveryPath(job.ID, site, account), AccountID: account}
	for _, id := range q.loggedInAccounts(internalSiteID(site)) {
		if id == account {
			execution.PriorLoggedIn = true
		}
	}
	resume := withWebRecovery(withAttemptAccount(ctx, account), execution)
	resume, cancelRound := q.webRoundContext(resume, internalSiteID(site), len(job.Sites))
	defer cancelRound()
	one, err, recovery := q.recoverWebAttempt(resume, lease, cdp, site, job.Prompt, job.ID, len(job.Sites), false, recoveryFailure("recovery_unconfirmed"))
	if err == nil && validDelivery(one) {
		out = one
		out["site_id"], out["account_id"] = site, account
	} else if err != nil {
		code, message, retryable := publicScrapeError(err)
		out = siteError(site, job.Prompt, code, message, retryable, time.Since(started).Milliseconds())
		out["account_id"] = account
	}
	out["web_recovery"] = recovery
	return out
}
