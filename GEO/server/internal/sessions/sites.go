package sessions

type Site struct {
	ID    string
	Name  string
	Home  string
	Login string
	Open  []string
}

var Sites = []Site{
	{ID: "deepseek", Name: "DeepSeek", Home: "https://chat.deepseek.com/", Login: "https://chat.deepseek.com/sign_in"},
	{ID: "doubao", Name: "豆包", Home: "https://www.doubao.com/chat/", Login: "https://www.doubao.com/chat/", Open: []string{"登录", "手机号登录"}},
	{ID: "yiyan", Name: "文心一言", Home: "https://yiyan.baidu.com/", Login: "https://wappass.baidu.com/passport/login?sms=1&u=https%3A%2F%2Fwenxin.baidu.com%2F", Open: []string{"同意并继续", "短信登录", "手机号登录"}},
	{ID: "yuanbao", Name: "腾讯元宝", Home: "https://yuanbao.tencent.com/chat", Login: "https://yuanbao.tencent.com/chat", Open: []string{"登录", "手机", "手机号登录"}},
	{ID: "qianwen", Name: "通义千问", Home: "https://www.qianwen.com/", Login: "https://passport.qianwen.com/havanaone/login/login.htm?bizEntrance=tongyi&bizName=tongyi&redirectType=topRedirect&lang=zh_CN&returnUrl=https%3A%2F%2Fwww.qianwen.com%2F", Open: []string{"登录", "手机号登录", "短信登录"}},
	{ID: "kimi", Name: "Kimi", Home: "https://www.kimi.com/", Login: "https://www.kimi.com/", Open: []string{"登录以同步历史会话", "手机号快捷登录", "登录"}},
}

func SiteByID(id string) (Site, bool) {
	for _, s := range Sites {
		if s.ID == id {
			return s, true
		}
	}
	return Site{}, false
}
