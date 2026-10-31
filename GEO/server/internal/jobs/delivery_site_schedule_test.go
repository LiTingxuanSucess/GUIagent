package jobs

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func scheduleResult(job *Job, site string) map[string]any {
	for _, row := range job.Results {
		if internalSiteID(stringField(row, "site_id")) == internalSiteID(site) {
			return row
		}
	}
	return nil
}

func TestDeliverySiteScheduleMixedWaitOnlyRunsResourceSibling(t *testing.T) {
	q := attemptFixtureQueue(t, "import json,pathlib,sys\nsite=sys.argv[sys.argv.index('--site')+1]\nwith pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(site+'\\n')\nprint(json.dumps({'ok':True,'answer':'valid web body','source':'web','answer_origin':'web'}))\n", "account")
	job := deliveryFixtureJob(t, q, "deepseek", "yuanbao", "doubao")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	operatorAt := time.Now().Add(5 * time.Minute).Format(time.RFC3339Nano)
	operatorAttempts := []map[string]any{{"channel": "web", "skipped": true, "error_code": "platform_auth_unavailable"}}
	operator := map[string]any{"site_id": "deepseek", "ok": false, "error_code": "platform_auth_unavailable", "retry_state": "operator_wait", "delivery_pending": true,
		"api_error_code": "api_auth_failed", "attempts": operatorAttempts}
	completed := map[string]any{"site_id": "doubao", "ok": true, "answer": "preserved existing body", "source": "web", "share_url": "https://www.doubao.com/thread/public-fixture", "share_status": "verified"}
	job.RetryAt, job.RetryCount = operatorAt, 22
	job.Results = []map[string]any{operator, fairResourceRow("yuanbao"), completed}
	q.run(job.ID)
	final, _ := q.Get(job.ID)
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	remaining := scheduleResult(final, "deepseek")
	if final.Status != "queued" || final.CompletedSites != 2 || string(events) != "yuanbao\n" || final.RetryAt != operatorAt || final.WaitReason != "api_auth_failed" {
		t.Fatalf("operator wait blocked resource sibling or was retried: status=%s completed=%d sends=%q retry=%s reason=%s", final.Status, final.CompletedSites, events, final.RetryAt, final.WaitReason)
	}
	if stringField(remaining, "delivery_retry_at") != operatorAt || !reflect.DeepEqual(cloneAttemptList(remaining["attempts"]), operatorAttempts) || evidenceCount(remaining["delivery_retry_count"]) != 22 {
		t.Fatal("skipped operator platform changed its deadline or attempt evidence")
	}
	if scheduleResult(final, "doubao")["answer"] != completed["answer"] || scheduleResult(final, "doubao")["share_url"] != completed["share_url"] || scheduleResult(final, "yuanbao")["source"] != "web" {
		t.Fatal("partial delivery did not preserve prior webpage body/share")
	}
	if _, mutated := operator["delivery_retry_at"]; mutated {
		t.Fatal("schedule initialization mutated an already published result map")
	}
}

func TestDeliverySiteScheduleCapacityRetriesNeverPollOperatorSibling(t *testing.T) {
	q := attemptFixtureQueue(t, "import json,pathlib,sys\nsite=sys.argv[sys.argv.index('--site')+1]\nwith pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(site+'\\n')\nprint(json.dumps({'ok':True,'answer':'valid web body','source':'web','answer_origin':'web'}))\n", "account")
	job := deliveryFixtureJob(t, q, "deepseek", "yuanbao")
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_DELIVERY_ADMISSION_SEC", "1")
	t.Setenv("GEO_DELIVERY_RESOURCE_RETRY_SEC", "1")
	operatorAt := time.Now().Add(5 * time.Minute).Format(time.RFC3339Nano)
	operatorAttempts := []map[string]any{{"channel": "api", "ok": false, "error_code": "api_quota_exhausted", "skipped": false}}
	job.RetryAt, job.RetryCount = operatorAt, 22
	job.Results = []map[string]any{{"site_id": "deepseek", "ok": false, "error_code": "platform_auth_unavailable", "api_error_code": "api_quota_exhausted", "retry_state": "operator_wait", "delivery_pending": true, "attempts": operatorAttempts}, fairResourceRow("yuanbao")}
	q.siteSem = make(chan struct{}, 1)
	holder, _ := q.acquireBrowserResources(context.Background(), "kimi", "long-holder", &stageTimings{})
	defer holder()
	for wave := 0; wave < 2; wave++ {
		q.run(job.ID)
		pending, _ := q.Get(job.ID)
		if pending.Status != "queued" || pending.WaitReason != "browser_admission_timeout" || stringField(scheduleResult(pending, "deepseek"), "delivery_retry_at") != operatorAt || evidenceCount(scheduleResult(pending, "deepseek")["delivery_retry_count"]) != 22 {
			t.Fatal("capacity wave polled/reclassified an operator platform")
		}
		if evidenceCount(scheduleResult(pending, "yuanbao")["delivery_retry_count"]) != 0 {
			t.Fatal("browser-only admission wait increased provider retry count")
		}
		if _, err := os.Stat(filepath.Join(q.GeoRoot, "events")); !os.IsNotExist(err) {
			t.Fatal("capacity-only wave sent a question")
		}
		q.patch(job.ID, func(j *Job) {
			past := time.Now().Add(-time.Second).Format(time.RFC3339Nano)
			scheduleResult(j, "yuanbao")["delivery_retry_at"] = past
			j.RetryAt = past
		})
	}
	holder()
	q.run(job.ID)
	final, _ := q.Get(job.ID)
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	if final.Status != "queued" || final.CompletedSites != 1 || final.RetryAt != operatorAt || string(events) != "yuanbao\n" || !reflect.DeepEqual(cloneAttemptList(scheduleResult(final, "deepseek")["attempts"]), operatorAttempts) {
		t.Fatal("resource sibling failed to deliver exactly once while permanent failure remained parked")
	}
}

func TestDeliverySiteScheduleWakePreservesOtherPlatformDeadlines(t *testing.T) {
	for _, code := range []string{"api_auth_failed", "api_quota_exhausted", "api_risk_control", "api_request_failed", "recovery_target_missing"} {
		t.Run(code, func(t *testing.T) {
			q := fairFixture(t)
			job := fairJob(q, "mixed", "deepseek", time.Now().Add(-time.Hour), true)
			job.Sites = append(job.Sites, "kimi")
			operatorAt := job.RetryAt
			row := map[string]any{"site_id": "kimi", "ok": false, "error_code": "platform_busy", "api_error_code": code, "delivery_pending": true}
			if code == "recovery_target_missing" {
				row["error_code"], row["retry_state"] = code, "awaiting_confirmation"
				delete(row, "api_error_code")
			}
			job.Results = append(job.Results, row)
			q.fairTicketFor("deepseek", job.ID)
			q.wakeFairResourceWaiters()
			current, _ := q.Get(job.ID)
			if !deliveryResourceSiteDue(current, "deepseek", time.Now()) || stringField(scheduleResult(current, "kimi"), "delivery_retry_at") != operatorAt || deliverySiteDue(scheduleResult(current, "kimi"), time.Now()) {
				t.Fatalf("capacity release advanced another platform's %s deadline", code)
			}
			if _, changed := row["delivery_retry_at"]; changed {
				t.Fatal("wake mutated the prior snapshot")
			}
			select {
			case id := <-q.ch:
				if id != job.ID {
					t.Fatal("woken wrong job")
				}
			case <-time.After(time.Second):
				t.Fatal("mixed job's ready resource platform stayed asleep")
			}
		})
	}
}

func TestDeliverySiteScheduleRestartPreservesOriginalIDAndTimes(t *testing.T) {
	q := attemptFixtureQueue(t, "raise AssertionError('read-only restart fixture must not execute')\n", "account")
	job := deliveryFixtureJob(t, q, "deepseek", "yuanbao", "doubao")
	now := time.Now()
	resourceAt, operatorAt := now.Add(10*time.Second).Format(time.RFC3339Nano), now.Add(5*time.Minute).Format(time.RFC3339Nano)
	resource := fairResourceRow("yuanbao")
	resource["delivery_retry_at"], resource["delivery_retry_count"] = resourceAt, 0
	job.RetryAt, job.RetryCount = resourceAt, 23
	job.Results = []map[string]any{{"site_id": "deepseek", "ok": false, "error_code": "platform_auth_unavailable", "retry_state": "operator_wait", "delivery_pending": true, "delivery_retry_at": operatorAt, "delivery_retry_count": 1}, resource,
		{"site_id": "doubao", "ok": true, "answer": "preserved body", "source": "web"}}
	q.save(job)
	q.jobs = map[string]*Job{}
	_, queued, interrupted := q.load()
	restored, _ := q.Get(job.ID)
	if len(queued) != 1 || len(interrupted) != 0 || restored.ID != job.ID || restored.RequestID != job.RequestID || restored.RetryAt != resourceAt || stringField(scheduleResult(restored, "deepseek"), "delivery_retry_at") != operatorAt || scheduleResult(restored, "doubao")["answer"] != "preserved body" {
		t.Fatal("restart changed ID, successful body, or independent platform backoffs")
	}
	if deliveryDue(restored, now.Add(9*time.Second)) || !deliveryDue(restored, now.Add(11*time.Second)) || deliverySiteDue(scheduleResult(restored, "deepseek"), now.Add(11*time.Second)) {
		t.Fatal("reloaded platform timing coupled resource and operator waits")
	}
	q.restoreFairAdmissionTickets()
	q.wakeFairResourceWaiters()
	woken, _ := q.Get(job.ID)
	if !deliveryDue(woken, time.Now()) || stringField(scheduleResult(woken, "deepseek"), "delivery_retry_at") != operatorAt {
		t.Fatal("post-restart resource wake advanced the operator platform")
	}
}

func TestDeliverySiteScheduleInvalidLegacyOperatorDeadlineIsBounded(t *testing.T) {
	clearOfficialAPIKeys(t)
	now := time.Now()
	for _, at := range []string{"", "invalid"} {
		job := &Job{RetryAt: at, RetryCount: 2, Results: []map[string]any{{"site_id": "deepseek", "ok": false, "error_code": "platform_auth_unavailable", "retry_state": "operator_wait"}, fairResourceRow("yuanbao")}}
		ensureDeliverySiteSchedule(job, now)
		operator, _ := deliverySiteRetryTime(job.Results[0])
		if operator.Sub(now) < 30*time.Second || !deliverySiteDue(job.Results[1], now) || deliverySiteDue(job.Results[0], now) {
			t.Fatal("invalid old operator deadline caused immediate polling or blocked resource sibling")
		}
	}
}

func TestDeliverySiteScheduleHistoricWebSkipWithPermanentAPIRejectStaysParked(t *testing.T) {
	for _, code := range []string{"api_auth_failed", "api_quota_exhausted", "api_risk_control"} {
		t.Run(code, func(t *testing.T) {
			q := fairFixture(t)
			job := fairJob(q, "historical-mixed", "deepseek", time.Now().Add(-time.Hour), true)
			job.Sites = append(job.Sites, "yuanbao")
			row := job.Results[0]
			row["api_error_code"] = code
			row["attempts"] = []map[string]any{{"channel": "web", "skipped": true}, {"channel": "api", "ok": false, "skipped": false, "error_code": code}}
			job.Results = append(job.Results, fairResourceRow("yuanbao"))
			before := job.RetryAt
			q.fairTicketFor("deepseek", job.ID)
			q.fairTicketFor("yuanbao", job.ID)
			q.wakeFairResourceWaiters()
			current, _ := q.Get(job.ID)
			permanent := scheduleResult(current, "deepseek")
			state, reason := deliveryWaitState("deepseek", permanent)
			if resourceAdmissionResult(permanent) || deliverySiteDue(permanent, time.Now()) || stringField(permanent, "delivery_retry_at") != before || state != "operator_wait" || reason != code || !deliveryResourceSiteDue(current, "yuanbao", time.Now()) {
				t.Fatal("historical actual API refusal was turned into a short capacity retry")
			}
		})
	}
}

func TestDeliverySiteScheduleResourceWavesDoNotInflateProviderBackoff(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DEEPSEEK_API_KEY", "fixture-only")
	t.Setenv("GEO_DELIVERY_RETRY_BASE_SEC", "30")
	t.Setenv("GEO_DELIVERY_RETRY_MAX_SEC", "300")
	now := time.Now()
	previous := fairResourceRow("deepseek")
	for wave := 0; wave < 10; wave++ {
		next := fairResourceRow("deepseek")
		scheduleDeliverySiteRetry("deepseek", previous, next, 100+wave, now)
		if evidenceCount(next["delivery_retry_count"]) != 0 {
			t.Fatal("capacity retry consumed provider backoff progression")
		}
		previous = next
	}
	provider := map[string]any{"site_id": "deepseek", "ok": false, "error_code": "platform_busy", "api_error_code": "api_request_failed"}
	scheduleDeliverySiteRetry("deepseek", previous, provider, 110, now)
	at, _ := deliverySiteRetryTime(provider)
	if evidenceCount(provider["delivery_retry_count"]) != 1 || at.Sub(now) != 30*time.Second {
		t.Fatal("first real provider failure inherited browser capacity retry count")
	}
	next := map[string]any{"site_id": "deepseek", "ok": false, "error_code": "platform_busy", "api_error_code": "api_request_failed"}
	scheduleDeliverySiteRetry("deepseek", provider, next, 111, now)
	at, _ = deliverySiteRetryTime(next)
	if evidenceCount(next["delivery_retry_count"]) != 2 || at.Sub(now) != 60*time.Second {
		t.Fatal("provider failures lost their independent exponential backoff")
	}
}
