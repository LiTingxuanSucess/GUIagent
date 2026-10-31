package jobs

import (
	"context"
	"math"
	"strings"
	"time"
)

// API admission has its own short wait. A queued call must still have a useful
// execution window when its slot becomes available, including search/reasoning.
func apiMinimumExecution(site string) time.Duration {
	seconds := apiEnvInt("GEO_API_MIN_EXECUTION_SEC", 15, 1, 120)
	seconds = apiEnvInt("GEO_"+strings.ToUpper(internalSiteID(site))+"_API_MIN_EXECUTION_SEC", seconds, 1, 120)
	minimum := time.Duration(seconds) * time.Second
	if timeout := apiTimeout(site); timeout < minimum {
		minimum = timeout
	}
	return minimum
}

func hasUsefulAPIWindow(parent context.Context, site string, now time.Time) bool {
	if parent.Err() != nil {
		return false
	}
	deadline, bounded := parent.Deadline()
	return !bounded || deadline.Sub(now) >= apiMinimumExecution(site)
}

func apiAdmissionContext(parent context.Context, site string) (context.Context, context.CancelFunc) {
	wait := time.Duration(apiEnvInt("GEO_API_ADMISSION_WAIT_SEC", 5, 1, 60)) * time.Second
	deadline := time.Now().Add(wait)
	if overall, bounded := parent.Deadline(); bounded {
		latestStart := overall.Add(-apiMinimumExecution(site))
		if latestStart.Before(deadline) {
			deadline = latestStart
		}
	}
	return context.WithDeadline(parent, deadline)
}

// Copy numeric timings rather than aliasing the completed browser's map. The
// only data added here are durations; provider errors and request bodies stay out.
func recordAPITimings(result, previous map[string]any, waitMS, executionMS int64) {
	timings := make(map[string]any)
	copyTimings := func(value any) {
		switch values := value.(type) {
		case map[string]any:
			for key, value := range values {
				switch number := value.(type) {
				case int:
					if number >= 0 {
						timings[key] = number
					}
				case int64:
					if number >= 0 {
						timings[key] = number
					}
				case float64:
					if number >= 0 && !math.IsNaN(number) && !math.IsInf(number, 0) {
						timings[key] = number
					}
				}
			}
		case map[string]int64:
			for key, number := range values {
				if number >= 0 {
					timings[key] = number
				}
			}
		}
	}
	if previous != nil {
		copyTimings(previous["stage_timings"])
	}
	copyTimings(result["stage_timings"])
	result["api_wait_ms"], result["api_execution_ms"] = waitMS, executionMS
	timings["api_wait_ms"], timings["api_execution_ms"] = waitMS, executionMS
	result["stage_timings"] = timings
}

func apiSkipped(failure map[string]any, code, reason string, waitStarted time.Time, attempts *attemptCollector, apiAccountID string) map[string]any {
	failure["api_skipped"], failure["api_skip_reason"] = true, reason
	failure["api_error_code"] = code
	failure["api_elapsed_ms"] = int64(0)
	failure["api_error"] = "API 调用因排队或剩余执行预算不足而跳过"
	delete(failure, "api_fallback_failed")
	recordAPITimings(failure, nil, time.Since(waitStarted).Milliseconds(), 0)
	attempts.skip("api", apiAccountID, code, reason, waitStarted)
	return failure
}
