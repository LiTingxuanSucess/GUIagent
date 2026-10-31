package jobs

import (
	"context"
	"time"
)

// Preparation waits differ from browser admission: the job reached account
// preparation, but no question was sent. Their budget spans delivery rounds.
func deliverySetupWait(row map[string]any) bool {
	if row == nil || validDelivery(row) || row["web_skipped"] != true {
		return false
	}
	return setupWaitCode(stringField(row, "error_code"), stringField(row, "web_skip_reason"))
}

func setupWaitCode(code, reason string) bool {
	switch code {
	case "session_setup_timeout":
		return reason == "session_setup" || reason == "session_setup_timeout"
	case "platform_auth_unavailable":
		return reason == "no_logged_in_account"
	case "site_request_failed":
		return reason == "session_setup" // legacy preparation error
	}
	return false
}

func deliverySetupWaitEvidence(row map[string]any) (count int, first time.Time) {
	count = evidenceCount(row["web_setup_wait_count"])
	first, _ = time.Parse(time.RFC3339Nano, stringField(row, "web_setup_wait_started_at"))
	observed := 0
	for _, a := range cloneAttemptList(row["attempts"]) {
		if a["channel"] != "web" || a["skipped"] != true || !setupWaitCode(stringField(a, "error_code"), stringField(a, "skip_reason")) {
			continue
		}
		observed++
		if at, err := time.Parse(time.RFC3339Nano, stringField(a, "started_at")); err == nil && (first.IsZero() || at.Before(first)) {
			first = at
		}
	}
	return max(count, observed), first
}

func deliverySetupBudgetExhausted(count int, first, now time.Time) bool {
	return count > 0 && (count >= envInt("GEO_DELIVERY_SETUP_WAIT_LIMIT", 2, 1, 10) || !first.IsZero() && now.Sub(first) >= time.Duration(envInt("GEO_DELIVERY_SETUP_WAIT_SEC", 180, 1, 900))*time.Second)
}

func (q *Queue) deliverySafeBeforeSend(job *Job, site string) bool {
	// Get the current dispatch record. The caller's round snapshot may predate
	// a scraper launch; that stale snapshot cannot prove a question unsent.
	if current, ok := q.Get(job.ID); ok {
		job = current
	}
	if job.Status == "cancelled" || job.Status == "succeeded" {
		return false
	}
	_, pending, before := q.deliveryJournalState(job, site)
	return !pending && (before || q.deliveryNeverSent(job, site))
}

func newSetupFallbackResult(previous map[string]any) map[string]any {
	row := make(map[string]any, len(previous))
	for k, v := range previous {
		switch k {
		case "attempts", "attempts_dropped", "attempts_truncated", "web_recovery", "delivery_pending", "delivery_resume_required", "retry_state", "delivery_retry_at", "delivery_retry_count", "delivery_retry_reason":
			continue
		}
		row[k] = v
	}
	return row
}

func markSetupFallback(row map[string]any, count int, first, now time.Time) {
	row["web_setup_wait_exhausted"], row["fallback_reason"] = true, "web_setup_wait_exhausted"
	row["web_setup_wait_count"] = count
	if !first.IsZero() {
		row["web_setup_wait_started_at"] = first.Format(time.RFC3339Nano)
		row["web_setup_wait_ms"] = max(int64(0), now.Sub(first).Milliseconds())
	}
}

// Once a persisted preparation budget is spent, do not start another Chrome
// or CLI turn just to rediscover the same wait. Normal API limits still apply.
func (q *Queue) deliverySetupFallback(ctx context.Context, job *Job, site string, previous map[string]any) map[string]any {
	count, first := deliverySetupWaitEvidence(previous)
	now := time.Now()
	if ctx.Err() != nil || !deliverySetupWait(previous) || !deliverySetupBudgetExhausted(count, first, now) || !siteAPIFallbackEnabled(site) || !officialAPIConfigured(site) || !q.deliverySafeBeforeSend(job, site) {
		return nil
	}
	row := newSetupFallbackResult(previous)
	markSetupFallback(row, count, first, now)
	return q.applyAPIFallbackContext(ctx, site, job.Prompt, row)
}

func (q *Queue) applyDeliverySetupWaitFallback(ctx context.Context, job *Job, site string, previous, current map[string]any) map[string]any {
	if !deliverySetupWait(current) {
		return current
	}
	count, first := deliverySetupWaitEvidence(previous)
	added, started := deliverySetupWaitEvidence(current)
	count += max(1, added)
	if first.IsZero() || !started.IsZero() && started.Before(first) {
		first = started
	}
	if first.IsZero() {
		first = time.Now()
	}
	current["web_setup_wait_count"], current["web_setup_wait_started_at"] = count, first.Format(time.RFC3339Nano)
	if ctx.Err() != nil || current["api_fallback_failed"] == true || !officialAPIConfigured(site) || !deliverySetupBudgetExhausted(count, first, time.Now()) || !q.deliverySafeBeforeSend(job, site) {
		return current
	}
	markSetupFallback(current, count, first, time.Now())
	return q.applyAPIFallbackContext(ctx, site, job.Prompt, current)
}
