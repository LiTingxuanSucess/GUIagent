package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"geo-server/internal/sessions"
)

type manualOpenPool struct {
	*sessions.Pool
	page     sessions.WorkflowPage
	err      error
	savedURL string
	newCalls int
}

func (p *manualOpenPool) NewWorkflowPage(_ context.Context, owner, account, site string) (sessions.WorkflowPage, error) {
	p.newCalls++
	return p.page, p.err
}

func TestManualNewTabPreservesSendEvidenceAndPausesForOperator(t *testing.T) {
	for _, sendState := range []string{"sent", "sending", "before_send"} {
		t.Run(sendState, func(t *testing.T) {
			q, p := openFixture(t)
			original := q.jobs["manual-one"]
			state := original.ManualStates["kimi"]
			state.SendState = sendState
			original.ManualStates["kimi"] = state
			path := q.webRecoveryPath(original.ID, "kimi", state.AccountID)
			raw, _ := os.ReadFile(path)
			var journal map[string]any
			_ = json.Unmarshal(raw, &journal)
			journal["send_state"] = sendState
			journal["checkpoint"] = map[string]any{"answer": "原回答证据"}
			raw, _ = json.Marshal(journal)
			if err := os.WriteFile(path, raw, 0600); err != nil {
				t.Fatal(err)
			}
			p.page = sessions.WorkflowPage{TargetID: "new-home-tab", URL: "https://www.kimi.com/"}
			if _, err := q.ManualAction(context.Background(), original.ID, "kimi", "new-tab", 0); !errors.Is(err, ErrManualConflict) {
				t.Fatal("stale create accepted", err)
			}
			if p.newCalls != 0 {
				t.Fatal("stale request created a tab")
			}
			job, err := q.ManualAction(context.Background(), original.ID, "kimi", "new-tab", state.Revision)
			if err != nil {
				t.Fatal(err)
			}
			updated := job.ManualStates["kimi"]
			if updated.Status != "operator_active" || updated.TargetID != "new-home-tab" || updated.SendState != sendState || updated.ConversationURL != state.ConversationURL || len(q.ch) != 0 || job.Prompt != original.Prompt {
				t.Fatalf("new tab reset or queued original question: %+v", job)
			}
			raw, _ = os.ReadFile(path)
			_ = json.Unmarshal(raw, &journal)
			history := journal["manual_turn_history"].([]any)
			audit := history[len(history)-1].(map[string]any)
			if journal["manual_new_tab"] != true || journal["send_state"] != sendState || journal["checkpoint"].(map[string]any)["answer"] != "原回答证据" || audit["previous_target_id"] != state.TargetID || audit["previous_send_state"] != sendState {
				t.Fatalf("original evidence lost: %s", raw)
			}
			if _, err := q.ManualAction(context.Background(), original.ID, "kimi", "new-tab", state.Revision); !errors.Is(err, ErrManualConflict) {
				t.Fatal("duplicate create accepted", err)
			}
			if p.newCalls != 1 {
				t.Fatal("duplicate request created more tabs")
			}
		})
	}
}

func TestManualNewTabHandlesFailureBeforeFirstPageExists(t *testing.T) {
	q, p := openFixture(t)
	j := q.jobs["manual-one"]
	state := j.ManualStates["kimi"]
	path := q.webRecoveryPath(j.ID, "kimi", state.AccountID)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	state.TargetID, state.SendState, state.ConversationURL = "", "", ""
	j.ManualStates["kimi"] = state
	j.Results = []map[string]any{{"site_id": "doubao", "answer": "另一平台已完成的结果"}}
	j.WebDispatched = []string{"doubao"}
	p.page = sessions.WorkflowPage{TargetID: "first-page", URL: "https://www.kimi.com/"}
	out, err := q.ManualAction(context.Background(), j.ID, "kimi", "new-tab", state.Revision)
	if err != nil {
		t.Fatal(err)
	}
	if out.ManualStates["kimi"].SendState != "before_send" || out.ManualStates["kimi"].TargetID != "first-page" || len(q.ch) != 0 {
		t.Fatalf("bad first page state: %+v", out)
	}
}

func TestManualNewTabDoesNotResetDispatchedSiteWithoutItsJournal(t *testing.T) {
	q, p := openFixture(t)
	j := q.jobs["manual-one"]
	state := j.ManualStates["kimi"]
	if err := os.Remove(q.webRecoveryPath(j.ID, "kimi", state.AccountID)); err != nil {
		t.Fatal(err)
	}
	state.TargetID, state.SendState, state.ConversationURL = "", "", ""
	j.ManualStates["kimi"] = state
	j.WebDispatched = []string{"kimi"}
	if _, err := q.ManualAction(context.Background(), j.ID, "kimi", "new-tab", state.Revision); err == nil {
		t.Fatal("dispatched question reset as unsent")
	}
	if p.newCalls != 0 {
		t.Fatal("unbound new page created")
	}
}

func TestManualNewTabDoesNotInventAnUnsentJournalForKnownSend(t *testing.T) {
	q, p := openFixture(t)
	j := q.jobs["manual-one"]
	state := j.ManualStates["kimi"]
	if err := os.Remove(q.webRecoveryPath(j.ID, "kimi", state.AccountID)); err != nil {
		t.Fatal(err)
	}
	if _, err := q.ManualAction(context.Background(), j.ID, "kimi", "new-tab", state.Revision); err == nil {
		t.Fatal("known send was reset without its journal")
	}
	if p.newCalls != 0 {
		t.Fatal("created unbound replacement for lost sent evidence")
	}
}

func (p *manualOpenPool) OpenWorkflowPage(_ context.Context, owner, account, site, id, savedURL string) (sessions.WorkflowPage, error) {
	p.savedURL = savedURL
	return p.page, p.err
}

func openFixture(t *testing.T) (*Queue, *manualOpenPool) {
	q := manualFixture(t)
	pool := &manualOpenPool{Pool: sessions.NewPool("/no/chrome", t.TempDir(), "", filepath.Join(t.TempDir(), "accounts.json"))}
	q.Sess = pool
	j := q.jobs["manual-one"]
	state := j.ManualStates["kimi"]
	state.ConversationURL = "https://www.kimi.com/chat/original"
	state.SendState = "sent"
	j.ManualStates["kimi"] = state
	if err := q.createManualJournal(j, "kimi", state); err != nil {
		t.Fatal(err)
	}
	path := q.webRecoveryPath(j.ID, "kimi", state.AccountID)
	raw, _ := os.ReadFile(path)
	var data map[string]any
	_ = json.Unmarshal(raw, &data)
	data["page_url"] = state.ConversationURL
	data["send_state"] = "sent"
	data["original_answer"] = map[string]any{"text": "已生成的原回答"}
	raw, _ = json.Marshal(data)
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	pool.page = sessions.WorkflowPage{TargetID: state.TargetID, URL: state.ConversationURL}
	return q, pool
}

func TestManualOpenIgnoresStaleViewRevisionWithoutScheduling(t *testing.T) {
	q, _ := openFixture(t)
	job, err := q.ManualAction(context.Background(), "manual-one", "kimi", "open", 0)
	if err != nil || job.ManualStates["kimi"].Revision != 3 || len(q.ch) != 0 {
		t.Fatalf("view mutated/queued: %+v %v", job, err)
	}
	if _, err = q.ManualAction(context.Background(), "manual-one", "kimi", "continue", 0); !errors.Is(err, ErrManualConflict) {
		t.Fatal("execution lost revision guard", err)
	}
}

func TestManualOpenRebindsOriginalJournalWithoutResending(t *testing.T) {
	for _, foregroundFails := range []bool{false, true} {
		t.Run(map[bool]string{false: "success", true: "foreground failure"}[foregroundFails], func(t *testing.T) {
			q, p := openFixture(t)
			p.page.TargetID = "restored"
			p.page.Reopened = true
			if foregroundFails {
				p.err = errors.New("Mac foreground unavailable")
			}
			_, err := q.ManualAction(context.Background(), "manual-one", "kimi", "open", 0)
			if (err != nil) != foregroundFails {
				t.Fatal(err)
			}
			job, _ := q.Get("manual-one")
			state := job.ManualStates["kimi"]
			if state.TargetID != "restored" || state.SendState != "sent" || state.Status != "waiting_manual" || job.Prompt != "当前问题" || len(q.ch) != 0 {
				t.Fatalf("restored state unsafe: %+v", job)
			}
			raw, _ := os.ReadFile(q.webRecoveryPath(job.ID, "kimi", state.AccountID))
			var data map[string]any
			_ = json.Unmarshal(raw, &data)
			if data["target_id"] != "restored" || data["send_state"] != "sent" || data["prompt"] != "当前问题" || data["original_answer"].(map[string]any)["text"] != "已生成的原回答" {
				t.Fatalf("journal lost original evidence: %s", raw)
			}
			raw, err = os.ReadFile(q.jobPath(job.ID))
			if err != nil {
				t.Fatal(err)
			}
			var persisted Job
			_ = json.Unmarshal(raw, &persisted)
			if persisted.ManualStates["kimi"].TargetID != "restored" {
				t.Fatal("job binding not durable")
			}
		})
	}
}

func TestManualOpenUntrustedJournalCannotRestore(t *testing.T) {
	q, p := openFixture(t)
	j := q.jobs["manual-one"]
	s := j.ManualStates["kimi"]
	path := q.webRecoveryPath(j.ID, "kimi", s.AccountID)
	raw, _ := os.ReadFile(path)
	var data map[string]any
	_ = json.Unmarshal(raw, &data)
	data["prompt"] = "其他问题"
	raw, _ = json.Marshal(data)
	_ = os.WriteFile(path, raw, 0600)
	if _, err := q.ManualAction(context.Background(), j.ID, "kimi", "open", 3); err != nil {
		t.Fatal("existing tab focus should remain available", err)
	}
	if p.savedURL != "" {
		t.Fatal("unbound journal allowed automatic restore")
	}
}

func TestManualOpenPersistenceFailureRollsBackBothBindings(t *testing.T) {
	q, p := openFixture(t)
	j := q.jobs["manual-one"]
	s := j.ManualStates["kimi"]
	path := q.webRecoveryPath(j.ID, "kimi", s.AccountID)
	before, _ := os.ReadFile(path)
	// Preserve the journal location but make the job directory unwritable.
	if err := os.Remove(q.DataDir); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(q.DataDir, []byte("blocked"), 0600); err != nil {
		t.Fatal(err)
	}
	p.page.TargetID = "restored"
	p.page.Reopened = true
	if _, err := q.ManualAction(context.Background(), j.ID, "kimi", "open", 3); err == nil {
		t.Fatal("persistence failure hidden")
	}
	after, _ := os.ReadFile(path)
	job, _ := q.Get(j.ID)
	if string(before) != string(after) || job.ManualStates["kimi"].TargetID != s.TargetID || len(q.ch) != 0 {
		t.Fatal("failed restore left partial binding")
	}
}
