package sessions

import (
	"context"
	"errors"
)

var ErrSMSGenerationChanged = errors.New("SMS generation or original target changed")

type SMSCallbackContext struct {
	SentAt       float64
	TargetID     string
	SentTargetID string
	Status       string
}

// SMSContext never creates a browser or navigates a page.
func (p *Pool) SMSContext(account, site string) SMSCallbackContext {
	p.mu.Lock()
	a, m := p.accounts[account], p.runtimes[account]
	enabled := a != nil && a.Enabled
	var current SMSCallbackContext
	if enabled {
		state := a.Sites[site]
		current.SentAt, current.Status = state.LastSendAt, state.Status
		current.SentTargetID = state.LastSendTargetID
	}
	p.mu.Unlock()
	if !enabled || m == nil {
		return current
	}
	m.mu.Lock()
	if state := m.states[site]; state != nil {
		current.SentAt, current.Status = state.LastSendAt, state.Status
		current.SentTargetID = state.LastSendTargetID
	}
	m.mu.Unlock()
	current.TargetID = managerSiteTargetID(m, site)
	return current
}

func callbackContextMatches(m *Manager, site string, expected SMSCallbackContext) bool {
	if expected.SentAt <= 0 || expected.TargetID == "" || managerSiteTargetID(m, site) != expected.TargetID {
		return false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	state := m.states[site]
	return state != nil && state.LastSendAt == expected.SentAt && state.LastSendTargetID == expected.TargetID
}

// SubmitCallbackCode holds the original site's operation guard and checks the
// sending generation again after admission. Callback retries never open a new
// page, send an SMS, or fill an OTP into a replacement target.
func (p *Pool) SubmitCallbackCode(account, site, code string, expected SMSCallbackContext) (*State, error) {
	p.mu.Lock()
	a, m := p.accounts[account], p.runtimes[account]
	enabled := a != nil && a.Enabled && !p.stopping
	p.mu.Unlock()
	if !enabled || m == nil {
		return nil, ErrExistingRecoveryMissing
	}
	if release, recovering := beginRecoverySMSAction(m, site); recovering {
		if release == nil {
			return nil, ErrRecoverySMSNotReady
		}
		defer release()
		if !callbackContextMatches(m, site, expected) {
			return nil, ErrSMSGenerationChanged
		}
		state, err := m.submitRecoveryCode(site, code)
		p.syncOne(account)
		return state, err
	}
	select {
	case p.opSem <- struct{}{}:
		defer func() { <-p.opSem }()
	default:
		return nil, ErrRecoverySMSNotReady
	}
	lease, err := p.TryAcquireExistingForRecoveryContext(context.Background(), site, account)
	if err != nil {
		return nil, err
	}
	defer lease.Release()
	if !callbackContextMatches(lease.Manager, site, expected) {
		return nil, ErrSMSGenerationChanged
	}
	state, err := lease.Manager.submitRecoveryCode(site, code)
	p.syncOne(account)
	return state, err
}
