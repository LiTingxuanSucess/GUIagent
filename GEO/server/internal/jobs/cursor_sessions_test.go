package jobs

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCursorSessionRequiresVerificationAndScopedResume(t *testing.T) {
	workspace := t.TempDir()
	const sid = "68aa0ec4-72da-4847-add1-3c3176ff76b0"
	out := &cursorOutput{}
	_, _ = out.Write([]byte(`{"type":"system","subtype":"init","session_`))
	_, _ = out.Write([]byte(`id":"` + sid + `"}` + "\n" + strings.Repeat("x", 100000) + "\n"))
	if out.session() != sid || len(out.stream) != 0 {
		t.Fatal("lost session metadata")
	}
	call := cursorCall{ID: "call", AccountID: "account", Site: "doubao", TaskType: "manual_page_assist", Status: "candidate", SessionID: out.session()}
	if err := call.save(workspace); err != nil {
		t.Fatal(err)
	}
	next := call
	next.ResumedSessionID = ""
	if strings.Contains(strings.Join(cursorArgsForCall(workspace, "new target", &next), " "), "--resume") {
		t.Fatal("unverified session reused")
	}
	verified := true
	call.Status, call.Verified = "success", &verified
	if err := call.save(workspace); err != nil {
		t.Fatal(err)
	}
	args := cursorArgsForCall(workspace, "new target", &next)
	if next.ResumedSessionID != sid || !strings.Contains(strings.Join(args, " "), "--resume "+sid) || !strings.Contains(args[len(args)-1], "new target") {
		t.Fatal("session not resumed with fresh context")
	}
	for _, mutate := range []func(*cursorCall){func(c *cursorCall) { c.AccountID = "other" }, func(c *cursorCall) { c.Site = "kimi" }, func(c *cursorCall) { c.TaskType = "web_recovery" }} {
		other := call
		other.ResumedSessionID = ""
		mutate(&other)
		cursorArgsForCall(workspace, "task", &other)
		if other.ResumedSessionID != "" {
			t.Fatal("session crossed account/site/task boundary")
		}
	}
	info, _ := os.Stat(cursorSessionPath(workspace, call))
	if info.Mode().Perm() != 0600 {
		t.Fatal("session file is public")
	}
	next.Status, next.ErrorCode = "failed", "cli_canceled"
	if err := next.save(workspace); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(cursorSessionPath(workspace, call)); err != nil {
		t.Fatal("caller cancellation evicted a previously verified session")
	}
	next.ErrorCode = "cli_verification_failed"
	if err := next.save(workspace); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(cursorSessionPath(workspace, call)); !os.IsNotExist(err) {
		t.Fatal("failed resume remained reusable")
	}
	if matches, _ := filepath.Glob(filepath.Join(filepath.Dir(cursorSessionPath(workspace, call)), ".session-*")); len(matches) != 0 {
		t.Fatal("temporary files leaked")
	}
}

func TestCursorSessionReadsFinalEventWithoutNewline(t *testing.T) {
	const sid = "68aa0ec4-72da-4847-add1-3c3176ff76b0"
	for _, tc := range []struct{ event, want string }{
		{`{"type":"result","session_id":"` + sid + `"}`, sid},
		{`{"type":"system","subtype":"init","session_id":"` + sid + `"}`, sid},
		{`{"type":"assistant","session_id":"` + sid + `"}`, ""},
		{`{"type":"result","session_id":"invalid"}`, ""},
	} {
		out := &cursorOutput{}
		_, _ = out.Write([]byte(tc.event))
		if got := out.session(); got != tc.want {
			t.Fatalf("session=%q want=%q for %s", got, tc.want, tc.event)
		}
	}
}

func TestCursorSessionRecoveryReusesVerifiedSessionWithNewTarget(t *testing.T) {
	const sid = "68aa0ec4-72da-4847-add1-3c3176ff76b0"
	script := "for arg do printf '%s\\n' \"$arg\"; done > invocation\n" +
		"printf '%s' '{\"type\":\"result\",\"session_id\":\"" + sid + "\",\"result\":\"recovery-candidate\",\"is_error\":false}'\n"
	q, req := cursorRecoveryFixture(t, script)
	first := q.runCursorRecovery(context.Background(), req)
	if first.Status != "candidate" {
		t.Fatalf("first recovery failed: %+v", first)
	}
	q.markCursorRecovery(first.CallID, true, false, "verify")
	req.JobID, req.TargetID = "next-job", "next-target"
	second := q.runCursorRecovery(context.Background(), req)
	call := readRecoveryCall(t, q, second.CallID)
	invocation, err := os.ReadFile(filepath.Join(q.GeoRoot, "invocation"))
	if err != nil || second.Status != "candidate" || call.ResumedSessionID != sid || call.SessionID != sid ||
		!strings.Contains(string(invocation), "--resume\n"+sid+"\n") || !strings.Contains(string(invocation), "next-target") {
		t.Fatalf("verified session was not resumed for the fresh target: result=%+v call=%+v err=%v", second, call, err)
	}
}
