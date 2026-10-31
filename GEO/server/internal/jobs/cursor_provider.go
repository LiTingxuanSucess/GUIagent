package jobs

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Provider outages are shared by all platforms and survive a service restart.
// No raw provider response, credentials, or browser context is persisted here.
type CursorProviderHealth struct {
	State      string    `json:"state"`
	ErrorCode  string    `json:"error_code,omitempty"`
	RetryAt    time.Time `json:"retry_at,omitempty"`
	UpdatedAt  time.Time `json:"updated_at,omitempty"`
	ProbeUntil time.Time `json:"probe_until,omitempty"`
	Revision   int64     `json:"revision"`
}

var cursorProviderMu sync.Mutex
var cursorProviderCheckMu sync.Mutex

// A real, read-only model response confirms provider recovery independently
// of whether a webpage still needs human attention.
func (q *Queue) RecheckCursorProvider(parent context.Context) CursorProviderHealth {
	if !cursorProviderCheckMu.TryLock() {
		return CursorProviderHealth{State: "probing", ErrorCode: "cli_provider_probing"}
	}
	defer cursorProviderCheckMu.Unlock()
	if !claimCursorCapacity() {
		return CursorProviderHealth{State: "probing", ErrorCode: "cli_busy"}
	}
	defer releaseCursorCapacity()
	workspace := q.cursorWorkspace()
	permit, code := acquireCursorProvider(workspace, time.Now())
	if code != "" {
		return q.CursorHealth()
	}
	ctx, cancel := context.WithTimeout(parent, 45*time.Second)
	defer cancel()
	bin := strings.TrimSpace(os.Getenv("GEO_CURSOR_AGENT"))
	if bin == "" {
		bin = "/root/.local/bin/agent"
	}
	args := []string{"-p", "--mode", "ask", "--trust", "--workspace", cursorAgentWorkspace(workspace), "--output-format", "stream-json"}
	if model := strings.TrimSpace(os.Getenv("GEO_CURSOR_MODEL")); model != "" {
		args = append(args, "--model", model)
	}
	args = append(args, "Reply only GEO_PROVIDER_OK. Do not use tools, read files, browse, or modify anything.")
	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = cursorAgentWorkspace(workspace)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.WaitDelay = 2 * time.Second
	cmd.Cancel = func() error {
		if cmd.Process != nil {
			return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		return nil
	}
	output := &cursorOutput{}
	cmd.Stdout, cmd.Stderr = output, output
	err := cmd.Run()
	code = output.providerCode()
	responsive := err == nil && output.hasProviderResponse()
	permit.complete(code, responsive, time.Now())
	health := q.CursorHealth()
	if !responsive && code == "" && health.State == "ready" {
		return CursorProviderHealth{State: "unavailable", ErrorCode: "cli_probe_failed"}
	}
	return health
}

func CursorProviderErrorLabel(code string) string {
	return map[string]string{"cli_billing_required": "Cursor 账单未支付，辅助登录已暂停，请处理账单后等待自动复查", "cli_auth_failed": "Cursor 认证失效，辅助登录已暂停", "cli_quota_exhausted": "Cursor 额度不足，辅助登录已暂停", "cli_rate_limited": "Cursor 限流，等待冷却后重试", "cli_transport_failed": "Cursor 上游连接失败，等待自动复查", "cli_provider_probing": "Cursor 正在复查可用性，请稍后重试", "cli_health_unavailable": "Cursor 健康状态暂不可用"}[code]
}

func cursorProviderPath(workspace string) string {
	return filepath.Join(filepath.Dir(cursorCallDirectory(workspace)), "cursor-provider-health.json")
}

func readCursorProvider(path string) (CursorProviderHealth, error) {
	h := CursorProviderHealth{State: "unknown"}
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return h, nil
	}
	if err != nil {
		return h, err
	}
	err = json.Unmarshal(raw, &h)
	return h, err
}

func writeCursorProvider(path string, h CursorProviderHealth) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	raw, err := json.Marshal(h)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".cursor-health-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(raw); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

func (q *Queue) CursorHealth() CursorProviderHealth {
	cursorProviderMu.Lock()
	defer cursorProviderMu.Unlock()
	h, err := readCursorProvider(cursorProviderPath(q.cursorWorkspace()))
	if err != nil {
		return CursorProviderHealth{State: "unavailable", ErrorCode: "cli_health_unavailable"}
	}
	if h.ErrorCode != "" && time.Now().Before(h.RetryAt) {
		h.State = "blocked"
	} else if time.Now().Before(h.ProbeUntil) {
		h.State = "probing"
	} else if h.ErrorCode != "" {
		h.State = "retry_available"
	}
	return h
}

type cursorProviderPermit struct {
	path     string
	revision int64
}

func (p *cursorProviderPermit) canStart(now time.Time) string {
	if p == nil {
		return "cli_health_unavailable"
	}
	cursorProviderMu.Lock()
	defer cursorProviderMu.Unlock()
	h, err := readCursorProvider(p.path)
	if err != nil {
		return "cli_health_unavailable"
	}
	if now.Before(h.RetryAt) {
		return h.ErrorCode
	}
	if now.Before(h.ProbeUntil) && h.Revision != p.revision {
		return "cli_provider_probing"
	}
	return ""
}

func acquireCursorProvider(workspace string, now time.Time) (*cursorProviderPermit, string) {
	cursorProviderMu.Lock()
	defer cursorProviderMu.Unlock()
	path := cursorProviderPath(workspace)
	h, err := readCursorProvider(path)
	if err != nil {
		return nil, "cli_health_unavailable"
	}
	if now.Before(h.RetryAt) {
		return nil, h.ErrorCode
	}
	if now.Before(h.ProbeUntil) {
		return nil, "cli_provider_probing"
	}
	if h.ErrorCode != "" {
		// Exactly one real pending invocation may test recovery after cooldown.
		h.State = "probing"
		h.ProbeUntil = now.Add(10 * time.Minute)
		h.Revision++
		if writeCursorProvider(path, h) != nil {
			return nil, "cli_health_unavailable"
		}
	}
	return &cursorProviderPermit{path: path, revision: h.Revision}, ""
}

func cursorProviderCooldown(code string) time.Duration {
	switch code {
	case "cli_billing_required":
		return 15 * time.Minute
	case "cli_auth_failed", "cli_quota_exhausted":
		return 5 * time.Minute
	case "cli_rate_limited":
		return time.Minute
	case "cli_transport_failed":
		return 30 * time.Second
	}
	return 0
}

func (p *cursorProviderPermit) complete(code string, responsive bool, now time.Time) {
	if p == nil {
		return
	}
	cursorProviderMu.Lock()
	defer cursorProviderMu.Unlock()
	h, err := readCursorProvider(p.path)
	if err != nil {
		return
	}
	if delay := cursorProviderCooldown(code); delay > 0 {
		h.State = "blocked"
		h.ErrorCode = code
		h.RetryAt = now.Add(delay)
		h.ProbeUntil = time.Time{}
		h.UpdatedAt = now
		h.Revision++
	} else if h.Revision == p.revision {
		// A sibling's newer outage must not be cleared by an older success.
		h.ProbeUntil = time.Time{}
		if responsive {
			h.State = "ready"
			h.ErrorCode = ""
			h.RetryAt = time.Time{}
			h.UpdatedAt = now
			h.Revision++
		} else if h.ErrorCode != "" {
			h.RetryAt = now.Add(time.Minute)
			h.State = "blocked"
		}
	} else {
		return
	}
	_ = writeCursorProvider(p.path, h)
}

func cursorProviderError(text string) string {
	text = strings.ToLower(text)
	switch {
	case strings.Contains(text, "unpaid invoice"), strings.Contains(text, "payment required"):
		return "cli_billing_required"
	case strings.Contains(text, "unauthorized"), strings.Contains(text, "invalid api key"), strings.Contains(text, "authentication required"), strings.Contains(text, "not logged in"), strings.Contains(text, "token expired"):
		return "cli_auth_failed"
	case strings.Contains(text, "rate limit"), strings.Contains(text, "too many requests"):
		return "cli_rate_limited"
	case strings.Contains(text, "usage limit"), strings.Contains(text, "quota exceeded"), strings.Contains(text, "insufficient credits"):
		return "cli_quota_exhausted"
	case strings.Contains(text, "econnrefused"), strings.Contains(text, "enotfound"), strings.Contains(text, "fetch failed"), strings.Contains(text, "connection reset"):
		return "cli_transport_failed"
	}
	return ""
}
