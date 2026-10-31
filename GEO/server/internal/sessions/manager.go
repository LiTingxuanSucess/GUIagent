package sessions

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/cdproto/emulation"
	"github.com/chromedp/cdproto/input"
	cdpmem "github.com/chromedp/cdproto/memory"
	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/cdproto/storage"
	"github.com/chromedp/cdproto/target"
	"github.com/chromedp/chromedp"

	"geo-server/internal/procguard"
)

type State struct {
	SiteID           string  `json:"site_id"`
	Name             string  `json:"name"`
	Status           string  `json:"status"`
	URL              string  `json:"url"`
	Message          string  `json:"message"`
	Evidence         string  `json:"evidence,omitempty"`
	Phone            string  `json:"phone"`
	LastSendAt       float64 `json:"last_send_at"`
	LastSendTargetID string  `json:"last_send_target_id,omitempty"`
	LastCheckedAt    string  `json:"last_checked_at,omitempty"`
	LastLoginAt      string  `json:"last_login_at,omitempty"`
	UpdatedAt        string  `json:"updated_at"`
}

type Manager struct {
	ChromePath       string
	ProfileDir       string
	SessionStorePath string
	Phone            string

	mu                 sync.Mutex
	cancel             context.CancelFunc
	browserCtx         context.Context
	ctrlCtx            context.Context
	tabs               map[string]context.Context
	tabCancels         map[string]context.CancelFunc
	opened             map[string]bool
	states             map[string]*State
	siteMu             map[string]*sync.Mutex
	tabInitMu          sync.Mutex
	dumpMu             sync.Mutex
	stopOnce           sync.Once
	busy               map[string]bool
	pendingLS          map[string][][2]string
	done               chan struct{}
	ready              bool
	started            bool
	externalChrome     bool
	workflowControlled bool
	workflowTargets    map[string]string
	cdpMu              sync.Mutex
	dumpFails          int
	dumpOff            bool
	keepWarmTo         time.Time
	autoWarmTo         time.Time
	autoCaptcha        map[string]bool
	pendingSince       map[string]time.Time
}

func New(chromePath, profileDir, phone string) *Manager {
	st := map[string]*State{}
	for _, s := range Sites {
		st[s.ID] = &State{SiteID: s.ID, Name: s.Name, Status: "starting", Phone: phone, UpdatedAt: now()}
	}
	locks := map[string]*sync.Mutex{}
	for _, s := range Sites {
		locks[s.ID] = &sync.Mutex{}
	}
	return &Manager{
		ChromePath: chromePath,
		ProfileDir: profileDir,
		Phone:      phone,
		tabs:       map[string]context.Context{},
		tabCancels: map[string]context.CancelFunc{},
		opened:     map[string]bool{},
		states:     st,
		siteMu:     locks,
		busy:       map[string]bool{},
		pendingLS:  map[string][][2]string{},
		done:       make(chan struct{}),
	}
}

func (m *Manager) Start() error {
	return m.StartContext(context.Background())
}

func (m *Manager) StartContext(parent context.Context) (err error) {
	if err := parent.Err(); err != nil {
		return err
	}
	m.mu.Lock()
	if m.started {
		m.mu.Unlock()
		return nil
	}
	m.started = true
	m.mu.Unlock()

	if err := os.MkdirAll(m.ProfileDir, 0o700); err != nil {
		return err
	}
	_ = os.Chmod(m.ProfileDir, 0o700)
	external, err := macCDPMode()
	if err != nil {
		return err
	}
	var allocCtx context.Context
	var cancelAlloc context.CancelFunc
	reused := false
	if external {
		var wsURL string
		wsURL, reused, err = m.ensureMacChrome(parent)
		if err != nil {
			return err
		}
		allocCtx, cancelAlloc = chromedp.NewRemoteAllocator(context.Background(), wsURL, chromedp.NoModifyURL)
	} else {
		for _, name := range []string{"SingletonLock", "SingletonCookie", "SingletonSocket"} {
			_ = os.Remove(filepath.Join(m.ProfileDir, name))
		}
		markChromeClean(m.ProfileDir)
		enableChromeMemorySaver(m.ProfileDir)
		purgeChromeSessions(m.ProfileDir)
		opts := append(chromedp.DefaultExecAllocatorOptions[:],
			// Run headed under Xvfb. Doubao and Kimi accept the SMS code in
			// headless mode but immediately invalidate the resulting session.
			chromedp.Flag("headless", false),
			// Default chromedp turns on enable-automation, which sets
			// navigator.webdriver. Kimi's slider then rejects a real drag.
			chromedp.Flag("enable-automation", false),
			chromedp.Flag("disable-blink-features", "AutomationControlled"),
			chromedp.Flag("no-sandbox", true),
			chromedp.Flag("disable-dev-shm-usage", true),
			chromedp.Flag("disable-gpu", true),
			chromedp.Flag("disable-software-rasterizer", true),
			chromedp.Flag("disable-webgl", true),
			chromedp.Flag("disable-3d-apis", true),
			chromedp.Flag("ignore-certificate-errors", true),
			chromedp.Flag("no-first-run", true),
			chromedp.Flag("no-default-browser-check", true),
			chromedp.Flag("hide-crash-restore-bubble", true),
			chromedp.Flag("disable-session-crashed-bubble", true),
			chromedp.Flag("restore-on-startup", "5"),
			// chromedp 默认会关掉后台节流，6 个页会一直按前台算内存。false = 不传该 flag。
			chromedp.Flag("disable-renderer-backgrounding", false),
			chromedp.Flag("disable-backgrounding-occluded-windows", false),
			chromedp.Flag("disable-background-timer-throttling", false),
			chromedp.Flag("enable-low-end-device-mode", true),
			chromedp.Flag("disable-site-isolation-trials", true),
			chromedp.Flag("process-per-site", true),
			chromedp.Flag("renderer-process-limit", "4"),
			chromedp.Flag("js-flags", "--max-old-space-size=192 --optimize-for-size"),
			chromedp.Flag("disable-features", "IsolateOrigins,site-per-process,Translate,BlinkGenPropertyTrees,BackForwardCache,MediaRouter"),
			chromedp.Flag("enable-features", "NetworkService,NetworkServiceInProcess,HighEfficiencyModeAvailable"),
			chromedp.Flag("mute-audio", true),
			chromedp.Flag("remote-allow-origins", "*"),
			chromedp.UserDataDir(m.ProfileDir),
			chromedp.WindowSize(1280, 800),
			chromedp.WSURLReadTimeout(90*time.Second),
			chromedp.Env("LANG=zh_CN.UTF-8"),
		)
		if m.ChromePath != "" {
			opts = append(opts, chromedp.ExecPath(m.ChromePath))
		}
		allocCtx, cancelAlloc = chromedp.NewExecAllocator(context.Background(), opts...)
	}
	browserCtx, cancelBrowser := chromedp.NewContext(allocCtx, chromedp.WithLogf(log.Printf))
	m.externalChrome = external
	cancelConnection := func() {
		cancelBrowser()
		cancelAlloc()
		if external {
			// Site contexts outlive the control context. Disconnect first so
			// chromedp's cancellation cleanup cannot close the user's tabs.
			if c := chromedp.FromContext(browserCtx); c != nil && c.Browser != nil {
				<-c.Browser.LostConnection
			}
			m.mu.Lock()
			cancels := make([]context.CancelFunc, 0, len(m.tabCancels))
			for _, cancel := range m.tabCancels {
				cancels = append(cancels, cancel)
			}
			m.mu.Unlock()
			for _, cancel := range cancels {
				cancel()
			}
		}
	}
	stopDeadline := context.AfterFunc(parent, cancelConnection)
	defer func() {
		stopDeadline()
		if parent.Err() != nil {
			m.mu.Lock()
			m.ready = false
			m.mu.Unlock()
			err = parent.Err()
		}
	}()
	if err := chromedp.Run(browserCtx); err != nil {
		cancelConnection()
		return fmt.Errorf("start chrome: %w", err)
	}
	if c := chromedp.FromContext(browserCtx); c != nil && c.Browser != nil {
		if p := c.Browser.Process(); p != nil {
			procguard.Adopt(p.Pid)
		}
	}
	m.mu.Lock()
	m.browserCtx = browserCtx
	m.cancel = cancelConnection
	m.mu.Unlock()
	tAll := time.Now()
	t0 := time.Now()
	if !reused {
		m.restoreSession()
	}
	log.Printf("profile restore_cookies %dms", time.Since(t0).Milliseconds())

	ctrlCtx := browserCtx
	if !external {
		ctrlCtx, _ = chromedp.NewContext(browserCtx)
	}
	// 首次 Run 必须使用长期 context。若用带超时的子 context 初始化，
	// 子 context cancel 时会同时关闭刚创建的 target，后续页签只会得到空 URL。
	if err := chromedp.Run(ctrlCtx, navigateNoWait("about:blank")); err != nil {
		log.Printf("control tab: %v", err)
	}
	m.mu.Lock()
	m.ctrlCtx = ctrlCtx
	m.mu.Unlock()

	for _, site := range Sites {
		tabCtx, tabCancel := chromedp.NewContext(m.siteContextParent(browserCtx))
		m.mu.Lock()
		m.tabs[site.ID] = tabCtx
		m.tabCancels[site.ID] = tabCancel
		m.setLocked(site.ID, "idle", "未打开，发题或截图时再加载")
		m.mu.Unlock()
	}
	m.applyCookieLoginGuess()
	m.mu.Lock()
	m.ready = true
	m.mu.Unlock()
	log.Printf("profile ready dir=%s total_ms=%d (lazy tabs, no site navigate)", filepath.Base(m.ProfileDir), time.Since(tAll).Milliseconds())
	go m.dumpSession()
	go m.watch()
	go m.flushLoop()
	return nil
}

func (m *Manager) Stop() {
	m.stopOnce.Do(func() {
		m.mu.Lock()
		m.ready = false
		ctx := m.browserCtx
		cancel := m.cancel
		m.cancel = nil
		m.mu.Unlock()
		close(m.done)

		m.dumpSession()
		if ctx != nil && !m.externalChrome {
			tctx, tcancel := context.WithTimeout(ctx, 5*time.Second)
			_ = chromedp.Run(tctx, chromedp.ActionFunc(func(c context.Context) error {
				return browser.Close().Do(c)
			}))
			tcancel()
		}
		if cancel != nil {
			cancel()
		}
	})
}

func markChromeClean(profileDir string) {
	pref := filepath.Join(profileDir, "Default", "Preferences")
	raw, err := os.ReadFile(pref)
	if err != nil {
		return
	}
	var obj map[string]any
	if json.Unmarshal(raw, &obj) != nil {
		return
	}
	prof, _ := obj["profile"].(map[string]any)
	if prof == nil {
		prof = map[string]any{}
		obj["profile"] = prof
	}
	prof["exit_type"] = "Normal"
	prof["exited_cleanly"] = true
	sess, _ := obj["session"].(map[string]any)
	if sess == nil {
		sess = map[string]any{}
		obj["session"] = sess
	}
	sess["restore_on_startup"] = 5
	out, err := json.Marshal(obj)
	if err != nil {
		return
	}
	_ = os.WriteFile(pref, out, 0o644)
}

func enableChromeMemorySaver(profileDir string) {
	mergeJSON := func(path string, patch func(map[string]any)) {
		obj := map[string]any{}
		if raw, err := os.ReadFile(path); err == nil {
			_ = json.Unmarshal(raw, &obj)
		}
		patch(obj)
		out, err := json.Marshal(obj)
		if err != nil {
			return
		}
		_ = os.WriteFile(path, out, 0o644)
	}
	mergeJSON(filepath.Join(profileDir, "Local State"), func(obj map[string]any) {
		perf, _ := obj["performance_tuning"].(map[string]any)
		if perf == nil {
			perf = map[string]any{}
			obj["performance_tuning"] = perf
		}
		he, _ := perf["high_efficiency_mode"].(map[string]any)
		if he == nil {
			he = map[string]any{}
			perf["high_efficiency_mode"] = he
		}
		he["state"] = 2
	})
}

func purgeChromeSessions(profileDir string) {
	def := filepath.Join(profileDir, "Default")
	_ = os.RemoveAll(filepath.Join(def, "Sessions"))
	for _, name := range []string{"Current Session", "Current Tabs", "Last Session", "Last Tabs"} {
		_ = os.Remove(filepath.Join(def, name))
	}
}

func waitForVisiblePage(ctx context.Context) {
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		var ready bool
		err := runTab(ctx, 2*time.Second, chromedp.Evaluate(`(() => {
			const t = ((document.body && document.body.innerText) || "").replace(/\s+/g, " ").trim();
			return t.length > 20 && /登录|未登录|验证码|请输入|新对话|发送|安全验证/.test(t);
		})()`, &ready))
		if err == nil && ready {
			return
		}
		time.Sleep(400 * time.Millisecond)
	}
}

func pageOpenBudget() time.Duration {
	sec := 45
	if raw := strings.TrimSpace(os.Getenv("GEO_PAGE_OPEN_SEC")); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil {
			sec = n
		}
	}
	if sec < 15 {
		sec = 15
	}
	if sec > 120 {
		sec = 120
	}
	return time.Duration(sec) * time.Second
}

func runTab(ctx context.Context, d time.Duration, actions ...chromedp.Action) error {
	tctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	return chromedp.Run(tctx, actions...)
}

func (m *Manager) hibernateIfIdle(siteID string, tabCtx context.Context) {
	if m.externalChrome || m.workflowIsControlled() {
		return
	}
	siteLock := m.lockFor(siteID)
	if !siteLock.TryLock() {
		return
	}
	defer siteLock.Unlock()
	m.mu.Lock()
	st := m.states[siteID]
	status := ""
	if st != nil {
		status = st.Status
	}
	m.mu.Unlock()
	if keepHot(status) || m.ShouldKeepWarm() {
		return
	}
	err := runTab(tabCtx, 3*time.Second, chromedp.ActionFunc(func(ctx context.Context) error {
		if e := page.SetWebLifecycleState(page.SetWebLifecycleStateStateFrozen).Do(ctx); e != nil {
			return e
		}
		_ = cdpmem.ForciblyPurgeJavaScriptMemory().Do(ctx)
		return nil
	}))
	if err != nil {
		log.Printf("freeze %s: %v", siteID, err)
		return
	}
	log.Printf("froze idle tab %s", siteID)
}

func keepHot(status string) bool {
	switch status {
	case "logged_in", "waiting_sms", "need_captcha", "login_form", "sending", "submitting", "recovery_pending":
		return true
	}
	return false
}

func navigateNoWait(urlstr string) chromedp.Action {
	return chromedp.ActionFunc(func(ctx context.Context) error {
		_, _, errorText, err := page.Navigate(urlstr).Do(ctx)
		if err != nil {
			return err
		}
		if errorText != "" {
			return fmt.Errorf("page load error %s", errorText)
		}
		return nil
	})
}

func (m *Manager) pruneExtraTabs() {
	m.mu.Lock()
	keep := map[target.ID]bool{}
	if c := chromedp.FromContext(m.browserCtx); c != nil && c.Target != nil {
		keep[c.Target.TargetID] = true
	}
	if c := chromedp.FromContext(m.ctrlCtx); c != nil && c.Target != nil {
		keep[c.Target.TargetID] = true
	}
	for _, ctx := range m.tabs {
		if c := chromedp.FromContext(ctx); c != nil && c.Target != nil {
			keep[c.Target.TargetID] = true
		}
	}
	m.mu.Unlock()
	listCtx := m.liveCtx()
	if listCtx == nil {
		return
	}
	tctx, cancel := context.WithTimeout(listCtx, 3*time.Second)
	defer cancel()
	infos, err := target.GetTargets().Do(tctx)
	if err != nil {
		log.Printf("list chrome tabs: %v", err)
		return
	}
	closed := 0
	pages := 0
	for _, t := range infos {
		if t.Type != "page" {
			continue
		}
		pages++
		if keep[t.TargetID] {
			continue
		}
		if err := target.CloseTarget(t.TargetID).Do(tctx); err == nil {
			closed++
		}
	}
	log.Printf("chrome tabs: keep=%d closed=%d total_pages=%d", len(keep), closed, pages)
}

func (m *Manager) Ready() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.ready {
		return false
	}
	if m.browserCtx != nil {
		if c := chromedp.FromContext(m.browserCtx); c != nil && c.Browser != nil {
			select {
			case <-c.Browser.LostConnection:
				return false
			default:
			}
		}
	}
	return true
}

func (m *Manager) lockFor(siteID string) *sync.Mutex {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.siteMu[siteID] == nil {
		m.siteMu[siteID] = &sync.Mutex{}
	}
	return m.siteMu[siteID]
}

func (m *Manager) AcquireSite(siteID string) {
	m.lockFor(siteID).Lock()
	m.MarkBusy(siteID, true)
}

func (m *Manager) TryAcquireSite(siteID string) bool {
	if !m.lockFor(siteID).TryLock() {
		return false
	}
	m.MarkBusy(siteID, true)
	return true
}

func (m *Manager) ReleaseSite(siteID string) {
	m.MarkBusy(siteID, false)
	m.lockFor(siteID).Unlock()
	if ctx, err := m.tab(siteID); err == nil {
		go m.hibernateIfIdle(siteID, ctx)
	}
}

func (m *Manager) MarkBusy(siteID string, busy bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if busy {
		m.busy[siteID] = true
	} else {
		delete(m.busy, siteID)
	}
}

func (m *Manager) isBusy(siteID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.busy[siteID]
}

func (m *Manager) Busy() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.busy) > 0
}

func (m *Manager) KeepWarmFor(duration time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	until := time.Now().Add(duration)
	if until.After(m.keepWarmTo) {
		m.keepWarmTo = until
	}
}

func (m *Manager) ShouldKeepWarm() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	at := time.Now()
	return at.Before(m.keepWarmTo) || at.Before(m.autoWarmTo)
}

func (m *Manager) keepAutomaticChallengeWarmFor(siteID string, duration time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.autoCaptcha == nil {
		m.autoCaptcha = map[string]bool{}
	}
	m.autoCaptcha[siteID] = true
	if until := time.Now().Add(duration); until.After(m.autoWarmTo) {
		m.autoWarmTo = until
	}
}

// An unanswered question's challenge may yield browser capacity to another
// account. Operator windows and every other pending login still take priority.
func (m *Manager) canReclaimAutomaticChallenge(at time.Time) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.busy) > 0 || at.Before(m.keepWarmTo) {
		return false
	}
	found := false
	for siteID, st := range m.states {
		if st == nil || !pendingLoginStatus(st.Status) {
			continue
		}
		if st.Status != "need_captcha" || !m.autoCaptcha[siteID] {
			return false
		}
		found = true
	}
	return found
}

func (m *Manager) CDPEndpoint() (string, error) {
	endpoint, _, err := profileCDPAddress(m.ProfileDir)
	return endpoint, err
}

func (m *Manager) Snapshot() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	sites := make([]State, 0, len(Sites))
	for _, s := range Sites {
		st := *m.states[s.ID]
		st.Phone = m.Phone
		sites = append(sites, st)
	}
	return map[string]any{"phone": m.Phone, "ready": m.ready, "sites": sites}
}

func (m *Manager) CurrentPhone() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.Phone
}

func (m *Manager) WaitingSMSSites() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]string, 0, 2)
	for _, s := range Sites {
		st := m.states[s.ID]
		if st != nil && st.Status == "waiting_sms" {
			out = append(out, s.ID)
		}
	}
	return out
}

func (m *Manager) SetPhone(phone string) error {
	phone = digitsOnly(phone)
	if len(phone) != 11 {
		return fmt.Errorf("手机号需为 11 位")
	}
	m.mu.Lock()
	m.Phone = phone
	for _, st := range m.states {
		st.Phone = phone
	}
	m.mu.Unlock()
	return nil
}

func (m *Manager) Screenshot(siteID string) ([]byte, error) {
	if err := m.EnsurePage(siteID); err != nil {
		return nil, err
	}
	ctx, err := m.tab(siteID)
	if err != nil {
		return nil, err
	}
	waitForVisiblePage(ctx)
	var (
		buf     []byte
		lastErr error
	)
	// A freshly opened site may replace its initial target while redirecting.
	// Retry briefly so the admin panel does not show a broken image during
	// that normal transition.
	for attempt := 1; attempt <= 6; attempt++ {
		buf = nil
		lastErr = runTab(ctx, 5*time.Second, chromedp.CaptureScreenshot(&buf))
		if lastErr == nil && len(buf) > 0 {
			return buf, nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("截图结果为空")
	}
	return nil, lastErr
}

func (m *Manager) ClickNorm(siteID string, nx, ny float64) (*State, error) {
	if err := m.EnsurePage(siteID); err != nil {
		return nil, err
	}
	ctx, err := m.tab(siteID)
	if err != nil {
		return nil, err
	}
	if nx < 0 {
		nx = 0
	}
	if nx > 1 {
		nx = 1
	}
	if ny < 0 {
		ny = 0
	}
	if ny > 1 {
		ny = 1
	}
	var wh []float64
	_ = runTab(ctx, 3*time.Second, chromedp.Evaluate(`([window.innerWidth, window.innerHeight])`, &wh))
	w, h := 1280.0, 800.0
	if len(wh) == 2 {
		w, h = wh[0], wh[1]
	}
	err = runTab(ctx, 5*time.Second, chromedp.MouseClickXY(w*nx, h*ny), chromedp.Sleep(800*time.Millisecond))
	if err != nil {
		return nil, err
	}
	return m.Refresh(siteID), nil
}

func (m *Manager) DragNorm(siteID string, x1, y1, x2, y2 float64) (*State, error) {
	if err := m.EnsurePage(siteID); err != nil {
		return nil, err
	}
	ctx, err := m.tab(siteID)
	if err != nil {
		return nil, err
	}
	x1, y1, x2, y2 = clamp01(x1), clamp01(y1), clamp01(x2), clamp01(y2)
	var wh []float64
	_ = runTab(ctx, 3*time.Second, chromedp.Evaluate(`([window.innerWidth, window.innerHeight])`, &wh))
	w, h := 1280.0, 800.0
	if len(wh) == 2 {
		w, h = wh[0], wh[1]
	}
	const steps = 28
	actions := []chromedp.Action{
		mouseButton(input.MousePressed, w*x1, h*y1, 1),
		chromedp.Sleep(40 * time.Millisecond),
	}
	for i := 1; i <= steps; i++ {
		t := float64(i) / float64(steps)
		x := w * (x1 + (x2-x1)*t)
		y := h * (y1 + (y2-y1)*t)
		actions = append(actions,
			mouseButton(input.MouseMoved, x, y, 1),
			chromedp.Sleep(35*time.Millisecond),
		)
	}
	actions = append(actions,
		mouseButton(input.MouseReleased, w*x2, h*y2, 0),
		chromedp.Sleep(500*time.Millisecond),
	)
	if err = runTab(ctx, 12*time.Second, actions...); err != nil {
		return nil, err
	}
	return m.Refresh(siteID), nil
}

func clamp01(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 1 {
		return 1
	}
	return v
}

func mouseButton(kind input.MouseType, x, y float64, buttons int64) chromedp.Action {
	return chromedp.ActionFunc(func(ctx context.Context) error {
		return input.DispatchMouseEvent(kind, x, y).
			WithButton(input.Left).
			WithButtons(buttons).
			Do(ctx)
	})
}

func (m *Manager) SendCode(siteID string, force bool) (*State, error) {
	return m.sendCode(siteID, force, 45*time.Minute)
}

func (m *Manager) sendCode(siteID string, force bool, warmFor time.Duration) (*State, error) {
	if err := m.EnsurePage(siteID); err != nil {
		return nil, err
	}
	if st := m.Refresh(siteID); st != nil && st.Status == "logged_in" {
		return st, nil
	}
	site, ok := SiteByID(siteID)
	if !ok {
		return nil, fmt.Errorf("unknown site")
	}
	ctx, err := m.tab(siteID)
	if err != nil {
		return nil, err
	}
	m.mu.Lock()
	st := m.states[siteID]
	if st.Status == "logged_in" {
		cp := *st
		m.mu.Unlock()
		return &cp, nil
	}
	if !force && st.LastSendAt > 0 && nowUnix()-st.LastSendAt < 300 {
		st.Message = "发码冷却中，请稍候或直接填短信"
		st.UpdatedAt = now()
		cp := *st
		m.mu.Unlock()
		return &cp, nil
	}
	for otherID, other := range m.states {
		if otherID == siteID || other == nil {
			continue
		}
		switch other.Status {
		case "waiting_sms", "sending", "need_captcha", "submitting", "logging_in":
			cp := *st
			cp.Message = "同一账号正在登录其他平台，完成后再发码"
			cp.UpdatedAt = now()
			m.mu.Unlock()
			return &cp, nil
		}
	}
	m.setLocked(siteID, "sending", "准备登录页并发送验证码")
	phone := m.Phone
	m.mu.Unlock()
	if siteID == "yuanbao" {
		m.KeepWarmFor(warmFor)
	}

	if err := m.ensureLoginForm(ctx, site); err != nil {
		return m.set(siteID, "login_form", "打开手机号登录页失败，请查看截图手动处理: "+err.Error()), nil
	}
	text, _ := pageText(ctx)
	if captchaOnPage(ctx, text) {
		return m.set(siteID, "need_captcha", "请在截图上点击验证码"), nil
	}
	if err := fillPhone(ctx, siteID, phone); err != nil {
		text, _ = pageText(ctx)
		if captchaOnPage(ctx, text) {
			return m.set(siteID, "need_captcha", "请在截图上点击验证码"), nil
		}
		return m.set(siteID, "login_form", "未找到手机号输入框，请查看截图切换到手机号登录: "+err.Error()), nil
	}
	agreeIfNeeded(ctx)
	text, _ = pageText(ctx)
	if captchaOnPage(ctx, text) {
		return m.set(siteID, "need_captcha", "请在截图上点击验证码"), nil
	}
	sendTarget := managerSiteTargetID(m, siteID)
	if !clickSendCode(ctx) {
		text, _ = pageText(ctx)
		if captchaOnPage(ctx, text) {
			return m.set(siteID, "need_captcha", "请先在截图中完成安全验证"), nil
		}
		return m.set(siteID, "login_form", "手机号已填写，但未找到发送验证码按钮，请在截图中点击"), nil
	}
	m.mu.Lock()
	m.states[siteID].LastSendAt = nowUnix()
	m.states[siteID].LastSendTargetID = sendTarget
	m.mu.Unlock()
	clickDialogs(ctx)
	for attempt := 0; attempt < 8; attempt++ {
		text, _ = pageText(ctx)
		if strings.Contains(text, "尚未注册") || strings.Contains(text, "请先完成注册") {
			_, _ = runJSBool(ctx, clickTextJS([]string{"同意并注册"}))
		}
		// 百度移动登录的“验证码登录”是发码动作，不是验证码输入框。
		// 前面的“下一步/同意并注册”完成后还必须再点一次它。
		if siteID == "yiyan" && strings.Contains(text, "验证码登录") && !hasCodeForm(ctx) {
			if clicked, _ := runJSBool(ctx, clickTextJS([]string{"验证码登录"})); clicked {
				time.Sleep(1200 * time.Millisecond)
				continue
			}
		}
		if captchaOnPage(ctx, text) {
			return m.set(siteID, "need_captcha", "已点击发送，请在截图上完成安全验证"), nil
		}
		if waitingSMS(text) || strings.Contains(text, "验证码已发送") || hasCodeForm(ctx) {
			return m.set(siteID, "waiting_sms", "验证码已发送，请填写收到的短信验证码"), nil
		}
		time.Sleep(750 * time.Millisecond)
	}
	return m.set(siteID, "login_form", "已点击发送，但平台未返回明确结果；请查看截图或稍后重试"), nil
}

func (m *Manager) SubmitCode(siteID, code string) (*State, error) {
	return m.submitCode(siteID, code, false)
}

// Recovery owns an existing target. Unlike the normal login flow, a callback
// cannot navigate home or reopen a login form if that target changed state.
func (m *Manager) submitRecoveryCode(siteID, code string) (*State, error) {
	return m.submitCode(siteID, code, true)
}

func (m *Manager) submitCode(siteID, code string, recovery bool) (*State, error) {
	if !recovery {
		if err := m.EnsurePage(siteID); err != nil {
			return nil, err
		}
	}
	code = digitsOnly(code)
	if len(code) < 4 {
		return nil, fmt.Errorf("验证码太短")
	}
	site, ok := SiteByID(siteID)
	if !ok {
		return nil, fmt.Errorf("unknown site")
	}
	ctx, err := m.tab(siteID)
	if err != nil {
		return nil, err
	}
	if recovery && !hasCodeForm(ctx) {
		return nil, fmt.Errorf("当前恢复页面没有短信验证码输入框，未导航或重新发码")
	}
	var (
		doubaoResponses <-chan []byte
		stopDoubaoWatch context.CancelFunc
	)
	if siteID == "doubao" {
		doubaoResponses, stopDoubaoWatch, err = watchDoubaoLoginResponses(ctx)
		if err != nil {
			log.Printf("watch doubao login response: %v", err)
		} else {
			defer stopDoubaoWatch()
		}
	}
	m.set(siteID, "submitting", "正在提交验证码")
	if !recovery && !hasCodeForm(ctx) {
		_ = m.ensureLoginForm(ctx, site)
		_ = fillPhone(ctx, siteID, m.Phone)
		agreeIfNeeded(ctx)
		if !hasCodeForm(ctx) {
			_, _ = runJSBool(ctx, clickTextJS([]string{"下一步"}))
			time.Sleep(1500 * time.Millisecond)
			clickDialogs(ctx)
		}
	}
	if !hasCodeForm(ctx) {
		return m.set(siteID, "login_form", "还没到验证码页，可点截图或等短信页出现后再提交"), nil
	}
	if err := fillCode(ctx, siteID, code); err != nil {
		return m.set(siteID, "login_form", "验证码填入失败: "+err.Error()), nil
	}
	if siteID == "doubao" {
		// 豆包的单个 decimal 输入框在第 6 位时自动提交。此时再按通用
		// “登录”按钮会误点页面右上角入口，重新打开登录弹窗。
		if doubaoResponses != nil {
			if message, rejected := waitDoubaoLoginResponse(doubaoResponses, 6*time.Second); rejected {
				return m.set(siteID, "waiting_sms", message), nil
			}
		}
		// 一个手机号绑定多个豆包账号时，短信校验成功后会先展示账号选择页。
		// 选择页不是登录表单，必须点击账号卡片才能真正建立会话。
		selectDoubaoBoundAccount(ctx)
		return m.waitForLogin(siteID, 8*time.Second), nil
	} else if siteID == "yiyan" {
		// 文心的 6 格验证码会自动提交，页面中的“验证码登录”是重新
		// 发码按钮，不能把它当成提交按钮。
		time.Sleep(3500 * time.Millisecond)
	} else if siteID == "yuanbao" {
		clickYuanbaoLogin(ctx)
		return m.waitForLogin(siteID, 12*time.Second), nil
	} else if siteID == "kimi" {
		clickSubmit(ctx)
		// Kimi briefly removes the login dialog before the account session is
		// committed. A one-shot Refresh in that gap reports a false success.
		return m.waitForLogin(siteID, 12*time.Second), nil
	} else {
		clickSubmit(ctx)
		time.Sleep(1800 * time.Millisecond)
	}
	text, _ := pageText(ctx)
	if strings.Contains(text, "完善账号信息") || strings.Contains(text, "请设置用户名") {
		_, _ = runJSBool(ctx, clickTextJS([]string{"跳过"}))
		time.Sleep(2000 * time.Millisecond)
	}
	_, _ = runJSBool(ctx, clickTextJS([]string{"知道了"}))
	return m.Refresh(siteID), nil
}

// ClearSiteSession removes only the browser data owned by one platform. This
// is useful when a platform account is stuck in a stale login state while the
// other platform sessions on the same profile must remain intact.
func (m *Manager) ClearSiteSession(siteID string) (*State, error) {
	if _, ok := SiteByID(siteID); !ok {
		return nil, fmt.Errorf("unknown site")
	}
	if err := m.EnsurePage(siteID); err != nil {
		return nil, err
	}
	ctx, err := m.tab(siteID)
	if err != nil {
		return nil, err
	}
	// Delete cookies by their exact domain/path so cookies for the other five
	// platforms in this isolated Chrome profile are not touched.
	if err := runTab(ctx, 5*time.Second, chromedp.ActionFunc(func(c context.Context) error {
		cookies, e := storage.GetCookies().Do(c)
		if e != nil {
			return e
		}
		keep := map[string]bool{}
		for _, domain := range domainsOf(siteID) {
			keep[domain] = true
		}
		for _, cookie := range cookies {
			if cookie == nil || !hostKept(cookie.Domain, keep) {
				continue
			}
			if e := network.DeleteCookies(cookie.Name).
				WithDomain(cookie.Domain).
				WithPath(cookie.Path).Do(c); e != nil {
				return e
			}
		}
		return storage.ClearDataForOrigin(originOf(siteID), "cookies,local_storage,session_storage,indexeddb,cache_storage,service_workers").Do(c)
	})); err != nil {
		return nil, err
	}
	_ = runTab(ctx, 5*time.Second, navigateNoWait("about:blank"))
	m.mu.Lock()
	delete(m.pendingLS, originOf(siteID))
	cleared := siteID
	if site, ok := SiteByID(siteID); ok && site.Name != "" {
		cleared = site.Name
	}
	m.setLocked(siteID, "not_logged_in", cleared+"会话已清除，请重新登录")
	m.states[siteID].Evidence = "已清除" + cleared + " cookies 和本地会话数据"
	m.states[siteID].URL = ""
	m.states[siteID].LastLoginAt = ""
	cp := *m.states[siteID]
	m.mu.Unlock()
	m.dumpSession()
	return &cp, nil
}

func selectDoubaoBoundAccount(ctx context.Context) {
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		clicked, _ := runJSBool(ctx, `(() => {
		  const visible = el => {
		    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
		    return r.width > 2 && r.height > 2 && s.display !== "none" &&
		      s.visibility !== "hidden" && Number(s.opacity || 1) > 0;
		  };
		  const body = document.body ? (document.body.innerText || "") : "";
		  if (!/已绑定了以下账号|选择账号/.test(body)) return false;
		  const nodes = [...document.querySelectorAll("button,[role=button],a,div,span")]
		    .filter(visible)
		    .filter(n => /^用户\d{4,}$/.test((n.innerText || "").trim()));
		  if (!nodes.length) return false;
		  nodes.sort((a, b) => {
		    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
		    return ar.top - br.top || ar.left - br.left;
		  });
		  nodes[0].click();
		  return true;
		})()`)
		if clicked {
			time.Sleep(1800 * time.Millisecond)
			return
		}
		time.Sleep(350 * time.Millisecond)
	}
}

func watchDoubaoLoginResponses(ctx context.Context) (<-chan []byte, context.CancelFunc, error) {
	listenCtx, cancel := context.WithCancel(ctx)
	responses := make(chan []byte, 1)
	var (
		mu      sync.Mutex
		pending = map[network.RequestID]bool{}
	)
	chromedp.ListenTarget(listenCtx, func(event any) {
		switch ev := event.(type) {
		case *network.EventResponseReceived:
			if ev.Response.Status == 200 && strings.Contains(ev.Response.URL, "accounts.doubao.com/passport/web/sms_login/") {
				mu.Lock()
				pending[ev.RequestID] = true
				mu.Unlock()
			}
		case *network.EventLoadingFinished:
			mu.Lock()
			matched := pending[ev.RequestID]
			delete(pending, ev.RequestID)
			mu.Unlock()
			if !matched {
				return
			}
			go func(requestID network.RequestID) {
				var body []byte
				err := chromedp.Run(listenCtx, chromedp.ActionFunc(func(c context.Context) error {
					var getErr error
					body, getErr = network.GetResponseBody(requestID).Do(c)
					return getErr
				}))
				if err != nil {
					log.Printf("read doubao login response: %v", err)
					return
				}
				select {
				case responses <- body:
				default:
				}
			}(ev.RequestID)
		}
	})
	if err := runTab(listenCtx, 2*time.Second, network.Enable()); err != nil {
		cancel()
		return nil, func() {}, err
	}
	return responses, cancel, nil
}

func waitDoubaoLoginResponse(responses <-chan []byte, timeout time.Duration) (string, bool) {
	select {
	case body := <-responses:
		return parseDoubaoLoginError(body)
	case <-time.After(timeout):
		return "", false
	}
}

func parseDoubaoLoginError(body []byte) (string, bool) {
	var payload struct {
		Message string `json:"message"`
		Data    struct {
			Description string `json:"description"`
			ErrorCode   int    `json:"error_code"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil || payload.Data.ErrorCode == 0 {
		return "", false
	}
	message := strings.TrimSpace(payload.Data.Description)
	if message == "" || message == "error" {
		message = strings.TrimSpace(payload.Message)
	}
	// A phone number can own multiple Doubao identities. The SMS endpoint
	// reports that account-picker state with a non-zero code, but the code was
	// accepted and the browser must continue by selecting an account card.
	if strings.Contains(message, "已绑定了以下账号") ||
		strings.Contains(message, "选择账号") {
		return "", false
	}
	if message == "" || message == "error" {
		message = fmt.Sprintf("豆包登录失败（错误码 %d）", payload.Data.ErrorCode)
	}
	return message, true
}

func (m *Manager) waitForLogin(siteID string, timeout time.Duration) *State {
	deadline := time.Now().Add(timeout)
	var state *State
	loggedStreak := 0
	for {
		state = m.Refresh(siteID)
		if state.Status == "logged_in" {
			loggedStreak++
			// Kimi's login modal has a short intermediate state with neither a
			// form nor a login button. Require several stable observations.
			if siteID != "kimi" || loggedStreak >= 15 {
				return state
			}
		} else {
			loggedStreak = 0
		}
		if time.Now().After(deadline) {
			return state
		}
		time.Sleep(350 * time.Millisecond)
	}
}

func (m *Manager) Refresh(siteID string) *State {
	ctx, err := m.tab(siteID)
	if err != nil {
		return m.set(siteID, "error", err.Error())
	}
	tctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	var href string
	_ = chromedp.Run(tctx, chromedp.Location(&href))
	text, _ := pageText(tctx)
	hasForm, _ := runJSBool(tctx, hasFormJS)
	hasLoginBtn, _ := runJSBool(tctx, `!![...document.querySelectorAll("button,a,div,span")].find(n => {
	  const t = (n.innerText||"").trim();
	  const r = n.getBoundingClientRect();
	  const s = getComputedStyle(n);
	  const visible = r.width > 2 && r.height > 2 && s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity || 1) > 0;
	  return (t === "登录" || t === "登录 / 注册" || t === "注册") && visible && t.length <= 8;
	})`)
	hasAvatar, _ := runJSBool(tctx, `!!document.querySelector("img[alt*='头像'], [class*='avatar'], [class*='user-info']")`)
	hasLoggedUI := hasAvatar ||
		maskedPhoneRe.MatchString(text) ||
		userIDRe.MatchString(text) ||
		strings.Contains(text, "开启新对话") ||
		strings.Contains(text, "给 DeepSeek 发送消息") ||
		strings.Contains(text, "暂无历史对话") ||
		strings.Contains(text, "发消息或按住空格说话") ||
		strings.Contains(text, "向千问提问") ||
		strings.Contains(text, "文心助手") ||
		qwenUserRe.MatchString(text) ||
		strings.Contains(href, "from_login=1")
	frameCaptcha, _ := runJSBool(tctx, visibleCaptchaFrameJS)
	captcha := captchaBlocksLogin(text, frameCaptcha)
	logged := false
	// 豆包登录成功后会保留登录弹窗/入口的隐藏 DOM。聊天首页的主标题
	// 与侧栏“API 服务/更多”是更强的已登录证据，不能因为隐藏节点被
	// hasLoginBtn 扫到就误判为未登录。
	if siteID == "doubao" {
		logged = doubaoLoggedEvidence(text, hasForm, hasLoginBtn, captcha)
	} else if siteID == "yuanbao" {
		logged = yuanbaoLoggedEvidence(text, hasAvatar, hasForm, hasLoginBtn, captcha)
	} else if siteID == "kimi" {
		// Kimi's public, server-rendered shell includes 我的 Kimi even before
		// its scripts load. A generic navigation label is not an account.
		accountControl, _ := runJSBool(tctx, kimiAccountControlJS)
		logged = kimiLoggedEvidence(text, accountControl, hasForm, hasLoginBtn, captcha)
	} else if !captcha && !hasForm && !hasLoginBtn && !strings.Contains(href, "sign_in") && !strings.Contains(href, "passport") {
		// 豆包/元宝未登录首页也是 /chat/，不能当登录证据
		if hasLoggedUI || strings.Contains(href, "/a/chat/") {
			logged = true
		}
	}
	m.mu.Lock()
	prevStatus := ""
	var sentAt float64
	var sentTarget string
	if prev := m.states[siteID]; prev != nil {
		prevStatus = prev.Status
		sentAt, sentTarget = prev.LastSendAt, prev.LastSendTargetID
	}
	m.mu.Unlock()
	// Countdown expiry does not remove the original OTP input. Keep that
	// generation available to a delayed callback without treating an untouched
	// login form, or a replacement target, as a pending SMS.
	boundSMSForm := smsFormForGeneration(sentAt, sentTarget, managerSiteTargetID(m, siteID), time.Now(), hasCodeForm(tctx))
	status, msg, evidence := "unknown", "未识别到输入框，可点截图操作", "页面缺少稳定的登录/退出特征"
	switch {
	case captcha:
		status, msg, evidence = "need_captcha", "请在截图上点击验证码", "页面出现安全验证提示"
	case logged:
		status, msg, evidence = "logged_in", "登录有效", "检测到账号标识或已登录聊天界面"
	case waitingSMS(text) || boundSMSForm:
		status, msg, evidence = "waiting_sms", "等待你填写短信验证码", "页面显示验证码已发送或倒计时"
	case hasForm || hasLoginBtn || strings.Contains(href, "sign_in") || strings.Contains(href, "passport"):
		if prevStatus == "logged_in" || prevStatus == "expired" {
			status, msg = "expired", "登录已失效，请重新登录"
		} else {
			status, msg = "not_logged_in", "尚未登录"
		}
		evidence = "检测到登录表单、登录按钮或登录页"
	}
	if siteID != "kimi" {
		status, msg, evidence = keepPriorLogin(prevStatus, status, msg, evidence)
	}
	m.set(siteID, status, msg)
	m.mu.Lock()
	m.states[siteID].URL = href
	m.states[siteID].Evidence = evidence
	m.states[siteID].LastCheckedAt = now()
	if status == "logged_in" && prevStatus != "logged_in" {
		m.states[siteID].LastLoginAt = now()
	}
	cp := *m.states[siteID]
	m.mu.Unlock()
	return &cp
}

func smsFormForGeneration(sentAt float64, sentTarget, currentTarget string, at time.Time, codeForm bool) bool {
	if !codeForm || sentAt <= 0 || sentTarget == "" || sentTarget != currentTarget {
		return false
	}
	age := float64(at.UnixNano())/1e9 - sentAt
	return age >= 0 && age < 300
}

// keepPriorLogin leaves a known login in place when the page has not painted
// account markers yet. A blank or slow tab used to be stored as unknown, which
// dropped the account out of the pool for the rest of the job.
func keepPriorLogin(prev, status, msg, evidence string) (string, string, string) {
	if status == "unknown" && prev == "logged_in" {
		return "logged_in", "登录有效", "页面暂未显示稳定特征，沿用已登录状态"
	}
	return status, msg, evidence
}

func doubaoLoggedEvidence(text string, hasForm, hasLoginBtn, captcha bool) bool {
	if captcha || hasForm || hasLoginBtn || strings.Contains(text, "未登录") || strings.Contains(text, "请登录后") {
		return false
	}
	// The current guest shell also renders API 服务, 更多 and the composer.
	// Require a real account identifier, as the Playwright login check does.
	return userIDRe.MatchString(text) || maskedPhoneRe.MatchString(text)
}

func yuanbaoLoggedEvidence(text string, _ bool, hasForm, hasLoginBtn, captcha bool) bool {
	if captcha || hasForm || hasLoginBtn || strings.Contains(text, "未登录") || strings.Contains(text, "请登录后") {
		return false
	}
	// The logged-out shell also renders a generic avatar. Require an account
	// identifier so a public/guest conversation cannot enter the account pool.
	return maskedPhoneRe.MatchString(text) || userIDRe.MatchString(text)
}

const kimiAccountControlJS = `!![...document.querySelectorAll("button.user-info-container, .user-info-container .user-profile-trigger, .user-area .user-profile-trigger")].find(n => {
  const text = (n.innerText || "").trim();
  if (!text || /登录|注册|我的\s*Kimi/.test(text) ||
      n.closest('[data-message-role], [data-role=user], [data-role=assistant], .segment')) return false;
  const r = n.getBoundingClientRect();
  if (r.width <= 2 || r.height <= 2) return false;
  for (let p = n; p; p = p.parentElement) {
    const s = getComputedStyle(p);
    if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity || 1) === 0) return false;
  }
  return true;
})`

func kimiLoggedEvidence(text string, accountControl, hasForm, hasLoginBtn, captcha bool) bool {
	if captcha || hasForm || hasLoginBtn || strings.Contains(text, "未登录") || strings.Contains(text, "请登录后") {
		return false
	}
	return accountControl || maskedPhoneRe.MatchString(text) || userIDRe.MatchString(text)
}

func (m *Manager) watch() {
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-m.done:
			return
		case <-ticker.C:
		}
		if !m.Ready() {
			continue
		}
		if m.workflowIsControlled() {
			continue
		}
		for _, site := range Sites {
			if m.isBusy(site.ID) {
				continue
			}
			m.mu.Lock()
			st := m.states[site.ID]
			status := ""
			if st != nil {
				status = st.Status
			}
			m.mu.Unlock()
			switch status {
			case "logged_in", "error", "starting", "unknown", "idle", "need_captcha", "recovery_pending":
				continue
			}
			_ = m.Refresh(site.ID)
		}
	}
}

func (m *Manager) liveCtx() context.Context {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.ctrlCtx != nil {
		return m.ctrlCtx
	}
	for _, id := range []string{"deepseek", "yiyan", "qianwen", "doubao", "yuanbao", "kimi"} {
		if ctx := m.tabs[id]; ctx != nil {
			return ctx
		}
	}
	return m.browserCtx
}

func (m *Manager) adoptExistingPage(site Site) bool {
	host := ""
	if u, err := url.Parse(site.Home); err == nil {
		host = u.Host
	}
	if host == "" || m.liveCtx() == nil {
		return false
	}
	tctx, cancel := context.WithTimeout(m.liveCtx(), 3*time.Second)
	defer cancel()
	var infos []*target.Info
	err := chromedp.Run(tctx, chromedp.ActionFunc(func(ctx context.Context) error {
		var err error
		infos, err = target.GetTargets().Do(ctx)
		return err
	}))
	if err != nil {
		return false
	}
	var chosen *target.Info
	for _, info := range infos {
		if info.Type != "page" {
			continue
		}
		pageURL := info.URL
		if pageURL == "" || pageURL == "about:blank" || strings.HasPrefix(pageURL, "chrome://") {
			continue
		}
		if !siteOwnsPage(site, pageURL) {
			continue
		}
		if chosen == nil || (strings.Contains(pageURL, "/chat/local_") && !strings.Contains(chosen.URL, "/chat/local_")) {
			copyInfo := info
			chosen = copyInfo
		}
	}
	if chosen == nil {
		return false
	}
	m.mu.Lock()
	browser := m.browserCtx
	oldCancel := m.tabCancels[site.ID]
	oldCtx := m.tabs[site.ID]
	m.mu.Unlock()
	if browser == nil {
		return false
	}
	if oldCtx != nil {
		if c := chromedp.FromContext(oldCtx); c != nil && c.Target != nil && c.Target.TargetID != "" && c.Target.TargetID != chosen.TargetID {
			return false
		}
	}
	next, nextCancel := chromedp.NewContext(m.siteContextParent(browser), chromedp.WithTargetID(chosen.TargetID))
	if err := chromedp.Run(next); err != nil {
		nextCancel()
		log.Printf("adopt tab %s: %v", site.ID, err)
		return false
	}
	m.mu.Lock()
	m.tabs[site.ID] = next
	m.tabCancels[site.ID] = nextCancel
	m.opened[site.ID] = true
	m.mu.Unlock()
	if oldCancel != nil {
		oldCancel()
	}
	log.Printf("adopt tab %s", site.ID)
	return true
}

func (m *Manager) EnsurePage(siteID string) error {
	return m.EnsurePageContext(context.Background(), siteID)
}

func (m *Manager) EnsurePageContext(parent context.Context, siteID string) (err error) {
	if err := parent.Err(); err != nil {
		return err
	}
	site, ok := SiteByID(siteID)
	if !ok {
		return fmt.Errorf("unknown site")
	}
	ctx, err := m.tab(siteID)
	if err != nil {
		return err
	}
	m.mu.Lock()
	tabCancel := m.tabCancels[siteID]
	m.mu.Unlock()
	// Initialize with the long-lived tab context, but cancel that target when
	// the caller's budget expires. A timeout child on the first Run destroys
	// the target even after successful initialization.
	stopDeadline := context.AfterFunc(parent, func() {
		if tabCancel != nil {
			tabCancel()
		}
	})
	defer func() {
		stopDeadline()
		if parent.Err() != nil {
			err = parent.Err()
			_ = m.replaceTab(siteID)
		}
	}()
	m.mu.Lock()
	opened := m.opened[siteID]
	m.mu.Unlock()
	if !opened {
		// chromedp contexts create their targets on the first Run. Concurrent
		// first Runs against one browser can intermittently fail before the
		// default execution context exists, so initialize lazy tabs serially.
		for !m.tabInitMu.TryLock() {
			if err := waitContext(parent, 50*time.Millisecond); err != nil {
				return err
			}
		}
		defer m.tabInitMu.Unlock()
		m.mu.Lock()
		opened = m.opened[siteID]
		m.mu.Unlock()
		if opened {
			return nil
		}
		if m.adoptExistingPage(site) {
			return nil
		}
		log.Printf("open tab %s", siteID)
		// 首次运行必须绑定长期 context，否则 timeout 子 context 结束时
		// chromedp 会一并关闭刚创建的 target。
		var navErr error
		for attempt := 1; attempt <= 3; attempt++ {
			navErr = chromedp.Run(ctx, navigateNoWait(site.Home))
			if navErr == nil {
				break
			}
			log.Printf("open tab %s attempt=%d: %v", siteID, attempt, navErr)
			if err := waitContext(parent, 500*time.Millisecond); err != nil {
				return err
			}
		}
		if navErr != nil {
			return navErr
		}
		if m.restoreLocalStorage(ctx, originOf(siteID)) {
			if err := chromedp.Run(ctx, navigateNoWait(site.Home)); err != nil {
				log.Printf("reload after localStorage restore %s: %v", siteID, err)
			}
		}
		m.mu.Lock()
		m.opened[siteID] = true
		m.mu.Unlock()
		return nil
	}
	// Freezing purges JavaScript, so Location and clicks both fail and the
	// login button looks dead. Unfreeze first. If the document still has no
	// script context, reload and wait until it answers before any click.
	_ = runTab(ctx, 2*time.Second, chromedp.ActionFunc(func(c context.Context) error {
		return page.SetWebLifecycleState(page.SetWebLifecycleStateStateActive).Do(c)
	}))
	if pageAnswers(ctx) {
		var href string
		if err := runTab(ctx, 2*time.Second, chromedp.Location(&href)); err == nil && realPage(href) {
			return nil
		}
	}
	log.Printf("wake tab %s", siteID)
	var wakeErr error
	for attempt := 1; attempt <= 2; attempt++ {
		wakeErr = runTab(ctx, pageOpenBudget(), navigateNoWait(site.Home))
		if wakeErr == nil || parent.Err() != nil {
			break
		}
		log.Printf("wake tab %s attempt=%d: %v", siteID, attempt, wakeErr)
	}
	if wakeErr != nil {
		return wakeErr
	}
	waitForVisiblePage(ctx)
	if !pageAnswers(ctx) {
		return fmt.Errorf("打开平台失败")
	}
	return nil
}

func realPage(href string) bool {
	return href != "" && href != "about:blank" && !strings.HasPrefix(href, "chrome://")
}

func pageAnswers(ctx context.Context) bool {
	var n int
	err := runTab(ctx, 2*time.Second, chromedp.Evaluate(`1`, &n))
	return err == nil && n == 1
}

func (m *Manager) replaceTab(siteID string) error {
	old, err := m.tab(siteID)
	if err != nil {
		return err
	}
	if c := chromedp.FromContext(old); c != nil && c.Target != nil && c.Target.TargetID != "" {
		tid := c.Target.TargetID
		_ = runTab(m.liveCtx(), 3*time.Second, chromedp.ActionFunc(func(ctx context.Context) error {
			return target.CloseTarget(tid).Do(ctx)
		}))
	}
	m.mu.Lock()
	browser := m.browserCtx
	m.mu.Unlock()
	if browser == nil {
		return fmt.Errorf("browser not ready")
	}
	next, nextCancel := chromedp.NewContext(m.siteContextParent(browser))
	m.mu.Lock()
	m.tabs[siteID] = next
	if m.tabCancels == nil {
		m.tabCancels = make(map[string]context.CancelFunc)
	}
	m.tabCancels[siteID] = nextCancel
	m.opened[siteID] = false
	m.mu.Unlock()
	return nil
}

func (m *Manager) hibernateIdle() {
	m.mu.Lock()
	idle := make([]string, 0, len(Sites))
	for _, site := range Sites {
		st := m.states[site.ID]
		if st == nil {
			continue
		}
		switch st.Status {
		case "logged_in", "waiting_sms", "need_captcha", "login_form", "sending", "submitting":
			continue
		}
		idle = append(idle, site.ID)
	}
	m.mu.Unlock()
	for _, id := range idle {
		ctx, err := m.tab(id)
		if err != nil {
			continue
		}
		if err := runTab(ctx, 5*time.Second, navigateNoWait("about:blank")); err == nil {
			log.Printf("hibernate idle tab %s", id)
		}
	}
}

func (m *Manager) ensureLoginForm(ctx context.Context, site Site) error {
	has, _ := runJSBool(ctx, hasFormJS)
	if has {
		return nil
	}
	if site.ID == "doubao" {
		// The Doubao guest shell renders before its top-right login button.
		// Wait for the real visible button instead of clicking an early hidden
		// node with the same text, then switch the modal to phone login.
		for attempt := 0; attempt < 10; attempt++ {
			clicked, _ := runJSBool(ctx, clickPreferredVisibleTextJS([]string{"登录"}))
			if clicked {
				time.Sleep(900 * time.Millisecond)
				_, _ = runJSBool(ctx, clickPreferredVisibleTextJS([]string{"手机号登录", "短信登录"}))
				time.Sleep(700 * time.Millisecond)
				has, _ = runJSBool(ctx, hasFormJS)
				if has {
					return nil
				}
			}
			time.Sleep(time.Second)
		}
	}
	if site.ID == "kimi" {
		_ = clickWithTimeout(ctx, `button.user-info-container`, 800*time.Millisecond)
		time.Sleep(900 * time.Millisecond)
		_, _ = runJSBool(ctx, `document.querySelector("button.next-sidebar-history-list__login")?.click()`)
		time.Sleep(800 * time.Millisecond)
		// Kimi 经常调整侧栏 class，保留基于可见文案和按钮面积的兜底。
		_, _ = runJSBool(ctx, `(() => {
		  const nodes = [...document.querySelectorAll("button,[role=button],a")].filter(n => {
		    const r = n.getBoundingClientRect();
		    return n.offsetParent && (n.innerText||"").trim() === "登录" && r.width > 2 && r.height > 2;
		  });
		  nodes.sort((a,b) => {
		    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
		    return ar.width * ar.height - br.width * br.height;
		  });
		  if (!nodes[0]) return false;
		  nodes[0].click();
		  return true;
		})()`)
		time.Sleep(800 * time.Millisecond)
		has, _ = runJSBool(ctx, hasFormJS)
		if has {
			return nil
		}
	}
	for _, t := range site.Open {
		_, _ = runJSBool(ctx, clickTextJS([]string{t}))
		time.Sleep(time.Second)
		if openLoginIframe(ctx) {
			has, _ = runJSBool(ctx, hasFormJS)
			if has {
				return nil
			}
		}
		has, _ = runJSBool(ctx, hasFormJS)
		if has {
			return nil
		}
	}
	if site.ID == "yiyan" {
		emulateMobile(ctx)
	}
	_ = chromedp.Run(ctx, chromedp.Navigate(site.Login), chromedp.Sleep(1600*time.Millisecond))
	for _, t := range append(site.Open, "登录", "注册", "登录 / 注册", "手机号登录", "短信登录") {
		has, _ = runJSBool(ctx, hasFormJS)
		if has {
			return nil
		}
		_, _ = runJSBool(ctx, clickTextJS([]string{t}))
		time.Sleep(800 * time.Millisecond)
	}
	return nil
}

func (m *Manager) tab(siteID string) (context.Context, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	ctx, ok := m.tabs[siteID]
	if !ok {
		return nil, fmt.Errorf("site not started: %s", siteID)
	}
	return ctx, nil
}

// notePriorStatus records the pool's last decision before a live page read.
// A freshly started browser has no in-memory status, so a slow paint would
// otherwise replace a persisted login with unknown.
func (m *Manager) notePriorStatus(siteID, status string) {
	if m == nil || status == "" {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if st := m.states[siteID]; st != nil {
		switch st.Status {
		case "", "starting", "idle", "unknown":
		default:
			return
		}
	}
	st := m.states[siteID]
	if st == nil {
		st = &State{SiteID: siteID}
		m.states[siteID] = st
	}
	st.Status = status
	st.Phone = m.Phone
}

func (m *Manager) set(siteID, status, message string) *State {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.setLocked(siteID, status, message)
}

func (m *Manager) setLocked(siteID, status, message string) *State {
	st := m.states[siteID]
	if st == nil {
		st = &State{SiteID: siteID}
		m.states[siteID] = st
	}
	if status != "need_captcha" {
		delete(m.autoCaptcha, siteID)
		if len(m.autoCaptcha) == 0 {
			m.autoWarmTo = time.Time{}
		}
	}
	if pendingLoginStatus(status) {
		if m.pendingSince == nil {
			m.pendingSince = map[string]time.Time{}
		}
		if m.pendingSince[siteID].IsZero() || (st.Status == "need_captcha" && status == "waiting_sms") {
			// Solving a challenge can outlast the unattended grace period.
			// Start the SMS phase once; repeated refreshes cannot extend it.
			m.pendingSince[siteID] = time.Now()
		}
	} else {
		delete(m.pendingSince, siteID)
	}
	st.Status = status
	st.Message = message
	st.Phone = m.Phone
	st.UpdatedAt = now()
	cp := *st
	return &cp
}

func pageText(ctx context.Context) (string, error) {
	var t string
	err := chromedp.Run(ctx, chromedp.Evaluate(`document.body ? (document.body.innerText||"") : ""`, &t))
	return t, err
}

func runJS(ctx context.Context, js string) error {
	var ignored bool
	return chromedp.Run(ctx, chromedp.Evaluate(js, &ignored))
}

func runJSBool(ctx context.Context, js string) (bool, error) {
	var ok bool
	err := chromedp.Run(ctx, chromedp.Evaluate(js, &ok))
	return ok, err
}

func openLoginIframe(ctx context.Context) bool {
	var src string
	_ = chromedp.Run(ctx, chromedp.Evaluate(`(() => {
	  const f = [...document.querySelectorAll("iframe")].find(el => /login|passport|havana/i.test(el.src||""));
	  return f && f.src ? f.src : "";
	})()`, &src))
	if src == "" {
		return false
	}
	_ = chromedp.Run(ctx, chromedp.Navigate(src), chromedp.Sleep(1600*time.Millisecond))
	return true
}

func emulateMobile(ctx context.Context) {
	_ = chromedp.Run(ctx,
		chromedp.EmulateViewport(390, 844, chromedp.EmulateMobile),
		emulation.SetUserAgentOverride("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36").WithAcceptLanguage("zh-CN,zh;q=0.9"),
	)
}

func clickDialogs(ctx context.Context) {
	for _, t := range []string{"同意并继续", "同意并注册"} {
		ok, _ := runJSBool(ctx, clickTextJS([]string{t}))
		if ok {
			time.Sleep(1800 * time.Millisecond)
		}
	}
}

func clickSubmit(ctx context.Context) {
	if clickWithTimeout(ctx, `.hyc-phone-login__btn`, 500*time.Millisecond) {
		return
	}
	if clickWithTimeout(ctx, `div.ds-button--filled.ds-button--xl`, 500*time.Millisecond) {
		return
	}
	_, _ = runJSBool(ctx, `(() => {
	  const prefer = ["同意并注册","同意并继续","验证并登录","登录 / 注册"];
	  const nodes = [...document.querySelectorAll("button,div,span,a,[role=button]")];
	  for (const w of prefer) {
	    const el = nodes.find(n => (n.innerText||"").trim() === w && n.offsetParent);
	    if (el) { el.click(); return true; }
	  }
	  const modal = document.querySelector("[role=dialog], .hyc-phone-login, [class*='login']");
	  const scope = modal ? [...modal.querySelectorAll("button")] : [...document.querySelectorAll("button")];
	  const el = scope.find(n => (n.innerText||"").trim() === "登录" && n.offsetParent);
	  if (el) { el.click(); return true; }
	  return false;
	})()`)
}

func clickWithTimeout(ctx context.Context, sel string, d time.Duration) bool {
	tctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	return chromedp.Run(tctx, chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible)) == nil
}

func agreeIfNeeded(ctx context.Context) {
	_, _ = runJSBool(ctx, `(() => {
	  for (const b of document.querySelectorAll("input[type=checkbox]")) {
	    if (!b.checked && (b.offsetParent || b.getClientRects().length)) b.click();
	  }
	  const interesting = /协议|隐私|已阅读|同意|须知|勾选协议/;
	  const nodes = [...document.querySelectorAll("[role=checkbox], label")];
	  for (const n of nodes) {
	    const t = ((n.innerText || n.getAttribute("aria-label") || "") + "").slice(0, 80);
	    if (!interesting.test(t)) continue;
	    const box = n.matches("[role=checkbox]") ? n : (n.querySelector("input[type=checkbox],[role=checkbox]") || null);
	    if (!box) continue;
	    const checked = !!(box.checked || box.getAttribute("aria-checked") === "true" || /checked/.test(box.className||""));
	    if (!checked) box.click();
	  }
	  return true;
	})()`)
	time.Sleep(300 * time.Millisecond)
}

func clickSendCode(ctx context.Context) bool {
	sels := []string{
		`.phone-login-verify-code button`,
		`a.send-btn-link`,
		`.send-btn:not(.send-btn-disabled) a`,
		`.ds-verify-code-input-countdown`,
		`.ds-verify-code-input-form-item .ds-button`,
		`div.ds-input__suffix`,
	}
	for _, sel := range sels {
		if clickWithTimeout(ctx, sel, 700*time.Millisecond) {
			time.Sleep(1600 * time.Millisecond)
			return true
		}
	}
	ok, _ := runJSBool(ctx, `(() => {
	  const nodes = [...document.querySelectorAll("button,div,span,a,[role=button]")];
	  const prefer = ["发送验证码","获取验证码","获取短信验证码","同意并继续","下一步","发送"];
	  let el = null;
	  for (const w of prefer) {
	    const cands = nodes.filter(n => (n.innerText||"").trim() === w && n.offsetParent);
	    if (!cands.length) continue;
	    cands.sort((a,b) => {
	      const pa = a.tagName === "BUTTON" ? 0 : 1;
	      const pb = b.tagName === "BUTTON" ? 0 : 1;
	      if (pa !== pb) return pa - pb;
	      return a.getBoundingClientRect().width - b.getBoundingClientRect().width;
	    });
	    el = cands[0];
	    break;
	  }
	  if (!el) return false;
	  const r = el.getBoundingClientRect();
	  const x = r.left + r.width/2, y = r.top + r.height/2;
	  for (const type of ["pointerdown","mousedown","pointerup","mouseup","click"]) {
	    el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, view:window, clientX:x, clientY:y}));
	  }
	  return true;
	})()`)
	if ok {
		time.Sleep(1800 * time.Millisecond)
	}
	return ok
}

func clickTextJS(texts []string) string {
	b, _ := jsonQuote(texts)
	return fmt.Sprintf(`(() => {
	  const want = %s;
	  const nodes = [...document.querySelectorAll("button,div,span,a,[role=button]")];
	  for (const w of want) {
	    const el = nodes.find(n => (n.innerText||"").trim() === w && n.offsetParent);
	    if (el) { el.click(); return true; }
	  }
	  for (const w of want) {
	    const el = nodes.find(n => (n.innerText||"").includes(w) && n.offsetParent && (n.innerText||"").trim().length < 20);
	    if (el) { el.click(); return true; }
	  }
	  return false;
	})()`, b)
}

func clickPreferredVisibleTextJS(texts []string) string {
	b, _ := jsonQuote(texts)
	return fmt.Sprintf(`(() => {
	  const want = %s;
	  const visible = n => {
	    const r = n.getBoundingClientRect(), s = getComputedStyle(n);
	    return r.width > 2 && r.height > 2 && s.display !== "none" &&
	      s.visibility !== "hidden" && Number(s.opacity || 1) > 0;
	  };
	  const nodes = [...document.querySelectorAll("button,[role=button],a,span,div")]
	    .filter(visible);
	  for (const text of want) {
	    const matches = nodes.filter(n => (n.innerText || "").trim() === text);
	    matches.sort((a, b) => {
	      const rank = n => n.tagName === "BUTTON" ? 0 :
	        n.getAttribute("role") === "button" ? 1 : n.tagName === "A" ? 2 : 3;
	      return rank(a) - rank(b);
	    });
	    if (matches[0]) { matches[0].click(); return true; }
	  }
	  return false;
	})()`, b)
}

func fillPhone(ctx context.Context, siteID, phone string) error {
	sel := `input[placeholder*='手机号'], input[placeholder*='手机'], input[type=tel]`
	if siteID == "kimi" {
		// Kimi 使用 React 受控输入，直接设置 value 会被下一次渲染清空。
		// 聚焦后发送真实键盘事件，才能同步组件内部状态。
		tctx, cancel := context.WithTimeout(ctx, 4*time.Second)
		defer cancel()
		var already bool
		if err := chromedp.Run(tctx, chromedp.Evaluate(fmt.Sprintf(`(() => {
		  const el = document.querySelector("input[placeholder='手机号']");
		  if (!el) throw new Error("找不到 Kimi 手机号输入框");
		  el.focus();
		  if ((el.value||"").replace(/\D/g,"") === %q) return true;
		  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
		  if (setter) setter.call(el, ""); else el.value = "";
		  el.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"deleteContentBackward"}));
		  return false;
		})()`, phone), &already)); err != nil {
			return err
		}
		if already {
			return nil
		}
		return chromedp.Run(tctx, chromedp.KeyEvent(phone))
	}
	ok, _ := runJSBool(ctx, fmt.Sprintf(`(() => {
	  const el = document.querySelector(%q);
	  if (!el) return false;
	  el.focus();
	  const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
	  if (desc && desc.set) desc.set.call(el, %q);
	  else el.value = %q;
	  el.dispatchEvent(new InputEvent("input", {bubbles:true, data:%q, inputType:"insertText"}));
	  el.dispatchEvent(new Event("change", {bubbles:true}));
	  return (el.value || "").replace(/\D/g,"") === %q;
	})()`, sel, phone, phone, phone, phone))
	if ok {
		return nil
	}
	tctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	return chromedp.Run(tctx,
		chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible),
		chromedp.Sleep(150*time.Millisecond),
		chromedp.SendKeys(sel, "\b\b\b\b\b\b\b\b\b\b\b\b"+phone, chromedp.ByQuery),
		chromedp.Sleep(300*time.Millisecond),
	)
}

const hasFormJS = `(() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity || 1) > 0;
  };
  const fields = [...document.querySelectorAll("input[placeholder*='手机'],input[type=tel],input[placeholder*='验证码']")];
  const digits = [...document.querySelectorAll("input[maxlength='1']")].filter(visible);
  return fields.some(visible) || digits.length >= 4 || (document.body && /输入\s*6\s*位验证码/.test(document.body.innerText||""));
})()`

func hasCodeForm(ctx context.Context) bool {
	ok, _ := runJSBool(ctx, `(() => {
	  const visible = el => {
	    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
	    return r.width > 2 && r.height > 2 && s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity || 1) > 0;
	  };
	  const fields = [...document.querySelectorAll("input[placeholder*='验证码'], input[inputmode='decimal'], input.semi-input")];
	  const digits = [...document.querySelectorAll("input[maxlength='1']")].filter(visible);
	  // Baidu renders six visible cells over its transparent keyboard input.
	  // Opacity zero is intentional here; the widget must still be visible.
	  const baiduInput = document.getElementById("sms_verify_new-keyboardInputHidden");
	  const baiduWidget = document.getElementById("pass-sms-keyboardInputWrapper");
	  const baiduCode = baiduInput && baiduWidget && baiduWidget.contains(baiduInput) &&
	    visible(baiduWidget) && [...baiduWidget.querySelectorAll("span")].filter(visible).length >= 6;
	  return !!(fields.some(visible) || digits.length >= 4 || baiduCode || (document.body && /输入\s*6\s*位验证码/.test(document.body.innerText||"")));
	})()`)
	return ok
}

func fillYuanbaoCode(ctx context.Context, code string) error {
	// The visible code box is a controlled input. CDP SendKeys can hit a
	// hidden match and leave the field blank, so type into the visible box
	// and require the value to stick.
	ok, _ := runJSBool(ctx, fmt.Sprintf(`(() => {
	  const visible = el => {
	    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
	    return r.width > 2 && r.height > 2 && s.display !== "none" && s.visibility !== "hidden";
	  };
	  const inputs = [...document.querySelectorAll("input")].filter(el => visible(el) && ((el.placeholder || "").includes("验证码")));
	  const el = inputs[inputs.length - 1];
	  if (!el) return false;
	  el.focus();
	  el.click();
	  const code = %q;
	  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
	  if (setter) setter.call(el, "");
	  else el.value = "";
	  el.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"deleteContentBackward"}));
	  const inserted = document.execCommand("insertText", false, code);
	  if (!inserted || (el.value || "").replace(/\D/g, "") !== code) {
	    if (setter) setter.call(el, code);
	    else el.value = code;
	    el.dispatchEvent(new InputEvent("input", {bubbles:true, data:code, inputType:"insertText"}));
	    el.dispatchEvent(new Event("change", {bubbles:true}));
	  }
	  return (el.value || "").replace(/\D/g, "") === code;
	})()`, code))
	if !ok {
		return fmt.Errorf("验证码没有写进元宝输入框")
	}
	return nil
}

func clickYuanbaoLogin(ctx context.Context) {
	_, _ = runJSBool(ctx, `(() => {
	  const dialog = document.querySelector("[class*='phone-login'], [role='dialog']") || document.body;
	  const btn = [...dialog.querySelectorAll("button, [role='button']")].find(el => (el.innerText || "").trim() === "登录");
	  if (!btn) return false;
	  btn.click();
	  return true;
	})()`)
}

func fillCode(ctx context.Context, siteID, code string) error {
	if siteID == "doubao" {
		const sel = `input.semi-input[inputmode='decimal'], input[inputmode='decimal']`
		tctx, cancel := context.WithTimeout(ctx, 4*time.Second)
		defer cancel()
		return chromedp.Run(tctx,
			chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible),
			chromedp.KeyEvent("\b\b\b\b\b\b"),
			chromedp.SendKeys(sel, code, chromedp.ByQuery),
		)
	}
	if siteID == "yiyan" {
		// 百度移动登录把真实输入框移到视口外，用 6 个 span 显示每一位。
		// 直接改 value 不会经过它的键盘事件处理，因此必须聚焦后逐键输入。
		tctx, cancel := context.WithTimeout(ctx, 4*time.Second)
		defer cancel()
		if err := chromedp.Run(tctx,
			chromedp.Evaluate(`document.querySelector("#sms_verify_new-keyboardInputHidden").focus()`, nil),
			chromedp.KeyEvent(code),
		); err != nil {
			return err
		}
		return nil
	}
	if siteID == "yuanbao" {
		return fillYuanbaoCode(ctx, code)
	}
	if siteID == "kimi" || siteID == "qianwen" {
		// These sites use controlled inputs. Click the box and type through
		// CDP key events; setting the DOM value leaves the React state empty
		// and the login button submits a blank code.
		sel := `input[placeholder*='验证码'], input[autocomplete='one-time-code'], input[inputmode='decimal']`
		tctx, cancel := context.WithTimeout(ctx, 6*time.Second)
		defer cancel()
		if err := chromedp.Run(tctx,
			chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible),
			chromedp.SendKeys(sel, code, chromedp.ByQuery),
		); err != nil {
			return err
		}
		return nil
	}
	sel := `input[placeholder*='验证码'], input[inputmode='decimal'], input.semi-input`
	ok, _ := runJSBool(ctx, fmt.Sprintf(`(() => {
	  const el = [...document.querySelectorAll(%q)].find(n => n.offsetParent || n.getClientRects().length);
	  if (!el) return false;
	  el.focus();
	  const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
	  if (desc && desc.set) desc.set.call(el, %q);
	  else el.value = %q;
	  el.dispatchEvent(new InputEvent("input", {bubbles:true, data:%q, inputType:"insertText"}));
	  el.dispatchEvent(new Event("change", {bubbles:true}));
	  return (el.value || "").replace(/\D/g,"") === %q;
	})()`, sel, code, code, code, code))
	if ok {
		return nil
	}
	ok, _ = runJSBool(ctx, fmt.Sprintf(`(() => {
	  const code = %q;
	  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
	  const visible = (el) => !!(el.offsetParent || el.getClientRects().length);
	  let inputs = [...document.querySelectorAll("input[maxlength='1']")].filter(visible);
	  if (inputs.length < 4) {
	    inputs = [...document.querySelectorAll("input")].filter(el => {
	      if (!visible(el) || el.type === "hidden" || el.type === "checkbox") return false;
	      const ph = (el.placeholder||"");
	      return ph.includes("验证码") || el.maxLength === 1 || el.inputMode === "numeric" || (el.type === "tel" && el !== document.querySelector("input[placeholder*='手机']"));
	    });
	  }
	  if (inputs.length < 4) {
	    const dialog = document.querySelector("[role=dialog], [class*='modal'], [class*='login']") || document.body;
	    inputs = [...dialog.querySelectorAll("input")].filter(el => visible(el) && el.type !== "hidden" && el.type !== "checkbox" && el.type !== "password");
	    if (inputs.length > 1 && inputs[0].type === "tel") inputs = inputs.slice(1);
	  }
	  if (inputs.length >= 4) {
	    inputs.slice(0, code.length).forEach((el, i) => {
	      el.focus();
	      if (setter && setter.set) setter.set.call(el, code[i]);
	      else el.value = code[i];
	      el.dispatchEvent(new InputEvent("input", {bubbles:true, data:code[i], inputType:"insertText"}));
	      el.dispatchEvent(new Event("change", {bubbles:true}));
	    });
	    return true;
	  }
	  const one = document.querySelector("input[placeholder*='验证码'], input[autocomplete='one-time-code']");
	  if (!one) return false;
	  one.focus();
	  if (setter && setter.set) setter.set.call(one, code);
	  else one.value = code;
	  one.dispatchEvent(new InputEvent("input", {bubbles:true, data:code, inputType:"insertText"}));
	  one.dispatchEvent(new Event("change", {bubbles:true}));
	  return true;
	})()`, code))
	if ok {
		return nil
	}
	tctx, cancel := context.WithTimeout(ctx, 800*time.Millisecond)
	defer cancel()
	if err := chromedp.Run(tctx,
		chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible),
		chromedp.SendKeys(sel, code, chromedp.ByQuery),
	); err == nil {
		return nil
	}
	tctx2, cancel2 := context.WithTimeout(ctx, 800*time.Millisecond)
	defer cancel2()
	if err := chromedp.Run(tctx2,
		chromedp.Click(`input[maxlength='1']`, chromedp.ByQuery, chromedp.NodeVisible),
		chromedp.SendKeys(`input[maxlength='1']`, code, chromedp.ByQuery),
	); err == nil {
		return nil
	}
	return fmt.Errorf("找不到验证码输入框")
}

func jsonQuote(v any) (string, error) {
	b, err := json.Marshal(v)
	return string(b), err
}

const visibleCaptchaFrameJS = `(() => {
  const frames = [...document.querySelectorAll("iframe")];
  return frames.some(frame => {
    const box = frame.getBoundingClientRect();
    const onScreen = box.width >= 180 && box.height >= 180 && box.right > 0 && box.bottom > 0 && box.left < window.innerWidth && box.top < window.innerHeight;
    if (!onScreen) return false;
    let node = frame;
    while (node && node.nodeType === 1) {
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) === 0) return false;
      node = node.parentElement;
    }
    const hint = ((frame.title || "") + " " + (frame.name || "") + " " + (frame.src || "")).toLowerCase();
    if (/captcha|tcaptcha|geetest|安全验证/.test(hint)) return true;
    try {
      const text = (frame.contentDocument && frame.contentDocument.body && frame.contentDocument.body.innerText) || "";
      return /安全验证|依次点击|请完成验证|选择最符合描述|换一组/.test(text);
    } catch (err) {
      return false;
    }
  });
})()`

func captchaOnPage(ctx context.Context, text string) bool {
	if hasCaptcha(text) {
		return true
	}
	ok, _ := runJSBool(ctx, visibleCaptchaFrameJS)
	return ok
}

// captchaBlocksLogin reports a captcha that should block a login. A visible
// captcha frame always counts. Loose words such as 请点击 or 安全验证 do not,
// once the page already shows an account id — Doubao keeps that copy in
// leftover login nodes on an otherwise usable chat page.
func captchaBlocksLogin(text string, frameCaptcha bool) bool {
	if frameCaptcha {
		return true
	}
	if !hasCaptcha(text) {
		return false
	}
	identified := (userIDRe.MatchString(text) || maskedPhoneRe.MatchString(text)) &&
		!strings.Contains(text, "未登录") && !strings.Contains(text, "请登录后")
	return !identified
}

func hasCaptcha(text string) bool {
	for _, h := range []string{"点击图中", "请完成验证", "请完成安全验证", "向右滑动", "向右拖动", "请依次点击", "依次点击", "安全验证", "选择最符合描述", "换一组", "请点击"} {
		if strings.Contains(text, h) {
			return true
		}
	}
	return false
}

var (
	secRe         = regexp.MustCompile(`\d+\s*秒`)
	maskedPhoneRe = regexp.MustCompile(`\d{3}\*{4,}\d{2}`)
	userIDRe      = regexp.MustCompile(`用户[A-Za-z0-9]{4,}`)
	qwenUserRe    = regexp.MustCompile(`Qwen\d{3,}`)
)

func waitingSMS(text string) bool {
	if secRe.MatchString(text) {
		return true
	}
	for _, h := range []string{"重新发送", "再次发送", "已发送"} {
		if strings.Contains(text, h) {
			return true
		}
	}
	return false
}

func digitsOnly(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func now() string { return time.Now().Format("2006-01-02T15:04:05") }
func nowUnix() float64 {
	return float64(time.Now().Unix())
}
