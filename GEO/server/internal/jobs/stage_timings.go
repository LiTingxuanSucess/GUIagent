package jobs

import (
	"context"
	"sync"
	"time"
)

type webWindowKey struct{}

type stageTimings struct {
	mu sync.Mutex
	ms map[string]int64
}

func (t *stageTimings) add(name string, elapsed time.Duration) {
	if elapsed < 0 {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.ms == nil {
		t.ms = map[string]int64{}
	}
	t.ms[name] += elapsed.Milliseconds()
}

func (t *stageTimings) attach(result map[string]any) {
	if result == nil {
		return
	}
	merged := map[string]any{}
	if existing, ok := result["stage_timings"].(map[string]any); ok {
		for key, value := range existing {
			merged[key] = value
		}
	}
	t.mu.Lock()
	for key, value := range t.ms {
		merged[key] = value
	}
	t.mu.Unlock()
	result["stage_timings"] = merged
}

func webWindowRemaining(ctx context.Context, site string) bool {
	minimum, _ := ctx.Value(webWindowKey{}).(time.Duration)
	if minimum == 0 {
		minimum = minimumWebWindow(site)
	}
	deadline, ok := ctx.Deadline()
	return ctx.Err() == nil && (!ok || time.Until(deadline) >= minimum)
}

func webSkip(site, prompt, code, reason, message string) map[string]any {
	result := siteError(site, prompt, code, message, true, 0)
	result["web_skipped"], result["web_skip_reason"] = true, reason
	result["web_elapsed_ms"] = int64(0)
	return result
}

func localWebFailure(code string) bool {
	switch code {
	case "insufficient_web_budget", "browser_admission_timeout", "session_setup_timeout", "browser_start_failed":
		return true
	}
	return false
}
