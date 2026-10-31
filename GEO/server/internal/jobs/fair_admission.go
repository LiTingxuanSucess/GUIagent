package jobs

import (
	"context"
	"sort"
	"time"
)

type fairAdmissionKey struct{ job, site string }
type fairAdmissionContextKey struct{}

// A ticket is scheduling intent, never an offline browser/account permit.
// browserMu protects its lifecycle; its immutable age is independent of the
// order that channel send goroutines or subsequent worker rounds arrive.
type fairAdmissionTicket struct {
	key                fairAdmissionKey
	since              time.Time
	sequence           uint64
	gate               chan struct{}
	local, cloud, live bool
	cancel             context.CancelFunc
	handoffUntil       time.Time
	workerHandedOff    bool
}

func resourceAdmissionResult(row map[string]any) bool {
	return row != nil && !validDelivery(row) && stringField(row, "error_code") == "browser_admission_timeout" && row["web_skipped"] == true && !deliveryPermanentAPIRejection(row)
}

func onlyResourceAdmissionPending(rows []map[string]any, sites []string) bool {
	missing := false
	for _, site := range sites {
		var row map[string]any
		for _, candidate := range rows {
			if internalSiteID(stringField(candidate, "site_id")) == internalSiteID(site) {
				row = candidate
				break
			}
		}
		if validDelivery(row) {
			continue
		}
		if !resourceAdmissionResult(row) {
			return false
		}
		missing = true
	}
	return missing
}

func resourceAdmissionRetryDelay() time.Duration {
	return time.Duration(envInt("GEO_DELIVERY_RESOURCE_RETRY_SEC", 2, 1, 15)) * time.Second
}

func fairHandoffGrace() time.Duration {
	return time.Duration(envInt("GEO_DELIVERY_HANDOFF_MS", 1000, 50, 2000)) * time.Millisecond
}

func (q *Queue) fairTicketFor(site, job string) *fairAdmissionTicket {
	site = internalSiteID(site)
	since := time.Now()
	q.mu.Lock()
	if current := q.jobs[job]; current != nil {
		var previous map[string]any
		for _, row := range current.Results {
			if internalSiteID(stringField(row, "site_id")) == site {
				previous = row
				break
			}
		}
		// Restore old unexecuted admission waits from durable request age.
		// A real platform failure begins a fresh admission turn instead.
		if previous == nil || resourceAdmissionResult(previous) && !hasActualWebEvidence(previous) && previous["attempts_truncated"] != true && evidenceCount(previous["attempts_dropped"]) == 0 {
			if created, err := time.Parse(time.RFC3339Nano, current.CreatedAt); err == nil && created.Before(since) {
				since = created
			}
		}
	}
	q.mu.Unlock()
	gate := q.siteGate(site)
	q.browserMu.Lock()
	defer q.browserMu.Unlock()
	key := fairAdmissionKey{job, site}
	if ticket := q.fairTickets[key]; ticket != nil {
		return ticket
	}
	if q.fairTickets == nil {
		q.fairTickets = map[fairAdmissionKey]*fairAdmissionTicket{}
	}
	q.fairSequence++
	ticket := &fairAdmissionTicket{key: key, since: since, sequence: q.fairSequence, gate: gate,
		local: !usesAgentBay() && !doubaoUsesVolcengine() && q.Sess != nil, cloud: usesAgentBay() && q.cloudSem != nil}
	q.fairTickets[key] = ticket
	return ticket
}

func hasActualWebEvidence(row map[string]any) bool {
	for _, entry := range cloneAttemptList(row["attempts"]) {
		if entry["channel"] == "web" && entry["skipped"] != true {
			return true
		}
	}
	return false
}

func (q *Queue) acquireDeliveryBrowserResources(parent, admission context.Context, site, job string, timings *stageTimings) (func(), bool) {
	if !isEventualDelivery(parent) {
		return q.acquireBrowserResources(admission, site, job, timings)
	}
	ticket := q.fairTicketFor(site, job)
	ctx, cancel := context.WithCancel(admission)
	q.browserMu.Lock()
	if ticket.live {
		q.browserMu.Unlock()
		cancel()
		return nil, false
	}
	ticket.live, ticket.cancel, ticket.workerHandedOff = true, cancel, false
	q.browserMu.Unlock()
	defer func() {
		cancel()
		q.browserMu.Lock()
		if q.fairTickets[ticket.key] == ticket {
			ticket.live, ticket.cancel = false, nil
			if parent.Err() != nil {
				delete(q.fairTickets, ticket.key)
			}
		}
		q.browserMu.Unlock()
	}()
	return q.acquireBrowserResources(context.WithValue(ctx, fairAdmissionContextKey{}, ticket), site, job, timings)
}

func ticketOlder(a, b *fairAdmissionTicket) bool {
	return a.since.Before(b.since) || a.since.Equal(b.since) && a.sequence < b.sequence
}

func (q *Queue) pruneFairTicketsLocked() {
	q.mu.Lock()
	defer q.mu.Unlock()
	for key, ticket := range q.fairTickets {
		job := q.jobs[key.job]
		remove := job == nil || job.DeliveryPolicy != "eventual" || job.Status != "queued" && job.Status != "running"
		if !remove {
			requested := false
			for _, site := range job.Sites {
				if internalSiteID(site) == key.site {
					requested = true
					break
				}
			}
			remove = !requested
		}
		if !remove {
			for _, row := range job.Results {
				if internalSiteID(stringField(row, "site_id")) == key.site && validDelivery(row) {
					remove = true
					break
				}
			}
		}
		if remove {
			if ticket.cancel != nil {
				ticket.cancel()
			}
			delete(q.fairTickets, key)
		}
	}
}

// Keep the old alternation chooser for legacy-only traffic. With tickets,
// grant the oldest currently eligible live turn; blocked sites cannot hold
// unrelated sites behind them. An offline older turn gets one bounded wake
// grace, then ceases to block active callers until it re-registers.
func (q *Queue) chooseFairBrowserWaiterLocked(legacy *browserWaiter, now time.Time) *browserWaiter {
	if len(q.fairTickets) == 0 {
		return legacy
	}
	q.pruneFairTicketsLocked()
	chosen := legacy
	var best *fairAdmissionTicket
	bestRank := 5
	for _, waiter := range q.browserWaiters {
		ticket, _ := waiter.ctx.Value(fairAdmissionContextKey{}).(*fairAdmissionTicket)
		if ticket == nil || q.fairTickets[ticket.key] != ticket || waiter.ctx.Err() != nil || q.browserBlockedBy(waiter) != "" {
			continue
		}
		rank := 0
		if waiter.site == q.browserLastSite {
			rank += 2
		}
		if waiter.job == q.browserLastJob {
			rank++
		}
		if best == nil || ticket.since.Before(best.since) || ticket.since.Equal(best.since) && (rank < bestRank || rank == bestRank && ticket.sequence < best.sequence) {
			chosen, best, bestRank = waiter, ticket, rank
		}
	}
	var offline *fairAdmissionTicket
	for _, ticket := range q.fairTickets {
		if ticket.live || best != nil && !ticket.since.Before(best.since) {
			continue
		}
		if q.browserBlockedBy(&browserWaiter{site: ticket.key.site, gate: ticket.gate, local: ticket.local, cloud: ticket.cloud}) != "" {
			continue
		}
		q.mu.Lock()
		job := q.jobs[ticket.key.job]
		eligible := job != nil && (job.Status == "running" || job.Status == "queued") && deliveryResourceSiteDue(job, ticket.key.site, now)
		q.mu.Unlock()
		if eligible && (offline == nil || ticketOlder(ticket, offline)) {
			offline = ticket
		}
	}
	if offline != nil {
		if offline.handoffUntil.IsZero() {
			offline.handoffUntil = now.Add(fairHandoffGrace())
			go q.wakeFairJob(offline.key.job)
		}
		if now.Before(offline.handoffUntil) {
			return nil
		}
	}
	return chosen
}

func (q *Queue) fairBrowserGrantedLocked(waiter *browserWaiter) {
	if ticket, _ := waiter.ctx.Value(fairAdmissionContextKey{}).(*fairAdmissionTicket); ticket != nil {
		delete(q.fairTickets, ticket.key)
	}
}

func (q *Queue) wakeFairResourceWaiters() {
	q.browserMu.Lock()
	q.pruneFairTicketsLocked()
	ids := map[string]bool{}
	for _, ticket := range q.fairTickets {
		if !ticket.live && q.browserBlockedBy(&browserWaiter{site: ticket.key.site, gate: ticket.gate, local: ticket.local, cloud: ticket.cloud}) == "" {
			ids[ticket.key.job] = true
		}
	}
	q.browserMu.Unlock()
	for id := range ids {
		q.wakeFairJob(id)
	}
}

// Resource wakeups bypass capacity retry delay only. Retain an active-round
// wake until after it has persisted queued and relinquished activeRuns.
func (q *Queue) wakeFairJob(id string) {
	q.mu.Lock()
	job := q.jobs[id]
	if job == nil || job.DeliveryPolicy != "eventual" {
		q.mu.Unlock()
		return
	}
	if q.activeRuns[id] || job.Status == "running" {
		if q.fairWakePending == nil {
			q.fairWakePending = map[string]bool{}
		}
		q.fairWakePending[id] = true
		q.mu.Unlock()
		return
	}
	if job.Status != "queued" || !hasPendingDeliveryResources(job) {
		q.mu.Unlock()
		return
	}
	changed := wakeDeliveryResourceSites(job, time.Now())
	if changed {
		job.UpdatedAt = time.Now().Format(time.RFC3339Nano)
	}
	saved := copyJob(job)
	q.mu.Unlock()
	if changed {
		q.publish(id)
		q.save(saved)
	}
	q.enqueueJob(id, time.Now())
}

func (q *Queue) finishFairRun(id string) {
	q.mu.Lock()
	wake := q.fairWakePending[id]
	delete(q.fairWakePending, id)
	q.mu.Unlock()
	q.browserMu.Lock()
	q.pruneFairTicketsLocked()
	q.browserMu.Unlock()
	if wake {
		q.wakeFairJob(id)
	}
}

func (q *Queue) restoreFairAdmissionTickets() {
	q.mu.Lock()
	var keys []fairAdmissionKey
	for id, job := range q.jobs {
		if job.DeliveryPolicy == "eventual" && (job.Status == "queued" || job.Status == "running") {
			for _, row := range job.Results {
				if resourceAdmissionResult(row) {
					keys = append(keys, fairAdmissionKey{id, internalSiteID(stringField(row, "site_id"))})
				}
			}
		}
	}
	q.mu.Unlock()
	for _, key := range keys {
		q.fairTicketFor(key.site, key.job)
	}
}

// A woken old job may be behind newer goroutine channel sends. Transfer the
// popped token back to its owner before serving the oldest due resource wait.
// Stale tokens for a selected job are harmless: run's atomic guard still owns
// the execution claim, and no channel send occurs under either queue lock.
func (q *Queue) prioritizeFairQueuedJob(popped string) string {
	q.browserMu.Lock()
	q.pruneFairTicketsLocked()
	var eligible []*fairAdmissionTicket
	for _, ticket := range q.fairTickets {
		if ticket.live || ticket.workerHandedOff {
			continue
		}
		blocked := q.browserBlockedBy(&browserWaiter{site: ticket.key.site, gate: ticket.gate, local: ticket.local, cloud: ticket.cloud})
		// A globally full browser queue is exactly where an older ready
		// platform must register its live waiter before the next release.
		// Account/site/cloud waits still cannot consume this worker handoff.
		if blocked == "" || blocked == "web_slot_wait_ms" {
			eligible = append(eligible, ticket)
		}
	}
	q.mu.Lock()
	original := q.jobs[popped]
	if original == nil || original.Status != "queued" || original.DeliveryPolicy != "eventual" {
		q.mu.Unlock()
		q.browserMu.Unlock()
		return popped
	}
	var ordered []*fairAdmissionTicket
	for _, ticket := range eligible {
		job := q.jobs[ticket.key.job]
		if !ticket.live && job != nil && job.Status == "queued" && !q.activeRuns[job.ID] && deliveryResourceSiteDue(job, ticket.key.site, time.Now()) {
			ordered = append(ordered, ticket)
		}
	}
	sort.Slice(ordered, func(i, j int) bool { return ticketOlder(ordered[i], ordered[j]) })
	chosen := popped
	if len(ordered) > 0 {
		chosen = ordered[0].key.job
	}
	if chosen != popped {
		ordered[0].workerHandedOff = true
		delete(q.enqueued, popped)
	}
	q.mu.Unlock()
	q.browserMu.Unlock()
	if chosen != popped {
		q.enqueueJob(popped, time.Now())
	}
	return chosen
}
