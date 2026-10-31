package jobs

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func cursorRecoveryFixture(t *testing.T, script string) (*Queue, cursorRecoveryRequest) {
	t.Helper()
	root := t.TempDir()
	t.Setenv("GEO_CURSOR_WORKSPACE", root)
	t.Setenv("GEO_DATA_DIR", filepath.Join(root, "private-data"))
	t.Setenv("GEO_CURSOR_WEB_RECOVERY", "1")
	t.Setenv("GEO_CURSOR_RECOVERY_TIMEOUT_SEC", "10")
	t.Setenv("GEO_CURSOR_STARTUP_SEC", "")
	bin := filepath.Join(root, "fixture-agent")
	t.Setenv("GEO_CURSOR_AGENT", bin)
	if err := os.WriteFile(bin, []byte("#!/bin/sh\n"+script), 0700); err != nil {
		t.Fatal(err)
	}
	for _, site := range []string{"deepseek", "doubao", "kimi", "yuanbao", "qianwen", "yiyan"} {
		for _, path := range []string{filepath.Join(root, ".cursor", "skills", "geo-"+site+"-web-recovery", "SKILL.md"), filepath.Join(root, "docs", cursorRecoverySOP(site))} {
			if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, []byte("fixture instructions"), 0600); err != nil {
				t.Fatal(err)
			}
		}
	}
	return &Queue{GeoRoot: root}, cursorRecoveryRequest{AccountID: "acct_fixture", Phone: "13800138000", Site: "doubao", JobID: "job_fixture", CDP: "http://127.0.0.1:12345", TargetID: "target_fixture", StatePath: filepath.Join(root, "private-journal.json"), Reason: "captcha"}
}

func readRecoveryCall(t *testing.T, q *Queue, id string) cursorCall {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), id+".json"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "13800138000") || strings.Contains(string(raw), "private CLI output") || strings.Contains(string(raw), "127.0.0.1") || strings.Contains(string(raw), "private-journal") {
		t.Fatal("private operating context leaked into the public event record")
	}
	var call cursorCall
	if err := json.Unmarshal(raw, &call); err != nil {
		t.Fatal(err)
	}
	return call
}

func TestCursorRecoverySixPlatformsRequireIndependentVerification(t *testing.T) {
	q, req := cursorRecoveryFixture(t, "for arg do printf '%s\\n' \"$arg\"; done > invocation\nprintf 'private CLI output\\nrecovery-candidate\\n'\n")
	for _, site := range []string{"deepseek", "doubao", "kimi", "yuanbao", "qianwen", "wenxinyiyan"} {
		req.Site = site
		result := q.runCursorRecovery(context.Background(), req)
		call := readRecoveryCall(t, q, result.CallID)
		if result.Status != "candidate" || call.Status != "unknown" || call.Phase != "candidate" || call.OK != nil || call.Verified != nil || call.ResumeOK != nil || call.TaskType != "web_recovery" || call.Site != internalSiteID(site) {
			t.Fatalf("CLI marker bypassed independent verification: result=%v call=%+v", result, call)
		}
		prompt, err := os.ReadFile(filepath.Join(q.GeoRoot, "invocation"))
		if err != nil || !strings.Contains(string(prompt), "geo-"+internalSiteID(site)+"-web-recovery") || !strings.Contains(string(prompt), cursorRecoverySOP(internalSiteID(site))) || !strings.Contains(string(prompt), req.TargetID) || !strings.Contains(string(prompt), req.CDP) {
			t.Fatalf("runner lost exact target or platform instructions: site=%s err=%v", site, err)
		}
		q.markCursorRecovery(result.CallID, true, false, "verify")
		call = readRecoveryCall(t, q, result.CallID)
		if call.Status != "success" || call.OK == nil || !*call.OK || call.Verified == nil || !*call.Verified || call.ResumeOK != nil {
			t.Fatalf("verification incorrectly claimed resume or lost its outcome: %+v", call)
		}
		q.markCursorRecovery(result.CallID, true, false, "completed")
		call = readRecoveryCall(t, q, result.CallID)
		if call.Status != "success" || call.OK == nil || !*call.OK || call.ResumeOK == nil || *call.ResumeOK || call.ErrorCode != "cli_resume_failed" {
			t.Fatalf("resume failure erased independent CLI recovery success: %+v", call)
		}
		logs, _ := filepath.Glob(filepath.Join(filepath.Dir(cursorCallDirectory(q.GeoRoot)), "cursor-logs", result.CallID+".log"))
		if len(logs) != 1 {
			t.Fatal("CLI output did not go to its private per-call log")
		}
		info, _ := os.Stat(logs[0])
		if info.Mode().Perm() != 0600 {
			t.Fatal("CLI log is not private")
		}
	}
}

func TestCursorRecoveryReadsStreamJSONResult(t *testing.T) {
	script := "for arg do printf '%s\\n' \"$arg\"; done > invocation\n" +
		"printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\"}'\n" +
		"printf '%s\\n' '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"recovery-candidate\"}]}}'\n" +
		"printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"result\":\"recovery-stopped: sms_required\",\"is_error\":false}'\n"
	q, req := cursorRecoveryFixture(t, script)
	result := q.runCursorRecovery(context.Background(), req)
	if result.Status != "waiting_sms" || result.ErrorCode != "sms_required" {
		t.Fatalf("assistant text overrode the final result event: %+v", result)
	}
	invocation, err := os.ReadFile(filepath.Join(q.GeoRoot, "invocation"))
	if err != nil || !strings.Contains(string(invocation), "stream-json") {
		t.Fatalf("CLI must stream progress so the silence watchdog sees it: %v", err)
	}
}

func TestCursorFinalText(t *testing.T) {
	cases := []struct {
		name, tail, text string
		failed           bool
	}{
		{"result event", "{\"type\":\"thinking\"}\n{\"type\":\"result\",\"result\":\"recovery-candidate\",\"is_error\":false}\n", "recovery-candidate", false},
		{"error event", "{\"type\":\"result\",\"result\":\"\",\"is_error\":true}\n", "", true},
		{"plain text", "private output\nrecovery-candidate\n", "private output\nrecovery-candidate\n", false},
		{"no result yet", "{\"type\":\"assistant\",\"message\":{}}\n", "{\"type\":\"assistant\",\"message\":{}}\n", false},
	}
	for _, c := range cases {
		text, failed, fromResult := cursorFinalText([]byte(c.tail))
		wantResult := c.name == "result event" || c.name == "error event"
		if text != c.text || failed != c.failed || fromResult != wantResult {
			t.Fatalf("%s: text=%q failed=%v fromResult=%v", c.name, text, failed, fromResult)
		}
	}
	out := &cursorOutput{tail: []byte("{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"recovery-candidate\"}]}}\n")}
	if status, _, _ := out.recoveryOutcome(nil); status == "candidate" {
		t.Fatal("a marker quoted before the result event must not count as a candidate")
	}
	inline := &cursorOutput{tail: []byte("{\"type\":\"result\",\"result\":\"页面写明禁言至明天。recovery-stopped: platform_quota\",\"is_error\":false}\n")}
	if status, code, _ := inline.recoveryOutcome(nil); status != "failed" || code != "platform_quota" {
		t.Fatalf("inline stop marker: status=%s code=%s", status, code)
	}
}

func TestCursorRecoveryCancellationKillsProcessGroupAndReleasesAccount(t *testing.T) {
	q, req := cursorRecoveryFixture(t, "sleep 30 &\nchild=$!\nprintf '%s' \"$child\" > child-pid\nwait\n")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan cursorRecoveryResult, 1)
	go func() { done <- q.runCursorRecovery(ctx, req) }()
	var child int
	until := time.Now().Add(time.Second)
	for time.Now().Before(until) {
		raw, _ := os.ReadFile(filepath.Join(q.GeoRoot, "child-pid"))
		child, _ = strconv.Atoi(string(raw))
		if child > 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if child == 0 {
		cancel()
		<-done
		t.Fatal("mock CLI did not start its child")
	}
	busyCtx, cancelBusy := context.WithTimeout(context.Background(), 25*time.Millisecond)
	busy := q.runCursorRecovery(busyCtx, req)
	cancelBusy()
	if busy.Status != "skipped" || busy.ErrorCode != "cli_canceled" {
		t.Fatalf("queued recovery shared the account or ignored cancellation: %v", busy)
	}
	cancel()
	select {
	case result := <-done:
		if result.Status != "failed" || result.ErrorCode != "cli_canceled" {
			t.Fatalf("canceled CLI result=%v", result)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("canceled recovery left the CLI or child holding stdout")
	}
	stopped := false
	until = time.Now().Add(time.Second)
	for time.Now().Before(until) {
		if err := syscall.Kill(child, 0); err != nil {
			stopped = true
			break
		}
		stat, _ := os.ReadFile(filepath.Join("/proc", strconv.Itoa(child), "stat"))
		fields := strings.Fields(string(stat))
		if len(fields) >= 3 && fields[2] == "Z" {
			stopped = true
			break
		}
		time.Sleep(time.Millisecond)
	}
	if !stopped {
		_ = syscall.Kill(child, syscall.SIGKILL)
		t.Fatal("cancellation left an active CLI descendant")
	}
	if !claimCaptchaAgent(req.AccountID) {
		t.Fatal("canceled CLI retained its account mutex")
	}
	releaseCaptchaAgent(req.AccountID)
}

func TestCursorRecoveryWaitsForAccountAndPublishesCallIDBeforeLaunch(t *testing.T) {
	q, req := cursorRecoveryFixture(t, "touch invoked\nprintf 'recovery-candidate\\n'\n")
	q.jobs = map[string]*Job{req.JobID: {ID: req.JobID, Status: "running"}}
	if !claimCaptchaAgent(req.AccountID) {
		t.Fatal("could not occupy mock CLI account")
	}
	defer releaseCaptchaAgent(req.AccountID)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	done := make(chan cursorRecoveryResult, 1)
	go func() { done <- q.runCursorRecovery(ctx, req) }()
	var callID string
	until := time.Now().Add(time.Second)
	for time.Now().Before(until) {
		job, _ := q.Get(req.JobID)
		if state := job.RecoveryStates["doubao"]; state != nil && state["phase"] == "waiting_cli" {
			callID = stringField(state, "cli_call_id")
			break
		}
		time.Sleep(time.Millisecond)
	}
	if callID == "" {
		t.Error("waiting CLI was not correlated with its call ID")
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "invoked")); !os.IsNotExist(err) {
		t.Error("second CLI launched while the account was occupied")
	}
	select {
	case early := <-done:
		t.Fatalf("CLI contention skipped recovery instead of waiting: %v", early)
	default:
	}
	releaseCaptchaAgent(req.AccountID)
	select {
	case result := <-done:
		if result.Status != "candidate" || result.CallID != callID {
			t.Fatalf("queued recovery did not resume with the same call ID: %v", result)
		}
	case <-ctx.Done():
		t.Fatal("CLI account wait did not resume after release")
	}
}

func TestRecoveryStopCodeKeepsTheSOPReason(t *testing.T) {
	for _, tc := range []struct{ line, code string }{
		{"recovery-stopped: sms_required", "sms_required"},
		{"recovery-stopped: no_progress恢复已停止：CDP 无数据", "no_progress"},
		{"recovery-stopped: target_missing", "target_missing"},
		{"captcha-stopped: 页面已关闭", "cli_task_stopped"},
		{"recovery-stopped: 页面异常", "cli_task_stopped"},
	} {
		if got := recoveryStopCode(tc.line); got != tc.code {
			t.Fatalf("%q => %s, want %s", tc.line, got, tc.code)
		}
	}
}

func TestCursorRecoverySilenceStopsEmptyProcess(t *testing.T) {
	q, req := cursorRecoveryFixture(t, "sleep 30\n")
	t.Setenv("GEO_CURSOR_RECOVERY_TIMEOUT_SEC", "30")
	t.Setenv("GEO_CURSOR_SILENCE_SEC", "1")
	started := time.Now()
	result := q.runCursorRecovery(context.Background(), req)
	if result.Status != "failed" || result.ErrorCode != "cli_no_output" || time.Since(started) > 8*time.Second {
		t.Fatalf("silent CLI was not stopped: %+v elapsed=%s", result, time.Since(started))
	}
}

func TestCursorRecoveryTimeoutAndUnverifiedExitAreNotSuccess(t *testing.T) {
	for _, tc := range []struct{ name, script, code string }{
		{"timeout", "sleep 30\n", "cli_timeout"},
		{"exit_zero", "printf 'finished without a marker\\n'\n", "cli_result_unconfirmed"},
		{"stopped", "printf 'recovery-stopped: sms_required\\n'\n", "sms_required"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			q, req := cursorRecoveryFixture(t, tc.script)
			t.Setenv("GEO_CURSOR_RECOVERY_TIMEOUT_SEC", "1")
			result := q.runCursorRecovery(context.Background(), req)
			call := readRecoveryCall(t, q, result.CallID)
			if result.ErrorCode != tc.code || call.Verified != nil || call.ResumeOK != nil || call.OK != nil && *call.OK {
				t.Fatalf("unverified CLI became successful: result=%v call=%+v", result, call)
			}
		})
	}
}

func TestSkippedCursorRecoveryCannotBecomeAnExecutedVerificationFailure(t *testing.T) {
	q, req := cursorRecoveryFixture(t, "printf 'recovery-candidate\\n'\n")
	req.TargetID = ""
	result := q.runCursorRecovery(context.Background(), req)
	if result.Status != "skipped" || result.ErrorCode != "cli_context_missing" {
		t.Fatalf("missing exact target started CLI: %v", result)
	}
	q.markCursorRecovery(result.CallID, false, false, "completed")
	call := readRecoveryCall(t, q, result.CallID)
	if call.Status != "skipped" || call.ErrorCode != "cli_context_missing" || call.Verified != nil || call.ResumeOK != nil || call.OK != nil {
		t.Fatalf("a post-CLI probe fabricated an executed failure for a skipped call: %+v", call)
	}
}

func TestCursorRecoveryStartupReconcilesOnlyInterruptedUnfinishedRecoveries(t *testing.T) {
	q, _ := cursorRecoveryFixture(t, "printf 'recovery-candidate\\n'\n")
	q.jobs = map[string]*Job{
		"interrupted": {ID: "interrupted", Status: "failed", ErrorCode: "service_interrupted"},
		"completed":   {ID: "completed", Status: "succeeded"},
	}
	started := time.Now().Add(-time.Minute).UTC()
	finished := started.Add(500 * time.Millisecond)
	before := map[string][]byte{}
	for _, tc := range []struct {
		id, job, phase, task string
		verified             *bool
		finished             bool
	}{
		{"running", "interrupted", "running", "web_recovery", nil, false},
		{"candidate", "interrupted", "candidate", "web_recovery", nil, true},
		{"verified", "interrupted", "verify", "web_recovery", cursorBool(true), true},
		{"finished_history", "interrupted", "completed", "web_recovery", cursorBool(true), true},
		{"manual_history", "interrupted", "running", "captcha_recovery", nil, false},
		{"completed_owner", "completed", "candidate", "web_recovery", nil, true},
		{"missing_owner", "missing", "running", "web_recovery", nil, false},
	} {
		call := newCursorCall("acct_fixture", "doubao", tc.job)
		call.ID, call.TaskType, call.Phase, call.Verified = tc.id, tc.task, tc.phase, tc.verified
		call.StartedAt = started
		if tc.finished {
			call.FinishedAt, call.ElapsedMS = &finished, 500
		}
		if err := call.save(q.cursorWorkspace()); err != nil {
			t.Fatal(err)
		}
		before[tc.id], _ = os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), tc.id+".json"))
	}
	q.reconcileCursorRecoveries()
	for _, id := range []string{"running", "candidate", "verified"} {
		call := readRecoveryCall(t, q, id)
		if call.Phase != "completed" || call.FinishedAt == nil || call.OK == nil {
			t.Fatalf("interrupted recovery remained active: %+v", call)
		}
		if id == "verified" {
			if call.Status != "success" || !*call.OK || call.ResumeOK == nil || *call.ResumeOK || call.ErrorCode != "cli_resume_interrupted" || call.ElapsedMS != 500 {
				t.Fatalf("restart erased verified CLI success or invented resume success: %+v", call)
			}
		} else if call.Status != "failed" || *call.OK || call.ErrorCode != "service_interrupted" || id == "running" && call.ElapsedMS != 0 || id == "candidate" && call.ElapsedMS != 500 {
			t.Fatalf("restart lost interruption evidence or invented CLI elapsed time: %+v", call)
		}
	}
	for _, id := range []string{"finished_history", "manual_history", "completed_owner", "missing_owner"} {
		raw, _ := os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), id+".json"))
		if string(raw) != string(before[id]) {
			t.Fatalf("restart rewrote unrelated or completed history: %s", id)
		}
	}
	first, _ := os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), "verified.json"))
	q.reconcileCursorRecoveries()
	second, _ := os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), "verified.json"))
	if string(first) != string(second) {
		t.Fatal("another restart rewrote an already reconciled recovery")
	}
}
