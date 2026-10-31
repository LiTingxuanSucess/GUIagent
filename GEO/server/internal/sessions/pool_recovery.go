package sessions

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/chromedp"
)

var ErrRecoverySMSNotReady = fmt.Errorf("网页正在恢复，当前阶段未开放短信提交")

type recoveryTargetKey struct {
	manager *Manager
	site    string
}

type recoveryTargetState struct {
	recovering   bool
	closing      bool
	manual       int
	smsAllowed   int
	smsAdmission func() bool
	sms          sync.Mutex
}

// An uncertain sent question must not be replaced by a later customer's
// question while its original page waits for the recovery lane.
func (l *Lease) MarkRecoveryPending() {
	if l == nil || l.Manager == nil {
		return
	}
	l.Manager.set(l.siteID, "recovery_pending", "原问题页面等待恢复，暂不接收新问题")
	if l.pool != nil {
		l.pool.markState(l.AccountID, l.siteID, "recovery_pending", "原问题页面等待恢复，暂不接收新问题")
	}
}

func (p *Pool) ClearRecoveryPending(account, site string) {
	p.mu.Lock()
	m := p.runtimes[account]
	a := p.accounts[account]
	if a == nil || a.Sites[site].Status != "recovery_pending" {
		p.mu.Unlock()
		return
	}
	p.mu.Unlock()
	if m != nil {
		m.set(site, "unknown", "原问题已交付，等待重新核验登录")
	}
	p.markState(account, site, "unknown", "原问题已交付，等待重新核验登录")
}

var recoveryTargetMu sync.Mutex
var recoveryTargets = map[recoveryTargetKey]*recoveryTargetState{}

// RecoveryAccountIDs reports existing explicit recovery candidates without
// touching a browser or advancing normal lease selection.
func (p *Pool) RecoveryAccountIDs(siteID string) []string {
	return p.recoveryCandidates(siteID, nil)
}

// MarkRecoveryWaitingSMS routes an existing in-flight verification code to
// the leased account. The caller first proves the code field and sent/countdown
// state on the same target. This never sends a code or changes LastSendAt.
func managerSiteTargetID(m *Manager, siteID string) string {
	if m == nil {
		return ""
	}
	m.mu.Lock()
	tab := m.tabs[siteID]
	m.mu.Unlock()
	if tab == nil || tab.Err() != nil {
		return ""
	}
	attached := chromedp.FromContext(tab)
	if attached == nil || attached.Target == nil {
		return ""
	}
	return string(attached.Target.TargetID)
}

// HoldManualPage keeps the account's current site tab open for an operator
// action. It does not mark the page logged in. The caller must release.
func (p *Pool) HoldManualPage(id, siteID string) (cdp, targetID string, release func(), err error) {
	if _, ok := SiteByID(siteID); !ok {
		return "", "", nil, fmt.Errorf("未知站点")
	}
	m, err := p.acquireManagerSite(id, siteID, false)
	if err != nil {
		return "", "", nil, err
	}
	held := false
	defer func() {
		if !held {
			m.ReleaseSite(siteID)
			p.stopIfCold(id, m)
		}
	}()
	unlock, err := beginManualTargetAction(m, siteID)
	if err != nil {
		return "", "", nil, err
	}
	manualHeld := true
	defer func() {
		if manualHeld {
			unlock()
		}
	}()
	m.KeepWarmFor(15 * time.Minute)
	targetID = managerSiteTargetID(m, siteID)
	if targetID == "" {
		if err = m.EnsurePage(siteID); err != nil {
			return "", "", nil, err
		}
		targetID = managerSiteTargetID(m, siteID)
	}
	cdp, err = m.CDPEndpoint()
	if err != nil {
		return "", "", nil, err
	}
	if targetID == "" {
		return "", "", nil, fmt.Errorf("当前页还没有可操作的浏览器标签")
	}
	held = true
	manualHeld = false
	var once sync.Once
	release = func() {
		once.Do(func() {
			unlock()
			m.ReleaseSite(siteID)
			m.KeepWarmFor(10 * time.Minute)
			p.stopIfCold(id, m)
		})
	}
	return cdp, targetID, release, nil
}

func (l *Lease) SiteTargetID() string {
	if l == nil || l.Manager == nil {
		return ""
	}
	if l.workflowTargetID != "" {
		return l.workflowTargetID
	}
	l.Manager.mu.Lock()
	tab := l.Manager.tabs[l.siteID]
	l.Manager.mu.Unlock()
	if tab == nil || tab.Err() != nil {
		return ""
	}
	attached := chromedp.FromContext(tab)
	if attached == nil || attached.Target == nil {
		return ""
	}
	return string(attached.Target.TargetID)
}

func (l *Lease) MarkRecoveryWaitingSMS() {
	if l == nil || l.Manager == nil || !l.Manager.isBusy(l.siteID) {
		return
	}
	recoveryTargetMu.Lock()
	state := recoveryTargets[recoveryTargetKey{l.Manager, l.siteID}]
	allowed := state != nil && state.recovering && !state.closing
	recoveryTargetMu.Unlock()
	if !allowed {
		return
	}
	const message = "等待本次已发送的短信验证码回调，未重新发码"
	l.Manager.set(l.siteID, "waiting_sms", message)
	if l.pool != nil {
		l.pool.markState(l.AccountID, l.siteID, "waiting_sms", message)
	}
}

// BeginWebRecovery prevents screenshot-dialog clicks and drags from changing
// this exact target while CLI, independent verification and resume hold it.
// Read-only screenshots remain available to the operator.
func (l *Lease) BeginWebRecovery() (func(), error) {
	if l == nil || l.Manager == nil || !l.Manager.isBusy(l.siteID) {
		return nil, fmt.Errorf("恢复需要持有账号站点租约")
	}
	key := recoveryTargetKey{l.Manager, l.siteID}
	recoveryTargetMu.Lock()
	state := recoveryTargets[key]
	if state != nil && (state.recovering || state.manual > 0) {
		recoveryTargetMu.Unlock()
		return nil, fmt.Errorf("网页当前正在恢复或人工操作")
	}
	recoveryTargets[key] = &recoveryTargetState{recovering: true}
	recoveryTargetMu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			recoveryTargetMu.Lock()
			state := recoveryTargets[key]
			if state != nil {
				state.closing, state.smsAllowed = true, 0
			}
			recoveryTargetMu.Unlock()
			if state != nil {
				state.sms.Lock()
				recoveryTargetMu.Lock()
				if recoveryTargets[key] == state {
					delete(recoveryTargets, key)
				}
				recoveryTargetMu.Unlock()
				state.sms.Unlock()
			}
		})
	}, nil
}

// AllowRecoverySMS opens only the existing SMS callback while CLI is stopped.
// Closing the window also waits for an admitted callback to finish before the
// orchestrator independently probes/resumes the same leased target.
func (l *Lease) AllowRecoverySMS() (func(), error) {
	return l.AllowRecoverySMSWithAdmission(nil)
}

// A yielded browser execution slot must be reclaimed before the SMS callback
// touches the retained page. The callback stays serialized with close/probe.
func (l *Lease) AllowRecoverySMSWithAdmission(admit func() bool) (func(), error) {
	if l == nil || l.Manager == nil {
		return nil, fmt.Errorf("短信恢复需要有效账号租约")
	}
	key := recoveryTargetKey{l.Manager, l.siteID}
	recoveryTargetMu.Lock()
	state := recoveryTargets[key]
	if state == nil || !state.recovering || state.closing {
		recoveryTargetMu.Unlock()
		return nil, fmt.Errorf("短信恢复需要持有网页恢复独占锁")
	}
	state.smsAllowed++
	state.smsAdmission = admit
	recoveryTargetMu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			recoveryTargetMu.Lock()
			if state.smsAllowed > 0 {
				state.smsAllowed--
			}
			if state.smsAllowed == 0 {
				state.smsAdmission = nil
			}
			recoveryTargetMu.Unlock()
			state.sms.Lock()
			state.sms.Unlock()
		})
	}, nil
}

func beginRecoverySMSAction(m *Manager, site string) (release func(), recovering bool) {
	key := recoveryTargetKey{m, site}
	recoveryTargetMu.Lock()
	state := recoveryTargets[key]
	if state == nil || !state.recovering {
		recoveryTargetMu.Unlock()
		return nil, false
	}
	if state.smsAllowed == 0 || state.closing {
		recoveryTargetMu.Unlock()
		return nil, true
	}
	recoveryTargetMu.Unlock()
	state.sms.Lock()
	recoveryTargetMu.Lock()
	allowed := recoveryTargets[key] == state && state.recovering && !state.closing && state.smsAllowed > 0
	admit := state.smsAdmission
	recoveryTargetMu.Unlock()
	if !allowed {
		state.sms.Unlock()
		return nil, true
	}
	if admit != nil && !admit() {
		state.sms.Unlock()
		return nil, true
	}
	recoveryTargetMu.Lock()
	allowed = recoveryTargets[key] == state && state.recovering && !state.closing && state.smsAllowed > 0
	recoveryTargetMu.Unlock()
	if !allowed {
		state.sms.Unlock()
		return nil, true
	}
	var once sync.Once
	return func() { once.Do(state.sms.Unlock) }, true
}

func beginManualTargetAction(m *Manager, site string) (func(), error) {
	key := recoveryTargetKey{m, site}
	recoveryTargetMu.Lock()
	state := recoveryTargets[key]
	if state != nil && state.recovering {
		recoveryTargetMu.Unlock()
		return nil, fmt.Errorf("该网页正在自动恢复，请等待复核与续跑完成")
	}
	if state == nil {
		state = &recoveryTargetState{}
		recoveryTargets[key] = state
	}
	state.manual++
	recoveryTargetMu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			recoveryTargetMu.Lock()
			state.manual--
			if state.manual == 0 {
				delete(recoveryTargets, key)
			}
			recoveryTargetMu.Unlock()
		})
	}, nil
}

// Recovery selects only explicitly challenged/logged-out accounts. It does
// not Refresh, probe unknown accounts or start a background pool-wide sweep.
func (p *Pool) recoveryCandidates(siteID string, excluded map[string]bool) []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	var ids []string
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled || excluded[id] {
			continue
		}
		st := a.Sites[siteID]
		if recoverableAccountState(st) {
			ids = append(ids, id)
		}
	}
	return ids
}

func recoverableAccountState(st State) bool {
	switch st.Status {
	case "need_captcha", "expired", "not_logged_in", "waiting_sms":
	default:
		return false
	}
	evidence := strings.ToLower(st.Message + " " + st.Evidence)
	return !strings.Contains(evidence, "quota") && !strings.Contains(evidence, "额度") && !strings.Contains(evidence, "本月用量已用完")
}

// AcquireExistingForRecoveryContext binds retry work to one account's already
// attached site target. It cannot start a runtime, navigate, refresh login
// state or substitute another account/page when the original target is lost.
func (p *Pool) AcquireExistingForRecoveryContext(parent context.Context, siteID, accountID string) (*Lease, error) {
	if _, ok := SiteByID(siteID); !ok {
		return nil, fmt.Errorf("unknown site")
	}
	ctx, cancel := context.WithTimeout(parent, accountWait())
	defer cancel()
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		p.mu.Lock()
		a := p.accounts[accountID]
		m := p.runtimes[accountID]
		if a == nil || !a.Enabled || p.stopping || m == nil || !m.Ready() {
			p.mu.Unlock()
			return nil, fmt.Errorf("指定账号的原浏览器不可用")
		}
		lk := p.accountLockLocked(accountID)
		p.mu.Unlock()
		if !lk.TryLock() {
			if err := waitContext(ctx, 50*time.Millisecond); err != nil {
				return nil, err
			}
			continue
		}
		p.mu.Lock()
		a = p.accounts[accountID]
		valid := a != nil && a.Enabled && !p.stopping && p.runtimes[accountID] == m
		phone := ""
		if valid {
			phone = a.Phone
		}
		p.mu.Unlock()
		if !valid || !m.Ready() {
			lk.Unlock()
			return nil, fmt.Errorf("指定账号的原浏览器不可用")
		}
		if !m.TryAcquireSite(siteID) {
			lk.Unlock()
			if err := waitContext(ctx, 50*time.Millisecond); err != nil {
				return nil, err
			}
			continue
		}
		lk.Unlock()
		m.mu.Lock()
		tab := m.tabs[siteID]
		hasTarget := false
		if tab != nil && tab.Err() == nil {
			attached := chromedp.FromContext(tab)
			hasTarget = attached != nil && attached.Target != nil && attached.Target.TargetID != ""
		}
		m.mu.Unlock()
		if ctx.Err() != nil {
			m.ReleaseSite(siteID)
			return nil, ctx.Err()
		}
		if !hasTarget {
			m.ReleaseSite(siteID)
			return nil, fmt.Errorf("指定账号的原网页目标不可用")
		}
		if _, err := m.CDPEndpoint(); err != nil {
			m.ReleaseSite(siteID)
			return nil, err
		}
		if err := ctx.Err(); err != nil {
			m.ReleaseSite(siteID)
			return nil, err
		}
		return &Lease{AccountID: accountID, Phone: phone, Manager: m, pool: p, siteID: siteID}, nil
	}
}

// AcquireForRecoveryContext holds the same site lease as normal ask flows, so
// another question cannot use the recovering account until Release. A live
// captcha target is preserved: EnsurePage/Refresh are deliberately skipped
// when that target is already present.
func (p *Pool) AcquireForRecoveryContext(parent context.Context, siteID string, excluded map[string]bool) (*Lease, error) {
	if _, ok := SiteByID(siteID); !ok {
		return nil, fmt.Errorf("unknown site")
	}
	ctx, cancel := context.WithTimeout(parent, accountWait())
	defer cancel()
	attempted := map[string]bool{}
	for id, skip := range excluded {
		attempted[id] = skip
	}
	var lastSetupError error
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		ids := p.recoveryCandidates(siteID, attempted)
		if len(ids) == 0 {
			if lastSetupError != nil {
				return nil, lastSetupError
			}
			return nil, fmt.Errorf("%s 无待恢复的号池账号", siteID)
		}
		for _, id := range ids {
			// Busy candidates are skipped without opening any browser. Once a
			// candidate fails setup, move on once to the next explicit recovery
			// candidate, without login probing or rechecking unknown accounts.
			p.mu.Lock()
			lk := p.accountLockLocked(id)
			p.mu.Unlock()
			if !lk.TryLock() {
				continue
			}
			m, err := p.managerForLockedContext(ctx, id)
			if err != nil {
				lk.Unlock()
				if ctx.Err() != nil {
					return nil, ctx.Err()
				}
				attempted[id], lastSetupError = true, err
				continue
			}
			if !m.TryAcquireSite(siteID) {
				lk.Unlock()
				continue
			}
			lk.Unlock()
			p.mu.Lock()
			a := p.accounts[id]
			eligible := a != nil && a.Enabled && !excluded[id] && recoverableAccountState(a.Sites[siteID])
			var phone, prior string
			if eligible {
				phone, prior = a.Phone, a.Sites[siteID].Status
			}
			p.mu.Unlock()
			if !eligible {
				m.ReleaseSite(siteID)
				continue
			}
			m.notePriorStatus(siteID, prior)
			m.mu.Lock()
			hasTab := m.tabs[siteID] != nil
			m.mu.Unlock()
			if !hasTab {
				err = m.EnsurePageContext(ctx, siteID)
			}
			if ctx.Err() != nil {
				m.ReleaseSite(siteID)
				return nil, ctx.Err()
			}
			if err == nil {
				_, err = m.CDPEndpoint()
			}
			if err != nil {
				m.ReleaseSite(siteID)
				attempted[id], lastSetupError = true, err
				continue
			}
			return &Lease{AccountID: id, Phone: phone, Manager: m, pool: p, siteID: siteID}, nil
		}
		if err := waitContext(ctx, 100*time.Millisecond); err != nil {
			return nil, err
		}
	}
}
