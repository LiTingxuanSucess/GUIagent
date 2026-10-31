package httpserver

import (
	"encoding/json"
	"geo-server/internal/config"
	"geo-server/internal/sessions"
	"geo-server/internal/sms"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestHandoffRestartNeverReplaysAmbiguousSubmissionOrLeaksOTP(t *testing.T) {
	root := t.TempDir()
	s := &Server{cfg: &config.Config{Root: root}}
	s.loadSMSHandoffs()
	key := handoffKey("acct_a", "kimi", 123.25)
	s.smsHandoffs.rows[key] = smsHandoff{AccountID: "acct_a", Site: "kimi", SentAt: 123.25, TargetID: "original-target", Stage: "submitting", UpdatedAt: time.Now()}
	if !s.saveHandoffsLocked() {
		t.Fatal("could not persist")
	}
	reloaded := &Server{cfg: s.cfg}
	reloaded.loadSMSHandoffs()
	stage, _ := reloaded.monitorHandoff("acct_a", "kimi", 123.25)
	if stage != "submission_unknown" || pendingHandoff(stage) {
		t.Fatalf("ambiguous submit replayed: %s", stage)
	}
	accounts := []*sessions.Account{{ID: "acct_a", Phone: "13900001234", Enabled: true, Sites: map[string]sessions.State{"kimi": {Status: "unknown"}}}}
	rows := reloaded.appendInterruptedHandoffActions(nil, accounts)
	if len(rows) != 1 || rows[0].Kind != "manual" || rows[0].HandoffStage != "submission_unknown" {
		t.Fatalf("interrupted handoff disappeared: %+v", rows)
	}
	state := accounts[0].Sites["kimi"]
	state.Status = "logged_in"
	accounts[0].Sites["kimi"] = state
	if rows := reloaded.appendInterruptedHandoffActions(nil, accounts); len(rows) != 0 {
		t.Fatal("resolved login resurfaced as manual work")
	}
	if stage, _ := reloaded.monitorHandoff("acct_a", "kimi", 124.25); stage != "" {
		t.Fatal("new send generation inherited previous state")
	}
	raw, _ := os.ReadFile(s.handoffPath())
	for _, secret := range []string{"验证码", "code", "receiver", "phone"} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("secret in metadata: %s", raw)
		}
	}
}

func TestHandoffCannotUseAnotherPhonePlatformOrUndatedCallback(t *testing.T) {
	root := t.TempDir()
	p := sessions.NewPool("/nonexistent/chrome", root, "", filepath.Join(root, "accounts.json"))
	a, err := p.Add("13900001234")
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{cfg: &config.Config{Root: root}, sess: p, sms: sms.New("")}
	s.loadSMSHandoffs()
	msg := sms.Message{Receiver: "13900005678", Result: "【Kimi】验证码123456", Code: "123456", Received: time.Now().Format("2006-01-02T15:04:05")}
	s.startSMSHandoff(a.ID, "kimi", msg)
	msg.Receiver = "13900001234"
	s.startSMSHandoff(a.ID, "deepseek", msg)
	if len(s.smsHandoffs.rows) != 0 {
		t.Fatal("wrong account/site matched")
	}
	s.startSMSHandoff(a.ID, "kimi", msg)
	stage, _ := s.monitorHandoff(a.ID, "kimi", 0)
	if stage != "send_unconfirmed" || len(s.smsHandoffs.running) != 0 {
		t.Fatalf("unconfirmed generation launched browser: %s", stage)
	}
	before, _ := json.Marshal(s.smsHandoffs.rows)
	s.startSMSHandoff(a.ID, "kimi", msg)
	after, _ := json.Marshal(s.smsHandoffs.rows)
	if string(before) != string(after) {
		t.Fatal("duplicate callback reprocessed")
	}
	stored, _ := os.ReadFile(s.handoffPath())
	if strings.Contains(string(stored), "13900001234") || strings.Contains(string(stored), "123456") {
		t.Fatal("OTP or phone leaked to handoff metadata")
	}
}
