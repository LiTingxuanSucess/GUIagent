package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

func deliveryFixtureJob(t *testing.T, q *Queue, sites ...string) *Job {
	t.Helper()
	t.Setenv("GEO_BROWSER_MEMORY_HIGH_MB", "0")
	t.Setenv("GEO_BROWSER_MEMORY_MAX_MB", "0")
	t.Setenv("GEO_SITE_FAILURE_RETRIES", "0")
	t.Setenv("GEO_API_FALLBACK_WEB_RETRIES", "0")
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	q.DataDir = filepath.Join(q.GeoRoot, "data", "jobs")
	now := time.Now()
	j := &Job{ID: "delivery-fixture", RequestID: "original-request", Status: "queued", Prompt: "fixture question", Sites: sites,
		DeliveryPolicy: "eventual", DeadlinePolicy: "web_first", CreatedAt: now.Format(time.RFC3339Nano), createdUnix: now.UnixMilli()}
	q.jobs = map[string]*Job{j.ID: j}
	q.save(j)
	return j
}

func expireDeliveryRetry(q *Queue, id string) {
	q.patch(id, func(j *Job) {
		j.RetryAt = time.Now().Add(-time.Second).Format(time.RFC3339Nano)
		for _, row := range j.Results {
			if !validDelivery(row) {
				row["delivery_retry_at"] = j.RetryAt
			}
		}
	})
}

func TestEventualDeliveryRetriesMissingSiteAndCallbacksOnlyAfterAnswers(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,os,pathlib,sys
a=sys.argv; site=a[a.index('--site')+1]; prompt=a[a.index('--prompt')+1]; question=a[a.index('--question-id')+1]
with pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(site+'\n')
state=pathlib.Path(os.environ['GEO_WEB_RECOVERY_FILE'])
state.parent.mkdir(parents=True,exist_ok=True)
state.write_text(json.dumps({'site_id':site,'prompt':prompt,'question_id':question,'account_id':os.environ['GEO_WEB_RECOVERY_ACCOUNT_ID'],'send_state':'before_send'}),encoding='utf-8')
failed=pathlib.Path('failed-once')
if site=='doubao' and not failed.exists():
 failed.touch(); print(json.dumps({'ok':False,'error_code':'platform_busy','retryable':False}))
else:print(json.dumps({'ok':True,'answer':site+' effective answer','source':'web','answer_origin':'web'}))
`, "account")
	job := deliveryFixtureJob(t, q, "deepseek", "doubao")
	var callbacks atomic.Int32
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body Job
		if json.NewDecoder(r.Body).Decode(&body) != nil || body.Status != "succeeded" || body.CompletedSites != 2 {
			t.Error("callback ran before every requested platform delivered an answer")
		}
		callbacks.Add(1)
		w.WriteHeader(200)
	}))
	defer callback.Close()
	job.CallbackURL, q.AllowPrivateCallbacks = callback.URL, true
	q.run(job.ID)
	first, _ := q.Get(job.ID)
	if first.Status != "queued" || first.RetryCount != 1 || first.CompletedSites != 1 || first.FinishedAt != "" || callbacks.Load() != 0 || len(q.siteSem) != 0 {
		t.Fatalf("failed round was ended or retained resources: %+v", first)
	}
	if due, err := time.Parse(time.RFC3339Nano, first.RetryAt); err != nil || !due.After(time.Now()) {
		t.Fatalf("retry not durably delayed: %q", first.RetryAt)
	}
	q.callback(job.ID)
	if callbacks.Load() != 0 {
		t.Fatal("manual callback bypassed pending delivery guard")
	}
	if paths, _ := filepath.Glob(filepath.Join(filepath.Dir(q.webRecoveryPath(job.ID, "doubao", "account")), "*.json")); len(paths) != 1 {
		t.Fatal("pending journal was cleaned before effective delivery")
	}
	expireDeliveryRetry(q, job.ID)
	q.run(job.ID)
	final, _ := q.Get(job.ID)
	if final.Status != "succeeded" || final.CompletedSites != 2 || final.RetryAt != "" || final.WaitReason != "" || len(final.Results) != 2 {
		t.Fatalf("missing result not completed: %+v", final)
	}
	for _, row := range final.Results {
		if !validDelivery(row) || row["delivery_pending"] == true {
			t.Fatalf("invalid final result: %v", row)
		}
	}
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if strings.Count(string(events), "deepseek\n") != 1 || strings.Count(string(events), "doubao\n") != 2 {
		t.Fatalf("successful platform was resubmitted: %q", events)
	}
	deadline := time.Now().Add(time.Second)
	for callbacks.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if callbacks.Load() != 1 {
		t.Fatalf("callbacks=%d", callbacks.Load())
	}
}

func TestEventualRestartWithoutJournalRunsWebpageFresh(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,os,pathlib,sys
a=sys.argv; site=a[a.index('--site')+1]; prompt=a[a.index('--prompt')+1]; question=a[a.index('--question-id')+1]
with pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(site+'\n')
state=pathlib.Path(os.environ['GEO_WEB_RECOVERY_FILE'])
state.parent.mkdir(parents=True,exist_ok=True)
state.write_text(json.dumps({'site_id':site,'prompt':prompt,'question_id':question,'account_id':os.environ['GEO_WEB_RECOVERY_ACCOUNT_ID'],'send_state':'sent'}),encoding='utf-8')
print(json.dumps({'ok':True,'answer':site+' effective answer','source':'web','answer_origin':'web'}))
`, "account")
	job := deliveryFixtureJob(t, q, "doubao")
	q.patch(job.ID, func(j *Job) {
		j.DispatchTracked = true
		j.Results = []map[string]any{{"site_id": "doubao", "ok": false, "error_code": "service_interrupted", "delivery_pending": true,
			"delivery_resume_required": true, "retry_state": "awaiting_confirmation", "web_skipped": true,
			"attempts": []map[string]any{{"channel": "web", "skipped": true, "error_code": "browser_admission_timeout"}}}}
	})
	tracked, _ := q.Get(job.ID)
	if !q.deliveryNeverSent(tracked, "doubao") {
		t.Fatal("a tracked site without a dispatch record or journal must count as never sent")
	}
	if q.deliveryNeverSent(&Job{ID: job.ID, Sites: job.Sites}, "doubao") {
		t.Fatal("jobs created before dispatch tracking must stay on the resume path")
	}
	q.run(job.ID)
	final, _ := q.Get(job.ID)
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if final.Status != "succeeded" || len(final.Results) != 1 || final.Results[0]["source"] != "web" || strings.Count(string(events), "doubao\n") != 1 {
		t.Fatalf("restart flag without a dispatch skipped the webpage: %+v events=%q", final, events)
	}
	raw, _ := os.ReadFile(q.jobPath(job.ID))
	var stored Job
	if json.Unmarshal(raw, &stored) != nil || len(stored.WebDispatched) != 1 || stored.WebDispatched[0] != "doubao" || q.deliveryNeverSent(&stored, "doubao") {
		t.Fatalf("scraper start was not durably recorded: %s", raw)
	}
	other := &Job{ID: "journal-fixture", Prompt: job.Prompt, Sites: []string{"doubao"}, DispatchTracked: true}
	journal := q.webRecoveryPath(other.ID, "doubao", "account")
	if err := os.MkdirAll(filepath.Dir(journal), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(journal, []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	if q.deliveryNeverSent(other, "doubao") {
		t.Fatal("an existing journal, even an unreadable one, must keep the resume path")
	}
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	other = &Job{ID: "cloud-fixture", Prompt: job.Prompt, Sites: []string{"doubao"}, DispatchTracked: true}
	if q.deliveryNeverSent(other, "doubao") {
		t.Fatal("cloud browsers keep no journal and must stay on the cautious path")
	}
}

type deliveryExistingPool struct{ *recoveryPool }

func (p *deliveryExistingPool) AcquireExistingForRecoveryContext(ctx context.Context, site, account string) (*sessions.Lease, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	for i, lease := range p.leases {
		if lease.AccountID == account {
			if !lease.Manager.Busy() {
				lease.Manager.AcquireSite("")
				lease = &sessions.Lease{AccountID: account, Phone: lease.Phone, Manager: lease.Manager}
				p.leases[i] = lease
			}
			return lease, nil
		}
	}
	return nil, fmt.Errorf("original target missing")
}

func TestEventualAPIFailureRetriesOriginalWebPageWithoutResending(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,os,pathlib,sys
a=sys.argv; site=a[a.index('--site')+1]; prompt=a[a.index('--prompt')+1]; question=a[a.index('--question-id')+1]
action=a[a.index('--recovery-action')+1] if '--recovery-action' in a else 'normal'
with pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(action+'\n')
state=pathlib.Path(os.environ['GEO_WEB_RECOVERY_FILE'])
if action=='probe':
 print(json.dumps({'ok':True,'recovery_action':'probe','context_valid':True,'ready':True,'target_id':'original-target','send_state':'sent','phase':'sent'}))
else:
 if action=='normal':
  state.parent.mkdir(parents=True,exist_ok=True)
  state.write_text(json.dumps({'site_id':site,'prompt':prompt,'question_id':question,'account_id':os.environ['GEO_WEB_RECOVERY_ACCOUNT_ID'],'send_state':'sent','target_id':'original-target'}),encoding='utf-8')
 print(json.dumps({'ok':False,'error_code':'platform_busy','retryable':False}))
`, "account")
	q.Sess = &deliveryExistingPool{q.Sess.(*recoveryPool)}
	job := deliveryFixtureJob(t, q, "doubao")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			w.WriteHeader(503)
			fmt.Fprint(w, `{"error":{"message":"temporary fixture failure"}}`)
			return
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"effective API fallback answer"}}]}`)
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	q.run(job.ID)
	first, _ := q.Get(job.ID)
	if first.Status != "queued" || first.Results[0]["api_fallback_failed"] != true || first.Results[0]["retry_state"] != "retry_wait" || first.WaitReason != "api_request_failed" {
		t.Fatalf("API temporary failure was not retryable delivery: %+v", first)
	}
	expireDeliveryRetry(q, job.ID)
	q.run(job.ID)
	final, _ := q.Get(job.ID)
	if final.Status != "succeeded" || final.Results[0]["source"] != "official_api" || !validDelivery(final.Results[0]) || calls.Load() != 2 {
		t.Fatalf("API fallback did not complete persistent request: %+v", final)
	}
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if string(events) != "normal\nprobe\nresume\n" {
		t.Fatalf("web question repeated: %q", events)
	}
	if got := len(cloneAttemptList(final.Results[0]["attempts"])); got != 4 {
		t.Fatalf("attempt evidence not merged: %d", got)
	}
}

func TestEventualRestartPreservesIDAnswersAndResumesSentJournal(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,pathlib,sys
a=sys.argv; action=a[a.index('--recovery-action')+1] if '--recovery-action' in a else 'normal'
with pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(action+'\n')
assert action!='normal','must not send again after restart'
if action=='probe':print(json.dumps({'ok':True,'recovery_action':'probe','context_valid':True,'ready':True,'target_id':'original-target','send_state':'sent','phase':'sent'}))
else:print(json.dumps({'ok':True,'answer':'resumed original answer','source':'web','answer_origin':'web'}))
`, "account")
	q.Sess = &deliveryExistingPool{q.Sess.(*recoveryPool)}
	job := deliveryFixtureJob(t, q, "deepseek", "doubao")
	job.Status, job.DeliveryPolicy = "running", ""
	job.Results = []map[string]any{{"site_id": "deepseek", "ok": true, "answer": "already delivered original answer", "source": "web"}}
	q.save(job)
	path := q.webRecoveryPath(job.ID, "doubao", "account")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(map[string]any{"site_id": "doubao", "question_id": "job_" + job.ID, "prompt": job.Prompt, "account_id": "account", "send_state": "sending", "target_id": "original-target"})
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GEO_REQUIRE_DELIVERY", "1")
	q.jobs = map[string]*Job{}
	_, queued, interrupted := q.load()
	restored, _ := q.Get(job.ID)
	if len(queued) != 1 || len(interrupted) != 0 || restored.Status != "queued" || restored.DeliveryPolicy != "eventual" || restored.RequestID != job.RequestID || restored.CompletedSites != 1 || restored.Results[0]["answer"] != "already delivered original answer" {
		t.Fatalf("restart lost request or partial answers: %+v", restored)
	}
	q.run(job.ID)
	final, _ := q.Get(job.ID)
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if final.Status != "succeeded" || final.CompletedSites != 2 || string(events) != "probe\nresume\n" || final.Results[0]["answer"] != "already delivered original answer" {
		t.Fatalf("restart replayed instead of resuming: job=%+v events=%q", final, events)
	}
}

func TestEventualMissingTargetWaitsOrUsesAPIWithoutFreshSend(t *testing.T) {
	for _, apiEnabled := range []bool{false, true} {
		t.Run(fmt.Sprint(apiEnabled), func(t *testing.T) {
			q := attemptFixtureQueue(t, `from pathlib import Path
Path('unexpected-web-send').touch()
print('{"ok":true,"answer":"must not send again"}')
`, "account")
			job := deliveryFixtureJob(t, q, "doubao")
			job.Status = "running"
			q.save(job)
			if apiEnabled {
				api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"effective fallback answer"}}]}`)
				}))
				defer api.Close()
				t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
				t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
			}
			q.jobs = map[string]*Job{}
			q.load()
			q.run(job.ID)
			current, _ := q.Get(job.ID)
			if apiEnabled {
				if current.Status != "succeeded" || current.Results[0]["source"] != "official_api" {
					t.Fatalf("fallback provenance lost: %+v", current)
				}
			} else if current.Status != "queued" || current.Results[0]["retry_state"] != "awaiting_confirmation" || current.Results[0]["delivery_pending"] != true {
				t.Fatalf("unknown send was discarded: %+v", current)
			}
			if _, err := os.Stat(filepath.Join(q.GeoRoot, "unexpected-web-send")); !os.IsNotExist(err) {
				t.Fatal("missing target caused a new question")
			}
		})
	}
}

func TestEventualPreparationExhaustionAllowsAPIButLocalAdmissionDoesNot(t *testing.T) {
	q, calls := webFirstLocalFailureFixture(t, "first", "second")
	job := deliveryFixtureJob(t, q, "doubao")
	for _, account := range []string{"first", "second"} {
		if err := os.Remove(filepath.Join(q.GeoRoot, account, "DevToolsActivePort")); err != nil {
			t.Fatal(err)
		}
	}
	q.run(job.ID)
	current, _ := q.Get(job.ID)
	if current.Status != "succeeded" || current.Results[0]["source"] != "official_api" || calls.Load() != 1 {
		t.Fatalf("actual preparation exhaustion did not permit final fallback: %+v calls=%d", current, calls.Load())
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "web-started")); !os.IsNotExist(err) {
		t.Fatal("unexpected web execution")
	}
	blocked := webSkip("doubao", "fixture question", "browser_admission_timeout", "browser_admission", "fixture blocked")
	q.applyAPIFallback("doubao", "fixture question", blocked)
	if calls.Load() != 1 {
		t.Fatal("resource admission failure substituted API")
	}
}

func TestEventualAttemptsRemainBoundedAndKeepLatestSuccess(t *testing.T) {
	prior := map[string]any{"attempts_dropped": 7}
	var entries []map[string]any
	for i := 0; i < 300; i++ {
		entries = append(entries, map[string]any{"channel": "web", "id": i, "ok": false})
	}
	prior["attempts"] = entries
	merged := mergeDeliveryResult(prior, map[string]any{"ok": true, "answer": "effective answer", "delivery_pending": true, "retry_state": "retry_wait", "attempts": []map[string]any{{"channel": "api", "id": "latest", "ok": true}}})
	got := cloneAttemptList(merged["attempts"])
	if len(got) != 256 || got[len(got)-1]["id"] != "latest" || merged["attempts_truncated"] != true || merged["attempts_dropped"] != 52 || merged["delivery_pending"] != nil || merged["retry_state"] != nil {
		t.Fatalf("bounded evidence or pending cleanup incorrect: %v", merged)
	}
	if validDelivery(mergeDeliveryResult(nil, map[string]any{"ok": true, "answer": " \n\t"})) {
		t.Fatal("empty body counted as delivery")
	}
}

func TestDeliveryAdmissionTimeoutDefaultsToFortyFiveSeconds(t *testing.T) {
	t.Setenv("GEO_DELIVERY_ADMISSION_SEC", "")
	if deliveryAdmissionTimeout() != 45*time.Second {
		t.Fatalf("default = %s", deliveryAdmissionTimeout())
	}
	t.Setenv("GEO_DELIVERY_ADMISSION_SEC", "90")
	if deliveryAdmissionTimeout() != 90*time.Second {
		t.Fatalf("override = %s", deliveryAdmissionTimeout())
	}
}

func TestEventualSchedulerPrefersNeverStartedJobs(t *testing.T) {
	now := time.Now()
	old := &Job{ID: "old", Status: "queued", CreatedAt: now.Add(-time.Hour).Format(time.RFC3339Nano), RetryCount: 4, Results: []map[string]any{{"ok": false}}}
	fresh := &Job{ID: "fresh", Status: "queued", CreatedAt: now.Format(time.RFC3339Nano)}
	q := &Queue{jobs: map[string]*Job{old.ID: old, fresh.ID: fresh}, ch: make(chan string, 4)}
	if got := q.scheduleDeliveries(now); got != 2 {
		t.Fatalf("dispatched %d", got)
	}
	first, second := <-q.ch, <-q.ch
	if first != "fresh" || second != "old" {
		t.Fatalf("order %s then %s", first, second)
	}
}

func TestEventualSchedulerDispatchesDueJobOnce(t *testing.T) {
	now := time.Now()
	job := &Job{ID: "due", Status: "queued", DeliveryPolicy: "eventual", RetryAt: now.Add(time.Minute).Format(time.RFC3339Nano)}
	q := &Queue{jobs: map[string]*Job{job.ID: job}, ch: make(chan string, 4)}
	if q.scheduleDeliveries(now) != 0 {
		t.Fatal("future retry dispatched early")
	}
	var wg sync.WaitGroup
	var dispatched atomic.Int32
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); dispatched.Add(int32(q.scheduleDeliveries(now.Add(2 * time.Minute)))) }()
	}
	wg.Wait()
	select {
	case id := <-q.ch:
		if id != job.ID {
			t.Fatal("wrong dispatched ID")
		}
	case <-time.After(time.Second):
		t.Fatal("due retry missing")
	}
	if dispatched.Load() != 1 {
		t.Fatalf("duplicate dispatches=%d", dispatched.Load())
	}
	select {
	case <-q.ch:
		t.Fatal("duplicate queue token")
	default:
	}
	q.mu.Lock()
	q.activeRuns = map[string]bool{job.ID: true}
	delete(q.enqueued, job.ID)
	q.mu.Unlock()
	if q.scheduleDeliveries(now.Add(2*time.Minute)) != 0 {
		t.Fatal("active job dispatched concurrently")
	}
}

func TestEventualOptInAndTerminalMigrationSafety(t *testing.T) {
	t.Setenv("GEO_REQUIRE_DELIVERY", "1")
	q := &Queue{jobs: map[string]*Job{}, ch: make(chan string, 8), DataDir: t.TempDir()}
	accepted, _, err := q.Submit("fixture question", []string{"doubao"}, "", "fixture-key", "request")
	if err != nil || accepted.DeliveryPolicy != "eventual" {
		t.Fatalf("new policy not adopted: %+v %v", accepted, err)
	}
	terminal := Job{ID: "old-terminal", Status: "failed", DeadlinePolicy: "web_first", CreatedAt: time.Now().Format(time.RFC3339Nano), CallbackURL: "https://example.org/fixture"}
	writeStartupJob(t, q.DataDir, terminal)
	before, _ := os.ReadFile(q.jobPath(terminal.ID))
	reloaded := &Queue{DataDir: q.DataDir, jobs: map[string]*Job{}}
	_, _, interrupted := reloaded.load()
	after, _ := os.ReadFile(q.jobPath(terminal.ID))
	stored, _ := reloaded.Get(terminal.ID)
	if len(interrupted) != 0 || stored.Status != "failed" || stored.DeliveryPolicy != "" || string(before) != string(after) {
		t.Fatal("old terminal history or callback was replayed")
	}
}

func TestEventualWaitClassificationAndBackoff(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	for _, test := range []struct {
		code string
		want string
	}{
		{"api_auth_failed", "operator_wait"}, {"api_quota_exhausted", "operator_wait"}, {"api_request_failed", "retry_wait"}, {"api_admission_timeout", "retry_wait"},
	} {
		state, reason := deliveryWaitState("doubao", map[string]any{"error_code": "generation_timeout", "api_error_code": test.code})
		if state != test.want || reason != test.code {
			t.Fatalf("%s classified as %s/%s", test.code, state, reason)
		}
	}
	for i, want := range []time.Duration{30, 60, 120, 240, 300, 300} {
		if got := deliveryDelay(i+1, "retry_wait"); got != want*time.Second {
			t.Fatalf("round %d delay=%s", i+1, got)
		}
	}
	code, message, retry := publicScrapeError(&scrapeFailure{code: "send_timeout", retryable: true})
	if code != "send_timeout" || message != "发送阶段超时，保留原页面核实，未重复提交问题" || !retry {
		t.Fatal("send timeout lost safe error classification")
	}
}

func writeDeliveryJournal(t *testing.T, q *Queue, job *Job, site, account, state string, checkpoint map[string]any) {
	t.Helper()
	path := q.webRecoveryPath(job.ID, site, account)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(map[string]any{"site_id": internalSiteID(site), "prompt": job.Prompt, "question_id": "job_" + job.ID, "account_id": account, "send_state": state, "checkpoint": checkpoint})
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestEventualMultiplePendingAndCorruptJournalDoNotChooseAccount(t *testing.T) {
	for _, malformed := range []bool{false, true} {
		t.Run(fmt.Sprint(malformed), func(t *testing.T) {
			q := attemptFixtureQueue(t, `print('{"ok":true,"answer":"must not execute"}')`, "first", "second")
			job := deliveryFixtureJob(t, q, "doubao")
			writeDeliveryJournal(t, q, job, "doubao", "first", "sent", nil)
			writeDeliveryJournal(t, q, job, "doubao", "second", "sending", nil)
			if malformed {
				if err := os.WriteFile(q.webRecoveryPath(job.ID, "doubao", "second"), []byte(`{"bad":"journal"}`), 0600); err != nil {
					t.Fatal(err)
				}
			}
			account, pending, safe := q.deliveryJournalState(job, "doubao")
			if account != "" || !pending || safe {
				t.Fatalf("ambiguous journal chosen: %q pending=%v safe=%v", account, pending, safe)
			}
		})
	}
}

type deliveryBusyExistingPool struct{ *deliveryExistingPool }

func (p *deliveryBusyExistingPool) AcquireExistingForRecoveryContext(context.Context, string, string) (*sessions.Lease, error) {
	return nil, context.DeadlineExceeded
}

func TestEventualOriginalLeaseWaitNeverSubstitutesAPI(t *testing.T) {
	q, calls := webFirstLocalFailureFixture(t, "account")
	q.Sess = &deliveryBusyExistingPool{&deliveryExistingPool{q.Sess.(*recoveryPool)}}
	job := deliveryFixtureJob(t, q, "doubao")
	writeDeliveryJournal(t, q, job, "doubao", "account", "sent", nil)
	q.run(job.ID)
	current, _ := q.Get(job.ID)
	if calls.Load() != 0 || current.Status != "queued" || current.Results[0]["error_code"] != "session_setup_timeout" || current.Results[0]["delivery_pending"] != true || current.Results[0]["delivery_resume_required"] != true {
		t.Fatalf("local lease contention substituted API or lost original state: %+v calls=%d", current, calls.Load())
	}
}

func TestEventualCompletedCheckpointSurvivesLostTargetWithIdentityValidation(t *testing.T) {
	for _, wrongIdentity := range []bool{false, true} {
		t.Run(fmt.Sprint(wrongIdentity), func(t *testing.T) {
			q := attemptFixtureQueue(t, `from pathlib import Path
Path('unexpected-send').touch()
print('{"ok":true,"answer":"must not execute"}')
`, "account")
			job := deliveryFixtureJob(t, q, "doubao")
			checkpoint := map[string]any{"web_answer_completed": true, "ok": true, "site_id": "doubao", "question_id": "job_" + job.ID, "prompt": job.Prompt, "answer": "durably completed original webpage answer", "answer_origin": "web", "source": "api:captured", "url": "https://www.doubao.com/chat/private"}
			if wrongIdentity {
				checkpoint["question_id"] = "another-question"
			}
			writeDeliveryJournal(t, q, job, "doubao", "account", "sent", checkpoint)
			var calls atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.WriteHeader(503)
				fmt.Fprint(w, `{"error":{"message":"fixture temporary failure"}}`)
			}))
			defer api.Close()
			t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
			t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
			q.run(job.ID)
			current, _ := q.Get(job.ID)
			if !wrongIdentity {
				row := current.Results[0]
				if current.Status != "succeeded" || calls.Load() != 0 || row["answer"] != checkpoint["answer"] || row["answer_origin"] != "web" || row["source"] != "api:captured" || row["url"] != "" || row["share_error_code"] != "share_not_attempted" {
					t.Fatalf("saved webpage body lost: %+v calls=%d", current, calls.Load())
				}
			} else if current.Status != "queued" || validDelivery(current.Results[0]) || calls.Load() != 1 {
				t.Fatalf("foreign checkpoint accepted: %+v calls=%d", current, calls.Load())
			}
			if _, err := os.Stat(filepath.Join(q.GeoRoot, "unexpected-send")); !os.IsNotExist(err) {
				t.Fatal("lost target resubmitted question")
			}
		})
	}
}

func TestEventualConcurrentRunCannotResubmitSameJob(t *testing.T) {
	q := attemptFixtureQueue(t, `import json,pathlib,time
with pathlib.Path('events').open('a',encoding='utf-8') as f:f.write('normal\n')
pathlib.Path('started').touch()
while not pathlib.Path('release').exists():time.sleep(0.005)
print(json.dumps({'ok':True,'answer':'effective original answer','source':'web'}))
`, "account")
	job := deliveryFixtureJob(t, q, "doubao")
	done := make(chan struct{})
	go func() { q.run(job.ID); close(done) }()
	defer os.WriteFile(filepath.Join(q.GeoRoot, "release"), nil, 0600)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(q.GeoRoot, "started")); err == nil {
			break
		}
		time.Sleep(time.Millisecond)
	}
	second := make(chan struct{})
	go func() { q.run(job.ID); close(second) }()
	select {
	case <-second:
	case <-time.After(time.Second):
		t.Fatal("duplicate runner did not return")
	}
	if err := os.WriteFile(filepath.Join(q.GeoRoot, "release"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("original runner did not complete")
	}
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if string(events) != "normal\n" {
		t.Fatalf("duplicate runner sent question: %q", events)
	}
}

type deliveryPreparationPool struct {
	*recoveryPool
	challenge bool
}

func (p *deliveryPreparationPool) AcquireContext(context.Context, string) (*sessions.Lease, error) {
	return nil, &sessions.PreparationError{Site: "doubao", CandidateIDs: []string{"first", "second"}, FailedIDs: []string{"first", "second"}, FailureCodes: map[string]string{"first": "browser_start_failed", "second": "session_setup_failed"}}
}

func (p *deliveryPreparationPool) RecoveryAccountIDs(string) []string {
	if p.challenge {
		return []string{"challenge"}
	}
	return nil
}

func (p *deliveryPreparationPool) AcquireForRecoveryContext(context.Context, string, map[string]bool) (*sessions.Lease, error) {
	return nil, context.DeadlineExceeded
}

func TestEventualStructuredPreparationProofAndPendingChallenge(t *testing.T) {
	for _, challenge := range []bool{false, true} {
		t.Run(fmt.Sprint(challenge), func(t *testing.T) {
			q, calls := webFirstLocalFailureFixture(t, "first", "second")
			q.Sess = &deliveryPreparationPool{recoveryPool: q.Sess.(*recoveryPool), challenge: challenge}
			job := deliveryFixtureJob(t, q, "doubao")
			q.run(job.ID)
			current, _ := q.Get(job.ID)
			if !challenge {
				row := current.Results[0]
				if current.Status != "succeeded" || row["source"] != "official_api" || row["web_prepare_exhausted"] != true || calls.Load() != 1 {
					t.Fatalf("actual preparation proof ignored: %+v calls=%d", current, calls.Load())
				}
				attempts := cloneAttemptList(row["attempts"])
				if len(attempts) != 3 || attempts[0]["account_id"] != "first" || attempts[0]["skipped"] != true || attempts[1]["account_id"] != "second" || attempts[1]["skipped"] != true {
					t.Fatalf("preparation evidence not retained: %v", attempts)
				}
			} else if current.Status != "queued" || calls.Load() != 0 || current.Results[0]["web_prepare_exhausted"] == true {
				t.Fatalf("pending challenge skipped for API: %+v calls=%d", current, calls.Load())
			}
			if _, err := os.Stat(filepath.Join(q.GeoRoot, "web-started")); !os.IsNotExist(err) {
				t.Fatal("preparation fixture started real web stage")
			}
		})
	}
}

func TestEventualCheckpointPreservesOnlyVerifiedPublicShare(t *testing.T) {
	public := "https://www.doubao.com/thread/xshUuUfTeBHlyEMbv"
	for _, test := range []struct {
		name, status, url string
		keep              bool
	}{
		{"verified_public", "verified", public, true},
		{"unverified_public", "generation_failed", public, false},
		{"private_chat", "verified", "https://www.doubao.com/chat/private", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			q := attemptFixtureQueue(t, `raise AssertionError('no browser call allowed')`, "account")
			job := deliveryFixtureJob(t, q, "doubao")
			checkpoint := map[string]any{"web_answer_completed": true, "ok": true, "site_id": "doubao", "question_id": "job_" + job.ID, "prompt": job.Prompt, "answer": "completed original webpage answer", "answer_origin": "web", "source": "web", "url": test.url, "share_status": test.status}
			writeDeliveryJournal(t, q, job, "doubao", "account", "sent", checkpoint)
			row := q.completedDeliveryCheckpoint(job, "doubao")
			if !validDelivery(row) {
				t.Fatal("completed body lost")
			}
			if test.keep {
				if row["url"] != public || row["share_status"] != "verified" || row["share_error_code"] != "" || row["share_skipped"] == true || row["optional_stage_error_code"] == "share_not_attempted" {
					t.Fatalf("verified share lost: %v", row)
				}
			} else if row["url"] != "" || row["share_error_code"] != "share_not_attempted" {
				t.Fatalf("unverified share exposed: %v", row)
			}
		})
	}
}

type deliveryPartialPreparationPool struct {
	*recoveryPool
	mode         string
	calls        int
	firstContext context.Context
	freshWindow  bool
}

func (p *deliveryPartialPreparationPool) AcquireContext(ctx context.Context, site string) (*sessions.Lease, error) {
	p.calls++
	p.firstContext = ctx
	partial := &sessions.PreparationError{Site: site, CandidateIDs: []string{"first", "second"}}
	if p.mode != "busy" {
		partial.FailedIDs = []string{"first"}
		partial.FailureCodes = map[string]string{"first": "browser_start_failed"}
	}
	return nil, errors.Join(partial, context.DeadlineExceeded)
}

func (p *deliveryPartialPreparationPool) AcquireContextExcept(ctx context.Context, site string, skip map[string]bool) (*sessions.Lease, error) {
	p.calls++
	p.freshWindow = ctx != p.firstContext && ctx.Err() == nil && skip["first"] && !skip["second"]
	if p.mode == "success" {
		return p.recoveryPool.AcquireContextExcept(ctx, site, skip)
	}
	return nil, &sessions.PreparationError{Site: site, CandidateIDs: []string{"second"}, FailedIDs: []string{"second"}, FailureCodes: map[string]string{"second": "browser_start_failed"}}
}

func TestEventualPartialPreparationFailureGivesRemainingCandidatesFreshWindow(t *testing.T) {
	for _, mode := range []string{"success", "fullfail", "busy"} {
		t.Run(mode, func(t *testing.T) {
			q, calls := webFirstLocalFailureFixture(t, "first", "second")
			pool := &deliveryPartialPreparationPool{recoveryPool: q.Sess.(*recoveryPool), mode: mode}
			q.Sess = pool
			job := deliveryFixtureJob(t, q, "doubao")
			q.run(job.ID)
			current, _ := q.Get(job.ID)
			if mode == "busy" {
				if pool.calls != 1 || current.Status != "queued" || calls.Load() != 0 || current.Results[0]["web_prepare_exhausted"] == true {
					t.Fatalf("pure waiting became failure/retry loop: %+v calls=%d acquisitions=%d", current, calls.Load(), pool.calls)
				}
				return
			}
			if pool.calls != 2 || !pool.freshWindow {
				t.Fatalf("remaining candidate did not receive fresh window: calls=%d fresh=%v", pool.calls, pool.freshWindow)
			}
			row := current.Results[0]
			if current.Status != "succeeded" || !validDelivery(row) {
				t.Fatalf("delivery not completed: %+v", current)
			}
			entries := cloneAttemptList(row["attempts"])
			if len(entries) < 2 || entries[0]["account_id"] != "first" || entries[0]["skipped"] != true {
				t.Fatalf("completed partial failure evidence lost: %v", entries)
			}
			if mode == "success" {
				if calls.Load() != 0 || row["source"] == "official_api" || row["account_id"] != "second" {
					t.Fatalf("remaining webpage displaced by API: %v calls=%d", row, calls.Load())
				}
			} else if calls.Load() != 1 || row["source"] != "official_api" || row["web_prepare_exhausted"] != true || len(entries) != 3 || entries[1]["account_id"] != "second" || entries[1]["skipped"] != true {
				t.Fatalf("complete proof did not permit final API: %v calls=%d", row, calls.Load())
			}
		})
	}
}
