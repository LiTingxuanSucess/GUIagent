package sessions

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

func macCDPMode() (bool, error) {
	switch mode := strings.TrimSpace(os.Getenv("GEO_CHROME_MODE")); mode {
	case "", "auto":
		return runtime.GOOS == "darwin", nil
	case "exec":
		return false, nil
	case "mac-cdp":
		if runtime.GOOS != "darwin" {
			return false, fmt.Errorf("GEO_CHROME_MODE=mac-cdp 仅支持 macOS")
		}
		return true, nil
	default:
		return false, fmt.Errorf("未知 GEO_CHROME_MODE: %q", mode)
	}
}

// Independent Chrome owns the profile and tabs; the server owns only its
// connection. In particular, never remove Chrome locks or session files here.
func (m *Manager) ensureMacChrome(parent context.Context) (string, bool, error) {
	ctx, cancel := context.WithTimeout(parent, 45*time.Second)
	defer cancel()
	if wsURL, err := probeProfileChrome(ctx, m.ProfileDir); err == nil {
		log.Printf("mac Chrome CDP reuse profile=%s", filepath.Base(m.ProfileDir))
		return wsURL, true, nil
	}
	args, err := macChromeOpenArgs(m.ChromePath, m.ProfileDir)
	if err != nil {
		return "", false, err
	}
	if _, err := os.Stat(m.ChromePath); err != nil {
		return "", false, fmt.Errorf("Chrome 程序不可用: %w", err)
	}
	// LaunchServices keeps Chrome independent of the GEO LaunchAgent. Closing
	// or restarting GEO therefore does not terminate the browser process.
	if out, err := exec.CommandContext(ctx, "/usr/bin/open", args...).CombinedOutput(); err != nil {
		return "", false, fmt.Errorf("打开 Mac Chrome: %w: %s", err, strings.TrimSpace(string(out)))
	}
	var lastErr error
	for ctx.Err() == nil {
		wsURL, err := probeProfileChrome(ctx, m.ProfileDir)
		if err == nil {
			log.Printf("mac Chrome CDP started profile=%s", filepath.Base(m.ProfileDir))
			return wsURL, false, nil
		}
		lastErr = err
		if err := waitContext(ctx, 200*time.Millisecond); err != nil {
			break
		}
	}
	return "", false, fmt.Errorf("等待 Chrome CDP: %w (%v)；请确认该账号资料未被未启用 CDP 的 Chrome 占用", ctx.Err(), lastErr)
}

func macChromeOpenArgs(chromePath, profileDir string) ([]string, error) {
	const marker = ".app/Contents/MacOS/"
	i := strings.Index(chromePath, marker)
	if i < 0 {
		return nil, fmt.Errorf("Mac CDP 模式需配置 Chrome.app 内的可执行文件")
	}
	profile, err := filepath.Abs(profileDir)
	if err != nil {
		return nil, err
	}
	return []string{"-na", chromePath[:i+len(".app")], "--args",
		"--user-data-dir=" + profile,
		"--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
		"--no-first-run", "--no-default-browser-check", "--restore-last-session"}, nil
}

func profileCDPAddress(profileDir string) (string, string, error) {
	raw, err := os.ReadFile(filepath.Join(profileDir, "DevToolsActivePort"))
	if err != nil {
		return "", "", fmt.Errorf("Chrome CDP 未就绪: %w", err)
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	port, err := strconv.Atoi(strings.TrimSpace(lines[0]))
	if err != nil || port < 1 || port > 65535 {
		return "", "", fmt.Errorf("DevTools 端口无效")
	}
	path := ""
	if len(lines) > 1 {
		path = strings.TrimSpace(lines[1])
	}
	return "http://127.0.0.1:" + strconv.Itoa(port), path, nil
}

func probeProfileChrome(ctx context.Context, profileDir string) (string, error) {
	endpoint, browserPath, err := profileCDPAddress(profileDir)
	if err != nil {
		return "", err
	}
	if !strings.HasPrefix(browserPath, "/devtools/browser/") {
		return "", fmt.Errorf("缺少该资料对应的 DevTools browser ID")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/json/version", nil)
	if err != nil {
		return "", err
	}
	// Never send a localhost CDP request through a configured internet proxy.
	transport := &http.Transport{Proxy: nil}
	defer transport.CloseIdleConnections()
	client := &http.Client{Timeout: 2 * time.Second, Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("Chrome CDP HTTP %d", resp.StatusCode)
	}
	var version struct {
		WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&version); err != nil {
		return "", err
	}
	u, err := url.Parse(version.WebSocketDebuggerURL)
	if err != nil || u.Scheme != "ws" || u.User != nil || u.RawQuery != "" || u.Fragment != "" ||
		u.Host != strings.TrimPrefix(endpoint, "http://") || u.Path != browserPath {
		return "", fmt.Errorf("Chrome CDP 与该账号资料的 browser ID 不匹配")
	}
	return u.String(), nil
}

func (m *Manager) siteContextParent(ctx context.Context) context.Context {
	if m.externalChrome {
		return context.WithoutCancel(ctx)
	}
	return ctx
}

// Match parsed hostnames, including the site's own login domains. Substring
// matching would miss passport pages and could adopt an unrelated website.
func siteOwnsPage(site Site, rawURL string) bool {
	u, err := url.Parse(rawURL)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") {
		return false
	}
	for _, raw := range []string{site.Home, site.Login} {
		configured, err := url.Parse(raw)
		if err == nil && strings.TrimPrefix(strings.ToLower(u.Hostname()), "www.") == strings.TrimPrefix(strings.ToLower(configured.Hostname()), "www.") {
			return true
		}
	}
	if site.ID == "yiyan" && strings.EqualFold(u.Hostname(), "wenxin.baidu.com") {
		return true
	}
	return false
}
