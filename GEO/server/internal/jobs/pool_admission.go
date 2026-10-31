package jobs

import (
	"context"
	"sync"
	"time"
)

// Wait outside the global browser gate when this site's usable accounts are
// already occupied. Capacity is rechecked so newly logged-in accounts can join
// without replacing a semaphore underneath its existing waiters.
func (q *Queue) acquirePoolCapacity(ctx context.Context, site string) (func(), bool) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		if ctx.Err() != nil {
			return nil, false
		}
		limit := len(q.loggedInAccounts(site))
		if limit < 1 {
			limit = 1 // Permit one lazy login-state check.
		}
		if max := cap(q.siteSem); max > 0 && limit > max {
			limit = max
		}
		q.mu.Lock()
		if q.poolActive == nil {
			q.poolActive = make(map[string]int)
			q.poolChanged = make(chan struct{})
		}
		if q.poolActive[site] < limit {
			q.poolActive[site]++
			q.mu.Unlock()
			var once sync.Once
			return func() {
				once.Do(func() {
					q.mu.Lock()
					q.poolActive[site]--
					close(q.poolChanged)
					q.poolChanged = make(chan struct{})
					q.mu.Unlock()
				})
			}, true
		}
		changed := q.poolChanged
		q.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, false
		case <-changed:
		case <-ticker.C:
		}
	}
}
