package sms

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

type Message struct {
	Type      string `json:"type"`
	Sender    string `json:"sender"`
	Receiver  string `json:"receiver"`
	RequestID string `json:"requestid"`
	Result    string `json:"result"`
	Time      string `json:"time"`
	Code      string `json:"code,omitempty"`
	Received  string `json:"received_at"`
}

type Inbox struct {
	path string
	mu   sync.Mutex
	all  []Message
}

func New(path string) *Inbox {
	in := &Inbox{path: path}
	in.load()
	return in
}

func (in *Inbox) Add(m Message) Message {
	msg, _ := in.AddIfNew(m)
	return msg
}

// AddIfNew records one provider callback. A non-empty request ID is treated as
// the provider's idempotency key so callback retries cannot submit an OTP twice.
func (in *Inbox) AddIfNew(m Message) (Message, bool) {
	m.Type = strings.TrimSpace(m.Type)
	m.Sender = strings.TrimSpace(m.Sender)
	m.Receiver = strings.TrimSpace(m.Receiver)
	m.RequestID = strings.TrimSpace(m.RequestID)
	m.Result = strings.TrimSpace(m.Result)
	m.Code = ExtractCode(m.Result)
	if m.Received == "" {
		m.Received = time.Now().Format("2006-01-02T15:04:05")
	}
	in.mu.Lock()
	if m.RequestID != "" {
		for i := len(in.all) - 1; i >= 0; i-- {
			if in.all[i].RequestID == m.RequestID {
				existing := in.all[i]
				in.mu.Unlock()
				return existing, false
			}
		}
	}
	in.all = append(in.all, m)
	if len(in.all) > 200 {
		in.all = in.all[len(in.all)-200:]
	}
	in.mu.Unlock()
	in.append(m)
	return m, true
}

func (in *Inbox) Recent(n int) []Message {
	in.mu.Lock()
	defer in.mu.Unlock()
	if n <= 0 || n > len(in.all) {
		n = len(in.all)
	}
	out := make([]Message, n)
	copy(out, in.all[len(in.all)-n:])
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out
}

var (
	codeLabel = regexp.MustCompile(`验证码[:：是为]?\s*(\d{4,8})`)
	codeIs    = regexp.MustCompile(`(\d{4,8})\s*是您的验证码`)
	codeSix   = regexp.MustCompile(`(?:^|[^\d])(\d{6})(?:[^\d]|$)`)
)

func ExtractCode(text string) string {
	if m := codeLabel.FindStringSubmatch(text); len(m) > 1 {
		return m[1]
	}
	if m := codeIs.FindStringSubmatch(text); len(m) > 1 {
		return m[1]
	}
	if m := codeSix.FindStringSubmatch(text); len(m) > 1 {
		return m[1]
	}
	return ""
}

// SiteFromText identifies which GEO site a provider SMS belongs to.
// One phone can wait on several platforms at once; the signature is what
// keeps a Baidu code from being typed into Yuanbao.
func SiteFromText(text string) string {
	switch {
	case strings.Contains(text, "豆包"):
		return "doubao"
	case strings.Contains(text, "Kimi"), strings.Contains(text, "月之暗面"):
		return "kimi"
	case strings.Contains(text, "文心"), strings.Contains(text, "百度"):
		return "yiyan"
	case strings.Contains(text, "元宝"):
		return "yuanbao"
	case strings.Contains(text, "千问"), strings.Contains(text, "通义"):
		return "qianwen"
	case strings.Contains(text, "DeepSeek"), strings.Contains(text, "深度求索"):
		return "deepseek"
	default:
		return ""
	}
}

func PhoneMatch(a, b string) bool {
	da, db := digits(a), digits(b)
	if da == "" || db == "" {
		return false
	}
	if len(da) > 11 {
		da = da[len(da)-11:]
	}
	if len(db) > 11 {
		db = db[len(db)-11:]
	}
	return da == db
}

func digits(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func (in *Inbox) load() {
	raw, err := os.ReadFile(in.path)
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var m Message
		if json.Unmarshal([]byte(line), &m) == nil {
			in.all = append(in.all, m)
		}
	}
	if len(in.all) > 200 {
		in.all = in.all[len(in.all)-200:]
	}
}

func (in *Inbox) append(m Message) {
	if in.path == "" {
		return
	}
	_ = os.MkdirAll(filepath.Dir(in.path), 0o755)
	f, err := os.OpenFile(in.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	raw, _ := json.Marshal(m)
	_, _ = f.Write(append(raw, '\n'))
}
