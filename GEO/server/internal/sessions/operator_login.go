package sessions

import (
	"context"
	"errors"
	"fmt"
	"time"
)

var ErrOperatorLoginBusy = errors.New("login check resources busy")

// CheckWithLoginAssist retains the site lease through CLI verification. It
// never waits for a customer's lease or changes a retained question page.
func (p *Pool) CheckWithLoginAssist(id, site string, assist func(string, string) error) (*State, error) {
	if _, ok := SiteByID(site); !ok {
		return nil, fmt.Errorf("未知站点")
	}
	select {
	case p.opSem <- struct{}{}:
		defer func() { <-p.opSem }()
	default:
		return nil, ErrOperatorLoginBusy
	}
	p.mu.Lock()
	a := p.accounts[id]
	if a == nil || !a.Enabled || p.stopping {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号不可用")
	}
	if a.Sites[site].Status == "recovery_pending" || (p.runtimes[id] == nil && len(p.runtimes) >= autoLoginBrowserLimit()) {
		p.mu.Unlock()
		return nil, ErrOperatorLoginBusy
	}
	lk := p.accountLockLocked(id)
	p.mu.Unlock()
	if !lk.TryLock() {
		return nil, ErrOperatorLoginBusy
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	m, err := p.managerForLockedContext(ctx, id)
	if err != nil {
		lk.Unlock()
		cancel()
		return nil, err
	}
	if !m.TryAcquireSite(site) {
		lk.Unlock()
		cancel()
		return nil, ErrOperatorLoginBusy
	}
	lk.Unlock()
	defer func() {
		// An explicit login check owns the same SMS window as SendCode.
		// CLI verification can consume the short unattended grace period.
		retainOperatorLoginPage(m, site)
		p.syncOne(id)
		m.ReleaseSite(site)
		p.stopIfCold(id, m)
	}()
	err = m.EnsurePageContext(ctx, site)
	cancel()
	if err != nil {
		return nil, err
	}
	st := m.Refresh(site)
	for i := 0; i < 2 && (st.Status == "unknown" || st.Status == "idle"); i++ {
		time.Sleep(time.Second)
		st = m.Refresh(site)
	}
	// The record can have changed while its browser was starting.
	if st.Status == "recovery_pending" {
		return st, ErrOperatorLoginBusy
	}
	assists := 0
	runAssist := func() error {
		assists++
		if assist == nil {
			return fmt.Errorf("CLI 不可用")
		}
		lease := &Lease{AccountID: id, Manager: m, pool: p, siteID: site}
		guard, err := lease.BeginWebRecovery()
		if err != nil {
			return ErrOperatorLoginBusy
		}
		defer guard()
		cdp, err := m.CDPEndpoint()
		if err != nil {
			return err
		}
		target := managerSiteTargetID(m, site)
		if target == "" {
			return fmt.Errorf("当前登录页面不可用")
		}
		err = assist(cdp, target)
		st = m.Refresh(site)
		return err
	}
	if st.Status == "need_captcha" {
		if err := runAssist(); err != nil || st.Status == "need_captcha" {
			return st, err
		}
	}
	if operatorLoginReady(st) {
		return st, nil
	}
	st, err = m.sendCode(site, false, 20*time.Minute)
	if err != nil || st == nil || operatorLoginReady(st) {
		return st, err
	}
	if err := runAssist(); err != nil {
		return st, err
	}
	if operatorLoginReady(st) || st.Status == "need_captcha" {
		return st, nil
	}
	// The CLI may have opened the phone form. Sending remains a server
	// operation with the bound phone and the existing five-minute cooldown.
	st, err = m.sendCode(site, false, 20*time.Minute)
	if err == nil && st != nil && st.Status == "need_captcha" && assists < 2 {
		err = runAssist()
	}
	return st, err
}

func operatorLoginReady(st *State) bool {
	return st != nil && (st.Status == "logged_in" || st.Status == "waiting_sms" || st.Status == "submitting" || st.Status == "sending" || st.Status == "logging_in")
}

func retainOperatorLoginPage(m *Manager, site string) {
	m.mu.Lock()
	pending := m.states[site] != nil && pendingLoginStatus(m.states[site].Status)
	m.mu.Unlock()
	if pending {
		m.KeepWarmFor(20 * time.Minute)
	}
}
