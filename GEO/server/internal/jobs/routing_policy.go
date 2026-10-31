package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"time"
)

const (
	RoutingWebFirst = "web_first"
	RoutingAPIOnly  = "api_only"
	RoutingMixed    = "mixed"
)

var ErrRoutingConflict = errors.New("request routing changed; reload current settings")
var ErrRoutingSite = errors.New("未知平台")

var routingSites = []string{"doubao", "deepseek", "kimi", "qianwen", "wenxinyiyan", "yuanbao"}

type SiteRoutingPolicy struct {
	WebFirst bool   `json:"web_first"`
	Mode     string `json:"mode"`
}

type RoutingPolicy struct {
	WebFirst  bool                         `json:"web_first"`
	Mode      string                       `json:"mode"`
	Revision  uint64                       `json:"revision"`
	UpdatedAt string                       `json:"updated_at,omitempty"`
	Sites     map[string]SiteRoutingPolicy `json:"sites"`
}

type routingPolicyStore struct {
	mu      sync.Mutex
	state   *RoutingPolicy
	loadErr error
}

type routingModeContextKey struct{}
type routingSitesContextKey struct{}

func routingSiteID(site string) string {
	if internalSiteID(site) == "yiyan" {
		return "wenxinyiyan"
	}
	return site
}

func knownRoutingSite(site string) bool {
	for _, candidate := range routingSites {
		if candidate == routingSiteID(site) {
			return true
		}
	}
	return false
}

func siteRouting(webFirst bool) SiteRoutingPolicy {
	mode := RoutingAPIOnly
	if webFirst {
		mode = RoutingWebFirst
	}
	return SiteRoutingPolicy{WebFirst: webFirst, Mode: mode}
}

func cloneRoutingPolicy(state RoutingPolicy) RoutingPolicy {
	sites := make(map[string]SiteRoutingPolicy, len(routingSites))
	for _, site := range routingSites {
		value, exists := state.Sites[site]
		if !exists {
			value = siteRouting(state.WebFirst)
		}
		sites[site] = value
	}
	state.Sites = sites
	modes := map[string]string{}
	for site, policy := range sites {
		modes[site] = policy.Mode
	}
	state.Mode = aggregateRoutingModes(modes)
	return state
}

func aggregateRoutingModes(modes map[string]string) string {
	mode := ""
	for _, candidate := range modes {
		candidate = canonicalRoutingMode(candidate)
		if mode != "" && mode != candidate {
			return RoutingMixed
		}
		mode = candidate
	}
	return canonicalRoutingMode(mode)
}

func (p RoutingPolicy) snapshotSites(sites []string) map[string]string {
	modes := make(map[string]string, len(sites))
	for _, site := range sites {
		key := routingSiteID(site)
		policy, exists := p.Sites[key]
		if !exists {
			policy = siteRouting(p.WebFirst)
		}
		modes[key] = policy.Mode
	}
	return modes
}

func (j *Job) routingMode(site string) string {
	if mode, exists := j.SiteExecutionModes[routingSiteID(site)]; exists {
		return canonicalRoutingMode(mode)
	}
	return canonicalRoutingMode(j.ExecutionMode)
}

func routingModeForContext(ctx context.Context, site string) string {
	if modes, ok := ctx.Value(routingSitesContextKey{}).(map[string]string); ok {
		if mode, exists := modes[routingSiteID(site)]; exists {
			return canonicalRoutingMode(mode)
		}
	}
	mode, _ := ctx.Value(routingModeContextKey{}).(string)
	return canonicalRoutingMode(mode)
}

func canonicalRoutingMode(mode string) string {
	if mode == RoutingAPIOnly {
		return RoutingAPIOnly
	}
	return RoutingWebFirst // Existing accepted jobs keep their original behavior.
}

func (q *Queue) routingPolicyPath() string {
	return filepath.Join(q.DataDir, "request-routing", "state.json")
}

func (q *Queue) loadRoutingPolicy() {
	if q.DataDir == "" {
		return
	}
	raw, err := os.ReadFile(q.routingPolicyPath())
	if os.IsNotExist(err) {
		return
	}
	var state struct {
		WebFirst  *bool  `json:"web_first"`
		Revision  uint64 `json:"revision"`
		UpdatedAt string `json:"updated_at"`
		Sites     map[string]struct {
			WebFirst *bool `json:"web_first"`
		} `json:"sites"`
	}
	if err == nil {
		err = json.Unmarshal(raw, &state)
		if err == nil && state.WebFirst == nil {
			err = errors.New("missing routing setting")
		}
		if err == nil {
			for site, policy := range state.Sites {
				if !knownRoutingSite(site) || routingSiteID(site) != site || policy.WebFirst == nil {
					err = errors.New("invalid platform routing setting")
					break
				}
			}
		}
	}
	if err != nil {
		q.routing.loadErr = err
		log.Printf("request routing state unavailable; new submissions suspended")
		return
	}
	sites := map[string]SiteRoutingPolicy{}
	for site, policy := range state.Sites {
		sites[site] = siteRouting(*policy.WebFirst)
	}
	policy := cloneRoutingPolicy(RoutingPolicy{WebFirst: *state.WebFirst, Revision: state.Revision, UpdatedAt: state.UpdatedAt, Sites: sites})
	q.routing.state = &policy
}

func (q *Queue) RoutingPolicy() (RoutingPolicy, error) {
	q.routing.mu.Lock()
	defer q.routing.mu.Unlock()
	if q.routing.loadErr != nil {
		return RoutingPolicy{}, errors.New("请求渠道设置无法读取，请检查服务配置")
	}
	if q.routing.state == nil {
		return cloneRoutingPolicy(RoutingPolicy{WebFirst: true}), nil
	}
	return cloneRoutingPolicy(*q.routing.state), nil
}

// Persist first, publish second. A failed disk write cannot change live routing.
func (q *Queue) SetRoutingPolicy(webFirst bool, revision uint64) (RoutingPolicy, error) {
	return q.setRoutingPolicy("", webFirst, revision)
}

func (q *Queue) SetSiteRoutingPolicy(site string, webFirst bool, revision uint64) (RoutingPolicy, error) {
	if !knownRoutingSite(site) {
		return RoutingPolicy{}, ErrRoutingSite
	}
	return q.setRoutingPolicy(routingSiteID(site), webFirst, revision)
}

func (q *Queue) setRoutingPolicy(site string, webFirst bool, revision uint64) (RoutingPolicy, error) {
	q.routing.mu.Lock()
	defer q.routing.mu.Unlock()
	current := cloneRoutingPolicy(RoutingPolicy{WebFirst: true})
	if q.routing.state != nil {
		current = cloneRoutingPolicy(*q.routing.state)
	}
	if q.routing.loadErr != nil {
		return current, errors.New("请求渠道设置无法读取")
	}
	if revision != current.Revision {
		return current, ErrRoutingConflict
	}
	if q.DataDir == "" {
		return current, errors.New("请求渠道设置存储目录不可用")
	}
	next := cloneRoutingPolicy(current)
	if site == "" {
		next.WebFirst = webFirst
		for _, key := range routingSites {
			next.Sites[key] = siteRouting(webFirst)
		}
	} else {
		next.Sites[site] = siteRouting(webFirst)
	}
	next = cloneRoutingPolicy(next)
	if q.routing.state != nil && reflect.DeepEqual(current, next) {
		return current, nil
	}
	next.Revision, next.UpdatedAt = current.Revision+1, time.Now().Format(time.RFC3339Nano)
	path := q.routingPolicyPath()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return current, errors.New("请求渠道设置保存失败")
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".routing-*")
	if err != nil {
		return current, errors.New("请求渠道设置保存失败")
	}
	defer os.Remove(file.Name())
	if err = json.NewEncoder(file).Encode(next); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(file.Name(), path)
	}
	if err != nil {
		return current, errors.New("请求渠道设置保存失败")
	}
	q.routing.state = &next
	log.Printf("request routing updated site=%s mode=%s revision=%d scope=new_jobs", site, next.Mode, next.Revision)
	return cloneRoutingPolicy(next), nil
}

func (q *Queue) runDirectAPIContext(parent context.Context, site, prompt string) (out map[string]any) {
	parent, attempts := newAttemptContext(parent)
	started := time.Now()
	defer func() {
		if out != nil {
			out["routing_mode"], out["source"], out["account_id"] = RoutingAPIOnly, "official_api", "api:"+internalSiteID(site)
			out["web_elapsed_ms"] = int64(0)
			delete(out, "web_error_code")
			delete(out, "web_skipped")
			delete(out, "web_skip_reason")
			delete(out, "api_fallback_failed")
			if out["ok"] != true && stringField(out, "api_error_code") != "" {
				out["error_code"] = out["api_error_code"]
				out["error"] = out["api_error"]
				out["elapsed_ms"] = int64(evidenceCount(out["api_elapsed_ms"]))
			}
			out["wait_ms"] = int64(evidenceCount(out["api_wait_ms"]))
			out["total_ms"] = time.Since(started).Milliseconds()
			attempts.attach(out)
		}
	}()
	base := siteError(site, prompt, "api_request_failed", "API 尚未取得回答", true, 0)
	base["source"], base["routing_mode"] = "official_api", RoutingAPIOnly
	return q.applyAPIContext(parent, site, prompt, base, true)
}
