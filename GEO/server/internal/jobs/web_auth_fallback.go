package jobs

import "context"

func deliverySessionWaitLimit() int {
	// One wait is enough. The customer poll treats this intermediate result as a
	// platform failure, so the next identical wait uses the configured API.
	return envInt("GEO_DELIVERY_SESSION_WAIT_LIMIT", 1, 1, 10)
}

func deliverySessionWaitCount(result map[string]any) int {
	count := 0
	for _, attempt := range cloneAttemptList(result["attempts"]) {
		if attempt["channel"] == "web" &&
			attempt["skipped"] == true &&
			stringField(attempt, "error_code") == "platform_session_unavailable" &&
			stringField(attempt, "skip_reason") == "session_unavailable" {
			count++
		}
	}
	return count
}

// A recovery page can be briefly unavailable while its browser is being
// handed over. The first identical wait is already a customer-visible failure,
// so the default limit uses the configured API instead of waiting again.
func deliverySessionWaitExhausted(previous, current map[string]any) bool {
	if current == nil ||
		stringField(current, "error_code") != "platform_session_unavailable" ||
		stringField(current, "web_skip_reason") != "session_unavailable" ||
		current["delivery_resume_required"] == true {
		return false
	}
	return deliverySessionWaitCount(previous)+deliverySessionWaitCount(current) >= deliverySessionWaitLimit()
}

func (q *Queue) applyDeliverySessionWaitFallback(parent context.Context, site, prompt string, previous, current map[string]any) map[string]any {
	if parent.Err() != nil || !deliverySessionWaitExhausted(previous, current) {
		return current
	}
	current["web_recovery_exhausted"] = true
	return q.applyAPIFallbackContext(parent, site, prompt, current)
}

// Waiting for capacity has no bearing on whether the webpage can answer.
// Login-only recovery may legitimately exhaust every account without sending
// the question; keep those attempts skipped while permitting the final API.
func deliveryWebFallbackEligible(parent context.Context, result map[string]any) bool {
	return !isEventualDelivery(parent) || result["web_skipped"] != true ||
		result["web_prepare_exhausted"] == true || result["web_recovery_exhausted"] == true || result["web_setup_wait_exhausted"] == true
}

func exhaustedWebAuthentication(candidates []string, preparation, authentication map[string]bool, result map[string]any) bool {
	if len(candidates) == 0 || len(authentication) == 0 || result["delivery_resume_required"] == true {
		return false
	}
	switch stringField(result, "error_code") {
	case "platform_auth_unavailable", "platform_risk_control", "sms_timeout", "sms_required", "platform_session_unavailable", "browser_start_failed", "session_setup_failed":
	default:
		return false
	}
	for _, id := range candidates {
		if id == "" || (!preparation[id] && !authentication[id]) {
			return false
		}
	}
	return true
}
