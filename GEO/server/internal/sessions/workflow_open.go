package sessions

import (
	"context"
	"fmt"
	"net/url"
	"os/exec"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/systeminfo"
	"github.com/chromedp/cdproto/target"
	"github.com/chromedp/chromedp"
)

type WorkflowPage struct {
	TargetID string
	URL      string
	Reopened bool
}

// A view operation must not acquire an execution lease or depend on the
// manager's control tab: users can close that tab while Chrome remains alive.
// This connection owns only a websocket, never a Chrome process or a page.
func profileBrowserContext(ctx context.Context, profile string) (context.Context, context.CancelFunc, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	ws, err := probeProfileChrome(ctx, profile)
	if err != nil {
		cancel()
		return nil, nil, fmt.Errorf("该账号的 Chrome 连接不可用，请在 Mac 启动原账号浏览器后重试: %w", err)
	}
	b, err := chromedp.NewBrowser(ctx, ws)
	if err != nil {
		cancel()
		return nil, nil, fmt.Errorf("连接原账号 Chrome 失败: %w", err)
	}
	return cdp.WithExecutor(ctx, b), cancel, nil
}

// Restore only saved, persistent conversation URLs. Homepages, public shares,
// login links and Doubao's in-memory local_ IDs cannot restore the original turn.
func restorableWorkflowURL(site Site, raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Fragment != "" || u.Port() != "" || !siteOwnsPage(site, raw) {
		return false
	}
	patterns := map[string]string{
		"qianwen":  `^/chat/[a-zA-Z0-9_-]+$`,
		"kimi":     `^/chat/[a-zA-Z0-9_-]+$`,
		"deepseek": `^/a/chat/s/[a-zA-Z0-9_-]+$`,
		"doubao":   `^/chat/[0-9]+$`,
		"yuanbao":  `^/chat/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$`,
		"yiyan":    `^/search/[0-9]+$`,
	}
	pattern, ok := patterns[site.ID]
	if !ok || !regexp.MustCompile(pattern).MatchString(u.Path) {
		return false
	}
	query, err := url.ParseQuery(u.RawQuery)
	if err != nil {
		return false
	}
	for key := range query {
		if !(site.ID == "qianwen" && key == "lang") && !(site.ID == "yiyan" && key == "enter_type") {
			return false
		}
	}
	// The login host is owned by the platform, but is never a conversation host.
	return !strings.HasPrefix(strings.ToLower(u.Hostname()), "passport.")
}

func locateWorkflowPage(ctx context.Context, site Site, id, savedURL string) (WorkflowPage, error) {
	infos, err := target.GetTargets().Do(ctx)
	if err != nil {
		return WorkflowPage{}, fmt.Errorf("读取原浏览器标签页失败: %w", err)
	}
	for _, info := range infos {
		if string(info.TargetID) == id && id != "" {
			if info.Type != "page" || !siteOwnsPage(site, info.URL) {
				return WorkflowPage{}, fmt.Errorf("原标签页已离开该平台，请在 Mac 核对原对话")
			}
			return WorkflowPage{TargetID: id, URL: info.URL}, nil
		}
	}
	if !restorableWorkflowURL(site, savedURL) {
		return WorkflowPage{}, fmt.Errorf("原标签页已关闭，且没有可恢复的原对话链接；可点击“新建平台标签页”，在该账号历史记录中找回本题")
	}
	for _, info := range infos {
		if info.Type == "page" && info.URL == savedURL {
			return WorkflowPage{TargetID: string(info.TargetID), URL: savedURL, Reopened: true}, nil
		}
	}
	next, err := target.CreateTarget(savedURL).Do(ctx)
	if err != nil {
		return WorkflowPage{}, fmt.Errorf("重开原对话失败: %w", err)
	}
	return WorkflowPage{TargetID: string(next), URL: savedURL, Reopened: true}, nil
}

func revealWorkflowPage(ctx context.Context, page WorkflowPage) error {
	id := target.ID(page.TargetID)
	window, bounds, err := browser.GetWindowForTarget().WithTargetID(id).Do(ctx)
	if err != nil {
		return fmt.Errorf("读取原页面窗口失败: %w", err)
	}
	if bounds != nil && bounds.WindowState == browser.WindowStateMinimized {
		if err := browser.SetWindowBounds(window, &browser.Bounds{WindowState: browser.WindowStateNormal}).Do(ctx); err != nil {
			return fmt.Errorf("恢复最小化的 Chrome 窗口失败: %w", err)
		}
	}
	if err := target.ActivateTarget(id).Do(ctx); err != nil {
		return fmt.Errorf("切换原标签页失败: %w", err)
	}
	if runtime.GOOS != "darwin" {
		return nil
	}
	processes, err := systeminfo.GetProcessInfo().Do(ctx)
	if err != nil {
		return fmt.Errorf("读取原 Chrome 进程失败: %w", err)
	}
	for _, process := range processes {
		if process.Type == "browser" && process.ID > 0 {
			// Activate this account's actual process, not a different Chrome profile.
			const script = `function run(argv) { ObjC.import('AppKit'); var app=$.NSRunningApplication.runningApplicationWithProcessIdentifier(parseInt(argv[0],10)); if (!app.activateWithOptions(2)) throw new Error('Chrome foreground failed'); }`
			if out, err := exec.CommandContext(ctx, "/usr/bin/osascript", "-l", "JavaScript", "-e", script, "--", strconv.FormatInt(process.ID, 10)).CombinedOutput(); err != nil {
				return fmt.Errorf("原标签页已定位，但 Mac 窗口置前失败: %w: %s", err, strings.TrimSpace(string(out)))
			}
			return nil
		}
	}
	return fmt.Errorf("原标签页已定位，但未找到该账号的 Chrome 进程")
}

func (p *Pool) OpenWorkflowPage(ctx context.Context, owner, account, siteID, id, savedURL string) (WorkflowPage, error) {
	viewCtx, cancel, site, err := p.workflowViewContext(ctx, owner, account, siteID)
	if err != nil {
		return WorkflowPage{}, err
	}
	defer cancel()
	page, err := locateWorkflowPage(viewCtx, site, id, savedURL)
	if err != nil {
		return WorkflowPage{}, err
	}
	// Return the binding even if foregrounding fails, so callers can durably
	// retain a reopened original conversation rather than lose its new target.
	return page, revealWorkflowPage(viewCtx, page)
}

// Explicit operator action: open the configured platform homepage in the
// original account's Chrome. Never navigate an existing page or send a prompt.
func (p *Pool) NewWorkflowPage(ctx context.Context, owner, account, siteID string) (WorkflowPage, error) {
	viewCtx, cancel, site, err := p.workflowViewContext(ctx, owner, account, siteID)
	if err != nil {
		return WorkflowPage{}, err
	}
	defer cancel()
	page, err := createWorkflowPage(viewCtx, site)
	if err != nil {
		return WorkflowPage{}, err
	}
	return page, revealWorkflowPage(viewCtx, page)
}

func createWorkflowPage(ctx context.Context, site Site) (WorkflowPage, error) {
	id, err := target.CreateTarget(site.Home).Do(ctx)
	if err != nil {
		return WorkflowPage{}, fmt.Errorf("新建平台标签页失败: %w", err)
	}
	return WorkflowPage{TargetID: string(id), URL: site.Home}, nil
}

func (p *Pool) workflowViewContext(ctx context.Context, owner, account, siteID string) (context.Context, context.CancelFunc, Site, error) {
	site, ok := SiteByID(siteID)
	if !ok {
		return nil, nil, Site{}, fmt.Errorf("未知平台")
	}
	p.mu.Lock()
	a := p.accounts[account]
	if a == nil || p.workflowHeldLocked(account, siteID, owner) {
		p.mu.Unlock()
		return nil, nil, Site{}, fmt.Errorf("原账号不存在或该平台已交给其他任务")
	}
	profile, _ := p.pathsFor(account)
	p.mu.Unlock()
	viewCtx, cancel, err := profileBrowserContext(ctx, profile)
	if err != nil {
		return nil, nil, Site{}, err
	}
	return viewCtx, cancel, site, nil
}
