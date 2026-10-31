package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

const cursorFixtureBoot = "11111111-1111-4111-8111-111111111111"

func cursorProcFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	path := filepath.Join(root, "sys/kernel/random/boot_id")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(cursorFixtureBoot+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	return root
}

func writeCursorProc(t *testing.T, root string, pid, parent, group int, start, state string) {
	t.Helper()
	fields := make([]string, 20)
	for i := range fields {
		fields[i] = "0"
	}
	fields[0], fields[1], fields[2], fields[19] = state, strconv.Itoa(parent), strconv.Itoa(group), start
	path := filepath.Join(root, strconv.Itoa(pid), "stat")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	// Parentheses in comm must not shift the Linux stat field offsets.
	if err := os.WriteFile(path, []byte(fmt.Sprintf("%d (fixture (worker)) %s\n", pid, strings.Join(fields, " "))), 0600); err != nil {
		t.Fatal(err)
	}
}

func fingerprintCursorCall() cursorCall {
	return cursorCall{ID: "fingerprint-fixture", JobID: "queued", Site: "doubao", AccountID: "fixture",
		TaskType: "web_recovery", Status: "running", Phase: "running", StartedAt: time.Now().Add(-time.Minute).UTC(),
		OwnerPID: 501, OwnerStart: "111", ProcessPID: 502, ProcessStart: "112", ProcessGroup: 502, BootID: cursorFixtureBoot}
}

func TestCursorProcessBindingRequiresExactOwnerBootAndProcessGroup(t *testing.T) {
	for _, change := range []string{"valid", "parent", "group", "owner_start", "boot", "missing_process"} {
		t.Run(change, func(t *testing.T) {
			proc := cursorProcFixture(t)
			writeCursorProc(t, proc, 501, 1, 501, "111", "S")
			writeCursorProc(t, proc, 502, 501, 502, "112", "S")
			call := fingerprintCursorCall()
			call.ProcessPID, call.ProcessStart, call.ProcessGroup = 0, "", 0
			switch change {
			case "parent":
				writeCursorProc(t, proc, 502, 999, 502, "112", "S")
			case "group":
				writeCursorProc(t, proc, 502, 501, 999, "112", "S")
			case "owner_start":
				call.OwnerStart = "110"
			case "boot":
				call.BootID = "22222222-2222-4222-8222-222222222222"
			case "missing_process":
				if err := os.RemoveAll(filepath.Join(proc, "502")); err != nil {
					t.Fatal(err)
				}
			}
			call.bindProcessAt(proc, 501, 502)
			if call.ProcessPID != 502 {
				t.Fatal("observed launch lost its PID even when fingerprinting was unavailable")
			}
			if change == "valid" {
				if call.ProcessStart != "112" || call.ProcessGroup != 502 || call.invocationState(proc) != cursorProcessAlive {
					t.Fatal("owned invocation was not bound to its original process")
				}
			} else if call.ProcessStart != "" || call.ProcessGroup != 0 || call.invocationState(proc) != cursorProcessUnknown {
				t.Fatal("unconfirmed ownership was promoted to a complete invocation fingerprint")
			}
		})
	}
}

func TestCursorInvocationEvidenceDistinguishesDeadAliveAndUncertain(t *testing.T) {
	for _, tc := range []struct {
		name string
		want cursorProcessState
	}{
		{"missing_both", cursorProcessDead}, {"owner_alive", cursorProcessAlive},
		{"cli_alive_owner_dead", cursorProcessAlive}, {"orphan_group_alive", cursorProcessAlive},
		{"zombie_leader_live_group", cursorProcessAlive}, {"both_zombie", cursorProcessDead},
		{"owner_pid_reused", cursorProcessDead}, {"cli_pid_reused_other_group", cursorProcessDead},
		{"cli_pid_reused_live_group", cursorProcessAlive}, {"boot_changed", cursorProcessDead},
		{"unknown_owner_stat", cursorProcessUnknown}, {"unknown_group_member", cursorProcessUnknown},
		{"missing_boot", cursorProcessUnknown}, {"invalid_boot", cursorProcessUnknown},
		{"invalid_stored_start", cursorProcessUnknown}, {"partial_binding", cursorProcessUnknown},
	} {
		t.Run(tc.name, func(t *testing.T) {
			proc, call := cursorProcFixture(t), fingerprintCursorCall()
			switch tc.name {
			case "owner_alive":
				writeCursorProc(t, proc, 501, 1, 501, "111", "S")
			case "cli_alive_owner_dead":
				writeCursorProc(t, proc, 502, 1, 502, "112", "S")
			case "orphan_group_alive", "zombie_leader_live_group":
				writeCursorProc(t, proc, 503, 1, 502, "113", "S")
				if tc.name == "zombie_leader_live_group" {
					writeCursorProc(t, proc, 502, 1, 502, "112", "Z")
				}
			case "both_zombie":
				writeCursorProc(t, proc, 501, 1, 501, "111", "Z")
				writeCursorProc(t, proc, 502, 1, 502, "112", "Z")
			case "owner_pid_reused":
				writeCursorProc(t, proc, 501, 1, 501, "211", "S")
			case "cli_pid_reused_other_group":
				writeCursorProc(t, proc, 502, 1, 900, "212", "S")
			case "cli_pid_reused_live_group":
				writeCursorProc(t, proc, 502, 1, 502, "212", "S")
			case "boot_changed":
				call.BootID = "22222222-2222-4222-8222-222222222222"
			case "unknown_owner_stat", "unknown_group_member":
				pid := "501"
				if tc.name == "unknown_group_member" {
					pid = "599"
				}
				if err := os.MkdirAll(filepath.Join(proc, pid, "stat"), 0700); err != nil {
					t.Fatal(err)
				}
			case "missing_boot":
				if err := os.Remove(filepath.Join(proc, "sys/kernel/random/boot_id")); err != nil {
					t.Fatal(err)
				}
			case "invalid_boot":
				call.BootID = "malformed"
			case "invalid_stored_start":
				call.OwnerStart = "not-a-start-time"
			case "partial_binding":
				call.ProcessStart = ""
			}
			if got := call.invocationState(proc); got != tc.want {
				t.Fatalf("invocation evidence state=%d want=%d", got, tc.want)
			}
		})
	}
}

func TestCursorReconcileProvenDeadAdoptedInvocationDoesNotChangeJobs(t *testing.T) {
	q, _ := cursorRecoveryFixture(t, "printf 'recovery-candidate\\n'\n")
	q.jobs = map[string]*Job{
		"queued": {ID: "queued", Status: "queued", DeliveryPolicy: "eventual", Results: []map[string]any{{"site_id": "kimi", "ok": true, "answer": "preserved answer"}}},
		"active": {ID: "active", Status: "running", DeliveryPolicy: "eventual"},
		"failed": {ID: "failed", Status: "failed", ErrorCode: "service_interrupted"},
	}
	jobsBefore, _ := json.Marshal(q.jobs)
	proc := cursorProcFixture(t)
	writeCursorProc(t, proc, 601, 1, 601, "212", "S")
	before := map[string][]byte{}
	for _, id := range []string{"dead_queued", "dead_running", "dead_captcha", "verified", "live", "legacy", "partial", "known_failure", "completed"} {
		call := fingerprintCursorCall()
		call.ID = id
		switch id {
		case "dead_running":
			call.JobID = "active"
		case "dead_captcha":
			call.TaskType, call.Phase = "captcha_recovery", ""
		case "verified":
			call.Status, call.Phase, call.Verified = "success", "verify", cursorBool(true)
			finished := call.StartedAt.Add(500 * time.Millisecond)
			call.FinishedAt, call.ElapsedMS = &finished, 500
		case "live":
			call.ProcessPID, call.ProcessStart, call.ProcessGroup = 601, "212", 601
		case "legacy":
			call.OwnerPID, call.OwnerStart, call.ProcessPID, call.ProcessStart, call.ProcessGroup, call.BootID = 0, "", 0, "", 0, ""
		case "partial":
			call.JobID, call.ProcessStart = "failed", ""
		case "known_failure":
			call.Status, call.ErrorCode = "failed", "cli_timeout"
		case "completed":
			call.Phase = "completed"
		}
		if err := call.save(q.cursorWorkspace()); err != nil {
			t.Fatal(err)
		}
		before[id], _ = os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), id+".json"))
	}
	q.reconcileCursorRecoveriesAt(proc)
	for _, id := range []string{"dead_queued", "dead_running", "dead_captcha"} {
		call := readRecoveryCall(t, q, id)
		if call.Status != "failed" || call.OK == nil || *call.OK || call.Phase != "completed" || call.ErrorCode != "service_interrupted" || call.FinishedAt == nil || call.ElapsedMS != 0 {
			t.Fatal("proven dead invocation remained running or acquired guessed success/duration")
		}
	}
	verified := readRecoveryCall(t, q, "verified")
	if verified.Status != "success" || verified.OK == nil || !*verified.OK || verified.ResumeOK == nil || *verified.ResumeOK || verified.ErrorCode != "cli_resume_interrupted" || verified.ElapsedMS != 500 {
		t.Fatal("reconciliation erased independently verified CLI success or invented resume success")
	}
	for _, id := range []string{"live", "legacy", "partial", "known_failure", "completed"} {
		raw, _ := os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), id+".json"))
		if string(raw) != string(before[id]) {
			t.Fatalf("reconciliation changed live, unbound, uncertain or finished call %s", id)
		}
	}
	jobsAfter, _ := json.Marshal(q.jobs)
	if string(jobsBefore) != string(jobsAfter) {
		t.Fatal("CLI reconciliation changed an adopted job or its saved answer")
	}
	first, _ := os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), "dead_queued.json"))
	q.reconcileCursorRecoveriesAt(proc)
	second, _ := os.ReadFile(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), "dead_queued.json"))
	if string(first) != string(second) {
		t.Fatal("repeated startup changed a finalized invocation")
	}
}

func TestFakeCursorLaunchPersistsActualInvocationFingerprint(t *testing.T) {
	for _, task := range []string{"web", "captcha"} {
		t.Run(task, func(t *testing.T) {
			q, req := cursorRecoveryFixture(t, "printf 'recovery-candidate\\ncaptcha-ok\\n'\n")
			var id string
			if task == "web" {
				id = q.runCursorRecovery(context.Background(), req).CallID
			} else {
				if err := os.MkdirAll(filepath.Join(q.GeoRoot, "server"), 0700); err != nil {
					t.Fatal(err)
				}
				runCursorCaptchaAgent(req.AccountID, req.Phone, req.Site, req.JobID)
				paths, _ := filepath.Glob(filepath.Join(cursorCallDirectory(q.cursorWorkspace()), "*.json"))
				if len(paths) != 1 {
					t.Fatal("fake launch did not produce one invocation record")
				}
				id = strings.TrimSuffix(filepath.Base(paths[0]), ".json")
			}
			call := readRecoveryCall(t, q, id)
			if call.OwnerPID != os.Getpid() || call.OwnerStart == "" || call.ProcessPID <= 0 || call.ProcessStart == "" || call.ProcessGroup != call.ProcessPID || !cursorValidBootID(call.BootID) {
				t.Fatal("actual fake subprocess was not bound to its exact Go owner and invocation")
			}
		})
	}
}
