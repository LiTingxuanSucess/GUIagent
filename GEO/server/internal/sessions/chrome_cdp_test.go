package sessions

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/chromedp"
)

func TestProfileChromeRejectsStaleOrForeignBrowser(t *testing.T) {
	for _, tc := range []struct {
		name, websocket string
		ok              bool
	}{
		{"matching browser", "ws://HOST/devtools/browser/expected", true},
		{"stale port reused by another browser", "ws://HOST/devtools/browser/other", false},
		{"foreign websocket", "ws://example.com/devtools/browser/expected", false},
		{"websocket credentials", "ws://secret@HOST/devtools/browser/expected", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var ws string
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/json/version" {
					t.Errorf("unexpected probe path %s", r.URL.Path)
				}
				_ = json.NewEncoder(w).Encode(map[string]string{"webSocketDebuggerUrl": ws})
			}))
			defer srv.Close()
			u, _ := url.Parse(srv.URL)
			ws = strings.ReplaceAll(tc.websocket, "HOST", u.Host)
			profile := t.TempDir()
			if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte(u.Port()+"\n/devtools/browser/expected\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			_, err := probeProfileChrome(context.Background(), profile)
			if (err == nil) != tc.ok {
				t.Fatalf("probe success=%v, want %v: %v", err == nil, tc.ok, err)
			}
		})
	}
}

func TestProfileChromeRejectsInvalidPorts(t *testing.T) {
	for _, port := range []string{"", "0", "65536", "-1", "9222@other-host", "http://example.com"} {
		t.Run(port, func(t *testing.T) {
			profile := t.TempDir()
			if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte(port), 0o600); err != nil {
				t.Fatal(err)
			}
			if _, _, err := profileCDPAddress(profile); err == nil {
				t.Fatal("invalid CDP port accepted")
			}
		})
	}
}

func TestAdoptPageMatchesLoginDomainsWithoutSubstringTrust(t *testing.T) {
	site, _ := SiteByID("qianwen")
	for _, tc := range []struct {
		page string
		want bool
	}{
		{"https://www.qianwen.com/", true},
		{"https://qianwen.com/", true},
		{"https://passport.qianwen.com/login", true},
		{"https://example.com/?return=https://www.qianwen.com/", false},
		{"https://www.qianwen.com.example.com/", false},
		{"https://www.qianwen.com@example.com/", false},
		{"about:blank", false},
	} {
		if got := siteOwnsPage(site, tc.page); got != tc.want {
			t.Errorf("owns %q = %v, want %v", tc.page, got, tc.want)
		}
	}
	wenxin, _ := SiteByID("yiyan")
	if !siteOwnsPage(wenxin, "https://wenxin.baidu.com/") || siteOwnsPage(site, "https://wenxin.baidu.com/") {
		t.Fatal("Wenxin redirect must belong only to the Wenxin site")
	}
}

// Explicit opt-in: uses an isolated temporary profile and a local test page;
// sends no platform requests or SMS and closes only this test's Chrome.
func TestMacCDPStopAndReconnectPreservesPage(t *testing.T) {
	if runtime.GOOS != "darwin" || os.Getenv("GEO_TEST_MAC_CDP") != "1" {
		t.Skip("set GEO_TEST_MAC_CDP=1 on macOS for real Chrome lifecycle test")
	}
	t.Setenv("GEO_CHROME_MODE", "mac-cdp")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("<title>GEO CDP lifecycle fixture</title>"))
	}))
	defer srv.Close()
	profile := t.TempDir()
	chrome := "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
	m := New(chrome, profile, "")
	m.SessionStorePath = filepath.Join(profile, ".session-store.json")
	if err := m.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		m.Stop()
		ws, err := probeProfileChrome(context.Background(), profile)
		if err != nil {
			return
		}
		alloc, cancelAlloc := chromedp.NewRemoteAllocator(context.Background(), ws, chromedp.NoModifyURL)
		defer cancelAlloc()
		ctx, cancel := chromedp.NewContext(alloc)
		defer cancel()
		_ = chromedp.Run(ctx, chromedp.ActionFunc(func(ctx context.Context) error { return browser.Close().Do(ctx) }))
		for i := 0; i < 50; i++ {
			if _, err := os.Lstat(filepath.Join(profile, "SingletonLock")); os.IsNotExist(err) {
				break
			}
			time.Sleep(100 * time.Millisecond)
		}
		time.Sleep(200 * time.Millisecond)
	})
	page := m.tabs["deepseek"]
	if err := chromedp.Run(page, chromedp.Navigate(srv.URL), chromedp.Evaluate(`localStorage.setItem("geo-test", "preserved"); document.cookie="geo_test=preserved; path=/"`, nil)); err != nil {
		t.Fatal(err)
	}
	m.mu.Lock()
	m.opened["deepseek"] = true
	m.mu.Unlock()
	before := chromedp.FromContext(page).Target.TargetID
	ws, err := probeProfileChrome(context.Background(), profile)
	if err != nil {
		t.Fatal(err)
	}
	m.Stop()
	if current, err := probeProfileChrome(context.Background(), profile); err != nil || current != ws {
		t.Fatalf("stopping manager closed/replaced independent Chrome: %v", err)
	}
	next := New(chrome, profile, "")
	next.SessionStorePath = m.SessionStorePath
	if err := next.Start(); err != nil {
		t.Fatal(err)
	}
	defer next.Stop()
	if !next.adoptExistingPage(Site{ID: "deepseek", Home: srv.URL, Login: srv.URL}) {
		t.Fatal("could not reattach existing page")
	}
	if after := chromedp.FromContext(next.tabs["deepseek"]).Target.TargetID; after != before {
		t.Fatal("reconnect replaced existing tab")
	}
	var stored, cookie string
	if err := runTab(next.tabs["deepseek"], 5*time.Second,
		chromedp.Evaluate(`localStorage.getItem("geo-test")`, &stored), chromedp.Evaluate(`document.cookie`, &cookie)); err != nil {
		t.Fatal(err)
	}
	if stored != "preserved" || !strings.Contains(cookie, "geo_test=preserved") {
		t.Fatalf("browser state lost: storage=%q cookie=%q", stored, cookie)
	}
	// A failed acquisition must leave the independent process available too.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	failed := New(chrome, profile, "")
	if err := failed.StartContext(ctx); err == nil {
		t.Fatal("cancelled start succeeded")
	}
	if _, err := probeProfileChrome(context.Background(), profile); err != nil {
		t.Fatal("cancelled start stopped Chrome")
	}
}
