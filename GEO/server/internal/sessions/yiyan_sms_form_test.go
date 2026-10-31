package sessions

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/chromedp/chromedp"
)

func TestYiyanTransparentSMSInputRequiresVisibleSixDigitWidget(t *testing.T) {
	if _, err := os.Stat("/usr/bin/google-chrome"); err != nil {
		t.Skip("Chrome is required for the real SMS DOM fixture")
	}
	opts := append(chromedp.DefaultExecAllocatorOptions[:], chromedp.ExecPath("/usr/bin/google-chrome"),
		chromedp.NoSandbox, chromedp.Flag("disable-dev-shm-usage", true))
	allocator, closeAllocator := chromedp.NewExecAllocator(context.Background(), opts...)
	defer closeAllocator()
	ctx, closeBrowser := chromedp.NewContext(allocator)
	defer closeBrowser()
	ctx, cancel := context.WithTimeout(ctx, 25*time.Second)
	defer cancel()
	if err := chromedp.Run(ctx, chromedp.Navigate("about:blank")); err != nil {
		t.Fatal(err)
	}
	widget := `<article id="sms_verify_new"><div id="pass-sms-keyboardInputWrapper" style="width:330px;height:44px;position:relative">
	<input id="sms_verify_new-keyboardInputHidden" type="tel" placeholder="请输入验证码" style="position:absolute;width:330px;height:44px;opacity:0">
	<span style="display:inline-block;width:40px;height:44px"></span><span style="display:inline-block;width:40px;height:44px"></span>
	<span style="display:inline-block;width:40px;height:44px"></span><span style="display:inline-block;width:40px;height:44px"></span>
	<span style="display:inline-block;width:40px;height:44px"></span><span style="display:inline-block;width:40px;height:44px"></span>
	</div></article>`
	for _, tt := range []struct {
		name, html string
		want       bool
	}{
		{"transparent_input_visible_cells", widget, true},
		{"hidden_widget", `<div style="display:none">` + widget + `</div>`, false},
		{"unrelated_hidden_input", `<input id="sms_verify_new-keyboardInputHidden" placeholder="请输入验证码" style="opacity:0">`, false},
		{"ordinary_visible_code_box", `<input placeholder="短信验证码">`, true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			encoded, _ := json.Marshal(tt.html)
			if err := chromedp.Run(ctx, chromedp.Evaluate("document.body.innerHTML="+string(encoded), nil)); err != nil {
				t.Fatal(err)
			}
			if got := hasCodeForm(ctx); got != tt.want {
				t.Fatalf("hasCodeForm = %v, want %v", got, tt.want)
			}
		})
	}
}
