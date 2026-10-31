package sessions

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/domstorage"
	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/cdproto/storage"
	"github.com/chromedp/chromedp"
)

type cookieSnap struct {
	Name     string  `json:"name"`
	Value    string  `json:"value"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires"`
	HTTPOnly bool    `json:"http_only"`
	Secure   bool    `json:"secure"`
	SameSite string  `json:"same_site,omitempty"`
	Session  bool    `json:"session"`
}

type sessionStore struct {
	SavedAt      string                 `json:"saved_at"`
	Cookies      []cookieSnap           `json:"cookies"`
	LocalStorage map[string][][2]string `json:"local_storage"`
}

func (m *Manager) storePath() string {
	if m.SessionStorePath != "" {
		return m.SessionStorePath
	}
	return filepath.Join(filepath.Dir(m.ProfileDir), ".session-store.json")
}

func (m *Manager) restoreSession() {
	raw, err := os.ReadFile(m.storePath())
	if err != nil || len(raw) == 0 {
		return
	}
	var st sessionStore
	if err := json.Unmarshal(raw, &st); err != nil {
		log.Printf("session-store 读失败: %v", err)
		return
	}
	// The Chrome profile is the source of truth after a restart. The JSON
	// snapshot only fills cookies the profile no longer has, so an older
	// hy_token cannot log Yuanbao out on the next boot.
	profileSnaps := []cookieSnap{}
	if err := chromedp.Run(m.browserCtx, chromedp.ActionFunc(func(ctx context.Context) error {
		cookies, e := storage.GetCookies().Do(ctx)
		if e != nil {
			return e
		}
		profileSnaps = snapsFromNetwork(cookies)
		return nil
	})); err != nil {
		log.Printf("read profile cookies before restore: %v", err)
	}
	// A one-shot prefer file lets the post-login snapshot replace a stale
	// profile token on this start only. It is removed before cookies are set.
	prefer := consumePreferCookieNames(m.storePath() + ".prefer")
	params := cookieParams(cookiesToRestore(profileSnaps, st.Cookies, time.Now(), prefer))
	if len(params) == 0 {
		m.mu.Lock()
		m.pendingLS = st.LocalStorage
		m.mu.Unlock()
		return
	}
	if err := chromedp.Run(m.browserCtx, chromedp.ActionFunc(func(ctx context.Context) error {
		return storage.SetCookies(params).Do(ctx)
	})); err != nil {
		log.Printf("restore cookies: %v", err)
		return
	}
	log.Printf("restored %d cookies from session-store", len(params))
	m.mu.Lock()
	m.pendingLS = st.LocalStorage
	m.mu.Unlock()
}

func (m *Manager) applyCookieLoginGuess() {
	st := m.loadStore()
	if st == nil {
		return
	}
	type hint struct {
		site    string
		needAny []string
	}
	hints := []hint{
		{"deepseek", []string{"ds_session_id"}},
		{"doubao", []string{"sessionid", "uid_tt", "sid_tt"}},
		{"yiyan", []string{"BDUSS"}},
		{"yuanbao", []string{"hy_token"}},
		{"qianwen", []string{"tongyi_sso_ticket"}},
	}
	has := map[string]bool{}
	for _, c := range st.Cookies {
		if c.Name != "" && c.Value != "" {
			has[c.Name] = true
		}
	}
	for _, h := range hints {
		ok := false
		for _, name := range h.needAny {
			if has[name] {
				ok = true
				break
			}
		}
		if !ok {
			continue
		}
		// A cookie can remain long after the server-side session has expired.
		// Keep it for the next page load, but never promote the account to
		// logged_in until the site's active detector confirms the session.
		log.Printf("cookie hint found %s (active check required)", h.site)
	}
}

func (m *Manager) restoreLocalStorage(ctx context.Context, origin string) bool {
	m.mu.Lock()
	items := m.pendingLS[origin]
	m.mu.Unlock()
	if len(items) == 0 {
		return false
	}
	sid := &domstorage.StorageID{SecurityOrigin: origin, IsLocalStorage: true}
	tctx, cancel := context.WithTimeout(ctx, pageOpenBudget())
	defer cancel()
	err := chromedp.Run(tctx, chromedp.ActionFunc(func(c context.Context) error {
		for _, kv := range items {
			if err := domstorage.SetDOMStorageItem(sid, kv[0], kv[1]).Do(c); err != nil {
				return err
			}
		}
		return nil
	}))
	if err != nil {
		log.Printf("restore localStorage %s (%d keys): %v", origin, len(items), err)
		return false
	}
	m.mu.Lock()
	delete(m.pendingLS, origin)
	m.mu.Unlock()
	return true
}

func (m *Manager) dumpSession() {
	m.dumpMu.Lock()
	defer m.dumpMu.Unlock()
	m.mu.Lock()
	ctx := m.browserCtx
	if ctx == nil || m.dumpOff {
		m.mu.Unlock()
		return
	}
	m.mu.Unlock()
	var cookies []*network.Cookie
	tctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	err := chromedp.Run(tctx, chromedp.ActionFunc(func(c context.Context) error {
		var e error
		cookies, e = storage.GetCookies().Do(c)
		return e
	}))
	if err != nil {
		m.mu.Lock()
		m.dumpFails++
		if m.dumpFails >= 2 {
			m.dumpOff = true
			log.Printf("pause cookie flush after %d failures: %v", m.dumpFails, err)
		} else {
			log.Printf("dump cookies: %v", err)
		}
		m.mu.Unlock()
		return
	}
	m.mu.Lock()
	m.dumpFails = 0
	m.mu.Unlock()
	log.Printf("profile dump_cookies n=%d", len(cookies))
	snaps := make([]cookieSnap, 0, len(cookies))
	for _, c := range cookies {
		if c == nil || c.Value == "" {
			continue
		}
		snaps = append(snaps, cookieSnap{
			Name:     c.Name,
			Value:    c.Value,
			Domain:   c.Domain,
			Path:     c.Path,
			Expires:  c.Expires,
			HTTPOnly: c.HTTPOnly,
			Secure:   c.Secure,
			SameSite: string(c.SameSite),
			Session:  c.Session,
		})
	}
	ls := map[string][][2]string{}
	m.mu.Lock()
	tabs := make(map[string]context.Context, len(m.tabs))
	for id, ctx := range m.tabs {
		tabs[id] = ctx
	}
	states := make(map[string]string, len(m.states))
	opened := make(map[string]bool, len(m.opened))
	for id, st := range m.states {
		if st != nil {
			states[id] = st.Status
		}
		opened[id] = m.opened[id]
	}
	m.mu.Unlock()
	for id, ctx := range tabs {
		if !opened[id] {
			continue
		}
		origin := originOf(id)
		if origin == "" {
			continue
		}
		// 未登录时不要用空/游客 localStorage 覆盖上次好快照
		if states[id] != "logged_in" {
			continue
		}
		var items []domstorage.Item
		_ = runTab(ctx, 3*time.Second, chromedp.ActionFunc(func(c context.Context) error {
			var e error
			items, e = domstorage.GetDOMStorageItems(&domstorage.StorageID{
				SecurityOrigin: origin, IsLocalStorage: true,
			}).Do(c)
			return e
		}))
		pairs := make([][2]string, 0, len(items))
		for _, it := range items {
			if len(it) >= 2 {
				pairs = append(pairs, [2]string{it[0], it[1]})
			}
		}
		if len(pairs) > 0 {
			ls[origin] = pairs
		}
	}
	old := m.loadStore()
	if old != nil {
		for origin, pairs := range old.LocalStorage {
			if _, ok := ls[origin]; !ok && preserveOldOrigin(origin, states, opened) {
				ls[origin] = pairs
			}
		}
		// 仅在站点未打开或检测结果不确定时保留旧快照。主动确认退出/
		// 过期后必须接受浏览器中的删除结果，避免重启时恢复旧认证材料。
		keep := map[string]bool{}
		for id, st := range states {
			if preserveOldSession(st, opened[id]) {
				for _, d := range domainsOf(id) {
					keep[d] = true
				}
			}
		}
		// A live Yuanbao auth cookie must replace the preserved snapshot.
		// Otherwise the next browser start restores the pre-login token.
		forgetPreservedAuth(keep, snaps)
		if len(keep) > 0 {
			kept := make([]cookieSnap, 0, len(old.Cookies))
			for _, c := range old.Cookies {
				if hostKept(c.Domain, keep) {
					kept = append(kept, c)
				}
			}
			fresh := make([]cookieSnap, 0, len(snaps))
			for _, c := range snaps {
				if !hostKept(c.Domain, keep) {
					fresh = append(fresh, c)
				}
			}
			snaps = append(fresh, kept...)
		}
	}
	st := sessionStore{SavedAt: time.Now().Format(time.RFC3339), Cookies: snaps, LocalStorage: ls}
	raw, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return
	}
	path := m.storePath()
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return
	}
	if _, err := tmp.Write(raw); err != nil {
		_ = tmp.Close()
		return
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return
	}
	if err := tmp.Close(); err != nil {
		return
	}
	_ = os.Rename(tmpName, path)
}

func (m *Manager) loadStore() *sessionStore {
	raw, err := os.ReadFile(m.storePath())
	if err != nil {
		return nil
	}
	var st sessionStore
	if json.Unmarshal(raw, &st) != nil {
		return nil
	}
	return &st
}

func (m *Manager) flushLoop() {
	ticker := time.NewTicker(5 * time.Minute)
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
		m.mu.Lock()
		off := m.dumpOff
		m.mu.Unlock()
		if off {
			m.mu.Lock()
			m.dumpOff = false
			m.dumpFails = 0
			m.mu.Unlock()
		}
		m.dumpSession()
	}
}

func snapsFromNetwork(cookies []*network.Cookie) []cookieSnap {
	out := make([]cookieSnap, 0, len(cookies))
	for _, c := range cookies {
		if c == nil || c.Name == "" || c.Value == "" || c.Domain == "" {
			continue
		}
		out = append(out, cookieSnap{
			Name: c.Name, Value: c.Value, Domain: c.Domain, Path: c.Path,
			Expires: c.Expires, HTTPOnly: c.HTTPOnly, Secure: c.Secure,
			SameSite: string(c.SameSite), Session: c.Session,
		})
	}
	return out
}

func cookieIdentity(name, domain string) string {
	return name + "\n" + strings.TrimPrefix(strings.ToLower(domain), ".")
}

func cookieUnexpired(c cookieSnap, now time.Time) bool {
	if c.Value == "" {
		return false
	}
	if c.Session || c.Expires <= 0 {
		return true
	}
	return c.Expires > float64(now.Unix())
}

// cookiesToRestore returns snapshot cookies that the live profile does not
// already hold. An unexpired profile cookie wins, including Yuanbao hy_token,
// unless prefer names that cookie for this one restore.
func cookiesToRestore(profile, stored []cookieSnap, now time.Time, prefer map[string]bool) []cookieSnap {
	live := map[string]struct{}{}
	for _, c := range profile {
		if c.Name == "" || c.Domain == "" || !cookieUnexpired(c, now) {
			continue
		}
		live[cookieIdentity(c.Name, c.Domain)] = struct{}{}
	}
	out := make([]cookieSnap, 0)
	for _, c := range stored {
		if c.Name == "" || c.Domain == "" || c.Value == "" {
			continue
		}
		if _, ok := live[cookieIdentity(c.Name, c.Domain)]; ok && !prefer[c.Name] {
			continue
		}
		out = append(out, c)
	}
	return out
}

// consumePreferCookieNames reads and deletes a one-shot override. The file
// lists cookie names whose stored values must replace the profile copy.
func consumePreferCookieNames(path string) map[string]bool {
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return nil
	}
	if err := os.Remove(path); err != nil {
		log.Printf("remove one-shot cookie prefer file: %v", err)
		return nil
	}
	out := map[string]bool{}
	for _, name := range strings.Fields(string(raw)) {
		if name != "" {
			out[name] = true
		}
	}
	if len(out) > 0 {
		log.Printf("one-shot cookie restore prefers stored names n=%d", len(out))
	}
	return out
}

func cookieParams(cookies []cookieSnap) []*network.CookieParam {
	params := make([]*network.CookieParam, 0, len(cookies))
	for _, c := range cookies {
		if c.Name == "" || c.Domain == "" {
			continue
		}
		exp := c.Expires
		if c.Session || exp <= 0 {
			exp = float64(time.Now().Add(30 * 24 * time.Hour).Unix())
		}
		tse := cdp.TimeSinceEpoch(time.Unix(int64(exp), 0))
		p := &network.CookieParam{
			Name: c.Name, Value: c.Value, Domain: c.Domain, Path: c.Path,
			Secure: c.Secure, HTTPOnly: c.HTTPOnly, Expires: &tse,
		}
		if p.Path == "" {
			p.Path = "/"
		}
		switch strings.ToLower(c.SameSite) {
		case "strict":
			p.SameSite = network.CookieSameSiteStrict
		case "none":
			p.SameSite = network.CookieSameSiteNone
		case "lax":
			p.SameSite = network.CookieSameSiteLax
		}
		params = append(params, p)
	}
	return params
}

func freshHasCookie(snaps []cookieSnap, name, siteID string) bool {
	domains := map[string]bool{}
	for _, d := range domainsOf(siteID) {
		domains[d] = true
	}
	for _, c := range snaps {
		if c.Name == name && c.Value != "" && hostKept(c.Domain, domains) {
			return true
		}
	}
	return false
}

// forgetPreservedAuth drops domains whose fresh browser cookies already
// contain a login token, so dumpSession does not keep the previous token.
func forgetPreservedAuth(keep map[string]bool, fresh []cookieSnap) {
	if !freshHasCookie(fresh, "hy_token", "yuanbao") {
		return
	}
	for _, d := range domainsOf("yuanbao") {
		delete(keep, d)
	}
}

func preserveOldSession(status string, opened bool) bool {
	if !opened {
		return true
	}
	switch status {
	case "expired", "not_logged_in", "logged_in":
		return false
	default:
		return true
	}
}

func preserveOldOrigin(origin string, states map[string]string, opened map[string]bool) bool {
	for id, status := range states {
		if originOf(id) == origin {
			return preserveOldSession(status, opened[id])
		}
	}
	return true
}

func originOf(siteID string) string {
	switch siteID {
	case "deepseek":
		return "https://chat.deepseek.com"
	case "doubao":
		return "https://www.doubao.com"
	case "yiyan":
		return "https://wenxin.baidu.com"
	case "qianwen":
		return "https://www.qianwen.com"
	case "kimi":
		return "https://www.kimi.com"
	case "yuanbao":
		return "https://yuanbao.tencent.com"
	}
	return ""
}

func domainsOf(siteID string) []string {
	switch siteID {
	case "deepseek":
		return []string{"deepseek.com"}
	case "doubao":
		return []string{"doubao.com"}
	case "yiyan":
		return []string{"baidu.com", "bdimg.com"}
	case "qianwen":
		return []string{"qianwen.com", "aliyun.com"}
	case "kimi":
		return []string{"kimi.com", "moonshot.cn"}
	case "yuanbao":
		return []string{"yuanbao.tencent.com", "tencent.com"}
	}
	return nil
}

func hostKept(domain string, keep map[string]bool) bool {
	d := strings.TrimPrefix(domain, ".")
	for k := range keep {
		if d == k || strings.HasSuffix(d, "."+k) || strings.Contains(d, k) {
			return true
		}
	}
	return false
}
