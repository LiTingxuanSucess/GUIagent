package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

func manualFixture(t *testing.T) *Queue {
	t.Helper()
	return &Queue{DataDir: t.TempDir(), ch: make(chan string, 5), jobs: map[string]*Job{"manual-one": {
		ID: "manual-one", AK: "owner", Prompt: "当前问题", Sites: []string{"kimi"}, Status: "waiting_manual", ManualWorkflow: true, DeadlinePolicy: "manual_web",
		ManualStates: map[string]ManualState{"kimi": {Site: "kimi", Status: "waiting_manual", AccountID: "acct_one", TargetID: "original-target", SendState: "sending", Revision: 3}},
	}}}
}

func TestManualResumePersistsBeforeQueueAndRejectsReplay(t *testing.T) {
	q := manualFixture(t)
	job, err := q.ManualAction(context.Background(), "manual-one", "kimi", "submitted", 3)
	if err != nil || job.Status != "queued" || job.ManualStates["kimi"].Status != "resume_queued" {
		t.Fatalf("resume: %+v %v", job, err)
	}
	raw, err := os.ReadFile(q.jobPath(job.ID))
	if err != nil {
		t.Fatal(err)
	}
	var saved Job
	if err := json.Unmarshal(raw, &saved); err != nil {
		t.Fatal(err)
	}
	if saved.ManualStates["kimi"].Action != "submitted" || len(q.ch) != 1 {
		t.Fatal("resume was not durable or not queued exactly once")
	}
	if _, err := q.ManualAction(context.Background(), job.ID, "kimi", "submitted", 3); !errors.Is(err, ErrManualConflict) {
		t.Fatal("stale/repeated resume accepted", err)
	}
	if len(q.ch) != 1 {
		t.Fatal("duplicate resume enqueued")
	}
}

func TestManualSaveFailureDoesNotScheduleOrAdvance(t *testing.T) {
	q := manualFixture(t)
	blocked := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(blocked, []byte("blocked"), 0600); err != nil {
		t.Fatal(err)
	}
	q.DataDir = blocked
	if _, err := q.ManualAction(context.Background(), "manual-one", "kimi", "continue", 3); err == nil {
		t.Fatal("save failure hidden")
	}
	job, _ := q.Get("manual-one")
	if job.Status != "waiting_manual" || job.ManualStates["kimi"].Revision != 3 || len(q.ch) != 0 {
		t.Fatal("failed persistence advanced or scheduled task")
	}
}

func TestManualRestorePersistsAndKeepsBoundTarget(t *testing.T) {
	q := manualFixture(t)
	job, err := q.ManualAction(context.Background(), "manual-one", "kimi", "restore", 3)
	if err != nil || job.ManualStates["kimi"].Action != "restore" || job.ManualStates["kimi"].TargetID != "original-target" || len(q.ch) != 1 {
		t.Fatalf("restore changed identity or failed to schedule: %+v %v", job, err)
	}
}

func TestManualConfirmCurrentIsExplicitAndRevisionBound(t *testing.T) {
	q := manualFixture(t)
	job, err := q.ManualAction(context.Background(), "manual-one", "kimi", "confirm-current", 3)
	if err != nil || job.ManualStates["kimi"].Action != "confirm-current" || job.ManualStates["kimi"].TargetID != "original-target" {
		t.Fatalf("confirmation changed bound account/tab: %+v %v", job, err)
	}
	if _, err := q.ManualAction(context.Background(), "manual-one", "kimi", "confirm-current", 3); !errors.Is(err, ErrManualConflict) {
		t.Fatal("repeated/stale confirmation was accepted", err)
	}
}

func TestManualErrorsKeepTheirMeaningAndDoNotRetry(t *testing.T) {
	for _, expected := range []string{"manual_question_mismatch", "manual_send_unconfirmed"} {
		code, message, retry := publicScrapeError(&scrapeFailure{code: expected, raw: "private account details", retryable: false})
		if code != expected || message != manualReason(expected) || retry {
			t.Fatalf("manual error hidden or retried: %s %s %v", code, message, retry)
		}
	}
}

func TestManualPublicShareIsValidatedAndBoundToRevision(t *testing.T) {
	q := manualFixture(t)
	j := q.jobs["manual-one"]
	s := j.ManualStates["kimi"]
	s.Site = "qianwen"
	j.Sites = []string{"qianwen"}
	j.ManualStates = map[string]ManualState{"qianwen": s}
	if _, err := q.ManualAction(context.Background(), j.ID, "qianwen", "share", 3, "https://example.org/private"); err == nil {
		t.Fatal("arbitrary URL accepted")
	}
	url := "https://qianwen.my.cn/share/chat/59f383d7ff314307b844885e77ea5815?lang=zh-CN"
	job, err := q.ManualAction(context.Background(), j.ID, "qianwen", "share", 3, url)
	if err != nil || job.ManualStates["qianwen"].SharedURL != url || job.ManualStates["qianwen"].Action != "share" {
		t.Fatalf("share recovery not persisted: %+v %v", job, err)
	}
}

func TestManualRestartDoesNotReplayUncertainSend(t *testing.T) {
	q := manualFixture(t)
	q.jobs["manual-one"].Status = "running"
	state := q.jobs["manual-one"].ManualStates["kimi"]
	state.Status = "collecting"
	state.Action = "submitted"
	q.jobs["manual-one"].ManualStates["kimi"] = state
	q.save(q.jobs["manual-one"])
	reloaded := &Queue{DataDir: q.DataDir, jobs: map[string]*Job{}}
	_, queued, _ := reloaded.load()
	job, _ := reloaded.Get("manual-one")
	if len(queued) != 0 || job.Status != "waiting_manual" || job.ManualStates["kimi"].TargetID != "original-target" || job.ManualStates["kimi"].Action != "" || job.ManualStates["kimi"].SendState != "sending" {
		t.Fatalf("unsafe restart recovery: %+v queued=%v", job, queued)
	}
}

func TestManualWaitingDoesNotHoldWorkerAndCancellationReleasesAccount(t *testing.T) {
	q := manualFixture(t)
	pool := sessions.NewPool("/no/chrome", t.TempDir(), "13800138000", filepath.Join(t.TempDir(), "accounts.json"))
	q.Sess = pool
	state := q.jobs["manual-one"].ManualStates["kimi"]
	state.AccountID = "default"
	q.jobs["manual-one"].ManualStates["kimi"] = state
	q.restoreManualReservations()
	if _, err := pool.ReserveWorkflow("other/kimi", "kimi", "default"); !errors.Is(err, sessions.ErrWorkflowBusy) {
		t.Fatal("manual browser was not reserved")
	}
	job, _ := q.Get("manual-one")
	started := time.Now()
	q.runManualWorkflow(context.Background(), job, started, 0)
	if time.Since(started) > time.Second {
		t.Fatal("waiting manual task occupied worker")
	}
	if _, err := q.CancelVisible(job.ID, "owner", false); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.ReserveWorkflow("other/kimi", "kimi", "default"); err != nil {
		t.Fatal("cancel did not release account", err)
	}
}

func TestManualPublicSnapshotHidesBrowserIdentity(t *testing.T) {
	q := manualFixture(t)
	q.publish("manual-one")
	raw, ok := q.VisibleBody("manual-one", "owner", false)
	if !ok {
		t.Fatal("missing snapshot")
	}
	var public Job
	if err := json.Unmarshal(raw, &public); err != nil {
		t.Fatal(err)
	}
	if public.ManualStates != nil || public.Status != "waiting_manual" {
		t.Fatal("private identity leaked or waiting state lost")
	}
	job, _ := q.Get("manual-one")
	if job.ManualStates["kimi"].TargetID != "original-target" {
		t.Fatal("snapshot mutated stored record")
	}
}

func TestManualEmptyPoolPausesWithoutAPIFallback(t *testing.T) {
	q := manualFixture(t)
	q.Sess = sessions.NewPool("/no/chrome", t.TempDir(), "", filepath.Join(t.TempDir(), "accounts.json"))
	job := q.jobs["manual-one"]
	job.Status = "running"
	job.ManualStates["kimi"] = ManualState{Site: "kimi", Status: "queued"}
	q.runManualWorkflow(context.Background(), copyJob(job), time.Now(), 0)
	got, _ := q.Get(job.ID)
	if got.Status != "waiting_manual" || got.ManualStates["kimi"].ErrorCode != "no_account" || len(got.Results) != 0 {
		t.Fatalf("empty pool did not pause: %+v", got)
	}
}
