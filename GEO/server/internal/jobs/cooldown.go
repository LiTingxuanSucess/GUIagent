package jobs

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"time"
)

// Browser cooldowns survive a restart and only affect the failed platform.
// Official APIs remain available during cooldown; no credentials are stored.
type siteCooldownState struct {
	Until       time.Time `json:"until"`
	Code        string    `json:"error_code"`
	Failures    int       `json:"consecutive_failures,omitempty"`
	LastFailure time.Time `json:"last_failure,omitempty"`
}

func (q *Queue) cooldownPath(site string) string {
	return filepath.Join(q.DataDir, "cooldowns", internalSiteID(site)+".json")
}

func (q *Queue) siteCooldown(internalSite, site, prompt string) map[string]any {
	if q.DataDir == "" || envInt("GEO_SITE_COOLDOWN_ENABLED", 1, 0, 1) == 0 {
		return nil
	}
	raw, err := os.ReadFile(q.cooldownPath(internalSite))
	if err != nil {
		return nil
	}
	var state siteCooldownState
	if json.Unmarshal(raw, &state) != nil || !time.Now().Before(state.Until) {
		return nil
	}
	// A prior account's quota, auth error or timeout cannot disable healthy
	// local profiles or prevent a pending page from receiving CLI recovery.
	// Keep the old record for cloud/no-candidate cases and actual global limits.
	if state.Code != "platform_rate_limited" && q.Sess != nil && !usesAgentBay() && !doubaoUsesVolcengine() &&
		(len(q.loggedInAccounts(internalSite)) > 0 || len(q.recoveryAccountIDs(internalSite)) > 0) {
		return nil
	}
	failure := siteError(site, prompt, state.Code, "平台网页正在冷却，请稍后重试或完成后台验证", true, 0)
	failure["cooldown_until"] = state.Until.Format(time.RFC3339)
	failure["retry_after_sec"] = int(time.Until(state.Until).Seconds()) + 1
	failure["web_skipped"] = true
	failure["web_skip_reason"] = "site_cooldown"
	return failure
}

func (q *Queue) recordSiteCooldown(site string, result map[string]any) {
	if q.DataDir == "" || result == nil || envInt("GEO_SITE_COOLDOWN_ENABLED", 1, 0, 1) == 0 {
		return
	}
	q.cooldownMu.Lock()
	defer q.cooldownMu.Unlock()
	if ok, _ := result["ok"].(bool); ok {
		_ = os.Remove(q.cooldownPath(site))
		return
	}
	if skipped, _ := result["web_skipped"].(bool); skipped {
		return // Repeated jobs must not extend the cooldown forever.
	}
	code := stringField(result, "error_code")
	if localWebFailure(code) {
		return // Local scheduling/setup failures say nothing about platform health.
	}
	state := siteCooldownState{Code: code, LastFailure: time.Now()}
	var seconds int
	switch code {
	case "platform_rate_limited":
		seconds=envInt("GEO_RATE_LIMIT_BACKOFF_SEC",60,1,600)
		if after,ok:=result["retry_after_sec"].(int64);ok && after>0 && after<=600 { seconds=int(after) }
	case "platform_risk_control":
		seconds = envInt("GEO_RISK_COOLDOWN_SEC", 600, 0, 3600)
	case "platform_quota":
		seconds = envInt("GEO_QUOTA_COOLDOWN_SEC", 1800, 0, 86400)
	case "platform_auth_unavailable":
		// Local session state already excludes a logged-out account. Cloud
		// browsers need a short cooldown to avoid repeating SMS login.
		if usesAgentBay() {
			seconds = envInt("GEO_AUTH_COOLDOWN_SEC", 120, 0, 3600)
		}
	case "sms_timeout":
		seconds = envInt("GEO_SMS_COOLDOWN_SEC", 300, 0, 3600)
	case "site_timeout", "platform_busy", "site_request_failed", "empty_answer":
		// Only actual failed browser attempts count. Admission and cooldown
		// skips above must not delay the next real recovery probe.
		if !officialAPIConfigured(site) {
			return
		}
		raw, _ := os.ReadFile(q.cooldownPath(site))
		var previous siteCooldownState
		_ = json.Unmarshal(raw, &previous)
		if time.Since(previous.LastFailure) < 10*time.Minute {
			state.Failures = previous.Failures
		}
		state.Failures++
		if state.Failures >= envInt("GEO_TRANSIENT_COOLDOWN_THRESHOLD", 3, 2, 10) {
			seconds = envInt("GEO_TRANSIENT_COOLDOWN_SEC", 120, 0, 600)
		}
	}
	if seconds == 0 && state.Failures == 0 {
		return
	}
	until := time.Now().Add(time.Duration(seconds) * time.Second)
	if seconds > 0 {
		state.Until = until
		result["cooldown_until"] = until.Format(time.RFC3339)
		result["retry_after_sec"] = seconds
	}
	raw, _ := json.Marshal(state)
	path := q.cooldownPath(site)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		log.Printf("site cooldown mkdir site=%s: %v", site, err)
		return
	}
	// Multiple pool accounts may finish concurrently. Protect the complete
	// read/modify/write above; rename remains atomic for readers.
	if err := os.WriteFile(path+".tmp", raw, 0o600); err != nil {
		log.Printf("site cooldown write site=%s: %v", site, err)
		return
	}
	if err := os.Rename(path+".tmp", path); err != nil {
		log.Printf("site cooldown rename site=%s: %v", site, err)
	}
	if seconds > 0 {
		log.Printf("site cooldown site=%s code=%s failures=%d until=%s", site, code, state.Failures, until.Format(time.RFC3339))
	}
}
