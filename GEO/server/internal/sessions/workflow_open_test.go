package sessions

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"
	"time"

	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/systeminfo"
	"github.com/chromedp/cdproto/target"
	"github.com/chromedp/chromedp"
)

type workflowViewExecutor struct {
	pages   []map[string]any
	created []string
}

func (e *workflowViewExecutor) Execute(_ context.Context, method string, params, result any) error {
	var value any
	switch method {
	case "Target.getTargets":
		value = map[string]any{"targetInfos": e.pages}
	case "Target.createTarget":
		p := params.(*target.CreateTargetParams)
		e.created = append(e.created, p.URL)
		value = map[string]any{"targetId": "restored"}
	default:
		return fmt.Errorf("unexpected mutation %s", method)
	}
	raw, _ := json.Marshal(value)
	return json.Unmarshal(raw, result)
}

func TestWorkflowOpenKeepsExactTargetAndRestoresOnlySavedConversation(t *testing.T) {
	site, _ := SiteByID("qianwen")
	saved := "https://www.qianwen.com/chat/original"
	for _, tc := range []struct {
		name, id, url string
		pages         []map[string]any
		wantID        string
		created       int
		fails         bool
	}{
		{"original page wins", "old", saved, []map[string]any{{"targetId": "old", "type": "page", "url": "https://www.qianwen.com/chat/current"}}, "old", 0, false},
		{"reuse restored exact URL", "old", saved, []map[string]any{{"targetId": "existing", "type": "page", "url": saved}}, "existing", 0, false},
		{"restore original", "old", saved, nil, "restored", 1, false},
		{"foreign bound target", "old", saved, []map[string]any{{"targetId": "old", "type": "page", "url": "https://example.com/"}}, "", 0, true},
		{"no saved URL", "old", "", nil, "", 0, true},
		{"never homepage", "old", "https://www.qianwen.com/", nil, "", 0, true},
		{"never public share", "old", "https://qianwen.my.cn/share/chat/original", nil, "", 0, true},
		{"never foreign URL", "old", "https://example.com/chat/original", nil, "", 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := &workflowViewExecutor{pages: tc.pages}
			page, err := locateWorkflowPage(cdp.WithExecutor(context.Background(), e), site, tc.id, tc.url)
			if (err != nil) != tc.fails || page.TargetID != tc.wantID || len(e.created) != tc.created {
				t.Fatalf("page=%+v creates=%v err=%v", page, e.created, err)
			}
		})
	}
	doubao, _ := SiteByID("doubao")
	for _, url := range []string{"https://www.doubao.com/chat/local_123", "https://www.doubao.com/chat/123?redirect=https://example.com", "https://user@www.doubao.com/chat/123"} {
		if restorableWorkflowURL(doubao, url) {
			t.Fatalf("unsafe restore URL accepted: %s", url)
		}
	}
}

func TestWorkflowNewTabCreatesOnlyConfiguredHomepage(t *testing.T) {
	for _, site := range Sites {
		e := &workflowViewExecutor{}
		page, err := createWorkflowPage(cdp.WithExecutor(context.Background(), e), site)
		if err != nil || page.URL != site.Home || len(e.created) != 1 || e.created[0] != site.Home {
			t.Fatalf("%s: created=%v page=%+v err=%v", site.ID, e.created, page, err)
		}
	}
}

func TestWorkflowExecutionRequiresExactOriginalButReusesItsOwnNewPage(t *testing.T) {
	site, _ := SiteByID("qianwen")
	for _, tc := range []struct {
		name, original, previous string
		pages                    []map[string]any
		want                     string
		wantErr                  error
		creates                  int
	}{
		{"live original", "original", "", []map[string]any{{"targetId": "original", "type": "page", "url": site.Home}}, "original", nil, 0},
		{"missing is not another page", "original", "other", []map[string]any{{"targetId": "other", "type": "page", "url": site.Home}}, "", ErrWorkflowPageMissing, 0},
		{"wrong platform", "original", "", []map[string]any{{"targetId": "original", "type": "page", "url": "https://example.com/"}}, "", ErrWorkflowPageMismatch, 0},
		{"reuse own next-question page", "", "previous", []map[string]any{{"targetId": "previous", "type": "page", "url": site.Home}}, "previous", nil, 0},
		{"create first page", "", "", nil, "restored", nil, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := &workflowViewExecutor{pages: tc.pages}
			id, err := resolveWorkflowTarget(cdp.WithExecutor(context.Background(), e), site, tc.original, tc.previous)
			if id != tc.want || !errors.Is(err, tc.wantErr) || len(e.created) != tc.creates {
				t.Fatalf("id=%s err=%v created=%v", id, err, e.created)
			}
		})
	}
}

// Real independent Chrome, a local fixture, and a deliberately closed control
// page reproduce the production failure without touching any sampling profile.
func TestMacWorkflowOpenSurvivesClosedControlPage(t *testing.T) {
	if runtime.GOOS != "darwin" || os.Getenv("GEO_TEST_MAC_CDP") != "1" {
		t.Skip("opt-in Mac Chrome integration")
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "<title>Original local question</title><textarea>unsent fixture</textarea>")
	}))
	defer srv.Close()
	site, _ := SiteByID("qianwen")
	site.Home = srv.URL
	site.Login = srv.URL
	profile := t.TempDir()
	m := New("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", profile, "")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	ws, _, err := m.ensureMacChrome(ctx)
	if err != nil {
		t.Fatal(err)
	}
	b, err := chromedp.NewBrowser(ctx, ws)
	if err != nil {
		t.Fatal(err)
	}
	bctx := cdp.WithExecutor(ctx, b)
	processes, err := systeminfo.GetProcessInfo().Do(bctx)
	if err != nil {
		t.Fatal(err)
	}
	var browserPID int
	for _, process := range processes {
		if process.Type == "browser" {
			browserPID = int(process.ID)
		}
	}
	defer func() {
		_ = browser.Close().Do(bctx)
		process, _ := os.FindProcess(browserPID)
		for i := 0; i < 100; i++ {
			if process == nil || process.Signal(syscall.Signal(0)) != nil {
				break
			}
			time.Sleep(100 * time.Millisecond)
		}
	}()
	control, err := target.CreateTarget("about:blank").Do(bctx)
	if err != nil {
		t.Fatal(err)
	}
	original, err := target.CreateTarget(srv.URL).Do(bctx)
	if err != nil {
		t.Fatal(err)
	}
	if err = target.CloseTarget(control).Do(bctx); err != nil {
		t.Fatal(err)
	}
	view, stop, err := profileBrowserContext(ctx, profile)
	if err != nil {
		t.Fatal(err)
	}
	page, err := locateWorkflowPage(view, site, string(original), "")
	if err != nil {
		t.Fatal(err)
	}
	window, _, err := browser.GetWindowForTarget().WithTargetID(original).Do(view)
	if err != nil {
		t.Fatal(err)
	}
	if err = browser.SetWindowBounds(window, &browser.Bounds{WindowState: browser.WindowStateMinimized}).Do(view); err != nil {
		t.Fatal(err)
	}
	if err = revealWorkflowPage(view, page); err != nil {
		t.Fatal(err)
	}
	_, bounds, err := browser.GetWindowForTarget().WithTargetID(original).Do(view)
	if err != nil || bounds.WindowState == browser.WindowStateMinimized {
		t.Fatalf("window not restored: %+v %v", bounds, err)
	}
	before, err := target.GetTargets().Do(view)
	if err != nil {
		t.Fatal(err)
	}
	stop() // Disconnecting view must preserve the original page and browser.
	check, finish, err := profileBrowserContext(ctx, profile)
	if err != nil {
		t.Fatal(err)
	}
	defer finish()
	after, err := target.GetTargets().Do(check)
	if err != nil {
		t.Fatal(err)
	}
	if len(before) != len(after) {
		t.Fatalf("view changed tab count: %d -> %d", len(before), len(after))
	}
	if _, err = locateWorkflowPage(check, site, string(original), ""); err != nil {
		t.Fatal(err)
	}
	newPage, err := createWorkflowPage(check, site)
	if err != nil {
		t.Fatal(err)
	}
	if err = revealWorkflowPage(check, newPage); err != nil {
		t.Fatal(err)
	}
	if newPage.TargetID == string(original) {
		t.Fatal("new-tab reused original page")
	}
	if _, err = locateWorkflowPage(check, site, string(original), ""); err != nil {
		t.Fatal("new tab removed original", err)
	}
	if _, err = os.Stat(filepath.Join(profile, "DevToolsActivePort")); err != nil {
		t.Fatal(err)
	}
	// Reproduce the actual resume failure: the manager's page/control context
	// has died while the account's independent Chrome and exact page are live.
	oldSites := append([]Site(nil), Sites...)
	defer func() { Sites = oldSites }()
	for i := range Sites {
		if Sites[i].ID == site.ID {
			Sites[i] = site
		}
	}
	pool := NewPool(m.ChromePath, profile, "13800138000", filepath.Join(t.TempDir(), "accounts.json"))
	stale := New(m.ChromePath, profile, "")
	dead, kill := context.WithCancel(context.Background())
	kill()
	stale.ctrlCtx = dead
	stale.browserCtx = dead
	stale.ready = true
	stale.externalChrome = true
	pool.runtimes["default"] = stale
	lease, err := pool.AcquireWorkflow(ctx, "fixture/qianwen", "default", site.ID, string(original))
	if err != nil {
		t.Fatal("live original cannot be acquired after control page closed", err)
	}
	if lease.SiteTargetID() != string(original) {
		t.Fatal("resume substituted original question page")
	}
	lease.Release()
	if _, err = locateWorkflowPage(check, site, string(original), ""); err != nil {
		t.Fatal("releasing workflow closed original question", err)
	}
}
