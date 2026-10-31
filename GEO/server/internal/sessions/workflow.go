package sessions

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/chromedp/cdproto/target"
	"github.com/chromedp/chromedp"
)

var ErrWorkflowBusy = errors.New("账号正在由其他任务或人工处理")
var ErrWorkflowNoAccount = errors.New("号池没有可用的启用账号")
var ErrWorkflowPageMissing = errors.New("原问题标签页已不存在")
var ErrWorkflowPageMismatch = errors.New("原标签页已离开本任务的平台")

type workflowContextKey struct{}
type workflowSiteContextKey struct{}

func siteWorkflowIsolation() bool { return os.Getenv("GEO_WORKFLOW_SITE_ISOLATION") == "1" }

// Ordinary browser/admin/login operations remain blocked while any workflow
// owns a tab. Only an explicitly owned workflow can enter its own site.
func (p *Pool) workflowAccessAllowedLocked(account string, ctx context.Context) bool {
	if p.workflowOwners[account] == "" {
		return true
	}
	owner, _ := ctx.Value(workflowContextKey{}).(string)
	if owner == "" {
		return false
	}
	if !siteWorkflowIsolation() {
		return p.workflowOwners[account] == owner
	}
	site, _ := ctx.Value(workflowSiteContextKey{}).(string)
	return site != "" && p.workflowSiteOwners[account+"/"+site] == owner
}

func (p *Pool) workflowHeldLocked(account, site, owner string) bool {
	if siteWorkflowIsolation() {
		held := p.workflowSiteOwners[account+"/"+site]
		return held != "" && held != owner
	}
	held := p.workflowOwners[account]
	return held != "" && held != owner
}

func (p *Pool) reserveWorkflowLocked(account, site, owner string) {
	if siteWorkflowIsolation() {
		if p.workflowSiteOwners == nil {
			p.workflowSiteOwners = map[string]string{}
		}
		p.workflowSiteOwners[account+"/"+site] = owner
		p.workflowOwners[account] = "site-workflows"
	} else {
		p.workflowOwners[account] = owner
	}
	if m := p.runtimes[account]; m != nil {
		m.setWorkflowControlled(true)
	}
}

func workflowRuntimeBusy(m *Manager, site string) bool {
	if m == nil {
		return false
	}
	if siteWorkflowIsolation() {
		return m.isBusy(site)
	}
	return m.Busy()
}

// Reservations default to the whole account. Site isolation permits separate
// workflow tabs to run concurrently while still excluding background login.
// Both modes restore reservations before background login starts.
func (p *Pool) ReserveWorkflow(owner, site, preferred string) (*Account, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.workflowOwners == nil {
		p.workflowOwners = map[string]string{}
	}
	if preferred != "" {
		a := p.accounts[preferred]
		if a == nil || !a.Enabled {
			return nil, ErrWorkflowNoAccount
		}
		if p.workflowHeldLocked(preferred, site, owner) {
			return nil, ErrWorkflowBusy
		}
		lk := p.accountLockLocked(preferred)
		if !lk.TryLock() {
			return nil, ErrWorkflowBusy
		}
		lk.Unlock()
		if workflowRuntimeBusy(p.runtimes[preferred], site) {
			return nil, ErrWorkflowBusy
		}
		p.reserveWorkflowLocked(preferred, site, owner)
		return copyAccount(a), nil
	}
	var fallback *Account
	enabled := false
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled {
			continue
		}
		enabled = true
		if p.workflowHeldLocked(id, site, owner) {
			continue
		}
		lk := p.accountLockLocked(id)
		if !lk.TryLock() {
			continue
		}
		lk.Unlock()
		if workflowRuntimeBusy(p.runtimes[id], site) {
			continue
		}
		if fallback == nil {
			fallback = a
		}
		if a.Sites[site].Status == "logged_in" {
			fallback = a
			break
		}
	}
	if fallback == nil {
		if enabled {
			return nil, ErrWorkflowBusy
		}
		return nil, ErrWorkflowNoAccount
	}
	p.reserveWorkflowLocked(fallback.ID, site, owner)
	return copyAccount(fallback), nil
}

func (p *Pool) ReleaseWorkflow(owner string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if siteWorkflowIsolation() {
		for key, held := range p.workflowSiteOwners {
			if held == owner {
				delete(p.workflowSiteOwners, key)
			}
		}
		for account := range p.workflowOwners {
			occupied := false
			for key := range p.workflowSiteOwners {
				if strings.HasPrefix(key, account+"/") {
					occupied = true
					break
				}
			}
			if !occupied {
				delete(p.workflowOwners, account)
				if m := p.runtimes[account]; m != nil {
					m.setWorkflowControlled(false)
				}
			}
		}
		return
	}
	for id, held := range p.workflowOwners {
		if held == owner {
			delete(p.workflowOwners, id)
			if m := p.runtimes[id]; m != nil {
				m.setWorkflowControlled(false)
			}
		}
	}
}

func (p *Pool) AcquireWorkflow(ctx context.Context, owner, account, site, originalTarget string) (*Lease, error) {
	if _, err := p.ReserveWorkflow(owner, site, account); err != nil {
		return nil, err
	}
	ctx = context.WithValue(ctx, workflowContextKey{}, owner)
	ctx = context.WithValue(ctx, workflowSiteContextKey{}, site)
	m, err := p.acquireManagerSite(account, site, true, ctx)
	if err != nil {
		return nil, err
	}
	lease := &Lease{AccountID: account, Phone: m.CurrentPhone(), Manager: m, pool: p, siteID: site}
	if m.externalChrome {
		// Python owns the page session and already binds its journal to the
		// exact target. A stale Go renderer/control session must not prevent it
		// from reading a live page in this independent Chrome.
		siteConfig, ok := SiteByID(site)
		if !ok {
			lease.Release()
			return nil, fmt.Errorf("未知平台")
		}
		view, cancel, err := profileBrowserContext(ctx, m.ProfileDir)
		if err != nil {
			lease.Release()
			return nil, err
		}
		m.mu.Lock()
		previous := m.workflowTargets[site]
		m.mu.Unlock()
		id, err := resolveWorkflowTarget(view, siteConfig, originalTarget, previous)
		cancel()
		if err != nil {
			lease.Release()
			return nil, err
		}
		lease.workflowTargetID = id
		m.mu.Lock()
		if m.workflowTargets == nil {
			m.workflowTargets = map[string]string{}
		}
		m.workflowTargets[site] = id
		m.mu.Unlock()
	} else if originalTarget != "" {
		if err := m.attachWorkflowTarget(site, originalTarget); err != nil {
			lease.Release()
			return nil, err
		}
	} else if err := m.EnsurePageContext(ctx, site); err != nil {
		lease.Release()
		return nil, err
	}
	m.KeepWarmFor(45 * time.Minute)
	return lease, nil
}

func resolveWorkflowTarget(ctx context.Context, site Site, original, previous string) (string, error) {
	infos, err := target.GetTargets().Do(ctx)
	if err != nil {
		return "", fmt.Errorf("读取原浏览器页面: %w", err)
	}
	for _, info := range infos {
		if original != "" && string(info.TargetID) == original {
			if info.Type != "page" || !siteOwnsPage(site, info.URL) {
				return "", ErrWorkflowPageMismatch
			}
			return original, nil
		}
	}
	if original != "" {
		return "", ErrWorkflowPageMissing
	}
	for _, info := range infos {
		if previous != "" && string(info.TargetID) == previous && info.Type == "page" && siteOwnsPage(site, info.URL) {
			return previous, nil
		}
	}
	page, err := createWorkflowPage(ctx, site)
	return page.TargetID, err
}

func (m *Manager) setWorkflowControlled(value bool) {
	m.mu.Lock()
	m.workflowControlled = value
	m.mu.Unlock()
}
func (m *Manager) workflowIsControlled() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.workflowControlled
}

func (m *Manager) attachWorkflowTarget(siteID, id string) error {
	if managerSiteTargetID(m, siteID) == id {
		return nil
	}
	site, ok := SiteByID(siteID)
	if !ok {
		return fmt.Errorf("未知平台")
	}
	ctx := m.liveCtx()
	var infos []*target.Info
	var err error
	if m.externalChrome {
		view, cancel, connectErr := profileBrowserContext(context.Background(), m.ProfileDir)
		if connectErr != nil {
			return connectErr
		}
		defer cancel()
		infos, err = target.GetTargets().Do(view)
	} else {
		err = runTab(ctx, 3*time.Second, chromedp.ActionFunc(func(ctx context.Context) error { var err error; infos, err = target.GetTargets().Do(ctx); return err }))
	}
	if err != nil {
		return err
	}
	found := false
	for _, info := range infos {
		if string(info.TargetID) == id && info.Type == "page" && siteOwnsPage(site, info.URL) {
			found = true
			break
		}
	}
	if !found {
		return fmt.Errorf("原问题标签页已不存在，请人工检查；未重新发送问题")
	}
	next, cancel := chromedp.NewContext(m.siteContextParent(m.browserCtx), chromedp.WithTargetID(target.ID(id)))
	stop := time.AfterFunc(5*time.Second, cancel)
	err = chromedp.Run(next)
	stop.Stop()
	if err != nil || next.Err() != nil {
		cancel()
		if err != nil {
			return err
		}
		return next.Err()
	}
	m.mu.Lock()
	old := m.tabCancels[siteID]
	m.tabs[siteID] = next
	m.tabCancels[siteID] = cancel
	m.opened[siteID] = true
	m.mu.Unlock()
	if old != nil {
		old()
	}
	return nil
}

func (p *Pool) FocusWorkflow(ctx context.Context, owner, account, site, id string) error {
	_, err := p.OpenWorkflowPage(ctx, owner, account, site, id, "")
	return err
}
