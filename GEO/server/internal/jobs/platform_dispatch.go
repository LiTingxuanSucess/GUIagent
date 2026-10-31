package jobs

import (
	"context"
	"sync"
	"time"
)

type jobExecutionKey struct{}

// A parent remains queued while its platforms only wait for resources.
// Several parents may run together when browser and API lanes overlap.
func (q *Queue) beginPlatformActivity(id string) func() {
	if q.dispatchSem == nil || id == "" {
		return func() {}
	}
	started := q.patchIf(id, func(j *Job) bool {
		if !q.activeRuns[id] || j.Status != "queued" && j.Status != "running" {
			return false
		}
		if q.activePlatforms == nil {
			q.activePlatforms = make(map[string]int)
		}
		q.activePlatforms[id]++
		j.Status = "running"
		if j.StartedAt == "" {
			j.StartedAt = time.Now().Format(time.RFC3339Nano)
		}
		return true
	})
	var once sync.Once
	return func() {
		once.Do(func() {
			if !started {
				return
			}
			q.patchIf(id, func(j *Job) bool {
				q.activePlatforms[id]--
				if q.activePlatforms[id] > 0 {
					return false
				}
				delete(q.activePlatforms, id)
				if q.activeRuns[id] && j.Status == "running" {
					j.Status = "queued"
					return true
				}
				return false
			})
		})
	}
}

func (q *Queue) platformAdmissionContext(parent context.Context) (context.Context, context.CancelFunc) {
	if q.dispatchSem == nil {
		return context.WithTimeout(parent, deliveryAdmissionTimeout())
	}
	// Waiting is cheap and cancellable; no browser/account permit is held.
	// Keep the existing fair ticket instead of recycling it every 45s.
	return context.WithTimeout(parent, time.Duration(envInt("GEO_DISPATCH_RESOURCE_WAIT_SEC", 300, 45, 900))*time.Second)
}

func (q *Queue) webRoundContext(parent context.Context, site string, count int) (context.Context, context.CancelFunc) {
	ctx := context.WithValue(parent, webExecutionBudgetKey{}, count)
	if q.dispatchSem == nil {
		return context.WithCancel(ctx)
	}
	now := time.Now()
	deadline := requestWebDeadline(ctx, site, now.Add(siteAttemptTimeout()), now, count)
	// All account acquisition, failover, CLI and resume in this round share
	// this deadline. API fallback still uses the independent parent budget.
	return context.WithDeadline(ctx, deadline)
}
