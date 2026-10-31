package jobs

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// An account whose platform quota is exhausted or that the platform muted
// stays logged in, so the pool would keep handing it out. Skip it for that
// site until the stated end time, or the quota cooldown when none is given.
func (q *Queue) blockAccount(site, accountID, message string) {
	if accountID == "" {
		return
	}
	q.accountBlockMu.Lock()
	defer q.accountBlockMu.Unlock()
	limit := q.recordAccountLimitLocked(site, accountID, message, time.Now(), "live")
	until := limit.NextProbeAt
	key := internalSiteID(site) + "/" + accountID
	if q.accountBlocks == nil {
		q.accountBlocks = make(map[string]time.Time)
	}
	if until.After(q.accountBlocks[key]) {
		q.accountBlocks[key] = until
	}
	q.saveAccountProtectionLocked()
	log.Printf("account blocked site=%s account=%s until=%s", internalSiteID(site), accountID, until.Format(time.RFC3339))
}

func (q *Queue) blockedAccounts(site string) []string {
	prefix := internalSiteID(site) + "/"
	now := time.Now()
	q.accountBlockMu.Lock()
	defer q.accountBlockMu.Unlock()
	var ids []string
	for key, until := range q.accountBlocks {
		if !now.Before(until) {
			delete(q.accountBlocks, key)
			continue
		}
		if len(key) > len(prefix) && key[:len(prefix)] == prefix {
			ids = append(ids, key[len(prefix):])
		}
	}
	for key := range q.recoveryOwners {
		if strings.HasPrefix(key, prefix) {
			id := strings.TrimPrefix(key, prefix)
			found := false
			for _, old := range ids {
				if old == id {
					found = true
					break
				}
			}
			if !found {
				ids = append(ids, id)
			}
		}
	}
	return ids
}

type accountProtectionState struct {
	BlockedUntil  map[string]time.Time    `json:"blocked_until"`
	RecoveryOwner map[string]string       `json:"recovery_owner"`
	Limits        map[string]AccountLimit `json:"limits"`
}

func (q *Queue) protectionPath() string {
	return filepath.Join(q.DataDir, "account-protection", "state.json")
}

func (q *Queue) loadAccountBlocks() {
	if q.DataDir == "" {
		return
	}
	var state accountProtectionState
	if raw, err := os.ReadFile(q.protectionPath()); err == nil {
		if json.Unmarshal(raw, &state) != nil {
			log.Printf("account protection state unreadable; preserving file")
			return
		}
	}
	q.accountBlocks = state.BlockedUntil
	q.accountLimits = state.Limits
	q.recoveryOwners = make(map[string]string)
	for key, owner := range state.RecoveryOwner {
		if j := q.jobs[owner]; j != nil && (j.Status == "queued" || j.Status == "running") {
			q.recoveryOwners[key] = owner
		}
	}
	if state.Limits == nil {
		q.backfillAccountLimits()
		q.saveAccountProtectionLocked()
	}
}

// Caller holds accountBlockMu. IDs and expiry times only; no credentials.
func (q *Queue) saveAccountProtectionLocked() {
	if q.DataDir == "" {
		return
	}
	raw, err := json.Marshal(accountProtectionState{BlockedUntil: q.accountBlocks, RecoveryOwner: q.recoveryOwners, Limits: q.accountLimits})
	if err != nil {
		return
	}
	if err = os.MkdirAll(filepath.Dir(q.protectionPath()), 0700); err != nil {
		return
	}
	path := q.protectionPath()
	if err = os.WriteFile(path+".tmp", raw, 0600); err == nil {
		err = os.Rename(path+".tmp", path)
	}
	if err != nil {
		log.Printf("account protection persist: %v", err)
	}
}

func (q *Queue) coolAccount(site, account, code string, duration time.Duration) {
	if account == "" || duration <= 0 {
		return
	}
	q.coolAccountUntil(site, account, code, time.Now().Add(duration))
}

func (q *Queue) coolAccountUntil(site, account, code string, until time.Time) {
	if account == "" || !until.After(time.Now()) {
		return
	}
	key := internalSiteID(site) + "/" + account
	q.accountBlockMu.Lock()
	defer q.accountBlockMu.Unlock()
	if q.accountBlocks == nil {
		q.accountBlocks = make(map[string]time.Time)
	}
	if !until.After(q.accountBlocks[key]) {
		return
	}
	q.accountBlocks[key] = until
	q.saveAccountProtectionLocked()
	log.Printf("account cooldown site=%s account=%s code=%s until=%s", internalSiteID(site), account, code, until.Format(time.RFC3339))
}

func (q *Queue) clearAccountBlock(site, account string) {
	q.accountBlockMu.Lock()
	defer q.accountBlockMu.Unlock()
	key := internalSiteID(site) + "/" + account
	changed := false
	if _, ok := q.accountBlocks[key]; ok {
		delete(q.accountBlocks, key)
		changed = true
	}
	if limit, ok := q.accountLimits[key]; ok && limit.RecoveredAt.IsZero() {
		limit.RecoveredAt = time.Now()
		q.accountLimits[key] = limit
		changed = true
	}
	if changed {
		q.saveAccountProtectionLocked()
	}
}

func (q *Queue) withoutBlockedAccounts(site string, ids []string) []string {
	blocked := make(map[string]bool)
	for _, id := range q.blockedAccounts(site) {
		blocked[id] = true
	}
	var out []string
	for _, id := range ids {
		if !blocked[id] {
			out = append(out, id)
		}
	}
	return out
}

func (q *Queue) reserveRecoveryTarget(site, account, job string) bool {
	q.accountBlockMu.Lock()
	defer q.accountBlockMu.Unlock()
	if q.recoveryOwners == nil {
		q.recoveryOwners = make(map[string]string)
	}
	key := internalSiteID(site) + "/" + account
	if owner := q.recoveryOwners[key]; owner != "" && owner != job {
		return false
	}
	if q.recoveryOwners[key] == job {
		return true
	}
	q.recoveryOwners[key] = job
	q.saveAccountProtectionLocked()
	return true
}

func (q *Queue) releaseRecoveryTargets(job, site string) {
	q.accountBlockMu.Lock()
	changed := false
	var released [][2]string
	for key, owner := range q.recoveryOwners {
		parts := strings.SplitN(key, "/", 2)
		if owner == job && len(parts) == 2 && (site == "" || parts[0] == internalSiteID(site)) {
			delete(q.recoveryOwners, key)
			changed = true
			released = append(released, [2]string{parts[0], parts[1]})
		}
	}
	if changed {
		q.saveAccountProtectionLocked()
	}
	q.accountBlockMu.Unlock()
	if pool, ok := q.Sess.(interface{ ClearRecoveryPending(string, string) }); ok {
		for _, pair := range released {
			pool.ClearRecoveryPending(pair[1], pair[0])
		}
	}
}
