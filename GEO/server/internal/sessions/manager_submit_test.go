package sessions

import "testing"

func TestParseDoubaoLoginError(t *testing.T) {
	message, rejected := parseDoubaoLoginError([]byte(`{
		"data":{"description":"验证码错误，请重新输入","error_code":1202},
		"message":"error"
	}`))
	if !rejected {
		t.Fatal("expected response to be rejected")
	}
	if message != "验证码错误，请重新输入" {
		t.Fatalf("unexpected message: %q", message)
	}
}

func TestParseDoubaoLoginSuccess(t *testing.T) {
	message, rejected := parseDoubaoLoginError([]byte(`{"data":{"error_code":0},"message":"success"}`))
	if rejected || message != "" {
		t.Fatalf("unexpected rejection: rejected=%v message=%q", rejected, message)
	}
}

func TestParseDoubaoBoundAccountsContinuesLogin(t *testing.T) {
	message, rejected := parseDoubaoLoginError([]byte(`{
		"data":{
			"description":"+86 186******06 已绑定了以下账号",
			"error_code":1108
		},
		"message":"error"
	}`))
	if rejected || message != "" {
		t.Fatalf("account picker must continue: rejected=%v message=%q", rejected, message)
	}
}
