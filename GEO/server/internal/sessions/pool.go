package sessions

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Account is one phone-number identity. Every account owns an isolated Chrome
// profile; cookies from different phone numbers are therefore never mixed.
type Account struct {
	ID        string           `json:"id"`
	Phone     string           `json:"phone"`
	Enabled   bool             `json:"enabled"`
	CreatedAt string           `json:"created_at"`
	UpdatedAt string           `json:"updated_at"`
	Sites     map[string]State `json:"sites"`
}

type poolStore struct {
	Accounts []*Account `json:"accounts"`
}

type Pool struct {
	ChromePath    string
	LegacyProfile string
	DataPath      string

	mu                   sync.Mutex
	accounts             map[string]*Account
	order                []string
	runtimes             map[string]*Manager
	locks                map[string]*sync.Mutex
	started              bool
	stopping             bool
	workflowOwners       map[string]string
	workflowSiteOwners   map[string]string
	rr                   map[string]int
	opSem                chan struct{}
	probeAfter           map[string]time.Time
	loginChallengeAssist func(accountID, siteID string) error
}

// SetLoginChallengeAssist is wired before Start, avoiding a sessions -> jobs
// dependency. Only a challenge just observed by background login is handed off.
func (p *Pool) SetLoginChallengeAssist(handler func(accountID, siteID string) error) {
	p.mu.Lock()
	p.loginChallengeAssist = handler
	p.mu.Unlock()
}

type Lease struct {
	AccountID        string
	Phone            string
	Manager          *Manager
	pool             *Pool
	siteID           string
	workflowTargetID string
	once             sync.Once
}

func NewPool(chromePath, legacyProfile, defaultPhone, dataPath string) *Pool {
	p := &Pool{
		ChromePath: chromePath, LegacyProfile: legacyProfile, DataPath: dataPath,
		accounts: map[string]*Account{}, runtimes: map[string]*Manager{},
		locks: map[string]*sync.Mutex{}, rr: map[string]int{}, opSem: make(chan struct{}, 2),
	}
	loaded := p.load()
	if !loaded && len(p.accounts) == 0 && digitsOnly(defaultPhone) != "" {
		a := newAccount("default", digitsOnly(defaultPhone))
		p.accounts[a.ID] = a
		p.order = append(p.order, a.ID)
		_ = p.save()
	}
	return p
}

func newAccount(id, phone string) *Account {
	ts := time.Now().Format(time.RFC3339)
	a := &Account{ID: id, Phone: phone, Enabled: true, CreatedAt: ts, UpdatedAt: ts, Sites: map[string]State{}}
	for _, site := range Sites {
		a.Sites[site.ID] = State{
			SiteID: site.ID, Name: site.Name, Status: "not_logged_in",
			Message: "尚未检测", Phone: phone, UpdatedAt: ts,
		}
	}
	return a
}

func (p *Pool) Start() error {
	p.mu.Lock()
	if p.started {
		p.mu.Unlock()
		return nil
	}
	p.started = true
	p.mu.Unlock()
	// Persisted state is sufficient for availability. Open a profile only for
	// an actual job or explicit admin check; eager unknown-page sweeps can
	// recreate every challenged browser before the first customer request.
	go p.reapLoop()
	go p.reloginLoop()
	return nil
}

// reconcileUnknownSessions actively verifies only indeterminate persisted
// states. Confirmed logged-out accounts still fail acquisition immediately.
func (p *Pool) reconcileUnknownSessions() {
	type target struct {
		accountID string
		siteID    string
	}
	p.mu.Lock()
	var targets []target
	ready := map[string]bool{}
	for _, a := range p.accounts {
		if a != nil && a.Enabled {
			for siteID, st := range a.Sites {
				if st.Status == "logged_in" {
					ready[siteID] = true
				}
			}
		}
	}
	for _, accountID := range p.order {
		a := p.accounts[accountID]
		if a == nil || !a.Enabled {
			continue
		}
		for _, site := range Sites {
			// A healthy pool already serves this site. Reconcile uncertain
			// accounts lazily if that pool runs out, rather than opening six
			// extra pages while customer requests are trying to generate.
			if ready[site.ID] {
				continue
			}
			if !shouldReconcileSite(a.Phone, site.ID) {
				continue
			}
			if st, ok := a.Sites[site.ID]; ok && st.Status == "unknown" {
				targets = append(targets, target{accountID: accountID, siteID: site.ID})
			}
		}
	}
	p.mu.Unlock()
	for _, item := range targets {
		st, err := p.Check(item.accountID, item.siteID)
		if err != nil {
			log.Printf("reconcile unknown account=%s site=%s: %v", item.accountID, item.siteID, err)
			continue
		}
		log.Printf("startup reconcile account=%s site=%s status=%s", item.accountID, item.siteID, st.Status)
	}
}

func (p *Pool) Stop() {
	p.mu.Lock()
	p.stopping = true
	type runtimeRef struct {
		id string
		m  *Manager
		lk *sync.Mutex
	}
	runtimes := make([]runtimeRef, 0, len(p.runtimes))
	for id, m := range p.runtimes {
		runtimes = append(runtimes, runtimeRef{id: id, m: m, lk: p.accountLockLocked(id)})
	}
	p.mu.Unlock()
	for _, rt := range runtimes {
		rt.lk.Lock()
		p.mu.Lock()
		current := p.runtimes[rt.id]
		p.mu.Unlock()
		if current == rt.m {
			rt.m.Stop()
		}
		rt.lk.Unlock()
	}
	p.syncAll()
}

func (p *Pool) Ready() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.started && !p.stopping
}

func (p *Pool) Add(phone string) (*Account, error) {
	phone = digitsOnly(phone)
	if len(phone) != 11 {
		return nil, fmt.Errorf("手机号需为 11 位")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, a := range p.accounts {
		if a.Phone == phone {
			return nil, fmt.Errorf("手机号已存在")
		}
	}
	id, err := accountID()
	if err != nil {
		return nil, err
	}
	a := newAccount(id, phone)
	p.accounts[id] = a
	p.order = append(p.order, id)
	if err := p.saveLocked(); err != nil {
		delete(p.accounts, id)
		p.order = p.order[:len(p.order)-1]
		return nil, err
	}
	return copyAccount(a), nil
}

func (p *Pool) Import(phones []string) (added []*Account, errors []string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	existing := make(map[string]bool, len(p.accounts)+len(phones))
	for _, a := range p.accounts {
		existing[a.Phone] = true
	}
	startOrder := len(p.order)
	for _, raw := range phones {
		phone := digitsOnly(raw)
		if phone == "" {
			continue
		}
		if len(phone) != 11 {
			errors = append(errors, phone+": 手机号需为 11 位")
			continue
		}
		if existing[phone] {
			errors = append(errors, phone+": 手机号已存在")
			continue
		}
		id, err := accountID()
		if err != nil {
			errors = append(errors, phone+": "+err.Error())
			continue
		}
		a := newAccount(id, phone)
		p.accounts[id] = a
		p.order = append(p.order, id)
		existing[phone] = true
		added = append(added, copyAccount(a))
	}
	if len(added) == 0 {
		return added, errors
	}
	if err := p.saveLocked(); err != nil {
		for _, a := range added {
			delete(p.accounts, a.ID)
		}
		p.order = p.order[:startOrder]
		errors = append(errors, "保存号池: "+err.Error())
		return nil, errors
	}
	return
}

func (p *Pool) SetEnabled(id string, enabled bool) (*Account, error) {
	p.mu.Lock()
	a := p.accounts[id]
	if a == nil {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号不存在")
	}
	lk := p.accountLockLocked(id)
	p.mu.Unlock()

	lk.Lock()
	defer lk.Unlock()
	p.mu.Lock()
	a = p.accounts[id]
	if a == nil {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号不存在")
	}
	m := p.runtimes[id]
	if !enabled && p.workflowOwners[id] != "" {
		p.mu.Unlock()
		return nil, ErrWorkflowBusy
	}
	if !enabled && m != nil && m.Busy() {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号正在执行任务或登录操作，暂不能停用")
	}
	a.Enabled = enabled
	a.UpdatedAt = time.Now().Format(time.RFC3339)
	if !enabled {
		delete(p.runtimes, id)
	}
	err := p.saveLocked()
	cp := copyAccount(a)
	p.mu.Unlock()
	if !enabled && m != nil {
		m.Stop()
	}
	return cp, err
}

func (p *Pool) Delete(id string) error {
	p.mu.Lock()
	if p.workflowOwners[id] != "" {
		p.mu.Unlock()
		return ErrWorkflowBusy
	}
	a := p.accounts[id]
	if a == nil {
		p.mu.Unlock()
		return fmt.Errorf("账号不存在")
	}
	lk := p.accountLockLocked(id)
	p.mu.Unlock()

	lk.Lock()
	defer lk.Unlock()
	p.mu.Lock()
	a = p.accounts[id]
	if a == nil {
		p.mu.Unlock()
		return fmt.Errorf("账号不存在")
	}
	if p.workflowOwners[id] != "" {
		p.mu.Unlock()
		return ErrWorkflowBusy
	}
	m := p.runtimes[id]
	if m != nil && m.Busy() {
		p.mu.Unlock()
		return fmt.Errorf("账号正在执行任务或登录操作，暂不能删除")
	}
	previousOrder := append([]string(nil), p.order...)
	delete(p.runtimes, id)
	delete(p.accounts, id)
	for i, v := range p.order {
		if v == id {
			p.order = append(p.order[:i], p.order[i+1:]...)
			break
		}
	}
	err := p.saveLocked()
	if err != nil {
		p.accounts[id] = a
		p.order = previousOrder
		if m != nil {
			p.runtimes[id] = m
		}
		p.mu.Unlock()
		return err
	}
	p.mu.Unlock()
	if m != nil {
		m.Stop()
	}
	return err
}

func (p *Pool) List() []*Account {
	p.syncAll()
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]*Account, 0, len(p.order))
	for _, id := range p.order {
		if a := p.accounts[id]; a != nil {
			out = append(out, copyAccount(a))
		}
	}
	return out
}

func (p *Pool) Get(id string) (*Account, bool) {
	p.syncOne(id)
	p.mu.Lock()
	defer p.mu.Unlock()
	a, ok := p.accounts[id]
	return copyAccount(a), ok
}

func (p *Pool) Check(id, siteID string) (*State, error) {
	return p.checkContext(context.Background(), id, siteID, false)
}

type checkStateSnapshot struct {
	prior, checking, runtimePrior State
	hadPrior, hadRuntime          bool
}

// The checking value is also a compare-and-restore token. Its precise timestamp
// distinguishes this probe from another update in the same wall-clock second.
func (p *Pool) beginCheckState(id, siteID string, m *Manager) (checkStateSnapshot, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	a := p.accounts[id]
	if a == nil {
		return checkStateSnapshot{}, false
	}
	prior, present := a.Sites[siteID]
	snapshot := checkStateSnapshot{prior: prior, hadPrior: present}
	if m != nil {
		m.notePriorStatus(siteID, prior.Status)
		m.mu.Lock()
		if state := m.states[siteID]; state != nil {
			snapshot.runtimePrior, snapshot.hadRuntime = *state, true
		}
		m.mu.Unlock()
	}
	checking := prior
	if !present {
		site, _ := SiteByID(siteID)
		checking = State{SiteID: siteID, Name: site.Name, Phone: a.Phone}
	}
	checking.Status, checking.Message = "checking", "正在主动访问平台验证登录状态"
	checking.UpdatedAt = time.Now().Format(time.RFC3339Nano)
	checking.LastCheckedAt = checking.UpdatedAt
	if a.Sites == nil {
		a.Sites = make(map[string]State)
	}
	a.Sites[siteID], a.UpdatedAt = checking, time.Now().Format(time.RFC3339)
	snapshot.checking = checking
	_ = p.saveLocked()
	return snapshot, true
}

func (p *Pool) restoreCancelledCheck(id, siteID string, snapshot checkStateSnapshot, m *Manager, observed *State) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	a := p.accounts[id]
	if a == nil || a.Sites[siteID] != snapshot.checking {
		return false // A newer persisted decision, including a real logout, wins.
	}
	// Refresh has its own context. If cancellation raced an indeterminate read,
	// prevent that exact read from poisoning a later syncOne. Positive login,
	// captcha and logout evidence, or any concurrent runtime update, is retained.
	if m != nil && observed != nil && snapshot.hadRuntime {
		switch observed.Status {
		case "unknown", "idle", "error":
			m.mu.Lock()
			if current := m.states[siteID]; current != nil && *current == *observed {
				prior := snapshot.runtimePrior
				m.states[siteID] = &prior
			}
			m.mu.Unlock()
		}
	}
	if snapshot.hadPrior {
		a.Sites[siteID] = snapshot.prior
	} else {
		delete(a.Sites, siteID)
	}
	a.UpdatedAt = time.Now().Format(time.RFC3339)
	_ = p.saveLocked()
	return true
}

func (p *Pool) checkContext(ctx context.Context, id, siteID string, try bool) (*State, error) {
	if _, ok := SiteByID(siteID); !ok {
		return nil, fmt.Errorf("未知站点")
	}
	select {
	case p.opSem <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	defer func() { <-p.opSem }()
	m, err := p.acquireManagerSite(id, siteID, try, ctx)
	if err != nil {
		return nil, err
	}
	var st *State
	snapshot, marked := p.beginCheckState(id, siteID, m)
	defer func() {
		if marked && ctx.Err() != nil {
			p.restoreCancelledCheck(id, siteID, snapshot, m, st)
		}
		m.ReleaseSite(siteID)
		p.stopIfCold(id, m)
	}()
	if !marked {
		return nil, fmt.Errorf("账号不存在")
	}
	ensureErr := m.EnsurePageContext(ctx, siteID)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	for i := 0; i < 6; i++ {
		if i > 0 {
			if err := waitContext(ctx, 2*time.Second); err != nil {
				return nil, err
			}
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		st = m.Refresh(siteID)
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if st.Status != "unknown" && st.Status != "idle" {
			break
		}
	}
	if ensureErr != nil && (st == nil || st.Status == "unknown" || st.Status == "idle") {
		st = m.set(siteID, "error", "打开平台失败: "+ensureErr.Error())
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	p.syncOne(id)
	return st, nil
}

func (p *Pool) SendCode(id, siteID string, force bool) (*State, error) {
	warmFor := 20 * time.Minute
	if siteID == "yuanbao" {
		warmFor = 45 * time.Minute
	}
	return p.sendCode(id, siteID, force, warmFor)
}

// Explicit operator actions keep the page available longer than unattended
// login attempts, which must not pin an unused Chrome through customer load.
func (p *Pool) sendCode(id, siteID string, force bool, warmFor time.Duration) (*State, error) {
	if _, ok := SiteByID(siteID); !ok {
		return nil, fmt.Errorf("未知站点")
	}
	p.markState(id, siteID, "logging_in", "正在打开登录页")
	p.opSem <- struct{}{}
	defer func() { <-p.opSem }()
	m, err := p.acquireManagerSite(id, siteID, false)
	if err != nil {
		return nil, err
	}
	defer func() {
		m.ReleaseSite(siteID)
		p.stopIfCold(id, m)
	}()
	st, err := m.sendCode(siteID, force, warmFor)
	if st != nil && (st.Status == "need_captcha" || st.Status == "waiting_sms") {
		m.KeepWarmFor(warmFor)
	}
	p.syncOne(id)
	return st, err
}

func (p *Pool) SubmitCode(id, siteID, code string) (*State, error) {
	p.mu.Lock()
	m := p.runtimes[id]
	p.mu.Unlock()
	if m != nil {
		if release, recovering := beginRecoverySMSAction(m, siteID); recovering {
			if release == nil {
				return nil, ErrRecoverySMSNotReady
			}
			defer release()
			st, err := m.submitRecoveryCode(siteID, code)
			p.syncOne(id)
			return st, err
		}
	}
	p.opSem <- struct{}{}
	defer func() { <-p.opSem }()
	m, err := p.acquireManagerSite(id, siteID, false)
	if err != nil {
		return nil, err
	}
	defer func() {
		m.ReleaseSite(siteID)
		p.stopIfCold(id, m)
	}()
	st, err := m.SubmitCode(siteID, code)
	if err == nil && siteID == "kimi" && st != nil && st.Status == "logged_in" {
		const warmFor = 10 * time.Minute
		m.KeepWarmFor(warmFor)
		time.AfterFunc(warmFor, func() {
			p.stopIfCold(id, m)
		})
	}
	p.syncOne(id)
	return st, err
}

func (p *Pool) ClearSiteSession(id, siteID string) (*State, error) {
	if _, ok := SiteByID(siteID); !ok {
		return nil, fmt.Errorf("未知站点")
	}
	p.opSem <- struct{}{}
	defer func() { <-p.opSem }()
	m, err := p.acquireManagerSite(id, siteID, false)
	if err != nil {
		return nil, err
	}
	defer func() {
		m.ReleaseSite(siteID)
		p.stopIfCold(id, m)
	}()
	st, err := m.ClearSiteSession(siteID)
	p.syncOne(id)
	return st, err
}

func (p *Pool) Screenshot(id, siteID string) ([]byte, error) {
	if m := p.busyRuntime(id); m != nil {
		return m.Screenshot(siteID)
	}
	m, err := p.acquireManagerSite(id, siteID, false)
	if err != nil {
		return nil, err
	}
	defer func() {
		m.ReleaseSite(siteID)
		p.stopIfCold(id, m)
	}()
	buf, err := m.Screenshot(siteID)
	if err == nil {
		m.KeepWarmFor(10 * time.Minute)
	}
	return buf, err
}

func (p *Pool) ClickNorm(id, siteID string, nx, ny float64) (*State, error) {
	if m := p.busyRuntime(id); m != nil {
		release, err := beginManualTargetAction(m, siteID)
		if err != nil {
			return nil, err
		}
		defer release()
		st, err := m.ClickNorm(siteID, nx, ny)
		p.syncOne(id)
		return st, err
	}
	m, err := p.acquireManagerSite(id, siteID, false)
	if err != nil {
		return nil, err
	}
	defer func() {
		m.ReleaseSite(siteID)
		p.stopIfCold(id, m)
	}()
	st, err := m.ClickNorm(siteID, nx, ny)
	m.KeepWarmFor(10 * time.Minute)
	p.syncOne(id)
	return st, err
}

func (p *Pool) DragNorm(id, siteID string, x1, y1, x2, y2 float64) (*State, error) {
	if m := p.busyRuntime(id); m != nil {
		release, err := beginManualTargetAction(m, siteID)
		if err != nil {
			return nil, err
		}
		defer release()
		st, err := m.DragNorm(siteID, x1, y1, x2, y2)
		p.syncOne(id)
		return st, err
	}
	m, err := p.acquireManagerSite(id, siteID, false)
	if err != nil {
		return nil, err
	}
	defer func() {
		m.ReleaseSite(siteID)
		p.stopIfCold(id, m)
	}()
	st, err := m.DragNorm(siteID, x1, y1, x2, y2)
	m.KeepWarmFor(10 * time.Minute)
	p.syncOne(id)
	return st, err
}

func (p *Pool) busyRuntime(id string) *Manager {
	p.mu.Lock()
	defer p.mu.Unlock()
	m := p.runtimes[id]
	if m != nil && m.Busy() {
		// Record operator intervention before releasing the pool lock so the
		// customer lease's concurrent cleanup cannot close this live page.
		m.KeepWarmFor(10 * time.Minute)
		return m
	}
	return nil
}

func (p *Pool) WaitingSMS() []struct {
	AccountID string
	Phone     string
	SiteID    string
} {
	p.syncAll()
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []struct {
		AccountID string
		Phone     string
		SiteID    string
	}
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled {
			continue
		}
		for siteID, st := range a.Sites {
			if st.Status == "waiting_sms" {
				out = append(out, struct {
					AccountID string
					Phone     string
					SiteID    string
				}{id, a.Phone, siteID})
			}
		}
	}
	return out
}

// Acquire chooses a logged-in, idle account and holds its site lock until
// Lease.Release. A stale persisted status is actively verified before use.
func (p *Pool) Acquire(siteID string) (*Lease, error) {
	return p.AcquireContext(context.Background(), siteID)
}

func (p *Pool) AcquireContext(parent context.Context, siteID string) (*Lease, error) {
	return p.acquireContext(parent, siteID, nil)
}

// AcquireContextExcept leases a logged-in account other than the phones that
// already failed this task. It returns immediately when every logged-in
// account is in skip, instead of waiting out the account idle timeout.
func (p *Pool) AcquireContextExcept(parent context.Context, siteID string, skip map[string]bool) (*Lease, error) {
	return p.acquireContext(parent, siteID, skip)
}

func (p *Pool) acquireContext(parent context.Context, siteID string, skip map[string]bool) (*Lease, error) {
	ctx, cancel := context.WithTimeout(parent, accountWait())
	defer cancel()
	preparation := &PreparationError{Site: siteID}
	reconciledUnknown := false
	for {
		if err := ctx.Err(); err != nil {
			return nil, preparation.withCause(err)
		}
		candidates := p.candidates(siteID)
		candidates = omitIDs(candidates, skip)
		preparation.candidates(candidates)
		if len(candidates) == 0 {
			if !reconciledUnknown {
				reconciledUnknown = true
				unknown := omitIDs(p.candidatesWithStatus(siteID, "unknown"), skip)
				for _, id := range unknown {
					if err := ctx.Err(); err != nil {
						return nil, err
					}
					if !p.claimUnknownProbe(id, siteID, time.Now()) {
						continue
					}
					probeCtx, stopProbe := context.WithTimeout(ctx, pageOpenBudget()+15*time.Second)
					st, err := p.checkContext(probeCtx, id, siteID, true)
					stopProbe()
					if err == nil && st != nil && st.Status == "logged_in" {
						p.clearUnknownProbe(id, siteID)
					}
					if err != nil {
						log.Printf("acquire reconcile account=%s site=%s: %v", id, siteID, err)
						continue
					}
					log.Printf("acquire reconciled account=%s site=%s status=%s", id, siteID, st.Status)
				}
				if len(unknown) > 0 {
					continue
				}
			}
			return nil, preparation.withCause(fmt.Errorf("%s 无已登录的号池账号", siteID))
		}
		for _, id := range candidates {
			if preparation.FailureCodes[id] != "" {
				continue
			}
			if err := ctx.Err(); err != nil {
				return nil, preparation.withCause(err)
			}
			m, err := p.acquireManagerSite(id, siteID, true, ctx)
			if err != nil {
				var failed *preparationStageFailure
				if ctx.Err() == nil && errors.As(err, &failed) {
					preparation.failed(id, failed.code)
				}
				continue
			}
			m.notePriorStatus(siteID, p.persistedStatus(id, siteID))
			ensureErr := m.EnsurePageContext(ctx, siteID)
			if err := ctx.Err(); err != nil {
				// A local setup deadline is not evidence of a lost login. Do
				// not probe or persist a transient replacement tab after cancel.
				m.ReleaseSite(siteID)
				return nil, preparation.withCause(err)
			}
			var st *State
			for i := 0; i < 6; i++ {
				if i > 0 {
					if err := waitContext(ctx, 2*time.Second); err != nil {
						m.ReleaseSite(siteID)
						return nil, preparation.withCause(err)
					}
				}
				st = m.Refresh(siteID)
				if st.Status != "unknown" && st.Status != "idle" {
					break
				}
			}
			if ensureErr != nil && (st == nil || st.Status == "unknown" || st.Status == "idle") {
				m.set(siteID, "error", "打开平台失败: "+ensureErr.Error())
			}
			p.syncOne(id)
			if err := ctx.Err(); err != nil {
				m.ReleaseSite(siteID)
				return nil, preparation.withCause(err)
			}
			if st.Status != "logged_in" {
				if ensureErr != nil && (st.Status == "unknown" || st.Status == "idle" || st.Status == "error") {
					preparation.failed(id, "session_setup_failed")
				}
				m.ReleaseSite(siteID)
				continue
			}
			cdp, err := m.CDPEndpoint()
			if err != nil {
				if ctx.Err() == nil {
					preparation.failed(id, "browser_start_failed")
				}
				m.ReleaseSite(siteID)
				continue
			}
			_ = cdp
			a, _ := p.Get(id)
			return &Lease{AccountID: id, Phone: a.Phone, Manager: m, pool: p, siteID: siteID}, nil
		}
		if preparation.AllCandidatesFailed() {
			if err := ctx.Err(); err != nil {
				return nil, preparation.withCause(err)
			}
			return nil, preparation
		}
		if err := waitContext(ctx, 500*time.Millisecond); err != nil {
			return nil, preparation.withCause(err)
		}
	}
}

// AcquireForLogin leases any enabled account, including one whose persisted
// site state is logged out. It is used by flows that can establish a fresh
// webpage login themselves (for example Doubao's SMS login fallback).
func (p *Pool) AcquireForLogin(siteID string) (*Lease, error) {
	return p.AcquireForLoginContext(context.Background(), siteID)
}

func (p *Pool) AcquireForLoginContext(parent context.Context, siteID string) (*Lease, error) {
	ctx, cancel := context.WithTimeout(parent, accountWait())
	defer cancel()
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		candidates := p.loginCandidates(siteID)
		if len(candidates) == 0 {
			return nil, fmt.Errorf("%s 无可用的号池账号", siteID)
		}
		for _, id := range candidates {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			m, err := p.acquireManagerSite(id, siteID, true, ctx)
			if err != nil {
				continue
			}
			if err := m.EnsurePageContext(ctx, siteID); err != nil {
				log.Printf("acquire login ensure account=%s site=%s: %v", id, siteID, err)
				m.ReleaseSite(siteID)
				continue
			}
			if _, err := m.CDPEndpoint(); err != nil {
				m.ReleaseSite(siteID)
				continue
			}
			if err := ctx.Err(); err != nil {
				m.ReleaseSite(siteID)
				return nil, err
			}
			a, _ := p.Get(id)
			return &Lease{AccountID: id, Phone: a.Phone, Manager: m, pool: p, siteID: siteID}, nil
		}
		if err := waitContext(ctx, 500*time.Millisecond); err != nil {
			return nil, fmt.Errorf("%s 无空闲的号池账号: %w", siteID, err)
		}
	}
}

func waitContext(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return ctx.Err()
	}
}

func (l *Lease) CDPEndpoint() (string, error) {
	if l == nil || l.Manager == nil {
		return "", fmt.Errorf("账号租约无效")
	}
	return l.Manager.CDPEndpoint()
}

func (l *Lease) Release() {
	if l == nil || l.Manager == nil {
		return
	}
	l.once.Do(func() {
		l.Manager.ReleaseSite(l.siteID)
		if l.pool != nil {
			l.pool.syncOne(l.AccountID)
			l.pool.stopIfCold(l.AccountID, l.Manager)
		}
	})
}

// MarkUnavailable removes a session that failed an authenticated platform
// action from subsequent candidate selection until it is actively rechecked.
func (l *Lease) MarkUnavailable(message string) {
	if l == nil || l.Manager == nil {
		return
	}
	l.Manager.set(l.siteID, "expired", message)
	if l.pool != nil {
		l.pool.markState(l.AccountID, l.siteID, "expired", message)
	}
}

// MarkAvailable records a login established by an external Playwright client
// connected to this lease's browser.
func (l *Lease) MarkAvailable(message string) {
	if l == nil || l.Manager == nil {
		return
	}
	l.Manager.set(l.siteID, "logged_in", message)
	if l.pool != nil {
		l.pool.markState(l.AccountID, l.siteID, "logged_in", message)
	}
}

// MarkNeedsCaptcha removes a challenged account from automatic selection. Its
// short browser grace yields to capacity pressure unless an operator intervenes.
func (l *Lease) MarkNeedsCaptcha(message string) {
	if l == nil || l.Manager == nil {
		return
	}
	l.Manager.set(l.siteID, "need_captcha", message)
	// Explicit screenshot/click/login actions use their separate longer hold.
	l.Manager.keepAutomaticChallengeWarmFor(l.siteID, pendingLoginGrace())
	// A question-time semantic challenge is the page the operator clicks.
	// Keep that browser up long enough to open the same admin dialog.
	if strings.Contains(message, "语义安全验证") || strings.Contains(message, "平台触发安全验证") {
		l.Manager.KeepWarmFor(20 * time.Minute)
	}
	if l.pool != nil {
		l.pool.markState(l.AccountID, l.siteID, "need_captcha", message)
	}
}

// Snapshot keeps the public API backward compatible: one site is available
// when at least one enabled account is actively known as logged in.
func (p *Pool) Snapshot() map[string]any {
	accounts := p.List()
	states := make([]State, 0, len(Sites))
	for _, site := range Sites {
		best := State{SiteID: site.ID, Name: site.Name, Status: "not_logged_in", Message: "号池无已登录账号", UpdatedAt: now()}
		for _, a := range accounts {
			if !a.Enabled {
				continue
			}
			st, ok := a.Sites[site.ID]
			if !ok {
				continue
			}
			if st.Status == "logged_in" {
				best = st
				best.Phone = ""
				best.Message = "号池中有可用账号"
				break
			}
			if statePriority(st.Status) > statePriority(best.Status) {
				best = st
				best.Phone = ""
			}
		}
		states = append(states, best)
	}
	return map[string]any{"ready": p.Ready(), "sites": states, "accounts": len(accounts)}
}

func statePriority(status string) int {
	switch status {
	case "logged_in":
		return 100
	case "waiting_sms", "need_captcha", "submitting", "sending", "logging_in", "login_form":
		return 80
	case "checking", "starting":
		return 60
	case "unknown", "idle":
		return 40
	case "error":
		return 20
	case "expired":
		return 10
	case "not_logged_in":
		return 0
	default:
		return -1
	}
}

// Compatibility helpers target the migrated default account.
func (p *Pool) CurrentPhone() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	if a := p.accounts["default"]; a != nil {
		return a.Phone
	}
	if len(p.order) > 0 && p.accounts[p.order[0]] != nil {
		return p.accounts[p.order[0]].Phone
	}
	return ""
}

func (p *Pool) SetPhone(phone string) error {
	phone = digitsOnly(phone)
	if len(phone) != 11 {
		return fmt.Errorf("手机号需为 11 位")
	}
	p.mu.Lock()
	id := "default"
	a := p.accounts[id]
	if a == nil && len(p.order) > 0 {
		id, a = p.order[0], p.accounts[p.order[0]]
	}
	if a == nil {
		p.mu.Unlock()
		_, err := p.Add(phone)
		return err
	}
	if p.workflowOwners[id] != "" {
		p.mu.Unlock()
		return ErrWorkflowBusy
	}
	a.Phone = phone
	a.UpdatedAt = time.Now().Format(time.RFC3339)
	m := p.runtimes[id]
	err := p.saveLocked()
	p.mu.Unlock()
	if m != nil {
		_ = m.SetPhone(phone)
	}
	return err
}

func (p *Pool) managerFor(id string) (*Manager, error) {
	p.mu.Lock()
	a := p.accounts[id]
	if a == nil {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号不存在")
	}
	if !a.Enabled {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号已停用")
	}
	lk := p.accountLockLocked(id)
	p.mu.Unlock()

	lk.Lock()
	defer lk.Unlock()
	return p.managerForLocked(id)
}

func (p *Pool) managerForLocked(id string) (*Manager, error) {
	return p.managerForLockedContext(context.Background(), id)
}

func (p *Pool) managerForLockedContext(ctx context.Context, id string) (*Manager, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	p.mu.Lock()
	if !p.workflowAccessAllowedLocked(id, ctx) {
		p.mu.Unlock()
		return nil, ErrWorkflowBusy
	}
	a := p.accounts[id]
	if a == nil {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号不存在")
	}
	if !a.Enabled {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号已停用")
	}
	previous := p.runtimes[id]
	if previous != nil && previous.Ready() {
		p.mu.Unlock()
		return previous, nil
	}
	if p.stopping {
		p.mu.Unlock()
		return nil, fmt.Errorf("服务正在停止")
	}
	profile, store := p.pathsFor(id)
	m := New(p.ChromePath, profile, a.Phone)
	m.workflowControlled = p.workflowOwners[id] != ""
	m.SessionStorePath = store
	p.runtimes[id] = m
	p.mu.Unlock()
	if previous != nil {
		previous.Stop()
	}
	// Register first so concurrent initializations count each other before
	// launching Chrome; only unattended question challenges can yield room.
	p.reclaimAutomaticChallengeCapacity(id)
	if err := m.StartContext(ctx); err != nil {
		p.mu.Lock()
		delete(p.runtimes, id)
		p.mu.Unlock()
		return nil, preparationStartError(ctx, err)
	}
	p.syncOne(id)
	return m, nil
}

func (p *Pool) accountLockLocked(id string) *sync.Mutex {
	if p.locks == nil {
		p.locks = map[string]*sync.Mutex{}
	}
	lk := p.locks[id]
	if lk == nil {
		lk = &sync.Mutex{}
		p.locks[id] = lk
	}
	return lk
}

func (p *Pool) acquireManagerSite(id, siteID string, try bool, contexts ...context.Context) (*Manager, error) {
	ctx := context.Background()
	if len(contexts) > 0 {
		ctx = contexts[0]
	}
	p.mu.Lock()
	if a := p.accounts[id]; a == nil {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号不存在")
	} else if !a.Enabled {
		p.mu.Unlock()
		return nil, fmt.Errorf("账号已停用")
	}
	if !p.workflowAccessAllowedLocked(id, ctx) {
		p.mu.Unlock()
		return nil, ErrWorkflowBusy
	}
	lk := p.accountLockLocked(id)
	p.mu.Unlock()

	if try {
		if !lk.TryLock() {
			return nil, fmt.Errorf("账号浏览器正在初始化")
		}
	} else {
		lk.Lock()
	}
	defer lk.Unlock()
	m, err := p.managerForLockedContext(ctx, id)
	if err != nil {
		return nil, err
	}
	if try {
		if !m.TryAcquireSite(siteID) {
			return nil, fmt.Errorf("账号站点正忙")
		}
	} else {
		m.AcquireSite(siteID)
	}
	return m, nil
}

func (p *Pool) stopIfCold(id string, m *Manager) {
	if id == "default" || m == nil {
		return
	}
	p.mu.Lock()
	lk := p.accountLockLocked(id)
	p.mu.Unlock()
	lk.Lock()
	defer lk.Unlock()
	p.mu.Lock()
	if p.runtimes[id] != m || m.Busy() {
		p.mu.Unlock()
		return
	}
	at := time.Now()
	reclaimAutomatic := len(p.runtimes) >= autoLoginBrowserLimit() && m.canReclaimAutomaticChallenge(at)
	// Saved captcha/SMS flags may come from a browser that was closed hours
	// ago. Only an active login in this runtime needs a live page; otherwise
	// rotating through the pool retains every Chrome and exhausts memory.
	if !reclaimAutomatic && (m.ShouldKeepWarm() || m.pendingLoginActive(at, pendingLoginGrace())) {
		p.mu.Unlock()
		return
	}
	delete(p.runtimes, id)
	p.mu.Unlock()
	m.Stop()
}

// The caller holds its own account lock. TryLock avoids waiting on another
// account's initialization or active operation while reclaiming spare room.
func (p *Pool) reclaimAutomaticChallengeCapacity(currentID string) {
	p.mu.Lock()
	if len(p.runtimes) <= autoLoginBrowserLimit() {
		p.mu.Unlock()
		return
	}
	type candidate struct {
		id      string
		manager *Manager
		lock    *sync.Mutex
	}
	var candidates []candidate
	for id, m := range p.runtimes {
		if id != currentID && id != "default" && m != nil {
			candidates = append(candidates, candidate{id, m, p.accountLockLocked(id)})
		}
	}
	p.mu.Unlock()
	for _, item := range candidates {
		if !item.lock.TryLock() {
			continue
		}
		p.mu.Lock()
		reclaim := len(p.runtimes) > autoLoginBrowserLimit() && p.runtimes[item.id] == item.manager && item.manager.canReclaimAutomaticChallenge(time.Now())
		if reclaim {
			delete(p.runtimes, item.id)
		}
		p.mu.Unlock()
		if reclaim {
			item.manager.Stop()
		}
		item.lock.Unlock()
	}
}

func (p *Pool) pathsFor(id string) (profile, store string) {
	if id == "default" {
		return p.LegacyProfile, filepath.Join(filepath.Dir(p.LegacyProfile), ".session-store.json")
	}
	root := filepath.Join(filepath.Dir(p.LegacyProfile), ".chrome-cn-ai-profiles", id)
	return root, filepath.Join(root, ".session-store.json")
}

// LoggedInAccountIDs lists enabled accounts persisted as logged_in for siteID.
// Unlike candidates, it does not advance the lease round-robin.
func (p *Pool) persistedStatus(id, siteID string) string {
	p.mu.Lock()
	defer p.mu.Unlock()
	a := p.accounts[id]
	if a == nil {
		return ""
	}
	st, ok := a.Sites[siteID]
	if !ok {
		return ""
	}
	return st.Status
}

// LoginCount is the number of enabled accounts whose saved status is logged_in.
type LoginCount struct {
	SiteID   string `json:"site_id"`
	Name     string `json:"name"`
	LoggedIn int    `json:"logged_in"`
	Enabled  int    `json:"enabled"`
}

func (p *Pool) LoginCounts() []LoginCount {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]LoginCount, 0, len(Sites))
	for _, site := range Sites {
		row := LoginCount{SiteID: site.ID, Name: site.Name}
		for _, id := range p.order {
			a := p.accounts[id]
			if a == nil || !a.Enabled {
				continue
			}
			row.Enabled++
			if st, ok := a.Sites[site.ID]; ok && st.Status == "logged_in" {
				row.LoggedIn++
			}
		}
		out = append(out, row)
	}
	return out
}

func (p *Pool) LoggedInAccountIDs(siteID string) []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []string
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled {
			continue
		}
		if st, ok := a.Sites[siteID]; ok && st.Status == "logged_in" {
			out = append(out, id)
		}
	}
	return out
}

func omitIDs(ids []string, skip map[string]bool) []string {
	if len(skip) == 0 || len(ids) == 0 {
		return ids
	}
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		if skip[id] {
			continue
		}
		out = append(out, id)
	}
	return out
}

func (p *Pool) candidates(siteID string) []string {
	p.syncAll()
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []string
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled {
			continue
		}
		if st, ok := a.Sites[siteID]; ok && st.Status == "logged_in" {
			out = append(out, id)
		}
	}
	if len(out) > 1 {
		start := p.rr[siteID] % len(out)
		out = append(out[start:], out[:start]...)
		p.rr[siteID] = (start + 1) % len(out)
	}
	return out
}

// callbackPhones lists numbers that actually receive the SMS inbound webhook.
// GEO_CALLBACK_PHONES is an explicit list; otherwise the Doubao callback number
// is the only one known to be wired. Accounts outside this set are skipped for
// fresh SMS login so a code is not sent to a handset that never calls back.
func callbackPhones() map[string]bool {
	raw := strings.TrimSpace(os.Getenv("GEO_CALLBACK_PHONES"))
	if raw == "" {
		raw = strings.TrimSpace(os.Getenv("GEO_DOUBAO_PHONE"))
	}
	out := map[string]bool{}
	for _, part := range strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == ';' || r == ' ' || r == '\n' || r == '\t'
	}) {
		d := digitsOnly(part)
		if len(d) > 11 {
			d = d[len(d)-11:]
		}
		if len(d) == 11 {
			out[d] = true
		}
	}
	return out
}

// shouldReconcileSite skips a startup check that cannot be selected for the
// site. Yuanbao is pinned to one handset, so another account's logged-out
// page must not be checked or logged beside that handset's job.
func shouldReconcileSite(phone, siteID string) bool {
	if siteID != "yuanbao" {
		return true
	}
	preferred := preferredSitePhone(siteID)
	if preferred == "" {
		return true
	}
	got := digitsOnly(phone)
	if len(got) > 11 {
		got = got[len(got)-11:]
	}
	return got == preferred
}

// preferredSitePhone pins a site to the handset that receives its SMS.
// Yuanbao login is wired to the same callback number as Doubao.
func preferredSitePhone(siteID string) string {
	if siteID != "yuanbao" {
		return ""
	}
	raw := strings.TrimSpace(os.Getenv("GEO_YUANBAO_PHONE"))
	if raw == "" {
		raw = ""
	}
	d := digitsOnly(raw)
	if len(d) > 11 {
		d = d[len(d)-11:]
	}
	if len(d) != 11 {
		return ""
	}
	return d
}

func accountWait() time.Duration {
	sec := 120
	if v := strings.TrimSpace(os.Getenv("GEO_ACCOUNT_WAIT_SEC")); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			if n < 0 {
				n = 0
			}
			if n > 600 {
				n = 600
			}
			sec = n
		}
	}
	return time.Duration(sec) * time.Second
}

func (p *Pool) loginCandidates(siteID string) []string {
	p.syncAll()
	p.mu.Lock()
	defer p.mu.Unlock()
	phones := callbackPhones()
	preferred := preferredSitePhone(siteID)
	var loggedIn, other []string
	consider := func(onlyCallback bool) {
		loggedIn, other = nil, nil
		for _, id := range p.order {
			a := p.accounts[id]
			if a == nil || !a.Enabled {
				continue
			}
			phone := digitsOnly(a.Phone)
			if len(phone) > 11 {
				phone = phone[len(phone)-11:]
			}
			if preferred != "" && phone != preferred {
				continue
			}
			if onlyCallback && !phones[phone] {
				continue
			}
			if st, ok := a.Sites[siteID]; ok && st.Status == "logged_in" {
				loggedIn = append(loggedIn, id)
			} else {
				other = append(other, id)
			}
		}
	}
	consider(len(phones) > 0)
	if preferred == "" && len(loggedIn)+len(other) == 0 {
		consider(false)
	}
	// The callback handset is the only one that can complete a fresh SMS login.
	// Once that handset is sitting on a captcha, another already-logged-in
	// account can still answer. Leaving the captcha account in front makes
	// risk-control failover lease the same phone again.
	if preferred == "" && len(loggedIn) == 0 {
		var extras []string
		for _, id := range p.order {
			a := p.accounts[id]
			if a == nil || !a.Enabled {
				continue
			}
			phone := digitsOnly(a.Phone)
			if len(phone) > 11 {
				phone = phone[len(phone)-11:]
			}
			if len(phones) > 0 && phones[phone] {
				continue
			}
			if st, ok := a.Sites[siteID]; ok && st.Status == "logged_in" {
				extras = append(extras, id)
			}
		}
		if len(extras) > 0 {
			loggedIn = extras
			other = nil
		}
	}
	if len(loggedIn) > 1 {
		key := siteID + ":login"
		start := p.rr[key] % len(loggedIn)
		loggedIn = append(loggedIn[start:], loggedIn[:start]...)
		p.rr[key] = (start + 1) % len(loggedIn)
	}
	return append(loggedIn, other...)
}

func (p *Pool) candidatesWithStatus(siteID, status string) []string {
	p.syncAll()
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]string, 0)
	for _, id := range p.order {
		a := p.accounts[id]
		if a == nil || !a.Enabled {
			continue
		}
		if st, ok := a.Sites[siteID]; ok && st.Status == status {
			out = append(out, id)
		}
	}
	return out
}

func (p *Pool) syncAll() {
	p.mu.Lock()
	ids := make([]string, 0, len(p.runtimes))
	for id := range p.runtimes {
		ids = append(ids, id)
	}
	p.mu.Unlock()
	for _, id := range ids {
		p.syncOne(id)
	}
}

func (p *Pool) syncOne(id string) {
	p.mu.Lock()
	m := p.runtimes[id]
	p.mu.Unlock()
	if m == nil || !m.Ready() {
		return
	}
	snap := m.Snapshot()
	raw, _ := json.Marshal(snap["sites"])
	var states []State
	if json.Unmarshal(raw, &states) != nil {
		return
	}
	p.mu.Lock()
	a := p.accounts[id]
	if a != nil {
		if a.Sites == nil {
			a.Sites = map[string]State{}
		}
		changed := false
		for _, st := range states {
			old := a.Sites[st.SiteID]
			st = mergeRuntimeState(old, st, a.Phone)
			if st != old {
				a.Sites[st.SiteID] = st
				changed = true
			}
		}
		if changed {
			a.UpdatedAt = time.Now().Format(time.RFC3339)
			_ = p.saveLocked()
		}
	}
	p.mu.Unlock()
}

func mergeRuntimeState(old, current State, phone string) State {
	if current.Status == "idle" && old.Status != "" && old.Status != "starting" {
		switch old.Status {
		case "checking", "logging_in", "sending", "submitting", "waiting_sms":
			old.Status = "unknown"
			old.Message = "服务重启中断了登录流程，请重新检测"
			old.Evidence = "登录流程未完成即发生服务重启"
		}
		current = old
	} else {
		if current.LastCheckedAt == "" {
			current.LastCheckedAt = old.LastCheckedAt
		}
		if current.LastLoginAt == "" {
			current.LastLoginAt = old.LastLoginAt
		}
		if current.URL == "" && current.Status == old.Status {
			current.URL = old.URL
		}
	}
	current.Phone = phone
	return current
}

func (p *Pool) markState(id, siteID, status, message string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	a := p.accounts[id]
	if a == nil {
		return
	}
	st, ok := a.Sites[siteID]
	if !ok {
		site, exists := SiteByID(siteID)
		if !exists {
			return
		}
		st = State{SiteID: site.ID, Name: site.Name, Phone: a.Phone}
	}
	st.Status = status
	st.Message = message
	st.UpdatedAt = now()
	st.LastCheckedAt = now()
	if status == "logged_in" {
		st.LastLoginAt = now()
	}
	a.Sites[siteID] = st
	a.UpdatedAt = time.Now().Format(time.RFC3339)
	_ = p.saveLocked()
}

func (p *Pool) load() bool {
	raw, err := os.ReadFile(p.DataPath)
	if err != nil {
		// Only a missing store permits first-run default migration. An
		// unreadable store must not be replaced with a new default account.
		if !os.IsNotExist(err) {
			log.Printf("account store unavailable: %v", err)
			return true
		}
		return false
	}
	var store poolStore
	if err := json.Unmarshal(raw, &store); err != nil {
		log.Printf("account store invalid JSON: %v", err)
		return true
	}
	for _, a := range store.Accounts {
		if a == nil || a.ID == "" || len(digitsOnly(a.Phone)) != 11 {
			continue
		}
		if a.Sites == nil {
			a.Sites = map[string]State{}
		}
		for _, site := range Sites {
			if st, ok := a.Sites[site.ID]; ok {
				if isInterruptedLoginStatus(st.Status) {
					st.Status = "unknown"
					st.Message = "服务重启中断了登录流程，正在重新检测"
					st.Evidence = "登录流程未完成即发生服务重启"
					st.LastSendAt = 0
					st.UpdatedAt = now()
					a.Sites[site.ID] = st
				}
			} else {
				a.Sites[site.ID] = State{SiteID: site.ID, Name: site.Name, Status: "not_logged_in", Phone: a.Phone}
			}
		}
		p.accounts[a.ID] = a
		p.order = append(p.order, a.ID)
	}
	sort.SliceStable(p.order, func(i, j int) bool {
		return p.accounts[p.order[i]].CreatedAt < p.accounts[p.order[j]].CreatedAt
	})
	return true
}

func isInterruptedLoginStatus(status string) bool {
	switch status {
	case "checking", "logging_in", "sending", "submitting", "waiting_sms", "login_form":
		return true
	default:
		return false
	}
}

func (p *Pool) save() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.saveLocked()
}

func (p *Pool) saveLocked() error {
	if p.DataPath == "" {
		return nil
	}
	store := poolStore{Accounts: make([]*Account, 0, len(p.order))}
	for _, id := range p.order {
		if a := p.accounts[id]; a != nil {
			store.Accounts = append(store.Accounts, a)
		}
	}
	raw, err := json.MarshalIndent(store, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p.DataPath), 0o755); err != nil {
		return err
	}
	tmp := p.DataPath + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p.DataPath)
}

func copyAccount(a *Account) *Account {
	if a == nil {
		return nil
	}
	cp := *a
	cp.Sites = make(map[string]State, len(a.Sites))
	for k, v := range a.Sites {
		cp.Sites[k] = v
	}
	return &cp
}

func accountID() (string, error) {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "acct_" + hex.EncodeToString(b), nil
}

func splitPhones(raw string) []string {
	return strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == '，' || r == '\n' || r == '\r' || r == ';' || r == '；' || r == ' ' || r == '\t'
	})
}
