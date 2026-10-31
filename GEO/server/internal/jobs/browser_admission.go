package jobs

import (
	"context"
	"sync"
	"time"
)

type browserWaiter struct {
	ctx          context.Context
	job, site    string
	gate         chan struct{}
	local, cloud bool
	ready        chan struct{}
	granted      bool
	release      func()
	timings      *stageTimings
	blocked      string
	blockedSince time.Time
}

func (w *browserWaiter) blockedBy(phase string, now time.Time) {
	if phase == w.blocked {
		return
	}
	if w.blocked != "" {
		w.timings.add(w.blocked, now.Sub(w.blockedSince))
	}
	w.blocked, w.blockedSince = phase, now
}

// All required browser resources are reserved together. A waiter never owns
// a site's account capacity while it is still waiting for a global slot.
func (q *Queue) acquireBrowserResources(ctx context.Context, site, job string, timings *stageTimings) (func(), bool) {
	w := &browserWaiter{ctx: ctx, site: site, job: job, gate: q.siteGate(site),
		local: !usesAgentBay() && !doubaoUsesVolcengine() && q.Sess != nil,
		cloud: usesAgentBay() && q.cloudSem != nil, ready: make(chan struct{}), timings: timings}
	q.browserMu.Lock()
	q.browserWaiters = append(q.browserWaiters, w)
	q.dispatchBrowsersLocked()
	if len(q.browserWaiters) > 0 && !q.browserPolling {
		q.browserPolling = true
		go q.pollBrowserWaiters()
	}
	q.browserMu.Unlock()
	select {
	case <-w.ready:
		if ctx.Err() != nil {
			w.release()
			return nil, false
		}
		finish := q.beginPlatformActivity(job)
		return func() { w.release(); finish() }, true
	case <-ctx.Done():
		q.browserMu.Lock()
		granted, release := w.granted, w.release
		if !granted {
			q.removeBrowserWaiterLocked(w)
			w.blockedBy("", time.Now())
			q.dispatchBrowsersLocked()
		}
		q.browserMu.Unlock()
		if granted {
			release()
		}
		return nil, false
	}
}

// Capacity may change after a login or after a legacy caller releases a slot.
// One short-lived poller per queue supplements immediate release wakeups.
func (q *Queue) pollBrowserWaiters() {
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for range ticker.C {
		q.browserMu.Lock()
		q.dispatchBrowsersLocked()
		if len(q.browserWaiters) == 0 {
			q.browserPolling = false
			q.browserMu.Unlock()
			return
		}
		q.browserMu.Unlock()
	}
}

func (q *Queue) removeBrowserWaiterLocked(w *browserWaiter) {
	for i, pending := range q.browserWaiters {
		if pending == w {
			q.browserWaiters = append(q.browserWaiters[:i], q.browserWaiters[i+1:]...)
			return
		}
	}
}

func (q *Queue) browserPoolLimit(site string) int {
	limit := len(q.loggedInAccounts(site))
	if limit < 1 {
		limit = 1 // Retain one bounded lazy login check.
	}
	if max := cap(q.siteSem); max > 0 && limit > max {
		limit = max
	}
	return limit
}

func (q *Queue) browserBlockedBy(w *browserWaiter) string {
	if w.job != "" && q.deploymentHolds(w.job) {
		return "deployment_wait_ms"
	}
	if w.local {
		limit := q.browserPoolLimit(w.site)
		q.mu.Lock()
		busy := q.poolActive[w.site] >= limit
		q.mu.Unlock()
		if busy {
			return "pool_wait_ms"
		}
	}
	if len(w.gate) >= cap(w.gate) {
		return "site_gate_wait_ms"
	}
	if w.cloud && len(q.cloudSem) >= cap(q.cloudSem) {
		return "cloud_slot_wait_ms"
	}
	if q.siteSem == nil || len(q.siteSem) >= cap(q.siteSem) {
		return "web_slot_wait_ms"
	}
	return ""
}

func (q *Queue) dispatchBrowsersLocked() {
	for {
		var chosen *browserWaiter
		bestRank := 5
		now := time.Now()
		for _, w := range q.browserWaiters {
			if w.ctx.Err() != nil {
				continue
			}
			blocked := q.browserBlockedBy(w)
			w.blockedBy(blocked, now)
			if blocked != "" {
				continue
			}
			// FIFO within each job/site, and alternate eligible jobs/sites
			// across grants so a six-platform fanout cannot monopolize turns.
			rank := 0
			if w.site == q.browserLastSite {
				rank += 2
			}
			if w.job == q.browserLastJob {
				rank++
			}
			if rank < bestRank {
				chosen, bestRank = w, rank
			}
		}
		chosen = q.chooseFairBrowserWaiterLocked(chosen, now)
		if chosen == nil {
			return
		}
		release, ok := q.reserveBrowserResources(chosen)
		if !ok {
			return // External semaphore owners may have raced the readiness check.
		}
		q.removeBrowserWaiterLocked(chosen)
		q.fairBrowserGrantedLocked(chosen)
		chosen.blockedBy("", time.Now())
		chosen.granted, chosen.release = true, release
		q.browserLastSite, q.browserLastJob = chosen.site, chosen.job
		close(chosen.ready)
	}
}

func tryBrowserSlot(slot chan struct{}) bool {
	select {
	case slot <- struct{}{}:
		return true
	default:
		return false
	}
}

func (q *Queue) reserveBrowserResources(w *browserWaiter) (func(), bool) {
	limit := 0
	if w.local {
		limit = q.browserPoolLimit(w.site)
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if w.ctx.Err() != nil || w.local && q.poolActive[w.site] >= limit {
		return nil, false
	}
	if !tryBrowserSlot(w.gate) {
		return nil, false
	}
	if w.cloud && !tryBrowserSlot(q.cloudSem) {
		<-w.gate
		return nil, false
	}
	if !tryBrowserSlot(q.siteSem) {
		if w.cloud {
			<-q.cloudSem
		}
		<-w.gate
		return nil, false
	}
	permit := browserPermitFrom(w.ctx)
	if permit != nil {
		permit.grant()
	}
	if w.local {
		if q.poolActive == nil {
			q.poolActive = map[string]int{}
			q.poolChanged = make(chan struct{})
		}
		q.poolActive[w.site]++
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			q.mu.Lock()
			if w.local {
				q.poolActive[w.site]--
				if q.poolChanged != nil {
					close(q.poolChanged)
				}
				q.poolChanged = make(chan struct{})
			}
			if permit != nil {
				permit.release()
			} else {
				<-q.siteSem
			}
			if w.cloud {
				<-q.cloudSem
			}
			<-w.gate
			q.mu.Unlock()
			q.browserMu.Lock()
			q.dispatchBrowsersLocked()
			q.browserMu.Unlock()
			q.wakeFairResourceWaiters()
		})
	}, true
}
