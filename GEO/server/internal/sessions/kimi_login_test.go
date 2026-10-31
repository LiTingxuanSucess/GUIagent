package sessions

import "testing"

func TestKimiLoginRequiresAccountEvidence(t *testing.T) {
	for _, tc := range []struct {
		name, text                          string
		account, form, login, captcha, want bool
	}{
		{name: "public shell", text: "新建会话 我的 Kimi 插件 定时任务 尽管问"},
		{name: "account menu", text: "我的 Kimi Alice", account: true, want: true},
		{name: "phone identity", text: "185****0895", want: true},
		{name: "login dialog", text: "185****0895", form: true},
		{name: "login button", account: true, login: true},
		{name: "captcha", account: true, captcha: true},
		{name: "guest warning", text: "未登录", account: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := kimiLoggedEvidence(tc.text, tc.account, tc.form, tc.login, tc.captcha); got != tc.want {
				t.Fatalf("logged = %v, want %v", got, tc.want)
			}
		})
	}
}
