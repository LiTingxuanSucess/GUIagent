package sessions

import (
	"context"
	"errors"
	"sync"

	"github.com/chromedp/chromedp"
)

// ExistingRecoveryAvailability describes an attached page without starting,
// navigating, refreshing, or waiting for a browser operation.
type ExistingRecoveryAvailability uint8

const (
	ExistingRecoveryMissing ExistingRecoveryAvailability = iota
	ExistingRecoveryBusy
	ExistingRecoveryReady
)

var (
	ErrExistingRecoveryBusy    = errors.New("existing recovery account is busy")
	ErrExistingRecoveryMissing = errors.New("existing recovery target is unavailable")
)

// InspectExistingForRecovery is an advisory, read-only admission check. A busy
// lock is uncertainty, not evidence that the original page has been lost.
func (p *Pool) InspectExistingForRecovery(siteID, accountID string) ExistingRecoveryAvailability {
	_, state, _ := p.existingRecoveryAdmission(context.Background(), siteID, accountID, false)
	return state
}

// TryAcquireExistingForRecoveryContext rechecks and claims the exact existing
// account/site without waiting. Callers can release global execution capacity
// immediately if another operation won the race after the advisory check.
func (p *Pool) TryAcquireExistingForRecoveryContext(parent context.Context, siteID, accountID string) (*Lease, error) {
	lease, state, err := p.existingRecoveryAdmission(parent, siteID, accountID, true)
	if err != nil {
		return nil, err
	}
	switch state {
	case ExistingRecoveryReady:
		return lease, nil
	case ExistingRecoveryBusy:
		return nil, ErrExistingRecoveryBusy
	default:
		return nil, ErrExistingRecoveryMissing
	}
}

func (p *Pool) existingRecoveryAdmission(parent context.Context, siteID, accountID string, claim bool) (*Lease, ExistingRecoveryAvailability, error) {
	if err := parent.Err(); err != nil {
		return nil, ExistingRecoveryBusy, err
	}
	if _, ok := SiteByID(siteID); !ok {
		return nil, ExistingRecoveryMissing, nil
	}
	if !p.mu.TryLock() {
		return nil, ExistingRecoveryBusy, nil
	}
	defer p.mu.Unlock()
	a := p.accounts[accountID]
	if a == nil || !a.Enabled || p.stopping {
		return nil, ExistingRecoveryMissing, nil
	}
	lk := p.locks[accountID]
	if claim && lk == nil {
		lk = p.accountLockLocked(accountID)
	}
	if lk != nil {
		if !lk.TryLock() {
			return nil, ExistingRecoveryBusy, nil
		}
		defer lk.Unlock()
	}
	m := p.runtimes[accountID]
	if m == nil {
		return nil, ExistingRecoveryMissing, nil
	}
	if !m.mu.TryLock() {
		return nil, ExistingRecoveryBusy, nil
	}
	defer m.mu.Unlock()
	if m.busy[siteID] {
		return nil, ExistingRecoveryBusy, nil
	}
	// Check the site mutex too: another operation may have acquired it but
	// not yet published its busy flag while waiting for the manager mutex.
	siteLock := m.siteMu[siteID]
	if siteLock != nil {
		if !siteLock.TryLock() {
			return nil, ExistingRecoveryBusy, nil
		}
	}
	retained := false
	defer func() {
		if siteLock != nil && !retained {
			siteLock.Unlock()
		}
	}()
	tab := m.tabs[siteID]
	if !m.ready || tab == nil || tab.Err() != nil {
		return nil, ExistingRecoveryMissing, nil
	}
	attached := chromedp.FromContext(tab)
	if attached == nil || attached.Target == nil || attached.Target.TargetID == "" {
		return nil, ExistingRecoveryMissing, nil
	}
	// CDPEndpoint only reads the local DevToolsActivePort file. No HTTP or
	// browser protocol operation runs before execution admission.
	if _, err := m.CDPEndpoint(); err != nil {
		return nil, ExistingRecoveryMissing, nil
	}
	if err := parent.Err(); err != nil {
		return nil, ExistingRecoveryBusy, err
	}
	if !claim {
		return nil, ExistingRecoveryReady, nil
	}
	if siteLock == nil {
		if m.siteMu == nil {
			m.siteMu = make(map[string]*sync.Mutex)
		}
		siteLock = &sync.Mutex{}
		m.siteMu[siteID] = siteLock
		siteLock.Lock() // A new, unpublished mutex cannot be contended.
	}
	if m.busy == nil {
		m.busy = make(map[string]bool)
	}
	m.busy[siteID] = true
	retained = true
	return &Lease{AccountID: accountID, Phone: a.Phone, Manager: m, pool: p, siteID: siteID}, ExistingRecoveryReady, nil
}
