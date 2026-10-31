package jobs

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func fairFixture(t *testing.T) *Queue {
	t.Helper()
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
	t.Setenv("GEO_DELIVERY_HANDOFF_MS", "50")
	q := schedulerFixture()
	q.siteSem = make(chan struct{}, 1)
	q.jobs = make(map[string]*Job)
	q.ch = make(chan string, 32)
	return q
}

func fairResourceRow(site string) map[string]any {
	return map[string]any{"site_id": site, "ok": false, "error_code": "browser_admission_timeout", "web_skipped": true,
		"delivery_pending": true, "retry_state": "retry_wait", "elapsed_ms": int64(15000),
		"attempts": []map[string]any{{"channel": "web", "ok": nil, "skipped": true, "elapsed_ms": int64(15000)}}}
}

func fairJob(q *Queue, id, site string, created time.Time, pending bool) *Job {
	job := &Job{ID: id, Sites: []string{site}, Status: "running", DeliveryPolicy: "eventual", DeadlinePolicy: "web_first", CreatedAt: created.Format(time.RFC3339Nano)}
	if pending {
		job.Status, job.RetryAt = "queued", time.Now().Add(5*time.Minute).Format(time.RFC3339Nano)
		job.Results = []map[string]any{fairResourceRow(site)}
	}
	q.mu.Lock()
	q.jobs[id] = job
	q.mu.Unlock()
	return job
}

type fairGrant struct {
	id      string
	release func()
}

func fairAcquire(q *Queue, parent context.Context, site, id string, out chan<- fairGrant) {
	window, cancel := context.WithTimeout(parent, 2*time.Second)
	defer cancel()
	if release, ok := q.acquireDeliveryBrowserResources(parent, window, site, id, &stageTimings{}); ok {
		out <- fairGrant{id, release}
	}
}

func expectFairGrant(t *testing.T, grants <-chan fairGrant, want string) func() {
	t.Helper()
	select {
	case grant := <-grants:
		if grant.id != want {
			grant.release()
			t.Fatalf("grant=%s want=%s", grant.id, want)
		}
		return grant.release
	case <-time.After(2 * time.Second):
		t.Fatalf("waiting ticket %s never received browser resources", want)
		return nil
	}
}

func TestFairAdmissionSoftTimeoutKeepsAgeAndOldTurn(t *testing.T) {
	q := fairFixture(t)
	t.Setenv("GEO_DELIVERY_HANDOFF_MS", "1000")
	parent := context.WithValue(context.Background(), eventualDeliveryKey{}, true)
	created := time.Now().Add(-time.Hour)
	fairJob(q, "old", "deepseek", created, false)
	fairJob(q, "new", "kimi", time.Now(), false)
	holder, ok := q.acquireBrowserResources(context.Background(), "doubao", "holder", &stageTimings{})
	if !ok {
		t.Fatal("fixture could not occupy global resource")
	}
	defer holder()
	window, cancel := context.WithTimeout(parent, 30*time.Millisecond)
	_, ok = q.acquireDeliveryBrowserResources(parent, window, "deepseek", "old", &stageTimings{})
	cancel()
	if ok {
		t.Fatal("full browser resource unexpectedly granted")
	}
	q.browserMu.Lock()
	ticket := q.fairTickets[fairAdmissionKey{"old", "deepseek"}]
	preserved := ticket != nil && ticket.since.Equal(created) && !ticket.live
	q.browserMu.Unlock()
	if !preserved || len(q.siteGate("deepseek")) != 0 || q.poolActive["deepseek"] != 0 {
		t.Fatal("soft deadline lost ticket order or reserved offline capacity")
	}
	q.mu.Lock()
	q.jobs["old"].Status = "queued"
	q.jobs["old"].RetryAt = time.Now().Add(5 * time.Minute).Format(time.RFC3339Nano)
	q.jobs["old"].Results = []map[string]any{fairResourceRow("deepseek")}
	q.mu.Unlock()
	grants := make(chan fairGrant, 2)
	go fairAcquire(q, parent, "kimi", "new", grants)
	waitForBrowserWaiters(t, q, 1)
	holder()
	// The released slot gives the offline oldest ticket a bounded registration
	// grace and advances its durable retry, without granting it a permit.
	select {
	case id := <-q.ch:
		if id != "old" {
			t.Fatalf("woken=%s want=old", id)
		}
	case <-time.After(time.Second):
		t.Fatal("resource release did not wake the retained old ticket")
	}
	go fairAcquire(q, parent, "deepseek", "old", grants)
	oldRelease := expectFairGrant(t, grants, "old")
	oldRelease()
	newRelease := expectFairGrant(t, grants, "new")
	newRelease()
	newRelease()
	q.browserMu.Lock()
	tickets, waiters := len(q.fairTickets), len(q.browserWaiters)
	q.browserMu.Unlock()
	if tickets != 0 || waiters != 0 || len(q.siteSem) != 0 {
		t.Fatalf("completed turns leaked tickets=%d waiters=%d permits=%d", tickets, waiters, len(q.siteSem))
	}
}

func TestFairAdmissionOfflineTicketHasBoundedGraceAndNoPermit(t *testing.T) {
	q := fairFixture(t)
	parent := context.WithValue(context.Background(), eventualDeliveryKey{}, true)
	fairJob(q, "offline", "deepseek", time.Now().Add(-time.Hour), true)
	q.fairTicketFor("deepseek", "offline")
	fairJob(q, "live", "kimi", time.Now(), false)
	holder, _ := q.acquireBrowserResources(context.Background(), "doubao", "holder", &stageTimings{})
	defer holder()
	grants := make(chan fairGrant, 1)
	go fairAcquire(q, parent, "kimi", "live", grants)
	waitForBrowserWaiters(t, q, 1)
	holder()
	if len(q.siteGate("deepseek")) != 0 || q.poolActive["deepseek"] != 0 {
		t.Fatal("offline ticket took a browser/account permit")
	}
	// No worker ever registers the offline ticket. A newer live request must
	// still proceed after the single grace, even while the ticket is retained.
	release := expectFairGrant(t, grants, "live")
	release()
	q.mu.Lock()
	q.jobs["offline"].Status = "cancelled"
	q.mu.Unlock()
	q.finishFairRun("offline")
	q.browserMu.Lock()
	remaining := len(q.fairTickets)
	q.browserMu.Unlock()
	if remaining != 0 || len(q.siteSem) != 0 {
		t.Fatal("cancelled offline ticket or permit remained")
	}
}

func TestFairAdmissionCancellationDoesNotLeaveTicketsOrPermits(t *testing.T) {
	for i := 0; i < 12; i++ {
		q := fairFixture(t)
		parent, cancel := context.WithCancel(context.WithValue(context.Background(), eventualDeliveryKey{}, true))
		fairJob(q, "cancelled", "kimi", time.Now(), false)
		holder, _ := q.acquireBrowserResources(context.Background(), "doubao", "holder", &stageTimings{})
		done := make(chan struct{})
		go func() {
			release, ok := q.acquireDeliveryBrowserResources(parent, parent, "kimi", "cancelled", &stageTimings{})
			if ok {
				release()
				release()
			}
			close(done)
		}()
		waitForBrowserWaiters(t, q, 1)
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); cancel() }()
		go func() { defer wg.Done(); holder() }()
		wg.Wait()
		<-done
		q.browserMu.Lock()
		tickets, waiters := len(q.fairTickets), len(q.browserWaiters)
		q.browserMu.Unlock()
		if tickets != 0 || waiters != 0 || len(q.siteSem) != 0 || q.poolActive["kimi"] != 0 {
			t.Fatalf("cancel/grant race leaked tickets=%d waiters=%d permits=%d", tickets, waiters, len(q.siteSem))
		}
	}
}

func TestFairAdmissionSameAgeRetainsJobAndSiteAlternation(t *testing.T) {
	q := fairFixture(t)
	created := time.Now().Add(-time.Minute)
	old := fairJob(q, "old", "deepseek", created, false)
	old.Sites = append(old.Sites, "yuanbao")
	fairJob(q, "new", "kimi", created, false)
	holder, _ := q.acquireBrowserResources(context.Background(), "doubao", "holder", &stageTimings{})
	defer holder()
	parent := context.WithValue(context.Background(), eventualDeliveryKey{}, true)
	grants := make(chan fairGrant, 3)
	for i, tc := range []struct{ site, job string }{{"deepseek", "old"}, {"yuanbao", "old"}, {"kimi", "new"}} {
		go fairAcquire(q, parent, tc.site, tc.job, grants)
		waitForBrowserWaiters(t, q, i+1)
	}
	holder()
	for _, want := range []string{"old", "new", "old"} {
		release := expectFairGrant(t, grants, want)
		release()
	}
	if len(q.siteSem) != 0 {
		t.Fatal("same-age fanout leaked a browser permit")
	}
}

func TestFairAdmissionWorkerHandoffOwnsOriginalTokenOnce(t *testing.T) {
	q := fairFixture(t)
	old := fairJob(q, "old", "deepseek", time.Now().Add(-time.Hour), true)
	old.RetryAt = ""
	q.fairTicketFor("deepseek", "old")
	newJob := fairJob(q, "new", "kimi", time.Now(), false)
	newJob.Status = "queued"
	q.enqueued = map[string]bool{"new": true, "old": true}
	if got := q.prioritizeFairQueuedJob("new"); got != "old" {
		t.Fatalf("worker chose %s before eligible older ticket", got)
	}
	select {
	case id := <-q.ch:
		if id != "new" {
			t.Fatalf("original token owner lost: %s", id)
		}
	case <-time.After(time.Second):
		t.Fatal("popped new job token was not returned")
	}
	if got := q.prioritizeFairQueuedJob("new"); got != "new" {
		t.Fatal("offline older ticket repeatedly replaced the same worker token")
	}
	q.mu.Lock()
	retained := q.enqueued["new"]
	q.mu.Unlock()
	if !retained || len(q.siteSem) != 0 {
		t.Fatal("token handoff lost ownership or reserved offline resources")
	}
}

func TestFairAdmissionBlockedOldSiteDoesNotDelayAvailableJob(t *testing.T) {
	q := attemptFixtureQueue(t, "import json,pathlib,sys\nsite=sys.argv[sys.argv.index('--site')+1]\nwith pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(site+'\\n')\nprint(json.dumps({'ok':True,'answer':'valid fixture body','source':'web','answer_origin':'web'}))\n", "account")
	job := deliveryFixtureJob(t, q, "kimi")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_DELIVERY_HANDOFF_MS", "50")
	q.ch = make(chan string, 8)
	holder, _ := q.acquireBrowserResources(context.Background(), "doubao", "holder", &stageTimings{})
	defer holder()
	old := fairJob(q, "blocked-old", "doubao", time.Now().Add(-time.Hour), true)
	old.RetryAt = ""
	q.fairTicketFor("doubao", old.ID)
	if got := q.prioritizeFairQueuedJob(job.ID); got != job.ID {
		t.Fatal("blocked old platform consumed another platform's worker")
	}
	loopDone := make(chan struct{})
	go func() { q.loop(); close(loopDone) }()
	q.enqueueJob(job.ID, time.Now())
	final := waitFairJobStatus(t, q, job.ID, "succeeded")
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if len(final.Results) != 1 || final.Results[0]["answer_origin"] != "web" || string(events) != "kimi\n" {
		t.Fatalf("available platform did not deliver once: status=%s sends=%q", final.Status, events)
	}
	if len(q.ch) != 0 {
		t.Fatal("unrelated capacity release woke the still-blocked old platform")
	}
	q.mu.Lock()
	q.jobs[old.ID].Status = "cancelled"
	q.mu.Unlock()
	q.finishFairRun(old.ID)
	holder()
	close(q.ch)
	<-loopDone
}

func TestFairAdmissionGloballyFullWorkerRegistersOldTicketBeforeRelease(t *testing.T) {
	q := attemptFixtureQueue(t, "import json,pathlib,sys\nsite=sys.argv[sys.argv.index('--site')+1]\nwith pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(site+'\\n')\nprint(json.dumps({'ok':True,'answer':'valid fixture body','source':'web','answer_origin':'web'}))\n", "account")
	newJob := deliveryFixtureJob(t, q, "kimi")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	q.siteSem = make(chan struct{}, 1)
	q.ch = make(chan string, 8)
	holder, _ := q.acquireBrowserResources(context.Background(), "doubao", "long-running-holder", &stageTimings{})
	defer holder()
	oldJob := fairJob(q, "old", "deepseek", time.Now().Add(-time.Hour), true)
	oldJob.RetryAt = ""
	ticket := q.fairTicketFor("deepseek", oldJob.ID)
	// This ticket has already spent its one offline grace in a previous wave.
	// The worker must register it even though a global slot is currently full.
	q.browserMu.Lock()
	ticket.handoffUntil = time.Now().Add(-time.Minute)
	q.browserMu.Unlock()
	loopDone := make(chan struct{})
	go func() { q.loop(); close(loopDone) }()
	q.enqueueJob(newJob.ID, time.Now())
	waitForBrowserWaiters(t, q, 1)
	q.browserMu.Lock()
	registeredOld := len(q.browserWaiters) == 1 && q.browserWaiters[0].job == oldJob.ID && ticket.live
	q.browserMu.Unlock()
	q.mu.Lock()
	newOwned := q.enqueued[newJob.ID] && !q.activeRuns[newJob.ID]
	q.mu.Unlock()
	if !registeredOld || !newOwned {
		t.Fatal("full global capacity lost older live registration or original token ownership")
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "events")); !os.IsNotExist(err) {
		t.Fatal("offline ticket acquired a permit while global capacity was full")
	}
	holder()
	oldFinal := waitFairJobStatus(t, q, oldJob.ID, "succeeded")
	newFinal := waitFairJobStatus(t, q, newJob.ID, "succeeded")
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if string(events) != "deepseek\nkimi\n" || oldFinal.Results[0]["source"] != "web" || newFinal.Results[0]["source"] != "web" || len(q.siteSem) != 0 {
		t.Fatalf("global-pressure turns were repeated or overtaken: %q", events)
	}
	close(q.ch)
	<-loopDone
}

func TestFairAdmissionGloballyFullDoesNotPrioritizeUnavailablePlatform(t *testing.T) {
	for _, reason := range []string{"pool", "site", "cloud"} {
		t.Run(reason, func(t *testing.T) {
			q := fairFixture(t)
			if reason == "cloud" {
				t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
				q.cloudSem = make(chan struct{}, 1)
				q.cloudSem <- struct{}{}
			}
			old := fairJob(q, "old", "deepseek", time.Now().Add(-time.Hour), true)
			old.RetryAt = ""
			q.fairTicketFor("deepseek", old.ID)
			newJob := fairJob(q, "new", "kimi", time.Now(), false)
			newJob.Status = "queued"
			q.enqueued = map[string]bool{newJob.ID: true}
			q.siteSem <- struct{}{}
			switch reason {
			case "pool":
				q.poolActive = map[string]int{"deepseek": 1}
			case "site":
				q.siteGate("deepseek") <- struct{}{}
			}
			if got := q.prioritizeFairQueuedJob(newJob.ID); got != newJob.ID {
				t.Fatalf("global-full exception let unavailable %s consume another worker", reason)
			}
			if !q.enqueued[newJob.ID] || len(q.ch) != 0 {
				t.Fatal("unchosen blocked ticket changed original token ownership")
			}
		})
	}
}

func waitFairJobStatus(t *testing.T, q *Queue, id, status string) *Job {
	t.Helper()
	until := time.Now().Add(3 * time.Second)
	for time.Now().Before(until) {
		job, _ := q.Get(id)
		if job != nil && job.Status == status {
			return job
		}
		time.Sleep(time.Millisecond)
	}
	job, _ := q.Get(id)
	t.Fatalf("job %s failed to reach %s: %+v", id, status, job)
	return nil
}

func TestFairAdmissionWakeSurvivesActiveRoundAndEnqueuesOnce(t *testing.T) {
	q := fairFixture(t)
	job := fairJob(q, "old", "deepseek", time.Now().Add(-time.Hour), true)
	q.fairTicketFor("deepseek", job.ID)
	q.activeRuns = map[string]bool{job.ID: true}
	q.wakeFairResourceWaiters()
	q.mu.Lock()
	wake := q.fairWakePending[job.ID]
	delete(q.activeRuns, job.ID)
	q.mu.Unlock()
	if !wake || len(q.ch) != 0 {
		t.Fatal("active round lost its release wake or was concurrently enqueued")
	}
	q.finishFairRun(job.ID)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); q.wakeFairResourceWaiters() }()
	}
	wg.Wait()
	select {
	case id := <-q.ch:
		if id != job.ID {
			t.Fatalf("woken wrong job %s", id)
		}
	case <-time.After(time.Second):
		t.Fatal("release wake disappeared while active round completed")
	}
	if len(q.ch) != 0 {
		t.Fatal("repeated capacity wake enqueued the job more than once")
	}
	current, _ := q.Get(job.ID)
	if !deliveryDue(current, time.Now()) {
		t.Fatal("resource wake kept the old five-minute backoff")
	}
}

func TestFairAdmissionExpeditesResourceSiblingButPreservesOperatorWait(t *testing.T) {
	q := fairFixture(t)
	job := fairJob(q, "mixed", "deepseek", time.Now().Add(-time.Hour), true)
	job.Sites = append(job.Sites, "kimi")
	job.Results = append(job.Results, map[string]any{"site_id": "kimi", "ok": false, "error_code": "platform_auth_unavailable", "retry_state": "operator_wait"})
	before := job.RetryAt
	q.fairTicketFor("deepseek", job.ID)
	q.wakeFairResourceWaiters()
	current, _ := q.Get(job.ID)
	if current.RetryAt == before || !deliveryDue(current, time.Now()) || stringField(current.Results[1], "delivery_retry_at") != before {
		t.Fatal("resource sibling stayed blocked or operator backoff was advanced")
	}
	legacy := &Job{ID: "legacy", Status: "queued"}
	q.mu.Lock()
	q.jobs[legacy.ID] = legacy
	q.mu.Unlock()
	if got := q.prioritizeFairQueuedJob(legacy.ID); got != legacy.ID {
		t.Fatal("fair delivery tickets changed legacy worker scheduling")
	}
}

func TestFairAdmissionRestoresOnlyKnownUnexecutedAge(t *testing.T) {
	q := fairFixture(t)
	created := time.Now().Add(-time.Hour)
	expectedOld := make(map[string]bool)
	for _, tc := range []struct {
		id         string
		change     func(map[string]any)
		restoreAge bool
	}{
		{"skipped", func(map[string]any) {}, true},
		{"actual", func(row map[string]any) {
			row["attempts"] = []map[string]any{{"channel": "web", "skipped": false, "ok": false}}
		}, false},
		{"truncated", func(row map[string]any) { row["attempts_truncated"] = true }, false},
		{"dropped", func(row map[string]any) { row["attempts_dropped"] = 1 }, false},
	} {
		job := fairJob(q, tc.id, "kimi", created, true)
		tc.change(job.Results[0])
		expectedOld[tc.id] = tc.restoreAge
	}
	q.restoreFairAdmissionTickets()
	q.browserMu.Lock()
	defer q.browserMu.Unlock()
	for key, ticket := range q.fairTickets {
		wantOld := expectedOld[key.job]
		if ticket.since.Equal(created) != wantOld {
			t.Fatalf("historical actual/unknown evidence got incorrect wait age: %s", key.job)
		}
	}
	if len(q.fairTickets) != 4 || len(q.siteSem) != 0 {
		t.Fatal("restart failed to restore resource wait tickets without permits")
	}
}

func TestFairAdmissionAPIOffResourceWaitAutomaticallyDelivers(t *testing.T) {
	q := attemptFixtureQueue(t, "import json,pathlib\nwith pathlib.Path('events').open('a',encoding='utf-8') as f:f.write('web\\n')\nprint(json.dumps({'ok':True,'answer':'valid web body','source':'web','answer_origin':'web'}))\n", "account")
	job := deliveryFixtureJob(t, q, "doubao")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_DOUBAO_API_FALLBACK", "0")
	t.Setenv("GEO_DELIVERY_ADMISSION_SEC", "1")
	t.Setenv("GEO_DELIVERY_RESOURCE_RETRY_SEC", "1")
	q.siteSem = make(chan struct{}, 1)
	q.ch = make(chan string, 8)
	holder, _ := q.acquireBrowserResources(context.Background(), "kimi", "holder", &stageTimings{})
	defer holder()
	q.run(job.ID)
	pending, _ := q.Get(job.ID)
	if pending.Status != "queued" || pending.WaitReason != "browser_admission_timeout" || pending.Results[0]["retry_state"] != "retry_wait" || time.Until(mustFairRetryTime(t, pending)) > 1100*time.Millisecond {
		t.Fatalf("API-off capacity wait became manual/provider backoff: %+v", pending)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "events")); !os.IsNotExist(err) {
		t.Fatal("admission-only round started an actual webpage call")
	}
	loopDone := make(chan struct{})
	go func() { q.loop(); close(loopDone) }()
	holder()
	final := waitFairJobStatus(t, q, job.ID, "succeeded")
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if final.RetryCount != 1 || final.Results[0]["source"] != "web" || !validDelivery(final.Results[0]) || strings.Count(string(events), "web\n") != 1 || len(q.siteSem) != 0 {
		t.Fatal("API-off resource wake did not deliver one genuine web result")
	}
	close(q.ch)
	<-loopDone
}

func mustFairRetryTime(t *testing.T, job *Job) time.Time {
	t.Helper()
	at, err := time.Parse(time.RFC3339Nano, job.RetryAt)
	if err != nil {
		t.Fatal(err)
	}
	return at
}
