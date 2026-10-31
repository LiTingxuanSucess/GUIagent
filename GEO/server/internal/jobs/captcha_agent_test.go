package jobs

import (
	"testing"
	"time"
)

func TestCursorCaptchaAgentEligible(t *testing.T) {
	t.Setenv("GEO_CURSOR_CAPTCHA_AGENT", "")
	if cursorCaptchaAgentEligible("doubao", "平台触发安全验证，请完成验证后重试") {
		t.Fatal("disabled agent was eligible")
	}
	t.Setenv("GEO_CURSOR_CAPTCHA_AGENT", "1")
	cases := []struct {
		site, message string
		want          bool
	}{
		{"doubao", "平台触发安全验证，请完成验证后重试", true},
		{"doubao", "豆包触发语义安全验证，请人工完成验证后重试", true},
		{"doubao", "已点击发送，请在截图上完成安全验证", false},
		{"qianwen", "平台触发安全验证，请完成验证后重试", false},
		{"doubao", "登录有效", false},
	}
	for _, tc := range cases {
		if got := cursorCaptchaAgentEligible(tc.site, tc.message); got != tc.want {
			t.Fatalf("eligible(%s, %q)=%v want %v", tc.site, tc.message, got, tc.want)
		}
	}
}

func TestStartCursorCaptchaAgentOncePerAccount(t *testing.T) {
	t.Setenv("GEO_CURSOR_CAPTCHA_AGENT", "1")
	releaseCaptchaAgent("acct_test")
	var runs int
	prev := runCaptchaAgentFn
	runCaptchaAgentFn = func(accountID, phone, site, jobID string) {
		runs++
		time.Sleep(50 * time.Millisecond)
	}
	t.Cleanup(func() {
		runCaptchaAgentFn = prev
		releaseCaptchaAgent("acct_test")
	})
	startCursorCaptchaAgent("acct_test", "13800138000", "doubao", "job", "平台触发安全验证，请完成验证后重试")
	startCursorCaptchaAgent("acct_test", "13800138000", "doubao", "job", "平台触发安全验证，请完成验证后重试")
	time.Sleep(20 * time.Millisecond)
	if runs != 1 {
		t.Fatalf("runs=%d want 1", runs)
	}
	time.Sleep(80 * time.Millisecond)
	startCursorCaptchaAgent("acct_test", "13800138000", "doubao", "job2", "豆包触发语义安全验证，请人工完成验证后重试")
	time.Sleep(80 * time.Millisecond)
	if runs != 2 {
		t.Fatalf("runs=%d want 2 after the first agent finished", runs)
	}
}
