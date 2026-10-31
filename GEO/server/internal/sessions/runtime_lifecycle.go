package sessions

import (
	"context"
	"log"
	"os"
	"strconv"
	"strings"
	"time"
)

func pendingLoginStatus(status string) bool {
	switch status {
	case "logging_in", "sending", "waiting_sms", "need_captcha", "submitting", "login_form", "recovery_pending":
		return true
	}
	return false
}

func pendingLoginGrace() time.Duration { return 2 * time.Minute }

func (m *Manager) pendingLoginActive(at time.Time, grace time.Duration) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	for site, st := range m.states {
		if st == nil || !pendingLoginStatus(st.Status) {
			continue
		}
		since := m.pendingSince[site]
		if since.IsZero() {
			// Older runtimes and tests may predate transition tracking.
			since, _ = time.ParseInLocation("2006-01-02T15:04:05", st.UpdatedAt, time.Local)
		}
		if !since.IsZero() && at.Sub(since) < grace {
			return true
		}
	}
	return false
}

func (p *Pool) reapIdleRuntimes() {
	p.mu.Lock()
	if p.stopping {
		p.mu.Unlock()
		return
	}
	copy := make(map[string]*Manager, len(p.runtimes))
	for id, m := range p.runtimes {
		copy[id] = m
	}
	p.mu.Unlock()
	for id, m := range copy {
		p.stopIfCold(id, m)
	}
}

func (p *Pool) reapLoop() {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		p.mu.Lock()
		stopped := p.stopping
		p.mu.Unlock()
		if stopped {
			return
		}
		p.reapIdleRuntimes()
	}
}

// Uncertain profiles get one bounded automatic check per five minutes. Admin
// checks bypass this backoff so an operator can validate a new login at once.
func (p *Pool) claimUnknownProbe(id, site string, at time.Time) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.probeAfter == nil {
		p.probeAfter = map[string]time.Time{}
	}
	key := id + "/" + site
	if at.Before(p.probeAfter[key]) {
		return false
	}
	p.probeAfter[key] = at.Add(5 * time.Minute)
	return true
}

func (p *Pool) clearUnknownProbe(id, site string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.probeAfter, id+"/"+site)
}

func needsAutoRelogin(status string) bool {
	switch status {
	case "", "not_logged_in", "expired", "login_form", "error", "idle", "unknown":
		return true
	default:
		return false
	}
}

func (p *Pool) reloginLoop() {
	timer := time.NewTimer(20 * time.Second)
	defer timer.Stop()
	ticker := time.NewTicker(25 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-timer.C:
		case <-ticker.C:
		}
		p.mu.Lock()
		stopped := p.stopping
		p.mu.Unlock()
		if stopped {
			return
		}
		p.reloginOnce()
	}
}

func browsersTooBusy() bool {
	raw, err := os.ReadFile("/sys/fs/cgroup/geo-browsers/memory.current")
	if err != nil {
		return false
	}
	n, err := strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return false
	}
	return n > 1200*1024*1024
}

func autoLoginBrowserLimit() int {
	n, err := strconv.Atoi(os.Getenv("GEO_SITE_CONCURRENCY"))
	if err != nil || n < 1 || n > 6 {
		return 2
	}
	return n
}

func (p *Pool) autoLoginShouldDefer(accountID string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.stopping || (accountID != "" && p.runtimes[accountID] == nil && len(p.runtimes) >= autoLoginBrowserLimit()) {
		return true
	}
	for _, m := range p.runtimes {
		if m != nil && m.Busy() {
			return true
		}
	}
	return false
}

func (p *Pool) automaticLoginStillNeeded(accountID, siteID string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	a := p.accounts[accountID]
	return a != nil && a.Enabled && p.workflowOwners[accountID] == "" && needsAutoRelogin(a.Sites[siteID].Status)
}

func (p *Pool) assistFreshLoginChallenge(id, site string, st *State) {
	if st == nil || st.Status != "need_captcha" || p.autoLoginShouldDefer(id) {
		return
	}
	p.mu.Lock()
	handler := p.loginChallengeAssist
	a := p.accounts[id]
	current := a != nil && a.Enabled && a.Sites[site].Status == "need_captcha"
	p.mu.Unlock()
	if handler != nil && current {
		if err := handler(id, site); err != nil {
			log.Printf("auto login captcha assist account=%s site=%s deferred: %v", id, site, err)
		}
	}
}

func (p *Pool) reloginOnce() {
	// Background recovery has no customer lease in the queue's global gate.
	// Leave its browser and memory capacity available while real work runs.
	if p.autoLoginShouldDefer("") || browsersTooBusy() {
		return
	}
	type job struct {
		id, site, status string
	}
	p.mu.Lock()
	live := map[string]bool{}
	for id, m := range p.runtimes {
		if m != nil {
			live[id] = true
		}
	}
	browserN := len(live)
	blocked := map[string]bool{}
	recent := map[string]bool{}
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled {
			continue
		}
		for _, st := range a.Sites {
			if st.LastSendAt > 0 && time.Since(time.Unix(int64(st.LastSendAt), 0)) < 5*time.Minute {
				recent[a.Phone] = true
			}
			if !live[id] {
				continue
			}
			switch st.Status {
			case "waiting_sms", "sending", "submitting", "logging_in", "need_captcha":
				blocked[a.Phone] = true
			}
		}
	}
	var jobs []job
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled || p.workflowOwners[id] != "" || blocked[a.Phone] || recent[a.Phone] {
			continue
		}
		for _, site := range Sites {
			st, ok := a.Sites[site.ID]
			status := ""
			if ok {
				status = st.Status
			}
			// A question-time challenge is not proof of an expired login.
			// Reopening the homepage and seeing a logged-in header cannot
			// establish that a person completed that challenge.
			if !needsAutoRelogin(status) {
				continue
			}
			if ok && st.LastSendAt > 0 && time.Since(time.Unix(int64(st.LastSendAt), 0)) < 5*time.Minute {
				continue
			}
			jobs = append(jobs, job{id: id, site: site.ID, status: status})
		}
	}
	p.mu.Unlock()
	if len(jobs) == 0 {
		return
	}
	if p.autoLoginShouldDefer("") || browsersTooBusy() {
		log.Printf("auto relogin deferred jobs=%d browsers=%d", len(jobs), browserN)
		return
	}
	var item job
	found := false
	now := time.Now()
	for _, candidate := range jobs {
		if p.autoLoginShouldDefer(candidate.id) || !p.automaticLoginStillNeeded(candidate.id, candidate.site) {
			continue
		}
		if p.claimUnknownProbe(candidate.id, candidate.site, now) {
			item = candidate
			found = true
			break
		}
	}
	if !found {
		return
	}
	if !p.automaticLoginStillNeeded(item.id, item.site) {
		return
	}
	log.Printf("auto relogin account=%s site=%s from=%s", item.id, item.site, item.status)
	if item.status == "" || item.status == "unknown" || item.status == "error" || item.status == "idle" {
		ctx, cancel := context.WithTimeout(context.Background(), pageOpenBudget()+15*time.Second)
		st, err := p.checkContext(ctx, item.id, item.site, true)
		cancel()
		if err != nil {
			log.Printf("auto relogin check account=%s site=%s: %v", item.id, item.site, err)
			return
		}
		if st == nil || !needsAutoRelogin(st.Status) {
			if st != nil && (st.Status == "need_captcha" || st.Status == "waiting_sms") {
				p.mu.Lock()
				if m := p.runtimes[item.id]; m != nil {
					m.KeepWarmFor(pendingLoginGrace())
				}
				p.mu.Unlock()
			}
			if st != nil {
				log.Printf("auto relogin account=%s site=%s now=%s", item.id, item.site, st.Status)
				p.assistFreshLoginChallenge(item.id, item.site, st)
			}
			return
		}
	}
	if p.autoLoginShouldDefer(item.id) || browsersTooBusy() || !p.automaticLoginStillNeeded(item.id, item.site) {
		return
	}
	st, err := p.sendCode(item.id, item.site, false, pendingLoginGrace())
	if err != nil {
		log.Printf("auto relogin send account=%s site=%s: %v", item.id, item.site, err)
		return
	}
	if st != nil {
		log.Printf("auto relogin account=%s site=%s status=%s", item.id, item.site, st.Status)
		p.assistFreshLoginChallenge(item.id, item.site, st)
	}
}
