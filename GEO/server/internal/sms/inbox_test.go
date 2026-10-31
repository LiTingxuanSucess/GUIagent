package sms

import (
	"path/filepath"
	"testing"
)

func TestSiteFromTextUsesSignature(t *testing.T) {
	cases := map[string]string{
		"【百度】验证码：111111":   "yiyan",
		"【腾讯元宝】验证码为：111111": "yuanbao",
		"【豆包】验证码111112":    "doubao",
		"【Kimi】验证码111112":  "kimi",
		"普通通知，没有平台名":         "",
	}
	for text, want := range cases {
		if got := SiteFromText(text); got != want {
			t.Fatalf("SiteFromText(%q) = %q, want %q", text, got, want)
		}
	}
}

func TestExtractCodeAndPhoneMatch(t *testing.T) {
	if got := ExtractCode("【平台】验证码： 111111，5分钟内有效"); got != "111113" {
		t.Fatalf("ExtractCode() = %q", got)
	}
	if !PhoneMatch("+86 138-0013-8000", "13800138000") {
		t.Fatal("expected normalized phone numbers to match")
	}
}

func TestAddIfNewDeduplicatesProviderRequestID(t *testing.T) {
	in := New(filepath.Join(t.TempDir(), "sms.jsonl"))
	first, added := in.AddIfNew(Message{
		Type:      "recieve",
		Sender:    "10690000",
		Receiver:  "13800138000",
		RequestID: " callback-1 ",
		Result:    "验证码是 111111",
	})
	if !added || first.Code != "111111" {
		t.Fatalf("first callback = %#v, added=%v", first, added)
	}

	duplicate, added := in.AddIfNew(Message{
		Type:      "receive",
		Sender:    "10690000",
		Receiver:  "13800138000",
		RequestID: "callback-1",
		Result:    "验证码是 111114",
	})
	if added {
		t.Fatal("duplicate callback was added")
	}
	if duplicate.Code != "111111" {
		t.Fatalf("duplicate returned code %q, want original code", duplicate.Code)
	}
	if got := len(in.Recent(10)); got != 1 {
		t.Fatalf("stored messages = %d, want 1", got)
	}
}

func TestAddIfNewKeepsCallbacksWithoutRequestID(t *testing.T) {
	in := New("")
	for i := 0; i < 2; i++ {
		if _, added := in.AddIfNew(Message{
			Receiver: "13800138000",
			Result:   "验证码 111111",
		}); !added {
			t.Fatal("callbacks without request IDs must not be deduplicated")
		}
	}
	if got := len(in.Recent(10)); got != 2 {
		t.Fatalf("stored messages = %d, want 2", got)
	}
}
