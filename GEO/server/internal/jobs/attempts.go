package jobs

import (
	"context"
	"sync"
	"time"
)

// A collector belongs to one site execution, never the shared job context.
// Keeping it in the context lets retries and replacement results retain the
// evidence without sharing mutable result maps between concurrent sites.
type attemptCollector struct {
	mu      sync.Mutex
	entries []map[string]any
	dropped int
}

type attemptCollectorKey struct{}
type attemptAccountKey struct{}

const maxResultAttempts = 256

func newAttemptContext(parent context.Context) (context.Context, *attemptCollector) {
	collector := &attemptCollector{entries: make([]map[string]any, 0)}
	return context.WithValue(parent, attemptCollectorKey{}, collector), collector
}

func ensureAttemptContext(parent context.Context, result map[string]any) (context.Context, *attemptCollector) {
	if collector := attemptsFromContext(parent); collector != nil {
		return parent, collector
	}
	ctx, collector := newAttemptContext(parent)
	for _, attempt := range cloneAttemptList(result["attempts"]) {
		collector.append(attempt)
	}
	return ctx, collector
}

func attemptsFromContext(ctx context.Context) *attemptCollector {
	collector, _ := ctx.Value(attemptCollectorKey{}).(*attemptCollector)
	return collector
}

func withAttemptAccount(ctx context.Context, accountID string) context.Context {
	return context.WithValue(ctx, attemptAccountKey{}, accountID)
}

func attemptAccount(ctx context.Context) string {
	accountID, _ := ctx.Value(attemptAccountKey{}).(string)
	return accountID
}

func (c *attemptCollector) append(attempt map[string]any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.entries) >= maxResultAttempts {
		c.dropped++
		return
	}
	c.entries = append(c.entries, attempt)
}

func (c *attemptCollector) record(channel, accountID string, ok bool, code string, started time.Time, details ...map[string]any) {
	if ok {
		code = ""
	}
	entry := map[string]any{
		"channel": channel, "account_id": accountID, "ok": ok, "error_code": code,
		"started_at": started.UTC().Format(time.RFC3339Nano), "elapsed_ms": time.Since(started).Milliseconds(),
	}
	for _, detail := range details {
		for _, key := range []string{"failure_stage", "failure_reason", "diagnostic_id"} {
			if value := stringField(detail, key); value != "" {
				entry[key] = value
			}
		}
	}
	c.append(entry)
}

// Skips carry the blocking reason for inspection, but are never actual failed
// attempts in channel success-rate denominators.
func (c *attemptCollector) skip(channel, accountID, code, reason string, started time.Time) {
	c.append(map[string]any{
		"channel": channel, "account_id": accountID, "ok": false, "error_code": code,
		"started_at": started.UTC().Format(time.RFC3339Nano), "elapsed_ms": time.Since(started).Milliseconds(),
		"skipped": true, "skip_reason": reason,
	})
}

func (c *attemptCollector) hasChannel(channel string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, attempt := range c.entries {
		if attempt["channel"] == channel {
			return true
		}
	}
	return false
}

func (c *attemptCollector) hasExecutedChannel(channel string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, attempt := range c.entries {
		if attempt["channel"] == channel && attempt["skipped"] != true {
			return true
		}
	}
	return false
}

func (c *attemptCollector) attach(result map[string]any) {
	if result == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	result["attempts"] = cloneAttemptList(c.entries)
	if c.dropped > 0 {
		result["attempts_truncated"] = true
		result["attempts_dropped"] = c.dropped
	}
}

func cloneAttemptList(value any) []map[string]any {
	cloned := make([]map[string]any, 0)
	clone := func(original map[string]any) {
		if original == nil {
			return
		}
		one := make(map[string]any, len(original))
		for key, value := range original {
			one[key] = value
		}
		cloned = append(cloned, one)
	}
	switch entries := value.(type) {
	case []map[string]any:
		for _, entry := range entries {
			clone(entry)
		}
	case []any:
		for _, entry := range entries {
			one, _ := entry.(map[string]any)
			clone(one)
		}
	}
	return cloned
}

func preserveAttemptEvidence(from, to map[string]any) {
	if attempts, exists := from["attempts"]; exists {
		to["attempts"] = cloneAttemptList(attempts)
	}
	for _, key := range []string{"attempts_truncated", "attempts_dropped"} {
		if value, exists := from[key]; exists {
			to[key] = value
		}
	}
}
